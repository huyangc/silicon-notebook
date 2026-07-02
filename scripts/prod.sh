#!/usr/bin/env bash
# silicon-notebook 生产启动:前端 build + start,后端单进程 uvicorn。
#
#   npm run start          等价调用本脚本
#   SKIP_BUILD=1 npm run start   跳过 `next build`(镜像/CI 已预构建时用)
#
# 环境变量:PYTHON_BIN BACKEND_HOST PORT FRONTEND_PORT SKIP_BUILD
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PYTHON="/opt/homebrew/Caskroom/miniconda/base/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]]; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT

# Load the repo-root .env so BOTH processes see the same vars. The backend reads
# it via pydantic regardless, but the Next.js frontend only reads frontend/.env*
# — without this, NEXT_PUBLIC_API_BASE_URL set in the root .env never reaches it.
#
# 缺 .env 直接报错退出(ALLOW_NO_ENV_FILE=1 显式跳过):pydantic 对缺失的
# env_file 静默降级成默认空配置,曾把「.env 改名成 .env.local」演成 embed 未
# 配置 → 大库问答半小时假死;Next.js 打印的「Environments: .env.local」只代表
# 前端自己,极具误导性,故在这里最早出声。
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

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi

LOG_DIR="$ROOT_DIR/.local/logs"
mkdir -p "$LOG_DIR"
FRONTEND_LOG="$LOG_DIR/frontend.log"
BACKEND_LOG="$LOG_DIR/backend.log"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  ( cd "$ROOT_DIR/frontend" && npm run build )
else
  echo "SKIP_BUILD=1 — skipping 'npm run build' (expecting a prebuilt frontend/.next)"
fi

# BACKEND_HOST=0.0.0.0 to expose the API beyond localhost (e.g. server deploys).
# --workers 1: process-internal caches and dedup sets (e.g. VectorCache, extraction
# pools) are per-process and NOT shared across workers — N workers would mean N×
# memory and N independent (inconsistent) caches, not more throughput.
cd "$ROOT_DIR/backend"
"$PYTHON_BIN" -m uvicorn app.main:app \
  --host "${BACKEND_HOST:-0.0.0.0}" \
  --port "${PORT:-8000}" \
  --workers 1 \
  >>"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

cd "$ROOT_DIR/frontend"
npm run start -- -p "${FRONTEND_PORT:-3000}" >>"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

echo "backend  : http://${BACKEND_HOST:-0.0.0.0}:${PORT:-8000}   (PID $BACKEND_PID, log $BACKEND_LOG)"
echo "frontend : http://0.0.0.0:${FRONTEND_PORT:-3000}   (PID $FRONTEND_PID, log $FRONTEND_LOG)"
echo "(the backend's first log line prints the resolved absolute db/storage/log paths — check it if unsure which .local a launch is using)"

# Portable "wait for either PID to exit" — `wait -n` needs bash>=4.3, but macOS
# ships bash 3.2, so poll instead. Whichever process exits first ends the loop;
# the EXIT trap then kills the other and this exits non-zero.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done

if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo "backend process exited; shutting down frontend" >&2
else
  echo "frontend process exited; shutting down backend" >&2
fi
exit 1
