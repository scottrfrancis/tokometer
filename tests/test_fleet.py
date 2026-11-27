"""fleet.py: one report for every machine, built from Beaufort's bronze table.

Every machine pushes its ledger rows to bronze.tokometer_usage. On the hub,
fleet.py reads them back into an in-memory ledger (same columns, plus host,
arrival time and build), renders the morning report over all of them, and
writes a summary that Morning Kata prints verbatim.

Records below have the types psycopg2 returns for bronze rows: aware
datetimes, Decimal cost, JSONB meta as a dict. Expected values are worked out
by hand from lib/pricing.py rates, not by the code under test.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

import fleet
from lib import pricing

M = 1_000_000
NOW = dt.datetime(2026, 12, 3, 4, 40).astimezone()          # report time, local
TODAY = NOW.date()
YDAY = dt.datetime(2026, 12, 2, 10, 0).astimezone()          # inside "yesterday"
HUB_BUILD = "0d06d72"
EXPECTED = ["studio", "bladerunner14", "dev", "dev-ai", "hasami", "mini"]


def rec(host, ts, *, harness="claude-code", provider="anthropic", model="claude-opus-5-5",
        i=0, o=0, cr=0, cw=0, org="Personal", sub="max", build=HUB_BUILD,
        source="session-file", arrived=None, uid=None, **meta):
    m = {"source": source, "confidence": "exact", **meta}
    if org is not None:
        m["org"] = org
    if sub is not None:
        m["subscription"] = sub
    if build is not None:
        m["tokometer_version"] = build
    return {"uid": uid or f"{host}:{harness}:{ts.isoformat()}:{model}", "host": host, "ts": ts,
            "harness": harness, "provider": provider, "model": model, "account": "a",
            "session_id": "s", "cwd": "/w/repo", "input_tokens": i, "output_tokens": o,
            "cache_read": cr, "cache_write": cw, "cost_usd": Decimal("0"), "meta": m,
            "arrived_at": arrived or ts}


def heartbeat(host, at, build=HUB_BUILD):
    return rec(host, at, harness="tokometer", provider=None, model=None, org=None, sub=None,
               build=build, source="heartbeat", uid=f"heartbeat:{host}:{at.date()}")


def fleet_rows():
    """Yesterday: studio and dev on Opus, hasami's OpenClaw on dev-ai.
    Earlier this month: bladerunner14 on Sonnet. Before the month: studio."""
    return [
        rec("studio", YDAY, i=M),                                             # $4.00
        rec("dev", YDAY, i=M, org="Acme", sub="team", build="4d43908"),     # $4.00
        rec("hasami", YDAY, harness="openclaw", provider="dev-ai", model="workhorse",
            i=M, org="Local", sub="self-hosted"),                             # $0.1875
        rec("bladerunner14", dt.datetime(2026, 12, 1, 9, 0).astimezone(),
            model="claude-sonnet-5-5", i=M,
            arrived=dt.datetime(2026, 12, 1, 9, 30).astimezone()),            # $2.00, Tue
        rec("studio", dt.datetime(2026, 11, 30, 12, 0).astimezone(), i=M),    # before Dec 1
        heartbeat("studio", dt.datetime(2026, 12, 3, 0, 10).astimezone()),
        heartbeat("dev", dt.datetime(2026, 12, 3, 0, 20).astimezone(), build="4d43908"),
        heartbeat("hasami", dt.datetime(2026, 12, 3, 0, 5).astimezone()),
        heartbeat("dev-ai", dt.datetime(2026, 12, 3, 1, 0).astimezone()),
    ]


@pytest.fixture
def con():
    return fleet.load(fleet_rows())


def _report(con, since=dt.date(2026, 11, 28)):
    return fleet.Report(con, expected=EXPECTED, since=since, now=NOW, hub_build=HUB_BUILD,
                        last_seen={})


# ---- loading bronze rows

def test_bronze_columns_and_meta_become_ledger_columns():
    r = rec("studio", YDAY, i=10, o=5, cr=None, cw=None, provider=None, reasoning_tokens=7)
    r["meta"]["provider"] = "omlx"
    con = fleet.load([r])

    row = con.execute("SELECT host, provider, input_tokens, cache_read_tokens, cache_write_tokens,"
                      " reasoning_tokens, org, subscription, build FROM usage").fetchone()

    assert row == ("studio", "omlx", 10, 0, 0, 7, "Personal", "max", HUB_BUILD)


def test_heartbeats_stay_out_of_the_usage_table(con):
    (n,) = con.execute("SELECT COUNT(*) FROM usage WHERE harness = 'tokometer'").fetchone()
    assert n == 0


# ---- the numbers Kata prints

def test_yesterday_counts_every_machine_that_did_work(con):
    s = _report(con).summary()

    assert s["yesterday"]["tokens"] == 3 * M
    assert s["yesterday"]["value"] == pytest.approx(4.00 + 4.00 + 0.1875)
    assert s["yesterday"]["machines"] == 3


def test_month_to_date_value_is_split_by_account(con):
    s = _report(con).summary()

    assert s["period"]["start"] == "2026-12-01", "the month start is after the start date"
    assert s["period"]["value"] == pytest.approx(4 + 4 + 0.1875 + 2), "Nov 30 is excluded"
    assert s["period"]["by_account"] == [
        ["Personal max", pytest.approx(6.00)],
        ["Acme team", pytest.approx(4.00)],
        ["Local", pytest.approx(0.1875)],
    ]


def test_the_summary_matches_the_price_table_row_by_row(con):
    """Independent check: price each row with lib.pricing directly."""
    expected = sum(pricing.value_usd(r["model"], r["input_tokens"], 0, 0, 0, 0, r["provider"])
                   for r in fleet_rows()
                   if r["harness"] != "tokometer" and r["ts"].date() >= dt.date(2026, 12, 1))

    assert _report(con).summary()["period"]["value"] == pytest.approx(expected)


def test_kata_lines(con):
    lines = _report(con).summary()["kata_lines"]

    assert lines == [
        ":bar_chart: Tokens yesterday: 3.0M on 3 machines, $8.19 list value",
        "   MTD $10: Personal max $6.00 · Acme team $4.00 · Local $0.19",
        ":warning: bladerunner14: no data since Tue",
        ":warning: mini: no data yet",
        ":warning: dev: build 4d43908, hub runs 0d06d72",
    ]


# ---- flags

def test_a_machine_heard_from_today_is_not_flagged_even_when_idle(con):
    by_host = {m.host: m for m in _report(con).machines()}

    assert by_host["dev-ai"].flags == [], "dev-ai did no work but its heartbeat arrived"
    assert by_host["studio"].flags == []


def test_last_arrival_comes_from_all_of_bronze_when_older_than_the_window(con):
    r = fleet.Report(con, expected=EXPECTED, since=dt.date(2026, 11, 28), now=NOW,
                     hub_build=HUB_BUILD,
                     last_seen={"mini": dt.datetime(2026, 11, 20, 8, 0).astimezone()})

    mini = next(m for m in r.machines() if m.host == "mini")

    assert mini.flags == ["no data since Nov 20"]


# ---- the start date

def test_before_the_first_full_day_kata_says_when_counting_starts():
    con = fleet.load([rec("studio", dt.datetime(2026, 11, 28, 2, 0).astimezone(), i=M)])
    r = fleet.Report(con, expected=["studio"], since=dt.date(2026, 11, 28),
                     now=dt.datetime(2026, 11, 28, 4, 40).astimezone(), hub_build=HUB_BUILD,
                     last_seen={"studio": dt.datetime(2026, 11, 28, 2, 0).astimezone()})

    assert r.summary()["kata_lines"] == [
        ":bar_chart: Tokens: the all-machines count starts today, Nov 28"]


def test_in_the_start_month_the_period_reads_since_the_start_date():
    con = fleet.load([rec("studio", dt.datetime(2026, 11, 28, 10, 0).astimezone(), i=M)])
    r = fleet.Report(con, expected=["studio"], since=dt.date(2026, 11, 28),
                     now=dt.datetime(2026, 11, 29, 4, 40).astimezone(), hub_build=HUB_BUILD,
                     last_seen={"studio": dt.datetime(2026, 11, 29, 0, 10).astimezone()})

    s = r.summary()

    assert s["period"]["start"] == "2026-11-28"
    assert s["kata_lines"][1].startswith("   Since Nov 28 $4.00: Personal max $4.00")


# ---- the HTML

def test_the_page_covers_every_machine_without_local_only_tables(con):
    html = _report(con).html()

    assert "tokometer fleet report" in html
    assert '$10.19</div><div class="lbl">list value' in html, "Nov 30 is before the period"
    for host in EXPECTED:
        assert f"<td>{host}</td>" in html, host
    assert "Productivity" not in html


# ---- outputs

def test_write_puts_the_page_and_summary_where_kata_and_studio_read_them(con, tmp_path):
    paths = _report(con).write(tmp_path)

    assert (tmp_path / "fleet-2026-12-03.html").is_file()
    assert (tmp_path / "fleet-latest.html").read_text() == (tmp_path / "fleet-2026-12-03.html").read_text()
    summary = json.loads((tmp_path / "fleet-summary.json").read_text())
    assert summary["date"] == "2026-12-03"
    assert paths["summary"] == tmp_path / "fleet-summary.json"


def test_recent_rows_that_name_no_build_are_flagged():
    """A client older than the build stamp sends rows without one."""
    con = fleet.load([rec("dev", YDAY, i=M, build=None)])
    r = fleet.Report(con, expected=["dev"], since=dt.date(2026, 11, 28), now=NOW,
                     hub_build=HUB_BUILD, last_seen={})

    (dev,) = r.machines()

    assert dev.flags == ["build unknown, hub runs 0d06d72"]
