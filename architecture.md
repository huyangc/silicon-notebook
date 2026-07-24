# silicon-notebook 架构

更新日期：2026-07-22

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

本地 beta 保持 FastAPI + Next.js 的双进程形态，repository backend 由 `DATABASE_URL` 在 SQLite 与 PostgreSQL 之间选择；发行默认 SQLite 快速启动不要求 PostgreSQL、pgvector、Docker、GPU 或本地模型服务器。生产启动固定为一个 FastAPI/Uvicorn worker，保证进程内的系统模型服务调度器就是部署全局容量边界。chat、embedding 与 reranker 仍只通过 URL 服务访问。MinerU 是独立的解析适配器：`MINERU_MODE=http` 调用远端 `mineru-api`，`MINERU_MODE=cli` 在隔离子进程运行 MinerU Python API，`MINERU_MODE=off` 使用 pypdf 回退。未配置服务时使用离线、确定性的回退路径。全新数据库不创建 demo notebook 或合成来源。

## 2. 运行时组件

### 2.1 进程与持久化

- `backend/app/main.py` 创建 FastAPI 应用，挂载认证、请求上下文、CORS、日志中间件和 `/api` 路由；生产拓扑固定单 Uvicorn worker，不允许用多进程复制模型容量。
- `frontend/` 是唯一前端；Next.js/React/TypeScript 负责 notebook collection 与 notebook workspace。
- SQLite 默认位于 `.local/silicon_notebook.db`，原始来源文件默认位于 `.local/storage`。DATABASE_URL 通过唯一的 repository factory 选择正式 repository 后端。运行时只有一个 active repository 后端，由 `DATABASE_URL` 集中选择。SQLite 和 PostgreSQL 都是可直接启动的后端；发行默认值仍是 SQLite。`SHADOW_DATABASE_URL` 只保留/校验，不选择 active backend，也不启用同步。
- SQLite 使用标准库 `sqlite3`、WAL 与 `busy_timeout`，模型向量存 float32 BLOB。PostgreSQL 使用有界 Psycopg pool、数据库事务/row/advisory lock 支持跨进程访问，向量存 float32 `bytea`；不安装也不需要 pgvector。

### 2.2 Repository 组合与兼容 facade

`backend/app/services/repository_facade.py` 中的 `RepositoryFacade` 是后端中立 facade；唯一 factory 根据已验证的 `DATABASE_URL` 构造 `SQLiteRepository` 或 `PostgresRepository`，两者注入同一个 `RepositoryRuntime` 组合边界。公共方法只保留显式兼容 adapter 或单跳委托，不再通过 mixin 继承复用实现。AST guard 会验证每个委托的真实目标与 ownership manifest 一致；依赖方向单向：factory/wrapper → facade → runtime → application services → stores；service/store 不得反向 import facade、判断 SQL dialect 或 import 对侧 adapter。

