"""Tests for report_morning.py::headline() cache-read-ratio surfacing.

Mirrors test_report_html_cache_ratio.py: cache_read_tokens dominates the
input-side token count on a real box and drives actual cost, but was never
shown as a ratio -- only a raw number. These tests pin down the same
cache_read/(input+cache_read+cache_write) ratio in the Markdown headline
table (plus a zero-division edge case) for consistency with report_html.py.
"""

from __future__ import annotations

import sqlite3

import report_morning as rm
from lib import ledger

# headline() is month-to-date: rows must fall in the current month.
NOW = ledger.iso_utc()


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def _insert(con, uid, ts, harness, i, o, cache_read, cache_write, credits=0):
    con.execute(
        "INSERT INTO usage (uid, ts, harness, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, credits, source, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', 'exact')",
        (uid, ts, harness, i, o, cache_read, cache_write, credits))


def test_headline_shows_cache_hit_ratio_per_harness(tmp_tokometer):
    con = _connect(tmp_tokometer)
    # input=100, cache_read=900, cache_write=0 -> 90%
    _insert(con, "u1", NOW, "claude-code", 100, 50, 900, 0)
    con.commit()

    md = rm.headline(con)
    con.close()

    assert "90%" in md


def test_headline_cache_hit_ratio_accounts_for_cache_write(tmp_tokometer):
    con = _connect(tmp_tokometer)
    # input=0, cache_read=800, cache_write=200 -> 80%
    _insert(con, "u1", NOW, "droid", 0, 10, 800, 200)
    con.commit()

    md = rm.headline(con)
    con.close()

    assert "80%" in md


def test_headline_cache_hit_ratio_zero_division_shows_dash(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _insert(con, "u1", NOW, "cursor", 0, 0, 0, 0, credits=5)
    con.commit()

    md = rm.headline(con)  # must not raise ZeroDivisionError
    con.close()

    assert "—" in md


def test_headline_still_shows_raw_cache_read_tokens(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _insert(con, "u1", NOW, "claude-code", 100, 50, 12_500_000, 0)
    con.commit()

    md = rm.headline(con)
    con.close()

    assert "12.5M" in md
