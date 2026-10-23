# 开发与仓库契约

[返回 README](../README_zh.md) · [English](./development.md)

本文拥有开发护栏、schema/迁移编写、验证、工作流与文档维护规则。
[AGENTS.md](../AGENTS.md) 是精简的 Agent 入口；运行时架构由
[architecture.md](../architecture.md) 拥有。

## 数值上限与截断

生产代码不得在调用点隐藏会改变结果的数字切片或上限。不可调的 wire/storage
边界复用具名协议常量；可在质量/成本之间调整的预算放入带校验的 `Settings`。
用户编辑的列表超过前后端共用护栏时必须明确校验/拒绝，不得静默切片。embedding
等模型输入截断必须在线、批处理与回填路径共用同一配置真源。测试 fixture
中的显式数字不在本规则范围内。

## 架构边界

运行时所有权与数据流以 [architecture.md](../architecture.md) 为准；修改对应功能前，
阅读其中的 [Repository 组合](../architecture.md#22-repository-组合与兼容-facade)、
[前端边界](../architecture.md#24-前端边界) 与 [核心数据流](../architecture.md#3-核心数据流)。
公开行为和精确数值上限由 [产品/API](./product-and-api_zh.md) 拥有，操作步骤由
[运维文档](./operations_zh.md) 拥有。以下只保留贡献者必须遵守的约束，不再重复实现历史。

### 依赖、授权与状态所有权

- 保持 `factory/wrapper → facade → runtime → services → stores`。store 独占业务 SQL
  与原始行选择；application/query 组件可以组装领域投影。service 不分支判断 dialect，
  不导入另一 adapter。稳定跨层值放在 `backend/app/domain`，repository ports 不依赖
  services，静态依赖图无环。backend factory 与两个后端 wrapper 都在
  `backend/app/services`（`repository_factory.py`、`sqlite_repository.py`、
  `postgres_repository.py`）：services 可以依赖 repositories，反过来不行；剩下的
  repositories→services 边（filesystem scale artifact store 对 `services.kg` 的 import）
  是由 `repository_service_import_ceiling` 钉住的债务。领域 router 拥有端点实现，`app/api/routes.py` 只做组合；
  兼容 facade 重导出原对象，不新增第二套实现。
- extension SDK 保持依赖轻量。只有 `app.bootstrap` 连接 adapter 与
  `backend/app/extensions`；workflow 消费领域 host port，plugin 不导入具体 repository、
  facade 或 runtime。availability probe 不做 I/O，capability port 保持窄投影，
  某 contribution 不可用不能关闭其他独立贡献。保留启动冻结的路由/准入、无贡献时原样
  返回 baseline、有界批量 hydration、取消传播与不得持连接执行的边界。
  扩展编写和 UI 包约束见 [部署扩展 SOP](./deployment-extensions-sop_zh.md)。
  模块化扩展 PR 仍需两次独立 subagent review，并遵守 [评审与 CI 流程](#开发流程)。
- `backend/app/application` 的 application stage envelope 保持不可变、依赖轻量。
  有意扩展显式 import allowlist，不放开会绑定 `app` 的裸根 import。Ask/Report stage
  接缝保持精确 source scope、retrieval run、actor、取消 token 和 connection probe
  的身份；只检索的 Ask（`output="evidence"`）在提交边界被拒绝保存：不写回答也不写会话。在既有边界重验授权，违反契约直接失败。stage wrapper 不持数据库连接或外层
  leaf-I/O 槽位；core 拥有最终审计与落库。终态后 observer 不得改写已提交产物或
  逆转 `done`；`report.completed_observer` 另不得启动检索/模型工作。修改 Ask 完成
  路径时保留其既有 observer 顺序、失败隔离与成本。
- G1 的 `scripts/check_architecture_boundaries.py` 守住
  `scripts/architecture_boundary_baseline.json`：repositories→services 债务只减不增，
  facade 公开面、core/models→services import、列出的热点函数长度、repository
  Protocol 方法数均无预留空间；缩小时同批下调 baseline。只在运行时按名字取用的 facade
  属性不存在时，同一脚本也会报错：包括 `repository_facade.py` 里的
  `_repository_from_weakref(...)` 接缝，以及 `backend/app`、`scripts` 中对 facade 形状
  接收者用字面量做的 `getattr`/`hasattr` 探测。这类调用方不会报错而是静默降级，普查脚本
  也看不到它们。facade 被当作协议交出去时（`cast(P, repository())`，或标注 `-> P` 并
  返回 `repository()`），协议声明了 facade 没有的方法也会报错。检查上线前已有的缺口曾登记在
  `facade_protocol_binding_gaps`（同样无预留空间），现已全部补上，名单为空，任何缺口都报错。退役 facade 成员按
  [调用方台账](./superpowers/plans/2026-08-23-facade-retirement-ledger.md) 逐批进行，
  用 `scripts/audit_facade_callers.py` 复现，并同步 ownership/surface fixture：
  `scripts/generate_repository_contract_fixtures.py --rebaseline-surface`。
- facade 普查只看 facade。组件层由 `scripts/audit_component_callers.py --check`（contracts
  泳道）做同口径普查：`backend/app` 下 `api/` 与 facade 层之外每个类的方法与属性（服务、两种
  adapter 的 store、ports、扩展宿主），只要生产代码不以任何写法引用它的名字（经另一个同类
  成员转手也不算），就报红，除非 `scripts/component_test_only_allowlist.json` 把它登记为
  测试观察点（`def` 上注明「测试观察点」）、扩展端口、扫描看不到的运行时引用或待拍板项；
  登记了却已不是候选的条目同样报红。不要新增只给测试调用的成员：改测试走生产路径，或者
  带理由登记。已知两个盲区：普查按名字而不按接收者判定，只被测试调用的成员若与某个生产在用的
  名字同名（`neighbors`、`snapshot` 等），永远不会被报出；登记成员的方法体按生产调用计，
  所以登记的观察点调用到的成员会随它一起保持存活。
- 两种 adapter 的 `access_sql.py`、`mount_sql.py` 与 `memory_sql.py` 判据同步维护；
  `memory_sql.py` 是「谁能读 Memory 派生来源」与「哪些 KG 行算派生自 Memory」的共享
  定义点，新读者一律 import（`source_store.hidden_source_ids` 与来源读取闸
  `sharing_store.source_notebook_id(viewer_id=...)` 已经如此）；早于它的读者
  （`postgres/chunk_store.py` 里的 Memory 属主判断）仍自带手写谓词，在改动它们的变更里
  迁入。
- `mount_sql.py` 的参与集**随查看者**（M3）：解析挂载的 store 方法（`participant_*`、
  `resolve_participants`、`notebook_has_usable_base_kg`、`mounted_bases_row`、
  `mounted_base_ids`、`any_mounted_has_kg*`、`follow_start_row`）都收必填 `viewer_id`。
  服务层传 `retrieval_run.current_viewer_id()`（运行的 actor，否则请求用户，否则 `''`，
  只剩公共库与 `everyone`），或在 `backend/tests/test_mount_viewer_guard.py` 登记过的
  调用点显式传查看者；一律不许写字面量。按参与集建的进程级缓存以查看者的有效集合为键
  （`graph_retrieval._participant_graph_cache_key`；查看者指纹用独立的 BLAKE2s
  personalisation，与参与集覆盖的指纹分开；`vector_cache.quota_bucket` 剥掉 16 位十六进制
  指纹段，各变体都计入所属族的配额）。
- 检索范围每类入口只有一个安装点：所有问答入口经 `AskService._retrieval_ceiling`
  （`source_scope.default_ceiling_context`，读取器来自 `RepositoryRuntime.ceiling_readers()`），
  报告各阶段经 `ReportExecutionCoordinator._default_ceiling` / `ReportEngine._refreshed_ceiling`，
  全局问答经 `global_run.global_ask_run`；`backend/tests/test_default_ceiling_guard.py` 以相等断言
  钉住，路由、MCP 工具或回答引擎自己进入 `source_scope_context` 即红。两个构造器与全局运行都置
  `ceilings_total`：没有逐库条目的库不参与任何通道。读取 scope 自身笔记本的新 producer 必须从
  `scoped_allowed_source_ids` 取来源清单（本次运行的 `ceiling_binds` 判定证明排除不了任何东西时它返回
  `None`），并用 `verify_unbound_read` 核验不带清单的读取。
- 新的 notebook 写
  端点走 `require_notebook_capability(...)`；在 body 中才解析身份的路径也使用同一
  capability 表及独立 mirror-write fence。先授权再返回镜像写错误，保留已登记的
  报告创建者私有权限例外。不得根据空/null principal id 猜 grant 种类或绕过实时读权；
  读判据扩展时同步更新 Memory 授权锁位 allowlist。access-SQL 与
  notebook-capability 守卫负责验证。
- 由成员记忆派生的知识图谱行只属于该成员（裁决 M1）；下列读者、构建与守卫要同步维护：
  - 建图读者一律使用 `memory_derived_in_notebook`（绑定笔记本的判据），并把每个按 id 引用的
    对象探针绑定到引用行所在的笔记本；不绑定的 `memory_derived_object` 会让 PostgreSQL 把
    全站的 Memory 来源都拿去建哈希。
  - KG 读者的 `viewer_id` 只有一种含义（`memory_sql.memory_viewer_filter`）：`None` 返回全部行；
    `""` 不返回任何记忆派生行；用户 id 不返回其他成员记忆派生的行。对行读者，`None` 时语句
    文本不变；对计数读者，`None` 时结果不变且零现读。探针一律绑定到行所在的笔记本
    （`foreign_memory_in_notebook_excluded`、`memory_derived_in_notebook`）。类型计数缓存按
    `(kg_reset_epoch, kg_mutation_seq)` 保存同一条语句算出的两半：共享半与全体成员的记忆半
    （总数减去由记忆来源驱动的计数）。`None` 取两半之和，`""` 读共享半，用户 id 再现读一次
    本人的那一半（`own_memory_source`）。由记忆来源驱动的读取，在 PostgreSQL 上用
    `= ANY(ARRAY(子查询))` 的 InitPlan 绑定来源 id；在 SQLite 上用 `CROSS JOIN` 固定连接顺序
    或用不相关的 `IN` / `NOT IN` 列表，并写出 `hidden_type_index_term`。
  - `memory_sql` 的五个片段及其消费者（未注明的两个后端都有）：`memory_derived_in_notebook`
    （无绑定参数）——共享工具 `duplicate_member_rows`、
    `edge_centrality_source_rows`，`memory_viewer_filter` 的 `""` 支，以及建图读者；
    `foreign_memory_in_notebook_excluded`（一个参数）——只经 `memory_viewer_filter` 使用；
    `memory_viewer_filter`（三值）——两个 `knowledge_store` 的行读者、PostgreSQL `search.py` 的
    `_candidate_rows_for_terms` 与 `notebook_knowledge_rows`（PostgreSQL
    `query_store.search_notebook` 经后者间接用到）、SQLite `query_store.search_notebook`；
    `own_memory_source`（一个参数）——两个 `knowledge_counts_cache.memory_type_status_counts`、
    两个 `knowledge_store` 的 `own_memory_only` 叠加读、两个 `query_store.notebook_has_kg` 的
    本人 EXISTS；`hidden_type_index_term`（无绑定参数，SQLite 规划器提示，PostgreSQL 只为两个
    后端同名）——SQLite 的两半计数语句与本人半、`knowledge_store` 的叠加读与
    `_fts_viewer_filter`、`query_store.notebook_has_kg` 的本人 EXISTS。原有片段也新增了消费者：
    SQLite `knowledge_store._fts_viewer_filter` 用 `memory_source_readable` 与
    `memory_source_type_predicate`；两个 `knowledge_counts_cache` 用
    `memory_source_type_predicate`（两半语句的记忆 CTE）。
  - `kg_viewer_scope.KgViewerScope` 是 `memory_sql` 查看者片段的 Python 孪生（对象或关系只由
    自身来源决定是否隐藏，每个证据条目也只按自身来源判断），由 `test_kg_viewer_scope_twins*`
    在两个后端钉住；服务层只通过 `store_viewer_kwargs` 向 KG store 读取传 `viewer_id`，也就是
    仅当笔记本里有查看者不能读的记忆时才传。`test_kg_viewer_scope_assembly.py` 的装配绊线核对
    开关（`STORE_READERS_TAKE_VIEWER_ID`）与 store 接受的关键字一致，并核对隔离标记读取已接好。
  - 没有记忆来源的笔记本，其 scale 与 viz 工件与隔离前逐字节相同，差别只在三份 manifest：主索引
    和 viz 的 manifest 多出 `memory_isolation`、`memory_sources_digest`、`shared_content_digest`
    三个字段；所有 version 列表（主索引、viz、来源分区伴随工件的 `parent_version`）都多一对
    (`memory_isolation`, 1)，这一对现在每个笔记本的 version 都带。构建发出的语句也相同，只有
    三点不同：每条对象和关系读取都在自身语句里排除记忆（hashed `NOT IN` 子查询；PostgreSQL 上
    实测与不过滤等价，SQLite 上整表读取多 13–30%）；多出记忆来源读取，以及由（为空的）记忆
    来源集合驱动的记忆对象 / 关系 / 簇 id 读取；viz 派生改读三列精简投影，不再读整行。没有
    任何语句由先行的记忆探针来挑选：按语句隔离时，探针会被两条语句之间提交的第一条记忆作废，
    所以来源清单（水位与 fold 增量）在语句内无条件排除记忆，没有记忆的库上返回的行与执行计划
    节点与不过滤的读取一致（两端计划钉子）。version 信号不探测记忆。ANN 喂数和成员关系腿用
    预读的记忆 id 集合过滤；构建持续累计观察到的记忆行，只有新出现的行确实进入了产物时才重跑，
    再次命中则丢弃。fold 在读取前后各观察一次记忆，凡是它将发布的节点、ANN 标签、关系标签或
    增量来源中有记忆，就什么也不发布，记一条 `scale_fold_refused`（`memory_reached_fold`）
    并改走全量构建。
  - `backend/tests/test_memory_chunk_write_guard.py` 扫描 `backend/app`、`scripts/` 与
    `examples/`（也扫描其中 `migrations` 目录以外的 `.sql` 文件）：每处写 `chunks` 的地方都必须
    在写入前拒绝 Memory 来源，或带经检查的理由登记；`sources.source_type` 只由 INSERT 写入。
  - `backend/tests/test_memory_reader_guard.py` 让读者清单保持封闭：`backend/app/repositories`
    下凡是按笔记本读取 `knowledge_objects`、`knowledge_relations` 或 `concept_clusters` 的函数或
    SQL 常量，都要按名字登记到一个类别（带查看者的读者、本人记忆读、排除全部记忆的共享工具或
    建图读者、计数缓存、按键 / 天花板 / 调用方限定的读、派生层、状态探针、记忆一侧、拷贝、维护）。
    未登记的读者报出 `文件:行` 并失败，已登记却不再读取的名字同样失败；查看者、本人记忆、排除与
    计数缓存四类按代码核对。
- participant 集合替换只由 `retrieval_participants.py` 在检索消费边界提供，
  `global_run.py` 是唯一安装者。授权仍走真实 mount/read 判据，`source_scope.py`
  只能收窄已证明的集合。保留精确 reader/writer allowlist、actor/run 绑定、无内容
  cache fingerprint 和 `ParticipantOverrideError` 传播。新增可读该上下文的
  fail-soft handler 必须登记到 `_SEAT_FAILSOFT_SITES`；`_bounded_participants`
  必须在这些 handler 外先验证。模式问题 `subjectless_run_active` 与过滤问题
  `peer_scope_ceiling_active` 分开判断。安全边界由
  `test_participant_override_guard.py` 守住。无主体 run 里枚举条目的引用按本次冻结的
  参与库集合复核，该集合由全局问答管理器按同一读权限判据授权。
- 枚举与目录入口（`enumerate_elements`、`enumerate_kg_objects`、`enumerate_sources`、
  `resolve_source_title`、`collection_map`、`collection_map_text`）的每个调用方都显式
  传入 `ceiling_binds`，取值为本次 run 的判词 `reasoning_retrieval.ceiling_binds_for_run`；
  默认值会过度过滤。问答、推理与目录概览侧的调用方由
  `test_every_collection_entry_call_passes_the_ceiling_verdict` 钉住。知识重读用的
  孪生判词 `source_scope.ceiling_binds` 多一条分支（库里有别人的 Memory）；清单对
  私有 Memory 是无条件剔除的，所以不需要它。两个判词的其余分支要保持一致。两者都只有
  配合读后核验才可以按 run 记住：走不绑天花板快路径的集合读取，要拿手里的来源行
  （signal 行、按绑定路径判据逐条核对的 KG 行、Knowhow 目录里各表的投影源）对冻结天花板
  核对，出现天花板外的来源就记下漂移（`source_scope.record_collection_ceiling_drift`），
  这次读取与之后的每次读取都改为绑定。
- 可能不带来源清单读取的检索腿——chunk 各 lane 与贡献水合、精确查找、mix 漫游及其叠加块
  后盾、弱支撑提示——在读取**之前**只读一次本次运行的判词（`source_scope.run_ceiling_binds`）：
  `unbound_ceiling(nb)` 返回读后用来核验的冻结天花板（`verify_unbound_read`），或返回 `None`
  （约束：取清单）——绝不能一次读出「不带清单」、另一次再去取天花板。绑定天花板的 store 拿到的是
  本次运行未经复制的 `CeilingSet`（`bindable_library_ceiling`），并在规范化 id 之前读取它的
  `bound_forms` 备忘（键含后端与 SQL 形态）。
- 回答上下文的关系行不绑定来源清单：被来源范围覆盖的库读关系行时带上 `source_id`
  （每条边每个来源一行），在 Python 里按本次运行冻结的范围判定——范围起约束时过滤并计数，
  不起约束时读后核验（发现越界行即记为漂移）。因此每个带冻结 include 范围的 HTTP 提问都要付
  这次读取（4.9 万来源库实测 +0.3–9 ms，随每条边的来源数增长）。更省的核验方式（用这些行的
  `MAX(created_at)` 与冻结时刻比较）需要范围对象带上冻结时间戳，目前未实现。
- 所有提问人共用的按库检索缓存（关键词词项、关系向量矩阵）只由非 Memory 派生内容构建，
  所以谁先构建都不改变缓存内容，缓存键也只属于笔记本本身。Memory 派生关系放在同一个缓存
  条目里另行存放，带上来源，每次按提问人的天花板掩码；证据被天花板裁剪过的对象现场分词。
  冷构建时按每 900 个 Memory 来源一次 `relation_delta_rows(with_source_id=True)` 读取，
  得到 Memory 关系到来源的映射（2 万条共享关系旁有 1,000 个 Memory 来源：与逐来源读取相比，
  PostgreSQL 0.90 s → 0.70 s，SQLite 6.1 s → 3.7 s）。
- 可变运行态归 `RepositoryRuntime`，`REPORT_CANCELLATIONS` 是刻意保留的进程全局
  例外，与 coordinator 和兼容函数共享同一身份。领域 builder 只接较早的 frozen
  bundle，不接 runtime 本身；保留窄迟绑定 accessor 与启动副作用顺序。组合后受支持
  的替换必须同步到所有既有消费者。
- KG edge 定义只放在 `domain/kg/edge_schema.py`，service shim 不定义新 `EdgeSpec`。
  production 不导入 `app.eval`，不从分数重建 provenance，不新增第二套 selected-source
  rollout parser/activation 路径。新增检索贡献保持冻结 baseline，先授权再 hydration；
  工件与来源范围身份由服务端拥有。
- 停服维护使用 `open_maintenance_cli_repository`，先做 factory 前安全检查，再取得
  独立 session 锁；模型调用前释放页读连接，最终始终关闭 repository。在线 scale CLI
  使用独立组合根，强制 `migrate=False, seed=False`，组合前验证 schema，并持
  per-notebook claim 到发布完成。全部工件根及回滚/退役复用 staging/swap 与 claim
  检查，不得原地覆盖活目录。操作步骤见 [运维文档](./operations_zh.md)。
- 所有非服务进程入口都是 schema guest：经 `app.services.maintenance_cli` 打开库
  （`open_cli_repository`、`open_maintenance_cli_repository`、`create_schema_guest_repository`，
  从未带插件宿主的工具用 `create_bare_schema_guest_repository`），不得用带 seed 默认值的
  `create_application_repository(settings)` / `SQLiteRepository(settings)`。版本闸
  （`app.repositories.schema_currency`）与唯一一条拒绝文案共用；拒绝经
  `MaintenanceCliError.exit_code` 以退出码 3 退出。只有服务启动与 `database migrate` 拥有
  schema。`backend/tests/test_schema_guest_guard.py` 让服务进程、登记过的 guest 入口与 schema
  所有者之外的任何构造报红——自己写 guest 关键字也算，因为那会跳过版本闸；确实拥有临时库的
  工具在那里登记理由。连不上库时只输出那条不含凭据的固定文案（退出码 2）。

### 前端实现护栏

- workspace hook 独占领域状态，壳层只消费 readonly view 与具名 command，不传递其他
  领域 setter。保留精确 actor/notebook/generation 所有权、迟到响应拒绝、删除 tombstone、
  single-flight 与既有请求预算。导航只脱离持久任务，显式 Stop 才取消。新增 notebook
  owner 登记在 `notebookTransitionSteps`，统一走 `notebook-transition.ts` 的
  begin/commit/settle 路径，root-modal 清理仍在首位。
- `api-client.ts` 拥有 HTTP mechanics，领域 API 模块拥有 endpoint policy；生产
  `fetch` 只能在共享 transport。根弹窗使用 `use-root-modal-coordinator.ts` lease：
  异步前 issue，仅向当前 owner publish；被覆盖的弹窗 inert/ARIA-hidden，提交后确认
  底层 lease 仍有效才归还焦点。关闭弹窗不能释放动作仍在飞的互斥。
- 复用来源/图谱 renderer 和 readonly props，不把领域状态搬进呈现组件。UI extension
  declaration 只投影元数据，在启动/构建期冻结，并受精确 tuple、capability、UI mode
  与 owner 门控。新增贡献后运行 `scripts/generate_ui_extension_contract.py` 更新
  `backend/tests/fixtures/ui_extension_contract.json`，通过 `--check` 对账。
  内建 registry 的 import 闭包保持 `.ts` 以兼容 Node 测试。本地 UI 包遵守
  [扩展 SOP](./deployment-extensions-sop_zh.md) 及独立部署验收门，不放宽基础仓库
  的零插件 registry 断言。
- `KnowhowPanel`（`knowhow-panel.tsx`）持有面板的顶层状态机（选中的表与行、哪个弹窗
  打开、表详情、引用跳转、表级写命令、`handleCellSave`）和全部挂载点。不共享的领域
  状态在三个 owner hook 里：`use-knowhow-table-list.ts`、`use-knowhow-row-code.ts`、
  `use-knowhow-concept-matrix.ts`。列表、网格、行抽屉、徽章是呈现模块，自带状态机的
  弹窗各有模块，样式在 `knowhow-panel-styles.tsx`，作为 section 的最后一个子元素渲染
  一次。每个 owner hook 调用一次、绑定到一个标识符，只经属性访问使用：不解构、不摊平、
  不放进依赖数组、不整体下传，其 `view` 同理，因为返回对象和 `view` 每次渲染都是新的。调用位置就是 effect 顺序，
  属于行为：表列表 hook 最先，行代码 hook 在拉表详情与 notebook 复位两个 effect 之间，
  概念矩阵 hook 是最后一个 hook 调用；notebook 复位必须先于引用跳转。搬动代码不得改变
  任何回调的 `useCallback` 状态或依赖数组，例如 `handleCodeSaved` 保持每次渲染新建的
  普通函数。`owner-view-no-reflatten-guard` 与 `knowhow-panel-boundary-guard` 钉住这些
  约定。`knowhow-panel-golden` 里的请求序列金标永久保留；提交次数与样式哈希只在拆分期
  使用。
- 修改群组/Schema 面板时保留既有样式与行为守卫，不复制 CSS 或使用未定义类名。
  Schema 选择/草稿身份是 `(object_type, proposal-status)`，写入回调只向发起时的
  pane 提交可见结果，`SchemaWriteOutcome` 的 `confirmed`、`unconfirmed` 与
  `failed` 三值不可合并；已提交但未确认的写入不得提示为失败并要求重试。
- `globals.css` 只保留一条元素级按下基线：
  `button:not(:disabled):not([aria-disabled="true"]):active`，使用 `opacity: .7` 与
  `filter: brightness(.88)`，不加 `transform`、`translate`、`scale`、`rotate`
  或基线 `transition`，避免几何变化吞掉边缘点击或覆盖定位。动作结果落在所按控件
  本身或紧邻处，页面横幅不能单独充当反馈；长任务在飞时另需禁用或替换控件。
- 复制结果复用 `copy-result.ts` 的 `useCopyResult`，按所复制 token/item 的身份分格，
  身份变化时 reset，由共享 timer 恢复 idle。JSX 保留字面量结果类，
  `button.copy-result-copied` / `button.copy-result-failed` 同时声明 background
  与 hover 规则。失败时仅在 `input.value === link` 仍成立后选中紧邻只读输入框。
  `button-press-feedback-guard`、`long-task-button-guard`、
  `command-catalog-button-guard` 与复制结果组件测试共同守护。
- Secure Context 限定 API 必须有共享退路：前端 id 走 `client-request-id.ts` 的
  `newClientRequestId()`，复制走 `copy-text.ts` 的 `copyTextSafely()`。
  发请求前的提交准备也要放进错误边界，使同步异常能还原草稿并释放忙碌状态。
  `secure-context-api-guard.test.mjs` 守住 HTTP 内网部署。
- 问答输入区停止键复用 `stop-control.tsx` 的 `STOP_CONTROL_CLASS` 与 `StopGlyph`，
  只显示图标，语义写入 `aria-label`/`title`，停止中禁用；报告带字动作行只复用图标。
  停止后轮次的判据与文案只来自 `stopped-turn.tsx`，被停止的 notebook 轮次不得作为
  可重发的 storage 草稿复活。全局问答复用既有会话对账与 missing-job 路径，调用点
  不另算替换/历史状态。
- 共享流使用 `task_stream.py::deliver_ask_events` 与浏览器 `ndjson-stream.ts` 行读取，
  route 模块不互相导入。`yieldToPaint` 保留后台标签页需要的 timer 兜底。权限变动走
  `use-notebook-collection.ts::refreshAfterAccessChange` 与壳层窄
  `reconcileOpenNotebook` effect，使已打开工作区与列表一起对账。

### SQL 里绑定 id 清单

清单大小由数据决定的语句——首先是一次运行冻结的来源天花板，它可以装下一个笔记本的全部
来源——一律经所在后端的 `id_binding` 模块绑定：
[PostgreSQL](../backend/app/repositories/postgres/id_binding.py) 用 `bind_ids` +
`member_of` / `not_member_of`，经 `execute_ids` 执行（或 `execute_bound`：没绑清单时保持
原样的普通 execute）；[SQLite](../backend/app/repositories/sqlite/id_binding.py) 用
`bind_ids` + `member_of` / `not_member_of` / `drive_by`。两边都返回 `BoundIds(sql, param)`：
不论多长，一个清单只占一个参数。完整测量写在两个模块的 docstring 里；决定这条规则的事实
（49,000 个来源的笔记本上实测）：

- PostgreSQL：psycopg 在同一连接上第 5 次执行时预备语句，PostgreSQL 约从第 11 次起可能改用
  generic plan，绑定的数组变成按 10 个元素估计的不透明参数。chunk FTS 在 49k 个 id 时从
  1.1 s 变成 3.1 s，超过它 3 s 的超时；超时的回滚又会清空预备缓存，于是每约 11 次循环一次，
  并打开词法熔断；稀有词 7 ms 对 7.7 s；KG 证据闸 169 ms 对 6.7 s。`execute_ids` 发
  `prepare=False`，每次执行都按真实清单规划。代价是每次都要规划：列上没有高频值时 49k 个 id
  规划 5–11 ms，有高频值时（每个来源的 chunk 数偏斜）62–81 ms（稀有词每问约 0.2 s）——换来的
  是有界的成本而不是悬崖。半连接写法 `col IN (SELECT unnest(…))` 规划只要几毫秒，但会让清单
  驱动计划，在每条语句上都有输的格子（常见词 chunk FTS 1.8 s → 5–7 s），所以不用。
- SQLite：每个 id 一个 `?` 会撞上部署版 32,766 个变量的上限（`SQLITE_LIMIT_VARIABLE_NUMBER`；
  本地版允许 250,000，所以故障只在生产出现），fail-open 的调用方会把报错变成静默的空通道。
  有索引的列上 `col IN (清单)` 还会让清单驱动计划：在复合索引 `(object_id, source_id)` 的
  相关探针里，每个候选行对每个 id 各探一次（49k 个 id 时 6.6–8.7 s）。`member_of` 写成
  `+col IN (…)`，清单只做过滤（14–20 ms）；`drive_by` 是「逐个查清单里每个 id」的显式写法。
  生产库没有规划统计信息，所以计划在有没有 `sqlite_stat1` 时都必须成立，计划钉子两种状态
  各跑一遍。
- 由有界主键窗口驱动、大天花板只做过滤的语句，PostgreSQL 谓词可以写在无统计的表达式上
  （如 `graph_hydrate_rows` 的 `(c.source_id||'')`），使 custom plan 跳过逐元素的 MCV 估算
  （49k 个 id：规划 146 ms → 5 ms），相当于 SQLite 的一元 `+`。

绑定层从不改变成员：id 原样绑定（包括空串和空白 id），非字符串 id 抛 `TypeError`；去重由
store 决定，两个后端一致。清单只有属于以下三类之一时才能直接绑定：**构造上有界**（一页或
`LIMIT` 窗口、排序后的主键窗口、一次运行的笔记本、固定的状态集合、单个来源的元素 id）；
**分批的键探针**（id 就是语句要读写的行的键，也是它唯一的选择性谓词，PostgreSQL 每条不超过
1024 个、SQLite 不超过 900 个占位符；如 `relation_delta_rows`，调用方按每批不超过 900 个传入
增量来源或 Memory 来源的 id）；**由清单驱动、只占一个参数**（整份清单就是要读的键
集合，如 `visible_source_scope_snapshot`）。清单用来过滤一条由别的东西驱动的语句时，永远不
属于豁免。`backend/tests/test_id_list_binding_guard.py` 扫描两个 repository 目录，任何既没
改走模块、也没在那里登记类别与理由的 id 清单绑定都会报红。刻意不采用：补集清单（冻结的
天花板不能放进冻结之后新增的来源）、绑定层里的身份缓存（属于持有长寿命清单的调用方）、
语句级规划器设置（实测两个方向都有）。

### Schema 与迁移编写

- SQLite 与 PostgreSQL 两个后端长期并存（2026-10-19 产品决定），PostgreSQL 是规范定义，
  SQLite 是支持离线、单机与测试的镜像；支持范围与能力对等矩阵归
  [架构文档](../architecture.md#222-双后端长期并存与能力对等矩阵)。存储层改动（schema、store 方法、
  SQL 谓词与锁序）在同一个 PR 里两侧都做：先定 PostgreSQL 口径，SQLite 按同一语义镜像，
  两侧迁移成对登记 `POSTGRES_SCHEMA_MANIFEST`；只为性能存在、SQLite 没有也不改变结果的结构可以
  只做 PostgreSQL，但要登记进矩阵。行为用例两侧各一份（SQLite 进 G1，PostgreSQL 孪生进 PG 泳道）；
  `backend/tests/test_dual_backend_parity.py` 静态核对版本配对、端口实现、类级公开成员集合，以及矩阵
  出处逐条可解析（只证明存在：改变被引用符号行为的 PR 要人工复核对应矩阵行）。
- SQLite schema 变更新增 `_migration_N`，同步提升
  [migrator](../backend/app/repositories/sqlite/migrations.py) 的 `SCHEMA_VERSION`；
  不修改已封存迁移。启动恢复、稳定 seed 与管理员原地升级不进入版本门，每次启动照跑。
- PostgreSQL 在 [migrations](../backend/app/repositories/postgres/migrations/) 追加
  连续编号 SQL，并同步 [POSTGRES_SCHEMA_MANIFEST](../backend/app/repositories/postgres/schema_manifest.py)。
  [migrator](../backend/app/repositories/postgres/migrator.py) 在迁移锁下验证 checksum
  与 ledger，不能改写已应用 SQL。当前版本号与逐版本 DDL 以这些可执行源文件为准。
- 同时保留新库与升级行为、null/default 语义、归属、keyset 排序/collation、
  FK/cascade/unique surface 和复制/清理分类。表特有的设计理由写在引入它的迁移旁；
  同批更新受影响的 schema/seed/snapshot fixture 与 migration manifest，不能
  重设旧兼容 fixture 的基线来掩盖升级破坏。
- SQLite 迁移单向：数据库 `PRAGMA user_version` 超前时以
  `schema contains a future version` 拒绝启动；PostgreSQL 同样拒绝超前 ledger。
  readiness 只暴露脱敏的初始化失败。恢复使用升级前备份或兼容/更新的程序，
  不执行反向迁移。执行与切换步骤见 [运维文档](./operations_zh.md)。
- 冻结的 [v9 fixture](../backend/tests/fixtures/repository_v9/) 与
  `scripts/verify_repository_snapshot.py` 保留升级兼容验证。snapshot 校验只在临时
  backup 上构造 repository，验证精确逐版本迁移与稳定 seed，并保持原始 DB/WAL
  metadata 及 SHM 存在性/大小不变（只有 live-WAL 的 SHM mtime 可不同），不记录私有行。

以前的逐版本叙述和交付说明可查阅
[精简前历史](https://github.com/huyangc/silicon-notebook/blob/403b796f/docs/development_zh.md#架构边界)。
这些只供追溯；现状以上面链接的运行时契约、迁移源文件和运维步骤为准。

## 验证

运行：

```bash
bash scripts/check.sh
```

验证门禁分为四级：

| 级别 | 范围 | 执行频率 |
| --- | --- | --- |
| G0 目标测试 | 按当前改动文件与行为选跑 | 编辑循环中随时执行 |
| G1 标准门 | `scripts/check.sh`：稳定后端、契约/harness、对照基线的后端 ruff、前端测试、对照基线的前端 ESLint、负责类型检查的 production build 与补测试文件诊断的包内类型检查 | 本地交付前以及每次 PR/push/手动 CI |
| G2 扩展门 | `scripts/check_extended.sh`：G1 加真实索引/性能测试、冷图/索引契约、全仓语义扫描（重活子集）、真实子进程合同与老版本升级矩阵 | 每天 `17 18 * * *` UTC（北京时间次日 02:17）一次，也可手动触发 |
| G3 PostgreSQL | `scripts/check_postgres.sh`：直接 PostgreSQL adapter 集成 | 独立的 PR/push/手动 CI job |

G1 并行运行三个有界 lane：`check_backend.sh` 以默认 16 个 worker 执行稳定 backend pytest（CI 固定 4 个）；`check_contracts.sh` 执行语法/依赖预检、hermetic smoke、契约检查、后端 lint 门（见[后端 lint 门](#后端-lint-门)）与确定性抽取评分 harness；`check_frontend.sh` 执行递归发现的全部 `*.test.mjs`、全部 `*.component.test.tsx`、ESLint（`npm run lint`，见[前端 ESLint 门](#前端-eslint-门)）、production build 与包内类型检查。在不少于 8 个 CPU 的主机上，同步一次插件投影之后，node 测试、组件测试、ESLint 与「build 之后类型检查」四组并发运行，各自写独立日志，全部结束后按固定顺序打印并逐步给出退出码；更小的主机（CI runner）保持串行顺序、在第一个失败处停止，`FRONTEND_LANE_PARALLEL=1`/`0` 可覆盖 CPU 判断。Node 原生 test runner 和 Vitest 各限制为 4 workers，为 backend 临界路径保留 CPU；Next build 不得启用 `ignoreBuildErrors`、仍是生产代码类型检查的权威，但 Next 的构建期类型检查会静默丢弃 `*.test.*`/`*.spec.*` 文件与 `__tests__`/`__mocks__` 目录里的全部诊断（`next/dist/lib/typescript/runTypeCheck.js` 的 `ignoreRegex`，Next 15.5 实测），只存在于 `frontend/tests/**` 的类型错误对 build 完全不可见；因此前端 lane 在 build **之后**补跑 `npm run typecheck`（`tsc --noEmit`）——排在 build 后是让它检查刚重新生成的 `.next/types` 而不是脏树上的陈旧生成物——这是唯一看得见测试文件的检查；`incremental` 使 warm 重查不到 1 秒（冷 ~5 秒），当初「同一程序重复解析两遍」要省的成本已不再成立。G1 backend 排除 `slow` 真实索引/性能用例、`graph_index_contract` 冷图/索引契约、`architecture_contract_heavy`（`_ARCHITECTURE_CONTRACT_HEAVY_TESTS` 中的八条全仓语义扫描）、`process_contract`（启动真实子进程——subprocess、multiprocessing spawn 或 fork——检查进程级合同的用例：服务启停与 launcher 信号交接、release manifest 的 git 历史与打包、batch ingest 进程池、真进程拒启、脚本与 CLI 入口）、`legacy_upgrade_contract`（升级已部署老版本库的用例：快照 verifier 的「已部署 vNN」用例、老版本迁移矩阵，以及这类升级排上的记忆隔离重建；同一模块里的当前 schema、新库与源码一致性检查仍留在 G1）和 PostgreSQL 树，其余轻量 `architecture_contract` 测试随 G1 每次 PR/push 跑。按 2026-10-14 把本机目标定为 80 秒的裁决，这些进程级与老版本合同由每日 G2 报告，不再每个 PR 即时覆盖。`process_contract` 与 `legacy_upgrade_contract` 逐测试标注，同一个文件可以与 G1 用例混排。下文的 lane 划分守卫虽然启动子进程，仍不加标记、留在 G1：只放在 G2 的话，表达式一旦写坏把它排除掉，它就再也不会运行。G2 先执行 G1，再执行精确互补的 backend marker 集——`backend/tests/test_test_architecture_policy.py::test_verification_lane_markers_partition_every_architecture_contract_test` 用 `--collect-only` 实测验证这个划分，不只是钉两条 `-m` 字符串。每个 lane 都有独立进程组，因此中断或终止 controller 时，也会终止并回收 pytest、npm 和 Next.js 的后代进程。官方 client MCP smoke 精确锁定已公开的二十一个工具：发现与概况两个、搜索与点查两个、问答两个、问答记录与报告四个、提交两个、来源四个、构建一个与四个 knowhow 工具。缺少 `frontend/node_modules` 会直接失败，不再静默跳过前端门禁。

验收时使用项目一直采用的 Homebrew/Miniconda Python：

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

G1 标准门并发运行 backend、contracts、frontend 三个 lane。`check_backend.sh` 默认使用 16 个 backend pytest worker，可用 `BACKEND_PYTEST_WORKERS` 覆盖。Apple Silicon warm gate 硬目标是不超过 100 秒（2026-10-19 裁决由 80 秒放宽，2026-10-14 挪到 G2 的合同测试仍留在 G2；60 秒仍是长期目标）；G2 每日扩展门不受该本机时限约束，各 CI lane 时长仅作观察，因此这不是对每一台 CI 机器的可移植超时断言。`check.sh` 结束时打印整门墙钟、各 lane 秒数，以及本次 JUnit 报告里最慢的 10 个 backend 测试文件与 10 条测试（`scripts/check_timing_summary.py`），墙钟超过 100 秒时打印醒目警告；这份汇总只是本地警告，从不改变门禁的退出码。门禁脚本把 Python 字节码（含 pytest 改写后的测试模块）写到 gitignored 的 `.local/pycache`（`PYTHONPYCACHEPREFIX`），并 unset 调用方 shell 导出的 `PYTHONDONTWRITEBYTECODE`：新 worktree 或 agent 会话也复用字节码缓存，不再在每个 worker 里重新编译全部模块。backend 测试里用手工构造的环境启动子进程时，一律展开 `BYTECODE_ENV`（`backend/tests/child_env_testkit.py`，由策略测试强制），所以 G1 运行不会往源码树写任何字节码。由于前缀按绝对路径镜像，测试写在临时目录下的模块也会被缓存进去；backend conftest 在每次 pytest 会话开始时删掉镜像临时目录的那一棵子树，缓存因此有界，仓库自身与解释器的模块缓存保持热。

测试加速必须保持结果语义：G1 标准门与 G2 扩展门的 marker 表达式精确互补，PostgreSQL 独立负责，任何已提交用例都不能变成不可达；全仓 AST/协议扫描在同一测试进程（pytest worker 或隔离的 Node guard 进程）内只解析每个生产文件一次，冻结 fixture 用例若只关心成员集合，就应使用只投影名称的入口，不生成详细站点、签名与 ownership；可执行的全仓守卫只在其归属的 contracts lane 对真实源码树运行一次，参数解析、失败模式与 extra-root 的单元测试把默认根重定向到最小 fixture，不再重复扫描全仓；同一份不可变行为矩阵上的断言只遍历一次，并给每一格保留明确失败标签，不能为每一格或不同断言族重复搭建完全相同的数据库世界；frontend lane 只同步一次不可变的本地插件投影，随后仅抑制 npm 重复的 `pretest`/`prebuild`/`pretypecheck` 钩子，开发者单独执行每条命令时这些钩子仍是必跑项；缓存容器策略直接验证容器，不搭建无关数据库与 ANN 索引；autouse 隔离路径从 worker 已有的 pytest base temp 派生，不为每条纯测试额外创建 `tmp_path`；普通 SQLite 仓储测试按 worker 只构建一次当前空 schema（这次构建本身就是每个 worker 里一次真实的从零完整迁移梯），再复制成每条测试各自独立的可变数据库文件。是否从零跑真实迁移梯按测试决定，不按模块：迁移梯本身是检查对象的测试标 `@pytest.mark.real_sqlite_migration`（或由所在模块的 `pytestmark` 统一标）。`test_test_architecture_policy.py` 要求：在拥有迁移器的模块里，凡是测试函数自身（直接或经模块内 helper）驱动 `SqliteMigrator` 的，以及启动迁移接线用例，都必须带这个标记；迁移与仓储快照模块里的升级用例逐条标注，新加进这些模块的用例默认用模板、留在 G1。只建当前 schema 空库的 fixture 使用模板副本；测试改了迁移梯（`SCHEMA_VERSION` 或某个 `_migration_N` 步骤，沿迁移器的类继承链查找）却没有标记时，autouse fixture 让这次复制直接失败；步骤调用的 helper 被打补丁时检测不到，这类用例要自己带标记。已经存在的库文件（老版本 fixture、回滚出来的副本）始终按真实迁移梯升级；仓储密集测试只可在 pytest autouse fixture 中降低默认密码派生成本，认证 helper 保留生产默认，比较凭据字段的快照模块必须登记 `_REAL_PASSWORD_HASH_MODULES`；普通 UT 与 G1 测试必须环境自足，不绑定宿主端口、不依赖环境服务；只有合同本身属于进程级行为时才保留自包含的子进程/信号覆盖，启动真实子进程的用例标 `process_contract`，在 G2 运行。`test_test_architecture_policy.py` 扫描测试模块：G1 用例自身代码（或它用到的模块内 helper、fixture）经 `subprocess`、`multiprocessing`、`ProcessPoolExecutor`、`os.fork`/`os.spawn*` 或 `asyncio.create_subprocess_*` 启动进程就判失败；lane 划分守卫是唯一经审查的例外，进程在应用代码内部启动时这项扫描看不到。并发顺序与公平性使用 event/barrier，而非固定 sleep 或线程唤醒顺序；分波次排队时由控制线程运行被测同步编排，在观测到目标容量后用 event 放行，不能让后一波单独落进 cyclic barrier；进程级延迟任务须在共享 teardown 中取消待执行项并等待活跃项收敛，不能只清理由某个局部 repository 对象可见的任务。测试若把 `app.*` 模块从 `sys.modules` 摘掉，必须经 `monkeypatch`（`delitem`，并钉住父包属性）还原，不能裸 `pop`：父包属性仍绑着旧模块对象，下一次 import 会让属性查找与 `sys.modules` 查找分叉，对着一条路径打的补丁另一条路径看不见；backend conftest 在每条测试结束后核对这一不变量，让污染者报错而不是后跑的受害者。收集结束时 backend conftest 先做一次完整回收，再对存活对象（已导入的 app、测试模块与已收集的用例）执行 `gc.freeze()`，循环回收器不再在每次完整回收时重新遍历这块常驻堆；测试之后分配的对象照常回收。

测试进程在多次 `create_app()` 之间复用固定核心 router 的路由。`backend/tests/conftest.py` 在 `pytest_configure` 里安装 `tests/speedups/` 下的测试专用加速，早于任何测试模块导入 `app.main`。`tests/speedups/app_routes.py` 包装 `APIRouter.include_router`：FastAPI app 自己的 router 引入 `app.main` 全局变量里的某个 `APIRouter`，且只带 `prefix`/`dependencies`、源 router 没有 startup/shutdown 处理器时，第一个 app 之后的每个 app 拿到首次构建出的路由的副本。每个副本都有自己的容器和依赖树，`dependency_overrides_provider` 绑定到当前 app，handler 重新生成；原调用末尾的 lifespan 合并仍走真实实现，传入的是源 router 自己的 lifespan 对象。onboarding router 和部署插件 router 不是 `app.main` 的全局变量，所以始终走真实路径（插件 factory 每次返回同一个 router 对象也一样）；源 router 的路由被增删或原地修改时重新构建。其他调用同样走真实路径：换 prefix 或 include 级依赖、传入其他 `include_router` 参数、目标 app 或 router 自带依赖或配置、源 router 带 startup/shutdown 处理器。有一个输入模板看不到：真实构建会按端点模块的全局变量重新解析字符串注解，所以测试不得在定义端点的 `app.api` 模块上 monkeypatch 类型名；pin 文件里的源码守卫会拒绝这类补丁。产品 `create_app()` 没有改动，每个 app 仍有自己的 FastAPI 对象、MCP 服务、中间件栈和 state。`backend/tests/test_app_route_templates.py` 钉住：模板构建的 app 与真实构建一致（OpenAPI、按顺序的路由属性、lifespan 链）；一个 app 上的 override 不会影响另一个 app，两个 app 之间不共享任何路由容器或依赖树节点；失效和白名单规则；命中条件的每条边界都与真实调用的结果一致。shim 只在验证过的 FastAPI 版本（`requirements.txt` 的钉版）上安装，升级后这条 pin 会变红，直到重新核对内部字段。设 `SILICON_NOTEBOOK_TEST_ROUTE_TEMPLATES=0` 可让整轮测试走真实路径；单个测试内用 `app_routes.disabled()`。

界面词汇守卫的 `guard.main()` 自检用例（`backend/tests/test_ui_vocabulary_guard.py`）跑在最小 fixture 树上而不是真实仓库：`_minimal_guard_tree` 把每条通道的全部扫描根与登记模块重定向到一棵每条通道都非空且干净的小树，并把 `MIN_*` 非空性下限降到这棵树的站点数。`test_最小树上守卫全绿` 钉住基线（`main() == 0`），每条变异用例除了断言目标通道自己的报错文案，还要断言没有任何别的通道在报错，避免邻近通道塌了而让用例碰巧变红。真实源码的全仓扫描仍只在 contracts lane（`check_contracts.sh` 调用的 `scripts/check_ui_vocabulary.py`）里跑一次，保留下来的真实树用例（登记的展示字段模块都存在、出厂服务显示名、contracts lane 接线）直接读取仓库。

扫描 `backend/app` 的 Python 源码守卫通过 `backend/tests/architecture/source_trees.py` 的 `app_source_trees()`（单文件用 `read_source_tree()`）读取，同类新守卫也应如此，而不是各自解析整棵树。解析缓存只以文件内容为键（语法树不带文件名）：无论多少个守卫跑在同一个 pytest worker 里，每份不同的源码文本在该 worker 中只解析一次，文件被改动则一定得到新树；调用方不得修改返回的语法树。尚未迁到共享入口的守卫仍在私下解析：`backend/tests/architecture/semantic_source.py` 的 `PythonSourceIndex`（默认天花板与参与集覆盖守卫使用）和 `scripts/check_architecture_boundaries.py`（phase-0 守卫使用）。扫描整棵树并经共享入口解析的守卫共用一个 `xdist_group`（引用相关守卫用 `source_scans_citation`，其余用 `source_scans_core`），使它们落在同一个 worker 上复用这份缓存，而不是在多个 worker 里重复解析。分组只影响调度：不改任何 marker，也不改变 G1/G2 的选择；已有分组的模块保留原组、不叠加第二个组；从共享中得不到好处的模块保持不分组。每个分组的门内忙时应保持在约 20 秒以内。

种子世界模板（`backend/tests/world_templates.py`）让一个模块按 pytest 进程只构建一次昂贵的种子世界，再给每条测试复制一份，每条测试仍只改自己的副本。构建在 `MonkeyPatch.context()` 内从进程基线环境开始，而不是继承触发构建的那条测试的环境；世界依赖的设置全部经 `env=` 显式给出，并计入模板身份，同一个 key 换了设置会直接报错；构建返回的 payload 只能是 JSON 纯数据，不能夹带活对象。构建结束必须关掉自己打开的一切：每个 SQLite 文件执行 `PRAGMA wal_checkpoint(TRUNCATE)` 且 WAL 必须为空，进程里不能再有指向世界目录的文件描述符。世界必须重开稳定（只屏蔽种子管理员每次打开都会重新派生的凭据列），中断作业恢复、种子补插这类打开即规范化在构建期就报红。`relocatable` 世界的任何行、存储文件和 payload 里都不能出现构建根路径；存了绝对路径的世界（上传源）使用 `pinned`，每条测试都从未被触碰的 `pristine` 副本重建固定的 `live` 目录。每次 materialize 都复核 pristine 的文件，并先复位按身份作键的进程级缓存，因为同一 worker 的测试现在共享 notebook 与用户 id；随后只回填构建期为本世界自己的 id 留下的条目（版本来自本世界历史的计数 memo、按原有时长回填的资产清扫节流），让每份副本开局时的缓存状态与以前自建世界时一致；`backend/app` 新增模块级缓存而没有在 helper 的清单里分类时，`test_world_templates.py` 会报红。

MCP 协议 fixture 按 worker 为用户、笔记本、档案、成员关系和 token 构建一份
`relocatable` 种子世界。payload 只保存实体 ID；每条测试从自己的数据库副本读取对象
与 token 原文，并重新构建 app 和服务图。协议客户端与 lifespan 上下文仍按测试独立，
整个模块不强制集中到单个 worker。一个副本中的 token 撤销、成员变更、token 使用记录
或 app 状态变更不得影响另一个副本。构建时逐一核对原创建返回值与重读对象的具体类型
和完整字段（包括签发的 token 原文），防止正确落库掩盖错误的接口返回值。

Schema guest、第三方导入与笔记本生命周期源码守卫也经共享的按内容 AST 入口读取。
Schema guest 扫描缓存每棵树的不可变语义投影，并把三条真实树检查放在同一小分组；
笔记本生命周期扫描把所属函数信息放在独立投影中，不向共享 AST 节点写入 parent 属性。
目录发现与源码读取仍每次执行，包括文件大小与时间戳不变的编辑；缓存投影不替代变异覆盖。

同步测试（`test_sync_incremental_import.py`、`test_sync_import.py`、`test_sync_export.py`、`test_sync_incremental.py`）的种子库、全量导出与基线导入走 `pinned` 世界模板：上传源保存绝对 `file_path`，导入报告写在包目录旁。导出/导入报告以纯数据随 payload 携带（`backend/tests/sync_world_cases.py`），构建期先证明重建出的报告与原对象逐字段相等、类型不变，每条测试再各自重建一份。`test_sync_import.py` 另有一级「默认导入之后」的世界：只有测试体是「用默认参数导入一次、随后只读断言」的用例才经 `first_import` 读取构建期用真实代码跑出的导入结果；打补丁、观察导入过程或事件、换参数、或导入之后还要再行动的用例，仍在测试体内自己导入；模块收集时会拒绝请求 `first_import` 却又用 `monkeypatch`、`MonkeyPatch`、`setattr` 或再次调用导入的用例。把用例改成读这一级世界之前，必须证明给导入代码注入的缺陷仍让它变红。

测试 fixture 的成本应跟随被测行为。Scale-build 的锁准入与接力测试使用尚未建索引
的笔记本；真实 build/fold/发布测试继续保留所需的种子数据或索引产物。Scale CLI 的
未知笔记本拒绝用例使用真实迁移后的数据库，无需导入、知识图谱或向量；非 PostgreSQL
URL 拒绝用例使用可连接的 SQLite 文件。完整 CLI 制品流程仍执行导入、抽取与嵌入，
其中只读查找笔记本不再重复迁移和初始化。正常阶段事件
与进度回调断言共用一次 facade build，回调失败与直接 builder 的产物合同仍独立覆盖。
三条笔记本生命周期字面量扫描共用一个 `xdist_group`，确保进程内 AST 缓存确实复用；
G1/G2 选择表达式不变。前端源码策略检查在隔离的 guard 进程内复用不可变模块输入
与解析树。

计时器测试通过受控时钟推进原有截止时间，并保留到期前后的状态断言，不再等待真实
时间。后台删除 HTTP 测试用事件暂停和释放真实 runner；启动 sweeper 测试等待观察到
周期 sweep，并检查关闭后线程确实退出。真实仓库的 UI 词汇扫描非空性归 contracts
lane 的 `check_ui_vocabulary.py` 负责；单元测试保留最小空扫描失败 fixture 和变异
覆盖，不再重复执行一次全仓计数。

性能钉子计工作量，不计时：墙钟上限在负载重的门禁上会假红（35 ms 的分享拷贝快照在
CI 上量到 3.19 s），对真回退也没有鉴别力。分享拷贝的钉子在 n 与 2n 两档规模上比较发出
的语句、「含 Memory 成员的簇」集合的读取次数与 SQLite 虚拟机指令数（`_snapshot_work`），
平方级的遍历量出来约 3.9 倍。PostgreSQL 计划钉里随可见性图翻转的选择（同库有早于灌数的
快照挡住 VACUUM 时，Index Only Scan 按回表计价）在 `bystander_snapshot` 两态下各跑一遍：
精确计划只在 `vacuum_until_all_visible` 核对可见性图全满之后断言，与访问方式无关的不变量
（不顺序扫描、每个扫描都以被探测的行为键）两态都断言。这个 helper 只看快照早于表里最新
一行的会话：普通会话过了 10 s 还挡着就让调用失败并点名；维护会话（autovacuum worker、
进行中的 ANALYZE 或 VACUUM——autoanalyze 在整个采样期间都持有快照）最多等 180 s。
可见性图不满时它从不返回。

扩展服务生命周期测试共用一个 `xdist_group`，限制相互争抢资源的 supervisor 启动数；
每条测试内部仍保留真实进程和线程并发。状态文件回归测试在打开后强制替换路径：
JSON 读取方可读取已打开、当前用户所有的普通文件快照，即使其最后一个链接已移除；
锁和可写句柄仍要求单链接，不安全文件仍被拒绝。
启动器信号测试一律以默认信号处置启动启动器（`preexec_fn`）：非交互 shell 不能 trap
启动时已被忽略的信号，而在 `nohup` 下或作为后台泳道运行的门禁，会把 SIGHUP 或 SIGINT
以「已忽略」的状态传给子进程。等待一律以握手结束（按改名发布的 pid 文件，或启动器退出），
20 s 只作兜底上限。
启动器退出收尾的用例（`dev.sh` 与 `prod.sh` 共用 `scripts/launcher_cleanup.sh`）在
`backend/tests/launcher_checkout_testkit.py` 里用假的 uvicorn / `next` / `node` 子进程，记录
收到的 SIGTERM（`TERM_EXITS`）或无视它（`TERM_IGNORED`）。钉住：「TERM → 宽限 → KILL」与两个
启动器共用的退出码 130 / 143 / 129；宽限等待期间再按一次 Ctrl-C、再收到 TERM 或 HUP 会立刻结束
等待、配套服务仍被停止（prod.sh 用假 `rm`（`RM_BLOCK`）停在交接之前）；同样的信号发给启动器的整个进程组
（像终端的 Ctrl-C 那样：启动器作为会话首进程，`new_session`，用例用 `os.killpg`），它也会打断启动器
自己的 `sleep`，这不得在 `set -e` 下中止收尾；后端的父进程就是启动器
本身（`exec` 守卫；`( 单条简单命令 ) &` 在 bash 里根本不 fork 子 shell，所以可观察的是子 shell 里
多出第二条命令，而不是去掉 `exec`）；挂起的 trap 没来得及记下 pid 的子进程仍被停止（用 fork 与
`PID=$!` 之间的 `kill` 确定性复现）；两个启动器都 source 共享脚本且每次 fork 都有括号标记。
`dev.sh` 直接启动 `next dev`，所以也要手工执行 `predev` 的 UI 插件同步：用例钉住同步先于 next
启动完成、同步失败时不启动前端并停掉后端、同步期间被取消或后端退出都不留任何进程，以及
`frontend/package.json` 的钩子仍恰好是 `dev.sh` 复现的那些。运维侧行为见
[operations_zh.md](./operations_zh.md)。

### 压测脚本

有些回归落在任何确定性用例都打不中的窗口里，由 `scripts/` 下的压测脚本守护，而不是
pytest 用例：已提交的用例一律会被收集，没有任何环境开关能把它从门禁里藏起来。G1 和 G2
都不跑这些脚本；改到它守护的代码后，在满载机器上手动跑一次。

- `scripts/stress_launcher_signals.py` 守护 `scripts/extension_services.sh` 里伴随服务控制器
  fork 附近的三条 macOS `/bin/bash` 3.2 路径：`extension_services_start` 必须轮询控制器、不得
  阻塞在 `wait` 上；`extension_services_cleanup` 必须重复发送 TERM；挂起的 trap 恰在 fork 之后
  执行时，收尾必须经 `$!` 找到刚 fork 出的控制器。每次尝试在一个假检出目录里起真实启动器
  （`backend/tests/launcher_checkout_testkit.py`，与启动器用例共用），在启动器创建回执后
  0–4 ms 内打断它：20 s 后仍未退出记为挂住；启动器退出了却留下控制器在跑记为泄漏，那个
  控制器按精确 pid 杀掉：

  ```bash
  python scripts/stress_launcher_signals.py --load auto
  ```

  `--load auto` 同时为每个核起一个忙循环。默认 2 个启动器（`--launchers dev.sh,backend.sh`）
  × 4 个并行分片（`--shards`）× 1500 次（`--runs`），`--seed` 固定随机偏移。退出码 0 表示没有
  挂住也没有泄漏，1 表示有挂住或泄漏，2 表示用法错误或分片自身失败。`--load auto` 实测：
  修复后的启动器 12000 次里挂住 0 次、泄漏 0 次；去掉 `$!` 兜底时（空载）400 次里泄漏 92 次；
  改回阻塞 `wait` 时两轮 12000 次里分别挂住 2 次和 4 次。这一竞态约每 3000–6000 次命中一次，
  默认一轮约有 85–98% 的把握抓到；需要更高把握时加大 `--runs` 或多跑几轮。`$!` 那条路径泄漏
  频繁，默认一轮必然抓到。

### Python 依赖

Python 依赖带哈希锁定，安装时不做任何解析。`backend/requirements.in` 声明运行依赖的
版本范围，`backend/requirements-dev.in` 声明测试与门禁工具（pytest、pytest-xdist、ruff，以及
pytest 主控预热字体缓存用的 matplotlib）；其中 `-c requirements.txt` 一行让两边都需要的
包保持运行锁里的版本。`scripts/lock_python_deps.sh` 用 pip-tools `--generate-hashes`
把它们编译成 `backend/requirements.txt`（运行锁）与 `backend/requirements-dev.txt`
（测试锁）。解析固定用 CI 与生产所跑的 CPython 3.13（`LOCK_PYTHON`，默认
`python3.13`）；锁定的每个版本也发布了 3.14 的 wheel，本机 3.14 环境同样能装这两份锁
（hnswlib 在哪个版本上都是源码编译）。锁脚本在 `.local/` 下的私有 venv 里跑钉死版本的
pip 与 pip-tools。哈希覆盖
PyPI 为每个钉定版本发布的全部文件，所以同一份锁在所有支持的平台上都能装——Apple Silicon
的 macOS 14+ 与 glibc ≥ 2.28 的 Linux x86_64/aarch64，这是 onnxruntime、igraph、PyMuPDF、
pymupdf-layout 定下的下限（见 `docs/deployment-and-configuration_zh.md`）；Intel Mac 装不上。只要没有
依赖在两个平台间按平台条件区分，在哪个平台解析结果都相同；若锁漏了这样的依赖，
`pip install --require-hashes` 会响亮失败，而不是装上别的东西。

| 安装场景 | 命令 |
| --- | --- |
| CI，以及要跑门禁的开发环境 | `python -m pip install --require-hashes -r backend/requirements.txt -r backend/requirements-dev.txt` |
| 部署（`scripts/prod.sh`、README/部署文档） | `python -m pip install --require-hashes -r backend/requirements.txt` |
| 离线包（`scripts/pack.sh` → `packaging/install.sh`） | 按运行锁 `pip wheel --require-hashes`，再 `pip install --no-index --require-hashes -r wheelhouse/locked-wheels.txt` |

改依赖是显式、可评审的改动：

- 新增、删除或改范围：编辑 `.in` 文件，运行 `bash scripts/lock_python_deps.sh`，
  把 `.in` 与两份锁文件一起提交。仍满足范围的既有钉版全部保留，diff 里只出现这次
  改动必需的变化。
- 升级：`bash scripts/lock_python_deps.sh --upgrade-package NAME`（可重复，允许
  `NAME==VERSION`）只升指定包；`--upgrade` 把所有包升到范围允许的最新版。下文
  FastAPI/Pydantic 的精确钉版仍须连同 OpenAPI 重生一起升级。
- `bash scripts/lock_python_deps.sh --check` 在临时副本里重锁，若已提交的锁会变化
  就失败（需要访问包索引）。

`backend/tests/test_python_lockfiles.py` 是后端泳道里的离线门禁。每份锁都带着编译它的
`.in` 文件的指纹（`scripts/python_lockfiles.py`；注释、顺序、空白不计），改了 `.in`
却没重锁就会红。以下情况同样会红：直接依赖不在锁里或钉版超出范围；某个钉版没有哈希、
已不再被需要、或被未锁定的包需要；锁不是锁脚本在 Python 3.13 下的产物；测试锁与运行锁
在共享包上不一致；测试工具进了运行锁。它还沿每个锁定包自己的 `Requires-Dist`（按 marker
与被请求的 extras 求值）逐条检查，缺进锁的依赖就红；这一步读已安装包的元数据，所以在按锁
安装的环境（如 CI）里覆盖全部锁定包；`CI=true` 时还断言确实读到了每个锁定包。`backend/tests/test_third_party_imports_resolve.py`
把 `backend/app` 与 `scripts/` 引用的每个第三方模块都导入一遍（包括函数内 fail-open 的
懒加载，以及 `TYPE_CHECKING` 块与 `try`/`except ImportError` 里的导入；没有允许清单；`scripts/`
同级模块算本仓库自己的），核对每个被导入的名字、每个对懒加载模块读取的属性，并要求提供它的发行包在锁里；
`test_parsers_pdf_python.py` 用已安装的 pymupdf4llm 真转换一份 PDF。两者合起来，防止一次
升级把某个坏版本锁进来、又被 fail-open 兜底悄悄吞掉。

首份锁（2026-10-21）沿用 `3132d4667` 那次 `master` CI 实际安装的版本，只有一个例外：那次
CI 和 PyPI 于 2026-10-19 发布它以来的每次全新安装，装到的都是 `pymupdf4llm` 2.0——一个没有模块的空壳
占名包，PDF 兜底因此一直静默退回 pypdf 纯文本。`requirements.in` 把它限制在 2 以下，锁里
钉 1.28.2 及其依赖（PyMuPDF、pymupdf-layout、onnxruntime 等七个包），后端环境约增 200 MB。

### 前端 ESLint 门

`npm run lint`（在 `frontend/` 下）用 `frontend/eslint.config.mjs` 跑 ESLint 9：Next 自带的
`next/core-web-vitals` 与 `next/typescript` 两套规则（eslint-config-next 15 仍是 eslintrc 格式，
经 `FlatCompat` 载入），`react-hooks/rules-of-hooks` 与 `react-hooks/exhaustive-deps` 都是 error。
所有规则一律是 error：ESLint 的 bulk suppressions 只记录 error，warning 既进不了基线也拦不住
任何东西；`--max-warnings 0` 加 `reportUnusedDisableDirectives: "error"` 让 warning 这一档彻底
不存在。前导下划线表示「刻意不用」的绑定。`tests/**` 下关掉两条只保护 Next 打包与对外服务内容的
规则（`@next/next/no-img-element`、`@next/next/no-assign-module-variable`）。ESLint 不读
`.gitignore`，所以不参与 lint 的内容由配置里唯一一份全局 `ignores` 列出：构建与工具产物
（`.next`、`node_modules`、`.local`、`coverage`、`dist`、`build`、`next-env.d.ts`），以及
`sync-ui-plugins.mjs` 生成的插件投影（`features/ext-*`、`registry.local.ts`）。lint 与类型检查是两步：
`npm run typecheck` 是 `tsc --noEmit`（引入 ESLint 之前它叫 `npm run lint`）；`next build` 不跑
lint（`eslint.ignoreDuringBuilds`），因为 Next 构建期那一遍不带基线。

门禁之前就存在的违反记在 `frontend/eslint-suppressions.json`，按「文件 + 规则」计数。对每个
lint 到的文件，某规则的违反数超过该格就失败，该格大于实际仍存在的数也失败（exit 2，「suppressions
left that do not occur anymore」）；它只核对 lint 到的文件。
`frontend/tests/guards/eslint-baseline-guard.test.mjs` 补上它管不到的部分：每条规则的总条数与
文件数在 `BASELINE_CEILING` 里零松弛钉死；基线里列出的文件必须存在且未被忽略（文件删除、改名或
新加进忽略表后，ESLint 从不核对它的条目）；全局 `ignores` 表与 `reportUnusedDisableDirectives`
钉死；`app`、`features`、`scripts`、`test-support`、`tests` 下的每个源码文件都必须受检，两条
hooks 规则都解析为 error，且没有规则停在 warn；`react-hooks/rules-of-hooks` 永不豁免，既不进
基线，也不许有针对它的 `eslint-disable` 注释（它的违反要么是真 bug，要么是被误命名为 `use*`
的 helper，都当场修）。钉的是按规则的合计，所以同一规则在文件间对冲（A 文件修掉一条、B 文件新增
一条）时所有数字都不变，只能靠评审 `eslint-suppressions.json` 的 diff 发现。降基线：修掉违反，跑
`npm run lint:prune`（`eslint --prune-suppressions .`），同一改动里调低 `BASELINE_CEILING`。
不要用 `eslint --suppress-all` 或 `--suppress-rule` 收编新违反：修掉它；代码确属刻意如此时，在被
报告那一行的正上方写 `// eslint-disable-next-line <rule> -- <理由>`。并行路径上 ESLint 自成一组
（参考机冷跑约 5 秒，被 build 盖住），不拉长前端 lane。

### 后端 lint 门

`scripts/check_backend_lint.py` 用 `backend/requirements-dev.in` 钉死的那个 ruff 版本，
按仓库根的 `ruff.toml` 检查 `backend/app`、`backend/tests` 与 `scripts`：git 在这三处看得见的
每个 Python 文件，已跟踪的和未跟踪但未被忽略的都算。三处都没有生成的 Python，所以不排除任何
文件。它在 contracts lane 里跑，耗时不到 1 秒，不在 backend 临界路径上。本机在开发环境里
（测试锁会装上 ruff）直接跑 `python scripts/check_backend_lint.py`。

只开 lint 规则与 import 排序，不开格式化，也不开其它风格类规则：

- pyflakes（`F`）、`E9`（解析器读不了的文件）与 `E722`（裸 `except`）；
- pylint 的错误类（`PLE`）；
- flake8-bugbear 里指向疑似 bug 的那部分：可变的参数默认值与 `ContextVar` 默认值、`finally`
  里的 `return`/`break`/`continue`、重复或空的异常元组、`pytest.raises(Exception)`、无意义的
  比较与表达式、方法上的 `lru_cache`、集合里的重复项等。完整清单见 `ruff.toml`；
- import 排序（`I`，isort 默认风格）。排序器的设置照仓库现有的导入写法定：`src = ["backend",
  "scripts"]` 让 `app`、`tests` 和 scripts 之间的同级模块（`import diag_common`）算第一方，
  存量从 1836 条降到 1450 条；不把仓库根算进去，是为了不把第三方库 `packaging` 误认成仓库里
  的 `packaging/` 目录。`line-length = 120` 只给排序器用（没有开任何行长规则）：99% 的单行
  import 本来就不超过 88 列，但超出的 133 行在 88 列下都会因换行被报，120 列下只剩 4 行
  （1009 个文件共 1395 条）。`force-sort-within-sections` 与 `order-by-type = false` 都和现有
  写法差得更多（分别是 1715 和 1623 条）。存量只记进基线，没有修：开这条规则时没有重排任何
  import。排序器只排连续的 import 块，不会把 import 挪过 `sys.path.insert` 这类语句；门禁也
  从不跑 `--fix`。报新违反，意思是新写或改动的 import 在它所在的块里放错了位置，放对即可；
  已经记在基线里的乱序块，再往里加 import 条数也不变，所以新违反从不逼人挪动已有的 import。

刻意不开：

- `B008`：FastAPI 的 `Depends()` 默认值。
- `B904`、`B905`、`B007`、`B009`/`B010`：异常链、`zip(strict=)`、未用的循环变量、常量
  `getattr`/`setattr`。这几条属于风格，或者改了会改变行为。
- `B023`：闭包里引用循环变量；命中的大多是在循环体内当场调用的闭包。
- `UP`（2026-10-22 裁决）：537 个文件共 6324 条，几乎全是注解改写（`List` → `list`、
  `Optional[X]` → `X | None`、去掉注解引号、`datetime.UTC`）。这些是风格搅动而不是 bug，
  其中 `UP042`（`(str, Enum)` → `StrEnum`）还会改变 `str()` 的结果。

门禁之前就有的违反记在 `scripts/backend_lint_baseline.json`，形状是「文件 → 规则 → 条数」。
出现下面任何一种情况，门禁都会失败：

- 某个（文件, 规则）的条数高于基线（新违反）；
- 基线某格高于实际仍存在的条数（过期：修掉了一条违反、某个 import 块已经排好序、文件被删或
  改名）；
- 基线里出现永不进基线的规则（`F821`、`F822`、`F823`、`E902`
  与语法错误（`invalid-syntax`）一律当场修）；
- ruff 实际检查的文件集合与 git 在这三处看到的不一致（用 `git ls-files -z`，任何文件名都
  能处理）、其中有 Python 文件是符号链接，或者三处目录下出现 ruff 配置文件（`ruff.toml`、
  `.ruff.toml`、`pyproject.toml`）；
- 点名受检规则的行级豁免与脚本里的 `SUPPRESSIONS` 不一致（按规则比对条数与文件数；现有的
  都早于这道门：为注入而导入的 pytest fixture、再导出、为副作用而做的导入），或者出现被禁止
  的指令：不带规则码的 `# noqa`、文件级的 `# ruff: noqa` 与 `# flake8: noqa`，以及所有
  `# isort:` 指令（`skip`、`skip_file`、`off`、`split`）；
- 装的 ruff 不是钉死的版本。

环境问题（没装 ruff、没有 git、不在 git 检出里）以退出码 2 结束，并说明该装什么、该在哪里跑。

`backend/tests/test_backend_lint_gate.py`（backend lane，不起子进程）补门禁自己看不到的几处：

- 每条规则的总条数与涉及文件数零松弛钉在 `BASELINE_CEILING`；`SUPPRESSION_CEILING` 必须与
  脚本里的 `SUPPRESSIONS` 相等。
- `ruff.toml` 必须与预期配置逐项相等：不许有 `ignore`、`per-file-ignores`、`exclude`、
  preview 或 `extend-*` 键；检查的根目录和空的排除清单也都钉死。
- `check_contracts.sh` 必须恰好有一条有效命令行
  `"$PYTHON_BIN" "$ROOT_DIR/scripts/check_backend_lint.py"`，位于 `set -euo pipefail` 之下、
  任何 `exit` 之前：把它注释掉、行尾加 `|| true` 或 `; true`、挪到 `exit` 之后，都会报红。

这些数字按规则合计。同一条规则下「A 文件修掉一条、B 文件新增一条」各项数字都不变，只能靠
评审基线的 diff 发现。

降基线的步骤：

1. 修掉违反。
2. 跑 `python scripts/check_backend_lint.py --prune`。它只调低基线、删掉降到零的格（包括
   已删文件的格）；只要还有新违反，它什么都不写。它会打印每条规则的钉住总数要降多少。
3. 在同一个改动里按打印的数字调低 `BASELINE_CEILING`；若删掉了豁免注释，同时调低
   `SUPPRESSION_CEILING` 与 `SUPPRESSIONS`。

改名或挪动一个有基线条目的文件时，门禁会报旧路径不再受检、新路径的违反是新增。此时跑
`python scripts/check_backend_lint.py --rename OLD NEW`：只有旧路径已不存在、新路径还没有
条目时，它才把旧路径的条目搬到新路径，每格不超过新路径实际还有的条数；只要还有别的新违反，它
什么都不写。它会打印上限要变多少（单纯改名时不变）。

过期报错的文案也是这么指引的。开了 import 排序后，过期比其它规则更容易碰到：改动之后某个
记在基线里的 import 块恰好排好了序，或者删掉了一个有基线条目的文件，都要 prune。用 `master`
最近 300 个提交回放（其中 220 个动了受检的 Python 文件）：有 4 个需要 prune（涉及 9 个文件，
多是被删或重写的测试模块），37 个新写的 import 顺序不对。

没有任何命令会往基线里加格。新违反不许靠改基线或加 `# noqa` 收编，一律修掉；只为再导出而
保留的名字写进模块的 `__all__`。升级 ruff 自成一次需要评审的改动：改钉版、重锁，并在同一个
改动里重算基线和两张上限表，因为规则行为会随版本变化。

### GitHub Actions CI

每个 workflow 最多三个 job，因为每个 job 都会作为一个 check 出现在 PR 上。不设只做汇总的
job：job 名本身说明它把关什么，每个 job 都必须通过，workflow 只有在全部 job 通过时才算通过。

`.github/workflows/ci.yml` 在目标为 `master` 的 PR、`master` push 与手动触发时运行。G1 是
其中两个 job：`CI / level-1-backend` 在一台 runner 上执行 `scripts/check_backend.sh`（完整
后端泳道，不分片，4 个 pytest worker）；`CI / level-1-contracts-frontend` 执行
`bash scripts/check.sh --lane contracts --lane frontend`，其余两条泳道按本地 `check.sh`
的方式并行运行。第三个 job 是 G3 `CI / level-3-postgres-integration`。不带参数的
`check.sh` 仍是完整的本地 G1；`--lane <name>`（可重复）只跑指定泳道，存在的目的就是让
CI 把后端泳道放到单独一台 runner 上。

`.github/workflows/daily-extended.yml`（`Daily Extended Gate`，每日一个 cron 加手动触发）
用三个 job 跑 G2。`level-2-backend` 与 `level-2-contracts-frontend` 逐条命令对齐
`ci.yml` 跑 G1，唯一差别是依赖安装仍按下文关闭 pip wheel cache。
`level-2-contracts-frontend` 随后再以 `FRONTEND_LANE_PARALLEL=1` 运行
`scripts/check_frontend.sh`，G1 那一步失败也照跑：CI runner 不足 8 个 CPU，这是 frontend
lane 并行路径唯一的 CI 覆盖，PR CI 从不强制走它。`level-2-complement` 执行
`check_extended.sh --complement-only`：扩展后端 marker 集合、样板插件 UI lane 与 facade
退役棘轮，不重跑 G1。`--complement-only` 只供这个 workflow 使用；本地带这个参数跑不算
G2，不带参数的 `check_extended.sh` 仍是完整的本地 G2（先 G1，再按顺序跑补集）。
2026-10-07 之前每日 workflow 是单个 job，在一台 4 vCPU runner 上 25 分钟内跑完
`check_extended.sh`；最后几次绿跑已用 23–25 分钟，其中 G1 后端单独就涨过 20 分钟，
2026-10-07 起每一次都在超时处被取消，没有产出任何 G2 结果。

两个 workflow 都固定使用 `ubuntu-24.04`、Python 3.13 和唯一的 Node.js 大版本，从声明的
依赖文件安装，并把测试选择委托给 wrapper 脚本。`test_ci_workflow_contract.py` 钉住
job 集合与名字、最多三个 job 的规则、G1 的 job 合起来恰好把 `check.sh` 的 `LANES` 每条
跑一次、每日 G1 半边与 `ci.yml` 对齐，并带与不带参数实际执行 `check.sh` 与
`check_extended.sh`。

仓库只支持一个 Node.js 大版本 26，钉在 `.nvmrc`。每个 `actions/setup-node` 都读
`node-version-file: .nvmrc`；`frontend/package.json` 声明
`"engines": {"node": ">=26 <27"}`，`frontend/.npmrc` 设 `engine-strict=true`，于是
`npm ci`/`npm install` 拒绝其他大版本；`scripts/check_frontend.sh`、
`scripts/check_sample_plugin.sh` 与 `scripts/pack.sh` 发现要用的 Node 不是这个大版本时，
在做任何事之前就退出（`scripts/node_version.sh`）。Node 26 自带 Web Storage 全局，不给
`--localstorage-file` 时它们的 getter 返回 `undefined`，而 vitest 的 jsdom 环境会让它们
盖住 jsdom 自己那份；`frontend/test-support/setup.ts` 在内建 storage 取值为 `undefined`
时补回真正的 jsdom storage，并连 `Storage` 类一起补（否则 `vi.spyOn(Storage.prototype, …)`
会静默打空）。

backend 与 contracts wrapper 单独执行时仍隔离部署环境，打印慢 pytest 时长，并将 JUnit
写入 `backend/.local`；CI 包括失败时也上传这些报告以及扩展后端泳道的
`backend/.local/backend-extended-junit.xml`，保留七天。扩展后端泳道与其他泳道使用同样的
离线隔离与字节码前缀。

六条生产源码扫描共用一个 worker 和按文件内容校验的 AST 缓存；每次仍重读文件，
修改不会复用旧树。Agent Profile job base/overlay 测试与 store 测试使用当前 schema 的
独立副本，store 的「已部署 v52 升级」用例仍执行真实迁移梯并保留旧 schema 断言。

已提交的 OpenAPI 契约是字节语义冻结契约，因此
`backend/requirements.in` 精确固定 FastAPI `0.135.3` 与 Pydantic
`2.12.4`。只能在有意重生 OpenAPI 契约并在干净环境跑 G2 扩展门时，
才同步升级这两个框架。

该 workflow 只有读权限，不接收模型或部署 secrets，并把后端 pytest worker
限制为每台 runner 4 个，避免 GitHub 托管 runner 过度抢占。后端安装设置
`HNSWLIB_NO_NATIVE=1`，G1 Python 泳道共享 G3 专用的可移植 wheel cache，使用精确的
OS/架构/Python/锁文件/策略 key，不回退到其他缓存；G2 继续禁用 pip wheel cache。
`hnswlib` 默认会用
`-march=native` 编译，把这种本机 wheel 缓存后恢复到 CPU 特性不同的托管
runner，可能以 `SIGILL` 崩溃。CI 使用可移植构建，以少量 ANN 性能换取确定性；
生产 wheelhouse 仍可按已声明的部署 CPU 定向构建。job 的 timeout 都包含依赖安装，
与 Apple Silicon 本地 warm gate 的 100 秒内目标刻意分开。两个后端 job（`level-1-backend`、
`level-2-backend`）给 30 分钟：2026-10-14 实测不分片的后端泳道在 PR CI 用 8:05、每日
run 用 11:59（4 个 worker；job 总时长 8:41 与 13:03），单个 job 没有别的 runner 可以分摊，
这个上限为增长和慢 runner 留出两倍以上余量。其余离线 job 保持 20 分钟（实测 contracts
与前端在 PR CI 5:00、每日含并行路径 7:30；补集 3:50）；G3 保持 35 分钟。这些 check 都不是
`master` 的 required status check；要设为 required，须由用户明确批准分支保护变更。

PostgreSQL 覆盖与离线门禁明确分离。`level-3-postgres-integration` job 启动 PostgreSQL 16，
通过 `bash scripts/check_postgres.sh` 选择 `postgres_integration or postgres_lane_contract`；
后者将环境自足的适配器/迁移契约与 launcher/目标安全检查纳入它所归属的泳道。
仅 CI service 将 PGDATA 放在上限 4 GiB 的 tmpfs，减少短命 schema 的宿主磁盘开销。
WAL、`fsync`、`synchronous_commit`、`full_page_writes` 保持默认值，建库时检查后三项；
job 记录存储用量、容器内存峰值与 CPU 计数，以及 PostgreSQL 16 `pg_stat_bgwriter`
中的 checkpoint 次数、写入/同步耗时和 buffer 写入计数，区分 checkpoint 压力与测试 CPU
开销。此泳道验证运行中的数据库行为，不验证容器丢失或宿主
断电后的介质持久性；生产与本地 PostgreSQL 存储不变。CI 先创建四组显式的主库、
非 C UTF8 库与非 UTF 库，再交给权限不变的最小权限应用账号。`TEST_POSTGRES_TARGETS_JSON`
是四个对象组成的数组，每个对象包含 `primary`、`non_c`、`non_utf` URL 字段；同一显式
服务端地址上的十二个数据库必须互不相同。launcher 在启动四个 pytest worker 前校验并
预检全部目标，每个 worker 仅使用自己的三个库，密码只存放在临时 pgpass 文件。
目标缺失或重复、串行与并行配置混用、未知 worker 都立即失败，禁用 worker 自动重启。
每条测试仍创建独立 schema 并执行真实迁移；数据库级隔离避免固定迁移 advisory lock
互相竞争。锁观察查询必须限定当前数据库，服务端全局活动视图不会因 schema 隔离而隔离。
batch4 的只读计划矩阵共用一份规模不变的大语料；在线安装与迁移变异测试仍保留独立 schema。
batch2 的 payload 计划矩阵只创建一次 10 万行笔记本：先检查稀有词的自然计划，再加入
其他笔记本的 2 万行命中数据并重新 ANALYZE，检查位图访问被限定在当前笔记本且本地
结果为空。两组原有计划断言仍可达，语料规模和计划检查均不缩减。

KG findings、store-evidence、id-binding 和已认证 KG 枚举的计划检查也以带场景标签的
只读矩阵运行，每份矩阵只准备一次执行过真实迁移的语料，保留全部原有结果、SQL、
计划断言和数据规模。store-evidence、id-binding 与已认证枚举的各场景使用新连接池，
避免预编译语句对照预热后续场景的缓存；录制补丁在场景之间还原。未认证反向索引世界
仍使用独立 schema。这不引入共享可变的模块级 fixture 或 PostgreSQL 迁移模板。
id-binding 的重复执行缓存测试也保留独立 schema：它会切换反向索引认证标记，不能改变
行序检查所用的已认证世界。

本地仍可使用已安装的 PostgreSQL 16 和显式 `TEST_POSTGRES_URL` 运行串行泳道，此时
不得同时设置并行 JSON 变量；权威 CI 仍要求辅助目标齐全。`scripts/check.sh` 不得启动
或连接 PostgreSQL。PG launcher 固定报告慢 setup/call/teardown，并输出
`backend/.local/postgres-junit.xml`，CI 即使失败也上传；不会把任意 `PYTEST_ADDOPTS`
转发给隔离的子进程。backend、extended-backend 与 PostgreSQL wrapper 另加载可选
`tests.ci_metrics` 插件，在 JUnit 旁写入对应的 `*-metrics.json`；CI 在原有保留七天的
artifact 中一起上传。指标包括墙钟、setup/call/teardown 的累计耗时与报告数、可用 CPU
数量、worker CPU 秒数，以及单个测试进程的最大峰值 RSS 字节数（不是并发进程峰值之和，
也不包含 PostgreSQL 服务内存）。xdist controller 对每份报告只计一次，并收集 worker
资源计数；worker 崩溃或缺失时标记进程指标不完整。阶段累计耗时是 worker 时间而非墙钟，
进程 CPU 不包含数据库服务和子进程。JSON 与终端汇总只包含数值指标和完整性标记，
不包含测试标识、环境、连接 URL、源码内容或异常文本。测量或写入失败不改变门禁退出码，
也不新增 CI 性能通过阈值。

PG 依赖缓存使用专属 portable wheel 目录及精确的
OS/架构/Python/锁文件/构建策略 key，不回退到通用缓存，仍必须设置
`HNSWLIB_NO_NATIVE=1`。整条 PG job 以三分钟为优化目标，测试全绿本身不代表达到目标，
实测需计入冷缓存时的依赖安装。
该泳道只覆盖直接 PostgreSQL 行为。2026-07 删除的几组专项测试（SQLite 适配器实现测试、
SQLite→PostgreSQL 导入/正向 shadow 测试与独立的跨后端 parity 套件）不属于当前覆盖；
删掉它们并不意味着 SQLite 后端退役，SQLite 长期保留。
当前的双后端对等由各特性在 G1 的 SQLite 用例与本泳道里的 PostgreSQL 孪生用例分别守，
外加 G1 里不连数据库的 `backend/tests/test_dual_backend_parity.py`。

CI 可移植性属于门禁契约：所有由 CI 执行的测试使用的文件系统、数据和依赖
路径都必须相对仓库，并且独立于进程 cwd。已提交 fixture 必须从其仓库文件位置
定位，禁止依赖开发机 checkout 绝对路径或 `HOME`，测试也不得读取仓库外源文档。
测试启动时直接导入的第三方包必须声明在 `backend/requirements.in` 或
`backend/requirements-dev.in` 并已锁定；干净 hosted runner 必须从两份锁文件和
`frontend/package-lock.json` 安装，并且只凭这些声明即可全绿。各 lane 时长继续输出供观察，100 秒内目标只约束已验证的 Apple Silicon
Homebrew warm gate。

依赖仓库外 PDF 解析产物的 gold 生成、构建与校验脚本仍属于 developer-only
工具并保持在 `scripts/check.sh` 之外；该例外绝不适用于已提交测试。

效率是一等工程约束。新增 LLM、embedding 或数据库调用前，必须评估能否合并、
缓存、延后、异步执行，或按用户真正进入的界面再门控触发。强一致与提前计算必须
显式选择；普通路径默认保持低开销。

跨界面的前端交互护栏保持精简但必须明确：

- 启动长任务的控件在动作发出后必须立即禁用，并显示忙碌文案或 spinner，直到该
  动作结束；服务端单飞不能替代本地防重复提交。新入口若超出有界的长任务守卫，
  必须扩展守卫或在评审中登记覆盖缺口。
- 新增居中或可拖动弹窗必须复用 `FloatingModalCard`，并经 root modal coordinator
  发布，不得另造遮罩、拖动、焦点、色板、边框或圆角行为。
- 要叠在另一个 root 弹窗之上、又不能把它顶掉的弹窗（例如全局问答窗口上的大图
  预览），在 `ROOT_MODAL_POLICIES[slot].stacksOver` 里声明这一对，并用
  `rootModals.openAbove(slot, parent)` 打开。Esc、遮罩与焦点归还随即按协调器的
  「最上层先吃」规则走；不得在父层里为子层的 Esc 写特判。一次 Esc 按键经
  `requestClose(slot, "escape")` 至多关一层（协调器按按键记账；按住 Esc 也只算一次
  按键，自动重复的 keydown 不另起一次），浏览器在 keydown
  之后补发的关闭请求关不掉刚露出来的父层。不经协调器、在捕获期自己吃掉 Esc 的层，
  对它吃掉的那一次按键调用 `claimEscapePress()`。
- 来源活动异常只能经 `sourceAnomalies()` 支撑的 `AnomalyBadge` 渲染，不得手搓
  内联警告样式或符号。
- 五档强度选择必须复用 `frontend/app/effort-picker.tsx::EffortPicker`，不得复制 range
  控件或重造其 popover；`frontend/tests/unit/effort-picker.test.mjs` 钉住共享实现及调用方。

## 开发流程

凡任务会写入仓库代码、测试、文档或配置，都必须在第一次写入前新建 linked git worktree 和分支，并在其中完成开发、验证及后续 PR；该任务期间本地主 checkout 保持只读，小修也不例外。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。纯调研、设计、状态汇报和只读审查不要求 worktree。

行动前明确预期结果与授权范围。评审和设计任务交付发现或方案，实施任务交付已验证的改动。评审请求不授权代码修改或远端交付，本地实施请求也不授权部署、对外发消息、创建 PR、合入或其他远端变更。在授权范围内自行决定常规实现选择，持续推进直到完成预期结果，并说明重要假设。只有缺失信息会实质影响正确性、范围、兼容性或授权，且无法从已有证据解决时才询问；等待回答期间继续不依赖该回答的工作。既有授权在其明确范围内持续有效，不重复索取同一批准。

**实现一个特性时，不允许把它的一部分遗留到之后再实现。**

- 计划写进去的范围在这一轮做完；范围大就拆成多个 PR **连续交付**，不砍范围。
- 实现或评审中发现的、属于该特性效果闭环的缺口，当场补进同一轮，不登记到 `fangan_todo.md`。
- 需要产品裁决的点在动手前问清，不用「先做一半」绕过。
- 子代理不得自行把范围内缺口登记为延后项；发现即报告并补齐。

对于已经批准的多步骤实施计划，默认采用 subagent-driven development：每个任务交给一个全新的实现子 Agent，并在进入下一任务前完成该任务范围内的规格符合性与代码质量审查。小任务、纯调研、设计、状态汇报和只读审查不强制使用子 Agent。只委托范围明确、文件与任务归属清晰、验收标准及验证责任明确的工作。归属不冲突的独立任务可以并发；不要为了多用代理而拆分小任务。按验收标准和具体疑点复核委托结果，不把整项任务重新做一遍。

编辑期间运行聚焦检查，声明实施完成前运行规定的标准门。两者通过后即停止验证，除非后续改动、失败或具体未解决风险需要重跑；此停止条件不豁免规定的验证通道或下述 PR 评审与 CI 检查。不要新增仅复述实现细节的测试。只读评审检查 diff 和提交方的验证证据；除非用户明确要求，否则不修改工作树，也不重跑完整门。

交付简洁并先说结果：说明改动、已运行及未能运行的检查和剩余局限，不重复执行过程。评审应注明被审版本与范围；每项发现都关联具体触发条件、影响和代码位置，区分已验证行为、静态推断与架构债务。

共享开发、验证与交付规则由本文及英文配对文档拥有。`AGENTS.md` 提供通用 Agent 入口和权威文档路由；`CLAUDE.md` 补充 Claude Code 专属常驻规则。共享规则有冲突时以对应权威文档为准并修正入口；载体专属规则只适用于其点名的载体。两个入口都不是产品或架构的副本。

Claude Code 自动加载 `CLAUDE.md`。其中最硬的 Claude 专属规则是**起子代理必须显式选模型，不得默认继承主 Agent**，按任务需要的判断力分层——需要判断力（写计划、评审、架构取舍、疑难归因）用 `opus`，规格已定死的转录型实现用 `sonnet`，纯检索定位用 `haiku`。这条由 PreToolUse 硬门 `.claude/hooks/require-subagent-model.py` 强制：没显式传 `model`、且 `subagent_type` 未在 `.claude/agents/` 中钉好模型的调用会被拒绝。`.claude/agents/` 已提供三个钉好模型的角色：`impl-task`（sonnet）、`spec-review`（opus）、`code-quality-review`（opus）。`backend/tests/test_claude_subagent_model_hook.py` 是这个 hook 的回归网：以子进程方式跑真实脚本，两个方向都覆盖——既盖「绕过」（让继承模型的调用溜过去），也盖「误拦」（把合法调用堵死，逼人绕开守卫）。

用户需要知道、或需要因此做点什么的改动，要在同一个 PR 里加一条 `release-notes/<slug>.md`：文件头写 `level`(`change` / `feature` / `fix` / `internal`)、`audience`(`all` / `admin`)和一行中文 `title`，正文可选、一两句，写给普通用户看，用[界面词汇](./ui-vocabulary.md)。只有 `change` 和 `feature` 进升级后的弹窗；`fix` 在弹窗里只计数，`internal` 只出现在「更新记录」页。一个特性只写一条：后续 PR 在说明上线前直接扩写同一个文件，不另加。纯内部改动(重构、测试、文档)不加。后端守卫会解析每条真实说明，并检查[系统更新通知](./product-and-api_zh.md#系统更新通知)里登记的长度上限。写法约定见 [`release-notes/README.md`](../release-notes/README.md)；`scripts/check_ui_vocabulary.py` 会像扫描其他用户可见文案一样扫描这些说明。

PR 在合入前必须经过 codex 评审，且**每一轮的原始输出都要逐字贴回 PR**——零意见的轮次要贴，手动补跑的轮次也要贴，并附上触发方式、完整命令、head SHA、退出码与输出字节数，便于核对评审确实跑过、结论没被转述失真。判一轮成功要**退出码为 0 且输出非空**两个条件：codex 被 SIGTERM 杀掉时退出码同样是 0，只看退出码会贴出一条空评论、看起来像通过。P0/P1 阻塞：核实后把站得住的意见修掉并重审，直到判定转为非阻塞——只有意见站不住（走下面的驳回规则）或修复方向需要人拍板时才停下来交人决定；P2/P3 不阻塞、可如实说明后不改；优先级标签解析不出来时保守拦人而不是默认放行。评审意见可以在核实后驳回（codex 评的是 diff，未必了解运行时事实），但驳回要同时给出 PR 上的理由与证据、代码里记录取舍的注释，以及钉住既有行为的回归用例。合入不再逐次征求同意：评审非阻塞**且** CI 全绿时直接 `--rebase` 合。评审仍阻塞或输出解析不出等级时一律不合——先修掉并重审；CI 未全绿、或用户说过等他自己合，同样不合。CI 判绿只认 `gh pr checks` 全部 `pass`——`mergeStateStatus: CLEAN` 只说没有东西拦着合并，不等于检查跑绿了。合入前还必须在 PR 上确认**针对 PR 远端 head（`headRefOid`）的评审已经贴出**（不能用本地 `git rev-parse HEAD`：本地落后时会命中一条旧评审而放行，而合入的是远端那个未经评审的 head）：评审自动化静默没触发，和它跑完判了通过，在外部看起来一模一样；agent 的汇报和 hook 的本地状态都不是证据，PR 上的那条评论才是。评审的自动化本身是开发者本机的 Claude Code hook、不是仓库产物，新 clone 上没有它——规则依然成立，那就手动跑；机制细节见 `CLAUDE.md`。

### 测试架构

- 与规模无关的边界分支只允许降低测试局部阈值，并另行钉住生产 floor。检查同一不可变索引/产物多个视图的断言共享一次真实构建；只验证算术或观测分支的用例走最小归属接缝，同时邻近集成覆盖仍须真实构建、打开并查询该产物。
- 后端与前端静态契约使用模块路径、限定 scope、操作种类、目标和审核后的计数等语义身份。源码位置只能作为诊断元数据；行号、offset、CSS 顺序和源码切片都不得用来标识预期站点。
- 前端测试不得再与生产代码混放：`frontend/tests/unit` 放 `node:test` 纯逻辑用例，`frontend/tests/guards` 放架构/安全/词汇/入口契约，`frontend/tests/component` 放 Vitest/jsdom/Testing Library 行为用例。共享 setup 和语义源码适配器位于 `frontend/test-support`；runner 递归收集这些目录，位置守卫会拒绝 `frontend/app` 或 `frontend/features` 中的测试。
- 组件行为不得由 CSS 几何或源码布局钉死。普通特性重构只有在可观察契约改变时才应修改测试。
- 每条新增的静态或语义守卫在合入前都必须做变异验证：删除受保护行为和把违规形态移动到另一个相关站点，两种变异都必须让守卫报红；并且要先确认变异确实命中了预期站点。记录结果后移除变异；对一条实际未生效的变异跑出绿色不构成证据。
- 已提交测试不得使用 skip/xfail/todo/only 禁用；repository policy 会同时检查测试入口及其 helper 模块，并禁止绕过共享 semantic-source 适配器直接读取生产源码。
- 前端源码策略必须保持有界：通过语法规则拒绝 AST 位置/集合顺序 API，以及源码语义命名值上的文本位置操作；共享 `semantic-source.mjs` 只能暴露 AST 语义，不能把文本切片、分行、下标或长度当作契约。不要为此实现整套 JavaScript 数据流解释器，普通数组操作仍然合法。
- backend 测试会在 xdist worker 启动前，由主进程预热一份仓库本地 Matplotlib 字体缓存。必须保留这个 controller 边界，不能让每个图谱 worker 各自重复枚举 macOS 字体。

## 文档维护

配置改动必须明确使用者和归属文档。`.env.example` 只提供常用部署选择（连接、凭据、
模型兼容、资源容量、使用及保留策略），不镜像全部 `Settings` 字段。可选默认覆盖值
使用注释赋值，让复制后的环境继续继承改进的代码默认值。高级调优、故障恢复、实验
灰度和扩展预算归入配对的部署/运维文档；独立工具参数归入 `scripts/README.md`。
默认值与校验由 `backend/app/core/config.py` 拥有，固定协议边界使用具名常量。
新增、修改、删除配置时同步对应文档，只有常用部署表面变化才同步主示例。仍接受但
已无消费者的兼容字段必须明确标记，并移出有效配置指引。测试校验解析、行为及归属
文档，不要求主示例覆盖所有字段。空值带行内注释时必须写成 `KEY="" # 说明`，保证
shell 与 dotenv 读取一致。维护模板时不得重写已有部署的 `.env`。

改动应更新所有其负责表面确实发生变化的权威文档；同一改动可能同时影响产品、部署、运维与开发表面。`product-and-api`、`deployment-and-configuration`、`operations`、`development` 的中英文版本必须同批保持一致。只有快速开始、高层当前边界或导航变化时才更新根 README 对；只有全仓 Agent 工作流/路由变化时才更新 `AGENTS.md`，只有 Claude Code 常驻规则变化时才更新 `CLAUDE.md`。测试应逐一校验相应权威文档，不得反向要求把详细事实复制进入口文件。

Claude Code 会自动加载 `CLAUDE.md`，因此 `scripts/check_claude_md_budget.py` 在 G1 contracts 泳道把它的总字符数与最长行钉成精确 baseline。文件体量发生任何变化，都必须在同一个 PR 里同步更新 baseline，避免积累无人记账的余量。特性级契约应留在其权威文档；只有 Claude Code 专属常驻规则或路由变化时才修改 `CLAUDE.md`。
