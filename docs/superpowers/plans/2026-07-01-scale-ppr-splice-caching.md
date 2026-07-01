# P0-3 scale_ppr splice 增量化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 active（场景B：少量上传）查询不再每次付 O(base) 的图重建代价，从而"上传即快查"。

**Architecture:** 两步走，均不改 PPR 结果语义（对齐现有全量重建为 oracle）。
(1) **缓存 combined 图**——splice 出来的 `combined_ids/_A/_index/_chunk_ids/_idf` 与 query 无关（同义桥用 active 向量、非查询向量），按 (base 索引版本 + active KG 版本) 版本键缓存，同一 active notebook 的连续查询直接复用、彻底跳过 splice。
(2) **向量化 splice_active + build_transition**——去掉 Python 逐边循环，让缓存未命中（上传后首查）的那一次构建也是 C 级速度。

**Tech Stack:** Python 3.13, scipy.sparse (CSR), numpy, pytest；复用 `VectorCache`（进程内版本键缓存）。

**Non-goals（本计划不做，记录为 follow-up）：** 真正 O(active) 的 bordered-block 增量（scipy 列手术 + 持久化 base colsum）。仅当缓存未命中的首查在 10M+ 边下实测仍太慢时才做。当前两步已把稳态查询降到 O(reset+PPR)、首查降到 C 级 O(E_base)。

---

## File Structure

- `backend/app/services/kg/scale_index.py` — 向量化 `build_transition`、`splice_active`（纯函数，Task 2）。
- `backend/app/services/sqlite_repository.py` — 抽出 `_scale_combined_graph()` 并接 `_vector_cache`；`scale_ppr` 改为消费缓存图（Task 1）。
- `backend/tests/test_scale_index.py` — splice/build_transition 向量化等价性测试（Task 2）。
- `backend/tests/test_ppr_retrieve.py` — combined 图缓存命中/失效测试（Task 1）。

运行测试统一在 worktree 的 backend 目录下（**不要**用 root checkout），解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。

---

## Task 1: 缓存 combined 图（稳态查询跳过 splice）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`scale_ppr` 约 L6319–6442 抽出为 `_scale_combined_graph`）
- Test: `backend/tests/test_ppr_retrieve.py`

- [ ] **Step 1: 写失败测试 — 连续两次 scale_ppr 只 splice 一次**

在 `tests/test_ppr_retrieve.py` 末尾追加。复用文件里已有的 `repo` fixture 与 `_seed_two_doc_moe`，再把该 notebook 标为 base 并建 scale 索引后，用第二个（active）notebook 触发 splice。若文件已有"建 base 索引 + active 查询"的辅助，优先复用；否则内联如下：

```python
def test_scale_ppr_caches_combined_graph(repo, monkeypatch):
    # base notebook with a persisted scale index
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))

    # active notebook (small) that will be spliced onto base each query
    active = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(active.id)

    import app.services.kg.scale_index as si
    calls = {"n": 0}
    real_splice = si.splice_active
    def counting_splice(*a, **k):
        calls["n"] += 1
        return real_splice(*a, **k)
    monkeypatch.setattr(si, "splice_active", counting_splice)

    r1 = repo.scale_ppr(active.id, "Mixture of Experts")
    n_after_first = calls["n"]
    r2 = repo.scale_ppr(active.id, "Mixture of Experts")
    # 第二次查询必须命中缓存、不再 splice
    assert calls["n"] == n_after_first
    # 结果形状一致（都返回 chunk 排名列表）
    assert isinstance(r1, list) and isinstance(r2, list)
```

- [ ] **Step 2: 运行确认失败**

Run: `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py::test_scale_ppr_caches_combined_graph -q`
Expected: FAIL —— `assert calls["n"] == n_after_first` 失败（当前每查询都 splice，第二次 `calls["n"]` 会翻倍）。

- [ ] **Step 3: 抽出 `_scale_combined_graph` 并缓存**

在 `SQLiteRepository` 中新增方法（放在 `scale_ppr` 之前）。把 `scale_ppr` 里从"组合图起点"到"combined_idf 计算完"（原 L6347–6442，含多 base splice、跨层同义桥、active splice、combined_idf）整段搬进 loader：

