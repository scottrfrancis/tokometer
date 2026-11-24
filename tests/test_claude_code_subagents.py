"""Subagent usage in collectors/claude_code.py.

Claude Code writes each subagent's turns to its own transcript,
<session>/subagents/agent-<agentId>.jsonl, beside the parent session file. The
lines look like the parent's (one per content block, message.id/requestId/usage)
and carry the parent's sessionId, so they are attributed to the account that
ran the session. Nested agents (spawnDepth 2) land in the same subagents dir.

Background launches (status "async_launched") never get a completed
toolUseResult in the parent, so before these files were read their usage was
missing: about 18-22% of each account's tokens on a real machine. A completed
toolUseResult holds only the agent's LAST response's usage, so when the agent's
file exists the file is the record and the aggregate is skipped.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from lib import ledger
from collectors import claude_code as cc

SID = "sess-parent"
ACCOUNT = {"emailAddress": "me@employer.example", "organizationName": "Employer",
           "organizationType": "claude_team"}


@pytest.fixture(autouse=True)
def _no_retention(monkeypatch):
    monkeypatch.setattr(ledger, "older_than_retention", lambda ts: False)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config root whose history maps SID to ACCOUNT."""
    root = tmp_path / "cfg"
    (root / "projects" / "proj").mkdir(parents=True)
    (root / "history.jsonl").write_text(
        json.dumps({"sessionId": SID, "project": "/repo", "timestamp": 1}) + "\n")
    (root / ".claude.json").write_text(json.dumps({"oauthAccount": ACCOUNT}))
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))
    return root


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")


def _response(msg_id, out, blocks=("text",), agent=None):
    """The transcript lines of one API response: one per content block."""
    lines = []
    for i, block in enumerate(blocks):
        o = {"type": "assistant", "sessionId": SID, "timestamp": "2026-11-24T10:00:00Z",
             "cwd": "/repo", "uuid": f"{msg_id}-line{i}", "requestId": f"req-{msg_id}",
             "message": {"id": msg_id, "model": "claude-opus-5-5",
                         "content": [{"type": block}],
                         "usage": {"input_tokens": 2, "output_tokens": out,
                                   "cache_read_input_tokens": 100,
                                   "cache_creation_input_tokens": 10}}}
        if agent:
            o.update(isSidechain=True, agentId=agent)
        lines.append(o)
    return lines


def _aggregate(agent, out, status="completed"):
    """The parent's toolUseResult for an agent. Its usage is the agent's last
    response only."""
    tur = {"status": status, "agentId": agent}
    if status == "completed":
        tur.update(resolvedModel="claude-opus-5-5",
                   usage={"input_tokens": 2, "output_tokens": out,
                          "cache_read_input_tokens": 100,
                          "cache_creation_input_tokens": 10})
    return {"type": "user", "sessionId": SID, "timestamp": "2026-11-24T10:05:00Z",
            "cwd": "/repo", "toolUseResult": tur}


def _parent(cfg):
    return cfg / "projects" / "proj" / f"{SID}.jsonl"


def _agent_file(cfg, agent):
    return cfg / "projects" / "proj" / SID / "subagents" / f"agent-{agent}.jsonl"


