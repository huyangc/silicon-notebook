# silicon-notebook 架构

更新日期：2026-08-22

本文记录当前已经由代码与绿色回归测试固定的运行时边界。部署与环境变量全集以 `docs/deployment-and-configuration.md` / `_zh.md` 和 `.env.example` 为准，产品操作说明以 `docs/product-and-api.md` / `_zh.md` 为准；协作约束由 `AGENTS.md` 路由到对应权威文档。架构整改采用 contract-first strangler，不用文档中的目标结构反向描述尚未发生的迁移。

## 1. 真实行为与验证

历史说明与实现不一致时，按以下顺序判定真实行为：

1. 已通过的回归测试与 characterization test。
2. 被这些测试覆盖的生产代码。
3. `docs/` 下负责该主题的现行权威文档与本文；根 README 只作入口，`AGENTS.md` 只作 Agent 工作流和文档路由。

第一阶段用 `backend/tests/test_architecture_documentation.py` 固定以下容易漂移的架构契约：

- Ask stream 的 transport 断连与用户显式取消是两种事件；前者不取消 detached worker。
- 检索联合范围按 mode 区分；知识对象的 exact-score `base` 次序不能泛化到 chunk 或 relation 检索。
- notebook 内页是来源栏 + 主区域的两列 workspace，主区域有 问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab；没有固定 Studio 右栏。
- Memory 独立于 source/chunk/KG，始终绑定创建者和一个 notebook；Agent candidate 与 confirmed-only notebook 正式检索是两个隔离平面。

本地 beta 保持 FastAPI + Next.js 的双进程形态，repository backend 由 `DATABASE_URL` 在 SQLite 与 PostgreSQL 之间选择；发行默认 SQLite 快速启动不要求 PostgreSQL、pgvector、Docker、GPU 或本地模型服务器。生产启动固定为一个 FastAPI/Uvicorn worker，保证进程内的系统模型服务调度器就是部署全局容量边界。chat、embedding 与 reranker 仍只通过 URL 服务访问。MinerU 是独立的解析适配器：`MINERU_MODE=http` 调用远端 `mineru-api`，`MINERU_MODE=cli` 在隔离子进程运行 MinerU Python API，`MINERU_MODE=off` 使用 PyMuPDF4LLM 版面/Markdown 回退（pypdf 仅最后兜底）。未配置服务时使用离线、确定性的回退路径。全新数据库不创建 demo notebook 或合成来源。

## 2. 运行时组件

### 2.1 进程与持久化

- `backend/app/main.py` 创建 FastAPI 应用，挂载认证、请求上下文、CORS、日志中间件和 `/api` 路由；生产拓扑固定单 Uvicorn worker，不允许用多进程复制模型容量。
- `frontend/` 是唯一前端；Next.js/React/TypeScript 负责 notebook collection 与 notebook workspace。
- SQLite 默认位于 `.local/silicon_notebook.db`，原始来源文件默认位于 `.local/storage`。DATABASE_URL 通过唯一的 repository factory 选择正式 repository 后端。运行时只有一个 active repository 后端，由 `DATABASE_URL` 集中选择。SQLite 和 PostgreSQL 都是可直接启动的后端；发行默认值仍是 SQLite。`SHADOW_DATABASE_URL` 不选择 active backend，单独设置也不启动同步；只有临时 `migration/shadow` 运维组合根会把它作为 PostgreSQL target 读取。
- SQLite 使用标准库 `sqlite3`、WAL 与 `busy_timeout`，模型向量存 float32 BLOB。PostgreSQL 使用有界 Psycopg pool、数据库事务/row/advisory lock 支持跨进程访问，向量存 float32 `bytea`；不安装也不需要 pgvector。

### 2.2 Repository 组合与兼容 facade

`backend/app/services/repository_facade.py` 中的 `RepositoryFacade` 是后端中立 facade；唯一 factory 根据已验证的 `DATABASE_URL` 构造 `SQLiteRepository` 或 `PostgresRepository`，两者注入同一个 `RepositoryRuntime` 组合边界。公共方法只保留显式兼容 adapter 或单跳委托，不再通过 mixin 继承复用实现。AST guard 会验证每个委托的真实目标与 ownership manifest 一致；依赖方向单向：factory/wrapper → facade → runtime → application services → stores；service/store 不得反向 import facade、判断 SQL dialect 或 import 对侧 adapter。facade 公开面有逐方法调用者账本（`docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md`，由只读普查脚本 `scripts/audit_facade_callers.py` 重新生成），把每个公开成员按生产/脚本/测试三桶调用数分类为 `keep`/`test-only`/`ambiguous`/`retire-now`；退役按账本分批推进，每次退役需同步 `scripts/architecture_boundary_baseline.json::facade_public_surface` 与 `scripts/generate_repository_contract_fixtures.py --rebaseline-surface` 产出的 `facade_surface.json`/`ownership_manifest.py`。

模块化扩展的 Phase 0 与 Phase-1 retrieval host 合同已落地，selected-source graph 与 generated-question recall 是前两个真实内建 contributor。稳定跨层值下沉到 `backend/app/domain`；repository ports 只依赖 domain/models/core，不再反向 import service，当前 backend 静态 import 图为 0 SCC。全部 repository 的既有 services import 按 SQLite/PostgreSQL/other 分区记录为只许下降的债务上限。`backend/app/extension_sdk` 提供 manifest、四类 contribution（Provider / ProviderChain / Contributor / Observer）、point-specific retrieval context/result/budget/cancellation/provenance 和脱敏失败合同；capability `requires` 与插件 `depends_on` 分开，contribution 默认按稳定 ID 排序。`backend/app/extensions` 拥有冻结 registry 拓扑、capability 判定入口目录与 Ask/Report 共用的 `RetrievalContributorHost`；required capability 在 freeze 时只校验判定入口存在，availability 仍在每次调用时实时判定。一个 capability 不可用只关闭对应 contribution，workflow 仍进入共享扩展点并保留其他 contributor 输出。唯一外层组合根 `app.bootstrap` 解析 process-wide extension runtime，再把窄 domain host port 注入 repository runtime、Ask 与每次构造的 Report engine；业务 workflow 不 import registry/SDK。插件实现只能依赖 SDK/domain 和获授窄端口，不得 import 具体 services/repositories。Host 共享执行循环而不强迫统一物理时点：selected-source graph 在冻结 B 后执行 `selected_evidence`，generated-question 在 `_retrieve_chunks` 完成 baseline 后、MMR/fusion 前执行 `chunk_candidates`。Proposal 先有界，再由 core 一次批量水合权威 evidence；插件 value 不直接进入结果，禁止 per-hit N+1。Graph bridge 私有持有 baseline 与 graph service并复用原 activation result，因此 attestation、rollout、scope drift、duplicate-support overlay、独立 token budget、baseline manifest/eviction guard、status 与整段 fail-closed 行为不被通用 host 降级；Ask/Report 已无 service 直调。Generated-question bridge 私有持有 query、settings、index 与 `(scored, ids, matrix)`，在隔离 copy 上暂存 collision support，proposal/read 只用请求内存，host 完整接纳后才提交；off/trigger/empty/overflow/failure 与 shadow 都保持 baseline，on 只追加原 chunk。SQLite/PostgreSQL question scan 在 `LIMIT` 前应用 notebook/source 与 retrieval-run actor 的私有 Memory 谓词。Database adapter 提供无 I/O 的当前执行上下文连接探针，host 在持有 SQLite transaction boundary 或 PostgreSQL pooled lease 时于 contributor fan-out 前阻断，pool-size-1 conformance 钉住释放顺序。Availability probe 无 I/O，执行 context 按 manifest + live capability 做最小端口投影；invocation 与 core admission policy 启动时快照。核心 request cancellation 在单/多 query 路径传播，插件局部失败/超时 fail-open；call-scoped event sink 让 context 构造前的 unavailable/failure 也可观测。工作流若已知本次调用无法提供某个 point-specific access capability（特性未配置），可经 host 只读启动冻结快照（不碰请求状态、时钟、I/O 与 capability 判定）的 `has_contributions` 查询提前判定该扩展点是否已休眠，并把该 capability 作为 `disabled_capabilities` 声明给 `run`；休眠时在 call/context 构造前原样返回 baseline。两条生产 lane（`selected_evidence` 与 `chunk_candidates`）现在都在各自 disabled 分支这样做——`selected_evidence_lane_is_dormant` 与镜像它的 `generated_question_lane_is_dormant` 分别在构造 `*ContributionCall`/`*_call_context` 之前调用。被 `disabled_capabilities` 过滤掉的 contribution 不发 unavailable 事件——「部署未配置该特性」是调用方事实而非一次实时判定失败。没有适用 contribution 时 host 仍在任何工作之前原样返回同一个 baseline 对象。`scripts/check_architecture_boundaries.py` 在 G1 contracts lane 检查无环、domain/SDK 依赖方向、ports/repository 债务上限、唯一 composition root、插件依赖隔离以及 facade 公开面只减不增；同一守卫还检查 `app.core`/`app.models` 对 `app.services` 的反向依赖——allowlist 当前为空，任何 core/models→services 边都是违规（历史仅有的两条边已修复：`app.core.llm` 改为从 `app.domain.cancellation` 取消原语，`app.models.agent_profile` 自持 `PROFILE_LABEL_ORDER` 常量并被 `app.services.agent_profile_block` 正向 import）——并把一批热点函数的行数、以及 `backend/app/repositories/ports.py` 的 Protocol 方法总数各自钉在零余量上限上（只许降、降了必须同步 baseline）。后续迁移以模块化设计与交付计划为准。

Extension runtime admission 是叠在冻结 registry 拓扑之上的另一道闸，不改写 freeze 时刻决定的拓扑本身：唯一持有者 `app.core.extension_admission`（仅标准库，一个 frozenset 加一个单调发布 token，读路径零锁零 I/O）供 registry 的两个求值口消费——`contribution_availability` 与 `capability_availability` 在做任何 provider 判定之前先查这份快照，命中即短路返回 `Availability(DISABLED, "admin_disabled")`；`ui_projection` 把它折进既有 `"disabled"` 公开取值，wire 上不新增取值，builtin 插件永不落入这份快照。填充与消费物理分层：`app.core` 不 import `app.services`（这条边被 core/models→services 的零余量 allowlist 棘轮钉住）；填充侧 `app.services.extension_toggles`（读 `extension_runtime_toggles` 表、发布快照）不被 extension 层 import，两者只经唯一组合根 `app.bootstrap.create_application_repository`（建库、迁移跑完之后 prime 一次）单向联结——因此任何经这个组合根启动的进程（服务、CLI、批处理）从第一次判定起就带着当时的 DB 真值，而不是空默认。服务进程随后由 `startup_warmup.run_startup` 另起一条低频 daemon 轮询线程收敛其它进程的写入（间隔取自 `EXTENSION_ADMISSION_REFRESH_SECONDS`），零 `trust="deployment"` 插件的进程连这条线程都不起；CLI/批处理只有启动时那一次 prime，运行期间不再刷新。管理员写路径（`PATCH /admin/extensions/{plugin_id}`）额外在本进程内立即重新发布，发起改动的那次请求当次即可见；多个发布者之间用「读前先取号、按号排序应用」的规则防止一次开始得更早、却完成得更晚的慢查询，在发布顺序上撤销一次开始得更晚、却更早落地的写入。

Parser ProviderChain 是生产 ingestion 的唯一解析路由，启动拓扑为 self-hosted MinerU → MinerU cloud → builtin 三环；链序用 `after`/`before` DAG 表达并以稳定 ID 处理并列，不允许整数 priority。`app.bootstrap` 把 host 经 repository/runtime 的 domain port 注入 service，service 不依赖 SDK/registry。Runner 在任何 provider I/O 前冻结全链 core route，随后才做实时 availability；配置 self-hosted 后只能降级到 builtin。插件 probe 与 core admission/materialization 物理分层，workbook 拒收前零资产写，accepted materializer 才替换资产。URL 的 self-hosted/builtin 共享一次临时下载，同一来源的锁覆盖资产替换、parse、element replacement 与 chunk marker 发布。旧 dispatcher 与 facade patch seam 已删除，不保留双路真源。

`.zip` 由同一 backend parser capability registry 投影到上传校验、系统配置、前端格式提示与 MCP `add_source_file`，固定路由到 builtin `markdown_bundle`，不进入 MinerU。原始 ZIP 是一个来源；解析器只在内存中读取安全、唯一、stored/deflate 的包内成员，稳定遍历所有 Markdown，按每份 Markdown 自身目录解析相对图片并经既有 `persist_image` 端口落资产，从不把归档解到宿主文件系统。整包结构/总量错误原子拒绝，单图缺失或不支持只降级为图注/描述文本；重解析继续处于同一来源锁与资产代际替换边界内。

