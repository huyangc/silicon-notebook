# 开发与仓库契约

[返回 README](../README_zh.md) · [English](./development.md)

本文保留面向贡献者的架构摘要、验证门、工作流、测试架构和文档维护契约。完整 Agent/开发约束仍以 [AGENTS.md](../AGENTS.md) 为准，详细运行时架构以 [architecture.md](../architecture.md) 为准。

外部 Agent MCP 面由一个 API-owned registration host 和启动期冻结目录组成。它先把 `app.api.mcp_tools` 的固定 bundle 捕获为精确 22-tool core 前缀，再追加显式信任的进程内 `agent.tool_provider` contributor 标量 descriptor。core handler 保持既有 validation/auth/I/O 顺序；provider handler 复用实时权威、owner-write、progress 与 output 边界，拿不到 FastMCP、repository、原始凭据或通用 service locator。插件异常只映射稳定公开码，core 在已认证 token owner 的 request/log context 下只发 content-free 的 tool/plugin/status audit，结果 rail 在递归复制过程中执行。默认 provider topology 为空，注册/listing 为零 repository/model 工作。

深度报告后端批量导出是首个真实 single Provider 消费者。Repository 负责授权和已完成行收窄，释放连接后才把最小不可变批次交给启动冻结的 `report.exporter` host。默认内建 Markdown provider 是默认 topology 的唯一 provider，不存在 fallback formatter；core 校验完整有序结果并继续拥有文件名冲突策略与 ZIP 构造。浏览器既有单篇 Markdown 本地下载刻意保持不变，不属于后端 provider 路径。

## 数值上限与截断

生产代码不得在调用点隐藏会改变结果的数字切片或上限。不可调的 wire/storage
边界复用具名协议常量；可在质量/成本之间调整的预算放入带校验的 `Settings`。
用户编辑的列表超过前后端共用护栏时必须明确校验/拒绝，不得静默切片。embedding
等模型输入截断必须在线、批处理与回填路径共用同一配置真源。测试 fixture
中的显式数字不在本规则范围内。

## 架构边界

