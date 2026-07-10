# silicon-notebook 架构

更新日期：2026-07-10

本文记录当前已经由代码与绿色回归测试固定的运行时边界。部署、环境变量全集与产品操作说明以 `README.md`、`README_zh.md` 和 `.env.example` 为准；协作约束以 `AGENTS.md` 为准。架构整改采用 contract-first strangler，不用文档中的目标结构反向描述尚未发生的迁移。

## 1. 真实行为与验证

历史说明与实现不一致时，按以下顺序判定真实行为：

1. 已通过的回归测试与 characterization test。
2. 被这些测试覆盖的生产代码。
3. `README.md`、`README_zh.md`、`AGENTS.md` 与本文。

第一阶段用 `backend/tests/test_architecture_documentation.py` 固定以下容易漂移的架构契约：

- Ask stream 的 transport 断连与用户显式取消是两种事件；前者不取消 detached worker。
- 检索联合范围按 mode 区分；知识对象的 exact-score `base` 次序不能泛化到 chunk 或 relation 检索。
- notebook 内页是来源栏 + 主区域的两列 workspace，主区域有问答 / 知识库 / 深度报告三个 tab；没有固定 Studio 右栏。

本地 beta 保持 FastAPI + SQLite + Next.js 的双进程形态，不要求 PostgreSQL、pgvector、Docker、GPU 或本地模型服务器。LLM、embedding 与 reranker 仍只通过 URL 服务访问。MinerU 是独立的解析适配器：`MINERU_MODE=http` 调用远端 `mineru-api`，`MINERU_MODE=cli` 在隔离子进程运行 MinerU Python API，`MINERU_MODE=off` 使用 pypdf 回退。未配置服务时使用离线、确定性的回退路径。全新数据库不创建 demo notebook 或合成来源。

## 2. 运行时组件

### 2.1 进程与持久化

- `backend/app/main.py` 创建 FastAPI 应用，挂载认证、请求上下文、CORS、日志中间件和 `/api` 路由。
- `frontend/` 是唯一前端；Next.js/React/TypeScript 负责 notebook collection 与 notebook workspace。
- SQLite 默认位于 `.local/silicon_notebook.db`，原始来源文件默认位于 `.local/storage`。生产 repository 尚未切换到 PostgreSQL；非 `sqlite:///` 的 `DATABASE_URL` 会被拒绝，不能静默回落到本地库。
- SQLite 使用标准库 `sqlite3`、WAL 与 `busy_timeout`。模型向量以 JSON 持久化，并在查询时装配为有界的 float32 numpy 矩阵或显式维护的 scale index。

### 2.2 Repository facade 与领域接缝

`backend/app/services/sqlite_repository.py` 中的 `SQLiteRepository` 是现有消费者使用的公共 facade。它仍负责连接、迁移接入、摄取、检索、Ask、缓存与作业协调，因此不是最终的 persistence port。

已有两个高内聚 mixin 是渐进拆分接缝：

- `sqlite_identity.py`：账号、认证 session、用户模型配置与管理员用量查询。
- `sqlite_notebook_sharing.py`：分享 token、只读成员、读权限归属、notebook 深拷贝与清理生命周期。

facade 通过继承复用这两个实现，并保持既有 repository 方法、请求 Context、`_COPY_CHUNK` 与 `_remap_json_ids` 等兼容导出。后续迁移必须先建立小型领域 Protocol，再移动实现；不能一次性替换 facade 或改变公开 API。

### 2.3 API 与领域服务

- `backend/app/api/routes.py` 目前仍是聚合 FastAPI router，承载 notebook、source、Ask、knowledge、report 与治理端点；`auth_routes.py` 和 `deps.py` 分别承载认证路由与访问控制依赖。
- `backend/app/services/kg/`、`kg_ingest.py` 与 `kg_merge.py` 负责 Concept / Claim / Formula / Procedure 的抽取、证据绑定、图推理、PPR、合并、质量过滤与 scale-index 支撑。
- `retrieval.py`、`retrieval_service.py`、`reasoning_retrieval.py` 与 `ask_modes.py` 负责关键词/向量召回、候选融合、查询改写、mode 注册和 reasoning 迭代。
- `report_engine.py` 负责两阶段深度报告；`background_jobs.py`、`cancellation.py` 和 repository 中的 job 状态共同管理后台任务与显式取消。
- `parsers.py`、`structural_markdown.py` 与 `mineru_client.py` 负责 PDF、Markdown、DOCX、PPTX、CSV、XLSX 等来源的结构化解析；FastAPI 进程不直接加载 torch 或 MinerU 模型。

