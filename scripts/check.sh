#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Verification must not inherit a developer's repo-root .env or accidentally
# call paid/network model services. Tests opt into any provider explicitly.
export SILICON_NOTEBOOK_ENV_FILE=""
export MODEL_SERVICES_CONFIG=""
export EXTENSIONS_CONFIG=""   # 验证绝不装入部署插件（否则会漏进冻结的 app.openapi()）
export MINERU_MODE="off" MINERU_API_TOKEN=""

mkdir -p "$ROOT_DIR/.local/pycache"
export PYTHONPYCACHEPREFIX="$ROOT_DIR/.local/pycache"
# Bytecode, including pytest's rewritten test modules, goes to the gitignored
# prefix above, never into the source tree. So the gate writes and reuses it
# even when the calling shell exports PYTHONDONTWRITEBYTECODE=1 (agent
# sessions do): without a cache every pytest worker recompiles and rewrites
# every module it imports before its first test.
unset PYTHONDONTWRITEBYTECODE

declare -a LANES=(contracts backend frontend)

# With no arguments check.sh is all of G1. `--lane <name>` (repeatable) runs
# only the named lanes, still concurrently, in LANES order: CI runs its two
# backend parts explicitly, then runs the other two together with
# `--lane contracts --lane frontend`.
declare -a SELECTED_LANES=()
while (( $# )); do
  case "$1" in
    --lane)
      if (( $# < 2 )) || [[ " ${LANES[*]} " != *" $2 "* ]]; then
        printf 'Expected one of [%s] after --lane\n' "${LANES[*]}" >&2
        exit 2
      fi
      SELECTED_LANES+=("$2")
      shift 2
      ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if (( ${#SELECTED_LANES[@]} )); then
  declare -a RUN_LANES=()
  for lane in "${LANES[@]}"; do
    if [[ " ${SELECTED_LANES[*]} " == *" $lane "* ]]; then
      RUN_LANES+=("$lane")
    fi
  done
  LANES=("${RUN_LANES[@]}")
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/silicon-check.XXXXXX")"
GATE_START_SECONDS=$SECONDS
GATE_STARTED_AT="$(date +%s)"
declare -a PIDS=()

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    kill -TERM -- "-$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$TMP_DIR"
  exit "$status"
}

handle_interrupt() {
  exit 130
}

handle_terminate() {
  exit 143
}

trap cleanup EXIT
trap handle_interrupt INT
trap handle_terminate TERM

for lane in "${LANES[@]}"; do
  CHECK_LANE_NAME="$lane" \
  CHECK_TIMING_FILE="$TMP_DIR/$lane.time" \
  ROOT_DIR="$ROOT_DIR" \
  PYTHON_BIN="$PYTHON_BIN" \
    "$PYTHON_BIN" -c \
      'import os, sys; os.setpgrp(); os.execv(sys.argv[1], sys.argv[1:])' \
      "$ROOT_DIR/scripts/check_${lane}.sh" \
    >"$TMP_DIR/$lane.log" 2>&1 &
  PIDS+=("$!")
done

status=0
for index in "${!LANES[@]}"; do
  if ! wait "${PIDS[$index]}"; then
    status=1
  fi
done

for lane in "${LANES[@]}"; do
  printf "\n===== %s =====\n" "$lane"
  cat "$TMP_DIR/$lane.log"
  if [[ -f "$TMP_DIR/$lane.time" ]]; then
    cat "$TMP_DIR/$lane.time"
  else
    printf "%s=missing\n" "$lane"
  fi
done

# Wall time, lane seconds and the slowest backend files/tests from this run's
# junit; a prominent warning over the 100 s local target. Observational only:
# it never changes the gate's exit status.
"$PYTHON_BIN" "$ROOT_DIR/scripts/check_timing_summary.py" \
  --wall "$((SECONDS - GATE_START_SECONDS))" \
  --timing-dir "$TMP_DIR" \
  --junit "$ROOT_DIR/backend/.local/backend-junit.xml" \
  --started-at "$GATE_STARTED_AT" \
  --target 100 || true

exit "$status"
