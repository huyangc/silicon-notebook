# Repository 组合式重构设计

**日期**：2026-07-10
**状态**：设计已逐段批准；等待书面规范复核
**基线提交**：3334626（origin/master）
**交付方式**：一个 PR，九个顺序 review gate；复杂 gate 可含多个 rollback commit

> **实施更正（2026-07-11，rebase 消化）**：实现期间分支 rebase 消化了 master
> 自身新增的 `_migration_10`（`kg_rebuild_checkpoint` 断点续跑表），
> `SCHEMA_VERSION` 现为 10——这是 master 的独立特性，不是本重构新增的迁移；
> 本文各处「SCHEMA_VERSION 保持 9 / 版本收敛到 9」应读作「保持基线版本、
> 不因重构而变」。冻结的 v9 fixture 由当前代码打开后合法升级到 v10
> （见 `backend/tests/test_repository_v9_fixture.py`），真实旧库的 backup-only
> 保护性验证由 `scripts/verify_repository_snapshot.py` 落地。

## 1. 决策与适用范围

本设计取代“架构渐进整改设计”中 Repository 相关的阶段 2、4、6。原方案把
Repository ports、SQLite migrations、runtime / retrieval / Ask 分成多个 PR；
用户后续决定将这些后端 Repository 整改合并为一个 PR。FastAPI router 拆分、
前端 API client 与前端 workspace 状态拆分仍是独立工作，不进入本 PR。

取代范围只包括本规范明确列出的 Repository 工作。旧阶段 4 的 Pydantic model
分文件延后为独立工作；旧阶段 6 的 FastAPI lifespan、executor shutdown 与统一应用
生命周期也延后。本 PR 的 RepositoryRuntime 是 cached repository 内部组合对象，
不改变当前 daemon/background queue 的启动或退出语义，也不新增 shutdown hook。

本 PR 是保持行为完全一致的结构重构，不是功能开发。现有 endpoint、请求与响应
schema、SQLite schema、数据内容、检索排序、Ask 持久化、后台任务和前端交互均
不得改变。

## 2. 目标

1. 把当前约 1.38 万行、同时承担 persistence、业务编排、缓存和 runtime 状态的
   SQLiteRepository 拆成可单独理解和测试的组件。
2. 让 API 和应用服务依赖按消费者划分的小型 Protocol，而不是依赖一个不完整的
   巨型 NotebookRepository。
3. 保留 SQLiteRepository 作为显式兼容 facade，使现有 API、脚本、测试和旧 import
   在迁移期间继续工作。
4. 在每个 RepositoryRuntime 内建立唯一的 SQLite connection factory、写锁和事务
   边界，避免拆分后出现每 store 一个锁或跨 store 半提交。
5. 保证重构后的代码可以直接打开、迁移并读取重构前已经创建的 SQLite 数据库，
   同时继续读取旧 JSON 向量与现有 float32 BLOB 向量。
6. 明确 SQLite 数据、文件系统索引与进程内 runtime 状态的所有权，为未来
   PostgreSQL adapter 留出真实边界，但本次不实现 PostgreSQL。

## 3. 非目标

- 不新增或删除 endpoint、字段、表、索引、列或 migration version。
- 不修正任何已知但与当前行为不同的检索、治理、取消或报告语义。
- 不改变默认 Ask mode、tier 排序、grounding、citation 或大库守卫。
- 不引入 SQLAlchemy、PostgreSQL、pgvector、依赖注入框架或新的状态库。
- 不拆 FastAPI 总 router，不重构前端，不改页面布局。
- 不以文件行数为唯一验收指标；依赖方向、状态所有权和测试隔离才是指标。
- 不用通用的 __getattr__ 代理隐藏未迁移方法，也不通过运行时 Protocol 检查兜底。

## 4. 真实行为与兼容优先级

出现历史文档与实现冲突时，按以下顺序判断：

1. 基线提交上已通过的 regression / characterization tests。
2. 被这些测试覆盖的生产代码行为。
3. README.md、README_zh.md、AGENTS.md 与 architecture.md。

以下行为是硬约束：

- API 的路径、依赖、同步/异步形态、响应模型和错误映射保持不变。
- deps.repository() 仍是缓存的单例入口，返回对象仍提供现有全部 facade 方法。
- SQLiteRepository 的公共方法名、参数默认值、返回值、异常类型和顺序保持不变。
- transport 断连不取消 detached Ask worker；只有显式 cancel 才设置取消事件。取消在
  基线现有 final-save checkpoint 前被观察到时不保存 answer；checkpoint 之后与
  _save_answer 并发到达的竞态不在本 PR 中原子化。
