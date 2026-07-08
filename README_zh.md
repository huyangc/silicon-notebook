# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是一个面向半导体研发团队的 knowhow notebook 平台。它把上传的技术文档转化成可查询的知识图谱（Concept / Claim / Formula / Procedure 对象），并提供元素级 evidence 引用与接地多轮问答。

## 当前范围

当前仓库已进入以 KG-native 管线为核心的本机真实 beta 闭环：

- Python FastAPI 后端；SQLite 持久化路径 `.local/silicon_notebook.db`
- `frontend/` 下的 Next.js / React / TypeScript 前端
- OpenAI-compatible LLM 端点，用于抽取、回答和文章研究；embedding 通过 `EMBED_*` 独立配置
- 未配置 LLM/embedder 时全管线可离线运行（deterministic fallback）
- 干净起点：全新数据库只初始化本机用户，不预置 demo 笔记本或合成来源
- 支持 PDF、Markdown、DOCX、PPTX、CSV、XLSX 的 multipart 文件上传（异步 BackgroundTasks）
- **KG-native 摄取**：结构化 Markdown 解析 → 贪心窗口化 KG 抽取（Concept / Claim / Formula / Procedure）并发 embedding → 抽取优先状态（`extracted` = KG 就绪，不等 embedding）
- PDF 走 MinerU（公式/表格/版面）；本机或未配置时回退 pypdf
- 混合检索：CJK 感知 bi-gram 关键词 + float32 矩阵语义检索（每 notebook 独立缓存）
- KG-native 接地问答：逐句 `[k_i]` 引用（渲染为紧凑编号引用；模型直接输出的数字复合引用如 `[1, 2, 3]` 在能映射到已知引用时也可点击）、多轮会话、1-hop KG 邻居扩展，推理模式实时显示可展开的一行 agent 轨迹
- 两层知识库：每个 notebook 带 `tier`（`base` | `personal`，默认 `personal`）。`base` 是权威参考 KG（如模拟设计教材），`personal` 是用户自己的笔记。`federated_retrieve` 跨 `base ∪ 当前 personal` 收集候选、给每条命中打 tier 标签，排序时施加 base 权威权重；当 base 与 personal 冲突时答案以 base 立场为准并指出差异。引用携带其 tier（`AnswerAnchor.tier`），Ask 在每条引用上渲染 `base`/`personal` 标记。notebook 操作菜单（「分析」）提供「设为基准库 / 取消基准库」以把某 notebook 标为基准 KG 并撤销（经 `POST /api/notebooks/{id}/tier`）
- **用户系统**：自助注册（用户名规则：单个字母 + `00` + 6 位数字，如 `a00123456`，存储为小写）+ 密码登录，使用不透明 Bearer 会话 token。每个 notebook 由其创建者所有，用户只能看到自己的 notebook。首次启动时自动创建内置 `admin` 账号（登录用户名 `admin`，密码来自 `SILICON_NOTEBOOK_ADMIN_PASSWORD`，默认 `admin`）；admin 持有原有 notebook 并是唯一可将 notebook 标为基准库的用户。基准库 notebook 对普通用户的列表隐藏，但问答时仍作为权威检索上下文使用。本地/测试场景可设置 `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` 跳过登录。前端在首次加载时显示登录/注册界面，顶栏展示已登录用户名和退出按钮。
- 可选图推理问答模式（`mode="graph"`，opt-in / 实验性）：基于 `knowledge_relations` 构建 rustworkx 内存图，做有界多跳 derivation/support 链遍历，答题时做对抗式链路校验并给出最弱环 `chain_trust` 分（默认 Ask 仍为 `chunk`）
- 深度报告（两阶段后台任务）：notebook 级「深度报告」动作把一个问题变成多节技术报告。**阶段1（秒级）**:STORM 式多视角规划器——先做零 LLM 语料侦察（来源标题 + KG 命中 + chunk 出处,大纲不再盲规划）——预写出大纲,每节带**专家视角 / 跨视角张力 / 证据充分性判定**（充足/薄弱/缺失 + 缺口说明,来自零 LLM 检索探针 + rewrite 模型上的 Judge）;用户在**大纲编辑器**里审阅/修改后再确认。**阶段2（几分钟,确认后）**:每节独立跑一次完整 `reasoning` 深挖（节间并行,各自独立检索预算）,按三层证据纪律撰写（`[k]` 库内引用 /（推断）库内推断 /【通识】库外通识，行内标注且提示未经验证），最后汇总加执行摘要、参考文献，以及（仅当某节缺库内支撑时）结尾一行「局限」说明。研究深度控件为五个命名档「概览/标准/深入/详尽/穷尽」（默认「标准」，= 每节 reflect 步预算，在生成按钮旁弹出选择）用充分程度换时延；章节按 `KG_JOB_CONCURRENCY` 并行深挖，前端显示逐节实时进度（`section_status`）。以可取消的后台 job 运行；每份报告可下 `.md`，或多选批量下 `reports.zip`
- 边可信与治理：每条边的可信信号（evidence / 同源佐证 / 类型合法性）+ 高风险边优先的审核队列；被审核拒绝的边从图推理中排除
- 知识治理：通过 `/knowledge-types` + `/knowledge?type=...` 浏览任意对象类型，状态生命周期，重复检测与合并；`deprecated` 对象从检索和 1-hop 扩展中排除。个人→基准节点晋升（propose → under_review → approve/reject），批准时去重入库，配套策展晋升队列
- 统一 KG：跨文档概念聚类（`concept_clusters`），待合并审核
- Object 级 KG 可视化：Concept / Claim / Formula / Procedure 节点，类型形状、边标签、多选过滤、按类型分组侧栏
- Notebook 集合页（网格/紧凑/列表、编辑/删除）；点击「＋ 新建」直接创建 `Untitled notebook` 并进入，无弹窗
- 第一版不使用 Docker

