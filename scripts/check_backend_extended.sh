#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_EXTENDED_PYTEST_WORKERS="${BACKEND_EXTENDED_PYTEST_WORKERS:-${BACKEND_PYTEST_WORKERS:-4}}"

# Complement check_backend.sh exactly: expensive real-index/performance tests,
# cold graph/index contracts, and the >2s architecture_contract_heavy subset of
# repository-wide semantic source scans remain available, but they do not
# compete with every edit-time verification run. The other 56
# architecture_contract tests (<=2s each) already ran in check_backend.sh, so
# this deliberately says architecture_contract_heavy, not architecture_contract
# — see conftest._ARCHITECTURE_CONTRACT_HEAVY_TESTS for exactly which 8.
# It also runs every process_contract test (a real child process) and every
# legacy_upgrade_contract test (old-version upgrade matrix), which G1 leaves
# out to keep the local warm gate within its 100 s target.
# PostgreSQL has its own authoritative check_postgres.sh lane.

# Same offline isolation as check.sh and check_backend.sh. check_extended.sh
# runs this after check.sh, whose exports end with its own process, and the
# daily workflow runs it on a runner of its own, so this lane sets them itself.
export SILICON_NOTEBOOK_ENV_FILE="" MODEL_SERVICES_CONFIG="" EXTENSIONS_CONFIG=""
export MINERU_MODE="off" MINERU_API_TOKEN=""
mkdir -p "$ROOT_DIR/.local/pycache" "$ROOT_DIR/backend/.local"
export PYTHONPYCACHEPREFIX="$ROOT_DIR/.local/pycache"
# Bytecode goes to the gitignored prefix above, so write and reuse it even
# when the calling shell exports PYTHONDONTWRITEBYTECODE=1 (see check.sh).
unset PYTHONDONTWRITEBYTECODE

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider -p tests.ci_metrics -n "$BACKEND_EXTENDED_PYTEST_WORKERS" \
  -m "slow or architecture_contract_heavy or graph_index_contract or process_contract or legacy_upgrade_contract" \
  --ignore="$ROOT_DIR/backend/tests/postgres" \
  --durations=30 --junitxml="$ROOT_DIR/backend/.local/backend-extended-junit.xml" \
  --ci-metrics="$ROOT_DIR/backend/.local/backend-extended-metrics.json" \
  "$ROOT_DIR/backend/tests"
