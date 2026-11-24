"""Per-response dedup for collectors/claude_code.py, and the ledger migration.

Claude Code writes one transcript line per content block of an assistant
response (thinking, text, tool_use). Every line carries its own `uuid` but the
same `message.id`, `requestId` and `message.usage`. Keying rows on the line
uuid stored each response once per block, about 2.3-2.5x on real transcripts.

The fixture tests/fixtures/claude_code/split_blocks.jsonl holds:
  * response 1 (msg_01...): three lines (thinking, text, tool_use), one usage;
  * response 2 (msg_02...): two lines (text, tool_use) whose usage carries an
    `iterations` array of two billed attempts;
  * a `<synthetic>` line with all-zero usage.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from lib import ledger
from collectors import claude_code as cc

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code" / "split_blocks.jsonl"
SID = "5bf133fa-0000-4000-8000-split0blocks"
UID1 = "claude-code:msg_01SplitBlocksResponseOne"
UID2 = "claude-code:msg_02IterationsResponseTwo"


@pytest.fixture(autouse=True)
def _no_retention(monkeypatch):
    """The fixture's timestamps are fixed; keep them inside the retention window."""
    monkeypatch.setattr(ledger, "older_than_retention", lambda ts: False)


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """Install the fixture as config_root/projects/proj/<session>.jsonl."""
    config_root = tmp_path / "claude_config"
    proj = config_root / "projects" / "proj"
    proj.mkdir(parents=True)
    path = proj / f"{SID}.jsonl"
    shutil.copy(FIXTURE, path)
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(config_root))
    return path


def _write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")


def _rows(home, where="harness='claude-code'"):
    con = sqlite3.connect(str(home / "ledger.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(f"SELECT * FROM usage WHERE {where} ORDER BY id").fetchall()
    con.close()
    return rows


def _fixture_lines():
    with open(FIXTURE) as f:
        return [json.loads(line) for line in f if line.strip()]


def _assistant(**kw):
    o = {"type": "assistant", "sessionId": "sess-k", "timestamp": "2026-11-24T10:00:00Z",
         "cwd": "/repo", "message": {"model": "claude-sonnet-5-5", "usage": {
             "input_tokens": 1, "output_tokens": 2,
             "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}}
    o.update(kw)
    return o


# --- collector ---------------------------------------------------------------


def test_split_content_blocks_collapse_to_one_row_per_response(tmp_tokometer, transcript):
    inserted = cc.harvest()

    assert inserted == 2
    rows = _rows(tmp_tokometer)
    assert [r["uid"] for r in rows] == [UID1, UID2]
    assert all(r["source"] == "session-file" for r in rows)
    assert [r["request_id"] for r in rows] == [
        "req_01SplitBlocksRequestOne", "req_02IterationsRequestTwo"]


def test_split_blocks_token_totals_count_each_response_once(tmp_tokometer, transcript):
    cc.harvest()

    r1, r2 = _rows(tmp_tokometer)
    # response 1: one copy of its usage, not three
    assert (r1["input_tokens"], r1["output_tokens"],
            r1["cache_read_tokens"], r1["cache_write_tokens"]) == (3, 197, 30739, 1204)
    # response 2: the iterations sum, applied once across its two lines
    assert (r2["input_tokens"], r2["output_tokens"],
            r2["cache_read_tokens"], r2["cache_write_tokens"]) == (
        1 + 4, 225 + 11467, 50 + 50, 595587 + 431751)
    total = sum(r["total_tokens"] for r in (r1, r2))
    assert total == (3 + 197 + 30739 + 1204) + (5 + 11692 + 100 + 1027338)


def test_reharvesting_split_blocks_inserts_nothing(tmp_tokometer, transcript):
    assert cc.harvest() == 2
    assert cc.harvest() == 0  # unchanged mtime: file skipped

    # Force a full re-read: the dedup key itself must make it idempotent.
    st = os.stat(transcript)
    os.utime(transcript, (st.st_atime, st.st_mtime + 60))
    assert cc.harvest() == 0
    assert len(_rows(tmp_tokometer)) == 2


def test_synthetic_zero_usage_line_yields_no_row(tmp_tokometer, tmp_path, monkeypatch):
    synthetic = [o for o in _fixture_lines() if o["message"].get("model") == "<synthetic>"]
    assert len(synthetic) == 1
    root = tmp_path / "cfg"
    _write_lines(root / "projects" / "proj" / "s.jsonl", synthetic)
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))

    assert cc.harvest() == 0
    assert _rows(tmp_tokometer) == []


def test_key_falls_back_to_request_id_then_line_uuid(tmp_tokometer, tmp_path, monkeypatch):
    root = tmp_path / "cfg"
    _write_lines(root / "projects" / "proj" / "s.jsonl", [
        _assistant(uuid="line-a", requestId="req-only"),   # no message.id
        _assistant(uuid="line-b"),                         # no message.id, no requestId
    ])
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))

    assert cc.harvest() == 2
    assert {r["uid"] for r in _rows(tmp_tokometer)} == {
        "claude-code:req-only", "claude-code:line-b"}


def test_line_with_no_response_key_yields_no_row(tmp_tokometer, tmp_path, monkeypatch):
    """No message.id, requestId or uuid: nothing to dedupe on, so no row. Keying
    it would give every such line the same `claude-code:None` uid."""
    root = tmp_path / "cfg"
    _write_lines(root / "projects" / "proj" / "s.jsonl", [_assistant()])
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))

    assert cc.harvest() == 0


