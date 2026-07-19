#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Verification must not inherit a developer's repo-root .env or accidentally
# call paid/network model services. Tests opt into any provider explicitly.
export SILICON_NOTEBOOK_ENV_FILE=""
export OPENAI_COMPAT_BASE_URL="" OPENAI_COMPAT_API_KEY="" OPENAI_COMPAT_MODEL=""
export REASONING_LLM_BASE_URL="" REASONING_LLM_API_KEY="" REASONING_LLM_MODEL=""
export REWRITE_LLM_BASE_URL="" REWRITE_LLM_API_KEY="" REWRITE_LLM_MODEL=""
export KG_LLM_BASE_URL="" KG_LLM_API_KEY="" KG_LLM_MODEL=""
export EMBED_PROVIDER="" EMBED_BASE_URL="" EMBED_API_KEY="" EMBED_MODEL=""
export RERANK_MODEL="" RERANK_API_KEY=""
export MINERU_MODE="off" MINERU_API_TOKEN=""

mkdir -p "$ROOT_DIR/.local/pycache"
export PYTHONPYCACHEPREFIX="$ROOT_DIR/.local/pycache"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/silicon-check.XXXXXX")"
declare -a PIDS=()
declare -a LANES=(contracts backend frontend)

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

for lane in "${LANES[@]}"; do
  CHECK_LANE_NAME="$lane" \
  CHECK_TIMING_FILE="$TMP_DIR/$lane.time" \
  ROOT_DIR="$ROOT_DIR" \
  PYTHON_BIN="$PYTHON_BIN" \
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
