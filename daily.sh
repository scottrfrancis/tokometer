#!/usr/bin/env bash
# Daily driver: harvest every source, then regenerate the HTML morning report.
# Designed to run unattended from cron, which starts with a bare PATH -- so we
# put the toolchain (miniconda python/sqlite3, homebrew gh, git, ~/.local/bin)
# on PATH explicitly. One failing step never aborts the rest.
#
# Install the cron entry (4 AM daily) with:
#   (crontab -l 2>/dev/null; echo "0 4 * * * $HOME/.tokometer/daily.sh >> $HOME/.tokometer/daily.log 2>&1") | crontab -
set +e
# cron/launchd start with a bare PATH; put common toolchains up front, then keep
# whatever PATH an interactive run already has (so both contexts find python3 etc).
export PATH="$HOME/miniconda3/bin:$HOME/miniforge3/bin:/opt/homebrew/Caskroom/miniforge/base/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
TOKOMETER_HOME="${TOKOMETER_HOME:-$HOME/.tokometer}"

# per-machine config (git scan root/author, etc.); created by install.sh, not in
# version control. Sourced so cron/launchd runs pick up machine-specific paths.
[ -f "$TOKOMETER_HOME/tokometer.env" ] && . "$TOKOMETER_HOME/tokometer.env"

PY="$(command -v python3)"

echo "===== tokometer daily $(date) ====="

# 1. harvest all collectors (opencode, droid, copilot, git, gh, cursor)
"$TOKOMETER_HOME/harvest.sh"

# 1a. session-log collector. Not part of harvest.sh's hardcoded list, but run
#     here so it lands in the daily flow with a visible [session_logs] line in
#     this log (its absence here once hid a stalled collector for days). Reads
#     TOKOMETER_REPO_ROOT from tokometer.env, sourced above.
if [ -f "$TOKOMETER_HOME/collectors/session_logs.py" ]; then
  "$PY" "$TOKOMETER_HOME/collectors/session_logs.py" \
    && echo "[$(date)] session_logs ok" \
    || echo "[$(date)] session_logs FAILED"
fi

# 1b. monthly rollover -- catch-up safe; a no-op except at the first run after a
#     month turns over (so it survives the Mac being off at midnight on the 1st).
"$PY" "$TOKOMETER_HOME/monthly.py" \
  && echo "[$(date)] monthly ok" \
  || echo "[$(date)] monthly FAILED"

# 2. regenerate the self-contained HTML report, then open it in the browser so
#    it is waiting for you in the morning. (launchd runs in your GUI session, so
#    `open` works; if the Mac was asleep at 04:00 it opens on next wake.)
REPORT_PATH="$("$PY" "$TOKOMETER_HOME/report_html.py")"
if [ -n "$REPORT_PATH" ] && [ -f "$REPORT_PATH" ]; then
  echo "[$(date)] report ok -> $REPORT_PATH"
  [ "$(uname)" = "Darwin" ] && open "$REPORT_PATH" 2>/dev/null || true
else
  echo "[$(date)] report FAILED"
fi

echo "===== done $(date) ====="
