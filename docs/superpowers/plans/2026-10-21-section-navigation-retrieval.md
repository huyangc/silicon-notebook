# 实现计划：检索 Agent 的文档内导航（导航页出排序 + 向量章节线索 + browse_sections / read_section）

状态：第三版（2026-10-21）。吸收 opus 评审两轮，以及用户的两条裁决：①章节线索必须做向量匹配；
②不要针对性的判断规则（不按「Contents/Tables」之类的标签词、不按目录行正则判定），不做生产取样。
分支 `claude/production-user-issue-analysis-2b6d9d`，worktree
`R=/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/charming-lamarr-8f6079`。
本文同时充当规格：「契约」小节就是验收依据。

## 0. 背景与证据（生产只读诊断，2026-10-21）

问题：UCIE 笔记本（无知识图谱，standard 档，grounded）问 2.0/3.0 两份规范 Advanced/Standard
Package 的插损、驱动架构等参数，答案称第 5 章「未解析」。生产诊断结论：

1. **数据完好**：5.7.2.1 Loss and Crosstalk Mask、5.7.3 Standard Package（Table 5-18 数值同块）都在
   chunks 里，表标题与数值未被拆开。
2. **导航页霸榜**：全库 4468 块里目录、图表清单页只有 53 块，但生产子查询
   `UCIe Advanced Package Standard Package 插损 insertion loss 规格` 前 8 名里占 7 名；
   `UCIe insertion loss mask standard package` 前 7 名全是。导航页把整本书的标题集中在一块里，词法与
   向量融合分系统性高于正文。
3. **检索词对不上**：原文写 "VTF Loss"、"IL"、"Loss and Crosstalk Mask"，模型的检索词停在第 1 章
   用语；换成原文用词后同一条链路立即把 5.7.1/5.7.2.1/表 5-25 送进前 8。
4. **线索在 reflect 时不可见**：reflect 摘要每个 chunk 只显示前 80 字符（`RR:5408` `_ch_line`）。
5. exact_lookup 被拒的是「1.4 UCIe Key Performance Targets」——已经拿到的第 1 章；放宽形状门救不了
   本例，本计划不改 exact_lookup 形状门。
6. 结构可用但平铺：MinerU 不产面包屑，601/718 个 section_path 只有一段；图表题注也作为 section_path
   出现；同名标题会在一篇里重复（`exact_lookup.py:196-205` 的教训）；MinerU 目录表格会把词拆进不同
   单元格（`Voltage T | ransfer Function`）。整份目录 3.5～4 万字符，不能整份给模型。

`RR` = `backend/app/services/reasoning_retrieval.py`。

## 1. 范围与不做的事

做（全部在本计划内完成）：

- **A 导航页出排序**：用**文档自身结构**判定导航页（§3.1），建块时落标记，服务内自动回填存量
  （§3.2），检索各产出点剔除（§3.3）。
- **B 文档内导航**：向量章节线索块 + 两个新 reflect 动作 `browse_sections`、`read_section`。
- **C 文案**：合成答案对缺失维度只说「本次检索未取到」；修正首轮计划步摘要的数字口径。
- **D 回归集**：纯函数 + 仓储级 + 推理循环端到端的密闭回归测试；部署后只读复验作为验收（附录）。

不做（有理由）：

- 不改 `exact_probe_terms` 形状门（§0.5）。
- 不做 `read_around`：§0.1 已证明表标题与数值未拆分。
- 不给标题另建向量表：章节线索用已有 chunk 向量按章节聚合（§3.7），不新增嵌入实体与它的重嵌入、
  同步、复制、删除生命周期。
- 「引用 N 处证据」的 11 vs 25 不是缺陷：N 是去重后的 `[k]` 锚点数（`ask_service.py:7447-7467`）。

## 2. 裁决（已定）

- **D1 导航页剔除而不是沉底**（用户同意）：MMR、`quota_fuse*`、rerank、联邦合并、
  `reasoning_passage_order` 都会按 relevance 重排把沉底的块抬回来。推理模式由 `browse_sections` 覆盖
  「目录里有哪些章节」这类问题。对 exact_lookup 命中也生效。
- **D2 两个新动作与 `search_chunks` 同闸，overview 档不提供**（用户同意）。
- **D3 章节线索用向量匹配**（用户裁决，§3.7）。
- **D4 不做针对性判定规则、不做生产取样**（用户裁决）：判定只用文档自身结构（§3.1），阈值是协议
  常量，用密闭合成文档定标。生产侧只有部署后的只读复验（附录），它是验收，不用来定规则。

## 3. 契约

### 3.1 导航页判定：有序的标题自引用（纯函数，新模块 `backend/app/services/navigation_chunks.py`）

> 2026-10-22 更新：判定已改为五条通用结构条件（引用数、有序比、字母覆盖、局部性、密度），现行定义与数值见
> `docs/product-and-api.md`「导航页判定」；本节以下为原方案描述，保留作历史。

思路：目录、图表清单、章节导览的共同点不在名字，而在于**正文主要由本文档其他章节的标题组成，并且
这些标题按它们在文档里出现的先后顺序排列**。只用同一来源的结构，零 I/O、与语言无关。

- **输入**：该来源的标题元素序列（建块时 `source_elements_for_chunking` 已加载的 heading 元素，含只有
  标题没有正文的章节，如 `5`、`5.7`），以及 chunk 列表 `(section_path, text)`。回填时读同样的两份数据。
- **规范化**（标题与 chunk 正文用同一种）：casefold，只保留字母、数字与 CJK 字符，得到一条字符流。
  这样 MinerU 拆开的 `Voltage T | ransfer Function` 与点状引导线、页码分隔都不影响匹配，覆盖率也在这条
  字符流上算。
