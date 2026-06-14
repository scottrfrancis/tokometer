#!/usr/bin/env bash
# Install the tokometer harness from this source repo into ~/.tokometer (local only).
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKOMETER_HOME="${TOKOMETER_HOME:-$HOME/.tokometer}"

echo "Installing tokometer -> $TOKOMETER_HOME"
mkdir -p "$TOKOMETER_HOME/collectors" "$TOKOMETER_HOME/lib" "$TOKOMETER_HOME/state" "$TOKOMETER_HOME/cursor-exports"

# code
cp "$SRC/lib/ledger.py"            "$TOKOMETER_HOME/lib/"
cp "$SRC/collectors/"*.py          "$TOKOMETER_HOME/collectors/"
cp "$SRC/harvest.sh"               "$TOKOMETER_HOME/harvest.sh"
cp "$SRC/daily.sh"                 "$TOKOMETER_HOME/daily.sh"
cp "$SRC/report_morning.py"        "$TOKOMETER_HOME/report_morning.py"
cp "$SRC/report_html.py"           "$TOKOMETER_HOME/report_html.py"
cp "$SRC/monthly.py"               "$TOKOMETER_HOME/monthly.py"
chmod +x "$TOKOMETER_HOME/harvest.sh" "$TOKOMETER_HOME/daily.sh"

# ledger schema (idempotent)
sqlite3 "$TOKOMETER_HOME/ledger.db" < "$SRC/schema.sql"

# flightplan extensions: session_log_raw + collectors/session_logs.py + push/
if [ -f "$SRC/schema_session_logs.sql" ]; then
  sqlite3 "$TOKOMETER_HOME/ledger.db" < "$SRC/schema_session_logs.sql" \
    && echo "migrated: session_log_raw table"
fi

# also stage the push package + session_logs collector for the flightplan path.
# Existing tokometer installs stay valid without these (no env vars set ->
# push.client refuses to run; session_logs collector is opt-in via cron).
if [ -d "$SRC/push" ]; then
  mkdir -p "$TOKOMETER_HOME/push"
  cp "$SRC/push/__init__.py" "$TOKOMETER_HOME/push/__init__.py"
  cp "$SRC/push/state.py"    "$TOKOMETER_HOME/push/state.py"
  cp "$SRC/push/client.py"   "$TOKOMETER_HOME/push/client.py"
fi
if [ -f "$SRC/collectors/session_logs.py" ]; then
  cp "$SRC/collectors/session_logs.py" "$TOKOMETER_HOME/collectors/session_logs.py"
fi
# Example env (operator copies to ~/.tokometer/flightplan.env and fills in)
if [ -f "$SRC/flightplan.env.example" ]; then
  cp "$SRC/flightplan.env.example" "$TOKOMETER_HOME/flightplan.env.example"
fi

# additive migrations for pre-existing ledgers (ignore "duplicate column" errors)
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "ALTER TABLE usage ADD COLUMN credits INTEGER DEFAULT 0;" 2>/dev/null \
  && echo "migrated: added usage.credits" || true
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "ALTER TABLE usage ADD COLUMN cwd TEXT;" 2>/dev/null \
  && echo "migrated: added usage.cwd" || true
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "ALTER TABLE usage ADD COLUMN account TEXT;" 2>/dev/null \
  && echo "migrated: added usage.account" || true
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "ALTER TABLE usage ADD COLUMN subscription TEXT;" 2>/dev/null \
  && echo "migrated: added usage.subscription" || true
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "ALTER TABLE usage ADD COLUMN org TEXT;" 2>/dev/null \
  && echo "migrated: added usage.org" || true
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "CREATE INDEX IF NOT EXISTS idx_usage_account ON usage(account);" 2>/dev/null || true
sqlite3 "$TOKOMETER_HOME/ledger.db" \
  "CREATE INDEX IF NOT EXISTS idx_usage_org ON usage(org);" 2>/dev/null || true

