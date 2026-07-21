# silicon-notebook 架构

更新日期：2026-07-21

本文记录当前已经由代码与绿色回归测试固定的运行时边界。部署、环境变量全集与产品操作说明以 `README.md`、`README_zh.md` 和 `.env.example` 为准；协作约束以 `AGENTS.md` 为准。架构整改采用 contract-first strangler，不用文档中的目标结构反向描述尚未发生的迁移。

## 1. 真实行为与验证

历史说明与实现不一致时，按以下顺序判定真实行为：

1. 已通过的回归测试与 characterization test。
2. 被这些测试覆盖的生产代码。
3. `README.md`、`README_zh.md`、`AGENTS.md` 与本文。

第一阶段用 `backend/tests/test_architecture_documentation.py` 固定以下容易漂移的架构契约：

- Ask stream 的 transport 断连与用户显式取消是两种事件；前者不取消 detached worker。
- 检索联合范围按 mode 区分；知识对象的 exact-score `base` 次序不能泛化到 chunk 或 relation 检索。
- notebook 内页是来源栏 + 主区域的两列 workspace，主区域有 问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab；没有固定 Studio 右栏。
- Memory 独立于 source/chunk/KG，始终绑定创建者和一个 notebook；Agent candidate 与 confirmed-only notebook 正式检索是两个隔离平面。

本地 beta 保持 FastAPI + SQLite + Next.js 的双进程形态，不要求 PostgreSQL、pgvector、Docker、GPU 或本地模型服务器。LLM、embedding 与 reranker 仍只通过 URL 服务访问。MinerU 是独立的解析适配器：`MINERU_MODE=http` 调用远端 `mineru-api`，`MINERU_MODE=cli` 在隔离子进程运行 MinerU Python API，`MINERU_MODE=off` 使用 pypdf 回退。未配置服务时使用离线、确定性的回退路径。全新数据库不创建 demo notebook 或合成来源。

## 2. 运行时组件

### 2.1 进程与持久化

- `backend/app/main.py` 创建 FastAPI 应用，挂载认证、请求上下文、CORS、日志中间件和 `/api` 路由。
- `frontend/` 是唯一前端；Next.js/React/TypeScript 负责 notebook collection 与 notebook workspace。
- SQLite 默认位于 `.local/silicon_notebook.db`，原始来源文件默认位于 `.local/storage`。生产 repository 尚未切换到 PostgreSQL；非 `sqlite:///` 的 `DATABASE_URL` 会被拒绝，不能静默回落到本地库。
- SQLite 使用标准库 `sqlite3`、WAL 与 `busy_timeout`。模型向量以 float32 BLOB 持久化（历史 JSON 文本向量保持可读、可批量转换），并在查询时装配为有界的 float32 numpy 矩阵或显式维护的 scale index。

### 2.2 Repository 组合与兼容 facade

`backend/app/services/sqlite_repository.py` 中的 `SQLiteRepository` 是现有消费者使用的兼容 facade：它构造并持有内部组合根 `RepositoryRuntime`（`backend/app/services/repository_runtime.py`），公共方法只保留显式兼容 adapter 或单跳委托，不再通过 mixin 继承复用实现。AST guard 会验证每个委托的真实目标与 ownership manifest 一致；facade body 与 ownership debt 均为 0。依赖方向单向：facade → runtime → application services → stores → `SqliteDatabase`；被抽出的 service/store 不得反向 import facade。

