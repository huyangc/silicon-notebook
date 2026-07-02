# P1 增量融合提速 + Tier2 规模化修复 Plan(审计 P1-2/P1-3)

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景**(docs/kg-perf-audit-16c64g.md P1-2/P1-3):
1. **P1-2**:`incremental_fuse_source`(sqlite_repository.py ~:3884 与 ~:3947,行号已漂移自查)每次上传**两遍**调用 `cluster_map(nb)`——每遍都是百万成员行全扫 + dict 构建,同步在上传抽取路径里。
2. **P1-3(功能性缺陷,不只是慢)**:Tier2 向量桥接被 `kg_incremental_tier2_max_entities`(默认 50000)硬门挡住——49万实体的库上 Tier2 **静默失能**:新上传的概念再也得不到跨文档同义桥(emb 桥候选),孤岛照旧。而现在大库**有 scale 索引的 kg hnsw**(#156 后必建、#155 自动建),完全可以 ANN 化桥接,任意规模可用。

**Goal**:上传路径的融合成本从「2×百万行扫描 + (≤50k 时)全量向量加载」→「缓存命中 O(新对象数) + ANN topk 查询」;Tier2 在任意规模的已建索引库上恢复工作;无索引超阈值时**显式**跳过(事件可观测,不再静默)。

**Tech Stack**:pytest;复用 `_vector_cache`(single-flight+LRU)、`tuple(_scale_index_version(nb))` 版本键、`_scale_index(nb, allow_stale=True)` + `_open_scale_ann(idx,'kg')`(#147 handle 缓存)。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`,worktree `backend/` 跑测试。基线 `pytest tests/ -q` = **1552 passed, 1 skipped**(#157 合入后,以实际首跑为准并记入报告)。

## Global Constraints
- **融合语义不变**:Tier1 名种子 append、Tier2 只入队 merge 候选不自动并、Tier3 全量 rebuild 逃生口——三层决策与阈值语义(`kg_incremental_tier2_*`)不动;本 PR 只改「怎么找候选」与「扫描成本」。
- **cluster_map 缓存的失效审计是硬门**(edge_centrality 的教训):concept_clusters 上任何 **in-place UPDATE**(改名/翻转裁决/canonical 重指派——PR#132 memory 证实存在)都不动 COUNT/MAX(created_at) → 版本元组不动 → 显式 `_invalidate_unified_cache` 必须覆盖。审计所有 UPDATE concept_clusters 位点,逐点确认已调 `_invalidate_unified_cache`(或补上),结论表进报告与提交信息。注意 cluster_map 只消费 member_object_id→canonical_id,只有影响这两列的变更才算内容变更,但宁可过失效不可欠失效。
- ANN 桥接的**等价口径**:与既有暴力实现在「同一 topk/threshold 下的候选集合」上等价(hnsw 近似性允许边际差,测试用小数据 ef 足够高保证精确);type 过滤(只桥概念/或现实现所限类型——照抄现语义)后再阈值判定。
- 无索引且实体数 > max_entities:跳过但发 `event_log` 事件(kind="tier2_skipped", 附实体数与原因)+ 返回值/统计里可见;≤ max_entities 无索引:保留现暴力路径 byte-identical。
- 新配置(如需)一律 validation_alias。

---

## Task 1: cluster_map 版本缓存 + 失效审计 + fuse 内共享(P1-2)

**Files:** `backend/app/services/sqlite_repository.py`;Test 新建 `backend/tests/test_incremental_fuse_perf.py`。

- [ ] Step 1 测试先行:
  - `cluster_map` 两次调用第二次不跑 SQL(loader 计数);cluster 写入(append_clusters/write_clusters/rebuild)后重算;返回值与未缓存 oracle 相等。
  - **in-place 编辑失效**:模拟改名/翻转裁决类 UPDATE(找真实入口,如 merge 裁决写回)→ cluster_map 缓存被显式失效(值刷新)。
  - `incremental_fuse_source` 全程只触发一次 cluster_map 构建(loader 计数=1,含 Tier1+非概念两段)。
- [ ] Step 2 实现:
  - `cluster_map` → `self._vector_cache.get(f"{nb}:clustermap", tuple(self._scale_index_version(nb)), loader)`;`_invalidate_unified_cache` 加 `:clustermap` 兄弟失效行(同注释风格)。
  - **UPDATE concept_clusters 位点审计**(grep 全部 UPDATE/DELETE concept_clusters):逐点确认调用链里有 `_invalidate_unified_cache`,缺的补上;审计表进报告。
  - `incremental_fuse_source` 两处调用共享同一次取值(局部变量传递即可,缓存命中后本就 O(1),共享是防御性)。
  - 其他 cluster_map 调用方(viz build/_unified_graph_full/neighbors 兜底等)自动受益,不改语义。
- [ ] Step 3 回归:`pytest tests/test_incremental_fuse_perf.py -q` + `-k "fuse or cluster_map or incremental"` + 全量。
- [ ] Step 4 提交 `perf(kg): cluster_map 版本缓存+失效审计(上传融合去双百万行扫描)`。

## Task 2: Tier2 桥接 ANN 化(P1-3,功能修复)

**Files:** `backend/app/services/sqlite_repository.py`(`incremental_fuse_source` Tier2 段,~:3897-3908 漂移自查)、`backend/app/core/config.py`(如需);Test 同上文件追加。

- [ ] Step 1 测试先行:
  - **有索引大库恢复桥接**:建小库+scale 索引,monkeypatch `kg_incremental_tier2_max_entities=0`(旧代码必静默跳过的条件)→ 新上传概念经 ANN 路径产出桥接候选(与暴力 oracle 同 topk/threshold 的集合一致,ef 调高保精确);候选进 merge 队列的形态与现有 Tier2 一致(pending 候选,不自动并)。
  - **无索引小库**:≤ max_entities → 走原暴力路径,结果 byte-identical(oracle)。
  - **无索引超阈值**:跳过 + `event_log` 收到 tier2_skipped 事件(spy emit),不再静默。
  - type 过滤:非概念类型不进桥接(照抄现语义,如现实现桥全类型则保持)。
- [ ] Step 2 实现:
  - Tier2 段改三分支:`idx = self._scale_index(nb, allow_stale=True)` 且 idx 有 kg ANN → 对每个新对象向量 `_open_scale_ann(idx,'kg').knn_query(vec, k=tier2_topk)`,命中过 threshold + type 过滤 + 排除自身/同簇 → 现有入队逻辑;无索引且 n ≤ max_entities → 现暴力路径不动;否则跳过+事件。
  - stale 索引可接受(桥接是 advisory、入队人审;报告写明论证);新上传对象自身不在 stale ANN 里——只影响「新↔新」桥(下轮重建后补),「新↔存量」桥(主场景)不受影响,报告写明。
  - hnsw handle 并发:knn_query 线程安全(#147 memoize handle),抽取 job 线程直用。
- [ ] Step 3 回归:`-k "fuse or tier2 or incremental"` + 全量。
- [ ] Step 4 提交 `feat(kg): Tier2 向量桥接 ANN 化(大库恢复跨文档桥;无索引超阈值显式事件不再静默)`。

## 收尾
- opus 全分支终审(重点:缓存失效矩阵增量、Tier2 新旧路径边界)→ rebase origin/master → push → PR(说明功能修复属性:49万库的跨文档桥接从「没在发生」到「恢复工作」)。
