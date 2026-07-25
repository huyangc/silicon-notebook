#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Preserve repo-relative interpreter paths after the script changes into backend/.
if [[ "$PYTHON_BIN" != /* && -x "$ROOT_DIR/$PYTHON_BIN" ]]; then
  PYTHON_BIN="$ROOT_DIR/$PYTHON_BIN"
fi

: "${TEST_POSTGRES_URL:?TEST_POSTGRES_URL is required}"

if [[ "${POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED:-0}" == "1" ]]; then
  : "${TEST_POSTGRES_NON_C_URL:?TEST_POSTGRES_NON_C_URL is required in the authoritative CI lane}"
  : "${TEST_POSTGRES_NON_UTF_URL:?TEST_POSTGRES_NON_UTF_URL is required in the authoritative CI lane}"
fi

# The PostgreSQL lane is still network-offline with respect to model providers.
# It talks only to the explicitly supplied disposable database targets.
export SILICON_NOTEBOOK_ENV_FILE=""
export OPENAI_COMPAT_BASE_URL="" OPENAI_COMPAT_API_KEY="" OPENAI_COMPAT_MODEL=""
export REASONING_LLM_BASE_URL="" REASONING_LLM_API_KEY="" REASONING_LLM_MODEL=""
export REWRITE_LLM_BASE_URL="" REWRITE_LLM_API_KEY="" REWRITE_LLM_MODEL=""
export KG_LLM_BASE_URL="" KG_LLM_API_KEY="" KG_LLM_MODEL=""
export EMBED_PROVIDER="" EMBED_BASE_URL="" EMBED_API_KEY="" EMBED_MODEL=""
export RERANK_MODEL="" RERANK_API_KEY=""
export MINERU_MODE="off" MINERU_API_TOKEN=""

# The Python launcher owns URL parsing, database_status/redact_database_url
# diagnostics, password-free pytest URLs, and the `-m postgres_integration`
# selection. It runs adapter/conformance tests first, then the complete shadow
# E2E alone and serially. Keeping conninfo parsing out of shell prevents
# accidental echoing.
cd "$ROOT_DIR/backend"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" -m tests.postgres.lane
