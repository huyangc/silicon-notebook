# 离线打包 / 无 node 目标机部署 — 设计

日期:2026-07-13
分支:`claude/python-env-packaging-script-b277b4`

## 背景与目标

把本服务(Next.js 前端 + FastAPI 后端)部署到一台**没有 npm/node、有 python 源、无 root** 的
Ubuntu 目标机。产出两个脚本:

- `pack.sh` — 在**打包机**上跑,产出一个自包含 tar 包。
- `install.sh` — 在**目标机**上跑,一键装好并可启动。

### 两个环境(关键前提)

| | 打包机 | 目标机 |
|---|---|---|
| node/npm | **有**,能 `npm install` + `next build` | **无** |
| python 源(pip 镜像) | — | **有**(允许在线装第三方包) |
| root | — | **无** |
| OS/架构 | 与目标机**一致**(由用户保证) | Linux `x86_64` 或 `aarch64` |

"一致"指 **OS/架构一致**——因此打包机构建出的 node 二进制、`@next/swc-linux-*`、`sharp`
原生模块、以及预编译的 python wheel,都能直接在目标机运行,不存在跨平台二进制问题。

### 为什么选"捆绑便携 node"(而非静态导出)

前端已验证是纯客户端 SPA(无 route handlers / middleware / server actions / 动态路由段),
理论上可静态导出彻底去 node。但用户明确选择**保留现有 `next start` 启动方式**并**捆绑便携 node**,
故本设计走这条路:目标机不装 node,改用包内自带的 node 运行前端。运行时行为与当前 `prod.sh`
完全一致(前端 `:3000` 反代 `/api/*` 到后端 `:8000`)。

## 交付物与包结构

```
silicon_notebook_<version>_linux-<arch>.tar.gz
├── backend/            # 后端源码(app/、requirements.txt),纯 python,免构建
├── frontend/           # next build standalone 产物:server.js + 精简 node_modules + .next/static + public
├── node/               # 便携 node 运行时(bin/node),与目标机同架构
├── wheelhouse/         # 打包机预编译的全部 python 依赖 wheel(默认开;可关)
├── scripts/            # autotune.sh 等运行时脚本
├── .env.example
├── install.sh          # 目标机:建 venv、装依赖、生成 .env、就绪自检
├── start.sh            # 启动:venv uvicorn + 便携 node 前端
├── stop.sh             # 停止
├── VERSION
└── DEPLOY.md           # 目标机部署说明(通用口径)
```

打包产物输出到 `dist/`(已在 `.gitignore`)。

## 组件设计

### 1. 仓库改动(唯一一处,无害)

`frontend/next.config.mjs`:把 `output` 改成环境变量门控。

```js
const nextConfig = {
  typedRoutes: true,
  output: process.env.NEXT_OUTPUT_STANDALONE === "1" ? "standalone" : undefined,
  async rewrites() { /* 不变 */ },
};
```

- `pack.sh` 构建时设 `NEXT_OUTPUT_STANDALONE=1` → 产出 `.next/standalone`。
- 不设时行为与现在完全相同,`npm run dev` / `npm run start` 不受影响。
- standalone 服务器仍会加载 `next.config` 的 `rewrites`,`/api/*` 反代照旧。

### 2. `pack.sh`(打包机)

职责:一条命令产出 tar 包。步骤:

1. **前置校验**:确认 `node`/`npm` 可用,确认在 Linux 上(便携 node 与 wheelhouse 都按当前平台产出)。
2. **前端构建**:
   - `cd frontend && npm install`(有 lockfile 用 `npm ci`)
   - `NEXT_OUTPUT_STANDALONE=1 npm run build`
   - 组装 standalone 运行目录:`.next/standalone/` 为根,把 `.next/static` 复制到
     `standalone/.next/static`,`public`(若有)复制到 `standalone/public`。
3. **便携 node**:
   - 默认从 `NODE_DIST_URL`(缺省 `https://nodejs.org/dist`)下对应
     `NODE_VERSION`(缺省跟随打包机 `node -v`)、对应架构的 `node-vX-linux-<arch>.tar.xz`,
     解压到 `node/`。
   - 可用 `NODE_TARBALL=/path/to/node.tar.xz` 直接指定本地 tarball(离线打包机场景)。
4. **python wheelhouse**(默认开,`SKIP_WHEELHOUSE=1` 关闭):
   - 在打包机上 `pip wheel -r backend/requirements.txt -w wheelhouse/`。
   - 目的:numpy/scipy/hnswlib/rustworkx/python-igraph/orjson 等带 C 扩展的包在**同架构**
     打包机上预编译,目标机可离线 `--no-index` 安装,免 gcc、免 root、免依赖镜像有全套 wheel。
5. **组装 + 打包**:复制 backend/、scripts/、`.env.example`,生成 `install.sh`/`start.sh`/`stop.sh`/
   `VERSION`/`DEPLOY.md`,`tar czf dist/silicon_notebook_<version>_linux-<arch>.tar.gz`。

