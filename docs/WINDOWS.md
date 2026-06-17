# Running tokometer on Windows (Git Bash)

tokometer targets macOS and Linux, but it runs on Windows under **Git Bash** with a few
adaptations. The collectors are stdlib Python and the drivers are bash scripts, so the
only real work is supplying `python3` + `sqlite3`, forcing UTF-8 output, and swapping the
macOS launchd schedule for a Windows **Task Scheduler** job. Everything below is additive
to the standard [Quick start](../README.md#quick-start) — the same `install.sh`,
`harvest.sh`, `report_html.py`, and `tokometer.env` are used unchanged.

> Tested on Windows 11 with Git for Windows (Git Bash), Python 3.12, SQLite 3.53.

## 1. Prerequisites

Install a real Python and the SQLite CLI. With [winget](https://learn.microsoft.com/windows/package-manager/winget/):

```powershell
winget install --id Python.Python.3.12 -e
winget install --id SQLite.SQLite -e
```

`install.sh` shells out to the `sqlite3` **CLI** for the schema, and the collectors use
Python's bundled `sqlite3` **module** — you need both, which the two packages above cover.
`git` (and optionally `gh`) come from Git for Windows / the GitHub CLI as usual.

## 2. Make `python3` resolve to the real interpreter

The scripts invoke `python3`. The Windows Store ships an *App execution alias* named
`python3.exe` that is only a redirector to the Store — it will shadow real Python and the
scripts will silently fail to run. The Python.org/winget installer provides `python.exe`
but **not** `python3.exe`, so create one next to the genuine interpreter:

```powershell
$pydir = "$env:LOCALAPPDATA\Programs\Python\Python312"
Copy-Item "$pydir\python.exe" "$pydir\python3.exe"
```

The winget installer already puts `...\Python312\` on your user `PATH` **ahead of**
`...\WindowsApps\`, so this `python3.exe` wins over the Store stub. Verify in a fresh
Git Bash:

```sh
command -v python3      # -> /c/Users/<you>/AppData/Local/Programs/Python/Python312/python3
python3 --version       # -> Python 3.12.x  (NOT a Store prompt)
command -v sqlite3      # -> .../WinGet/Links/sqlite3
```

If `python3` still points at `.../WindowsApps/python3`, either reorder your PATH so the
Python dir precedes WindowsApps, or turn the alias off in
**Settings → Apps → Advanced app settings → App execution aliases**.

## 3. Force UTF-8 output

The morning **text** report prints unicode glyphs (e.g. `⚠`). The Windows console defaults
to the cp1252 codec, which raises `UnicodeEncodeError` on those. Add this to your
`~/.tokometer/tokometer.env` (it is sourced by both `harvest.sh` and `daily.sh`, so every
run — manual or scheduled — picks it up):

```sh
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
```

The self-contained **HTML** report writes UTF-8 directly and is unaffected, but setting
these is harmless and keeps the text report working too.

## 4. Install and first run

From the cloned repo, in Git Bash:

```sh
TOKOMETER_SKIP_PLAYWRIGHT=1 ./install.sh   # Cursor scrape is the only Playwright user
$EDITOR ~/.tokometer/tokometer.env         # set TOKOMETER_GIT_ROOT / author; add the UTF-8 lines
~/.tokometer/harvest.sh                     # first harvest
python3 ~/.tokometer/report_html.py         # build HTML report (prints its path)
```

`install.sh` skips the macOS launchd step on non-Darwin platforms (see §5 for the Windows
schedule). It auto-detects `TOKOMETER_GIT_ROOT`; note that root is scanned recursively but
is a **single** path, so if your repos live under more than one tree (e.g. both `~/repos`
and `~/workspace`) point it at the one you care most about — don't point it at your home
directory, which would crawl all of `AppData`.

Open the HTML report with `start ~/.tokometer/reports/morning-YYYY-MM-DD.html` (Git Bash)
or just double-click it.

## 5. Schedule the daily run with Task Scheduler

macOS uses launchd and Linux uses cron; on Windows, register `daily.sh` (harvest → monthly
rollover → regenerate HTML) as a Task Scheduler job. Run this **once** in PowerShell,
substituting your Git Bash path if different:

```powershell
$bash    = "C:\Program Files\Git\bin\bash.exe"   # NOT C:\Windows\System32\bash.exe (that's WSL)
$home_   = "/c/Users/$env:USERNAME/.tokometer"
$action  = New-ScheduledTaskAction -Execute $bash `
             -Argument "-lc `"$home_/daily.sh >> $home_/daily.log 2>&1`""
$trigger = New-ScheduledTaskTrigger -Daily -At 4:00AM
$set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$prin    = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName "tokometer-daily" -Action $action -Trigger $trigger `
  -Settings $set -Principal $prin -Description "tokometer daily harvest + report" -Force
```

Notes:
- Invoke Git Bash with **`-lc`** (login shell) so it loads your user `PATH` and `python3`
  resolves — without `-l`, the task starts with a bare PATH and the run fails.
- `-LogonType Interactive` runs only while you're logged in (no stored password needed),
  matching the macOS launchd default. With `StartWhenAvailable`, a missed 04:00 run (machine
  asleep/off) fires at the next opportunity.
- The browser auto-open in `daily.sh` is macOS-only (`open`), so the Windows job just
  refreshes the report on disk.

Test it without waiting for 04:00:

```powershell
Start-ScheduledTask -TaskName "tokometer-daily"
(Get-ScheduledTaskInfo -TaskName "tokometer-daily").LastTaskResult   # 0 = success
```

Then check `~/.tokometer/daily.log` for the run output. Remove the job with
`Unregister-ScheduledTask -TaskName "tokometer-daily" -Confirm:$false`.

## Caveats on Windows

- **Cursor** harvesting (Playwright) is untested here; `TOKOMETER_SKIP_PLAYWRIGHT=1` keeps
  it out of the way. Everything else (OpenCode, Claude Code, git/gh metrics) works.
- Paths in report output mix separators (e.g. `C:\Users\you/.tokometer\reports\...`) — that
  is cosmetic; the files resolve fine.
