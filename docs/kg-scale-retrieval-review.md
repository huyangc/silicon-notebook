# 百万级 KG 索引 / 检索算法 Review（场景驱动）

> 日期：2026-07-01 ·
> 范围：`backend/app/services/{vector_index,retrieval,reasoning_retrieval,kg_merge}.py`、
> `kg/{scale_index,ppr,search,graph_reason}.py`、`sqlite_repository.py` 检索/索引段。
> 目标：最终 KG 规模到 **10⁵–10⁶ 节点**时，索引与检索算法的可扩展性问题与优化。

---

## 1. 背景：两个必须同时支持的场景

| | 场景 A · 离线大库 | 场景 B · 少量上传 + 快查 |
|---|---|---|
| 数据 | 离线批量摄取的领域大库（10⁶ 级） | 用户人工上传少数文档 |
| 期望 | 构建可以慢（离线建索引） | 上传后**立即、快速**查询 |
| 变化频率 | 低（批次重建） | 高（随传随查） |

这两个场景**恰好就是代码里已有的 `tier='base'` / active 分层**：

- 场景 A = **base tier**：`build_scale_index` 离线为 base notebook 建 CSR 转移阵 + hnsw ANN + 折叠 viz 图，落盘 7~9 个文件。
- 场景 B = **active delta**：`scale_ppr` 每查询把 active 的 KG delta 现场 `splice` 到 base 图上，再跑 PPR。

