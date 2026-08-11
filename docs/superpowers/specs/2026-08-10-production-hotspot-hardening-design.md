# 生产热点整改（2026-08-10 审计第二轮）设计

基于 2026-08-10 对 `origin/master`（d5733f2e）的静态复杂度审计。六类 P0 热点全部经代码核实属实；
本设计给出五批整改方案。P1/P2 项登记在文末，不在本轮实现。

核心原则（承 CLAUDE.md「效率是一等约束」与「数值上限与截断」红线）：

- 纯性能改造必须**数值/行为等价**，用 old-vs-new 等价测试钉住；
- 会改变结果的截断一律走带校验的 `Settings` 预算 + 可见披露，精确数值只登记
  `docs/product-and-api*.md`；
- 近似化（ANN 替代全比较）只在超过规模阈值时启用，阈值以下逐位保持既有行为。

## 批 1：冲突检测有界化

现状：`conflict_detect.py` Strategy 3（`_discriminative_conflict` 全对）与 Strategy 4
（同 type 组内全对余弦）的 `_MAX_DISC_PAIRS`/`_MAX_SEM_PAIRS` 只限「命中数」不限「比较数」，
零命中时仍是 O(N²)；`conflict_resolution_rows` 一次拉全 notebook 对象 payload+evidence+
全部向量；`/kg/conflicts/resolve` 无单飞、无规模准入，每次 POST 起新线程；LLM 裁决逐条
串行且 edge 策略候选无全局上限。前端目前没有任何调用该端点的入口（无 UI 负担）。

改造：

1. **Strategy 3 无损分桶**。`_discriminative_conflict(a,b)` 为真 ⟺ 存在对立组 g 使
   `Counter(tokens_a) - {ta} == Counter(tokens_b) - {tb}` 且 `ta≠tb`、`ta,tb ∈ g`。
   按 `(组序号, 剩余 token 多重集排序元组)` 建倒排桶，桶内跨 token 配对即候选全集；
   把候选对按 `(i,j)` 原遍历序排序后逐个提交，直到 `_MAX_DISC_PAIRS`——与旧实现
   （外层 i、内层 j 的字典序发现顺序）**逐位等价**。每对提交前用原
   `_discriminative_conflict` 复核一遍（成本可忽略，防桶实现偏差）。
2. **Strategy 4 规模分派**。组内节点数 ≤ `KG_CONFLICT_SEMANTIC_BRUTEFORCE_MAX`
   （默认 2000）保持原逐对实现逐位不变；超过时改用 hnswlib cosine ANN（仓库既有模式，
   conflict_detect 注释本就预留此升级路径）：组内建索引、`knn_query` 批量取
   `KG_CONFLICT_SEMANTIC_ANN_K`（默认 10）近邻、`sim ≥ threshold` 的无序对去重后按
   原序提交，沿用 `_MAX_SEM_PAIRS` 与 discriminative→semantic 升级逻辑。ANN 是近似
   召回，这是**登记接受的行为差异**（只影响超过阈值的大库；今天这些库根本跑不完）。
3. **读取瘦身**。对象读取去掉 evidence 列；向量读取在 SQL 侧限定
   `object_type IN ('concept','claim') AND status != 'deprecated'`；关系读取改瘦投影
   （id/端点/edge_type/review_status，不带 evidence）。候选确定后按 candidate refs
   有界补读 evidence（node 侧复用既有 `object_evidence_rows`；edge 侧新增按 id 批量
   的关系 evidence 查询，两后端同修）。
4. **候选总上限**。新增 `KG_CONFLICT_MAX_CANDIDATES`（默认 800）截断进入 LLM 裁决的
   候选总数（检测器产出序确定），截断计数进返回值与事件日志（脱敏、只计数）。
5. **单飞与准入**。runtime 持有独立于 relink/rebuild 槽的 per-notebook 冲突检测单飞
   （复用 `KgMaintenanceJobs` 机制、独立实例，不与 relink/rebuild 共槽——它们互斥是因
   为共享派生产物，冲突检测不写那些产物）；占用时 `user_error(409, "当前笔记本正在检
   测知识冲突，请等它完成")`。规模准入：route 预检活跃对象数超过
   `KG_CONFLICT_MAX_OBJECTS`（默认 200000）时 `user_error(409, ...)` 拒绝，不再把
   超大库放进注定跑不完的后台任务。

