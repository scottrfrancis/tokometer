"""Post-harvest degradation classifier — labels model downgrades with a mechanism.

The four mechanisms (see the routometer research corpus) plus the crash-triggered
compound: a client OOM tears down the cache boundary and the next turn re-routes
cheap — a client failure masquerading as a server downgrade. The classifier walks
consecutive copilot ledger rows, finds tier drops, and attributes each one from
nearby events:

    crash-family event within the window   -> client-oom-reroute
    request_failure / rate-limit nearby    -> quota
    otherwise                              -> downroute   (task-complexity routing)

Emits one `model_downgrade` event per drop (uid keyed to the downgraded request,
so re-runs are idempotent). Runs after harvest; stdlib-only; Python 3.11-compatible.
"""
import json
import datetime as dt

CRASH_KINDS = ("worker_oom", "v8_oom", "exthost_crash", "exthost_restart")
WINDOW_MIN = 10

# capability tiers, low to high; matched by substring, first hit wins
_TIERS = (
    ("mini", 0),
    ("haiku", 1),
    ("gpt-4o", 2),
    ("sonnet", 3),
    ("codex", 4),
    ("gpt-5", 4),
    ("opus", 5),
)


def model_rank(model):
    """Capability tier for a model id; unknown models rank mid-tier."""
    m = (model or "").lower()
    for needle, rank in _TIERS:
        if needle in m:
            return rank
    return 2


def _parse_ts(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def classify(con, window_minutes=WINDOW_MIN):
    """Label every model downgrade in the copilot ledger. Returns rows inserted."""
    usage = con.execute(
        "SELECT uid, ts, model FROM usage"
        " WHERE harness='copilot' AND model IS NOT NULL ORDER BY ts, id").fetchall()
    events = con.execute(
        "SELECT ts, kind FROM event WHERE kind != 'model_downgrade'").fetchall()

    window = dt.timedelta(minutes=window_minutes)
    inserted = 0
    for (prev_uid, prev_ts, prev_model), (uid, ts, model) in zip(usage, usage[1:]):
        if model_rank(model) >= model_rank(prev_model):
            continue
        t = _parse_ts(ts)
        nearby = [k for (ets, k) in events if abs(t - _parse_ts(ets)) <= window]
        if any(k in CRASH_KINDS for k in nearby):
            mechanism = "client-oom-reroute"
        elif any(k in ("request_failure", "quota") for k in nearby):
            mechanism = "quota"
        else:
            mechanism = "downroute"
        detail = json.dumps({"from": prev_model, "to": model,
                             "prev_uid": prev_uid, "uid": uid})
        cur = con.execute(
            "INSERT OR IGNORE INTO event"
            " (uid, ts, harness, kind, mechanism, detail, source)"
            " VALUES (?, ?, 'copilot', 'model_downgrade', ?, ?, 'classifier')",
            (f"mech:{uid}", ts, mechanism, detail))
        inserted += cur.rowcount
    con.commit()
    return inserted


def main():
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from lib import ledger
    con = ledger.connect()
    n = classify(con)
    con.close()
    print(f"[mechanisms] classified: {n} new model_downgrade events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
