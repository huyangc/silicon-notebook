#!/usr/bin/env bash
# silicon-notebook 后端启停 / 状态脚本(只管后端;要前后端一起跑用 scripts/dev.sh)。
#
#   scripts/backend.sh start     启动后端到 :8000(后台,日志落文件)
#   scripts/backend.sh stop      停掉 :8000 上的服务(无论是不是本后端)
#   scripts/backend.sh restart   停当前 + 启 silicon-notebook(":8000 起错服务" 最常用)
#   scripts/backend.sh status    看 :8000 现在跑的是什么 + notebook 数
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
APP="app.main:app"

port_pid() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }

# silicon-notebook 后端独有 /api/notebooks;EDA Agent 等其它服务没有→返回非 200。
http_code() { curl -s -o /dev/null -w "%{http_code}" -m 3 "http://$HOST:$PORT/api/notebooks" 2>/dev/null || echo 000; }
is_sn()     { [[ "$(http_code)" == "200" ]]; }
# :PORT 服务的 openapi 标题(用来说明"占用的到底是谁")
svc_title() { curl -s -m 3 "http://$HOST:$PORT/openapi.json" 2>/dev/null \
                | "$PYTHON_BIN" -c "import sys,json;print(json.load(sys.stdin).get('info',{}).get('title','?'))" 2>/dev/null || echo "?"; }
nb_count()  { curl -s -m 3 "http://$HOST:$PORT/api/notebooks" 2>/dev/null \
                | "$PYTHON_BIN" -c "import sys,json;print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?"; }
database_status() {
  ( cd "$ROOT_DIR/backend" && PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" -c \
      "from app.core.config import Settings; from app.core.database_url import database_status; print(database_status(Settings().database_url))" )
}

cmd_status() {
  echo "● $(database_status)"
  local pid; pid="$(port_pid)"
  if [[ -z "$pid" ]]; then echo "● :$PORT 空闲 —— 没有服务在跑。"; return 0; fi
  if is_sn; then
    echo "● :$PORT = silicon-notebook 后端(PID $pid),notebooks=$(nb_count) ✅"
  else
    echo "● :$PORT 被占用(PID $pid),但不是 silicon-notebook —— 是 \"$(svc_title)\"。"
    echo "  ⚠ 前端调 /api/notebooks 会 404(notebook 看似'消失')。用 '$0 restart' 换回 silicon-notebook。"
  fi
}

cmd_stop() {
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
  local pid; pid="$(port_pid)"
  if [[ -n "$pid" ]]; then
    if is_sn; then echo "✓ silicon-notebook 已在 :$PORT 运行(PID $pid),notebooks=$(nb_count)。无需重复启动。"; return 0; fi
    echo "✗ :$PORT 已被别的服务占用(\"$(svc_title)\", PID $pid)。"
    echo "  先跑 '$0 stop'(会停掉它)再 start,或换端口:PORT=8001 $0 start"; return 1
  fi
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "启动 silicon-notebook 后端…"
  echo "  python = $PYTHON_BIN"
  echo "  cwd    = $ROOT_DIR/backend  (路径已锚定仓库根,与 cwd 无关)"
  echo "  $(database_status)"
  echo "  listen = http://$HOST:$PORT   日志 = $LOG_FILE"
  ( cd "$ROOT_DIR/backend" && nohup "$PYTHON_BIN" -m uvicorn "$APP" --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 & )
  echo -n "  等待就绪"
  for _ in $(seq 1 40); do
    if is_sn; then echo " ok"; echo "✅ 启动成功(PID $(port_pid)):/api/notebooks 可用,notebooks=$(nb_count)。"; return 0; fi
    echo -n "."; sleep 1
  done
  echo " 超时"; echo "✗ 40s 内 /api/notebooks 仍不可用,看日志排错:tail -50 $LOG_FILE"; return 1
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
