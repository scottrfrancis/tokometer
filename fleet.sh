#!/bin/bash
# Build the fleet report on the hub (cron 40 4 * * *, after every machine's
# 04:00 harvest and push). See fleet.py. Config: $TOKOMETER_HOME/fleet.env,
# plain KEY=VALUE lines (FLEET_PG_*, FLEET_SINCE, FLEET_HOSTS, and FLEET_PYTHON
# for an interpreter with psycopg2; FLEET_TZ pins the report's time zone).
set -uo pipefail
TOKOMETER_HOME="${TOKOMETER_HOME:-$HOME/.tokometer}"
ENV_FILE="$TOKOMETER_HOME/fleet.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "[$(date)] fleet: no $ENV_FILE; skipping"
  exit 0
fi
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
[ -n "${FLEET_TZ:-}" ] && export TZ="$FLEET_TZ"
PY="${FLEET_PYTHON:-python3}"

echo "===== tokometer fleet $(date) ====="
if out="$("$PY" "$TOKOMETER_HOME/fleet.py" 2>&1)"; then
  echo "[$(date)] fleet ok -> $out"
else
  echo "[$(date)] fleet FAILED"
  printf '%s\n' "$out" | tail -n 20
  exit 1
fi
