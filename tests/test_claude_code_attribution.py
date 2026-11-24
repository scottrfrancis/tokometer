"""Account attribution for collectors/claude_code.py.

Sessions are attributed through each config root's history.jsonl, so a root the
collector never scans leaves its sessions unattributed. Three ways that happened
on a real machine:

* A shell hook exports CLAUDE_CONFIG_DIR by project (e.g. ~/.claude-employer for
  client repos). Those dirs share the transcript store but were never roots,
  so their sessions had no account: 51-65% of Claude Code tokens.
* A profile launcher started setting CLAUDE_CONFIG_DIR, which moves the account
  record to <root>/.claude.json. The collector read the stale <home>/.claude.json
  first and reported the old login.
* On macOS the login token is in the Keychain, so <root>/.credentials.json is
  absent and the plan came back NULL.

Every test sets HOME to a tmp dir so discovery never touches the real one.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from lib import ledger
from collectors import claude_code as cc

PERSONAL = {"emailAddress": "me@personal.example", "organizationName": "me@personal.example's Organization",
            "organizationType": "claude_max"}
EMPLOYER = {"emailAddress": "me@employer.example", "organizationName": "Employer",
         "organizationType": "claude_team"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    for var in ("CLAUDE_CONFIG_ROOTS", "CLAUDE_CONFIG_DIR", "CLAUDE_PROFILES_GLOB",
                "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return h


def _account(path, oauth):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"oauthAccount": oauth}))


def _history(root, *session_ids):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "history.jsonl", "w") as f:
        for sid in session_ids:
            f.write(json.dumps({"sessionId": sid, "project": "/repo", "timestamp": 1}) + "\n")


def _routed(home, name, oauth, *session_ids):
    """A config dir selected via CLAUDE_CONFIG_DIR, sharing the transcript store."""
    root = home / name
    _history(root, *session_ids)
    _account(root / ".claude.json", oauth)
    (root / "projects").symlink_to(home / ".claude" / "projects")
    return root


def test_routed_config_dirs_are_roots(home):
    employer = _routed(home, ".claude-employer", EMPLOYER)
    (home / ".claude-server-commander").mkdir()  # not a Claude config dir
    (home / ".claude-profiles" / "al" / ".claude").mkdir(parents=True)

    roots = cc.config_roots()

    assert str(employer.resolve()) in roots
    assert str((home / ".claude-server-commander").resolve()) not in roots
    assert str((home / ".claude-profiles").resolve()) not in roots
    assert roots[0] == str((home / ".claude").resolve())


def test_default_root_kept_when_config_dir_is_exported(home, monkeypatch):
    """The routing hook leaves CLAUDE_CONFIG_DIR set in the shell; a manual run
    from a client repo must not drop ~/.claude."""
    employer = _routed(home, ".claude-employer", EMPLOYER)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(employer))

    roots = cc.config_roots()

    assert str((home / ".claude").resolve()) in roots
    assert str(employer.resolve()) in roots
    assert len(roots) == len(set(roots))


def test_identity_prefers_the_config_dir_account_record(home):
    profile_home = home / ".claude-profiles" / "al"
    root = profile_home / ".claude"
    _account(profile_home / ".claude.json", PERSONAL)  # stale, pre-CLAUDE_CONFIG_DIR
    _account(root / ".claude.json", EMPLOYER)

    assert cc.resolve_identity(str(root)) == ("me@employer.example", "team", "Employer")


def test_default_root_identity_still_comes_from_home(home):
    _account(home / ".claude.json", PERSONAL)

    assert cc.resolve_identity(str(home / ".claude")) == ("me@personal.example", "max", "Personal")


def test_plan_from_organization_type_when_token_is_in_keychain(home, monkeypatch):
    root = home / ".claude-profiles" / "ss" / ".claude"
    _account(root / ".claude.json", PERSONAL)  # no .credentials.json
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set-for-some-other-tool")

    assert cc.resolve_identity(str(root))[1] == "max"


def test_credentials_file_plan_still_wins(home):
    root = home / ".claude-employer"
    _account(root / ".claude.json", EMPLOYER)
    (root / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": "enterprise"}}))

    assert cc.resolve_identity(str(root))[1] == "enterprise"


def test_session_from_a_routed_dir_is_attributed(tmp_tokometer, home):
    _account(home / ".claude.json", PERSONAL)
    _history(home / ".claude", "sess-personal")
    _routed(home, ".claude-employer", EMPLOYER, "sess-employer")
    proj = home / ".claude" / "projects" / "-Volumes-workspace-client-repo"
    proj.mkdir()
    for sid in ("sess-employer", "sess-personal"):
        line = {"type": "assistant", "sessionId": sid, "timestamp": ledger.iso_utc(),
                "cwd": "/repo", "uuid": f"u-{sid}", "requestId": f"r-{sid}",
                "message": {"id": f"msg-{sid}", "model": "m",
                            "usage": {"input_tokens": 1, "output_tokens": 1}}}
        (proj / f"{sid}.jsonl").write_text(json.dumps(line) + "\n")

    assert cc.harvest() == 2

    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    got = dict((r[0], r[1:]) for r in con.execute(
        "SELECT session_id, account, subscription, org FROM usage"))
    con.close()
    assert got == {
        "sess-employer": ("me@employer.example", "team", "Employer"),
        "sess-personal": ("me@personal.example", "max", "Personal"),
    }
