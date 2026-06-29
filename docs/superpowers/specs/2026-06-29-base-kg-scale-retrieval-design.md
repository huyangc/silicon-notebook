# 设计：领域基础 KG 的规模化检索(SP2)

- 日期：2026-06-29
- 范围：SP2 —— 让 graph/reasoning 检索在「领域基础 KG（10^5–10^6 object 级节点）」上不物化全量、不超时。
- 不在本 spec：SP1（可视化/搜索规模化）、SP3（摄取/统一聚类在 10^6 的扩展性）。

## 背景与动机

「基础 KG」= 一个被管理员标 `tier='base'` 的 notebook，持有某垂域（如模拟 IC 设计）的全部知识，object 级量级可达 10^5–10^6（concept+claim+formula+procedure），边 5–10×。检索时由 federation 把 base + 当前 personal notebook 合并。

当前检索基底在该量级会塌，三处都因「物化全量」：

1. `_ppr_graph` 把 active + 每个 base notebook 的全部 object/relation/chunk/cluster **load 进 Python 建 rustworkx 图并缓存**，且**建图时对全量节点向量重算 emb-synonym 边**。
2. 节点向量检索 `_vector_matrix` 把全部节点向量建成一个 **~4GB 稠密 float32 矩阵**做 matmul，**无 ANN 索引**。
3. 以上都随每个 base 重复（federation 逐 notebook）。

`concept_detail`/`node_context` 走邻接索引（有界邻域），不受影响。

### HippoRAG 的真实实现（设计依据）

HippoRAG 跑的是**精确的全图个性化 PPR**，不是局部近似：用 igraph `personalized_pagerank(..., implementation='prpack')` 在整张图上跑，reset 向量种在 query 命中的节点（phrase×passage 权重），并用 **node specificity（≈IDF，节点出现的 passage 数的倒数）** 加权稀有节点。

它能在大图工作的原因**不是近似**，而是基底：① 图是紧凑稀疏的 C 结构（非 Python 对象图）；② **synonym 同义边在建索引时离线算好**；③ 种子用稠密检索/实体链接（≈ANN）定位。

结论：**不改成近似 PPR；保持 HippoRAG 式精确全图 PPR，把基底换成「离线预计算的紧凑稀疏图 + ANN 种子 + 离线 synonym 边」**。这契合「基础 KG 离线批量构建、基本静态」的生命周期（用户已确认）。

来源：HippoRAG repo `src/hipporag/HippoRAG.py`（`run_ppr` → igraph prpack 全图）；NeurIPS 2024 论文。

## 架构总览

**base = 离线预计算的静态紧凑基底；每次查询把「当前 personal notebook 的小 delta」拼接到 base 之上跑精确全图 PPR。** 每查询成本 = base 加载（一次性常驻）+ active 规模。

```
离线（batch_ingest 新增 index 阶段）          在线（查询）
build_scale_index(base_nb):                  scale_ppr(active_nb, query):
  读 SQLite KO/rel/chunk/cluster               1. 种子: ANN(base) + 暴力(active) → seeds+权重
  离线算 synonym 边（一次）                      2. 拼接: base CSR ⊕ active delta(按 canonical_id 合一)
  → 持久化: CSR图 + hnswANN + IDF + 成员表      3. PPR: numpy CSR 幂迭代(reset=种子×IDF) 精确
  写 .local/storage/kg_index/{nb}/             4. 节点分 → 成员表 → chunk/knowledge 打分(下游不变)
```

## 持久化产物

每个 base notebook 一份，位于 `{storage_dir}/kg_index/{notebook_id}/`（`storage_dir` 默认 `.local/storage`）：

- `graph.npz` —— PPR 图的紧凑 **CSR**：`indptr/indices`(int32) + `data`(float32) + `node_ids`(index↔object/chunk id)。含 relation/membership/synonym/variant 边；**synonym 边离线算好烘进去**。10^6 节点/10^7 边 ≈ 100–150MB。
- `ann.bin` —— hnswlib 节点向量索引（种子链接用）+ label↔id 映射。常驻 ~4GB（与现状 matmul 矩阵同量级，但只建一次、查询 ms 级）。
- `idf.npy` —— node specificity（成员 passage 数的倒数）。
- `members.npz` —— node→chunk 成员稀疏表（PPR 分映射回 chunk）。
- `manifest.json` —— version key（沿用现有 COUNT/MAX(created_at/updated_at) 模式）+ dim/counts，用于判定过期。

## 离线构建 `build_scale_index(notebook_id)`

