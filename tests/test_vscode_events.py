"""Tests for collectors/vscode_events.py — crash-side strings from VS Code session logs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from collectors import vscode_events as ve

FIXTURE_ROOT = str(Path(__file__).parent / "fixtures" / "vscode_logs")


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def test_classify_line_worker_oom():
    kind = ve.classify_line(
        "2026-07-17 08:40:12.001 [error] Worker terminated due to reaching "
        "memory limit: JS heap out of memory")
    assert kind == "worker_oom"


def test_classify_line_exthost_crash():
    kind = ve.classify_line(
        "Extension host (LocalProcess pid: 12345) terminated unexpectedly. Code: 133")
    assert kind == "exthost_crash"


def test_classify_line_restart_and_leak_and_v8():
    assert ve.classify_line("Automatically restarting the extension host") == "exthost_restart"
    assert ve.classify_line("potential listener LEAK detected, having 200") == "listener_leak"
    assert ve.classify_line("OOM error in V8: Reached heap limit") == "v8_oom"


def test_classify_line_normal_returns_none():
    assert ve.classify_line("2026-07-17 08:39:59.900 [info] window loaded") is None


def test_collect_finds_crash_events(tmp_tokometer):
    con = _connect(tmp_tokometer)
    result = ve.collect(con=con, logs_root=FIXTURE_ROOT)
    rows = con.execute("SELECT kind, mechanism FROM event").fetchall()
    con.close()
    kinds = [r[0] for r in rows]
    assert result["events"] == 5
    assert kinds.count("worker_oom") == 1
    assert kinds.count("v8_oom") == 1
    assert kinds.count("exthost_crash") == 1
    assert kinds.count("exthost_restart") == 1
    assert kinds.count("listener_leak") == 1
    # crash-family events carry the client-oom mechanism label up front
    mech = {r[0]: r[1] for r in rows}
    assert mech["worker_oom"] == "client-oom"


def test_collect_tolerates_empty_session_dirs(tmp_tokometer, tmp_path):
    empty_root = tmp_path / "logs"
    (empty_root / "20260707T150649").mkdir(parents=True)   # empty session dir (real case)
    con = _connect(tmp_tokometer)
    result = ve.collect(con=con, logs_root=str(empty_root))
    con.close()
    assert result["events"] == 0


def test_collect_idempotent(tmp_tokometer):
    con = _connect(tmp_tokometer)
    ve.collect(con=con, logs_root=FIXTURE_ROOT)
    ve.collect(con=con, logs_root=FIXTURE_ROOT, force=True)
    (n,) = con.execute("SELECT COUNT(*) FROM event").fetchone()
    con.close()
    assert n == 5
