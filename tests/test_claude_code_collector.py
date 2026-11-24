"""Tests for collectors/claude_code.py.

Covers two real bugs found via live-transcript inspection on this machine:

1. Subagent ("Agent" tool) usage never harvested -- subagents don't get their
   own transcript lines (isSidechain is always false); their aggregate usage
   only shows up in a type=="user" line's top-level toolUseResult block.
2. Multi-iteration/fallback usage collapse -- a message.usage.iterations array
   (e.g. an overload retry: "message" attempt then "fallback_message" retry)
   folds multiple billed API attempts under one transcript line, but the
   top-level usage fields mirror only the LAST iteration.

Fixtures build a synthetic Claude Code config root (CLAUDE_CONFIG_ROOTS env
override) with one projects/<proj>/<session>.jsonl transcript file per test.
"""

from __future__ import annotations

import json
import sqlite3

from lib import ledger
from collectors import claude_code as cc


def _write_transcript(config_root, lines):
    """Write `lines` (dicts) as a JSONL transcript under a synthetic config root
    at config_root/projects/proj/session.jsonl. Returns the config_root path.
    """
    proj_dir = config_root / "projects" / "proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    transcript = proj_dir / "session.jsonl"
    with open(transcript, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return config_root


def _harvest(tmp_path, monkeypatch, lines):
    config_root = tmp_path / "claude_config"
    config_root.mkdir()
    _write_transcript(config_root, lines)
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(config_root))
    return cc.harvest()


def _rows(tmp_tokometer):
    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM usage WHERE harness='claude-code'").fetchall()
    con.close()
    return rows


def test_plain_single_iteration_message_still_works(tmp_tokometer, tmp_path, monkeypatch):
    """Regression: a normal assistant line with no 'iterations' key at all --
    the common case, previously untested -- must harvest exactly as before."""
    ts = ledger.iso_utc()
    lines = [{
        "type": "assistant",
        "sessionId": "sess-1",
        "timestamp": ts,
        "cwd": "/repo",
        "uuid": "uuid-normal",
        "requestId": "req-1",
        "message": {
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 3,
            },
        },
    }]
    inserted = _harvest(tmp_path, monkeypatch, lines)
    assert inserted == 1

    rows = _rows(tmp_tokometer)
    assert len(rows) == 1
    r = rows[0]
    assert r["uid"] == "claude-code:req-1"  # no message.id: falls back to requestId
    assert r["source"] == "session-file"
    assert r["model"] == "claude-sonnet-5"
    assert r["input_tokens"] == 10
    assert r["output_tokens"] == 20
    assert r["cache_read_tokens"] == 5
    assert r["cache_write_tokens"] == 3


def test_multi_iteration_message_sums_across_iterations(tmp_tokometer, tmp_path, monkeypatch):
    """Bug 2: usage.iterations present -> sum all iterations, not just the
    top-level fields (which mirror only the last one)."""
    ts = ledger.iso_utc()
    lines = [{
        "type": "assistant",
        "sessionId": "sess-1",
        "timestamp": ts,
        "cwd": "/repo",
        "uuid": "uuid-multi",
        "requestId": "req-2",
        "message": {
            "model": "claude-opus-4",
            "usage": {
                # top-level mirrors only iterations[1] (the fallback) -- must NOT be trusted
                "input_tokens": 4,
                "output_tokens": 11467,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 431751,
                "iterations": [
                    {
                        "type": "message",
                        "input_tokens": 1,
                        "output_tokens": 225,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 595587,
                    },
                    {
                        "type": "fallback_message",
                        "input_tokens": 4,
                        "output_tokens": 11467,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 431751,
                    },
                ],
            },
        },
    }]
    inserted = _harvest(tmp_path, monkeypatch, lines)
    assert inserted == 1

    rows = _rows(tmp_tokometer)
    assert len(rows) == 1
    r = rows[0]
    assert r["uid"] == "claude-code:req-2"
    assert r["input_tokens"] == 1 + 4
    assert r["output_tokens"] == 225 + 11467
    assert r["cache_read_tokens"] == 50 + 50
    assert r["cache_write_tokens"] == 595587 + 431751


