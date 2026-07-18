# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是一个面向半导体研发团队的 knowhow notebook 平台。它把上传的技术文档转化成可查询的知识图谱（Concept / Claim / Formula / Procedure 对象），并提供元素级 evidence 引用与接地多轮问答。

## 当前范围

当前仓库已进入以 KG-native 管线为核心的本机真实 beta 闭环：

- Python FastAPI 后端；SQLite 持久化路径 `.local/silicon_notebook.db`
- `frontend/` 下的 Next.js / React / TypeScript 前端
- OpenAI-compatible LLM 端点，用于 KG 抽取、接地回答和深度报告；embedding 通过 `EMBED_*` 独立配置
- 未配置 LLM/embedder 时全管线可离线运行（deterministic fallback）
- 干净起点：全新数据库只初始化本机用户，不预置 demo 笔记本或合成来源
- 支持 PDF、Markdown、DOCX、PPTX、CSV、XLSX 的 multipart 文件上传（经共享 KG job scheduler 异步执行）
- **KG-native 摄取**：结构化 Markdown 解析 → 贪心窗口化 KG 抽取（Concept / Claim / Formula / Procedure）并发 embedding → 抽取优先状态（`extracted` = KG 就绪，不等 embedding）
- PDF/DOCX/PPTX 走 MinerU（公式/表格/版面、内嵌图片）；本机或未配置时回退 pypdf（仅纯文本）
- MinerU 抽取的内嵌图片在来源正文内联展示；图注与文字保持可搜索
- 混合检索：CJK 感知 bi-gram 关键词 + float32 矩阵语义检索（每 notebook 独立缓存）
- KG-native 接地问答：逐句 `[k_i]` 引用（渲染为紧凑编号引用；模型直接输出的数字复合引用如 `[1, 2, 3]` 在能映射到已知引用时也可点击）、多轮会话、1-hop KG 邻居扩展，推理模式实时显示可展开的一行 agent 轨迹
- **推理模式的类型化查询期推导：** agent 可调用 `follow_chain`，把有证据的两跳 `A→B→C` 临时组合成 `A→C`；首版只允许 `derived_from / kind_of / prerequisite_of / precedes / part_of`。两条直接关系各自保留可引用的关系证据；被拒绝、无 quote、类型或 `validity_scope` 冲突的路径 fail-closed；推论明确标作「推断」，且绝不写回 KG。该能力不新增 migration、索引或历史回填；查询只对既有 source/target 索引做有界抽样，高度节点无法在预算内确认时直接放弃推论。
- 两层知识库：每个 notebook 带 `tier`（`base` | `personal`，默认 `personal`）。`chunk` 基线只从当前 active notebook 读取 chunk；可选 KG overlay / PPR 才可能加入 federated KG 上下文与 base-backed chunk，`graph` / `reasoning` 使用 federated KG 路径。exact-score 的 `base` 次序只适用于知识对象命中：`federated_retrieve()` 不改相关度分数，分数更高的 personal hit 仍排在前面；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。回答合成阶段另有独立规则：当 base 与 personal 证据冲突时，以 base 立场为准并指出差异。引用携带其 tier（`AnswerAnchor.tier`），Ask 在每条引用上渲染 `base`/`personal` 标记。
- **用户系统**：自助注册（用户名规则：单个字母 + `00` + 6 位数字，如 `a00123456`，存储为小写）+ 密码登录，使用不透明 Bearer 会话 token。每个 notebook 由其创建者所有；用户库包含自己拥有的 notebook，以及主动加入的大型只读共享 notebook。首次启动时自动创建内置 `admin` 账号（登录用户名 `admin`，密码来自 `SILICON_NOTEBOOK_ADMIN_PASSWORD`，本地默认 `admin`；production/对外监听必须修改）；admin 持有原有 notebook 并是唯一可将 notebook 标为基准库的用户。基准库 notebook 对普通用户的列表隐藏，但问答时仍作为权威检索上下文使用。本地/测试场景可设置 `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` 跳过登录。前端在首次加载时显示登录/注册界面，顶栏展示已登录用户名和退出按钮。
- **分享链接**：owner 可发布不透明 notebook 链接；小 notebook 复制到接收者账号，大 notebook 以只读成员方式加入。写权限仍归 owner；当前没有实时协同编辑或修改密码流程。
- **绑定 notebook 的私有 Memory**：用户可手动把 Ask 回答生成可编辑预览，并在确认后沉淀为可复用 Memory。外层提供用户级总 Memory 页面，notebook 卡片显示当前用户的数量，工作区为 **问答**（Ask） | **知识库**（Knowledge） | **记忆**（Memory） | **深度报告**（Deep Report）。外部 Agent 可经 MCP 提交 `candidate`；它只在同一用户、同一 notebook 的获授权 Agent 间共享，用户确认前不会进入正式 Ask/搜索/报告检索。
- 可选图推理问答模式（`mode="graph"`，opt-in / 实验性）：基于 `knowledge_relations` 构建 rustworkx 内存图，做有界多跳 derivation/support 链遍历，答题时做对抗式链路校验并给出最弱环 `chain_trust` 分（默认 Ask 仍为 `chunk`）
- 深度报告（两阶段后台任务）：notebook 级「深度报告」动作把一个问题变成多节技术报告。**阶段1（秒级）**:STORM 式多视角规划器——先做零 LLM 语料侦察（来源标题 + KG 命中 + chunk 出处,大纲不再盲规划）——预写出大纲,每节带**专家视角 / 跨视角张力 / 证据充分性判定**（充足/薄弱/缺失 + 缺口说明,来自零 LLM 检索探针 + rewrite 模型上的 Judge）;用户在**大纲编辑器**里审阅/修改后再确认。**阶段2（几分钟,确认后）**:每节独立跑一次完整 `reasoning` 深挖（节间并行,各自独立检索预算）,按三层证据纪律撰写（`[k]` 库内引用 /（推断）库内推断 /【通识】库外通识，行内标注且提示未经验证），最后汇总加执行摘要、参考文献，以及（仅当某节缺库内支撑时）结尾一行「局限」说明。研究深度控件为五个命名档「概览/标准/深入/详尽/穷尽」（默认「标准」，= 每节 reflect 步预算，在生成按钮旁弹出选择）用充分程度换时延；章节按 `KG_JOB_CONCURRENCY` 并行深挖，前端显示逐节实时进度（`section_status`）。以可取消的后台 job 运行；每份报告可下 `.md`，或多选批量下 `reports.zip`
- 边可信与治理：每条边的可信信号（evidence / 同源佐证 / 类型合法性）+ 高风险边优先的审核队列；被审核拒绝的边从图推理中排除
- 知识治理：通过 `/knowledge-types` + `/knowledge?type=...` 浏览任意对象类型，状态生命周期，重复检测与合并；`deprecated` 对象从检索和 1-hop 扩展中排除。个人→基准节点晋升（propose → under_review → approve/reject），批准时去重入库，配套策展晋升队列
- 统一 KG：跨文档概念聚类（`concept_clusters`），待合并审核
- Object 级 KG 可视化：Concept / Claim / Formula / Procedure 节点，类型形状、边标签、多选过滤、按类型分组侧栏
- Notebook 集合页（网格/紧凑/列表、编辑/删除）；点击「＋ 新建」直接创建 `Untitled notebook` 并进入，无弹窗
- 第一版不使用 Docker

PostgreSQL + pgvector 仍是后续生产/团队 beta 目标，当前本机开发不需要。

## 架构边界

- `SQLiteRepository` 是组合式 `RepositoryRuntime` 之上的兼容 facade。application service 不拼装主业务库 SQL。store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。store 共享一个 `SqliteDatabase` 连接工厂、写锁与版本闸 `SqliteMigrator`；service 保留顺序与策略。facade 每个操作要么是显式兼容 adapter，要么是源码守卫验证的单跳委托，真实目标必须与 ownership manifest 一致。消费者依赖 `backend/app/repositories/ports.py` 中可执行、按消费者划分的小型 Protocol；依赖方向单向——facade → runtime → services → stores → SQLite——未来 PostgreSQL adapter 只需在同一 ports 后替换 store 层，调用方不动。`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim，请求 Context、`_COPY_CHUNK`、`_remap_json_ids` 等旧导出继续可 import。
- `RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有；完成组合后替换受支持的兼容属性时，所有已持有它们的消费者都会同步更新。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再把提交异常重新抛出；成功 worker 的次序与既有 Ask 事务 checkpoint 不变。
- 重构前创建的数据库可原样加载。`scripts/verify_repository_snapshot.py` 使用精确的逐版本 migration manifest 与稳定 seed manifest，对 SQLite URI 路径做百分号编码，只在临时 backup 上构造 repository；cleanup 失败时只报告保留的 backup 路径，不输出私有行。它校验原 DB/WAL metadata 以及 SHM 的存在性和大小；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。

当前 schema 版本为 15。已提交的 v9 兼容 fixture 会经由既有 v10 migration、v11/v12 SQLite 热路径索引 migration、v13 Memory/Agent migration 与 v14/v15 Memory 派生源 link/index migration 升级，并保持可读。
- `frontend/app/page.tsx` 只承担 notebook workspace 编排，不再持有全部共享模型和面板实现。API/视图类型与常量位于 `workspace-model.ts`，答案/引用/推理轨迹位于 `answer-panel.tsx`，图谱和答案共用的类型标记位于 `kg-type-mark.tsx`。
- 结构回归测试会阻止这些职责重新复制回巨型文件。后续拆分沿用同一增量方式：保持端点与用户行为不变，每次只迁移一个高内聚领域，然后运行完整离线门禁。

## 部署

silicon-notebook 以两个进程运行——FastAPI 后端 + Next.js 前端——数据落在本地 SQLite。
**无需 GPU、无需数据库服务、无需本地模型服务**。LLM、嵌入和 rerank 仍只通过 URL 服务访问；MinerU 则独立支持
远端 HTTP（`MINERU_MODE=http`）、同机隔离子进程（`MINERU_MODE=cli`）或 pypdf 回退
（`MINERU_MODE=off`）。未配置模型服务或 MinerU parser 时，整条管线以确定性回退离线运行。

### 前置条件