- 模块化扩展底座与 Phase-1 retrieval host 合同已经生效。稳定跨层值放在 `backend/app/domain`；repository ports 不得依赖 services，静态图保持无环，repositories→services 债务上限只许下降。低依赖 SDK 拥有类型合同，`backend/app/extensions` 拥有冻结 registry、实时 capability 判定与共享 host，唯一外层组合根是 `app.bootstrap`；workflow 只依赖 domain host port。availability probe 无 I/O，manifest 声明与实时判定只投影窄 capability port；一个 capability 不可用时只关闭对应 contribution，独立 contributor 仍运行；invocation 路由和 core admission policy 启动时快照。空路径或无适用 contribution 时在任何工作前原样返回 baseline。内建 proposal 用请求内存做权威复验，其他未解析 proposal 至多走一次 core-owned batch hydrate，禁止 per-hit N+1；畸形插件 fail-open，强合同 lane 可用 atomic admission。Selected-source graph 与 generated-question recall 已分别成为 `selected_evidence` / `chunk_candidates` 的前两个内建 contributor。Graph bridge 保留 legacy activation result，故 attestation、rollout、scope drift、duplicate-support overlay、独立预算、status 与 fail-closed 行为等价；Ask/Report 已删除 graph service 直调。Generated-question bridge 私有持有 query/index/settings 与 `(scored, ids, matrix)`，固定在 MMR/fusion 前，在隔离 copy 上暂存 collision support，并只在 host 接纳后提交；`off`、trigger 已满足、空索引、越界、失败与 `shadow` 都保持原 baseline，`on` 只追加原 chunk。SQLite/PostgreSQL scan 在 `LIMIT` 前应用 notebook/source 与 retrieval-run actor 的私有 Memory 谓词，actor 不进入 event。连接探针在持有 transaction/pool lease 时于 host fan-out 前阻断，PostgreSQL conformance 强制 pool size 1；核心取消在单/多 query 路径都传播。插件实现不得 import 具体 repository、facade 或 runtime。G1 守卫固定这些规则，facade 公开面只可收缩。新增扩展点先读[设计](./modular-plugin-architecture-design-2026-08-21.md)，合入按[流水](./modular-plugin-architecture-delivery-plan-2026-08-21.md)：两路独立 subagent review 和 CI 全绿后 squash merge。 `scripts/check_architecture_boundaries.py`（G1 contracts 泳道）另持有三条登记在 `scripts/architecture_boundary_baseline.json` 的零余量棘轮：`app.core`/`app.models` → `app.services` 的 import allowlist（当前为空；裸 `import app`、`from app import services`、`import app.services` 都算）、所列热点函数的逐函数行数上限，以及 `repositories/ports.py` 的 Protocol 方法总数（按该模块自己的 `Protocol` import 别名解析，`from typing import Protocol as P` 躲不过）。三者任一收缩都必须在同一改动里下调 baseline。两条生产检索 lane（所选来源图与生成问题召回）都识别休眠：工作流已知特性未配置、且 `RetrievalContributorHostPort.has_contributions` 答该 invocation 上没有其他 contribution 存活时，lane 在构造任何 call/context 之前原样返回 baseline；已注册但此刻不可用的 contributor 仍进入 host。
- 生产来源 ingestion 只走启动冻结的 self-hosted MinerU → MinerU cloud → builtin ProviderChain。Link 用 `after`/`before` 边和稳定 ID tie-break 表序，禁止整数 priority；整条 core route plan 在实时 availability 与 provider I/O 前冻结，因此配置 self-hosted 后即使失败也不能打开公共云。Probe 最多做一次远端解析与纯映射且没有持久化/资产端口；workbook 对账与 core admission 先于 accepted materializer。URL 本地 fallback 复用一次 request-local 下载，同一来源的锁覆盖资产替换到 element/chunk marker 发布。旧 dispatcher 与 facade parser patch seam 已退役，禁止重建双路。
- `source.element_enricher` 是 accepted parser materialization 与 core element 事务之间的独立 dormant 批量 Contributor point。默认生产组合不注入 host seat，所以 no-plugin 路径在 snapshot、clock、probe、event 或 I/O 前就是原路径。Active contributor 必须在 required capability 中声明元素正文访问并通过实时 decision 后，才会拿到不可变最小元素视图与 request-local opaque ref；每个来源/贡献只调用一次，不持数据库 lease，只能返回有界的命名空间 metadata，持久化字节预算同时计算 namespace 与 plugin owner provenance。Core 保留 text、type、location、顺序、identity 与 parser/table/image/asset/section provenance；caption 必须已出现在解析文本中。每个 contribution 独立原子接纳，普通失败或非法输出只丢本 contribution，不得抹掉其他已接纳 contribution 或 parser baseline。不得通过本 point 加逐元素调用、直接持久化/模型/资产 capability、Memory/Knowhow 路由或图片检索语义。
- `knowledge.candidate_projector` 是旧 core `extract_graph → build_records → relink → partial-retry` 序列之后、唯一一次来源代次 `store_kg` 事务之前的独立 dormant 批量 Contributor point。默认组合不注入 seat，隐藏 Memory/Knowhow 也在 schema read、snapshot、clock、probe、event 或 I/O 前短路。Active contributor 必须声明且实时通过 scoped-source-element capability，只取得不可变的来源内 element/active-schema 视图和 opaque ref，绝不取得 baseline KG 容器或 source/notebook/path/runtime 权限；返回值只能是一个有界、原子的 object/relation batch。Core 用 exact 当前元素 ref 与逐字原文重建 evidence，复检 payload schema 与中央 edge pair，分配 local id，保留 baseline prefix/order/identity，并继续拥有 review status、generation CAS、source facts、单次写事务、embedding 与 retrieval publication。普通失败或非法输出只丢当前 contribution。同步 callback 可以完成，但 point deadline 后返回的输出会被拒绝，也不再启动后续 contribution。不得把 point 搬进持久化、partial-retry 保留 gate 之前或来源代次提交之后，也不得增加逐元素/N+1/model/database capability。
- `backend/app/application` 持有低依赖的不可变 stage envelope；模块级 import allowlist 当前只开放 application、core.ask_retrieval_policy、domain.cancellation、models.ask，裸 root import 与无别名的 allowed-submodule import 都会绑定 `app`、因此禁止；显式 alias 的 allowed submodule 可用。未来合同必须显式扩展，不能放开一个存在实现层反向边的整包。Ask reasoning 显式跨越 prepare、retrieval-evidence、response-draft 与 committed-answer 边界；冻结的响应 envelope 独占传递 typed response 对象图，不做破坏共享 citation 身份的 JSON/deepcopy。runtime 权威交叉绑定同一个 source scope、`ask_reasoning` 请求级 run（也就是 leaf-I/O semaphore 的所有者）、取消 token、非空持久化 actor、trace sink 和 AskService 注入的同一个无 I/O 连接探针；service 与 typed retrieval seam 各自校验 run kind/actor。Stage 边界违约必须作为核心错误响亮失败，不能伪装成可选检索 miss。Report 的可变 `ReasoningResult` 工作副本继续保留；只有既有 KG/chunk/element/PPR leaf 调用取得 run slot，任何 stage wrapper 都不得持有数据库连接或外层 slot。
- Deep Report 以独立的不可变 application envelope 交接 confirmed planning、generated sections、core final audit 与 committed completion。Planning/generation 各保留新 retrieval run，typed 边界逐次校验同一 scope/run/cancellation/actor/probe，不移动任何 leaf slot；可变 evidence/id-map 只做独占所有权转移，不 JSON/deepcopy。多节 all-retrieval → 一次 synthesis → 并行 drafting、单节零 synthesis、final editor、claim ledger、citation remap、整篇 image batch、零正文失败与 retry 均由 core 按原序拥有，final audit 禁止改写章节 Markdown。SQLite/PostgreSQL 用 `status='generating'` 的同构 CAS 发布 `done`，取消同样只可从非终态 CAS 到 `cancelled`，只有完成 CAS 成功才产生 `CommittedReport`；manual/auto generation 在 coordinator 汇入同一路径，并先释放 generation gate 与 scope/retrieval/model 上下文。随后默认空的 `report.audit` 只获有界计数，内建 `report.completed_observer` 通过一个 opaque at-most-once access 保留既有 agent-profile 信号，最后才注销取消注册以保留既有 active-job 窗口；两者都不能改持久产物或启动 retrieval/model。
- 流式 Ask 的完成后处理使用两个独立、启动冻结的 `answer.audit` 与 `ask.completed_observer` host。持久答案、job 终态、取消注销和浏览器 final 事件先于二者，sentinel 在其后。Auditor 只拿不可变、无正文的结构快照且不能替换答案；空 auditor 拓扑不触碰 context、probe、clock、event 或 I/O。三个内建 observer 保持 agent-profile → retrieval-experience（仅 reasoning）→ search-profile 的既有串行行为和成本，分别独立 fail-open，并只获 notebook+actor / 零身份 / actor。Connection probe 在插件执行前拒绝仍持 transaction/pool lease 的调用；普通失败不能反转 `done`，终态后 hook 不读取请求取消。同步 POST Ask 与 MCP Ask 仍不计入该完成口径，service 只依赖 domain host port，不依赖 SDK/registry/concrete host。
- Point-specific proposal source 与通用 admission reader 是两个独立 domain port。Selected-source graph 的权威复验只读请求级内存 map，零新增 DB/leaf；只有其他未解析 proposal 才调用一次 repository-runtime reader，并在 SQL 返回行之前应用 notebook/source 谓词。Report fallback 读取必须取得共享 retrieval leaf gate。SQL 始终按 actor 过滤 Memory source，包括没有冻结 scope 的兼容调用；可见来源和 notebook-wide Knowhow 仍可进入。
- 后端 endpoint body 位于由 `backend/app/api/routes.py` 组合的领域 FastAPI router；聚合层只负责 composition/order，不承载产品 handler，也不提供兼容导出。边界测试直接检查领域 router 的 endpoint 所有权，并以语义 AST 检查聚合组合声明；不要假设 `include_router()` 一定把子路由平铺，因为新版 FastAPI 会保留惰性的 included-router 节点。领域 Pydantic model 位于 `backend/app/models/`；`backend/app/models/schemas.py` 是旧导入的兼容 facade，re-export 同一批 model object。
- 唯一 repository factory 按 `DATABASE_URL` 选择 `SQLiteRepository` 或 `PostgresRepository`；两者组合相同运行时边界。`RepositoryFacade` 是注入 `RepositoryRuntime` bundle 之上的后端中立 facade。application service 不拼装主业务库 SQL、不判断 dialect，也不 import 对侧 adapter。store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。SQLite 保留 migration/maintenance 兼容 wrapper，PostgreSQL 拥有有界 Psycopg pool 和 checksummed migration。facade 操作仍是显式兼容 adapter 或源码守卫验证的单跳委托，真实目标必须与 ownership manifest 一致；这些单跳委托由 ownership manifest 固定。依赖方向固定为 factory/wrapper → facade → runtime → services → stores。`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim，请求 Context、`_COPY_CHUNK`、`_remap_json_ids` 等旧导出继续可 import。
- notebook 授权判定收敛在每后端一个唯一定义点 `backend/app/repositories/{sqlite,postgres}/access_sql.py`（镜像 `mount_sql.py`，两份文件占位符风格互为镜像、必须同修）。写权 owner-only、管理权 owner∪`role='admin'` 有效授权边（`NOTEBOOK_ADMIN_SQL`，复用读权的受限三臂＋`role='admin'`、排除 `everyone`）、读权 owner∪只读成员∪有效授权边（`notebook_grants` 的 user/group/group_admins/everyone 四值精确白名单，停车行因而 fail-safe），三者成 `写权 ⊆ 管理权 ⊆ 读权` 包含链，这条不对称是安全边界；Memory 读/检索 SQL 里的「owner∨成员」子句同源派生，memory store 的三段式 `FOR SHARE`/三态站点按 allowlist 刻意保留、扩读权时须手动同步。读权⇒可挂载（`mount_sql.MOUNT_VALID_EXPR`），但**受限**授权（everyone 以外）只在挂载方笔记本自身未被共享时生效——这道未共享门堵的是把借来的参考库转手再分享；`tier='base'` 与 everyone 不受此限。API 写端点经 `app/api/deps.py::require_notebook_capability("<能力>")` 按 9 个能力名归类，值域 `{owner, admin}`（P2 把六个内容管理能力 `sources:write`/`kg:write`/`knowhow:write`/`knowledge:write`/`catalog:write`＋`notebook:manage` 翻 admin，`notebook:configure`（挂载配置＋链接分享）／`notebook:delete`／`reports:write` 恒 owner；未知能力名 import 期 `KeyError`），体内自查经 `notebook_capability_allowed(capability, ...)` 吃同一张表；新写端点不得挂裸 owner 守卫。守卫：`backend/tests/test_access_sql_contract.py`（双后端自省 parity/占位符方向/内联形状扫描/两段式 allowlist）与 `test_notebook_capability_guard.py`（AST 标识符扫描＋空转保护）。群组知识共享（设计稿 `docs/superpowers/specs/2026-08-17-group-knowledge-sharing-design_zh.md`）扩读权谓词、翻能力映射表，端点声明不动（Agent/MCP 面刻意不翻；P2 另增成员贡献审批流 `notebook_share_requests`：成员对自己 manage 的库、向只是成员的组提交申请，组管理员批准即同事务插 `(group, viewer)` 边，`status` 精确匹配三值、撤回走整行 `DELETE`）——**深度报告是唯一登记的例外**：共享笔记本的成员可以建**自己的**报告，而报告按创建者隔离，这个形状表达不成一个 notebook 级能力。9 个报告写端点因此改挂 `require_notebook_read` ＋ 体内行级 `reports.created_by == 当前用户`（`report_routes.py::_own_report_or_404`；凡路径含 `{report_id}` 的端点都必须调用它，有 AST 守卫钉住），列表/导出按同一谓词在 SQL 里收窄，别人的报告与「不存在」同为 404。`reports:write` 保留在能力表里但当前零消费点，留给 P2 的组管理员管理动作；免登录分享页每次请求实时复核创建者的读权，失权即链接失效。
- 离线生产维护统一通过 `open_maintenance_cli_repository`：PostgreSQL 停服确认/能力拒绝必须早于 factory 构造，随后由独立非池化 session 持有 fail-fast advisory lock，任何退出路径都关闭 repository。`BatchMaintenancePort` 是可移植编排契约；SQLite 文本向量转换保留为独立物理格式 port。PostgreSQL keyset 的谓词与排序都使用 `COLLATE "C"`，读取一页后释放数据库连接再等待模型；来源清单按 phase 排除隐藏投影源。离线 full gate 不连接 PostgreSQL，真实覆盖放在独立 PostgreSQL 16 lane。
- `prepare_selected_source_graph.py` 把可移植维护操作编排成全 notebook 部署状态机：持久来源反查索引页、持久 source-fact 代次、低成本版本/计数工件探针（失配时才做有界重建）及独立事实审计都在离线维护锁内完成。receipt 无正文且不是权威状态。只有 repository 关闭后脚本才能原子写入四个不可见 shadow 环境变量；任一阶段失败都保留原 env 文件。再次进入会重新验证权威状态并跳过当前代次/工件，不在大库上重复已完成工作。
- `RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有；完成组合后替换受支持的兼容属性时，所有已持有它们的消费者都会同步更新。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再把提交异常重新抛出；成功 worker 的次序与既有 Ask 事务 checkpoint 不变。
- 内置 KG 关系统一由 `backend/app/services/kg/edge_schema.py` 的有类型注册表治理。核心抽取 fail-closed；graph/PPR/canonical/relation 与 Ask 证据上下文消费者过滤历史非法 core 端点，同时保留连接管理员扩展类型的已知边；`EDGE_SCHEMA_VERSION` 进入 scale/PPR 工件 identity。可选关系补全按模式和来源代次的持久 keyset 水位逐页推进，通过索引化且契约合法的 relation `EXISTS` 优先 anchor，并只使用同源、有界 overfetch 的 FTS/ANN 候选及 section/pair/batch/字符护栏。每个任务只 hydrate 当前有界对象及其受限证据 ID；未完水位重新入队，启动时恢复当前代次的 pending 状态；模式改变用同一 generation-CAS 事务先发布新模式可恢复游标，再把旧模式游标标为 `stale`。proposal 与 verification 在数据库事务外完成，最后在短写事务内复核代次、归属、存在性，保存 verifier 看到的同一段服务端 excerpt 并幂等写入；非法零值护栏 fail-closed 且不推进水位。检索来源保存为按 producer 累积的 support record，选择层不得从 score 反推来源。
- 超大所选来源图伴生产物与旧 scale 目录分离。离线 builder 通过 source-first 有界投影每次只读取并发布一个可见来源 partition，用恒定大小的伴生根 manifest 与每个 partition 绑定主 manifest version，校验所有 payload 文件摘要，并用确定性哈希路径让运行时只打开所选来源。reader 在 payload I/O 前先用所有所选小 manifest 预检累计 node/nnz/cross-edge 护栏。本地 CSR 行携带对象类型/chunk 身份；只选一个来源时直接复用其落盘 CSR，所选并集则使用数组化稀疏组合和一次受限 cross-edge 分配。来源自有的跨 partition 关系只有在并集再次校验两端及中央 edge registry 后才接纳；候选排名使用局部 Top-K，不做全量 Python 排序。旧版、缺失、损坏、越界或 identity 失配伴生产物只返回 capability unavailable，绝不授权整图事后过滤。full rebuild 与 delta fold 都会重发伴生产物并失效其专用 single-flight LRU。运行时 reader 只供统一 Ask/Report 激活服务消费，不可用时 fail closed 回 B。
- 所选来源质量边界刻意拆开：`app.eval.selected_source_graph` 负责 golden case 评测与 observation 解析；`app.services.source_graph_quality` 负责 production 使用的版本化、无正文 attestation schema/verification；`app.services.source_graph_rollout` 负责纯函数式 off/shadow/allowlist/hash/on 决策。production module 禁止 import `app.eval`。套件冻结 model/sampling/corpus/scope/source alias，把 citation anchor 逐项绑定 evidence provenance，先检查硬隔离与 baseline preservation，再比较质量/成本，并同时检查逐案例和汇总。激活钉死 canonical golden 摘要，自定义 golden 只作诊断；production 会重算所有无正文逐案例/汇总护栏，corpus/model 任一 pin 缺失均 fail closed。attestation 摘要只检测意外修改，受信路径所有权仍归部署负责。只有统一激活服务 import rollout decision；Ask/Report consumer 不得实现第二套 gate。
- 重构前创建的数据库可原样加载。`scripts/verify_repository_snapshot.py` 使用精确的逐版本 migration manifest 与稳定 seed manifest，对 SQLite URI 路径做百分号编码，只在临时 backup 上构造 repository；cleanup 失败时只报告保留的 backup 路径，不输出私有行。它校验原 DB/WAL metadata 以及 SHM 的存在性和大小；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。
- 逐步推理的来源身份查找是纯身份 repository 操作，不读取来源正文、摘要、元素、KG payload 或 embedding。两个 adapter 都按稳定的 `(created_at,id)` 顺序分页读取可见且已授权的来源目录，并使用部分索引 `idx_sources_visible_identity`：`(notebook_id, created_at, id) WHERE source_type NOT IN ('memory','knowhow')`。消费这份目录的服务层解析器已随「模型判断来源」一并移除，因此 `visible_source_identity_rows_bounded` 目前没有生产调用方；索引与两侧实现仍予保留，因为检索范围依旧以 `(notebook_id,source_id)` key 表达，且空来源 id 集合表示空、绝不表示不限制。

