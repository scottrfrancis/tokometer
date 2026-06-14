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
