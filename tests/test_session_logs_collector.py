"""Tests for collectors/session_logs.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from collectors import session_logs as sl


def test_classify_filename_session():
    out = sl._classify_filename("2026-06-13-1400-topic-here.md")
    assert out is not None
    assert out["kind"] == "session"
    assert out["log_date"] == "2026-06-13"
    assert out["log_time_local"] == "14:00"
    assert out["topic"] == "topic-here"


def test_classify_filename_handoff():
    out = sl._classify_filename("handoff-2026-06-13-2200.md")
    assert out is not None
    assert out["kind"] == "handoff"
    assert out["log_date"] == "2026-06-13"
    assert out["log_time_local"] == "22:00"


def test_classify_filename_mine_report():
    out = sl._classify_filename("mine-report-2026-06-13.md")
    assert out is not None
    assert out["kind"] == "mine-report"


def test_classify_filename_invalid_returns_none():
    assert sl._classify_filename("not-a-session-log.txt") is None
    assert sl._classify_filename("random.md") is None


def test_iter_session_log_paths_finds_both_scopes(tmp_home_with_logs):
    paths = list(sl.iter_session_log_paths(home=tmp_home_with_logs,
                                            repo_root=tmp_home_with_logs / "repos"))
    scopes = {p[1] for p in paths}
    assert {"global", "project"}.issubset(scopes)
    # The project ones should carry source_project='myproject'
    projects = {p[2] for p in paths if p[1] == "project"}
    assert projects == {"myproject"}


def test_collect_inserts_rows(tmp_tokometer, tmp_home_with_logs):
    db_path = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db_path))
    result = sl.collect(con=con, home=tmp_home_with_logs,
                        repo_root=tmp_home_with_logs / "repos")
    con.close()
    # 4 files in fixture, all valid filenames -> 4 inserted
    assert result["scanned"] == 4
    assert result["inserted"] == 4
    assert result["skipped"] == 0

    con = sqlite3.connect(str(db_path))
    count = con.execute("SELECT COUNT(*) FROM session_log_raw").fetchone()[0]
    con.close()
    assert count == 4


def test_iter_finds_bare_project_session_logs(tmp_path):
    """Repos that keep logs at <repo>/session-logs/ (project-root convention,
    not under .claude/) must be discovered."""
    repos = tmp_path / "ws"
    proj = repos / "coder"
    (proj / "session-logs").mkdir(parents=True)
    (proj / "session-logs" / "2026-06-13-0900.md").write_text("x")
    paths = list(sl.iter_session_log_paths(home=tmp_path / "nohome", repo_root=repos))
    proj_paths = [p for p in paths if p[1] == "project"]
    assert len(proj_paths) == 1
    assert proj_paths[0][2] == "coder"


def test_iter_finds_nested_repo_session_logs(tmp_path):
    """Repos nested two levels deep (e.g. group/subrepo/session-logs) must be
    discovered, not just direct children of repo_root."""
    repos = tmp_path / "ws"
    nested = repos / "Catalyst-RCM" / "Dashboard"
    (nested / "session-logs").mkdir(parents=True)
    (nested / "session-logs" / "2026-06-13-1000-x.md").write_text("y")
    paths = list(sl.iter_session_log_paths(home=tmp_path / "nohome", repo_root=repos))
    proj = [p for p in paths if p[1] == "project"]
    assert any(p[2] == "Dashboard" for p in proj)


def test_iter_prunes_heavy_dirs(tmp_path):
    """session-logs dirs buried inside node_modules/.git must be ignored."""
    repos = tmp_path / "ws"
    nm = repos / "app" / "node_modules" / "pkg"
    (nm / "session-logs").mkdir(parents=True)
    (nm / "session-logs" / "2026-06-13-1100.md").write_text("z")
    paths = list(sl.iter_session_log_paths(home=tmp_path / "nohome", repo_root=repos))
    assert all("node_modules" not in str(p[0]) for p in paths)


def test_rel_filepath_bare_and_claude(tmp_path):
    home = tmp_path / "h"
    bare = Path("/ws/coder/session-logs/2026-06-13-0900.md")
    assert sl._rel_filepath(bare, "project", "coder", home) == \
        "session-logs/2026-06-13-0900.md"
    claude = Path("/ws/coder/.claude/session-logs/foo.md")
    assert sl._rel_filepath(claude, "project", "coder", home) == \
        ".claude/session-logs/foo.md"


def test_collect_warns_when_repo_root_yields_nothing(tmp_tokometer, tmp_path, capsys):
    """If TOKOMETER_REPO_ROOT exists but no session-logs dirs are found (e.g. the
    launchd context can't read an external volume), fail loud on stderr instead of
    silently reporting a clean zero-row scan -- mirrors git_metrics' 'no repos' line."""
    import sqlite3
    empty = tmp_path / "empty_ws"
    empty.mkdir()
    db = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db))
    sl.collect(con=con, home=tmp_path / "nohome", repo_root=empty)
    con.close()
    err = capsys.readouterr().err
    assert "no session-logs" in err.lower()
    assert str(empty) in err


def test_collect_is_idempotent(tmp_tokometer, tmp_home_with_logs):
    db_path = tmp_tokometer / "ledger.db"
    for _ in range(2):
        con = sqlite3.connect(str(db_path))
        sl.collect(con=con, home=tmp_home_with_logs,
                   repo_root=tmp_home_with_logs / "repos")
        con.close()
    con = sqlite3.connect(str(db_path))
    count = con.execute("SELECT COUNT(*) FROM session_log_raw").fetchone()[0]
    con.close()
    assert count == 4
