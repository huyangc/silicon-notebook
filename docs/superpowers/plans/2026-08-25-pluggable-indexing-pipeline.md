# 可插拔索引管线实施计划（按 PR 拆分）

日期：2026-08-25
状态：已完成（2026-08-25）。PR-1 的扩展点、notebook selection、pending 写入闸、
前端设置入口与代次投影，以及 PR-2 的 KG prompt/mapper、证据 handle 准入和 durable
notebook staging 均已落地。全库来源的 chunk/KG/extraction payload 只在 stage 中可见；
单个 SQLite/PostgreSQL 发布事务经 job/generation/source-snapshot CAS 后才一起替换 live
产品并发布 identity。失败、取消、恢复与迟到 worker 只清 stage，旧 live generation 保持可读。

## 0. 范围与当前约束

本计划对应规格：

- `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/arxiv-search-plugin-design-47e3d5/docs/superpowers/specs/2026-08-25-pluggable-indexing-pipeline-design.md`

执行边界：

- parser 不进入可选范围：继续使用启动冻结的 ProviderChain，用户只选 indexing pipeline。
- embedding 不插件化：插件只可影响 chunking 策略与 KG 抽取策略；向量模型、embedding 写入、FTS/ANN、schema admission 都归核心。
- 插件有策略权，核心有 schema / admission / write 权：插件不能写库、不能拿 repository/service locator、不能绕过 source lock、不能绕过 KG evidence/schema 校验。
- 每个用户可见能力必须同 PR 带前端入口；实现已同步 README/README_zh/AGENTS/CLAUDE 与 owning docs。

当前 worktree 已有其它改动，索引管线 PR 不得覆盖：

- `backend/app/services/remote_sources.py`
- `frontend/features/extension-sdk/ui.tsx`
- `frontend/app/globals.css`
- `examples/extensions/arxiv-search/ui/arxiv-search/workspace-plugin.tsx`
- `frontend/tests/component/extension-ui-kit.component.test.tsx`
- `frontend/tests/guards/extension-ui-kit-style-guard.test.mjs`
- `frontend/tests/guards/arxiv-sample-panel-guard.test.mjs`
- `analysis.md`
- `scripts/oneoff_pg_performance_audit.sh`

进入 PR-1 前先跑动态核验，而不是死认今天的编号：

```bash
git status --short
rg -n "SCHEMA_VERSION =" backend/app/repositories/sqlite/migrations.py
rg -n "PostgresSchemaManifest|postgres_version|RUNNING_SCHEMA_PAIR" \
  backend/app/repositories/postgres/schema_manifest.py \
  backend/app/migration/shadow/manifest.py
ls backend/app/repositories/postgres/migrations
```

实施时采用的迁移编号：

- SQLite 使用 `_migration_58` 并升到 `SCHEMA_VERSION = 58`。
- PostgreSQL 使用 `0036_pluggable_indexing_pipeline.sql` 并升到 36。
- shadow pair 同步到 SQLite 58 / PostgreSQL 36。

## 1. 已审计接缝

### 1.1 Extension runtime

相关文件：

- `backend/app/extension_sdk/contracts.py`
- `backend/app/domain/extensions.py`
- `backend/app/extensions/registry.py`
- `backend/app/extensions/bootstrap.py`
- `backend/app/bootstrap.py`
- `backend/app/extensions/admin_projection.py`
- `backend/app/api/system_routes.py`
- `backend/app/api/admin_routes.py`

当前 registry 规则：

- `ContributionKind.PROVIDER` 在同一个 point 下只能有一个 provider。
- 多个 pipeline 选项不能直接用同一个 `indexing.pipeline` point + 多个 `PROVIDER`，否则 `ExtensionRegistry.freeze()` 会拒绝启动。
- 推荐落地：SDK/domain 层可以把概念命名为 provider，但 registry kind 用 `CONTRIBUTOR` 承载“多个可选 descriptor”；或者在 PR-1 显式扩展 registry 的多 provider 语义。为减少共享基座冲突，PR-1 优先用 `CONTRIBUTOR`。