- **Python ≥ 3.11**
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
```

服务在全空配置下即可启动——确定性离线模式(仅关键词检索,无 LLM 抽取/作答)。要启用完整
能力,至少填:

- **LLM**(KG 抽取、作答、深度报告)—— `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` /
  `OPENAI_COMPAT_MODEL`;任意 OpenAI 兼容端点。
- **嵌入**(语义检索;否则仅关键词)—— `EMBED_PROVIDER=dashscope` 加 `EMBED_MODEL` /
  `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_DIM`(必须等于模型输出维度)。可选
  `EMBED_RUNTIME_DIM`(默认 `0`=关)把相似度空间截断到前 N 维 + re-normalize(MRL),
  使进程内矩阵 / ANN 内存约 `EMBED_DIM/N`× 缩减,而库内原生向量保留为真相源。开关它需
  重建 scale 索引,见 [docs/runtime-dim-truncation-runbook.md](docs/runtime-dim-truncation-runbook.md)。
  **切勿改小 `EMBED_DIM` 来降维** —— 那会把全部存量向量当异维丢弃。
- **PDF 高保真**(可选)—— 一个 MinerU 端点,见 [用 MinerU 解析 PDF](#用-mineru-解析-pdf);
  保持 `MINERU_MODE=off` 则走 pypdf 文本兜底。

`.env.example` 是权威、逐项带注释的完整变量清单;[配置](#配置)按组列出常用项。

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

**没有迁移 / seed 步骤**——首次启动时后端会自建 SQLite 表结构,并创建 `.local/storage`
与 `.local/logs` 目录,只 seed 本地用户。后端务必**不带 `--reload`**:reload 重启会杀掉
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

`npm run start` 调用 `scripts/prod.sh`:前端 `next build` + `next start`,后端
`uvicorn --workers 1`,两者日志都落 `.local/logs/`。设 `SKIP_BUILD=1` 可复用已构建好
的 `frontend/.next`(如预构建镜像场景)。可用 `BACKEND_HOST` / `PORT` / `FRONTEND_PORT`
覆盖监听地址/端口。后端默认只监听 `127.0.0.1`；显式绑定非 loopback 地址时必须
配置非默认 `SILICON_NOTEBOOK_ADMIN_PASSWORD`，否则启动直接失败。

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
上传或排查卡住的 source 见[可观测性 / 日志](#可观测性--日志)。

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
vi .env         # 填模型服务 URL(同 2 · 配置)
./start.sh      # 便携 node 跑 standalone 前端 + venv 的 uvicorn 后端
./stop.sh       # 停止两者
```

打包机可配置项:`NODE_VERSION` / `NODE_DIST_URL` / `NODE_TARBALL`(便携 Node 来源)、
`SKIP_WHEELHOUSE=1`(改为目标机在线装依赖)、`PIP_INDEX_URL`、`PACK_PYTHON`。目标机可配置项:
`PYTHON_BIN`、`PIP_INDEX_URL`、`FRONTEND_HOST` / `FRONTEND_PORT` / `BACKEND_HOST` / `PORT`。
打包机的 Python **小版本**应与目标机一致,否则预编译 wheel 装不上(install.sh 会自动回退在线
安装)。目标侧细节见包内 `DEPLOY.md`。

## 产品流程

外层页面为 notebook 集合页（KG-native 管线）：

1. 点击「＋ 新建」——系统立即创建 `Untitled notebook` 并进入，无弹窗。
2. 上传 PDF、Markdown、DOCX、PPTX、CSV 或 XLSX 来源（multipart）。
3. 后端（异步后台作业）：结构化 Markdown 解析 → 分块 + 向量化——源处理完即可做 chunk-native 问答。
4. **KG 抽取按需触发**（见下方「KG 抽取触发」）：摄取期仅当该 notebook 已有 KG、或 `KG_AUTO_EXTRACT=true` 时才抽。抽取发生时经**全局抽取池**并发——窗口并发由 `KG_EXTRACT_WORKERS` 跨所有文档封顶、文档并发由 `KG_JOB_CONCURRENCY` 控制——抽完的新源随后增量融入统一 KG。
5. 知识对象写入 `knowledge_objects` + `knowledge_relations`，并绑定元素级 evidence。
6. 混合检索（bi-gram 关键词 + float32 矩阵语义）驱动 KG-native 问答：答案含逐句 `[k_i]` 引用，支持多轮会话，并沿 KG 关系做 1-hop 邻居扩展。
7. 统一 KG 跨文档聚合概念；待合并的跨文档概念对可逐一确认或拒绝。

进入单个 notebook 后：

- 顶栏：左上角只保留可编辑 notebook 标题；notebook 描述在没有对话时显示到问答欢迎态里，顶部工具栏在桌面宽度下保持各动作标签完整。
- 左栏：用户导入来源文件，实时显示 parse-status（绿色仅给 `extracted`，其余处理中为橙色），支持详情预览和删除。网络来源检索暂不开放。
- 主栏：四个 tab——**问答**（Ask）、**知识库**（Knowledge）、**记忆**（Memory）、**深度报告**（Deep Report）。Ask 提供逐句 `[k_i]` 引用、三种检索模式、多轮会话、实时推理轨迹与反馈；Knowledge 负责动态类型浏览与治理；Memory 只显示当前用户绑定在此 notebook 的私有记录；Deep Report 负责两阶段报告、大纲审阅、进度、导出、取消和删除。问答输入框中 `Enter` 发送，`Shift+Enter` 保留换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制。transport 断连只停止向当前客户端继续推送；导航、刷新或 transport 丢失后 detached Ask job 仍在后台运行并可保存最终回答。用户点击中断则调用 `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel`，由后端设置取消事件，使 worker / LLM 路径停止，且不保存被取消的最终回答。主工作区保持两列且没有固定 Studio 右栏。
- 知识图谱以全屏浮层打开：object 级 KG 节点（Concept / Claim / Formula / Procedure），类型形状，边关系标签，多选类型过滤，按类型分组侧栏（选中节点聚焦画布）。侧栏的「出处」以结构化证据卡片展示，长标题、位置、公式与中英混排正文会在面板内换行。
- 「分析」菜单本身只包含晋升队列（admin）、基准库/个人层切换（admin）与边审查队列。看板、Schema、全屏知识图谱是其他顶栏动作；当前不再暴露已退役的内容生成或派生规则动作。

知识对象类型的显示名只有一份真源：后端 `app/services/extraction_profiles.py` 的 `OBJECT_TYPE_LABELS`，由 `GET /notebooks/{id}/knowledge-types` 以 `KnowledgeTypeCount.label` 下发给前端。凡是拿得到这个 API label 的调用点——Knowledge 浏览器的类型 tab 与条目——一律直接使用它，因此用户自定义类型（例如 knowhow 表列名投影出来的类型）同样能显示正确的中文名。只拿得到 `object_type` 字符串的调用点——引用浮层与知识图谱画布/侧栏——回落到前端内置小表 `frontend/app/kg-type-mark.tsx` 的 `KG_TYPE_LABELS`，该表逐字等于后端常量；`scripts/check_object_type_labels_contract.py` 作为硬门挂在 `scripts/check.sh` 里，两份一旦漂移即构建失败。未知/自定义类型一律原样显示其 `object_type`，绝不 TitleCase 成臆造的英文。这两张表的键都由用户可控字符串索引，查表必须走 `Object.hasOwn(...)` 而非裸下标：`constructor`、`__proto__` 会命中原型链上继承的函数/对象，而不是「查不到」。

面向用户的文案另有一份词汇契约，真源是 `AGENTS.md`「界面词汇表」：表中每一行把一个内部词（基准库、chunk、KG、抽取、投影、晋升、schema、deprecated……）映射到界面唯一允许使用的说法。内部名保留在代码、类型、注释与架构文档里——只有渲染给用户看的字符串才改写；而**被持久化**而非被渲染的值（`Untitled notebook` 这个默认库名、协议上的 enum id）属于契约不属于文案，任何一轮措辞调整都不得顺手改动它们。`scripts/check_ui_vocabulary.py` 作为硬门挂在 `scripts/check.sh` 里执行该表，其**作用域跟着信任边界走、不跟着目录树走**：既扫描 `frontend/app` 每个源文件的渲染文本——字符串字面量加 JSX 文本节点，并先剥离注释、标识符、正则体与 `${…}` / `{…}` 插值——也扫描后端每处 `user_error(status, "…")` 的消息字面量，因为 `api/deps.py` 恰恰只给这批 4xx `detail` 打上 `X-User-Message: 1`，而 deny-by-default 的前端见到该标记就把它原样显示给用户。打标记等于声明「这是给人看的文案」，那就同样受这份词表约束；此前把守卫圈在 `frontend/app` 里，正是「仅管理员可设置基准库」「仅管理员可管理晋升队列」四条 403 一路上屏而守卫全绿的原因。裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内——它永远不上屏，detail 是诊断 / MCP 契约，这条分界由 `backend/tests/test_user_error.py` 守。任一侧命中黑名单词即构建失败。另有一条独立检查，拒绝「兜底即原值」（`MAP[x] ?? x`）：这种查表一旦后端新增枚举值，就会把英文 id 直接渲染给用户；应改用 `frontend/app/vocabulary.ts` 的 `label(MAP, value, fallback)`，它强制传中性兜底词，使该 bug 写不出来。若确实要原样透出**用户自己写的**字符串（自定义 `object_type`、用户自建的 schema 字段名），则显式写成 `Object.hasOwn(...) ? ... : raw`，顺带规避上面那个原型链隐患。该守卫是词黑名单而非语义检查：有两行只覆盖其无歧义的复合形态——图谱视图里裸用「节点」「边」是正当的，且「边」与「旁边」「边框」同形。`backend/tests/test_ui_vocabulary_guard.py` 存放它的正例与反例，并额外在「词汇表新增一行却既没有对应规则、也没有登记豁免理由」时失败，使黑名单无法悄悄退化成只覆盖词表的一个子集。

重新解析保留 source 行与原始文件：替换 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。删除复用同一 source-derived cleanup，随后删除 source 行（外键级联 source-owned records）与本地文件。

notebook 工作区隐藏集合页全局上边栏，采用偏工程风格的视觉治理。

## Knowhow 表

notebook 内的 **Knowhow 表** 动作（与知识图谱并列，单开一个面板）管理 **knowhow 表**：把领域经验沉淀成一行行经验记录，列名自由命名。首个实例是半导体时序违例排查（行=违例类型；列=现象识别、根因分析、修复方法、依赖工具），但列名完全是用户自定义文本，不锁定词表。建表可以从**导入**开始（xlsx/csv/Markdown，预览时给出列→内容类型的映射建议），也可以用**建表向导**从零搭建（先定列名表头，再填行）。填值两条路可自由混用：应用内经**格子编辑器**（markdown 编辑/预览分栏、图片粘贴或拖拽即可上传、自动本地草稿、保存并下一格连续录入），或线下走 **Excel 模板往返**：按当前表头下载 `.xlsx` 模板（表头行冻结），批量填写后上传追加（提交前会预览未匹配列，以及行标题与已有行重名的提示）。