def test_completed_agent_aggregate_harvested_as_separate_row(tmp_tokometer, tmp_path, monkeypatch):
    """Bug 1: a completed Agent tool_use_result carries aggregate usage for an
    entire subagent run that never appears as individual assistant lines."""
    ts = ledger.iso_utc()
    lines = [{
        "type": "user",
        "sessionId": "sess-1",
        "timestamp": ts,
        "cwd": "/repo",
        "toolUseResult": {
            "status": "completed",
            "agentId": "af2d78baf4b957523",
            "agentType": "general-purpose",
            "resolvedModel": "claude-sonnet-5",
            "totalDurationMs": 20610,
            "totalTokens": 37386,
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 2107,
                "cache_read_input_tokens": 35067,
                "output_tokens": 210,
            },
        },
    }]
    inserted = _harvest(tmp_path, monkeypatch, lines)
    assert inserted == 1

    rows = _rows(tmp_tokometer)
    assert len(rows) == 1
    r = rows[0]
    assert r["uid"] == "claude-code:agent:af2d78baf4b957523"
    assert r["source"] == "agent-aggregate"
    assert r["model"] == "claude-sonnet-5"
    assert r["input_tokens"] == 2
    assert r["output_tokens"] == 210
    assert r["cache_read_tokens"] == 35067
    assert r["cache_write_tokens"] == 2107


def test_async_agent_launch_is_skipped(tmp_tokometer, tmp_path, monkeypatch):
    """An in-flight async Agent launch (status=='async_launched') has no usage
    yet and nothing ties it to its eventual completion line -- must be skipped,
    not guessed at."""
    lines = [{
        "type": "user",
        "sessionId": "sess-1",
        "timestamp": ledger.iso_utc(),
        "cwd": "/repo",
        "toolUseResult": {
            "status": "async_launched",
            "agentId": "async-agent-1",
            "agentType": "general-purpose",
        },
    }]
    inserted = _harvest(tmp_path, monkeypatch, lines)
    assert inserted == 0
    assert _rows(tmp_tokometer) == []


def test_mixed_transcript_harvests_all_expected_rows_and_dedupes_on_rerun(
    tmp_tokometer, tmp_path, monkeypatch
):
    """End-to-end: one transcript file with a normal message, a multi-iteration
    message, a completed agent aggregate, and a skipped async launch -- confirms
    they coexist correctly and that a second harvest is idempotent (no dupes)."""
    ts = ledger.iso_utc()
    lines = [
        {
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": ts,
            "cwd": "/repo",
            "uuid": "uuid-normal",
            "requestId": "req-1",
            "message": {
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 10, "output_tokens": 20,
                    "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3,
                },
            },
        },
        {
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": ts,
            "cwd": "/repo",
            "uuid": "uuid-multi",
            "requestId": "req-2",
            "message": {
                "model": "claude-opus-4",
                "usage": {
                    "input_tokens": 4, "output_tokens": 11467,
                    "cache_read_input_tokens": 50, "cache_creation_input_tokens": 431751,
                    "iterations": [
                        {"type": "message", "input_tokens": 1, "output_tokens": 225,
                         "cache_read_input_tokens": 50, "cache_creation_input_tokens": 595587},
                        {"type": "fallback_message", "input_tokens": 4, "output_tokens": 11467,
                         "cache_read_input_tokens": 50, "cache_creation_input_tokens": 431751},
                    ],
                },
            },
        },
        {
            "type": "user",
            "sessionId": "sess-1",
            "timestamp": ts,
            "cwd": "/repo",
            "toolUseResult": {
                "status": "completed",
                "agentId": "af2d78baf4b957523",
                "resolvedModel": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 2, "cache_creation_input_tokens": 2107,
                    "cache_read_input_tokens": 35067, "output_tokens": 210,
                },
            },
        },
        {
            "type": "user",
            "sessionId": "sess-1",
            "timestamp": ts,
            "cwd": "/repo",
            "toolUseResult": {"status": "async_launched", "agentId": "async-agent-1"},
        },
    ]
    inserted_first = _harvest(tmp_path, monkeypatch, lines)
    assert inserted_first == 3  # normal + multi-iteration + agent aggregate; async skipped

    rows = _rows(tmp_tokometer)
    assert len(rows) == 3
    uids = {r["uid"] for r in rows}
    assert uids == {
        "claude-code:req-1",
        "claude-code:req-2",
        "claude-code:agent:af2d78baf4b957523",
    }

    # Re-run: file mtime is unchanged so the collector should skip re-reading it,
    # and even if it did re-read, INSERT OR IGNORE would dedupe by uid.
    inserted_second = cc.harvest()
    assert inserted_second == 0
    assert len(_rows(tmp_tokometer)) == 3
