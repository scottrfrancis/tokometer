"""flightplan push client -- ship bronze-shaped rows to Beaufort's REST ingest.

Per SRS §8.3. Cron-triggered (every FLIGHTPLAN_RETRY_INTERVAL, default 2h)
on each device. Event-driven calls also welcome (e.g. b's after-write hook).

Stdlib only (urllib + sqlite3 + json) -- consistent with tokometer's
dependency posture.

Env:
  FLIGHTPLAN_INGEST_URL    e.g. https://hasami:7321
  FLIGHTPLAN_INGEST_TOKEN  bearer token for this device (write scope)
  TOKOMETER_HOME           default ~/.tokometer
  FLIGHTPLAN_PUSH_BATCH    rows per POST (default 1000)
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from . import state as state_mod

TOKOMETER_HOME = Path(os.path.expanduser(
    os.environ.get("TOKOMETER_HOME", "~/.tokometer")
))
DB_PATH = TOKOMETER_HOME / "ledger.db"

DEFAULT_BATCH = int(os.environ.get("FLIGHTPLAN_PUSH_BATCH", "1000"))
SCHEMA_VERSION = 1


# ─── kind -> SQLite table + endpoint path + row column mapping ───────────

KIND_TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # kind: (sqlite_table, endpoint_path, columns_to_ship)
    "time_entry": (
        "time_entry", "time-entries",
        ("uid", "host", "start_ts", "end_ts", "duration_sec",
         "customer_raw", "project_raw", "tags", "notes", "cwd", "session_id"),
    ),
    "todo": (
        "todo", "todos",
        ("uid", "host", "created_ts", "done_ts", "customer_raw", "project_raw",
         "text", "state", "blocker", "due_date", "tags", "cwd"),
    ),
    "note": (
        "note", "notes",
        ("uid", "host", "created_ts", "customer_raw", "project_raw",
         "text", "tags", "cwd"),
    ),
    "tokometer_usage": (
        "usage", "tokometer-usage",
        ("uid", "ts", "harness", "model", "account", "session_id", "cwd",
         "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
         "cost_usd"),
    ),
    "commit_metric": (
        "commit_metric", "commit-metrics",
        ("uid", "repo_path", "sha", "ts", "author",
         "code_add", "code_del", "files", "is_merge"),
    ),
    "pr_metric": (
        "pr_metric", "pr-metrics",
        ("uid", "repo", "pr_number", "ts", "state", "title", "author", "branch"),
    ),
    "cursor_repo_hour": (
        "cursor_repo_hour", "cursor-repo-hours",
        ("uid", "repo", "hour_ts", "edits", "accepts", "rejects"),
    ),
    "session_log": (
        "session_log_raw", "session-logs",
        ("uid", "scope", "source_project", "rel_filepath", "kind",
         "log_date", "log_time_local", "topic",
         "raw_md", "raw_md_sha256", "mtime", "size_bytes"),
    ),
}


# Column renames + transforms for non-1:1 mappings between tokometer's
# SQLite columns and the bronze.* Pydantic models.
COLUMN_REMAPS: dict[str, dict[str, str]] = {
    "tokometer_usage": {"ts": "ts"},          # passthrough but documents the model alignment
    "commit_metric": {                         # tokometer uses repo_path, code_add/del; bronze uses repo, loc_added/removed
        "repo_path": "repo",
        "code_add": "loc_added",
        "code_del": "loc_removed",
        "files": "files_changed",
    },
}


def _host() -> str:
    return socket.gethostname().split(".")[0]


# ─── HTTP ────────────────────────────────────────────────────────────────

class PushResult:
    """A simple result envelope for a single kind's push attempt."""
    def __init__(self, *, kind: str, attempted: int = 0,
                 accepted: int = 0, conflicted: int = 0,
                 quarantined: int = 0, error: str | None = None):
        self.kind = kind
        self.attempted = attempted
        self.accepted = accepted
        self.conflicted = conflicted
        self.quarantined = quarantined
        self.error = error

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "attempted": self.attempted,
            "accepted": self.accepted, "conflicted": self.conflicted,
            "quarantined": self.quarantined, "error": self.error,
        }


def _post(url: str, token: str, body: dict, timeout: int = 30) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {"detail": exc.reason}
        return exc.code, payload


# ─── per-kind fetcher ────────────────────────────────────────────────────

