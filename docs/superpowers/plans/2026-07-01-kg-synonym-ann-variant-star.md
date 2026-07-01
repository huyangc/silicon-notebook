# PR3:KG 边质量 — 同义边 ANN(P1-5)+ variant 星型(P1-7)+ 分片全局化(P1-6) Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**Goal:** 让跨文档同义桥/变体边在**百万级仍成立且不爆 O(n²)/O(k²)**——消除「base>5万实体时 emb_synonym 返 []、跨文档同义桥消失」(直接关系对比检索坍缩)、variant 组内 O(k²)、`_ann_candidates` 分片跨片丢对。

**Tech Stack:** hnswlib、numpy、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试在 worktree `backend/`。

## Global Constraints
- 依据 [review P1-5/6/7](../../kg-scale-retrieval-review.md)、[[comparative-retrieval-collapse]]。
- 边生成是**离线 build/聚类期**;正确性=同义/变体连通性 ≥ 旧实现(小规模等价,大规模不再丢)。
- 不改边的消费方(`_gather_kg_graph` 的 extra_edges / `cluster_seeds`);只改边**怎么算出来**。

---

## Task 1: P1-5 emb_synonym ANN + P1-7 variant 星型(都在 `kg/ppr.py`)

**Files:** Modify `backend/app/services/kg/ppr.py`;Test `backend/tests/test_ppr.py` 或 `test_relation_embed.py`(择同义/变体测试所在文件)。

- [ ] **Step 1: 写测试**
```python
def test_variant_edge_pairs_star_not_quadratic():
    from app.services.kg.ppr import variant_edge_pairs
    kg = {f"o{i}": {"name": f"GPT v{i}"} for i in range(5)}  # 同 base "gpt"
    edges = variant_edge_pairs(kg, 0.5)
    # 星型:4 条无向(代表↔其余),非 C(5,2)=10 条
    undirected = {frozenset((a, b)) for a, b, _ in edges}
    assert len(undirected) == 4                    # k-1,非 k(k-1)/2
    # 连通性:所有成员经代表连通(代表在每条边里)
    from collections import Counter
    deg = Counter(); [deg.update([a, b]) for a, b, _ in edges]
    assert max(deg.values()) == 4                  # 代表 degree=k-1

def test_emb_synonym_edges_ann_beyond_cutoff():
    import numpy as np
    from app.services.kg.ppr import emb_synonym_edges
    rng = np.random.RandomState(0)
    n = 60000                                       # > 旧 max_entities=50000
    # 造 3 对近似同义(其余随机),确认 ANN 版能召回这几对(旧版此规模返 [])
    M = rng.randn(n, 16).astype(np.float32)
    for a, b in [(0, 1), (2, 3), (4, 5)]:
        M[b] = M[a] + 0.001 * rng.randn(16)
    ids = [f"e{i}" for i in range(n)]
    edges = emb_synonym_edges(ids, M, threshold=0.9, top_k=10)
    pairs = {frozenset((a, b)) for a, b, _ in edges}
    assert {"e0","e1"} in [set(p) for p in pairs] or frozenset(("e0","e1")) in pairs
    assert edges != []                              # 关键:超 5 万不再返 []
```
(实现者按实际造数据保证近似对被召回;核心断言:变体星型 k-1 条、emb_synonym 超 5 万非空。)

- [ ] **Step 2: 跑测试确认失败**(variant 现返 C(k,2)=10;emb_synonym n=6万返 [])。

- [ ] **Step 3: P1-7 variant 星型**——`variant_edge_pairs` 组内改:
```python
    for members in groups.values():
        uniq = sorted(set(members))
        if len(uniq) < 2:
            continue
        rep = uniq[0]
        for m in uniq[1:]:
            out.append((rep, m, float(weight)))   # 星型:O(k),连通性经 rep 保持
```

