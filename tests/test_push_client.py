"""Tests for push.client -- HTTP push to Beaufort's ingest API.

Uses a stdlib HTTP server fixture (no responses or httpx needed; keeps
tokometer's stdlib-only discipline for tests too).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from push import client as pc


# ─── stdlib mock ingest server ────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.response_status = 200
        self.response_body: dict = {"accepted": 0, "conflicted": 0}


def _make_handler(rec: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            ln = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
            rec.requests.append((self.path, body))
            self.send_response(rec.response_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(rec.response_body).encode("utf-8"))

        def log_message(self, *args, **kwargs):  # silence
            return
    return Handler


@pytest.fixture
def mock_ingest():
    rec = _Recorder()
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    sock.close()
    server = HTTPServer(("127.0.0.1", port), _make_handler(rec))
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        rec.url = f"http://127.0.0.1:{port}"
        yield rec
    finally:
        server.shutdown()


@pytest.fixture
def seeded_ledger(tmp_tokometer):
    """Insert a couple of bronze-shaped rows in tokometer's usage table."""
    db = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO usage (uid, ts, harness, model, input_tokens, output_tokens, "
        "                   source, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u-test-100", "2026-06-13T10:00:00+00:00", "claude-code", "opus", 1000, 500,
         "session-file", "exact"),
    )
    con.execute(
        "INSERT INTO usage (uid, ts, harness, model, input_tokens, output_tokens, "
        "                   source, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("u-test-200", "2026-06-13T11:00:00+00:00", "claude-code", "opus", 2000, 700,
         "session-file", "exact"),
    )
    con.commit()
    con.close()
    return db


def test_push_kind_happy_path_advances_watermark(mock_ingest, seeded_ledger,
                                                  tmp_tokometer):
    mock_ingest.response_status = 200
    mock_ingest.response_body = {"accepted": 2, "conflicted": 0}

    from push import state as ps
    state = ps.load()
    con = sqlite3.connect(str(seeded_ledger))
    res = pc.push_kind(
        con, kind="tokometer_usage", state=state,
        ingest_url=mock_ingest.url, token="test-token",
    )
    con.close()
    assert res.attempted == 2
    assert res.accepted == 2
    assert res.error is None

    assert ps.watermark(state, "tokometer_usage") == 2  # max rowid shipped

    assert mock_ingest.requests[0][0] == "/ingest/tokometer-usage"
    body = mock_ingest.requests[0][1]
    assert body["schema_version"] == 1
    assert len(body["rows"]) == 2


def test_new_row_with_lexically_smaller_uid_is_still_pushed(
    mock_ingest, tmp_tokometer
):
    """Regression: the watermark must track INSERTION ORDER (rowid), not the
    lexical max uid.

    uids are ``<harness>:<random-uuid>`` (e.g. ``opencode:msg_...`` vs
    ``claude-code:...``) -- NOT monotonic over time. With a lexical
    ``WHERE uid > ? ORDER BY uid`` watermark, once a high-sorting uid
    (``opencode:``) advances the cursor, every later-inserted lower-sorting
    uid (``claude-code:``) is permanently skipped. This stranded ~1,852
    real usage rows on the studio-3 capture node.
    """
    db = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db))
    # Inserted FIRST -> lowest rowid, but HIGHEST lexical uid.
    con.execute(
        "INSERT INTO usage (uid, ts, harness, model, input_tokens, output_tokens, "
        "                   source, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("opencode:zzz", "2026-06-09T17:42:04Z", "opencode", "x", 1, 1,
         "cli-json", "exact"),
    )
    con.commit()
    con.close()

    from push import state as ps
    state = ps.load()

    # First push ships the opencode row and advances the watermark.
    mock_ingest.response_body = {"accepted": 1, "conflicted": 0}
    con = sqlite3.connect(str(db))
    res1 = pc.push_kind(con, kind="tokometer_usage", state=state,
                        ingest_url=mock_ingest.url, token="t")
    con.close()
    assert res1.attempted == 1

    # A NEW row arrives later (higher rowid) whose uid sorts BEFORE the cursor.
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO usage (uid, ts, harness, model, input_tokens, output_tokens, "
        "                   source, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("claude-code:aaa", "2026-06-20T05:59:53Z", "claude-code", "opus", 9, 9,
         "session-file", "exact"),
    )
    con.commit()
    con.close()

    con = sqlite3.connect(str(db))
    res2 = pc.push_kind(con, kind="tokometer_usage", state=state,
                        ingest_url=mock_ingest.url, token="t")
    con.close()

    assert res2.attempted == 1, (
        "Newer row with a lexically smaller uid must still be pushed"
    )
    assert mock_ingest.requests[-1][1]["rows"][0]["uid"] == "claude-code:aaa"


