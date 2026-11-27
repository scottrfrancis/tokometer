"""collectors/openwebui.py counts Open WebUI's LLM calls.

Open WebUI keeps chats in webui.db (SQLite), usually inside its container.
Table chat_message has one row per message with a `usage` JSON column, and
table model maps a custom model (a wrapper with its own prompt and tools) to
the base model it calls, e.g. "hazel-kb" -> "vllm.workhorse", where "vllm" is
the connection prefix.

usage is Open WebUI's merged shape: prompt_tokens == input_tokens (prompt,
cached part included), completion_tokens == output_tokens (reasoning
included), plus Ollama-style timings. Some rows hold NULL or
{"incomplete": true} instead. Fixture rows follow real rows on mini
(2026-11-27) with synthetic values.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from collectors import openwebui

FULL_USAGE = {"input_tokens": 1339, "output_tokens": 379, "total_tokens": 1718,
              "prompt_tokens": 1339, "completion_tokens": 379, "response_token/s": 100.4,
              "prompt_token/s": 2605.8, "total_duration": 4415109663, "load_duration": 53372104,
              "prompt_eval_count": 1339, "prompt_eval_duration": 513846218, "eval_count": 379,
              "eval_duration": 3776251831, "approximate_total": "0h0m4s",
              "completion_tokens_details": {"reasoning_tokens": 120,
                                            "accepted_prediction_tokens": 0,
                                            "rejected_prediction_tokens": 0}}


class WebUI:
    """A webui.db with the chat_message and model columns the collector reads."""

    def __init__(self, path):
        self.path = path
        con = sqlite3.connect(path)
        con.executescript("""
          CREATE TABLE chat_message (id TEXT, chat_id TEXT, user_id TEXT, role TEXT,
            content JSON, model_id TEXT, usage JSON, created_at BIGINT, updated_at BIGINT);
          CREATE TABLE model (id TEXT, user_id TEXT, base_model_id TEXT, name TEXT);
          INSERT INTO model VALUES ('hazel-kb', 'u', 'vllm.workhorse', 'Hazel');""")
        con.commit()
        con.close()
        self.n = 0

    def add(self, usage, *, model="hazel-kb", role="assistant", created=1795033893,
            updated=None) -> str:
        self.n += 1
        mid = f"m{self.n}"
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO chat_message VALUES (?,?,?,?,?,?,?,?,?)",
                    (mid, "c1", "u", role, json.dumps("never stored"), model,
                     None if usage is None else json.dumps(usage), created,
                     updated or created))
        con.commit()
        con.close()
        return mid


@pytest.fixture
def webui(tmp_path, tmp_tokometer, monkeypatch):
    db = WebUI(tmp_path / "webui.db")
    monkeypatch.setenv("TOKOMETER_OPENWEBUI_DB", str(db.path))
    monkeypatch.delenv("TOKOMETER_OPENWEBUI_CONTAINER", raising=False)
    monkeypatch.setenv("TOKOMETER_PROVIDER_ALIASES", "vllm=dev-ai")
    monkeypatch.delenv("TOKOMETER_COLLECT_SINCE", raising=False)
    return db


def _rows(tmp_tokometer):
    con = sqlite3.connect(tmp_tokometer / "ledger.db")
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM usage WHERE harness='openwebui' ORDER BY ts").fetchall()


def test_a_reply_becomes_one_row_on_the_base_models_server(webui, tmp_tokometer):
    webui.add(FULL_USAGE)

    openwebui.harvest()

    (r,) = _rows(tmp_tokometer)
    assert r["uid"] == "openwebui:m1"
    assert r["ts"] == "2026-11-18T20:31:33Z"
    assert (r["provider"], r["model"]) == ("dev-ai", "workhorse"), "hazel-kb wraps vllm.workhorse"
    assert (r["input_tokens"], r["output_tokens"], r["cache_read_tokens"]) == (1339, 379, 0)
    assert r["reasoning_tokens"] == 0, "completion_tokens already includes reasoning"
    assert (r["account"], r["subscription"], r["org"]) == ("openwebui", "self-hosted", "Local")


def test_cached_prompt_tokens_are_split_out_when_reported(webui, tmp_tokometer):
    webui.add(dict(FULL_USAGE, prompt_tokens_details={"cached_tokens": 1024}))

    openwebui.harvest()

    (r,) = _rows(tmp_tokometer)
    assert (r["input_tokens"], r["cache_read_tokens"]) == (1339 - 1024, 1024)


@pytest.mark.parametrize("usage", [None, {"incomplete": True}, {"prompt_tokens": 0}])
def test_replies_without_token_counts_are_skipped(webui, tmp_tokometer, usage):
    webui.add(usage)

    openwebui.harvest()

    assert _rows(tmp_tokometer) == []


def test_user_messages_are_not_calls(webui, tmp_tokometer):
    webui.add(FULL_USAGE, role="user")

    openwebui.harvest()

    assert _rows(tmp_tokometer) == []


def test_a_model_name_with_dots_is_not_split_into_a_provider(webui, tmp_tokometer):
    webui.add(FULL_USAGE, model="qwen3.6:35b-a3b")

    openwebui.harvest()

    (r,) = _rows(tmp_tokometer)
    assert (r["provider"], r["model"]) == (None, "qwen3.6:35b-a3b")
    assert r["subscription"] is None


def test_runs_are_incremental_and_catch_usage_that_arrives_late(webui, tmp_tokometer):
    webui.add(FULL_USAGE, created=1795033893)
    openwebui.harvest()
    late = webui.add(None, created=1795040000)
    openwebui.harvest()
    con = sqlite3.connect(webui.path)
    con.execute("UPDATE chat_message SET usage=?, updated_at=? WHERE id=?",
                (json.dumps(FULL_USAGE), 1795040100, late))
    con.commit()
    con.close()

    openwebui.harvest()

    assert [r["uid"] for r in _rows(tmp_tokometer)] == ["openwebui:m1", "openwebui:m2"]


def test_in_container_mode_the_query_runs_through_docker_exec(monkeypatch):
    monkeypatch.setenv("TOKOMETER_OPENWEBUI_CONTAINER", "openwebui-openwebui-1")

    assert openwebui.command(1795000000) == [
        "docker", "exec", "-i", "openwebui-openwebui-1", "python3", "-",
        "/app/backend/data/webui.db", "1795000000"]


def test_with_a_local_database_the_query_runs_here(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKOMETER_OPENWEBUI_CONTAINER", raising=False)
    db = tmp_path / "webui.db"
    db.write_bytes(b"")
    monkeypatch.setenv("TOKOMETER_OPENWEBUI_DB", str(db))

    assert openwebui.command(5) == [sys.executable, "-", str(db), "5"]


def test_no_open_webui_configured_is_not_an_error(tmp_tokometer, monkeypatch, capsys):
    monkeypatch.delenv("TOKOMETER_OPENWEBUI_CONTAINER", raising=False)
    monkeypatch.delenv("TOKOMETER_OPENWEBUI_DB", raising=False)

    assert openwebui.harvest() == 0
    assert "no Open WebUI database configured" in capsys.readouterr().out


def test_each_run_reads_from_a_day_before_the_last_update_seen(webui, tmp_tokometer,
                                                                monkeypatch):
    webui.add(FULL_USAGE, created=1795033893)
    openwebui.harvest()
    seen = []
    real = openwebui.command
    monkeypatch.setattr(openwebui, "command", lambda since: seen.append(since) or real(since))

    openwebui.harvest()

    assert seen == [1795033893 - 86400]