- 非流式 Ask 不创建 ask_jobs；流式 Ask 的 started、progress、trace、answer 与
  job 状态顺序保持不变。
- 重启时 running 的 merge-review job 变为 failed，running 的 Ask job 变为
  interrupted；报告当前没有同等恢复逻辑，本次不补。
- 所有检索 mode、federation 顺序、base exact-score tie break、relation 纯 score
  排序、ANN / FTS / full-matrix guard 和索引调度时机保持不变。
- source reparse、delete、深拷贝补偿、KG mutation version 和 cache invalidation
  的现有事务语义保持不变。
- 每个请求使用当前用户动态解析模型配置；不能在进程启动时固定成单一用户 client。

## 5. 目标依赖方向

目标结构采用 composition-root strangler：

~~~text
FastAPI routes / auth dependencies / scripts
                    |
                    v
        consumer-specific Protocols
                    |
                    v
 application services and coordinators
                    |
          +---------+----------+
          |                    |
          v                    v
   SQLite stores       cache / index / model /
          |            filesystem adapters
          v
      SqliteDatabase

Compatibility path:
existing caller -> SQLiteRepository facade -> RepositoryRuntime -> same services/stores
~~~

依赖规则：

- SQLiteRepository 可以依赖 runtime 和内部组件；任何被抽出的 service/store
  不能反向 import SQLiteRepository。
- 应用服务不能直接调用 _connect、_write 或拼接 SQL。
- silicon-notebook 主业务数据库的 SQL 只存在于 repositories/sqlite 下的 adapter，
  以及明确标记为 SQLite-only 的 maintenance adapter。独立的 LLM cache SQLite DB
  与 read-only eval DB 不属于主 repository，保留各自 adapter，并加入静态审计
  exclude/allowlist，不能被误判为越界。
- 纯算法模块继续不依赖 repository、SQLite 或 application service。
- facade 只做显式参数转发、兼容 property 和测试替换接缝，不承载领域 SQL。

## 6. 目录与组合根

目标目录如下；实施计划可以在不改变边界的前提下细化文件名：

~~~text
backend/app/repositories/
  ports.py
  sqlite/
    database.py
    migrations.py
    identity_store.py
    notebook_store.py
    sharing_store.py
    source_store.py
    embedding_store.py
    knowledge_store.py
    governance_store.py
    unified_kg_store.py
    ask_state_store.py
    report_store.py
    query_store.py

backend/app/services/
  repository.py
  sqlite_repository.py
  repository_runtime.py
  source_ingestion.py
  knowledge_lifecycle.py
  knowledge_governance.py
  retrieval_service.py
  ask_service.py
  report_engine.py
~~~

RepositoryRuntime 是内部 composition root，构造并持有一个 SqliteDatabase、全部
store、service、cache/index coordinator、model provider 和 Ask cancellation
registry，并引用独立的 process-global report cancellation registry。
SQLiteRepository 构造 runtime，并用显式方法转发保持兼容。FastAPI
deps.repository() 的签名、lru_cache 生命周期和调用方式不变。

services/repository.py 保留为兼容 import 入口，重新导出 Protocol aggregate、
UploadedSourceFile 和现有公共符号。SQLiteRepository 不显式继承 Protocol；
Protocol 使用 Python 的结构化类型约束，避免 Protocol 中省略号方法通过继承进入
运行时 MRO。

services/sqlite_repository.py 也继续重新导出当前被测试、脚本或应用模块 import 的
兼容符号，包括 SCHEMA_VERSION、_now、_new_id、_REQUEST_USER、请求用户 helper、
USABLE_STATUSES、_COPY_CHUNK 和 _remap_json_ids。实施前先用静态测试锁定完整 import
清单，不能只保留这里列举的示例。identity/sharing 的旧“必须通过 mixin 继承”结构
测试改为“facade 显式委托到 composition component”测试；方法签名和运行行为不变。

现有 production module 或运维脚本调用的 facade 私有方法，在消费者迁移后仍保留
显式兼容 wrapper。纯测试 monkeypatch 接缝只有在对应组件已有等价 characterization
且所有生产消费者已迁移时才能改为 patch 新 port；不能因内部调用绕过 facade 而让
既有行为测试失去控制点。