- **标题文本**：去掉开头的编号字符串（数字、`.`、`-`、括号组成的前导串，前面至多一个词，如
  `5.7.2.1`、`Figure 1-1.`、`表 3-2`、`A.2`）；去掉后不足 `NAV_MIN_TITLE_CHARS` 的用未去编号的完整
  标题；仍不足的不参与（`Notes`、`Overview`）。
- **单块度量**（chunk 先去掉 `[标签] ` 前缀）：
  - 命中 = 正文字符流里包含的**其他**章节标题（排除本块所在章节及其祖先的标题；被另一个命中完全
    包含的较短标题不重复计）；
  - `coverage` = 命中区间并集长度 / 正文字符流长度；
  - `ordered_refs` = 命中标题按「在正文里出现的位置」排序后，其文档章节序号的**最长递增子序列**长度。
- **判定**：`ordered_refs ≥ NAV_MIN_REFERENCES` 且 `coverage ≥ NAV_MIN_COVERAGE`（模块内命名的协议常量，
  初值 6 与 0.35，T1 用合成文档定标，理由写进 docstring，不进 Settings）。顺序条件是关键：参数表、术语表
  的行名即使恰好等于若干小节标题，也不会按文档章节顺序成串出现，因此不会被判成导航页。
- **性能**：先用「词元 → 标题」倒排索引预筛，只对全部词元都出现在该块里的标题做子串校验；标题数超过
  `NAV_MAX_TITLES`（协议常量）的来源跳过判定并记事件 `navigation_detection_skipped`。计算在
  `_notebook_lock` 外完成（见 §3.2），不拉长笔记本锁。

### 3.2 标记的存储、计算与回填

- **迁移**（先看 origin/master 最新号防撞号）：SQLite `_migration_101`、PG `0082_chunk_navigation.sql`（master 先落了 v99 / 0080 `ask_jobs.report_id` 与 v100 / 0081 笔记本删除窗口，本迁移顺延为下一号）：
  - `chunks.navigation INTEGER NOT NULL DEFAULT 0`（PG 也用 integer，与仓库多数 PG 标志列一致，并
    与 SQLite 同型）；
  - `sources.navigation_scanned_at`（可空时间戳）：该来源当前这批 chunk 已判定过的凭证。
  - 连带清单（`docs/development.md` Schema and migration authoring）：`SCHEMA_VERSION`、
    `POSTGRES_SCHEMA_MANIFEST` 版本对、`shadow/manifest.RUNNING_SCHEMA_PAIR`、
    `verify_repository_snapshot.MIGRATION_MANIFEST[(100, 101)]` 与两张表的列钉、`test_postgres_schema`
    迁移列表、契约夹具再生成。
  - 增量同步：PG 捕获触发器只记 `{id}` 加变更标记（`0064_sync_change_capture.sql:949-979`），导出时读
    当前行，所以新列会随 chunks / sources 一起同步；`docs/incremental-sync-design.md` 写明这一点，并
    写明回填或 `--clear` 会让目标端下一次增量导入重建派生索引。
- **建块时写入**：
  1. `build_chunks_for_source`（`source_chunking.py:296-307`）：在 `_chunk_dicts` 之后算标记，
     `ChunkWrite` 增加 `navigation` 字段（默认 False），与 chunks 同一事务写入并置
     `navigation_scanned_at`。判定本身移到 `_notebook_lock` 之外：锁内只做读元素与写入，判定在读取之后、
     写事务之前完成，且写事务内核对 `chunked_at` 未被并发改变。插件提案（`IndexingChunkProposal`）走同一处。
  2. 索引流水线切换的暂存与发布（`source_chunking.py:377-402` 暂存 payload，两侧
     `kg_build_job_store.publish_indexing_pipeline_success` 的 chunk INSERT）：payload 行带标记。
  3. 深拷贝（`sharing_store` 的 `SELECT *`）与 knowhow 跨库复制（`knowhow_transfer_store.py:211`
     `SELECT *`）原样复制该列；knowhow 投影新建的隐藏来源 chunk 取默认 0（无标题结构），注释写明。
- **存量回填：服务内自动执行**（部署后无需人工）：
  - 仿全局问答归因的就绪后后台补写（`scripts/backfill_global_ask_attribution.py:5-8`、
    `startup_warmup.py:937-950`）：服务就绪后后台扫描 `navigation_scanned_at IS NULL OR < chunked_at` 的
    来源，逐来源在 `hold_source_chunk_lock` 下判定并写入（先例：checkup 在该锁下
    `UPDATE chunks SET element_ids`，`maintenance.py:2040`）。
  - 写入是 compare-and-set：`UPDATE chunks SET navigation=? WHERE id=? AND source_id=?`，同一事务内核对
    来源 `chunked_at` 未变，否则放弃本来源（下次再扫）。只写有变化的行，然后置 `navigation_scanned_at`，
    并对涉及的笔记本 `mark_unified_dirty`。
  - 走后台轻量作业池（`_LIGHT_MAINTENANCE_OPERATIONS` 登记前缀），离线批处理期间
    （`offline_batch_active()`）不运行。`UPDATE chunks SET navigation` 是新写点，登记
    `test_memory_chunk_write_guard.REVIEWED`。
- **CLI**：`scripts/backfill_navigation_chunks.py` 只承担 `--dry-run`（统计每来源块数与命中数，
  `--show-sections` 时列命中块的章节标签）、`--clear`（数据级回滚）与手动重跑；它写库时用同样的
  compare-and-set，不依赖进程内锁。PG 上手动写入要求 `--confirm-service-stopped`（与 batch_ingest 维护
  一致），dry-run 不需要。
- **两把开关**：写侧 `NAVIGATION_DETECTION_ENABLED`（关闭时建块与后台回填一律写 0、不扫描）；读侧
  `RETRIEVAL_NAVIGATION_FILTER_ENABLED`（关闭时 §3.3 的剔除全部旁路，误判事故无需写库即可止血）。

### 3.3 剔除位置（覆盖所有产出点）

