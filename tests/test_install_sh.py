"""install.sh must not silently abort, and must actually schedule on Linux.

Regression 1: ``GITAUTHOR="$(git config --global user.email 2>/dev/null)"``
returns a non-zero exit status when no global git email is configured (a
realistic state on a freshly provisioned box). Under `set -e`, a plain
`VAR=$(cmd)` assignment propagates that failure and kills the whole script
right there -- silently, with no error message -- before the intended
`${GITAUTHOR:-you@example.com}` fallback on the next line ever runs.

Regression 2: the README documents "on Linux, add the cron line install.sh
prints", but the scheduling block only has a Darwin branch and a Windows
branch -- Linux falls through and nothing is printed or scheduled.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _run_install(tmp_path, *, uname_override=None, extra_env=None):
    """Run install.sh with an isolated HOME/TOKOMETER_HOME and no global git
    config, optionally with a stubbed `uname` earlier on PATH.

    `launchctl` is ALWAYS stubbed to a no-op: on a real Darwin dev box,
    install.sh's Darwin branch would otherwise call the real `launchctl
    load -w` against a plist under a tmp HOME, registering a real launchd
    job pointing at a directory that vanishes when the test cleans up.
    """
    home = tmp_path / "home"
    home.mkdir()
    tokometer_home = tmp_path / "tokometer_home"

    stub_dir = tmp_path / "stub_bin"
    stub_dir.mkdir()
    launchctl_stub = stub_dir / "launchctl"
    launchctl_stub.write_text("#!/bin/sh\nexit 0\n")
    launchctl_stub.chmod(launchctl_stub.stat().st_mode | stat.S_IEXEC)

    if uname_override:
        uname_stub = stub_dir / "uname"
        uname_stub.write_text(f'#!/bin/sh\necho "{uname_override}"\n')
        uname_stub.chmod(uname_stub.stat().st_mode | stat.S_IEXEC)

    path_entries = [
        str(stub_dir),
        "/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin",
    ]

    env = {
        "HOME": str(home),
        "TOKOMETER_HOME": str(tokometer_home),
        "TOKOMETER_SKIP_PLAYWRIGHT": "1",
        "PATH": os.pathsep.join(path_entries),
    }
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, tokometer_home


def test_survives_missing_global_git_email(tmp_path):
    result, tokometer_home = _run_install(tmp_path)

    assert result.returncode == 0, (
        f"install.sh aborted (rc={result.returncode}) with no global git email set.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    env_text = (tokometer_home / "tokometer.env").read_text()
    assert 'TOKOMETER_GIT_AUTHOR="you@example.com"' in env_text


def test_linux_prints_cron_line(tmp_path):
    result, tokometer_home = _run_install(tmp_path, uname_override="Linux")

    assert result.returncode == 0, (
        f"install.sh failed on simulated Linux (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "crontab" in result.stdout.lower()
    assert f"{tokometer_home}/daily.sh" in result.stdout
    assert "0 4 * * *" in result.stdout


def test_darwin_path_unaffected_by_linux_fix(tmp_path):
    """Regression guard: adding the Linux branch must not touch the existing
    Darwin (launchd) behavior for machines that actually run this on macOS."""
    import platform

    if platform.system() != "Darwin":
        import pytest

        pytest.skip("only meaningful on a real Darwin host")

    result, tokometer_home = _run_install(tmp_path)
    assert result.returncode == 0
    assert "scheduled daily.sh at 04:00" in result.stdout
    assert "crontab" not in result.stdout.lower()