当前 schema 版本为 57。这里指 SQLite schema。已提交的 v9 兼容 fixture 会经由 v10–v57 migration 升级并保持可读：v10–v12 覆盖兼容与 SQLite 热路径索引，v13–v15 覆盖 Memory/Agent 与 Memory 派生源 link/index，v16/v18 覆盖 knowhow 表与格子代码，v17 覆盖论文元数据，v19 覆盖来源内嵌图片资产，v20 覆盖多领域参考库挂载与晋升目标，v21 覆盖交互式规整 anchor 成员检查的归一化表达式索引，v22 增加持久化的 notebook 级 KG 构建任务，v23 增加每用户最新模型服务状态，v24 增加 kg_canonical_scratch，v25 清除旧用户模型凭据并新增部署级模型服务状态，v26 增加 knowhow 变更流水/里程碑，v27 增加 sources.chunked_at，v28 增加文档数量上限 schema，v29 确定性清理重复 cluster membership 并安装唯一索引，v30 增加 sources(notebook_id, file_hash) 内容哈希去重索引（上传去重 / batch_ingest 续跑），v31 只增加 inert、无 payload 的 shadow_change_log 与 shadow_capture_control 内部表，v32 增加 reports.understanding_json，持久化深度报告的问题理解确认契约，v33 增加 `(notebook_id, source_object_id/target_object_id, id)` 覆盖索引，供关系词法补召回稳定地做有界 keyset 查询，v34 增加关系补全水位与对象 keyset 索引，v35 增加浏览器提交时间 `ask_jobs.asked_at` 供生成中会话重连，v36 增加 KG 质量分析的三张预计算产物表（kg_community_edges、kg_source_profiles 与产物账本 kg_analysis_artifacts）；rebuild_communities 整体重写它们，账本逐份记下产物建于哪个 kg_mutation_seq；发布是原子的——板块划分、community_seq 戳与三张产物表在同一个写事务里提交，而喂给它们的全表读全部待在那个事务之外（SQLite 写锁是进程级的）。三张表都不带 level 列——社区层的新鲜度闸本身不分 level，产物描述的 level 记在账本 payload 里；v37 增加 `source_elements` 上按 `(source_id, element_type, created_at, id)` 的索引，供有界、按类型的集合枚举（公式/表格/图片/代码块清单）；v38 增加部分可见来源身份索引 `idx_sources_visible_identity`：`sources(notebook_id, created_at, id)`，排除隐藏的 Memory/Knowhow 投影；v39 增加命令目录抽取的 `catalog_jobs`（每次运行一行，带按来源的 `queued`/`running` 条件唯一索引——那就是跨进程单飞守卫）与 `catalog_candidates`（每条抽取结果或被接地校验拦下的条目一行，按 job 内 `position` 做 keyset 排序）；`catalog_jobs.source_generation` 记下任务创建时刻的来源元素代次，来源被重新解析后这一轮候选整批作废，不会被确认成文档里已经不存在的内容。`catalog_candidates.job_id` **刻意不加外键**：候选直接挂在 notebooks/sources 上级联删除，而一条指向 catalog_jobs 的入向外键会让它不再是叶表，那个 source_id 单列守卫就没有可用的正向 shadow 停车方案了。v39 还在既有表上装了本迁移唯一的一个索引 `idx_knowhow_tables_nb_title`：`knowhow_tables(notebook_id, title, created_at, id)`，让按标题解析目标表变成一次索引定位——前两列等值 seek，后两列直接给出 `(created_at, id)` 的 tie-break 顺序，不再在 apply 的持锁窗口里把该 notebook 下每一张表都读一遍。SQLite v40 增加不可变的 `knowledge_source_facts` 与规范化 `knowledge_source_fact_elements` 绑定；写入方在全局 KG 同一事务内校验当前 running 抽取代次和每个证据元素的来源，替换时同事务清除旧代次；`global_object_id` 刻意不加外键，避免全局融合/治理抹掉来源事实。本迁移只启用存储与写生命周期，读取由后续 PR 激活。PostgreSQL v20 是配对业务 schema。临时 shadow 边界已有 preflight/control/guard、run-bound 原子 snapshot、有界可续跑 baseline COPY/H0，以及 fail-stop 单消费者正向 replicator 原语。replicator 连续校验全局 seq、在短只读 snapshot 仅为 upsert hydration 当前行，delete 保持 key-only 且 hydrated bytes 为零；同一 stable key 在 accepted prefix 内保留最后 event 并按全局最后 seq 排序，raw seq/checkpoint 仍连续，每个 identity 的最终 actual apply 覆盖 synthetic dependency contribution，只有 dependency-only identity 才引用计数一次 synthetic 行及其 bytes；短读窗口若在 allocated high-water 前结束，会在 hydration/apply 前立即判为 suffix gap；满窗口低于 high-water 时在同一 snapshot 探测相邻 seq，缺失即失败；PG apply 事务 claim worker 后、业务 DML 前复查既有 run/direction poison；poison 发布在 binding/checkpoint 校验后锁定检查该方向任意既有记录，完全相同视为 ACK-loss 成功，不同则 stale 且绝不新增第二条，再重新锁定 ledger+81 表并复核 snapshot source/target、live target identity 与精确 catalog后，把业务收敛、脱敏 progress 与 checkpoint CAS 同事务提交。批次硬上限为 4096 events/64 MiB；仅一个 final bundle 可独占超限，同 key replacement 若在已有其他 actual bundle 时使 bytes 超限则回滚并延后。FK 父闭包只读同一验证 source snapshot，每事件最多 64 行；固定 v32 图按 FK constraint branch 计数的上界为 12 个 row slots，依赖行计入 bytes且批内去重，不扫描 suffix log。PG 只延后 FK/UNIQUE ordering SQLSTATE，CHECK/NOT NULL 立即 poison；精确 PG32 catalog 的 110 个 unique surface 通过 NULL、按其他唯一列的非 NULL 等值/NULL `IS NULL` 与固定 predicate 定域的确定性 text/bigint 候选（`C` collation 文本 max 拼 `chr(1)`，或先走可索引 bigint MIN/MAX 快速路径选择 min−1/max+1，仅在两个 int64 边界都占用时扫描首个 gap），或仅限无入向 FK 且有 accepted current-final 恢复行的叶表同事务 delete/reinsert 来解 cycle。停车状态按 `(unique surface, row identity)` 跟踪；每个 stagnant pass 会停车所有可独立停车的冲突，final apply 成功会清除该 identity 的所有停车面。限制为 8 passes、32 actual statements/apply、16384 actual statements 总量；每次候选查询都计入预算，ordering、statement、pass、`ProgramLimitExceeded`/`DataError` 候选搜索与候选 UPDATE 容量耗尽保持 non-poison，`QueryCanceled` 保持瞬态并整事务重试，最终窗口不可停车的 UNIQUE 冲突则按最早实际 seq poison。worker 从 256 events/8 MiB 自适应倍增至硬上限，仍 ordering-blocked 时 non-poison；ack-loss 与 poison publication 使用相同 identity 绑定，snapshot 与业务 apply 前均要求 `progress.applied_seq == checkpoint.last_seq`。每个有效 batch 结局恰好记录一条脱敏 metric，batch events 使用实际 accepted/observed raw-event 数并尽可能保留 retries。瞬态错误整事务有界重试，SQLite path/file binding 失败使用专用 identity 异常而不依赖文本分类；已证明的确定性错误在实际阻断 seq 写一条脱敏 poison 后停止。显式运维 CLI 已提供 preflight/start-forward/status/verify；前台 worker 使用数据库时钟排他 lease、SIGTERM/INT 批次边界，并只在 FULL 校验、barrier/replay/poison、至少 7 天/100,000 events tail 等边界之后保守清理。`SHADOW_DATABASE_URL` 单独设置仍不启动同步，且只有该 CLI 可以读取；本阶段不含 cutover、反向复制或自动 active URL 交换。
SQLite v41 新增 `knowledge_source_fact_backfills`，以「可见来源 + 来源代次」记录显式离线历史投影的游标、计数、投影版本、稳定不完整原因、独立运维失败码和终态；`knowledge_source_facts.projection_origin` 显式区分在线抽取与历史投影，在线事实即使已失去融合全局对象仍会被保留并计数。命令每本 notebook 只先构建一次来源反查索引，后续运行复用其完成标记，再按来源做有界对象 keyset 分页，每页一个短写事务。只有 owner 与全部证据元素都能证明属于该来源的历史对象才会进入来源事实；混合或缺失来源的旧数据只记为 `incomplete`，绝不猜测。审计会独立对账有效 KG 代次、投影版本和持久事实数量，不信任账本上的 `complete`；它只输出聚合计数与有界 source id，不输出证据原文。深复制用同一来源代次映射重写事实、证据绑定与终态账本，并生成副本本地的 completed KG run，因此副本可独立审计或强制修复，不保留对原 notebook 运维抽取历史的依赖。这仍是只写准备阶段，不改变在线 Ask 读路径。

