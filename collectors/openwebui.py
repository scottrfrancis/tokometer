#!/usr/bin/env python3
"""Open WebUI harvester.

Source: Open WebUI's webui.db (SQLite). Where Open WebUI runs in Docker, set
TOKOMETER_OPENWEBUI_CONTAINER and the query runs inside it with
`docker exec -i <container> python3 -` against /app/backend/data/webui.db
(TOKOMETER_OPENWEBUI_DB_IN_CONTAINER overrides); otherwise set
TOKOMETER_OPENWEBUI_DB to a local webui.db. Either way the database is opened
read-only and only usage, model and time fields leave it.

An LLM call is a chat_message row with role "assistant" and token counts in
`usage`, Open WebUI's merged shape: prompt_tokens (input_tokens) includes any
cached prompt, completion_tokens (output_tokens) includes reasoning, and
prompt_tokens_details.cached_tokens appears when the server reports it.
Rows with NULL usage or {"incomplete": true} are not counted.

The model is resolved through table model: a custom model ("hazel-kb") calls
its base_model_id, written <connection prefix>.<model> ("vllm.workhorse").
A prefix naming a local model server (OMP's list, or a name in
TOKOMETER_PROVIDER_ALIASES) becomes the provider, aliased to the server's own
name, with subscription=self-hosted, org=Local. Anything else keeps the model
id as it is, provider unknown.

Rows are keyed openwebui:<message id>. Each run re-reads messages updated
within a day of the last one seen, so usage that lands after a message was
first read is still counted; the uid keeps each call to one row.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402
from collectors.omp import BUILTIN_LOCAL, SELF_HOSTED  # noqa: E402

HARNESS = "openwebui"
IN_CONTAINER_DB = "/app/backend/data/webui.db"
OVERLAP_S = 86400

# Runs where webui.db is (inside the container, or here): argv = db path,
# updated_at lower bound. Prints one JSON object per assistant message.
QUERY_SCRIPT = r'''
import json, sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
cur = con.execute(
    "SELECT m.id, m.chat_id, m.created_at, m.updated_at, m.model_id, md.base_model_id, m.usage "
    "FROM chat_message m LEFT JOIN model md ON md.id = m.model_id "
    "WHERE m.role = 'assistant' AND m.updated_at >= ?", (int(sys.argv[2]),))
for row in cur:
    print(json.dumps(dict(zip(("id", "chat_id", "created_at", "updated_at", "model_id",
                               "base_model_id", "usage"), row))))
'''


def command(since: int) -> list[str] | None:
    """The argv that runs QUERY_SCRIPT on stdin, or None if nothing is configured."""
    container = os.environ.get("TOKOMETER_OPENWEBUI_CONTAINER", "").strip()
    if container:
        db = os.environ.get("TOKOMETER_OPENWEBUI_DB_IN_CONTAINER", IN_CONTAINER_DB)
        return ["docker", "exec", "-i", container, "python3", "-", db, str(since)]
    local = os.environ.get("TOKOMETER_OPENWEBUI_DB", "").strip()
    if local and Path(local).is_file():
        return [sys.executable, "-", local, str(since)]
    return None


def _model(rec: dict, aliases: dict[str, str]) -> tuple[str | None, str | None, bool]:
    """(provider, model, self_hosted) for one message."""
    base = rec.get("base_model_id") or rec.get("model_id")
    if base and "." in base:
        prefix, rest = base.split(".", 1)
        if prefix in BUILTIN_LOCAL or prefix in aliases:
            return aliases.get(prefix, prefix), rest, True
    return None, base, False


def _usage_row(rec: dict, aliases: dict[str, str]) -> dict | None:
    try:
        u = json.loads(rec["usage"]) if rec.get("usage") else None
    except json.JSONDecodeError:
        return None
    if not isinstance(u, dict):
        return None
    prompt = u.get("prompt_tokens", u.get("input_tokens", u.get("prompt_eval_count"))) or 0
    completion = u.get("completion_tokens", u.get("output_tokens", u.get("eval_count"))) or 0
    if not prompt and not completion:
        return None
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    provider, model, local = _model(rec, aliases)
    sub, org = SELF_HOSTED if local else (None, None)
    return {
        "uid": f"{HARNESS}:{rec['id']}",
        "ts": ledger.iso_utc(epoch_s=rec["created_at"]),
        "harness": HARNESS,
        "provider": provider,
        "model": model,
        "session_id": rec.get("chat_id"),
        "request_id": None,
        "input_tokens": max(int(prompt) - cached, 0),
        "output_tokens": int(completion),
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "credits": 0,
        "cost_usd": 0.0,
        "source": "session-db",
        "confidence": "exact",
        "raw_ref": f"webui.db#chat_message={rec['id']}",
        "cwd": None,
        "account": HARNESS,
        "subscription": sub,
        "org": org,
    }


def harvest() -> int:
    state = ledger.load_state(HARNESS)
    mark = int(state.get("updated_at", 0))
    since = max(mark - OVERLAP_S, 0)
    cutoff = ledger.collect_since()
    if cutoff:
        since = max(since, ledger.local_midnight_epoch_ms(cutoff) // 1000)
    argv = command(since)
    if argv is None:
        print(f"[{HARNESS}] no Open WebUI database configured "
              "(TOKOMETER_OPENWEBUI_CONTAINER or TOKOMETER_OPENWEBUI_DB); skipping")
        return 0
    done = subprocess.run(argv, input=QUERY_SCRIPT, capture_output=True, text=True,
                          timeout=120, check=True)
    recs = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    aliases = ledger.provider_aliases()
    rows = [r for r in (_usage_row(rec, aliases) for rec in recs) if r]
    con = ledger.connect()
    try:
        inserted = ledger.insert_usage(con, rows)
    finally:
        con.close()
    top = max([mark] + [int(rec["updated_at"] or 0) for rec in recs])
    ledger.save_state(HARNESS, {"updated_at": top})
    print(f"[{HARNESS}] {len(recs)} replies read, {len(rows)} with usage, {inserted} new")
    return inserted


if __name__ == "__main__":
    harvest()