- [ ] **Step 4: P1-5 emb_synonym ANN**——`emb_synonym_edges` 重写为 hnswlib KNN(镜像 `kg_merge._ann_candidates`),去掉 `n>max_entities 返 []`:
```python
def emb_synonym_edges(ids, matrix, threshold: float = 0.8, top_k: int = 20,
                      max_entities: int = 50000):
    """hnswlib ANN KNN over entity embeddings → synonym edges (id_a,id_b,cosine).
    每节点取 top_k 邻居、cosine ≥ threshold。规模化:超 max_entities 不再返 []
    而是照常走 ANN(hnswlib 支持百万级);max_entities 仅作极端 OOM 兜底(见下)。"""
    import numpy as np, hnswlib
    n = len(ids)
    if n < 2 or matrix is None:
        return []
    M = np.asarray(matrix, dtype=np.float32)
    if M.ndim != 2 or M.shape[0] != n:
        return []
    norms = np.linalg.norm(M, axis=1, keepdims=True); norms[norms == 0] = 1.0
    M = M / norms
    dim = int(M.shape[1])
    try:
        idx = hnswlib.Index(space="cosine", dim=dim)
        idx.init_index(max_elements=n, ef_construction=200, M=16, random_seed=42)
        idx.add_items(M, np.arange(n))
        idx.set_ef(max(top_k + 1, 64))
        k = min(top_k + 1, n)                       # +1 因含自身
        labels, distances = idx.knn_query(M, k=k)
    except Exception:
        return []                                   # fail-open:同义边为空,不崩 build
    out, seen = [], set()
    for i in range(n):
        for lab, dist in zip(labels[i], distances[i]):
            j = int(lab)
            if j == i:
                continue
            sim = 1.0 - float(dist)
            if sim >= threshold:
                a, b = (i, j) if i < j else (j, i)
                if (a, b) not in seen:
                    seen.add((a, b)); out.append((ids[a], ids[b], sim))
    return out
```
（`max_entities` 参数保留兼容签名；语义从「超限返 []」改为「不再截断」。若真机极端规模需 OOM 兜底,可保留一个远高于 5 万的上限——实现者按需,但**默认不得再在 5 万处返 []**。）

- [ ] **Step 5: 跑测试 + 回归**——`test_ppr.py test_ppr_retrieve.py test_scale_index_repo.py test_graph_seed_fusion.py test_ppr_emb_synonym_defaults.py` 全绿(小规模同义/变体行为等价;新测试绿)。

- [ ] **Step 6: 提交**——`perf(kg): emb_synonym via ANN (P1-5, no 50k cutoff) + variant star edges (P1-7)`。

---

## Task 2: P1-6 `_ann_candidates` 分片→全局 hnsw(`kg_merge.py`)

**Files:** Modify `backend/app/services/kg_merge.py`(`_ann_candidates`);Test `backend/tests/test_kg_merge.py`。

- [ ] **Step 1: 读现状** —— `_ann_candidates`(L169)在 `idx_seeds > max_reps` 时分片,跨片同义对丢失 + WARNING。

- [ ] **Step 2: 写测试** —— 构造一个「跨片才配对」的场景(两个近似同义 seed,若分成两片则丢),断言全局版能配上:
```python
def test_ann_candidates_no_cross_shard_loss():
    import numpy as np
    from app.services.kg_merge import _ann_candidates
    rng = np.random.RandomState(1)
    seeds = [f"s{i}" for i in range(6)]
    reps = {s: rng.randn(16).astype(np.float32) for s in seeds}
    reps["s0"] = reps["s5"] + 0.001*rng.randn(16).astype(np.float32)  # s0~s5 近似同义
    # max_reps=3 旧版会把 s0,s5 分到不同片 → 丢;全局版应配上
    pairs = _ann_candidates(seeds, reps, lo=0.8, top_k=5, max_reps=3)
    got = {frozenset((a, b)) for a, b, *_ in pairs}
    assert frozenset(("s0", "s5")) in got
```

- [ ] **Step 3: 改为单全局 hnsw** —— 去掉分片分支,恒建单个全局 index(hnswlib 支持百万级);`max_reps` 保留为**极端 OOM 兜底**(远高于旧值,超过才 log WARNING + 可选分片),但默认路径**不分片**。保持返回 (a,b,sim) 去重格式与旧一致。实现者复用现有 `_run_shard` 的 hnsw 构造逻辑,只是不再切片、对全量 seeds 建一次。

- [ ] **Step 4: 跑测试 + 回归** —— `test_kg_merge.py test_unified_kg_repository.py test_kg_quality.py` 全绿。
- [ ] **Step 5: 提交** —— `perf(kg): _ann_candidates single global hnsw, no cross-shard synonym loss (P1-6)`。

---

## Self-Review
- **连通性 ≥ 旧**:variant 星型经 rep 连通(PPR 传播等价,不需全连);emb_synonym/`_ann_candidates` 超阈值不再丢对。
- **小规模等价**:小库下 ANN top_k ⊇ 原 blocked-matmul top_k(同 threshold),边集合近似一致(允许 ANN 近似小差)。
- **fail-open**:hnswlib 异常→同义边为空,不崩 build。
- **不改消费方**:边格式 (id_a,id_b,weight/sim) 不变。
