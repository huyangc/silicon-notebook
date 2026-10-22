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

# 退出收尾与 prod.sh 共用 scripts/launcher_cleanup.sh:TERM → 宽限 → KILL,宽限期间
# 再次 Ctrl-C 则立即 KILL 且配套服务照常清理。
# 环境变量:START_CLEANUP_GRACE_SECONDS(默认 10,退出时 SIGTERM 到 SIGKILL 的宽限)
source "$ROOT_DIR/scripts/launcher_cleanup.sh"
launcher_install_traps

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

launcher_check_grace_seconds

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi
if [[ ! -x "$ROOT_DIR/frontend/node_modules/.bin/next" ]]; then
  echo "frontend Next.js launcher not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi
extension_services_start

# BACKEND_HOST=0.0.0.0 to expose the API beyond localhost (e.g. server deploys).
# Paths (db/storage/env_file) are anchored to the repo root in code (Settings), not to
# the launch directory — the `cd` below is only so Python resolves the `app` package.
# Both children are launched as `( exec ... ) &` like prod.sh, so $! is the real
# process: a SIGTERM from cleanup reaches uvicorn / next itself, not an npm or
# subshell layer that would sit in front of it and leave it running.
# 与 prod.sh 一样不加 uvicorn 的 --timeout-graceful-shutdown:prod.sh 现行做法没有它,
# 退出时限完全由上面 cleanup 的 TERM→宽限→KILL 负责。
cd "$ROOT_DIR/backend"
launcher_fork_mark BACKEND_PID
( export PYTHONPATH="$INHERITED_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}"
  exec "$PYTHON_BIN" "$ROOT_DIR/scripts/python_env.py" -m uvicorn app.main:app \
  --host "${BACKEND_HOST:-127.0.0.1}" --port 8000 ) &
BACKEND_PID=$!
launcher_fork_done

cd "$ROOT_DIR/frontend"
# `npm run dev` used to run frontend/package.json's `predev` hook first
# (`npm run sync:ui-plugins` = `node scripts/sync-ui-plugins.mjs`, which projects the
# out-of-tree UI plugins into the app). Starting next directly would skip it, so the
# same script runs here, from the same directory, before next starts. It runs as
# FRONTEND_PID, a direct `exec`ed child polled like the companion controller, so the
# one cleanup covers a cancellation inside this window (no npm or node left behind),
# a failed sync starts no frontend and stops the backend, as a failed `predev` did,
# and a backend that exits meanwhile ends the launcher with its status without
# waiting for the sync. backend/tests/test_extension_service_launchers.py pins these
# commands to package.json so a new hook there cannot be skipped unnoticed.
launcher_fork_mark FRONTEND_PID
( exec node scripts/sync-ui-plugins.mjs ) &
FRONTEND_PID=$!
launcher_fork_done
while kill -0 "$FRONTEND_PID" 2>/dev/null && kill -0 "$BACKEND_PID" 2>/dev/null; do
  sleep "$LAUNCH_MONITOR_POLL_SECONDS"
done
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  backend_status=0
  wait "$BACKEND_PID" || backend_status=$?
  exit "$backend_status"
fi
sync_status=0
wait "$FRONTEND_PID" || sync_status=$?
unset FRONTEND_PID
if [[ "$sync_status" != "0" ]]; then
  echo "错误: 前端插件同步失败(退出码 $sync_status),未启动前端。" >&2
  exit "$sync_status"
fi

launcher_fork_mark FRONTEND_PID
( exec "$ROOT_DIR/frontend/node_modules/.bin/next" dev ) &
FRONTEND_PID=$!
launcher_fork_done

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep "$LAUNCH_MONITOR_POLL_SECONDS"
done
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  wait "$BACKEND_PID"
else
  wait "$FRONTEND_PID"
fi