```python
def _scale_combined_graph(self, notebook_id: str, base_indexes):
    """query 无关的 base⊕active 组合图（进程内版本键缓存）。
    base_indexes: [(bid, ScaleIndex), ...]，非空。返回 dict:
      {combined_ids, combined_A, combined_index, combined_chunk_ids, combined_idf}。
    版本键 = (各 base manifest version) + (active _scale_index_version)，
    故 base 重建或 active 变更（含上传新源）都会失效重算，query 变化不失效。"""
    import numpy as np
    from app.services.kg import scale_index as si

    base_ver = tuple(sorted(
        (bid, tuple(idx.manifest.get("version", []))) for bid, idx in base_indexes))
    active_ver = tuple(self._scale_index_version(notebook_id))
    version = ("scale_combined", base_ver, active_ver)

    def _load():
        first_id, first = base_indexes[0]
        combined_ids = list(first.node_ids)
        combined_A = first.transition
        combined_idf_map: Dict[str, float] = {
            nid: float(first.idf[i]) for i, nid in enumerate(first.node_ids)}
        combined_chunk_ids: set = {
            first.node_ids[i] for i in first.chunk_index
            if 0 <= int(i) < len(first.node_ids)}

        for bid, idx in base_indexes[1:]:
            extra_ids, extra_A = si.splice_active(
                combined_ids, combined_A, list(idx.node_ids),
                si.csr_to_edges(idx.node_ids, idx.transition))
            combined_ids, combined_A = extra_ids, extra_A
            for i, nid in enumerate(idx.node_ids):
                combined_idf_map.setdefault(nid, float(idx.idf[i]))
            for i in idx.chunk_index:
                if 0 <= int(i) < len(idx.node_ids):
                    combined_chunk_ids.add(idx.node_ids[int(i)])

        active_node_ids, active_edges, active_chunk_ids = self._active_kg_delta(notebook_id)

        # 跨层同义桥（与 query 无关：用 active 节点向量 + base ANN）
        if self.settings.ppr_emb_synonym_enabled:
            active_edges = self._scale_xlayer_bridge_edges(
                notebook_id, base_indexes, active_edges)

        combined_ids, combined_A = si.splice_active(
            combined_ids, combined_A, active_node_ids, active_edges)
        combined_index = {nid: i for i, nid in enumerate(combined_ids)}
        combined_chunk_ids.update(active_chunk_ids)
        combined_idf = np.array(
            [combined_idf_map.get(nid, 1.0) for nid in combined_ids], dtype=np.float64)
        return {
            "combined_ids": combined_ids, "combined_A": combined_A,
            "combined_index": combined_index,
            "combined_chunk_ids": combined_chunk_ids, "combined_idf": combined_idf,
        }

    return self._vector_cache.get(f"{notebook_id}:scale_combined", version, _load)
```

把原 scale_ppr 里的跨层同义桥整段（原 L6387–6433）抽成 `_scale_xlayer_bridge_edges(notebook_id, base_indexes, active_edges)`，原样返回 `active_edges`（拼上 bridge 边）；逻辑与位置照搬，仅参数化。

- [ ] **Step 4: scale_ppr 改为消费缓存图**

`scale_ppr` 中，`base_indexes` 计算保留；随后删掉原 L6347–6442 的组合图构建，替换为：

```python
        graph = self._scale_combined_graph(notebook_id, base_indexes)
        combined_ids = graph["combined_ids"]
        combined_A = graph["combined_A"]
        combined_index = graph["combined_index"]
        combined_chunk_ids = graph["combined_chunk_ids"]
        combined_idf = graph["combined_idf"]
```

reset 构建（3a base ANN 种子、3b active 种子、3c chunk 种子）与其后 PPR、chunk 排名**保持不变**（它们本就在 combined_* 之后、且 query 相关）。注意 3a 仍用 `base_indexes` 循环查 ANN——不变。

- [ ] **Step 5: 运行新测试 + 回归**

Run: `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py tests/test_reasoning_ppr.py tests/test_scale_index_repo.py -q`
Expected: PASS（含新 `test_scale_ppr_caches_combined_graph`；原有全部仍绿）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "perf(ppr): cache query-independent combined base⊕active graph in scale_ppr"
```

---

## Task 2: 向量化 splice_active + build_transition（加速缓存未命中的首查）

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`（`build_transition` L113–138、`splice_active` L221–241）
- Test: `backend/tests/test_scale_index.py`

- [ ] **Step 1: 写等价性测试（向量化前后结果逐元素一致）**

在 `tests/test_scale_index.py` 追加。构造一个中等随机图，断言 `splice_active` 结果与"参考全量重建"逐元素相等（allclose），并覆盖悬空边、共享 id 合并：

```python
def test_splice_active_matches_full_rebuild():
    import numpy as np
    from app.services.kg import scale_index as si
    base_ids = [f"b{i}" for i in range(20)]
    base_edges = [(base_ids[i], base_ids[(i * 7 + 3) % 20], 1.0) for i in range(20)]
    base_A, _ = si.build_transition(base_ids, base_edges)
    active_ids = ["b3", "b7", "x0", "x1"]          # 含共享 id (b3,b7) + 新 id
    active_edges = [("x0", "b3", 1.0), ("b3", "x0", 1.0),
                    ("x1", "x0", 1.0), ("x0", "x1", 1.0),
                    ("x1", "ZZZ", 1.0)]              # 末条悬空(端点不存在)应被丢弃
    combined_ids, combined_A = si.splice_active(base_ids, base_A, active_ids, active_edges)

    # 参考：与 splice 同语义的全量重建（base 边由结构还原、权重 1.0）
    ref_base_edges = si.csr_to_edges(base_ids, base_A)
    ref_ids = list(base_ids) + [a for a in active_ids if a not in set(base_ids)]
    ref_A, _ = si.build_transition(ref_ids, ref_base_edges + active_edges)
    assert combined_ids == ref_ids
    assert np.allclose(combined_A.toarray(), ref_A.toarray())
    # 列随机性守恒（非空列和为 1）
    cs = np.asarray(combined_A.sum(axis=0)).ravel()
    assert np.allclose(cs[cs > 0], 1.0)
```