- **SQLite 持久化**：`backend/app/repositories/sqlite/` 下是 identity / notebook / sharing / source / chunk / embedding / knowledge / governance / unified-KG / ask-state / report / memory / query / index-projection 等领域 store。这些 store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。它们共享唯一的 `SqliteDatabase`（connection factory、WAL/busy_timeout PRAGMA、实例级写锁）。application service 不拼装主业务库 SQL，只保留业务顺序、策略与 transaction seat。`SqliteMigrator` 持有 `SCHEMA_VERSION` 与版本化迁移注册表；启动顺序固定为 migrate → 恢复中断的 merge-review/Ask job → seed 与 admin 原地升级，后两步不进版本闸、每次启动照跑。
- **文件系统工件**：`backend/app/repositories/source_files.py`（原始上传文件）与 `backend/app/repositories/filesystem/`（scale/viz 索引工件）。
- **业务编排**：application services（摄取、检索、evidence context、Ask、报告、KG lifecycle/governance、分享/深拷贝、scale runtime）由 runtime 组装；service 不直接拼 SQL。SQLite 专用的运维能力（批量 backfill、raw build/fold、诊断投影）归 maintenance adapter，不进入可移植 ports。
- **消费者契约**：`backend/app/repositories/ports.py` 按消费者划分可执行的小型 Protocol；最小 Protocol-only fake 可运行其声明支持的 Ask chunk/reasoning/graph/stream、report 与 evaluation 路径，不需要 facade 或 private runtime。`app/services/repository.py` 保留为兼容 import 入口。SQLite 与 PostgreSQL adapter 均在同一 ports 后提供实现，application code 不做 dialect 分支。
- **运行态与启动补偿**：`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有，组合完成后的受支持替换会同步到全部既有消费者。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再重新抛出提交异常；成功 worker 的顺序及 Ask begin/save/finish/cleanup transaction checkpoint 不变。
- **旧库兼容**：迁移版本闸 + 冻结 v9 fixture（`backend/tests/fixtures/repository_v9/`、`test_repository_v9_fixture.py`）+ `test_legacy_db_compat.py` schema golden 共同守护「重构前创建的数据库直接打开、迁移、读取」。`scripts/verify_repository_snapshot.py` 以 backup-only 方式验证真实旧库：逐版本 migration manifest 精确列出允许新增的表/列/index/trigger/view，稳定 seed manifest 只接受指定主键与值；SQLite URI 路径经百分号编码。repository 只在临时 backup/storage 上构造；cleanup 失败时只输出保留的 backup 路径，不输出私有行。原 DB/WAL metadata 与 SHM 的存在性/大小都必须不变；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。

本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。

当前 schema 版本为 31。这里指 SQLite schema。已提交的 v9 兼容 fixture 会经由 v10–v31 migration
升级并保持可读：v10–v12 覆盖兼容与 SQLite 热路径索引，v13–v15 覆盖 Memory/Agent
与 Memory 派生源 link/index，v16/v18 覆盖 knowhow 表与格子代码，v17 覆盖论文元数据，
v19 覆盖来源内嵌图片资产，v20 覆盖多领域参考库挂载与晋升目标，v21 为交互式规整的
anchor 成员检查加入 `(column_id, JS-trim(content_md), row_id)` 归一化表达式索引，v22
增加持久化的 notebook 级 KG 构建任务，v23 增加每用户最新模型服务状态，v24 为写锁
瘦身的簇映射切换段增加 kg_canonical_scratch 表，v25 不可逆清除已存用户模型凭据与
旧状态并新增按服务 ID 存储的部署级健康状态，v26 增加 knowhow 表变更流水与命名
里程碑，v27 增加 sources.chunked_at 完成标记使「已就绪但无分块」的来源历史可判定
（合法零分块解析 vs 中途失败的分块），v28 新增 app_settings 全局设置表与可空的
user_profiles.upload_document_limit 列（每笔记本文档数量上限），v29 确定性清理旧的重复
cluster membership 并增加唯一索引，v30 增加 sources(notebook_id, file_hash) 去重查找索引
（内容哈希上传去重 / batch_ingest 续跑，此前是全表扫），v31 增加 inert、无 payload 的
shadow_change_log 与 shadow_capture_control 内部表；run-scoped guard/capture/freeze DDL
由迁移工具另行安装，guard 安装后立即强制唯一性，capture/freeze 行为在 run control
状态启用前保持禁用；配对 PostgreSQL 业务 schema 为 v9。SQLite store
以同一 ECMAScript trim 表达式等值查询，避免在 `BEGIN IMMEDIATE` 中按保存单元扫描整列。

`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim；请求 Context、`_COPY_CHUNK` 与 `_remap_json_ids` 等兼容导出继续有效，既有测试 monkeypatch 接缝保持可用。

### 2.2.1 PostgreSQL adapter 与切换边界

