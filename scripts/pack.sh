#!/usr/bin/env bash
# scripts/pack.sh — 在「与目标机同 OS/同架构」的打包机上,产出一个自包含离线部署包。
#
# 目标机无需 npm/node:包内自带便携 node(跑 standalone 前端)+ 预编译 python wheelhouse。
# 前端必须在有 node/npm 的机器上构建一次;因打包机 == 目标机(同架构),产出的 node 二进制、
# @next/swc-linux-*、sharp 原生模块、python C 扩展 wheel 都能直接在目标机运行。
#
# 用法:  bash scripts/pack.sh
#
# 可配置(环境变量):
#   VERSION                  包版本串(默认 <date>-<git短sha>)
#   OUT_DIR                  产出目录(默认 <repo>/dist)
#   NODE_VERSION             便携 node 版本(默认跟随打包机 `node -v`,如 v22.22.0)
#   NODE_DIST_URL            node 下载源(默认 https://nodejs.org/dist;可指内网镜像)
#   NODE_TARBALL             直接用本地 node tarball(设了就不下载;离线打包机场景)
#   NEXT_PUBLIC_API_BASE_URL 前端 API 基址(默认 /api,走同源反代;远程访问务必保持 /api)
#   SKIP_WHEELHOUSE=1        不预编译 wheelhouse(目标机改为纯在线 pip 安装)
#   PACK_PYTHON              打包机 python(默认 python3;其小版本须与目标机一致)
#   PIP_INDEX_URL            pip 源(建 wheelhouse 时用)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
PACK_PYTHON="${PACK_PYTHON:-python3}"

log() { printf '\033[1;34m[pack]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[pack:错误]\033[0m %s\n' "$*" >&2; exit 1; }

command -v node >/dev/null 2>&1 || die "打包机需要 node(用于 next build)。"
command -v npm  >/dev/null 2>&1 || die "打包机需要 npm。"
command -v "$PACK_PYTHON" >/dev/null 2>&1 || die "找不到 $PACK_PYTHON(建 wheelhouse 用)。"
command -v curl >/dev/null 2>&1 || [[ -n "${NODE_TARBALL:-}" ]] || die "需要 curl 下载便携 node(或设 NODE_TARBALL 指向本地包)。"

# --- 平台探测(打包机 == 目标机) ---
os_raw="$(uname -s)"; arch_raw="$(uname -m)"
case "$os_raw" in
  Linux)  nos=linux ;;
  Darwin) nos=darwin ;;
  *) die "不支持的 OS: $os_raw(仅 Linux / Darwin)。" ;;
esac
case "$arch_raw" in
  x86_64|amd64)  narch=x64 ;;
  aarch64|arm64) narch=arm64 ;;
  *) die "不支持的架构: $arch_raw。" ;;
esac
[[ "$nos" == linux ]] || log "注意:当前在 $nos 上打包,产物只能给 $nos 目标机;要部署到 Linux 请在 Linux 打包机上跑本脚本。"

VERSION="${VERSION:-$(date +%Y%m%d)-$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo nogit)}"
PKGNAME="silicon_notebook_${VERSION}_${nos}-${narch}"
STAGE="$OUT_DIR/stage/$PKGNAME"
log "版本=$VERSION  平台=${nos}-${narch}  产出目录=$OUT_DIR"

rm -rf "$OUT_DIR/stage"
mkdir -p "$STAGE" "$OUT_DIR"

# --- 1) 前端 standalone 构建 ---
log "构建前端(standalone)…"
(
  cd "$ROOT_DIR/frontend"
  if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
  NEXT_OUTPUT_STANDALONE=1 \
  NEXT_PUBLIC_API_BASE_URL="${NEXT_PUBLIC_API_BASE_URL:-/api}" \
    npm run build
)
[[ -f "$ROOT_DIR/frontend/.next/standalone/server.js" ]] || \
  die "未生成 .next/standalone/server.js —— next.config 的 standalone 门控没生效?(需 NEXT_OUTPUT_STANDALONE=1)"

log "组装前端产物(standalone + static + public)…"
mkdir -p "$STAGE/frontend"
cp -R "$ROOT_DIR/frontend/.next/standalone/." "$STAGE/frontend/"
mkdir -p "$STAGE/frontend/.next"
cp -R "$ROOT_DIR/frontend/.next/static" "$STAGE/frontend/.next/static"
[[ -d "$ROOT_DIR/frontend/public" ]] && cp -R "$ROOT_DIR/frontend/public" "$STAGE/frontend/public"

# --- 2) 便携 node ---
NODE_VERSION="${NODE_VERSION:-$(node -v)}"   # 形如 v22.22.0
[[ "$NODE_VERSION" == v* ]] || NODE_VERSION="v$NODE_VERSION"
NODE_PKG="node-${NODE_VERSION}-${nos}-${narch}"
log "准备便携 node: $NODE_PKG"
if [[ -n "${NODE_TARBALL:-}" ]]; then
  [[ -f "$NODE_TARBALL" ]] || die "NODE_TARBALL 不存在: $NODE_TARBALL"
  node_tar="$NODE_TARBALL"