Task 1 同时生成 attribute read/write/descriptor audit，覆盖 settings、storage_dir、
embedder、模型 client setter、retrieval、_vector_cache、_write_lock、build sets 与
_ask_cancel_events。_COPY_CHUNK、_new_id 等当前通过 reverse import 动态解析的符号
继续 late-bound；不能在 runtime 构造时捕获常量副本，从而破坏 class/module
monkeypatch。

## 7. Consumer-driven ports

NotebookRepository 变为以下小型 Protocol 的兼容组合，而不是继续手工维护一份
扁平、遗漏大量方法的接口：

| Protocol | 主要消费者与职责 |
|---|---|
| IdentityRepository | auth、当前用户、session、用户模型配置、用户 CRUD |
| NotebookAccessRepository | owner / reader 权限守卫 |
| NotebookCatalogRepository | notebook CRUD、tier、summary、analytics、跨 metadata/source 的 notebook search |
| NotebookSharingRepository | share token、member、readonly、copy |
| SourceRepository | source 注册、查询、elements、状态、删除 |
| KnowledgeReadRepository | knowledge type/list/graph/context 与 KG object search |
| SchemaRegistryRepository | object schema CRUD 与 propose |
| KnowledgeGovernanceRepository | update、merge、duplicate、edge review、promotion、conflict |
| KnowledgeLifecycleRepository | 产品 API 使用的 KG build/rebuild/relink 与状态 |
| IndexLifecycleRepository | 产品 API 使用的 scale/viz 状态与维护触发 |
| AskStateRepository | conversations、answers、Ask jobs、traces、feedback |
| ReportRepository | report CRUD、进度、导出 |
| AdminQueryRepository | admin usage、list-user-notebooks、pending actions 与跨领域只读投影 |

应用服务另用更窄的 RetrievalPort、AskExecutionPort、ModelClientProvider、
EvidenceContextPort、CommunityQueryPort 和 notebook scope/index ports。这样
ReasoningRetriever 与 ReportEngine 只需要自己的能力集合，不再持有完整 repository。

NotebookRepository 兼容 aggregate 同时组合仍由旧 facade 暴露的 application ports
与 property contract，直到对应 route 完成迁移。Task 1 生成一份 method/property
ownership manifest：每个生产成员只能有一个 canonical owner，并明确区分
search_notebook 与 KG search、identity 管理与 admin usage、notebook analytics 与
pending-actions projection。manifest 也覆盖 settings、模型 client property、mutable
setter、mode handler 和脚本使用的私有 wrapper，不能只枚举 callable method。

deps.repository() 继续返回兼容 aggregate，但最终每个 route/helper 必须通过零运行时
开销的 typed accessor 或显式局部注解依赖指定窄 port；只有 composition root 和尚未
分类的兼容入口可以标注 NotebookRepository。此要求不拆 router、不改变 FastAPI
dependency graph 或 endpoint。

KnowledgeLifecycleRepository 与 IndexLifecycleRepository 只包含产品 API 使用的
status/trigger/read 能力。delete_notebook_kg、批量 backfill、raw build/fold helper、
diagnostic 和 eval 写入等 SQLite 专用能力归 SQLiteMaintenancePort；同名 facade
wrapper 可兼容保留，但不进入可移植 aggregate。

不使用 runtime_checkable 作为完整性证明。测试通过 inspect.signature、代表性
调用绑定和 consumer fake 验证接口。本 PR 不新增 mypy/pyright 依赖，因此不宣称
Protocol 已获得完整静态 assignability 证明；Protocol 的验收范围是消费者边界、
签名/property 清单和最小 fake，而不是 repo-wide type safety。

eval_insert_source_for_test 从 production Protocol 移除。eval/speed.py 用公开
upload_sources 加 no-op scheduler 注册文件、公开 parse_source 完成预解析，再只对
公开 extract_source 计时，最后 delete_notebook；必须保持现有“只测 KG extraction”
的计时区间与结果 schema。具体 facade 方法在本 PR 中保留为 deprecated 的显式兼容
wrapper，但它不是正式 port，不能再有新的生产消费者。

## 8. Persistence、service 与 runtime 组件

### 8.1 数据库基础层

SqliteDatabase 唯一持有：

- database URL/path 解析；
- sqlite3 connection factory 与 row_factory；
- foreign_keys、WAL、busy_timeout、synchronous、cache_size、temp_store、mmap_size；
- 一个 RepositoryRuntime / SqliteDatabase 实例级 RLock；
- read connection 与 write transaction context。

