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


def test_fresh_install_enables_omp(tmp_path):
    result, tokometer_home = _run_install(tmp_path, uname_override="Linux")
    assert result.returncode == 0, result.stderr

    env_text = (tokometer_home / "tokometer.env").read_text()
    enabled = next(line for line in env_text.splitlines()
                   if line.startswith("export TOKOMETER_HARNESSES="))
    assert " omp " in f" {enabled.split('=', 1)[1].strip(chr(34))} "


def test_installs_without_the_sqlite3_cli(tmp_path):
    """A stock Kubuntu desktop has python3 but not the sqlite3 CLI. install.sh
    stopped at its first `sqlite3` call there (set -e), after copying the
    collectors but before the schema, the push package and the migrations."""
    import shutil
    tools = tmp_path / "only_bin"
    tools.mkdir()
    for name in ("bash", "sh", "dirname", "mkdir", "cp", "chmod", "cat", "grep",
                 "git", "python3", "whoami"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    for name, body in (("uname", 'echo "Linux"'), ("launchctl", "exit 0")):
        stub = tools / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    tokometer_home = tmp_path / "tokometer_home"

    result = subprocess.run(
        [str(tools / "bash"), str(INSTALL_SH)], cwd=str(REPO_ROOT),
        env={"HOME": str(home), "TOKOMETER_HOME": str(tokometer_home),
             "TOKOMETER_SKIP_PLAYWRIGHT": "1", "PATH": str(tools)},
        capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert (tokometer_home / "push" / "client.py").is_file()
    import sqlite3
    con = sqlite3.connect(str(tokometer_home / "ledger.db"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(usage)")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"account", "subscription", "org", "cwd", "credits"} <= cols
    assert "session_log_raw" in tables


def test_installed_report_renders(tmp_path):
    """daily.sh runs report_html.py from TOKOMETER_HOME, so every module it
    imports has to be copied there. A missing lib module fails the report at
    04:00 with nothing but "report FAILED" in daily.log."""
    import sys
    result, tokometer_home = _run_install(tmp_path, uname_override="Linux")
    assert result.returncode == 0, result.stderr

    report = subprocess.run(
        [sys.executable, str(tokometer_home / "report_html.py")],
        env={"TOKOMETER_HOME": str(tokometer_home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60)
    assert report.returncode == 0, report.stderr
    assert Path(report.stdout.strip()).is_file()
