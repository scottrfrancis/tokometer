"""Tests for collectors/copilot_observe.py — the manual observation entry point."""

from __future__ import annotations

import sqlite3

from collectors import copilot_observe as obs


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def test_record_rating(tmp_tokometer):
    con = _connect(tmp_tokometer)
    obs.record(con=con, quality=4, note="solid refactor")
    row = con.execute("SELECT kind, quality, note FROM manual_obs").fetchone()
    con.close()
    assert row == ("rating", 4, "solid refactor")


def test_record_stall_and_continue(tmp_tokometer):
    con = _connect(tmp_tokometer)
    obs.record(con=con, kind="stall")
    obs.record(con=con, kind="continue_prompt")
    kinds = [k for (k,) in con.execute("SELECT kind FROM manual_obs ORDER BY id")]
    con.close()
    assert kinds == ["stall", "continue_prompt"]


def test_record_rejects_bad_quality(tmp_tokometer):
    con = _connect(tmp_tokometer)
    try:
        obs.record(con=con, quality=9)
        raised = False
    except ValueError:
        raised = True
    con.close()
    assert raised


def test_main_argv_rating(tmp_tokometer):
    rc = obs.main(["4", "good plan, slow finish"])
    assert rc == 0
    con = _connect(tmp_tokometer)
    row = con.execute("SELECT quality, note FROM manual_obs").fetchone()
    con.close()
    assert row == (4, "good plan, slow finish")


def test_main_argv_stall_flag(tmp_tokometer):
    rc = obs.main(["--stall"])
    assert rc == 0
    con = _connect(tmp_tokometer)
    (kind,) = con.execute("SELECT kind FROM manual_obs").fetchone()
    con.close()
    assert kind == "stall"
