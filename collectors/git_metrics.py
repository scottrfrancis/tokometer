#!/usr/bin/env python3
"""Git code-metrics collector.

Scans git repos under a root (default ~/workspace), reads commits authored by
the configured email(s) since the start of the current month, and records
per-commit LOC by file role (code / docs / tests) into the `commit_metric` table.

Discovery is RECURSIVE (TOKOMETER_GIT_DEPTH levels, default 4) so nested layouts like
workspace/<group>/<repo> are picked up, not just top-level dirs. Vendored/build
trees (node_modules, .venv, dist, ...) are pruned; hidden dirs (incl. .git) are
never descended into. A checkout is recognized whether `.git` is a directory
(normal repo) or a FILE containing a `gitdir: ...` pointer (a `git worktree add`
checkout) -- both are discovered and scanned.

Because a worktree shares its commit object database with its parent checkout,
the SAME commit sha can be reachable from more than one discovered checkout (a
worktree and its parent, or two independent clones of the same repo). Rather
than dropping those checkouts, each discovered repo is grouped by a stable
identity (see repo_identity()) and every checkout in a group has its commits
attributed to ONE canonical repo_path -- shared shas collide harmlessly on the
(repo_path, sha) primary key (INSERT OR REPLACE), while shas unique to one
checkout (e.g. commits made only in a worktree) are still captured. The `repo`
label mirrors the report's repo_of() (first path segment under .../workspace,
else the leaf dir), computed from the canonical path, so duplicate checkouts
roll up into one repo_path/repo pair instead of splitting LOC across several.

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

# dirs we never descend into when hunting for repos (vendored code, build output).
# NOTE: a directory literally named "worktrees" is deliberately NOT pruned here --
# that used to be an (ineffective) attempt to dodge worktree double-counting, but
# it also hid real `git worktree add` checkouts nested under such a dir. Worktrees
# are now detected properly (see _is_git_checkout) and de-duplicated by identity
# (see repo_identity/group_repos), so no directory-name-based special case is needed.
PRUNE_DIRS = {"node_modules", ".venv", "venv", "vendor", ".tox", "dist", "build",
              ".next", "target", ".gradle", ".cache", "__pycache__"}

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(\.|_|-)(test|spec)\.", re.I)
DOC_EXT = (".md", ".rst", ".adoc", ".txt")


def repo_label(path):
    """Match report_html.repo_of: first segment under .../workspace, else leaf.

    Normalize Windows backslashes first so mixed-separator paths
    (e.g. ``C:\\Users\\me/workspace\\repo``) resolve to the leaf, not the tail.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p]
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


def _is_git_checkout(dirpath):
    """True if dirpath is a git repo root OR a `git worktree add` checkout.

    A normal repo has `.git` as a directory. A worktree checkout has `.git` as a
    FILE containing a `gitdir: <path-to-main-repo>/.git/worktrees/<name>`
    pointer -- previously this was missed entirely (isdir() is False for a
    file), silently dropping every worktree's commit history from the ledger.
    """
    git_path = os.path.join(dirpath, ".git")
    if os.path.isdir(git_path):
        return True
    if os.path.isfile(git_path):
        try:
            with open(git_path, errors="replace") as f:
                head = f.read(200)
        except OSError:
            return False
        return head.startswith("gitdir:")
    return False


def find_repos(root=None):
    """Recursively find git repos/worktrees under root (bounded depth, vendor
    dirs pruned).

    Nested repos are kept (a repo inside another repo, e.g. group/project, is its
    own checkout -- git does not cross into it, so there is no double counting).
    Duplicate checkouts of the SAME repo (worktrees, or two clones) are still
    returned here -- de-duplication by identity happens later in group_repos(),
    once each checkout's own history has actually been discovered.
    """
    root = root or ROOT
    repos = []
    base = root.rstrip("/").count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        if _is_git_checkout(dirpath):
            repos.append(dirpath)
        # prune traversal: depth cap, vendor dirs, and hidden dirs (incl. .git)
        if dirpath.rstrip("/").count(os.sep) - base >= MAXDEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in PRUNE_DIRS and not d.startswith(".")]
    return sorted(repos)