所有 SQLite store 共用同一个实例。生产 deps.repository() 是单例，因此正常进程
仍只有一个写锁。两个独立 SQLiteRepository 实例即使指向同一 DB，也像基线一样
各有自己的 Python RLock，并依赖 WAL/busy_timeout 协调；本 PR 不把它们改成跨实例
全局锁。增加双实例同库回归测试固定这一点。

SqliteMigrator 持有 SCHEMA_VERSION、migration registry、DDL 与
add-column-if-missing helper。Repository 初始化顺序固定为：

~~~text
migrate
  -> recover interrupted merge-review / Ask jobs
  -> seed and admin in-place upgrade
~~~

恢复和 seed 不进入 schema version gate；即使数据库已是最新版本也必须每次启动
执行。Migrator、recovery 和 seed 都使用 SqliteDatabase 的 connection factory，
并继续在接收请求前串行运行；不借重构改变 executescript、commit 或启动写入语义。

### 8.2 Stores

- IdentityStore：users、profiles、auth_sessions、model settings。
- NotebookStore：notebook 行 CRUD 与 tier；NotebookSummaryQuery 负责跨表汇总，
  不把 source/KG 聚合塞回纯 row mapper。
- SharingStore：token/member/access SQL；NotebookCopyService 负责跨表和文件系统
  深拷贝、分段事务与失败补偿。
- SourceStore：source、elements 和 source projection。
- EmbeddingStore / ChunkStore：element、knowledge、relation、chunk 的向量和分块
  persistence，是 ingestion、knowledge、retrieval 与 index 共用基础设施。
- KnowledgeStore：objects、relations、evidence reverse index、FTS 和 schema 数据。
- GovernanceStore：edge review、promotion、merge candidate、conflict、cluster 状态。
- UnifiedKgStore：unified state、canonical relations、mention bridge、communities。
- AskStateStore：conversations、answers、jobs、trace steps、feedback。
- ReportStore：report CRUD、outline、progress 与 export persistence。
- QueryStore：pending actions、admin usage 与其他跨领域 admin/read projection。
  notebook_analytics 由 NotebookAnalyticsQuery 作为 NotebookCatalogRepository 的
  canonical owner，即使它内部组合多个 store。

### 8.3 Application services

- NotebookScaleProfile：共享 notebook 规模事实的 lazy provider，而非每次 eager
  聚合。sharing 使用 is_copyable；answer 使用“large 且无 disk index”的
  index_required；index eligibility 保留 base/existing-index/chunk-threshold/large
  的原短路顺序；auto-index 保留 once-set 快路径。阈值、memo invalidation 和 query
  count 与当前逻辑相同。
- SourceIngestionService：上传/URL 导入、parse、状态、background embedding、KG
  extraction 与调度编排。
- KnowledgeLifecycleService：store/relink/rebuild/source-derived cleanup。
- KnowledgeGovernanceService：dedupe、merge、conflict、promotion、edge review。
- KgMutationCoordinator：统一持有 mutation sequence、unified dirty 与 cache
  invalidation，但只接管基线本来就会 bump/invalidate 的 online semantic mutation。
  deep copy、migration/fixture 写入，以及刻意保留 kg_mutation_seq 的 cluster rebuild
  等例外写入 phase matrix，不得因“统一入口”新增 dirty/version side effect。
- RetrievalService：只拥有 object/relation/chunk/element retrieval、federation、
  follow-chain 与 PPR；不拥有 answer prompt/synthesis，也不再反向委托 facade。
- EvidenceContextService：唯一拥有 evidence/source hydration、context rendering、
  anchor parsing、citation metadata 与 grounding；通过 EvidenceContextPort 同时服务
  Ask 和 report。
- AskService：mode dispatch、conversation/history、模型解析、retrieval、LLM
  synthesis/retry 与 save；持久化交给 AskStateStore。
- AskExecutionCoordinator：durable job、trace、detached worker 和 finish cleanup；
  它唯一持有 runtime 的 AskCancellationRegistry。
- RetrievalSnapshotCache：vector matrix、keyword token、membership、federated graph
  和 PPR snapshot；由 retrieval runtime 所有。
- ScaleArtifactRuntime：scale/viz LRU、HNSW handle、load/build locks、builder/fold、
  scheduler、single-flight 与 background queue；不复制 RetrievalSnapshotCache。
- ReportCancellationRegistry：保持 process-global；report_engine.py 的
  register_cancel/cancel_report/unregister_cancel 是指向同一对象的兼容 wrapper。
- ReportEngine：保留两阶段行为，但改为依赖 ReportStore、RetrievalPort、
  EvidenceContextPort、SourceCatalogQueryPort、ModelClientProvider、ModelErrorSink
  和 EventLogger。