管理投影：

- `/admin/extensions` 已只读展示 runtime contributions / UI contributions；新增 `indexing.pipeline` contribution 后应自然出现在 admin 页面。
- 该页面不能泄露 endpoint、path、settings、exception、capability 内部原因。

### 1.2 权限与前端入口

相关文件：

- `backend/app/api/deps.py`
- `backend/app/api/notebook_routes.py`
- `frontend/app/workspace-transitions.ts`
- `frontend/app/use-notebook-collection.ts`
- `frontend/app/page.tsx`
- `frontend/app/notebook-api.ts`
- `frontend/app/workspace-model.ts`
- `frontend/features/kg-maintenance/kg-api.ts`
- `frontend/app/use-kg-graph.ts`

当前能力格：

- `kg:write` 是 admin 档，适合“索引/图谱构建、重建、切换后重建”。
- `notebook:configure` 是 owner-only，不能用于索引管线选择；否则组管理员有内容管理权但看不到/不能改索引管线。
- `notebook:manage` 是 admin 档，但当前“笔记本设置”弹窗入口被 `canConfigureNotebook` owner-only 门控，且里面包含参考库挂载（owner-only）。因此不能简单把索引管线 section 塞进现有设置弹窗就结束。

PR-1 前端决策：

- 管线选择 UI 归工作区内的笔记本设置/索引设置，但入口必须对 `canManageContent` 可见。
- 如果复用现有 `notebook-editor` root modal，必须拆权限：
  - `canManageContent` 可打开基本信息 + 索引管线；
  - 参考库挂载、公开链接等仍只在 `canConfigureNotebook` 时渲染并发起 I/O；
  - group admin 打开设置时不得触发 mountable/bases 读取。
- 也可以新增一个窄的“索引管线”设置卡片/弹窗，但必须接入 root modal owner 与稳定 owner token，不能用局部裸 state 绕开 `use-root-modal-coordinator`。

### 1.3 Chunking 写入链

相关文件：

- `backend/app/services/source_ingestion.py`
- `backend/app/services/source_chunking.py`
- `backend/app/services/chunking.py`
- `backend/app/repositories/ports.py`
- `backend/app/repositories/sqlite/chunk_store.py`
- `backend/app/repositories/postgres/chunk_store.py`
- `backend/app/repositories/chunk_elements.py`

当前流程：

- `SourceIngestionService.process_source()` 在 per-source lock 内完成 materialize、asset replacement、`replace_elements`、`clear_chunked_at`、summary/meta、`chunking.build_chunks_for_source()`。
- `SourceChunkingService.build_chunks_for_source()` 读取 `source_elements_for_chunking(source_id)`，调用纯函数 `build_chunks(...)`，把结果转为 `ChunkWrite`，再由 `replace_source_chunks()` 原子替换该 source 的 chunks + chunk_elements + chunked_at。
- `replace_source_chunks()` 是双后端关键写口：SQLite 同步维护 FTS，Postgres 依赖索引/触发器；两边都使用 `chunk_elements.reverse_rows_for_writes()`。

PR-1 必须保持：

- 插件策略只参与 `build_chunks` 与 `ChunkWrite` 之间的纯计算，不拿 DB/HTTP/model port。
- source lock 不变，不能让 parser/materializer 与 chunk 写入分离成两个并发窗口。
- malformed proposal fail-closed 到 builtin chunker，并产生可见但安全的 warning code。
- proposal 上限来自 `Settings`，生产路径不能新增结果改变型魔数切片。

### 1.4 KG build / rebuild job

相关文件：

- `backend/app/services/knowledge_lifecycle.py`
- `backend/app/repositories/sqlite/kg_build_job_store.py`
- `backend/app/repositories/postgres/kg_build_job_store.py`
- `backend/app/api/kg_routes.py`
- `frontend/features/kg-maintenance/kg-api.ts`
- `frontend/app/use-kg-graph.ts`

当前 durable job 已存在：