SQLite v42 新增 notebook 级 `source_index_backfills` 执行账本。来源反查索引的每个有界 keyset 页面都在同一个短事务里写索引行并推进游标/计数，因此进程重启会从最后已提交页面继续，而不是先清空 notebook 再重来。账本固定 `kg_mutation_seq`；代次漂移只记录稳定的 `kg_generation_changed` 并保持快速路径标记为 false，下次运行再按新代次从头构建。当前完成标记会被规范化成完成账本，不重写索引行。账本不保存证据正文或原始异常。PostgreSQL v20 为配对 schema。

SQLite v43 新增可撤销的报告公开分享 token。SQLite v44 新增 `chunks.question_indexed_at` 与归属原 chunk 的 `chunk_questions`，用于可选的生成问题检索补充；删除/重解析级联清理，深复制会重写 chunk/source/notebook 身份。PostgreSQL v22 为配对业务 schema。SQLite v45 新增可空的 `user_profiles.ui_mode` 列，承载每用户界面模式偏好（默认「自动」/「高级」）；列或 profile 行缺失时读路径回落「自动」，PostgreSQL v23 为配对业务 schema。正向 shadow 当前使用 SQLite54/PG32/epoch1、81 张业务表、110 个 unique surface，固定 FK 图的分支计数闭包上界为 12 个 row slot。

SQLite v46 增加 element→chunk 反查索引 `chunk_elements`、它的 notebook 级执行账本 `chunk_element_backfills`，以及分叉读路径的 `unified_kg_state.chunk_elements_indexed` 标记。`chunks.element_ids` 存的是正向关系，所以「哪些 chunk 含这个证据元素」过去要按索引代次全量扫该 notebook 的 chunk 行并逐行解 JSON；复合主键 `(notebook_id, element_id, chunk_id)` 把它变成有界点查，额外那条 `chunk_id` 索引只为服务 `chunks` 的级联删除。活库够得着的每条 chunk 写路径都在与 chunk 行**同一个写事务**内维护反查行，删除来源/重新解析/改写 knowhow 格子经该级联带走旧行。唯一已登记的豁免是整本深拷贝：它不复制 `unified_kg_state`，副本 marker 恒缺失、走旧全量路径。迁移只建空表；历史行只由显式离线的 `backfill-chunk-elements` 阶段投影，其账本形状与 `kg_generation_changed` fail-closed 规则同 `source_index_backfills`，同样不存 chunk 正文或原始异常。标记仍为 false 的 notebook 逐字保持旧的整库扫描路径。PostgreSQL v24 为配对 schema。

SQLite v47 新增以 `(notebook_id, object_type)` 为主键的 `notebook_object_schemas`，承载笔记本本地的图谱对象类型定义；全局 `object_schemas` 继续作为管理员维护的默认基线。生效注册表以 notebook 行覆盖同名全局类型，因此本地 `disabled` 只屏蔽当前笔记本。每条本地定义还保留创建者用于归属/审计，实际授权仍由实时 notebook owner/read 守卫执行。PostgreSQL v25 为配对 schema，正向 shadow manifest 同步纳入这张业务表。

SQLite v48 新增可空的 `sources.agent_profile_id` 出处列，记录某份来源是 Agent（而不是人）添加的。NULL 是承载语义的取值——它表示「这是人添加的」——因此不做任何回填：已部署的每一行按定义都是用户添加。该列刻意不建索引、不加唯一约束，也不对 `agent_profiles` 建外键：MCP `delete_source` 背后的权限判定是一次主键单行读取，没有任何地方枚举「这个 Agent 的来源」，出处必须比 profile 行活得更久，而一条入向外键还会给正向 shadow 的父闭包多加一条边。该列只在 INSERT 分支写入，所以同内容去重复用既有行时保留首写者的出处，笔记本深拷贝则显式清空它。`SourceSummary` 与来源详情模型把它投影成 `agent_created` 布尔。PostgreSQL v26 为配对 schema；由于该列不新增表、索引、约束或外键边，它没有改动当代的正向 shadow 不变量（74 张业务表、100 个 unique surface、分支计数闭包上界 12 个 row slot）。

SQLite v49 新增群组知识共享的三张表。`groups` 记一个群组的名称、`kind`（`project`｜`department`｜`domain`——只是**分类标签**，影响谁能建组与界面文案，不影响任何权限机制）与说明；`group_members` 以 `(group_id, user_id)` 为主键把用户映射到群组并带两级组内角色（`member`｜`admin`），另按 `user_id` 建索引服务「我在哪几个组里」这个方向；`notebook_grants` 每行是一条**生效中**的授权边 `(notebook_id, principal_type, principal_id, role)`，`principal_type ∈ {user, group, group_admins, everyone}`、`role ∈ {viewer, admin}`。所有取值枚举一律在应用层校验，schema 刻意不加 CHECK；`principal_id` 是**多态**引用（user id｜group id｜`everyone` 存空串），也刻意不对 principal 建外键——正向 shadow 的静态停车方案要求这两列里至少有一列保持裸文本列。

这个形状有两条不可省的推论。其一，`principal_id` 必须保持 `NOT NULL DEFAULT ''`：NULL 不参与唯一比较，`everyone` 行会整个逃出 `UNIQUE (notebook_id, principal_type, principal_id)`——重复授权可累积、撤销撤不干净；而 NOT NULL 还把 shadow 的停车列让给了 `principal_type`（SENTINEL_TEXT）。其二，`everyone` 的判据只能写 `principal_type='everyone'` 的四值精确匹配，绝不能从 `principal_id` 推断（`IS NULL`／`=''` 都不行）：停车会给冲突行的 `principal_type` 暂写一个哨兵串，精确匹配正是让停车行 fail-safe（谁也匹配不上）的原因。`UNIQUE` 的隐式索引已覆盖 `notebook_id` 前缀查找，因此不另建 notebook 单列索引；`idx_notebook_grants_principal`（`(principal_type, principal_id)`）服务「这个组被授权了哪些库」这个方向。笔记本深拷贝**不带**授权边，照 `notebook_members` 先例——访问控制状态不是知识，副本由新 owner 重新授权。删组在**同一个写事务**里清掉指向该组的授权行（`principal_id` 无外键，数据库替不了这件事）；合库可能复活的孤儿边由 `scripts/merge_dbs.py` 清扫。

PostgreSQL v27 为配对 schema。由于 v49/v27 新增三张表与一条 UNIQUE 约束，正向 shadow 的不变量变为 77 张业务表、104 个 unique surface；分支计数闭包上界仍为 12 个 row slot（三张表都是浅层）。