- 读取与现 `_ppr_graph._load` 同源的数据（KO/relation/chunk/concept_clusters/membership）。
- **synonym 边在此一次性计算**（复用 `app/services/kg/ppr.py:emb_synonym_edges`，离线慢可接受）。
- 计算 IDF、成员表，组装 CSR，建 hnswlib 索引。
- 原子写出 5 个文件 + manifest；**幂等**；base 重建后重跑。
- 入口：`batch_ingest` 新增 `index` 阶段（Phase 3）/`index` 子命令；缺模型配置按既有约定显式报错（不静默降级）。

## 在线查询 `scale_ppr(active_nb, query, ...)`

1. **种子**：query 向量 → base 的 hnsw ANN top-k + active（小，暴力 matmul）→ 合并 seeds 与权重；沿用现 `_ppr_reset_vector` 的 fact/keyword 信号组装 reset。
2. **拼接 active delta**：取 base CSR，追加 active 的节点（按 `canonical_id` 与 base 去重合一）、active 边、跨层 membership/synonym（active 节点对 base ANN 查 → 有界条数）。成本 ∝ active 规模；按 active version 缓存拼接结果。
3. **PPR**：numpy CSR 幂迭代实现个性化 PPR（`reset = 种子 × IDF`，damping 沿用现 `settings.ppr_*`），精确、确定性。可选 scipy 加速（不默认引入新依赖）。
4. **映射**：节点分 × 成员表 → chunk/knowledge 分。**输出与现 `federated_retrieve`/PPR 同形**（`RetrievedKnowledge`，带 `.notebook_id`/`.tier`），下游答案组装零改动。

## 激活/切换（不回归小库）

- **判据**：参与检索的 base notebook **存在有效 scale 索引**（manifest version 与 DB 一致）→ 走 scale 路径；否则走现有 rustworkx/matmul 路径。
- 小型 personal-only notebook 完全不变。
- **base 数量假设**：v1 面向「单个大领域 base + active delta」。若同时有多个 base notebook 各带索引，则把它们的 CSR 依次并入（base 数量小、各为静态，成本可控）；任一 base 无有效索引则整体回退现路径（不混用，保证一致性）。
- **等价性保证**：scale PPR 在小图上应与现 rustworkx PPR **top-k 排序高度一致**（等价测试；允许近平局的浮点微差，断言 top-k 集合重合与主序不翻转，而非逐位 byte 相等）。

## 新单元/接口

- `app/services/kg/scale_index.py`（新）——纯函数为主、可单测：
  - `build_scale_index(notebook_id, repo, ...) -> manifest`
  - `load_scale_index(notebook_id) -> ScaleIndex | None`（带进程内缓存 + manifest 校验）
  - `personalized_ppr(csr, reset, damping, tol, max_iter) -> np.ndarray`（numpy CSR 幂迭代）
  - `splice_active(base_index, active_delta) -> combined_csr`（按 canonical_id 合一）
- `SQLiteRepository.scale_ppr(active_nb, query, ...)`（新）——在 `_ppr_graph`/`federated_retrieve` 入口按判据分流，返回与现路径同形结果。
- `batch_ingest`：新增 `index` 阶段调用 `build_scale_index`；README/README_zh 补用法（按既有「CLI 进 README」约定）。

## 测试

- **等价测试（最关键）**：小图上 `scale_ppr` 的 top-k 排序与现 rustworkx PPR 高度一致（top-k 集合重合 + 主序不翻转），证明无质量回归。
- **拼接测试**：active 与 base 按 `canonical_id` 合一；跨层桥（base↔active 共享概念）保留。
- **构建/加载测试**：`build_scale_index` 在小图上产出正确 CSR/ANN/IDF/成员表；manifest version 失配触发重建。
- **回退测试**：无索引时现路径不变。
- **规模测试（gated 慢测）**：合成 10^5–10^6 图，构建 + 查询达延迟/内存目标。

## 风险与预算

- **内存**：ANN 常驻 ~4GB（一次性、全局共享，非每查询/每用户）。与现 matmul 矩阵同量级但更快、免重建。未来可降维/量化（faiss IVF-PQ）进一步压。
- **synonym 离线计算**在 10^6 是 O(N·k) ANN——离线可接受。
- **索引过期**：base 重建后 manifest version 失配 → 需重跑 `index`（batch_ingest 内联）。
- **等价性**：scale PPR 与现 rustworkx PPR 必须排序一致（由等价测试守护）。
- **scipy 不引入**：PPR 用 numpy CSR 幂迭代；若实测 10^6 延迟不达标，再评估引入 scipy.sparse（仅性能优化，行为不变）。

## 实施顺序（writing-plans 细化）

1. `scale_index.py`：`personalized_ppr`（numpy CSR）+ 等价测试（对照 rustworkx）。
2. `build_scale_index` + 持久化格式 + 加载/manifest。
3. `splice_active` + 跨层合一测试。
4. `scale_ppr` 接入 + 切换判据 + 回退测试。
5. `batch_ingest` index 阶段 + README。
6. 规模慢测 + 内存预算核验。
