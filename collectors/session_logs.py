"""collectors/session_logs.py -- scan ~/.claude/session-logs/ + per-repo session-log dirs.

Lands rows in tokometer's session_log_raw table (additive migration in
schema_session_logs.sql). The Beaufort REST push client then ships rows
to flightplan's bronze.session_log via POST /ingest/session-logs.

Idempotent: a file is only re-ingested if its (rel_filepath, raw_md_sha256)
pair changes — i.e. content edits produce a new uid + new row, but a
repeated scan of the same content is a no-op.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

TOKOMETER_HOME = Path(os.path.expanduser(
    os.environ.get("TOKOMETER_HOME", "~/.tokometer")
))
DB_PATH = TOKOMETER_HOME / "ledger.db"

# Repo scan root: defaults to ~/repos but configurable for non-standard setups.
REPO_SCAN_ROOT = Path(os.path.expanduser(
    os.environ.get("TOKOMETER_REPO_ROOT", "~/repos")
))

# Filename heuristics:
#   handoff-YYYY-MM-DD-HHMM.md
#   mine-report-YYYY-MM-DD.md
#   YYYY-MM-DD-HHMM[-topic].md   (the /session-logger default)
_FN = re.compile(
    r"^(?:(?P<kind>handoff|mine-report)-)?"
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-(?P<hhmm>\d{4}))?"
    r"(?:-(?P<topic>[^.]+))?"
    r"\.md$"
)


def _host() -> str:
    return socket.gethostname().split(".")[0]


def _classify_filename(fn: str) -> dict | None:
    m = _FN.match(fn)
    if not m:
        return None
    d = m.groupdict()
    kind = d["kind"] or "session"
    hhmm = d["hhmm"]
    log_time = f"{hhmm[:2]}:{hhmm[2:]}" if hhmm else None
    return {
        "kind": kind,
        "log_date": d["date"],
        "log_time_local": log_time,
        "topic": d.get("topic"),
    }


def _uid(host: str, rel_filepath: str, mtime_ns: int) -> str:
    sha = hashlib.sha256(rel_filepath.encode("utf-8")).hexdigest()[:8]
    return f"sl-{host}-{sha}-{mtime_ns}"


def _read_md(path: Path) -> tuple[str, str]:
    """Return (raw_md, sha256_hex)."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    return data.decode("utf-8", errors="replace"), sha


def iter_session_log_paths(
    home: Path = Path.home(),
    repo_root: Path | None = None,
) -> Iterable[tuple[Path, str, str | None]]:
    """Yield (abs_path, scope, source_project?) tuples.

    - global: ~/.claude/session-logs/*.md
    - project: <repo>/.claude/session-logs/*.md for each repo under repo_root
      with a .claude/session-logs directory.

    scope is 'global' | 'project'; source_project is set only for project scope.
    """
    if repo_root is None:
        repo_root = REPO_SCAN_ROOT

    # global
    global_dir = home / ".claude" / "session-logs"
    if global_dir.is_dir():
        for p in sorted(global_dir.glob("*.md")):
            yield (p, "global", None)

    # per-repo: shallow scan, repo_root/<name>/.claude/session-logs
    if repo_root.is_dir():
        for repo in sorted(repo_root.iterdir()):
            if not repo.is_dir():
                continue
            sl = repo / ".claude" / "session-logs"
            if sl.is_dir():
                for p in sorted(sl.glob("*.md")):
                    yield (p, "project", repo.name)


def _rel_filepath(p: Path, scope: str, source_project: Optional[str], home: Path) -> str:
    """For global: relative to ~. For project: relative to repo root."""
    if scope == "project":
        # ~/repos/<repo>/.claude/session-logs/foo.md -> .claude/session-logs/foo.md
        marker = "/.claude/"
        s = str(p)
        if marker in s:
            return s.split(marker, 1)[1] and "/.claude/" + s.split("/.claude/", 1)[1]
    try:
        return str(p.relative_to(home))
    except ValueError:
        return str(p)


def collect(
    *,
    con: Optional[sqlite3.Connection] = None,
    home: Path = Path.home(),
    repo_root: Path | None = None,
    skip_dirs: tuple[str, ...] = (".venv", "node_modules"),  # noqa: unused -- shallow scan
) -> dict[str, int]:
    """Scan + insert any new (rel_filepath, sha256) into session_log_raw.

    Returns {scanned, inserted, skipped}.
    """
    own_con = False
    if con is None:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA journal_mode = WAL;")
        own_con = True

    cur = con.cursor()
    host = _host()
    counts = {"scanned": 0, "inserted": 0, "skipped": 0}

    try:
        for abs_path, scope, source_project in iter_session_log_paths(home, repo_root):
            counts["scanned"] += 1
            cls = _classify_filename(abs_path.name)
            if cls is None:
                counts["skipped"] += 1
                continue

            raw_md, sha = _read_md(abs_path)
            rel = _rel_filepath(abs_path, scope, source_project, home)
            stat = abs_path.stat()
            mtime_ns = stat.st_mtime_ns
            mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            uid = _uid(host, rel, mtime_ns)

            cur.execute(
                """
                INSERT OR IGNORE INTO session_log_raw
                  (uid, scope, source_project, rel_filepath, abs_filepath,
                   kind, log_date, log_time_local, topic,
                   raw_md, raw_md_sha256, mtime, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, scope, source_project, rel, str(abs_path),
                    cls["kind"], cls["log_date"], cls["log_time_local"], cls["topic"],
                    raw_md, sha, mtime_iso, stat.st_size,
                ),
            )
            if cur.rowcount == 1:
                counts["inserted"] += 1
            else:
                counts["skipped"] += 1
        con.commit()
    finally:
        if own_con:
            con.close()

    return counts


if __name__ == "__main__":
    import json
    import sys
    result = collect()
    json.dump(result, sys.stdout)
    print()
