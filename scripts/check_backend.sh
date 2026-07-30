#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-12}"
CHECK_LANE_NAME="${CHECK_LANE_NAME:-backend}"
CHECK_TIMING_FILE="${CHECK_TIMING_FILE:-/dev/stdout}"
START_SECONDS=$SECONDS
trap 'printf "%s=%s\n" "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))" > "$CHECK_TIMING_FILE"' EXIT

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider -n "$BACKEND_PYTEST_WORKERS" \
  -m "not slow and not architecture_contract" \
  --ignore="$ROOT_DIR/backend/tests/postgres" \
  "$ROOT_DIR/backend/tests"
