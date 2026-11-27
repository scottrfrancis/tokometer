#!/bin/bash
# Copy the hub's fleet report and open it (macOS LaunchAgent ai.tokometer.fleet-open,
# 05:05; launchd runs it on wake if the Mac was asleep). If today's report is not
# on the hub, open this machine's own report with a banner saying why.
# Config: FLEET_HUB (ssh host) in $TOKOMETER_HOME/fleet.env; FLEET_HUB_REPORTS is
# the reports dir on the hub, relative to its home (default .tokometer/reports).
set -uo pipefail
TOKOMETER_HOME="${TOKOMETER_HOME:-$HOME/.tokometer}"
ENV_FILE="$TOKOMETER_HOME/fleet.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
if [ -z "${FLEET_HUB:-}" ]; then
  echo "[$(date)] fleet-open: FLEET_HUB not set; nothing to open"
  exit 0
fi

REPORTS="$TOKOMETER_HOME/reports"
REMOTE="${FLEET_HUB_REPORTS:-.tokometer/reports}"
OPEN_CMD="${TOKOMETER_OPEN:-open}"
mkdir -p "$REPORTS"
today="$(date +%F)"
work="$(mktemp -d "${TMPDIR:-/tmp}/fleet-open.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT

fetch() {
  scp -q -o BatchMode=yes -o ConnectTimeout=20 "$FLEET_HUB:$REMOTE/$1" "$work/$1"
}

reason=""
if fetch fleet-summary.json && fetch fleet-latest.html; then
  day="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("date", ""))' \
        "$work/fleet-summary.json" 2>/dev/null)"
  if [ "$day" = "$today" ]; then
    page="$REPORTS/fleet-$today.html"
    mv "$work/fleet-latest.html" "$page"
    echo "[$(date)] fleet-open: fetched $page"
  else
    reason="The fleet report on $FLEET_HUB is stale (from ${day:-an unknown day}), so this is this machine's report only."
  fi
else
  reason="fleet-open could not copy the fleet report from $FLEET_HUB, so this is this machine's report only."
fi

if [ -n "$reason" ]; then
  echo "[$(date)] fleet-open: $reason"
  page="$REPORTS/fleet-unavailable-$today.html"
  python3 - "$REPORTS/morning-$today.html" "$page" "$reason" <<'PY'
import html, sys
from pathlib import Path
local, out, reason = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
banner = ('<div style="background:#fff4e5;border:1px solid #f59f00;border-radius:6px;'
          f'padding:8px 12px;margin:10px 0">{html.escape(reason)}</div>')
page = local.read_text() if local.is_file() else "<html><body></body></html>"
out.write_text(page.replace("<body>", "<body>" + banner, 1))
PY
fi

if "$OPEN_CMD" "$page"; then
  echo "[$(date)] fleet-open: opened $page"
else
  echo "[$(date)] fleet-open: open FAILED for $page"
  exit 1
fi
