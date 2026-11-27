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

# 1. harvest all collectors (opencode, droid, copilot, git, gh, cursor).
# Invoke via /bin/bash explicitly rather than letting harvest.sh's `#!/usr/bin/env
# bash` shebang run it: under macOS launchd, the `/usr/bin/env` indirection inserts
# an ungranted binary into TCC's responsible-process chain, so harvest's python
# grandchildren lose the /bin/bash Full-Disk/removable-volume grant and silently
# read zero files from external volumes like /Volumes/workspace (git_metrics &
# gh_metrics reported "no repos"). Keeping the whole chain on /bin/bash preserves
# the grant. (No-op on Linux/cron where TCC doesn't apply.)
/bin/bash "$TOKOMETER_HOME/harvest.sh"

# 1a. session-log collector. Not part of harvest.sh's hardcoded list, but run
#     here so it lands in the daily flow with a visible [session_logs] line in
#     this log (its absence here once hid a stalled collector for days). Reads
#     TOKOMETER_REPO_ROOT from tokometer.env, sourced above.
if [ -f "$TOKOMETER_HOME/collectors/session_logs.py" ]; then
  "$PY" "$TOKOMETER_HOME/collectors/session_logs.py" \
    && echo "[$(date)] session_logs ok" \
    || echo "[$(date)] session_logs FAILED"
fi

# 1b. push to Beaufort now, so the fleet report built on the hub at 04:40 has
#     this morning's harvest. The 2-hourly push timer keeps running as well; a
#     row sent twice is ignored there (bronze dedups on uid). Skipped on a
#     machine with no flightplan.env.
if [ -f "$TOKOMETER_HOME/flightplan.env" ] && [ -f "$TOKOMETER_HOME/push/client.py" ]; then
  ( set -a; . "$TOKOMETER_HOME/flightplan.env"; set +a
    cd "$TOKOMETER_HOME" && "$PY" -m push.client ) >> "$TOKOMETER_HOME/push-daily.log" 2>&1 \
    && echo "[$(date)] push ok" \
    || echo "[$(date)] push FAILED (see push-daily.log)"
fi

# 1c. monthly rollover -- catch-up safe; a no-op except at the first run after a
#     month turns over (so it survives the Mac being off at midnight on the 1st).
"$PY" "$TOKOMETER_HOME/monthly.py" \
  && echo "[$(date)] monthly ok" \
  || echo "[$(date)] monthly FAILED"

# 1d. API price recheck -- a no-op until 30 days after the last good check
#     (retried daily while it fails). Compares lib/pricing.py with OpenRouter;
#     the report shows any drift. Needs the network once a month, nothing else.
"$PY" "$TOKOMETER_HOME/price_check.py" \
  && echo "[$(date)] price_check ok" \
  || echo "[$(date)] price_check FAILED"

# 2. regenerate the self-contained HTML report, then open it in the browser so
#    it is waiting for you in the morning. (launchd runs in your GUI session, so
#    `open` works; if the Mac was asleep at 04:00 it opens on next wake.) With a
#    fleet hub set in fleet.env, fleet_open.sh opens the all-machines report at
#    05:05 instead. TOKOMETER_OPEN / TOKOMETER_UNAME exist for the tests.
REPORT_PATH="$("$PY" "$TOKOMETER_HOME/report_html.py")"
if [ -n "$REPORT_PATH" ] && [ -f "$REPORT_PATH" ]; then
  echo "[$(date)] report ok -> $REPORT_PATH"
  if [ "$("${TOKOMETER_UNAME:-uname}")" = "Darwin" ]; then
    if grep -qE '^FLEET_HUB=.+' "$TOKOMETER_HOME/fleet.env" 2>/dev/null; then
      echo "[$(date)] local report not opened: fleet-open opens the fleet report"
    elif "${TOKOMETER_OPEN:-open}" "$REPORT_PATH"; then
      echo "[$(date)] opened $REPORT_PATH"
    else
      echo "[$(date)] open FAILED for $REPORT_PATH"
    fi
  fi
else
  echo "[$(date)] report FAILED"
fi

# 3. Copilot strategy report (locked-down-laptop machines): daily always,
#    weekly rollup on Mondays. No-ops quietly when the collector is disabled.
case " ${TOKOMETER_HARNESSES:-} " in
  *" copilot_chat_log "*)
    "$PY" "$TOKOMETER_HOME/report_copilot.py" >/dev/null \
      && echo "[$(date)] copilot daily report ok" \
      || echo "[$(date)] copilot daily report FAILED"
    if [ "$(date +%u)" = "1" ]; then
      "$PY" "$TOKOMETER_HOME/report_copilot.py" --weekly >/dev/null \
        && echo "[$(date)] copilot weekly report ok" \
        || echo "[$(date)] copilot weekly report FAILED"
    fi
    ;;
esac

echo "===== done $(date) ====="