- **行构造补列**：`score_chunks` 读的是按固定键重建的 dict，新列不会自动带上。以下每个构造点都要带出
  `navigation`（缺键按 0 读）：`_hydrated_chunk_row`（`retrieval_candidates.py:311`）、`_gather_chunks`
  （`:1499`）、生成问题路径从 `RetrievedChunk` 重建 dict 处（`:3740`）、`retrieval_rows`、
  `hydrate_rows`、`graph_hydrate_rows`、`rows_by_ids`（`_kg_source_chunks` 用，`graph_retrieval.py:2150`）、
  `chunks_by_section`、`retrieval_contribution_rows`、`chunk_exact_search` 行，以及来源子图投影 SQL
  （`source_subgraph_projection.py:337`）与 `SourceSubgraphChunk`（`source_subgraph.py:97`）。来源子图
  快照缓存签名加入导航标记的版本（回填不改 `created_at`，否则旧快照不失效）。
- **守卫测试**：新增一条测试，枚举上述 chunk 行构造点，断言每个都带 `navigation`。
- 共享 helper `drop_navigation_rows(rows)` / `drop_navigation_chunks(chunks)`（同模块，读侧开关在此生效）。
  接入点：
  1. `retrieval.py::score_chunks`（:1937）：按行标记跳过；`_CANDIDATE_SOURCE_SINK` 登记**保持在过滤
     之前**（它的语义是「候选池里出现过谁」，供天花板核验）。
  2. 不经过 `score_chunks` 的产出点：PPR 输出（`graph_retrieval.py:1449`）、`_kg_source_chunks`
     （`:2180`）、`source_graph_activation.py:98`/`:234`、exact_lookup 构造（`exact_lookup.py:381` 一带）。
  3. `test_retrieved_hit_origin_guard.ORIGIN_REGISTRY` 每个登记项增加「是否经过导航过滤」字段，新增产出
     点必须显式选择。
- **元素通道**：元素行水合时经 `chunk_elements` 反查，凡出现在导航块 `element_ids` 里的元素同样剔除
  （`score_elements`，`retrieval.py:741` 一带）；不引入新度量。

### 3.4 章节大纲：按「标题出现」寻址（查询时读取，不建表）

- `ChunkStorePort.source_outline_rows(db, notebook_id, source_id, max_scan)`：按文档序（PG `ordinal`、
  SQLite `rowid`）`ORDER BY … LIMIT max_scan` 取 `(id, section_path, navigation)`，先截断再返回，扫描
  有界，并返回是否截断；不读正文。由 `idx_chunks_source_ordinal` / `idx_chunks_source` 支撑。
- 纯函数（新模块 `backend/app/services/section_outline.py`）把行折成**标题出现**：文档序里相邻且
  section_path 相同的一段 chunk 是一次出现，记 `(label, chunk_ids, index)`；全部为导航页的出现不进大纲。
- 层级（通用，不依赖词表）：面包屑型按 ` > ` 段数；平铺型带编号的（标题以点分编号开头，允许一个前导
  字母，如 `5.7.2.1`、`A.2`）按编号长度；其余标题（图表题注、`Notes`）是「叶」，不结束所在的带号章节。
  只有子节有块的编号父节点（如 `5.7`）**合成为虚节点**，范围为其全部子孙，使 `read_section("5.7")` 与
  目录树成立。
- **节的范围**：从它开始到下一个同级或更高级出现为止；`5.7.3` 因此包含 `5.7.3.1`、其后的表格题注块与
  `Notes` 块。叶出现的范围只到下一个任意出现。整篇都是叶的文档里不做子树扩展。
- `under` 按编号前缀（`5.7` 命中 `5.7` 与 `5.7.*`，不命中 `5.70`）；无编号文档里 `under` 不可用并回喂说明。
- 渲染：每行 `[sN] 标题（N 段）`，`sN` 是本 run 内地址号，按深度缩进，总字符受
  `REASONING_SECTION_BROWSE_MAX_CHARS` 约束，截断时注明「另有 K 条未列出，可用 under/query 收窄」；扫描
  截断时注明「大纲只覆盖本篇前 N 段」。
- `ports_protocol_method_count`：PR-2 开工时 master 为 996，本 PR 加 1 个 `ChunkStorePort` + 3 个 `RetrievalPort` → 1000（同 diff 改 baseline，以开工时实际值为准）。

### 3.5 地址与封闭集合

- `state.surfaced_sections: dict[str, SectionAddress]`，`SectionAddress = (notebook_id, source_id,
  chunk_ids, label)`；`chunk_ids` 是该节范围内按文档序的 chunk id（跨库同步、深拷贝后仍可校验），
  `notebook_id` 取行自己的属主（空串 = 当前库）。
- 进入集合：章节线索块、`browse_sections` 结果。
- `read_section.section` 接受 `sN`；也接受标题文本，此时在已缓存大纲里解析：唯一 → 采用；多处出现 →
  优先包含本 run 已有 chunk 的那一处；仍不唯一 → skip `section_ambiguous` 并回喂各处 `sN`。不在缓存大纲里
  → skip `section_not_surfaced`。地址是封闭集合，不经过 `exact_probe_terms`。

### 3.6 分层与权限（检索层负责）

- `RetrievalPort.section_outline(notebook_id, source_id)`、
  `RetrievalPort.section_chunk_similarities(notebook_id, source_id, chunk_ids, query_texts)`（§3.7；返回
  每个 chunk 对各查询向量余弦的最大值，按节取最大由纯函数 `section_outline` 完成）、
  `RetrievalPort.section_chunks(notebook_id, source_id, chunk_ids, limit, question)`（§3.9），由
  `RetrievalService` 实现、委托 `CandidateRetrievalService`。
