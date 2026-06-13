#!/usr/bin/env python3
"""GitHub Copilot CLI harvester.

Source: ~/.copilot/session-state/<uuid>/events.jsonl. Verified shape (CLI v1.0.61):
each `assistant.message` event carries data.{model, outputTokens, requestId} with a
top-level ISO `timestamp`. This CLI version does NOT emit input/cache tokens locally
(no `session.shutdown`/`modelMetrics` block), so totals are OUTPUT-ONLY and every row
is `estimate`. The authoritative input/cache numbers require the org metrics API.
"""
import os
import sys
import json
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "copilot"
PROVIDER = "github"
SESSION_GLOB = os.path.expanduser(
    os.environ.get("COPILOT_EVENTS_GLOB", "~/.copilot/session-state/*/events.jsonl")
)


def harvest():
    files = sorted(glob.glob(SESSION_GLOB))
    if not files:
        print(f"[{HARNESS}] no copilot session logs; skipping", file=sys.stderr)
        return 0

    state = ledger.load_state(HARNESS)
    seen_mtime = state.get("file_mtime", {})
    new_mtime = dict(seen_mtime)

    rows = []
    read = 0
    for path in files:
        mtime = os.path.getmtime(path)
        if seen_mtime.get(path) == mtime:
            continue  # unchanged since last harvest; dedup still covers re-reads
        new_mtime[path] = mtime
        session_uuid = os.path.basename(os.path.dirname(path))
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
        cwd = None
        for line in lines:
            if '"session.start"' not in line:
                continue
            try:
                ctx = (json.loads(line).get("data") or {}).get("context") or {}
            except json.JSONDecodeError:
                continue
            cwd = ctx.get("gitRoot") or ctx.get("cwd")
            break
        for line in lines:
            line = line.strip()
            if not line or "assistant.message" not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant.message":
                continue
            d = ev.get("data") or {}
            out = int(d.get("outputTokens") or 0)
            if out == 0:
                continue
            ts = ledger.normalize_iso(ev.get("timestamp"))
            if ledger.older_than_retention(ts):
                continue   # pruned by monthly rollover; don't resurrect it
            read += 1
            rid = d.get("requestId") or d.get("messageId") or d.get("apiCallId")
            rows.append({
                "uid": f"{HARNESS}:{session_uuid}:{rid}",
                "ts": ts,
                "harness": HARNESS,
                "provider": PROVIDER,
                "model": d.get("model"),
                "session_id": session_uuid,
                "request_id": rid,
                "input_tokens": 0,
                "output_tokens": out,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": 0.0,
                "source": "session-file",
                "confidence": "estimate",  # output-only; input/cache unavailable locally
                "raw_ref": f"{path}#{rid}",
                "cwd": cwd,
            })

    con = ledger.connect()
    inserted = ledger.insert_usage(con, rows)
    con.close()

    ledger.save_state(HARNESS, {"file_mtime": new_mtime})
    print(f"[{HARNESS}] {read} read, {inserted} new (output-only)", file=sys.stderr)
    return inserted


if __name__ == "__main__":
    harvest()
