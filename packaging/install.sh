#!/usr/bin/env bash
# 离线包内一键安装(目标机)。无 root:建用户态 venv,优先用包内 wheelhouse 离线装
# python 依赖(缺的再从 python 源在线补),不启动服务(启停用 ./start.sh / ./stop.sh)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[install:错误]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1) python 版本校验(SQLite 写锁公平性依赖 CPython 3.13 的 PyMutex 交接语义,
#        需 >= 3.13;numpy2 / pydantic 2.12 本身只需 >= 3.10,3.13 是更严格的下限) ---
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "找不到 $PYTHON_BIN;设 PYTHON_BIN 指向目标机 python3。"
"$PYTHON_BIN" - <<'PY' || die "python 版本过低,需 >= 3.13。"
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 13) else 1)
PY
log "python: $("$PYTHON_BIN" -V 2>&1) @ $(command -v "$PYTHON_BIN")"

# --- 2) 建 venv(无 root;缺 ensurepip/python3-venv 时 --without-pip + wheelhouse 引导 pip 兜底) ---
if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  log ".venv 已存在,复用。"
else
  log "创建 venv…"
  if "$PYTHON_BIN" -m venv .venv 2>/tmp/sn_venv_err; then
    :
  else
    log "python -m venv 失败(通常缺 ensurepip/python3-venv,装它需 root)。尝试 --without-pip 兜底…"
    rm -rf .venv
    "$PYTHON_BIN" -m venv --without-pip .venv 2>>/tmp/sn_venv_err || {
      cat /tmp/sn_venv_err >&2 || true
      die "venv 创建失败。三选一:(a) 让管理员安装 python3-venv;(b) 设 PYTHON_BIN 指向自带 venv/pip 的 python(如 conda/pyenv);(c) 手动建好 venv 后重跑。"
    }
    pipwhl="$(ls "$SCRIPT_DIR"/wheelhouse/pip-*.whl 2>/dev/null | head -1 || true)"
    [[ -n "$pipwhl" ]] || die "venv 无 pip、wheelhouse 里也没有 pip 轮子,无法离线引导 pip。请让管理员装 python3-venv,或改用自带 pip 的 python。"
    log "用 wheelhouse 里的 pip 轮子引导 pip 进 venv…"
    .venv/bin/python "$pipwhl/pip" install --no-index --find-links wheelhouse pip setuptools wheel
  fi
fi

PIP=(".venv/bin/python" -m pip)

# --- 3) 装 python 依赖:优先离线 wheelhouse,缺的回退在线(python 源) ---
REQ="$SCRIPT_DIR/backend/requirements.txt"
[[ -f "$REQ" ]] || die "缺 $REQ,包不完整。"
index_args=()
[[ -n "${PIP_INDEX_URL:-}" ]] && index_args=(--index-url "$PIP_INDEX_URL")

if compgen -G "$SCRIPT_DIR/wheelhouse/*.whl" >/dev/null 2>&1; then
  log "从 wheelhouse 离线安装依赖…"
  if "${PIP[@]}" install --no-index --find-links wheelhouse -r "$REQ"; then
    log "离线安装完成。"
  else
    log "wheelhouse 不全,回退在线补装(python 源)…"
    "${PIP[@]}" install --find-links wheelhouse ${index_args[@]+"${index_args[@]}"} -r "$REQ"
  fi
else
  log "无 wheelhouse,从 python 源在线安装依赖…"
  "${PIP[@]}" install ${index_args[@]+"${index_args[@]}"} -r "$REQ"
fi

# --- 4) .env ---
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  log ".env 已存在,保留不动。"
else
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  log "已从 .env.example 生成 .env —— 启动前必须填模型服务 URL(OPENAI_COMPAT_* / EMBED_* / RERANK_* 等),否则问答不可用(不会静默降级)。"
fi

# --- 5) 就绪自检(只读,不启动服务) ---
log "自检 python 依赖…"
"$SCRIPT_DIR/.venv/bin/python" - <<'PY' || die "python 依赖自检未通过,见上面缺失项。"
import importlib.util, sys
mods = ["fastapi", "uvicorn", "pydantic", "pydantic_settings", "numpy", "scipy",
        "hnswlib", "networkx", "rustworkx", "igraph", "orjson", "openai", "httpx",
        "yaml", "docx", "pypdf", "openpyxl", "markdown_it"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
if missing:
    print("  缺少:", ", ".join(missing)); sys.exit(1)
print("  python 依赖 OK")
PY

NODE_BIN="$SCRIPT_DIR/node/bin/node"
[[ -x "$NODE_BIN" ]] || die "缺便携 node($NODE_BIN),包不完整。"
log "便携 node OK: $("$NODE_BIN" -v)"
[[ -f "$SCRIPT_DIR/frontend/server.js" ]] || die "缺前端 standalone($SCRIPT_DIR/frontend/server.js)。"
log "前端 standalone OK"

cat <<EOF

安装完成 ✅
下一步:
  1) 编辑 $SCRIPT_DIR/.env,填好模型服务 URL(要对外暴露再设非默认 SILICON_NOTEBOOK_ADMIN_PASSWORD)。
  2) 启动:  ./start.sh
             (默认 loopback:后端 127.0.0.1:8000、前端 127.0.0.1:3000)
     对外:  FRONTEND_HOST=0.0.0.0 ./start.sh
  3) 停止:  ./stop.sh
EOF