## 批 2：viz 可视化产物紧凑化

现状：`viz_edges` 以 `list[[src,dst,edge_type],...]` 形式在 `load_scale_index` 时全量
json 物化并随 ScaleIndex 常驻 LRU（启动预热即物化）；`_unified_graph_bounded` 每请求
对全部节点 Python `sorted` + 全部边线性扫描 + 全量建 name/type 字典；
`_kg_neighbors_unchecked` 每次点邻居用全部 viz_edges 重建 `(s,t)→et` 字典；
`viz_neighbors` 每次调用对全部 viz_ids 重建 id→index 映射。

改造：

1. **紧凑存储**。viz.npz 新增 `viz_edge_src`/`viz_edge_dst`（int32，指向 viz_ids 下标）
   与 `viz_edge_code`（uint16）+ `viz_edge_type_table`（唯一 edge_type 字符串表；不硬编
   码 12 内置边，兼容管理员扩展类型）。旧 `viz_edges` JSON 键继续可读（加载后一次性转
   紧凑数组并释放 list，不常驻）；新构建只写紧凑键。`decode_viz_edges` 兼容行为保留。
2. **运行时表示**。ScaleIndex/VizIndex 的边数据统一为紧凑结构（数组 + 惰性派生索引）：
   - 惰性缓存 `viz_node_index`（id→下标 dict，构建一次）；
   - 惰性缓存按 src 排序的 permutation + indptr（一次 O(E log E) numpy sort），
     邻居点击的 `(s,t)→edge_type` 查询降为 O(deg)；
   - 加载时缓存 `argsort(-viz_deg, stable)` 度序（或构建期落盘），
     `_unified_graph_bounded` 直接取前缀，选边用向量化布尔掩码。
3. **输出等价**。`_unified_graph_bounded`、`kg_neighbors` 的响应 JSON（节点集、顺序、
   边集、totals、truncated）与现状逐位一致，由 old-vs-new 等价测试钉住（含 degree 并列
   tie 顺序：stable argsort 语义 == Python stable sorted）。
4. 名称/类型查表只对 kept 节点构建，不再每请求全量 zip。

## 批 3：reasoning 邻居展开有界化 + 后台任务全局并发闸

改造 A（邻居展开）：

1. `neighbor_ids`（两后端 + ports 签名）新增 `limit` 参数，`ORDER BY r.id LIMIT ?+1`
   （`idx_knowledge_relations_nb_source_id/_nb_target_id` 三列索引两侧已存在，索引满足
   的确定序）；`limit=None` 保持旧行为（其余调用方不变）。
2. `_retrieve_neighbors` 每方向传 `REASONING_NEIGHBOR_EXPAND_LIMIT`（默认 1000），
   LIMIT+1 哨兵判定截断；hydrate 从而按 ≤2×limit 有界。
3. 截断披露：`expand_graph` 的 TraceStep detail 记 `truncated: true` 与计数，并按
   `exact_lookup` 账目范式回喂 reflect（「该节点邻居过多，仅展开前 N 个」），不静默。
   这是**登记的行为变化**：超级枢纽节点的展开召回被预算约束，默认值取在只有病态
   枢纽才会触发的量级。

改造 B（后台并发）：

1. `background_jobs` 新增进程级 maintenance 闸（`BoundedSemaphore`，容量
   `BACKGROUND_MAINTENANCE_CONCURRENCY`，默认 4），镜像 `ReportGenerationGate` 形态：
   闸在 **worker 线程内** acquire（submit 仍立即返回，请求线程不等待），finally 释放。
2. 按既有 `name` 前缀分类：`papermeta/buildkg/rebuildkg/relinkkg/unifiedkg/
   conflictresolve/mergereview/catalog/knowhow-*` 进闸；`ask-*`、`report-*` 不进
   （用户交互路径，报告已有整篇准入闸）。实现前须核实进闸类别没有「父 job 等待子 job」
   的嵌套提交（防自锁）。

