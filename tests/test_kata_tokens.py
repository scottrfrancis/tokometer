"""kata_tokens.py prints the fleet summary's lines for Morning Kata, verbatim.

Kata pastes this output as is, so every number in it comes from fleet.py.
When today's summary is missing, the output says so in place of the numbers,
so a stale figure can never pass for today's.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import kata_tokens

TODAY = dt.date(2026, 12, 3)
LINES = [":bar_chart: Tokens yesterday: 3.0M on 3 machines, $8.19 list value",
         "   MTD $10: Personal max $6.00 · Acme team $4.00 · Local $0.19"]


def _summary(tmp_path: Path, date: str) -> Path:
    p = tmp_path / "fleet-summary.json"
    p.write_text(json.dumps({"date": date, "kata_lines": LINES}))
    return p


def test_todays_summary_is_printed_as_written(tmp_path):
    assert kata_tokens.lines(_summary(tmp_path, "2026-12-03"), TODAY) == LINES


def test_an_old_summary_is_reported_missing_not_printed(tmp_path):
    assert kata_tokens.lines(_summary(tmp_path, "2026-12-02"), TODAY) == [
        ":warning: Tokens: no fleet report for today (last 2026-12-02)"]


def test_no_summary_at_all(tmp_path):
    assert kata_tokens.lines(tmp_path / "fleet-summary.json", TODAY) == [
        ":warning: Tokens: no fleet report yet"]


def test_the_command_reads_the_installed_summary(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    _summary(reports, dt.date.today().isoformat())

    out = subprocess.run([sys.executable, str(Path(kata_tokens.__file__))],
                         env={"TOKOMETER_HOME": str(tmp_path)}, capture_output=True, text=True,
                         timeout=30)

    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == LINES
