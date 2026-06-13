#!/usr/bin/env python3
"""Cursor repo-attribution collector.

The Cursor usage CSV has no repo/path column, so token usage can't be tied to a
repo directly. Cursor's local AI-code tracking DB, however, records the absolute
file path of every AI-authored edit with a timestamp. This collector reads that
DB (read-only) and records, per local-time hour, how many AI-code records each
repo received -- a weight the report uses to split Cursor's hourly output tokens
across repos (a heuristic match by hour, not an exact join).

Source: ~/.cursor/ai-tracking/ai-code-tracking.db  (ai_code_hashes.fileName)
Honors the same retention window as the rest of the pipeline.
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

SRC = os.path.expanduser(os.environ.get(
    "CURSOR_AI_DB", "~/.cursor/ai-tracking/ai-code-tracking.db"))
WORKSPACE_MARKER = "/workspace/"


def repo_of_path(p):
    if not p:
        return None
    i = p.find(WORKSPACE_MARKER)
    if i < 0:
        return None
    seg = p[i + len(WORKSPACE_MARKER):].split("/", 1)[0]
    return seg or None


def harvest():
    if not os.path.exists(SRC):
        print(f"[cursor_repos] no AI-code DB at {SRC}; skipping", file=sys.stderr)
        return 0
    cutoff = ledger.retention_cutoff_date().isoformat()

    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=30)
    src.execute("PRAGMA busy_timeout = 30000;")
    rows = src.execute(
        "SELECT strftime('%Y-%m-%dT%H', timestamp/1000, 'unixepoch', 'localtime') hr, "
        "       fileName "
        "FROM ai_code_hashes "
        "WHERE date(timestamp/1000,'unixepoch','localtime') >= ?", (cutoff,)
    ).fetchall()
    src.close()

    counts = {}
    for hr, fn in rows:
        repo = repo_of_path(fn)
        if not hr or not repo:
            continue
        counts[(hr, repo)] = counts.get((hr, repo), 0) + 1

    con = ledger.connect()
    con.execute("DELETE FROM cursor_repo_hour WHERE hour >= ?", (cutoff,))
    con.executemany(
        "INSERT OR REPLACE INTO cursor_repo_hour(hour,repo,hits) VALUES(?,?,?)",
        [(hr, repo, n) for (hr, repo), n in counts.items()])
    con.commit()
    con.close()
    print(f"[cursor_repos] {len(rows)} AI-code records -> {len(counts)} (hour,repo) weights",
          file=sys.stderr)
    return len(counts)


if __name__ == "__main__":
    harvest()