## 批 4：跨库组合图向量化 splice + ANN 批量桥接

现状：多参考库时 `_scale_combined_graph` 用 `csr_to_edges`（纯 Python 逐 nnz 建 tuple）
展开第 2..N 个 base 再 `splice_active`；`_scale_xlayer_bridge_edges` 逐 active 向量单条
`knn_query`。

改造：

1. 新增向量化 `splice_csr`：直接对 extra base 的 `transition.tocoo()` 坐标做 numpy
   remap（旧→combined 下标映射数组），与既有 splice 逐库追加的 id 去重序完全一致；
   由于 `splice_active` 本就把 base 边权重置为 1.0 且列归一只取决于最终结构，
   「逐库展开-拼接-归一」与「坐标 remap-拼接-归一」**数值等价**，由 old-vs-new
   `allclose` + `combined_ids` 精确相等测试钉住（既有
   `test_splice_active_matches_full_rebuild` 已提供范式）。单 base self-index 的
   identity 快路径不动。
2. `_scale_xlayer_bridge_edges` 改 hnswlib 批量查询（仓库已有 `ppr.py`/`kg_merge.py`
   两处批量先例）：按块（如 4096 行）`knn_query(M, k)`，块级 fail-open（原为逐行
   fail-open——登记的可观测性差异：一次坏块丢 4096 行的桥而不是 1 行；换来的是
   查询次数降 3-4 个数量级）。边追加顺序与逐行版一致（行序×近邻序）。

## 批 5：element→chunk 持久反查索引

现状：`_elem_chunk_map` 版本缓存冷载时全量扫 chunks + 逐行 json.loads。消费方两个：
`_kg_source_chunks`（每查询只要少数 element 的点查——真正受益方）与 `_ent_chunk_map`
（graph 模式 PPR 需要全库 membership——本质全量，保留旧路径）。

改造（完全镜像既有 `knowledge_object_sources` + `source_index_backfills` 模式）：

1. **迁移**：SQLite `_migration_44`（bump SCHEMA_VERSION→44）+ PG `0022`（v22）：
   - `chunk_elements(notebook_id, element_id, chunk_id)` 反查表 +
     `(notebook_id, element_id, chunk_id)` 覆盖索引；
   - `chunk_element_backfills` 账本表（同 `source_index_backfills` 形状：状态、keyset
     游标、代次、计数、失败码）；
   - `unified_kg_state.chunk_elements_indexed` 标记列。
   迁移只建空表，不回填（仓库既有立场：迁移内不做大表回填）。
2. **写路径前向维护**：`replace_source_chunks` 与 knowhow 投影的
   `insert_rows`/`delete_by_ids` 在**同一写事务**内维护反查行（两后端同修）。
3. **离线回填**：`batch_ingest` 新 phase `backfill-chunk-elements`，逐页 keyset +
   逐页事务 + `kg_mutation_seq` 代次校验 fail-closed，完成后 flip 标记（可续跑）。
4. **读路径分叉**：`_kg_source_chunks` 冷路径先看标记——已回填 notebook 直接按
   element_ids 有界点查（SQLite `ORDER BY rowid` / PG `ORDER BY ordinal`，贴近旧
   「chunk 扫描序」；该顺序本就声明非契约，差异登记）；未回填走旧 `_elem_chunk_map`
   全量缓存，行为不变。`_ent_chunk_map` 不动。

## 验证与交付

- 每批：实现子代理 → 规格评审 + 代码质量评审 → 修复 → 目标测试；
- 全部批次后：`bash scripts/check.sh` 全绿 → 文档同步（新 Settings 数值登记
  `docs/product-and-api*.md`；本设计文档随 PR 入库）→ 单一 PR → codex 评审闭环。

## 第二期（P1 轮）：三批

第一期五批（PR #493）合入后启动。经代码核实后的定型方案：

### 批 A+B：治理路径读取瘦身（review_queue + promotion）