else
  node_tar="$OUT_DIR/${NODE_PKG}.tar.xz"
  if [[ ! -f "$node_tar" ]]; then
    url="${NODE_DIST_URL:-https://nodejs.org/dist}/${NODE_VERSION}/${NODE_PKG}.tar.xz"
    log "下载 $url"
    curl -fsSL "$url" -o "$node_tar" || die "下载 node 失败;可设 NODE_TARBALL 指向本地包,或 NODE_DIST_URL 指向内网镜像。"
  fi
fi
mkdir -p "$STAGE/node"
tar -xf "$node_tar" -C "$STAGE/node" --strip-components=1
"$STAGE/node/bin/node" -v >/dev/null 2>&1 || die "便携 node 无法执行(架构不匹配?)。"
log "便携 node OK: $("$STAGE/node/bin/node" -v)"
# 瘦身:目标机只用 node 跑 standalone 的 server.js,删掉 npm/npx/corepack/头文件/文档(省 ~150M)
rm -rf "$STAGE/node/lib/node_modules/npm" "$STAGE/node/lib/node_modules/corepack" \
       "$STAGE/node/include" "$STAGE/node/share" \
       "$STAGE/node/bin/npm" "$STAGE/node/bin/npx" "$STAGE/node/bin/corepack" 2>/dev/null || true

# --- 3) 后端源码 + 运行时脚本 ---
log "复制后端源码…"
mkdir -p "$STAGE/backend"
cp -R "$ROOT_DIR/backend/app" "$STAGE/backend/app"
cp "$ROOT_DIR/backend/requirements.txt" "$STAGE/backend/requirements.txt"
find "$STAGE/backend" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/backend" -type f -name '*.py[co]' -delete 2>/dev/null || true
mkdir -p "$STAGE/scripts"
cp "$ROOT_DIR/scripts/autotune.sh" "$STAGE/scripts/autotune.sh"

# --- 4) python wheelhouse(在同架构打包机上预编译,目标机离线可装) ---
if [[ "${SKIP_WHEELHOUSE:-0}" == "1" ]]; then
  log "SKIP_WHEELHOUSE=1 — 跳过 wheelhouse(目标机将纯在线 pip 安装)。"
else
  log "预编译 python wheelhouse(numpy/scipy/hnswlib/… 就地编译)…"
  mkdir -p "$STAGE/wheelhouse"
  idx=(); [[ -n "${PIP_INDEX_URL:-}" ]] && idx=(--index-url "$PIP_INDEX_URL")
  "$PACK_PYTHON" -m pip wheel -r "$ROOT_DIR/backend/requirements.txt" -w "$STAGE/wheelhouse" ${idx[@]+"${idx[@]}"} \
    || die "pip wheel 失败;修复后重试,或用 SKIP_WHEELHOUSE=1 改纯在线安装。"
  # 追加 pip/setuptools/wheel,供目标机在缺 ensurepip 时引导 pip
  "$PACK_PYTHON" -m pip download pip setuptools wheel -d "$STAGE/wheelhouse" ${idx[@]+"${idx[@]}"} \
    || log "warn: pip/setuptools/wheel 预取失败(仅影响目标机缺 ensurepip 的兜底路径)。"
  log "wheelhouse: $(find "$STAGE/wheelhouse" -maxdepth 1 -type f | wc -l | tr -d ' ') 个文件"
fi

# --- 5) 包内脚本 + 元数据 ---
log "放入 install/start/stop + 说明…"
cp "$ROOT_DIR/packaging/install.sh" "$STAGE/install.sh"
cp "$ROOT_DIR/packaging/start.sh"   "$STAGE/start.sh"
cp "$ROOT_DIR/packaging/stop.sh"    "$STAGE/stop.sh"
cp "$ROOT_DIR/packaging/DEPLOY.md"  "$STAGE/DEPLOY.md"
cp "$ROOT_DIR/.env.example"         "$STAGE/.env.example"
cp "$ROOT_DIR/model-services.example.toml" "$STAGE/model-services.example.toml"
chmod +x "$STAGE/install.sh" "$STAGE/start.sh" "$STAGE/stop.sh"
cat > "$STAGE/VERSION" <<EOF
version=$VERSION
platform=${nos}-${narch}
node=$("$STAGE/node/bin/node" -v)
wheelhouse=$([[ "${SKIP_WHEELHOUSE:-0}" == "1" ]] && echo skipped || echo included)
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
built_by_python=$("$PACK_PYTHON" -V 2>&1)
EOF

# --- 6) 打 tar ---
TARBALL="$OUT_DIR/${PKGNAME}.tar.gz"
log "打包 → $TARBALL"
tar -czf "$TARBALL" -C "$OUT_DIR/stage" "$PKGNAME"
log "完成 ✅  $(du -sh "$TARBALL" | cut -f1)  $TARBALL"
echo
echo "拷到目标机后:"
echo "  tar xzf ${PKGNAME}.tar.gz && cd ${PKGNAME} && ./install.sh"