- `kg_build_jobs` 支持 `mode="incremental" | "rebuild"`，同一 notebook 只允许一个 running job。
- `/notebooks/{id}/kg/build` 和 `/notebooks/{id}/kg/rebuild` 均使用 `kg:write`。
- 前端 `use-kg-graph.ts` 已负责 KG build/rebuild 的 claim、poll、terminal toast。

PR-1 切换管线时应复用这个 durable job 能力，不新建第二套 rebuild job。若还需要重建 scale index，必须接入现有 scale-index 状态/重建链，不要在前端开一个互相不知道的轮询器。

### 1.5 Product identity / scale artifact

相关文件：

- `backend/app/repositories/sqlite/unified_kg_store.py`
- `backend/app/repositories/postgres/unified_kg_store.py`
- `backend/app/repositories/sqlite/index_projection_store.py`
- `backend/app/repositories/postgres/index_projection_store.py`
- `backend/app/services/scale_artifact_runtime.py`
- `backend/app/services/scale_index_builder.py`
- `backend/app/repositories/filesystem/scale_artifact_store.py`

当前 scale identity：

- `ScaleArtifactRuntime.version(notebook_id)` 由 `IndexProjectionStore.version_facts()` + settings tail + `EDGE_SCHEMA_VERSION` 组成。
- `version_signal()` 只读 `unified_kg_state` 与 settings tail，用作 memo key。
- manifest stale 判定是 `manifest.version != runtime.version(notebook_id)`。
- fold/rebuild 的选择逻辑在 `scale_index_builder.py` 与 runtime status 之间协作。

PR-1 必须新增 pipeline identity：

- `pipeline_id` 与 `pipeline_version` 必须进入 KG product identity、`unified_kg_state`、scale manifest version facts。
- 当 manifest identity 与当前已发布 KG identity 不一致时，scale rebuild 必须走 full rebuild，不可 fold；否则会把不同 pipeline 的 graph/vector 产品混合。
- `version_signal()` memo key 必须能感知 pipeline identity 变化，否则 stale 会被缓存遮住。

### 1.6 迁移、双后端、shadow

相关文件：

- `backend/app/repositories/sqlite/migrations.py`
- `backend/app/repositories/postgres/migrations/*.sql`
- `backend/app/repositories/postgres/schema_manifest.py`
- `backend/app/migration/shadow/manifest.py`
- `backend/app/migration/sqlite_to_postgres.py`
- `backend/tests/postgres/test_migrations.py`
- `backend/tests/test_postgres_schema.py`
- `backend/tests/test_sqlite_postgres_repository_conformance.py`（或当前同类 conformance tests）

PR-1 schema 预期：

- `notebooks.indexing_pipeline TEXT NULL`，`NULL` 表示 builtin。
- 不新增业务表、FK、unique。
- shadow manifest 加 `notebooks.indexing_pipeline` 列映射；business table list 不变。
- 如果新增 `unified_kg_state` product identity 列，SQLite/PG/shadow 也必须同步；若用现有 JSON/manifest 不能满足查询与 memo，优先加显式列。

## 2. PR-1：管线选择 + chunking strategy + identity + rebuild 闭环

### 2.1 测试先行

先写失败测试，再实现。

后端测试建议：

- `backend/tests/test_indexing_pipeline_registry.py`
  - 多个 pipeline contributions 可同时注册；
  - `pipeline_id` 必须以 `plugin_id + "."` 前缀命名，`builtin`/空 id/跨插件前缀被拒绝；
  - descriptor 只允许稳定 label/description/version 与两个 override flag；
  - 同一 `pipeline_id` 重复 fail-closed at startup；
  - 当前 registry 不可被误用成单 provider point。
- `backend/tests/test_indexing_pipeline_notebook_api.py`
  - `GET /notebooks/{id}/indexing-pipeline` 对 reader 可读，只返回 label/description/version/flags/availability/missing，不返回内部 reason、path、capability、exception；
  - `PUT /notebooks/{id}/indexing-pipeline` 走 `kg:write`，owner 与 group admin 可写，reader 不可写；
  - `pipeline_id=null` 选择 builtin；
  - no-op 不触发 rebuild；
  - 真实切换写入 selection、标记 pending、启动/复用 KG rebuild job；
  - 已选插件缺席时：读 notebook 与旧 artifacts 不失败；新 upload/reparse/KG build/scale rebuild 写 409；revert builtin 可成功。
