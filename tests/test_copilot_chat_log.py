"""Tests for collectors/copilot_chat_log.py — the primary Copilot-in-VS-Code source.

Fixture: tests/fixtures/vscode_logs/…/1-GitHub Copilot Chat.log, synthesized from
Trace-level output photographed on the target box 2026-07-17 (sanitized). Contains:
- one Anthropic/Bedrock turn WITH full usage (ccreq 478c2085)
- one long turn with no adjacent usage (ccreq 5f8a7005)
- one OpenAI-style turn without usage (ccreq 59a9e1b9)
- one utility-model turn (ccreq 3814725e)
- one failed request (ccreq 9a0b1c2d)
- quota, power-throttle, context-budget, tool-result-disk event lines
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from collectors import copilot_chat_log as ccl

FIXTURE_GLOB = str(
    Path(__file__).parent / "fixtures" / "vscode_logs" / "*" / "window*" /
    "exthost" / "output_logging_*" / "*GitHub Copilot Chat*.log"
)


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


# ---------- line-level parsers ----------

def test_parse_ccreq_with_deployment():
    m = ccl.parse_ccreq(
        "ccreq:478c2085.copilotmd | success | claude-haiku-4.5 -> "
        "claude-haiku-4-5-20251001 | 3398ms | [tool/runSubagent-Explore]")
    assert m == {
        "ccreq": "478c2085", "status": "success",
        "alias": "claude-haiku-4.5", "deployment": "claude-haiku-4-5-20251001",
        "latency_ms": 3398, "origin": "tool/runSubagent-Explore",
    }


def test_parse_ccreq_without_deployment():
    m = ccl.parse_ccreq(
        "ccreq:59a9e1b9.copilotmd | success | gpt-5.3-codex | 2974ms | [panel/editAgent]")
    assert m["alias"] == "gpt-5.3-codex"
    assert m["deployment"] is None
    assert m["latency_ms"] == 2974


def test_parse_ccreq_failure():
    m = ccl.parse_ccreq(
        "ccreq:9a0b1c2d.copilotmd | failure | claude-haiku-4.5 -> "
        "claude-haiku-4-5-20251001 | 120001ms | [panel/editAgent]")
    assert m["status"] == "failure"


def test_parse_ccreq_rejects_other_lines():
    assert ccl.parse_ccreq("chat model claude-haiku-4.5") is None


def test_extract_usage_anthropic_style():
    line = ('{"copilot_usage":{"total_nano_aiu":852380000},"type":"message_delta",'
            '"usage":{"cache_creation_input_tokens":4384,"cache_read_input_tokens":21408,'
            '"input_tokens":13,"output_tokens":178}}')
    u = ccl.extract_usage(line)
    assert u["input_tokens"] == 13
    assert u["output_tokens"] == 178
    assert u["cache_write_tokens"] == 4384
    assert u["cache_read_tokens"] == 21408


def test_extract_usage_bedrock_metrics():
    line = ('{"amazon-bedrock-invocationMetrics":{"cacheReadInputTokenCount":21408,'
            '"cacheWriteInputTokenCount":4384,"inputTokenCount":13,'
            '"invocationLatency":2714,"outputTokenCount":178},"type":"message_stop"}')
    u = ccl.extract_usage(line)
    assert u["input_tokens"] == 13
    assert u["invocation_latency_ms"] == 2714


def test_extract_usage_openai_style():
    line = ('{"usage":{"prompt_tokens":900,"completion_tokens":40,"total_tokens":940}}')
    u = ccl.extract_usage(line)
    assert u["input_tokens"] == 900
    assert u["output_tokens"] == 40


def test_extract_usage_none_for_deltas():
    assert ccl.extract_usage('{"delta":{"text":"hi"},"type":"content_block_delta"}') is None


# ---------- file-level collect ----------

def test_collect_inserts_usage_rows(tmp_tokometer):
    con = _connect(tmp_tokometer)
    result = ccl.collect(con=con, log_glob=FIXTURE_GLOB)
    rows = con.execute(
        "SELECT uid, model, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens, confidence FROM usage ORDER BY ts").fetchall()
    con.close()
    # 5 ccreq lines in the fixture -> 5 usage rows
    assert result["requests"] == 5
    assert len(rows) == 5
    by_uid = {r[0]: r for r in rows}
    # the turn with adjacent usage is exact, with full cache accounting
    exact = by_uid["copilot-vscode:478c2085"]
    assert exact[1] == "claude-haiku-4-5-20251001"   # deployment preferred over alias
    assert exact[2] == 13 and exact[3] == 178
    assert exact[4] == 21408 and exact[5] == 4384
    assert exact[6] == "exact"
    # the turn without usage still lands, marked estimate
    est = by_uid["copilot-vscode:59a9e1b9"]
    assert est[1] == "gpt-5.3-codex"                  # alias when no deployment
    assert est[2] == 0 and est[6] == "estimate"


def test_collect_emits_events(tmp_tokometer):
    con = _connect(tmp_tokometer)
    ccl.collect(con=con, log_glob=FIXTURE_GLOB)
    kinds = {k for (k,) in con.execute("SELECT DISTINCT kind FROM event")}
    con.close()
    assert {"power_throttle", "quota", "context_budget",
            "tool_result_disk", "request_failure"}.issubset(kinds)


def test_collect_attaches_session_id(tmp_tokometer):
    con = _connect(tmp_tokometer)
    ccl.collect(con=con, log_glob=FIXTURE_GLOB)
    (sid,) = con.execute(
        "SELECT session_id FROM usage WHERE uid='copilot-vscode:478c2085'").fetchone()
    con.close()
    assert sid == "8eee8653-a088-4388-bfd3-0c62ecaaf167"


def test_collect_is_idempotent(tmp_tokometer):
    con = _connect(tmp_tokometer)
    ccl.collect(con=con, log_glob=FIXTURE_GLOB)
    before = (con.execute("SELECT COUNT(*) FROM usage").fetchone()[0],
              con.execute("SELECT COUNT(*) FROM event").fetchone()[0])
    # force reparse (bypass the high-water state): uid dedupe must hold the line
    ccl.collect(con=con, log_glob=FIXTURE_GLOB, force=True)
    after = (con.execute("SELECT COUNT(*) FROM usage").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM event").fetchone()[0])
    con.close()
    assert before[0] == 5
    assert after == before


def test_dry_run_writes_nothing(tmp_tokometer):
    con = _connect(tmp_tokometer)
    result = ccl.collect(con=con, log_glob=FIXTURE_GLOB, dry_run=True)
    (n,) = con.execute("SELECT COUNT(*) FROM usage").fetchone()
    con.close()
    assert result["requests"] == 5
    assert n == 0