PostgreSQL + pgvector 仍是后续生产/团队 beta 目标，当前本机开发不需要。

## 部署

silicon-notebook 以两个进程运行——FastAPI 后端 + Next.js 前端——数据落在本地 SQLite。
**无需 GPU、无需数据库服务、无需本地模型服务**:所有模型(LLM / 嵌入 / rerank /
MinerU)都经 URL 端点接入;在未配置任何模型时,整条管线以确定性回退离线运行。

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

- **LLM**(抽取、作答、文章研究)—— `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` /
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
```

`npm run start` 调用 `scripts/prod.sh`:前端 `next build` + `next start`,后端
`uvicorn --workers 1`,两者日志都落 `.local/logs/`。设 `SKIP_BUILD=1` 可复用已构建好
的 `frontend/.next`(如预构建镜像场景)。可用 `BACKEND_HOST` / `PORT` / `FRONTEND_PORT`
覆盖监听地址/端口。

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
bash scripts/check.sh                        # 后端离线 smoke + 前端测试 + tsc
```

后端会把结构化 JSONL 日志写入 `.local/logs/`(`requests` / `events` / `llm`);跟踪一次
上传或排查卡住的 source 见[可观测性 / 日志](#可观测性--日志)。

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
- 主栏：两个 tab——**问答**（接地问答，逐句 `[k_i]` 引用渲染为可点击编号引用，并支持有效的数字复合引用如 `[1, 2, 3]`，三种检索模式见下方「检索模式（问答）」一节，多轮会话列表、默认折叠的实时推理轨迹与可展开详情、👍/👎 反馈）和**知识库**（从 `/knowledge-types` 动态获取类型，支持状态生命周期、重复检测与合并）。问答输入框中 `Enter` 发送，`Shift+Enter` 保留换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制。中断会 abort `/ask/stream` 请求，并把取消信号传给后端，使正在执行的 Ask worker / LLM 路径停止，且不保存被中断的最终回答。主工作区不再固定展示尚未成熟的 Studio 右栏，让问答面板使用释放出的宽度。
- 知识图谱以全屏浮层打开：object 级 KG 节点（Concept / Claim / Formula / Procedure），类型形状，边关系标签，多选类型过滤，按类型分组侧栏（选中节点聚焦画布）。侧栏的「出处」以结构化证据卡片展示，长标题、位置、公式与中英混排正文会在面板内换行。
- Studio 类文章研究、思维导图/信息图生成、派生规则审核、治理**晋升队列**（把个人 KG 节点申请晋升到基准语料，并批准/拒绝待审请求）、**基准库/个人层切换**，以及**边审查队列**（按「高中心性 × 低可信」排序确认/拒绝关系，被拒边从图推理中排除）仍可从顶部分析工具栏进入，输出以弹窗形式展示，而不是占用固定右栏。

notebook 工作区隐藏集合页全局上边栏，采用偏工程风格的视觉治理。

## KG 抽取触发

源解析 + 向量化完成后即可做 chunk-native 检索，因此 **KG 抽取按 notebook「按需开启」，并非每次上传都抽**：

| 上传时 notebook 状态 | 是否抽 KG | 怎么触发 |
|---|---|---|
| 尚无 KG（新库） | **不**自动抽 | 按需构建：`POST /api/notebooks/{id}/kg/build`（界面：notebook 的**「构建知识图谱」**动作；在无 KG 的库上选严格推理模式时也会提示构建） |
| 已有 KG | 每个新源**自动后台抽取** | 无需手动触发——续抽以保持 KG 完整；新源随后增量融入跨文档统一 KG |

摄取期判定 = `KG_AUTO_EXTRACT 或 该 notebook 已有 KG`：

- `KG_AUTO_EXTRACT`（默认 `false`）——为 `true` 时**所有** notebook 每次上传都抽 KG。
- 否则仅当该 notebook 已有 KG 对象时，上传才抽。

即：**首次 opt-in**（构建 KG，或设 `KG_AUTO_EXTRACT=true`），之后新文档自动抽取 + 融合。整库重抽用 `POST /api/notebooks/{id}/kg/rebuild`；离线批量构建见「离线批量摄取」一节。

## 检索模式（问答）

`POST /ask` 按 `mode` 分派——注册表 `backend/app/services/ask_modes.py` 是唯一真源（默认 `chunk`）。所有模式都跨 `tier=base` ∪ 当前 personal 笔记本联合检索，产出逐句 `[k_i]` 锚点，并用同一口径判接地：`classify_evidence` → `grounded` / `overview` / `inferred`，对比校准过的 `EVIDENCE_TAU_*` 阈值。**排序信号（rerank / RRF / tier 权重）只重排候选，绝不进接地阈值**（阈值读每项的「关键词+语义」融合相关度）。

| 模式 | 分组 | 需 KG | 一句话 |
|------|------|-------|--------|
| **`chunk`**（默认） | general | 否 | chunk-native 通用问答：大召回 → 选择 → 长上下文综合 → 引用绑回源 chunk。 |
| **`graph`** | strict | 是 | 对跨文档知识图谱做单趟个性化 PageRank（PPR）传播。 |
| **`reasoning`** | strict | 是 | agentic 迭代 plan → retrieve → reflect → answer（流式输出实时轨迹）。 |

**`chunk` —— chunk-native，含可选 chunk×graph mix。**
- *基线：* chunk 大召回（`CHUNK_RECALL`）→ MMR / 多子查询配额多样性选择（`CHUNK_MMR_K`）→ 长上下文综合，不碰 KG。
- *mix*（仅当 `CHUNK_KG_OVERLAY_ENABLED=true` **且** 配齐 qwen3-rerank **且** 有 KG 时生效）：三路并池——(a) 向量 chunk、(b) query 种子周围的 KG 局部结构（实体 + 其 1-hop 关系，只检索一次）、(c) 这些 KG 对象背后的源 chunk——round-robin 合并 → qwen3 cross-encoder rerank → 按 token 预算装填（`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`）。答案在同一套 `[k]` 映射里同时引用 chunk 与 KG 项，接地跨 chunk ∪ KG。未配 rerank 或无 KG 时**字节等价回退**到基线。（忠实 LightRAG 的 `mix` 模式。）

**`graph` —— 跨文档 KG 上的 PPR。** 经 `federated_retrieve` 取种子（KG 实体 + 其源 chunk；`RELATION_RETRIEVAL_ENABLED=true` 时再融合关系索引命中）作为 HippoRAG 式**个性化 PageRank**（`GRAPH_PPR_ENABLED`，默认开）的个性化向量，通过共享知识图谱把相关度跨文档传播；排名靠前的 chunk 喂出接地答案，`[k]` 锚点指向 KG 对象/关系。`GRAPH_PPR_ENABLED=false` 时回退为沿推理边的有界 BFS。

**`reasoning` —— agentic 深挖检索。** 委托 `ReasoningRetriever`：拆解问题、检索（与 `graph` 同样走 PPR 传播）、反思是否充分，按需扩图/加子查询直到能回答——经 NDJSON stream（`/ask/stream`）输出 `reasoning_trace`。严格 / KG 接地。

退役 id `fast`、`global` 透明映射到 `chunk`（旧会话/书签不会 422）；其余未知 mode 返回 HTTP 422。

## API

当前 beta 的关键 API：

- `GET /api/notebooks`、`POST /api/notebooks`、`PATCH /api/notebooks/{id}`、`DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `POST /api/notebooks/{id}/sources` — multipart 文件上传（异步解析/抽取）
- `GET /api/sources/{id}`、`DELETE /api/sources/{id}`、`POST /api/sources/{id}/parse`、`GET /api/sources/{id}/elements`
- `GET /api/notebooks/{id}/knowledge-types`、`GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`、`PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask` — 接地问答（逐句 `[k_i]` 引用；`mode`：默认 `chunk` | `graph` | `reasoning`，见上文「检索模式（问答）」；tier 感知，跨 base + 当前 personal 联合检索）
- `POST /api/notebooks/{id}/ask/stream` — 推理模式问答进度的 NDJSON stream（先发 `progress` 轨迹事件并渲染为实时折叠摘要行，最后发完整 `AskResponse`）；客户端断开 / abort 会设置后端取消事件，使正在进行的 Ask 路径在写入最终答案前停止
- `GET /api/notebooks/{id}/conversations`、`GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- `GET|POST /api/notebooks/{id}/articles`、`DELETE /api/articles/{id}`、`POST /api/articles/{id}/research`
- 统一 KG：`POST .../unified-kg/rebuild`、`GET .../unified-kg`、`GET .../unified-kg/pending-merges`、`POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`、`GET .../objects/{object_id}/context`
- `GET /api/object-schemas`、`POST /api/object-schemas`、`PATCH /api/object-schemas/{type}`、`DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`、`POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- `GET /api/notebooks/{id}/derived-rules`、`POST /api/notebooks/{id}/derived-rules/{candidate_id}/approve|reject`
- 两层：`POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → 返回更新后的 `NotebookSummary`（tier 非法 400，notebook 不存在 404）。设置 notebook 的联合层（base = 权威参考 KG，personal = 默认用户笔记）。
- 边可信与策展：`GET /api/notebooks/{id}/edge-review-queue`、`POST /api/notebooks/{id}/relations/{rel_id}/review`
- 治理 / 晋升：`POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`、`GET /api/promotion-queue`、`POST /api/promotion-queue/{candidate_id}/approve|reject`
- 深度报告（两阶段）：`POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`;跑**阶段1 规划**后停在 `status=outline_ready`（`auto_generate=true` 则一路直出）。`GET .../reports/{rid}` 轮询状态 + 富 `outline`（每节 视角/张力/充分性）+ `content_md` + 实时 `section_status`。`PATCH .../reports/{rid}/outline` body `{sections}` 编辑草案大纲（仅 `outline_ready` 态,无有效节 422）。`POST .../reports/{rid}/generate` body `{depth?}` 启**阶段2 生成**（仅从 `outline_ready`,否则 409）。另 `GET /reports`（列表）、`POST .../cancel`、`DELETE`、`POST .../reports/export` `{report_ids}` → `reports.zip`。章节按 `KG_JOB_CONCURRENCY` 并行深挖。

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
DATABASE_URL            # SQLite 路径（默认 .local/silicon_notebook.db）
SILICON_NOTEBOOK_STORAGE_DIR   # 上传文件存储目录（默认 .local/storage）
```

**检索：**

```text
RETRIEVAL_TOP_N         # 1-hop 扩展前的 top-N 命中数（默认 12）
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
KG_COMMUNITY_SUMMARY_ENABLED # LLM 社区报告；Global 问答必需（默认 false）
ANSWER_CONTEXT_BUDGET_CHARS  # 答案上下文装配字符预算（默认 6000）
ANSWER_CONTEXT_MIN_ITEMS     # 不论预算至少保留 N 条（默认 3）
RETRIEVAL_RRF_ENABLED        # BM25(Okapi)+RRF 排序，替代关键词+语义融合（默认 false）
RETRIEVAL_RRF_K              # RRF 的 k（默认 60）
KG_QUERY_REFINE_ENABLED      # 答题前做问题感知证据精炼（默认 true）
QUERY_REFINE_MAX_CHARS       # 喂给精炼的证据最大字符数（默认 4000）
GLOBAL_MAX_COMMUNITIES       # Global 问答(ask mode="global")考虑的社区报告上限（默认 20）
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
REPORT_SECTION_TOP_N         # 深度报告：每节深挖保留的 KG 命中数（默认 12）
REPORT_SECTION_CHUNK_BUDGET  # 深度报告：每节 chunk 上下文字预算（默认 20000）
REPORT_SECTION_MAX_TOKENS    # 深度报告：每节撰写 max_tokens（默认 8192）
REPORT_ALLOW_PARAMETRIC      # 深度报告：允许【通识】层（库外通识，行内标注且提示未经验证，默认 true）
```

**两层知识库与图推理（Wave 1+2）：** 目前没有 `.env` 开关。notebook 的 `tier`
（`base` | `personal`，默认 `personal`）是 notebook 行上的数据，通过仓库方法
`mark_notebook_base()` 设置；一旦某 notebook 标为 `base`，tier 感知联合检索、base
权威排序权重（base `1.20` vs personal `1.00`）、以及答案里的 base 优先冲突规则即始终生效。
可选的图推理 Ask 模式（`mode="graph"`）多跳遍历用固定默认 `max_depth=3`、`max_fan_out=8`
（经 `getattr` 读取 settings，因此将来加 `GRAPH_MAX_DEPTH` / `GRAPH_MAX_FAN_OUT` env 覆盖无需改代码）。
边可信打分、策展审核队列、个人→基准晋升同样是行为，不由 env 控制。

**用户系统：**

```text
SILICON_NOTEBOOK_ADMIN_PASSWORD   # admin 登录密码（每次后端启动重置；默认 "admin"）
SILICON_NOTEBOOK_AUTH_OPTIONAL    # true = 无 token 请求回退为 admin（仅本地/测试）；
                                  # false（默认）= 所有请求必须登录
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

浏览器 DevTools console 会镜像请求为 `[api] 方法 /路径 -> 状态 N毫秒 (request_id)`；轮询期间 UI 显示当前阶段/已用时长，失败时展示来源的 `error_message`。

部署机慢因排查可直接在持有 `.local/` 的机器上运行 `python3 scripts/diag_slow.py`。
脚本除汇总请求、事件和 LLM 延迟外，还会基于 DB 聚合与 scale-index manifest 输出
strict reasoning / PPR 路径审计，用来判断大库是否只走 indexed core、chunk/relation ANN
是否齐全、delta 策略是否会放开未索引部分，以及跨 base 的路径是否可能触碰 active 全量向量。

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

**URL 来源（「添加链接」）优先用本地 MinerU。** 只要配置了本地 MinerU 服务（`MINERU_MODE=http`/`cli`），粘贴的 PDF 链接就由本地解析：后端把 PDF 下载下来，走与文件上传相同的「本地 MinerU→pypdf」路径，因此内网部署时内部 PDF 不会离开网络。`MINERU_API_TOKEN` 云端（mineru.net）仅在未配置本地 MinerU 时作为回退——一旦走本地，绝不会再静默调用云端。添加链接需要本地 MinerU 或云端 token 二者其一。

MinerU 输出会映射为结构化 `SourceElement`：公式→`formula` 元素（保留 LaTeX），表格→`table` 元素（HTML 存入 metadata），标题保留层级。前端在 source detail 里渲染它们——公式用 KaTeX、表格用其 HTML——所以公式是排版后的样子而不是原始 LaTeX。若 MinerU 不可达或出错，摄取会降级到 pypdf，保证上传不被阻塞，同时 pipeline log 和 source `error_message` 会保留回退诊断；若某 PDF 解析出 0 文本（如扫描/图片型 PDF），会给出提示而不是看起来"空成功"。

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
```

`embed` 子命令只补**缺失**的 chunk 与 KG 节点向量(例如某次被 429 限流后留下的空洞)。必须给 `--notebook-id` 且 EMBED 已配好——它本身就是补向量的命令,故**忽略 `--allow-no-embed`**,EMBED 未配时直接报错退出。

`vectors-to-blob` 子命令是一次性存储迁移:embedding 向量过去以 JSON 文本存 SQLite,导致把几十万行加载成矩阵(建索引、检索冷启动)时大部分时间耗在 `json.loads` 上。现在新写入统一存成 float32 BLOB(`np.frombuffer` 零解析直接重解读字节),且所有读点都已兼容两种格式——所以这个命令是可选但推荐的升级后操作:它把四张 embeddings 表(`chunk_embeddings`、`knowledge_embeddings`、`element_embeddings`、`relation_embeddings`)里仍是 JSON 文本的旧行原地转成 BLOB,分批事务提交(每批 5000 行)并按表打印进度。它**不计算新向量**(故不需要 EMBED 配置),且幂等/可中断重跑——只选 SQLite 仍判定为 `text` 类型的行,跑第二遍时天然无行可转。用 `--notebook-id` 限定单个库,或 `--all-notebooks` 转换全库所有 notebook。`json.loads`/重编码这一步(百万行规模下的单核瓶颈)按 `--workers` 个进程并行(默认 `min(8, CPU核数)`;`--workers 1` 完全不启动进程池)——主进程始终独占全部数据库读写,SQLite 单写者不变;进程池崩溃时自动回退串行,绝不丢run。

`backfill-source-index` 子命令主动填充 `knowledge_object_sources` 反查表(`object_id, source_id`)——删除或重解析某个来源时,需要找出哪些 KG 对象引用了它;没有这张表,该查找就得逐行 `json.loads` 整本 notebook 的 evidence JSON 才能找到匹配,几十万对象规模下很慢。这张表本来会「首用惰性回填」(未迁移库的第一次来源删除/重解析会付一次全扫描,扫描的同时顺带填表并标记该 notebook,此后每次都是索引直查)——这个命令让你提前批量付这笔成本(有界内存分批 + 打印进度),而不是让某个用户操作(删除来源)撞上它。不需要 EMBED 配置,且幂等/可中断重跑(每次重跑都清空并按当前 evidence 重建该 notebook 的行,再重新标记)。用 `--notebook-id` 限定单个库,或 `--all-notebooks` 覆盖全库所有 notebook。若怀疑某库的反查表与实际 evidence 不一致(例如异常中断后),重跑本命令即是修复手段——它总是按当前 evidence 全量重建。

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

选项:`--owner`(notebook 属主用户名,大小写不敏感,默认 = admin 用户)、`--workers`(`all` 阶段同时抽取的文档数 = `KG_JOB_CONCURRENCY`,`ingest` 阶段为文件并发;`vectors-to-blob` 阶段为解析/编码进程池大小,默认 `min(8, CPU核数)`,`1` = 不启进程池)、`--embed-conc`(embedding 并发 = `EMBED_CONCURRENCY`;避 429)、`--limit`(kg 抽取子集——聚类仍覆盖全量)、`--no-rebuild` / `--rebuild-only`(分批大库构建时拆分「抽取」与「末尾聚类」)、`--allow-no-embed`(EMBED 未配时显式允许无向量降级;默认拒绝,不静默;`embed` 子命令忽略此项)、`--pool-report-interval`(`all`/`kg` 阶段每隔几秒自报线程池占用,显示 KG-LLM vs embed 并发以验证多模型同时打满;默认 15,`0` 关)、`--all-notebooks`(仅 `vectors-to-blob` / `backfill-source-index`:作用于全部 notebook 而非单个)、`--dry-run`(只扫描预估)。`embed` 子命令只补缺失的 chunk + 节点向量,需 `--notebook-id`。`vectors-to-blob` 子命令把旧 JSON 文本向量迁移成 BLOB,需 `--notebook-id` 或 `--all-notebooks`。`backfill-source-index` 子命令主动构建来源删除反查表,需 `--notebook-id` 或 `--all-notebooks`。

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

## 当前限制

- 检索使用 SQLite 关键词（CJK bi-gram）+ float32 矩阵语义检索（每 notebook 独立缓存）。内存占用有界（约百 MB，旧版 Python list 约 1.3 GB）。BM25/FTS5 和 pgvector 放量方向后续再做。
- 大文档摄取已加固：贪心窗口化 KG 抽取（成本线性），并发 embedding 逐批落库。极大规模下可再接入 `sqlite-vec`。
- Ask 不再在请求路径里同步补齐 embedding 或全量扫描 source elements；使用已有的关键词/向量索引，在维护任务运行时仍保持响应；并输出每阶段计时（`ask_stage` 事件）。
- 统一 KG rebuild 改为显式且可观测（`GET /notebooks/{id}/unified-kg/status`）；摄取来源只标记图谱为 dirty 而非同步重建，打开图谱浮层不再自动重建（按需刷新）。
- 跨文档概念合并使用确定性别名归一化 + 有界 top-k 向量候选（可扩展到上千概念）；可选 LLM 预审（`POST /notebooks/{id}/unified-kg/merges/review`）对小批量近义词候选做高置信确认/拒绝。
- KG 抽取需要配置 `OPENAI_COMPAT_*`；离线 smoke 在需要验证检索/治理时会显式写入 KG 对象。
- 两层与深度推理尚属早期：图推理 Ask 模式（`mode="graph"`）为 opt-in / 实验性（Ask 面板开关仍驱动默认的 `chunk`/`reasoning` 路径）。把 notebook 标为 `base`/`personal`（经 `POST /notebooks/{id}/tier`）、边可信审核队列、晋升（个人→基准）现都已有专属前端控件（在分析工具栏）；一旦某 notebook 标为 `base`，tier 感知联合检索与 base 优先冲突规则自动生效。
- Article Studio 当前基于标题/摘要文本，并在存在关联 source 时使用其元素；一等文章全文上传与更丰富的关系打分是下一步（Tier 3）。
- PostgreSQL + pgvector 暂不阻塞本机 beta，后续再迁移。
- `off` 模式 PDF 回退用 pypdf layout 抽取（阅读顺序尚可、零新依赖）；但公式、表格、扫描/图片型 PDF 仍需 MinerU，见"用 MinerU 解析 PDF"。
- 用户记忆保持手动 opt-in，当前没有自动记忆行为。

## 验证

运行：

```bash
bash scripts/check.sh
```

该脚本进行后端语法检查（`py_compile`）和离线 hermetic smoke（`smoke_backend.py`——钉死 `mineru_mode=off`，不读真实 LLM/embedding 密钥），覆盖：上传/解析、结构化 Markdown 解析、KG 窗口化、并发 embedding 逐批落库、float32 向量矩阵构建与缓存、混合检索（关键词/向量/None 三态）、多轮 ask、状态机（`extracted` = 绿）、文章研究、反馈、JSON fence 清理、重启持久化。依赖已安装时同步运行前端 `node --test app/*.test.mjs` 和 `tsc --noEmit`。

## 开发流程

每开始一个新的特性开发任务，默认先新建 git worktree，并在该 worktree 内基于新 feature 分支开发；完成后从该分支提交 PR。不要为了特性开发直接在本地主 checkout 里切分支。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。

## 文档维护

后续只要产品行为、启动方式、架构或开发约束发生变化，需要同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`
