#!/usr/bin/env bash
# silicon-notebook 后端启停 / 状态脚本(只管后端;要前后端一起跑用 scripts/dev.sh)。
#
#   scripts/backend.sh start     启动后端到 :8000(后台,日志落文件)
#   scripts/backend.sh stop      停掉 :8000 上的服务(无论是不是本后端)
#   scripts/backend.sh restart   停当前 + 启 silicon-notebook(":8000 起错服务" 最常用)
#   scripts/backend.sh status    看 :8000 现在跑的是什么 + readiness
#
# 为什么需要它:路径(DB/storage/.env)在代码里(Settings)已锚定到仓库根,与启动
# 目录无关——DB 解析到 仓库根/.local/silicon_notebook.db(你的真实库)。若 :8000 被
# 别的服务(如 "EDA Agent")占用,前端调 /api/notebooks 会 404,notebook 看起来像
# "全没了"——status/restart 能立刻识别并纠正。详见 scripts/README.md。
#
# 可用环境变量:PYTHON_BIN HOST PORT LOG_FILE
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 与 dev.sh 一致:默认用 miniconda python(带依赖),否则回退 python3
DEFAULT_PYTHON="/opt/homebrew/Caskroom/miniconda/base/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/.local/logs/backend.log}"
# 大库启动会在 readiness gate 内加载多 GB scale/ANN/PPR 工件；默认允许 30 分钟。
# 小库仍会立即就绪。可按磁盘/RAM 实测覆盖，脚本会持续打印 phase/detail。
START_TIMEOUT_SECONDS="${START_TIMEOUT_SECONDS:-1800}"
APP="app.main:app"

