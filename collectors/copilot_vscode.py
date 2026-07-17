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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "copilot"
PROVIDER = "github"
SOURCE = "otel-file"
STATE_KEY = "copilot_otel"


def _attr(d, *keys):
    attrs = d.get("attributes") or {}
    for k in keys:
        if k in attrs:
            return attrs[k]
        if k in d:
            return d[k]
    return None


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