- [ ] **Step 2: 运行确认通过（当前实现已满足语义，作为改前基线）**

Run: `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py::test_splice_active_matches_full_rebuild -q`
Expected: PASS（此测试锁住语义；Step 3 向量化后必须仍 PASS——这才是防回归的意义）。

- [ ] **Step 3: 向量化 build_transition（去掉 Python 逐边 append）**

替换 `scale_index.py` 的 `build_transition`：

```python
def build_transition(node_ids, edges):
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    if not edges:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    src = np.fromiter((index.get(s, -1) for s, _, _ in edges), dtype=np.int64, count=len(edges))
    tgt = np.fromiter((index.get(t, -1) for _, t, _ in edges), dtype=np.int64, count=len(edges))
    w = np.fromiter((float(wt) for _, _, wt in edges), dtype=np.float64, count=len(edges))
    keep = (src >= 0) & (tgt >= 0)
    src, tgt, w = src[keep], tgt[keep], w[keep]
    if src.size == 0:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    M = sp.csr_matrix((w, (tgt, src)), shape=(n, n), dtype=np.float64)  # A[j,i]=i->j
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index
```

- [ ] **Step 4: 向量化 splice_active（base 边直接用 CSR 结构数组，不建元组列表）**

替换 `splice_active`：

```python
def splice_active(base_ids, base_transition, active_ids, active_edges):
    base_set = set(base_ids)
    combined_ids = list(base_ids) + [a for a in active_ids if a not in base_set]
    index = {nid: i for i, nid in enumerate(combined_ids)}
    n = len(combined_ids)

    coo = base_transition.tocoo()
    # base 结构还原：source=base_ids[col], target=base_ids[row]，权重 1.0（保持原语义）。
    # combined 前 len(base_ids) 个 id 与 base 对齐，故索引可直接复用。
    base_src = coo.col.astype(np.int64)
    base_tgt = coo.row.astype(np.int64)
    base_w = np.ones(coo.nnz, dtype=np.float64)

    if active_edges:
        a_src = np.fromiter((index.get(s, -1) for s, _, _ in active_edges),
                            dtype=np.int64, count=len(active_edges))
        a_tgt = np.fromiter((index.get(t, -1) for _, t, _ in active_edges),
                            dtype=np.int64, count=len(active_edges))
        a_w = np.fromiter((float(w) for _, _, w in active_edges),
                          dtype=np.float64, count=len(active_edges))
        keep = (a_src >= 0) & (a_tgt >= 0)
        src = np.concatenate([base_src, a_src[keep]])
        tgt = np.concatenate([base_tgt, a_tgt[keep]])
        w = np.concatenate([base_w, a_w[keep]])
    else:
        src, tgt, w = base_src, base_tgt, base_w

    if src.size == 0:
        return combined_ids, sp.csr_matrix((n, n), dtype=np.float64)
    M = sp.csr_matrix((w, (tgt, src)), shape=(n, n), dtype=np.float64)
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return combined_ids, (M @ D).tocsr()
```

- [ ] **Step 5: 运行等价性测试 + scale/ppr 全回归**

Run: `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py tests/test_scale_index_repo.py tests/test_ppr.py tests/test_ppr_retrieve.py tests/test_reasoning_ppr.py -q`
Expected: PASS（Step 1 等价性测试仍绿证明语义不变；67+ 用例全绿）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/kg/scale_index.py backend/tests/test_scale_index.py
git commit -m "perf(scale): vectorize build_transition/splice_active (drop per-edge Python loops)"
```

---

## Self-Review

- **Spec 覆盖**：P0-3 的两个成本源——重复查询的重建（Task 1 缓存消除）、单次构建的 Python 逐边循环（Task 2 向量化消除）——均有任务覆盖。真正 O(active) 增量列为显式 non-goal/follow-up。
- **语义不变**：Task 1 只是把 query 无关段搬进版本键缓存（同 `_ppr_graph` 既有套路），结果不变；Task 2 有逐元素等价性测试对齐 oracle。两者都不改 reset/PPR/排序。
- **类型一致**：`_scale_combined_graph` 返回 dict 的键与 scale_ppr 解构一一对应；`splice_active`/`build_transition` 签名与返回类型不变（`(ids, csr)` / `(csr, index)`）。
- **版本键正确性**：base 变更经 manifest version、active 变更（含上传新源）经 `_scale_index_version` 反映；bridge 设置已含在 `_scale_index_version` 内——上传即失效、query 不失效。✔
```