def test_watermark_is_stored_as_integer_rowid(mock_ingest, seeded_ledger):
    """After a successful push the watermark is the max rowid shipped (an int),
    not a uid string -- so it stays monotonic regardless of uid content."""
    mock_ingest.response_body = {"accepted": 2, "conflicted": 0}
    from push import state as ps
    state = ps.load()
    con = sqlite3.connect(str(seeded_ledger))
    pc.push_kind(con, kind="tokometer_usage", state=state,
                 ingest_url=mock_ingest.url, token="t")
    con.close()
    wm = ps.watermark(state, "tokometer_usage")
    assert isinstance(wm, int), f"watermark should be an int rowid, got {wm!r}"
    assert wm == 2  # two rows inserted -> rowids 1, 2


def test_legacy_uid_string_watermark_is_ignored(mock_ingest, seeded_ledger):
    """Migration: an old lexical-uid watermark left in push_state.json must be
    treated as 'start over' (re-walk by rowid). Re-sent rows are deduped
    server-side by the uid UNIQUE key, so a full re-walk is safe."""
    from push import state as ps
    state = ps.load()
    state["tokometer_usage"] = "opencode:msg_legacy_stringy_cursor"  # legacy value
    mock_ingest.response_body = {"accepted": 0, "conflicted": 2}
    con = sqlite3.connect(str(seeded_ledger))
    res = pc.push_kind(con, kind="tokometer_usage", state=state,
                       ingest_url=mock_ingest.url, token="t")
    con.close()
    assert res.attempted == 2, "legacy string watermark must not strand rows"
    assert ps.watermark(state, "tokometer_usage") == 2


def test_push_kind_422_quarantines_does_not_advance_watermark(
    mock_ingest, seeded_ledger
):
    mock_ingest.response_status = 422
    mock_ingest.response_body = {"reason": "schema_version_mismatch", "quarantined": 2}

    from push import state as ps
    state = ps.load()
    state["tokometer_usage"] = "u-test-0"  # pre-existing watermark

    con = sqlite3.connect(str(seeded_ledger))
    res = pc.push_kind(
        con, kind="tokometer_usage", state=state,
        ingest_url=mock_ingest.url, token="test-token",
    )
    con.close()

    assert res.quarantined == 2
    assert res.error and res.error.startswith("422")
    assert ps.watermark(state, "tokometer_usage") == "u-test-0", (
        "Watermark must NOT advance on 422"
    )


def test_push_kind_5xx_records_failure_does_not_advance(mock_ingest, seeded_ledger):
    mock_ingest.response_status = 503

    from push import state as ps
    state = ps.load()
    con = sqlite3.connect(str(seeded_ledger))
    res = pc.push_kind(
        con, kind="tokometer_usage", state=state,
        ingest_url=mock_ingest.url, token="test-token",
    )
    con.close()
    assert res.error and "503" in res.error
    assert ps.watermark(state, "tokometer_usage") is None


def test_push_kind_handles_missing_table_gracefully(mock_ingest, tmp_tokometer):
    """If the SQLite source table is missing (e.g. older tokometer install), we
    return an informative error rather than crashing."""
    db = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE IF EXISTS session_log_raw")
    con.commit()

    from push import state as ps
    state = ps.load()
    res = pc.push_kind(
        con, kind="session_log", state=state,
        ingest_url=mock_ingest.url, token="test-token",
    )
    con.close()
    assert "missing table" in (res.error or "")


def test_missing_table_is_not_a_real_failure():
    """A missing optional source table (e.g. the b-CLI tables on a capture-only
    node) must NOT count as a failure -- otherwise last_success_at never updates."""
    results = [
        {"kind": "time_entry", "error": "missing table time_entry"},
        {"kind": "note", "error": "missing table note"},
        {"kind": "tokometer_usage", "error": None},
        {"kind": "session_log", "error": None},
    ]
    assert pc._has_real_failure(results) is False


