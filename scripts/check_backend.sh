#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-12}"
CHECK_LANE_NAME="${CHECK_LANE_NAME:-backend}"
CHECK_TIMING_FILE="${CHECK_TIMING_FILE:-/dev/stdout}"
START_SECONDS=$SECONDS
trap 'printf "%s=%s\n" "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))" > "$CHECK_TIMING_FILE"' EXIT

# architecture_contract itself is not excluded: structural item B7 measured
# per-test cost (`pytest -m architecture_contract --durations=0 -n0`) and
# found only 8 of the 64 tests cost >2s. Those 8 alone carry
# architecture_contract_heavy (see conftest._ARCHITECTURE_CONTRACT_HEAVY_TESTS)
# and stay excluded here; the remaining 56 cheap architecture_contract tests
# now run on every PR/push instead of waiting for the daily G2 lane.
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider -n "$BACKEND_PYTEST_WORKERS" \
  -m "not slow and not architecture_contract_heavy and not graph_index_contract" \
  --ignore="$ROOT_DIR/backend/tests/postgres" \
  "$ROOT_DIR/backend/tests"
