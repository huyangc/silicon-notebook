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

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/silicon-check.XXXXXX")"
declare -a PIDS=()
declare -a LANES=(contracts backend frontend)

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

exit "$status"