Ask reasoning 与 Deep Report 的应用编排都已迁到 `backend/app/application` 的不可变 stage envelope。Ask 的 prepared input、retrieval evidence、response draft、committed answer 是四个所有权交接点，其中 response draft 由**可注入**的 `ResponseDraftStage`（入口 `execute_response_draft_stage`）产出、默认实现 `DefaultResponseDraftStage` 就是既有内联的合成/绑定逻辑，它只收冻结的 `ResponseDraftInput`（激活与 fail-open 降级之后的证据 + 检索前的披露事实）、只欠一份 `ReasoningResponseDraft`，取消在 seam 前与提交边界各检查一次；提交边界按 prepared 复核 mode 与身份元数据（`notebook_id`/`question`/`conversation_id`/`user_id`/`job_id`/`asked_at`，不一致即 `StageBoundaryError`），`model_errors` 由 core 在 stage 返回后、检索 ContextVar reset 之前统一填充；Report 明确交接 confirmed planning、generated sections、core final audit artifact 与 committed report。application 的精确 import allowlist 禁止 implementation/SDK/registry 反向依赖。两条流水都显式绑定 source scope、point-specific retrieval run、取消权威、非空 actor 与注入连接探针；retrieval run 仍是 embedding single-flight 与 leaf-I/O semaphore 的唯一所有者，stage wrapper 不占外层 slot，也不移动任何 KG/chunk/element/PPR leaf。Report planning 与 generation 各创建新 run，保留可变 `ReasoningResult` 作为 generation 内的独占工作副本，不做 evidence/id-map JSON 或递归 deep copy。多节 all-retrieval barrier → 至多一次 synthesis → 并行 drafting、单节零 synthesis、final editor、claim ledger、citation remap、整篇图片 batch、zero-body failed 与 retry 顺序均不变；final-audit 边界额外禁止改写 section Markdown。连接持有或 authority 漂移抛显式 boundary error，不能伪装成 optional retrieval miss。

流式 Ask 的完成后扩展只有 `ask.completed_observer` 一个 point-specific host，它把既有 agent-profile、retrieval-experience 与 search-profile 三段后处理迁成三个内建 contribution。唯一组合根把 host 作为 domain port 注入 runtime，workflow 不 import SDK/registry。执行顺序保持 answer save → job done/unregister → browser final → agent-profile → reasoning-only retrieval-experience → search-profile → sentinel；三个 observer 仍串行、各自 fail-open，身份能力分别只有 notebook+actor、零身份、actor。入口/每贡献边界都用无 I/O connection probe 防止带 lease 调用插件；无插件或无适用 contribution 不触碰 clock/event/context/I/O。同步 POST Ask 与 MCP Ask 不进入该 streaming completion 口径，facade 没有新增公开插件 seat。

新增生产扩展点 `ask.gap_consult`（`GapConsultHost`）只服务逐步推理 Ask，接在 `_run_reasoning_stage` 内、**response-draft seam 返回之后、持久化之前**，每 run 恰好一次——触发判据读草拟前就冻结的检索事实，但调用本身、披露步与建议全部由 core 在 stage 之后落到响应上，注入的草拟实现从 envelope 里看不到任何 gap 痕迹（这正是「建议影响不了正文」的结构保证）；外发面只有一个有界 `GapConsultQuery`（用户实际见过的问题措辞——只有走过澄清门的 `resolved_question` 才算审阅过，自动确认的模型改写不外发——加至多两条缺口方向标签，均截断且剥除引用标记，不含任何 notebook/actor/source 身份）。宿主对每个 contribution 的可用性探测与 `consult` 调用共用一条私有 `daemon=True` 线程（不进线程池、不 `copy_context()`），受一个覆盖整次调用的硬墙钟 deadline 约束——这段预算花在答案返回给读者之前，与 `ask.completed_observer`/`report.completed_observer` 的协作式完成后 deadline 是两类不同的东西。产出经核心净化（URL 限 `http`/`https`、不截断改写）后填进 `AskResponse.gap_suggestions`，填充点在 response-draft stage 返回**之后**——不经过 `ResponseDraftInput`——故不是证据、进不了 `anchors`/`citations`、也不出现在公开会话分享投影里。

Deep Report 的终态扩展同样只有 `report.completed_observer`。SQLite/PostgreSQL 以 `WHERE status='generating'` 的同构原子完成写发布正文与 `done`，取消也只允许从非终态 CAS 到 `cancelled`，因此任一终态胜出后都不可反转；只有完成 CAS 成功才构造 `CommittedReport`。manual generate 与 auto-generate 都由 coordinator 汇入同一个 post-terminal 路径，且先退出 generation gate 与 report retrieval/source/model contexts，再运行内建 agent-profile observer，最后按 identity 注销取消事件，以保留既有 active-job 窗口。Observer 仅见 actor/notebook/report ref 和 opaque at-most-once core access；它不能回写正文、引用、参考文献、claim ledger 或终态，也不新建 retrieval/model/I/O 工作。

Retrieval 的 point-specific proposal source 与通用 admission reader 是两个独立 domain port。Selected-source graph 的权威复验只命中请求级内存 map，不增加 DB/leaf；只有其他未解析 proposal 才调用一次由 repository runtime 注入、在 SQL 读取前带 notebook/source ceiling 的批量 reader。Report fallback 读取进入同一 retrieval leaf gate；SQL 即使在兼容 no-scope 调用中也按 actor 过滤 Memory source，同时保留可见来源与 notebook-wide Knowhow。

- **SQLite 持久化**：`backend/app/repositories/sqlite/` 下是 identity / notebook / sharing / source / chunk / embedding / knowledge / governance / unified-KG / ask-state / report / memory / wish / query / index-projection 等领域 store。这些 store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。全局许愿墙由独立 `WishStorePort` 持有写入、列表和单用户点赞切换；跨问答与深度报告的管理员提问汇总仍归只读 `QueryStorePort`，不会把分析查询塞进按用户活动页面的前端拼接逻辑。它们共享唯一的 `SqliteDatabase`（connection factory、WAL/busy_timeout PRAGMA、实例级写锁）。application service 不拼装主业务库 SQL，只保留业务顺序、策略与 transaction seat。`SqliteMigrator` 持有 `SCHEMA_VERSION` 与版本化迁移注册表；启动顺序固定为 migrate → 恢复中断的 merge-review/Ask job → seed 与 admin 原地升级，后两步不进版本闸、每次启动照跑。
- **删除后的活动分析边界**：SQLite/PostgreSQL notebook store 在删除 notebook 聚合的同一事务、且级联发生前，把可见来源/提问/报告投影到无 notebook 外键的 `retained_user_activity`。该表只承载用户分析所需的归属、问题/提示、显示元数据、状态与删除/到期时间；答案、引用、轨迹、来源元素/正文、报告章节/参考文献继续由原聚合拥有并立即删除。query store 只合并未到期行，self-service 权限仍要求实时 notebook 可读，管理员才可查看删除后摘要。Ask 管理详情把 notebook 生命周期、self-service 的有效读授权链、job/trace 与单条 answer 投影放在同一 adapter 事务，并把锁持有到 API 响应对象组装完，避免删除或撤权提交后仍从先前无锁读取返回正文。启动恢复与下一次删除负责物理清理，`expires_at` 读闸负责精确的逻辑到期；两种 adapter 同形。
- **来源活动归因边界**：`sources.uploaded_by` 表示真实的可见来源提交者，只用于用户最近活跃；notebook owner 仍拥有文档用量与 owner-only 活动流。Memory/Knowhow 合成来源和深拷贝行不产生上传活动；删除留存同时保存 actor 与 owner，不能拿其中一方代替另一方。
- **文件系统工件**：`backend/app/repositories/source_files.py`（原始上传文件）、`backend/app/repositories/filesystem/`（scale/viz 索引工件）与 `backend/app/repositories/analysis_artifacts.py`（Excel 分析快照、解析问题最小元数据、来源隔离副本及模型 JSON 协议失败的完整请求/响应）。`AnalysisArtifactStore` 是后两类分析工件的唯一 writer；根目录固定在当前 storage 下的 `analysis-artifacts/`，不进入用户来源目录或业务数据库。管理员问题列表只经内容最小化的只读 projection 访问；完整模型正文必须再按一个随机案例 id 单条读取，所有接口都拿不到物理路径或哈希。笔记本删除会先销毁模型正文与可关联 id，再把中性分类记录移入不含原标识的归档路径；模型 prompt 没有可信的逐来源 id 账本，因此删除任一来源也会保守销毁该笔记本全部留存模型正文。notebook 业务 scope 在进入时从共享工件目录冻结持久生命周期代次，所有嵌套线程/模型调用沿用最早的同 notebook 快照；案例发布和单条正文读取持共享文件锁，来源/笔记本清理持排他文件锁并先推进持久代次，所以 Web、CLI 与 backfill 进程中删除前已物化输入的旧响应都无法在清理结束后重新写回正文。清理逐案例销毁正文，某条中性元数据归档失败不会阻断后续正文删除。
- **业务编排**：application services（摄取、检索、evidence context、Ask、报告、KG lifecycle/governance、分享/深拷贝、scale runtime）由 runtime 组装；service 不直接拼 SQL。SQLite 专用的运维能力（批量 backfill、raw build/fold、诊断投影）归 maintenance adapter，不进入可移植 ports。
- **消费者契约**：`backend/app/repositories/ports.py` 按消费者划分可执行的小型 Protocol；最小 Protocol-only fake 可运行其声明支持的 Ask chunk/reasoning/stream、report 与 evaluation 路径，不需要 facade 或 private runtime。`app/services/repository.py` 保留为兼容 import 入口。SQLite 与 PostgreSQL adapter 均在同一 ports 后提供实现，application code 不做 dialect 分支。
- **运行态与启动补偿**：`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有，组合完成后的受支持替换会同步到全部既有消费者。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再重新抛出提交异常；成功 worker 的顺序及 Ask begin/save/finish/cleanup transaction checkpoint 不变。组合按领域拆分：`RepositoryRuntime.__init__` 只按顺序调用模块级 `_build_*` 领域构造函数（外加它自己的两把 `threading.Lock()`），再把每个返回的 frozen bundle 的字段逐条显式挂到自己身上，一个座位一行。调用顺序即依赖拓扑——构造函数只接收更早的 bundle，绝不接收 runtime 本身，因此写不出回指组合根的环；唯一允许的runtime 绑定输入是窄的迟绑定 callable（当前用户访问器、`ask_service` 访问器与 `_note_ask_completed`）。进程级副作用（scheduler 校验、event logger、`kg_scheduler.initialize`）与那唯一一次持久化 bundle 构造保持原有顺序；`backend/tests/test_repository_runtime_composition.py` 冻结已挂载属性集合并钉住这两条规则。
- **旧库兼容**：迁移版本闸 + 冻结 v9 fixture（`backend/tests/fixtures/repository_v9/`、`test_repository_v9_fixture.py`）共同守护「重构前创建的数据库直接打开、迁移、读取」。`scripts/verify_repository_snapshot.py` 以 backup-only 方式验证真实旧库：逐版本 migration manifest 精确列出允许新增的表/列/index/trigger/view，稳定 seed manifest 只接受指定主键与值；SQLite URI 路径经百分号编码。repository 只在临时 backup/storage 上构造；cleanup 失败时只输出保留的 backup 路径，不输出私有行。原 DB/WAL metadata 与 SHM 的存在性/大小都必须不变；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。

本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。

当前 schema 版本为 39。这里指 SQLite schema。已提交的 v9 兼容 fixture 会经由 v10–v39 migration
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
状态启用前保持禁用；v32 增加 reports.understanding_json，持久化深度报告确认门之前的
问题理解契约；v33 增加
knowledge_relations(notebook_id, source_object_id/target_object_id, id) 覆盖索引，供关系词法补召回
稳定地做有界 keyset 查询；v34 增加关系补全水位与对象 keyset 索引，v35 增加
`ask_jobs.asked_at`；v36 增加 KG 质量分析的三张预计算产物表（kg_community_edges、kg_source_profiles 与产物账本 kg_analysis_artifacts）；rebuild_communities 在一个事务里整体重写它们，账本逐份记下产物建于哪个 kg_mutation_seq。三张表都不带 level 列——社区层的新鲜度闸本身不分 level，产物描述的 level 记在账本 payload 里；v37 为 `source_elements` 增加 `(source_id, element_type,
created_at, id)` 索引，供有界、按类型的集合枚举（公式/表格/图片/代码块清单）；
配对 PostgreSQL 业务 schema 为 v17。SQLite store
以同一 ECMAScript trim 表达式等值查询，避免在 `BEGIN IMMEDIATE` 中按保存单元扫描整列。

`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim；请求 Context、`_COPY_CHUNK` 与 `_remap_json_ids` 等兼容导出继续有效，既有测试 monkeypatch 接缝保持可用。

### 2.2.1 PostgreSQL adapter 与切换边界

