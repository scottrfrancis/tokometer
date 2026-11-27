#!/usr/bin/env python3
"""OpenClaw harvester.

Source: each agent's event store, $OPENCLAW_HOME/agents/<agent>/agent/
openclaw-agent.sqlite (OPENCLAW_HOME defaults to ~/.openclaw), table
transcript_events: one row per transcript event, unique on (session_id, seq),
with the event as JSON in event_json or, for newer rows, only zstd-compressed
in event_zstd. The database is opened read-only.

An LLM call is an event with message.role == "assistant" and
message.{provider, model, timestamp (epoch ms, the request start), usage}.
usage = {input, output, cacheRead, cacheWrite, reasoningTokens, totalTokens,
cost:{total}}: input excludes cacheRead and output already includes
reasoning (the OMP convention), so reasoning_tokens stays 0. Provider
"openclaw" marks OpenClaw's own bookkeeping entries (automation results,
delivery mirrors), which are not calls.

Rows are keyed openclaw:<session id>:<seq>. A per-database rowid watermark
keeps each run to new events. Only usage, model, provider and time are kept;
message text is never stored.

Who paid: a call to a local model server (OMP's list of local server names,
or a name in TOKOMETER_PROVIDER_ALIASES) is account=openclaw,
subscription=self-hosted, org=Local, under the aliased provider name so it
prices with other clients of the same server. Anything else is account=
openclaw with plan and org unknown.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402
from collectors.omp import BUILTIN_LOCAL, SELF_HOSTED  # noqa: E402

HARNESS = "openclaw"
INTERNAL_PROVIDERS = {"openclaw"}


class DecodeError(RuntimeError):
    """A compressed event could not be read; the run stops so nothing is skipped."""


def openclaw_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("OPENCLAW_HOME", "~/.openclaw")))


def agent_dbs() -> list[Path]:
    return sorted(openclaw_home().glob("agents/*/agent/openclaw-agent.sqlite"))


def decompress(blob: bytes) -> bytes:
    """zstd-decode one event: the zstandard module if present, else the zstd CLI."""
    try:
        import zstandard  # type: ignore[import-not-found]
        with zstandard.ZstdDecompressor().stream_reader(blob) as r:
            return r.read()
    except ImportError:
        pass
    try:
        done = subprocess.run(["zstd", "-q", "-d", "-c"], input=blob, capture_output=True,
                              timeout=30, check=True)
    except FileNotFoundError:
        raise DecodeError("compressed OpenClaw events need the zstd CLI or the "
                          "zstandard module") from None
    except subprocess.CalledProcessError as e:
        raise DecodeError(f"zstd could not decode an event: {e.stderr.decode()[:200]}") from None
    return done.stdout


def _usage_row(event: dict, session_id: str, seq: int, created_at: int, ref: str,
               aliases: dict[str, str]) -> dict | None:
    m = event.get("message") or {}
    provider = m.get("provider")
    if m.get("role") != "assistant" or not provider or provider in INTERNAL_PROVIDERS:
        return None
    u = m.get("usage") or {}
    i, o, cr, cw = (int(u.get(k) or 0) for k in ("input", "output", "cacheRead", "cacheWrite"))
    if i + o + cr + cw == 0:
        return None
    ts_ms = m.get("timestamp") if isinstance(m.get("timestamp"), (int, float)) else created_at
    local = provider in BUILTIN_LOCAL or provider in aliases
    sub, org = SELF_HOSTED if local else (None, None)
    return {
        "uid": f"{HARNESS}:{session_id}:{seq}",
        "ts": ledger.iso_utc(epoch_ms=ts_ms),
        "harness": HARNESS,
        "provider": aliases.get(provider, provider),
        "model": m.get("model"),
        "session_id": session_id,
        "request_id": None,
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "reasoning_tokens": 0,
        "credits": 0,
        "cost_usd": float((u.get("cost") or {}).get("total") or 0),
        "source": "session-db",
        "confidence": "exact",
        "raw_ref": ref,
        "cwd": None,
        "account": HARNESS,
        "subscription": sub,
        "org": org,
    }


def _read_db(path: Path, after_rowid: int, since_ms: int,
             aliases: dict[str, str]) -> tuple[list[dict], int]:
    """New calls in one database, and the rowid to resume after."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        (top,) = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM transcript_events").fetchone()
        cur = con.execute(
            "SELECT rowid, session_id, seq, event_json, event_zstd, created_at "
            "FROM transcript_events WHERE rowid > ? AND rowid <= ? "
            "AND message_role = 'assistant' AND created_at >= ? ORDER BY rowid",
            (after_rowid, top, since_ms))
        rows = []
        for rowid, session_id, seq, event_json, event_zstd, created_at in cur:
            if event_json is not None:
                raw = event_json
            elif event_zstd is not None:
                raw = decompress(event_zstd)
            else:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            row = _usage_row(event, session_id, seq, created_at, f"{path}#rowid={rowid}", aliases)
            if row:
                rows.append(row)
        return rows, top
    finally:
        con.close()


def harvest() -> int:
    dbs = agent_dbs()
    if not dbs:
        print(f"[{HARNESS}] no OpenClaw agent databases under {openclaw_home()}; skipping")
        return 0
    aliases = ledger.provider_aliases()
    since = ledger.collect_since()
    since_ms = ledger.local_midnight_epoch_ms(since) if since else 0
    marks = ledger.load_state(HARNESS).get("last_rowid", {})
    new_marks = dict(marks)
    rows: list[dict] = []
    for path in dbs:
        found, top = _read_db(path, int(marks.get(str(path), 0)), since_ms, aliases)
        rows.extend(found)
        new_marks[str(path)] = top
    con = ledger.connect()
    try:
        inserted = ledger.insert_usage(con, rows)
    finally:
        con.close()
    ledger.save_state(HARNESS, {"last_rowid": new_marks})
    print(f"[{HARNESS}] {len(dbs)} database(s), {len(rows)} calls read, {inserted} new")
    return inserted


if __name__ == "__main__":
    harvest()