def _rows(home):
    con = sqlite3.connect(str(home / "ledger.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM usage WHERE harness='claude-code' ORDER BY uid").fetchall()
    con.close()
    return rows


def test_background_agent_responses_are_counted_for_the_parent_account(tmp_tokometer, cfg):
    _write(_parent(cfg), _response("msg_parent", 50) + [_aggregate("bg1", 0, "async_launched")])
    _write(_agent_file(cfg, "bg1"),
           _response("msg_bg1_a", 300, ("thinking", "text", "tool_use"), agent="bg1")
           + _response("msg_bg1_b", 700, ("text",), agent="bg1"))

    assert cc.harvest() == 3

    rows = {r["uid"]: r for r in _rows(tmp_tokometer)}
    assert set(rows) == {"claude-code:msg_parent", "claude-code:msg_bg1_a",
                         "claude-code:msg_bg1_b"}
    sub = rows["claude-code:msg_bg1_a"]
    assert sub["session_id"] == SID
    assert (sub["account"], sub["subscription"], sub["org"]) == (
        "me@employer.example", "team", "Employer")
    assert sub["output_tokens"] == 300
    assert cc.harvest() == 0


def test_completed_aggregate_is_skipped_when_the_agent_file_exists(tmp_tokometer, cfg):
    _write(_parent(cfg), [_aggregate("fg1", 700)])
    _write(_agent_file(cfg, "fg1"),
           _response("msg_fg1_a", 300, agent="fg1") + _response("msg_fg1_b", 700, agent="fg1"))

    cc.harvest()

    rows = _rows(tmp_tokometer)
    assert [r["uid"] for r in rows] == ["claude-code:msg_fg1_a", "claude-code:msg_fg1_b"]
    assert sum(r["output_tokens"] for r in rows) == 1000


def test_nested_agent_aggregate_is_skipped_when_its_file_exists(tmp_tokometer, cfg):
    """An agent that launches agents gets their toolUseResults in its own file;
    the children's files sit in the same subagents dir."""
    _write(_parent(cfg), [_aggregate("outer", 40)])
    _write(_agent_file(cfg, "outer"),
           _response("msg_outer", 40, agent="outer") + [_aggregate("inner", 90)])
    _write(_agent_file(cfg, "inner"), _response("msg_inner", 90, agent="inner"))

    cc.harvest()

    assert [r["uid"] for r in _rows(tmp_tokometer)] == [
        "claude-code:msg_inner", "claude-code:msg_outer"]


def test_agent_already_stored_as_an_aggregate_is_not_counted_again(tmp_tokometer, cfg):
    """A ledger harvested before agent files were read holds the agent's
    aggregate row. Its file would add that last response a second time."""
    _write(_parent(cfg), [_aggregate("old", 700)])
    _write(_agent_file(cfg, "old"),
           _response("msg_old_a", 300, agent="old") + _response("msg_old_b", 700, agent="old"))
    con = ledger.connect()
    ledger.insert_usage(con, [{
        "uid": "claude-code:agent:old", "ts": "2026-11-24T10:05:00Z",
        "harness": "claude-code", "provider": "anthropic", "model": "claude-opus-5-5",
        "session_id": SID, "input_tokens": 2, "output_tokens": 700,
        "cache_read_tokens": 100, "cache_write_tokens": 10, "source": "agent-aggregate",
        "confidence": "exact"}])
    con.close()

    assert cc.harvest() == 0
    assert [r["uid"] for r in _rows(tmp_tokometer)] == ["claude-code:agent:old"]


def test_fork_copy_of_a_parent_response_is_counted_once(tmp_tokometer, cfg):
    """A forked agent's transcript opens with a copy of the parent's last
    response, under the same message.id."""
    shared = _response("msg_shared", 60, ("thinking", "tool_use"))
    _write(_parent(cfg), shared)
    _write(_agent_file(cfg, "fork1"),
           [dict(o, isSidechain=True, agentId="fork1") for o in shared]
           + _response("msg_fork_own", 80, agent="fork1"))

    assert cc.harvest() == 2
    assert [r["uid"] for r in _rows(tmp_tokometer)] == [
        "claude-code:msg_fork_own", "claude-code:msg_shared"]


def test_only_agent_transcripts_under_subagents_are_read(tmp_tokometer, cfg):
    subagents = _agent_file(cfg, "x").parent
    _write(subagents / ".claude" / "stray.jsonl", _response("msg_stray", 5))
    _write(subagents / "notes.jsonl", _response("msg_notes", 5))
    (subagents / "agent-x.meta.json").write_text(json.dumps({"agentType": "fork"}))

    assert cc.harvest() == 0