## 9. 事务、状态和 cache 一致性

简单的单领域写操作由对应 store 使用 SqliteDatabase.write() 提交。基线中原本同一
事务的跨领域操作，由 application service 打开一个显式 transaction，并调用接受
同一 connection 的内部 store 方法；不能把一个既有事务拆成多个提前提交。反过来，
也不能把基线刻意分开的 durable checkpoints 合成一个长事务。

必须保持同一事务或补偿边界的操作包括：

- source reparse/delete 与 source-derived KG/embedding cleanup；
- knowledge object/relation 存储与 provenance；
- merge、promotion、conflict resolution 和 edge review；
- begin Ask job 时的 conversation + job 创建；
- notebook 深拷贝的现有分段提交与文件补偿。

Streaming Ask 保留以下分开的短事务和崩溃窗口，模型/retrieval 执行期间不持有
SQLite transaction：

~~~text
begin_ask_job: conversation + running job commit
  -> detached retrieval / model execution outside transaction
  -> handler saves answer in its own transaction
  -> finish_ask_job writes done + answer_id in a later transaction
  -> failed/cancelled cleanup uses its own transaction
~~~

因此进程可能在 answer 已保存但 job 尚未 finish 时崩溃；重启后的现有恢复结果必须
保持。begin_ask_job 对 payload.conversation_id 的原地赋值也保持不变。synthetic
progress/start 只发送、不写 ask_trace_steps；只有真实 reasoning trace 持久化。

KgMutationCoordinator 不把所有写路径强行原子化。Task 1 先建立按 call site 的
phase matrix，固定 store_kg 的分段提交、embedding、cache invalidation、dirty/version
bump 和 index scheduling 顺序，以及 update/merge/promotion/review 各自的 fail-open
阶段。每个 side effect 只能在它所依赖的那次 transaction 成功后发生，但不能跨越、
合并或重排基线已有的 transaction。当前分段提交并补偿的深拷贝也不伪装成长事务。

至少固定以下 operation-level boundary，并为每行增加 failure injection：

| 操作 | 必须保留的基线 boundary |
|---|---|
| process_source | parsed source/elements 先提交；chunk、embedding、extraction 继续按现有 best-effort/后续阶段运行 |
| store_kg | 继续按 1000 行分段提交；崩溃时允许出现基线已有的 partial-source 中间态，后续 embedding/invalidation/bump 顺序不变 |
| source delete/reparse cleanup | 保留当前 DB cleanup transaction，文件删除仍在 commit 后；失败记录与状态顺序不变 |
| update/merge/promotion/conflict/review | 保留各自现有 transaction 与 fail-open embedding/invalidation 阶段，不合并成通用大事务 |
| notebook deep copy | 保留 chunked commits、文件复制与 compensation sweep；copy 写入不额外触发 KG dirty |
| Ask stream | begin、answer save、job finish、failed/cancel cleanup 是前述独立短事务 |
| migration/recovery/seed | 启动前串行、各自沿用当前 connection/commit 语义，不走在线 mutation coordinator |

状态所有权固定为：

| 状态 | 所有者 |
|---|---|
| source/KG/conversation/answer/job/report/version | SQLite |
| scale CSR/ANN、viz arrays、manifest、source files | storage filesystem |
| cache、single-flight lock、build queue、cancel event、client cache | process runtime |
| request user、Ask embed memo、model errors | request ContextVar |

## 10. SQLite 旧库兼容设计

### 10.1 不变项

- SCHEMA_VERSION 保持 9。
- 不新增 migration，不更新 schema golden，不自动 rebuild table。
- DDL、migration 顺序、守卫式 ALTER 和幂等行为只搬位置，不改变语义。
- 当前 sqlite:/// URL 解析、相对路径锚点与非 SQLite URL fail-fast 保持不变。
- 表、列、index、foreign key、ID、时间字段、JSON payload 和行数据保持不变。
- 不做自动全库向量转码、JSON 重写、ID 重映射或数据“清洗”。

SQLite vector 列虽然 DDL affinity 是 TEXT，生产数据可能同时包含：

- 早期 json.dumps 写入的 JSON TEXT；
- 现在 encode_vector 写入的 little-endian float32 BLOB；
- SQLite 返回的 bytes、bytearray 或 memoryview。

新的 store 必须继续复用 encode_vector/decode_vector 双格式路径，不能假设所有旧库
已经完成 BLOB backfill。