### 2.4 前端边界

`frontend/app/page.tsx` 是 collection/workspace 编排器，不再是所有模型与面板实现的唯一所有者：

- `workspace-model.ts` 保存共享 API/视图类型与常量。
- `answer-panel.tsx` 保存答案、引用与 reasoning trace UI。
- `kg-type-mark.tsx` 保存答案与图谱共用的知识类型标记。
- `ask-stream.ts`、`ask-reconnect.ts` 等 helper 保存流式问答和恢复行为。

notebook 内页采用来源栏 + 主区域的两列 workspace，主区域提供问答 / 知识库 / 深度报告三个 tab。全屏 Knowledge Graph、看板和 Schema 是独立顶栏动作；「分析」菜单本身只含晋升队列（admin）、tier 切换（admin）与边审查队列。当前没有文章研究、思维导图、信息图或派生规则入口，也没有固定 Studio 右栏。

### 2.5 配置边界

关键配置由 `.env.example` 作为字段真源；LLM、embedding 与 reranker 保持 URL 驱动，MinerU 单独按解析模式选择远端服务、隔离子进程或 pypdf 回退：

- 数据与认证：`DATABASE_URL`、`SILICON_NOTEBOOK_STORAGE_DIR`、`SILICON_NOTEBOOK_ADMIN_PASSWORD`、`SILICON_NOTEBOOK_AUTH_OPTIONAL`。
- LLM：`OPENAI_COMPAT_BASE_URL`、`OPENAI_COMPAT_API_KEY`、`OPENAI_COMPAT_MODEL`、`OPENAI_COMPAT_TIMEOUT_SECONDS`。
- embedding：`EMBED_PROVIDER`、`EMBED_BASE_URL`、`EMBED_API_KEY`、`EMBED_MODEL`、`EMBED_DIM`。
- PDF：`MINERU_MODE`、`MINERU_API_URL`、`MINERU_BACKEND`、`MINERU_PARSE_METHOD`、`MINERU_LANG`、`MINERU_TIMEOUT_SECONDS`。
- KG / index 调度：`KG_AUTO_EXTRACT`、`KG_EXTRACT_WORKERS`、`KG_JOB_CONCURRENCY`、`SCALE_INDEX_AUTO_ENABLED`、`SCALE_INDEX_AUTO_WHEN`。

新增可由环境覆盖的 pydantic v2 setting 必须使用 `validation_alias`；列表类值按现有 `NoDecode` 约定解析。

## 3. 核心数据流

### 3.1 创建与摄取

```text
创建 Untitled notebook 并立即打开
  → multipart 或受约束的公开 URL 导入 source
  → parse 为 SourceElement
  → chunk / element embedding 后台写入
  → 按 notebook 的 KG opt-in 状态执行或跳过 KG 抽取
  → 抽取时写入 knowledge_objects / knowledge_relations 与证据
  → 标记 unified KG / index 维护状态，由独立维护路径处理
```

source 状态沿 `queued → parsing → parsed → extracting → extracted` 推进，失败进入 `failed`。重新解析保留 source 行与原始文件；它替换旧 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。删除复用同一 source-derived cleanup，随后删除 source 行（外键级联 source-owned records）与本地文件。当前代码没有额外的文章产物清理步骤。`extracted` 的 UI 状态不等待后台 element embedding 全部结束。

### 3.2 Ask 与 detached job

`POST /api/notebooks/{id}/ask` 保留非流式兼容路径。`POST /api/notebooks/{id}/ask/stream` 先让 `ask_jobs` 行持久化 job 元数据与状态、让 `ask_trace_steps` 持久化后续 trace；cancellation event 注册在进程内，然后启动脱离 transport 生命周期的 worker：

```text
stream start
  → started {job_id}
  → detached worker 执行 chunk / graph / reasoning
  → progress / trace 事件尽力推送给当前客户端
  → worker 正常完成并保存 answer，job=done

transport disconnect / navigation / refresh
  → 停止向该客户端继续推送
  → 不设置 cancellation event
  → detached worker 继续并可保存结果

用户点击显式中断
  → POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel
  → 设置 cancellation event
  → worker / LLM 路径停止，取消的最终回答不保存
```

