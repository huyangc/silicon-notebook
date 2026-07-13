#!/usr/bin/env bash
# 离线包内启动脚本。用包内便携 node 跑 standalone 前端 + venv 的 uvicorn 后端,
# 目标机无需系统 node/npm。骨架复刻 scripts/prod.sh(加载根 .env、autotune、
# 对外暴露的 admin 密码预检),两处改为自包含:后端走 .venv,前端走便携 node。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
NODE_BIN="$SCRIPT_DIR/node/bin/node"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
LOG_DIR="$SCRIPT_DIR/.local/logs"
RUN_DIR="$SCRIPT_DIR/.local/run"

die() { printf '\033[1;31m[start:错误]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 存在性检查(未 install 就早报错) ---
[[ -x "$VENV_PY"  ]] || die "缺 $VENV_PY —— 先跑 ./install.sh。"
[[ -x "$NODE_BIN" ]] || die "缺便携 node $NODE_BIN —— 包不完整。"
[[ -f "$FRONTEND_DIR/server.js" ]] || die "缺 $FRONTEND_DIR/server.js —— 包不完整。"

# --- 加载根 .env(与 prod.sh 同款严格预检:缺 .env 默认报错,ALLOW_NO_ENV_FILE=1 跳过) ---
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
elif [[ "${ALLOW_NO_ENV_FILE:-0}" == "1" ]]; then
  echo "ALLOW_NO_ENV_FILE=1 — 无 .env,仅用系统环境变量启动" >&2
else
  die "缺 $SCRIPT_DIR/.env —— ./install.sh 会从 .env.example 生成一份;填好模型 URL 再启动(纯环境变量部署可设 ALLOW_NO_ENV_FILE=1)。"
fi

# --- 按核自调 BLAS/OMP 线程(须早于 python 起) ---
if [[ -f "$SCRIPT_DIR/scripts/autotune.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/scripts/autotune.sh"
fi

# --- host/port ---
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

# --- 对外暴露的 admin 密码预检(与 prod.sh 一致) ---
is_loopback() { [[ "$1" == "127.0.0.1" || "$1" == "localhost" || "$1" == "::1" ]]; }
if ! is_loopback "$BACKEND_HOST" || ! is_loopback "$FRONTEND_HOST"; then
  if [[ -z "${SILICON_NOTEBOOK_ADMIN_PASSWORD:-}" || "${SILICON_NOTEBOOK_ADMIN_PASSWORD}" == "admin" ]]; then
    die "对外监听(非 loopback)前必须设置非默认 SILICON_NOTEBOOK_ADMIN_PASSWORD。"
  fi
fi

mkdir -p "$LOG_DIR" "$RUN_DIR"

# --- 已在运行?(pidfile 存活即拒绝重复启动) ---
for name in backend frontend; do
  pf="$RUN_DIR/$name.pid"
  if [[ -f "$pf" ]] && kill -0 "$(cat "$pf" 2>/dev/null)" 2>/dev/null; then
    die "$name 已在运行(PID $(cat "$pf"))。先 ./stop.sh。"
  fi
  rm -f "$pf"
done

# --- 起后端(venv 的 uvicorn;--workers 1 与 prod.sh 一致,进程内缓存不跨 worker 共享) ---
(
  cd "$SCRIPT_DIR/backend"
  exec "$VENV_PY" -m uvicorn app.main:app \
    --host "$BACKEND_HOST" --port "$BACKEND_PORT" --workers 1
) >>"$LOG_DIR/backend.log" 2>&1 &
echo $! > "$RUN_DIR/backend.pid"

# --- 起前端(便携 node 跑 standalone server.js) ---
# ⚠ standalone 用 process.env.HOSTNAME 作绑定地址;Linux 上 HOSTNAME 常被设成主机名,
#   不显式覆盖会绑错地址,故这里强制设定。
(
  cd "$FRONTEND_DIR"
  export PATH="$SCRIPT_DIR/node/bin:$PATH"
  export NODE_ENV=production
  export HOSTNAME="$FRONTEND_HOST"
  export PORT="$FRONTEND_PORT"
  exec "$NODE_BIN" server.js
) >>"$LOG_DIR/frontend.log" 2>&1 &
echo $! > "$RUN_DIR/frontend.pid"

# --- 启动健康检查:等 2s,确认两进程都还活着(否则打印其日志尾并失败) ---
sleep 2
ok=1
for name in backend frontend; do
  pf="$RUN_DIR/$name.pid"
  if ! kill -0 "$(cat "$pf" 2>/dev/null)" 2>/dev/null; then
    echo "[start:错误] $name 启动即退出,最后日志:" >&2
    tail -n 25 "$LOG_DIR/$name.log" >&2 || true
    rm -f "$pf"
    ok=0
  fi
done
[[ "$ok" == 1 ]] || die "启动失败;必要时 ./stop.sh 收尾另一进程。"

echo "backend  : http://${BACKEND_HOST}:${BACKEND_PORT}   (PID $(cat "$RUN_DIR/backend.pid"), log $LOG_DIR/backend.log)"
echo "frontend : http://${FRONTEND_HOST}:${FRONTEND_PORT}  (PID $(cat "$RUN_DIR/frontend.pid"), log $LOG_DIR/frontend.log)"
echo "浏览器打开 frontend 地址即可访问。停止:./stop.sh"
