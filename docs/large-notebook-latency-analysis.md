# 生产延迟根因分析：2.15M-object notebook（打开 / 看板 / 索引状态)

> 结论先行：三个卡顿共享**同一个根因族**——对 `knowledge_objects`（该 notebook 约 2.15M 行）的 `GROUP BY object_type` / `COUNT` 每次打开都**重算、无缓存**。计数只在 ingest/rebuild 时变化，却在每次请求重扫 2M 覆盖索引条目，纯浪费。修复基础设施（`kg_mutation_seq` 版本闸 + 既有 `version_memo` 模式）已存在且已验证有效，只是没接到这几个计数点上。

---

## 1. 症状 → 三条慢路径（一句话定位）

| 症状 | 直接触发点（前端→端点） | 同步执行的那条 SQL |
|---|---|---|
| **打开 notebook 冻结 5-6s** | `openNotebook` 的 `Promise.all([GET /notebooks/{id}, GET .../sources])`（page.tsx:1737-1740）阻塞工作区绘制 | `from_row`→`knowledge_type_counts`：`SELECT object_type, COUNT(*) … WHERE notebook_id=? AND status IN USABLE_STATUSES GROUP BY object_type`（query_store.py:50-55） |
| **点「看板」冻结 5-6s** | `openAnalytics` 无条件 `await GET /notebooks/{id}/analytics`（page.tsx:2357），模态被 `analytics &&` 门控（page.tsx:4163） | `notebook_analytics` 的 `knowledge_counts`：`… WHERE notebook_id=? AND status!='deprecated' GROUP BY object_type`（query_store.py:259-262） |
| **索引状态「过一会才弹」** | `openAnalytics` 先 await `/analytics`、随后**才** fire-and-forget `GET /notebooks/{id}/index-status`（page.tsx:2362），卡片在 `indexStatus ?` 前为空（page.tsx:4197） | `index_status()` 每次同步跑：`COUNT(*) FROM chunks`（index_projection_store.py:134）+ `COUNT(DISTINCT canonical_id) FROM concept_clusters`（unified_kg_store.py:338）+ delta chunk COUNT（index_projection_store.py:168） |

---

## 2. 根因排序

### 真根因 R1 — 打开路径：per-type GROUP BY，一次打开跑 **2 次** 2.15M 覆盖扫描 【已确认】

- **确切操作**：`knowledge_type_counts`（query_store.py:50-55），表 `knowledge_objects`，扫描规模 ~2.15M。
- **index-only？** 是。`EXPLAIN QUERY PLAN` 已实测为 `SEARCH … USING COVERING INDEX idx_knowledge_objects_nb_type_status (notebook_id=?)`（migrations.py:466）。`object_type` 是索引第 2 列 → 天然分组，无 temp b-tree、无 ORDER BY 排序。成本纯粹是 **2.15M 叶条目的单线程连续遍历**。
- **每次重算 / 无缓存？** 是。无 `kg_mutation_seq` 版本闸、无 memo、无路由级响应缓存。
- **为什么 5-6s？** 覆盖扫描 2.15M 条目单核 ≈ 0.5-2s 热页；生产为多用户、共享服务、页缓存被别的 notebook 挤占，冷/半冷页错更贵（migrations.py:480 注释记录了同类非覆盖聚合在 490k-object 部署上重叠测得 96-147s 的历史）。
- **复合性 = 关键放大器**：一次打开该 `from_row` fan-out 跑 **2 遍**——
  1. `GET /notebooks/{id}` → `catalog.get_notebook`→`from_row`（notebook_catalog.py:110）；
  2. `GET /notebooks/{id}/scale-index/status` → `scale_artifacts.status`→`self.get_notebook`（sqlite_repository.py:366-368）**又跑一遍整个 `from_row`**。
  - 即**一次打开 = 2× 2M GROUP BY + 2× 全套 fan-out**。若 `shouldResumeKgBuild`/`buildingScaleIndex` 为真，`setInterval` 每 **6s** 重打 `GET /notebooks/{id}` 与 `scale-index/status` → 每 6s 再来一轮。