- `backend/app/repositories/factory.py` 是唯一 backend choice；PostgreSQL bundle 组合与 SQLite 对等的领域 store，共享一个有界 `PostgresDatabase` pool。启动 lease 覆盖 checksummed migration、恢复、warmup 与 readiness 发布，失败或被替换的实例只关闭自己的 pool。
- 跨进程访问由 PostgreSQL 自身的 MVCC、row/advisory lock 与 transaction isolation 处理，可消除 SQLite 的单 writer 文件锁争用；它不能消除业务层锁序错误或长事务，因此 pool acquire、statement、lock timeout 仍保持有界。
- 切换只允许“停写 → 停服务 → 一致备份 → 修改唯一 `DATABASE_URL` → 启动并自动 migration → status/`/api/ready`/认证/数量/代表性读取验证 → 放流量”。只改 URL 不复制数据。`SHADOW_DATABASE_URL` 不启用 dual-write；切回 SQLite 也不会回放 PG-only 写入。临时 `migration/shadow` 边界目前实现 SQLite39/PostgreSQL17/epoch1（66 张 replicated 表、四个逻辑键 guard）的 preflight/control/guard、原子 snapshot、可续跑 baseline COPY/H0，以及 fail-stop 单消费者正向 replicator 原语。replicator 先在 PG 侧按 migration→control→run→worker→checkpoint 锁序取得 run/lease/checkpoint并释放，再以短 SQLite 只读 snapshot 连读全局 seq，仅为 upsert hydration 当前行，delete 保持 key-only 且 hydrated bytes 为零；同一 stable key 在 accepted prefix 内保留最后 event 并按全局最后 seq 排序，raw seq/checkpoint 连续性不变，每个 identity 的最终 actual apply 覆盖 synthetic dependency contribution，只有 dependency-only identity 才引用计数一次 synthetic 行及其 bytes；短读窗口若在 allocated high-water 前结束，会在 hydration/apply 前立即判为 suffix gap；满窗口低于 high-water 时在同一 snapshot 探测相邻 seq，缺失即失败；批次硬上限 4096 events/64 MiB；仅一个 final bundle 可独占超限，同 key replacement 若在已有其他 actual bundle 时使 bytes 超限则回滚并延后。FK 父闭包只读该验证 snapshot，每事件最多 64 行；固定 v17 图按 FK constraint branch 计数的上界为 9 个 row slots，依赖行计入 bytes并跨事件去重，不查询 suffix log。最终 PG apply 事务重新取得同一控制锁序，对 migration ledger 和全部业务表取 `SHARE ROW EXCLUSIVE`，在锁内复核 snapshot source/target、live target identity 和完整 v17 catalog/guard。逐语句 savepoint 只延后 FK/UNIQUE ordering SQLSTATE，CHECK/NOT NULL 立即 poison；精确 catalog 派生的 89 个 unique surface 通过 NULL、按其他唯一列的非 NULL 等值/NULL `IS NULL` 与固定 predicate 定域的确定性 text/bigint 候选（`C` collation 文本 max 拼 `chr(1)`，或先走可索引 bigint MIN/MAX 快速路径选择 min−1/max+1，仅在两个 int64 边界都已占用时扫描首个 gap），或仅限无入向 FK 且有 accepted current-final 恢复行的叶表同事务 delete/reinsert 来打破 cycle。停车状态按 `(unique surface, row identity)` 跟踪；每个 stagnant pass 会停车所有可独立停车的冲突，final apply 成功会清除该 identity 的所有停车面。延后处理限制为 8 passes、32 actual statements/apply、16384 actual statements 总量；每次候选查询都计入预算，ordering、statement、pass、`ProgramLimitExceeded`/`DataError` 候选搜索与候选 UPDATE 容量耗尽保持 non-poison，`QueryCanceled` 保持瞬态并重试整事务，最终窗口内不可停车的 UNIQUE 漂移则在最早实际 seq poison。`run_forever` 从 256 events/8 MiB 倍增至硬上限，仍 ordering-blocked 时 non-poison；apply 事务 claim worker 后、业务 DML 前复查既有 run/direction poison；poison 发布在 binding/checkpoint 校验后锁定检查该方向任意既有记录，完全相同视为 ACK-loss 成功，不同则 stale 且绝不新增第二条；apply、ack-loss 与 poison publication 使用同一 identity 绑定，snapshot 与业务 apply 前都要求 `progress.applied_seq == checkpoint.last_seq`。业务收敛、脱敏 progress 与连续 checkpoint 一起提交；每个有效 batch 结局恰好记录一条脱敏 metric，batch events 使用实际 accepted/observed raw-event 数并尽可能保留 retries。这样不在等待 PG 时持有 SQLite transaction，也不留下 catalog proof→apply 的 DDL/DML TOCTOU。暂态错误整事务有界重试；SQLite path/file binding 失败使用专用 identity 异常而不依赖文本分类；已证明的 gap、错误 run/epoch/table/op/key、转换、schema、identity 或约束错误写一条脱敏 poison 并永久阻断该方向。显式运维 CLI 现在负责 preflight/start-forward/status/verify，前台 worker 使用数据库时钟的排他 lease、SIGTERM 批次边界和保守 retention（至少 7 天/100,000 events，并受 FULL 校验/barrier/replay/poison 约束）。这只建立 SQLite-active 的正向 shadow；cutover、反向复制和自动 URL 交换仍未实现。
- Verifier 在 SQLite snapshot 记录 `Hv` 并把规范化事实流式写入私有临时 spool，释放 SQLite 后等待 PG checkpoint，再固定 `REPEATABLE READ, READ ONLY` 的 `Ht`；第二个 SQLite snapshot 扫描 `(Hv,Hseen]` retained dirty key，仅把这些 key 标记为 concurrent，PG verifier barrier 保留到脱敏报告提交。Structural 层校验精确 catalog/guard、stable key/hash、FK/unique/cascade 与 storage-root 文件引用；Full 层增加领域投影、float32 bytes/dimension/norm/抽样 cosine 与固定中英检索门禁；Cutover 层再次复核 source write-frozen，并要求 `Hv=Ht=MAX(seq)`、零 concurrent、100% coverage 和前一轮完整 full/cutover。Clean report 只能按同级或更强等级 supersede drift。
- Baseline snapshot 目录必须 owner-only 且不可为 symlink；snapshot/live fence fresh 打开 `SqliteDatabase.db_path` 当前文件而不复用线程缓存连接，跨 open/transaction 及发布/PG commit 前复核 resolved path + device/inode。COPY 将全部业务 SQL 全限定到 run 绑定 schema，使用 named server cursor 有界复核 prefix，并用 statement timeout/阶段间取消轮询约束长操作。起始绑定、逐批提交/完成点和最终 H0 前均短暂取得 live SQLite `BEGIN IMMEDIATE` 来复核 capture 仍启用，但最终 66 表 proof/`ANALYZE` 期间不持有 SQLite；JSONB prefix proof 仅在 JSON 子树内统一有限 int/float/Decimal 的精确十进制语义（bool 排除、负零归零），普通 SQL 数值列仍保持类型差异；起始和最终另以 checksummed migration 派生契约验证精确 v17 table/column/PK/FK/unique/check、operational+GIN index 与 `public.pg_trgm`，逐批只走轻量 run/control/identity gate。
- 最终 live SQLite fence 是跨 commit 的 lease：只在 PG 双锁/run/table lock 与 66 表 proof/`ANALYZE` 完成后取得，持有期间写入并实际提交 PG H0 checkpoint + run progress，成功后才释放；PG 事务/commit 失败则不落 H0 并释放 SQLite。该 fence 期间不得再等 PG pool/advisory lock 或执行长 proof。
- 停写 importer 与连续 shadow 是两个独立运维边界，必须使用不同 PostgreSQL 目标：`scripts/migrate_sqlite_to_postgres.py` 负责 SQLite→PostgreSQL 存量导入与本地激活；`scripts/shadow_sqlite_to_postgres.py` 负责 SQLite-active 的连续正向影子同步。前者默认 dry-run，SQLite 只读 backup-API 快照与工作副本升级，目标空库/manifest 守卫，按 FK 排序的有界 COPY，JSON/时间/旧 JSON 向量/NUL 的显式兼容转换，rowid→ordinal 保留与 reseed，全表内容 checksum，逐表 checkpoint 提交（run 头绑定 sealed snapshot hash + 每张已校验表一条已提交流水）使中断可从最后完成的表续跑而非整体重来、finalize（ordinal reseed/索引重建/ANALYZE）幂等，会话级批量装载调优（`maintenance_work_mem`/并行建索引/为离线装载设定的 `synchronous_commit`/`idle_in_transaction_session_timeout`/`statement_timeout`），无凭据 receipt。它只排除 SQLite-only 的 `shadow_capture_control` / `shadow_change_log` 运维表并把排除写入 receipt，退役用户数据表仍要求为空。默认 preview/apply 不进入应用 runtime、不修改 `DATABASE_URL`；显式 `--activate-env ... --confirm-service-stopped` 会重新快照停写 SQLite（切换锚点），默认按 receipt 重算 PG 全表 checksum（`--fast-activation` 只跳过这第二遍目标全表校验，保留源快照锚点与 schema/清单校验），再通过同目录临时文件/fsync/`os.replace` 原子切换 `.env` 并保存权限受限回退副本。它不复制 storage、不读取 MySQL，也不提供持续同步或反向回放。
- 在线导入仅用于演练，因为快照后的 SQLite 写入不会被捕获。正式切换只允许“停全部 writer → 停服务 → 向新空目标做最终迁移 → 验证共享/已复制 storage → CLI 原子修改唯一 `DATABASE_URL` → 启动并自动 migration → `/api/ready`/认证/数量/搜索/代表性读取/canary 写验证 → 放流量”。CLI 不负责启停服务；旧 SQLite URL 只保存在惰性的 `SHADOW_DATABASE_URL`，不启用 dual-write。PG 接受业务写后，未经外部 PG→SQLite 对账不得切回。
- PostgreSQL 依赖 `public.pg_trgm`，向量为 float32 `bytea`，不依赖 pgvector。生产仍用 `--workers 1`，因为模型 scheduler、breaker 与 cancellation registry 是进程内状态。
- `batch_ingest` 除 SQLite 物理格式修复 `vectors-to-blob` 外的 mutation phase 支持 SQLite 与 PostgreSQL。PostgreSQL 直连维护必须先停 API/后台 writer、显式传 `--confirm-service-stopped`，并由共享 opener 在独立非池化 session 上持有数据库级 advisory lock；flag 本身不会停服务。离线 `scripts/check.sh` 不连接 PostgreSQL；`scripts/check_postgres.sh` 和 CI 的独立 PostgreSQL 16 lane 验证 adapter、migration、批处理与跨进程语义。

### 2.3 API、模型与领域服务

- `backend/app/api/routes.py` composes the domain FastAPI routers；aggregate 只负责组合顺序，不承载产品 endpoint body，也不提供兼容导出。边界契约直接检查各 domain router 的 endpoint 所有权，并以语义 AST 固定 aggregate 的组合清单与 `include_router` 调用；不依赖框架是否把子路由平铺（新版 FastAPI 会保留 lazy included-router 节点）。`system_routes.py`、`notebook_routes.py`、`source_routes.py`、`knowhow_routes.py`、`knowledge_routes.py`、`ask_routes.py`、`report_routes.py`、`kg_routes.py` 与 `admin_routes.py` 各自拥有领域 endpoint；`memory_routes.py`、`auth_routes.py`、`content_overview_routes.py`、`debug_logs.py` 与 Agent Knowhow router 保持独立。`mcp_server.py` 提供默认二十四个 core 工具（七个 Memory/context、四个 knowhow、一个引用点查、七个来源、三个构建与两个库理解）的 scoped Streamable HTTP 面；`CORE_TOOLS` 是默认二十四个内建前缀；`PUBLIC_TOOLS`、静态 guard 与默认 server-local discovery 均来自同一冻结组合目录；`deps.py` 承载访问控制依赖。
- 领域 Pydantic model 位于 `backend/app/models/` 的 `common.py`、`identity.py`、`memory.py`、`notebooks.py`、`sources.py`、`knowledge.py`、`kg.py`、`ask.py`、`reports.py`、`knowhow.py`、`content_overview.py`、`admin.py` 与 `model_services.py`。`backend/app/models/schemas.py` is a legacy compatibility facade：它只 re-export 同一 model object；领域模块不得反向 import facade 或 service/router/repository/store。
- `backend/app/services/model_registry.py` 持有稳定 workload 目录并加载部署 TOML；`model_provider.py` 是进程级模型访问组合根，按 workload 解析物理服务并复用每服务唯一的 `ServiceScheduler`；`model_scheduler.py` 与 `model_circuit_breaker.py` 持有容量、公平队列、截止时间与熔断状态。业务 service、repository、batch、探测路径都只能请求 workload adapter，不得直接构造/暴露 raw chat、embedding 或 rerank client。底层 HTTP 只存在于架构测试明确许可的 transport 边界。
- `backend/app/services/kg/`、`kg_ingest.py` 与 `kg_merge.py` 负责 Concept / Claim / Formula / Procedure 的抽取、证据绑定、图推理、PPR、合并、质量过滤与 scale-index 支撑；`kg/maintenance_jobs.py` 独立拥有 relink/rebuild 的共享单飞槽和后台任务编排，算法仍归 `KnowledgeLifecycleService`。
- `retrieval.py`、`retrieval_service.py`、`reasoning_retrieval.py`、`structured_retrieval.py` 与 `ask_modes.py` 负责关键词/向量召回、候选融合、查询改写、Knowhow 稳定游标枚举、mode 注册和 reasoning 迭代；`core/ask_retrieval_policy.py` 集中声明五档预算与完整枚举安全线，`services/reports/policy.py` 集中深度报告充分性和 reasoning 动作线。`services/retrieval_run.py` 用 request/report-stage 级 ContextVar 状态在 worker 间共享 query embedding single-flight 和叶子 I/O 扇出闸，不跨请求、规划或生成阶段；报告等待槽位时感知取消，并在真正发起 leaf I/O 前复查。`collection_catalog.py`（零 LLM、索引辅助的集合地图，含来源元素按类型计数、KG 对象按类型计数与用户可见来源数的有界缓存/派生）与 `collection_enumeration.py`（对地图同一物理源集合做稳定游标遍历的枚举执行器）为 reasoning 的 `enumerate_elements`/`enumerate_kg_objects` reflect 动作及其 `collection="sources"` 参数值供数；两者与地图注入、reflect prompt、schema 分支、allowed_actions 共用同一个 `REASONING_ENUM_TOOLS_ENABLED`（默认 true）总闸。`ReasoningRetriever.run` 的**首轮**（run 级账目初始化 → 理解块/打法块/集合地图注入 → 规划 → 初检索 → PPR seed → 精确查找 seed → 空证据兜底 → 已确认方向补种）已抽成 `_run_first_round` 编排 + 若干 `_first_round_*` 阶段方法，run 级状态经模块级 `_ReasoningRunState` 显式交接、轨迹记账经 `_TraceRecorder`；该状态对象是**一次性交接**而非全程权威——`run` 只解包一次，可变容器是同一批对象，标量字段在 reflect 循环开始写局部名之后即陈旧。阶段顺序本身是合同（精确查找 seed 必须排在 PPR seed 之后）；长期回归由意图、PPR、精确检索、兜底、方向补种账目、枚举与合成的聚焦行为测试承担，不再保留重构期的逐字黄金快照。reflect 循环本体与它的三个嵌套 def 尚未拆分，是登记在案的下一件事。
- `report_engine.py` 保留两阶段深度报告的公共编排入口；`services/reports/policy.py` 和 `observability.py` 分别拥有规则与无内容分段事件；`background_jobs.py`、`cancellation.py` 和 repository 中的 job 状态共同管理后台任务与显式取消。
- `memory_service.py` 与 `memory_retrieval.py` 负责 owner/notebook 隔离的生命周期、revision/provenance、两个检索平面、Agent token policy 与 confirmed-only 正式投影；Memory 不写入 source/chunk/KG 表。
- `parsers.py`、`structural_markdown.py` 与 `mineru_client.py` 负责 PDF、Markdown、DOCX、PPTX、CSV、XLSX 等来源的结构化解析；FastAPI 进程不直接加载 torch 或 MinerU 模型。
- `spreadsheet_analysis.py` 是普通 parser 旁边的可选专业编译/执行 lane：摄取阶段用 openpyxl/xlrd 建有界类型化快照，逐步推理阶段最多用既有 `reasoning_agent` 做一次白名单计划，再在本地执行；它不 import Agent SDK、不执行工作簿代码，也不拥有来源状态。编译失败由 `AnalysisArtifactStore` 自动记录，普通解析仍由 `SourceIngestionService` 独立决定成败。

