#!/usr/bin/env python3
"""Cursor reconciler.

Cursor exposes NO token data locally (the cursor-agent CLI keeps none, and on an
SSO-managed seat there is no admin key and no dashboard CSV export). The only truth
source is the authenticated usage dashboard. Rather than automate SSO (which the
build guide flags as the last resort), this collector ingests data you hand it:

  1. self_tally        -- one estimate row per Cursor launch (activity floor, 0 tokens).
  2. ingest_exports    -- parse usage JSON you save from the dashboard's Network tab
                          into ~/.tokometer/cursor-exports/*.json. Handles the
                          /teams/filtered-usage-events shape (usageEvents[] with
                          tokenUsage). Writes EXACT rows and supersedes the
                          self-tally estimates for the covered window.
  3. manual override   -- ~/.tokometer/cursor-exports/manual-*.json
                          {"month":"2026-06","spend_usd":12.34[,"email":"..."]}
                          for when you only have the headline dollar figure.
  4. admin_reconcile   -- TODO: only if a CURSOR_ADMIN_KEY (crsr_..., admin:*) ever
                          becomes available -> POST /teams/filtered-usage-events.

Capture recipe (no SSO automation): open cursor.com/dashboard/usage, DevTools ->
Network, pick the usage XHR, right-click -> Save response as
~/.tokometer/cursor-exports/usage-YYYY-MM.json.
"""
import os
import sys
import csv
import json
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "cursor"
PROVIDER = "cursor"
EXPORT_DIR = os.path.join(ledger.TOKOMETER_HOME, "cursor-exports")
EMAIL = os.environ.get("CURSOR_EMAIL")  # optional filter for multi-user exports


def self_tally(con):
    launches = con.execute(
        "SELECT id, ts, model FROM launch WHERE harness IN ('cursor','cursor-agent')"
    ).fetchall()
    rows = [{
        "uid": f"{HARNESS}:launch:{lid}",
        "ts": ledger.normalize_iso(ts),
        "harness": HARNESS, "provider": PROVIDER, "model": model,
        "session_id": None, "request_id": None,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "reasoning_tokens": 0, "credits": 0,
        "cost_usd": 0.0, "source": "self-tally", "confidence": "estimate",
        "raw_ref": f"launch:{lid}",
    } for lid, ts, model in launches]
    return ledger.insert_usage(con, rows)  # ignore-on-conflict; never clobbers exact


def _events_from(doc):
    """Pull the usage-event list out of whatever wrapper the export uses."""
    if isinstance(doc, list):
        return doc
    for key in ("usageEvents", "events", "data", "items"):
        v = doc.get(key)
        if isinstance(v, list):
            return v
    return []


