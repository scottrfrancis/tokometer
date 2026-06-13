#!/usr/bin/env python3
"""Git code-metrics collector.

Scans git repos under a root (default ~/workspace), reads commits authored by
the configured email(s) since the start of the current month, and records
per-commit LOC by file role (code / docs / tests) into the `commit_metric` table.

Discovery is RECURSIVE (TOKOMETER_GIT_DEPTH levels, default 4) so nested layouts like
workspace/<group>/<repo> are picked up, not just top-level dirs. Vendored/worktree
trees (node_modules, .venv, .claude/worktrees, ...) are pruned. The `repo` label
mirrors the report's repo_of() (first path segment under .../workspace, else the
leaf dir) so commit metrics JOIN the usage side in the Sankey; `repo_path` keeps
the absolute path for audit.

TOKOMETER_GIT_AUTHOR may be a comma-separated list of emails (matched as OR); set it to
'*' to count every author. Local + read-only against git. Idempotent: rows keyed by
(repo_path, sha), re-inserted with REPLACE so re-runs refresh counts harmlessly.
"""
import os
import re
import sys
import subprocess
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

ROOT = os.path.expanduser(os.environ.get("TOKOMETER_GIT_ROOT", "~/workspace"))
AUTHORS = [a.strip() for a in os.environ.get("TOKOMETER_GIT_AUTHOR",
           "you@example.com").split(",") if a.strip()]
MAXDEPTH = int(os.environ.get("TOKOMETER_GIT_DEPTH", "4"))

# dirs we never descend into when hunting for repos (vendored code, build output,
# git worktrees that would double-count the parent repo's history)
PRUNE_DIRS = {"node_modules", ".venv", "venv", "vendor", ".tox", "dist", "build",
              ".next", "target", ".gradle", ".cache", "__pycache__", "worktrees"}

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(\.|_|-)(test|spec)\.", re.I)
DOC_EXT = (".md", ".rst", ".adoc", ".txt")


def repo_label(path):
    """Match report_html.repo_of: first segment under .../workspace, else leaf."""
    parts = [p for p in path.split("/") if p]
    if "workspace" in parts:
        i = parts.index("workspace")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else path


def classify(path):
    p = path.lower()
    if TEST_RE.search(p):
        return "test"
    if p.endswith(DOC_EXT) or "/docs/" in p or p.startswith("docs/") or "readme" in os.path.basename(p):
        return "docs"
    return "code"


def month_start_iso():
    now = dt.datetime.now()
    return dt.date(now.year, now.month, 1).isoformat()


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True, timeout=60).stdout


def find_repos():
    """Recursively find git repos under ROOT (bounded depth, vendor dirs pruned).

    Nested repos are kept (a repo inside another repo, e.g. group/project, is its
    own checkout -- git does not cross into it, so there is no double counting).
    """
    repos = []
    base = ROOT.rstrip("/").count(os.sep)
    for dirpath, dirnames, _ in os.walk(ROOT):
        if os.path.isdir(os.path.join(dirpath, ".git")):
            repos.append(dirpath)
        # prune traversal: depth cap, vendor dirs, and hidden dirs (incl. .git)
        if dirpath.rstrip("/").count(os.sep) - base >= MAXDEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in PRUNE_DIRS and not d.startswith(".")]
    return sorted(repos)


SEP = "__COMMIT__"


def harvest():
    since = month_start_iso()
    repos = find_repos()
    if not repos:
        print(f"[git] no repos under {ROOT}; skipping", file=sys.stderr)
        return 0

    # author filter: one --author per configured email (git ORs them); '*' = all
    author_args = []
    if AUTHORS and AUTHORS != ["*"]:
        for a in AUTHORS:
            author_args += [f"--author={a}"]

    rows = []
    for repo in repos:
        name = repo_label(repo)
        out = git(repo, "log", f"--since={since}", *author_args,
                  "--no-merges", "--date=iso-strict",
                  f"--pretty=format:{SEP}%H%x09%aI%x09%ae", "--numstat")
        cur = None
        for line in out.splitlines():
            if line.startswith(SEP):
                if cur:
                    rows.append(cur)
                sha, _, rest = line[len(SEP):].partition("\t")
                ts, _, email = rest.partition("\t")
                cur = {"repo": name, "repo_path": repo, "sha": sha,
                       "ts": ledger.normalize_iso(ts), "author": email or None,
                       "files": 0, "code_add": 0, "code_del": 0,
                       "docs_add": 0, "docs_del": 0, "test_add": 0, "test_del": 0,
                       "is_merge": 0}
                continue
            if not line.strip() or cur is None:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            add = 0 if a == "-" else int(a or 0)
            dele = 0 if d == "-" else int(d or 0)
            role = classify(path)
            cur["files"] += 1
            cur[f"{role}_add"] += add
            cur[f"{role}_del"] += dele
        if cur:
            rows.append(cur)

    con = ledger.connect()
    cols = ("repo", "repo_path", "sha", "ts", "author", "files",
            "code_add", "code_del", "docs_add", "docs_del",
            "test_add", "test_del", "is_merge")
    sql = (f"INSERT OR REPLACE INTO commit_metric ({','.join(cols)}) "
           f"VALUES ({','.join('?' for _ in cols)})")
    before = con.total_changes
    con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    changed = con.total_changes - before
    con.close()
    print(f"[git] {len(repos)} repos, {len(rows)} commits since {since}, {changed} written",
          file=sys.stderr)
    return changed


if __name__ == "__main__":
    harvest()