机制已在（[`scale_ppr`](../backend/app/services/sqlite_repository.py#L6319) / [`splice_active`](../backend/app/services/kg/scale_index.py#L221)）。**问题不在"要不要支持"，而在于现有实现没有严格守住"成本分离"这条不变量。**

---

## 2. 总纲：成本分离不变量

| | base（大、静态） | active（小、动态） |
|---|---|---|
| **构建期** | 所有重活离线做，O(N_base) 随便花（分钟级 OK） | 上传**绝不能**触发 base 重建/重聚类；active 增量只做 O(active) |
| **查询期** | 对 base 只能 O(polylog)：ANN + 预建结构，**绝不 O(N_base)** | O(active) 暴力**没问题**——甚至更好：零构建延迟、永远最新 |

**关键洞察：暴力检索（全量 matmul / 全量重分词）在 active 上是对的，在 base 上是错的。**
当前多处检索对 base 也走了 active 才该用的暴力路径——这是本 review 的主线。

现状已符合不变量的部分（**不要动**）：

- 流式聚类 `rebuild_unified_kg` / `_stream_seed_reps`：峰值内存 ∝ 唯一名数（非对象数），已在 5M 成员级测过。
- base scale 索引版本键（[`_scale_index_version`](../backend/app/services/sqlite_repository.py#L5996)）**只依赖 base 自己的表**——上传到 active **不会**让 base 索引失效，base ScaleIndex 进程缓存持续命中。✅
- CSR 稀疏 + 幂迭代 PPR、cluster 星型 hub（N 边非 N²）、base 种子用 ANN 避开 4GB matmul、概念描述 LLM 并发+缓存（PR#125）。✅

---

## 3. 分级问题清单

标注：【A】=场景A(base)侧改，【B】=直接影响场景B快查，【A+B】=两者共因。

### 🔴 P0 — 阻塞级

#### P0-3【B·命门】splice 每查询把整张 base 从 CSR 反推 + Python 重建整个转移矩阵

- **现状**：[`splice_active`](../backend/app/services/kg/scale_index.py#L221) 每查询 `base_transition.tocoo()` → Python 推导式遍历**全部 base 边** → `build_transition` 再 Python 循环从头重建整阵；顺带把边权丢成 1.0。调用点 [`scale_ppr` L6435](../backend/app/services/sqlite_repository.py#L6435)。
- **影响**：用户传 3 篇文档、active 只有几百节点，查询延迟却被百万条 base 边的重建绑架。**这是唯一一处 active 查询被迫付 O(base) 的地方**，直接决定场景 B 能否"上传即快查"。
- **改法**：base 组合矩阵离线预建、进程常驻；active 以 **bordered block** 拼接（`sp.bmat([[base_csr, B_ab],[B_ba, A_active]])`，base 子块复用缓存），成本降到 O(active + bridge 边)。
- **连带**：即便修了 splice，combined 图 PPR matvec 仍是每迭代 O(E_base)（scipy C 级，百万边约几十 ms，多半可接受）。**先修 Python 重建**（它比 C 级 matvec 慢得多）；若 base 涨到 10M+ 边，再上 **push-based 局部 PPR**（Forward-Push / Andersen-Chung-Lang，只在种子邻域展开、亚线性）——这才是"大库 + 个性化快查"的正解。

#### P0-1 / P0-2【A】base 侧检索仍是全量暴力（chunk & KG 对象），federated 未按 tier 分派

- **现状**：
  - [`_retrieve_chunks`](../backend/app/services/sqlite_repository.py#L6898) → [`query_sims`](../backend/app/services/vector_index.py#L47) 全量矩阵余弦 + [`score_chunks`](../backend/app/services/retrieval.py#L634) 逐 chunk `keyword_score` **重新分词**。chunk **完全没建 ANN**（`grep knn_query retrieval.py` = 0）。
  - [`_retrieve_scored`](../backend/app/services/sqlite_repository.py#L6716) 每查询对 element/knowledge 全矩阵 `query_sims`，并**每查询全表扫 `knowledge_relations` 算孤立点集合**（L6734）。
  - [`federated_retrieve`](../backend/app/services/sqlite_repository.py#L6771) 把上面这套 ×（base + active 的 N 个 notebook）——base 有百万对象时每查询 N 次全量。
- **改法（场景区分是重点）**：
  - **base**：离线建 chunk hnsw；KG 对象检索复用已有 `ann.bin`；`federated_retrieve` **按 tier 分派**——base notebook 走各自预建 ANN，active 走暴力。孤立点集合随写维护 / 进 scale 索引预计算，别查询期扫全表。
  - **active**：**保持暴力，不要建 ANN**——几百向量暴力 matmul 是亚毫秒，建 hnsw 反而引入构建延迟、破坏"上传即查"。
- **根因**：ANN 目前只服务 PPR 种子选择这一处；召回候选本身对 base 仍 O(N)。把 ANN 推广到 base 的所有召回路径，可一并解掉 P0-1/P0-2/P1-5/P1-6。

#### P0-4【A】hnsw 索引每查询从磁盘 `load_index`（种子 + 同义桥各一次，共两次）

- **现状**：[L6460](../backend/app/services/sqlite_repository.py#L6460) 与 [L6406](../backend/app/services/sqlite_repository.py#L6406) 每查询 `hnswlib.Index(...).load_index(...)`。百万向量 hnsw 反序列化不便宜，同一查询加载两次。
- **改法**：打开的 hnsw handle 随 `ScaleIndex` 进程缓存（与 `_scale_idx_cache` 同生命周期，版本失效才重开），`set_ef` 每查询设即可；bridge 与 seed 合并成一次 knn。

### 🟠 P1 — 规模下的正确性悬崖（不只是慢）

#### P1-5【A】`emb_synonym_edges` 在 5 万实体处直接返回 []

- **现状**：[`emb_synonym_edges` L152](../backend/app/services/kg/ppr.py#L152) `if n > max_entities: return []`，默认 `ppr_emb_synonym_max_entities=50000`。base>50k 时**跨文档同义桥彻底消失**——正是"对比检索坍缩 / 跨文档边=0"的根。本身还是 O(n²·d/512) 分块 matmul。
- **改法**：base 离线时间充裕，换 **ANN-KNN 版**（每实体 knn top-k，全局单 hnsw）。"超限即关"改成"超限走 ANN"。active 的 O(active²) inline 无需动。

#### P1-6【A】`_ann_candidates` 分片时跨片同义对静默丢失

- **现状**：[`_ann_candidates`](../backend/app/services/kg_merge.py#L169) 唯一名超 `max_reps` 时按分片各自建 ANN，**跨片同义对建不出来**（注释自认 v1 限制）。百万唯一名必然分片 → 聚类质量在规模下悄悄下降。违反"不静默降级"原则。
- **改法**：全局单 hnsw（本就支持百万级），或跨片二次合并；至少 `log` 出被牺牲的覆盖面。

#### P1-7【A】`variant_edge_pairs` 最坏 O(V²)

- **现状**：[`variant_edge_pairs` L131](../backend/app/services/kg/ppr.py#L131) 按去版本名分组后组内两两连边。某热门 base 名（大量 vN / 7B / 70B 变体）产生 k² 条边。
- **改法**：组内改星型 hub（和 cluster 一样 N 边非 N²），或对超大组设阈值截断。

#### P1-8【B】每查询在 base 百万行表上跑 5×`COUNT/MAX` 版本探针

- **现状**：[`_scale_index_version`](../backend/app/services/sqlite_repository.py#L5996) 每查询 5 个聚合；[`_ppr_graph`](../backend/app/services/sqlite_repository.py#L5929) ×N notebook ×4 聚合。缓存命中也付这笔税；场景 B 上传频繁时尤其浪费。
- **改法**：随写自增的 per-notebook 版本单行计数器，O(1) 读取替代聚合扫描。

### 🟡 P2 — 次要 / 打磨

- **`_ppr_graph` rustworkx 回退**（[L5917](../backend/app/services/sqlite_repository.py#L5917)）：reciprocal 边 2× 膨胀；[`run_ppr` L196](../backend/app/services/kg/ppr.py#L196) 用 `weight_fn` lambda **每边每迭代**回调 Python，百万边不可接受。→ 规模下确保始终走 scale 路径；PPR 换预存权重数组。
- **`bm25_scores` 内存版**（[L496](../backend/app/services/retrieval.py#L496)）：O(Σ doc_len) Python 在线算，数万文档以上不 scalable。→ base 交给 FTS5 bm25。
- **`viz_core` / `csr_to_edges`**（[scale_index L244](../backend/app/services/kg/scale_index.py#L244)）：`tocoo()` 后 Python 遍历全部边再筛 top-N。→ 先按 top-N 索引切子阵 `adj[top][:,top]` 再取 coo。
- **`merge_search_hits`**（[search L21](../backend/app/services/kg/search.py#L21)）：`-bm25` 与 `cosine` 同尺度混排，量纲不一致。→ 各自归一或 RRF。
- **`personalized_ppr`**（[scale_index L83](../backend/app/services/kg/scale_index.py#L83)）：`max_iter=100`、每迭代分配多个 8MB dense 向量。→ 降 max_iter（damping=0.5 通常 20~30 收敛）+ 原地运算。
- **FTS 重建**（[L1359](../backend/app/services/sqlite_repository.py#L1359)）：单 notebook 全量 `DELETE`+re-`INSERT`。→ 增量 upsert。
- **`build_transition` Python 逐边 append**（[scale_index L113](../backend/app/services/kg/scale_index.py#L113)）：纯离线 base 构建，几分钟可接受，低优先；可向量化提速重建。

---

## 4. 新增设计点：active 的"增长阈值 + 晋升"

场景 B 的 O(active) 暴力**只在 active 小时成立**。需给 active 设规模上限（chunk/对象数），超限即提示"晋升为基准库 / 触发离线建索引"——复用已有[基准库晋升治理入口](kg-staged-base-kg-construction.md)。否则 active 慢慢长大，场景 B 悄悄退化成"没有索引的场景 A"，延迟劣化却无人察觉。这条把两个场景用一条**可运维的迁移路径**缝合起来。

---

## 5. 落地顺序（场景驱动）

1. **P0-3**：base 组合矩阵离线预建 + active bordered-block 拼接 → **解锁"上传即快查"**（场景 B 命门，单点收益最集中）。
2. **base 侧 ANN**：`federated_retrieve` 按 tier 分派 + chunk ANN + 同义边 ANN 化（P0-1/P0-2/P1-5/P1-6）→ 场景 A 召回质量与延迟。
3. **P0-4** hnsw handle 缓存 + **P1-8** 版本探针 O(1) + **active 阈值晋升** → 缝合两场景。
4. 按 base 实测边规模，决定是否上 **push-based 局部 PPR**（P0-3 连带）。

---

## 6. 一句话主线

> 把 ANN 与预计算从"只服务 PPR 种子"推广到 **base 的所有召回路径**，同时保证 **active 永远只做 O(active)、上传永不触发 base 重建**。核心不变量是成本分离——base 的重活离线做完，active 保持轻量暴力。