- `review_queue_rows`：对象查询从「全 notebook 全部对象」收窄到「出现在非 rejected
  关系端点上的对象」（JOIN/EXISTS，无损——objects 只被用来取端点 type/name）；
  关系查询的 `evidence` 正文列替换为 SQL 侧派生的锚点信号投影（逐字复刻
  `evidence_anchor_score` 的判定输入，不再把全部 evidence JSON 拉进 Python）。
  corroboration 的跨边聚合保留在 Python（分组键依赖 `_norm`，刻意不下推）；
  centrality 缓存与 bounding 不动；排序改 `heapq.nlargest(limit)`。
- promotion `_base_dedup_rows_for_update`：SELECT 去掉 `evidence` 列（seed 匹配
  不读它），匹配成功后按 matched id 单行补读再合并。**FOR UPDATE 与锁序零变化**
  （并发正确性锚由既有 PG 并发测试钉住，不动）。

落地时的三处定型（与上文措辞的差异，均为实现期核实后的收窄）：

1. 对象收窄**不用** JOIN/EXISTS，改为「从已在手的关系行收集端点 id → 分批按 id
   取回」（页宽 `_REVIEW_ENDPOINT_LOOKUP_BATCH = 500`，是分页宽度不是上限）。
   理由是 JOIN/EXISTS 要为 `review_status != 'rejected'` 再走一遍
   knowledge_relations，而那一遍刚刚做过。关系查询的驱动计划逐字未变（仍是
   `idx_knowledge_relations_nb_target_id (notebook_id=?)` + 两侧端点主键
   LEFT-JOIN），只多了一个按行自身 evidence 求值的相关子查询。

   ⚠ **SQLite 侧那条取数必须是裸 `id IN (...)`，`notebook_id` 只进投影、比对
   放 Python**。本仓库从不对生产库跑 `ANALYZE`，无统计信息时
   `WHERE notebook_id=? AND id IN (...)` 会被 planner 选成
   `idx_knowledge_objects_nb_*(notebook_id=?)`，即**每批**扫遍该 notebook 的
   全部对象——比本条要消灭的那次全量扫更糟（20 万行真实 schema 实测：
   0.138s → 14.155s，×103 倒退）。裸 `id IN (...)` 在有无统计信息下都走
   `sqlite_autoindex_knowledge_objects_1 (id=?)`。这是仓库登记过的既有坑，
   同款结论与配方见 `query_store.notebook_source_ids` 与
   `maintenance.chunk_texts_by_ids`。⚠ **夹具规模的 `EXPLAIN QUERY PLAN` 证明
   不了这一点**（几行的表上两种拼写都走主键），所以护栏必须钉 SQL **文本形状**
   （`test_endpoint_lookup_sql_keeps_no_notebook_predicate`，范式取自
   `test_canonical_relations.py::test_relation_support_rows_issues_row_value_in_not_or_chain`）；
   本条第一版正是因为只有行为测试才漏过一轮评审。PG 侧
   `notebook_id = %s AND id = ANY(%s)` 已验证走 `pk_knowledge_objects`，保持原样。
2. 锚点下推的 trim 字符集必须是 `str.strip()` 的**精确** Unicode 空白集
   （`app/core/text_whitespace.py::PY_STRIP_WHITESPACE`，29 字符；放 `core` 是因为
   服务层与两个适配器都要用，同 `query_syntax` 口径）：SQLite `trim(X,Y)` 与
   PostgreSQL `btrim(X,Y)` 默认只去 U+0020，裸写会把 `"\n"` / `"　"` 这类
   纯空白 quote 判成已锚定。两后端各有逐形状对照用例。
3. **登记的健壮性差异**：畸形 evidence（非法 JSON TEXT、`quote` 非字符串）旧路径
   直接抛异常（整个 review_queue 500），不存在可保真的响应；下推版一律记 0.0 并
   正常返回。注意 `_edge_centrality_map` 仍在 Python 侧解析 evidence（本批不动），
   所以非法 JSON TEXT 仍可能在下游让端点失败——本批只是不再自己贡献这个失败面。

### 批 D：mention bridge 组合加界 + 增量 fusion 残余收敛

- mention bridge：单 claim 命中 canonical 集合加上限（具名常量）——O(hits²) 组合
  从此有界。逐 alias 查询保持（对象是连接私有内存 TEMP FTS 表，非磁盘 N+1，不值得
  批量化）。
