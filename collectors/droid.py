#!/usr/bin/env python3
"""Droid (Factory) harvester.

Source: per-session sidecars  ~/.factory/sessions/<proj>/<session-id>.settings.json
Verified shape:
  { model, providerLock, providerLockTimestamp,
    tokenUsage:          {inputTokens, outputTokens, cacheCreationTokens,
                          cacheReadTokens, thinkingTokens, factoryCredits},
    inclusiveTokenUsage: {...incl. child sessions...} }

We use `tokenUsage` (this session only) to avoid double-counting child/subagent
sessions, which carry their own sidecars. These files are LIVE -- a session's
totals grow during use -- so rows are upserted by session id (on_conflict=replace).

The legacy ~/.factory/token-ledger.json is a stale, derived snapshot and is NOT
used. Activity timestamp = last transcript line, else providerLockTimestamp,
else the sidecar's mtime.
"""
import os
import sys
import json
import glob
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "droid"
PROVIDER = "factory"
SETTINGS_GLOB = os.path.expanduser(
    os.environ.get("FACTORY_SESSIONS_GLOB",
                   "~/.factory/sessions/*/*.settings.json")
)


def scan_transcript(settings_path):
    """Return (last_iso_ts, cwd) from the matching <sid>.jsonl transcript.

    cwd comes from the session_start line; ts from the last timestamped line.
    """
    transcript = settings_path[:-len(".settings.json")] + ".jsonl"
    if not os.path.exists(transcript):
        return None, None
    last = cwd = None
    try:
        with open(transcript, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_start" and obj.get("cwd"):
                    cwd = obj["cwd"]
                if obj.get("timestamp"):
                    last = obj["timestamp"]
    except OSError:
        return None, None
    return last, cwd


def harvest():
    files = sorted(glob.glob(SETTINGS_GLOB))
    if not files:
        print(f"[{HARNESS}] no session sidecars; skipping", file=sys.stderr)
        return 0

    rows = []
    for path in files:
        try:
            with open(path) as f:
                s = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        tok = s.get("tokenUsage") or {}
        if not tok:
            continue
        sid = os.path.basename(path)[:-len(".settings.json")]
        last_ts, cwd = scan_transcript(path)

        ts = (last_ts
              or s.get("providerLockTimestamp")
              or dt.datetime.fromtimestamp(os.path.getmtime(path),
                                           tz=dt.timezone.utc).isoformat())
        ts = ledger.normalize_iso(ts)
        if ledger.older_than_retention(ts):
            continue   # pruned by monthly rollover; don't resurrect it
        rows.append({
            "uid": f"{HARNESS}:{sid}",            # one row per session, upserted
            "ts": ts,
            "harness": HARNESS,
            "provider": PROVIDER,
            "model": s.get("model"),
            "session_id": sid,
            "request_id": None,
            "input_tokens": int(tok.get("inputTokens") or 0),
            "output_tokens": int(tok.get("outputTokens") or 0),
            "cache_read_tokens": int(tok.get("cacheReadTokens") or 0),
            "cache_write_tokens": int(tok.get("cacheCreationTokens") or 0),
            "reasoning_tokens": int(tok.get("thinkingTokens") or 0),
            "credits": int(tok.get("factoryCredits") or 0),
            "cost_usd": 0.0,
            "source": "session-file",
            "confidence": "exact",
            "raw_ref": f"{path}",
            "cwd": cwd,
        })

    con = ledger.connect()
    changed = ledger.insert_usage(con, rows, on_conflict="replace")
    con.close()
    print(f"[{HARNESS}] {len(rows)} sessions, {changed} inserted/updated", file=sys.stderr)
    return changed


if __name__ == "__main__":
    harvest()
