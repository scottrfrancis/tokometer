#!/usr/bin/env python3
"""Print the fleet report's lines for Morning Kata (installed as kata-tokens).

Reads $TOKOMETER_HOME/reports/fleet-summary.json, written by fleet.py, and
prints its kata_lines when it is today's. Kata pastes the output verbatim, so
every number comes from fleet.py. A summary from another day is reported as
missing instead of printed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path


def summary_path() -> Path:
    home = Path(os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer")))
    return home / "reports" / "fleet-summary.json"


def lines(path: Path, today: dt.date) -> list[str]:
    try:
        summary = json.loads(path.read_text())
    except FileNotFoundError:
        return [":warning: Tokens: no fleet report yet"]
    if summary.get("date") != today.isoformat():
        return [f":warning: Tokens: no fleet report for today (last {summary.get('date')})"]
    return list(summary.get("kata_lines") or [])


def main() -> int:
    print("\n".join(lines(summary_path(), dt.date.today())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