def test_last_line_of_a_response_wins_within_a_file(tmp_tokometer, tmp_path, monkeypatch):
    """Usage is identical across a response's lines today. If a client ever
    streams partial usage, the last line carries the final count."""
    first = _assistant(uuid="l1", requestId="r1")
    first["message"] = {"id": "msg_stream", "model": "m", "usage": {"output_tokens": 5}}
    last = _assistant(uuid="l2", requestId="r1")
    last["message"] = {"id": "msg_stream", "model": "m", "usage": {"output_tokens": 50}}
    root = tmp_path / "cfg"
    _write_lines(root / "projects" / "proj" / "s.jsonl", [first, last])
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))

    assert cc.harvest() == 1
    (r,) = _rows(tmp_tokometer)
    assert r["uid"] == "claude-code:msg_stream"
    assert r["output_tokens"] == 50


def test_subagent_transcripts_are_not_read(tmp_tokometer, tmp_path, monkeypatch):
    """Locks in the non-recursive glob: <session>/subagents/agent-<id>.jsonl is
    not harvested, so a completed toolUseResult aggregate for the same agentId
    is that agent's only row and is not double-counted."""
    root = tmp_path / "cfg"
    proj = root / "projects" / "proj"
    agg = {"type": "user", "sessionId": "s", "timestamp": "2026-11-24T10:00:00Z", "cwd": "/repo",
           "toolUseResult": {"status": "completed", "agentId": "abc", "resolvedModel": "m",
                             "usage": {"input_tokens": 1, "output_tokens": 1}}}
    _write_lines(proj / "s.jsonl", [agg])
    sub = _assistant(uuid="sub-line", requestId="req-sub")
    sub["message"]["id"] = "msg_sub"
    _write_lines(proj / "s" / "subagents" / "agent-abc.jsonl", [sub])
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))

    assert cc.harvest() == 1
    (r,) = _rows(tmp_tokometer)
    assert r["uid"] == "claude-code:agent:abc"


# --- ledgers holding rows from the line-uuid collector ------------------------
#
# History is not rewritten: rows stored one per content block keep their
# claude-code:<line-uuid> uids. Re-reading their transcript must not add a
# second, response-keyed copy beside them.


def _old_scheme_rows(path, request_ids=None):
    """Rows exactly as the pre-fix collector stored them: one per line, keyed on
    the line uuid. request_ids limits them to those responses."""
    rows = []
    for o in _fixture_lines():
        if o.get("type") != "assistant":
            continue
        if request_ids is not None and o.get("requestId") not in request_ids:
            continue
        inp, out, cr, cw = cc._usage_tokens(o["message"]["usage"])
        if inp + out + cr + cw == 0:
            continue
        rows.append({
            "uid": f"claude-code:{o['uuid']}", "ts": ledger.normalize_iso(o["timestamp"]),
            "harness": "claude-code", "provider": "anthropic", "model": o["message"]["model"],
            "session_id": o["sessionId"], "request_id": o.get("requestId"),
            "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
            "reasoning_tokens": 0, "credits": 0, "cost_usd": 0.0,
            "source": "session-file", "confidence": "exact",
            "raw_ref": f"{path}#{o['uuid']}", "cwd": o["cwd"],
            "account": None, "subscription": None, "org": None,
        })
    return rows


def _seed(rows):
    con = ledger.connect()
    ledger.insert_usage(con, rows)
    con.close()


def test_rereading_a_transcript_stored_under_line_uuids_adds_nothing(
        tmp_tokometer, transcript):
    old = _old_scheme_rows(transcript)
    assert len(old) == 5  # 3 lines for response 1, 2 for response 2
    _seed(old)

    assert cc.harvest() == 0  # no state file: the transcript is re-read
    assert [r["uid"] for r in _rows(tmp_tokometer)] == [r["uid"] for r in old]


def test_new_responses_after_line_uuid_rows_are_counted_once(tmp_tokometer, transcript):
    """A session harvested before the upgrade keeps going: its earlier response
    stays as stored, the new one gets a single response-keyed row."""
    _seed(_old_scheme_rows(transcript, request_ids={"req_01SplitBlocksRequestOne"}))

    assert cc.harvest() == 1
    new = _rows(tmp_tokometer, "uid NOT LIKE 'claude-code:11111111-%'")
    assert [r["uid"] for r in new] == [UID2]
    assert new[0]["total_tokens"] == 5 + 11692 + 100 + 1027338
