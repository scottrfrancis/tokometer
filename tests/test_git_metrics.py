"""Tests for collectors/git_metrics.py -- repo/worktree discovery + dedup.

Bug 1: find_repos() only recognized `.git` as a directory, so `git worktree
add` checkouts (whose `.git` is a pointer FILE, e.g. `gitdir: /path/.git/
worktrees/<name>`) were silently skipped -- their commits never reached
commit_metric, even though they're a fully valid, independently-commit-able
checkout.

Bug 2: commit_metric's PK is (repo_path, sha); nothing deduped a commit
visible from two different checkouts of the SAME repo (a worktree shares
history with its parent; so do two independent clones), so LOC got
double-counted across repo_path values in reports (report_html.py GROUP BY
repo). The fix groups checkouts by a stable identity (root commit sha) and
attributes every member's commits to one canonical repo_path -- shared shas
collide harmlessly on the PK, shas unique to one checkout are still captured.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess

import pytest

from collectors import git_metrics as gm

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _run(args, cwd):
    env = {**os.environ, **GIT_ENV}
    result = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q", "-b", "main"], path)
    _run(["config", "user.email", GIT_ENV["GIT_AUTHOR_EMAIL"]], path)
    _run(["config", "user.name", GIT_ENV["GIT_AUTHOR_NAME"]], path)
    return path


def _commit(path, filename, content, message):
    (path / filename).write_text(content)
    _run(["add", filename], path)
    _run(["commit", "-q", "-m", message], path)
    return _run(["rev-parse", "HEAD"], path).strip()


def _clone(src, dest):
    _run(["clone", "-q", str(src), str(dest)], dest.parent)
    _run(["config", "user.email", GIT_ENV["GIT_AUTHOR_EMAIL"]], dest)
    _run(["config", "user.name", GIT_ENV["GIT_AUTHOR_NAME"]], dest)
    return dest


@pytest.fixture(autouse=True)
def _all_authors(monkeypatch):
    """Capture every commit made by the throwaway test repos regardless of
    whatever TOKOMETER_GIT_AUTHOR happens to be configured in the real env
    (git_metrics.AUTHORS is frozen at import time, like ledger.DB_PATH)."""
    monkeypatch.setattr(gm, "AUTHORS", ["*"])


# ---------- worktree detection (Bug 1) ----------------------------------

def test_is_git_checkout_true_for_normal_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert gm._is_git_checkout(str(repo)) is True


def test_is_git_checkout_true_for_worktree_pointer_file(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "hi", "init")
    wt = tmp_path / "repo-wt"
    _run(["worktree", "add", "-q", "-b", "feature", str(wt)], repo)
    assert (wt / ".git").is_file()  # confirms this is the FILE case, not dir
    assert gm._is_git_checkout(str(wt)) is True


def test_is_git_checkout_false_for_plain_directory(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert gm._is_git_checkout(str(plain)) is False


def test_find_repos_discovers_worktree_previously_invisible(tmp_path):
    scan_root = tmp_path / "workspace"
    repo = _init_repo(scan_root / "main-repo")
    _commit(repo, "a.txt", "hi", "init")
    # Nested under a dir literally named "worktrees" on purpose: the OLD
    # PRUNE_DIRS entry pruned traversal into any dir named "worktrees", which
    # would still hide this checkout even after fixing the isdir() check --
    # both halves of Bug 1 must be fixed together.
    wt = scan_root / "worktrees" / "main-repo-feature"
    wt.parent.mkdir(parents=True)
    _run(["worktree", "add", "-q", "-b", "feature", str(wt)], repo)

    found = gm.find_repos(root=str(scan_root))

    assert str(repo) in found
    assert str(wt) in found


# ---------- repo identity + grouping (Bug 2) -----------------------------

def test_repo_identity_stable_across_worktree_and_parent(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "hi", "init")
    wt = tmp_path / "repo-wt"
    _run(["worktree", "add", "-q", "-b", "feature", str(wt)], repo)
    assert gm.repo_identity(str(repo)) == gm.repo_identity(str(wt))


def test_repo_identity_stable_across_clones(tmp_path):
    src = _init_repo(tmp_path / "src")
    _commit(src, "a.txt", "hi", "init")
    clone = _clone(src, tmp_path / "clone")
    assert gm.repo_identity(str(src)) == gm.repo_identity(str(clone))


def test_repo_identity_returns_none_for_empty_repo(tmp_path):
    repo = _init_repo(tmp_path / "empty")
    assert gm.repo_identity(str(repo)) is None


def test_group_repos_groups_worktree_with_parent(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "hi", "init")
    wt = tmp_path / "repo-wt"
    _run(["worktree", "add", "-q", "-b", "feature", str(wt)], repo)

    groups = gm.group_repos([str(repo), str(wt)])

    assert len(groups) == 1
    canonical, members = groups[0]
    assert canonical == str(repo)
    assert set(members) == {str(repo), str(wt)}


def test_group_repos_keeps_unrelated_repos_separate(tmp_path):
    repo_a = _init_repo(tmp_path / "a")
    _commit(repo_a, "a.txt", "hi", "init a")
    repo_b = _init_repo(tmp_path / "b")
    _commit(repo_b, "b.txt", "hi", "init b")

    groups = gm.group_repos([str(repo_a), str(repo_b)])

    assert len(groups) == 2
    canonicals = {canonical for canonical, _ in groups}
    assert canonicals == {str(repo_a), str(repo_b)}


# ---------- end-to-end harvest() ------------------------------------------

def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def test_harvest_captures_worktree_only_commits(tmp_tokometer, tmp_path):
    """Verification (1): a worktree's own unique commit (made only on the
    worktree's branch, not reachable from the parent checkout) must be
    captured -- not silently dropped as it was before Bug 1's fix, and not
    thrown away by Bug 2's dedup either."""
    scan_root = tmp_path / "workspace"
    repo = _init_repo(scan_root / "proj")
    init_sha = _commit(repo, "a.txt", "v1", "init")
    wt = scan_root / "proj-wt"
    _run(["worktree", "add", "-q", "-b", "feature", str(wt)], repo)
    feature_sha = _commit(wt, "feature.txt", "new stuff", "feature commit")

    con = _connect(tmp_tokometer)
    gm.harvest(con=con, root=str(scan_root))
    rows = con.execute("SELECT repo_path, sha FROM commit_metric").fetchall()
    con.close()

    shas = {sha for _, sha in rows}
    assert init_sha in shas
    assert feature_sha in shas
    # both checkouts collapse onto ONE canonical repo_path (the main repo,
    # discovered first alphabetically), not split across "proj"/"proj-wt".
    assert {rp for rp, _ in rows} == {str(repo)}


