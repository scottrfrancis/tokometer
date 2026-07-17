#!/usr/bin/env bash
# ~/.tokometer/harvest.sh -- run each enabled collector; never let one failure abort
# the rest. Cron: 17 2 * * *  ~/.tokometer/harvest.sh >> ~/.tokometer/harvest.log 2>&1
set +e
TOKOMETER_HOME="${TOKOMETER_HOME:-$HOME/.tokometer}"
# per-machine config (also sourced by daily.sh; harmless to re-source). Defines
# TOKOMETER_HARNESSES = space-separated list of harnesses to run. Unset = all.
[ -f "$TOKOMETER_HOME/tokometer.env" ] && . "$TOKOMETER_HOME/tokometer.env"
PY="$(command -v python3)"

ALL_HARNESSES="opencode droid copilot copilot_chat_log vscode_events copilot_vscode claude_code git_metrics gh_metrics cursor"
ENABLED="${TOKOMETER_HARNESSES:-$ALL_HARNESSES}"
is_enabled() { case " $ENABLED " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

for h in opencode droid copilot copilot_chat_log vscode_events copilot_vscode claude_code git_metrics gh_metrics; do
  is_enabled "$h" || { echo "[$(date)] $h disabled (skipped)"; continue; }
  "$PY" "$TOKOMETER_HOME/collectors/${h}.py" \
    && echo "[$(date)] $h ok" \
    || echo "[$(date)] $h FAILED"
done

# cursor last (most fragile): fetch fresh CSV via playwright, then reconcile.
# A failed fetch must NOT block reconcile of the last good CSV; the morning
# report surfaces the failure from ~/.tokometer/state/cursor_fetch.json.
if is_enabled cursor; then
  "$PY" "$TOKOMETER_HOME/collectors/cursor_fetch.py" \
    && echo "[$(date)] cursor_fetch ok" \
    || echo "[$(date)] cursor_fetch FAILED (see morning report advisory)"
  "$PY" "$TOKOMETER_HOME/collectors/cursor_reconcile.py" \
    || echo "[$(date)] cursor FAILED"
  # repo attribution for cursor (from local AI-code tracking DB)
  "$PY" "$TOKOMETER_HOME/collectors/cursor_repos.py" \
    && echo "[$(date)] cursor_repos ok" \
    || echo "[$(date)] cursor_repos FAILED"
else
  echo "[$(date)] cursor disabled (skipped)"
fi

# degradation classifier: label model downgrades from the ledger + events just
# harvested (copilot-in-vscode machines). Cheap no-op when there's nothing new.
if is_enabled copilot_chat_log; then
  "$PY" "$TOKOMETER_HOME/lib/mechanisms.py" \
    && echo "[$(date)] mechanisms ok" \
    || echo "[$(date)] mechanisms FAILED"
fi