# per-machine config (git scan root + author), auto-detected, never clobbered.
# Edit ~/.tokometer/tokometer.env afterwards to taste; daily.sh sources it.
if [ ! -f "$TOKOMETER_HOME/tokometer.env" ]; then
  # pick the first candidate that actually contains git repos (1-2 levels deep);
  # fall back to the first that merely exists, else ~/workspace.
  GITROOT=""; GITROOT_EXISTS=""
  for cand in "$HOME/workspace" "/Volumes/workspace" "$HOME/src" "$HOME/code" "$HOME/projects"; do
    [ -d "$cand" ] || continue
    [ -z "$GITROOT_EXISTS" ] && GITROOT_EXISTS="$cand"
    if compgen -G "$cand/*/.git" >/dev/null 2>&1 || compgen -G "$cand/*/*/.git" >/dev/null 2>&1; then
      GITROOT="$cand"; break
    fi
  done
  GITROOT="${GITROOT:-${GITROOT_EXISTS:-$HOME/workspace}}"
  GITAUTHOR="$(git config --global user.email 2>/dev/null)"
  GITAUTHOR="${GITAUTHOR:-you@example.com}"
  cat > "$TOKOMETER_HOME/tokometer.env" <<ENVEOF
# tokometer per-machine config -- sourced by daily.sh and harvest.sh. Local only; not committed.
# Harnesses to harvest + surface (space-separated). Drop ones you don't use to avoid
# irrelevant errors/advisories. Available: opencode droid copilot claude_code git_metrics gh_metrics cursor
export TOKOMETER_HARNESSES="opencode claude_code git_metrics gh_metrics"
# Repos under TOKOMETER_GIT_ROOT are scanned recursively (TOKOMETER_GIT_DEPTH levels).
export TOKOMETER_GIT_ROOT="$GITROOT"
# Comma-separated author emails to count (your identities); '*' counts everyone.
export TOKOMETER_GIT_AUTHOR="$GITAUTHOR"
export TOKOMETER_GIT_DEPTH="4"
# Claude Code multi-account discovery (defaults shown; usually no need to change):
# export CLAUDE_PROFILES_GLOB="\$HOME/.claude-profiles/*/.claude"
ENVEOF
  echo "wrote $TOKOMETER_HOME/tokometer.env (TOKOMETER_GIT_ROOT=$GITROOT, author=$GITAUTHOR)"
else
  echo "tokometer.env already present; left untouched"
fi

# playwright is OPTIONAL -- only the Cursor dashboard fetch (§3.6) uses it. A failed
# or skipped install must never abort setup. Skip entirely with TOKOMETER_SKIP_PLAYWRIGHT=1.
if [ "${TOKOMETER_SKIP_PLAYWRIGHT:-0}" != "1" ] && ! python3 -c "import playwright" 2>/dev/null; then
  echo "installing playwright (optional; for Cursor harvesting)…"
  if python3 -m pip install --quiet playwright && python3 -m playwright install chromium; then
    echo "playwright ready"
  else
    echo "playwright unavailable -- Cursor fetch disabled (harmless unless you use Cursor)"
  fi
fi

# schedule daily.sh at 04:00 via launchd (macOS-native; cron is blocked by TCC).
if [ "$(uname)" = "Darwin" ]; then
  AGENT_DIR="$HOME/Library/LaunchAgents"
  PLIST="$AGENT_DIR/ai.tokometer.daily.plist"
  mkdir -p "$AGENT_DIR"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>ai.tokometer.daily</string>
    <key>ProgramArguments</key><array><string>$TOKOMETER_HOME/daily.sh</string></array>
    <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>$TOKOMETER_HOME/daily.log</string>
    <key>StandardErrorPath</key><string>$TOKOMETER_HOME/daily.log</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
PLISTEOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST" && echo "scheduled daily.sh at 04:00 (launchd: ai.tokometer.daily)"
fi

echo "done. run: $TOKOMETER_HOME/harvest.sh"
echo "one-time cursor login: python3 $TOKOMETER_HOME/collectors/cursor_fetch.py --login"