SQLite v50 新增成员贡献审批流表 `notebook_share_requests`——`notebook_grants` 的兄弟表，刻意独立于 grants 表，好让判定谓词零 status 过滤。普通成员对**自己 manage 的库**、向自己**只是普通成员**的目标组提交申请（组管理员分享进自己管理的组永远走既有 grants 端点、不经这张表），组管理员审批时在**同一写事务**里插入 `(group, viewer)` 授权边并更新状态。状态机 `pending → approved/rejected` 单向：撤回是申请者走整行 `DELETE`（仅 `pending` 时），不写第三个状态，两个 FK 均 CASCADE，深拷贝不带申请。`decided_at` 只允许写 SQL `NULL` 或 ISO 时间戳，绝不写空串——它是本表唯一进入正向 shadow 的可空时间列，PG 的 `timestamptz` 收到 `''` 会直接类型报错，且刻意不登记进 `POSTGRES_EMPTY_TIME_SENTINELS`。部分唯一索引 `uq_share_requests_one_pending`（`(notebook_id, group_id, status) WHERE status = 'pending'`）保证同一 (库, 组) 至多一条在飞申请，创建端点撞它时幂等返回既有 pending 行、而非 409；`status` 一律精确匹配 `pending`/`approved`/`rejected`，绝不用 `!=` 当判据。PostgreSQL v28 为配对 schema；由于 v50/v28 新增一张表与一条部分 UNIQUE 索引，正向 shadow 的不变量变为 78 张业务表、106 个 unique surface；分支计数闭包上界仍为 12 个 row slot（新表也是浅层）。

SQLite v51 新增 Agent 库理解的两张表 `agent_notebook_profile` 与 `agent_profile_jobs`，承载「AI 对这个库的理解」——一份低成本、经 LLM 巡固的、关于笔记本的理解摘要。`agent_notebook_profile` 以 `(notebook_id, owner_id, label)` 为主键存五个 label 块：三个共享底座块（`corpus_shape`／`key_entities`／`corpus_gaps`，`owner_id=''`，来源变更累计到阈值后由按笔记本的巡固 job 刷新）与两个每成员覆盖层块（`retrieval_notes`／`usage_gaps`，`owner_id` 为该成员用户 id，该成员完成足够多次提问或一次深度报告后刷新）。`owner_id` 沿用 v49/v27 `notebook_grants.principal_id` 的先例：`NOT NULL DEFAULT ''` 而非 nullable，刻意不对 `users` 建外键，也不对它或 `label` 加 CHECK 约束。`agent_profile_jobs` 是每条链路一行的状态/计数器表，以 `(notebook_id, owner_id)` 为主键；单飞由主键行 CAS 承担，不另建唯一索引。两张表的 replication key 都逐字等于声明主键，因此正向 shadow 自动按 `REPLICATION_KEY` 停车，不需要哨兵列，也不需要 `_UNIQUE_PREDICATES` 条目。`agent_notebook_profile.history_json` 是与块更新同一写事务内追加的有界环形 before/after 历史，代替独立的变更历史表——P1 界面只有看/改/清空/手动重建，没有历史回看，一张可查询流水表买不到任何 P1 能力。笔记本深拷贝不带这两张表：副本从零重新形成自己的理解，job 行是与 `catalog_jobs` 同理的过程状态。PostgreSQL v29 为配对 schema。由于 v51/v29 又新增两张（同样浅层的）表，正向 shadow 的不变量变为 80 张业务表、108 个 unique surface；分支计数闭包上界仍为 12 个 row slot。

SQLite v52 给 `conversations` 增加问答会话公开分享的三列：`share_token`（可空，部分唯一索引 `idx_conversations_share_token WHERE share_token IS NOT NULL` 只覆盖已发放的 token，NULL 停车与 `notebooks.share_token`／`reports.share_token` 同款）、读取水位 `shared_through_at`（时刻字面值，不是外键——存 answer id 会在该 answer 删除后失去意义）与展示用 `shared_through_id`。token 挂在会话行上而非侧表（同 `_migration_43` 报告 token 先例，会话删除即带走公开链接）。笔记本深拷贝无需处理这三列：`_COPY_VALIDATED_TABLES` 本就不含 `conversations`，故它们永不随副本走，也无从清空——迁移注释写明了这条，以防后来者照 notebooks/reports 先例加多余清空。PostgreSQL v30 为配对 schema。由于 v52/v30 只给既有表加列、不加表也不加外键，正向 shadow 的业务表数不变（仍 80 张），只是新增的这条部分唯一索引把 unique surface 从 108 抬到 109；分支计数闭包上界仍为 12 个 row slot。

SQLite v53 新增 `agent_profile_jobs.claim_token`（Agentic Memory P2）：巡固链路的**认领代际**，一列 `TEXT NOT NULL DEFAULT ''`，每次 `claim` 现铸一个新值，并由 `settle` 与 `write_block` 一起当作 CAS 条件的一部分。它关掉 P1 只按 `status` 做单飞留下的 ABA——成员被移出再加回来会得到一行主键逐字相同、`runs` 回到 0 的新行，旧 worker 的 settle 因此会落在替身行上（消费掉新 run 的快照），它的写入也能通过只看「行在不在」的存在性检查。删除+重建必然换 token，「我认领的那一行」与「现在这一行」从此可区分。`settle` 的返回值也因此从二值变三值：`settled`、`gone`（行没了——只有成员移出会删它，调用方必须把这一轮重建出来的块清掉）与 `superseded`（行还在，但属于更晚的一次认领——调用方**绝不能**清，新一代可能已经写好了自己的块）。不新增表、索引或 unique surface，故正向 shadow 的不变量保持 80 张业务表、109 个 unique surface、12 个 row slot。PostgreSQL v31 为配对 schema。

SQLite v54 新增 `retrieval_experiences`（Agentic Memory P2）：**部署级全局**的检索策略经验库。一条经验说的是「在**这类问题形态**下，**这个检索动作**值得／不值得用」，外加一句模型撰写的理由、一个 `support`（多少次 run 支持这条结论）与一个 `adopted`（注入之后模型真的选了这个动作多少次）。本表刻意**没有** `notebook_id`、没有 owner 列、两个方向都没有外键：它存的是「怎么查」的通用打法，不是任何人的内容——所以笔记本深拷贝结构上够不着它（与 `groups`/`group_members` 同一句论证），`scripts/merge_dbs.py` 把它归进全局并集表。主键是单列**内容寻址** `TEXT`——「情境指纹 + 动作」的确定性哈希——这既让跨独立部署的并集是安全的（递增 id 会在主键冲突时静默丢行），也因为声明的 replication key 与它逐字相等，让它唯一那个 unique surface 自动落 `REPLICATION_KEY` 停车：无哨兵列、无 `_UNIQUE_PREDICATES` 条目。刻意不建索引：行数有硬上限，读路径只有主键点查与一次有界全扫。由于 v54/v32 又新增一张（无父、叶）表，正向 shadow 的不变量变为 81 张业务表、110 个 unique surface；分支计数闭包上界仍为 12 个 row slot。PostgreSQL v32 为配对 schema，当前配对为 SQLite54/PG32/epoch1。

SQLite v55 一次迁移落两样（Agentic Memory P3）：叶表 `agent_observations`（一个出向 FK 到 `notebooks`、无入向 FK——外部 Agent 按 `(笔记本, 用户)` 的观察队列，环形淘汰，只喂不可信的覆盖层巡固 prompt）与可空列 `user_profiles.search_profile_json`（每用户检索/回答风格偏好文档；`NULL`＝从未设置过，与 `ui_mode` 同一套契约）。`agent_observations` 的幂等唯一索引 `idx_agent_observations_request`（`(notebook_id, owner_id, agent_profile_id, client_request_id) WHERE client_request_id IS NOT NULL`）与 `idx_conversations_share_token` 同款走 NULL 停车；另有一条非唯一索引 `idx_agent_observations_scope` 支撑环形淘汰删除与有界读取，不计入 unique surface。`user_profiles.search_profile_json` 不新增 unique surface、外键或 JSON 列登记（与 `ui_mode` 同等对待）。由于 v55/v33 又新增一张只带出向 FK 的叶表，正向 shadow 的不变量变为 82 张业务表、112 个 unique surface（新表声明的 PK 加它唯一那条部分索引）；分支计数闭包上界仍为 12 个 row slot。PostgreSQL v33 为该阶段配对 schema。

SQLite v56 / PostgreSQL v34 增加生效中的 `groups.owner_id` 指针。存量群组从当前管理员中确定性选择 owner（仅当 `created_by` 仍是管理员时优先创建者），不会把已降级或已退出的创建者重新拉回；新群组同时写创建者与 owner。转让在同一群组根事务内把目标成员提升为管理员，并让原 owner 保留管理员；转让完成前，owner 成员行不可降级、移出或自助退出。该列不新增表、索引、外键或 unique surface，因此正向 shadow 仍是 82 张业务表、112 个 unique surface、12 个 row slot；当前配对为 SQLite56/PG34/epoch1。

SQLite v57 / PostgreSQL v35 在群组根行增加可重复使用的邀请 capability：可空的
`invite_token`、`invite_created_at`、`invite_created_by`，以及仅覆盖非空 token 的部分唯一
索引 `idx_groups_invite_token`。token 留在 `groups` 上，使有权管理员能重新打开并复制同一条
生效链接；换新或撤销会原子清除旧权限，删组则随根行一起删除。时间字段只能是 SQL NULL 或
ISO 时刻，绝不能写空串。本迁移不加表、不加外键；正向 shadow 不变量为 82 张业务表、113 个
unique surface、12 个 row slot，当前配对为 SQLite57/PG35/epoch1。

只能在应用/API 与后台 writer 停止后执行：

```bash
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-facts \
  --notebook-id nb-... [--force] [--confirm-service-stopped]
PYTHONPATH=backend python scripts/audit_source_facts.py \
  --db .local/silicon_notebook.db --notebook nb-...
```

全部 notebook 用 `--all-notebooks` 代替 `--notebook-id`。PostgreSQL 必须传 `--confirm-service-stopped`，它只是运维确认，不会自动停服务；审计改用 `--database-url`。两种审计都是事务/连接只读，任一可见来源仍为 missing、running、failed、incomplete 或对账不一致时返回非零。

