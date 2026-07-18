# Knowhow 表 / Memory 跨 Notebook 复制与移动 — 设计文档

- 日期：2026-07-18
- 分支：`claude/knowhow-memory-notebook-copy-move-c29f2a`
- 状态：设计已与用户逐节确认（架构选择 ①=独立传输服务、②=K-1 忠实复制派生物零重嵌入）

## 1. 目标

让用户能把**单张 knowhow 表**、**单条（或多条）memory** 从一个 notebook **复制**或**移动**到另一个 notebook。

区别于既有的「整本笔记本深拷贝」（`NotebookCopyService.copy_notebook`，一次拷整个 notebook 到**新建**的 notebook）：本特性是**细粒度**（单表 / 单条 memory）、目标是**已存在**的 notebook，且同时支持 copy 与 move。

### 非目标（明确不做）
- 不做**行级** knowhow 迁移（迁移单位是整张表）。
- 不做 memory 的**跨用户**传输（memory 是 owner-private）。
- 不做 **Agent / MCP** 侧传输端点（本期纯用户面 UI + REST）。
- 移动**不清理**源笔记本的孤立资产文件（沿用现有「删表不删资产」语义，见 §4.4）。
- 不改动既有 `NotebookCopyService.copy_notebook`（整本拷贝链路）。

## 2. 现状与可复用件（已核对 live 代码）

### 2.1 knowhow 存储
- 5 张业务表（均在 `_migration_16` / `_migration_18`）：
  - `knowhow_tables(id, notebook_id, title, description, mutation_seq, hidden_source_id, created_by, created_at, updated_at)` — 仅此表带 `notebook_id`。
  - `knowhow_columns(id, table_id, name, role, position)`
  - `knowhow_rows(id, table_id, position, projection_status, created_at, updated_at)`
  - `knowhow_cells(id, row_id, column_id, content_md, updated_at)` — UNIQUE(row_id,column_id)；`content_md` 内嵌图片引用 `![alt](asset://<asset_id>)`。
  - `knowhow_cell_code(id, row_id, column_id, code_text, language, updated_by, cell_content_hash, created_at, updated_at)` — UNIQUE(row_id,column_id)。
  - `knowhow_columns/rows/cells/cell_code` **都不带 `notebook_id`**，须经 `table_id` join 下钻。
- 资产：`notebook_assets(id, notebook_id, filename, mime, size, created_by, created_at, source_id)`；磁盘落 `storage_dir/assets/<notebook_id>/<asset_id>.<ext>`（`AssetService.path_for`，`assets.py:73-78`）。knowhow 粘贴图 `source_id = NULL`。
- KG 投影：每张表挂一个**隐藏合成源** `sources.source_type='knowhow'`，id 记在 `knowhow_tables.hidden_source_id`。`KnowhowProjector.project_table(table_id, embed=True)`（`projection.py:301`）确定性重建 `source_elements` + `chunks` + `chunk_embeddings` + `knowledge_objects` + `knowledge_relations`。稳定内容派生 id：`element_id(row_id, column_id)`、`cell_chunk_id(row_id, part)`、`_cell_ko_id(...)`（均 `projection.py` 导出）。无 anchor 列的表投影**零 KO/边**。
- 删除：`delete_knowhow_table(table_id)` 级联删 columns/rows/cells/cell_code，返回 `hidden_source_id`；`delete_table_projection(hidden_source_id)` 删 relations/objects/chunks/elements/隐藏源。**都不删 `notebook_assets`**。

### 2.2 K-1 复用样板（整本拷贝里的 knowhow 段）
`NotebookCopyService.copy_notebook`（`notebook_sharing.py:213-378`）已实现**单表级别可复用的全部 remap 逻辑**，本特性直接镜像：
- 业务表 remap：tables→columns/rows→assets→cells（`content_md` 走 `_rewrite_asset_refs(content_md, asset_map)`，`notebook_sharing.py:37-46`）→cell_code。行 `projection_status` 一律置 `'pending'`。
- 派生物 remap（**零重嵌入的关键**，`notebook_sharing.py:294-378`）：
  - `source_elements`：knowhow 元素 id 用 `element_id(new_row_id, new_column_id)` 重算，`metadata.knowhow.{table_id,row_id,column_id}` 同步 remap。
  - `chunks`：knowhow chunk id 用 `cell_chunk_id(new_row_id, part)` 重算（`part` 从旧 id 尾段读，列 position 不变），`element_ids` 走 `_remap_json_ids`。
  - `chunk_embeddings`：跟随 chunk id 原样拷（不重嵌）。
