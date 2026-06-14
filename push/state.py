"""Device-side watermark state for the flightplan push client.

One JSON file at ~/.tokometer/state/push_state.json:

  {
    "time_entry":      "<last_uid>",
    "todo":            "<last_uid>",
    "note":            "<last_uid>",
    "tokometer_usage": "<last_uid>",
    "commit_metric":   "<last_uid>",
    "pr_metric":       "<last_uid>",
    "cursor_repo_hour":"<last_uid>",
    "session_log":     "<last_uid>",
    "_meta": {"last_success_at": "<iso>", "last_failure": null}
  }

Atomic write (write-to-temp + rename) so a crash mid-update can't corrupt.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

KINDS = (
    "time_entry", "todo", "note", "tokometer_usage",
    "commit_metric", "pr_metric", "cursor_repo_hour", "session_log",
)


def _state_path() -> Path:
    """Resolve at call time (NOT import time) so env overrides take effect."""
    home = Path(os.path.expanduser(
        os.environ.get("TOKOMETER_HOME", "~/.tokometer")
    ))
    return home / "state" / "push_state.json"


def load() -> dict:
    p = _state_path()
    if not p.exists():
        return {"_meta": {"last_success_at": None, "last_failure": None}}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"_meta": {"last_success_at": None, "last_failure": None}}


def save(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def watermark(state: dict, kind: str) -> Optional[str]:
    return state.get(kind)


def set_watermark(state: dict, kind: str, last_uid: str) -> dict:
    state[kind] = last_uid
    return state


def record_success(state: dict) -> dict:
    state.setdefault("_meta", {})["last_success_at"] = datetime.now(tz=timezone.utc).isoformat()
    state["_meta"]["last_failure"] = None
    return state


def record_failure(state: dict, kind: str, reason: str) -> dict:
    state.setdefault("_meta", {})["last_failure"] = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "kind": kind,
        "reason": reason,
    }
    return state