if [[ ! "$START_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "✗ START_TIMEOUT_SECONDS 必须是正整数。" >&2
  exit 2
fi

port_pid() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

# Shipped auth makes /api/notebooks anonymous requests return 401. Service
# identity therefore uses only the anonymous readiness document + exact
# OpenAPI title; readiness=true is a separate start-success condition.
svc_title() { curl -s -m 3 "http://$HOST:$PORT/openapi.json" 2>/dev/null \
                | "$PYTHON_BIN" -c "import sys,json;print(json.load(sys.stdin).get('info',{}).get('title','?'))" 2>/dev/null || echo "?"; }
ready_state() { curl -s -m 3 "http://$HOST:$PORT/api/ready" 2>/dev/null \
                | "$PYTHON_BIN" -c "import sys,json; v=json.load(sys.stdin); print('true' if v.get('ready') is True else 'false')" 2>/dev/null || echo "missing"; }
ready_summary() { curl -s -m 3 "http://$HOST:$PORT/api/ready" 2>/dev/null \
                | "$PYTHON_BIN" -c "import sys,json; v=json.load(sys.stdin); print(str(v.get('phase','starting'))+':'+str(v.get('detail','')))" 2>/dev/null || echo "starting:"; }
is_sn()       { [[ "$(svc_title)" == "silicon-notebook API" && "$(ready_state)" != "missing" ]]; }
is_sn_ready() { [[ "$(svc_title)" == "silicon-notebook API" && "$(ready_state)" == "true" ]]; }
database_status() {
  ( cd "$ROOT_DIR/backend" && PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" -c \
      "from app.core.config import Settings; from app.core.database_url import database_status; print(database_status(Settings().database_url))" )
}
DATABASE_STATUS=""
load_database_status() {
  local resolved
  if ! resolved="$(database_status 2>/dev/null)" || [[ -z "$resolved" ]]; then
    echo "✗ 数据库配置无效；请检查 DATABASE_URL（详细错误已隐藏）。" >&2
    return 1
  fi
  DATABASE_STATUS="$resolved"
}

terminate_launched_pid() {
  local pid="$1"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  kill -9 "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  return 1
}

cmd_status() {
  load_database_status
  echo "● $DATABASE_STATUS"
  local pid; pid="$(port_pid)"
  if [[ -z "$pid" ]]; then echo "● :$PORT 空闲 —— 没有服务在跑。"; return 0; fi
  if is_sn; then
    local state; state="$(ready_state)"
    if [[ "$state" == "true" ]]; then
      echo "● :$PORT = silicon-notebook 后端(PID $pid),ready=true ✅"
    else
      echo "● :$PORT = silicon-notebook 后端(PID $pid),ready=false ⚠"
    fi
  else
    echo "● :$PORT 被占用(PID $pid),但不是 silicon-notebook —— 是 \"$(svc_title)\"。"
    echo "  ⚠ 前端调 /api/notebooks 会 404(notebook 看似'消失')。用 '$0 restart' 换回 silicon-notebook。"
  fi
}

cmd_stop() {
  load_database_status
  local pid; pid="$(port_pid)"
  if [[ -z "$pid" ]]; then echo "✓ :$PORT 本来就没服务,无需停止。"; return 0; fi
  local title; title="$(is_sn && echo silicon-notebook || svc_title)"
  echo "停止 :$PORT 上的服务:PID=$pid(\"$title\")…"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do [[ -z "$(port_pid)" ]] && { echo "✓ 已停止,:$PORT 释放。"; return 0; }; sleep 0.5; done
  echo "  SIGTERM 未释放,强制 kill -9 $pid"; kill -9 "$pid" 2>/dev/null || true; sleep 1
  if [[ -z "$(port_pid)" ]]; then echo "✓ 已强制停止。"; else echo "✗ :$PORT 仍被占用,请手动检查。"; return 1; fi
}

cmd_start() {
  load_database_status
  local pid; pid="$(port_pid)"
  if [[ -n "$pid" ]]; then
    if is_sn_ready; then echo "✓ silicon-notebook 已在 :$PORT 运行(PID $pid),ready=true。无需重复启动。"; return 0; fi
    if is_sn; then echo "✗ silicon-notebook 已占用 :$PORT(PID $pid),但尚未就绪；请看日志。"; return 1; fi
    echo "✗ :$PORT 已被别的服务占用(\"$(svc_title)\", PID $pid)。"
    echo "  先跑 '$0 stop'(会停掉它)再 start,或换端口:PORT=8001 $0 start"; return 1
  fi
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "启动 silicon-notebook 后端…"
  echo "  python = $PYTHON_BIN"
  echo "  cwd    = $ROOT_DIR/backend  (路径已锚定仓库根,与 cwd 无关)"
  echo "  $DATABASE_STATUS"
  echo "  listen = http://$HOST:$PORT   日志 = $LOG_FILE"
  local launched_pid
  ( cd "$ROOT_DIR/backend" && exec nohup "$PYTHON_BIN" -m uvicorn "$APP" --host "$HOST" --port "$PORT" ) >>"$LOG_FILE" 2>&1 &
  launched_pid="$!"
  echo "  等待就绪（最长 ${START_TIMEOUT_SECONDS}s）"
  local last_summary=""
  for _ in $(seq 1 "$START_TIMEOUT_SECONDS"); do
    if is_sn_ready; then echo " ok"; echo "✅ 启动成功(PID $(port_pid)):ready=true。"; return 0; fi
    if ! kill -0 "$launched_pid" 2>/dev/null; then
      echo " 失败"; echo "✗ 后端进程提前退出,看日志排错:tail -50 $LOG_FILE"
      terminate_launched_pid "$launched_pid" || true
      return 1
    fi
    local summary; summary="$(ready_summary)"
    if [[ "$summary" != "$last_summary" ]]; then
      echo "  $summary"
      last_summary="$summary"
    fi
    if [[ "$summary" == error:* ]]; then
      echo "✗ 后端启动进入 error；已清理本次启动进程。看日志:tail -50 $LOG_FILE"
      terminate_launched_pid "$launched_pid" || true
      return 1
    fi
    sleep 1
  done
  echo " 超时"
  terminate_launched_pid "$launched_pid" || true
  echo "✗ ${START_TIMEOUT_SECONDS}s 内 /api/ready 未就绪；已清理本次启动进程。看日志:tail -50 $LOG_FILE"
  return 1
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status)  cmd_status ;;
  *)
    cat >&2 <<EOF
用法: $0 {start|stop|restart|status}
  start    启动后端到 :$PORT(后台);若 :$PORT 被非 silicon-notebook 占用,会拒绝并提示先 stop
  stop     停止 :$PORT 上的服务(graceful→必要时 kill -9)
  restart  停当前 + 启 silicon-notebook —— 修 ":$PORT 起错服务/notebook 消失" 最常用
  status   查看 :$PORT 现在跑的是什么 + notebook 数
环境变量: PYTHON_BIN(默认 $DEFAULT_PYTHON)  HOST(默认 127.0.0.1)  PORT(默认 8000)  LOG_FILE
EOF
    exit 2 ;;
esac
