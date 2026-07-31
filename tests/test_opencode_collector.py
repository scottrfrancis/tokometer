"""Tests for collectors/opencode.py."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from collectors import opencode
from lib import ledger


def _make_opencode_db(path):
    oc = sqlite3.connect(str(path))
    oc.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, data TEXT)"
    )
    oc.commit()
    oc.close()


def _insert_message(path, mid, sid, time_created, **tok_overrides):
    tokens = {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    tokens.update(tok_overrides)
    data = {
        "role": "assistant",
        "tokens": tokens,
        "cost": 0.0,
        "modelID": "test-model",
        "providerID": "test-provider",
        "time": {"created": time_created},
        "path": {"cwd": "/tmp/proj"},
    }
    oc = sqlite3.connect(str(path))
    oc.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?,?,?,?)",
        (mid, sid, time_created, json.dumps(data)),
    )
    oc.commit()
    oc.close()


def _now_ms():
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


@pytest.fixture
def opencode_db(tmp_path, monkeypatch):
    db_path = tmp_path / "opencode.db"
    _make_opencode_db(db_path)
    monkeypatch.setenv("OPENCODE_DB", str(db_path))
    return db_path


def test_fresh_scan_skips_rows_older_than_retention(tmp_tokometer, opencode_db, monkeypatch):
    """Fix 2a: opencode.py must apply the same retention-cutoff guard as every
    other token-usage collector. If the state file is lost/reset, a fresh scan
    from epoch 0 must not resurrect rows monthly.py already pruned."""
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_tokometer / "ledger.db"))

    old_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)
    old_ms = int(old_dt.timestamp() * 1000)
    _insert_message(opencode_db, "old-msg", "sess1", old_ms)

    inserted = opencode.harvest()
    assert inserted == 0

    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    count = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    con.close()
    assert count == 0


def test_watermark_tie_is_not_dropped_on_next_run(tmp_tokometer, opencode_db, monkeypatch):
    """Fix 2b: a message written with time_created exactly equal to the saved
    high-water mark must still be picked up on a later run (loosen > to >=);
    uid-based INSERT OR IGNORE dedup makes the re-included overlap safe."""
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_tokometer / "ledger.db"))

    tie_ts = _now_ms()
    _insert_message(opencode_db, "msg-a", "sess1", tie_ts)

    first = opencode.harvest()
    assert first == 1

    # A second message lands later but shares the exact same time_created
    # (the watermark saved by the first run) -- plausible for rapid/parallel events.
    _insert_message(opencode_db, "msg-b", "sess1", tie_ts)

    second = opencode.harvest()
    assert second == 1  # msg-b picked up; msg-a re-read but ignored by uid dedup

    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    count = con.execute("SELECT COUNT(*) FROM usage WHERE harness='opencode'").fetchone()[0]
    uids = {r[0] for r in con.execute("SELECT uid FROM usage").fetchall()}
    con.close()
    assert count == 2
    assert uids == {"opencode:msg-a", "opencode:msg-b"}
