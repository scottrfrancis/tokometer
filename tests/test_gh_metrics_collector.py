"""Tests for collectors/gh_metrics.py."""

from __future__ import annotations

import json

import pytest

from collectors import gh_metrics


def _fake_pr(i):
    return {
        "number": i,
        "title": f"PR {i}",
        "repository": {"nameWithOwner": "acme/widgets"},
        "createdAt": "2026-07-01T00:00:00Z",
        "closedAt": "2026-07-02T00:00:00Z",
        "state": "closed",
        "url": f"https://github.com/acme/widgets/pull/{i}",
    }


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def stub_gh_auth(monkeypatch):
    """Make _have_gh() report authenticated without touching the real gh CLI."""
    monkeypatch.setattr(gh_metrics, "_have_gh", lambda: True)


def test_result_count_at_limit_warns_of_possible_truncation(
    tmp_tokometer, stub_gh_auth, monkeypatch, capsys
):
    """A result count that exactly hits the requested --limit is a strong signal
    more results exist (GitHub search API hard-caps at 1000 total). Must warn."""
    from lib import ledger

    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_tokometer / "ledger.db"))

    captured_cmd = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=60):
        captured_cmd["cmd"] = cmd
        limit_idx = cmd.index("--limit") + 1
        limit = int(cmd[limit_idx])
        items = [_fake_pr(i) for i in range(limit)]
        return _FakeCompleted(returncode=0, stdout=json.dumps(items))

    monkeypatch.setattr(gh_metrics.subprocess, "run", fake_run)

    changed = gh_metrics.harvest()

    # the collector must request more than the old hard-coded 200-row cap
    limit_idx = captured_cmd["cmd"].index("--limit") + 1
    requested_limit = int(captured_cmd["cmd"][limit_idx])
    assert requested_limit > 200

    err = capsys.readouterr().err
    assert "truncat" in err.lower() or "limit" in err.lower()
    assert changed == requested_limit


def test_result_count_below_limit_no_warning(tmp_tokometer, stub_gh_auth, monkeypatch, capsys):
    from lib import ledger

    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_tokometer / "ledger.db"))

    def fake_run(cmd, capture_output=True, text=True, timeout=60):
        items = [_fake_pr(i) for i in range(3)]
        return _FakeCompleted(returncode=0, stdout=json.dumps(items))

    monkeypatch.setattr(gh_metrics.subprocess, "run", fake_run)

    changed = gh_metrics.harvest()
    assert changed == 3

    err = capsys.readouterr().err
    assert "truncat" not in err.lower()
