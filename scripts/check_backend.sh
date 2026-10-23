#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-16}"
CHECK_LANE_NAME="${CHECK_LANE_NAME:-backend}"
CHECK_TIMING_FILE="${CHECK_TIMING_FILE:-/dev/stdout}"
START_SECONDS=$SECONDS
record_timing() {
  if [[ "$CHECK_TIMING_FILE" == /dev/stdout ]]; then
    printf '%s=%s\n' "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))"
  else
    printf '%s=%s\n' "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))" > "$CHECK_TIMING_FILE"
  fi
}
trap record_timing EXIT

if (( $# )); then
  printf 'check_backend.sh takes no arguments (got: %s)\n' "$*" >&2
  exit 2
fi

# Match check.sh when CI runs this lane on its own runner.
export SILICON_NOTEBOOK_ENV_FILE="" MODEL_SERVICES_CONFIG="" EXTENSIONS_CONFIG=""
export MINERU_MODE="off" MINERU_API_TOKEN=""
mkdir -p "$ROOT_DIR/.local/pycache" "$ROOT_DIR/backend/.local"
export PYTHONPYCACHEPREFIX="$ROOT_DIR/.local/pycache"
# Bytecode goes to the gitignored prefix above, so write and reuse it even
# when the calling shell exports PYTHONDONTWRITEBYTECODE=1 (see check.sh).
unset PYTHONDONTWRITEBYTECODE

# architecture_contract itself is not excluded: structural item B7 measured
# per-test cost (`pytest -m architecture_contract --durations=0 -n0`) and
# found only 8 of the 64 tests cost >2s. Those 8 alone carry
# architecture_contract_heavy (see conftest._ARCHITECTURE_CONTRACT_HEAVY_TESTS)
# and stay excluded here; the remaining 56 cheap architecture_contract tests
# now run on every PR/push instead of waiting for the daily G2 lane.
# process_contract (tests that start a real child process) and
# legacy_upgrade_contract (old-version upgrade matrix) are G2-only by the
# 2026-10-14 ruling that set the local warm-gate target to 80 s: those
# contracts are reported by the daily gate, not on every PR.
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider -p tests.ci_metrics -n "$BACKEND_PYTEST_WORKERS" \
  -m "not slow and not architecture_contract_heavy and not graph_index_contract and not process_contract and not legacy_upgrade_contract" \
  --ignore="$ROOT_DIR/backend/tests/postgres" \
  --durations=30 --junitxml="$ROOT_DIR/backend/.local/backend-junit.xml" \
  --ci-metrics="$ROOT_DIR/backend/.local/backend-metrics.json" \
  "$ROOT_DIR/backend/tests"