def test_real_error_is_a_failure():
    results = [
        {"kind": "note", "error": "missing table note"},
        {"kind": "tokometer_usage", "error": "http 503: {'detail': 'down'}"},
    ]
    assert pc._has_real_failure(results) is True


def test_push_kind_no_rows_returns_zero(mock_ingest, tmp_tokometer):
    db = tmp_tokometer / "ledger.db"
    from push import state as ps
    state = ps.load()
    con = sqlite3.connect(str(db))
    res = pc.push_kind(
        con, kind="tokometer_usage", state=state,
        ingest_url=mock_ingest.url, token="test-token",
    )
    con.close()
    assert res.attempted == 0
    assert mock_ingest.requests == []


# ─── host identity ────────────────────────────────────────────────────────

def test_host_defaults_to_short_hostname(monkeypatch):
    monkeypatch.delenv("TOKOMETER_HOST", raising=False)
    monkeypatch.setattr(pc.socket, "gethostname", lambda: "SomeBox.local")
    assert pc._host() == "somebox"


def test_host_env_override_wins(monkeypatch):
    """Operators pin a canonical host so casual naming variants
    (BladeRunner14 / Razer14 / razer) can't fragment warehouse rows."""
    monkeypatch.setattr(pc.socket, "gethostname", lambda: "BladeRunner14")
    monkeypatch.setenv("TOKOMETER_HOST", "bladerunner14")
    assert pc._host() == "bladerunner14"


def test_host_is_normalised_lowercase(monkeypatch):
    monkeypatch.delenv("TOKOMETER_HOST", raising=False)
    monkeypatch.setattr(pc.socket, "gethostname", lambda: "BladeRunner14")
    assert pc._host() == "bladerunner14"


def test_host_strips_mdns_dedup_suffix(monkeypatch):
    """macOS/Bonjour appends -2/-3 when it thinks the name is taken, which
    would fragment one Mac across several warehouse hosts (studio/studio-3)."""
    monkeypatch.delenv("TOKOMETER_HOST", raising=False)
    monkeypatch.setattr(pc.socket, "gethostname", lambda: "studio-3.local")
    assert pc._host() == "studio"


def test_host_keeps_trailing_digits_without_hyphen(monkeypatch):
    """bladerunner14's digits are part of the name, not an mDNS suffix."""
    monkeypatch.delenv("TOKOMETER_HOST", raising=False)
    monkeypatch.setattr(pc.socket, "gethostname", lambda: "BladeRunner14")
    assert pc._host() == "bladerunner14"


def test_host_env_override_is_not_suffix_stripped(monkeypatch):
    """An explicit pin is authoritative -- never second-guess the operator."""
    monkeypatch.delenv("TOKOMETER_HOST", raising=False)
    monkeypatch.setenv("TOKOMETER_HOST", "rig-3")
    assert pc._host() == "rig-3"


# ─── tokometer_usage row shape matches Beaufort's ingest model ─────────────
#
# Beaufort's TokometerUsageRow (services/ingest/models.py) ignores unknown
# fields, so a column pushed under the wrong name is silently stored as NULL.
# Its fields: ts, harness, model, account, session_id, cwd, input_tokens,
# output_tokens, cache_read, cache_write, cost_usd, meta (plus uid, host and
# schema_version from the row base).
BEAUFORT_USAGE_FIELDS = {
    "uid", "host", "schema_version", "ts", "harness", "model", "account",
    "session_id", "cwd", "input_tokens", "output_tokens", "cache_read",
    "cache_write", "cost_usd", "meta",
}


