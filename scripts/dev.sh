#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INHERITED_PYTHONPATH="${PYTHONPATH-}"
DEFAULT_PYTHON="/opt/homebrew/Caskroom/miniconda/base/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
source "$ROOT_DIR/scripts/extension_services.sh"
LAUNCH_MONITOR_POLL_SECONDS=0.2

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]]; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
  extension_services_cleanup || status=1
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Load the repo-root .env so BOTH processes see the same vars. The backend reads
# it via pydantic regardless, but the Next.js frontend only reads frontend/.env*
# — without this, NEXT_PUBLIC_API_BASE_URL set in the root .env never reaches it.
#
# 缺 .env 直接报错退出(ALLOW_NO_ENV_FILE=1 显式跳过)——与 prod.sh 同款预检,
# 详见彼处注释(.env 改名残骸曾演成大库问答假死)。
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090,SC1091
  source "$ROOT_DIR/.env"
  set +a
elif [[ "${ALLOW_NO_ENV_FILE:-0}" == "1" ]]; then
  echo "ALLOW_NO_ENV_FILE=1 — 无 $ROOT_DIR/.env,仅用系统环境变量启动" >&2
else
  echo "错误: 缺 $ROOT_DIR/.env — 后端只读仓库根 .env(Next.js 的「Environments: .env.local」只代表前端)。" >&2
  lookalikes=""
  for f in "$ROOT_DIR"/.env.*; do
    [[ -f "$f" ]] || continue
    base="$(basename "$f")"
    [[ "$base" == ".env.example" ]] && continue
    lookalikes+="$base "
  done
  if [[ -n "$lookalikes" ]]; then
    echo "  发现疑似改名残骸: $lookalikes— 请改回,例如: mv $ROOT_DIR/${lookalikes%% *} $ROOT_DIR/.env" >&2
  else
    echo "  参照 .env.example 创建;纯环境变量部署可设 ALLOW_NO_ENV_FILE=1 跳过本检查。" >&2
  fi
  exit 1
fi

# 按核数自动调参:设 OMP/BLAS 线程(须早于 python 起)。AUTOTUNE=0 关闭。
# shellcheck source=scripts/autotune.sh
source "$ROOT_DIR/scripts/autotune.sh"

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi
extension_services_start

# BACKEND_HOST=0.0.0.0 to expose the API beyond localhost (e.g. server deploys).
# Paths (db/storage/env_file) are anchored to the repo root in code (Settings), not to
# the launch directory — the `cd` below is only so Python resolves the `app` package.
cd "$ROOT_DIR/backend"
PYTHONPATH="$INHERITED_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "$ROOT_DIR/scripts/python_env.py" \
  -m uvicorn app.main:app --host "${BACKEND_HOST:-127.0.0.1}" --port 8000 &
BACKEND_PID=$!

cd "$ROOT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep "$LAUNCH_MONITOR_POLL_SECONDS"
done
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  wait "$BACKEND_PID"
else
  wait "$FRONTEND_PID"
fi