### 2.4 前端边界

`frontend/app/page.tsx` 是 collection/workspace 编排器，不再是所有模型与面板实现的唯一所有者：

- `workspace-model.ts` 保存共享 API/视图类型与常量。
- `answer-panel.tsx` 保存答案、引用与 reasoning trace UI。
- `frontend/app/admin/usage/` 拥有用户总览及其只读「提问分析」「解析问题」页签；它只消费管理员 GET projection，不拥有解析、重试或隔离文件 mutation。解析问题列表可按 7 类模型功能筛选，模型正文只在展开一行时经单案例 GET 读取，不随列表批量下发。`page.tsx` 的 workspace hash 可带一个来源 id，只负责打开仍获授权的笔记本和来源详情。
- `kg-type-model.ts` 保存内置知识类型文案/样式；`kg-type-mark.tsx` 消费并 re-export 该模型，保存答案与图谱共用的类型标记渲染。
- `ask-stream.ts`、`ask-reconnect.ts` 等 helper 保存流式问答和恢复行为。
- `frontend/app/api-client.ts` is the shared transport，负责 base URL、认证 header、JSON/empty/Blob、trusted error、网络失败与 AbortSignal mechanics；七个 domain API module 仍拥有 endpoint path、body、response type 与产品策略。
- `frontend/app/notebook-transition.ts` 是「打开笔记本」的单一 transition 编排（纯逻辑，无 React/DOM/网络）：全部 `begin` 先按声明序跑完再判拒绝 → `enter` → `load` → `isCurrent` → `apply` → 各步可选 `commit`（按声明序）→ `conclude` → 对已 begin 成功的步骤按**逆 begin 序** settle 恰好一次（成功传 outcome；拒绝、取数失败、任一守门判否与异常一律传 `null` 回滚，异常随后原样抛出）。被顶替的旧 transition 只 settle 自己铸出的那批 ticket，绝不碰更晚 transition 刚建立的 owner。`page.tsx::notebookTransitionSteps` 是各 owner `begin`/可选 `commit`/`settle` 的唯一登记点，新增 owner 只加一项；root-modal 那一步必须排第一（它的 close sink 是暂存批次的唯一清理路径）。`openNotebook` 只保留自己的 prologue 与 plan 声明，四个相位落在具名 helper 里，请求数、epoch 语义、迟到丢弃、tombstone、history 与失败落点均不变。
- `frontend/app/use-source-library.ts` 是来源库状态的唯一 owner：列表/检索范围、分页、详情元素、重解析、删除 tombstone 与解析轮询都在 hook 内按 user + notebook + workspace generation 归属；`page.tsx` 只提交成对稳定的 notebook/source 首屏快照并消费 readonly view、具名 command 与窄刷新事件。文件/URL 写请求可以在服务端安全完成，但旧 owner 的迟到结果不得写入新工作区。
- `frontend/app/use-ask-session.ts` 是 Ask 状态的唯一 owner：草稿/对话、意图确认、持久 stream/reconnect、会话历史/tombstone 与会话 mutation 都按 actor + notebook + workspace owner 收口。导航只 detach durable job；同一 actor 重开该 notebook 时，restore 先接回 detached run（`started` 之前它没有 durable 会话，只能靠 hook 的本地 run 记录接回同一条 transport；推理模式的意图预检/澄清同样按本地记录接回，预检在离开期间照常完成并可直接启动 durable run），没有在途 run 才退回最新详情。显式 Stop 在 `started` 前保持 transport 读到 job id，再执行一次 cancel 后 abort。同步 cancel 端点没有可强制的整请求数据库期限，客户端因此只保留一条在飞权威请求直到服务端响应，不用本地 timer 提前释放重试权。`page.tsx` 仍拥有 notebook/source paired snapshot、Memory answer-link 批次和跨域展示，只显式触发一次历史恢复并消费 readonly view/具名 command。
- `frontend/app/use-report-workspace.ts` 是 Report 状态的唯一 owner：列表/详情、按需首读、互斥轮询、意图/大纲 mutation、分享/导出选择与删除 tombstone 都按 actor + notebook + view owner 收口。非报告页签保持零 report I/O；导航只 detach 后台任务，显式取消仍走原端点。成功删除按 actor+notebook identity 持久抑制旧响应，创建冻结 source/base scope；`page.tsx` 只组合 live policy、浏览器展示 effect 与 readonly view/具名 command。
- `frontend/app/use-kg-workspace.ts` 是三个独立可测领域 owner——`use-kg-knowledge.ts`（Knowledge 列表/类型/上下文）、`use-kg-schema.ts`（Schema view/mutation）、`use-kg-graph.ts`（统一图查询/节点/合并审阅、KG build/relink/rebuild）——之上的薄组合层，三者共享 `use-kg-owner.ts` 里唯一的 actor + notebook + generation 门，只按 exact actor + notebook + generation 接纳可见提交；维护认领与合并决定 tombstone 另按 actor+notebook identity 跨 A→B→A 收敛。三个领域互不可见，跨域协调（清空、失效、认领新 owner）只由组合层把门的扇出路由进各领域自己的具名 command。Knowledge/Schema/图内容保持惰性，打开 notebook 只保留既有维护状态探针；写命令逐次复核 live policy，只读成员没有审阅写入口或 review-job 请求。`page.tsx` 只组合权限、窄刷新 effect 与 readonly view/具名 command。
- `frontend/app/use-notebook-collection.ts` 是 collection 状态的唯一 owner：actor-scoped rows、有界搜索、筛选/排序/视图/菜单、issued/published list 水位、访问权对账、editor/delete、默认创建 single-flight 与删除 tombstone 均从 `page.tsx` 收口。壳层保留既有 model-status + health + notebook-list + system-config composite bundle，并在 sidecar settle 后用 opaque ticket 提交清单；打开 notebook 不新增 collection read。actor 替换同步隐藏旧状态，阶段写重验 live row authority，成功删除先写 actor tombstone，A→B→A 与旧 list 不能复活卡片。hook 只暴露 readonly view、具名 command 与窄 shell effect。
- `frontend/app/use-root-modal-coordinator.ts` 是 root dialog 呈现 lease 的唯一 owner：typed slot、actor/workspace/source generation、primary conflict、合法 info overlay、topmost/Escape/focus return 均由它统一裁决。domain payload、busy、权限、API 与 timer 仍归原 owner；异步 opener 先 issue frozen lease，读回后按 exact owner/issue publish。切库/换用户在导航 await 前同步撤销旧 lease，A→B→A 不复活，协调器自身不增加 I/O 或轮询。
- owner hook 的隐藏态回退值一律是稳定引用（冻结常量或 per-instance ref，见 `hook-view-stable-empty-guard`），actor 激活/离开的扇出只在 `page.tsx` 的 `activateWorkspaceOwners` / `leaveWorkspaceOwners` / `leaveActorOwners` 三个入口（见 `workspace-owner-transition-guard`）；owner 视图不得在 `Home` 顶层再摊平成局部别名（逐字段或对命名空间别名二次摊平皆算，只放行命名空间级解构本身，见 `owner-view-no-reflatten-guard`）。
- `frontend/features/extension-sdk` 是 build-time workspace UI registry 与窄 host contract。首批只承认 `workspace.side_panel` / `source.detail_section`；首个真实条目把既有 Agent Profile 入口迁入 side panel：它落在来源栏固定区（滚动的来源列表之上）的一行入口，不给工作区加独立一列，视觉复用既有按钮类与 `:root` token；插件组件点击前不做领域读取，仅通过 exact-owner action 打开既有根层面板。后端 manifest 的 metadata-only UI declaration 启动冻结，`/system/extensions` 每次请求实时判 capability、只投影脱敏 availability。成功提交 workspace 后每个 actor generation 共享一次读取；同用户切库复用投影，但 actor/notebook/workspace generation 在 transition 起点同步隐藏旧入口并拒绝旧 action。浏览器按 local exact tuple ∧ live server row ∧ core permission ∧ normalized UI mode ∧ current owner 渲染；集合/未登录/空 registry 为零请求与 exact null。禁止远程 JavaScript、runtime register、全局 store 或向插件泄露 page/domain owner。
- 来源详情进入同一 frozen primary issue 水位；来源目录审阅是唯一可与其详情兼容共存的 primary 上层。任何被覆盖的 root dialog（包括来源详情与图谱分析）都必须 inert/ARIA-hidden，不能继续接收后台交互；焦点只在提交后确认底层 lease 仍为 topmost 且 inert 已移除时归还。
- `frontend/features/kg-maintenance` 拥有 KG 维护 API 与轮询/忙碌状态纯逻辑，`use-kg-graph.ts` 是其唯一 workspace 编排 owner，经组合层 `use-kg-workspace.ts` 暴露给 `page.tsx`。
- `frontend/tests/{unit,component,guards}` 是测试入口的唯一位置，`frontend/test-support` 保存 setup 和语义源码 adapter；位置守卫禁止测试回流到 `app`/`features`。

notebook 内页采用来源栏 + 主区域的两列 workspace，主区域提供 问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab。外层另有当前用户的总 Memory 页面，notebook 卡片数量可深链到局部 Memory tab。全屏 Knowledge Graph 和看板是独立顶栏动作；「图谱 Schema」已移入知识图谱视图头部，不再是独立顶栏动作：成员可查看当前生效定义，owner 可维护本库覆盖/自建类型，管理员可另管全局基线。「分析」菜单本身只含晋升队列（admin）、tier 切换（admin）与边审查队列。当前没有文章研究、思维导图、信息图或派生规则入口，也没有固定 Studio 右栏。

### 2.5 配置边界

系统模型配置由部署者统一管理，用户侧没有保存、编辑或测试草稿配置的能力。`.env.example` 是普通运行参数和密钥槽位真源，`model-services.example.toml` 是服务/绑定/容量模板；MinerU 单独按解析模式选择远端服务、隔离子进程或 PyMuPDF4LLM 回退：

- 数据与认证：`DATABASE_URL`、`SILICON_NOTEBOOK_STORAGE_DIR`、`SILICON_NOTEBOOK_ADMIN_PASSWORD`、`SILICON_NOTEBOOK_AUTH_OPTIONAL`。
- 模型服务：`MODEL_SERVICES_CONFIG` 指向部署 TOML；`[services]` 声明服务种类、协议、URL、模型、`api_key_env` 和唯一容量参数 `max_concurrency`，`[bindings]` 把稳定 workload 映射到同种类服务。密钥只从 `.env` 中被 `api_key_env` 引用的变量读取；空路径是显式离线模式，非空但无效则启动失败。
- 模型调用调优：`OPENAI_COMPAT_TIMEOUT_SECONDS`、各 workload 的输出预算/重试、`EMBED_DIM`、`EMBED_RUNTIME_DIM` 与 embedding batch 设置。它们不改变模型并发容量；`EMBED_DIM` 必须匹配所绑定模型。
- PDF：`MINERU_MODE`、`MINERU_API_URL`、`MINERU_BACKEND`、`MINERU_PARSE_METHOD`、`MINERU_LANG`、`MINERU_TIMEOUT_SECONDS`。
- KG / index 调度：`KG_AUTO_EXTRACT`、`KG_JOB_CONCURRENCY`、自适应窗口参数、`SCALE_INDEX_AUTO_ENABLED`、`SCALE_INDEX_AUTO_WHEN`。来源 job 与本地 CPU/ANN 线程不是模型容量，所有模型调用仍受绑定服务的 `max_concurrency` 限制。
- Agent MCP：`MCP_PUBLIC_URL`；默认允许远程明文 HTTP 并放宽 Host/Origin 校验（仅可信内网），启动会打印明文告警；公网部署设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS + DNS-rebinding 保护。