服务重启后仍为 `running` 的 job 会转为 `interrupted`；进程内 cancellation event 不会跨重启恢复。`GET /api/notebooks/{id}/ask/jobs/{job_id}` 返回 `status`、`trace`、`answer_id` 等 job metadata，不直接返回 `AskResponse`；job 完成后，前端重新加载 conversation 取得已持久化的最终回答。前端 logout 仍会终止本地流并重置用户态，但 transport 生命周期本身不拥有后台 job。

### 3.3 联合检索与回答合成

联合范围按检索路径区分：`chunk` 基线只读取 active notebook 的 chunk；启用 KG overlay 或 PPR 时，才可能加入 federated KG 上下文与 base-backed chunk。`graph` 和 `reasoning` 使用 federated KG 路径。

知识对象 `federated_retrieve()` 跨 active + base 收集并标记 tier，其相关度 score 不乘 tier 常数，也不设置 tier 配额或地板；exact-score 的 `base` 次序只适用于知识对象命中。因此相关度更高的 personal knowledge hit 仍在前。`federated_retrieve_relations()` 的关系命中只按 score 降序，不使用 base 平局次序。

base 的权威性另在答案合成 prompt 中表达：如果 personal 与 base 证据矛盾，答案服从 base，并明确披露差异。这是 synthesis policy，不是 retrieval score policy，也不参与 grounding 阈值。

当前 Ask mode registry 的默认路径是 `chunk`；`graph` 为严格 KG 路径，`reasoning` 迭代执行计划、检索、反思并流式产出 trace。退役 mode id 只保留兼容映射，不能改回默认模式。

### 3.4 KG 与索引维护

- 新摄取数据使 unified KG 进入 dirty 状态，不在 Ask 请求路径同步整库重建。
- 打开 Knowledge Graph overlay 时读取当前图和 `GET /api/notebooks/{id}/unified-kg/status`；只有用户触发刷新时才调用 rebuild。
- KG 首次构建/整库重建使用显式 build/rebuild 端点；跨文档 merge review 只处理有界候选批次。
- vector cache 按数据版本失效；大库 scale index 由维护任务构建/刷新，并通过状态与 manifest 观察。即使 `SCALE_INDEX_AUTO_ENABLED` 开启，调度也发生在后台维护路径，而不是把全库 backfill 塞进 Ask。
- Ask 不同步补齐整库 embedding、不同步重建 unified KG，也不为 citation validation 扫描全部 source element。

### 3.5 深度报告

深度报告由 `report_engine.py` 作为可取消后台 job 执行。阶段一做语料侦察与多视角大纲，停在 `outline_ready` 供用户编辑；阶段二在确认后按 section 并行运行 reasoning 深挖并写成带证据纪律的 Markdown。状态、逐节进度、下载、批量导出、取消与删除都通过 report API 暴露，不能在请求线程内同步跑完整报告。

## 4. 关键行为契约

- **断连不等于取消**：transport 断连只停止向该客户端继续推送；detached Ask worker 仍执行并可持久化。只有显式 cancel endpoint 能设置 cancellation event。
- **显式中断端到端**：前端 interrupt 控件拿已返回的 `job_id` 调 cancel endpoint；worker 与流式 LLM 在保存最终回答前检查取消状态。
- **检索范围按 mode**：`chunk` 基线只读 active notebook；KG overlay/PPR 才可加入 federated KG/base-backed chunk；`graph`/`reasoning` 走 federated KG。
- **tier 次序只限知识对象**：`federated_retrieve()` 的 knowledge hit 完全平局时 base 作为第二排序键；relation hit 仍只按 score。base-wins 矛盾规则只属于回答合成。
- **两列三 tab workspace**：固定区域只有来源栏与主区域；主区域含问答、知识库、深度报告，当前没有固定 Studio 右侧栏。
- **source cleanup 边界**：reparse 保留 source 行和原始文件，替换解析/分块/embedding 并清理抽取派生；delete 再删除 source 行与本地文件。
- **维护工作显式可观测**：Ask 不承担整库 embedding、KG rebuild 或 scale-index build。图与索引状态必须可查询，重建/刷新由独立任务完成。
- **证据与治理一致**：只有 usable knowledge status 进入检索；所有图消费者排除 `review_status='rejected'` 的关系，并保持存储的 `source_object_id → target_object_id` 方向。
- **兼容 facade**：本阶段不改变 endpoint、SQLite schema、repository 公共方法、旧 import、前端交互或异步任务语义。
- **本地 beta 约束**：无 Docker 默认流程、无强制外部服务、无 demo 数据；模型服务通过 URL，测试保持离线且不读取真实密钥。

