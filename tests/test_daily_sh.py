"""daily.sh pushes to Beaufort right after harvesting.

The fleet report on hasami reads Beaufort at 04:40. The push timer alone runs
every two hours from whenever the machine booted, so without a push in the
daily run a machine's 04:00 harvest can miss that morning's report.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
STEP = 'import os, sys; open(os.environ["TRACE"], "a").write("{name}\\n")\n'


def _stub_install(home: Path, trace: Path, *, push_env: bool) -> Path:
    """A TOKOMETER_HOME whose every step appends its name to TRACE."""
    th = home / ".tokometer"
    (th / "collectors").mkdir(parents=True)
    (th / "push").mkdir()
    (th / "harvest.sh").write_text(f'#!/bin/bash\necho harvest >> "{trace}"\n')
    for name, rel in [("session_logs", "collectors/session_logs.py"),
                      ("monthly", "monthly.py"), ("price_check", "price_check.py"),
                      ("report", "report_html.py")]:
        (th / rel).write_text(STEP.format(name=name))
    (th / "push" / "__init__.py").write_text("")
    (th / "push" / "client.py").write_text(
        'import os\n'
        'open(os.environ["TRACE"], "a").write('
        '"push " + os.environ.get("FLIGHTPLAN_INGEST_URL", "-") + "\\n")\n')
    if push_env:
        (th / "flightplan.env").write_text("FLIGHTPLAN_INGEST_URL=http://hub.invalid:7321\n")
    daily = th / "daily.sh"
    daily.write_text((REPO_ROOT / "daily.sh").read_text())
    daily.chmod(daily.stat().st_mode | stat.S_IEXEC)
    return th


def _run_daily(tmp_path: Path, *, push_env: bool) -> list[str]:
    home = tmp_path / "home"
    trace = tmp_path / "trace"
    th = _stub_install(home, trace, push_env=push_env)
    result = subprocess.run(
        ["/bin/bash", str(th / "daily.sh")],
        env={"HOME": str(home), "TOKOMETER_HOME": str(th), "TRACE": str(trace),
             "PATH": os.environ["PATH"]},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return trace.read_text().split("\n")[:-1] if trace.exists() else []


def test_daily_pushes_after_harvesting_with_the_push_config(tmp_path):
    steps = _run_daily(tmp_path, push_env=True)

    assert "push http://hub.invalid:7321" in steps, steps
    push = steps.index("push http://hub.invalid:7321")
    assert push > steps.index("harvest") and push > steps.index("session_logs"), steps


def test_daily_skips_the_push_on_a_machine_without_push_config(tmp_path):
    steps = _run_daily(tmp_path, push_env=False)

    assert not [s for s in steps if s.startswith("push")], steps
    assert "harvest" in steps


def _run_daily_report(tmp_path: Path, *, fleet_hub: bool, darwin: bool) -> tuple[str, list[str]]:
    """daily.sh with a report step that prints a real page path, a stubbed
    uname and a recording open command."""
    home = tmp_path / "home"
    trace = tmp_path / "trace"
    th = _stub_install(home, trace, push_env=False)
    page = th / "reports" / "morning.html"
    page.parent.mkdir()
    page.write_text("<html></html>")
    (th / "report_html.py").write_text(f'print("{page}")\n')
    if fleet_hub:
        (th / "fleet.env").write_text("FLEET_HUB=hub.example\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "uname").write_text(f'#!/bin/sh\necho {"Darwin" if darwin else "Linux"}\n')
    (stubs / "record-open").write_text(f'#!/bin/sh\necho "$1" >> "{tmp_path / "opened"}"\n')
    for s in stubs.iterdir():
        s.chmod(s.stat().st_mode | stat.S_IEXEC)
    run = subprocess.run(
        ["/bin/bash", str(th / "daily.sh")],
        env={"HOME": str(home), "TOKOMETER_HOME": str(th), "TRACE": str(trace),
             "TOKOMETER_OPEN": str(stubs / "record-open"), "TOKOMETER_UNAME": str(stubs / "uname"),
             "PATH": os.environ["PATH"]},
        capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    opened = (tmp_path / "opened").read_text().split() if (tmp_path / "opened").exists() else []
    return run.stdout, opened


def test_on_a_mac_daily_opens_its_own_report_when_no_hub_is_configured(tmp_path):
    out, opened = _run_daily_report(tmp_path, fleet_hub=False, darwin=True)

    assert len(opened) == 1 and opened[0].endswith("morning.html"), out


def test_with_a_hub_configured_daily_leaves_opening_to_fleet_open(tmp_path):
    out, opened = _run_daily_report(tmp_path, fleet_hub=True, darwin=True)

    assert opened == []
    assert "fleet-open opens the fleet report" in out


def test_daily_never_opens_a_report_off_a_mac(tmp_path):
    out, opened = _run_daily_report(tmp_path, fleet_hub=False, darwin=False)

    assert opened == []
