#!/usr/bin/env python3
"""GitHub PR-metrics collector (merged PRs, month-to-date).

Uses the authenticated `gh` CLI: `gh search prs` for PRs you authored that were
merged since the start of the current month. Records into `pr_metric`.

Best-effort: gh search does not return additions/deletions, so those stay 0
(the Sankey width is output tokens, not PR size; PR counts are terminal labels).
Idempotent: rows keyed by (repo, number), REPLACE on re-run.

Pagination: `gh search prs --limit N` already pages internally against the
GitHub search API (fetching in batches until N results are gathered), but the
API hard-caps any single search query at 1000 total results regardless of how
it's paged. We request SEARCH_LIMIT (the API max) instead of an arbitrary
smaller cap, so `gh` does the real paging for us. If the result count still
comes back exactly at that ceiling, more matches may exist beyond what any
pagination strategy against this API can retrieve -- we warn rather than
silently under-count.
"""
import os
import sys
import json
import subprocess
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402


SEARCH_LIMIT = 1000  # GitHub search API's hard cap on total results per query


def month_start():
    now = dt.datetime.now()
    return dt.date(now.year, now.month, 1).isoformat()


def harvest():
    if not _have_gh():
        print("[gh] gh not installed or not authed; skipping", file=sys.stderr)
        return 0
    since = month_start()
    cmd = ["gh", "search", "prs", "--author=@me", f"--merged-at=>={since}",
           "--limit", str(SEARCH_LIMIT),
           "--json", "number,title,repository,createdAt,closedAt,state,url"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[gh] query failed: {e}", file=sys.stderr)
        return 0
    if out.returncode != 0:
        print(f"[gh] gh error: {out.stderr.strip()[:200]}", file=sys.stderr)
        return 0
    try:
        items = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        print("[gh] bad JSON from gh", file=sys.stderr)
        return 0

    if len(items) >= SEARCH_LIMIT:
        print(f"[gh] result count hit the GitHub search API limit "
              f"({SEARCH_LIMIT}); results since {since} may be truncated",
              file=sys.stderr)

    rows = []
    for it in items:
        repo = (it.get("repository") or {}).get("nameWithOwner") or "?"
        rows.append((repo, it.get("number"), it.get("title"), "merged",
                     ledger.normalize_iso(it.get("createdAt")),
                     ledger.normalize_iso(it.get("closedAt")), 0, 0))

    con = ledger.connect()
    sql = ("INSERT OR REPLACE INTO pr_metric "
           "(repo,number,title,state,created_at,merged_at,additions,deletions) "
           "VALUES (?,?,?,?,?,?,?,?)")
    before = con.total_changes
    con.executemany(sql, rows)
    con.commit()
    changed = con.total_changes - before
    con.close()
    print(f"[gh] {len(rows)} merged PRs since {since}, {changed} written", file=sys.stderr)
    return changed


def _have_gh():
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


if __name__ == "__main__":
    harvest()
