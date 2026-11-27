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
