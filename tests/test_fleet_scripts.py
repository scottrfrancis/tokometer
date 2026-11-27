"""fleet.sh builds the fleet report on the hub; fleet_open.sh opens it on a Mac.

fleet_open.sh copies the hub's fleet-latest.html and fleet-summary.json with
scp, opens the page if the summary is today's, and otherwise opens this
machine's own report with a banner saying why. Tests stub scp (it copies from
a local "hub" directory) and the open command (it records what it was given).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _setup(tmp_path: Path, *, hub_date: str | None, fleet_hub: str | None = "hub.example"):
    home = tmp_path / "tokometer_home"
    (home / "reports").mkdir(parents=True)
    hub = tmp_path / "hub" / ".tokometer" / "reports"
    hub.mkdir(parents=True)
    if hub_date:
        (hub / "fleet-summary.json").write_text(json.dumps({"date": hub_date}))
        (hub / "fleet-latest.html").write_text("<html><body>FLEET</body></html>")
    if fleet_hub:
        (home / "fleet.env").write_text(f"FLEET_HUB={fleet_hub}\n")
    today = dt.date.today().isoformat()
    (home / "reports" / f"morning-{today}.html").write_text("<html><body>LOCAL</body></html>")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # scp [opts] host:path dest  ->  cp <hub root>/path dest
    _exe(stubs / "scp", f'#!/bin/bash\nsrc="${{@: -2:1}}"; dst="${{@: -1}}"\n'
                        f'cp "{tmp_path / "hub"}/${{src#*:}}" "$dst"\n')
    _exe(stubs / "record-open", f'#!/bin/bash\necho "$1" >> "{tmp_path / "opened"}"\n')
    env = {"HOME": str(tmp_path), "TOKOMETER_HOME": str(home),
           "TOKOMETER_OPEN": str(stubs / "record-open"),
           "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    return home, env


def _open(tmp_path, env):
    run = subprocess.run(["/bin/bash", str(REPO_ROOT / "fleet_open.sh")], env=env,
                         capture_output=True, text=True, timeout=60)
    opened = (tmp_path / "opened").read_text().split() if (tmp_path / "opened").exists() else []
    return run, opened


def test_todays_fleet_report_is_copied_and_opened(tmp_path):
    today = dt.date.today().isoformat()
    home, env = _setup(tmp_path, hub_date=today)

    run, opened = _open(tmp_path, env)

    assert run.returncode == 0, run.stdout + run.stderr
    page = home / "reports" / f"fleet-{today}.html"
    assert opened == [str(page)]
    assert "FLEET" in page.read_text()


def test_a_stale_fleet_report_opens_the_local_report_with_the_reason(tmp_path):
    home, env = _setup(tmp_path, hub_date="2026-01-01")

    run, opened = _open(tmp_path, env)

    assert run.returncode == 0, run.stdout + run.stderr
    (page,) = opened
    text = Path(page).read_text()
    assert "LOCAL" in text and "2026-01-01" in text, "local report, banner names the stale day"
    assert "stale" in run.stdout or "2026-01-01" in run.stdout


def test_an_unreachable_hub_opens_the_local_report(tmp_path):
    home, env = _setup(tmp_path, hub_date=None)

    run, opened = _open(tmp_path, env)

    (page,) = opened
    assert "LOCAL" in Path(page).read_text() and "could not copy" in Path(page).read_text()


def test_without_a_hub_configured_nothing_is_opened(tmp_path):
    home, env = _setup(tmp_path, hub_date=dt.date.today().isoformat(), fleet_hub=None)

    run, opened = _open(tmp_path, env)

    assert run.returncode == 0 and opened == []


def test_fleet_sh_runs_fleet_py_with_the_fleet_config(tmp_path):
    home = tmp_path / "tokometer_home"
    home.mkdir()
    (home / "fleet.env").write_text("FLEET_SINCE=2026-11-28\nFLEET_PYTHON=python3\n")
    (home / "fleet.py").write_text('import os\nprint("built since", os.environ["FLEET_SINCE"])\n')

    run = subprocess.run(["/bin/bash", str(REPO_ROOT / "fleet.sh")],
                         env={"HOME": str(tmp_path), "TOKOMETER_HOME": str(home),
                              "PATH": os.environ["PATH"]},
                         capture_output=True, text=True, timeout=60)

    assert run.returncode == 0, run.stderr
    assert "fleet ok -> built since 2026-11-28" in run.stdout


def test_fleet_sh_without_config_does_nothing(tmp_path):
    run = subprocess.run(["/bin/bash", str(REPO_ROOT / "fleet.sh")],
                         env={"HOME": str(tmp_path), "TOKOMETER_HOME": str(tmp_path),
                              "PATH": os.environ["PATH"]},
                         capture_output=True, text=True, timeout=60)

    assert run.returncode == 0 and "skipping" in run.stdout
