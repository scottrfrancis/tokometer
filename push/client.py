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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

# Allow being run as a script (`python3 client.py`) AND as a module
# (`python3 -m push.client`). When invoked as a script, the package
# context is missing, so a relative `from . import state` fails. Bootstrap
# the parent dir so `import state as state_mod` works either way.
if __package__ in (None, ""):
    _PARENT = os.path.dirname(os.path.realpath(__file__))
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    import state as state_mod  # type: ignore[no-redef]
else:
    from . import state as state_mod

TOKOMETER_HOME = Path(os.path.expanduser(
    os.environ.get("TOKOMETER_HOME", "~/.tokometer")
))
DB_PATH = TOKOMETER_HOME / "ledger.db"

DEFAULT_BATCH = int(os.environ.get("FLIGHTPLAN_PUSH_BATCH", "1000"))
SCHEMA_VERSION = 1


# ─── kind -> SQLite table + endpoint path + row column mapping ───────────

# Tables we know how to ship. v1 scope: the b-CLI tables (time_entry,
# todo, note), tokometer's usage table, and session_log_raw. The other
# tokometer collector tables (commit_metric, pr_metric, cursor_repo_hour)
# use composite PKs (no `uid` column) AND have schema-name mismatches
# with the bronze side -- shipping them needs either an ALTER on the
# tokometer schema to add a uid OR a different watermark strategy. Punted
# to v1.1; see KNOWN_LIMITATIONS at the bottom of this file.
KIND_TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "time_entry": (
        "time_entry", "time-entries",
        ("uid", "host", "start_ts", "end_ts", "duration_sec",
         "customer", "project", "tags", "notes", "cwd", "session_id"),
    ),
    "todo": (
        "todo", "todos",
        ("uid", "host", "created_at", "done_date", "customer", "project",
         "title", "state", "blocker", "due", "tags", "cwd"),
    ),
    "note": (
        "note", "notes",
        ("uid", "host", "ts", "customer", "project",
         "text", "cwd"),
    ),
    "tokometer_usage": (
        "usage", "tokometer-usage",
        ("uid", "ts", "harness", "model", "account", "session_id", "cwd",
         "input_tokens", "output_tokens", "cache_read_tokens",
         "cache_write_tokens", "cost_usd"),
    ),
    "session_log": (
        "session_log_raw", "session-logs",
        ("uid", "scope", "source_project", "rel_filepath", "kind",
         "log_date", "log_time_local", "topic",
         "raw_md", "raw_md_sha256", "mtime", "size_bytes"),
    ),
    # Deferred to v1.1: commit_metric, pr_metric, cursor_repo_hour.
    # These tokometer tables have no uid column (composite PKs) AND have
    # column name mismatches with the bronze side. Will need either a
    # tokometer schema migration OR a different push strategy.
}


# Column renames for non-1:1 mappings between tokometer's SQLite column
# names and the bronze.* Pydantic-model names on the server.
COLUMN_REMAPS: dict[str, dict[str, str]] = {
    "time_entry": {
        # b CLI uses unsuffixed customer/project; bronze uses *_raw
        "customer": "customer_raw",
        "project": "project_raw",
    },
    "todo": {
        "customer": "customer_raw",
        "project": "project_raw",
        "title": "text",             # b stores the todo body in 'title'
        "due": "due_date",
        "created_at": "created_ts",
        "done_date": "done_ts",
    },
    "note": {
        "customer": "customer_raw",
        "project": "project_raw",
        "ts": "created_ts",          # b's note timestamp
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
        # Server responded; convey status + parsed body if possible.
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {"detail": exc.reason}
        return exc.code, payload
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        # Network-level failure: connection refused / DNS / timeout / etc.
        # Return a sentinel 0 status so callers can route this through the
        # "retry next tick" path uniformly with HTTP 5xx handling.
        return 0, {"detail": f"network: {type(exc).__name__}: {exc}"}


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

def _has_real_failure(results: list[dict]) -> bool:
    """Whether any result represents a genuine failure.

    A missing source table is expected on capture-only nodes (the b-CLI tables
    time_entry/todo/note simply don't exist there), so it must NOT count as a
    failure -- otherwise last_success_at never advances and every run looks
    broken. Real failures (HTTP 5xx, network errors, 422) still count.
    """
    return any(
        r["error"] and not r["error"].startswith("missing table")
        for r in results
    )


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

    # Source of truth for which kinds we ship is KIND_TABLES, not the
    # state module's KINDS list (which can drift from this file's
    # capability list and would result in "unknown kind" errors).
    selected = tuple(kinds) if kinds else tuple(KIND_TABLES.keys())
    state = state_mod.load()
    results: list[dict] = []

    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        for kind in selected:
            res = push_kind(con, kind=kind, state=state,
                            ingest_url=ingest_url, token=token)
            results.append(res.to_dict())
            # 422 (quarantine) and missing-table (optional source) are not
            # recorded as failures -- the former is server-side validation, the
            # latter is expected on capture-only nodes.
            if (res.error and not res.error.startswith("422")
                    and not res.error.startswith("missing table")):
                state_mod.record_failure(state, kind, res.error)
    finally:
        con.close()

    real_failure = _has_real_failure(results)
    if not real_failure:
        state_mod.record_success(state)
    state_mod.save(state)

    return {"ok": not real_failure, "results": results}


if __name__ == "__main__":
    import sys
    out = push_all()
    json.dump(out, sys.stdout, indent=2)
    print()
    sys.exit(0 if out["ok"] else 1)