- **SQLite 持久化**：`backend/app/repositories/sqlite/` 下是 identity / notebook / sharing / source / chunk / embedding / knowledge / governance / unified-KG / ask-state / report / memory / query / index-projection 等领域 store。这些 store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。它们共享唯一的 `SqliteDatabase`（connection factory、WAL/busy_timeout PRAGMA、实例级写锁）。application service 不拼装主业务库 SQL，只保留业务顺序、策略与 transaction seat。`SqliteMigrator` 持有 `SCHEMA_VERSION` 与版本化迁移注册表；启动顺序固定为 migrate → 恢复中断的 merge-review/Ask job → seed 与 admin 原地升级，后两步不进版本闸、每次启动照跑。
- **文件系统工件**：`backend/app/repositories/source_files.py`（原始上传文件）与 `backend/app/repositories/filesystem/`（scale/viz 索引工件）。
- **业务编排**：application services（摄取、检索、evidence context、Ask、报告、KG lifecycle/governance、分享/深拷贝、scale runtime）由 runtime 组装；service 不直接拼 SQL。SQLite 专用的运维能力（批量 backfill、raw build/fold、诊断投影）归 maintenance adapter，不进入可移植 ports。
- **消费者契约**：`backend/app/repositories/ports.py` 按消费者划分可执行的小型 Protocol；最小 Protocol-only fake 可运行其声明支持的 Ask chunk/reasoning/graph/stream、report 与 evaluation 路径，不需要 facade 或 private runtime。`app/services/repository.py` 保留为兼容 import 入口。未来 PostgreSQL adapter 只需在同一 ports 后替换 store 层（本地 beta 不实现）。
- **运行态与启动补偿**：`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有，组合完成后的受支持替换会同步到全部既有消费者。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再重新抛出提交异常；成功 worker 的顺序及 Ask begin/save/finish/cleanup transaction checkpoint 不变。
- **旧库兼容**：迁移版本闸 + 冻结 v9 fixture（`backend/tests/fixtures/repository_v9/`、`test_repository_v9_fixture.py`）+ `test_legacy_db_compat.py` schema golden 共同守护「重构前创建的数据库直接打开、迁移、读取」。`scripts/verify_repository_snapshot.py` 以 backup-only 方式验证真实旧库：逐版本 migration manifest 精确列出允许新增的表/列/index/trigger/view，稳定 seed manifest 只接受指定主键与值；SQLite URI 路径经百分号编码。repository 只在临时 backup/storage 上构造；cleanup 失败时只输出保留的 backup 路径，不输出私有行。原 DB/WAL metadata 与 SHM 的存在性/大小都必须不变；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。

本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。

此后 master 先以 v11/v12 增加 SQLite 热路径索引，Agent Memory 在合并后使用 v13
migration；当前 schema 版本为 23。已提交的 v9 兼容 fixture 会经由 v10–v23 migration
升级并保持可读：v10–v12 覆盖兼容与 SQLite 热路径索引，v13–v15 覆盖 Memory/Agent
与 Memory 派生源 link/index，v16/v18 覆盖 knowhow 表与格子代码，v17 覆盖论文元数据，
v19 覆盖来源内嵌图片资产，v20 覆盖多领域参考库挂载与晋升目标，v21 为交互式规整的
anchor 成员检查加入 `(column_id, JS-trim(content_md), row_id)` 归一化表达式索引，v22
增加持久化的 notebook 级 KG 构建任务，v23 增加每用户最新模型服务状态；store
以同一 ECMAScript trim 表达式等值查询，避免在 `BEGIN IMMEDIATE` 中按保存单元扫描整列。

`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim；请求 Context、`_COPY_CHUNK` 与 `_remap_json_ids` 等兼容导出继续有效，既有测试 monkeypatch 接缝保持可用。

### 2.3 API、模型与领域服务