- 权限直接用 `source_scope`（`scope.allows(notebook_id, source_id)` + subjectless run 的
  `source_ceiling_for(notebook_id) is None` 拒绝；非本库——参考库与 subjectless run 的每个库——还要求
  来源在该库可见来源集合内，与 chunk 通道对等腿同一份 `_peer_visible_sources`，他人 Memory / Knowhow
  投影不可大纲、不可读取），**不 import** `RR` 里的 `document_source_admitted`（向上依赖）。越界与不存在
  返回同一个空结果；天花板在每次读 memo 之前判，memo 不替权限作答。
- `section_chunks` 先校验 `chunk_ids` 全部属于该 `(notebook_id, source_id)`，再读行，输出过
  `drop_navigation_chunks` 与 `filter_retrieval_items(notebook_id, "chunk", …)`；推理侧调用经过
  `retrieval_fanout_slot()` 与 `_filter_candidates("chunk", …)`，与 `search_chunks` 同路（`RR:4727-4750`）。
- run 内记忆，键各自独立：大纲按 `(notebook_id, source_id)`；笔记本矩阵是否已暖按 `notebook_id`（每库每
  run 至多一次版本聚合，非 copyable 大库一次也不付）；该来源的 chunk 向量按 `(notebook_id, source_id,
  查询维)`（新增子查询不重复解码；单源约 `max_rows × 运行时维 × 4B`，整个 run 的向量缓存另有字节上限
  `SECTION_VECTOR_RUN_MEMO_MAX_BYTES`，按 LRU 淘汰）。**不做**第三份「章节分数」memo：分数是已缓存
  向量上的一次小矩阵乘，毫秒级现算。
- 进程级向量缓存（跨 run 复用，线索块默认开、每次推理都要读）：键 `(数据库指纹, notebook_id, source_id,
  chunked_at, 大纲 id 指纹, 查询维)`，`chunked_at` 随大纲同一条查询取出（主键子查询）；数据库指纹是整条
  `DATABASE_URL` 的摘要（含 search_path，两个库/schema 同 id 不串）；只缓存每个大纲块都有向量的来源；
  字节上限 `REASONING_SECTION_VECTOR_CACHE_MAX_BYTES`（默认 256MiB，0 = 关闭），LRU 淘汰。两级缓存都
  single-flight（报告多节并发同一来源只解码一次）。已知局限：同维换嵌入模型原地重嵌不改分块时，要等淘汰
  才看到新向量（仓库没有嵌入漂移信号）。
- `read_section` 打分只读这 ≤limit 段的向量（缓存里已有整源时直接用）；`section_retrieval
  .section_vector_stats()` 给出本 run 的 `vectors_read` / `vector_ms` / `vector_loads` /
  `process_cache_hits`，供 trace 放稀疏键。

### 3.7 向量章节线索

- **打分**：一个大纲出现的分数 = 该节范围内 chunk 向量与本 run 查询向量（问题原文 + 已执行的查询，
  含跑过的已确认方向；未执行的方向不参与，免得为没贡献过证据的文本多付嵌入）余弦相似度的最大值。
  - 查询向量走 `memoized_query_embedding`（本 run 里大多已算过，命中缓存）。
  - chunk 向量：笔记本矩阵已在缓存里时（`_vector_matrix_warm` 只窥视、不触发加载）直接按 id 取行；
    否则 `vector_rows_for_ids` 读该来源大纲涉及的 chunk（≤ `REASONING_SECTION_OUTLINE_MAX_ROWS`），
    `build_matrix` 统一运行时维度截断；维度不符返回空并记事件。
  - 只用 chunk 向量、不另建标题向量的理由：chunk 文本自带 `[标题]` 前缀（`chunking.py:25`），向量已含
    标题语义；新表会带来新的嵌入生命周期，而仓库今天对嵌入模型漂移没有检测。
- **线索块**：
  - 来源：本 run `state.chunks` 里出现过的来源（首次出现序，最多 `REASONING_SECTION_HINT_SOURCES` 个），
    只取大纲出现 ≥ 2 的来源。
  - 选择：排除本 run 已有 chunk 覆盖的节与导航节；**按名次取**，跨来源合并后取前
    `REASONING_SECTION_HINT_MAX_ENTRIES` 条，且只保留分数 ≥ 本 run 已取得 chunk 最高语义相似度 ×
    `REASONING_SECTION_HINT_RELATIVE_FLOOR` 的条目（相对阈值，换嵌入模型不失效）。
  - **惰性计算**：在 `_reflect_round_context_blocks`（`RR:8223`，无行数上限）里计算；新来源进入或本轮
    新增子查询时增量重算。不改 `run`（baseline 23）与 `_run_reflect_loop`。
  - 位置：紧邻 `consult_block_text` / `external_block_text`，在 `collection_map_text` 之前（那一对不能
    拆）。状态字段用 `_ReasoningRunState` 上的 `default_factory`，不碰 `_new_run_state`。
  - 文案：标题「相关章节线索（可用 read_section 按章节读取原文）」，每条
    `[s3]《来源标题》 5.7.2.1 Loss and Crosstalk Mask（3 段）`。
  - 失败开放：向量不可用、来源无大纲时不出块，trace 不报错。
- **验收**：附录的生产只读复验必须报告 Q1–Q3 下 5.7.2.1 与 5.7.3 在线索里的名次；进入前 8 才算达标。
  合成 embedder 的端到端测试只能证明接线，不能证明这一点。

### 3.8 `browse_sections` 动作

- 参数：`browse_sections: {source: "", under: "", query: ""}`。`query` 非空时按 §3.7 的打分在该来源
  （及 `under` 范围）内按名次取前若干节；为空时按文档序列出章节树。
- `source` 按标题解析，候选 = 本 run 已出现的来源（`state.chunks` 来源 ∪ 来源清单行），复用
  `_resolve_roster_rows` 的解析与「0 个 / 多个匹配」措辞；越界与不存在不可区分。
- 产出不进证据。每个来源只保留**最近一次** browse 的结果进下一轮 reflect 摘要（替换，不累加），更早的
  只在账目里留一行「已浏览：《标题》 under 5.7」。列出的地址进入 `state.surfaced_sections`。
