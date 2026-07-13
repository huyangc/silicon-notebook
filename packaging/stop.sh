#!/usr/bin/env bash
# 离线包内停止脚本。按 pidfile 优雅停止(SIGTERM,10s 未退再 SIGKILL),并连带
# 清理 uvicorn 可能派生的 worker 子进程。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.local/run"

stopped=0
for name in frontend backend; do
  pf="$RUN_DIR/$name.pid"
  [[ -f "$pf" ]] || continue
  pid="$(cat "$pf" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    pkill -TERM -P "$pid" 2>/dev/null || true   # 先招呼子进程(uvicorn worker)
    kill  -TERM "$pid"    2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill  -KILL "$pid"    2>/dev/null || true
    fi
    echo "已停止 $name (PID $pid)"
    stopped=1
  else
    echo "$name 未在运行(PID ${pid:-?} 不存在)"
  fi
  rm -f "$pf"
done

[[ "$stopped" == 1 ]] || echo "没有正在运行的进程。"