模型服务状态是只读投影：`GET /api/model-services/status` 返回脱敏后的服务身份、workload 绑定、容量、运行/排队数、熔断与最近健康状态，不触发上游探测。只有 admin 能显式调用单服务或全服务 test endpoint。所有模型失败都携带安全 `support_id`，用户把它提交给维护人员，维护人员再以服务端日志关联具体坏掉的服务；状态与 UI 永不返回端点、凭据、provider body、prompt/response 或 raw exception。schema v24 已不可逆清空 `user_profiles.model_settings`、删除旧的逐用户健康行，并按部署服务 ID 持久化健康状态；个人配置路由与页面已下线。

全部 27 个 chat workload 的模型 JSON 都在 scheduler 统一出口严格解析，并按 schema example 校验已声明顶层/嵌套形状；只有 `reasoning_agent` / `ask_answer` 可按 `MODEL_JSON_REPAIR_MODE` 进入 `app.core.model_json` 的保守恢复层。该层只处理首尾完整对象的可恢复语法错误（如缺引号/逗号），限制顶层 shape 与明确类型，并要求每个非空字符串值仍逐字存在；截断和语义重写不修。统一出口把每次被拒响应交给 `AnalysisArtifactStore` 私有保存，并由 model registry 的穷尽映射归入 `ask`/`report`/`source`/`knowledge`/`memory`/`knowhow`/`retrieval`；普通观测仍只记录稳定状态/reason、workload 与安全 `support_id`，不写 prompt/response。Ask 的 NDJSON transport 在队列空闲时发送 5 秒空白心跳并关闭常见代理缓冲，前端丢弃空行；它只保持传输活跃，不制造 trace step，也不改变 detached worker 的生命周期。

新增可由环境覆盖的 pydantic v2 setting 必须使用 `validation_alias`；列表类值按现有 `NoDecode` 约定解析。

### 2.6 生产 DFX 诊断边界

生产诊断目标是 Ubuntu 24.04 上从仓库根执行 `npm run start` 的双服务形态，后端保持单
Uvicorn worker。`npm run start` 只拉起脱离 terminal 的前后端进程就退出，不负责 readiness/HTTP 校验。它是内部基础设施，不新增前端 UI 或 API。卡顿现场的主路径是在操作仍然卡住时
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
  → Excel 来源在同一来源代次锁内编译专业分析快照（失败仅归档问题）
  → chunk / element embedding 后台写入
  → 按 notebook 的 KG opt-in 状态执行或跳过 KG 抽取
  → 抽取时写入 knowledge_objects / knowledge_relations 与证据
  → 标记 unified KG / index 维护状态，由独立维护路径处理
```

source 状态沿 `queued → parsing → parsed → extracting → extracted` 推进，失败进入 `failed`。重新解析保留 source 行与原始文件；它替换旧 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。Excel 快照在 authoritative elements 已提交、来源 parse lock 仍由本代持有时生成，所以快照中的行锚点与本代 elements 一致；专业编译失败不回滚 elements 或改 source 状态。来源 pipeline 终态失败自动归档 `source_parse`，之后用户侧重新解析成功会自动 resolve 并删隔离副本。删除 source/notebook 复用生命周期清理，立即删除快照/隔离副本，并把留存问题迁移到新生成的中性案例 ID 和不含原标识的归档路径后脱敏；管理员没有写入口。`extracted` 的 UI 状态不等待后台 element embedding 全部结束。

### 3.2 Ask 与 detached job

`POST /api/notebooks/{id}/ask` 保留非流式兼容路径。`POST /api/notebooks/{id}/ask/stream` 先让 `ask_jobs` 行持久化 job 元数据与状态、让 `ask_trace_steps` 持久化后续 trace；cancellation event 注册在进程内，然后启动脱离 transport 生命周期的 worker：

```text
stream start
  → started {job_id}
  → detached worker 执行 chunk / reasoning
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

联合范围按检索路径区分：`chunk` 基线只读取 active notebook 的 chunk；启用 KG overlay 或 PPR 时，才可能加入 federated KG 上下文与 base-backed chunk。`reasoning` 使用 federated KG 路径。

知识对象 `federated_retrieve()` 跨 active 与其显式挂载的参考库集合（`notebook_bases`，可能为空）收集并标记 tier，其相关度 score 不乘 tier 常数，也不设置 tier 配额或地板；exact-score 的 `base` 次序只适用于知识对象命中。因此相关度更高的 personal knowledge hit 仍在前。`federated_retrieve_relations()` 的关系命中只按 score 降序，不使用 base 平局次序。

base 的权威性另在答案合成 prompt 中表达：如果 personal 与 base 证据矛盾，答案服从 base，并明确披露差异。这是 synthesis policy，不是 retrieval score policy，也不参与 grounding 阈值。

当前 Ask mode registry 的默认路径是 `chunk`；`reasoning` 为严格 KG 路径，迭代执行计划、检索、反思并流式产出 trace。自动界面的请求级 `mode="auto"` 刻意不进入 registry：API 在持久会话/job 创建前调用既有 corpus-blind 问题理解 seam，把结构化意图按封闭规则解析成 `chunk`/`reasoning`，深入分析直接复用该合同作为自动确认；歧义、模型未配置或理解失败保守落 `chunk`。因此持久化 mode、retrieval-run kind 与引擎真源仍只有稳定 registry id，高级界面的具名选择不经过自动路由。退役 mode id 只保留兼容映射，不能改回默认模式。

Excel 专业分析插在 reasoning retrieval 结束与 response-draft seam 之前。它遍历冻结参与集中的当前笔记本及获准挂载库，只读取各自通过 `ActiveSourceScope.allows(owner_notebook_id, source_id)` 的可见来源 ceiling（显式选择，或当次全选快照，再减当前库隐藏合成来源），并按来源所属笔记本读取快照；仅在命中分析意图与已有快照时付 planner 成本。结果作为 `ResponseDraftInput.spreadsheet_results` 进入合成，并同时追加 `AskResponse.result_sets(kind="spreadsheet")` 与可点击来源引用。lane 内任何异常都只记录稳定异常类型并 fail-open，不能放宽 scope、阻塞原回答或修改用户来源。

### 3.3.1 逐步推理预算与结构化完整枚举

`backend/app/core/ask_retrieval_policy.py` 是逐步推理预算的后端真源，`frontend/app/ask-retrieval-effort.ts` 镜像用户可见合同并由跨栈测试锁定；`answer_element_items` 与 `enum_page_size`/`enum_pages_per_run`/`enum_rows_per_run` 是这条镜像关系的例外——它们都是后端专有字段，前端没有消费者，也不在 `ask-retrieval-effort.ts` 里重复：前者只控制最终合成 prompt 里直接来源元素（公式/表格/图片等）的纳入条数上限，后三者约束 §3.3.1 之外「集合枚举工具」一节所述集合枚举工具（`enumerate_elements`/`enumerate_kg_objects` 两个动作及其 `collection="sources"` 参数值）的每 run 预算。`retrieval_effort` 的五个稳定 id 与上限如下；最终相关性结果数按 `min(cap, max(floor, aspect × 实际执行查询数))` 计算，模型可以提前结束，不能越过上限。

| id | 每查询取数 | 最终 floor/aspect/cap | 最大步骤/首轮子查询 | KG/chunk prompt 字符 | 合成纳入的直接来源元素 | 枚举页大小 | 每 run 额外翻页 | 每 run 累计行数 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `overview` | 4 | 8/2/12 | 4/2 | 4,000/12,000 | 4 | 50 | 2 | 100 |
| `standard` | 8 | 20/3/36 | 8/5 | 6,000/30,000 | 6 | 50 | 4 | 200 |
| `deep` | 8 | 24/4/48 | 16/6 | 8,000/50,000 | 8 | 50 | 6 | 300 |
| `thorough` | 12 | 32/5/64 | 32/8 | 12,000/80,000 | 12 | 50 | 8 | 400 |
| `exhaustive` | 16 | 40/6/96 | 50/10 | 16,000/120,000 | 16 | 50 | 12 | 600 |

候选召回不随档位变化，而由部署参数独立控制：`CHUNK_RECALL` 默认 200，分别约束 Chunk/KG 的 ANN 与词法候选窗（默认去重前最多 400）；`RELATION_RECALL` 默认 200，分别约束 Relation ANN 和词法端点扩出的关系总窗（默认去重前最多 400，词法总窗内仍为 source/target 两个方向预留份额）。调整这两个部署值会改变候选窗，界面不把默认值伪装成请求级硬上限。档位只表达“允许多少轮判断与最终证据”。

PostgreSQL 的 KG 名词法 producer 在同一召回词项与额度合同下按词项选 SQL 路径：CJK/长词项避开 `<->` ordered KNN，改走 notebook-scoped 的拆分 trigram arms；短非 CJK 词项在规模、scope 与索引能力闸满足时走全局 GiST KNN，KNN 短页也回到拆分路径。拆分侧把 `%` 与 literal-ILIKE 变成各自有界的 ordered arm 后精确 union/top-k；planner 可选择 notebook-aware 复合 GIN，或其他可用的 bitmap 组合，应用不伪称固定某一个物理索引。因此访问路径差异不会泄漏成新的召回 cap；固定无正文 side-channel 只上报各 SQL 路径词项数与 producer 耗时，不进入 hit/API。

`QueryIntentContract.result_scope` 用 `ranked` / `complete` / `aggregate` / `hybrid` 分开相关性检索与集合问题。`structured_retrieval.py` 仅对可识别的 Knowhow 集合请求启用，调用 repository port 的稳定 `(table_id, position, row_id)` 游标逐页枚举，持久化回答则携带结构化 result set 与 coverage；Memory 与其他集合仍无完整枚举器，只返回相关性命中并披露边界。来源元素、KG 对象与库内文档目录另有独立的枚举路径，见下文「3.3.2 集合枚举工具」——它由模型显式调用，不由这里的 `result_scope` 自动触发。五档共享同一安全线：25 行/页、50 页、1,250 物理行/请求、8 表、每表 8 列、单元格模型摘录 1,000 字符、结构化载荷 256,000 字符、正文内联 100 行、结果卡初始显示 20 行。正文与初始显示阈值不删除已经回传的结构化行。只有游标耗尽，且枚举前后 `mutation_seq`、history-backed `enumeration_seq`、行数、列元数据和所选表范围全部稳定，才发布 `complete=true`；`enumeration_seq` 会随行增删历史原子推进，因此等量删一增一也会被判为并发变化。任一行/页/表/列/载荷上限触顶或并发改表都发布 `complete=false` + `explicit_partial`。因此一张 100 行表可证明并显示 `100/100`，而低档位也不能缩小显式“全部”的枚举上限。

执行边界是可证明的物理行语义，而不是所有被分类为集合的问题：确认后按最终措辞与权威澄清答案重算 scope；只有整表清单、直接物理行/记录计数及其 hybrid 可执行，“多少种”等 distinct/type count、条件筛选、group-by 必须回退并披露不支持完整。选择范围使用最多 8 个描述的轻量 catalog，不读取格、代码附件或健康 payload；截窗前按查询优先纳入显式点名表，全库聚合计数/序列用于 batch 稳定性。响应分开 per-table、batch 与 synthesis coverage，所以 200/200 枚举配 100/200 模型预览是“枚举完整、分析部分”，8 表截断仅使 batch partial。KG/Memory/chain 共享 KG 字符硬预算，结构化预览/chunk/direct element 共享原文字符硬预算，最终证据块不超过两者之和。

### 3.3.2 集合枚举工具

`enumerate_elements`/`enumerate_kg_objects` 是与上述 Knowhow 执行器并行、由模型在 reflect 循环里显式调用的两个零 LLM 动作（第三个集合「来源清单」是这两个动作上的参数值 `enumerate.collection="sources"`，这一特性内模型面的动作空间维持 10 个；解析只识别 `"sources"`，其他值落回按动作 id 分派——§3.3.3 的大纲便签 `update_outline` 之后另外新增了货真价实的第 11 个动作，与这里无关）：可枚举来源元素 kind 白名单为 `formula`/`table`/`image`/`code_block`，可枚举知识对象类型白名单为 `concept`/`claim`/`formula`/`procedure`（限可用状态）；两张白名单唯一真源是 `collection_catalog.ENUMERABLE_ELEMENT_KINDS`/`ENUMERABLE_KG_OBJECT_TYPES`。第三个集合「来源清单」由参数值 `enumerate.collection="sources"` 请求，给出它就忽略 enumerate 的其他参数（库的文档目录本身就是一整个集合，没有子类型）。每个 run 只构建一次的集合地图（`[Collections in scope] elements: formula 12 (3 sources), table 5, image 0, code_block 0 | KG objects: concept 1234, claim 567, formula 89, procedure 0 | knowhow tables: 2 | sources: 7`，每个白名单 kind/type 恒在场、缺省即 0，硬顶 600 字符）注入 plan/reflect 上下文，让模型先看计数再决定是否值得整份列出；地图统计的物理源集合与执行器实际遍历的源集合按构造逐字一致。