## 5. 当前模块边界

| 区域 | 当前所有者 | 当前边界与约束 |
|---|---|---|
| FastAPI 应用 | `backend/app/main.py` | 应用装配、中间件与 router 挂载；同步 SQLite 授权工作不能阻塞 event loop。 |
| API | `backend/app/api/routes.py`、`auth_routes.py`、`deps.py` | 总业务 router 仍偏大；拆分前后必须保持路径、依赖与 response schema。 |
| SQLite facade | `backend/app/services/sqlite_repository.py` | 现有公共入口；identity/sharing mixin 是迁移接缝，不是完成的 port 分层。 |
| Identity | `backend/app/services/sqlite_identity.py` | 用户、session、模型配置、管理员用量。 |
| Sharing | `backend/app/services/sqlite_notebook_sharing.py` | share token、reader 权限、深拷贝与补偿/恢复。 |
| KG | `backend/app/services/kg/`、`kg_ingest.py`、`kg_merge.py` | 抽取、证据、图、PPR、质量与合并；所有消费者共享 usable relation 规则。 |
| Retrieval / Ask | `retrieval.py`、`retrieval_service.py`、`reasoning_retrieval.py`、`ask_modes.py` 与 facade 中的兼容方法 | 分数、grounding 与 tier 次序保持分离；mode registry 是 mode 真源。 |
| Reports | `backend/app/services/report_engine.py` | 两阶段后台 job，保持 outline 审阅、取消与 section progress 语义。 |
| Frontend workspace | `frontend/app/page.tsx` 加共享 model/panel/helper | `page.tsx` 负责编排；共享类型、答案面板和 KG 标记不能复制回巨型组件。 |

当前主要耦合点是：`SQLiteRepository` 仍混合 persistence 与业务编排、`routes.py` 仍聚合多数业务端点、`page.tsx` 仍承担大量 workspace 异步状态。整改必须以现有 facade 和测试为保护层逐域迁移。

## 6. 已知架构债务与整改顺序

整改按已批准设计分六个独立阶段，每阶段单独提交/PR、同步最新 `master` 并运行完整门禁：

1. **行为契约与文档对齐**：修正 Ask disconnect、mode-specific federation/tier 排序、三 tab 两列 workspace、source cleanup 与退役能力文档漂移，重写本文并加入文档契约测试；不改运行时代码。
2. **Notebook 规模策略与 Repository ports**：引入中性 `NotebookScaleProfile`，让 copy 与 retrieval 分别消费自己的策略；把巨型 repository Protocol 拆成领域小 Protocol，同时保留兼容组合类型。
3. **FastAPI routers 与前端 API client**：按 notebook/source/ask/knowledge/report/admin 拆 router；统一前端 JSON、NDJSON、Blob、认证和错误解析。
4. **SQLite migrations 与模型边界**：把 migration registry、DDL 与 schema helper 迁到 `sqlite_migrations.py`；按领域拆 Pydantic 模型并从 `schemas.py` re-export 旧符号。
5. **前端 workspace 状态拆分**：先增加可迁移的 helper/hook 行为测试，再抽 `useAskSession`、`useSourceLibrary`、`useKnowledgeGraphWorkspace` 与对应 panel；不引入新全局状态库，不改轮询节奏。
6. **Runtime 生命周期与 Retrieval/Ask 实现**：引入 FastAPI lifespan 管理的 application runtime、job executor 与 cache coordinator，最后再把 retrieval/Ask 从 SQLite facade 迁出；保留取消、重连、缓存版本和大库守卫 characterization test。

非目标包括一次性 clean-architecture 重写、在本轮引入 PostgreSQL/SQLAlchemy/容器/新模型服务，或借整改改变公开 API、数据库表、检索排序、Ask 持久化、断连/取消语义和 UI 布局。

## 7. 验证命令

文档行为契约与对应运行时回归：

```bash
cd backend
python -m pytest tests/test_architecture_documentation.py tests/test_ask_stream_cancel.py tests/test_two_tier_federated.py -q
```

完整离线门禁与前端生产构建：

```bash
bash scripts/check.sh
cd frontend
npm run build
```
