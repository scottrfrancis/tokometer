"""Tests for collectors/copilot_vscode.py — the dormant OTel cross-check."""

from __future__ import annotations

import sqlite3

from collectors import copilot_vscode as otel


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def test_noop_when_dir_absent(tmp_tokometer, monkeypatch):
    monkeypatch.delenv("COPILOT_OTEL_FILE_EXPORTER_PATH", raising=False)
    con = _connect(tmp_tokometer)
    result = otel.collect(con=con)
    con.close()
    assert result == {"files": 0, "rows": 0, "inserted": 0}


def test_noop_when_dir_empty(tmp_tokometer, tmp_path):
    con = _connect(tmp_tokometer)
    result = otel.collect(con=con, otel_dir=str(tmp_path))
    con.close()
    assert result["rows"] == 0


def test_parses_gen_ai_spans(tmp_tokometer, tmp_path):
    (tmp_path / "spans.jsonl").write_text(
        '{"name":"invoke_agent","end_time":"2026-07-17T15:00:00Z",'
        '"attributes":{"gen_ai.request.model":"claude-haiku-4.5",'
        '"gen_ai.usage.input_tokens":10,"gen_ai.usage.output_tokens":20}}\n'
        'not json\n'
        '{"unrelated":true}\n')
    con = _connect(tmp_tokometer)
    result = otel.collect(con=con, otel_dir=str(tmp_path))
    row = con.execute(
        "SELECT model, input_tokens, output_tokens, confidence FROM usage").fetchone()
    con.close()
    assert result["rows"] == 1
    assert row == ("claude-haiku-4.5", 10, 20, "estimate")