- 停滞判定：本轮有大纲交付时置 `section_listing_delivered_this_turn`，**保持** stale 级别（不重置也不
  推进），沿用 `RR:8104-8109` 无证据交付的先例。
- 预算：每 run 上限 `REASONING_MAX_SECTION_BROWSES`（用完后动作不进 enum、不出说明）；同一
  `(source, under, query)` 重复 → **不 skip**：零 I/O 地把缓存目录放回摘要，记一条 `browse_sections`
  步（detail `repeat: true`），不扣次数、不持平 stale。
- `under` 下没有条目 → skip `section_under_empty`（不扣次数，不覆盖同源上一份目录）。
- 读大纲或按 query 读向量失败 → skip `section_browse_failed`（扣一次，detail 带异常类名；`AskCancelled`
  与 `RetrievalControlError` 照抛），整次提问不作废。
- skip 原因码：`section_nav_disabled`、`section_browse_cap`、`section_source_unresolved`、
  `section_outline_empty`、`section_under_unsupported`、`section_under_empty`、`section_browse_failed`。

### 3.9 `read_section` 动作

- 参数：`read_section: {source: "", section: ""}`（§3.5 解析规则）。
- 取数：`section_chunks(addr.notebook_id or state.notebook_id, addr.source_id, addr.chunk_ids,
  limit, question)`——把**整节** id 交给端口，由端口按文档序截取（导航页先剔除、不占名额，再取前 `limit`
  段；端口另把 `limit` 钳到 `REASONING_SECTION_READ_MAX_CHUNKS`、按批读行），
  `limit = REASONING_SECTION_READ_MAX_CHUNKS`，按文档序、`take_distinct_chunk_hits` 去重进
  `state.chunks`，**不开新证据号段**；超出上限时 trace 注明「截取本节前 N 段」。
- **relevance**：每块对问题的融合分，与普通 chunk 检索同一把尺子——`_fuse(关键词覆盖, 该块向量与问题
  向量的余弦)`，不设 floor；无论地址来自线索、browse 还是标题文本，都按这一条算。
- **不设 `exact_lookup` 标志**（它表示「用户点名」，驱动 `REASONING_EXACT_RESERVE` 前排席位
  `retrieval.py:1661-1666` 与大纲合成逐节注入 `outline_synthesis.py:196-203`）。新增 provenance 标志
  `section_read: bool = field(default=False, compare=False)`（`domain/retrieval.py`），
  `prefer_stronger_chunk_candidate` 同步合并（与 `exact_lookup` 同法，`retrieval.py:1737-1739`）。
  `reasoning_passage_order` 为 `section_read` 块保留席位 `REASONING_SECTION_READ_RESERVE`（排在 exact
  预留之后），保证读回的整节在 `chunk_context_chars` 分区里存活；大纲合成不对它逐节注入。新增
  `RetrievedChunk(...)` 构造点登记 `NAVIGATION_REGISTRY`（它传入属主库 `notebook_id`，按守卫规则不进
  `ORIGIN_REGISTRY`）。
- 预算：每 run 上限 `REASONING_MAX_SECTION_READS`；同地址重复 → skip `section_read_repeat`；零命中计入
  `zero_hit_by_action["read_section"]`。
- 读大纲（按标题解析）或读回整节失败 → skip `section_read_failed`（扣一次，同上）。次数用完后动作
  不进 enum、不出说明，线索块也不再计算。
- skip 原因码：`section_nav_disabled`、`section_read_cap`、`section_source_unresolved`、
  `section_not_surfaced`、`section_ambiguous`、`section_read_repeat`、`section_read_failed`。
- `exact_lookup` 形状门拒绝的回喂（`_NOT_A_NAME_NOTE`）在 `read_section` 提供时追加一句「如果这是章节
  标题，请改用 browse_sections 或 read_section」。

### 3.10 闸门

`section_navigation_active(limits)`：`settings.reasoning_section_nav_enabled` ∧ 调用方策略位
`allow_section_nav`（knowhow 补全关掉；knowhow 已关 `allow_search_chunks`，双保险）∧
`chunk_search_active()` ∧ 档位 ≠ overview ∧ 端口在场（先例 `element_search_offered(self.retrieval)`）∧
两个上限之和 > 0 ∧ 大纲扫描上限 > 0。同一判据被 prompt、schema、`allowed_actions`、解析、执行体第一道
纵深防御、`_first_round_offered_actions`、线索块计算共读；两个动作再各看自己的上限。
`core_reflect_actions` 新增**必填**关键字参数 `section_nav`。

### 3.11 Settings（`backend/app/core/config.py`，均带 alias 与范围校验）

| 字段 / env | 默认 | 含义 |
| --- | --- | --- |
| `navigation_detection_enabled` / `NAVIGATION_DETECTION_ENABLED` | true | 写侧：建块与后台回填是否判定 |
| `retrieval_navigation_filter_enabled` / `RETRIEVAL_NAVIGATION_FILTER_ENABLED` | true | 读侧：检索是否剔除导航块 |
| `reasoning_section_nav_enabled` / `REASONING_SECTION_NAV_ENABLED` | true | 导航动作与线索块总闸 |
| `reasoning_max_section_browses` / `REASONING_MAX_SECTION_BROWSES` | 3 | 每 run browse 次数 |
| `reasoning_max_section_reads` / `REASONING_MAX_SECTION_READS` | 3 | 每 run read 次数 |
| `reasoning_section_read_max_chunks` / `REASONING_SECTION_READ_MAX_CHUNKS` | 12 | 单次 read 的 chunk 上限 |
| `reasoning_section_read_reserve` / `REASONING_SECTION_READ_RESERVE` | 6 | 合成时 section_read 块保留席位 |
| `reasoning_section_browse_max_chars` / `REASONING_SECTION_BROWSE_MAX_CHARS` | 3000 | 单次 browse 渲染字符上限 |
| `reasoning_section_hint_max_entries` / `REASONING_SECTION_HINT_MAX_ENTRIES` | 8 | 线索块条目上限 |
| `reasoning_section_hint_sources` / `REASONING_SECTION_HINT_SOURCES` | 4 | 线索块覆盖的来源数上限 |
| `reasoning_section_hint_relative_floor` / `REASONING_SECTION_HINT_RELATIVE_FLOOR` | 0.5 | 线索相对阈值（占本 run 最高 chunk 语义相似度的比例；基准来自已覆盖的高分块，而线索只列未覆盖节，0.8 会挡住目标节） |
| `reasoning_section_outline_max_rows` / `REASONING_SECTION_OUTLINE_MAX_ROWS` | 4000 | 单源大纲扫描块数硬界 |
| `reasoning_section_vector_cache_max_bytes` / `REASONING_SECTION_VECTOR_CACHE_MAX_BYTES` | 268435456 | 章节向量进程级缓存字节上限（0 = 关闭） |