- `backend/tests/test_indexing_pipeline_chunking.py`
  - builtin chunker 路径字节级保持现有行为；
  - 插件 chunk strategy 只收到 immutable source element view；
  - proposal 产生 `ChunkWrite` 后仍走 `replace_source_chunks()`；
  - malformed/越界/异常 proposal fail-closed 到 builtin，并记录 sanitized warning code；
  - plugin 不能触发模型调用或 I/O port。
- `backend/tests/test_indexing_pipeline_identity.py`
  - `unified_kg_state` product identity 与 scale manifest version facts 包含 `(pipeline_id, pipeline_version)`；
  - pipeline version 变化让 scale status stale；
  - identity mismatch 禁止 fold，强制 full rebuild；
  - successful rebuild 才发布新 product identity；失败不覆盖旧 identity。
- 双后端/迁移：
  - SQLite fresh DB + migration from 57；
  - Postgres migration from 35；
  - `POSTGRES_SCHEMA_MANIFEST` 与 packaged migration count 同步；
  - shadow manifest pair 与 column transforms 同步；
  - repository conformance 覆盖 notebook selection get/set。

前端测试建议：

- `frontend/tests/unit/indexing-pipeline-settings.test.mjs`
  - `canManageContent=true` 时可见索引管线入口；
  - group admin 可打开索引设置，但不会发起参考库 mountable/bases I/O；
  - `canConfigureNotebook=false` 时参考库 section 不渲染；
  - 切换管线显示“将重建全库索引”的确认文案；
  - selected plugin missing 显示警告和“切回内建”动作；
  - owner token 过期/切库后，旧请求不能 commit visible state。
- `frontend/tests/guards/workspace-owner-transition-guard.test.mjs`
  - 若新增 owner hook，activate/leave fanout 只能在 AGENTS 指定的三个入口。
- 如果改动 `frontend/features/extension-sdk` UI/registry contract，运行并更新：
  - `scripts/generate_ui_extension_contract.py`
  - `backend/tests/fixtures/ui_extension_contract.json`

验收命令：

```bash
python -m pytest \
  backend/tests/test_indexing_pipeline_registry.py \
  backend/tests/test_indexing_pipeline_notebook_api.py \
  backend/tests/test_indexing_pipeline_chunking.py \
  backend/tests/test_indexing_pipeline_identity.py

python -m pytest backend/tests/postgres/test_migrations.py backend/tests/test_postgres_schema.py
cd frontend && npm run test -- indexing-pipeline-settings
scripts/check.sh
cd frontend && npm run build
```

### 2.2 后端文件级实现

新增/修改：

- `backend/app/domain/extensions.py`
  - 增加 `INDEXING_PIPELINE_POINT = "indexing.pipeline"` 与核心端口类型，供 services 使用，避免 services import SDK runtime。
  - 增加 `IndexingPipelineDescriptor`、`IndexingPipelineSelection`、`ResolvedIndexingPipeline` 等小 dataclass。
- `backend/app/extension_sdk/indexing.py`
  - dependency-light SDK：descriptor、chunk strategy context、chunk proposal、strategy protocol。
  - 不导入 settings/repository/API/service。
- `backend/app/extension_sdk/__init__.py`
  - re-export indexing SDK surface。
- `backend/app/extensions/indexing.py`
  - 实现 `IndexingPipelineHost`：
    - 启动时冻结 descriptors；
    - 校验 `pipeline_id` 前缀、version、flags、重复；
    - request-time availability 只返回 available/unavailable，不泄露原因；
    - 提供 `options()`、`resolve(pipeline_id)`、`build_chunks(baseline, context)`。
- `backend/app/extensions/bootstrap.py`
  - 将 `IndexingPipelineHost` 挂进 `ExtensionRuntime`。
  - builtin pipeline 作为核心 fallback，不必伪装成插件；API 层用 `pipeline_id=null` 表示。