- `backend/app/repositories/factory.py` 是唯一 backend choice；PostgreSQL bundle 组合与 SQLite 对等的领域 store，共享一个有界 `PostgresDatabase` pool。启动 lease 覆盖 checksummed migration、恢复、warmup 与 readiness 发布，失败或被替换的实例只关闭自己的 pool。
- 跨进程访问由 PostgreSQL 自身的 MVCC、row/advisory lock 与 transaction isolation 处理，可消除 SQLite 的单 writer 文件锁争用；它不能消除业务层锁序错误或长事务，因此 pool acquire、statement、lock timeout 仍保持有界。
- 切换只允许“停写 → 停服务 → 一致备份 → 修改唯一 `DATABASE_URL` → 启动并自动 migration → status/`/api/ready`/认证/数量/代表性读取验证 → 放流量”。只改 URL 不复制数据。`SHADOW_DATABASE_URL` 不启用 dual-write；切回 SQLite 也不会回放 PG-only 写入。临时 `migration/shadow` 边界目前实现 SQLite31/PostgreSQL9/epoch1（60 张 replicated 表、四个逻辑键 guard）的严格只读 UTF8-first preflight、脱敏身份绑定确认、可删除且 checksummed 的 PG control schema、revision CAS，以及两侧独立 guard 报告后才可确认 capture 的控制原语；也实现绑定 run 的原子 SQLite snapshot 与有界可续跑 baseline COPY，逐批业务行/checkpoint 同事务提交，resume 精确证明 target prefix 而不 truncate/delete，七张 ordinal 表完成前通过 catalog dependency reseed，最终在 v9 ledger、FK、guard、ANALYZE 通过后把 forward checkpoint 原子推进到 snapshot H0。PG control mutation 统一取 migration→control 双锁并复核精确 catalog；需要 SQLite live gate 时先取得 PG pool/双锁/run row，之后才短暂 `BEGIN IMMEDIATE`，避免跨库逆序等待。增量 replicator、CLI 与端到端 worker 尚未实现，因此当前仍不存在可执行的在线 shadow 迁移流程。
- Baseline snapshot 目录必须 owner-only 且不可为 symlink；snapshot/live fence fresh 打开 `SqliteDatabase.db_path` 当前文件而不复用线程缓存连接，跨 open/transaction 及发布/PG commit 前复核 resolved path + device/inode。COPY 将全部业务 SQL 全限定到 run 绑定 schema，使用 named server cursor 有界复核 prefix，并用 statement timeout/阶段间取消轮询约束长操作。起始绑定、逐批提交/完成点和最终 H0 前均短暂取得 live SQLite `BEGIN IMMEDIATE` 来复核 capture 仍启用，但最终 60 表 proof/`ANALYZE` 期间不持有 SQLite；JSONB prefix proof 仅在 JSON 子树内统一有限 int/float/Decimal 的精确十进制语义（bool 排除、负零归零），普通 SQL 数值列仍保持类型差异；起始和最终另以 checksummed migration 派生契约验证精确 v9 table/column/PK/FK/unique/check、operational+GIN index 与 `public.pg_trgm`，逐批只走轻量 run/control/identity gate。
- 最终 live SQLite fence 是跨 commit 的 lease：只在 PG 双锁/run/table lock 与 60 表 proof/`ANALYZE` 完成后取得，持有期间写入并实际提交 PG H0 checkpoint + run progress，成功后才释放；PG 事务/commit 失败则不落 H0 并释放 SQLite。该 fence 期间不得再等 PG pool/advisory lock 或执行长 proof。
- PostgreSQL 依赖 `public.pg_trgm`，向量为 float32 `bytea`，不依赖 pgvector。生产仍用 `--workers 1`，因为模型 scheduler、breaker 与 cancellation registry 是进程内状态。
- `batch_ingest` 的 mutation phase 仅支持 SQLite；PostgreSQL 使用正常 application/API 摄取与 KG/index 流程。离线 `scripts/check.sh` 不连接 PostgreSQL；`scripts/check_postgres.sh` 和 CI 的独立 PostgreSQL 16 lane 验证 adapter、migration 与跨进程语义。

### 2.3 API、模型与领域服务

- `backend/app/api/routes.py` composes the domain FastAPI routers；aggregate 只负责组合顺序，不承载产品 endpoint body，也不提供兼容导出。边界契约直接检查各 domain router 的 endpoint 所有权，并以语义 AST 固定 aggregate 的组合清单与 `include_router` 调用；不依赖框架是否把子路由平铺（新版 FastAPI 会保留 lazy included-router 节点）。`system_routes.py`、`notebook_routes.py`、`source_routes.py`、`knowhow_routes.py`、`knowledge_routes.py`、`ask_routes.py`、`report_routes.py`、`kg_routes.py` 与 `admin_routes.py` 各自拥有领域 endpoint；`memory_routes.py`、`auth_routes.py`、`content_overview_routes.py`、`debug_logs.py` 与 Agent Knowhow router 保持独立。`mcp_server.py` 提供十一个工具（七个 Memory/context 与四个 knowhow）的 scoped Streamable HTTP 面；`deps.py` 承载访问控制依赖。
- 领域 Pydantic model 位于 `backend/app/models/` 的 `common.py`、`identity.py`、`memory.py`、`notebooks.py`、`sources.py`、`knowledge.py`、`kg.py`、`ask.py`、`reports.py`、`knowhow.py`、`content_overview.py`、`admin.py` 与 `model_services.py`。`backend/app/models/schemas.py` is a legacy compatibility facade：它只 re-export 同一 model object；领域模块不得反向 import facade 或 service/router/repository/store。
- `backend/app/services/model_registry.py` 持有稳定 workload 目录并加载部署 TOML；`model_provider.py` 是进程级模型访问组合根，按 workload 解析物理服务并复用每服务唯一的 `ServiceScheduler`；`model_scheduler.py` 与 `model_circuit_breaker.py` 持有容量、公平队列、截止时间与熔断状态。业务 service、repository、batch、探测路径都只能请求 workload adapter，不得直接构造/暴露 raw chat、embedding 或 rerank client。底层 HTTP 只存在于架构测试明确许可的 transport 边界。
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

