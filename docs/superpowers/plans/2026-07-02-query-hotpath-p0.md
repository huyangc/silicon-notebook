# 查询热路径 P0 批 Plan(审计 P0-1/2/3/5)

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景**:全局审计(docs/kg-perf-audit-16c64g.md)P0 项中,建索引解决不了的三块查询时 O(N):
1. **关系检索无候选界定**(P0-1/2):`_retrieve_relations_scored`/`_relations_with_names` 全量 relations⋈objects⋈objects JOIN + 逐行解析;经 `_mix_retrieve` 的 overlay 挂在**默认 chunk 问答路径**(`chunk_kg_overlay_enabled` 默认 True + rerank 已配),每问必付。
2. **两个未缓存全库扫描**(P0-5):`_ent_chunk_map`(49万 evidence + 百万 chunk element_ids 全解析,PPR 回退每查询付)与 `_kg_source_chunks`(百万 chunk 全扫+集合交,mix/graph 每查询付)。
3. **边审查队列全图介数中心性**(P0-3):`review_queue` 每请求同步建全图跑 Brandes O(V·E),分钟级。

**Goal**:三块全部变「版本缓存命中 O(1) / 候选有界」;检索语义与输出形状不变(守 [0,1]/tau 不变量);49万库上默认问答的 KG overlay 从秒~几十秒 → 毫秒级。

**Tech Stack**:pytest;`_vector_cache`(已有 single-flight+LRU-32)与 `_scale_index_version`(O(1) seq 记忆化)是现成基建,全部复用。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`,worktree `backend/` 跑测试。基线 `pytest tests/ -q` = **1493 passed, 1 skipped**。

## Global Constraints
- **输出等价**:三处的返回值(条目集合/分数/排序/形状)与现实现一致;缓存只改「何时算」,不改「算什么」。
- 缓存一律走 `self._vector_cache.get(key, version, loader)`,version 用 `tuple(self._scale_index_version(nb))`(O(1) seq 探针,覆盖 objects/relations/chunks/clusters 变更);失效依赖版本键即可,无需新增显式 invalidate(`_invalidate_unified_cache` 若有相邻 key 清理惯例,跟随补上)。
- 新配置一律 pydantic-settings v2 `validation_alias`。
- 大对象进 LRU 的驻留权衡在报告中量化说明(ent_chunk_map/elem→chunk 在 49万/百万规模的估算)。

---

## Task 1: `_ent_chunk_map` 版本缓存 + `_kg_source_chunks` 反查化(P0-5)

**Files:** `backend/app/services/sqlite_repository.py`;Test 新建 `backend/tests/test_query_hotpath_cache.py`。

- [ ] Step 1 测试先行:
  - `_ent_chunk_map` 两次调用第二次不再跑 SQL(spy `_connect` 或 loader 计数经 `_vector_cache`);KG 变更(store_kg / `_mark_unified_kg_dirty` 级)后版本变 → 重算;返回值与未缓存 oracle 相等。
  - `_kg_source_chunks(nb, object_ids)` 改经缓存的 `{element_id: [chunk_id]}` 反查:结果与旧全扫实现(拷进测试当 oracle)相等,含 element_ids 空/对象无 evidence/多 chunk 命中场景;第二次调用无全量 SQL。
- [ ] Step 2 实现:
  - `_ent_chunk_map` → `self._vector_cache.get(f"{nb}:entchunk", version, loader)`,loader 即现逻辑。
  - 新 `_elem_chunk_map(nb)`(缓存,`{nb}:elemchunk`):一次 `SELECT id, element_ids FROM chunks WHERE notebook_id=?` 建 `{element_id: list[chunk_id]}`。`_kg_source_chunks` 重写:取目标对象们的 evidence(按 object_ids `IN(...)` 只查这几行)→ element_id 集合 → 反查映射并集。不再全扫 chunks。
  - 两个 map 的构建共享一次 chunks 扫描可选(loader 内部互相独立即可,LRU 管驻留)。
- [ ] Step 3 回归:`-k "hotpath or ppr or mix or graph or overlay"` + 全量(基线 1493)。
- [ ] Step 4 提交 `perf(retrieval): _ent_chunk_map/_kg_source_chunks 版本缓存+element反查(默认问答/PPR 热路径去全库扫)`。

## Task 2: 关系检索候选界定(P0-1/2)

**Files:** `backend/app/services/sqlite_repository.py`(`_retrieve_relations_scored`/`_relations_with_names`/`_mix_retrieve` 调用链);Test 同上文件追加。

- [ ] Step 0 **先验证现状流**(写进报告):`RELATION_RETRIEVAL_ENABLED` 默认关、relation_embeddings 空表时,`federated_retrieve_relations` 是否仍跑全量 `_relations_with_names` JOIN?按真实代码回答;若空表已有早退则本任务聚焦「有向量时」的界定 + 确认早退测试锁行为。
- [ ] Step 1 测试先行:
  - 空 relation_embeddings → 不执行全量 JOIN(spy SQL / loader),返回 []。
  - 有向量时:top-K 候选先定(K=现有取数上限,查配置/调用方),**只对候选 id 做 `IN(...)` 命名 hydration**;结果(集合/分数/序)与旧全量实现 oracle 一致(小数据)。
  - `_mix_retrieve` overlay:输出等价 + 不再触发全量 JOIN(spy)。
- [ ] Step 2 实现:相似度经既有 relation 向量矩阵(`_vector_matrix` on relation_embeddings,已缓存)算全量 sims(矩阵乘本身 O(N) 毫秒级可接受)→ top-K id → `_relations_with_names` 加 `relation_ids` 参数只 JOIN 这 K 行。保守:K 语义与旧实现最终截断一致,分数归一化路径不动。
- [ ] Step 3 回归:`-k "relation or mix or overlay or rerank"` + 全量。
- [ ] Step 4 提交 `perf(retrieval): 关系检索候选界定(top-K 先定再 hydration,空向量早退;默认问答 overlay 去全量 JOIN)`。

## Task 3: `review_queue` 中心性版本缓存 + 有界(P0-3)

**Files:** `backend/app/services/sqlite_repository.py`(review_queue)、`backend/app/core/config.py`;Test 同上追加。

- [ ] Step 1 测试先行:
  - 两次请求第二次不重算中心性(spy rustworkx 调用/loader 计数);KG 变更后重算;输出(边排序/字段)与未缓存 oracle 一致。
  - 超界:monkeypatch `edge_centrality_max_nodes=3` 的小图 → 只对 top-3 度数节点诱导子图算中心性,队列仍返回(降级排序合理:子图外的边 centrality=0 走 trust 分量),不抛错。
- [ ] Step 2 实现:
  - config `edge_centrality_max_nodes: int = Field(20000, validation_alias="EDGE_CENTRALITY_MAX_NODES")`。
  - 中心性计算抽 loader → `_vector_cache.get(f"{nb}:edge_centrality", version, loader)`;loader 内:节点数超界则取度数 top-K 诱导子图算,子图外边 centrality 取 0(排序退化为 trust 主导,文档写明)。
  - 请求内其余逻辑(trust/排序/limit 200)不动。
- [ ] Step 3 回归:`-k "review_queue or edge"` + 全量。
- [ ] Step 4 提交 `perf(kg): review_queue 介数中心性版本缓存+度数top-K有界(EDGE_CENTRALITY_MAX_NODES)`。

## 收尾
- opus 全分支终审 → rebase → push → PR(引用审计文档条目号;PR 描述交代 overlay 路径的每问收益)。
- 审计文档 docs/kg-perf-audit-16c64g.md(在主 checkout,未提交)随本 PR 一并提交入库。