def repo_identity(repo):
    """Stable identity for a checkout, used to group worktrees/duplicate clones
    of the same repo so their commits can be attributed to one canonical
    repo_path instead of splitting/double-counting across several.

    Uses the repo's root commit sha(s) (`git rev-list --max-parents=0 HEAD`),
    which is stable across clones and worktrees of the same repo -- including
    worktrees, which always share their parent's root commit by construction.
    Deliberately NOT the 'origin' remote URL: a plain `git clone` sets origin
    on the CLONE to point at its source, but the source repo itself has no
    origin configured -- that asymmetry would make a repo and its own clone
    resolve to different identities, defeating the dedup for the common case.
    Returns None when a repo has no commits yet (nothing to dedupe there).
    """
    roots = sorted(git(repo, "rev-list", "--max-parents=0", "HEAD").split())
    if roots:
        return f"root:{','.join(roots)}"
    return None


def group_repos(repos):
    """Group discovered checkouts by stable identity (see repo_identity).

    Returns a list of (canonical_path, [member_paths]) tuples, one per distinct
    identity, in first-seen order. canonical_path is the first-discovered path
    for that identity -- every member checkout's commits get attributed to it,
    so a worktree and its parent (or two duplicate clones) roll up into ONE
    repo_path in commit_metric. Checkouts with no resolvable identity (empty
    repos) are each their own singleton group. Logs every non-canonical member
    for visibility.
    """
    order = []
    members = {}
    for repo in repos:
        identity = repo_identity(repo)
        key = ("solo", repo) if identity is None else ("id", identity)
        if key not in members:
            order.append(key)
            members[key] = []
        members[key].append(repo)

    groups = []
    for key in order:
        paths = members[key]
        canonical = paths[0]
        for extra in paths[1:]:
            print(f"[git] {extra}: same repo as {canonical} (identity match) -- "
                  f"commits attributed to {canonical}", file=sys.stderr)
        groups.append((canonical, paths))
    return groups


SEP = "__COMMIT__"


def _harvest_repo(repo, name, canonical, since, author_args, rows):
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
            cur = {"repo": name, "repo_path": canonical, "sha": sha,
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


def harvest(con=None, root=None):
    """Scan and record commit metrics.

    con: an existing sqlite3 connection to write to (caller owns/closes it);
         defaults to ledger.connect(), opened and closed here.
    root: scan root override; defaults to ROOT (module-level, env-configured).
    """
    since = month_start_iso()
    all_repos = find_repos(root)
    if not all_repos:
        print(f"[git] no repos under {root or ROOT}; skipping", file=sys.stderr)
        return 0

    groups = group_repos(all_repos)

    # author filter: one --author per configured email (git ORs them); '*' = all
    author_args = []
    if AUTHORS and AUTHORS != ["*"]:
        for a in AUTHORS:
            author_args += [f"--author={a}"]

    rows = []
    for canonical, paths in groups:
        name = repo_label(canonical)
        for repo in paths:
            _harvest_repo(repo, name, canonical, since, author_args, rows)

    owns_con = con is None
    con = con or ledger.connect()
    cols = ("repo", "repo_path", "sha", "ts", "author", "files",
            "code_add", "code_del", "docs_add", "docs_del",
            "test_add", "test_del", "is_merge")
    sql = (f"INSERT OR REPLACE INTO commit_metric ({','.join(cols)}) "
           f"VALUES ({','.join('?' for _ in cols)})")
    before = con.total_changes
    con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    changed = con.total_changes - before
    if owns_con:
        con.close()
    print(f"[git] {len(all_repos)} checkouts ({len(groups)} distinct repos), "
          f"{len(rows)} commit-checkout observations, {changed} rows written "
          f"since {since}", file=sys.stderr)
    return changed


if __name__ == "__main__":
    harvest()
