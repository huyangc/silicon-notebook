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

"$PYTHON_BIN" -m py_compile \
  "$ROOT_DIR/backend/app/main.py" \
  "$ROOT_DIR/backend/app/api/routes.py" \
  "$ROOT_DIR/backend/app/core/config.py" \
  "$ROOT_DIR/backend/app/core/llm.py" \
  "$ROOT_DIR/backend/app/models/schemas.py" \
  "$ROOT_DIR/backend/app/services/ask_modes.py" \
  "$ROOT_DIR/backend/app/services/cancellation.py" \
  "$ROOT_DIR/backend/app/services/extraction_profiles.py" \
  "$ROOT_DIR/backend/app/services/kg/extract.py" \
  "$ROOT_DIR/backend/app/services/kg/graph_reason.py" \
  "$ROOT_DIR/backend/app/services/kg/models.py" \
  "$ROOT_DIR/backend/app/services/kg/canonicalize.py" \
  "$ROOT_DIR/backend/app/services/kg_ingest.py" \
  "$ROOT_DIR/backend/app/services/reextract.py" \
  "$ROOT_DIR/backend/app/services/mineru_client.py" \
  "$ROOT_DIR/backend/app/services/notebook_templates.py" \
  "$ROOT_DIR/backend/app/services/parsers.py" \
  "$ROOT_DIR/backend/app/services/prompts.py" \
  "$ROOT_DIR/backend/app/services/query_rewrite.py" \
  "$ROOT_DIR/backend/app/services/reasoning_retrieval.py" \
  "$ROOT_DIR/backend/app/services/remote_sources.py" \
  "$ROOT_DIR/backend/app/services/batch_ingest.py" \
  "$ROOT_DIR/backend/app/services/kg/scheduler.py" \
  "$ROOT_DIR/backend/app/services/repository.py" \
  "$ROOT_DIR/backend/app/services/retrieval.py" \
  "$ROOT_DIR/backend/app/services/sqlite_identity.py" \
  "$ROOT_DIR/backend/app/services/sqlite_notebook_sharing.py" \
  "$ROOT_DIR/backend/app/services/sqlite_repository.py" \
  "$ROOT_DIR/backend/app/api/deps.py" \
  "$ROOT_DIR/backend/app/api/auth_routes.py" \
  "$ROOT_DIR/backend/app/services/auth_utils.py"

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" - <<'PY'
import markdown_it  # noqa: F401
import numpy  # noqa: F401
PY

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" "$ROOT_DIR/scripts/smoke_backend.py"

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" "$ROOT_DIR/scripts/check_ask_modes_contract.py"

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" -m pytest -p no:cacheprovider "$ROOT_DIR/backend/tests"

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi

cd "$ROOT_DIR/frontend"
npm run test
npm run lint
npm run build
