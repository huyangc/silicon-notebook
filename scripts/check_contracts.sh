#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CHECK_LANE_NAME="${CHECK_LANE_NAME:-contracts}"
CHECK_TIMING_FILE="${CHECK_TIMING_FILE:-/dev/stdout}"
START_SECONDS=$SECONDS
trap 'printf "%s=%s\n" "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))" > "$CHECK_TIMING_FILE"' EXIT

"$PYTHON_BIN" -m py_compile \
  "$ROOT_DIR/backend/app/main.py" \
  "$ROOT_DIR/backend/app/api/routes.py" \
  "$ROOT_DIR/backend/app/api/admin_routes.py" \
  "$ROOT_DIR/backend/app/api/ask_routes.py" \
  "$ROOT_DIR/backend/app/core/config.py" \
  "$ROOT_DIR/backend/app/core/llm.py" \
  "$ROOT_DIR/backend/app/models/common.py" \
  "$ROOT_DIR/backend/app/models/identity.py" \
  "$ROOT_DIR/backend/app/models/memory.py" \
  "$ROOT_DIR/backend/app/models/sources.py" \
  "$ROOT_DIR/backend/app/models/notebooks.py" \
  "$ROOT_DIR/backend/app/models/reports.py" \
  "$ROOT_DIR/backend/app/models/ask.py" \
  "$ROOT_DIR/backend/app/models/knowledge.py" \
  "$ROOT_DIR/backend/app/models/kg.py" \
  "$ROOT_DIR/backend/app/models/knowhow.py" \
  "$ROOT_DIR/backend/app/models/command_catalog.py" \
  "$ROOT_DIR/backend/app/models/content_overview.py" \
  "$ROOT_DIR/backend/app/models/admin.py" \
  "$ROOT_DIR/backend/app/models/model_services.py" \
  "$ROOT_DIR/backend/app/models/schemas.py" \
  "$ROOT_DIR/backend/app/services/ask_modes.py" \
  "$ROOT_DIR/backend/app/services/cancellation.py" \
  "$ROOT_DIR/backend/app/services/catalog_job.py" \
  "$ROOT_DIR/backend/app/repositories/sqlite/catalog_store.py" \
  "$ROOT_DIR/backend/app/repositories/postgres/catalog_store.py" \
  "$ROOT_DIR/backend/app/services/extraction_profiles.py" \
  "$ROOT_DIR/backend/app/domain/extraction_profiles.py" \
  "$ROOT_DIR/backend/app/services/kg/extract.py" \
  "$ROOT_DIR/backend/app/services/kg/graph_reason.py" \
  "$ROOT_DIR/backend/app/services/kg/models.py" \
  "$ROOT_DIR/backend/app/services/kg/canonicalize.py" \
  "$ROOT_DIR/backend/app/services/kg_ingest.py" \
  "$ROOT_DIR/backend/app/services/reextract.py" \
  "$ROOT_DIR/backend/app/services/mineru_client.py" \
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
  "$ROOT_DIR/backend/app/api/content_overview_routes.py" \
  "$ROOT_DIR/backend/app/api/kg_routes.py" \
  "$ROOT_DIR/backend/app/api/knowhow_routes.py" \
  "$ROOT_DIR/backend/app/api/knowhow_agent_routes.py" \
  "$ROOT_DIR/backend/app/api/catalog_routes.py" \
  "$ROOT_DIR/backend/app/api/knowledge_routes.py" \
  "$ROOT_DIR/backend/app/api/memory_routes.py" \
  "$ROOT_DIR/backend/app/api/notebook_routes.py" \
  "$ROOT_DIR/backend/app/api/report_routes.py" \
  "$ROOT_DIR/backend/app/api/source_routes.py" \
  "$ROOT_DIR/backend/app/api/system_routes.py" \
  "$ROOT_DIR/backend/app/api/debug_logs.py" \
  "$ROOT_DIR/backend/app/services/auth_utils.py" \
  "$ROOT_DIR/backend/app/domain/auth_utils.py" \
  "$ROOT_DIR/scripts/audit_facade_callers.py"

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" -c \
  "import markdown_it, numpy"

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/smoke_backend.py"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/smoke_memory_mcp.py"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/check_ask_modes_contract.py"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/check_object_type_labels_contract.py"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/check_enumeration_list_labels_contract.py"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/check_ui_vocabulary.py" \
  --extra-root "$ROOT_DIR/examples/extensions/arxiv-search/src"
"$PYTHON_BIN" \
  "$ROOT_DIR/scripts/check_claude_md_budget.py"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/check_architecture_boundaries.py" --root "$ROOT_DIR"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/generate_ui_extension_contract.py" --check

PYTHONPATH="$ROOT_DIR/backend:$ROOT_DIR" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider "$ROOT_DIR/fangan/testcases/harness/tests"
