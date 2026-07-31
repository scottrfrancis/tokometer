"""Tests for collectors/droid.py."""

from __future__ import annotations

import json
import sqlite3

import pytest

from collectors import droid
from lib import ledger


@pytest.fixture
def droid_sessions(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    (root / "proj1").mkdir(parents=True)
    monkeypatch.setattr(droid, "SETTINGS_GLOB", str(root / "*" / "*.settings.json"))
    return root


def _write_settings(root, sid, tokens=None):
    tokens = tokens if tokens is not None else {"inputTokens": 10, "outputTokens": 20}
    path = root / "proj1" / f"{sid}.settings.json"
    path.write_text(json.dumps({"model": "gpt-x", "tokenUsage": tokens}))
    return path


def test_harvest_skips_unreadable_file_and_logs_warning(
    tmp_tokometer, droid_sessions, monkeypatch, capsys
):
    """A persistently unreadable/malformed session sidecar must not be dropped
    with zero operator-visible signal -- it should still be skipped, but a
    warning naming the file and the error must land on stderr, and good
    sessions alongside it must still be harvested."""
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_tokometer / "ledger.db"))

    _write_settings(droid_sessions, "good-session")
    bad_path = droid_sessions / "proj1" / "bad-session.settings.json"
    bad_path.write_text("{not valid json")

    changed = droid.harvest()
    assert changed == 1

    err = capsys.readouterr().err
    assert str(bad_path) in err
    assert "skipping" in err.lower()

    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    rows = con.execute("SELECT session_id FROM usage").fetchall()
    con.close()
    assert rows == [("good-session",)]