SQLite v34 新增 `(source_id,id)` 对象 keyset 索引和带来源代次的
`kg_relation_completion_state` 持久水位；v35 增加 `ask_jobs.asked_at`；v36 增加 KG 质量分析的三张预计算产物表；v37
新增 `source_elements` 上 `(source_id, element_type, created_at, id)` 索引；v38
新增部分索引 `idx_sources_visible_identity`；v39
增加命令目录抽取的 `catalog_jobs`（含来源代次列 `source_generation`）／`catalog_candidates`；
PostgreSQL v19 与之对等；v40/v41 的来源事实写入与回填尚不改变检索读取。

Verifier 在 SQLite 只读 snapshot 记录 `Hv`，把规范化事实流式写入 owner-private 临时 spool，释放 SQLite 后才等待 PG checkpoint；随后固定 PostgreSQL `REPEATABLE READ, READ ONLY` snapshot 的 `Ht`，并用第二个 SQLite 事务扫描 `(Hv, Hseen]` 的全部 retained dirty key，只排除这些可证明的 concurrent key。PG retention barrier 一直保留到报告事务提交。Structural 校验覆盖精确 catalog、稳定 key 集与规范化哈希、源/目标外键、unique/cascade 和 storage root 内文件引用；Full 再覆盖选定领域投影、float32 bytes/dimension/norm/抽样 cosine，以及固定中英检索集（recall@12 下降不超过 1 个百分点、top-10 overlap 不低于 0.90、citation/source id 集合完全一致）。Cutover 还会在报告前复核 SQLite 仍 write-frozen，并要求 `Hv=Ht=MAX(seq)`、零 concurrent key、100% coverage 和前一轮完整 full/cutover 报告。持久报告只含安全表名、stable key hash、类别、计数和固定摘要；干净报告只能 supersede 同级或更强等级已覆盖的 drift。

Baseline snapshot 发布要求 owner-only 的真实目录并以 0600 独占创建临时文件。Snapshot/live fence 必须 fresh 打开当前 SQLite 路径，不复用 repository 线程缓存连接，并跨 open/transaction 及 snapshot 发布/PG commit 前复核 resolved path 与 device/inode。COPY 的所有业务 SQL 全限定到 run 绑定 schema，在每个关键绑定处短暂 `BEGIN IMMEDIATE` 复核 live capture 仍启用；JSONB prefix proof 只在 JSON 子树内把有限 int/float/Decimal 统一成精确十进制语义，普通 SQL 数值列仍保持类型差异。Resume 使用有界 named server cursor，长阶段受 statement timeout 与取消轮询约束；起始/最终按 checksummed migration 派生契约完整验证 v32 表、列、约束、operational/GIN index 与 `public.pg_trgm`，逐批仅做轻量控制验证，且最终 81 表 proof/`ANALYZE` 不持有 SQLite 栅栏。

最终 live SQLite fence 是跨 commit 的 lease：只在 PG 双锁/run/table lock 与 81 表长 proof/`ANALYZE` 完成后取得，保持到 PG H0 checkpoint + run progress 事务实际提交成功再释放；PG 失败不落 H0 并释放 SQLite，持 fence 时不得再等待 PG pool/advisory lock 或执行长 proof。

- `frontend/app/page.tsx` 只承担 notebook workspace 编排，不再持有全部共享模型和面板实现。API/视图类型与常量位于 `workspace-model.ts`，答案/引用/推理轨迹位于 `answer-panel.tsx`，内置 KG 类型文案/样式位于 `kg-type-model.ts`，图谱和答案共用 `kg-type-mark.tsx` 渲染。`use-source-library.ts` 已成为来源行/检索范围、分页、详情元素、重解析/删除状态、tombstone 与解析轮询的唯一 owner。壳层只提交既有 notebook/source 成对首屏快照，并消费只读状态、具名命令和窄刷新事件；hook 不接收其他 workspace 领域的 setter。
- `frontend/app/use-ask-session.ts` 已成为 Ask 草稿/对话、意图预检与确认、持久 stream/reconnect、会话历史/tombstone、会话 mutation 和回答反馈的唯一 owner。打开 notebook 时，壳层仍执行既有 notebook/source 成对读取，随后通过显式 hook command 恰好读取一次会话列表、至多一次最新详情。导航只 detach durable job，不自动取消；显式停止在必要时等到 `started`/job id，恰好取消一次后才 abort 本地 transport。同步取消端点没有可强制的整请求数据库期限，因此浏览器只保留一条权威取消请求直到服务端响应，不以客户端 timer 提前释放重试权。意图预检与执行复用同一份冻结 source/base scope。hook 按 exact actor/notebook/workspace owner 接纳可见状态、按 actor/notebook identity 校准持久历史，不暴露 raw setter，也不接管壳层的 Memory answer-link 批次或跨域 callback。
- `frontend/app/use-report-workspace.ts` 已成为报告列表/详情、按需首读、列表/详情互斥轮询、意图/大纲提交、生成/取消/重试、分享、导出选择与删除 tombstone 的唯一 owner。打开 notebook 或非报告页签保持零 report I/O；进入报告页恰好拉一次列表，待确认中心焦点在列表 settle 后至多拉一次详情。导航只 detach 后台任务，不自动取消。exact actor/notebook/view owner 拒绝迟到可见提交，成功删除始终写 actor+notebook tombstone 以保证 A→B→A 收敛；创建冻结 source/base scope，每次写命令重验 live manage 权限。hook 只暴露 readonly view 和具名 command，只依赖 report/pure contract，保持既有请求数与六秒轮询节奏。
- `frontend/app/use-kg-workspace.ts` 已成为 Knowledge 行/类型/筛选/分页/重复项/上下文、Schema view/mutation、统一图搜索/范围/节点、合并审阅/tombstone 及持久 KG build/relink/rebuild 追踪的唯一 owner。打开 notebook 时 Knowledge、Schema、图内容仍保持惰性，只执行既有维护状态恢复探针。exact actor/notebook/generation 拒绝迟到可见提交，actor+notebook identity 让维护认领与合并决定 tombstone 在 A→B→A 后收敛；每条写命令重验 live policy，只读成员零合并审阅恢复/写请求，各类轮询保持既有节奏并单飞。hook 只暴露 readonly view 与具名 command，只依赖 Knowledge/KG/pure contract，保持既有请求数。
- `frontend/app/use-notebook-collection.ts` 已成为 actor-scoped notebook rows、有界集合搜索、筛选/排序/视图/菜单、issued/published 清单水位、访问权对账、editor/delete、默认创建 single-flight 与 notebook 删除 tombstone 的唯一 owner。壳层保留既有 model-status + health + notebook-list + system-config composite bundle，sidecar settle 后才用 opaque hook ticket 提交清单；打开 notebook 仍不新增 collection read。搜索保持 250ms gate 与模块级四条服务端工作上限；actor 替换同步隐藏旧 rows/dialog，分阶段写重验 live row authority，成功删除在派生刷新前写 actor tombstone，A→B→A 与 delete 前 list 都不能复活卡片。hook 只暴露 readonly view、具名 command 与窄 shell effect。
- `frontend/app/use-root-modal-coordinator.ts` 已成为 root dialog presentation lease、typed slot conflict/layer、actor/workspace/source generation、topmost 裁决与安全焦点归还的唯一 owner。它只 import React，不拥有领域 payload、权限、busy、API、repository、timeout、interval 或 poll。异步 opener 在读取前 issue frozen lease，只在 exact owner/issue 仍当前时 publish；workspace transition 与 actor replacement 同步隐藏旧 slot，合法 info overlay 与 primary conflict group 相互独立。协调器本身不增加请求、timer 或 mount-time read。
- `frontend/features/extension-sdk` 是唯一 build-time workspace UI registry/host，canonical slot 只有 `workspace.side_panel` 与 `source.detail_section`。首个 production 条目是既有 Agent Profile 入口：它渲染成来源栏固定区（滚动的来源列表之上）的一行入口，不给工作区加独立一列，样式复用既有按钮类与 `:root` token、不写颜色字面量；插件点击前不做 profile I/O，只经 exact-owner `openUnderstanding` action 委托既有根层 modal/data owner。workspace 成功提交后每个 actor generation 读取一次 `/system/extensions`；集合页、未登录、空 registry 在 request/controller/timer 前短路，同 actor 切库复用投影并同步失效旧入口/action。精确 tuple、实时 availability、核心 `workspaceCapabilities`、normalized UI mode 与当前 actor/notebook/workspace owner 全真才渲染。服务端只投影闭合脱敏元数据；props 只含 readonly 摘要与审过的窄 action。parity 测试保持 non-vacuous。
- 来源详情进入同一 frozen primary issue 水位；来源目录审阅是唯一与其兼容共存的 primary 上层。任何被覆盖的 root dialog（包括来源详情与图谱分析）在重新成为 topmost 前都必须 inert/ARIA-hidden。焦点不使用 timer，只在提交后的 layout 阶段复核预期底层 lease 与 inert 祖先均已收敛后归还。
- 群组管理是集合层独立工作台 `frontend/app/groups-page.tsx`；`.group-page-*` 壳层复用集合页的 token、控件、字体、间距与响应式断点。「共享给群组」仍位于 `frontend/app/notebook-group-share.tsx`，使用紧凑 `.group-*` 行：横向布局属于 `.group-row` 而非内联样式，只读标签使用 `.group-chip` 而非 42px 主按钮 `.new-pill`。`group-layout-guard.test.mjs` 继续守紧凑行，`groups-page.component.test.tsx` 覆盖独立工作台。
- **只读/群组共享库的顶栏身份行**：`ReaderNotebookBadge` 的那一行由 `globals.css` 的 `.reader-badge-row` 排版，**恒不换行**。它此前用 `.tag-row`（`flex-wrap: wrap`），而 `.workspace-header` 是固定 72px 单行——标题 + 徽章 + 一句长说明换成三行、在 72px 里垂直居中，标题那一行就被推到可视区**之上**（静态量过：内容高 141px vs 容器 72px），群组共享库点进去整份库名一个像素都看不见，说明文字还漏到标题栏外面盖住下方内容。标题用 `.reader-badge-title`（`width:auto` 解开 `.notebook-title-input` 的 `width:100%`，`min-width:0` + 省略号，被压缩的是它、不是徽章）；身份标注是**状态**不是主操作，用轻量的 `.reader-badge-chip` 而不是 42px 实心黑主按钮 `.new-pill`（后者摆在 26px 库名旁会把主角盖过去）。身份解释只进 tooltip，不在顶栏占一行——「怎么停止访问」这类指引尤其不该常驻，群组共享本来就没有自助退出。回归门：`frontend/tests/guards/reader-badge-layout-guard.test.mjs`（CSS 侧钉不换行与省略，jsdom 没有排版量不到那 141px）+ `frontend/tests/component/notebook-reader-actions.component.test.tsx`（结构侧钉库名在、用的是 `.reader-badge-row`、行内无 `.tool-hint`）。
- **访问权变动之后必须连当前工作区一起对账**：独立群组页里的退出、移出成员、删组或撤销共享都可能使此前打开的 Notebook 失权。`use-notebook-collection.ts::refreshAfterAccessChange` 独占一次 list read 与 issued/published 水位，再经窄 effect 调壳层唯一的 `reconcileOpenNotebook(remaining)`；「退出只读共享」与群组页的 `onChanged` 共用这条 command。远端撤销没有推送通道，只在标签页重新可见时节流复核；取数失败不执行对账。这是尽力而为不是保证，回归门在 `frontend/tests/guards/group-sharing-guard.test.mjs`。
- workspace HTTP 职责按领域模块拆分。共享 `frontend/app/api-client.ts` transport 负责 HTTP mechanics，领域模块保留 endpoint policy。来源读写由 source-library owner 编排，精确的 user/notebook/workspace generation 会拒绝迟到 UI 提交，同时允许已发出的写请求安全完成；打开 notebook 仍是一次成对 notebook + 首个来源页读取，解析轮询仍保持原 point-read 节奏，且没有引入全局状态库。`frontend/tests/guards/api-boundary.test.mjs` 用语义扫描禁止 transport core 外的生产 `fetch`。
- 结构回归测试只使用 public HTTP contract 或显式 domain seam，不得绑定 private aggregate helper、源码位置、行数或 route/model 总数。FastAPI lifespan/application lifecycle composition 仍是独立债务。

