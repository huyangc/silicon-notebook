#!/usr/bin/env bash
# silicon-notebook 停止:干净停掉 `npm run start`(scripts/prod.sh)拉起的前后端。
#
#   npm run stop          停掉后端 uvicorn 与前端 next-server
#
# 按「服务端口」定位真正的监听进程(后端 PORT、前端 FRONTEND_PORT),而不是记 PID:
# prod.sh 的前端经由 npm 包装,真正监听端口的是子进程 next-server,只 kill npm 会漏
# 掉它;按端口停则无论 start 前台/后台跑、也不管那层 npm 包装,都能停到实际的
# next-server / uvicorn。端口解析口径与 prod.sh 一致:先 source 仓库根 .env,再让
# PORT / FRONTEND_PORT 环境变量覆盖(缺省 8000 / 3000)。
#
# 环境变量:PORT FRONTEND_PORT
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 与 prod.sh 对齐:source 根 .env 以拿到其中可能配置的 PORT / FRONTEND_PORT。
# 停止侧对缺 .env 保持宽容(能停就停),不像启动侧那样硬报错退出。
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090,SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

BACKEND_PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

# 列出监听指定 TCP 端口的进程 PID(仅 LISTEN 状态,排除到该端口的客户端连接)。
# ss:Ubuntu/现代 Linux 基础包 iproute2 自带,部署机(Ubuntu 24.04)首选且必有;
# lsof:macOS 自带、但 Ubuntu 最小化安装默认没有,作开发机回落;fuser 再兜底。
# 三者都没有则报错退出。(只 root 或本人可见自己进程的 PID,与本机启停同用户一致。)
listeners() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -tlnp "sport = :${port}" 2>/dev/null | grep -oE 'pid=[0-9]+' | grep -oE '[0-9]+' | sort -u || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser "${port}/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true
  else
    echo "错误: 需要 ss / lsof / fuser 之一按端口定位进程,但都不可用。" >&2
    exit 1
  fi
}

stop_service() {
  local label="$1" port="$2"
  local pids
  pids="$(listeners "$port")"
  if [[ -z "$pids" ]]; then
    echo "$label (:$port) — 未在运行"
    return 0
  fi

  # 先礼后兵:透明打印将停掉的进程,发 SIGTERM,轮询等其自行退出。
  echo "$label (:$port) — 停止以下进程:"
  for pid in $pids; do
    ps -o pid=,comm= -p "$pid" 2>/dev/null | sed 's/^/    /' || true
  done
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true

  local waited=0
  while [[ $waited -lt 10 ]]; do
    local alive=""
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then alive+="$pid "; fi
    done
    [[ -z "$alive" ]] && break
    sleep 1
    waited=$((waited + 1))
  done

  # 超时仍存活的强杀。
  local remaining=""
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then remaining+="$pid "; fi
  done
  if [[ -n "$remaining" ]]; then
    echo "    超时未退,SIGKILL: $remaining"
    # shellcheck disable=SC2086
    kill -9 $remaining 2>/dev/null || true
  fi
  echo "$label (:$port) — 已停止"
}

stop_service "backend " "$BACKEND_PORT"
stop_service "frontend" "$FRONTEND_PORT"