def ingest_exports(con):
    inserted = 0
    # CSV exports from the dashboard's "Export" button
    for path in sorted(glob.glob(os.path.join(EXPORT_DIR, "*.csv"))):
        inserted += _ingest_csv(con, path)

    # JSON exports (Network-tab capture) and manual overrides
    for path in sorted(glob.glob(os.path.join(EXPORT_DIR, "*.json"))):
        base = os.path.basename(path)
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            print(f"[{HARNESS}] could not parse {base}; skipping", file=sys.stderr)
            continue

        if base.startswith("manual-"):
            inserted += _ingest_manual(con, path, doc)
            continue

        rows = []
        for ev in _events_from(doc):
            email = ev.get("userEmail") or ev.get("email")
            if EMAIL and email and email.lower() != EMAIL.lower():
                continue
            tok = ev.get("tokenUsage") or {}
            ts_raw = ev.get("timestamp") or ev.get("date") or ev.get("ts")
            try:
                ts_ms = int(ts_raw)
                ts = ledger.iso_utc(epoch_ms=ts_ms)
            except (TypeError, ValueError):
                ts = ledger.normalize_iso(str(ts_raw)) if ts_raw else ledger.iso_utc()
            charged = ev.get("chargedCents")
            cents = charged if charged is not None else tok.get("totalCents", 0)
            i = int(tok.get("inputTokens") or 0)
            o = int(tok.get("outputTokens") or 0)
            cr = int(tok.get("cacheReadTokens") or 0)
            cw = int(tok.get("cacheWriteTokens") or 0)
            rows.append({
                "uid": f"{HARNESS}:evt:{ts_raw}:{ev.get('model')}:{i}:{o}",
                "ts": ts, "harness": HARNESS, "provider": PROVIDER,
                "model": ev.get("model"),
                "session_id": None, "request_id": ev.get("requestId"),
                "input_tokens": i, "output_tokens": o,
                "cache_read_tokens": cr, "cache_write_tokens": cw,
                "reasoning_tokens": 0, "credits": 0,
                "cost_usd": round((cents or 0) / 100.0, 6),
                "source": "dashboard-export", "confidence": "exact",
                "raw_ref": f"{path}",
            })
        if rows:
            n = ledger.insert_usage(con, rows, on_conflict="replace")
            _supersede_self_tally(con, rows)
            inserted += n
            print(f"[{HARNESS}] {base}: {len(rows)} events, {n} written", file=sys.stderr)
    return inserted


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _ingest_csv(con, path):
    """Parse the dashboard 'Export' CSV (team-usage-events-*.csv).

    Columns: Date, User, Cloud Agent ID, Automation ID, Kind, Model, Max Mode,
      Input (w/ Cache Write), Input (w/o Cache Write), Cache Read, Output Tokens,
      Total Tokens, Cost   (Cost is in dollars; tokens verified: input_no_cw +
      cache_read + output = total).
    """
    base = os.path.basename(path)
    rows = []
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                email = (r.get("User") or "").strip()
                if EMAIL and email and email.lower() != EMAIL.lower():
                    continue
                date = (r.get("Date") or "").strip()
                model = (r.get("Model") or "").strip() or None
                i = _int(r.get("Input (w/o Cache Write)"))
                cw = _int(r.get("Input (w/ Cache Write)"))
                cr = _int(r.get("Cache Read"))
                o = _int(r.get("Output Tokens"))
                cost = r.get("Cost")
                try:
                    cost = float(cost)
                except (TypeError, ValueError):
                    cost = 0.0
                rows.append({
                    "uid": f"{HARNESS}:csv:{date}:{model}:{i}:{cr}:{o}",
                    "ts": ledger.normalize_iso(date),
                    "harness": HARNESS, "provider": PROVIDER, "model": model,
                    "session_id": r.get("Cloud Agent ID") or None,
                    "request_id": r.get("Automation ID") or None,
                    "input_tokens": i, "output_tokens": o,
                    "cache_read_tokens": cr, "cache_write_tokens": cw,
                    "reasoning_tokens": 0, "credits": 0,
                    "cost_usd": cost,
                    "source": "dashboard-export", "confidence": "exact",
                    "raw_ref": f"{path}",
                })
    except OSError:
        print(f"[{HARNESS}] could not read {base}; skipping", file=sys.stderr)
        return 0
    if not rows:
        return 0
    n = ledger.insert_usage(con, rows, on_conflict="replace")
    _supersede_self_tally(con, rows)
    print(f"[{HARNESS}] {base}: {len(rows)} events, {n} written", file=sys.stderr)
    return n


def _ingest_manual(con, path, doc):
    month = doc.get("month")          # 'YYYY-MM'
    spend = doc.get("spend_usd")
    if not month or spend is None:
        print(f"[{HARNESS}] manual file {os.path.basename(path)} needs month + spend_usd",
              file=sys.stderr)
        return 0
    ts = f"{month}-01T12:00:00Z"
    row = [{
        "uid": f"{HARNESS}:manual:{month}",
        "ts": ts, "harness": HARNESS, "provider": PROVIDER,
        "model": doc.get("model"), "session_id": None, "request_id": None,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "reasoning_tokens": 0, "credits": 0,
        "cost_usd": float(spend),
        "source": "manual", "confidence": "estimate",
        "raw_ref": f"{path}",
    }]
    return ledger.insert_usage(con, row, on_conflict="replace")


def _supersede_self_tally(con, rows):
    """Drop self-tally estimates inside the window covered by an exact export."""
    times = [r["ts"] for r in rows]
    lo, hi = min(times), max(times)
    con.execute(
        "DELETE FROM usage WHERE harness=? AND source='self-tally' "
        "AND ts BETWEEN ? AND ?", (HARNESS, lo, hi))
    con.commit()


def admin_reconcile(con):
    key = os.environ.get("CURSOR_ADMIN_KEY")
    if not key:
        return 0
    # TODO: POST https://api.cursor.com/teams/filtered-usage-events
    #   -u {key}:  body {startDate,endDate,email,page,pageSize}; paginate;
    #   map usageEvents[].tokenUsage -> exact rows (same mapping as ingest_exports).
    print(f"[{HARNESS}] CURSOR_ADMIN_KEY present but admin reconcile not implemented",
          file=sys.stderr)
    return 0


def harvest():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    con = ledger.connect()
    tallied = self_tally(con)
    ingested = ingest_exports(con)
    admin_reconcile(con)
    con.close()
    print(f"[{HARNESS}] {tallied} self-tally, {ingested} from exports", file=sys.stderr)
    return tallied + ingested


if __name__ == "__main__":
    harvest()
