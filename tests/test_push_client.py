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

    assert ps.watermark(state, "tokometer_usage") == "u-test-200"

    assert mock_ingest.requests[0][0] == "/ingest/tokometer-usage"
    body = mock_ingest.requests[0][1]
    assert body["schema_version"] == 1
    assert len(body["rows"]) == 2


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