def _push_one_usage_row(mock_ingest, tmp_tokometer, **cols):
    db = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db))
    row = {"uid": "omp:s1:e1", "ts": "2026-11-24T10:00:00Z", "harness": "omp",
           "model": "Qwen3.8-Flash-Next-oQ4e-mtp", "input_tokens": 120,
           "output_tokens": 30, "cache_read_tokens": 4096, "cache_write_tokens": 64,
           "source": "session-file", "confidence": "exact", **cols}
    names = ", ".join(row)
    con.execute(f"INSERT INTO usage ({names}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()))
    con.commit()
    from push import state as ps
    mock_ingest.response_body = {"accepted": 1, "conflicted": 0}
    res = pc.push_kind(con, kind="tokometer_usage", state=ps.load(),
                       ingest_url=mock_ingest.url, token="t")
    con.close()
    assert res.error is None
    (pushed,) = mock_ingest.requests[0][1]["rows"]
    return pushed


def test_usage_rows_use_beaufort_field_names(mock_ingest, tmp_tokometer):
    pushed = _push_one_usage_row(mock_ingest, tmp_tokometer)

    assert set(pushed) <= BEAUFORT_USAGE_FIELDS, set(pushed) - BEAUFORT_USAGE_FIELDS
    assert pushed["cache_read"] == 4096
    assert pushed["cache_write"] == 64


def test_usage_row_meta_carries_provider_plan_and_org(mock_ingest, tmp_tokometer):
    """Beaufort has no provider/subscription/org columns; they travel in meta,
    with the token and confidence fields its columns don't cover."""
    pushed = _push_one_usage_row(
        mock_ingest, tmp_tokometer, provider="omlx", account="omlx",
        subscription="self-hosted", org="Local", reasoning_tokens=7, credits=0)

    assert pushed["account"] == "omlx"
    assert pushed["meta"] == {
        "provider": "omlx", "subscription": "self-hosted", "org": "Local",
        "reasoning_tokens": 7, "credits": 0,
        "source": "session-file", "confidence": "exact",
    }


def test_usage_row_meta_omits_unknown_values(mock_ingest, tmp_tokometer):
    pushed = _push_one_usage_row(mock_ingest, tmp_tokometer, provider="anthropic")

    assert pushed["meta"]["provider"] == "anthropic"
    assert "subscription" not in pushed["meta"] and "org" not in pushed["meta"]


# ─── push_all drains the backlog ───────────────────────────────────────────
#
# The push runs every 2h. Sending one batch per kind per run caps throughput
# at batch x 12 rows a day, below what the collectors write, so Beaufort fell
# further behind every day.

def _seed_usage(tmp_tokometer, n):
    db = tmp_tokometer / "ledger.db"
    con = sqlite3.connect(str(db))
    for i in range(n):
        con.execute(
            "INSERT INTO usage (uid, ts, harness, input_tokens, source, confidence) "
            "VALUES (?, '2026-11-24T10:00:00Z', 'omp', 1, 'session-file', 'exact')",
            (f"u-drain-{i}",))
    con.commit()
    con.close()
    return db


def test_push_all_sends_every_batch_in_one_run(mock_ingest, tmp_tokometer):
    db = _seed_usage(tmp_tokometer, 5)
    mock_ingest.response_body = {"accepted": 2, "conflicted": 0}

    out = pc.push_all(kinds=["tokometer_usage"], ingest_url=mock_ingest.url,
                      token="t", db_path=db, batch=2)

    sizes = [len(body["rows"]) for _, body in mock_ingest.requests]
    assert sizes == [2, 2, 1]
    (res,) = out["results"]
    assert res["attempted"] == 5 and res["error"] is None
    from push import state as ps
    assert ps.watermark(ps.load(), "tokometer_usage") == 5


def test_push_all_stops_at_a_failed_batch(mock_ingest, tmp_tokometer):
    db = _seed_usage(tmp_tokometer, 5)
    mock_ingest.response_status = 503
    mock_ingest.response_body = {"detail": "down"}

    out = pc.push_all(kinds=["tokometer_usage"], ingest_url=mock_ingest.url,
                      token="t", db_path=db, batch=2)

    assert len(mock_ingest.requests) == 1
    assert out["ok"] is False
    from push import state as ps
    assert ps.watermark(ps.load(), "tokometer_usage") is None


def test_push_all_caps_batches_per_run(mock_ingest, tmp_tokometer):
    db = _seed_usage(tmp_tokometer, 5)
    mock_ingest.response_body = {"accepted": 1, "conflicted": 0}

    pc.push_all(kinds=["tokometer_usage"], ingest_url=mock_ingest.url,
                token="t", db_path=db, batch=1, max_batches=3)

    assert len(mock_ingest.requests) == 3
    from push import state as ps
    assert ps.watermark(ps.load(), "tokometer_usage") == 3
