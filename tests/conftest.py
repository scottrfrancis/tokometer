"""tokometer test fixtures."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_tokometer(tmp_path, monkeypatch):
    """Isolate TOKOMETER_HOME to a tmp dir; create a fresh ledger.db with the
    new session_log_raw table applied so collectors/session_logs.py can write
    against it.
    """
    home = tmp_path / "tokometer_home"
    home.mkdir()
    (home / "state").mkdir()
    monkeypatch.setenv("TOKOMETER_HOME", str(home))

    db = home / "ledger.db"
    con = sqlite3.connect(str(db))
    con.executescript((Path(__file__).parent.parent / "schema.sql").read_text())
    con.executescript((Path(__file__).parent.parent / "schema_session_logs.sql").read_text())
    con.commit()
    con.close()
    return home


@pytest.fixture
def tmp_home_with_logs(tmp_path):
    """Create a fake ~ with a few session logs (global + per-repo)."""
    home = tmp_path / "fake_home"
    (home / ".claude" / "session-logs").mkdir(parents=True)
    repos = home / "repos"
    repos.mkdir()
    proj = repos / "myproject"
    (proj / ".claude" / "session-logs").mkdir(parents=True)

    (home / ".claude" / "session-logs" / "2026-06-13-1400-test-topic.md").write_text(
        "# Session\nbody"
    )
    (home / ".claude" / "session-logs" / "handoff-2026-06-13-2200.md").write_text(
        "# Handoff\nstuff"
    )
    (proj / ".claude" / "session-logs" / "2026-06-13-0900.md").write_text(
        "# Project session\ncontent"
    )
    (proj / ".claude" / "session-logs" / "2026-06-13-1000-improvements.md").write_text(
        "# Improvements\nthings"
    )
    return home