- `backend/app/api/routes.py` composes the domain FastAPI routers；aggregate 只负责组合顺序，不承载产品 endpoint body，也不提供兼容导出。边界契约直接检查各 domain router 的 endpoint 所有权，并以语义 AST 固定 aggregate 的组合清单与 `include_router` 调用；不依赖框架是否把子路由平铺（新版 FastAPI 会保留 lazy included-router 节点）。`system_routes.py`、`notebook_routes.py`、`source_routes.py`、`knowhow_routes.py`、`knowledge_routes.py`、`ask_routes.py`、`report_routes.py`、`kg_routes.py` 与 `admin_routes.py` 各自拥有领域 endpoint；`memory_routes.py`、`auth_routes.py`、`content_overview_routes.py`、`debug_logs.py` 与 Agent Knowhow router 保持独立。`mcp_server.py` 提供十一个工具（七个 Memory/context 与四个 knowhow）的 scoped Streamable HTTP 面；`deps.py` 承载访问控制依赖。
- 领域 Pydantic model 位于 `backend/app/models/` 的 `common.py`、`identity.py`、`memory.py`、`notebooks.py`、`sources.py`、`knowledge.py`、`kg.py`、`ask.py`、`reports.py`、`knowhow.py`、`content_overview.py`、`admin.py` 与 `model_services.py`。`backend/app/models/schemas.py` is a legacy compatibility facade：它只 re-export 同一 model object；领域模块不得反向 import facade 或 service/router/repository/store。
- `backend/app/services/kg/`、`kg_ingest.py` 与 `kg_merge.py` 负责 Concept / Claim / Formula / Procedure 的抽取、证据绑定、图推理、PPR、合并、质量过滤与 scale-index 支撑。
- `retrieval.py`、`retrieval_service.py`、`reasoning_retrieval.py` 与 `ask_modes.py` 负责关键词/向量召回、候选融合、查询改写、mode 注册和 reasoning 迭代。
- `report_engine.py` 负责两阶段深度报告；`background_jobs.py`、`cancellation.py` 和 repository 中的 job 状态共同管理后台任务与显式取消。
- `memory_service.py` 与 `memory_retrieval.py` 负责 owner/notebook 隔离的生命周期、revision/provenance、两个检索平面、Agent token policy 与 confirmed-only 正式投影；Memory 不写入 source/chunk/KG 表。
- `parsers.py`、`structural_markdown.py` 与 `mineru_client.py` 负责 PDF、Markdown、DOCX、PPTX、CSV、XLSX 等来源的结构化解析；FastAPI 进程不直接加载 torch 或 MinerU 模型。

### 2.4 前端边界

`frontend/app/page.tsx` 是 collection/workspace 编排器，不再是所有模型与面板实现的唯一所有者：

- `workspace-model.ts` 保存共享 API/视图类型与常量。
- `answer-panel.tsx` 保存答案、引用与 reasoning trace UI。
- `kg-type-model.ts` 保存内置知识类型文案/样式；`kg-type-mark.tsx` 消费并 re-export 该模型，保存答案与图谱共用的类型标记渲染。
- `ask-stream.ts`、`ask-reconnect.ts` 等 helper 保存流式问答和恢复行为。
- `frontend/app/api-client.ts` is the shared transport，负责 base URL、认证 header、JSON/empty/Blob、trusted error、网络失败与 AbortSignal mechanics；七个 domain API module 仍拥有 endpoint path、body、response type 与产品策略。

notebook 内页采用来源栏 + 主区域的两列 workspace，主区域提供 问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab。外层另有当前用户的总 Memory 页面，notebook 卡片数量可深链到局部 Memory tab。全屏 Knowledge Graph、看板和 Schema 是独立顶栏动作；「分析」菜单本身只含晋升队列（admin）、tier 切换（admin）与边审查队列。当前没有文章研究、思维导图、信息图或派生规则入口，也没有固定 Studio 右栏。

### 2.5 配置边界

关键配置由 `.env.example` 作为字段真源；LLM、embedding 与 reranker 保持 URL 驱动，MinerU 单独按解析模式选择远端服务、隔离子进程或 pypdf 回退：

- 数据与认证：`DATABASE_URL`、`SILICON_NOTEBOOK_STORAGE_DIR`、`SILICON_NOTEBOOK_ADMIN_PASSWORD`、`SILICON_NOTEBOOK_AUTH_OPTIONAL`。
- LLM：`OPENAI_COMPAT_BASE_URL`、`OPENAI_COMPAT_API_KEY`、`OPENAI_COMPAT_MODEL`、`OPENAI_COMPAT_TIMEOUT_SECONDS`。
- embedding：`EMBED_PROVIDER`、`EMBED_BASE_URL`、`EMBED_API_KEY`、`EMBED_MODEL`、`EMBED_DIM`。
- PDF：`MINERU_MODE`、`MINERU_API_URL`、`MINERU_BACKEND`、`MINERU_PARSE_METHOD`、`MINERU_LANG`、`MINERU_TIMEOUT_SECONDS`。
- KG / index 调度：`KG_AUTO_EXTRACT`、`KG_EXTRACT_WORKERS`、`KG_JOB_CONCURRENCY`、`SCALE_INDEX_AUTO_ENABLED`、`SCALE_INDEX_AUTO_WHEN`。
- Agent MCP：`MCP_PUBLIC_URL`；默认允许远程明文 HTTP 并放宽 Host/Origin 校验（仅可信内网），启动会打印明文告警；公网部署设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS + DNS-rebinding 保护。

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

