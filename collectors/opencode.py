#!/usr/bin/env python3
"""OpenCode harvester.

Source: opencode.db `message` table. Each assistant message stores a JSON `data`
blob with exact per-message token counts. Verified shape (opencode 1.2+):
  data.tokens = {total, input, output, reasoning, cache:{read,write}}
  data.cost, data.modelID, data.providerID, data.time.created (epoch ms),
  data.path.cwd

Granularity: per-message, exact. cost is frequently 0 (local models) and is
recorded as-is; recompute later from a price table if cloud providers appear.
"""
import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "opencode"
DB_CANDIDATES = [
    "~/.local/share/opencode/opencode.db",
    "~/.local/state/opencode/opencode.db",
]


def find_db():
    if os.environ.get("OPENCODE_DB"):
        return os.path.expanduser(os.environ["OPENCODE_DB"])
    for c in DB_CANDIDATES:
        p = os.path.expanduser(c)
        if os.path.exists(p):
            return p
    return None


def harvest():
    src = find_db()
    if not src:
        print(f"[{HARNESS}] no opencode.db found; skipping", file=sys.stderr)
        return 0

    state = ledger.load_state(HARNESS)
    last = state.get("last_time_created", 0)

    oc = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    oc.execute("PRAGMA busy_timeout = 30000;")
    cur = oc.execute(
        "SELECT id, session_id, time_created, data FROM message "
        "WHERE time_created > ? ORDER BY time_created",
        (last,),
    )

    rows = []
    high = last
    for mid, sid, tcreated, data_json in cur:
        high = max(high, tcreated or 0)
        try:
            d = json.loads(data_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("role") != "assistant":
            continue
        tok = d.get("tokens") or {}
        cache = tok.get("cache") or {}
        created = (d.get("time") or {}).get("created") or tcreated
        rows.append({
            "uid": f"{HARNESS}:{mid}",
            "ts": ledger.iso_utc(epoch_ms=created),
            "harness": HARNESS,
            "provider": d.get("providerID"),
            "model": d.get("modelID"),
            "session_id": sid,
            "request_id": None,
            "input_tokens": int(tok.get("input") or 0),
            "output_tokens": int(tok.get("output") or 0),
            "cache_read_tokens": int(cache.get("read") or 0),
            "cache_write_tokens": int(cache.get("write") or 0),
            "reasoning_tokens": int(tok.get("reasoning") or 0),
            "cost_usd": float(d.get("cost") or 0.0),
            "source": "session-file",
            "confidence": "exact",
            "raw_ref": f"{src}#message:{mid}",
            "cwd": (d.get("path") or {}).get("cwd"),
        })
    oc.close()

    con = ledger.connect()
    inserted = ledger.insert_usage(con, rows)
    con.close()

    ledger.save_state(HARNESS, {"last_time_created": high})
    print(f"[{HARNESS}] {len(rows)} read, {inserted} new (high-water {high})", file=sys.stderr)
    return inserted


if __name__ == "__main__":
    harvest()
