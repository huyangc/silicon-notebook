#!/usr/bin/env bash
# Preserve the caller's working directory and arguments, including relative paths.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${PYTHON_BIN+x}" ]]; then
  PYTHON="$PYTHON_BIN"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

if ! command -v -- "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 Python 解释器；请将 PYTHON_BIN 设置为可执行的 Python 路径。" >&2
  exit 127
fi
exec "$PYTHON" "$ROOT/scripts/cli.py" "$@"