`ReasoningActionPolicy`（`reports/policy.py`）加 `max_section_browses` / `max_section_reads`。
精确数值写进 `docs/product-and-api*.md`，env 登记进 `docs/deployment-and-configuration*.md`，并注明：已有
插件动作名为 `browse_sections` / `read_section` 的部署升级后启动会被拒（保留名冲突）；插件提出的「章节
导览」类 chunk 同样会被判为导航页（写进 `docs/deployment-extensions-sop*.md`）。

### 3.12 合成文案

- 规则写进 `answer_prompt` 的字面规则（扩展 rule 12，`prompts.py:428-435`），不写进可覆盖的 L1 片段
  `answer.style_language`（被 `document_guide` 共用）：证据里没有问题要的某项内容时，只说明「本次检索到
  的资料中没有该项」，不得归因于解析、抽取、索引或资料库覆盖范围，除非某条证据明确这样写。报告章节
  合成已有同义约束（`prompts.py:243-287`），不改。
- 首轮计划步摘要（`RR:6112-6121`）：有待补方向时改为「首轮执行 N 个检索方向，另有 M 个已确认方向视预算
  在后续轮次补检」（覆盖轮只用一半步数预算，`RR:7562-7577`，不承诺全部执行），无待补时保持原句；
  `detail` 增加 `pending_count`。过 `scripts/check_ui_vocabulary.py`。

### 3.13 trace、前端与经验词表

- 新 step type `browse_sections`、`read_section` 进 `domain/reasoning_trace_stats.STEP_TYPES`；read 的
  detail 带 `found`、`source`、`section`、`result_ids`（`_capped_result_ids`）；browse 的 detail 用
  `listed`（不在 `_TRACE_COUNT_KEYS` 里，经验统计不把列目录算作证据命中）、`source`、`under`、`query`；
  skip 步不写 `result_ids`（`test_trace_result_ids`）。
- 摘要：`查看章节目录:《标题》 under 5.7,列出 N 条`；`按章节读取原文:《标题》 5.7.3 Standard
  Package,新增 N 段`。
- 前端 `frontend/app/reasoning-trace.ts`：`TRACE_STEP_LABELS` 加 `browse_sections: "章节目录"`、
  `read_section: "按章节读取"`；`NEXT_ACTION` 加动词短语；detail 渲染分支在通用 `found` 分支之前。
  `frontend/features/agent-profile/profile-model.ts` 的 `EXPERIENCE_ACTION_LABELS` 同步。用词核对
  `docs/ui-vocabulary.md`。
- 经验词表：`RETRIEVAL_ACTIONS` **末尾追加**两个词，`RetrievalAction` Literal、`_ACTION_IDS`、
  `ADOPTION_ACTIONS` 同步。

## 4. 任务

### PR-2 开工前置（2026-10-22，PR-1 已合 #971）

- PR-1 已落地：`chunks.navigation` / `sources.navigation_version`（SQLite v101 / PG 0082）、`app/services/navigation_filter.py`
  （`drop_navigation_rows` / `drop_navigation_chunks` / `navigation_filter_enabled`）、构造点守卫
  `test_retrieved_hit_origin_guard`（每个新 `RetrievedChunk` / `RetrievedElement` 构造点必须登记导航处理）。
- 因此：大纲读取带 `navigation`，全为导航页的出现不进大纲与线索；`section_chunks` 输出过 `drop_navigation_rows`；
  `read_section` 新构造点（`section_retrieval.section_chunks`）传入属主库 `notebook_id`，只登记
  `NAVIGATION_REGISTRY`（navigation-filtered）；守卫规定点名库归属的构造点不进 `ORIGIN_REGISTRY`。
- 本 PR 无迁移。

全局约定：

- `PY=/opt/homebrew/Caskroom/miniconda/base/bin/python`；后端测试在 `$R/backend` 下
  `SILICON_NOTEBOOK_ENV_FILE="" MODEL_SERVICES_CONFIG="" EXTENSIONS_CONFIG="" $PY -m pytest -p no:cacheprovider -n 4 <files>`。
  全门 `bash scripts/check.sh` 与 PG 泳道一律经 `heavy-gate.sh` 串行。
- 每个实现任务完成后跑 `spec-review` 与 `code-quality-review`，修完再推进。
- 两个 PR，按序合入，都在本程序内完成：**PR-1 = T1–T3**（导航判定 + 迁移 + 写入与后台回填 + 剔除 +
  文案 + 回归第一部分 + 对应文档与发布说明），**PR-2 = T4–T9**（导航工具）。PR-2 在 PR-1 合入后 rebase
  到 master 再开。每个 PR 自带它改变的 canonical 文档。
- 热函数行数 baseline 零松弛；ports 计数 baseline 同 diff 调整。

### T1 — 导航判定、迁移、建块写入、后台回填与 CLI（opus）