notebook 内页采用来源栏 + 主区域的两列 workspace，主区域提供 问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab。外层另有当前用户的总 Memory 页面，notebook 卡片数量可深链到局部 Memory tab。全屏 Knowledge Graph 和看板是独立顶栏动作；图谱 Schema（知识对象类型/字段管理，仅管理员）已移入知识图谱视图头部的「图谱 Schema」按钮，不再是独立顶栏动作。「分析」菜单本身只含晋升队列（admin）、tier 切换（admin）与边审查队列。当前没有文章研究、思维导图、信息图或派生规则入口，也没有固定 Studio 右栏。

### 2.5 配置边界

系统模型配置由部署者统一管理，用户侧没有保存、编辑或测试草稿配置的能力。`.env.example` 是普通运行参数和密钥槽位真源，`model-services.example.toml` 是服务/绑定/容量模板；MinerU 单独按解析模式选择远端服务、隔离子进程或 pypdf 回退：

- 数据与认证：`DATABASE_URL`、`SILICON_NOTEBOOK_STORAGE_DIR`、`SILICON_NOTEBOOK_ADMIN_PASSWORD`、`SILICON_NOTEBOOK_AUTH_OPTIONAL`。
- 模型服务：`MODEL_SERVICES_CONFIG` 指向部署 TOML；`[services]` 声明服务种类、协议、URL、模型、`api_key_env` 和唯一容量参数 `max_concurrency`，`[bindings]` 把稳定 workload 映射到同种类服务。密钥只从 `.env` 中被 `api_key_env` 引用的变量读取；空路径是显式离线模式，非空但无效则启动失败。
- 模型调用调优：`OPENAI_COMPAT_TIMEOUT_SECONDS`、各 workload 的输出预算/重试、`EMBED_DIM`、`EMBED_RUNTIME_DIM` 与 embedding batch 设置。它们不改变模型并发容量；`EMBED_DIM` 必须匹配所绑定模型。
- PDF：`MINERU_MODE`、`MINERU_API_URL`、`MINERU_BACKEND`、`MINERU_PARSE_METHOD`、`MINERU_LANG`、`MINERU_TIMEOUT_SECONDS`。
- KG / index 调度：`KG_AUTO_EXTRACT`、`KG_JOB_CONCURRENCY`、自适应窗口参数、`SCALE_INDEX_AUTO_ENABLED`、`SCALE_INDEX_AUTO_WHEN`。来源 job 与本地 CPU/ANN 线程不是模型容量，所有模型调用仍受绑定服务的 `max_concurrency` 限制。
- Agent MCP：`MCP_PUBLIC_URL`；默认允许远程明文 HTTP 并放宽 Host/Origin 校验（仅可信内网），启动会打印明文告警；公网部署设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS + DNS-rebinding 保护。

模型服务状态是只读投影：`GET /api/model-services/status` 返回脱敏后的服务身份、workload 绑定、容量、运行/排队数、熔断与最近健康状态，不触发上游探测。只有 admin 能显式调用单服务或全服务 test endpoint。所有模型失败都携带安全 `support_id`，用户把它提交给维护人员，维护人员再以服务端日志关联具体坏掉的服务；状态与 UI 永不返回端点、凭据、provider body、prompt/response 或 raw exception。schema v24 已不可逆清空 `user_profiles.model_settings`、删除旧的逐用户健康行，并按部署服务 ID 持久化健康状态；个人配置路由与页面已下线。