## 验证

运行：

```bash
bash scripts/check.sh
```

验证门禁分为四级：

| 级别 | 范围 | 执行频率 |
| --- | --- | --- |
| G0 目标测试 | 按当前改动文件与行为选跑 | 编辑循环中随时执行 |
| G1 标准门 | `scripts/check.sh`：稳定后端、契约/harness、前端测试及负责类型检查的 production build | 本地交付前以及每次 PR/push/手动 CI |
| G2 扩展门 | `scripts/check_extended.sh`：G1 加真实索引/性能测试、冷图/索引契约与全仓语义扫描 | 每天 `17 18 * * *` UTC（北京时间次日 02:17）一次，也可手动触发 |
| G3 PostgreSQL | `scripts/check_postgres.sh`：直接 PostgreSQL adapter 集成 | 独立的 PR/push/手动 CI job |

G1 并行运行三个有界 lane：`check_backend.sh` 以默认 12 个 worker 执行稳定 backend pytest；`check_contracts.sh` 执行语法/依赖预检、hermetic smoke、契约检查与确定性抽取评分 harness；`check_frontend.sh` 执行递归发现的全部 `*.test.mjs`、全部 `*.component.test.tsx` 与 production build。Node 原生 test runner 和 Vitest 各限制为 4 workers，为 backend 临界路径保留 CPU；Next build 负责 TypeScript 校验并且不得启用 `ignoreBuildErrors`，因此 G1 不再先用 `tsc --noEmit` 解析一遍同一程序再立即由 build 重复解析，`npm run lint` 仍作为 G0 定向命令保留。G1 backend 排除 `slow` 真实索引/性能用例、`graph_index_contract` 冷图/索引契约、`architecture_contract` 全仓语义扫描和 PostgreSQL 树；G2 先执行 G1，再执行精确互补的 backend marker 集。每个 lane 都有独立进程组，因此中断或终止 controller 时，也会终止并回收 pytest、npm 和 Next.js 的后代进程。官方 client MCP smoke 精确锁定已公开的二十二个工具：七个 Memory/context、四个 knowhow、一个引用点查、五个来源管理、三个构建与两个库理解工具。缺少 `frontend/node_modules` 会直接失败，不再静默跳过前端门禁。

验收时使用项目一直采用的 Homebrew/Miniconda Python：

仅对 Codex：完整门禁第一次运行就必须申请沙箱外执行。后端生命周期测试需要绑定 loopback 端口并管理子进程，先在沙箱内运行只会产生无效噪音，不能作为有价值的探测步骤。GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）也必须直接申请沙箱外执行；本地只读 Git 检查仍留在沙箱内。本规则不适用于 Claude Code。

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

G1 标准门并发运行 backend、contracts、frontend 三个 lane。`check_backend.sh` 默认使用 12 个 backend pytest worker，可用 `BACKEND_PYTEST_WORKERS` 覆盖。Apple Silicon warm gate 硬目标是不超过 60 秒；G2 每日扩展门不受该本机时限约束，各 CI lane 时长仅作观察，因此这不是对每一台 CI 机器的可移植超时断言。

测试加速必须保持结果语义：G1 标准门与 G2 扩展门的 marker 表达式精确互补，PostgreSQL 独立负责，任何已提交用例都不能变成不可达；全仓 AST/协议扫描在同一 pytest 进程内只解析每个生产文件一次；缓存容器策略直接验证容器，不搭建无关数据库与 ANN 索引；autouse 隔离路径从 worker 已有的 pytest base temp 派生，不为每条纯测试额外创建 `tmp_path`；普通 SQLite 仓储测试按 worker 只构建一次当前空 schema，再复制成每条测试各自独立的可变数据库文件，迁移/升级/仓储快照模块必须登记 `_REAL_SQLITE_MIGRATION_MODULES` 并执行真实迁移梯；仓储密集测试只可在 pytest autouse fixture 中降低默认密码派生成本，认证 helper 保留生产默认，比较凭据字段的快照模块必须登记 `_REAL_PASSWORD_HASH_MODULES`；生命周期测试只能显式设置私有 `_SCRIPT_TEST_*` 时间控制，未设置时发布脚本仍沿用生产超时与轮询间隔。并发顺序与公平性使用 event/barrier，而非固定 sleep 或线程唤醒顺序；分波次排队时由控制线程运行被测同步编排，在观测到目标容量后用 event 放行，不能让后一波单独落进 cyclic barrier；进程级延迟任务须在共享 teardown 中取消待执行项并等待活跃项收敛，不能只清理由某个局部 repository 对象可见的任务。真实进程生命周期模块使用独立 xdist group。

### GitHub Actions CI

`.github/workflows/ci.yml` 把 G1 暴露为 `CI / level-1-standard`，在目标为
`master` 的 PR、`master` push 与手动触发时运行；
`.github/workflows/daily-extended.yml` 把 G2 暴露为
`Daily Extended Gate / level-2-extended`，只保留每日一个 cron 和手动触发。
两者固定使用 `ubuntu-24.04`、Python 3.13、Node.js 22，从声明的依赖文件安装，
并把测试选择委托给对应 wrapper。G3 保持为
`CI / level-3-postgres-integration`。

`CI / level-1-frontend-node26` 在 Node.js **当前**大版本上重跑前端泳道与生产构建，
触发条件与 G1 相同。文档承诺的是「Node.js ≥ 20」而 G1 钉 22，没有这条泳道，承诺的
上半段就无人验证：Node ≥ 24 自带 Web Storage 全局，不给 `--localstorage-file` 时它们
的 getter 返回 `undefined`，而 vitest 的 jsdom 环境会让它们盖住 jsdom 自己那份——凡是
读 `localStorage` 的组件测试都在开发者本机整片红、CI 却全绿。
`frontend/test-support/setup.ts` 只在内建 storage 取值为 `undefined` 时补回真正的 jsdom
storage，并连 `Storage` 类一起补（否则 `vi.spyOn(Storage.prototype, …)` 会静默打空），
Node 22 上行为逐字不变。该泳道同时跑生产构建——那次修复的第一版误引了没有类型声明的
`jsdom`，正是被构建的类型检查当场抓到的。

已提交的 OpenAPI 契约是字节语义冻结契约，因此
`backend/requirements.txt` 精确固定 FastAPI `0.135.3` 与 Pydantic
`2.12.4`。只能在有意重生 OpenAPI 契约并在干净环境跑 G2 扩展门时，
才同步升级这两个框架。