storage_dir 下现有 source 文件、kg_index manifest、CSR、ANN、delta 与 viz artifact
格式也保持不变；当版本状态满足基线代码的加载条件时，重构代码必须直接加载已有
artifact。过期判断、allow_stale 行为和 rebuild 建议保持原样，不强制用户因代码搬移
而 rebuild。

### 10.2 自动化兼容矩阵

| 数据库形态 | 必须证明 |
|---|---|
| 由基线 master 3334626 真实创建的 v9 fixture | 新代码直接打开，schema 与去除动态时间/密码盐后的代表性 API 结果一致 |
| v9 + JSON TEXT / float32 BLOB 混合向量 | 两种格式均可检索，维度和排序不变 |
| user_version=0 但表已存在的历史库 | 幂等 migration 后数据不丢，版本收敛到 9 |
| user_version 低于对应 migration 且缺守卫 ALTER 列的旧库 | 现有 migration 补齐，非默认历史值不被覆盖 |
| running merge-review / Ask jobs | 启动恢复结果与基线一致 |
| 已有 source/KG/answer/conversation/report/share 数据 | 主键、外键和业务 payload 保持不变 |
| 已有 scale/viz artifact | 可直接加载；无格式迁移和强制 rebuild |
| 非 SQLite database URL | 与当前相同地立即报错，不静默创建本地库 |

第一项 fixture 必须在任何实现移动前，由未修改的基线代码生成并固定来源提交；测试
复制 fixture 到临时目录后再启动新 repository，绝不原地修改 fixture。fixture
package 同时保存代表性 source/scale/viz artifact、manifest 和校验摘要，SQLite
快照使用 backup/checkpoint 语义生成，不依赖未提交的 WAL sidecar。
缺列 fixture 必须把 user_version 设为引入该列之前的版本；已经标记 v9 却人为删列的
损坏数据库在基线会走 version fast path，本 PR 不新增修复逻辑。

### 10.3 真实旧库只读保护验证

最终验证还要对开发机现有 .local/silicon_notebook.db 做一次保护性验证：

1. 使用 SQLite backup API 创建一致性快照到临时目录，不直接让重构代码打开原库，
   也不遗漏 WAL 中尚未 checkpoint 的已提交数据。
2. 对原库和快照记录 schema、user_version、表级行数和主键集合摘要；不输出 source、
   prompt、answer 等私有正文。
3. 使用 offline settings、关闭 auto index/后台维护，并把 storage_dir 指向临时隔离
   目录；仅为 artifact 兼容用例复制所需文件，绝不用 symlink 指回原 storage。
4. 只在 SQLite 快照和临时 storage 上启动新 repository 并执行代表性只读 API。
5. 对比启动前后数据；仅允许当前基线本就存在的 recovery、seed 和 admin 密码重置
   写入，其他表的 ID、行数和 payload 摘要不得变化。
6. 对原 storage 的文件清单、size 与 mtime 做前后核对；删除临时快照/临时 storage
   不影响原数据。任何差异都阻断 PR 发布。

## 11. 错误与生命周期处理

- Store 默认不把 KeyError、ValueError、NotImplementedError 或 sqlite3 异常重新
  包装成新类型；现有 application/API 层继续负责 HTTP 映射。
- “Store 默认抛出”不覆盖基线已有的 fail-open/no-op 契约。Task 1 建 error-policy
  matrix，至少固定 append_ask_trace 写失败不终止 Ask、report corpus-map 查询失败
  回退、model-error 记录失败不影响主流程，以及 update/delete missing report 当前
  不检查 rowcount 的 silent no-op。对应 coordinator/store API 要显式表达这些策略，
  不能用一个通用严格 CRUD helper 改变它们。
- 找不到 notebook/source/knowledge/job 时，保留当前异常类型、detail 和是否隐藏资源
  存在性的 404 行为。
- background worker 继续通过现有 EventLogger 记录错误，不向日志输出向量或私有全文。
- Ask 保留基线现有 final-save 前检查；transport disconnect 只停止 delivery。
  本 PR 不承诺消除“检查后、写入前”到达的取消竞态。
- begin/finish Ask job 必须继续共同协调 durable row、conversation 和 process-local
  cancellation event。
- Scale/index 构建继续使用 single-flight、原子 artifact swap 和现有失败回退；
  retrieval 请求不能同步承担整库维护。
- 构造失败不能留下半初始化的 alternate database 或悄悄回落到默认路径。

## 12. 单 PR 的九个实施任务