新增可由环境覆盖的 pydantic v2 setting 必须使用 `validation_alias`；列表类值按现有 `NoDecode` 约定解析。

### 2.6 生产 DFX 诊断边界

生产诊断目标是 Ubuntu 24.04 上从仓库根执行 `npm run start` 的双服务形态，后端保持单
Uvicorn worker。它是内部基础设施，不新增前端 UI 或 API。卡顿现场的主路径是在操作仍然卡住时
运行 `python3 scripts/diag.py incident`；自动发现不能唯一选中仓库范围内的生产 Uvicorn 进程时，
才用 `--pid <backend-pid>` 绑定仍在运行的 worker，不能先重启再采集。

进程内 `backend/app/core/diagnostics_runtime.py` 维护有界 registry：活跃 request/phase、background
job、SQLite 操作、写锁 holder/waiter，以及 KG/LLM/embedding concurrency/readiness。所有 helper 在
runtime 未安装时为 no-op，安装后也必须 exception-safe；观测失败不传播到业务调用，不获取 SQLite
写锁，不改变 transaction 语义，也不按每条 SQL 持久化事件。SQL 只归一化为 verb/table/fingerprint，
永不持久化参数。运行态每两秒原子更新 `.local/diagnostics/runtime.json`，六秒以上的心跳按 stale
处理，不能参与高置信推断。machine-local snapshot 可保留精确 opaque notebook id 以关联只读 DB
证据；copyable report 必须把 notebook/request/job id 稳定映射为本报告内假名，并省略其它原始
opaque id。

主线程把 `SIGUSR1` 注册为 `faulthandler` 的不终止进程、全 Python 线程栈 dump，`all_threads=True`
且不采 locals。采集通过 `.local/diagnostics/incident.lock` 串行化，只读取本次追加的栈片段；
`.local/diagnostics/thread-dumps.log` 在成功采集后保持不超过 8 MiB。SQLite 分析从源 DB/WAL 的有界
副本读取，临时 workspace 位于 `.local/diagnostics/db-snapshots/`；源库不通过 SQLite 打开，不执行
checkpoint/vacuum/analyze/reindex/migration 或任何业务写入。诊断只允许维护这些有界工件，不需要
root，不重启/终止进程，也不自动修复。

运行态文件安全边界固定为当前用户拥有的 `0700` diagnostics 目录，以及同一用户拥有、单硬链接、
普通文件类型的 `0600` `runtime.json` / `thread-dumps.log`。writer 持有并复核目录 descriptor，临时
heartbeat 使用不可预测文件名、descriptor-relative 写入和原子替换；符号链接、硬链接、FIFO/device、
权限过宽或目录路径替换一律只增加降级计数，不跟随、不阻塞、不截断敌对目标。

`scripts/diag_incident.py` 在同一个最长 10 秒的 monotonic deadline 下组合 PID identity、Linux
`/proc` CPU/RSS/thread/FD/I/O/D-state、两次栈采样、loopback readiness、历史日志和最多一秒的 DB
probe；任一来源 missing/ambiguous/stale/busy/permission/deadline/corrupt/raced 时记录 category-only
degradation，并排除不完整信号，剩余采集继续。`scripts/diag_rules.py` 只消费 allow-listed metadata，
确定性排序最多三个假设，输出 `high`/`medium`/`low` 证据强度和安全下一步；默认 stdout 是一段最多
32 KiB 的 UTF-8 文本。空闲服务可以没有有效多信号结论，不能据此编造根因。

统一入口精确包含 `incident`、`slow`、`latency`、`locks`、`open`、`db`、`base-recall` 七命令；裸调用仍为
`slow`。七个命令及其 reporter 都是纯标准库、app-import-free。`base-recall` 复用 `db` 的
`O_NOATIME` pin、非阻塞锁、身份复核和有界 DB/WAL 拷贝，只在诊断自己拥有的快照上运行固定聚合
投影；它不构造 repository、不跑 application retrieval/migration、也不用 SQLite 打开源库。
legacy `<channel>.jsonl`、daily `<channel>-YYYY-MM-DD.jsonl`、daily gzip
`<channel>-YYYY-MM-DD.jsonl.gz` 与下一层 per-user 日志目录由共享 reader 有界读取、去重并统计
malformed/truncated；独立旧引擎入口继续可运行。