该 workflow 只有读权限，不接收模型或部署 secrets，并把后端 pytest worker
限制为 4，避免 GitHub 托管 runner 过度抢占。后端安装设置
`HNSWLIB_NO_NATIVE=1` 并禁用 pip wheel cache：`hnswlib` 默认会用
`-march=native` 编译，把这种本机 wheel 缓存后恢复到 CPU 特性不同的托管
runner，可能以 `SIGILL` 崩溃。CI 使用可移植构建，以少量 ANN 性能换取确定性；
生产 wheelhouse 仍可按已声明的部署 CPU 定向构建。20 分钟 timeout 包含依赖安装，
与 Apple Silicon 本地 warm gate 的 60 秒内目标刻意分开。初次接入时
`CI / level-1-standard` 仅用于观察；只有在 PR 与合并后的 `master` 都稳定绿跑后，
并由用户明确批准分支保护变更，才把它设为 `master` 的 required check。

PostgreSQL 覆盖与离线门禁明确分离。`level-3-postgres-integration` job 启动 PostgreSQL 16，
创建最小权限与辅助 encoding/locale 目标，并通过 `bash scripts/check_postgres.sh` 只运行
`postgres_integration` marker。本地使用已安装的 PostgreSQL 16 和显式 `TEST_POSTGRES_URL`；
`scripts/check.sh` 不得启动或连接 PostgreSQL。
该泳道只覆盖直接 PostgreSQL 行为；已退役的 SQLite 后端实现专项测试、
SQLite→PostgreSQL 导入/正向 shadow 测试与跨后端 parity 测试不属于当前覆盖。

CI 可移植性属于门禁契约：所有由 CI 执行的测试使用的文件系统、数据和依赖
路径都必须相对仓库，并且独立于进程 cwd。已提交 fixture 必须从其仓库文件位置
定位，禁止依赖开发机 checkout 绝对路径或 `HOME`，测试也不得读取仓库外源文档。
测试启动时直接导入的第三方包必须声明在 `backend/requirements.txt`；干净 hosted
runner 必须从该文件和 `frontend/package-lock.json` 安装，并且只凭这些声明即可
全绿。各 lane 时长继续输出供观察，60 秒内目标只约束已验证的 Apple Silicon
Homebrew warm gate。

依赖仓库外 PDF 解析产物的 gold 生成、构建与校验脚本仍属于 developer-only
工具并保持在 `scripts/check.sh` 之外；该例外绝不适用于已提交测试。

## 开发流程

凡任务会写入仓库代码、测试、文档或配置，都必须在第一次写入前新建 linked git worktree 和分支，并在其中完成开发、验证及后续 PR；该任务期间本地主 checkout 保持只读，小修也不例外。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。纯调研、设计、状态汇报和只读审查不要求 worktree。

对于已经批准的多步骤实施计划，默认采用 subagent-driven development：每个任务交给一个全新的实现子 Agent，并在进入下一任务前完成该任务范围内的规格符合性与代码质量审查。纯调研、设计、状态汇报和只读审查不要求创建 worktree 或使用子 Agent。

`CLAUDE.md` 是 Claude Code 在本仓库的操作规范：Claude Code 只自动加载 `CLAUDE.md` 与 `.claude/rules/`，不会加载 `AGENTS.md`，因此该文件内联了必须随时在线的红线，并给出 `AGENTS.md` 的章节索引；两者冲突时以 `AGENTS.md` 为准，刻意的例外由 `CLAUDE.md` 穷举列出。也正因为 Claude Code 读的是它而不是 `AGENTS.md`，`CLAUDE.md` 属于四份文档同步集合的一员。其中最硬的一条是**起子代理必须显式选模型，不得默认继承主 Agent**，按任务需要的判断力分层——需要判断力（写计划、评审、架构取舍、疑难归因）用 `opus`，规格已定死的转录型实现用 `sonnet`，纯检索定位用 `haiku`。这条由 PreToolUse 硬门 `.claude/hooks/require-subagent-model.py` 强制：没显式传 `model`、且 `subagent_type` 未在 `.claude/agents/` 中钉好模型的调用会被拒绝。`.claude/agents/` 已提供三个钉好模型的角色：`impl-task`（sonnet）、`spec-review`（opus）、`code-quality-review`（opus）。`backend/tests/test_claude_subagent_model_hook.py` 是这个 hook 的回归网：以子进程方式跑真实脚本，两个方向都覆盖——既盖「绕过」（让继承模型的调用溜过去），也盖「误拦」（把合法调用堵死，逼人绕开守卫）。

PR 在合入前必须经过 codex 评审，且**每一轮的原始输出都要逐字贴回 PR**——零意见的轮次要贴，手动补跑的轮次也要贴，并附上触发方式、完整命令、head SHA、退出码与输出字节数，便于核对评审确实跑过、结论没被转述失真。判一轮成功要**退出码为 0 且输出非空**两个条件：codex 被 SIGTERM 杀掉时退出码同样是 0，只看退出码会贴出一条空评论、看起来像通过。P0/P1 阻塞：核实后把站得住的意见修掉并重审，直到判定转为非阻塞——只有意见站不住（走下面的驳回规则）或修复方向需要人拍板时才停下来交人决定；P2/P3 不阻塞、可如实说明后不改；优先级标签解析不出来时保守拦人而不是默认放行。评审意见可以在核实后驳回（codex 评的是 diff，未必了解运行时事实），但驳回要同时给出 PR 上的理由与证据、代码里记录取舍的注释，以及钉住既有行为的回归用例。合入不再逐次征求同意：评审非阻塞**且** CI 全绿时直接 `--rebase` 合。评审仍阻塞或输出解析不出等级时一律不合——先修掉并重审；CI 未全绿、或用户说过等他自己合，同样不合。CI 判绿只认 `gh pr checks` 全部 `pass`——`mergeStateStatus: CLEAN` 只说没有东西拦着合并，不等于检查跑绿了。合入前还必须在 PR 上确认**针对 PR 远端 head（`headRefOid`）的评审已经贴出**（不能用本地 `git rev-parse HEAD`：本地落后时会命中一条旧评审而放行，而合入的是远端那个未经评审的 head）：评审自动化静默没触发，和它跑完判了通过，在外部看起来一模一样；agent 的汇报和 hook 的本地状态都不是证据，PR 上的那条评论才是。评审的自动化本身是开发者本机的 Claude Code hook、不是仓库产物，新 clone 上没有它——规则依然成立，那就手动跑；机制细节见 `CLAUDE.md`。

### 测试架构

- 与规模无关的边界分支只允许降低测试局部阈值，并另行钉住生产 floor。检查同一不可变索引/产物多个视图的断言共享一次真实构建；只验证算术或观测分支的用例走最小归属接缝，同时邻近集成覆盖仍须真实构建、打开并查询该产物。
- 后端与前端静态契约使用模块路径、限定 scope、操作种类、目标和审核后的计数等语义身份。源码位置只能作为诊断元数据；行号、offset、CSS 顺序和源码切片都不得用来标识预期站点。
- 前端测试不得再与生产代码混放：`frontend/tests/unit` 放 `node:test` 纯逻辑用例，`frontend/tests/guards` 放架构/安全/词汇/入口契约，`frontend/tests/component` 放 Vitest/jsdom/Testing Library 行为用例。共享 setup 和语义源码适配器位于 `frontend/test-support`；runner 递归收集这些目录，位置守卫会拒绝 `frontend/app` 或 `frontend/features` 中的测试。
- 组件行为不得由 CSS 几何或源码布局钉死。普通特性重构只有在可观察契约改变时才应修改测试。
- 已提交测试不得使用 skip/xfail/todo/only 禁用；repository policy 会同时检查测试入口及其 helper 模块，并禁止绕过共享 semantic-source 适配器直接读取生产源码。
- 前端源码策略必须保持有界：通过语法规则拒绝 AST 位置/集合顺序 API，以及源码语义命名值上的文本位置操作；共享 `semantic-source.mjs` 只能暴露 AST 语义，不能把文本切片、分行、下标或长度当作契约。不要为此实现整套 JavaScript 数据流解释器，普通数组操作仍然合法。
- backend 测试会在 xdist worker 启动前，由主进程预热一份仓库本地 Matplotlib 字体缓存。必须保留这个 controller 边界，不能让每个图谱 worker 各自重复枚举 macOS 字体。

## 文档维护

后续只要产品行为、启动方式、架构或开发约束发生变化，需要同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`
- `CLAUDE.md`

根 README 保持精简；同时更新 `docs/` 下负责该主题的中英文权威文档：`product-and-api`、`deployment-and-configuration`、`operations` 或 `development`。

SQLite source open 的分类只在 `open_fresh_live_sqlite` 调用边界生效：非瞬态 `sqlite3.OperationalError` 归为 source-binding identity；locked、busy、interrupted open 仍按瞬态整批重试，后续 SQLite operational error 保持原 schema/query 分类。
- `SelectedSourceGraphActivationService` 仍是唯一的所选来源图激活算法，但 Ask/深度报告只能经共享 host 的内建 contributor 与 core-private 请求 bridge 到达它。调用方必须先完成并冻结历史 `B` 再调用 host；服务只读取服务端冻结、真正收窄的 `include` scope，构建有界 snapshot，依次尝试在线 scoped PPR/邻居 membership，并在必要时读取按来源 partition 伴生产物，最后复验每个返回 source id，再把 `G` 交给 `BaselineProtectedEnrichmentService`。全范围/全选在 snapshot I/O 前直接返回；默认不可见 shadow 返回 `B`，质量批准的 active 模式返回 `B + G`，任何失败都返回 `B`。状态对象只属于内部观测，不得进入 Ask/报告 payload、轨迹、stream 或 UI；禁止新增第二套 rollout parser、workflow 级 service 直调、直接图 consumer 或客户端 narrowed 判据。