知识对象 `federated_retrieve()` 跨 active 与其显式挂载的参考库集合（`notebook_bases`，可能为空）收集并标记 tier，其相关度 score 不乘 tier 常数，也不设置 tier 配额或地板；exact-score 的 `base` 次序只适用于知识对象命中。因此相关度更高的 personal knowledge hit 仍在前。`federated_retrieve_relations()` 的关系命中只按 score 降序，不使用 base 平局次序。

base 的权威性另在答案合成 prompt 中表达：如果 personal 与 base 证据矛盾，答案服从 base，并明确披露差异。这是 synthesis policy，不是 retrieval score policy，也不参与 grounding 阈值。

当前 Ask mode registry 的默认路径是 `chunk`；`graph` 为严格 KG 路径，`reasoning` 迭代执行计划、检索、反思并流式产出 trace。退役 mode id 只保留兼容映射，不能改回默认模式。

### 3.4 Memory 与 Agent MCP

Ask 回答先生成不落库的 preview，用户编辑确认后写入 owner-private confirmed Memory；LLM 不可用时
使用确定性 preview。外部 Agent 通过 `propose_memory` 只能写 candidate；同一用户、同一 notebook
下具备 `memory:read_candidates` 的 Agent token 可立即在候选平面召回。网页 Ask、notebook 搜索、
Deep Report 与 `search_notebook_context` 只投影 confirmed；rejected/deprecated 在两个平面都排除。

MCP 以 scoped opaque Agent token 认证，每个 session 必须先 `select_notebook`。数据工具每次重新检查
token 是否撤销/过期、profile 状态、scope、allowlist 与用户当前 notebook 访问权，不能仅信 session
缓存。Memory→KG 由创建者提案；admin queue 只展示脱敏后的结构化提取候选与服务端验证过的
evidence，不提供原始 revision/provenance 浏览。批准前会重新校验 Memory 当前仍为 confirmed 且
创建者仍有访问权，再经既有 dedupe/merge 创建或合并一个或多个 Base KG 对象，并在 API/审计中
保存完整 `base_object_ids`；私有 Memory 行仍归原创建者。

当前公开十一个工具：`list_notebooks`、`select_notebook`、`search_agent_memory`、
`search_notebook_context`、`get_memory`、`ask_notebook`、`propose_memory`、
`list_knowhow_tables`、`get_knowhow_discrimination`、`get_knowhow_row` 与
`put_knowhow_cell_code`；读取需相应 read scope，格子代码写入需 `knowhow:code`。

### 3.5 KG 与索引维护

- 新摄取数据使 unified KG 进入 dirty 状态，不在 Ask 请求路径同步整库重建。
- 打开 Knowledge Graph overlay 时读取当前图和 `GET /api/notebooks/{id}/unified-kg/status`；只有用户触发刷新时才调用 rebuild。
- KG 首次构建/整库重建使用显式 build/rebuild 端点；跨文档 merge review 只处理有界候选批次。
- vector cache 按数据版本失效；大库 scale index 由维护任务构建/刷新，并通过状态与 manifest 观察。即使 `SCALE_INDEX_AUTO_ENABLED` 开启，调度也发生在后台维护路径，而不是把全库 backfill 塞进 Ask。
- Ask 不同步补齐整库 embedding、不同步重建 unified KG，也不为 citation validation 扫描全部 source element。

### 3.6 深度报告

深度报告由 `report_engine.py` 作为可取消后台 job 执行。阶段一做语料侦察与多视角大纲，停在 `outline_ready` 供用户编辑；阶段二在确认后按 section 并行运行 reasoning 深挖并写成带证据纪律的 Markdown。状态、逐节进度、下载、批量导出、取消与删除都通过 report API 暴露，不能在请求线程内同步跑完整报告。

### 3.7 Knowhow 表投影与 Agent 面