- 资产文件：`shutil.copy2(assets/<src>/<old>.ext, assets/<dst>/<new>.ext)`（`notebook_sharing.py:262-270`）。
- id 生成：`_new_id(prefix)`=`prefix + 128bit uuid hex`（`sqlite_repository.py:175-181`）；JSON 内嵌 id：`_remap_json_ids`（`sqlite_notebook_sharing.py:30-50`）。
- **差异点**：整本拷贝拷 `notebook_assets` 全量；单表只拷**本表 cells 引用到**的资产（解析 `content_md` 里 `asset://([\w-]+)`）。knowhow 的 `knowledge_objects/relations` 整本拷贝**不拷**（靠 reproject 重建）——单表同样不拷，靠 `project_table` 重建。

### 2.3 memory 存储
- `memory_items(id, notebook_id, created_by, agent_profile_id, source_answer_id, origin CHECK('ask_answer','external_agent'), status CHECK('candidate','confirmed','rejected','deprecated'), promotion_state, title, content_md, tags_json, confirmed_by, confirmed_at, embedding_status, embedding_error, created_at, updated_at)` — 键 `(notebook_id, created_by)`，owner-private。
- 唯一键 `idx_memory_answer_once UNIQUE(created_by, source_answer_id) WHERE source_answer_id IS NOT NULL` — 复制必须置空 `source_answer_id` 否则撞键。
- 依赖 4 表（级联于 `memory_items.id`）：`memory_revisions`、`memory_provenance`(1:1 UNIQUE, `payload_json`)、`memory_embeddings`(1:1, `vector BLOB`)、`memory_items_fts`(触发器维护，插 `memory_items` 自动填)。
- KG：确认记忆经 `SourceIngestionService.ingest_memory_source(nb, memory_id, title, content)`（`source_ingestion.py:777`）建隐藏源 `source_type='memory'`、`sources.memory_id=<id>`（**全局唯一** `idx_sources_memory_id`），抽 elements+element_embeddings+KG（**无 chunks**）。门 `memory_kg_eligible(nb)=should_extract_kg(nb) AND tier!='base'`。
- 删除：`MemoryService.delete`（`memory_service.py:725`）**只删 memory 行**（级联 4 表），**不删派生 KG 源**；只有 `deprecate`（`:710-723`）调 `memory_kg.remove_memory_source`。→ 移动流程须显式先 `remove_memory_source` 再 `delete`。
- 整本拷贝**不拷 memory**；只把派生的 `source_type='memory'` 源拷过去并抹 `memory_id=''`。

### 2.4 访问守卫
- `require_notebook_write`（`deps.py:72`）= owner-only（`user_can_access_notebook`）。
- `require_notebook_read`（`deps.py:84`）= owner ∪ 只读成员（`user_can_read_notebook`）。
- 越权一律 404，不泄漏存在性。

### 2.5 架构守卫（新增仓库方法必过）
`test_repository_facade_contract.py`（域方法元组）、`test_repository_surface_manifest.py`（`ownership_manifest.py` + `facade_surface.json` fixture，**consumers 是行号 pin**）、`test_repository_callers_static.py`（SQL 站点 / 私有成员 allowlist，行号 pin）、`test_architecture_module_boundaries.py`、`test_repository_phase_contracts.py`、`test_repository_protocol_coverage.py`（`ports.py` Protocol）。**新代码追加到文件 EOF 以免移动既有行号 pin**（`surface-manifest-line-shift-gotcha` 教训）。

## 3. 访问模型与范围

| 操作 | 源守卫 | 目标守卫 | 备注 |
|---|---|---|---|
| knowhow 复制 | `require_notebook_read`（owner ∪ 只读成员） | `require_notebook_write`（owner） | 从分享给我的只读表也可复制到我的库 |
| knowhow 移动 | `require_notebook_write`（owner） | `require_notebook_write`（owner） | 移动含删源，源必须我拥有 |
| memory 复制 | `created_by == 我` | `created_by == 我` | 私有，两端都是我 |
| memory 移动 | `created_by == 我` | `created_by == 我` | 同上 |

- 副本 `created_by = 当前用户`、`notebook_id = 目标`。
- 目标 == 源 notebook → 拒绝（400）。
- 目标选择器只列**当前用户拥有写权的 notebook**，排除源自身。knowhow 从只读源打开时前端只给「复制」。

## 4. 组件 A：knowhow 表传输

新服务 `KnowhowTransferService`（放 `app/services/knowhow/`，或作为 `knowhow/api.py` 的新函数 + store 层新方法，具体分层在计划阶段定）。复用 §2.2 全部 helper。

### 4.1 复制（单事务）
入参：`source_table_id`、`target_notebook_id`、`actor_user_id`。