至多一列可被指定为整张表的**行标题列**（表级设置，不是逐列打标）。设置后，表中每个非空格子都会成为知识图谱节点——节点的类型就是所在列名——并用 `about` 边连回该行的行标题格；同一列里不同行出现的相同值会归并成一个节点（十行都引用同一个工具，就是一个工具节点带十条入边）。不设置行标题列，整张表就只参与检索——格子照常切成 chunk 供问答使用，但不建任何图谱节点，适合每行是一条记录而非一个具名概念的流水型表格。

每列还带一个**内容类型**——仅作确定性解析提示，从不调用 LLM：**方法步骤**列解析成有序步骤列表，**工具/事物**列按列表项/换行拆分并去重成多个节点，**普通**列整格作为一个节点。格子编辑器与行详情抽屉都提供显式的**优化表达**按钮（绝不自动触发）：调用 notebook 已配置的 LLM，在保持原意的前提下规整结构与措辞，原文与建议对照展示，只有逐格确认后才会回填。

Ask 引用命中 knowhow 格子时会直接跳转到该行的详情抽屉，而非通用来源视图。notebook 深拷贝会把 knowhow 表完整带过去——表、列、行、格子、代码附件在副本里全部重新映射 id——且不重跑 embedding，未变化的格子文本在副本里复用原向量。

