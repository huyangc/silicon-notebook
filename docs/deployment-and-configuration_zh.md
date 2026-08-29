# 部署与配置

[返回 README](../README_zh.md) · [English](./deployment-and-configuration.md)

本文是源码 checkout 的详细部署与配置参考。最短本地路径见仓库根 README；离线部署包目标机说明见 [packaging/DEPLOY.md](../packaging/DEPLOY.md)。

## 部署

silicon-notebook 以两个进程运行——FastAPI 后端 + Next.js 前端——并由 `DATABASE_URL`
选择唯一 repository。发行默认 SQLite **无需 GPU、无需数据库服务、无需本地模型服务**；
准备好可访问的服务后，也可直接使用 PostgreSQL 16。LLM、嵌入和 rerank 仍只通过 URL 服务访问；MinerU 则独立支持
远端 HTTP（`MINERU_MODE=http`）、同机隔离子进程（`MINERU_MODE=cli`）或 PyMuPDF4LLM 回退
（`MINERU_MODE=off`）。未配置模型服务或 MinerU parser 时，整条管线以确定性回退离线运行。

### 前置条件

- **Python ≥ 3.13**——SQLite 写锁的公平性依赖 CPython 3.13 中 `threading.Lock`（由
  `PyMutex` 支撑）的交接语义；更低版本会静默退化为抢占式（barging），写者饿死无声
  重现（见 `backend/app/repositories/sqlite/database.py`）。
- **Node.js ≥ 20** 与 npm
- **git**
- C/C++ 工具链*仅作兜底*——`numpy`、`rustworkx`、`hnswlib` 在常见平台都有预编译 wheel;
  仅当 pip 不得不从源码编译时,才需装 Xcode Command Line Tools(macOS)或
  `build-essential`(Debian/Ubuntu)。

### 1 · 安装

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

# 后端 —— 装进一个隔离的 Python 环境
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt

# 前端
( cd frontend && npm install )
```

### 2 · 配置

```bash
cp .env.example .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
```

`MODEL_SERVICES_CONFIG` 指向部署者维护的 TOML。编辑其中的 `[services]` 与
`[bindings]`，为每个物理服务设置 `max_concurrency`，并只把 `api_key_env` 引用的密钥
写入 `.env`。删除配置路径或把它置空，会明确进入确定性离线模式（仅关键词检索，无模型
抽取/作答）。用户不能提供或覆盖模型凭据、端点、模型名和容量。

`ASK_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` 治理内部的 Ask 终态后扩展点。
超时是协作式的：已经进入同步 callback 的工作不会被遗弃，deadline 之后的后续
contribution 则不再启动。精确默认值与校验范围只在 product/API 合同登记。

`REPORT_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` 独立治理 Deep Report 的终态后
observer，不借用 Ask 预算。其语义同样是协作式：已开始 callback 安全完成，deadline
后不再启动后续 report contribution；并且只在持久 `done` CAS 成功后运行。精确默认值
与范围只在 product/API 合同登记。

如果供应商要求固定的核采样值，chat 服务可选配置 `top_p = 0.95`（或 `0` 到 `1` 的
其他有限数值）。该服务级值覆盖所有已绑定 workload 的调用默认值，并同时用于真实请求和
响应缓存键；省略字段则保留历史的逐调用行为。embedding 与 rerank 服务不允许配置
`top_p`。

后端会监视非空的模型服务 TOML，文件变化通常在约 2 秒内生效。watcher 会先连续两次
观察到同一变更签名，解析后再复核读后签名仍相同，才原子发布，避免原地 truncate/write
保存过程中发布移动快照。空或仅含注释的已配置 TOML 在 reload 中按非法版本拒绝；明确
进入离线模式须清空 `MODEL_SERVICES_CONFIG` 并重启。显式强制 reload 会跳过两次观察，
但仍做读后复核。新调用使用新 registry 与 runtime 代际，已提交调用继续在原代际完成。
文件暂时缺失、只写了一半或配置非法时，不会清空线上配置；后端保留上一份有效 registry，
并对稳定的非法文件版本记录不含凭据的诊断。修正并再次保存即可触发下一次 watcher 尝试。仅修改 `.env` 中
的密钥不会被监视；修改引用密钥后需要重启后端，或再保存一次 TOML。`MODEL_SERVICES_CONFIG`
路径本身在启动时选定，因此从 `.env` 删除或改换路径也仍需重启。

- **嵌入维度**——`EMBED_DIM` 必须等于所绑定 embedding 模型的输出维度。可选
  `EMBED_RUNTIME_DIM`（默认 `0`=关）把相似度空间截断到前 N 维 + re-normalize（MRL），
  使进程内矩阵 / ANN 内存约 `EMBED_DIM/N`× 缩减,而库内原生向量保留为真相源。开关它需
  重建 scale 索引,见 [docs/runtime-dim-truncation-runbook.md](./runtime-dim-truncation-runbook.md)。
  **切勿改小 `EMBED_DIM` 来降维** —— 那会把全部存量向量当异维丢弃。
- **PDF 高保真**(可选)—— 一个 MinerU 端点，见[用 MinerU 解析 PDF](./operations_zh.md#用-mineru-解析-pdf)；
  保持 `MINERU_MODE=off` 则走本地 PyMuPDF4LLM 版面/Markdown 兜底。
- **所选来源 PPR shadow 开关**——`SOURCE_SUBGRAPH_PPR_ENABLED` 只控制冻结的所选来源
  snapshot 内部稀疏 PPR producer，默认为不可见 Shadow 观测启用（`true`）。它与 `GRAPH_PPR_ENABLED` 相互独立：
  关闭它不会影响历史全范围 PPR、局部图原语、直接检索或受保护 baseline 通道。只有下述
  所选来源激活门运行在 shadow/active 时，它才会成为候选生产者。
- **超大来源图伴生产物**——`SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED` 让 scale
  rebuild/fold 额外发布按来源直接寻址的 CSR 伴生产物；`SOURCE_PARTITIONED_PPR_ENABLED`
  允许内部 shadow consumer 读取它。两者默认均为 `true`，并可独立回滚。只开 consumer
  而没有 identity 匹配的伴生产物时返回 capability unavailable，绝不回退整图遍历。
  `SOURCE_PARTITIONED_PPR_MAX_ITERATIONS` 限制请求期稀疏迭代，partition 发布复用既有
  `SOURCE_SUBGRAPH_MAX_*` 行数护栏。Ask/深度报告只能通过共用激活门消费；伴生产物
  不可用只产生局部降级原因，绝不授权整图回退。
- **所选来源 rollout 质量门**——active mode 只能读取配对评测命令生成且验证通过、
  不含正文的 attestation。摘要只做完整性检查，不是授权签名：输入与输出必须放在部署
  自己控制的受信工件路径，并用 `SELECTED_SOURCE_GRAPH_EXPECTED_CORPUS_SIGNATURE` 与
  `SELECTED_SOURCE_GRAPH_EXPECTED_MODEL_JSON` 钉住期望语料和模型。运行模式默认为不可见 `shadow`；
  它不进入公开 API payload、轨迹、stream 或 UI，也是唯一可以没有批准 attestation 的模式。`allowlist`、稳定 hash
  `rollout` 与 `on` 都从受信路径读取证明，任何不匹配 fail closed。Ask 与深度报告共用此门。

`.env.example` 是非服务变量与密钥槽位的权威清单；`model-services.example.toml` 是
服务、绑定与容量模板；[配置](#配置)按组列出常用项。

#### 从旧版逐角色 `.env` 升级

已有部署可把废弃的 `OPENAI_COMPAT_*`、`KG_LLM_*`、`EMBED_*`、`RERANK_*`
配置转换为系统统一配置：

```bash
# 默认只预览：读取 .env，不写任何文件。
python scripts/migrate_legacy_model_env.py --env .env

