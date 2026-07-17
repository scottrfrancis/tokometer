"""Tests for lib/mechanisms.py — the post-harvest degradation classifier."""

from __future__ import annotations

import sqlite3

from lib import mechanisms


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def _usage(con, uid, ts, model):
    con.execute(
        "INSERT INTO usage (uid, ts, harness, model, source, confidence)"
        " VALUES (?, ?, 'copilot', ?, 'vscode-chat-log', 'exact')", (uid, ts, model))


def _event(con, uid, ts, kind, mechanism=None):
    con.execute(
        "INSERT INTO event (uid, ts, kind, mechanism, source)"
        " VALUES (?, ?, ?, ?, 'vscode-session-log')", (uid, ts, kind, mechanism))


def test_model_rank_orders_tiers():
    assert mechanisms.model_rank("claude-haiku-4-5-20251001") < \
           mechanisms.model_rank("gpt-5.3-codex")
    assert mechanisms.model_rank("gpt-4o-mini-2024-07-18") < \
           mechanisms.model_rank("claude-sonnet-4.6")


def test_downgrade_after_crash_labeled_client_oom_reroute(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _usage(con, "u1", "2026-07-17T15:00:00Z", "gpt-5.3-codex")
    _event(con, "e1", "2026-07-17T15:01:00Z", "worker_oom", "client-oom")
    _usage(con, "u2", "2026-07-17T15:02:00Z", "claude-haiku-4-5-20251001")
    con.commit()
    n = mechanisms.classify(con)
    row = con.execute(
        "SELECT mechanism FROM event WHERE kind='model_downgrade'").fetchone()
    con.close()
    assert n == 1
    assert row[0] == "client-oom-reroute"


def test_downgrade_without_nearby_events_labeled_downroute(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _usage(con, "u1", "2026-07-17T15:00:00Z", "gpt-5.3-codex")
    _usage(con, "u2", "2026-07-17T18:00:00Z", "claude-haiku-4-5-20251001")
    con.commit()
    mechanisms.classify(con)
    row = con.execute(
        "SELECT mechanism FROM event WHERE kind='model_downgrade'").fetchone()
    con.close()
    assert row[0] == "downroute"


def test_downgrade_near_rate_limit_labeled_quota(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _usage(con, "u1", "2026-07-17T15:00:00Z", "gpt-5.3-codex")
    _event(con, "e1", "2026-07-17T15:00:30Z", "request_failure")
    _usage(con, "u2", "2026-07-17T15:01:00Z", "gpt-4o-mini-2024-07-18")
    con.commit()
    mechanisms.classify(con)
    row = con.execute(
        "SELECT mechanism FROM event WHERE kind='model_downgrade'").fetchone()
    con.close()
    assert row[0] == "quota"


def test_classify_is_idempotent(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _usage(con, "u1", "2026-07-17T15:00:00Z", "gpt-5.3-codex")
    _usage(con, "u2", "2026-07-17T18:00:00Z", "claude-haiku-4-5-20251001")
    con.commit()
    mechanisms.classify(con)
    mechanisms.classify(con)
    (n,) = con.execute(
        "SELECT COUNT(*) FROM event WHERE kind='model_downgrade'").fetchone()
    con.close()
    assert n == 1


def test_no_downgrade_no_events(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _usage(con, "u1", "2026-07-17T15:00:00Z", "claude-haiku-4-5-20251001")
    _usage(con, "u2", "2026-07-17T15:05:00Z", "gpt-5.3-codex")   # upgrade, not downgrade
    con.commit()
    n = mechanisms.classify(con)
    con.close()
    assert n == 0