def test_harvest_dedupes_duplicate_clones(tmp_tokometer, tmp_path):
    """Verification (2): two full clones of the same repo must record their
    shared commit(s) only ONCE in commit_metric, not once per clone."""
    scan_root = tmp_path / "workspace"
    scan_root.mkdir()
    src = _init_repo(scan_root / "orig")
    sha = _commit(src, "a.txt", "v1", "init")
    _clone(src, scan_root / "orig-clone")

    con = _connect(tmp_tokometer)
    gm.harvest(con=con, root=str(scan_root))
    rows = con.execute("SELECT repo_path, sha FROM commit_metric").fetchall()
    con.close()

    assert len(rows) == 1
    assert rows[0][1] == sha


def test_harvest_single_repo_scan_unaffected(tmp_tokometer, tmp_path):
    """Verification (3): a normal single-repo scan (no worktrees, no
    duplicates) must still work exactly as before -- no regression."""
    scan_root = tmp_path / "workspace"
    repo = _init_repo(scan_root / "solo")
    sha1 = _commit(repo, "a.txt", "v1", "first")
    sha2 = _commit(repo, "b.py", "print('hi')\n", "second")

    con = _connect(tmp_tokometer)
    changed = gm.harvest(con=con, root=str(scan_root))
    rows = con.execute(
        "SELECT repo, repo_path, sha, files FROM commit_metric"
    ).fetchall()
    con.close()

    # commits can land in the same author-date second under a fast test run,
    # so assert on the (unordered) set of shas rather than insertion order.
    assert changed == 2
    assert {r[2] for r in rows} == {sha1, sha2}
    assert all(r[0] == "solo" for r in rows)
    assert all(r[1] == str(repo) for r in rows)


def test_harvest_returns_zero_when_no_repos_found(tmp_tokometer, tmp_path):
    empty_root = tmp_path / "empty_workspace"
    empty_root.mkdir()
    con = _connect(tmp_tokometer)
    changed = gm.harvest(con=con, root=str(empty_root))
    con.close()
    assert changed == 0