来源清单的集合是**用户可见来源**：计划由 `source_change_signal_rows`（已排除私有 Memory 合成源）按它投影出的 `user_visible` 列筛出，纯算术、**零额外查询**：可见性由各适配器在 SQL 里对 `list_sources`/`list_sources_page`/`visible_document_count` 共用的那条谓词求值，作为投影列随行返回（不新造第三份拼写，也不另开一条查询——`source_type` 上没有索引，「哪些源被隐藏」只能整表扫这个 notebook 的全部源行，而那正发生在 signal 查询刚扫过同一批行之后）；遍历顺序取来源页签的 `(created_at, id)`（两侧列表查询也带 `id` 次键：并列 `created_at` 下缺次键会与目录分叉，PG 本来就带）。收尾除作用域指纹外再对整条链已发出的文档做一次有界批量复读，比对 (显示名, doc_type) 摘要（账目挂游标、不进用户面 coverage）：论文元数据回填只写 `source_paper_meta`、不动 `sources.updated_at`，指纹看不见它，不复检就会产出混代目录却报 complete（`source_change_signal_rows` 多投影一列 `created_at` 作排序键，同一行访问、零额外查询，双后端各自归一化成「字典序 == 本后端 ORDER BY」；指纹只消费前两个字段，创建时间不进摘要）。地图行尾的 `| sources: N` 与清单分母出自同一个 helper（`_notebook_visible_sources`），因此不可能出现「地图说 7、清单列 8」。条目为显示名（接地论文优先显示论文标题，与引用同口径）、文档类型的界面词（`extraction_profiles.PROFILES`，未识别即空串，绝不上屏 `academic_paper` 这类内部 id）与该源已存摘要的摘录；每份文档计一行，整份清单是**一个**分片，故首个 hydration 窗口免费、其后每个窗口计一次额外往返，页查询上界为 `1 + max_pages`。游标形态为 `(notebook_id, source_id)` 且指向**尚未列出**的第一份文档（inclusive resume），计划里有而 hydrate 时行已消失的文档按 `scanned>returned` 记账并由分母校验报 `concurrent_change`，绝不说成完整。

覆盖率是 `TypedCollectionCoverage`：`returned_total`/`total`（`None`=分母未知，渲染为未知而非 `/0`）/`complete`/`truncated_reason`（`budget`/`payload`/`concurrent_change`）/`overflow_semantics`；`complete=true` 要求游标耗尽、首尾作用域身份一致，且跨续跑链的累计条数与已知规模相符。首尾作用域身份包含**参与库集合本身**：收尾时经 `participant_ids`（与开场同一个 `resolve_participants` / `mount_sql.py` 谓词入口）重新解析，集合不等即 `scope_stable=False`，指纹/seq 复检也用收尾解析出的集合算。只按开场那份 id 列表重算指纹看不见「多了一个库」或「少了一个库」——空库不贡献来源信号，被卸载的库的信号也仍在，两种都会被误判成稳定。`TypedCollectionResult` 另携带 `synthesis_rows`/`synthesis_complete`，把「已枚举的清单」与「实际进入答案合成 prompt 的有界预览」分开披露，语义对齐 Knowhow batch 的枚举/合成两轨。预算按 run 累计：`enum_rows_per_run` 是本 run 所有枚举动作可返回的总行数，`enum_pages_per_run` 只计每个被访问分片的第二页及之后——元素侧按来源分片，KG 对象侧按参与的库分片，每个分片各自免首页（否则「每分片一条」的普通语料在预算耗尽前就无法枚全）；共享的结构化载荷上限 `structured_payload_chars` 同样是 run 级的，执行器在结果对象上回传本次真实消耗（`payload_chars`，与 `extra_pages` 同款「不进 coverage」的成本记账），run 每次只发剩余额度，否则一轮里的第 N 次枚举会拿到全新满额、累计返回数倍于文档上限。该游标是纯 run 内部句柄：不落库、不序列化进响应、模型也看不到它，只作为同一进程内续跑的凭据。

知识对象翻页只做纯 keyset 读取，不带状态谓词：`idx_knowledge_objects_nb_type_created` 不含 `status`，写进 SQL 就是一次无界残余过滤（停用对象占比高的老库上「一页」不再是 O(limit)）。执行器读回后按地图计数所用的**同一个** `USABLE_STATUSES` 对象过滤，每次动作最多过扫描 `max_rows × 4` 行原始行（`scanned` 计的就是原始行数），触顶发布 `truncated_reason="budget"` 的诚实部分结果并保留可续跑游标；游标越过已扫过的不可用区段，因此续跑必然推进而不会反复重扫同一段前缀。刻意不加状态索引：那需要在本次改动里再叠一次 schema bump，且会把状态词表冻进 schema。

限定单一来源按**标题**表达：内部 source id 从不上屏也从不给模型看，reflect 的 `enumerate.source_title` 由服务端在地图已规划的那批来源里做确定性解析（去空白 + 忽略大小写的精确匹配，按窗口批量读标题并有界，绝不模糊匹配）；零命中或多命中都跳过该动作并在轨迹里说明，绝不悄悄扩成全作用域枚举。计划长度超过解析上限时直接拒绝解析（`truncated=True`，不扫描、不给 id），不从前缀断言唯一——「唯一」是整个作用域的性质，同名的第二个源可能就在上限之后。同时给了 `source_id` 时以 id 为准。

因为这些工具不依赖图谱，逐步推理里「本笔记本尚未构建知识图谱」的早退只在作用域**同时**没有任何可枚举集合时才触发——元素、知识对象与**来源**三类计数全为零；来源数计入这道闸，因为纯散文库（有文档、零元素、零知识对象）正是来源清单的主力场景，挡住它只会让「库里有哪几篇」拿回一句非答案，而零源库仍然早退；放行后照常跑完整循环，`kg_required` 仍如实为 `True`。接线判据与上面的总闸是同一个函数，关掉 kill switch 会同时恢复早退。`complete=false` 恒意味着游标可续跑，唯一例外 `truncated_reason=concurrent_change`——两次调用之间作用域发生变化时终止且绝不静默重来；未耗尽预算的续跑请求走该游标继续，预算耗尽则跳过为 `enumeration_budget`（仍是部分结果），只有链条已 `complete` 才跳过为 `already_enumerated`。`replace_elements` 在同一个写事务里把该源的 `updated_at` 推到新元素所带的时刻，变更信号因此与元素换代原子同步：首解析的来源不再有「元素已落盘、信号未动、被数成 0」的窗口（那条曾被登记为「已披露的一致低报」，现已根治），刚解析完就能进遍历计划。显式点名的 `source_id` 仍直接查询该源而非把「不在计划」当作「为空」。每次动作的页查询次数还有一条**被强制的**上界：元素侧 `max_rows + max_pages`（零计数源不进计划⇒不访问，进计划的源访问即产行⇒受行预算约束），知识对象侧 `参与库数 + max_pages + 原始行过扫描上限`（该侧没有 per-分片计数可跳过，且状态过滤会产生补页）；越界抛 `EnumerationInvariantError`，由调用方按普通执行器失败 fail-open 成一次 skip。刻意**不**给首页计费：那会让「一百个源各一条公式」这类宽而薄语料在任何档位都无法达成 complete（本特性已修过一次的形态）。挂载参考库的跨库条目仍标注来源库名，但来源跳转与图片已由**参与集内的代理读取**补齐：`GET /notebooks/{active}/sources/{id}`、`.../sources/{id}/elements` 与 `GET /notebooks/{active}/assets/{asset_id}` 一律按路径里的 active notebook 过读权限，再只在该 notebook 的有效参与集内解析资源（资源自报所属 notebook，不在集内即 404，同库先短路不多付挂载 join），浏览器因此一次都不直连另一个库——挂载仍不等于持有该库直接成员权限，裸 `GET /sources/{id}` 保持 owner∪member 口径，写入（重新解析/删除）不代理，来源详情弹窗对参考库来源按只读渲染。「纯文本兜底解析来源」的覆盖披露仍登记为独立后续任务。总闸 `REASONING_ENUM_TOOLS_ENABLED`（默认 true）关闭时两个动作与来源清单参数一并不提供、地图也不注入。

范围指示语（「当前notebook」「这个库」「本库」「整个库」「知识图谱 / KG」等）只在 **prompt 层**接地：`prompts.SCOPE_DEIXIS_GROUNDING` 一段共用文本同时进入意图契约、两份规划拼写（`expand_query_prompt` 是生产实际发出的那份，`plan_prompt` 是保持同步的备份拼写）与 reflect，要求模型把这类短语解析成作用域后剥掉、不带进任何子查询/关键词/`exact_term`，同时保留问题本身。刻意不做确定性词表剥离：那会变成词法路由，也会误伤真正在讨论知识图谱的文档。

上述地图/枚举共用六个有界 repository 端口，双后端（SQLite/PostgreSQL）adapter 均实现：`SourceStorePort.source_change_signal_rows`（每 notebook 一条查询取全部源的不透明变更信号）、`SourceStorePort.element_type_count_rows`（按白名单批量 `GROUP BY source_id, element_type`）、`SourceStorePort.element_page_rows`（一个源一种 kind 的 keyset 分页）、`SourceStorePort.source_display_rows`（来源标题/论文标题批量查询，刻意不带摘要）、`SourceStorePort.source_listing_rows`（来源卡投影：标题/类型/已存摘要 + 论文元数据外连接，跑在调用方连接上；`source_metadata` 就是它加一个自己的连接，两者一份 SQL）与 `KnowledgeStorePort.knowledge_object_page_rows`（一个 notebook 一种 object_type 的 keyset 分页，复用地图同一条 `USABLE_STATUSES` 谓词）。

### 3.3.3 大纲便签与按节合成

`app/services/outline_synthesis.py` 与 `reasoning_retrieval.py` 里的 `update_outline` 分支共同实现一份有界的、由模型撰写的大纲便签与按节合成（设计文档 §3.1，借鉴 DualGraph）。门控函数 `outline_wiring_active(settings, limits)` 判 `limits.effort == "exhaustive"`（读的是 `AskRetrievalLimits` 自己新增的 `effort` 字段本身，不从预算数字反推——否则测试或未来调用方对某个预算字段的一次 `dataclasses.replace` 就会静默改变「这是哪一档」的答案）与 `REASONING_OUTLINE_ENABLED`（默认 true）同时成立。`update_outline` 是 reflect 循环的第 11 个动作 id（`OUTLINE_ACTION`）：章节结构是全量替换，至多 12 节、两层（节可带一个 parent，parent 自身必须是顶层节）、标题至多 60 字符、每节至多 8 个证据 key、每个 run 至多调用 6 次；同一稳定节 id 的证据则与旧绑定取并集，遗漏不删除，`remove_evidence` 才显式撤销旧键。8 键满额时旧键优先，未接纳的新键进入下一轮有界账目供模型显式腾位后重试。pending 不把普通额度内的后续载荷降成 repair-only：结构仍按全量替换更新，所有合法绑定先合并。只有 `sufficient`/stale 的终态纠错（且 `max_steps` 仍有余额）和第 6 次后的单次资格才限制为同结构纯换键；整体 reflect 次数绝不越过 `max_steps`，stale 熔断事实先落 trace 再进入仍在预算内的纠错。一次没有任何合法节的提交是一次 skip 并**保留**上一份大纲，绝不清空（`_unique_outline_id` 的有界后缀构造本身是一处已修的 P0：原 `while True` 去重循环在两种真实输入下把 worker 线程钉在 100% CPU，因为循环体内没有取消检查）。

证据 key 的合法性由服务端计算，不采信模型自报：合法集合 = 当前存活候选池 ∩（run 内候选摘要曾实际展示的 key ∪ 当前大纲已持有的合法 key）。`ever_shown_outline_keys` 单调累积，因此已展示并绑定的 key 不会因摘要窗口滑动而失效，从未展示的中段候选即使被猜中也过不了校验。枚举清单条目 id 与来源 id 刻意都不在合法集合内：前者模型根本看不到，后者是因为一份文档产不出可供分节合成使用的证据切片。因为章节结构是全量替换、且 reflect 的 prompt 不带对话历史，每轮 reflect 都会把当前整份大纲连同各节缺失绑定的清单一起回喂；证据即使被模型抄漏也由同 id 并集保底，空节仍被点名为下一步的检索方向。大纲修订对 stale 熔断账目保持中性——纯粹的措辞整理不推进也不清零 `no_progress`/熔断计数（真正的检索动作仍照常清零），避免两份大纲来回提交蒙混过 `reasoning_stale_limit`。

