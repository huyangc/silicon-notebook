#!/usr/bin/env bash
# Run-specific local supervisor for the foreground shadow worker.
set -euo pipefail

umask 077
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  echo "usage: scripts/shadow.sh {start|status|stop|restart} RUN_ID [WORK_DIR]" >&2
  exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
ACTION="$1"
RUN_ID="$2"
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "invalid shadow run id" >&2
  exit 2
}
WORK_DIR="${3:-$ROOT_DIR/.local/shadow/$RUN_ID}"
mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"
[[ ! -L "$WORK_DIR" && -d "$WORK_DIR" ]] || {
  echo "shadow work directory must be a real directory" >&2
  exit 2
}

PID_FILE="$WORK_DIR/worker.pid"
START_FILE="$WORK_DIR/worker.start"
LOG_FILE="$WORK_DIR/worker.log"
TOKEN_FILE="$WORK_DIR/worker.confirmation"
ENTRY="$ROOT_DIR/scripts/migrate_sqlite_to_postgres.py"

process_identity() {
  local pid="$1"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local command start expected_start
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  start="$(ps -p "$pid" -o lstart= 2>/dev/null || true)"
  expected_start="$(<"$START_FILE")"
  [[ -n "$start" && "$start" == "$expected_start" ]] || return 1
  [[ "$command" == *"$ENTRY worker --run-id $RUN_ID "* ]] || return 1
  return 0
}

worker_pid() {
  [[ -f "$PID_FILE" && -f "$START_FILE" ]] || return 1
  local pid
  pid="$(<"$PID_FILE")"
  process_identity "$pid" || return 1
  printf '%s' "$pid"
}

start_worker() {
  if pid="$(worker_pid)"; then
    echo "shadow worker already running (PID $pid)"
    return 0
  fi
  [[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || {
    echo "missing private worker confirmation artifact: $TOKEN_FILE" >&2
    exit 2
  }
  chmod 600 "$TOKEN_FILE"
  : >"$LOG_FILE"
  chmod 600 "$LOG_FILE"
  PYTHONPATH="$ROOT_DIR/backend" nohup "$PYTHON_BIN" "$ENTRY" worker \
    --run-id "$RUN_ID" \
    --direction forward \
    --work-dir "$WORK_DIR" \
    --confirmation-token-file "$TOKEN_FILE" \
    >>"$LOG_FILE" 2>&1 &
  local pid="$!"
  printf '%s\n' "$pid" >"$PID_FILE"
  chmod 600 "$PID_FILE"
  local attempts=0 start=""
  while [[ $attempts -lt 20 ]]; do
    start="$(ps -p "$pid" -o lstart= 2>/dev/null || true)"
    [[ -n "$start" ]] && break
    sleep 0.1
    attempts=$((attempts + 1))
  done
  [[ -n "$start" ]] || {
    echo "shadow worker failed to start; see $LOG_FILE" >&2
    exit 1
  }
  printf '%s\n' "$start" >"$START_FILE"
  chmod 600 "$START_FILE"
  sleep 0.2
  process_identity "$pid" || {
    echo "shadow worker exited during startup; see $LOG_FILE" >&2
    exit 1
  }
  echo "shadow worker started (PID $pid, log $LOG_FILE)"
}

stop_worker() {
  local pid
  if ! pid="$(worker_pid)"; then
    echo "shadow worker is not running"
    return 0
  fi
  kill -TERM "$pid"
  local attempts=0
  while process_identity "$pid" && [[ $attempts -lt 300 ]]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  if process_identity "$pid"; then
    echo "shadow worker did not stop after 30 seconds; refusing broad or forced kill" >&2
    exit 1
  fi
  echo "shadow worker stopped (PID $pid)"
}

case "$ACTION" in
  start) start_worker ;;
  status)
    if pid="$(worker_pid)"; then
      echo "shadow worker running (PID $pid, log $LOG_FILE)"
    else
      echo "shadow worker not running"
      exit 1
    fi
    ;;
  stop) stop_worker ;;
  restart) stop_worker; start_worker ;;
  *) usage ;;
esac
