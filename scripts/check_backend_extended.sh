#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_EXTENDED_PYTEST_WORKERS="${BACKEND_EXTENDED_PYTEST_WORKERS:-${BACKEND_PYTEST_WORKERS:-4}}"

# Complement check_backend.sh exactly: expensive real-index/performance tests,
# cold graph/index contracts, and repository-wide semantic source scans remain
# available, but they do not compete with every edit-time verification run.
# PostgreSQL has its own authoritative check_postgres.sh lane.
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider -n "$BACKEND_EXTENDED_PYTEST_WORKERS" \
  -m "slow or architecture_contract or graph_index_contract" \
  --ignore="$ROOT_DIR/backend/tests/postgres" \
  "$ROOT_DIR/backend/tests"
