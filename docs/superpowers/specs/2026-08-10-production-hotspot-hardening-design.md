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

## 本轮不做（P1/P2 登记）

- review_queue 全量计算后截取（同步接口 O(E log E)）：后续改为 SQL 侧预筛 + 分页。
- promotion 按类型全量 FOR UPDATE：后续改规范化 seed + 唯一索引精确冲突处理。
- unified rebuild 的 seed representatives 峰值内存与 mention bridge N+1/组合爆炸：
  离线路径，后续单独立项。
- 增量 fusion 的全 notebook orphan-cluster anti-join 与新类型全量 canonical names。
- `cluster_input_facts` 的精确 COUNT（版本检查频率可控，收益低）。
- source_embedding 整源 element 物化（按源有界）；knowhow 单项变更全表指纹；
  orphan asset 前导通配符扫描；兼容接口全量响应；单飞进程内状态的多 worker 化。
