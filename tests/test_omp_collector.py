"""collectors/omp.py: OMP session transcripts.

OMP writes ~/.omp/agent/sessions/<cwd-slug>/<ts>_<uuid>.jsonl. Usage comes
from assistant `message` records (turns) and `model_usage` records (side calls
such as titles, recaps and compaction). OMP is provider-based: the providers
in models.yml that point at this machine or the LAN are self-hosted model
servers, and its own Anthropic OAuth login is the only cloud account it records.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from lib import ledger
from collectors import omp

SID = "01a209dc-7070-73df-8783-47517c0fa78e"

MODELS_YML = """\
# providers shared across machines
providers:
  dev-ai:
    baseUrl: http://dev-ai.local:8000/v1
    apiKey: not-a-real-key
    api: openai-completions
    models:
      - id: workhorse
        name: Workhorse (dev-ai primary, vLLM)
  "llama.cpp":
    baseUrl: "http://127.0.0.1:8080/v1"
    models:
      - id: qwen3.6-35b-a3b-mtp
  omlx:
    baseUrl: 'http://127.0.0.1:8080/v1'
    models:
      - id: Qwen3.8-Flash-Next-oQ4e-mtp
  studio:
    baseUrl: http://studio.local:8080/v1   # Studio oMLX over the LAN
  router:
    baseUrl: https://openrouter.ai/api/v1
"""


@pytest.fixture(autouse=True)
def _no_retention(monkeypatch):
    monkeypatch.setattr(ledger, "older_than_retention", lambda ts: False)


@pytest.fixture
def omp_home(tmp_path, monkeypatch):
    agent = tmp_path / "omp" / "agent"
    (agent / "sessions").mkdir(parents=True)
    (agent / "models.yml").write_text(MODELS_YML)
    monkeypatch.setenv("OMP_SESSIONS_DIR", str(agent / "sessions"))
    monkeypatch.setenv("OMP_MODELS_YML", str(agent / "models.yml"))
    monkeypatch.setenv("OMP_AGENT_DB", str(agent / "agent.db"))
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(tmp_path / "no-claude"))
    return agent


def _usage(inp, out, cr=0, cw=0, reasoning=0):
    return {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw,
            "totalTokens": inp + out + cr + cw, "reasoningTokens": reasoning,
            "cost": {"total": 0}}


def _turn(rid, provider, model, usage, ts_ms=1795461777225):
    return {"type": "message", "id": rid, "timestamp": "2026-11-23T19:23:11.838Z",
            "message": {"role": "assistant", "api": "openai-completions",
                        "provider": provider, "model": model, "usage": usage,
                        "stopReason": "toolUse", "timestamp": ts_ms}}


def _side(rid, provider, model, usage, purpose="title"):
    return {"type": "model_usage", "id": rid, "timestamp": "2026-11-23T19:24:00.000Z",
            "purpose": purpose, "api": "openai-completions", "provider": provider,
            "model": model, "usage": usage}


def _session(agent, records, sid=SID, slug="--Volumes-workspace-repo--"):
    path = agent / "sessions" / slug / f"2026-11-23T19-22-51-376Z_{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {"type": "session", "version": 3, "id": sid,
            "timestamp": "2026-11-23T19:22:51.376Z", "cwd": "/Volumes/workspace/repo"}
    with open(path, "w") as f:
        for o in [head] + records:
            f.write(json.dumps(o) + "\n")
    return path


def _append(path, records):
    with open(path, "a") as f:
        for o in records:
            f.write(json.dumps(o) + "\n")
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))


def _rows(home):
    con = sqlite3.connect(str(home / "ledger.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM usage WHERE harness='omp' ORDER BY uid").fetchall()
    con.close()
    return rows


def test_assistant_turn_becomes_one_row(tmp_tokometer, omp_home):
    _session(omp_home, [_turn("4accfb69", "omlx", "Qwen3.8-Flash-Next-oQ4e-mtp:latest",
                              _usage(9958, 215, cr=4096, cw=32, reasoning=49))])

    assert omp.harvest() == 1

    (r,) = _rows(tmp_tokometer)
    assert r["uid"] == f"omp:{SID}:4accfb69"
    assert (r["provider"], r["model"]) == ("omlx", "Qwen3.8-Flash-Next-oQ4e-mtp")
    assert (r["input_tokens"], r["output_tokens"], r["cache_read_tokens"],
            r["cache_write_tokens"]) == (9958, 215, 4096, 32)
    # OMP's output already includes reasoning; total_tokens must not add it twice.
    assert r["reasoning_tokens"] == 0
    assert r["total_tokens"] == 9958 + 215 + 4096 + 32
    assert r["ts"] == ledger.iso_utc(epoch_ms=1795461777225)
    assert (r["session_id"], r["cwd"]) == (SID, "/Volumes/workspace/repo")


def test_side_calls_are_counted(tmp_tokometer, omp_home):
    _session(omp_home, [
        _turn("t1", "omlx", "Qwen3.8-Flash-Next-oQ4e-mtp", _usage(100, 10)),
        _side("s1", "omlx", "Qwen3.8-Flash-Next-oQ4e-mtp", _usage(5543, 27, cr=8192)),
        _side("s2", "dev-ai", "workhorse", _usage(900, 300), purpose="compaction:soft"),
    ])

    assert omp.harvest() == 3

    side = {r["uid"]: r for r in _rows(tmp_tokometer)}[f"omp:{SID}:s2"]
    assert (side["provider"], side["model"], side["input_tokens"]) == ("dev-ai", "workhorse", 900)
    assert side["ts"] == "2026-11-23T19:24:00Z"


def test_zero_usage_records_are_dropped(tmp_tokometer, omp_home):
    _session(omp_home, [_turn("err", "dev-ai", "workhorse", _usage(0, 0))])

    assert omp.harvest() == 0


def test_a_turn_written_after_a_later_one_is_still_counted(tmp_tokometer, omp_home):
    """Two sessions run at once. A turn that started earlier but finished after
    the last harvest must not be lost behind the other session's timestamps."""
    a = _session(omp_home, [_turn("a1", "omlx", "m", _usage(1, 1), ts_ms=1000)], sid="sess-a")
    _session(omp_home, [_turn("b1", "omlx", "m", _usage(1, 1), ts_ms=3000)], sid="sess-b")
    assert omp.harvest() == 2

    _append(a, [_turn("a2", "omlx", "m", _usage(2, 2), ts_ms=2000)])

    assert omp.harvest() == 1
    assert "omp:sess-a:a2" in {r["uid"] for r in _rows(tmp_tokometer)}


