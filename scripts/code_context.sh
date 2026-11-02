#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" && -z "${PYTHON_BIN+x}" ]]; then PYTHON=python3; fi
exec "$PYTHON" "$ROOT/scripts/code_context.py" "$@"