1. 读源表（`get_knowhow_table` 拿 columns/rows/cells + `hidden_source_id`）；读 `knowhow_cell_code`；读隐藏源的 `source_elements` / `chunks` / `chunk_embeddings`（`WHERE source_id = hidden_source_id`）。
2. 收集本表资产：扫所有 cell `content_md` 里 `asset://([\w-]+)` → 去重 asset_id 集 → 读对应 `notebook_assets` 行。
3. 建 remap 映射（`khtbl/khcol/khrow/khcel/khcode/asset/source/element/chunk`），全用 `_new_id`（隐藏源新 id 走 `source_map`）。
4. **单个 `database.write()` 事务**内按 FK 序插入并**在提交前校验**：`knowhow_tables`（新隐藏源 id、`projection_status` 无关，行级才有）→`knowhow_columns`→`knowhow_rows`(`projection_status='pending'`)→`notebook_assets`→`knowhow_cells`(`_rewrite_asset_refs`)→`knowhow_cell_code`→隐藏 `sources` 行（`source_type='knowhow'`，新 id、`notebook_id=目标`）→`source_elements`（`element_id` 重算 + `metadata.knowhow` remap）→`chunks`（`cell_chunk_id` 重算 + `element_ids` remap）→`chunk_embeddings`（跟随 chunk id）。**同事务内**用 join 计数比对源表 vs 副本的 columns/rows/cells/cell_code 及隐藏源 element/chunk 行数一致，不一致 → 抛错 → 事务自动回滚（不留半份副本）。
   - 单表规模小（设计约束「单表百行内」），单事务即可，**不需要**整本拷贝的 sentinel/分块/补偿机制。
5. 事务提交后落资产磁盘文件（`shutil.copy2`，缺源文件跳过——与整本拷贝一致）。落盘失败：copy2 单文件失败**逐一记事件并跳过**，不回滚已提交的 DB（图片可后补，DB 一致性优先；DB 提交前不写任何磁盘文件，故不存在「DB 失败但已落盘」的清理需求）。
6. 调度 `ProjectionScheduler.schedule(new_table_id)` 在目标 KG 重建 objects/relations（`chunks`+`chunk_embeddings` 已在位且 text/section_path 不变 → **零重嵌入**）。best-effort，失败只记事件。

### 4.2 移动
`mode='move'`：先执行 §4.1 完整复制**并校验通过**，再删源。**删源内部顺序＝先拆投影、后删表行**：
1. 复制**之后**、删表行**之前**读 `hidden_source_id`（它只存在于 `knowhow_tables` 行里，删了行就再也拿不到）。
   约束只是「读早于 DELETE」，不是「读早于复制」：放到复制之前会让这个读跨越整个复制窗口（快照+事务+资产落盘）
   形成 TOCTOU——源表若在此期间**首次**被投影，读到 `None` 会跳过拆投影却照删表行，重新变成下面说的那种
   不可回收的幽灵；并发的 `ensure_hidden_source` 顶替了 id 时，拿旧 id 去拆会静默 no-op 并把新的漏成孤儿。
2. `delete_table_projection(hidden_source_id)`（删源投影 + 源隐藏源）。
3. `delete_knowhow_table(source_table_id)`（删源表行）。
4. 源 `notebook_assets` **不动**（沿用现删表语义）。
- **为什么是这个顺序（2 在 3 之前）**：若反过来先删表行、拆投影再失败，源的 chunks/`chunks_fts`/
  `chunk_embeddings`/KO 会**永久且不可回收**地留在源笔记本里继续被检索到——`delete_table_projection`
  的唯一生产调用方需要一个已不存在的 `knowhow_tables` 行，而 `hidden_source_id` 只存在于该行；
  且 `copy_table` 紧接着调度了后台重投影，此刻存在并发写者，`delete_table_projection`
  跨两个写事务、可能因 busy timeout 抛 `OperationalError`，并非纯理论窗口。
  按本顺序，拆投影失败时源表行仍在 → 可重试移动、也可走正常删除路径，符合下面的「两边都留」承诺。
- 删源在复制事务**之后**独立进行。删源失败：复制已成功，报「已复制，源未删除」，两边都留，用户可重试。**永不先删后复制**。

### 4.3 资产作用域
只拷本表 cells 引用到的资产（非整本 `notebook_assets`）。同一 asset 被多 cell 引用只拷一次（asset_map 去重）。目标产生的是**独立副本**（新 asset_id + 新磁盘文件），源资产不受影响。

