"""Tests for report_html.py::headline() cache-read-ratio surfacing.

cache_read_tokens can dominate the input-side token count (97%+ on a real
box) and is the biggest driver of actual cost, but until now it was only
ever shown as a raw number -- never as a ratio against the rest of the
input-side tokens. These tests pin down the computed
cache_read/(input+cache_read+cache_write) ratio, both per-harness in the
table and as an overall MTD summary card, including the zero-division edge
case (a harness/period with no input-side tokens at all).
"""

from __future__ import annotations

import sqlite3

import report_html as rh
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
    # input=100, cache_read=900, cache_write=0 -> 900/(100+900+0) = 90%
    _insert(con, "u1", NOW, "claude-code", 100, 50, 900, 0)
    con.commit()

    html = rh.headline(con)
    con.close()

    assert "90%" in html


def test_headline_cache_hit_ratio_accounts_for_cache_write(tmp_tokometer):
    con = _connect(tmp_tokometer)
    # input=0, cache_read=800, cache_write=200 -> 800/(0+800+200) = 80%
    _insert(con, "u1", NOW, "droid", 0, 10, 800, 200)
    con.commit()

    html = rh.headline(con)
    con.close()

    assert "80%" in html


def test_headline_cache_hit_ratio_zero_division_shows_dash(tmp_tokometer):
    con = _connect(tmp_tokometer)
    # all-zero input-side tokens (e.g. a credits-only record) -> guard, no crash
    _insert(con, "u1", NOW, "cursor", 0, 0, 0, 0, credits=5)
    con.commit()

    html = rh.headline(con)  # must not raise ZeroDivisionError
    con.close()

    assert "—" in html  # em dash placeholder


def test_headline_overall_cache_hit_card(tmp_tokometer):
    con = _connect(tmp_tokometer)
    # two harnesses combine to an overall ratio distinct from either row's own ratio
    _insert(con, "u1", NOW, "claude-code", 100, 50, 900, 0)   # 90%
    _insert(con, "u2", NOW, "droid", 800, 50, 200, 0)         # 20%
    con.commit()

    html = rh.headline(con)
    con.close()

    # overall: cache_read=1100 / (input=900 + cache_read=1100 + cache_write=0) = 55%
    assert "55%" in html
    assert "cache hit" in html.lower()


def test_headline_still_shows_raw_cache_read_tokens(tmp_tokometer):
    """The raw cache_read_tokens display must remain -- ratio is additive."""
    con = _connect(tmp_tokometer)
    _insert(con, "u1", NOW, "claude-code", 100, 50, 12_500_000, 0)
    con.commit()

    html = rh.headline(con)
    con.close()

    assert "12.5M" in html  # human() formatting of the raw cache_read total
