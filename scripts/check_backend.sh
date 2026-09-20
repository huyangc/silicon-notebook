#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-12}"
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

# Explicit arguments stay on this pytest invocation. Exported shard settings
# would incorrectly narrow collect-only/sub-pytest contracts launched by tests.
SHARD_INDEX=""
SHARD_COUNT=""
while (( $# )); do
  case "$1" in
    --shard-index|--shard-count)
      option="$1"
      if (( $# < 2 )) || [[ ! "$2" =~ ^(0|[1-9][0-9]*)$ ]]; then
        printf 'Expected an integer after %s\n' "$option" >&2
        exit 2
      fi
      if [[ "$option" == --shard-index && -z "$SHARD_INDEX" ]]; then
        SHARD_INDEX="$2"
      elif [[ "$option" == --shard-count && -z "$SHARD_COUNT" ]]; then
        SHARD_COUNT="$2"
      else
        printf 'Duplicate option: %s\n' "$option" >&2
        exit 2
      fi
      shift 2
      ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -n "$SHARD_INDEX" && -z "$SHARD_COUNT" || -z "$SHARD_INDEX" && -n "$SHARD_COUNT" ]]; then
  printf '%s\n' '--shard-index and --shard-count must be supplied together' >&2
  exit 2
fi
set --
JUNIT_NAME=backend-junit.xml
if [[ -n "$SHARD_INDEX" ]]; then
  set -- -p tests.architecture.g1_sharding --g1-shard-index "$SHARD_INDEX" --g1-shard-count "$SHARD_COUNT"
  JUNIT_NAME="backend-junit-shard-$SHARD_INDEX.xml"
fi

# Match check.sh when CI runs this lane on its own runner.
export SILICON_NOTEBOOK_ENV_FILE="" MODEL_SERVICES_CONFIG="" EXTENSIONS_CONFIG=""
export MINERU_MODE="off" MINERU_API_TOKEN=""
mkdir -p "$ROOT_DIR/.local/pycache" "$ROOT_DIR/backend/.local"
export PYTHONPYCACHEPREFIX="$ROOT_DIR/.local/pycache"

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
  --durations=30 --junitxml="$ROOT_DIR/backend/.local/$JUNIT_NAME" \
  "$@" \
  "$ROOT_DIR/backend/tests"
