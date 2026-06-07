#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PYTHON="/opt/homebrew/Caskroom/miniconda/base/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

mkdir -p "$ROOT_DIR/.local/pycache"
export PYTHONPYCACHEPREFIX="$ROOT_DIR/.local/pycache"

"$PYTHON_BIN" -m py_compile \
  "$ROOT_DIR/backend/app/main.py" \
  "$ROOT_DIR/backend/app/api/routes.py" \
  "$ROOT_DIR/backend/app/core/config.py" \
  "$ROOT_DIR/backend/app/core/llm.py" \
  "$ROOT_DIR/backend/app/models/schemas.py" \
  "$ROOT_DIR/backend/app/services/extraction_profiles.py" \
  "$ROOT_DIR/backend/app/services/kg/extract.py" \
  "$ROOT_DIR/backend/app/services/kg/models.py" \
  "$ROOT_DIR/backend/app/services/kg/canonicalize.py" \
  "$ROOT_DIR/backend/app/services/kg_ingest.py" \
  "$ROOT_DIR/backend/app/services/reextract.py" \
  "$ROOT_DIR/backend/app/services/mineru_client.py" \
  "$ROOT_DIR/backend/app/services/notebook_templates.py" \
  "$ROOT_DIR/backend/app/services/parsers.py" \
  "$ROOT_DIR/backend/app/services/prompts.py" \
  "$ROOT_DIR/backend/app/services/repository.py" \
  "$ROOT_DIR/backend/app/services/retrieval.py" \
  "$ROOT_DIR/backend/app/services/sqlite_repository.py"

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" - <<'PY'
import markdown_it  # noqa: F401
import numpy  # noqa: F401
PY

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" "$ROOT_DIR/scripts/smoke_backend.py"

if [[ -d "$ROOT_DIR/frontend/node_modules" ]]; then
  cd "$ROOT_DIR/frontend"
  npm run test
  npm run lint
else
  echo "frontend/node_modules not found; skipping frontend lint"
fi