- `backend/app/extensions/admin_projection.py`
  - 确认 `indexing.pipeline` contribution 出现在 `/admin/extensions`，不额外暴露 strategy internals。
- `backend/app/core/config.py`
  - 增加 plugin chunk proposal bounds，例如 max proposals、max text chars、max element refs；具体数值只在 paired product/API docs 记录。

API/model/storage：

- `backend/app/models/notebooks.py`
  - `NotebookSummary` 增加只读投影：当前 selection、missing/pending/stale 状态；不要把 plugin internals 塞入 summary。
  - 新增 request/response model：`IndexingPipelineOptionsResponse`、`IndexingPipelineUpdateRequest`、`IndexingPipelineUpdateResult`。
- `backend/app/api/notebook_routes.py`
  - `GET /notebooks/{notebook_id}/indexing-pipeline`：`require_notebook_read`。
  - `PUT /notebooks/{notebook_id}/indexing-pipeline`：`require_notebook_capability("kg:write")`。
  - 写入时冻结当前 descriptor/version，no-op 直接返回；变更时确认并启动/复用 full rebuild。
- `backend/app/repositories/ports.py`
  - `NotebookStorePort` 增加 selection get/set。
  - 如果 product identity 加到 `UnifiedKgStorePort`，同步声明。
- `backend/app/repositories/sqlite/notebook_store.py`
- `backend/app/repositories/postgres/notebook_store.py`
  - 读写 `notebooks.indexing_pipeline`。
- `backend/app/services/notebook_catalog.py`
  - `NotebookSummaryQuery.from_row()` 投影 selection 与 missing/pending 状态。

迁移：

- `backend/app/repositories/sqlite/migrations.py`
  - 如果动态核验仍是 57，则新增 `_migration_58`：
    - `ALTER TABLE notebooks ADD COLUMN indexing_pipeline TEXT NULL`
    - 如 PR-1 决定显式列，给 `unified_kg_state` 加 `indexing_pipeline_id` / `indexing_pipeline_version`。
  - `SCHEMA_VERSION = 58`。
- `backend/app/repositories/postgres/migrations/0036_pluggable_indexing_pipeline.sql`
  - 对应 add column。
- `backend/app/repositories/postgres/schema_manifest.py`
  - `POSTGRES_SCHEMA_MANIFEST = PostgresSchemaManifest(sqlite_version=58, postgres_version=36)`。
  - `POSTGRES_BUSINESS_TABLES` 不变；如果只加列，不动 rowid ordinal list。
- `backend/app/migration/shadow/manifest.py`
  - `RUNNING_SCHEMA_PAIR = SchemaPair(sqlite_version=58, postgres_version=36, epoch=1)`。
  - `notebooks.indexing_pipeline` 列 transform 为 identity。
  - 若 `unified_kg_state` 加 identity 列，也同步列清单。

写入/重建 admission：

- 新增 `backend/app/services/indexing_pipeline.py`
  - `pipeline_options_for_notebook(actor, notebook_id)`
  - `set_notebook_pipeline(notebook_id, desired_pipeline_id)`
  - `require_pipeline_write_admission(notebook_id, operation)`
  - `resolved_chunk_strategy_for_source(source_id)`
  - 集中处理 missing plugin、pending switch、desired/published identity。
- `backend/app/services/source_ingestion.py`
  - upload/reparse 入队前与 processing 前均调用 admission。
  - plugin missing 或 pending switch 时 HTTP 写返回 409；后台路径记录 sanitized failure，不静默 fallback。
- `backend/app/services/source_chunking.py`
  - 在 `build_chunks_for_source()` 中解析当前 pipeline strategy；
  - builtin path 保持现状；
  - plugin proposal 校验后仍落到 `ChunkWrite` + `replace_source_chunks()`。
- `backend/app/services/knowledge_lifecycle.py`
  - PR-1 只负责 rebuild 调度与 product identity 发布；KG prompt/mapper 留到 PR-2。
  - `mode="rebuild"` 成功后才更新 `unified_kg_state` published pipeline identity。