# 检查服务列表和推算容量后，生成 TOML，并只改写 .env 中由脚本管理的模型字段。
python scripts/migrate_legacy_model_env.py --env .env --apply
```

应用迁移前会备份 `.env`，并把当前文件及所有含密钥的备份权限收紧为 `0600`；密钥会
保留在新命名的 `.env` 槽位中，不会写入 TOML，也不会打印到终端。脚本保留旧版角色
回退关系，并把 endpoint/model/key 完全相同的角色
合并为同一个物理服务。初始容量由旧的 `KG_EXTRACT_WORKERS`、`KG_ASK_RESERVE` 和
`EMBED_CONCURRENCY` 推算，仅作为迁移初值，必须按服务商的真实物理容量复核。可重复传入
`--max-concurrency general=20 --max-concurrency embedding=4` 覆盖推算值。安装流程生成且
尚未改动的示例 TOML 可直接替换；其他已存在的 TOML 应先检查，仅在确认替换时使用
`--force`。被替换的文件都会先备份。

**远程访问——浏览器在另一台机器上**(不是服务器),所以**不能用 `localhost`/`127.0.0.1`**
(那在每个访客自己机器上解析,连不到服务器)。

**单台同机部署推荐(同源反代):** Next.js 前端把 `/api/*` 转发到本机后端
(`frontend/next.config.mjs`),浏览器只跟前端同源通信。前端用相对的 `/api` 即可——
**免 CORS、后端无需对外暴露**:

```bash
# frontend/.env.local (NEXT_PUBLIC_* 构建期烘焙 → 改后要重新 build)
NEXT_PUBLIC_API_BASE_URL=/api
```

后端可留在 `127.0.0.1:8000`(反代在本机转发),只需前端端口对外可达。后端不在
`127.0.0.1:8000` 时用 `BACKEND_PROXY_TARGET` 覆盖。

**另一种——前后端在不同 host(双 origin 直连):** 把前端指向后端可达 URL,并在后端放行前端来源:

```bash
# frontend/.env.local (构建期烘焙)
NEXT_PUBLIC_API_BASE_URL=http://<backend-host>:8000/api
# 后端仓库根 .env — 逗号分隔的允许来源;不能用 `*`(开了 credentials)
SILICON_NOTEBOOK_CORS_ORIGINS=http://<frontend-host>:3000
```

再让 uvicorn 加 `--host 0.0.0.0`(或 `BACKEND_HOST=0.0.0.0 npm run dev`)使 API 对外可达。

### 3 · 运行

不需要手工 schema 步骤——首次启动时后端会迁移当前选中的 SQLite 或 PostgreSQL 数据存储，
并创建 `.local/storage` 与 `.local/logs` 目录，只 seed 本地用户。后端务必**不带 `--reload`**:reload 重启会杀掉
进行中的抽取后台任务,让上传卡在 `extracting`。

所有相对路径(数据库、存储、日志、`.env`)都在**代码里锚定到仓库根**,与启动脚本
`cd` 进哪个目录无关——启动目录从此不重要。后端首行日志会打印解析后的绝对路径
(`paths: db=... storage=... log_dir=...`),不确定某次启动到底用的哪个 `.local/`
时看它即可。离线 CLI(`scripts/batch_ingest.py`)与下面两种服务启动方式,解析到的
都是同一个仓库根 `.local/`。

**启动脚本要求仓库根存在 `.env`**(`npm run dev` / `npm run start`):缺失时直接报错
退出,而不是悄悄用空白默认值启动;如果发现改名残骸(如 `.env.local`)会点名提示改回
——注意 Next.js 自己打印的「Environments: .env.local」只代表**前端**读到了它,后端只读
`.env`。后端进程启动时也做同样检查(仅当存在残骸文件才硬报错;单纯缺 `.env` 只告警并
照常启动,全新 checkout 与容器纯环境变量部署不受影响)。纯环境变量部署可设
`ALLOW_NO_ENV_FILE=1` 显式跳过。

```bash
# 开发 —— 前后端一起(后端支持 reload)
npm run dev
```

```bash
# 生产 —— 先 build 前端,再同时提供两个服务(后端单进程)
npm run start

# 停止前后端(可从任意终端执行,无需回到 start 进程 Ctrl-C)
npm run stop
```

`npm run start` 调用 `scripts/prod.sh`:先用
`python -m pip install -r backend/requirements.txt` 把后端依赖安装到 `PYTHON_BIN`
所在环境，再用 `npm ci --prefix frontend` 按 lockfile 重建前端依赖树。然后在前台
完成 `next build`，并用 `nohup` 且脱离标准输入的方式后台启动
`next start` 与单 worker Uvicorn，两者日志都落在 `.local/logs/`。
两个脱离 terminal 的进程拉起后，`npm run start` 立即退出，不轮询后端 readiness
或前端 HTTP；运维方需自行校验 `/api/ready` 和前端。两个子进程完成交接前若被中断，
脚本会同时发 SIGTERM，最多等待 `START_CLEANUP_GRACE_SECONDS`(默认 10 秒)，
对残留进程发 SIGKILL 并 `wait` 回收。环境需要
`ss` / `lsof` / `fuser` 之一；目标端口已被占用时会在安装依赖前拒绝，
即使当前用户看不到监听器 PID 也不例外，避免新进程绑定失败后仍由旧服务占着端口。
后台服务用 `npm run stop` 停止。
已同时预装两端依赖的镜像可设 `SKIP_INSTALL=1`；该模式下若缺少
`frontend/node_modules/.bin/next`，仍会在 build 前直接报错，不会带病继续。

设 `SKIP_BUILD=1` 可复用已构建好的 `frontend/.next`(如预构建镜像场景)。可用
`BACKEND_HOST` / `PORT` / `FRONTEND_PORT` 覆盖监听地址/端口。后端默认只监听
`127.0.0.1`；显式绑定非 loopback 地址时必须配置非默认
`SILICON_NOTEBOOK_ADMIN_PASSWORD`，否则启动直接失败。

部署 Agent MCP 时，把 `MCP_PUBLIC_URL` 设为客户端真正可达的 `/mcp` 精确地址。该值同时用于
MCP transport metadata，以及新签发 token 旁所链接的匿名机器说明
`GET /api/agent-mcp/onboarding`。公网还必须设置 `MCP_REQUIRE_HTTPS=1`；接入说明链接本身绝不携带
bearer token。由于该值会进入 Agent 指令正文，启动会拒绝 userinfo、query、fragment、非精确
`/mcp` path，以及空白/控制符/反引号。

生产诊断支持的目标形态是 Ubuntu 24.04 上按上述 `npm run start` 启动、只含一个
Uvicorn worker 的普通部署。若部署疑似卡住，请保持服务运行，并在**卡顿正在发生时**
采集事故；见[生产事故即时采集](./operations_zh.md#生产事故即时采集)。先重启会丢掉命令需要关联的
活跃请求、锁、进程与线程栈证据。

`npm run stop` 调用 `scripts/stop.sh`:停掉正在监听后端 `PORT` 与前端 `FRONTEND_PORT`
(缺省 `8000` / `3000`)的进程。它与 start 一样先 source 仓库根 `.env` 解析端口,所以若
你用自定义 `PORT` / `FRONTEND_PORT` 启动,停止时也传同样的值。脚本先发 `SIGTERM`,等待
后再对残留进程 `SIGKILL`;没有服务在跑时是空操作。定位监听进程优先用 `ss`(Ubuntu/Linux
的 iproute2 基础包自带),回落 `lsof`(macOS 默认有)再回落 `fuser`——三者至少有其一即可。

> **一次性迁移注意**——如果你此前用 `npm run dev`(或手动 `cd backend && uvicorn ...`)
> 在路径锚定上线之前的版本启动过,数据可能落在 `backend/.local` 而非仓库根的
> `.local`。升级后二选一:①合并进去(在仓库根执行 `mv backend/.local/* .local/`,先
> 检查有无冲突);②用绝对路径 env 显式保留原位置
> (`SILICON_NOTEBOOK_STORAGE_DIR=/abs/path/storage`、
> `DATABASE_URL=sqlite:////abs/path/silicon_notebook.db`——绝对 sqlite 路径注意四条
> 斜杠)——绝对路径的 env 值永远原样尊重,不会被重新锚定。

### 3.1 · 选择 SQLite 或 PostgreSQL

`DATABASE_URL` 是唯一 active backend 选择器，接受 `sqlite:///...`、
`postgresql://...` 和会被规范化的旧别名 `postgres://...`。不支持的 scheme、建连、
migration 或 warmup 失败都 fail closed，不会回落到另一数据库。`SHADOW_DATABASE_URL`
不会选择 active backend，单独设置也不会启动同步；只有显式 forward-shadow CLI 会读取它。

```dotenv
# 发行默认
DATABASE_URL=sqlite:///.local/silicon_notebook.db

# 直接使用 PostgreSQL 16
DATABASE_URL=postgresql://silicon_app:change-me@127.0.0.1:5432/silicon_notebook

# DATABASE_URL 仍为 SQLite 时可选的单向影子目标
# SHADOW_DATABASE_URL=postgresql://silicon_shadow:change-me@127.0.0.1:5432/silicon_notebook_shadow
```

PostgreSQL 必须使用 UTF-8，并把 `pg_trgm` 安装在 `public`。数据库 owner 可让 migration
0001 创建，也可由 DBA 预装；同名扩展位于其他 schema 时会被拒绝。向量存为 float32
`bytea`，不需要 pgvector。生产仍保持单 backend worker（`--workers 1`）。

大型 PostgreSQL 数据库还应把 `btree_gin` 安装在 `public`，并建立两条 notebook-aware
词法索引。以 database owner 运行时，运维工具可创建该扩展；默认只检查，只有 `--apply`
才改数据库：

```bash
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py --apply
```

这些索引刻意采用在线运维发布，而不塞进启动 migration：在数百万行活表上建立 GIN
会消耗显著 CPU、I/O、临时磁盘和时间。缺少索引时应用结果仍正确，但常见词可能先扫描
全库 trgm 命中再按 notebook 过滤，最终撞 statement timeout。监控、上线和回退步骤见
[运维文档](./operations_zh.md#postgresql-notebook-aware-词法索引)。

改 URL 不会搬运既有行。全新目标可停服务后改 URL、启动并验证空库/bootstrap 状态。
对于存量 SQLite，已交付的 forward-shadow CLI 可在 SQLite 继续 active 时建立并持续维护
PostgreSQL 影子库。它要求 PostgreSQL 16、专用且可恢复的目标库、已验证的源/目标备份、
容量凭据、owner-private 工作目录和一个受监督 worker；它不会修改 `DATABASE_URL`、不会把
流量导向 PostgreSQL，也不会把 PostgreSQL 写入反向复制到 SQLite。完整命令、监控和故障处理
见[运维文档](./operations_zh.md)，离线包 checklist 也见
[packaging/DEPLOY.md](../packaging/DEPLOY.md)。

### 4 · 验证

```bash
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok","llm_configured":...}
bash scripts/check.sh                        # hermetic smoke + 全量 pytest + 前端 test/tsc/build
```

`scripts/check.sh` 同时会跑下列契约守卫;改动它们各自看护的代码时,也可以单独跑:

```bash
PYTHONPATH=backend python scripts/check_ask_modes_contract.py            # 提问模式 id 集合
PYTHONPATH=backend python scripts/check_object_type_labels_contract.py   # object_type 显示名
PYTHONPATH=backend python scripts/check_ui_vocabulary.py                 # 界面词汇
```

后端会把结构化 JSONL 日志写入 `.local/logs/`(`requests` / `events` / `llm`);跟踪一次
上传或排查卡住的 source 见[可观测性 / 日志](./operations_zh.md#可观测性--日志)。

### 5 · 离线打包(目标机没有 npm/node)

要部署到一台**没有 npm/node**、只有 Python 包索引、且**无 root** 的机器:在一台**有 Node、
且 OS/CPU 架构与目标机一致**的打包机上产出自包含 tar 包,再拷过去一键装:

```bash
bash scripts/pack.sh          # → dist/silicon_notebook_<version>_<os>-<arch>.tar.gz
```

`pack.sh` 把前端构建成 Next.js **standalone** 服务,捆绑一份**便携 Node 运行时**(匹配打包机
架构)来跑它,并预编译一个包含全部 Python 依赖的 **wheelhouse**——这样 `hnswlib` / `scipy`
等编译型包在目标机上无需编译器。因为打包机与目标机同 OS/同架构,包内每个二进制都能直接运行。

目标机上——无需 npm/node、无需 root:

```bash
tar xzf silicon_notebook_<version>_<os>-<arch>.tar.gz
cd    silicon_notebook_<version>_<os>-<arch>
./install.sh    # 建用户态 venv;优先用 wheelhouse 离线装依赖
                # (缺的再从 pip 源在线补);生成 .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
vi .local/model-services.toml  # 服务、workload 绑定、每服务 max_concurrency
vi .env         # MODEL_SERVICES_CONFIG + api_key_env 引用的密钥
./start.sh      # 便携 node 跑 standalone 前端 + venv 的 uvicorn 后端
./stop.sh       # 停止两者
```

打包机可配置项:`NODE_VERSION` / `NODE_DIST_URL` / `NODE_TARBALL`(便携 Node 来源)、
`SKIP_WHEELHOUSE=1`(改为目标机在线装依赖)、`PIP_INDEX_URL`、`PACK_PYTHON`。目标机可配置项:
`PYTHON_BIN`、`PIP_INDEX_URL`、`FRONTEND_HOST` / `FRONTEND_PORT` / `BACKEND_HOST` / `PORT`。
打包机的 Python **小版本**应与目标机一致,否则预编译 wheel 装不上(install.sh 会自动回退在线
安装)。目标侧细节见包内 `DEPLOY.md`。

## 配置

所有模型服务均通过 URL 端点接入，不启动本地模型服务。

### 系统模型服务、调度与诊断

模型 endpoint、协议、模型名、工作负载绑定与服务容量都由部署者统一管理，不再由用户配置。
把 `model-services.example.toml` 复制为 `.local/model-services.toml`，设置
`MODEL_SERVICES_CONFIG=.local/model-services.toml`，并在 `.env` 中只填写各服务
`api_key_env` 所引用的密钥。仓库中的示例不含凭证；`MODEL_SERVICES_CONFIG` 留空时，
系统明确进入离线 / 确定性降级。

每个 `[services.<id>]` 表配置 `display_name`、`kind`、`protocol`、`base_url`、
`model`、`api_key_env` 与 `max_concurrency`；`[bindings]` 把稳定的 workload id
（如 `ask_answer`、`reasoning_agent`、`knowhow_complete`、`kg_extract`、
`retrieval_query_embedding`、`retrieval_rerank`、`agent_profile_consolidate`——模型
服务状态页里标签为「库理解整理」，即「AI 对这个库的理解」背后的后台巡固调用，
见下面的 `AGENT_PROFILE_ENABLED`——与 `retrieval_experience_distill`（部署级、低频的
离线蒸馏调用，蒸出检索策略经验库的封闭词表条目，见下面的 `RETRIEVAL_EXPERIENCE_ENABLED`；
刻意独立于 `agent_profile_consolidate` 的 workload，好让部署能单独换模型或单独关闭它，
不必连带关掉笔记本理解巡固）映射到物理服务。多个 workload
可以共用一个服务，它们也会共用该服务唯一的调度器和并发预算。`max_concurrency`
是唯一的模型容量参数；来源作业数、窗口大小、batch 大小与本地 ANN 线程都不会再创建模型 gate。
自动界面的 Ask 后台路由复用 `reasoning_agent` 做一次 corpus-blind 问题理解；未绑定该 workload
或调用失败时，自动路由保守落到通用问答，不会新增另一套模型服务配置。

可选的 `[thinking]` 表按 **chat workload** 控制思考模式，值只能是 `enabled`、
`disabled` 或 `provider_default`。策略按 workload 而非物理服务配置，因为同一个 chat
服务可能同时承载推理任务与机械的结构化输出任务。解析后的策略是唯一开关：传输层通过
OpenAI SDK 的 `extra_body` 把 `enabled` 或 `disabled` 发送为 `thinking.type`，
`provider_default` 则不发送覆盖。provider 与 transport 两层都不检查配置的模型名。

仓库示例明确列出当前全部默认值：`ask_answer`、`reasoning_agent`、
`graph_chain_verify`、`report_outline`、`report_sufficiency`、`schema_induction`、
`agent_profile_consolidate` 与 `retrieval_experience_distill` 开启。它们是单次或有界的
规划、判断、合成调用，结果会直接影响用户或持久检索策略。其余现有 chat workload 全部
关闭；尤其是全部 KG 抽取/治理/描述阶段、chunk 问题生成、元数据/摘要抽取、查询/证据
改写、报告分节撰写与最终审计、Memory 预览、Knowhow 格式整理/补全。这些路径要么偏机械，
要么已有上游规划或人工审阅，要么按窗口/chunk/章节放大，隐藏推理的单位 token 质量收益
明显更低。省略某项时使用同一套内建默认值；设为 `provider_default` 则刻意不发送覆盖值。

每个已绑定 chat 服务的显式模式都通过 OpenAI-compatible
`extra_body={"thinking":{"type":"enabled|disabled"}}` 发送，不再附带
`reasoning_effort`。`model` 值只是不透明的 endpoint 路由标识，绝不决定 thinking
是否生效。未知 workload、
非 chat workload 或非法值会使配置校验失败，而非静默忽略。调用提交时会把解析后的模式
与物理路由一起冻结，所以 TOML 热加载只影响新调用，不改变已排队调用。显式模式属于
LLM 响应缓存身份；`provider_default` 请求保留历史缓存键。
chat 健康检查不进入 workload 策略：无论模型名是什么，它都固定发送
`thinking_mode="disabled"`、绕过响应缓存，也不能通过 `[thinking]` 覆盖。

可选生成问题索引使用后台 chat workload `chunk_question_generation` 与既有
`chunk_embedding` workload；执行离线 `question-index` 前必须同时绑定。rollout mode
保持关闭就是零成本默认；语义和全部数值护栏只在[产品与 API 参考](./product-and-api_zh.md#可选生成问题召回补充)登记。

部署问答引擎使用 interactive chat workload `plugin_engine`。仓库示例把它绑定到
`general` 并关闭 provider thinking，因为提示与调用循环由插件掌控。它的 completion
输出预算有意继承所绑定模型客户端的普通回答上限；`.env.example` 中独立的
`ASK_PLUGIN_ENGINE_*` 设置限制检索次数、证据与 prompt 大小、模型调用次数和轨迹形态。
精确默认值与合法范围只登记在
[产品与 API 参考](./product-and-api_zh.md#部署问答引擎-askengine)中。

部署索引管线在 PR-1 不新增独立模型 workload。插件可通过 `indexing.pipeline`
贡献按笔记本选择的分块/索引策略；parser 路由仍是自动的。浏览器里的笔记本设置会把
当前管线只读展示给纯 reader，对 owner 与组内容管理员提供带“将重建全库索引”明确
确认的切换入口，同时继续把参考库挂载管理保持为 owner-only。`pending` /
`missing` / `unavailable` 的语义与净化后的 API 面只在
[产品与 API 参考](./product-and-api_zh.md#部署索引管线-indexingpipeline)登记。
运维可调 `INDEXING_PIPELINE_MAX_PROPOSALS_PER_SOURCE`、
`INDEXING_PIPELINE_MAX_TEXT_CHARS`、`INDEXING_PIPELINE_MAX_ELEMENT_REFS`、
`INDEXING_PIPELINE_REBUILD_MAX_PROPOSALS` 与
`INDEXING_PIPELINE_REBUILD_MAX_TEXT_CHARS`；精确默认值/范围只在上述产品参考登记。
切换即便没有绑定 KG 模型也复用同一条持久 KG rebuild job。重建工作先持久化到不可见的
notebook stage；模型与 embedding I/O 都在最终事务外，只有精确 job/generation/source-snapshot
CAS 成功才会一起发布全部可见来源的 chunks 与可选 KG 产物。失败、取消、启动恢复或迟到 worker
只丢弃 stage，live generation 完全不动。未绑定 KG 模型时，同一 publisher 会显式保留 live KG、
发布 core chunk 代次，并在笔记本合格时做 scale full 代次。

Knowhow 单行空格补全使用两个 interactive chat workload：`reasoning_agent` 对当前 notebook 与当前有效
挂载参考库的联邦证据做规划和反思检索，`knowhow_complete` 再把这些证据与同表参考合成为结构化建议。
需要此功能时必须把两者都绑定到兼容的 chat 服务；任一未绑定或任一阶段 provider 失败时都不返回建议，
应用不会静默退成同表补全，也绝不伪造离线结果。

深度报告把规划质量与长正文 workload 分开：仓库示例中 `report_outline` 绑定 reasoning 服务，
长正文 `report_section` 与大型结构化终审 `report_summary` 绑定非思考型 general 服务。部署可覆盖，
但所选 provider/model 必须能在配置的 completion 预算内输出普通 content，不得把预算全部消耗于隐藏 reasoning。

调度策略固定在代码中：

- 每个物理服务最多同时运行 `max_concurrency` 个调用；不同服务拥有独立槽位；
- 总队列上限为 `10 × max_concurrency`，单个 actor 最多排队
  `2 × max_concurrency` 项；
- 调度按 8 个 interactive : 2 个 report : 1 个 background 的固定节奏循环，
  每个优先级内按 actor 轮转，因此持续交互流量下后台工作仍会前进；
- 排队截止时间固定为 interactive 30 秒、report 300 秒、background 1800 秒，
  派发前会响应取消；
- 致命 provider 错误立即打开熔断器；连续 3 次瞬态错误也会打开。冷却 30 秒后只允许
  1 个 half-open 恢复探针。

调度器与熔断状态只存在于进程内。生产必须只运行一个后端进程：
`scripts/prod.sh` 固定 Uvicorn `--workers 1`。多 worker 会把声明的服务并发度成倍放大，
并把队列、熔断与健康状态分裂到多个进程。

普通用户看到的**模型服务**面板是只读的，展示脱敏后的系统服务身份、绑定 workload、
最近健康状态、active/maximum、排队数、最老等待时间和熔断状态。
`GET /api/model-services/status` 只读本地状态，绝不自动探测上游。只有 admin 可通过
`POST /api/admin/model-services/{service_id}/test` 或
`POST /api/admin/model-services/test-all` 显式测试一个或全部服务。endpoint、凭证、
provider 响应正文和原始异常只保留在服务端日志。

Ask / 模型错误会尽量携带物理服务、workload、安全模型名与 `support_id`。用户遇到问题时，
应把 support id 提交给维护人员；维护人员结合服务端日志与只读服务面板即可定位坏掉的模型服务。
本地检索 / 索引错误不会把 provider 标为异常。

个人模型配置路由和可编辑配置页面已经删除。schema v24 会在与版本戳相同的事务中，
不可逆地把历史 `user_profiles.model_settings` 全部覆盖成 `{}`，并删除旧的逐用户健康状态。
如需把历史凭证留作外部记录，升级前先备份数据库；应用不会恢复或继续使用这些值。

模型调用超时、重试、输出预算与 batch 大小仍是普通 workload 调优项。`EMBED_DIM` 必须与绑定的
embedding 模型输出维度一致。KG 来源级并行仍由 `KG_JOB_CONCURRENCY` 控制，自适应抽取窗口使用
`kg_extract` 所绑定服务的容量；两者都不能覆盖服务 `max_concurrency`。

**按核数自动调参：** 本地 CPU 工作仍可按机器缩放：

```text
KG_CLUSTER_ANN_THREADS   # 概念聚类 hnswlib 线程；0（默认）= min(cpu核数, 32)
```

`scripts/dev.sh` / `scripts/prod.sh` 会通过 `scripts/autotune.sh` 调整本地 OMP/BLAS
线程，但不会改变任何模型服务容量。

**数据库：**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout（毫秒，默认 30000）
DB_WRITE_LOCK_STATS         # 开启进程级 SQLite 写锁 wait/hold 观测（默认 true）
DB_WRITE_LOCK_WARN_MS       # wait/hold 超过此毫秒数即记一条限流的 db_write_lock_slow 事件（默认 200）
DB_WRITE_LOCK_FLUSH_SECONDS # 周期性 db_write_lock_stats 快照的发出间隔（秒），也是 db_write_lock_slow 按调用点的限流窗口（默认 60）
SQLITE_CACHE_SIZE_KB    # 每连接 SQLite 页缓存(KB,负值=KB)。连接按线程复用,总内存≈线程数×|值|（默认 -16384）
DATABASE_URL            # SQLite 路径（默认 .local/silicon_notebook.db）
SILICON_NOTEBOOK_STORAGE_DIR   # 上传文件存储目录（默认 .local/storage）
```

Ask 同步取消端点可能跨越多个数据库事务，也可能等待进程内写锁或后端特有的连接锁。
现有部署项不能给出可强制的整请求期限，因此浏览器只保留一条取消请求直到服务端响应，
不采用猜测的客户端超时。

**来源文件上传：**

```text
SOURCE_UPLOAD_MAX_MB    # 单个上传来源文件的最大大小（默认 50）
```

`SOURCE_UPLOAD_MAX_MB` 必须是 1–1024 的整数；1 MB 严格等于 `1024 × 1024` 字节。后端从
Settings 派生字节上限，并对 multipart 的每个来源文件权威执行（413 会带当前上限）。
用户登录后，浏览器从 `GET /api/system/config` 取得解析后的字节上限，在添加来源弹窗
显示并即时拒绝超限选择，发送 multipart 前还会复查暂存文件。前后端还会固定限制每次
multipart 请求最多 20 个文件，避免可配置的单文件额度叠加成无界临时 spool。旧标签页和
直接 API 客户端仍始终以后端为准。同源部署下，Next.js external rewrite 还需要整次请求的
传输上限：其独立默认值只有 10 MiB，会在后端执行 `SOURCE_UPLOAD_MAX_MB` 之前截断合法的
multipart 上传。前端构建从同一个单文件设置、固定批量数和有界 multipart 余量推导该传输
上限，不增加第二个用户可见配置。离线 standalone 包按协议允许的最大值构建传输层，因此
目标机运行时 `.env` 仍可选择任一合法的 `SOURCE_UPLOAD_MAX_MB`。

**URL 来源导入：**

```text
URL_IMPORT_TRUSTED_PROXY_HOSTS  # URL 导入 SSRF 公网地址检查的受信插件代理 origin 豁免名单，逗号分隔（默认空 = 不豁免）
```

URL 导入链路拒绝解析到私网/回环/保留地址的出站 URL。与本服务同机部署的插件代理
（例如 `http://127.0.0.1:8100` 上的签名 PDF 下载代理）天然落在这类地址上；把它的
origin 写进此名单后 URL 导入才能触达。每项必须带 `http://` 或 `https://` scheme——
裸 `host:port` 会被整项静默忽略。匹配按 origin 精确进行（`scheme://host:port`，
统一小写、默认端口显式归一——不同端口就是不同 origin），命中只跳过导入探测与解析
下载中的「公网地址」检查；协议/凭证/端口形态检查照常生效。名单只来自本部署配置——
请求输入永远改不了它。探测半程的豁免只由插件路由适配器注入（浏览器与 MCP 的 URL
导入拿不到）；解析下载半程则按名单对所有 origin 命中的 URL 来源生效、含 reparse——
名单里公网可解析的 origin 因此也会让浏览器建的同 origin 来源在解析下载（含重定向链）
获得豁免。只把你信任到这个程度的 origin 写进名单。

**检索：**

```text
RETRIEVAL_TOP_N         # 推理/报告合成证据预算下界（默认 20）
REASONING_PER_QUERY_LIMIT # 不带检索档位的兼容调用每查询取数
REASONING_TOP_N_PER_QUERY  # 自适应预算：每个方面（子查询，含社区兄弟）保底席位（默认 3）
REASONING_TOP_N_CAP        # 自适应预算上限；对比题按方面数扩容（默认 36）
ASK_RELATED_KNOWLEDGE_LIMIT # Ask 响应中展示的相关 KG 条目上限
QUERY_REFINE_MAX_ITEMS / ASK_CONTEXT_RELATION_LIMIT # Ask 上下文中的精炼要点和排序关系条目上限
GRAPH_SEED_TOP_N / GRAPH_MAX_DEPTH / GRAPH_MAX_FAN_OUT # graph 遍历候选护栏
CHUNK_KG_NODE_SEED_TOP_N / CHUNK_KG_RELATION_SEED_TOP_N / CHUNK_KG_MAX_DEPTH / CHUNK_KG_FAN_OUT # chunk×KG overlay 护栏
CHUNK_GRAPH_RESERVE        # 为已过相关度门槛的纯图路径 chunk 预留席位（默认 0；评测后可设 1）
EXACT_LOOKUP_ENABLED       # 精确标识符通道：按 `set_db` 这类完整命令名整节取齐（默认 true）
EXACT_LOOKUP_MAX_IDENTIFIERS       # 每个问题最多探测几个名称（默认 3）
EXACT_LOOKUP_FTS_K                 # 每个标识符的精确命中采样窗口，用于挑选小节（默认 50）
EXACT_LOOKUP_MAX_SECTIONS          # 每个问题最多取齐几个小节（默认 3）
EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION  # 每个小节最多取几块（默认 12）
EXACT_SECTION_RESERVE      # mix 最终选择为这些块预留的席位，仍在既有预算内（默认 4）
```

**行为变化——这几项不再决定深度报告逐节深挖的预算。** 逐节深挖现在把报告自己的
「研究深度」映射到与逐步推理「检索档位」同一张五档预算表，并整行下传；数值契约在
`docs/product-and-api_zh.md`（「大纲便签与按节合成」与档位表），不在这里重复。

* `REASONING_TOP_N_PER_QUERY` / `REASONING_TOP_N_CAP`——报告路径完全不再读取，
  该节的最终相关性预算由档位的每方面席位与上限决定。
* `RETRIEVAL_TOP_N`——不再是报告节的证据预算下界（改由档位的 floor 决定）；它仍然
  约束「按已确认检索方向补检索」那一路的有界取数。
* `REASONING_MAX_SUBQUERIES`——报告路径不再读取。它连**默认**的深度 2 也受影响：该
  档每节首轮子查询为 5，而按配置推导的旧路径是 `REASONING_MAX_SUBQUERIES + 1`
  （默认值下为 6）。

所以调大这四项已经不会让报告更宽，要更宽请调高研究深度。逐步推理的 `mix`/`graph`
模式，以及任何不带档位的推理调用，仍照旧读取它们。

报告节此前从配置取的两项**上下文装配**预算同理，改由档位自己的数值决定：

* `ANSWER_CONTEXT_BUDGET_CHARS`——不再是报告节的 KG 上下文预算（改由档位的
  `kg_context_chars` 决定：4000/6000/8000/12000/16000）。它仍然决定逐步推理答案
  上下文的预算。
* `REPORT_SECTION_CHUNK_BUDGET`——报告带研究深度时不再读取（改由档位的
  `chunk_context_chars` 决定：12000/30000/50000/80000/120000；直接原文段的字符
  子预算仍按同一比例从它派生）。不带深度的调用方仍用它。

报告节进入 prompt 的直接原文段条数同样按档位的 `answer_element_items` 封顶
（4/6/8/12/16），并按检索相关度择优而非插入序切片。

**精确标识符通道：**问题里点到可精确查找的名称（`set_db`、`place_opt_design`、
`config.yaml`）时，检索先精确定位它所在的小节，再把整节取齐，避免一条命令的参数表
和示例被切散后又被预算截掉。

这条通道的闸比普通词法召回用的标识符抽取更窄，差别是一个成本决定：带 `_` 或 `.`
的名称一律放行，而只用连字符连接的词必须含数字。于是 `GPT-4`、`v1-2` 会探测，
`state-of-the-art`、`real-time`、`end-to-end` 不会。后面这批词出现在相当大比例的
分析型问题里（实际上深度报告的每一节问题都含一个），而每个词都要付一次真实的子串
探测——2 万块规模的库上实测 16 毫秒 / 50 命中——一旦命中还可能把整整一章推进答案的
证据预算。它们仍留在词法召回里，那边多一个 OR 词项几乎不花钱。

「整节取齐」依赖标题面包屑，而面包屑是 Markdown 解析路径才有的东西。MinerU 解析的
来源（PDF/DOCX）没有面包屑，通道在那里退回到「精确返回命中的那些块」。这仍然是本
特性要的效果——参数表能被救回来——但它不是整节补全，所以手册是 PDF 的库，收益会
小于手册是 Markdown 的库。

通道自身零模型调用、零 embedding；但在 mix 回答路径上它取回的 chunk 会一并进入既有
的一次 rerank 调用，最多给那一次请求多加
`EXACT_LOOKUP_MAX_SECTIONS × EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION` 篇文档。所有查询
都受上面这些参数硬界约束；问题里没有这类名称时不会多发一次查询。只作用于当前笔记本，
挂载的参考库刻意不在范围内。

**有界 KG 关系补全：**这是默认严格关闭的部署实验。它按来源代次绑定的持久
`(source_id,id)` keyset 水位逐页推进，每页只做索引化关系检查和有界的同源 FTS/ANN 候选，
绝不全扫整本书或整个 notebook。`shadow` 仅记录 proposal / verifier 聚合统计而不写库；`write`
把通过双阶段校验的关系以 `pending` 写入。两个启用模式仍必须命中 notebook allowlist 或稳定哈希灰度。每次调用只 hydrate 当前有界页中受限的证据 ID；pending 水位会另行重新入队，进程重启后由启动恢复再次调度当前来源代次。水位按模式隔离：两个启用模式互切时会在同一事务内先发布新模式的可恢复游标，再把旧 pending 游标标为 `stale`；切到 `off` 时只标旧游标且不排替代任务。

```text
KG_RELATION_COMPLETION_MODE              # off（默认）| shadow | write
KG_RELATION_COMPLETION_NOTEBOOK_ALLOWLIST # 逗号分隔 notebook id；* 匹配全部
KG_RELATION_COMPLETION_ROLLOUT_PERCENT    # 未命中 allowlist 的稳定灰度（默认 0）
KG_RELATION_COMPLETION_MAX_OBJECTS        # 每个 keyset 页的 anchor 上限（默认 160）
KG_RELATION_COMPLETION_MAX_PAIRS          # 下发的有向候选对上限（默认 120）
KG_RELATION_COMPLETION_SECTION_QUOTA      # 每个来源 section 的候选上限（默认 24）
KG_RELATION_COMPLETION_BATCH_PAIRS        # 每个 proposer/verifier batch 的候选数（默认 24）
KG_RELATION_COMPLETION_MAX_BATCHES        # 每轮最多模型 batch 数（默认 4）
KG_RELATION_COMPLETION_EXCERPT_CHARS      # 每个候选证据摘录字符上限（默认 800）
KG_RELATION_COMPLETION_MAX_PAGES_PER_RUN  # 每次调用最多 keyset 页数（默认 4）
KG_RELATION_COMPLETION_NEIGHBOR_TOP_K      # 每个 anchor 的 FTS/ANN 邻居数（默认 8）
KG_RELATION_COMPLETION_CANDIDATE_OVERFETCH # 候选 ID hydration 总上限（默认 64）
KG_RELATION_COMPLETION_BATCH_CHARS         # 每个模型 batch 的序列化字符上限（默认 48000）
```

该阶段复用已有 `kg_extract` proposer 与 `kg_refine` verifier workload。最后一个短事务会复核
source/run 代次仍是当前值，并确认所有对象与证据元素仍归属该来源，然后保存 verifier 看到的同一段服务端 excerpt，因此 reparse/delete 竞态写入零行。上述数值护栏均必须为正数（batch 字符至少 512）；非法配置无法启动，运行时零值也会 fail-closed 且不推进水位。上线应先用显式 allowlist + `shadow`，观察 `kg_relation_completion` 聚合事件，再只把选中的
notebook 切到 `write`；聚合事件名为 `kg_relation_completion_done`。

**可伸缩检索索引：** 规模大到不可拷贝的 notebook（与 notebook 拷贝/分享判定同一阈值——
字节数或 chunk+node 行数超过配置上限）会自动构建/刷新检索索引，无需手动点按钮或跑 CLI：
在来源抽取完成后、KG 重建后，以及查询首次发现无索引时兜底触发。默认会排队到低峰窗口而非
立即构建。

```text
SCALE_INDEX_AUTO_ENABLED   # 为大库自动构建/刷新检索索引（默认 true）
SCALE_INDEX_AUTO_WHEN      # "idle"=排队到低峰窗口（默认）｜ "now"=立即构建
STARTUP_PRELOAD_SCALE_INDEXES # readiness 前加载全部已发布 scale 索引、启用 ANN 与安全的单索引 PPR core（默认 true）
SCALE_IDX_CACHE_MAX        # scale 索引常驻上限；开预加载时必须不少于存量有效索引数（默认 8）
SEARCH_CONCURRENCY_LIMIT   # 笔记本全文搜索的进程级并发上限，HTTP /search 路由与 MCP search_notebook_context 工具共用一个闸（默认 4，对齐前端集合页自身的搜索扇出档位）。等待发生在事件循环上（不占线程、不占数据库连接）且无超时——结果只会延后、绝不收窄。这是部署成本预算：POSTGRES_POOL_MAX_SIZE 较小的主机应调低；只有池有余量时才调高。
SCALE_BUILD_CONCURRENCY    # 进程内同时执行的 scale 索引 build/fold 操作上限（默认 2）。此前每个构建都是裸的无界 daemon 线程，低峰调度器可能把整条 idle 队列一次性全部起线程、在同机上打出一次内存/CPU 峰值；超出上限的构建会先阻塞在这道闸前，构建本身一旦真正开始执行，耗时不受影响。
SCALE_BUILD_FAILURE_BACKOFF_SECONDS     # 同一 notebook 的 scale build/fold 失败后，**自动**重跑（调度器/发布后 follow-up——不含用户显式点击「立即重建」）前的最短等待（默认 60）。指数退避：每次连续失败翻倍。
SCALE_BUILD_FAILURE_BACKOFF_MAX_SECONDS # 该指数退避的封顶值（默认 1800），让持续失败的 notebook 重试间隔越拉越开而不是无界增长，同时仍能避免背靠背重跑——一次又一次立刻撞上同样会失败的构建、白白占掉并发 slot。
```

开启启动预加载后，`/api/ready` 会在 `preloading_indexes` 阶段持续返回 false。任一必需
工件损坏，或存量已发布索引数超过 `SCALE_IDX_CACHE_MAX`，启动都会保持 not-ready，不把冷加载
转嫁给首位用户。应按全部常驻索引配置 cache 与 RAM。只有在需要进入 UI/维护流程重建损坏索引
时才临时设 `STARTUP_PRELOAD_SCALE_INDEXES=false`，修复后恢复。`scripts/backend.sh start` 默认等待
1,800 秒并打印 readiness 阶段变化；极慢磁盘可用 `START_TIMEOUT_SECONDS` 覆盖。

预加载边界覆盖可复用的落盘工件、ANN handle 和每个 ScaleIndex 的 self-only PPR
transition/chunk-id core。它刻意不在启动时物化每一种跨 notebook mounted 组合图：当前多
participant 拼接会复制完整 node map，并可能还原全部 CSR 边；对所有挂载组合这样做会让一个
千万节点图成倍膨胀直至启动 OOM。跨库组合图在获得有界/共享表示前继续按需构造。严格保证
只覆盖启动时已经发布的工件集合；运行中 build/fold 或新增第
`SCALE_IDX_CACHE_MAX+1` 个索引仍走既有在线发布路径，应在变更前扩容并重启以重新建立
readiness 保证。

**Notebook 拷贝 vs 只读分享——规模闸：** 分享一个 notebook 时,库足够小就给「深拷贝」,
否则给只读「加入」。「足够小」(以及上面那条不可拷贝阈值)是同一组界限——必须**同时**低于
全部三条才算可拷贝。深拷贝会把该 notebook 的**每一张表**读进内存做 id 重映射,所以最后一条
对这个总量单独封顶,与 chunk+node 数无关:

```text
NOTEBOOK_COPY_MAX_BYTES          # 源文件总字节上限（默认 50MB）
NOTEBOOK_COPY_MAX_ROWS           # chunks + 知识对象 行数上限（默认 5000）
NOTEBOOK_COPY_MAX_SNAPSHOT_ROWS  # 深拷贝将物化的「所有表」总行数上限(含 relations/embeddings/
                                 # elements/knowhow)——纵深护栏:图/向量扇出远超 chunk+node 数时
                                 # 不至于把拷贝 OOM;超过即改为只读分享（默认 200000）
```

**内容寻址缓存（LLM + 向量化调用）：**

内容完全相同的重复调用——同一模型、同一 prompt 或文本——直接复用上次结果，
不再重新请求模型；大规模重跑（例如对已处理过的库重新抽取）是主要受益场景。
缓存独立存放在自己的 SQLite 文件中，与主数据库分开。健康/可用性探测始终
绕过缓存，不会被一次缓存里的成功结果掩盖正在发生的模型服务故障。

```text
LLM_CACHE_ENABLED        # 内容寻址缓存总开关（默认 true）
LLM_CACHE_PATH           # 缓存文件路径（默认 .local/llm_cache_v2.db）
LLM_CACHE_SIZE_LIMIT     # 容量上限（字节）；超出后按最近最少使用淘汰（默认 2147483648 = 2 GiB）
LLM_CACHE_TTL_DAYS       # 条目最长保留天数，超期视为过期（默认 90）
```

*查看与清理缓存（仅管理员）。* 缓存键 = 模型名 + 请求内容原文，所以改 prompt、
换成另一个名字的模型都会自动失效，不需要做任何事。唯一需要手动清理的场景是
**同名模型背后的权重被替换**：模型名没变、缓存键就没变，旧答案会一直被回放到
90 天 TTL 到期为止。换过模型服务之后，把那个模型的缓存清掉：

```bash
# 看当前缓存现状：总量、命中率、按模型分布
curl -H "Authorization: Bearer $TOKEN" http://<host>/api/admin/cache

# 清掉某个模型的缓存（更换该模型服务之后执行）
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"tag": "<模型名>"}' http://<host>/api/admin/cache/evict

# 清空全部（必须显式给这个标志，没有「留空即全清」）
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"clear_all": true}' http://<host>/api/admin/cache/evict
```

第一条返回里的 `by_tag` 就是「有哪些模型名、各占多少条」，据此决定清哪一份。
清掉缓存永远是安全操作——下一次调用重新去问模型即可。

**检索 / KG 增强（GraphRAG + ToG-3 借鉴，Phase 1+2）：**

opt-in（默认关）与默认开混合。默认开：`ANSWER_CONTEXT_*`、`KG_QUERY_REFINE_ENABLED`，以及 KG 质量增强 `KG_REFINE` / `KG_GLEANING` / `KG_CONCEPT_DESC`。其余请**逐个开启**并用
评测脚本（`backend/app/eval`）验证——RRF + 重排 + 精炼三个全开会回归。

```text
KG_REFINE_ENABLED            # 抽取自校验：丢弃幻觉节点（默认 true）
KG_GLEANING_ENABLED          # 额外几轮让 LLM 找回漏抽节点（默认 true）
KG_GLEANING_ROUNDS           # 开启时的 gleaning 轮数（默认 1）
KG_CONCEPT_DESC_ENABLED      # LLM 融合跨文档概念簇描述（默认 true）
KG_COMMUNITY_SUMMARY_ENABLED # rebuild 期生成 LLM 社区报告（社区层；默认 false）
ANSWER_CONTEXT_BUDGET_CHARS  # 答案上下文装配字符预算（默认 6000；深度报告的节不再读取，见检索段的行为变化说明）
ANSWER_CONTEXT_MIN_ITEMS     # 不论预算至少保留 N 条（默认 3）
RETRIEVAL_RRF_ENABLED        # BM25(Okapi)+RRF 排序，替代关键词+语义融合（默认 false）
RETRIEVAL_RRF_K              # RRF 的 k（默认 60）
KG_QUERY_REFINE_ENABLED      # 答题前做问题感知证据精炼（默认 true）
QUERY_REFINE_MAX_CHARS       # 喂给精炼的证据最大字符数（默认 4000）
GLOBAL_MAX_COMMUNITIES       # 兼容保留；退役的 `global` mode 已是 `chunk` 别名，此值当前不被消费（默认 20）
RELATION_RETRIEVAL_ENABLED   # 图/推理种子的关系向量检索（默认 false，按需开启待评测）
RELATION_SEED_TOP_N          # 开启时喂入图种子的关系/节点命中数（默认 8）
KG_CANONICAL_FOLD_ENABLED    # 检索时折叠同 canonical 的碎片化 KG 节点（默认 false）
KG_ABOUT_DOWNWEIGHT_ENABLED  # 关系检索里对弱 about 边降权排序（默认 false）
KNOWHOW_KG_NODE_RETRIEVAL_ENABLED # Knowhow 格子对象进入 reasoning/graph 节点检索（默认 true；false 只关闭直接节点路径，不影响格子 chunk 检索）
REASONING_ENUM_TOOLS_ENABLED # 逐步推理的类型化集合枚举 reflect 工具，enumerate_elements/enumerate_kg_objects（默认 true；false 同时关闭两个工具与集合地图，零额外查询）
REASONING_OUTLINE_ENABLED    # 逐步推理的大纲便签 reflect 动作，update_outline（默认 true；不论此开关，仅「穷尽」检索档位提供该动作；false 关闭该动作与按节合成，回到接入前逐字一致的行为）；同一个开关也管深度报告每节深挖在穷尽档（depth 16，见下方 REPORT_MAX_SECTIONS）的启用，不另设报告专属开关
REASONING_OUTLINE_KG_GAP_ENABLED # 大纲便签的 KG 弱支撑边回喂：每次被接受的 update_outline 之后附带弱支撑关系提示（默认 true；叠在 REASONING_OUTLINE_ENABLED 之上；false 关闭后大纲便签不再附带弱支撑关系提示，零额外查询）；深度报告每节深挖到达穷尽档时同样生效
AGENT_PROFILE_ENABLED        # 「AI 对这个库的理解」总闸：同时管住 plan/reflect 注入、后台巡固触发与两个 API 面的可见性（默认 true；false 处处逐字回到接入前——不注入、不记 trace 步、不排巡固，API 返回 enabled=false 而非 404）
AGENT_PROFILE_BASE_TRIGGER   # 共享底座层（corpus_shape/key_entities/corpus_gaps）重新巡固前累计的来源变更次数（默认 5）
AGENT_PROFILE_OVERLAY_TRIGGER # 该成员私有覆盖层（retrieval_notes/usage_gaps）重新巡固前累计的已完成提问次数；已完成的深度报告直接达阈（默认 10）
AGENT_CALL_LOG_ENABLED       # Agent 每次经 MCP 落到某个笔记本上的工具调用记一行（哪个 Agent、什么时候、按哪一档能力），只有该成员自己能在「Agent 记录」里查看与清空（默认 true；false 时零写入，判据在开事务之前）。**叠在 AGENT_PROFILE_ENABLED 之上**而不是与它并列：这份记账唯一的读处就是那个面板，而总闸关掉时它的入口按钮一个节点都不渲染，所以总闸关着还记就是在攒没人打开得了的行。读与清空**两把闸都不跟随**——关掉它是「从现在起不记」，绝不是「把已经记下的藏起来或冻住」。记账仍然既不进 prompt（巡固读取在 SQL 里钉死 kind='note'），也不触发任何巡固
RETRIEVAL_EXPERIENCE_ENABLED # 部署级全局检索策略经验库（Agentic Memory P2）的蒸馏总闸：是否读取已完成提问并蒸馏进 retrieval_experiences（默认 true——部署可以只蒸馏、只观测而从不注入，见下面的 RETRIEVAL_EXPERIENCE_INJECT_ENABLED）
RETRIEVAL_EXPERIENCE_INJECT_ENABLED # 同一份经验库的独立注入闸：蒸出的块是否会被加进 plan/reflect prompt（默认 **false**——先攒够观测数据再决定是否开启；关闭时在注入侧逐字等于该特性不存在：不读表、不拼块、不记 trace 步）
REASONING_CONSULT_MEMORY_ENABLED # consult_memory reflect 动作（Agentic Memory P4）的按场景 kill switch（纵深防御）；这个动作真正的可用性闸是「retrieval_effort 为 deep/thorough/exhaustive 之一 且 RETRIEVAL_EXPERIENCE_INJECT_ENABLED 也开着」——单独把这个开关打开、注入闸仍关着时，动作不会出现（默认 true）
REASONING_MAX_CONSULT_MEMORY # 每 run 的 consult_memory 调用次数上限（默认 2；ge=0）
RETRIEVAL_EXPERIENCE_TRIGGER # 蒸馏一批前需累计的已完成提问数（部署级全局，跨所有笔记本与用户；默认 40；ge=1）
USER_SEARCH_PROFILE_ENABLED  # 每用户检索/回答风格偏好文档总闸（Agentic Memory P3 B 线）：后台归纳、Ask 规划/答案注入、`PATCH /me/search-profile` 可写性都由它决定（默认 true；关闭后注入/写入两侧处处逐字回到接入前——不归纳、不注入、`PATCH` 409——但 `GET /me` 仍照常返回该行上已存在的取值，不会伪造成 `search_profile: null`）
USER_SEARCH_PROFILE_TRIGGER  # 确定性、零 LLM 的 `answer_language` 归纳任务再次运行前，该用户需累计的已完成提问数（默认 20；ge=1）
CHUNK_RECALL                 # chunk 大召回数（默认 200；mix 候选池 / 无 rerank 时 MMR 候选）
LEXICAL_LANGUAGE_GATE_ENABLED # 语料采样中没有任何 CJK 字符时，丢弃纯 CJK 的词法词项（默认 true；这些探针对该库保证零命中，却各买一次真实的 PostgreSQL LATERAL 探针——7,026 块的英文库实测：64 词项冷 29.7s / 3 词项暖 0.26s，返回同样的 26 行，未过滤形态在报告多节并发下会直接超时。绝不过滤用户引号短语与整句词项，不做拉丁方向，也不作用于选定来源的运行——那条路的词法臂是它唯一的候选来源。设 false 回到接入前逐字一致的行为，用于某库语言采样误判时的临时恢复）
POSTGRES_LEXICAL_KNN_ENABLED # 主导规模 PostgreSQL 库上 KG 名词法探针的 GiST `<->` KNN 早停（默认 true，设 false 回滚；仅 PostgreSQL——SQLite 适配器声明不具备该能力，判定零成本短路）。需要一个形状匹配的 GiST trigram 索引（覆盖知识对象名表达式）——按**精确**形状探测（单 gist_trgm_ops 键、名表达式、`COLLATE "C"`），运维已建的同形索引直接生效；缺索引时旧语句原样运行——结果层面默认开对未建索引部署零差异;成本层面每次未收窄探针为规模判定付一条已索引的单行版本查询（亚毫秒,与其后的词法 LATERAL 相比是噪声,登记接受）。只对未按来源收窄、且规模 ≥ POSTGRES_LEXICAL_KNN_MIN_ROWS 的库生效。分数仍是 `similarity()`；等相似度并列类内 KNN 路径不仅可能与 legacy 取不同成员，其**自身也不是 run-to-run 稳定的**（并列成员随 GiST 遍历序变化；9.1M 行 base 实测：285 行同名 "DAC" 的 sim=1.0 类）——登记接受的取舍；需要位稳定候选集的部署设 false。同库实测：常见短词单词项 7.4s → 123ms（60×）。索引 DDL 与上线/回滚步骤见运维文档。
POSTGRES_LEXICAL_KNN_MIN_ROWS # KNN 路由的库规模下限（nodes+chunks，默认 500000）。GiST 索引没有 notebook 键，KNN 走**全库**距离序逐行过滤——只有在整张表里占主导份额的库才划算；小份额库要在别人的行里翻找自己的候选，反而丢掉复合 GIN 快路径。请设在「除主导库外最大的那个库」之上（实测部署：主导库 1.1e7、次大 2.7e4，默认值干净分割）。低于下限走旧语句，逐字不变。
CHUNK_MMR_K                  # 无 rerank 时 MMR 精选 chunk 数（默认 16）
CHUNK_KG_OVERLAY_ENABLED     # chunk×graph mix：叠加 KG 局部结构+源 chunk（默认 true；rerank 路径需绑定 `retrieval_rerank`）
RERANK_MAX_DOCS              # 单次 rerank 文档上限，超出自动切 batch 并发（默认 500）
MAX_ENTITY_TOKENS            # mix KG 实体段 token 预算（默认 6000）
MAX_RELATION_TOKENS          # mix KG 关系段 token 预算（默认 8000）
MAX_TOTAL_TOKENS             # mix 总上下文 token 预算（默认 30000）
REPORT_MAX_SECTIONS          # 深度报告大纲：最大章节数（默认 6）
REPORT_MAX_SUBQUERIES_PER_SECTION # 每节检索方向合同，API/UI 同步使用
REPORT_PROBE_ELEMENT_LIMIT   # 规划与方向补检索的直接元素候选数
REPORT_SCOUT_KG_LIMIT / REPORT_SCOUT_CHUNK_LIMIT / REPORT_SCOUT_MEMORY_LIMIT # 语料地图侦察宽度
REPORT_SECTION_CHUNK_BUDGET  # 深度报告：每节 chunk 上下文字预算（默认 20000；仅对不带研究深度的调用方生效，见检索段的行为变化说明）
REPORT_GENERATION_CONCURRENCY # 深度报告：每个后端进程同时准入的整篇报告数（默认 1；排队时不占数据库连接）
REPORT_SECTION_CONCURRENCY   # 深度报告：每篇已准入报告的节级扇出（默认 5；还受模型容量和数据库连接池余量约束）
REPORT_RETRIEVAL_FANOUT      # 深度报告：规划/生成 run 共用的叶子 KG/chunk/element/PPR I/O 扇出（默认 8）
REPORT_PROBE_CHANNEL_CONCURRENCY # 规划探针：独立 KG/原文元素通道的并行度（1..2，默认 2）
REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS / REPORT_SUFFICIENCY_MIN_FAMILIES / REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES / REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE # 集中的报告充分性规则；默认保持历史判定，精确护栏见 product-and-api
REPORT_SECTION_MAX_TOKENS    # 深度报告：每节撰写 completion 上限（默认 65536）
REPORT_SYNTHESIS_MAX_TOKENS  # 深度报告：全篇 JSON 蓝图 completion 上限（默认 102400）
REPORT_SUMMARY_MAX_TOKENS    # 深度报告：最终只读终审 completion 上限（默认 102400）
REPORT_ALLOW_PARAMETRIC      # 深度报告：允许【通识】层（库外通识，行内标注且提示未经验证，默认 true）
REPORT_HIGH_RISK_DOWNGRADE_ENABLED # 深度报告高风险引证审计超阈值时是否把 grounded 章节封顶为 overview（默认 false；关闭时披露仍运行）
REPORT_HIGH_RISK_UNSUPPORTED_RATIO # 深度报告高风险引证审计阈值；数值契约只在 docs/product-and-api_zh.md 维护
REASONING_MAX_PPR_RETRIEVES / REASONING_MAX_EXACT_LOOKUPS / REASONING_MAX_FOLLOW_CHAIN_ACTIONS / REASONING_COMMUNITY_PEERS_CAP_FACTOR / REASONING_MAX_OUTLINE_UPDATES # 集中的 reasoning 动作/扩展护栏；默认保持历史行为，精确护栏见 product-and-api
```

三个 `REPORT_*_MAX_TOKENS` 是 completion 上限，不是总上下文声明，也不会预占输出。
prompt + completion 的兼容性由所绑定 provider/model 负责；部署时必须确认它能在对应
workload 的最大 prompt 下接受这些上限。若 provider 的输出或总窗口更小，应下调相应值。

**行为变化（PR-5，不新增开关）：** 每节深挖的检索预算现在按报告自己的 `depth` 值（1/2/4/8/16，接口侧夹在 `[1, 16]`）映射到与逐步推理相同的档名（`overview`/`standard`/`deep`/`thorough`/`exhaustive`），不再永远按 `standard` 预算跑。低档位因此比这次改动前检索预算更小、高档位更大——这是把同名档位对齐（同一档名在 Ask 与深度报告两处买到同一份预算）的有意修复，不是回归。到达 depth 16（`exhaustive`）时，该节深挖内部还会额外激活上文的大纲便签与 KG 弱支撑边回喂；完整合同见 `docs/product-and-api_zh.md`「深度报告接入大纲共演化」一节。

**两层知识库与图推理（Wave 1+2）：** 目前没有 `.env` 开关。notebook 的 `tier`
（`base` | `personal`，默认 `personal`）是 notebook 行上的数据，通过仓库方法
`mark_notebook_base()` 设置；把一个 notebook 发布为 `base` 并不会让它自动全局共享——
其它每个 notebook 都必须显式把它挂为参考库（持久化在 `notebook_bases`，经
`GET`/`PUT /api/notebooks/{id}/bases` 管理、`GET /api/notebooks/{id}/mountable` 发现候选）
之后，它才会加入该 notebook 的检索参与集。tier 感知联合检索不改相关度分数：相关度是
第一排序键，`base` 仅在参与集内命中相关度分数完全相同时作为第二排序键。答案里的 base
优先冲突规则是独立的合成策略，对来自已挂载 base notebook 的证据始终生效。
可选的图推理 Ask 模式（`mode="graph"`）多跳遍历用固定默认 `max_depth=3`、`max_fan_out=8`
（经 `getattr` 读取 settings，因此将来加 `GRAPH_MAX_DEPTH` / `GRAPH_MAX_FAN_OUT` env 覆盖无需改代码）。
边可信打分、策展审核队列、个人→基准晋升同样是行为，不由 env 控制。

**用户系统：**

```text
SILICON_NOTEBOOK_ADMIN_PASSWORD   # admin 登录密码（本地默认 "admin"；production/对外监听
                                  # 必须配置非默认值）
SILICON_NOTEBOOK_AUTH_OPTIONAL    # true = 无 token 请求回退为 admin（仅本地/测试）；
                                  # false（默认）= 所有请求必须登录
AUTH_SESSION_TOUCH_INTERVAL_SECONDS # session 滑动续期写库间隔（默认 300 秒）
```

**MinerU（PDF 解析）：**

```text
MINERU_MODE             # off（默认） | http | cli
MINERU_API_URL          # 远端 mineru-api 端点（http 模式）
MINERU_BACKEND          # pipeline | vlm-auto-engine | vlm-http-client | vlm-sglang-client
MINERU_VLM_SERVER_URL   # 独立 VLM 推理服务器 URL
MINERU_PARSE_METHOD     # auto | txt | ocr
MINERU_LANG             # 如 en、ch
MINERU_MODEL_SOURCE     # huggingface | modelscope
MINERU_TIMEOUT_SECONDS  # MinerU 调用超时
MINERU_MAX_RETRIES      # 瞬态 HTTP 失败的额外尝试数，0..5（默认 2，即总共最多 3 次）
MINERU_FORMULA_ENABLE   # true/false
MINERU_TABLE_ENABLE     # true/false
MINERU_RETURN_IMAGES    # 是否保留来源图片资产，含 PDF/DOCX/PPTX/XLSX 与 Markdown data-URI/ZIP 图片（默认 true；设 0/false 仅保留文字与图注）
MINERU_MAX_IMAGE_BYTES  # 单张内嵌图片大小上限（默认 5MB，超出丢弃）
MINERU_MAX_IMAGES_PER_SOURCE # 每个来源最多保留的内嵌图片张数（默认 200）
```

解析路由由后端唯一注册表声明，并经登录后的系统配置响应投影。顺序固定为：优先已配置的
自托管 MinerU；只有没有自托管路径时才允许公共云；内置解析器保留为按格式兜底。浏览器只会
收到能力、执行边界、可用状态与固定原因枚举，绝不收到 endpoint 或凭证。

**生成问题 rollout（可选检索补充）：**

```text
GENERATED_QUESTION_INDEX_MODE
GENERATED_QUESTION_QUESTIONS_PER_CHUNK
GENERATED_QUESTION_TRIGGER_HITS
GENERATED_QUESTION_RECALL
GENERATED_QUESTION_MAX_SCAN_ROWS
```

除非运维人员明确在构建/评估该索引，否则 mode 保持 `off`。先用 `shadow` 做只含计数的 A/B，
再考虑 `on`；精确默认值与边界见产品合同，离线命令见运维文档。

`MINERU_MAX_RETRIES` 由自建 `MINERU_MODE=http` 适配器与 mineru.net 云端请求共用，
覆盖 URL 提交/轮询/结果下载以及签名文件上传。默认按 1 秒、2 秒做有界指数退避，
只重试网络/超时、HTTP 408/425/429/5xx 和空响应或非 JSON 响应；明确的 4xx、解析
终态失败和业务拒绝不重试，`MINERU_MODE=cli` 本地子进程仍只执行一次。
适配器达到终态失败（或没有产出可用元素）后，来源摄取会回退本地 PyMuPDF4LLM；
URL 来源会先下载已经过安全校验的公开 PDF。降级成功的来源仍为 `extracted`，客户端
只收到安全的 `parse_quality_warning`，之后可重新解析；只有 PyMuPDF4LLM 自身缺失或
报错时才使用 pypdf 作最后兜底。

`MINERU_RETURN_IMAGES` / `MINERU_MAX_IMAGE_BYTES` / `MINERU_MAX_IMAGES_PER_SOURCE`
同样作用于 Markdown 来源里的 `data:image/...;base64,...` 内嵌图片：这三项是所有
来源图片持久化的统一护栏，不只管 MinerU 解析出的文档。

**日志：**

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS
MODEL_JSON_REPAIR_MODE  # off | shadow | on（默认 on）
EVENT_LOG_ENABLED / EVENT_LOG_DIR
SLOW_REQUEST_MS         # 超过该毫秒数的请求标记 SLOW（默认 3000）
SILICON_NOTEBOOK_CORS_ORIGINS
```

`.env.example` 是非服务变量与密钥槽位的权威清单，`model-services.example.toml` 是服务、绑定与容量模板；上面分组只列常用项。推理专用模型通过 TOML 把 `reasoning_agent` 绑定到独立服务，其护栏仍是 `REASONING_MAX_STEPS`、`REASONING_MAX_SUBQUERIES`、`REASONING_TIMEOUT_SECONDS`、`REASONING_MAX_RETRIES`。其余可调项还包括检索/接地参数（`PROC_MIN`、`EVIDENCE_TAU_LOW`、`EVIDENCE_TAU_HIGH`）、可选调试日志查看器（`DEBUG_LOGS_ENABLED`）和运行身份（`SILICON_NOTEBOOK_ENV`、`SILICON_NOTEBOOK_SINGLE_USER_EMAIL`、`SILICON_NOTEBOOK_SINGLE_USER_NAME`）。

`MODEL_JSON_REPAIR_MODE` 只作用于 `reasoning_agent` 与 `ask_answer`。`off` 保持严格拒绝，
`shadow` 记录响应是否可安全修复但仍拒绝，`on` 接受保守修复（默认）。它不会补全被截断的
输出，也不会放松 schema、类型或正文安全校验；修复事件不含业务内容，并用模型调用的安全
`support_id` 做关联。

同源 `/api/*` rewrite 存在有限的代理 idle timeout，因此 Ask 每 5 秒发送一条不含业务内容的
空白 NDJSON 心跳并返回禁缓冲 header；ingress 不应缓冲 `application/x-ndjson`。这只能处理
idle timeout。CDN/负载均衡若设置了总请求时长硬上限，部署者仍须把它调到最长 Ask run 之上，
或在断连后通过已持久化 job 重新打开完成的会话。

所需 chat workload 未绑定时，摘要和回答退化为 deterministic 行为；source 解析仍会完整执行，KG 抽取阶段记录完成的 `no-llm` run，不生成合成知识。

### 部署插件（EXTENSIONS_CONFIG）

`EXTENSIONS_CONFIG` 未设置＝零部署插件，装载出的拓扑与内建组合逐字一致。设置了但不可读或解析不了（文件缺失、TOML 语法错误、未知键、条目格式非法）是**启动失败**——进程直接不起，绝不降级。离线 CLI（`batch_ingest.py` 等）装载的是同一套插件拓扑，所以修复办法是改配置本身,绝不是清空变量——清空只会静默换成另一套发现/注册组合,而不是恢复接入插件之前的行为。

```toml
[extensions."corp.ieee_search"]
bundle = "silicon_notebook_ieee.bundle:BUNDLE"
enabled = true

[extensions."corp.ieee_search".settings]
```

每条配置遵守三条铁律：只加载**点名列表里、且未 `enabled = false`** 的插件——不扫描目录、不读 entry points、不看第二个环境变量；插件自带的 pydantic `settings_model` 会把未知键或类型错误判为启动失败（可接受键集合由 core 自己从模型算出，不指望插件写 `extra="forbid"`；带 `alias` 的字段**只按 alias** 接受，与 pydantic 自身的默认行为一致，除非模型设了 `populate_by_name`/`validate_by_name`），所以密钥应该经一个环境变量名字段引用（同 `model-services.toml` 的 `api_key_env` 约定），而不是把明文值直接写进配置,任何 settings 值都不会进日志、事件或 `GET /api/admin/extensions`;插件包装进与后端**同一个** `PYTHON_BIN` 环境,不是独立解释器。插件的 `configure()` 必须廉价且无副作用——不起线程、不开网络/数据库连接、不做阻塞 I/O,这类工作留到首次真正用到时再惰性执行。插件 capability 名只能用点/下划线/短横线分隔(`:` 留给 core 自己的 `point:name` capability)。启用、停用或升级插件一律靠重启进程,没有热加载。

运维方用下面这条命令自查某次部署的实时插件拓扑是否与配套前端构建一致:`EXTENSIONS_CONFIG=/etc/silicon/extensions.toml PYTHONPATH=backend python3 scripts/check_deployment_extension_parity.py --frontend-contract frontend/.local/ui-extension-contract.json`(退出码 `0` 对等 / `1` 漂移 / `2` 用法或环境错误)。插件包应对自己的源码跑一次 `python3 scripts/check_ui_vocabulary.py --extra-root <插件源码目录>`,拿到与 core 自己同等的中文界面文案保证。重新生成 `scripts/generate_ui_extension_contract.py` 时必须清空 `EXTENSIONS_CONFIG`——它提交的 fixture 只反映内建拓扑,绝不能带上某次部署的插件。 逐步的开发、联调与运维流程见 [`docs/deployment-extensions-sop_zh.md`](deployment-extensions-sop_zh.md)。