def _fetch_rows(con: sqlite3.Connection, kind: str, since_uid: str | None,
                limit: int) -> list[dict]:
    sqlite_table, _, cols = KIND_TABLES[kind]
    remap = COLUMN_REMAPS.get(kind, {})
    col_list = ", ".join(cols)
    if since_uid:
        sql = (
            f"SELECT {col_list} FROM {sqlite_table} "
            f"WHERE uid > ? "
            f"ORDER BY uid LIMIT ?"
        )
        cur = con.execute(sql, (since_uid, limit))
    else:
        sql = (
            f"SELECT {col_list} FROM {sqlite_table} "
            f"ORDER BY uid LIMIT ?"
        )
        cur = con.execute(sql, (limit,))

    rows: list[dict] = []
    for row in cur:
        rec = {}
        for i, c in enumerate(cols):
            target = remap.get(c, c)
            rec[target] = row[i]
        # Some bronze.* tables require `host`; for usage rows we set it from
        # this device's hostname if missing.
        rec.setdefault("host", _host())
        # Tags stored as comma-separated in some tokometer tables; convert.
        if "tags" in rec and isinstance(rec["tags"], str) and rec["tags"]:
            rec["tags"] = [t.strip() for t in rec["tags"].split(",") if t.strip()]
        elif "tags" in rec and not rec["tags"]:
            rec["tags"] = None
        rows.append(rec)
    return rows


# ─── per-kind push ───────────────────────────────────────────────────────

def push_kind(con: sqlite3.Connection, *, kind: str, state: dict,
              ingest_url: str, token: str,
              batch: int = DEFAULT_BATCH) -> PushResult:
    if kind not in KIND_TABLES:
        return PushResult(kind=kind, error=f"unknown kind: {kind}")

    sqlite_table, ep_path, _cols = KIND_TABLES[kind]

    # Sanity-check that the source table exists (older tokometer installs may
    # lack newer tables like session_log_raw until install.sh re-applies the
    # additive migration).
    have = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (sqlite_table,),
    ).fetchone()
    if not have:
        return PushResult(kind=kind, error=f"missing table {sqlite_table}")

    since = state_mod.watermark(state, kind)
    rows = _fetch_rows(con, kind, since, batch)
    if not rows:
        return PushResult(kind=kind, attempted=0)

    body = {
        "schema_version": SCHEMA_VERSION,
        "host": _host(),
        "rows": rows,
    }
    url = f"{ingest_url.rstrip('/')}/ingest/{ep_path}"
    status, payload = _post(url, token, body)

    if status == 200:
        # Advance watermark to the highest uid we just shipped (regardless of
        # accepted vs conflicted -- on-conflict counts still mean "Beaufort
        # has it").
        max_uid = max(r["uid"] for r in rows)
        state_mod.set_watermark(state, kind, max_uid)
        return PushResult(
            kind=kind, attempted=len(rows),
            accepted=payload.get("accepted", 0),
            conflicted=payload.get("conflicted", 0),
        )
    if status == 422:
        # Schema mismatch -- rows are quarantined server-side, DO NOT advance.
        return PushResult(
            kind=kind, attempted=len(rows),
            quarantined=payload.get("quarantined", len(rows)),
            error=f"422: {payload.get('reason', 'validation')}",
        )
    return PushResult(kind=kind, attempted=len(rows), error=f"http {status}: {payload}")


# ─── orchestration: push everything ──────────────────────────────────────

def push_all(*, kinds: Iterable[str] | None = None,
             ingest_url: str | None = None,
             token: str | None = None,
             db_path: Path | None = None) -> dict:
    ingest_url = ingest_url or os.environ.get("FLIGHTPLAN_INGEST_URL")
    token = token or os.environ.get("FLIGHTPLAN_INGEST_TOKEN")
    if not ingest_url or not token:
        raise RuntimeError(
            "FLIGHTPLAN_INGEST_URL and FLIGHTPLAN_INGEST_TOKEN must be set"
        )
    db_path = db_path or DB_PATH
    if not Path(db_path).exists():
        return {"ok": False, "error": f"ledger.db missing: {db_path}"}

    selected = tuple(kinds) if kinds else state_mod.KINDS
    state = state_mod.load()
    results: list[dict] = []

    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        for kind in selected:
            res = push_kind(con, kind=kind, state=state,
                            ingest_url=ingest_url, token=token)
            results.append(res.to_dict())
            if res.error and not res.error.startswith("422"):
                state_mod.record_failure(state, kind, res.error)
    finally:
        con.close()

    any_error = any(r["error"] for r in results)
    if not any_error:
        state_mod.record_success(state)
    state_mod.save(state)

    return {"ok": not any_error, "results": results}


if __name__ == "__main__":
    import sys
    out = push_all()
    json.dump(out, sys.stdout, indent=2)
    print()
    sys.exit(0 if out["ok"] else 1)