每个 review gate 遵守 RED -> GREEN -> targeted regression -> review -> commit。
RED 可以是新增 module contract、consumer fake、旧库 fixture 或行为
characterization 在新边界尚未实现时失败。每个提交保持 facade 可用，并运行影响
范围测试。Ask/report gate 必须拆出多个 rollback commit；“九个 gate”不是强迫每个
gate 只有一个巨型 commit。

1. **Ports、规模策略与行为护栏**
   - 建 ownership manifest、consumer-driven Protocol、兼容 aggregate 和 lazy
     NotebookScaleProfile。
   - 增加 facade surface、signature、OpenAPI/serialization、旧 master DB fixture
     与缺失 Ask golden characterization；建立 transaction/mutation/error phase
     matrices。
2. **SqliteDatabase 与 migrations**
   - 搬移 path/connection/write lock、SCHEMA_VERSION、migration registry、recovery、
     seed；保留 facade compatibility wrappers。
   - 把 request/job ContextVar 移入 repository-independent context module，并从
     sqlite_repository.py 继续 re-export，先消除 background_jobs 对 facade 的反向
     import。
3. **Identity、notebook、sharing 与 projections**
   - 抽 store、NotebookSummaryQuery、NotebookCopyService、admin/read projections；
     保留权限、copy 分段事务和 monkeypatch 接缝。
4. **Source、embedding/chunk 与 ingestion**
   - 先抽共享向量 persistence，再抽 source store 和 ingestion service；保持 scheduler、
     background embedding、状态时序和 cleanup。
5. **Knowledge、governance 与 unified KG**
   - 抽 object/relation/provenance/schema、治理、cluster/community stores 和 service；
     按 phase matrix 接入 KgMutationCoordinator。此时 invalidation port 先适配原有
     cache 对象，Task 6 只转移所有权，不创建第二份 cache。
6. **Scale/viz index 与 runtime caches**
   - 分开抽 RetrievalSnapshotCache 与 ScaleArtifactRuntime；抽 artifact store、
     catalog、builder/fold、scheduler、LRU/single-flight state，保持对象 identity、
     manifest、atomic swap 和现有 invalidation key。
7. **Retrieval 与 answer context**
   - 反转现有 RetrievalService 依赖，使其消费 store/index/model ports；逐族迁移
     element、object、relation、chunk、graph/PPR。
   - 单独建立 EvidenceContextService；RetrievalService 不吸收 answer synthesis。
8. **Ask、jobs、conversations 与 reports**
   - rollback commit A：Ask/answer/conversation/trace 与 Report stores 先通过 facade
     delegation 落地。
   - rollback commit B：AskExecutionCoordinator 与 AskCancellationRegistry，保持
     started/progress/trace、payload mutation、answer-before-finish 和断连/取消顺序。
   - rollback commit C：AskService/mode engines 与 ReasoningRetriever 改用 ports。
   - rollback commit D：ReportEngine 改用 report/retrieval/evidence/source/model/
     observability ports，并保留 process-global report cancel wrappers。
9. **Facade 收口、调用方迁移与文档**
   - SQLiteRepository 变为显式薄 facade；迁移 batch/eval/maintenance 调用；
     扩展静态 SQL/write 审计；同步 README.md、README_zh.md、AGENTS.md、
     architecture.md 与 fangan_done.md；让 architecture documentation test 固定新旧
     规范的 supersession；完整复验并创建一个 PR。

不允许先删除兼容方法再批量修消费者。每一步先让 facade 转发到新组件，待全部调用
迁移并有结构测试后才收口旧内部接缝。

## 13. 测试与质量闸门

### 13.1 结构契约

- NotebookRepository aggregate 覆盖所有 route/auth 实际使用的方法。
- 每个 consumer fake 只实现对应窄 port。
- SQLiteRepository 不继承 Protocol，不使用 __getattr__。
- application services 不 import SQLiteRepository，也不调用 _connect/_write。
- SQL/write 静态审计覆盖全部 repositories/sqlite 文件，不再只扫描旧单文件。
- facade 不含领域 SQL，所有公开转发方法保持 inspect.signature 兼容。
- sqlite_repository.py 与 services/repository.py 的既有外部 import 符号有静态清单
  和 re-export 测试；不因移动实现而删除脚本仍在使用的 helper。
- route/helper ownership manifest 最终没有未分类成员；除 composition root 和兼容
  adapter 外，生产消费者标注指定窄 port。
- facade 的 retrieval、model/embed/rerank client、cancel registry、cache/index
  compatibility properties 对外暴露同一个 mutable runtime object，setter 写回同一
  对象，不返回 snapshot/copy。