Knowhow 表是自由列名 × 行的结构化领域经验。存储是 `knowhow_tables/columns/rows/cells/assets`
加 `knowhow_cell_code` 的 5+1 表 schema 域（`knowhow_store.py`），每张表挂一个隐藏合成源，复用既有
element/chunk 管线做检索。其投影（`services/knowhow/projection.py`）是唯一零 LLM 的 KG 写入方：
表级最多一个行标题列，设置后每个非空格子确定性地成为 `object_type=列名` 的知识对象，用既有
`about` 边连回行标题节点，同列同值短文本跨行归并；不设置则该表只做 chunk 检索投影、零图谱节点。
列内容类型（方法步骤/工具事物/普通）只是确定性解析提示。所有变更路径（格子编辑、导入、追加、
重投影、深拷贝发布）收敛到 per-table 防抖单飞的 `ProjectionScheduler`，经 `background_jobs`
后台执行；启动时对 legacy 角色词表的存量表做一次自动结构性重投影（零 LLM、零重嵌入）。

新表导入在请求层接受 `orientation=columns|rows`（默认 `columns`）。`grid_parser.py`
先提取 xlsx/csv/Markdown 原始矩阵；`rows` 模式将不等长行右侧补空后转置，再统一进入
表头校验、预览、建表和投影。方向不持久化；追加导入、存储网格、检索和 KG 投影始终保持
“列是属性”的内部契约。属性行预览默认建议规范化后的首列为行标题，用户仍可改选或不设置。

格子可挂代码附件（每格一份，与格子内容分离）：代码只存不执行，永不进 element/chunk/embedding/
FTS/KG，Ask 上下文不含（隔离不变量有专门测试守护）；`implemented`/`stale` 新鲜度由附件保存时的
格子净文本 hash 与当前内容对比在读取时推导。LLM 表达优化是显式按钮 + 对照预览 + 逐格确认回填，
绝不自动触发。

外部 Agent 面（REST `/api/agent/knowhow/*` 与四个 knowhow MCP 工具）与会话路由共用同一服务核心：
双鉴权依赖同时接受登录会话与 `snm_` Agent token，读取需 `knowledge:read`、代码写入需
`knowhow:code`，跨 owner 探测一律统一 404、不暴露存在性。判别集按列全量返回（刻意不做语义预筛），
行详情机器视图把图片剥成占位文本并附代码本体，供外部 Agent 自带判别/修复逻辑消费。

## 4. 关键行为契约

- **断连不等于取消**：transport 断连只停止向该客户端继续推送；detached Ask worker 仍执行并可持久化。只有显式 cancel endpoint 能设置 cancellation event。
- **显式中断端到端**：前端 interrupt 控件拿已返回的 `job_id` 调 cancel endpoint；worker 与流式 LLM 在保存最终回答前检查取消状态。
- **启动失败有持久化终态**：Ask/report 同步提交失败时，已创建的 job/report 进入 failed、进程内 cancellation entry 被注销，提交异常继续抛给调用方；正常完成顺序不变。
- **检索范围按 mode**：`chunk` 基线只读 active notebook；KG overlay/PPR 才可加入 federated KG/base-backed chunk；`graph`/`reasoning` 走 federated KG。
- **tier 次序只限知识对象**：`federated_retrieve()` 的 knowledge hit 完全平局时 base 作为第二排序键；relation hit 仍只按 score。base-wins 矛盾规则只属于回答合成。
- **升级不回填挂载**：迁移到 schema 20 只建 `notebook_bases` 表，不写入任何挂载行；所有既有笔记本挂载数清零，联邦检索对所有人停止，直到用户显式挂载一个参考库。
- **两列四 tab workspace**：固定区域只有来源栏与主区域；主区域含 问答 (Ask)、知识库 (Knowledge)、记忆 (Memory)、深度报告 (Deep Report)，当前没有固定 Studio 右侧栏。
- **Memory 双平面隔离**：candidate 仅同用户、同 notebook 的 scoped Agent 候选召回可见；正式 Ask、搜索、报告与 notebook context 只使用 confirmed。
- **source cleanup 边界**：reparse 保留 source 行和原始文件，替换解析/分块/embedding 并清理抽取派生；delete 再删除 source 行与本地文件。
- **维护工作显式可观测**：Ask 不承担整库 embedding、KG rebuild 或 scale-index build。图与索引状态必须可查询，重建/刷新由独立任务完成。
- **证据与治理一致**：只有 usable knowledge status 进入检索；所有图消费者排除 `review_status='rejected'` 的关系，并保持存储的 `source_object_id → target_object_id` 方向。
- **兼容 facade**：本阶段不改变 endpoint、SQLite schema、repository 公共方法、旧 import、前端交互或异步任务语义。
- **本地 beta 约束**：无 Docker 默认流程、无强制外部服务、无 demo 数据；模型服务通过 URL，测试保持离线且不读取真实密钥。

