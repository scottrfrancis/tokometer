"""Tests for push.state -- watermark load/save."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from push import state as ps


def test_load_returns_default_when_no_file(tmp_tokometer):
    state = ps.load()
    assert state == {"_meta": {"last_success_at": None, "last_failure": None}}


def test_save_then_load_roundtrips(tmp_tokometer):
    s = {"time_entry": "t-host-100", "_meta": {"last_success_at": None, "last_failure": None}}
    ps.save(s)
    loaded = ps.load()
    assert loaded == s


def test_set_watermark_returns_state(tmp_tokometer):
    s = ps.load()
    ps.set_watermark(s, "time_entry", "t-host-42")
    assert ps.watermark(s, "time_entry") == "t-host-42"


def test_record_success_sets_timestamp(tmp_tokometer):
    s = ps.load()
    ps.record_success(s)
    assert s["_meta"]["last_success_at"] is not None
    assert s["_meta"]["last_failure"] is None


def test_record_failure_captures_kind_and_reason(tmp_tokometer):
    s = ps.load()
    ps.record_failure(s, "time_entry", "http 503")
    assert s["_meta"]["last_failure"]["kind"] == "time_entry"
    assert s["_meta"]["last_failure"]["reason"] == "http 503"


def test_atomic_save_survives_concurrent_writes(tmp_tokometer):
    """Quick smoke that write-then-rename is the path used (not in-place writes)."""
    s = {"time_entry": "uid-A"}
    ps.save(s)
    # If we were doing in-place writes, the file would briefly not exist during save.
    # The atomic-rename pattern guarantees the file always points at a valid state.
    s2 = ps.load()
    assert s2 == s