- `backend/app/services/scale_artifact_runtime.py`
- `backend/app/services/scale_index_builder.py`
- `backend/app/repositories/sqlite/index_projection_store.py`
- `backend/app/repositories/postgres/index_projection_store.py`
  - version facts/signal 加 pipeline identity；
  - mismatch 禁止 fold，走 full；
  - status payload 给前端足够提示，但不泄露 plugin internal reason。

### 2.3 “旧读继续、新写 fail-closed”的实现要点

规格要求“重建完成前读取不受影响，写入 409 / 不混用”。当前 chunks 是按 source 破坏式替换，因此 PR-1 必须明确避免 source-by-source 混合读。

推荐策略：

1. `notebooks.indexing_pipeline` 表示 desired selection。
2. `unified_kg_state` / scale manifest 表示 published product identity。
3. `desired != published` 时：
   - UI 显示“索引管线切换待重建/重建中”；
   - 普通 source upload/reparse、manual KG build、scale fold/rebuild 写入 409，除非它是本次 switch 启动的 authorized rebuild；
   - reads 继续使用 published identity 的旧产物。
4. 对 chunk rebuild：
   - 不允许在后台逐 source 调 `replace_source_chunks()` 并让在线读取看到一半旧、一半新；
   - PR-1 必须选择以下一条并测试：
     - A. 先计算并校验全 notebook proposals，再在一个双后端事务内替换所有相关 chunks/chunk_elements/chunked_at；
     - B. 给 chunk 产品引入 generation/pipeline identity 并让 retrieval 只读取 published generation。
   - 若选择 A，必须用 Settings bounds 防止内存爆炸；若超界，rebuild fail-closed，published identity 不变。
   - 若选择 B，超出当前 spec 的“只给 notebooks 加列”范围，需在 PR 描述中说明为什么它是满足“不混用”的必要扩展。

这个点是 PR-1 的主要风险；不解决就不能声称“reads unaffected and no mix”。

### 2.4 前端文件级实现

新增/修改：

- `frontend/app/workspace-model.ts`
  - 增加 notebook summary 的 indexing selection/status types。
- `frontend/app/notebook-api.ts`
  - 新增 `fetchIndexingPipelineOptions(notebookId)`、`updateIndexingPipeline(notebookId, payload)`。
- `frontend/app/use-notebook-collection.ts`
  - 如果复用 notebook editor：扩展 editor state，按 owner token 管理 pipeline options/read/write。
  - 确保 group admin 打开设置不触发 mountable/bases I/O。
- `frontend/app/page.tsx`
  - 设置入口门控改为 `canConfigureNotebook || canManageContent`。
  - 参考库 section 继续 `canConfigureNotebook` 才渲染。
  - 新增“索引管线” section：
    - select/radio 展示 builtin + plugin options；
    - descriptor 只展示 label/description/version/flags；
    - missing selected 显示 warning + “切回内建”；
    - 切换前二次确认“将重建全库索引”。
  - 成功后刷新 current notebook，并让 KG/scale 现有状态区域看到 rebuild 状态。
- `frontend/features/kg-maintenance/kg-api.ts`
  - 如后端把 switch 结果返回 KG job id，补充 narrow type；不要把 pipeline write 塞进 `use-kg-graph.ts` 的 KG domain owner，除非同时更新 AGENTS 约束。
- `frontend/app/system-api.ts`
  - 通常不需要承载 per-notebook pipeline options；只有如果 options 改走 `/system/config` 才修改。

owner 注意事项：

- 不引入 global store。
- 不把 notebook settings state re-flatten 到 `Home` 顶层局部别名。
- 异步 options/write 响应必须校验 actor/notebook/workspace owner token。
- 如果新增 owner hook，更新 `activateWorkspaceOwners` / `leaveWorkspaceOwners` / `leaveActorOwners`，并补稳定 empty reference。

## 3. PR-2：KG extraction prompt/mapper strategy

依赖：

- PR-1 已合并，pipeline selection、availability、missing 409、published identity、rebuild job 流程稳定。
- ask.engine PR-α/β 已合并或完成 rebase，因为 PR-2 也会碰 extension SDK/domain host 结构。