### 4.4 移动不清理源资产
`delete_knowhow_table` 现语义即不删资产（资产 notebook 级、可能被别的表/格共享）。移动沿用之，源可能留孤立资产文件——可接受（图片小、无正确性影响），孤立资产清理是独立议题。

## 5. 组件 B：memory 传输

新服务方法挂 `MemoryService`（`memory_service.py`）+ store 层新方法（`memory_store.py`）。

### 5.1 范围
- **仅 `status='confirmed'` 的 memory** 可传输（候选/弃用不参与）。
- 支持多条批量（`memory_ids: [...]`）。

### 5.2 复制（每条，单事务）
1. 新 `memory_items`：新 id、`notebook_id=目标`、`created_by=我`、拷 `title/content_md/tags_json`、`status='confirmed'`、`confirmed_by=我`、`confirmed_at=now`、`source_answer_id=NULL`（避免撞 `idx_memory_answer_once`）、`agent_profile_id=NULL`、`promotion_state='none'`、`embedding_status` 见下。
2. 初始 `memory_revisions`（revision=1，`change_reason='从笔记本 <源> 复制'`）——**不拷源的历史 revision**（历史属于源、且 ref 旧 memory_id）。
3. 新 `memory_provenance`：`origin` 沿用源值（不改 CHECK），`payload_json` 写 `imported_from: {notebook_id, memory_id, action, source_provenance:{…源 payload 原样…}}`。
   **源 provenance 必须嵌套保留、不得整体丢弃**：否则 `ask_answer` 出身的记忆复制后仍是 confirmed，
   却丢光了 `answer_id`/`question`/`citations`/`evidence_level`——即「确认态记忆不带任何当初确认它的证据」；
   **移动更会永久丢失**（源已删）。**嵌套而非平铺**是因为源的 `anchors`/`citations` 指向**源笔记本**的
   行 id，在目标库不可解析，放顶层会被消费方误当作活引用。
4. `memory_embeddings`：**拷源那 1 条向量**（新 memory_id key、同 model/dimension/vector），`embedding_status='ready'`（零重嵌入）。源无向量（pending/failed）→ 不拷，`embedding_status='pending'` 并调度补嵌（复用 `_schedule_embed`）。
5. FTS：插 `memory_items` 由触发器自动填，无需处理。
6. KG 重派生：按**目标** notebook 的 `memory_kg_eligible(target_nb)` 决定；沿用 confirm 的默认开 + 可取消开关（前端复用现「同时抽取到知识图谱」勾选）。走 `ingest_memory_source(target_nb, new_memory_id, title, content)` 在目标建**新**隐藏源（新 memory_id，不与源全局唯一键冲突）。后台 best-effort。

### 5.3 移动
`mode='move'`：先执行 §5.2 复制成功，再删源：
1. `memory_kg.remove_memory_source(source_memory_id)`（删源派生 KG 源；无则 no-op）。
2. `MemoryService.delete(source_memory_id, 我)`（级联 revisions/provenance/embeddings）。
- 同 §4.2：先复制后删源，删源失败报「已复制，源未删除」。

### 5.4 无需 schema 迁移
全部复用现有表；import 信息进现有 `payload_json`；不动任何 CHECK 约束；**不 bump SCHEMA_VERSION、不需重启**。

## 6. 数据流

```
用户点「复制/移动到…」→ 选目标 notebook（+ mode）
  → REST 端点（校验源/目标访问权 + 目标≠源 + 单位类型限制）
    → 传输服务（单事务复制 → 校验 → [move: 删源]）
      → 后台调度：knowhow=project_table(new)；memory=ingest_memory_source(target,new)（按 eligibility）
  ← 返回新建 id 列表（同步）；KG 派生异步
前端刷新目标/源列表
```

## 7. 错误处理与原子性
- **移动一律「先复制+校验，再删源」**，任何删源失败都不丢数据。
- 复制单事务（含提交前校验）：DB 层失败或校验不一致自动回滚，不留半份副本。磁盘资产文件仅在 DB 提交**之后**落，故不存在「DB 失败但已落盘」需补偿的情形；单文件 copy2 失败逐一记事件跳过（图片可后补）。源笔记本在整个复制过程中零改动。
- 批量 memory 复制：逐条独立事务，部分成功部分失败 → 返回 per-item 结果（成功 id + 失败原因），不整体回滚。
- KG 重投影 / 重抽 KG 后台 best-effort，失败只记 `model_error` / 相应事件，不阻断传输（与现有 confirm/copy 行为一致）。
- 越权/目标非法/单位状态非法 → 4xx，不产生任何副作用。