## 5. 当前模块边界

| 区域 | 当前所有者 | 当前边界与约束 |
|---|---|---|
| FastAPI 应用 | `backend/app/main.py` | 应用装配、中间件与 router 挂载；同步 SQLite 授权工作不能阻塞 event loop。 |
| API | `backend/app/api/routes.py` + domain routers、`auth_routes.py`、`deps.py` | aggregate 只负责组合顺序，不提供兼容导出；endpoint body 按领域所有权放置，保持路径、依赖与 response schema。 |
| API models | `backend/app/models/*.py` + `schemas.py` | domain module 是唯一 model definition 所有者；`schemas.py` 只作 legacy compatibility facade。 |
| SQLite facade | `backend/app/services/sqlite_repository.py` | 兼容 facade：显式委托到 `RepositoryRuntime` 组合的 store/service，不再承载领域 SQL；旧 import 与测试接缝保持。 |
| Repository stores | `backend/app/repositories/`（`sqlite/`、`source_files.py`、`filesystem/`、`ports.py`） | 主业务库 SQL 唯一所在；共享一个 `SqliteDatabase` 与版本闸 `SqliteMigrator`；ports 是消费者契约。 |
| Identity | `backend/app/repositories/sqlite/identity_store.py` | 用户、session、模型配置、管理员用量；`sqlite_identity.py` 为兼容 shim。 |
| Sharing | `backend/app/services/notebook_sharing.py` + `backend/app/repositories/sqlite/sharing_store.py` | share token、reader 权限、深拷贝与补偿/恢复；`sqlite_notebook_sharing.py` 为兼容 shim。 |
| KG | `backend/app/services/kg/`、`kg_ingest.py`、`kg_merge.py` | 抽取、证据、图、PPR、质量与合并；所有消费者共享 usable relation 规则。 |
| Retrieval / Ask | `retrieval.py`、`retrieval_service.py`、`reasoning_retrieval.py`、`ask_modes.py` 与 facade 中的兼容方法 | 分数、grounding 与 tier 次序保持分离；mode registry 是 mode 真源。 |
| Reports | `backend/app/services/report_engine.py` | 两阶段后台 job，保持 outline 审阅、取消与 section progress 语义。 |
| Memory / MCP | `memory_service.py`、`memory_retrieval.py`、`memory_store.py`、`memory_routes.py`、`mcp_server.py` | owner+notebook 隔离；Agent candidate 与 confirmed-only 正式投影分离；token/scope/allowlist 每次调用重校验。 |
| Knowhow 表 | `backend/app/services/knowhow/`（`projection.py`、`api.py`、`grid_parser.py`、`textops.py`、`assets.py`）+ `repositories/sqlite/knowhow_store.py` + `api/knowhow_agent_routes.py` | 5+1 表 schema 域；唯一零 LLM KG 写入方；变更统一走 `ProjectionScheduler`；代码附件与检索/KG 严格隔离；会话与 Agent 面共享服务核心。 |
| Frontend workspace | `frontend/app/page.tsx` 加共享 model/panel/helper 与七个 domain API module | `page.tsx` 负责编排；`api-client.ts` 统一 HTTP mechanics，domain API module 保留产品 policy；共享类型、答案面板和 KG 标记不能复制回巨型组件。 |

Repository 侧的 persistence 与业务编排已按上表分层完成；应用边界已完成 router、model facade 与 shared transport 的领域分工。当前主要耦合点是 `page.tsx` 仍承担大量 workspace 异步状态，以及 FastAPI lifespan/application lifecycle composition 尚未独立；后续整改继续以现有 facade 和测试为保护层逐域迁移。

