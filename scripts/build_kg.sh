#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python}"
exec "$PYTHON_BIN" -m app.scripts.build_kg "$@"