1. `navigation_chunks.py`（§3.1）与 `backend/tests/test_navigation_chunks.py`。合成文档覆盖：带编号规范
   （目录 + 图表清单 + 正文，含 MinerU 式拆词与点状引导线）、面包屑 markdown（有「目录」节 + 正文偶尔
   引用章节）、中文无空格编号标题（`5.7.2.1损耗`）、章首导览段落、**行名等于小节标题的参数表（不得
   判为导航）**、只有少量标题的短文档、同名短标题、附录编号（`A.2`）、超过 `NAV_MAX_TITLES` 的来源。
   阈值用这些用例定标。
2. 迁移与连带清单（§3.2），`ChunkWrite.navigation`，建块（判定在锁外）与流水线暂存/发布写入。
3. 服务内后台回填（就绪后调度、轻量作业池、compare-and-set、`mark_unified_dirty`、离线批处理期间不跑）
   与 CLI（dry-run、`--clear`、手动重跑），`REVIEWED` 登记。测试：并发重建时放弃、续跑、开关关闭时不扫描。
4. 文档：`docs/operations.md` / `_zh.md`（自动回填行为、dry-run 与 `--clear` 用法）；
   `docs/deployment-and-configuration*.md` 两把开关；`docs/incremental-sync-design.md`；`scripts/README.md`。

### T2 — 剔除接入 + 合成文案（opus）

1. §3.3：所有行构造点补列 + 守卫测试、来源子图快照签名、`score_chunks` 与绕行产出点接入、元素通道、
   `ORIGIN_REGISTRY` 字段、读侧开关。测试：`test_chunk_retrieval.py`（导航块向量分最高也不出现；sink 仍含
   其 source_id；读侧开关关闭时恢复原状）、各绕行产出点一条、跑 `test_chunk_retrieval*.py
   test_exact_lookup.py test_peer_mode_keyword_arm.py test_chunk_federation.py test_search_chunks_federation.py
   test_quota_fuse.py test_mmr.py test_reasoning_chunk_rerank.py test_retrieved_hit_origin_guard.py` 与 PG
   一致性测试。
2. §3.12 两项，更新 `test_prompts.py`（rule 12 段）、`test_language_policy.py`、
   `test_answer_json_output_prompt.py`（若尾部变化）。
3. 文档：`docs/product-and-api*.md` 写明「目录、图表清单类页面不作为检索证据」与 rule 12 段；
   `architecture.md` 一句话说明标记在建块层算、在检索产出点剔除。

### T3 — 回归集第一部分 + PR-1 收尾（opus）

- 新 `backend/tests/test_navigation_chunk_regression.py`：带标题的播种器（现有 `_seed_chunks` 只插
  paragraph 元素、造不出标题结构）写入 heading + paragraph 元素后走真实 `_chunk_and_embed_source`；把
  `FakeEmbedder` 换成按关键词给向量分量的确定性 embedder（`bind_all_embedding_clients` 重绑），让导航块
  向量分最高以复现生产形态。对生产 Q1–Q3 原句断言：`retrieve_chunk_candidates` 与
  `select_chunk_candidates(k=8)` 里导航块为 0，5.7.x 正文块至少一条进前 8；元素通道同样不出现导航元素。
- 发布说明 `release-notes/<slug>.md`（`level: fix`，写用户可见结果，如「问答不再被目录页和图表清单页
  占满结果」）。
- PR-1：全门 + PG 泳道 + codex 评审 + 合入。

### T4 — 大纲与章节端口（opus）

§3.4–§3.6：`source_outline_rows` 两侧适配器 + 三个 `RetrievalPort` 方法与服务实现（权限先行、三份 run 内
记忆、chunk_ids 归属校验、向量窥视与按 id 读取、维度处理）。测试：SQLite 单测（平铺 / 面包屑 / 同名标题
多处 / 空 path / 扫描截断）、`tests/postgres/test_search_conformance.py` 两侧对等、越界不可区分、挂载库
notebook_id 路由（仿 `test_exact_lookup.py:133-150`）、`test_repository_protocol_coverage.py` 生产调用点。
baseline 996 → 1000。

### T5 — 大纲投影与线索选择纯函数（opus）

§3.4、§3.7 的纯函数部分与 `backend/tests/test_section_outline.py`：出现折叠、层级（`5.70` 不属于 `5.7`、
`A.2` 可展开）、虚父节点、节范围（叶不截断带号章节）、整篇叶文档不展开、`under`、按给定相似度矩阵的
线索选择（排除已覆盖节与导航节、名次与相对阈值、条目上限）、渲染截断提示。

### T6 — reflect 接线（opus）

按 §3.5、§3.7–§3.11、§3.13 后端部分逐项接线：

- 常量与 `core_reflect_actions(section_nav=…)`；`RR:2755` 与 `domain/reflect_action.py:129-146` **两份**
  `CORE_REFLECT_ACTION_IDS`；`REFLECT_SCHEMA_TOP_LEVEL_FIELDS`；
  `test_reflect_action_contract._KNOWN_REFLECT_SCHEMA_GATES`。
- `prompts.reflect_prompt` / `reflect_schema_hint`：参数、动作说明（「线索里有对应缺口的章节时优先
  read_section；不知道文档用词时用 browse_sections(query=…)」）、schema 分支、next_action 枚举；更新
  `test_model_schema_hint_snapshots.py` 与 `test_peer_mode_reflect_actions.py` 的 prompt+schema 摘要。
- `reflect()` 解析分支、`ReflectDecision` 字段、`bounded_fields` 硬闸追加新文本字段、fail_closed 缺参检查。
- `_dispatch_reflect_action` 分支；新执行体 `_action_browse_sections` / `_action_read_section` 与线索计算
  方法（独立方法，登记 `test_reasoning_run_state_authority` 的 `GUARDED`）。
- 线索块接入 `_reflect_round_context_blocks`；`legacy_action_ledger_note`「已浏览 / 已读章节，勿重复」
  账目；`section_listing_delivered_this_turn` 的 stale 保持。
