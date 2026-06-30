#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
# python 解析:优先 $PYTHON_BIN(可执行才用),否则从 PATH 取 python3/python(不写死绝对路径)
PYTHON_BIN="${PYTHON_BIN:-}"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "error: 找不到可用的 python(请设 PYTHON_BIN 或安装 python3)" >&2
  exit 1
fi
exec "$PYTHON_BIN" -m app.scripts.build_kg "$@"