**大纲采用引导（设计文档 §3.1.1）**：`_outline_nudge_note` 是一个纯函数，在同一区位（几份账目之后、集合地图之前）追加一行确定性引导——闸开着、当前大纲**为空**、本 run 引导额度未用尽，且存在一条服务端手上现成的结构性理由（本 run 已把来源清单枚举到 `state == "complete"` 且条数达下限，或已确认检索方向数达同一下限；方向数取 `run()` 里已有的方向清单长度减一，不重新解析意图契约）。两条理由同时成立时用清单那条措辞。零新增查询、零模型调用、零动作 id；与大纲便签的互斥判据写在函数自己的「sections 非空即返回空串」里，而不是调用点的 `else`。只有真的发出引导的那一轮才给既有 reflect 步 detail 加 `outline_nudged: true`——无条件写 `False` 会破坏低档位/关闭态「detail 逐键不变」的冻结基线口径。同批把 `_answer_with_retry` 的报警口径改成「重试成功即摘除本次尝试记下的那几条 `ask_answer` 响应内 model_error（`mark = len(sink)` 起算，按 `workload_id` 过滤而非整段截断，sink 为 `None` 时不摘）」，两次都失败则一条不摘（含终态 empty-content 的 `RuntimeError`），`mark` 之前其它 workload 的报警一律不动，`events.jsonl` 始终记全。

run 收尾时，`outline_synthesis.plan_outline_sections` 把终态大纲的证据 key 对到当时存活的 `collected`/element/chunk 候选池上；解析后零证据的节与空节等价。解析后仍有 ≥2 节非空、且该 run 未产出集合清单/结构化整表枚举时（清单 run 保持单次合成——清单预览与覆盖披露只进单次路径的合成上下文，节化会拿 ranked 样本写散文而让完整清单闲置），`AskService._answer_reasoning_sections` 逐节调用 `_answer_reasoning(sectioned=True, key_offset=...)`：`key_offset` 按每节 10,000 的号段偏移各条上下文装配线（chunk/element/KG/Memory/推导链），保证跨节号段不相交；集合地图块、枚举工具预览、私有 Memory 与查询期推导链都不传入分节调用（它们不在合法绑定目标之列，一节根本没法「要」它们，且回退路径上保持原样不动）；分节模式跳过证据精炼（精炼是每次装配一次的模型调用，节模式下会把 k 次合成变成 2k 次，而一节至多 8 条证据本来就没有可精炼的中段）；每节的 `[k]` 锚点按该节自己的 `id_map` 解析后再合并——按合并后的 map 解析会把写出别节号段的标记（只可能是幻觉）一本正经地绑到那一节的证据上，而不是按节丢弃。`ReasoningResult.outline_evidence` 携带被大纲绑定、却被最终 rerank/`top_n` 截断挤出 `top_hits` 的候选（quota 路径下其相关度夹到选集最低分或空选集时的 0.0，不复用可能虚高的重排前分数）；这批候选只在分节合成真的跑过时才并入证据分类池。每节只用自己的证据切片与锚点通过 `classify_evidence` 判定；synthesis detail 的 `section_grounded` 是逐节记录列表（每项含 `grounded` 布尔和该节 `evidence_level`），不是整篇 flag，旁边另有无据节标题。全部节 grounded 时全局分类照旧；否则只把整篇 `evidence_level` 封顶 `overview`，不向上抬，也不把零节精确 grounded 但各节仍为 overview 的答案强制误写成 `inferred`。任一节合成失败（自身 `_answer_with_retry` 之后仍失败）会丢弃整个半成品并回退到单次合成；回退成功时，分节阶段记下的 `model_error` 会从用户可见的 `_err_sink` 里摘掉，`events.jsonl` 不受影响。每写完一节即发一条轻量的 `synthesis` 类型 `TraceStep`（`section_index`/`section_total`/`section_title`），收尾的 `synthesis` 步在大纲**规划跑过**时就在 `detail` 里新增 `outline_sections`/`outline_fallback`/`outline_skipped`/`section_grounded`/`ungrounded_sections`——按节被绕过（不足 2 节或清单 run）时 `outline_sections` 为 0、`outline_fallback` 为 false，被略过节的披露不随绕过消失；没有大纲时与冻结基线逐键一致。模型写进节标题的引用形 `[k]` 标记在 `parse_outline_sections` 入口剥除（留在 `##` 标题里会渲染成绑不上的裸引用，或撞上别节号段误绑）。`update_outline` 自身发一条 `outline` 类型 trace 步（前端标签「大纲」）。v1 刻意不为大纲新增 `AskResponse` 字段，只经 trace 与答案自身的 `##`/`###` 标题结构可见（限定在 `.chat-answer .answer-markdown h2/h3` 作用域，与深度报告的标题字阶分离）。深度报告的接线见本节末尾。

**KG 弱支撑边回喂（设计文档 §3.3，PR-4）** 叠在上述大纲机制之上，走一条纯粹的端口委托链：`RetrievalPort.weak_support_relations(notebook_id, object_ids)` → `retrieval_service.py` 一跳转发 → `retrieval_candidates.CandidateRetrievalService.weak_support_relations` 在服务端完成 fold（既有 `cluster_fold_rows`，只折本轮绑定 id）→ probe（`unified_kg.weak_support_relation_rows`，`canonical_src` 主键前缀 + `source_count` 阈值 + `LIMIT`）→ 名字解析（`unified_kg.relation_endpoint_name_rows`，经 `canonical_relations.sample_relation_ids` → `knowledge_relations` 主键 → 两端点对象主键 → `idx_clusters_member` 簇行，不直查无索引的 `concept_clusters.canonical_id`）三步，最终落到 SQLite/PostgreSQL 双后端各自的两个存储原语；`reasoning_retrieval.py` 里的 `collect_kg_gap` 只调用这个端口方法，零 SQL。`REASONING_OUTLINE_KG_GAP_ENABLED`（默认 true）关闭时执行处直接 skip，零 I/O、prompt 逐字不变。

**深度报告接线（PR-5）**：`report_engine.py` 不 import 上述任何大纲内部件，只经 `ReasoningRetriever.run` 的 `limits` 参数接入。`report_retrieval_effort(depth)` 把报告自己的研究深度 `depth`（1/2/4/8/16，路由层已夹在 `[1, 16]`）按阈值映射到与 Ask 相同的档位名（`overview`/`standard`/`deep`/`thorough`/`exhaustive`；中间值落更低档，不向上取整），`report_retrieval_limits(depth)` 再转成 `ask_retrieval_limits(effort)` 传给 `_deep_dive` 里的 `run(..., limits=...)`；每节自己的 `max_steps` 仍固定为报告的 depth 值，不采用档位表自身的步数上限（成本按节数放大，套用档位步数上限会把单节预算乘上节数）。到达 `exhaustive`（depth 16）时，`outline_wiring_active(settings, limits)` 的判据（`limits.effort == "exhaustive"` 且 `REASONING_OUTLINE_ENABLED`）在该节深挖里原样成立，大纲便签、`update_outline`、KG 弱支撑边回喂零改动生效；报告构造 `ReasoningRetriever` 时不传 `collection_catalog`/`collection_enumeration`，集合枚举闸不论档位都保持关闭。`_deep_dive` 收尾时用 `outline_structure_block(sections, id_map)` 把该节终态子大纲连同各子节绑定证据的 `[k]` 反查折成有界「发现的结构」块（≤12 行、行 ≤80 字符、块 ≤1200 字符，超界记账 `(+N 子节略)`），作为 `discovered_structure` 传入 `report_section_prompt`（`prompts.py`）——纯拼装、零模型调用、零新查询；prompt 措辞教撰写模型这只是 `###` 子标题的组织建议，缺证据的子话题如实略过，且它绝不触碰 `reports.outline_json`（用户确认的章节/必答主题绑定）。`_run_sections` 的 `on_step` 在观察到 `outline` 类型 trace 步时把该节 `section_status.phase` 细化为「深挖中（已整理大纲 N 节）」，复用既有 2 秒节流持久化，不新增表列或 SSE。档位的作用域覆盖整节而不止 `run()`：`clamp_merged_evidence(result, limits)` 在「按已确认方向补检索」的合并**之后**把 `top_hits` 压回 `ranked_final_cap`、`elements` 压回 `answer_element_items`（相关度降序，元素 tie-break `element_id`，与 `_answer_reasoning` 同一把键；未超上限时逐位不动；`outline_evidence` 那批豁免——它们的相关度被刻意夹到选集最低分以下，一起排序必然垫底），`_draft_section(..., depth)` 则用 `kg_context_chars` 装配 KG 分区、用一份**共享**的 `chunk_context_chars` 装配「chunk + 直接原文段」分区（原文段取 `max(0, chunk_budget - len(chunk_block))`，条数封顶 `answer_element_items`），`depth=None` 的调用方保持 `ANSWER_CONTEXT_BUDGET_CHARS`/`REPORT_SECTION_CHUNK_BUDGET` 定值与旧的 `max(2000, …//3)` 元素额度。大纲绑定对象的优先额度由 `EvidenceContextPort.knowledge_context` 的 `priority_object_ids`/`priority_budget_chars` 在**一次**调用内完成：该函数末尾的 `relations:` 行是对本次 `evidence_by_id` 内部的边求的，调用方拆成两次会把所有跨两半的关系静默丢掉、并渲染出两行各记一次预算的关系行。

### 3.4 Memory 与 Agent MCP

`app.api.mcp_server` 只拥有唯一 FastMCP/SSE transport、Bearer middleware 与 session manager；`app.api.mcp_tool_host` 是唯一 FastMCP tool registration exit。它从 `app.api.mcp_tools` 七个显式 registrar 捕获精确 24-tool core 目录，`mcp_server.PUBLIC_TOOLS` 就是这份活目录（`CORE_TOOLS` 是同名别名），文档/smoke 守卫全部由它派生，不存在第二份手抄。core handler 的 schema/validation/auth/I/O 顺序不变，统一 live token/scope/allowlist/membership 复核、owner-only 写策略、一次 progress wrapper 与 output budget；异常只映射稳定公开码。注册与 listing 零 repository/model I/O。原先「追加 startup-frozen、显式信任的进程内 `agent.tool_provider` contributor descriptor」那一半零消费者，已整体移除。

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

默认 core 公开二十四个工具，`mcp_server.CORE_TOOLS` 由 registrar 捕获该前缀；`mcp_server.PUBLIC_TOOLS` 是同一默认冻结组合目录（含默认受信 provider）的权威清单：Memory/context 七工具
`list_notebooks`、`select_notebook`、`search_agent_memory`、`search_notebook_context`、
`get_memory`、`ask_notebook`、`propose_memory`；knowhow 四工具 `list_knowhow_tables`、
`get_knowhow_discrimination`、`get_knowhow_row` 与 `put_knowhow_cell_code`；引用点查
`get_cited_element`；来源七工具 `list_sources`、`add_source_text`、`add_source_file`、`add_source_url`、
`get_source_status`、`reparse_source` 与 `delete_source`；构建三工具 `build_kg`、
`build_retrieval_index` 与 `get_build_status`；库理解两工具 `get_notebook_profile` 与
`add_observation`（Agentic Memory P3）。读取需相应 read scope，格子代码写入需
`knowhow:code`，观察记录写入需 `agent_observation:write`。

来源管理与构建工具的权限面刻意比浏览器窄（P2 后浏览器 HTTP 面的六个内容写能力已翻 admin、组管理员可写，MCP/Agent 面**仍恒 owner**、刻意不跟——长期 token 是独立凭据）。`add_source_text`/`add_source_file`/`add_source_url`/
`reparse_source` 需 `sources:write`，`build_kg`/`build_retrieval_index` 需
`maintenance:execute`，六者一律 **owner-only**：token 的白名单可能包含 owner 只是以只读成员
身份加入的笔记本，在那里发起写入或后台构建等于把共享的读侧升级成写侧。`delete_source` 另需
`sources:delete`（`sources:write` 不蕴含它），并且**只能删除 Agent 添加的来源**——判据是 v48
`sources.agent_profile_id` 非空的 `agent_created` 投影，与证明笔记本归属的是同一次单行读取；
判据是「某个 Agent 添加过」而非「本 profile 添加过」，否则轮换掉的 profile 会留下永远删不掉的
来源。出处只在 INSERT 分支写入，因此同内容去重复用用户的行时该列保持为空，笔记本深拷贝也会
显式清空它——重传用户的字节无法把它洗成 Agent 可删的来源；该列缺失时投影默认 false，闸门
fail closed。`get_source_status`/`get_build_status`/`get_cited_element` 是只读，停在
`knowledge:read` 与成员可读口径。

### 3.5 KG 与索引维护

- 新摄取数据使 unified KG 进入 dirty 状态，不在 Ask 请求路径同步整库重建。
- 打开 Knowledge Graph overlay 时读取当前图和 `GET /api/notebooks/{id}/unified-kg/status`；只有用户触发刷新时才调用 rebuild。
- KG 首次构建/整库重建使用显式 build/rebuild 端点；跨文档 merge review 只处理有界候选批次。
- vector cache 按数据版本失效；大库 scale index 由维护任务构建/刷新，并通过状态与 manifest 观察。即使 `SCALE_INDEX_AUTO_ENABLED` 开启，调度也发生在后台维护路径，而不是把全库 backfill 塞进 Ask。
- Ask 不同步补齐整库 embedding、不同步重建 unified KG，也不为 citation validation 扫描全部 source element。

### 3.6 深度报告