- attribute/descriptor audit 覆盖所有 production/test/script 读写；_COPY_CHUNK、
  _new_id 等已知 reverse-import seam 保持 late-bound。

### 13.2 行为回归

- identity/session/model settings、owner/readonly/share/copy。
- source upload/URL/parse/reparse/delete、并行 embedding、KG extraction。
- streaming Ask、KG scheduler、report section worker 经 contextvars.copy_context()
  线程切换后仍使用发起用户的 model settings；断连不清除 detached worker context。
- object/relation/schema/governance/promotion/conflict/unified KG。
- retrieval score/order、federation、follow-chain、PPR、ANN/FTS 与大库 guard。
- Ask 三种 mode 的 AskResponse 与 answers.payload golden、stream 事件顺序、disconnect、
  cancel、reconnect、restart recovery。
- Ask 明确锁定：synthetic start 不持久化、真实 trace 才入库、payload 的
  conversation_id 原地更新、answer commit 早于 job finish，以及 final-check 后取消
  竞态不被本 PR 原子化。
- report planning/generation/progress/cancel/export，以及“重启不恢复 report”的当前行为。
- report 的 process-global cancel module API、corpus-map fail-open 与 missing report
  silent no-op；append_ask_trace 写失败继续不影响 Ask。
- cache version、query count、single-flight、scale artifact load/fold/atomic swap。
- one-hop neighbors 当前不额外过滤 rejected edge；graph BFS 的 source-chunk hydration
  仍只取 active notebook；graph mode 先尝试 PPR 再执行大图拒绝；chunk KG overlay
  在 seed/object/relation retrieval 前执行 federated-size guard；rustworkx PPR
  fallback 只允许小图。上述已知不对称不在重构中“统一”。

### 13.3 性能与异步守卫

- notebook/source summary 等 projection 的 query count 不增加。
- NotebookScaleProfile 的 lazy facts 和 once-set 快路径不得把完整 copy-stat aggregates
  引入 retrieval/status 热路径。
- ANN 路径不加载完整向量矩阵，不做无界 relation/chunk hydration。
- full scale build 继续绕过 query-time vector cache，并复用同一次 KG HNSW build
  完成 synonym KNN 与持久化 ANN，避免多份 GB 级矩阵/索引常驻。
- Ask 不同步 build scale index、重建 unified KG 或全库 backfill embedding。
- 进程内 cache 保持有界，per-notebook locks 不形成全局锁等待环。
- 同一 runtime 内的并发 writer 继续共享一个 RLock；不同 repository 实例保持基线的
  独立锁语义，现有 WAL/busy_timeout 测试保持绿色。

### 13.4 每任务与最终命令

每个任务先运行新增测试与受影响领域测试，再运行完整：

~~~bash
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
cd frontend
npm run build
~~~

PR 发布前必须：

1. 合入最新 origin/master 的真实三方 merge。
2. 解决冲突后重新运行全部 compatibility、backend、frontend tests。
3. 对真实旧库快照运行保护性验证。
4. 进行最终 code review；任何行为、schema、旧库或性能差异都阻断发布。

基线证据：在 3334626 的隔离 worktree 中，完整门禁已通过：
2281 passed、1 skipped，前端 146 tests、TypeScript 与 Next.js build 均通过。

## 14. 完成标准

只有同时满足以下条件才算完成：

- 所有 endpoint、schema、序列化结果、异常和异步语义与基线一致。
- 基线 master fixture、历史 unversioned fixture 和真实旧库快照均能被新代码读取。
- SCHEMA_VERSION 仍为 9，schema golden 无变化，未发生自动数据重写。
- SQLiteRepository 是显式 compatibility facade；无 __getattr__、无业务 SQL、无
  Protocol runtime inheritance。
- 新 service/store 的职责、依赖和 state ownership 能从接口直接理解并单测。
- 所有并发业务写操作共享同一 database/write-lock boundary；启动 migration/seed
  保持既有串行语义。既有原子操作不被拆出新 partial commit，既有 chunked/best-effort
  checkpoint 也不被擅自合并。
- retrieval/Ask/report 不再依赖 facade private API。
- README.md、README_zh.md、AGENTS.md、architecture.md、fangan_done.md 与实际结构
  同步，文档契约测试覆盖 supersession。
- 最新 master merge 后 scripts/check.sh、frontend build、旧库验证和最终 review
  全部通过。
- 所有变更以一个 PR 交付；失败时可整体 revert，旧数据库不需要回滚 migration。
