#!/usr/bin/env bash
# 从新解压的离线包运行；目标始终是原安装目录。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON_BIN:-python3}" "$SCRIPT_DIR/upgrade.py" "$@"
