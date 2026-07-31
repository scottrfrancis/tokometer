"""Tests for collectors/cursor_reconcile.py.

Covers two bugs:
  1. uid built from mutable ts+model+token-count fields instead of the real
     event identifier (requestId for JSON exports; Automation ID / Cloud Agent
     ID for CSV exports) -- causes silent undercounts (uid collisions between
     distinct events) or overcounts (a corrected re-export gets counted as a
     brand-new row instead of updating the existing one).
  2. no retention-cutoff filtering, and export files get re-globbed on every
     run -- once monthly.py prunes old `usage` rows, the next run resurrects
     them straight back out of the still-present export file.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sqlite3

from collectors import cursor_reconcile as cr
from lib import ledger


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def _export_dir(home):
    d = os.path.join(str(home), "cursor-exports")
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(export_dir, name, doc):
    path = os.path.join(export_dir, name)
    with open(path, "w") as f:
        json.dump(doc, f)
    return path


def _write_csv(export_dir, name, rows, fieldnames):
    path = os.path.join(export_dir, name)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return path


def _recent_ms():
    """A timestamp safely inside the retention window (today, local noon)."""
    today = dt.datetime.now(tz=dt.timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0)
    return int(today.timestamp() * 1000)


def _old_ms():
    """A timestamp safely before the retention cutoff (5 days before it)."""
    cutoff = ledger.retention_cutoff_date()
    old_date = cutoff - dt.timedelta(days=5)
    old_dt = dt.datetime(old_date.year, old_date.month, old_date.day,
                          12, 0, 0, tzinfo=dt.timezone.utc)
    return int(old_dt.timestamp() * 1000)


CSV_FIELDS = [
    "Date", "User", "Cloud Agent ID", "Automation ID", "Kind", "Model",
    "Max Mode", "Input (w/ Cache Write)", "Input (w/o Cache Write)",
    "Cache Read", "Output Tokens", "Total Tokens", "Cost",
]


# ---------- Bug 1: uid must key off the real event identifier ----------

def test_json_events_same_ts_model_tokens_different_request_id_both_survive(tmp_tokometer):
    """Two distinct events that happen to share ts+model+token-counts must not
    collide on uid just because they share those mutable fields -- this fails
    against the old `evt:{ts}:{model}:{i}:{o}` uid scheme (second row clobbers
    the first via on_conflict='replace')."""
    con = _connect(tmp_tokometer)
    export_dir = _export_dir(tmp_tokometer)
    ts_ms = _recent_ms()
    doc = {"usageEvents": [
        {"timestamp": ts_ms, "model": "gpt-5", "requestId": "req-aaa",
         "tokenUsage": {"inputTokens": 3, "outputTokens": 2}},
        {"timestamp": ts_ms, "model": "gpt-5", "requestId": "req-bbb",
         "tokenUsage": {"inputTokens": 3, "outputTokens": 2}},
    ]}
    _write_json(export_dir, "usage-2026-07.json", doc)

    n = cr.ingest_exports(con, export_dir=export_dir)

    rows = con.execute(
        "SELECT request_id FROM usage WHERE harness='cursor' "
        "AND source='dashboard-export'").fetchall()
    con.close()
    assert n == 2
    assert {r[0] for r in rows} == {"req-aaa", "req-bbb"}


def test_json_event_reissued_with_corrected_totals_updates_existing_row(tmp_tokometer):
    """Same requestId, different token totals across two separate export
    files (Cursor re-issuing a corrected export) must UPDATE the existing
    row, not create a duplicate."""
    con = _connect(tmp_tokometer)
    export_dir = _export_dir(tmp_tokometer)
    ts_ms = _recent_ms()

    doc1 = {"usageEvents": [
        {"timestamp": ts_ms, "model": "gpt-5", "requestId": "req-corrected",
         "tokenUsage": {"inputTokens": 10, "outputTokens": 5}},
    ]}
    _write_json(export_dir, "usage-run1.json", doc1)
    cr.ingest_exports(con, export_dir=export_dir)

    doc2 = {"usageEvents": [
        {"timestamp": ts_ms, "model": "gpt-5", "requestId": "req-corrected",
         "tokenUsage": {"inputTokens": 12, "outputTokens": 6}},
    ]}
    _write_json(export_dir, "usage-run2.json", doc2)
    cr.ingest_exports(con, export_dir=export_dir)

    rows = con.execute(
        "SELECT input_tokens, output_tokens FROM usage WHERE request_id='req-corrected'"
    ).fetchall()
    con.close()
    assert rows == [(12, 6)]


def test_csv_rows_same_date_model_tokens_different_automation_id_both_survive(tmp_tokometer):
    """CSV path: Cloud Agent ID / Automation ID must drive the uid, not the
    mutable date+model+token-count fields."""
    con = _connect(tmp_tokometer)
    export_dir = _export_dir(tmp_tokometer)
    date = dt.date.today().isoformat()
    rows = [
        {"Date": date, "User": "", "Cloud Agent ID": "agent-1",
         "Automation ID": "auto-aaa", "Kind": "", "Model": "gpt-5",
         "Max Mode": "", "Input (w/ Cache Write)": "0",
         "Input (w/o Cache Write)": "3", "Cache Read": "0",
         "Output Tokens": "2", "Total Tokens": "5", "Cost": "0.01"},
        {"Date": date, "User": "", "Cloud Agent ID": "agent-1",
         "Automation ID": "auto-bbb", "Kind": "", "Model": "gpt-5",
         "Max Mode": "", "Input (w/ Cache Write)": "0",
         "Input (w/o Cache Write)": "3", "Cache Read": "0",
         "Output Tokens": "2", "Total Tokens": "5", "Cost": "0.01"},
    ]
    _write_csv(export_dir, "team-usage-events-2026-07.csv", rows, CSV_FIELDS)

    n = cr.ingest_exports(con, export_dir=export_dir)

    got = con.execute(
        "SELECT request_id FROM usage WHERE harness='cursor' "
        "AND source='dashboard-export'").fetchall()
    con.close()
    assert n == 2
    assert {r[0] for r in got} == {"auto-aaa", "auto-bbb"}


# ---------- Bug 2: retention cutoff + no resurrection of pruned rows ----------

def test_old_json_event_not_ingested(tmp_tokometer):
    con = _connect(tmp_tokometer)
    export_dir = _export_dir(tmp_tokometer)
    doc = {"usageEvents": [
        {"timestamp": _old_ms(), "model": "gpt-5", "requestId": "req-old",
         "tokenUsage": {"inputTokens": 10, "outputTokens": 5}},
    ]}
    _write_json(export_dir, "usage-old.json", doc)

    n = cr.ingest_exports(con, export_dir=export_dir)

    row = con.execute("SELECT 1 FROM usage WHERE request_id='req-old'").fetchone()
    con.close()
    assert n == 0
    assert row is None


def test_pruned_row_not_resurrected_on_second_run(tmp_tokometer):
    """Simulates: an old event was ingested in a prior cycle, monthly.py later
    pruned it out of `usage`, and cursor_reconcile runs again against the
    still-present export file. The retention-cutoff check must keep it gone."""
    con = _connect(tmp_tokometer)
    export_dir = _export_dir(tmp_tokometer)
    old_ms = _old_ms()
    doc = {"usageEvents": [
        {"timestamp": old_ms, "model": "gpt-5", "requestId": "req-was-pruned",
         "tokenUsage": {"inputTokens": 10, "outputTokens": 5}},
    ]}
    _write_json(export_dir, "usage-old2.json", doc)

    # Pretend this row existed from a previous ingest, then got pruned.
    con.execute(
        "INSERT INTO usage (uid, ts, harness, source, confidence) "
        "VALUES (?,?,?,?,?)",
        (f"{cr.HARNESS}:evt:req-was-pruned", ledger.iso_utc(epoch_ms=old_ms),
         cr.HARNESS, "dashboard-export", "exact"))
    con.commit()
    con.execute("DELETE FROM usage WHERE uid=?", (f"{cr.HARNESS}:evt:req-was-pruned",))
    con.commit()

    n = cr.ingest_exports(con, export_dir=export_dir)

    row = con.execute("SELECT 1 FROM usage WHERE request_id='req-was-pruned'").fetchone()
    con.close()
    assert n == 0
    assert row is None


def test_ingested_export_file_not_reprocessed_on_second_run(tmp_tokometer):
    """After a successful ingest, the export file must be moved out of
    EXPORT_DIR so a second run's glob doesn't re-parse (and, for exports that
    later age past the retention cutoff, resurrect) it."""
    con = _connect(tmp_tokometer)
    export_dir = _export_dir(tmp_tokometer)
    ts_ms = _recent_ms()
    doc = {"usageEvents": [
        {"timestamp": ts_ms, "model": "gpt-5", "requestId": "req-once",
         "tokenUsage": {"inputTokens": 4, "outputTokens": 1}},
    ]}
    path = _write_json(export_dir, "usage-once.json", doc)

    first = cr.ingest_exports(con, export_dir=export_dir)
    assert first == 1
    assert not os.path.exists(path)
    assert os.path.exists(os.path.join(export_dir, "processed", "usage-once.json"))

    second = cr.ingest_exports(con, export_dir=export_dir)
    con.close()
    assert second == 0