runtime snapshot 与 report 只含元数据；`base-recall` 的单段报告同样受 32 KiB 上限约束，只输出固定
状态、计数与本次报告内假名。诊断不持久化/打印 request body、用户控制的原始文件名、来源与
Ask/Memory/Knowhow 内容、prompt/model message、SQL 文本/参数、authorization/cookie/token/secret、
原始进程命令行或局部变量。脱敏报告离开可信团队前仍须人工复核。

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

### 3.2.1 系统模型服务调度

```text
业务调用选择稳定 workload + actor + 优先级 + deadline
  → RuntimeModelProvider 解析 workload → physical service
  → 该 service 的唯一 ServiceScheduler 排队/准入
  → 获得并发席位后调用 raw transport
  → 结果或故障更新同一 service 的 breaker / 健康观察
  → 用户错误仅返回安全 service/model 标签 + support_id
```

每个物理服务独立执行 TOML `max_concurrency`，总队列上限为 `10 × max_concurrency`，单 actor 排队上限为 `2 × max_concurrency`。调度按 interactive:report:background 固定 `8:2:1`，同优先级内按 actor round-robin；排队截止时间分别为 30/300/1800 秒。一次 fatal 或连续三次 transient provider 故障打开 breaker 30 秒，half-open 只允许一个探测调用。不同 service id 的容量、队列与 breaker 互不影响；batch 与在线调用共用同一流程，业务 worker 数不能乘大模型并发。

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

行详情或行标题分组矩阵物理分支的「智能补全空列」是另一个显式、建议式交互：`POST /api/notebooks/{id}/knowhow/{table_id}/rows/{row_id}/complete`
接收可选的 `target_column_ids`，只返回结构化 `retrieval_mode`、`retrieval_scope`、`retrieval_status`、`reasoning_trace`、服务端签发的库内 `evidence` 与 `suggestions`，不写库。
只有缺失格或精确空串可作为目标；纯空白存量文本不算空。服务先从当前位置附近至多 512 行构造候选池，只取至多 32 个已知列、
每格至多 1000 字符参与评分，再以固定大小 heap 挑出至多 8 条参考行；同一 anchor/行标题分组优先，其次按当前行已知列的相似度和覆盖度排序。
同一行的全部目标列只启动一次 `ReasoningRetriever`（`top_n=12`、`max_steps=6`），以当前行已知列与目标列构造最多 12000 字符的有效 JSON 不可信数据查询，对当前 notebook + 当前有效显式挂载库执行与 Ask `reasoning` 同源的 plan→federated retrieve→reflect/expand/follow-chain。补全专用 candidate policy 会在候选进入模型反思前过滤私有 Memory 派生证据和当前整张 table 的投影，并关闭无法在中间节点证明来源归属的 PPR/社区扩展；它只返回检索结果，绝不进入 Ask 的 answer synthesis、conversation/job 或 answer persistence。库内证据最多 24 张卡、合计最多 24000 字符，单卡摘录最多 900 字符；最终 prompt（含规则、schema、同表数据与库内证据）硬限 96000 字符，超限时先按证据预算截断并从最低优先参考行开始移除，绝不截掉规则或输出 schema。两个模型阶段都以 system 级指令把格子/检索文本视为不可信数据；模型只可回传服务端签发的 evidence key 或允许的同表 row id，未知引用被过滤，过滤后无引用的 suggestion 强制 abstain。base/personal 冲突时沿 Ask 合成规则以 base 为准并披露。检索使用 `reasoning_agent`，结构化合成使用 `knowhow_complete`；推理响应畸形、任一 provider 未配置/执行失败，或合成响应不可解析/顶层结构不可用时直接返回明确错误，单条 suggestion 畸形则过滤、降级或转成 abstain，禁止同表静默降级或离线伪补全。前端以可拖动审阅弹窗展示推理轨迹，并把同表参考与禁用链接/图片的库内 Markdown 证据分开供逐项人工接受；接受操作仍是
既有 cell PATCH，传 `expected_before=""` 和 `origin="llm_complete"`，从而在生成期间目标格被其他操作填入时返回冲突而不覆盖，
并保留正常的变更历史和投影调度。

