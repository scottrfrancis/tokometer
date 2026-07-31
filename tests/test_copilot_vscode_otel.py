"""Tests for collectors/copilot_vscode.py — the dormant OTel cross-check."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from collectors import copilot_chat_log as ccl
from collectors import copilot_vscode as otel

# Same Trace-log fixture used by test_copilot_chat_log.py, so we can run the
# real chat-log collector first and then feed the OTel collector a synthetic
# row describing the SAME underlying request.
CCL_FIXTURE_GLOB = str(
    Path(__file__).parent / "fixtures" / "vscode_logs" / "*" / "window*" /
    "exthost" / "output_logging_*" / "*GitHub Copilot Chat*.log"
)


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


# ---------- cross-source dedup guard (Bug: OTel and Trace-log are uid-disjoint
# readers of potentially the SAME underlying Copilot Chat request) ----------

def test_dedup_skips_otel_row_matching_chat_log_request(tmp_tokometer, tmp_path):
    """Same request surfaces via both the Trace-log AND the OTel exporter ->
    only ONE ledger row must result, not two."""
    con = _connect(tmp_tokometer)
    ccl.collect(con=con, log_glob=CCL_FIXTURE_GLOB)
    ts, model, in_tok, out_tok = con.execute(
        "SELECT ts, model, input_tokens, output_tokens FROM usage"
        " WHERE uid='copilot-vscode:478c2085'").fetchone()
    before_count = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]

    (tmp_path / "spans.jsonl").write_text(json.dumps({
        "name": "invoke_agent",
        "end_time": ts,
        "attributes": {
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": in_tok,
            "gen_ai.usage.output_tokens": out_tok,
            # a real OTel conversation id would differ in format from the
            # Trace-log uuid anyway; the point is the guard must not rely on
            # the ids matching, only ts + model + token counts.
            "gen_ai.conversation.id": "otel-conversation-id-unrelated-format",
        },
    }) + "\n")

    result = otel.collect(con=con, otel_dir=str(tmp_path))
    after_count = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    con.close()

    assert result["rows"] == 1        # the OTel collector did parse a candidate row
    assert result["inserted"] == 0    # ...but recognized it as a duplicate and skipped it
    assert after_count == before_count


def test_dedup_tolerates_small_clock_skew(tmp_tokometer, tmp_path):
    """The Trace-log and OTel exporter timestamp the same request slightly
    differently (span end vs. line-write time); a couple of seconds of skew
    must still be recognized as the same request."""
    con = _connect(tmp_tokometer)
    ccl.collect(con=con, log_glob=CCL_FIXTURE_GLOB)
    ts, model, in_tok, out_tok = con.execute(
        "SELECT ts, model, input_tokens, output_tokens FROM usage"
        " WHERE uid='copilot-vscode:478c2085'").fetchone()
    skewed = (dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
              + dt.timedelta(seconds=1.5))
    skewed_ts = skewed.isoformat().replace("+00:00", "Z")

    (tmp_path / "spans.jsonl").write_text(json.dumps({
        "name": "invoke_agent",
        "end_time": skewed_ts,
        "attributes": {
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": in_tok,
            "gen_ai.usage.output_tokens": out_tok,
        },
    }) + "\n")

    result = otel.collect(con=con, otel_dir=str(tmp_path))
    con.close()
    assert result["inserted"] == 0


def test_dedup_does_not_skip_unrelated_otel_rows(tmp_tokometer, tmp_path):
    """Regression guard: the dedup check must not suppress genuinely distinct
    requests (different tokens/model/time) — normal single-source OTel
    ingestion keeps working even with chat-log rows present in the ledger."""
    con = _connect(tmp_tokometer)
    ccl.collect(con=con, log_glob=CCL_FIXTURE_GLOB)
    before_count = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]

    (tmp_path / "spans.jsonl").write_text(json.dumps({
        "name": "invoke_agent",
        "end_time": "2026-07-17T15:00:00Z",
        "attributes": {
            "gen_ai.request.model": "claude-haiku-4.5",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 20,
        },
    }) + "\n")

    result = otel.collect(con=con, otel_dir=str(tmp_path))
    after_count = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    con.close()

    assert result["inserted"] == 1
    assert after_count == before_count + 1
