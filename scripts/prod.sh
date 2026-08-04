#!/usr/bin/env bash
# silicon-notebook 生产启动:前端 build + start,后端单进程 uvicorn。
#
#   npm run start          后台启动两个服务后立即退出
#   SKIP_INSTALL=1 npm run start 跳过前后端依赖安装(镜像已预装时用)
#   SKIP_BUILD=1 npm run start   跳过 `next build`(镜像/CI 已预构建时用)
#
# 环境变量:PYTHON_BIN BACKEND_HOST PORT FRONTEND_PORT SKIP_INSTALL SKIP_BUILD
#              START_CLEANUP_GRACE_SECONDS(默认 10,启动脚本中断时的 SIGTERM 宽限)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cleanup() {
  local status=$? pid alive deadline polls=0
  trap - EXIT INT TERM HUP

  if [[ -z "${BACKEND_PID:-}" && -z "${FRONTEND_PID:-}" ]]; then
    exit "$status"
  fi

  # Both services are direct children (the launch subshell execs into the real
  # process). Signal them together so one slow shutdown cannot consume a full
  # grace window before the other even receives SIGTERM.
  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done

  deadline=$((SECONDS + ${START_CLEANUP_GRACE_SECONDS:-10}))
  while true; do
    alive=0
    for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
      [[ -n "$pid" ]] || continue
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    [[ "$alive" == "0" || "$SECONDS" -ge "$deadline" ]] && break
    if [[ -n "${_SCRIPT_TEST_CLEANUP_MAX_POLLS:-}" \
      && "$polls" -ge "${_SCRIPT_TEST_CLEANUP_MAX_POLLS}" ]]; then
      break
    fi
    sleep "${_SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS:-1}"
    polls=$((polls + 1))
  done

  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      echo "startup child PID $pid did not stop after SIGTERM; sending SIGKILL" >&2
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] || continue
    wait "$pid" 2>/dev/null || true
  done
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

# 按核数自动调参:设 OMP/BLAS 线程(须早于 python 起)。AUTOTUNE=0 关闭。
# shellcheck source=scripts/autotune.sh
source "$ROOT_DIR/scripts/autotune.sh"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
START_CLEANUP_GRACE_SECONDS="${START_CLEANUP_GRACE_SECONDS:-10}"

if [[ ! "$START_CLEANUP_GRACE_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "错误: START_CLEANUP_GRACE_SECONDS 必须是正整数。" >&2
  exit 2
fi

if [[ "$BACKEND_HOST" != "127.0.0.1" && "$BACKEND_HOST" != "localhost" && "$BACKEND_HOST" != "::1" ]]; then
  if [[ -z "${SILICON_NOTEBOOK_ADMIN_PASSWORD:-}" || "${SILICON_NOTEBOOK_ADMIN_PASSWORD}" == "admin" ]]; then
    echo "错误: 对外监听 $BACKEND_HOST 前必须设置非默认 SILICON_NOTEBOOK_ADMIN_PASSWORD。" >&2
    exit 1
  fi
fi

# 在拉起新进程前拒绝占用中的端口，避免新进程绑定失败却留下旧服务。
# 生产目标 Ubuntu 有 ss，macOS 回落到 lsof。
listeners() {
  local port="$1" rows pids
  if command -v ss >/dev/null 2>&1; then
    rows="$(ss -H -tlnp "sport = :${port}" 2>/dev/null || true)"
    [[ -n "$rows" ]] || return 0
    pids="$(printf '%s\n' "$rows" | grep -oE 'pid=[0-9]+' | grep -oE '[0-9]+' | sort -u || true)"
    if [[ -n "$pids" ]]; then
      pids="${pids//$'\n'/,}"
      printf 'PID %s\n' "$pids"
    else
      printf 'PID unavailable\n'
    fi
  elif command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null || true)"
    [[ -n "$pids" ]] || return 0
    printf 'PID %s\n' "${pids//$'\n'/,}"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "${port}/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)"
    [[ -n "$pids" ]] || return 0
    printf 'PID %s\n' "${pids//$'\n'/,}"
  else
    echo "错误: 需要 ss / lsof / fuser 之一检查启动端口，但都不可用。" >&2
    exit 1
  fi
}

ensure_port_free() {
  local label="$1" port="$2" occupant
  occupant="$(listeners "$port")"
  if [[ -n "$occupant" ]]; then
    echo "错误: $label 端口 :$port 已被占用($occupant)；请先运行 npm run stop 或换端口。" >&2
    exit 1
  fi
}

ensure_port_free "backend" "$BACKEND_PORT"
ensure_port_free "frontend" "$FRONTEND_PORT"

if [[ "${SKIP_INSTALL:-0}" != "1" ]]; then
  echo "installing backend dependencies from backend/requirements.txt ..."
  "$PYTHON_BIN" -m pip install --disable-pip-version-check \
    -r "$ROOT_DIR/backend/requirements.txt"
  echo "installing frontend dependencies from frontend/package-lock.json ..."
  npm ci --prefix "$ROOT_DIR/frontend"
else
  echo "SKIP_INSTALL=1 — skipping backend/frontend dependency installation"
fi

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; rerun without SKIP_INSTALL=1" >&2
  exit 1
fi
if [[ ! -x "$ROOT_DIR/frontend/node_modules/.bin/next" ]]; then
  echo "frontend Next.js launcher not found; rerun without SKIP_INSTALL=1" >&2
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

# 默认只监听 loopback；显式 BACKEND_HOST=0.0.0.0 才对外暴露，并触发上面的密码预检。
# --workers 1: process-internal caches and dedup sets (e.g. VectorCache, extraction
# pools) are per-process and NOT shared across workers — N workers would mean N×
# memory and N independent (inconsistent) caches, not more throughput.
cd "$ROOT_DIR/backend"
( exec nohup "$PYTHON_BIN" -m uvicorn app.main:app \
  --host "$BACKEND_HOST" \
  --port "$BACKEND_PORT" \
  --workers 1 </dev/null ) \
  >>"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

cd "$ROOT_DIR/frontend"
( exec nohup "$ROOT_DIR/frontend/node_modules/.bin/next" start \
  -p "$FRONTEND_PORT" </dev/null ) \
  >>"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

echo "backend  : http://${BACKEND_HOST}:${BACKEND_PORT}   (PID $BACKEND_PID, log $BACKEND_LOG)"
echo "frontend : http://0.0.0.0:${FRONTEND_PORT}   (PID $FRONTEND_PID, log $FRONTEND_LOG)"
echo "(the backend's first log line prints the resolved absolute db/storage/log paths — check it if unsure which .local a launch is using)"
trap - EXIT INT TERM HUP
echo "silicon-notebook background processes were launched; readiness is not checked by this command."
echo "stop them with: npm run stop"