外部 Agent 接入面（HTTP + MCP、判别集、代码附件）见 [Memory 与 Agent MCP](#memory-与-agent-mcp)；HTTP 路径清单见 [API](#api)。

## Memory 与 Agent MCP

Memory 必须由用户手动选择、归创建者私有，并且始终绑定到且只绑定到一个 notebook。
在 Ask 回答上点击“保存到 Memory”后，后端先生成标题、正文和标签预览，用户可编辑，
只有最终确认才写入 `confirmed` Memory。预览模型未配置或失败时，系统确定性地用问题作
标题，并用移除显示引用后的回答作正文。当该 Memory 所属 notebook 非 base 库且已开启
知识图谱抽取（与上传来源同一判定门）时，确认动作与“保存到 Memory”弹窗会显示默认勾选的
“同时抽取到知识图谱”复选框；勾选后用与上传逐字相同的抽取管线把该 confirmed Memory 抽进
该 notebook 自己的 KG，记为对用户不可见、不进任何来源列表与计数的隐藏合成源，可在每次
确认时取消；base 库除外，只经下文的晋升人审进入 KG。总 Memory 页面只聚合当前登录用户的数据；
notebook 卡片数量和 notebook Memory 标签是同一份数据的 notebook 局部视图。总数与待确认数
始终按 owner 全量统计，不随状态、搜索或 notebook 筛选变化；notebook 筛选项来自有界的 owner
聚合查询，不做逐 notebook 查询。

生命周期为 `candidate | confirmed | rejected | deprecated`。Agent 只能创建 `candidate`；
token 具备 `memory:read_candidates` 时，同一用户、当前所选 notebook 下获授权的所有 Agent
profile 都可检索它。Candidate 永远不会进入正式 notebook Ask、notebook 搜索、Deep Report
或 `search_notebook_context`；只有用户确认后才进入正式平面。Rejected/deprecated 在两个
平面都排除。检索先判断相关性，权威只在同等相关或冲突证据间生效：
`candidate < personal 原始证据 < confirmed Memory < base KG/base 原始证据`。

Candidate provenance 会保存创建它的 Agent profile id/name 与每一条提交的 evidence ref，但绝不
保存 bearer token。服务端逐条按 candidate 的 owner 与 notebook 校验，并保存 `validated` 或
`invalid` 状态及有界原因；历史未验证或无效引用仍可由 owner 查看，但绝不会标成 trusted 或成为
可晋升 evidence。Candidate 详情、审核与 provenance API/UI 都只对 owner 开放。把 Ask 回答保存为
Memory 时，后端会在写 Memory、revision、provenance 的同一个 `BEGIN IMMEDIATE` 事务内再次校验
owner/member 实时权限，因此并发撤销分享不会留下半写入 Memory。

Memory 输入在 API 与 service 两层统一归一化并 fail-closed：title/content 去除首尾空白后必须非空。
当前上限为 title 80 字符、content 40,000 字符、tag 最多 20 个且每个 80 字符、审核/candidate
reason 1,000 字符、task context 序列化 UTF-8 8,192 bytes、evidence 最多 50 条且序列化 UTF-8
32,768 bytes、client request id 200 字符。HTTP 违规返回 422；MCP/内部调用也经过同一 service 校验。
嵌套 NaN、正负 Infinity 会在持久化前被拒绝，合法 JSON null 则保持原样往返。
MCP 提案严格使用这些 Core 上限，不再叠加更窄的重复限制。
tag 原始列表会先按 20 条限额校验，再 trim/去重；空白 tag 直接拒绝。

总 Memory 页的“Agent 接入”可创建稳定 Agent profile，以及明文只显示一次的 token。
Token 有过期时间、默认 notebook、notebook allowlist，并只授予所需的
`knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、
`ask:execute`、`knowhow:code` 子集；可即时撤销。后端 requirements 已包含官方 `mcp>=1.26.0` client/server
SDK。启动后，Streamable HTTP 服务位于 `/mcp`（到 `/mcp/` 的 redirect 已处理）。本机可用
loopback HTTP；默认允许远程明文 HTTP 并放宽 Host/Origin（DNS-rebinding）校验，供可信内网使用，
启动会打印明文告警（Agent token 明文过网）。公网部署请设 `MCP_REQUIRE_HTTPS=1` 强制 HTTPS
（并恢复 Host/Origin 校验），并把 `MCP_PUBLIC_URL` 设为公开的 HTTPS `/mcp` URL。
过期时间必须带明确时区偏移；浏览器把本地 `datetime-local` 转成 UTC，后端按 UTC 瞬间归一化保存。
无时区 datetime 会被拒绝，不会按服务端本地时区猜测。

Codex 推荐把签发的 token 放入环境变量，再注册服务：

```bash
export SILICON_NOTEBOOK_AGENT_TOKEN='<一次性显示的 token>'
codex mcp add silicon-notebook --url http://127.0.0.1:8000/mcp \
  --bearer-token-env-var SILICON_NOTEBOOK_AGENT_TOKEN
```

当前本机 Claude Code CLI 接受 HTTP transport 和显式 Authorization header：

```bash
claude mcp add --transport http silicon-notebook http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <一次性显示的 token>"
```

Claude Code 可能把这段原始 header 保存到本机配置。应使用最小 scope、短有效期，保护
本机配置，并在使用后撤销/轮换；不要假设该 header 会做 shell 环境变量插值。

每个新 MCP session 必须先调用 `select_notebook`，再调用数据工具。精确的十一个工具是：
`list_notebooks`、`select_notebook`、`search_agent_memory`、
`search_notebook_context`、`get_memory`、`ask_notebook`、`propose_memory`、
`list_knowhow_tables`、`get_knowhow_discrimination`、`get_knowhow_row`、
`put_knowhow_cell_code`。
服务端会在数据调用时重新检查 scope、allowlist、token 状态和 notebook 权限；返回文本是
不可信 evidence，不是可执行的 Agent 指令。

四个 knowhow 工具与 `/api/agent/knowhow/...` 下的 HTTP 端点（见 [API](#api)）共用同一套
service 函数，HTTP 与 MCP 不会在响应形状上走样。`list_knowhow_tables`、
`get_knowhow_discrimination`、`get_knowhow_row` 需要 `knowledge:read`；
`get_knowhow_discrimination` 对设有行标题列的表按行返回标题，以及每个方法步骤列的
`{column_id, column_name, text, code_status}`（表未设行标题列则返回 400），供 Agent
据此跑自己的判别逻辑挑选适用的修复方法。`get_knowhow_row` 返回一行的完整格子文本
（方法步骤/工具事物列另带 `steps`/`items`）及该行全部**代码附件**的代码本体。代码附件
是外部 Agent 针对某格方法已经写好的代码——notebook 从不生成也不执行，也从不进
embedding/chunk/索引/KG 投影——其新鲜度（`implemented`/`stale`/`none`）在读取时用格子
当前内容的 hash 与附件保存时的 hash 比对推导；判别集只带这个三态，不带代码本体，以控制
体积。读代码依然只需要 `knowledge:read`；只有写入（`put_knowhow_cell_code`，以及对应的
HTTP `PUT`/`DELETE .../code`）才需要 `knowhow:code`——一个既要读现有代码又要写新版本的
token，两个 scope 都要授予。

只有 `confirmed` Memory 可发起 KG 晋升。创建者提交后，admin queue 展示脱敏后的结构化提取
候选与服务端验证过的 evidence，而不是原始 Memory revision/provenance 浏览器。提案会固定精确的
来源 revision、脱敏候选快照和审核所见 evidence；编辑或弃用审核中的 Memory 会在同一事务中废止
旧提案并重置晋升状态，编辑后可重新提交。当前 provenance 会清除 proposal 指针，固定提案只保留在
快照与队列历史中。批准时会重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权，
并在写事务内校验固定 revision 与 notebook，再复用 KG dedupe/merge 创建或合并一个或多个 Base KG 对象；批准/拒绝会记录当前登录的
admin reviewer，API 与晋升审计记录完整的 `base_object_ids`。这一过程不会改变或暴露原私有 Memory。
删除 notebook 会级联删除所有成员绑定到它的私有 Memory，因此删除弹窗会提示这一生命周期
后果，但不会泄露成员身份或数量。

仓库内固定 Memory 评测计算 Recall@5、MRR、nDCG，以及三项零容忍计数：candidate 进入正式
平面、跨用户、跨 notebook 泄漏。A/B harness 比较 no-Memory、KB-only 与
KB+confirmed-Memory 三种检索条件。

## KG 抽取触发

源解析 + 向量化完成后即可做 chunk-native 检索，因此 **KG 抽取按 notebook「按需开启」，并非每次上传都抽**：

| 上传时 notebook 状态 | 是否抽 KG | 怎么触发 |
|---|---|---|
| 尚无 KG（新库） | **不**自动抽 | 按需构建：`POST /api/notebooks/{id}/kg/build`（界面：notebook 的**「构建知识图谱」**动作；在无 KG 的库上选「深入分析」组——即 `strict` 的 `reasoning` / `graph`——时也会提示构建） |
| 已有 KG | 每个新源**自动后台抽取** | 无需手动触发——续抽以保持 KG 完整；新源随后增量融入跨文档统一 KG |

摄取期判定 = `KG_AUTO_EXTRACT 或 该 notebook 已有 KG`：

- `KG_AUTO_EXTRACT`（默认 `false`）——为 `true` 时**所有** notebook 每次上传都抽 KG。
- 否则仅当该 notebook 已有 KG 对象时，上传才抽。

即：**首次 opt-in**（构建 KG，或设 `KG_AUTO_EXTRACT=true`），之后新文档自动抽取 + 融合。整库重抽用 `POST /api/notebooks/{id}/kg/rebuild`；离线批量构建见「离线批量摄取」一节。

## 检索模式（问答）

`POST /ask` 按 `mode` 分派——注册表 `backend/app/services/ask_modes.py` 是唯一真源（默认 `chunk`）。联合范围按路径区分：`chunk` 基线 active-only；可选 KG overlay / PPR 可加入 federated KG 与 base-backed chunk；`graph` / `reasoning` 走 federated KG。`federated_retrieve()` 的知识对象命中不改 score，只在完全平局时以 `base` 为第二排序键；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。这些排序信号不进入接地阈值。

| 模式 | 分组 | 需 KG | 一句话 |
|------|------|-------|--------|
| **`chunk`**（默认） | general | 否 | chunk-native 通用问答：大召回 → 选择 → 长上下文综合 → 引用绑回源 chunk。 |
| **`graph`** | strict | 是 | 对跨文档知识图谱做单趟个性化 PageRank（PPR）传播。 |
| **`reasoning`** | strict | 是 | agentic 迭代 plan → retrieve → reflect → answer（流式输出实时轨迹）。 |

### id 与显示名

上表的 id（`chunk` / `reasoning` / `graph`，以及分组 id `general` / `strict`）是**协议**：`POST /ask` 收的是它，历史会话与书签存的是它，后端注册表 `backend/app/services/ask_modes.py` 声明的也是它。它们是稳定量，不因为「名字不好听」而改。

界面上**显示**的名字是另一层，纯 UI，归前端注册表 `frontend/app/ask-modes.ts` 所有：

| 协议 id | 问答面板显示名 |
|---|---|
| `chunk` | 通用问答 |
| 分组 `strict`（选择器给出的入口，组内默认引擎是 `reasoning`） | 深入分析 |
| `reasoning` | 逐步推理 |
| `graph` | 关联追溯 |

该注册表的 `groupLabel()` / `modeLabel()` 是唯一读取口：前端任何其它文件都不得硬编码显示名，散文里提到就用模板插值。两边由 `ask-modes.test.mjs` 强制——它递归扫描 `frontend/app`，当前显示名出现在注册表之外即失败，退休名（严格推理 / 深挖推理 / 图谱多跳）复活也失败。因此改显示名只是改注册表一行，不动任何 id、请求/响应载荷或已存会话；id 集合另由 `scripts/check_ask_modes_contract.py` 跨前后端锁同步。

**`chunk` —— chunk-native，含可选 chunk×graph mix。**
- *基线：* chunk 大召回（`CHUNK_RECALL`）→ MMR / 多子查询配额多样性选择（`CHUNK_MMR_K`）→ 长上下文综合，不碰 KG。
- *mix*（仅当 `CHUNK_KG_OVERLAY_ENABLED=true` **且** 配齐 qwen3-rerank **且** 有 KG 时生效）：三路并池——(a) 向量 chunk、(b) query 种子周围的 KG 局部结构（实体 + 其 1-hop 关系，只检索一次）、(c) 这些 KG 对象背后的源 chunk——round-robin 合并 → qwen3 cross-encoder rerank → 按 token 预算装填（`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`）。答案在同一套 `[k]` 映射里同时引用 chunk 与 KG 项，接地跨 chunk ∪ KG。未配 rerank 或无 KG 时**字节等价回退**到基线。（忠实 LightRAG 的 `mix` 模式。）

**`graph` —— 跨文档 KG 上的 PPR。** 经 `federated_retrieve` 取种子（KG 实体 + 其源 chunk；`RELATION_RETRIEVAL_ENABLED=true` 时再融合关系索引命中）作为 HippoRAG 式**个性化 PageRank**（`GRAPH_PPR_ENABLED`，默认开）的个性化向量，通过共享知识图谱把相关度跨文档传播；排名靠前的 chunk 喂出接地答案，`[k]` 锚点指向 KG 对象/关系。`GRAPH_PPR_ENABLED=false` 时回退为沿推理边的有界 BFS。

**`reasoning` —— agentic 深挖检索。** 委托 `ReasoningRetriever`：拆解问题、检索（与 `graph` 同样走 PPR 传播）、反思是否充分，按需扩图/加子查询直到能回答——经 NDJSON stream（`/ask/stream`）输出 `reasoning_trace`。遇到显式推导问题时可调用 `follow_chain`：通过两轮有界邻接抽样复用既有 source/target 索引，再确定性检查类型、状态、审核、evidence 与 `validity_scope`；两条存储关系作为可引用前提，`A→C` 只作为带「推断」标记的查询期结论。高度节点抽样被截断且无法证明不存在直接边时，宁可不推。严格 / KG 接地。

退役 id `fast`、`global` 透明映射到 `chunk`（旧会话/书签不会 422）；其余未知 mode 返回 HTTP 422。

## API

当前 beta 的关键 API：

- `GET /api/notebooks`、`POST /api/notebooks`、`PATCH /api/notebooks/{id}`、`DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `POST /api/notebooks/{id}/sources` — multipart 文件上传（异步解析/抽取）
- `GET /api/sources/{id}`、`DELETE /api/sources/{id}`、`POST /api/sources/{id}/parse`、`GET /api/sources/{id}/elements`
- `GET /api/notebooks/{id}/knowledge-types`、`GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`、`PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- Knowhow 表：`GET|POST /api/notebooks/{id}/knowhow`、`GET|PATCH|DELETE .../knowhow/{table_id}`、`POST .../knowhow/{table_id}/reproject`——另有导入（`POST .../knowhow/import/preview`、`POST .../knowhow/import`）、列/行/格编辑（`POST .../knowhow/{table_id}/columns`、`PATCH|DELETE .../columns/{column_id}`、`POST .../knowhow/{table_id}/rows`、`DELETE .../rows/{row_id}`、`PATCH .../rows/{row_id}/cells/{column_id}`）、Excel 模板往返（`GET .../knowhow/{table_id}/template`、`POST .../knowhow/{table_id}/append` 配 `mode=preview|commit`），以及显式的建议式 LLM 表达优化（`POST .../rows/{row_id}/cells/{column_id}/optimize`）
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask` — 接地问答（逐句 `[k_i]` 引用；`mode`：默认 `chunk` | `graph` | `reasoning`；联合范围遵循上文各 mode 的边界）
- `POST /api/notebooks/{id}/ask/stream` — Ask 进度的 NDJSON stream（先发带 `job_id` 的 `started` 事件，再发进度/最终事件）；transport 断开连接只会停止当前客户端继续接收，后台 job 仍继续并可保存回答
- `GET /api/notebooks/{id}/ask/jobs/{job_id}` — 供重连/恢复流程读取 detached Ask job 的 `status`、`trace` 与 `answer_id`；状态为 `done` 后，前端重新加载 conversation 取得最终 `AskResponse`
- `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel` — 用户显式中断端点；设置取消事件并在保存被取消的最终回答前停止 worker
- `GET /api/notebooks/{id}/conversations`、`GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- Memory：`GET /api/memories`、`GET /api/notebooks/{id}/memories`、`GET|PATCH /api/memories/{memory_id}`、`POST /api/memories/{memory_id}/confirm|reject|deprecate|promote`、`POST /api/answers/{answer_id}/memory-preview`、`POST /api/notebooks/{id}/memories/from-answer`
- Agent 接入：`GET|POST /api/agent-profiles`、`PATCH /api/agent-profiles/{profile_id}`、`POST /api/agent-profiles/{profile_id}/tokens`、`GET /api/agent-tokens`、`DELETE /api/agent-tokens/{token_id}`；Streamable HTTP MCP 挂载在 `/mcp`
- Knowhow agent 接入面：`GET /api/agent/knowhow/tables?notebook_id=`、`GET /api/agent/knowhow/tables/{table_id}/discrimination`、`GET /api/agent/knowhow/rows/{row_id}`、`GET|PUT|DELETE /api/agent/knowhow/rows/{row_id}/cells/{column_id}/code`——session 或 Agent Bearer token 均可访问；读需要 `knowledge:read`，代码写入需要 `knowhow:code`（见 [Memory 与 Agent MCP](#memory-与-agent-mcp)）
- 统一 KG：`POST .../unified-kg/rebuild`、`GET .../unified-kg`、`GET .../unified-kg/pending-merges`、`POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`、`GET .../objects/{object_id}/context`
- `GET /api/object-schemas`、`POST /api/object-schemas`、`PATCH /api/object-schemas/{type}`、`DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`、`POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- 两层：`POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → 返回更新后的 `NotebookSummary`（tier 非法 400，notebook 不存在 404）。设置 notebook 的联合层（base = 权威参考 KG，personal = 默认用户笔记）。
- 边可信与策展：`GET /api/notebooks/{id}/edge-review-queue`、`POST /api/notebooks/{id}/relations/{rel_id}/review`
- 治理 / 晋升：`POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`、`GET /api/promotion-queue`、`POST /api/promotion-queue/{candidate_id}/approve|reject`
- 深度报告（两阶段）：`POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`;跑**阶段1 规划**后停在 `status=outline_ready`（`auto_generate=true` 则一路直出）。`GET .../reports/{rid}` 轮询状态 + 富 `outline`（每节 视角/张力/充分性）+ `content_md` + 实时 `section_status`。`PATCH .../reports/{rid}/outline` body `{sections}` 编辑草案大纲（仅 `outline_ready` 态,无有效节 422）。`POST .../reports/{rid}/generate` body `{depth?}` 启**阶段2 生成**（仅从 `outline_ready`,否则 409）。另 `GET /reports`（列表）、`POST .../cancel`、`DELETE`、`POST .../reports/export` `{report_ids}` → `reports.zip`。章节按 `KG_JOB_CONCURRENCY` 并行深挖。

当前持久化/API 契约是 `reports` 表与 `/reports` API；已退役的内容工作室存储与路由不属于当前 runtime。

## 配置

所有模型服务均通过 URL 端点接入，不启动本地模型服务。

**LLM（OpenAI-compatible）：**

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS   # 默认 60
OPENAI_COMPAT_MAX_RETRIES       # 默认 2
```

**嵌入（向量检索）：**

```text
EMBED_PROVIDER          # ""=关闭（仅关键词） | dashscope
EMBED_MODEL             # EMBED_PROVIDER=dashscope 时必填，如 text-embedding-v4
EMBED_BASE_URL          # 必填的嵌入端点 URL
EMBED_API_KEY
EMBED_DIM               # 须与模型输出维度一致（默认 1024）
EMBED_TRUNCATE_CHARS    # 每段文本喂给 embedder 的最大字符数（默认 2000）
EMBED_BATCH_SIZE        # 每次嵌入调用的元素数（默认 10）
EMBED_PERSIST_CHUNK     # 每批落库行数（默认 200）
EMBED_CONCURRENCY       # 并发嵌入线程数（默认 8；温和值，防 429）
```

**KG 抽取并发与窗口化：**

```text
KG_AUTO_EXTRACT             # 所有 notebook 每次上传都抽 KG（默认 false）；为 false 时，
                            # 若该 notebook 已有 KG，新源仍自动续抽（首次 opt-in，之后自动维护）
KG_EXTRACT_WORKERS          # 全局并发抽取窗口(LLM 调用)上限，跨所有文档共享(文档内+文档间)（默认 16）
KG_JOB_CONCURRENCY          # 同时抽取的文档数(作业池)；各文档窗口共享上面的全局预算（默认 8）
KG_ASK_RESERVE              # 为交互式 Ask 预留的 LLM 连接数，抽取打满时 Ask 不被饿死；连接池=WORKERS+RESERVE（默认 64）
KG_WINDOW_TARGET_CHARS      # 0=窗口大小自适应（默认）；>0 强制固定窗口字符数
KG_WINDOW_MIN_CHARS         # 自适应窗口下限（默认 4000）
KG_WINDOW_MAX_CHARS         # 自适应窗口上限（默认 8000）
KG_WINDOW_OVERLAP_CHARS     # 相邻窗口重叠字符数（默认 450）
KG_WINDOW_WARN_THRESHOLD    # 窗口数超此值记 WARNING（默认 1200）
```

**按核数自动调参：** 只有真正受 CPU 核数约束的旋钮才会按机器核数自动缩放，其余旋钮不论硬件如何都保持固定：

```text
KG_CLUSTER_ANN_THREADS   # 概念聚类/合并用的 hnswlib ANN 建索引线程数。
                         # 0（默认）= 自动 min(cpu核数, 32)；检索/KG 相关旋钮里
                         # 唯一按本机核数推导的一个。
```

`scripts/dev.sh` / `scripts/prod.sh` 会 source `scripts/autotune.sh`，在未显式设置时
把 `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS`（以及
`NUMEXPR_NUM_THREADS`）设为 `min(cpu核数, 8)`；离线回填 CLI 的进程池 worker 默认值同样是
`min(cpu核数, 32)`。设 `AUTOTUNE=0` 可整体关闭该 shell 层自动调参。以上任何值都可以用
显式 env 覆盖——显式设置的值永远优先于自动推导的默认值。后端启动时会打印一行已解析的
实际值（`autotune: kg_cluster_ann_threads=... backfill_default_workers=...` 控制台日志），
方便确认某次运行到底生效的是什么。

与此刻意相反，`KG_EXTRACT_WORKERS` / `KG_JOB_CONCURRENCY` / `EMBED_CONCURRENCY` 是针对
远程 LLM/嵌入端点的并发上限，不是本机资源——它们**不**按核数缩放，加核也不会改变它们；
要提升这块吞吐，应扩容模型/嵌入服务端，而不是调这几个值。

启用多 worker（`--workers N`）是**手动 opt-in**，autotune 不会替你打开——默认仍是单
worker。每多一个 worker 就多一份内存中的状态（大型 KG/ANN 索引可达 GB 级），内存占用
大致按 N 倍增长，且后台 KG job 可能落在任意一个 worker 上，会让状态追踪复杂化。只有在
清楚并接受这两个代价后再开启。

**数据库：**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout（毫秒，默认 30000）
SQLITE_CACHE_SIZE_KB    # 每连接 SQLite 页缓存(KB,负值=KB)。连接按线程复用,总内存≈线程数×|值|（默认 -16384）
DATABASE_URL            # SQLite 路径（默认 .local/silicon_notebook.db）
SILICON_NOTEBOOK_STORAGE_DIR   # 上传文件存储目录（默认 .local/storage）
```

**检索：**

```text
RETRIEVAL_TOP_N         # 推理/报告合成证据预算下界（默认 20）
REASONING_TOP_N_PER_QUERY  # 自适应预算：每个方面（子查询，含社区兄弟）保底席位（默认 3）
REASONING_TOP_N_CAP        # 自适应预算上限；对比题按方面数扩容（默认 36）
```

**可伸缩检索索引：** 规模大到不可拷贝的 notebook（与 notebook 拷贝/分享判定同一阈值——
字节数或 chunk+node 行数超过配置上限）会自动构建/刷新检索索引，无需手动点按钮或跑 CLI：
在来源抽取完成后、KG 重建后，以及查询首次发现无索引时兜底触发。默认会排队到低峰窗口而非
立即构建。

```text
SCALE_INDEX_AUTO_ENABLED   # 为大库自动构建/刷新检索索引（默认 true）
SCALE_INDEX_AUTO_WHEN      # "idle"=排队到低峰窗口（默认）｜ "now"=立即构建
```

**检索 / KG 增强（GraphRAG + ToG-3 借鉴，Phase 1+2）：**

opt-in（默认关）与默认开混合。默认开：`ANSWER_CONTEXT_*`、`KG_QUERY_REFINE_ENABLED`，以及 KG 质量增强 `KG_REFINE` / `KG_GLEANING` / `KG_CONCEPT_DESC`。其余请**逐个开启**并用
评测脚本（`backend/app/eval`）验证——RRF + 重排 + 精炼三个全开会回归。

```text
LLM_CACHE_ENABLED            # 把 LLM 响应缓存到独立 sqlite（默认 false）
LLM_CACHE_PATH               # 缓存 DB 路径（默认 .local/llm_cache.db）
KG_REFINE_ENABLED            # 抽取自校验：丢弃幻觉节点（默认 true）
KG_GLEANING_ENABLED          # 额外几轮让 LLM 找回漏抽节点（默认 true）
KG_GLEANING_ROUNDS           # 开启时的 gleaning 轮数（默认 1）
KG_CONCEPT_DESC_ENABLED      # LLM 融合跨文档概念簇描述（默认 true）
KG_COMMUNITY_SUMMARY_ENABLED # rebuild 期生成 LLM 社区报告（社区层；默认 false）
ANSWER_CONTEXT_BUDGET_CHARS  # 答案上下文装配字符预算（默认 6000）
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
CHUNK_RECALL                 # chunk 大召回数（默认 200；mix 候选池 / 无 rerank 时 MMR 候选）
CHUNK_MMR_K                  # 无 rerank 时 MMR 精选 chunk 数（默认 16）
CHUNK_KG_OVERLAY_ENABLED     # chunk×graph mix：叠加 KG 局部结构+源 chunk（默认 true；需配 qwen3-rerank 才生效）
RERANK_MODEL                 # qwen3-rerank 模型名；留空=关，mix 回退 MMR（默认空）
RERANK_BASE_URL              # DashScope 原生 text-rerank 基址（默认 dashscope api/v1；非 compatible-mode）
RERANK_API_KEY               # rerank 用 DashScope key（启用 mix rerank 必填）
RERANK_MAX_DOCS              # 单次 rerank 文档上限，超出自动切 batch 并发（默认 500）
MAX_ENTITY_TOKENS            # mix KG 实体段 token 预算（默认 6000）
MAX_RELATION_TOKENS          # mix KG 关系段 token 预算（默认 8000）
MAX_TOTAL_TOKENS             # mix 总上下文 token 预算（默认 30000）
REPORT_MAX_SECTIONS          # 深度报告大纲：最大章节数（默认 6）
REPORT_SECTION_CHUNK_BUDGET  # 深度报告：每节 chunk 上下文字预算（默认 20000）
REPORT_SECTION_MAX_TOKENS    # 深度报告：每节撰写 max_tokens（默认 8192）
REPORT_ALLOW_PARAMETRIC      # 深度报告：允许【通识】层（库外通识，行内标注且提示未经验证，默认 true）
```

**两层知识库与图推理（Wave 1+2）：** 目前没有 `.env` 开关。notebook 的 `tier`
（`base` | `personal`，默认 `personal`）是 notebook 行上的数据，通过仓库方法
`mark_notebook_base()` 设置。tier 感知联合检索不改相关度分数：相关度是第一排序键，
`base` 仅在相关度分数完全相同时作为第二排序键。答案里的 base 优先冲突规则是独立的合成策略，
在 notebook 标为 `base` 后始终生效。
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
MINERU_FORMULA_ENABLE   # true/false
MINERU_TABLE_ENABLE     # true/false
MINERU_RETURN_IMAGES    # 是否保留 PDF/DOCX/PPTX 文档中的内嵌图片（默认开 true；设 0/false 仅保留文字与图注）
MINERU_MAX_IMAGE_BYTES  # 单张内嵌图片大小上限（默认 5MB，超出丢弃）
MINERU_MAX_IMAGES_PER_SOURCE # 每个来源最多保留的内嵌图片张数（默认 200）
```

**日志：**

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS
EVENT_LOG_ENABLED / EVENT_LOG_DIR
SLOW_REQUEST_MS         # 超过该毫秒数的请求标记 SLOW（默认 3000）
SILICON_NOTEBOOK_CORS_ORIGINS
```

`.env.example` 是每个环境变量的权威完整清单（含默认值与逐行注释）——上面分组只列常用项。其余可调项还包括：可选的推理专用 LLM（`REASONING_LLM_BASE_URL` / `REASONING_LLM_API_KEY` / `REASONING_LLM_MODEL`）及其护栏（`REASONING_MAX_STEPS`、`REASONING_MAX_SUBQUERIES`、`REASONING_TIMEOUT_SECONDS`、`REASONING_MAX_RETRIES`）、检索/接地调参（`PROC_MIN`、`EVIDENCE_TAU_LOW`、`EVIDENCE_TAU_HIGH`）、可选调试日志查看器（`DEBUG_LOGS_ENABLED`），以及运行身份（`SILICON_NOTEBOOK_ENV`、`SILICON_NOTEBOOK_SINGLE_USER_EMAIL`、`SILICON_NOTEBOOK_SINGLE_USER_NAME`）。

没有配置 LLM 时，摘要和回答退化为 deterministic 行为；source 解析仍会完整执行，KG 抽取阶段记录完成的 `no-llm` run，不生成合成知识。

## 可观测性 / 日志

后端通过统一的 `EventLogger`（`app/core/event_logging.py`）输出结构化日志：每条事件一行 JSONL 写入 `.local/logs/`，并附控制台简要行。写日志是 best-effort，绝不影响它所观测的请求或管线；未配置模型时 LLM 通道为 no-op。

- `requests.jsonl` — 每个 HTTP 请求（方法、路径、状态码、耗时、`request_id`）。超过 `SLOW_REQUEST_MS`（默认 3000ms）标 `SLOW`；响应头带 `X-Request-Id` 关联前后端。
- `events.jsonl` — 异步来源管线：各阶段（`parse` / `embed` / `extract`）耗时与每次状态机跃迁。卡住时能看到当前阶段及已运行时长；失败记录真实异常（以及来源的 `error_message`）。
- `llm.jsonl` — 每次大模型调用：chat（prompt/响应/token/耗时，按 `LLM_LOG_MAX_CHARS` 截断）、embedding（仅摘要，不存原始向量）、以及 deterministic fallback 容易让人忽略的错误。

浏览器 DevTools console 会镜像请求为 `[api] 方法 /路径 -> 状态 N毫秒 (request_id)`；轮询期间 UI 显示当前阶段/已用时长，失败时点名是哪个来源。来源的 `error_message` 由后端写成 Python 异常字符串，因此进 console 而不上屏。

错误信息按受众分流。用户看到的一律是中文：前端把 HTTP 状态码映射成人话（「没有权限进行这个操作」「没找到，可能已被删除」），裸状态码和后端异常原文都不会出现在界面上。**除非后端明确声明「这句是写给用户的」，否则一概不原样展示**——API 会给这类响应打上 `X-User-Message` 头，只有它们才透传（如「用户名已被占用」，比泛化文案更具体）。其余一律泛化，**包括恰好是中文的后端文本**：像「解析失败：不支持的文件类型」这种串，同样可能是一条原始异常，光看内容分不出来。5xx 无论有没有标记都泛化，避免内部错误外泄。压根没产生 HTTP 响应的失败（断连、后端没起来、藏在流式事件、后台任务记录、失败的报告、来源解析失败里的错误串）走同一条规则，不会把原文直接印出来。

开发者与 MCP agent 看到的东西不变：后端 `detail` 在 API 响应和日志里保持原样，而完整诊断——状态码、状态文本、原始响应正文、以及能和 `requests.jsonl` 对上的 `X-Request-Id`——在每次请求失败时写进 DevTools console；凡是被界面换成泛化文案的错误，其原文也一并进 console。所以「它说我没权限」这类问题靠 console 里的 request id 定位，而不是猜是哪道校验拒的。

部署机慢因排查可直接在持有 `.local/` 的机器上运行 `python3 scripts/diag_slow.py`。
脚本除汇总请求、事件和 LLM 延迟外，还会基于 DB 聚合与 scale-index manifest 输出
strict reasoning / PPR 路径审计，用来判断大库是否只走 indexed core、chunk/relation ANN
是否齐全、delta 策略是否会放开未索引部分，以及跨 base 的路径是否可能触碰 active 全量向量。
该报告同时是统一诊断入口 `scripts/diag.py` 的 `slow` 子命令（`diag.py slow | latency | base-recall`）——
把三个「分析慢现象」的工具收敛到一个命令：离线的 `slow` / `latency` 子命令保持纯 stdlib、不 import app，
可在裸机上直接跑；`base-recall` 才懒加载 app，用于诊断深度报告为何不引用 base 库。

**日志可视化页面 — `/dev/logs`。** 针对上述 JSONL 通道的只读 debug 页面（v1 聚焦 LLM 通道）。左侧列表可按 kind / status / model 过滤并全文搜索；详情区完整展示发给 LLM 的内容（`system` / `user` 消息与 `schema_hint`）以及模型回复、token 用量、耗时。由门控的后端接口 `/api/debug/logs/...` 提供，需显式设置 `DEBUG_LOGS_ENABLED=true` 才会开启（默认关闭——完整 LLM 记录可能包含私有来源材料）。

## 用 MinerU 解析 PDF

PDF 解析与 GPU 解耦：后端本身不引入 torch，只有在配置 MinerU 时才调用它，否则回退到 pypdf 纯文本。

- **本机 / 无 GPU**：保持 `MINERU_MODE=off`，PDF 走 pypdf（仅纯文本）。
- **GPU 部署机（推荐 HTTP 服务）**：把 MinerU 作为独立服务运行，让后端指向它：

  ```bash
  pip install -U "mineru[all]"      # 在 GPU 机器上
  mineru-api --host 0.0.0.0 --port 8000
  ```

  然后在后端设置：

  ```text
  MINERU_MODE=http
  MINERU_API_URL=http://<gpu-host>:8000
  MINERU_BACKEND=pipeline
  MINERU_FORMULA_ENABLE=true
  MINERU_TABLE_ENABLE=true
  MINERU_TIMEOUT_SECONDS=600
  ```

- **同机 Python API**：如果 `mineru` Python 包与后端装在同一台机器，可改用 `MINERU_MODE=cli`（无需 `MINERU_API_URL`）。这个模式会在隔离子进程里调用 `mineru.cli.common.do_parse/read_fn`，不会调用 `mineru` shell 命令；因为部分 MinerU 版本的 CLI 会自行拉起本地 API server，长文档场景下更容易卡住。

- **远端 VLM 推理服务器**：若只想把 VLM 模型卸载到一台独立的 vllm/sglang 服务器（而非整套 `mineru-api`），用 client 后端并指向该服务器：

  ```text
  MINERU_BACKEND=vlm-http-client        # 或 vlm-sglang-client
  MINERU_VLM_SERVER_URL=http://<vlm-host>:30000
  ```

  `http` 与 `cli` 两种模式都生效；非 client 后端会忽略该 URL。

- **Apple Silicon 本地（MLX，离线）**：Apple Silicon 的 Mac 没有 NVIDIA GPU，但可用 MLX 加速 MinerU，因此本地也能跑同质的高保真解析：

  ```bash
  python -m pip install -U "mineru[core]"
  mineru-models-download -s huggingface -m vlm     # 一次性(~GB)；HF 慢可用 -s modelscope
  ```

  然后在本机 `.env` 写：

  ```text
  MINERU_MODE=cli
  MINERU_BACKEND=vlm-auto-engine     # Apple Silicon 上走 MLX
  MINERU_PARSE_METHOD=auto           # 如需对齐手工 MinerU 结果，可改 txt/ocr
  MINERU_LANG=en                     # 可选；已知 PDF 语言时建议设置
  MINERU_MODEL_SOURCE=huggingface
  MINERU_TIMEOUT_SECONDS=1800        # 本地 VLM 跑完整论文可能超过 10 分钟
  ```

  `.env.example` 默认仍保持 `MINERU_MODE=off`，让其他环境默认离线安全。

**URL 来源（「添加链接」）优先用本地 MinerU。** 只要配置了本地 MinerU 服务（`MINERU_MODE=http`/`cli`），公开 PDF 链接就由本地解析：后端下载后走与文件上传相同的「本地 MinerU→pypdf」路径。为防 SSRF，下载器会校验初始地址和每次重定向，拒绝 localhost、私网、link-local 与保留地址；内部文档请改用文件上传。`MINERU_API_TOKEN` 云端（mineru.net）仅在未配置本地 MinerU 时作为回退——一旦走本地，绝不会再静默调用云端。添加链接需要本地 MinerU 或云端 token 二者其一。文件上传遵循同一条规则：本地 MinerU 未配置、仅配置了云端 token 时，上传文件同样经该云端 v4 路径解析（含图片、公式、表格），云端调用失败会自动回落 pypdf。

MinerU 输出会映射为结构化 `SourceElement`：公式→`formula` 元素（保留 LaTeX），表格→`table` 元素（HTML 存入 metadata），标题保留层级。前端在 source detail 里渲染它们——公式用 KaTeX、表格用其 HTML——所以公式是排版后的样子而不是原始 LaTeX。若 MinerU 不可达或出错，摄取会降级到 pypdf，保证上传不被阻塞，同时 pipeline log 和 source `error_message` 会保留回退诊断；若某 PDF 解析出 0 文本（如扫描/图片型 PDF），会给出提示而不是看起来"空成功"。

### 单文件解析自检(`scripts/mineru_probe.py`)

一个单文件诊断脚本，把一个文件(`.pdf`/`.docx`/`.pptx`)沿**应用上传时的同一条内联路径**发出去——即配置好的 MinerU 服务(`MINERU_MODE=http` → `/file_parse`，或 `MINERU_MODE=cli`)，再经同样的 `content_list` → `SourceElement` 映射——并报告能否解析。用于在把某个 MinerU 部署接入摄取前，确认它可达、且确实能解析给定文件。

```bash
python scripts/mineru_probe.py /path/to/paper.pdf
python scripts/mineru_probe.py /path/to/paper.pdf --dump /tmp/content_list.json
```

它会先打印从仓库根 `.env` 读到的生效 MinerU 配置——含 `http_proxy`/`no_proxy` 对 MinerU URL 的解析结果（内网调用被正向代理静默接管是 `504` 的常见根因；注意 `no_proxy` 不识别 `10.0.0.0/8` 这类 CIDR 网段，只认精确主机）——再给出原始块数/类型分布，以及映射后的结构化元素数。退出码 `0`=解析成功(≥1 个元素)；`1`=根本没发请求(MinerU 未开/配置缺失，或文件不存在)；`2`=已发送但失败(不可达、超时、HTTP 错、或返回空/映射为 0 元素)，每种都附一句分类排障提示。它会 import backend 并读仓库根 `.env`，请从主 checkout 根目录运行。本探针只覆盖内联 `MINERU_MODE` 路径——不含 mineru.net 云端(URL 来源)与下面的异步 `/tasks` 批量端点。

### 批量 PDF→Markdown 解析(`scripts/mineru_batch_parse.py`)

独立于 backend 之外的部署侧 CLI,用于批量/离线预解析一整个 PDF 目录(如一批书),对接你自己的 MinerU 部署,产出供下面「离线批量摄取」消费:PDF 目录 → `mineru_batch_parse.py` → Markdown 目录 → `batch_ingest.py` → KG。它递归扫描 `--src` 下的 PDF,把每个文件提交到内网 MinerU server 的**异步** `/tasks` API(提交→轮询→取结果),轮流分派到各配置的 server(每台各自有并发上限),产出与源目录同构的 `.md` 文件树到 `--out`。这与上面应用内联的单文件上传解析(`MINERU_MODE=http`,MinerU 同步的 `/file_parse` 接口)以及 mineru.net 云端路径都是独立的两条路——请指向你自己的、支持异步 API 的 MinerU server。

配置走 `.env`(`MINERU_BATCH_*`,见 `.env.example`)——`--env-file` 用来指定加载哪个 `.env` 文件(默认 `./.env`)——每个 key 都可用对应的命令行参数按次覆盖(`--servers`、`--src`、`--out`、`--list <文件>` 显式给路径列表而非递归扫描、`--limit N` 限制处理文件数)。重跑会跳过已生成的 `.md`;每个文件的结果(`ok`/`skip`/`fail`,若 Ctrl-C 中断则还没轮到的文件记为 `cancelled`)都会追加进一份 JSONL manifest(默认 `{MINERU_BATCH_OUT_DIR}/_manifest.jsonl`),可续跑、可审计;Ctrl-C 会让进行中的文件跑完,但不再派发新的,重跑会重试所有 `fail`/`cancelled`。`--only-failed` 只重跑上次记为 `fail` 的文件(也会列在 `failed.txt` 里)。

```bash
# .env 里配好(MINERU_BATCH_SERVERS / _SRC_DIR / _OUT_DIR ...)
python scripts/mineru_batch_parse.py --dry-run      # 预览 server 分配
python scripts/mineru_batch_parse.py                # 正式跑
python scripts/mineru_batch_parse.py --only-failed  # 只重跑上次失败的文件
```

脚本不 import 任何 backend 代码——只依赖标准库和 `requests`(backend 已有此依赖)——通过普通 HTTP 与 MinerU server 通信,运行它的机器因此不需要 GPU/torch。

### 离线批量摄取(目录 → KG)

把一个目录里的 Markdown(及偶发 PDF)离线复用现有管线灌进库。分两阶段:
先 `ingest`(无 LLM、快,chunk 问答即可用),再 `kg`(LLM 抽取,单独可恢复)。

```bash
# 1) 解析+分块+向量(无 LLM):新建库须用 --notebook-name 指定名字
PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir /path/to/md_dir --notebook-name "我的库"

# 2) 先小范围验证 KG 质量(只抽前 50 个未抽源)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 50

# 3) 整批抽 KG(幂等,跳过已抽;失败可重跑续抽)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx

# 或一条命令跑完(ingest 然后 kg)
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "我的库"

# 为基准层 notebook 构建可伸缩检索索引(离线;静态基准重建 KG 后需重跑)
PYTHONPATH=backend python scripts/batch_ingest.py index --notebook-id nb-xxxx

# 补该 notebook 缺失的 chunk + 节点向量(幂等;需 EMBED 配好)
PYTHONPATH=backend python scripts/batch_ingest.py embed --notebook-id nb-xxxx

# 一次性存储迁移:把旧的 JSON 文本向量转成 float32 BLOB(幂等,不需要 EMBED)
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --all-notebooks --workers 8

# 主动回填「来源删除反查表」(幂等,不需要 EMBED)
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --all-notebooks

# 补该 notebook 内已解析论文源缺失的元数据(标题/作者/机构/期刊/年份;幂等,需 LLM 已配好,不需要 EMBED)
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx --force

# 修复历史空源:对无 source_elements(上次 parse 未落地)的存量源重新 parse 补 elements,再重抽 KG
PYTHONPATH=backend python scripts/batch_ingest.py reparse --notebook-id nb-xxxx
```

`embed` 子命令只补**缺失**的 chunk 与 KG 节点向量(例如某次被 429 限流后留下的空洞)。必须给 `--notebook-id` 且 EMBED 已配好——它本身就是补向量的命令,故**忽略 `--allow-no-embed`**,EMBED 未配时直接报错退出。

`vectors-to-blob` 子命令是一次性存储迁移:embedding 向量过去以 JSON 文本存 SQLite,导致把几十万行加载成矩阵(建索引、检索冷启动)时大部分时间耗在 `json.loads` 上。现在新写入统一存成 float32 BLOB(`np.frombuffer` 零解析直接重解读字节),且所有读点都已兼容两种格式——所以这个命令是可选但推荐的升级后操作:它把四张 embeddings 表(`chunk_embeddings`、`knowledge_embeddings`、`element_embeddings`、`relation_embeddings`)里仍是 JSON 文本的旧行原地转成 BLOB,分批事务提交(每批 5000 行)并按表打印进度。它**不计算新向量**(故不需要 EMBED 配置),且幂等/可中断重跑——只选 SQLite 仍判定为 `text` 类型的行,跑第二遍时天然无行可转。用 `--notebook-id` 限定单个库,或 `--all-notebooks` 转换全库所有 notebook。`json.loads`/重编码这一步(百万行规模下的单核瓶颈)按 `--workers` 个进程并行(默认 `min(8, CPU核数)`;`--workers 1` 完全不启动进程池)——主进程始终独占全部数据库读写,SQLite 单写者不变;进程池崩溃时自动回退串行,绝不丢run。

`backfill-source-index` 子命令主动填充 `knowledge_object_sources` 反查表(`object_id, source_id`)——删除或重解析某个来源时,需要找出哪些 KG 对象引用了它;没有这张表,该查找就得逐行 `json.loads` 整本 notebook 的 evidence JSON 才能找到匹配,几十万对象规模下很慢。这张表本来会「首用惰性回填」(未迁移库的第一次来源删除/重解析会付一次全扫描,扫描的同时顺带填表并标记该 notebook,此后每次都是索引直查)——这个命令让你提前批量付这笔成本(有界内存分批 + 打印进度),而不是让某个用户操作(删除来源)撞上它。不需要 EMBED 配置,且幂等/可中断重跑(每次重跑都清空并按当前 evidence 重建该 notebook 的行,再重新标记)。用 `--notebook-id` 限定单个库,或 `--all-notebooks` 覆盖全库所有 notebook。若怀疑某库的反查表与实际 evidence 不一致(例如异常中断后),重跑本命令即是修复手段——它总是按当前 evidence 全量重建。

`metadata` 子命令给 notebook 里还缺论文元数据(标题、作者、机构、期刊、年份)的来源补抽——适用于「论文元数据抽取」上线前就已入库的旧库,或抽取 prompt/校验升级后想刷新一遍。它只处理已解析、且看起来是论文的来源(doc_type 为空或 `academic_paper`);文本读的是库里已存的解析产物(source elements),原始 PDF 不在磁盘上也能跑。必须给 `--notebook-id`(本子命令绝不新建 notebook),且要求 LLM 已配置(`KG_LLM_*`,缺省回退全局 `OPENAI_COMPAT_*`)——两者都未配时直接报错退出,不会静默跳过;不需要 EMBED 配置。幂等、可中断重跑:已有元数据行的源默认跳过,加 `--force` 则对本次范围内所有源强制重抽(例如 prompt/校验升级后)。进度按源逐行打印(`[meta <done>/<total>] <source-id> <status>`),结束打印各状态计数的 JSON 汇总。

`reparse` 子命令修复一类历史存量:某些源已建、`parse_status` 看似前进,却没有 `source_elements`(上次 parse 中断或未落地)。KG 抽取有一道零-LLM 接地校验——每个 LLM 抽出的节点必须把引文匹配回该源的某个 element,否则丢弃;一个源若没有任何 element,抽出的节点会被**整源丢光**,导致 `knowledge_objects` 一行不增(抽了等于白抽),且直接重抽永远补不出。旧版 `all` 的续跑分流曾用「有没有 KG」当「是否已 parse」,把这类无-elements 源当成「已 parse、缺 KG」直接送去抽取,正是踩中此坑(该分流已修正,新导入不再遇到)。本命令对该 notebook 内所有缺 `source_elements` 的源重新跑 `process_source`(parse → 生成 elements),收尾一次 KG rebuild;有 elements 的源自动跳过(幂等、可中断重跑)。`--limit N` 只处理前 N 个;`--no-rebuild` 跳过收尾聚类(分批场景)。必须给 `--notebook-id`。

**MRL 截断质量 spike(`app.eval.mrl_truncation`)。** 回答「把存量向量截断到前 1024/2048 维(+ re-normalize),检索质量掉多少」——这既是进程内向量内存瘦身(4096→1024 约 ÷4)的前置,也是 pgvector HNSW 建索引(维度上限 2000/4000)的 gate。只读、流式分块(百万行表内存有界),并总是先打印该 notebook 四张 embeddings 表的行数。

```bash
# 邻居保持率模式(默认):零 API 调用,任意 notebook 可跑——
# 从表内采样向量当查询,对比原维 vs 截断维的 top-K 排名重合率
( cd backend && python -m app.eval.mrl_truncation )                          # 自动挑最大的 notebook
( cd backend && python -m app.eval.mrl_truncation --notebook nb-xxxx --tables knowledge,chunk,relation --dims 2048,1024 )
# 超大表(如百万级 relation):语料侧也抽样——排名在同一子集内对比,
# 原维 vs 截断维的相对结论依然成立(稀疏子集读数略偏乐观;边界值请全量复核)
( cd backend && python -m app.eval.mrl_truncation --tables relation --sample-rows 50000 )

# gold 模式(需配置 EMBED 端点;每题按原生维 embed 一次):
# 对提交在仓库里的 gold 集算各截断档的 recall@12 / MRR 相对衰减
( cd backend && python -m app.eval.mrl_truncation --gold app/eval/recall_gold.yaml --notebook nb-b37185f4ae )
```

判据(出自 pgvector 迁移评审 spec):2048 档 recall@12 相对降 ≤1pt 且 top-10 重合 ≥0.9 → `halfvec 2048`;1024 档降 ≤3pt → `vector 1024`;降 >5pt 该档不通过。把整段输出贴回即可出结论。

**大型基础 KG(10^5–10^6 对象)。** 末尾的 unified 聚类是流式的(内存随**唯一归一化概念名数**而非总对象数有界),所以 `kg` 不物化全量向量即可扩展。超大语料可分批抽取、末尾一次聚类:

```bash
# 分批抽取(跳过昂贵的末尾聚类),按需重复
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 1000 --no-rebuild
# 末尾只聚类 +(重)建 scale 索引,不再抽取
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --rebuild-only
```

`--limit` 只限本轮**抽取**的来源数;最终聚类始终覆盖整个 notebook。大库(见上文 `SCALE_INDEX_AUTO_ENABLED`)在 `kg` 重建后会**自动重建**可伸缩检索索引(不会陈旧)。`KG_CLUSTER_REP_ANN_MAX`(默认 2,000,000)封顶 rep-ANN 规模——超出则分片建索引并 WARNING(绝不静默截断)。

**并发调优。** 三个旋钮控制吞吐(与 429 压力):

- `--workers` —— `all` 阶段**同时抽取的文档数**(覆盖 `KG_JOB_CONCURRENCY`);`ingest` 阶段为文件解析并发;`vectors-to-blob` 阶段为 `json.loads`/重编码的并行进程数(默认 `min(8, CPU核数)`;`1` 完全不启动进程池)。
- `--embed-conc` —— embedding 并发(覆盖 `EMBED_CONCURRENCY`);`all` 阶段 chunk 向量在每篇文档管线的后台跑。
- `KG_EXTRACT_WORKERS`(`.env`,默认 16)—— KG 抽取 LLM 窗口级的全局总并发,跨所有文档共享(文档内 + 文档间)。
- `--pool-report-interval` —— `all`/`kg` 阶段每 N 秒打一行**实时线程池占用**(默认 15;`0` 关闭)。并排显示 KG-LLM(抽取窗口)池与 embedding 线程——如 `[pool 17:52:33] KG-LLM(window) 14/16 · 源(job) 8/8 · embed 6bg+20pool · 源完成 5/40`——用以确认 embedding 模型与 KG-LLM 在共享算力的模型服务上**同时**打满。

`all` 阶段 embedding 峰值并发 ≈ `--workers × --embed-conc`,两者同时调高易触发服务商 429,谨慎。若某次限流留下缺失向量,事后用 `embed` 子命令补修。

选项:`--owner`(notebook 属主用户名,大小写不敏感,默认 = admin 用户)、`--workers`(`all` 阶段同时抽取的文档数 = `KG_JOB_CONCURRENCY`,`ingest` 阶段为文件并发;`vectors-to-blob` 阶段为解析/编码进程池大小,默认 `min(8, CPU核数)`,`1` = 不启进程池)、`--embed-conc`(embedding 并发 = `EMBED_CONCURRENCY`;避 429)、`--limit`(kg 抽取子集——聚类仍覆盖全量)、`--no-rebuild` / `--rebuild-only`(分批大库构建时拆分「抽取」与「末尾聚类」)、`--fresh`(清空 rebuild checkpoint,强制 merge 审查 + 概念描述全量重裁;用于只换了 KG 模型/阈值、数据没变时——隐含强制 rebuild,`all` 阶段的末尾聚类同样适用)、`--allow-no-embed`(EMBED 未配时显式允许无向量降级;默认拒绝,不静默;`embed` 子命令忽略此项)、`--pool-report-interval`(`all`/`kg` 阶段每隔几秒自报线程池占用,显示 KG-LLM vs embed 并发以验证多模型同时打满;默认 15,`0` 关)、`--all-notebooks`(仅 `vectors-to-blob` / `backfill-source-index`:作用于全部 notebook 而非单个)、`--force`(仅 `metadata`:已有元数据行的源也重抽)、`--dry-run`(只扫描预估)。`embed` 子命令只补缺失的 chunk + 节点向量,需 `--notebook-id`。`vectors-to-blob` 子命令把旧 JSON 文本向量迁移成 BLOB,需 `--notebook-id` 或 `--all-notebooks`。`backfill-source-index` 子命令主动构建来源删除反查表,需 `--notebook-id` 或 `--all-notebooks`。`metadata` 子命令给已解析的论文来源补抽元数据(标题/作者/期刊/年份),需 `--notebook-id` 且 LLM 已配置。

前置:`.env` 配好 EMBED 与 `KG_LLM`(KG 抽取缺省回退全局 `OPENAI_COMPAT_*`)。EMBED 未配时 CLI **默认拒绝运行**——要无向量导入须显式 `--allow-no-embed`(此时跳过 chunk/KG 向量),绝不静默;KG 抽取在无可用 LLM 时报错。重复文件按内容哈希自动跳过;进度写 `<storage>/batch_ingest/<notebook>.jsonl`,中断后重跑自动续。

### 检索回放对照(`scripts/replay_retrieval.py`)

性能优化改动前后,证明"检索效果不变"的验收工具:拿一份固定问题集跑 reasoning 检索原语(`federated_retrieve` + `ppr_retrieve`),**不调用任何答案 LLM**,把命中的 id/分数序列存成 JSON;两次运行的输出可逐问题 diff。

```bash
# 记录一次(需 EMBED 端点已配置,用真实查询向量;仅读检索原语,不需要 LLM)
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt --out before.json

# --full:额外跑一遍完整 reasoning 编排层(plan/reflect 用固定子查询 + 立即 answer 的 stub 代替 LLM,
# 验证编排层改动的确定性部分等价),子查询从 plan.json 里取
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt \
    --full --plan-file plan.json --out before.json

# 改动后重新记录一次,再对照两份输出
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt --out after.json
python scripts/replay_retrieval.py --compare before.json after.json                  # --mode exact(默认):id + 分数序列须逐位相同
python scripts/replay_retrieval.py --compare before.json after.json --mode topk --k 30  # 只比较前 k 个 id 的集合重叠率与序(允许分数因 float32 化等改动而漂移)
```

`questions.txt` 每行一个问题;`plan.json` = `{"<问题>": ["子查询1", "子查询2", ...]}`。**必须从主 checkout 根目录运行**(`.env` 按当前工作目录加载,与 `batch_ingest.py` 相同)。`--owner` 复用与 `batch_ingest.py` 相同的属主解析(大小写不敏感,默认 = `"admin"`)。

退出码即验收结果,可直接接入 CI/脚本判定:`0` 成功(记录模式)或 `--compare` 全部一致;`1` `--compare` 发现不一致(两次运行结果有差异);`2` 对照发生前的前置条件失败(EMBED 未配置、notebook 不存在、或属主用户不存在)——CLI **直接报错退出**,绝不用零向量静默跑出误导性的"零召回"对照结果。

### 合并两个共享 base 库的部署(`scripts/merge_dbs.py`)

离线、非破坏性工具,用于把两个各自独立部署、但**共享同一个 base 库**(同一个 base notebook id)的 silicon-notebook 实例合并成一个。保留哪侧的 base 由 `--keep-base` 指定(通常选更全的那侧)——运行时会先打印两侧 base 的统计(`sources`/`chunks`/`knowledge_objects` 计数)供核对;两侧其余(个人)notebook 原样全部并入。源库的 `.db`/storage 文件只读,工具始终写出全新的 `--out` / `--out-storage`。两侧输入允许是旧 schema 版本——合并前会先各自迁移到最新(在私有临时副本上进行,不改动源文件)。

```bash
PYTHONPATH=backend python scripts/merge_dbs.py \
  --db-a A/silicon_notebook.db --storage-a A/storage \
  --db-b B/silicon_notebook.db --storage-b B/storage \
  --keep-base a \
  --out merged/silicon_notebook.db --out-storage merged/storage \
  --assume-same-users
```

- `--keep-base a|b` —— 保留哪侧的 base notebook(通常选更全的那侧)。
- `--assume-same-users` —— 两库存在相同 user id 时必须加此项,用于确认两侧确实是同一个人的账号,否则工具会中止以避免内容归属错乱。
- `--dry-run` —— 只迁移+校验+打印将会导入哪些 notebook,不产出任何文件;即使 `--out` 已存在也能预览。
- `--force` —— 覆盖已存在的 `--out` 文件。

前提条件:除共享的 base 外,两库的 notebook id 不得重叠——一旦撞车,工具会中止并列出冲突的 id。

**重要提醒:** `--db-a`/`--db-b` 要指向已静置(先停服务)的数据库文件。工具只拷贝 `.db` 文件本身,正在运行的部署若有未落盘的 `-wal` sidecar 不会被带上——直接对着运行中的实例合并可能静默丢失最近的写入。(工具自身做 schema 迁移时的写入会在使用前 checkpoint 回 `.db`,这部分是安全的;这条提醒针对的是你提供的源文件本身。)

合并完成后,把 `merged/` 产出(db + storage)部署到要保留下来的那台主机,首次启动后在 app 内触发一次索引重建(「重建索引」/「刷新图谱」)以重新生成 `kg_index`/`kg_viz`/ANN 等未被拷贝的产物。

## 当前限制

- 检索使用 SQLite 关键词（CJK bi-gram）+ float32 矩阵语义检索（每 notebook 独立缓存）。内存占用有界（约百 MB，旧版 Python list 约 1.3 GB）。BM25/FTS5 和 pgvector 放量方向后续再做。
- 大文档摄取已加固：贪心窗口化 KG 抽取（成本线性），并发 embedding 逐批落库。极大规模下可再接入 `sqlite-vec`。
- Ask 不再在请求路径里同步补齐 embedding 或全量扫描 source elements；使用已有的关键词/向量索引，在维护任务运行时仍保持响应；并输出每阶段计时（`ask_stage` 事件）。
- 统一 KG rebuild 改为显式且可观测（`GET /notebooks/{id}/unified-kg/status`）；摄取来源只标记图谱为 dirty 而非同步重建，打开图谱浮层不再自动重建（按需刷新）。
- 跨文档概念合并使用确定性别名归一化 + 有界 top-k 向量候选（可扩展到上千概念）；可选 LLM 预审（`POST /notebooks/{id}/unified-kg/merges/review`）对小批量近义词候选做高置信确认/拒绝。
- KG 抽取需要配置 `OPENAI_COMPAT_*`；离线 smoke 在需要验证检索/治理时会显式写入 KG 对象。
- 两层与深度推理尚属早期：图推理 Ask 模式（`mode="graph"`）为 opt-in / 实验性（Ask 面板开关仍驱动默认的 `chunk`/`reasoning` 路径）。把 notebook 标为 `base`/`personal`（经 `POST /notebooks/{id}/tier`）、边可信审核队列、晋升（个人→基准）现都已有专属前端控件（在分析工具栏）；一旦某 notebook 标为 `base`，tier 感知联合检索与 base 优先冲突规则自动生效。
- Notebook 分享采用链接复制/只读成员方式，不是实时协同编辑；写权限仍归 owner。
- PostgreSQL + pgvector 暂不阻塞本机 beta，后续再迁移。在 PostgreSQL repository 实现前，非 `sqlite:///` 的 `DATABASE_URL` 会直接报错，不再静默落到本地数据库。
- `off` 模式 PDF 回退用 pypdf layout 抽取（阅读顺序尚可、零新依赖）；但公式、表格、扫描/图片型 PDF 仍需 MinerU，见"用 MinerU 解析 PDF"。
- 用户记忆保持手动 opt-in，当前没有自动记忆行为。

## 验证

运行：

```bash
bash scripts/check.sh
```

该脚本进行后端语法检查（`py_compile`）、不读取仓库 `.env` 的离线 hermetic smoke、完整后端 pytest、递归发现的全部前端 `*.test.mjs`、`tsc --noEmit` 与 production build。缺少 `frontend/node_modules` 会直接失败，不再静默跳过前端门禁。

## 开发流程

每开始一个新的特性开发任务，默认先新建 git worktree，并在该 worktree 内基于新 feature 分支开发；完成后从该分支提交 PR。不要为了特性开发直接在本地主 checkout 里切分支。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。

对于已经批准的多步骤实施计划，默认采用 subagent-driven development：每个任务交给一个全新的实现子 Agent，并在进入下一任务前完成该任务范围内的规格符合性与代码质量审查。纯调研、设计、状态汇报和只读审查不要求创建 worktree 或使用子 Agent。

## 文档维护

后续只要产品行为、启动方式、架构或开发约束发生变化，需要同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`