- fusion 的 orphan-cluster anti-join（每源一次、无闸、全库扫描）：先核实对象删除
  路径是否已同事务精确清理 `concept_clusters`（member_object_id 有索引）；据此
  把 anti-join 降为低频兜底或在删除路径补精确清理。两形状二选一，以「删除路径
  精确清理 + fusion 兜底降频」为优先。
- fusion 无 ANN 分支的 `embedding_rows` 收窄到 concept 类型（该分支只做 concept
  桥接，claim 占 70% 的向量读取是纯浪费）；`cluster_map_rows` 的整表读**不动**
  （代码已登记撤回有界化的 17.6× 实测理由）。

落地时的三处定型（评审后核实，与上文措辞的差异均为实现期收窄）：

1. **组合上限是「整条 claim 放弃组合」，不是「按确定序截断」。** 常量
   `_MENTION_MAX_CANONS_PER_CLAIM = 16`（C(16,2)=120 对/claim 硬顶）按
   `_MAX_GROUP_REPS` 先例走**具名常量**而非 `Settings`——它是协议边界性质的
   稀疏化参数，不是需要按部署在质量/成本间调的预算。之所以选「整条跳过」：
   canonical id 由 `_norm(概念名)` 派生，字典序前缀对中文命名的概念有系统性
   偏置（CJK 码位在 ASCII 之后），一条中英混排的巨型 claim 会只保留英文那一半
   的桥接对；而「同时提到 17 个以上概念」的枚举式 claim 本就是**弱**桥接信号
   （它没说明任何两个概念的具体关系），整条放弃无偏置、更简单，也顺带消解了
   本文档「会改变结果的截断应走 `Settings`」那条原则与此处的表述冲突——现在
   没有截断，只有一条确定性的**准入**判据。跳过条数经只含计数的结构化事件
   `mention_comention_claim_skipped` 上报（对齐隔壁 `mention_alias_df_dropped`）；
   `mention_edges` 线性、不受影响。

2. **最终形状是把 orphan 的「生产者」清零，而不是给消费端加聪明的闸。** 闸走过
   两个被否的版本，都登记在这里，因为它们各代表一类容易重犯的错：

   - **v1 `kg_mutation_seq` 相等闸**（评审实测打回）：`store_kg` 收尾无条件
     bump 它，而 fusion 恰跟在抽取之后跑，于是上传主热路径上闸恒开——3 次上传
     3 次全库反连接，只有 fold 循环内部那一维真收敛。
   - **v2 进程内「疑似 orphan」全局单调 tick**（codex P1）：只由 knowhow 投影
     推进。多 worker 部署下 worker A 的投影只推 A 的本地信号，worker B 的记账
     永远停在旧世代 → **B 的清扫被永久压制**，劣于改造前的无条件扫。仓库虽登记
     「生产固定 `--workers 1`」，但改造前多 worker 也能扫到，这是真回归面。

   **v3（最终）**：把唯一剩下的生产者也变成事务内清理。knowhow 投影的
   delete-and-reinsert 走新的 `prune_cluster_rows_for_source(keep_object_ids=
   本次要重插的 id)`——KO id 是内容稳定 hash，绝大多数对象会原样回来，所以只清
   **差集**（这次重投影真正丢掉的对象：删掉的列/行/改名的格子），活对象一行不
   碰；`delete_table_projection` 不传 keep 集合即全清。于是三条删对象的路径
   （来源删除/重解析/replace_source、knowhow 重投影/删表、整库重建）**都**在
   自己的写事务里清簇行，全仓**零 orphan 生产者**。

   fusion 侧因此只剩「清掉改造之前留下的静态残渣」这一个职责，闸退化成**每进程
   每 notebook 至多一次**的纯兜底：多 worker 下每个 worker 各扫一次，不存在任何
   压制路径（进程本地性从此无害），进程重启后再扫一次。判据纯内存，跳过时不读库
   也不开写事务（fold 循环 D 次白拿进程写锁是 PR#320 写锁饿死的同一形状），记账
   落在写事务**提交之后**（回滚不记 → 下次重扫）。`orphan_cluster_signal` 模块
   随 v2 一并删除。

   守卫 `test_orphan_producing_paths_are_exhaustively_registered` 钉住这个穷举，
   且**豁免清单为空**：两个后端各恰 4 条 `DELETE FROM knowledge_objects`，每条
   所在函数要么自己删簇行、要么整表清空、要么是 knowhow 那两条（其清理由服务层
   在同一写事务内经 `prune_cluster_rows_for_source` 完成，由该文件的 AST 检查
   逐函数配对）；并断言 `orphan_cluster_signal` 已不存在，防止跨进程信号闸复辟。