- **`from_row` 内其余项——次要**：sources `COUNT`（小表）；`base_notebook_info` / `has_kg` 的 `EXISTS` 首行短路 ≈ O(1)；`count_pending_kg_sources` 为 O(#sources) 索引探针（随文档数，非 2M）。这些看着吓人但都短路，**不是** 5-6s 来源。

### 真根因 R2 — 看板：`/analytics` 的 `knowledge_counts` GROUP BY 【已确认】

- **确切操作**：`notebook_analytics`（query_store.py:259-262），`knowledge_objects` `status!='deprecated' GROUP BY object_type`，同一 `idx_knowledge_objects_nb_type_status` 覆盖扫描 ~2.15M。
- **index-only？** 是（实测负向 `!=` 与 `IN` **planner 生成完全相同的计划**，见 §3）。**无缓存**，每次点击重算，且前端每次关闭 `setAnalytics(null)`、无 `if (analytics) return` 客户端 memo → **每次点击都后端重算 + 全量重取**。
- **为什么 5-6s**：与 R1 同一条 2M 覆盖遍历；board 其余聚合（answers/feedback COUNT、source parse_status GROUP BY）都是小表，非瓶颈。
- **KG 概览页更糟（旁证，非本次症状）**：`knowledge_query.knowledge_types` 先 `get_notebook`（GROUP BY #1，`IN` 谓词，knowledge_query.py:174）再 `type_counts`（GROUP BY #2，`!=deprecated`，knowledge_store.py:658）→ **一次打开两遍 2.15M 覆盖扫描**；`/graph` 守卫再加 `count_active_objects` 第三遍。

### 真根因 R3 — index-status「过一会才弹」：三条无缓存 O(N) 计数 + 前端串行 【机制已确认，绝对耗时需 diag 实测】

`index_status()`（scale_artifact_runtime.py:430）每次同步 fan-out，**无条件、未 gate 的 O(N)**：

1. `total_chunk_count` — `COUNT(*) FROM chunks WHERE notebook_id=?`（index_projection_store.py:134-139），走 `idx_chunks_nb`，O(#chunks)，**每次调用都跑**。
2. `distinct_cluster_count` — `COUNT(DISTINCT canonical_id) FROM concept_clusters WHERE notebook_id=?`（unified_kg_store.py:338-342）**无条件跑**（knowledge_lifecycle.py:806），尽管 `state_row.cluster_count` 已有缓存值、仅当 null 兜底才用（knowledge_lifecycle.py:817）。`concept_clusters` 是**每成员一行**的高基数表，`idx_clusters_nb` **不覆盖 `canonical_id`** → DISTINCT 聚合扫全部成员行 = O(N)。**这是纯浪费的重复计数**。
3. `delta_chunk_count` — `COUNT(*) FROM chunks WHERE … source_id IN (…)`（index_projection_store.py:168），O(delta)，若有大批未索引 source 会失控。

- **唯一被 gate 的是 `version_facts`**（5 表 `COUNT/MAX`，index_projection_store.py:92-120）：`version()` 用 `version_memo` 对 `version_signal=(kg_mutation_seq, cluster_mutation_seq, mention_seq)`（O(1) 单行读）记忆化（scale_artifact_runtime.py:212-246）。memo 命中时跳过；**进程重启或任一 seq 变化后的首次调用**会冷跑 5×COUNT/MAX（含 `knowledge_objects`、`knowledge_relations`）。
- **「过一会才弹」的机制** = 后端这三条无缓存计数串行 + 冷 memo 时再叠 5 表 COUNT，**再叠前端串行**：`openAnalytics` 先 await `/analytics`（已经 5-6s），**之后**才发 `/index-status`，卡片在第二个更慢的调用返回前一直空（page.tsx:4197）。感知延迟 = `/analytics` 时长 + `/index-status` 时长，视觉上「先弹模态、索引卡后填」。

### 次要 / 已排除

- `has_kg` EXISTS：首行短路 O(1)。
- `base_notebook_info` / `count_pending_kg_sources`：短路或 O(#sources)。
- `version_facts`：已 memo，正确有效，**不是**重算浪费。
- `maintenance.kg_object_counts_by_notebook`（全表无 WHERE GROUP BY，maintenance.py:511）：**无任何 caller**，打开路径上是 dead code，勿担心（但若将来被调将是全 2M 全表聚合）。
- graph/viz/communities/unified-kg/knowledge-types：全部懒加载，**不在**打开路径。

---

## 3. 修复建议（按 价值/成本 排序，带效率论证）

### 【最高 ROI】F1 — 计数缓存：把 per-type counts 挂到 `kg_mutation_seq` 版本闸 | S-M | **无需 schema 迁移**

- **做法**：完全复刻既有、已验证的 P1-8 模式（scale_artifact_runtime.py:212-246 `version_memo`）。计数是 KG 状态的纯函数、只在 ingest/rebuild 变，signal 用现成的 `unified_kg_state.kg_mutation_seq`（migrations.py:421，由 `_mark_unified_kg_dirty` 每次 KG 写入 +1，是单调计数器——记忆里明确「时间戳 1s 粒度会漏同秒 in-place 编辑，必须用单调 seq」）。
- **覆盖的站点**（每个都从 2.15M 扫描降到 memo 命中 O(1) 单行读）：
  - `knowledge_type_counts`（query_store.py:50，`from_row`，R1 主因，×2/开）
  - `notebook_analytics.knowledge_counts`（query_store.py:259，R2）
  - `knowledge_store.type_counts`（knowledge_store.py:658，KG 概览）
  - `count_active_objects`（knowledge_store.py:648，/graph 守卫）
  - `effective_object_count`（index_projection_store.py:126，viz gate）
- **效率论证**：memo key 是 `(kg_mutation_seq)` 单行读，命中即返回上次计数字典；只有 ingest/rebuild bump seq 后首次才真扫。符合「强一致 opt-in、默认低开销」——计数天然最终一致，读侧永远 O(1)。**这一条同时消灭 R1、R2 及概览/graph 的重复扫描**。
- **存储选项**：进程内 LRU（跟 `version_memo` 一致，最简，重启失一次冷跑可接受）优先；若要跨进程/重启持久，可把计数字典物化进 `unified_kg_state`（那才需要 §迁移，见 F4）。**建议先做进程内版（S），零迁移**。
- **务必修的 gotcha**：`_mark_unified_kg_dirty` 记忆里被记录「非唯一汇聚点，`update_knowledge`/re-embed 曾绕过没标脏、已接回」——上缓存前需确认所有改 object status/type 的写路径都 bump 了 seq，否则计数缓存会陈旧（这正是缓存正确性的关键，需 diag 或代码审计确认覆盖面）。

### 【高 ROI，低成本】F2 — 消除打开路径的「×2 重复 fan-out」| S | 无需迁移

- `scale_artifact_runtime.status` 调 `self.get_notebook`（sqlite_repository.py:366-368）导致 `from_row` 在一次打开跑第二遍。让 `status` **不要**重建完整 catalog summary——它只需要 notebook 行的 `kg_ready/building/pending_sources` 少数列，直接读 `summary_notebook_row`（query_store.py:88，PK 查）即可，不必触发 GROUP BY。
- **效率论证**：即便 F1 已缓存，去掉这次多余调用仍减一次 memo 查 + 一整套 fan-out 对象构造；且**降低 6s 轮询期的放大**。与 F1 独立、可同 PR。

### 【高 ROI】F3 — index-status 去掉无条件重复计数 | S | 无需迁移

- `distinct_cluster_count`（unified_kg_store.py:338）：`state_row.cluster_count` 已缓存该值，把它从「无条件跑、仅 null 兜底」改成「优先用缓存、缺失才算」——直接删掉这条 O(N) DISTINCT 扫描（knowledge_lifecycle.py:806→817）。**零风险、纯删浪费**。
- `total_chunk_count`（index_projection_store.py:134）：同样纳入 F1 式 seq 缓存（chunk 数也只在 ingest 变），或走 `version_facts` 已算的 chunk count 复用。
- `delta_chunk_count`：保留（它是真实增量语义），但已由 manifest watermark 界定为 O(delta)，非主害。

### 【中 ROI】F4 — 看板 / index-status 前端异步 + 客户端缓存 | S（前端） | 无需迁移

- 客户端按 notebook id memo 化 `analytics`/`indexStatus`（Map 或 state guard `if (analytics) return`），关闭模态不清空 → 二次点击 board 瞬开。
- 把 `/analytics` 与 `/index-status` **并行** fire（不要串行 await），board 骨架先出、两卡各自 fill；配合 F1/F3 后端已 O(1)，感知延迟趋近 0。
- **效率论证**：即便后端已缓存，省掉往返 + 重渲染风暴（记忆里「轮询整页 re-render 风暴」教训）。

### 关于覆盖索引：**不需要补索引** 【已实测】

- 所有热计数已被 nb 前缀覆盖索引命中，`EXPLAIN QUERY PLAN` 无一是全表 SCAN。
- **`status!='deprecated'`（负向) vs `status IN USABLE_STATUSES`：planner 生成完全相同的计划**——两者都走 `idx_knowledge_objects_nb_type_status`、都以 `notebook_id=?` 为界、`status` 都只是每条目残差过滤（第 3 列无前导等值、无法 skip-scan）。**改写 `!=`→`IN` 在索引层零收益，别做这个「优化」**。
- 唯一「缺覆盖」的是 `concept_clusters` 的 `COUNT(DISTINCT canonical_id)`（`idx_clusters_nb` 不含 `canonical_id`），但**正解是 F3 用缓存 cluster_count 消除该查询**，而非为一条应被删的查询加索引。

---

## 4. 诊断脚本补充（scripts/diag*）

**目标**：让生产**自证**上述「已确认机制」的**绝对耗时/实际计划/实际行数**，零风险只读。

### 放哪：`diag_slow.py` 新增 report 函数 `report_notebook_count_hotpaths(conn, notebook_id)`

- **取舍**：放 `diag_slow.py` 而非 `diag.py`——本任务是「慢查询逐条 EXPLAIN+计时」，与 `diag_slow.py` 现有职责同族，共用其 `mode=ro` 只读连接与脱敏惯例；`diag.py` 只留一个薄委托入口即可。
- **⚠️ 硬约束（记忆 + 任务均点名）**：`diag_slow.py` 的 `conn.execute` 行号被 `test_repository_callers_static.py` 的**允许表钉死**。每新增一个 `conn.execute` 站点，**必须同步更新该允许表**，且保持 `mode=ro`（只读）、**无 DML**、输出脱敏（不打 object 文本/名，只打计数、计划、ms、行数）。

### 具体打印 spec（对「最大的 notebook」= `SELECT notebook_id FROM (SELECT notebook_id, COUNT(*) c FROM knowledge_objects GROUP BY notebook_id ORDER BY c DESC LIMIT 1)`）

对下列每条热查询，打印 **`EXPLAIN QUERY PLAN` 全文 + 用了哪个索引 + 是否出现 `SCAN`（全表）字样 + 实测 `elapsed_ms`（真实执行一次）**：

1. **count-by-type，谓词 A**：`… WHERE notebook_id=? AND status IN ('approved','reviewed','project_specific','conflict') GROUP BY object_type`（USABLE_STATUSES，knowledge_contracts.py:15）。
2. **count-by-type，谓词 B**：`… WHERE notebook_id=? AND status!='deprecated' GROUP BY object_type`。
   → 两条并列打印，**实证 planner 计划一致、耗时同量级**（验证 §3 结论）。
3. **relation 计数**：`COUNT(*), MAX(created_at) FROM knowledge_relations WHERE notebook_id=?`（index_projection_store.py 风格）。
4. **embedding 计数**：`COUNT(*), MAX(created_at) FROM knowledge_embeddings WHERE notebook_id=?`。
5. **cluster DISTINCT**：`COUNT(DISTINCT canonical_id) FROM concept_clusters WHERE notebook_id=?`（验证它不覆盖、扫全成员行）。
6. **community 计数**：`COUNT(*) FROM communities WHERE notebook_id=? AND level=?`（应命中 `idx_communities_nb_level`，作为对照的「便宜」样本）。
7. **chunk total**：`COUNT(*) FROM chunks WHERE notebook_id=?`（index-status R3）。
8. **index-status 的 delta 计算**：复算 `_index_delta`——读 manifest 的 `watermark_sources`（若可只读拿到）、`SELECT id FROM sources WHERE notebook_id=?` 求差集、对 delta source `COUNT(*) FROM chunks WHERE notebook_id=? AND source_id IN (…)` 分批，打印 **delta source 数、delta chunk 数、耗时**。

另外打印：

- **各热表在该 nb 的行数**：`knowledge_objects` / `knowledge_relations` / `knowledge_embeddings` / `chunks` / `concept_clusters`（成员行）/ `communities` 各一条 `COUNT(*) WHERE notebook_id=?`，让生产确认「2.15M」的真实数量级与分布。
- **关键覆盖索引存在性检查**：从 `PRAGMA index_list` / `sqlite_master` 核对 `idx_knowledge_objects_nb_type_status`、`idx_knowledge_objects_nb_status`、`idx_chunks_nb`、`idx_clusters_nb`、以及 `idx_clusters_nb` 是否含 `canonical_id`（预期否，佐证 F3）。逐个打印「存在/缺失」。
- **每条查询打印一行汇总**：`{name} | plan={COVERING INDEX xxx | SCAN table} | rows_in_nb=N | elapsed_ms=M`，方便一眼看出哪条是全表 SCAN、哪条 5-6s。

### 一次性打印「复合放大」证据

在同一 report 里，把「打开一次 = 2× from_row」翻译成打印：连续跑两遍 `from_row` 涉及的全部计数、汇总总 ms，直观展示单次打开的真实累加成本。

---

## 5. 落地次序

1. **先补 diag（零风险，先行）**：`report_notebook_count_hotpaths` 落地并同步 `test_repository_callers_static.py` 允许表。在生产对「最大 notebook」跑一次，**用真实 ms/计划/行数确认**：R1 的 2M 覆盖扫描耗时、`!=` 与 `IN` 计划一致、R3 三条无缓存计数各自耗时、`concept_clusters` DISTINCT 未覆盖。把「机制已确认」升级为「生产数字已确认」，再动代码。
2. **上计数缓存 F1（最高 ROI）+ F2 去重复 fan-out + F3 删 index-status 冗余计数**：优先进程内 seq-gated memo（无迁移）。上前用 diag/审计确认所有改 object status/type 的写路径都 bump `kg_mutation_seq`（缓存正确性前提）。这一步直接消掉打开与看板的 5-6s。
3. **再做 F4 前端懒加载/并行/客户端缓存**：board 骨架先出、两卡并行 fill、二次打开走客户端 memo，把感知延迟压到接近 0。
4. （可选，视 diag 数据）**F4-持久版**：若重启后冷跑仍不可接受，再把计数物化进 `unified_kg_state`——此时才需 `_migration_N + bump SCHEMA_VERSION`（记忆「schema 迁移约定」：必须新增 `_migration_N` 并 bump，不能塞进已封版的旧 migration，否则版本闸对已部署库短路漏建列）。

---

### 已确认 vs 需 diag 在生产验证

- **已确认（代码 + 本地 EXPLAIN 实测）**：热点是 `knowledge_objects` GROUP BY / COUNT；均走覆盖索引非全表 SCAN；`!=` 与 `IN` planner 计划一致；打开路径 `from_row` ×2；`version_facts` 已被 seq memo、其余五个计数点无任何缓存；`distinct_cluster_count` 无条件重复且不覆盖 `canonical_id`；前端无客户端缓存、看板每点必重取重算、index-status 前端串行于 analytics 之后。
- **需 diag 在生产验证**：该 nb 各热表**真实行数**与 2.15M 的分布；每条查询在生产磁盘/页缓存下的**绝对 ms**（本地 0.5-2s 估算 vs 生产 5-6s 的差距来自冷页/多用户争用，需实测）；`kg_mutation_seq` 是否被**所有**改 object 的写路径 bump（缓存正确性）；index-status 冷 memo 触发频率（进程重启/mutation 节奏）。