"""TOKOMETER_COLLECT_SINCE: a machine that starts counting on a given day.

Set when a machine is brought onto a new build or a collector is first switched
on. Collectors re-read whatever is on disk, so without a start date they would
add rows for past days (subagent transcripts an older build never read, months
of OpenClaw history) and change totals that have already been reported.
"""

from __future__ import annotations

import sqlite3

import pytest

from lib import ledger


def _row(uid: str, ts: str) -> dict:
    return {"uid": uid, "ts": ts, "harness": "claude-code", "source": "session-file",
            "confidence": "exact", "input_tokens": 10}


@pytest.fixture
def con(tmp_tokometer):
    c = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    yield c
    c.close()


def _uids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT uid FROM usage")}


def test_rows_before_the_start_date_are_not_stored(con, monkeypatch, capsys):
    monkeypatch.setenv("TOKOMETER_COLLECT_SINCE", "2026-11-28")

    added = ledger.insert_usage(con, [_row("old", "2026-11-26T12:00:00Z"),
                                      _row("new", "2026-11-29T12:00:00Z")])

    assert _uids(con) == {"new"}
    assert added == 1
    assert "1 row before TOKOMETER_COLLECT_SINCE=2026-11-28" in capsys.readouterr().err


def test_the_start_date_applies_to_upserts_too(con, monkeypatch):
    monkeypatch.setenv("TOKOMETER_COLLECT_SINCE", "2026-11-28")

    ledger.insert_usage(con, [_row("old", "2026-11-26T12:00:00Z")], on_conflict="replace")

    assert _uids(con) == set()


def test_without_a_start_date_every_row_is_stored(con, monkeypatch):
    monkeypatch.delenv("TOKOMETER_COLLECT_SINCE", raising=False)

    ledger.insert_usage(con, [_row("old", "2026-01-02T12:00:00Z"),
                              _row("new", "2026-11-29T12:00:00Z")])

    assert _uids(con) == {"old", "new"}


def test_a_malformed_start_date_fails_loudly(con, monkeypatch):
    """A typo must not silently drop every row (or keep every row)."""
    monkeypatch.setenv("TOKOMETER_COLLECT_SINCE", "28/11/2026")

    with pytest.raises(ValueError, match="TOKOMETER_COLLECT_SINCE"):
        ledger.insert_usage(con, [_row("new", "2026-11-29T12:00:00Z")])