def test_reharvest_adds_nothing(tmp_tokometer, omp_home):
    path = _session(omp_home, [_turn("t1", "omlx", "m", _usage(1, 1))])
    assert omp.harvest() == 1
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    assert omp.harvest() == 0


def test_self_hosted_providers_are_attributed_as_local(tmp_tokometer, omp_home):
    _session(omp_home, [
        _turn("t1", "dev-ai", "workhorse", _usage(1, 1)),
        _turn("t2", "llama.cpp", "qwen3.6-35b-a3b-mtp", _usage(1, 1)),
        _turn("t3", "omlx", "Qwen3.8-Flash-Next-oQ4e-mtp", _usage(1, 1)),
        _turn("t4", "studio", "coder", _usage(1, 1)),
        _turn("t5", "router", "some/cloud-model", _usage(1, 1)),
    ])

    omp.harvest()

    got = {r["provider"]: (r["account"], r["subscription"], r["org"])
           for r in _rows(tmp_tokometer)}
    for p in ("dev-ai", "llama.cpp", "omlx", "studio"):
        assert got[p] == (p, "self-hosted", "Local")
    assert got["router"] == (None, None, None)


def _omp_logins(agent, *identities):
    con = sqlite3.connect(str(agent / "agent.db"))
    con.execute("CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY, provider TEXT, "
                "credential_type TEXT, data TEXT, disabled_cause TEXT, identity_key TEXT)")
    for provider, ident in identities:
        con.execute("INSERT INTO auth_credentials (provider, credential_type, data, identity_key) "
                    "VALUES (?, 'oauth', 'secret-not-read', ?)", (provider, ident))
    con.commit()
    con.close()


def _claude_root(tmp_path, monkeypatch, email, org_uuid, org_name, org_type):
    root = tmp_path / "claude-root"
    root.mkdir()
    (root / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": email, "organizationUuid": org_uuid,
        "organizationName": org_name, "organizationType": org_type}}))
    monkeypatch.setenv("CLAUDE_CONFIG_ROOTS", str(root))


def test_anthropic_rows_take_omps_oauth_login(tmp_tokometer, omp_home, tmp_path, monkeypatch):
    _omp_logins(omp_home, ("anthropic", "me@personal.example|org:org-uuid-1"))
    _claude_root(tmp_path, monkeypatch, "me@personal.example", "org-uuid-1",
                 "me@personal.example's Organization", "claude_max")
    _session(omp_home, [_turn("t1", "anthropic", "claude-opus-5-5", _usage(10, 5, cr=900))])

    omp.harvest()

    (r,) = _rows(tmp_tokometer)
    assert (r["account"], r["subscription"], r["org"]) == ("me@personal.example", "max", "Personal")


def test_anthropic_login_without_a_matching_claude_account_keeps_the_email(
        tmp_tokometer, omp_home):
    _omp_logins(omp_home, ("anthropic", "me@personal.example|org:org-uuid-1"))
    _session(omp_home, [_turn("t1", "anthropic", "claude-opus-5-5", _usage(10, 5))])

    omp.harvest()

    (r,) = _rows(tmp_tokometer)
    assert (r["account"], r["subscription"], r["org"]) == ("me@personal.example", None, None)


def test_anthropic_identity_unknown_when_omp_has_several_logins(tmp_tokometer, omp_home):
    _omp_logins(omp_home, ("anthropic", "a@example.com|org:1"), ("anthropic", "b@example.com|org:2"))
    _session(omp_home, [_turn("t1", "anthropic", "claude-opus-5-5", _usage(10, 5))])

    omp.harvest()

    (r,) = _rows(tmp_tokometer)
    assert (r["account"], r["subscription"], r["org"]) == (None, None, None)


def test_no_sessions_dir_is_a_quiet_no_op(tmp_tokometer, tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_SESSIONS_DIR", str(tmp_path / "absent"))

    assert omp.harvest() == 0


def test_provider_base_urls_from_models_yml():
    assert omp.provider_base_urls(MODELS_YML) == {
        "dev-ai": "http://dev-ai.local:8000/v1",
        "llama.cpp": "http://127.0.0.1:8080/v1",
        "omlx": "http://127.0.0.1:8080/v1",
        "studio": "http://studio.local:8080/v1",
        "router": "https://openrouter.ai/api/v1",
    }


@pytest.mark.parametrize("url,local", [
    ("http://127.0.0.1:8080/v1", True),
    ("http://localhost:11434", True),
    ("http://dev-ai.local:8000/v1", True),
    ("http://studio:8080/v1", True),
    ("http://192.168.1.20:8000/v1", True),
    ("http://100.84.248.28:8080/v1", True),        # Tailscale CGNAT range
    ("http://bladerunner14.tail787a.ts.net:8080", True),
    ("https://openrouter.ai/api/v1", False),
    ("https://api.anthropic.com", False),
])
def test_self_hosted_endpoint(url, local):
    assert omp.is_self_hosted(url) is local