外部 Agent 面（REST `/api/agent/knowhow/*` 与四个 knowhow MCP 工具）与会话路由共用同一服务核心：
双鉴权依赖同时接受登录会话与 `snm_` Agent token，读取需 `knowledge:read`、代码写入需
`knowhow:code`，跨 owner 探测一律统一 404、不暴露存在性。判别集按列全量返回（刻意不做语义预筛），
行详情机器视图把图片剥成占位文本并附代码本体，供外部 Agent 自带判别/修复逻辑消费。

每张 knowhow 表带完整变更历史（`knowhow_changes` + `knowhow_milestones`，schema v26）。15 个写方法
在各自写事务的最后一步经模块级 `record_change` 追加一条流水，存受影响实体的 before/after 加**变更后的
整表指纹**（复用传输守卫的 `_FINGERPRINT_SQL`，覆盖表元/列/行/格子/代码附件、刻意不含时间戳）；一条
架构守卫（`test_knowhow_history_coverage_guard.py`）保证白名单外的写事务默认报红，防将来新增写路径漏挂。
回退是纯 delta 反向重放：在一个写事务内先校验当前指纹等于最新流水的指纹（否则中止），从 head 逆序把
before 写回到目标点（行/列**原样复用 id**，引用跳转与代码附件才不断），再校验结果指纹等于目标点的指纹
（否则整事务回滚），最后追加一条 `revert` 流水——历史只增不减，故「回退的回退」天然成立。里程碑零快照，
只是给某个 seq 起名；流水被「清理历史」删除后里程碑保留为「已失效」不级联删。清理只删最老的连续前缀
（按 seq 不按 `created_at`，防时钟回拨挖洞）且永远保留 head。孤儿图片清扫器的存活引用集扩到历史流水，
故图片进过格子后基本不再自动回收——代价是「清理历史」要等最后一次引用不在 head 上时才释放该图。回退提交
后经同一个 `ProjectionScheduler` 触发全量重投影。详见 `docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md`。

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
- **系统模型容量唯一性**：部署 TOML 的每服务 `max_concurrency` 是唯一模型容量；单进程 provider/scheduler 同时承接在线、后台、报告和批处理，不允许第二套 gate、用户覆盖或进程数乘法。

## 5. 当前模块边界

| 区域 | 当前所有者 | 当前边界与约束 |
|---|---|---|
| FastAPI 应用 | `backend/app/main.py` | 应用装配、中间件与 router 挂载；同步 SQLite 授权工作不能阻塞 event loop。 |
| API | `backend/app/api/routes.py` + domain routers、`auth_routes.py`、`deps.py` | aggregate 只负责组合顺序，不提供兼容导出；endpoint body 按领域所有权放置，保持路径、依赖与 response schema。 |
| API models | `backend/app/models/*.py` + `schemas.py` | domain module 是唯一 model definition 所有者；`schemas.py` 只作 legacy compatibility facade。 |
| Repository facade/factory | `backend/app/services/repository_facade.py`、`sqlite_repository.py`、`backend/app/repositories/factory.py` | 中立 facade + 唯一 backend choice；SQLite wrapper 只保留 migration/maintenance 兼容接缝。 |
| Repository stores | `backend/app/repositories/`（`sqlite/`、`postgres/`、`source_files.py`、`filesystem/`、`ports.py`） | 每种 SQL 只在所属 adapter；两套 bundle 实现同一 ports，application 不判断 dialect。 |
| Identity | `backend/app/repositories/sqlite/identity_store.py` | 用户、session、管理员用量与 v24 用户模型配置清理兼容；不再提供运行时个人模型配置；`sqlite_identity.py` 为兼容 shim。 |
| 系统模型服务 | `backend/app/services/model_registry.py`、`model_provider.py`、`model_scheduler.py`、`model_circuit_breaker.py` + model-service status/admin routes | 部署 TOML 绑定 workload；provider 独占 adapter 解析，scheduler 按物理服务独占容量/队列/熔断；状态只读脱敏，admin 探测显式执行，support id 关联维护日志。 |
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

非目标包括一次性 clean-architecture 重写、在本轮引入 SQLAlchemy/容器/新模型服务、实现应用内 dual-write/shadow replication，或借整改改变公开 API、检索排序、Ask 持久化、断连/取消语义和 UI 布局。

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