3. 两条精确清理的 DELETE 都带 `notebook_id` 残余谓词（`idx_clusters_member` 仍
   驱动 seek），让它们与被替代的 `sweep_orphan_clusters` 语义逐字同形，不依赖
   「对象 id 是全局主键 / 深拷贝重铸 id / 批次来自 notebook 内查询」这三条远端
   事实。`prune_cluster_rows_for_source` 的规模由**一张 knowhow 表的投影**界住
   （它读的正是紧随其后的 `delete_objects_by_source` 要扫的那一段
   `idx_knowledge_objects_source`，调用方本来就把同量级的 `object_rows` 拿在
   手里），必须在对象被删**之前**调用。

### 批 E：relink 倒排 + source_embedding 读入分页 + orphan asset 单遍扫描

- relink Rule-1：`element_id → nodes` 倒排替代逐孤立节点×全 sibling 交集
  （严格无损：overlap 计数经倒排桶累加与集合交集数学等价）；Rule-2 不动。
- `embed_source`：`source_elements()` 整源物化改 keyset 分页读入（计算/写回
  已分批，这是最后一块）；页大小具名常量。
- orphan asset sweep：O(资产×格子) 逐资产前导通配 LIKE 改为单遍扫描——一次取回
  notebook 全部 `content_md`/`payload_json`，regex 提取 `asset://<id>` token
  集合，与资产集合差集；写事务复核同一形状。**顺带修正 PG 侧不扫
  `knowhow_changes` 历史引用的行为差异**（同一资产 SQLite 保留、PG 会删——
  回退到旧版本会指向已删文件；修复后两侧同口径，登记为正确性修复）。

### P1 轮登记不做（经核实后主动放弃）

- rebuild 的 seed representatives ~18GB：已是流式 + float32 + 用完即 `del`，
  18GB 是 4.4M seed × 1024 维的数据本征规模；进一步压缩需分片聚类（跨片丢对
  的质量代价），不值。
- `cluster_input_facts` 的 COUNT：代码自认的防御性 backstop（抓绕过
  mark_dirty 的写路径），memo 化会掏空其存在意义，且成本占 rebuild 总时长
  比例极小。
- fusion 的「新类型全量 canonical names」读取：代码已登记不可收窄的完整论证
  （别名表键不可点查，逐候选 LIKE 替代实测更差）。
- knowhow 单项变更全表指纹：指纹是变更历史/传输守卫的对账契约本体，增量化
  等于换算法、破坏历史对账。
- H4/H5 checkup 首请求 anti-join：已有 30s TTL 缓解。
- 兼容接口全量响应：旧 API 客户端行为，收紧属破坏性变更，单独立项再议。
- relink/rebuild 单飞的多 worker 化：生产固定 `--workers 1`，收益为零。

## 本轮不做（P1/P2 登记）

- review_queue 全量计算后截取（同步接口 O(E log E)）：后续改为 SQL 侧预筛 + 分页。
- promotion 按类型全量 FOR UPDATE：后续改规范化 seed + 唯一索引精确冲突处理。
- unified rebuild 的 seed representatives 峰值内存与 mention bridge N+1/组合爆炸：
  离线路径，后续单独立项。
- 增量 fusion 的全 notebook orphan-cluster anti-join 与新类型全量 canonical names。
- `cluster_input_facts` 的精确 COUNT（版本检查频率可控，收益低）。
- source_embedding 整源 element 物化（按源有界）；knowhow 单项变更全表指纹；
  orphan asset 前导通配符扫描；兼容接口全量响应；单飞进程内状态的多 worker 化。
