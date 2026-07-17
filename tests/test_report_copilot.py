"""Tests for report_copilot.py — the laptop daily/weekly strategy report."""

from __future__ import annotations

import sqlite3

import report_copilot as rc


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def _seed(con):
    usage = [
        # ts (UTC noon avoids local-date rollover in any US tz), model, in, out, conf
        ("u1", "2026-07-15T15:00:00Z", "gpt-5.3-codex", 900, 200, "exact"),
        ("u2", "2026-07-15T16:00:00Z", "gpt-5.3-codex", 800, 150, "exact"),
        ("u3", "2026-07-15T16:30:00Z", "claude-haiku-4-5-20251001", 100, 50, "exact"),
        ("u4", "2026-07-15T17:00:00Z", "claude-haiku-4-5-20251001", 0, 0, "estimate"),
        ("u5", "2026-07-08T15:00:00Z", "gpt-5.3-codex", 500, 100, "exact"),  # prior week
    ]
    for uid, ts, model, i, o, conf in usage:
        con.execute(
            "INSERT INTO usage (uid, ts, harness, model, input_tokens, output_tokens,"
            " source, confidence) VALUES (?, ?, 'copilot', ?, ?, ?, 'vscode-chat-log', ?)",
            (uid, ts, model, i, o, conf))
    events = [
        ("e1", "2026-07-15T16:20:00Z", "worker_oom", "client-oom"),
        ("e2", "2026-07-15T16:25:00Z", "model_downgrade", "client-oom-reroute"),
        ("e3", "2026-07-15T17:10:00Z", "power_throttle", None),
        ("e4", "2026-07-15T17:20:00Z", "slow_request", None),
    ]
    for uid, ts, kind, mech in events:
        con.execute(
            "INSERT INTO event (uid, ts, kind, mechanism, source)"
            " VALUES (?, ?, ?, ?, 'test')", (uid, ts, kind, mech))
    con.execute(
        "INSERT INTO manual_obs (ts, kind, quality) VALUES"
        " ('2026-07-15T16:40:00Z', 'rating', 2),"
        " ('2026-07-15T17:30:00Z', 'stall', NULL),"
        " ('2026-07-15T18:00:00Z', 'continue_prompt', NULL)")
    con.commit()


def test_model_mix_counts_requests_and_tokens(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _seed(con)
    mix = rc.model_mix(con, "2026-07-15", "2026-07-15")
    con.close()
    by_model = {m["model"]: m for m in mix}
    assert by_model["gpt-5.3-codex"]["requests"] == 2
    assert by_model["gpt-5.3-codex"]["output_tokens"] == 350
    assert by_model["claude-haiku-4-5-20251001"]["requests"] == 2
    assert by_model["claude-haiku-4-5-20251001"]["estimated"] == 1


def test_model_mix_respects_date_bounds(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _seed(con)
    week = rc.model_mix(con, "2026-07-09", "2026-07-15")
    con.close()
    total = sum(m["requests"] for m in week)
    assert total == 4          # u5 (07-08) excluded


def test_event_summary_counts_kinds(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _seed(con)
    ev = rc.event_summary(con, "2026-07-15", "2026-07-15")
    con.close()
    assert ev["worker_oom"] == 1
    assert ev["power_throttle"] == 1
    assert ev["slow_request"] == 1
    assert ev["stall"] == 1              # from manual_obs
    assert ev["continue_prompt"] == 1    # from manual_obs


def test_downgrades_lists_mechanisms(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _seed(con)
    d = rc.downgrades(con, "2026-07-15", "2026-07-15")
    con.close()
    assert len(d) == 1
    assert d[0]["mechanism"] == "client-oom-reroute"


def test_hourly_mix_buckets_by_local_hour(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _seed(con)
    hours = rc.hourly_mix(con, "2026-07-15", "2026-07-15")
    con.close()
    assert sum(h["requests"] for h in hours) == 4
    assert all(0 <= int(h["hour"]) <= 23 for h in hours)


def test_render_daily_contains_sections(tmp_tokometer):
    con = _connect(tmp_tokometer)
    _seed(con)
    md = rc.render(con, "2026-07-15", "2026-07-15", title="Copilot daily")
    con.close()
    for needle in ("Copilot daily", "Model mix", "Health & failure events",
                   "Downgrades", "gpt-5.3-codex", "client-oom-reroute"):
        assert needle in md, f"missing section: {needle}"


def test_render_empty_ledger_says_so(tmp_tokometer):
    con = _connect(tmp_tokometer)
    md = rc.render(con, "2026-07-15", "2026-07-15", title="Copilot daily")
    con.close()
    assert "no copilot activity" in md.lower()
