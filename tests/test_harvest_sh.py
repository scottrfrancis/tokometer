"""harvest.sh runs every enabled collector, including omp."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_omp_collector_runs_when_enabled(tmp_path):
    home = tmp_path / "tokometer_home"
    (home / "collectors").mkdir(parents=True)
    (home / "collectors" / "omp.py").write_text("print('omp ran')\n")
    env = {"HOME": str(tmp_path), "TOKOMETER_HOME": str(home),
           "TOKOMETER_HARNESSES": "omp", "PATH": os.environ["PATH"]}

    out = subprocess.run(["bash", str(REPO_ROOT / "harvest.sh")], env=env,
                         capture_output=True, text=True, timeout=60).stdout

    assert "omp ran" in out
    assert "omp ok" in out


def test_openclaw_and_openwebui_collectors_run_when_enabled(tmp_path):
    home = tmp_path / "tokometer_home"
    (home / "collectors").mkdir(parents=True)
    for h in ("openclaw", "openwebui"):
        (home / "collectors" / f"{h}.py").write_text(f"print('{h} ran')\n")
    env = {"HOME": str(tmp_path), "TOKOMETER_HOME": str(home),
           "TOKOMETER_HARNESSES": "openclaw openwebui", "PATH": os.environ["PATH"]}

    out = subprocess.run(["bash", str(REPO_ROOT / "harvest.sh")], env=env,
                         capture_output=True, text=True, timeout=60).stdout

    for h in ("openclaw", "openwebui"):
        assert f"{h} ran" in out and f"{h} ok" in out, out
