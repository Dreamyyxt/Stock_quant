#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON="$PYTHON_BIN"
elif [[ -x "$PROJECT/.venv/bin/python" ]]; then
  PYTHON="$PROJECT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

LOGDIR="$PROJECT/logs"
DATE=$(date +"%Y%m%d")

# Ensure log directory exists
mkdir -p "$LOGDIR"

cd "$PROJECT" || exit 1

MODE="${1:-live}"

case "$MODE" in
  dry)
    echo "[$(date)] Running dry rebalance..." | tee -a "$LOGDIR/launcher_$DATE.log"
    "$PYTHON" main.py --mode dry 2>&1 | tee -a "$LOGDIR/dry_$DATE.log"
    ;;
  status)
    "$PYTHON" main.py --mode status 2>&1
    ;;
  rebalance)
    echo "[$(date)] Running live rebalance..." | tee -a "$LOGDIR/launcher_$DATE.log"
    "$PYTHON" main.py --mode rebalance 2>&1 | tee -a "$LOGDIR/rebalance_$DATE.log"
    ;;
  monitor)
    echo "[$(date)] Running monitor..." | tee -a "$LOGDIR/launcher_$DATE.log"
    "$PYTHON" main.py --mode monitor 2>&1 | tee -a "$LOGDIR/monitor_$DATE.log"
    ;;
  live|*)
    echo "[$(date)] Starting live scheduler..." | tee -a "$LOGDIR/launcher_$DATE.log"
    exec "$PYTHON" main.py --mode live 2>&1 | tee -a "$LOGDIR/scheduler_$DATE.log"
    ;;
esac