- `section_read` 标志、`prefer_stronger_chunk_candidate`、`reasoning_passage_order` 保留席位、`NAVIGATION_REGISTRY`。
- `_ZERO_HIT_TRACKED_ACTIONS` 追加 `read_section`；`STEP_TYPES`；经验词表四处；`_NOT_A_NAME_NOTE` 追加句；
  Settings 与 `ReasoningActionPolicy`。
- 测试：新 `backend/tests/test_reasoning_section_nav.py`（闸门矩阵；两动作每个 skip 原因码；越界不可区分；
  对等模式 / 挂载库路由；重复与上限；browse 不推进也不重置 stale；`section_read` 块在 `chunk_context_chars`
  预算下存活；三条地址来源的 relevance 都有定义；向量不可用时线索块失败开放；新增子查询不重复解码向量）；
  接线清单里的既有测试（`test_reasoning_loop_characterization.py`、`test_trace_result_ids.py`、
  `test_experience_offered_actions.py`、`test_retrieval_experience_job.py`、`test_reasoning_trace_stats.py`、
  `test_prompts.py`、`test_reflect_action_contract.py`）。
- 落地取舍（2026-10-22）：①`read_section` 按标题/编号读取时，该来源的大纲若本 run 尚未建过就当场建一次
  （仍是 run 内唯一一份，地址簿依赖它不变）；候选来源只来自本 run 已出现的来源，封闭集合不变。②导航账目
  列出每一次浏览与读取（含被替换的那份目录对应的浏览）以及各条教学式 skip，目录段本身每来源只留最近一次。
  ③`browse_sections(query=…)` 拿不到向量相似度时退回按文档序列出，并在目录抬头注明「向量不可用」。④线索块
  的相对阈值基准取本 run 已有 chunk 在同一组查询向量下的最高余弦（同一把尺子）；没有任何已有 chunk 拿得到
  余弦时不出块。⑤`section_read` 保留席位不按库拆分；按库分席位的保底规则用 `is_active_hit` 归一，本库块戳
  本库 id 与不戳 id 是同一个库（`test_reasoning_section_nav` 钉住）。

### T7 — 前端（sonnet，impl-task）

§3.13 前端部分 + `frontend/tests/unit/reasoning-trace.test.mjs`、`frontend/tests/unit/agent-profile-model.test.mjs`。
先确认 `frontend/node_modules` 软链，不跑 npm install。

### T8 — 回归集第二部分：推理循环端到端（opus）

在 T3 夹具上跑真实 `ReasoningRetriever`，reflect 用脚本化假模型：

1. 生产 Q1 的查询集下，线索块列出 `5.7.2.1 Loss and Crosstalk Mask`，1.4 等已覆盖节不重复出现；模型选
   `read_section(sN)`，断言 5.7.2.1 进入最终候选，trace 有 `read_section` 且 `found>0`。
2. `browse_sections(under="5.7")` → `read_section("5.7.3 Standard Package")`，断言带回 5.7.3.1 与含
   Table 5-18 数值的块；`read_section("5.7")` 经虚父节点带回整个 5.7。
3. 同名标题多处出现时按标题文本读取 → `section_ambiguous`，按 `sN` 读取成功。
4. `browse_sections(query="insertion loss")` 在来源内把 5.7.2.1 排在前列。

### T9 — 文档、发布说明与 PR-2 收尾（sonnet 写文档，收尾由主 agent）

- `docs/product-and-api.md` / `_zh.md`：动作计数句（`:3524` 一带，计数跟随
  `domain/reflect_action.CORE_REFLECT_ACTION_IDS`）、插件红线里的核心动作 id 数与 schema 字段数（zh
  `:3473` 一带）、新动作小节、精确上限、无图谱笔记本段落。
- `docs/deployment-and-configuration.md` / `_zh.md`：§3.11 env 与插件保留名冲突；
  `docs/deployment-extensions-sop*.md`：插件章节导览 chunk 会被判为导航页。
- `architecture.md`：大纲端口与章节导航一句话。
- 发布说明：PR-1 那条若未发布则扩展为同一条，加「能按章节查找和读取规范原文」；若已发布则新开一条
  `level: change`。
- PR-2：全门 + PG 泳道 + codex 评审 + 合入；部署后按附录复验。

## 附录：部署后只读复验（验收）

PR-1 部署后服务会自动回填；PR-2 部署后把下面的 prompt 交给生产 agent。它是**验收**：检查导航页是否
已出排序、向量线索在真实嵌入模型下能否把第 5 章送到模型眼前，不用于制定判定规则。

```text
只读任务，约束同上次诊断（SET default_transaction_read_only = on；不写库、不改配置；Python 侧只用
create_repository(settings, migrate=False, seed=False)）。ID 一律用别名，按上次规则脱敏。
1. 统计 NB 各来源 navigation=1 的块数与这些块的 section_path 分布（只列标签，不列正文），以及
   navigation_scanned_at 为空的来源数。
2. 对 NB 跑 retrieve_chunk_candidates 与 select_chunk_candidates(k=8)：
   Q1 'UCIe Advanced Package Standard Package 插损 insertion loss 规格'
   Q2 'UCIe Advanced Package reach channel bandwidth density per direction'
   Q3 'UCIe insertion loss mask standard package'
   Q4 'Loss and Crosstalk Mask VTF'
   每条输出前 8 名（section_path + 前 60 字符 + navigation 标记）与第 5 章块的名次。
3. 输出 SRC-A 的 section_outline 前 40 条；以 Q1–Q3 为查询文本、排除第 1 章已覆盖节，计算章节线索前 8 条
   与分数，并单独报告 5.7.2.1 与 5.7.3 的名次（验收线：进入前 8）。
4. 若用户愿意重新提问原问题，附上新答案的 trace 步骤表（是否出现 read_section / browse_sections，
   found / listed 多少）。
```
