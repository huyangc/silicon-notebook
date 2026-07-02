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
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090,SC1091
  source "$ROOT_DIR/.env"
  set +a
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