## 8. REST API
- `POST /knowhow/tables/{table_id}/transfer`
  - body `{ "target_notebook_id": str, "mode": "copy"|"move" }`
  - 守卫：源按 mode（copy=read / move=write）、目标 write；目标≠源。
  - 返回 `{ "new_table_id": str }`。
- `POST /memories/transfer`
  - body `{ "memory_ids": [str], "target_notebook_id": str, "mode": "copy"|"move", "extract_kg": bool? }`（`extract_kg` 可选，默认 `true`，与 confirm 默认开一致）
  - 守卫：每条源 memory `created_by==我`、目标 `created_by==我`（write）；目标≠源；仅 confirmed。
  - 返回 `{ "results": [ {"source_id":..., "new_id":..., "ok":bool, "error":str?, "status": "copied"|"moved"|"failed"|"copied_source_not_removed"} ] }`。
    `status` 枚举取代早期设计的 `source_deleted` 布尔：三个布尔要前端自行关联易错，且 `source_deleted`
    在 copy 模式无意义。`copied_source_not_removed` = **副本已在目标、源未删**（`ok=False` 但 `new_id` 有值），
    前端据此提示「副本已存在，别盲目重试」——否则用户重试会静默累积副本。
- 两端点同步返回；KG 派生走后台。

## 9. 前端触点（前后端同 PR 交付）
- **knowhow**：表管理面板 / 表头加「复制/移动到…」→ 目标选择器 modal（复制/移动切换；从只读源打开时只给「复制」）。复用既有 utility-modal 样板。
- **memory**：memory 列表单条 + 多选工具栏加「复制/移动到…」→ 目标选择器（只列我自己的 notebook）+「同时抽取到知识图谱」勾选。
- **传输 wire 契约测试**（防 PR#286 那类「前端自造契约、后端宽容默认静默丢字段」复现）：锁 request body 字段名与后端一致。
- 新前端 API 客户端模块（镜像 `notebook-share.ts` 风格）+ 单测。

## 10. 仓库 / 架构守卫义务
- 新增的 store/service 方法：加到对应 `ports.py` Protocol → 实现 → facade `SQLiteRepository` 显式一跳委托 → `ownership_manifest.py` 加 `SurfaceMember`（owner + 精确 consumer file:line）→ 重生成 `facade_surface.json` → 相应域方法元组加名 → 若新增 SQL 站点/私有成员访问，更新 `callers_static` allowlist。
- **新代码追加到文件 EOF**，避免移动既有行号 pin。
- 改 OpenAPI（新端点）→ 按既有约定重生成 `api_contract.json`（只重算 openapi 段，保 source_commit/serialization verbatim）。

## 11. 测试
后端（TDD）：
- knowhow 复制往返：列/行/格/代码/资产全随迁、id 全重映射无悬空、隐藏源 element/chunk 用稳定 id 落到相同 id、reproject 后零新增 embed。
- knowhow 移动：源表 + 投影 + 隐藏源被删、源资产保留、目标完整。
- 资产作用域：只拷本表引用的资产、多 cell 共享同图去重、磁盘文件随迁。
- memory 复制：4 表建齐、`source_answer_id` 置空不撞唯一键、向量拷贝零重嵌、provenance 带 imported_from。
- memory 移动：源派生 KG 源被删、源 memory 级联删净。
- 访问：只读源可复制 knowhow 不可移动；跨用户 memory 传输被拒；目标==源被拒；非 confirmed memory 被拒。
- 批量 memory 部分失败 per-item 结果正确。
- 架构守卫全绿；schema 无变更断言。

前端：传输 wire 契约测试 + 选择器/开关/mode 切换单测 + tsc clean。

## 12. 风险与开放项
- **K-1 稳定 id 重算的边界**：knowhow chunk 的 `part` 从旧 id 尾段读、依赖列 position 原样拷——单表复制保证 position 不变，安全；测试须覆盖多列/entity 拆分格。
- **批量 memory 的 KG 重抽成本**：N 条 confirmed → N 次 `ingest_memory_source`（LLM）。与「确认 N 条」等价、且默认开可取消，可接受；批量时前端应提示。
- **只读源复制 knowhow 的 created_by**：副本归复制者；源表 `created_by`（他人）不带过来。
- 分层落点（新独立 service vs 挂 `knowhow/api.py` + store）在 writing-plans 阶段定，倾向独立、复用导出 helper、不碰 `NotebookCopyService`。

## 13. 交付
- 后端传输服务 + 2 REST 端点 + 架构守卫/fixture 更新。
- 前端 2 处入口 + 目标选择器 + API 客户端 + 契约/单测。
- 前后端同一 PR（`frontend-backend-co-design`）；分支 rebase 到 master 保持线性后提 PR。