可配置(环境变量):`NODE_VERSION` `NODE_DIST_URL` `NODE_TARBALL` `SKIP_WHEELHOUSE` `OUT_DIR`。

### 3. `install.sh`(目标机)

职责:一键装好,不需要 root。步骤:

1. **python 校验**:找 `python3`(可 `PYTHON_BIN` 覆盖),要求版本 **≥ 3.10**(numpy2 / pydantic 2.12 要求),
   否则报错退出并说明。
2. **建 venv**:`python3 -m venv .venv`;若因缺 `python3-venv`(Ubuntu 上装它需 root)失败,
   自动回退:下载 `virtualenv` zipapp(从 python 源)在用户态建 venv;都失败则清晰报错并给出让管理员装
   `python3-venv` 的指引。
3. **装 python 依赖**:
   - **优先离线**:`pip install --no-index --find-links wheelhouse -r backend/requirements.txt`。
   - **回退在线**:wheelhouse 缺某包时,从 python 源补装(`PIP_INDEX_URL` / `--index-url` 可配,
     默认用目标机已配置的 pip 源)。
   - `SKIP_WHEELHOUSE` 打的包无 wheelhouse → 直接走在线装。
4. **`.env`**:仓库根无 `.env` 则从 `.env.example` 复制一份,并提示:服务靠 `.env` 配的模型 URL 才能起,
   必须填好再启动(不自动填、不静默降级)。
5. **就绪自检**:venv 内 `python -c "import fastapi, uvicorn, numpy, hnswlib, ..."`;`node/bin/node -v`;
   打印 `start.sh` 用法与访问地址。全部为**只读校验**,不启动服务(启停交给用户/其它流程,遵循用户偏好)。

可配置:`PYTHON_BIN` `PIP_INDEX_URL` `APP_DIR`。

### 4. `start.sh` / `stop.sh`(目标机)

`start.sh` 复刻现有 `scripts/prod.sh` 的骨架,两处改为自包含:

- **加载根 `.env`**、**admin 密码预检**(对外监听 `0.0.0.0` 时强制非默认密码)、**autotune 调参**——照搬 `prod.sh`。
- **后端**:`.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT:-8000} --workers 1`(用 venv 的 python,不依赖系统 python 装了包)。
- **前端**:`PATH` 前置包内 `node/bin`,`node frontend/server.js`(standalone,取代 `next start`);
  显式设 `HOSTNAME=${FRONTEND_HOST:-127.0.0.1}` `PORT=${FRONTEND_PORT:-3000}`。
  - ⚠ standalone 的 `server.js` 用 `process.env.HOSTNAME` 作绑定地址,而 Linux 常把 `HOSTNAME` 设成
    主机名 → 不显式覆盖会绑错。故 `start.sh` 必须显式设 `HOSTNAME`。
- **进程管理**:后台起两进程,PID 写 `.local/run/*.pid`;`stop.sh` 读 PID 优雅停。日志进 `.local/logs/`。

## 风险与取舍

- **hnswlib 无 wheel 要编译**:wheelhouse(同架构预编译)化解;关掉 wheelhouse 时,若目标机无 gcc/无 root 且
  镜像无 hnswlib wheel,会装失败——这是选择 `SKIP_WHEELHOUSE=1` 的已知代价,`install.sh` 会明确报错而非静默。
- **目标机缺 `python3-venv`**:装它要 root。`install.sh` 走 `virtualenv` zipapp 用户态兜底;仍失败则明确指引。
- **便携 node 下载**:打包机需能访问 `NODE_DIST_URL` 或用户用 `NODE_TARBALL` 提供本地包;`install.sh` 侧不下载 node。
- **数据目录 `.local`**:运行时自动建(路径锚定仓库根),本设计不含数据迁移。
- **端口对外**:默认全 loopback(前端 `127.0.0.1:3000` / 后端 `127.0.0.1:8000`)。需对外由 `FRONTEND_HOST=0.0.0.0`
  开启,并触发 admin 密码预检(同 `prod.sh`)。

## 非目标(YAGNI)

- 不做静态导出/去 node(用户已选捆绑 node)。
- 不做 systemd/service 单元、不做反向代理/https(交给部署方)。
- 不做数据迁移、不做多机编排。
- 不改后端运行时架构、不改前端 SPA 逻辑。

## 验证方式

- 打包机:`bash pack.sh` 成功产出 tar 包;解压检查目录结构完整(node/bin/node 可执行、frontend/server.js 存在、
  wheelhouse 非空)。
- 目标机模拟(同架构、PATH 去掉 node):解压 → `bash install.sh` 装好 → 填 `.env` → `bash start.sh` →
  `curl 127.0.0.1:3000` 出前端、`curl 127.0.0.1:3000/api/...`(或后端 8000)通 → `stop.sh` 停干净。
- README / README_zh 增补"离线打包部署"章节(通用口径,不写具体机器路径)。
