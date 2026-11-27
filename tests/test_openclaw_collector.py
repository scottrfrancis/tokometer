"""collectors/openclaw.py counts OpenClaw's LLM calls.

OpenClaw (the agent runtime on hasami) keeps every transcript event in
agents/<agent>/agent/openclaw-agent.sqlite, table transcript_events. An
assistant event carries message.{provider, model, timestamp, usage}; usage is
{input, output, cacheRead, cacheWrite, reasoningTokens, totalTokens, cost},
where input excludes cacheRead and output includes reasoning (the same
convention as OMP). Newer rows keep the event only zstd-compressed.

The fixture rows below follow the shape of real rows on hasami (2026-11-27),
with synthetic values.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from collectors import openclaw
from lib import pricing

DDL = """CREATE TABLE transcript_events (
  session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER,
  event_zstd BLOB, message_role TEXT, UNIQUE(session_id, seq))"""


def _event(provider: str, model: str, ts_ms: int, **usage) -> dict:
    u = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}
    u.update(usage)
    u["totalTokens"] = u["input"] + u["output"] + u["cacheRead"] + u["cacheWrite"]
    return {"type": "message", "id": "e", "parentId": None, "timestamp": "x",
            "message": {"role": "assistant", "provider": provider, "model": model,
                        "api": "openai-completions", "timestamp": ts_ms, "stopReason": "stop",
                        "content": [{"type": "text", "text": "never stored"}], "usage": u}}


def _zstd(data: bytes) -> bytes:
    return subprocess.run(["zstd", "-q", "-c"], input=data, capture_output=True, check=True).stdout


class Store:
    """An openclaw-agent.sqlite with helpers to append events."""

    def __init__(self, root: Path):
        self.path = root / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
        self.path.parent.mkdir(parents=True)
        con = sqlite3.connect(self.path)
        con.execute(DDL)
        con.commit()
        con.close()
        self.seq = 0

    def add(self, event: dict | None = None, *, role: str = "assistant", compressed: bool = False,
            session: str = "s1") -> None:
        self.seq += 1
        raw = json.dumps(event or {"type": "message", "message": {"role": role}}).encode()
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO transcript_events VALUES (?,?,?,?,?,?)",
                    (session, self.seq, None if compressed else raw.decode(),
                     1795712890927 + self.seq, _zstd(raw) if compressed else None, role))
        con.commit()
        con.close()


@pytest.fixture
def store(tmp_path, tmp_tokometer, monkeypatch):
    root = tmp_path / "openclaw"
    monkeypatch.setenv("OPENCLAW_HOME", str(root))
    monkeypatch.setenv("TOKOMETER_PROVIDER_ALIASES", "vllm=dev-ai")
    monkeypatch.delenv("TOKOMETER_COLLECT_SINCE", raising=False)
    return Store(root)


def _rows(tmp_tokometer) -> list[sqlite3.Row]:
    con = sqlite3.connect(tmp_tokometer / "ledger.db")
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM usage WHERE harness='openclaw' ORDER BY uid").fetchall()


def test_an_llm_call_becomes_one_self_hosted_row(store, tmp_tokometer):
    store.add(_event("vllm", "workhorse", 1795712879349, input=7242, output=778,
                     cacheRead=24000, reasoningTokens=316))

    openclaw.harvest()

    (r,) = _rows(tmp_tokometer)
    assert r["uid"] == "openclaw:s1:1"
    assert r["ts"] == "2026-11-26T17:07:59Z", "message.timestamp, the request start"
    assert (r["provider"], r["model"]) == ("dev-ai", "workhorse")
    assert (r["input_tokens"], r["output_tokens"], r["cache_read_tokens"]) == (7242, 778, 24000)
    assert r["reasoning_tokens"] == 0, "output already includes reasoning"
    assert (r["account"], r["subscription"], r["org"]) == ("openclaw", "self-hosted", "Local")


def test_compressed_events_are_read(store, tmp_tokometer):
    store.add(_event("vllm", "workhorse", 1795712879349, input=10, output=5), compressed=True)

    openclaw.harvest()

    assert [r["input_tokens"] for r in _rows(tmp_tokometer)] == [10]


def test_openclaw_internal_entries_and_other_roles_are_not_calls(store, tmp_tokometer):
    store.add(_event("openclaw", "automation-result", 1795712879349))
    # Guard, not observed: on 2026-11-27 all 1547 internal entries on hasami had
    # zero usage. A mirror that copied its call's usage would count the call twice.
    store.add(_event("openclaw", "delivery-mirror", 1795712879349, input=7242, output=778),
              compressed=True)
    store.add(role="user")
    store.add(_event("vllm", "workhorse", 1795712879349))  # a call that reported nothing

    openclaw.harvest()

    assert _rows(tmp_tokometer) == []


def test_each_event_is_read_once(store, tmp_tokometer, monkeypatch):
    store.add(_event("vllm", "workhorse", 1795712879349, input=10, output=5), compressed=True)
    openclaw.harvest()

    decoded = []
    real = openclaw.decompress
    monkeypatch.setattr(openclaw, "decompress", lambda b: decoded.append(1) or real(b))
    store.add(_event("vllm", "workhorse", 1795712889349, input=20, output=5), compressed=True)
    openclaw.harvest()

    assert [r["input_tokens"] for r in _rows(tmp_tokometer)] == [10, 20]
    assert len(decoded) == 1, "only the new event should be decoded"


def test_a_row_that_cannot_be_decoded_stops_the_run_without_skipping_it(store, tmp_tokometer,
                                                                         monkeypatch):
    store.add(_event("vllm", "workhorse", 1795712879349, input=10, output=5), compressed=True)
    real = openclaw.decompress

    def broken(blob):
        raise openclaw.DecodeError("zstd not installed")
    monkeypatch.setattr(openclaw, "decompress", broken)
    with pytest.raises(openclaw.DecodeError):
        openclaw.harvest()
    assert _rows(tmp_tokometer) == []

    monkeypatch.setattr(openclaw, "decompress", real)
    openclaw.harvest()
    assert [r["input_tokens"] for r in _rows(tmp_tokometer)] == [10], "retried on the next run"


def test_without_an_alias_the_provider_keeps_openclaws_name(store, tmp_tokometer, monkeypatch):
    monkeypatch.delenv("TOKOMETER_PROVIDER_ALIASES")
    store.add(_event("vllm", "workhorse", 1795712879349, input=10, output=5))

    openclaw.harvest()

    (r,) = _rows(tmp_tokometer)
    assert r["provider"] == "vllm"
    assert r["subscription"] == "self-hosted", "vllm is a local server whatever its name"


def test_rows_are_priced_as_the_server_they_ran_on(store, tmp_tokometer):
    store.add(_event("vllm", "workhorse", 1795712879349, input=1_000_000))

    openclaw.harvest()

    (r,) = _rows(tmp_tokometer)
    i, _, _ = pricing.OPEN_WEIGHT["qwen/qwen3.8-27b"]
    assert pricing.value_usd(r["model"], r["input_tokens"], 0, 0, 0, 0, r["provider"]) == \
        pytest.approx(i)


def test_no_openclaw_install_is_not_an_error(tmp_path, tmp_tokometer, monkeypatch, capsys):
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "absent"))

    assert openclaw.harvest() == 0
    assert "no OpenClaw agent databases" in capsys.readouterr().out
