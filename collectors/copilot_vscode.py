#!/usr/bin/env python3
"""Copilot OTel file-exporter parser — optional structured cross-check. DORMANT.

Probed 2026-07-17 on the target box (VS Code 1.129.0, Copilot Chat 0.42.3):
the exporter is NOT exposed under the managed profile — env vars produced no
output and Settings has no otel entries. This collector therefore no-ops
cleanly unless COPILOT_OTEL_FILE_EXPORTER_PATH exists and contains files.
It stays in the harvest so that if a future build ever exposes the exporter,
the ledger gains a second, structured source with zero re-transfer.

Expected shape (from the VS Code agent-monitoring docs — UNVERIFIED against a
real emission; adjust when one exists): JSONL, one span/metric per line, token
counts under gen_ai.* attributes. Rows are marked confidence='estimate' until
the shape is verified against reality. Local-only; stdlib-only; 3.11-compatible.
"""
import os
import sys
import json
import glob
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "copilot"
PROVIDER = "github"
SOURCE = "otel-file"
CHAT_LOG_SOURCE = "vscode-chat-log"
STATE_KEY = "copilot_otel"
# window within which an OTel row and a Trace-log row are treated as the same
# underlying request (see _duplicate_of_chat_log below)
DEDUP_TOLERANCE_S = 2


def _attr(d, *keys):
    attrs = d.get("attributes") or {}
    for k in keys:
        if k in attrs:
            return attrs[k]
        if k in d:
            return d[k]
    return None


def _duplicate_of_chat_log(con, row, tolerance_s=DEDUP_TOLERANCE_S):
    """Best-effort cross-source dedup guard.

    copilot_chat_log.py (source='vscode-chat-log') and this collector
    (source='otel-file') are independent, uid-disjoint readers of
    potentially the SAME underlying Copilot Chat request: the Trace-log uid
    is built from a `ccreq:<hex>` request id burned into the chat output
    channel, this collector's uid is `{file}:{lineno}` from the OTel export
    — neither format appears in the other source's raw data.

    The only field the two parsers share is a *conversation*-level id (the
    Trace-log's "New request for conversation <uuid>" vs. OTel's
    `gen_ai.conversation.id`) — but a conversation can span many individual
    requests, so it can't disambiguate which request within it a given OTel
    row refers to. There is no genuine per-request shared identifier.

    Absent a real shared key, fall back to a heuristic: an OTel row is
    treated as a duplicate if a chat-log row already in the ledger has the
    same model and token counts within a small time window. This can
    produce false positives (two distinct requests that coincidentally
    match) and false negatives (the same request whose fields drifted more
    than the tolerance) — but a false negative just means an occasional
    double-count, while a false positive means silently dropping a real
    request. Since the double-count is the worse, already-flagged failure
    mode this guard exists to prevent, on any doubt we skip the insert
    (favor under-count over double-count).
    """
    try:
        target = dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    cur = con.execute(
        "SELECT ts FROM usage WHERE harness=? AND source=? AND model=?"
        " AND input_tokens=? AND output_tokens=?",
        (HARNESS, CHAT_LOG_SOURCE, row.get("model"),
         row.get("input_tokens", 0), row.get("output_tokens", 0)),
    )
    for (ts,) in cur.fetchall():
        try:
            candidate = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((candidate - target).total_seconds()) <= tolerance_s:
            return True
    return False


def collect(con, otel_dir=None, dry_run=False):
    path = otel_dir or os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
    result = {"files": 0, "rows": 0, "inserted": 0}
    if not path or not os.path.isdir(path):
        print(f"[{STATE_KEY}] no OTel export dir; skipping (exporter not exposed"
              " on this profile — expected)", file=sys.stderr)
        return result
    files = sorted(glob.glob(os.path.join(path, "*.json*")))
    if not files:
        print(f"[{STATE_KEY}] OTel dir {path} is empty; skipping", file=sys.stderr)
        return result

    rows = []
    for fp in files:
        result["files"] += 1
        with open(fp, errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                model = _attr(d, "gen_ai.request.model", "gen_ai.response.model", "model")
                out_tok = _attr(d, "gen_ai.usage.output_tokens", "output_tokens")
                in_tok = _attr(d, "gen_ai.usage.input_tokens", "input_tokens")
                if model is None and out_tok is None:
                    continue
                ts = ledger.normalize_iso(d.get("end_time") or d.get("timestamp"))
                if ledger.older_than_retention(ts):
                    continue
                result["rows"] += 1
                rows.append({
                    "uid": f"copilot-otel:{os.path.basename(fp)}:{lineno}",
                    "ts": ts, "harness": HARNESS, "provider": PROVIDER,
                    "model": model, "session_id": _attr(d, "gen_ai.conversation.id"),
                    "input_tokens": int(in_tok or 0),
                    "output_tokens": int(out_tok or 0),
                    "source": SOURCE, "confidence": "estimate",
                    "raw_ref": f"{fp}:{lineno}",
                })
    if rows:
        # cross-source dedup guard: drop rows that look like a request the
        # Trace-log collector (copilot_chat_log.py) already recorded — see
        # _duplicate_of_chat_log for why this is heuristic, not exact.
        rows = [r for r in rows if not _duplicate_of_chat_log(con, r)]
    if rows and not dry_run:
        result["inserted"] = ledger.insert_usage(con, rows)
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    con = ledger.connect()
    r = collect(con, dry_run="--dry-run" in argv)
    con.close()
    print(f"[{STATE_KEY}] {r['files']} files, {r['rows']} rows (+{r['inserted']} new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