## 6. 已知架构债务与整改顺序

整改源自已批准设计的六阶段历史编号；Repository 相关的旧阶段 2、4、6 已合并为一个保持行为不变的 Repository composition refactor 交付（设计见 `docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md`）。下表按当前债务账本合并记录已完成工作与剩余项，列表序号不再等同于原阶段编号：

1. **2026-07-10 历史记录——行为契约与文档对齐**（已完成）：当时修正 Ask disconnect、mode-specific federation/tier 排序、三 tab 两列 workspace、source cleanup 与退役能力文档漂移，重写本文并加入文档契约测试；不改运行时代码。当前 workspace 已扩展为四 tab，见上文实时边界。
2. **Notebook 规模策略与 Repository ports**（已随 composition refactor 交付）：中性 `NotebookScaleProfile` 让 copy 与 retrieval 分别消费自己的策略；巨型 repository Protocol 拆成 `app/repositories/ports.py` 的领域小 Protocol，保留兼容组合类型。
3. **2026-07-21 历史记录——application boundary foundation**（已完成）：领域 FastAPI router 由 `app/api/routes.py` 组合，领域 Pydantic model 以 `schemas.py` compatibility facade 保持旧 import，七个前端 domain API module 共用 `api-client.ts` transport；public/domain seam 与等价性测试替代 aggregate-private coupling。完整 warm gate 已验证三 lane 均不超过 60 秒。
4. **前端 workspace 状态拆分**（计划项）：先增加可迁移的 helper/hook 行为测试，再抽 `useAskSession`、`useSourceLibrary`、`useKnowledgeGraphWorkspace` 与对应 panel；不引入新全局状态库，不改轮询节奏。
5. **FastAPI application lifecycle**（计划项）：repository 内部 runtime 组合、retrieval/Ask/report service 与取消/重连 characterization test 已交付；FastAPI lifespan 管理的 application runtime、executor shutdown 与统一应用生命周期仍延后为独立工作。

非目标包括一次性 clean-architecture 重写、在本轮引入 PostgreSQL/SQLAlchemy/容器/新模型服务，或借整改改变公开 API、数据库表、检索排序、Ask 持久化、断连/取消语义和 UI 布局。

## 7. 验证命令

文档行为契约与对应运行时回归：

```bash
cd backend
python -m pytest tests/test_architecture_documentation.py tests/test_ask_stream_cancel.py tests/test_two_tier_federated.py -q
```

Repository 组合与旧库兼容（fixture 重放 + backup-only 真库验证）：

```bash
cd backend
python -m pytest tests/test_repository_v9_fixture.py tests/test_legacy_db_compat.py tests/test_repository_snapshot_verifier.py -q
cd ..
python scripts/verify_repository_snapshot.py \
  --database backend/tests/fixtures/repository_v9/baseline.db \
  --storage-dir backend/tests/fixtures/repository_v9/storage
```

完整离线门禁与前端生产构建：

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
cd frontend
npm run build
```

`scripts/check.sh` 并行运行 backend、contracts、frontend 三个有界 lane；backend 默认使用 9 个 backend pytest worker，可用 `BACKEND_PYTEST_WORKERS` 覆盖；每个 lane
拥有独立进程组，controller 收到中断或终止信号时会终止并回收其 pytest/npm/Next.js
后代。静态契约用模块路径、限定 scope、操作类型、目标与审核计数作为语义身份；
源码行号/offset 仅供诊断，不得用作预期站点身份。前端纯逻辑/语义契约使用
`*.test.mjs`，真实组件交互使用 `*.component.test.tsx` +
Vitest/jsdom/Testing Library；策略同时覆盖测试入口和 helper 模块。pytest controller
在 xdist worker 启动前预热仓库本地 Matplotlib 字体缓存，避免每个图谱 worker
重复执行 macOS 字体枚举。Apple Silicon warm gate 硬目标是不超过 60 秒；CI 各 lane 时长仅作观察，不把该本机目标变成 hosted runner 的 timeout 断言。