深度报告由 `report_engine.py` 作为可取消后台 job 执行。阶段 1a 先做完全不读取语料的问题理解，停在 `intent_ready`；确认端点通过 store 级 compare-and-set 原子认领 `intent_ready → planning`，把用户已审阅的合同和澄清答案确定性冻结，不再做隐藏的二次 LLM 理解。阶段 1b 才做语料侦察与多视角大纲，停在 `outline_ready` 供用户编辑；覆盖/充分性探针先按逻辑组保留各自 first-N，再把跨主题/章节重复的 query 合并为一次检索，并在 report-wide leaf fanout 内并行 KG/element 叶子；聚合仍按原输入顺序。不可复制的大库不扫描整表 element，而从有界 chunk ANN 命中的 `element_ids` 恢复精确元素；ANN 不可用时才走有界 FTS 回退，精确短语/标识符仍是独立通道。阶段二在确认大纲后按 section 并行运行 reasoning 深挖并写成带证据纪律的 Markdown，内部检索问题可含澄清答案，但可见标题只使用确认后的研究问题。状态、逐节进度、下载、批量导出、取消与删除都通过 report API 暴露，不能在请求线程内同步跑完整报告。已认证的后端批量导出先由 repository SQL 完成 notebook/creator/done/nonempty 收窄并释放连接，再把不可变最小视图交给启动冻结的 single `report.exporter` Provider；默认内建 Markdown provider 是唯一默认实现，文件名/重复后缀和 ZIP 外壳继续归 core，不存在 fallback renderer。浏览器单篇 Markdown Blob 仍是已授权详情的本地呈现，不进入 backend Provider。

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

每张 knowhow 表带完整变更历史（`knowhow_changes` + `knowhow_milestones`，schema v26）。每个写事务方法
都在各自写事务的最后一步经模块级 `record_change` 追加一条流水，存受影响实体的 before/after 加**变更后的
整表指纹**（复用传输守卫的 `_FINGERPRINT_SQL`，覆盖表元/列/行/格子/代码附件、刻意不含时间戳）；一条
架构守卫（`backend/tests/test_knowhow_history_coverage_guard.py`）对 SQLite 与 PostgreSQL 两份
`KnowhowStore` 各扫一遍，保证白名单外的写事务默认报红，防将来新增写路径漏挂，也防某个后端的 store
文件搬家后悄悄脱离覆盖。它要求 `record_change` 落在写事务 `with` 块**体内**（挪到块外就丢了
「数据变更与流水同事务」的原子性，照样报红）；不钉的是它在块内的**位置**，「挂在最后一步」仍由
代码评审承担。
回退是纯 delta 反向重放：在一个写事务内先校验当前指纹等于最新流水的指纹（否则中止），从 head 逆序把
before 写回到目标点（行/列**原样复用 id**，引用跳转与代码附件才不断），再校验结果指纹等于目标点的指纹
（否则整事务回滚），最后追加一条 `revert` 流水——历史只增不减，故「回退的回退」天然成立。里程碑零快照，
只是给某个 seq 起名；流水被「清理历史」删除后里程碑保留为「已失效」不级联删。清理只删最老的连续前缀
（按 seq 不按 `created_at`，防时钟回拨挖洞）且永远保留 head。孤儿图片清扫器的存活引用集扩到历史流水，
故图片进过格子后基本不再自动回收——代价是「清理历史」要等最后一次引用不在 head 上时才释放该图。回退提交
后经同一个 `ProjectionScheduler` 触发全量重投影。详见 `docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md`。

## 4. 关键行为契约

- **断连不等于取消**：transport 断连只停止向该客户端继续推送；detached Ask worker 仍执行并可持久化。只有显式 cancel endpoint 能设置 cancellation event。
- **空闲不等于无响应**：Ask stream 每 5 秒发一条无内容空白行来防代理 idle timeout；总请求时长硬上限仍是部署侧边界。
- **显式中断端到端**：前端 interrupt 控件拿已返回的 `job_id` 调 cancel endpoint；worker 与流式 LLM 在保存最终回答前检查取消状态。
- **启动失败有持久化终态**：Ask/report 同步提交失败时，已创建的 job/report 进入 failed、进程内 cancellation entry 被注销，提交异常继续抛给调用方；正常完成顺序不变。
- **检索范围按 mode**：`chunk` 基线只读 active notebook；KG overlay/PPR 才可加入 federated KG/base-backed chunk；`reasoning` 走 federated KG。
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
| Sharing | `backend/app/services/notebook_sharing.py` + `backend/app/repositories/sqlite/sharing_store.py` | share token、reader 权限、深拷贝与补偿/恢复；`sqlite_notebook_sharing.py` 为兼容 shim。授权谓词唯一定义点在 `repositories/{sqlite,postgres}/access_sql.py`（双后端镜像）：三条谓词成严格包含链 `写权 ⊆ 管理权 ⊆ 读权`——写权 owner-only、管理权 owner∪`role='admin'` 有效授权边（`NOTEBOOK_ADMIN_SQL`，复用受限三臂+`role='admin'`、排除 everyone）、读权 owner∪成员∪四值授权边；Memory 读侧 SQL 的嵌入片段同源派生。API 写端点经 `api/deps.py::require_notebook_capability` 按 9 个能力名归类，值域 `{owner, admin}`（P2 把六个内容写能力＋`notebook:manage` 翻 admin，`notebook:configure`／`notebook:delete`／`reports:write` 恒 owner，Agent/MCP 面刻意不翻，73 个端点声明不动；守卫见 `test_access_sql_contract.py` / `test_notebook_capability_guard.py`）。群组／成员／授权边的行持久化在 `repositories/{sqlite,postgres}/group_store.py`（与 `sharing_store.py` 并列，同一层），策略集中在 `api/group_routes.py`（群组可见性 404 口径、发边双重条件、不对称撤销、系统管理员运维旁路，以及 P2 成员贡献审批流 `notebook_share_requests` 的 6 个端点：状态机 pending→approved/rejected、撤回=DELETE 整行、创建撞防重复 pending 索引时幂等）；`mount_sql.py` 仍是参与集解析的唯一定义点，只是有效性谓词加了「受限读权 + 挂载方未被共享」这一支。深度报告是唯一不能表达成单个 notebook 级能力的写面（成员建自己的报告），改由 `require_notebook_read` + 体内行级 `created_by` 校验承担。 |
| KG | `backend/app/services/kg/`、`kg_ingest.py`、`kg_merge.py` | 抽取、证据、图、PPR、质量与合并；`maintenance_jobs.py` 拥有 relink/rebuild 任务编排，算法仍归 lifecycle。 |
| Retrieval / Ask | `retrieval.py`、`retrieval_service.py`、`reasoning_retrieval.py`、`structured_retrieval.py`、`retrieval_run.py`、`collection_catalog.py`、`collection_enumeration.py`、`core/ask_retrieval_policy.py`、`ask_modes.py` 与 facade 中的兼容方法 | 分数、grounding、tier 次序与集合完整性保持分离；mode registry 是 mode 真源，effort policy 是档位与枚举阈值真源；retrieval run 是单轮 embedding 复用与报告叶子扇出的边界。 |
| Reports | `backend/app/services/report_engine.py` + `backend/app/services/reports/` | 两阶段后台 job，保持 outline 审阅、取消与 section progress 语义；policy 与无内容观测从编排器分离。 |
| Memory / MCP | `memory_service.py`、`memory_retrieval.py`、`memory_store.py`、`memory_routes.py`、`mcp_server.py` | owner+notebook 隔离；Agent candidate 与 confirmed-only 正式投影分离；token/scope/allowlist 每次调用重校验。 |
| Knowhow 表 | `backend/app/services/knowhow/`（`projection.py`、`api.py`、`grid_parser.py`、`textops.py`、`assets.py`）+ `repositories/sqlite/knowhow_store.py` + `api/knowhow_agent_routes.py` | 5+1 表 schema 域；唯一零 LLM KG 写入方；变更统一走 `ProjectionScheduler`；代码附件与检索/KG 严格隔离；会话与 Agent 面共享服务核心。 |
| Frontend workspace | `frontend/app/page.tsx`、`frontend/features/`、`frontend/tests/`、`frontend/test-support/` | `page.tsx` 负责编排，feature 纵切片拥有生产策略；测试与 production 物理分离并由 guard 强制。 |

Repository 侧的 persistence 与业务编排已按上表分层完成；应用边界已完成 router、model facade 与 shared transport 的领域分工。collection、来源、Ask、Report、KG workspace 与 typed root-modal 呈现状态已分别迁入独立 owner hook；`page.tsx` 只保留跨域 shell 编排与领域 payload，FastAPI lifespan/application lifecycle composition 仍尚未独立，后续整改继续以现有 facade 和测试为保护层逐域迁移。

## 6. 已知架构债务与整改顺序

整改源自已批准设计的六阶段历史编号；Repository 相关的旧阶段 2、4、6 已合并为一个保持行为不变的 Repository composition refactor 交付（设计见 `docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md`）。下表按当前债务账本合并记录已完成工作与剩余项，列表序号不再等同于原阶段编号：

1. **2026-07-10 历史记录——行为契约与文档对齐**（已完成）：当时修正 Ask disconnect、mode-specific federation/tier 排序、三 tab 两列 workspace、source cleanup 与退役能力文档漂移，重写本文并加入文档契约测试；不改运行时代码。当前 workspace 已扩展为四 tab，见上文实时边界。
2. **Notebook 规模策略与 Repository ports**（已随 composition refactor 交付）：中性 `NotebookScaleProfile` 让 copy 与 retrieval 分别消费自己的策略；巨型 repository Protocol 拆成 `app/repositories/ports.py` 的领域小 Protocol，保留兼容组合类型。
3. **2026-07-21 历史记录——application boundary foundation**（已完成）：领域 FastAPI router 由 `app/api/routes.py` 组合，领域 Pydantic model 以 `schemas.py` compatibility facade 保持旧 import，七个前端 domain API module 共用 `api-client.ts` transport；public/domain seam 与等价性测试替代 aggregate-private coupling。完整 warm gate 已验证三 lane 均不超过 60 秒。
4. **前端 workspace 状态拆分**（已完成当前范围）：`useNotebookCollection`、`useSourceLibrary`、`useAskSession`、`useReportWorkspace` 与 `useKgWorkspace` 已各自成为领域 owner，`useRootModalCoordinator` 另行负责 typed root presentation lease。未引入全局状态库，既有请求数量与轮询节奏保持。
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
python -m pytest tests/test_repository_v9_fixture.py tests/test_repository_snapshot_verifier.py -q
cd ..
python scripts/verify_repository_snapshot.py \
  --database backend/tests/fixtures/repository_v9/baseline.db \
  --storage-dir backend/tests/fixtures/repository_v9/storage
```

编辑期离线门禁与前端生产构建：

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
cd frontend
npm run build
```

门禁按 G0–G3 分级：G0 是按改动选跑的目标测试；G1 `scripts/check.sh` 并行运行 backend、contracts、frontend 三个有界 lane，并用于编辑期及每次 PR/push，backend 默认使用 12 个 backend pytest worker，可用 `BACKEND_PYTEST_WORKERS` 覆盖，Node 原生测试与 Vitest 各限制为 4 workers；frontend 的 production build 负责 TypeScript 校验且禁止 `ignoreBuildErrors`，G1 不在它之前重复执行同一遍 `tsc --noEmit`；G2 `scripts/check_extended.sh` 先复用 G1，再补跑 `slow` 真实索引/性能用例与 `architecture_contract_heavy`（8 个 >2s 的全仓语义扫描）；其余 56 个 `architecture_contract` 测试随 G1 每次 PR 都跑，由独立 GitHub Actions workflow 每天 18:17 UTC（北京时间次日 02:17）执行一次，也支持手动触发；G3 `scripts/check_postgres.sh` 独立负责 PostgreSQL 集成覆盖。G1/G2 backend marker 表达式必须精确互补。每个 lane
拥有独立进程组，controller 收到中断或终止信号时会终止并回收其 pytest/npm/Next.js
后代。静态契约用模块路径、限定 scope、操作类型、目标与审核计数作为语义身份；
源码行号/offset 仅供诊断，不得用作预期站点身份。前端纯逻辑/语义契约使用
`*.test.mjs`，真实组件交互使用 `*.component.test.tsx` +
Vitest/jsdom/Testing Library；策略同时覆盖测试入口和 helper 模块。pytest controller
在 xdist worker 启动前预热仓库本地 Matplotlib 字体缓存，避免每个图谱 worker
重复执行 macOS 字体枚举。Apple Silicon warm gate 硬目标是不超过 60 秒；CI 各 lane 时长仅作观察，不把该本机目标变成 hosted runner 的 timeout 断言。

测试性能优化保持结果语义不变：同一 pytest 进程内的全仓 AST/协议扫描只解析每个生产文件一次；缓存容器策略直接针对容器验证，不为纯淘汰语义构建数据库和 ANN 索引；autouse 隔离路径从各 worker 已有的 pytest base temp 派生，而不是为每条纯测试额外创建 `tmp_path`；普通 UT 与 G1 测试保持环境自足，不绑定宿主端口、不依赖环境服务；只有合同本身属于进程级行为时才保留自包含的子进程/信号覆盖。并发正确性以 event/barrier 握手证明，不把固定 sleep 或线程唤醒顺序当作契约。

SQLite source open 的分类只在 `open_fresh_live_sqlite` 调用边界生效：非瞬态 `sqlite3.OperationalError` 归为 binding identity；locked、busy、interrupted open 仍瞬态整批重试，后续 SQLite operational error 保持原 schema/query 分类。
