#!/usr/bin/env python3
"""GitHub PR-metrics collector (merged PRs, month-to-date).

Uses the authenticated `gh` CLI: `gh search prs` for PRs you authored that were
merged since the start of the current month. Records into `pr_metric`.

Best-effort: gh search does not return additions/deletions, so those stay 0
(the Sankey width is output tokens, not PR size; PR counts are terminal labels).
Idempotent: rows keyed by (repo, number), REPLACE on re-run.
"""
import os
import sys
import json
import subprocess
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402


def month_start():
    now = dt.datetime.now()
    return dt.date(now.year, now.month, 1).isoformat()


def harvest():
    if not _have_gh():
        print("[gh] gh not installed or not authed; skipping", file=sys.stderr)
        return 0
    since = month_start()
    cmd = ["gh", "search", "prs", "--author=@me", f"--merged-at=>={since}",
           "--limit", "200", "--json", "number,title,repository,createdAt,closedAt,state,url"]
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