测试先行：

- `backend/tests/test_indexing_pipeline_kg_strategy.py`
  - plugin 只能提供 prompt builder 与 response mapper；
  - core 仍负责 windowing、`kg_extract` model call、timeout/cancellation、schema admission、evidence binding；
  - mapper 返回 unknown object type / edge type / invalid evidence handle 时 per-source fail-closed，不污染旧 KG；
  - plugin exception / malformed output 不让 job 崩成未终止状态；
  - emitted events 只含 ids/count/status/timing，不含 prompt、query、element text、evidence text、exception text。
- `backend/tests/test_kg_repository.py`
  - `extraction_runs` 或等价 KG extraction record 带 `(pipeline_id, pipeline_version)`；
  - mixed identity 的 KG rows 不被 scale/index projection 混用；
  - rebuild 成功才发布 `unified_kg_state` identity。

文件级修改：

- `backend/app/extension_sdk/indexing.py`
  - 增加 KG prompt builder / response mapper protocols。
- `backend/app/domain/extensions.py` 或 `backend/app/domain/indexing_pipeline.py`
  - 增加 core-facing KG strategy port。
- `backend/app/extensions/indexing.py`
  - host 调用 KG strategy，复用 PR-1 descriptor/availability。
- `backend/app/services/knowledge_lifecycle.py`
- `backend/app/services/kg_ingest.py`（或当前 KG extract/mapping owner）
  - 将 prompt construction 与 mapper 接入 pipeline strategy。
  - 模型调用仍使用现有 `kg_extract` 绑定。
- `backend/app/domain/kg/edge_schema.py`
- `backend/app/repositories/sqlite/knowledge_store.py`
- `backend/app/repositories/postgres/knowledge_store.py`
  - 只在必要时补 admission/product identity 字段；schema 权仍属核心。

PR-2 不做：

- 不开放 parser 选择。
- 不开放 embedding/model binding 给插件。
- 不让插件直接持有 DB/API/service。
- 不新增第二套 KG job/polling。

## 4. 可选 PR-γ：样板/演示管线

只有在 PR-1/PR-2 稳定后再做。

目标：

- 给 deployment plugin 提供一个最小 indexing pipeline 示例，展示：
  - 自定义 chunking；
  - 可选 KG prompt/mapper；
  - missing plugin revert-to-builtin；
  - admin topology 可见。

注意：

- 示例 plugin 不能 ship CSS，不能 import `api.ts`，不能读 `error.message/.error`。
- 如果加入前端 UI contribution，必须更新 generated UI extension contract。
- 如果只是 backend indexing pipeline contribution，不需要新增 workspace UI slot。

## 5. 完成验收

实施前约束为：

1. ask.engine PR 的共享基座已稳定，或 PR-1 开始前重新审计并 rebase 以下文件：
   - `backend/app/extension_sdk/contracts.py`
   - `backend/app/domain/extensions.py`
   - `backend/app/extensions/registry.py`
   - `backend/app/extensions/bootstrap.py`
   - `backend/app/bootstrap.py`
   - `frontend/features/extension-sdk/contracts.ts`
   - `frontend/app/system-api.ts`
2. 动态迁移核验确认 58/36 未被占用；若被占用，重新编号并更新 shadow pair。
3. UI kit/arxiv sample 当前改动已由其任务收口，或 PR-1 明确避开这些文件。
4. PR-1 先解决 chunk rebuild “不混用”策略；否则只能做 storage/API skeleton，不能声明完成。

完成结论：共享扩展基座与既有 UI kit/arxiv 改动均被保留；SQLite 59 / PostgreSQL 37
新增 durable stage header 与逐来源 JSON payload 表。chunk、KG、事实、抽取状态与 identity
只经同一个最终事务发布，失败路径不会改变 live generation；双后端 schema/shadow、迁移、
迟到 generation、无 KG/model、零元素来源、多来源后段失败、隐藏来源保留和派生 KG 失效均有
定向回归覆盖。
