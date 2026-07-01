# Phase 1b-ii：scale_ppr 修 P0-00(self index 核 ⊕ self-delta) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`)。

**Goal:** 直接查询一个自身有 scale 索引的 notebook 时,`scale_ppr` 用它**自己的索引**(CSR 核 + ANN 种子)跑 PPR，而不是因 `id != active` 排除 self → 返回 [] → 回退 rustworkx 全内存图(P0-00）。self 的水位后新增(self-delta）以增量 splice 保新鲜。

**Architecture:** (1) `_gather_kg_graph` 加 `source_ids` 过滤（默认 None＝现状）；`_active_kg_delta` 用 Phase 1a 的 `_index_delta`——self 已索引时只取 delta 域、否则整库。(2) `scale_ppr` 把 self 的 `allow_stale` 索引纳入 participants（self CSR 当 substrate、self ANN 当种子源），active splice 变成 self-delta（避免把整张 self KG 重 splice 到自己 CSR 上）。

**Tech Stack:** scipy/numpy/hnswlib/SQLite/pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`；测试在 worktree `backend/`。

## Global Constraints

- 依据 [spec §5](../specs/2026-07-01-index-lifecycle-redesign.md) 与 GPT review 的 P0-00。
- **默认等价**：`_gather_kg_graph()`（不传 source_ids）与改前逐字节等价 → `build_scale_index`/既有 `_active_kg_delta` 行为不变。
- **保守**：self-index 路径只在 self 有有效（含 stale）索引时启用；无索引的小库/旧库仍回退 rustworkx（现状）。
- **不重复计入**：self 已索引时，active splice 只含 self-delta（水位后），不含索引核已有的内容。
- delta 连回核：靠共享 cluster hub（`cluster:{canonical_id}` 同 id，splice_active 自动 unify）+ 已有跨层 ANN 桥（`_scale_xlayer_bridge_edges`）。

---

## File Structure

- `backend/app/services/sqlite_repository.py` — `_gather_kg_graph`（加 source_ids）、`_active_kg_delta`（delta 域）、`scale_ppr`（self participant）。
- `backend/tests/test_ppr_retrieve.py` — 测试。

---

## Task 1: `_gather_kg_graph` 加 source 分域 + `_active_kg_delta` delta 域

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_gather_kg_graph` 约 L6678、`_active_kg_delta` 约 L7078）
- Test: `backend/tests/test_ppr_retrieve.py`

**Interfaces:**
- Consumes: `_index_delta(nb) -> {delta_sources, delta_chunks, indexed}`（Phase 1a）。
- Produces: `_gather_kg_graph(nb, source_ids=None)`（None＝全库现状；给定列表＝只取这些 source 的 objects/relations/chunks + 其 memberships/cluster-hub，跳过 variant/synonym 额外边）。`_active_kg_delta(nb)` 在 self 已索引时返回 self-delta 域，否则整库。

- [ ] **Step 1: 写失败测试 — 默认等价 + delta 域正确**

追加到 `tests/test_ppr_retrieve.py`（复用 `repo` fixture / `_seed_two_doc_moe`）：
```python
def test_gather_kg_graph_source_scoping(repo):
    from app.models.schemas import NotebookCreate
    import json
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    def add(sid, oid, cid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, name, "", "[]", now))
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": cid,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": name, "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), ev, sid, now, now))
    add("s1", "o1", "c1", "alpha", 1)
    add("s2", "o2", "c2", "beta", 2)
    # 全库(默认 None)= 两个 source 都在
    node_ids, edges, chunk_ids, kg_ids, _ = repo._gather_kg_graph(nb.id)
    assert set(kg_ids) == {"o1", "o2"} and set(chunk_ids) == {"c1", "c2"}
    # 只取 s2 域 = 只有 o2/c2
    n2, e2, c2, k2, _ = repo._gather_kg_graph(nb.id, source_ids=["s2"])
    assert set(k2) == {"o2"} and set(c2) == {"c2"} and "o1" not in set(n2)
    # 空 source_ids = 空
    assert repo._gather_kg_graph(nb.id, source_ids=[]) == ([], [], [], [], {})
```

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py::test_gather_kg_graph_source_scoping -q`
预期 FAIL（`_gather_kg_graph` 不接受 `source_ids`）。

- [ ] **Step 3: `_gather_kg_graph` 加 source_ids**

改签名 `def _gather_kg_graph(self, notebook_id: str, source_ids=None):`。在方法开头(取 `ph` 之后)加分域子句；空列表直接返回空；给定时在三个 SELECT 加 `AND source_id IN (...)`;memberships 限到已 gather 的对象;`source_ids` 非 None 时跳过 `extra_edges`(variant/synonym)。关键片段:
```python
        ph = ",".join("?" for _ in USABLE_STATUSES)
        scoped = source_ids is not None
        if scoped and not source_ids:
            return [], [], [], [], {}
        src_clause, src_params = "", ()
        if scoped:
            ph_s = ",".join("?" for _ in source_ids)
            src_clause = f" AND source_id IN ({ph_s})"
            src_params = tuple(source_ids)
        ...
        with self._connect() as db:
            for r in db.execute(
                    f"SELECT id, object_type, payload FROM knowledge_objects "
                    f"WHERE notebook_id=? AND status IN ({ph}){src_clause}",
                    (notebook_id, *USABLE_STATUSES, *src_params)).fetchall():
                kg_nodes[r["id"]] = {...}   # 不变
            for r in db.execute(
                    f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                    f"WHERE notebook_id=?{src_clause}", (notebook_id, *src_params)).fetchall():
                relations.append(dict(r))
            for r in db.execute(
                    f"SELECT id FROM chunks WHERE notebook_id=?{src_clause}",
                    (notebook_id, *src_params)).fetchall():
                chunk_ids.append(r["id"])
            for r in db.execute(   # cluster_groups 全取;下方 present 过滤天然按 node_ids 分域
                    "SELECT canonical_id, member_object_id FROM concept_clusters WHERE notebook_id=?",
                    (notebook_id,)).fetchall():
                cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])

        ent_chunk_map = self._ent_chunk_map(notebook_id)
        _kg_keys = set(kg_nodes.keys())
        memberships = [(oid, cid) for oid, cids in ent_chunk_map.items()
                       if (not scoped or oid in _kg_keys) for cid in cids]
        membership_counts = {oid: len(cids) for oid, cids in ent_chunk_map.items()
                             if (not scoped or oid in _kg_keys)}

        extra_edges = []
        if not scoped:
            extra_edges = variant_edge_pairs(kg_nodes, self.settings.ppr_variant_edge_weight)
            with self._connect() as db:
                ann_ids_raw, ann_matrix_raw = self._vector_matrix(
                    db, notebook_id, "knowledge_embeddings", "object_id")
            ann_ids = list(ann_ids_raw) if ann_ids_raw else []
            has_vecs = bool(ann_ids) and ann_matrix_raw is not None and len(ann_matrix_raw)
            if has_vecs and self.settings.ppr_emb_synonym_enabled:
                extra_edges = extra_edges + emb_synonym_edges(
                    ann_ids, np.asarray(ann_matrix_raw),
                    self.settings.ppr_emb_synonym_threshold,
                    self.settings.ppr_emb_synonym_topk,
                    self.settings.ppr_emb_synonym_max_entities)
```
其余(node_ids 组装、`_add_undirected` 三循环、cluster hub 循环、返回)保持不变。注意 `import numpy as np`/`variant_edge_pairs`/`emb_synonym_edges` 的导入仍需在(仅 not scoped 用)。

- [ ] **Step 4: `_active_kg_delta` 用 delta 域**

改 `_active_kg_delta`(约 L7078):
```python
    def _active_kg_delta(self, notebook_id: str):
        """ACTIVE/self 的 KG delta,供 splice 到 base/self scale 索引。
        self 已索引时只取水位后新增 source(避免与索引核重复);否则整库。"""
        delta = self._index_delta(notebook_id)
        src = delta["delta_sources"] if delta["indexed"] else None
        node_ids, edges, chunk_ids, _kg_node_ids, _membership_counts = \
            self._gather_kg_graph(notebook_id, source_ids=src)
        return node_ids, edges, chunk_ids
```

- [ ] **Step 5: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py tests/test_scale_index_repo.py tests/test_reasoning_ppr.py tests/test_graph_seed_fusion.py -q`
预期全 PASS(默认 None 等价 → 既有 splice/graph 行为不变;新分域测试绿)。

- [ ] **Step 6: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(scale): source-scoped _gather_kg_graph; _active_kg_delta uses post-watermark self-delta when indexed"
```

---

## Task 2: `scale_ppr` 纳入 self index(修 P0-00)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`scale_ppr` participant 选取，约 L7246–7253）
- Test: `backend/tests/test_ppr_retrieve.py`

**Interfaces:**
- Consumes: `_scale_index(nb, allow_stale=True)`（#142）、`_active_kg_delta`（Task 1，已 delta 域）。
- Produces: `scale_ppr(nb, q)` 在 self 有(含 stale)索引时不返回 []、不回退 rustworkx；用 self 索引核 ⊕ self-delta 跑 PPR。

- [ ] **Step 1: 写失败测试 — 直接查 base 走 self index**

```python
def test_scale_ppr_uses_self_index(repo, monkeypatch):
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    # 没有"别的" base —— 旧行为 base_indexes=[] → return [] → 回退 rustworkx
    import app.services.kg.ppr as ppr_mod
    called = {"n": 0}
    real = ppr_mod.build_ppr_graph
    monkeypatch.setattr(ppr_mod, "build_ppr_graph", lambda *a, **k: (called.__setitem__("n", called["n"]+1), real(*a, **k))[1])
    ranked = repo.scale_ppr(base.id, "Mixture of Experts")
    assert ranked != []                         # self index 生效,非空
    # scale_ppr 自身不应触发 rustworkx build_ppr_graph(那是 _ppr_retrieve 的回退)
    assert called["n"] == 0
```
（若 `scale_ppr` 内不直接调 `build_ppr_graph`，该 monkeypatch 计数天然为 0；核心断言是 `ranked != []`——旧代码此处必为 []。实现者可据实际调用简化断言，核心是"self-base 直接查返回非空 chunk 排名"。）

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py::test_scale_ppr_uses_self_index -q`
预期 FAIL（`ranked == []`，因当前 `base_indexes` 排除 self）。

- [ ] **Step 3: scale_ppr 纳入 self index**

改 `scale_ppr` 的 participant 选取(约 L7246–7253):
```python
        with self._connect() as db:
            base_ids = [r["id"] for r in db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (notebook_id,)).fetchall()]
        base_indexes = [(bid, self._scale_index(bid, allow_stale=True)) for bid in base_ids]
        base_indexes = [(bid, idx) for bid, idx in base_indexes if idx is not None]
        # P0-00: 自身若有(含 stale)索引,把 self 也当作 participant(self CSR=substrate,
        # self ANN=种子源)。active splice 由 _active_kg_delta 自动收窄为 self-delta。
        self_idx = self._scale_index(notebook_id, allow_stale=True)
        if self_idx is not None:
            base_indexes = base_indexes + [(notebook_id, self_idx)]
        if not base_indexes:
            return []
```
其余(`_scale_combined_graph(notebook_id, base_indexes)`、reset via base ANN、PPR、chunk 排名)不变——因为 `_active_kg_delta`(Task 1)已在 self 已索引时只取 delta,不会把 self 核重复 splice。

- [ ] **Step 4: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py tests/test_reasoning_ppr.py tests/test_scale_index_repo.py tests/test_graph_seed_fusion.py -q`
预期全 PASS。特别确认既有「active personal + 单独 base」场景(base_indexes 非空、self 无索引)不变。

- [ ] **Step 5: 加「新上传可达」测试**

```python
def test_scale_ppr_self_index_reaches_new_upload(repo):
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id); repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    # build 后新上传一篇(delta),其 chunk 应能经 self-delta splice 参与 PPR
    import json
    with repo._write() as db:
        now = "2026-07-09T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s-new", base.id, "new", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c-new", base.id, "s-new", "Mixture of Experts routing", "", "[]", now))
        ev = json.dumps([{"source_id": "s-new", "source_title": "", "element_id": "c-new",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": "MoE", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                   "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("o-new", base.id, "concept", "approved", "", json.dumps({"name": "Mixture-of-Experts (MoE)"}),
                    ev, "s-new", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                   "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,?,?)",
                   ("cl-new", base.id, "K-moe", "o-new", "Mixture-of-Experts (MoE)", "concept", now))
    ranked = repo.scale_ppr(base.id, "Mixture of Experts")
    assert "c-new" in {cid for cid, _ in ranked}   # 新上传 chunk 经 self-delta 可达
```

- [ ] **Step 6: 跑测试 + 提交**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ppr_retrieve.py -q`
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(scale): scale_ppr uses self index (fix P0-00: direct base query no longer falls back to rustworkx)"
```

---

## Self-Review

- **Spec 覆盖**：实现 spec §5 的 `scale_ppr` 修 P0-00（self index 核 ⊕ self-delta splice）。KG 对象/federated ⊕（1b-iii）后续。
- **默认等价**：`_gather_kg_graph()` 不传 source_ids 时字节等价 → build_scale_index 与既有 base+active splice 不变。
- **不重复计入**：self 已索引 → `_active_kg_delta` 只取 delta 域，self 核由其 CSR participant 提供，无双重表示。
- **保守**：self-index 仅在有(含 stale)索引时启用；小库无索引仍回退 rustworkx。
- **连通性**：delta 经共享 cluster hub 同 id unify + 跨层 ANN 桥连回核；新上传测试(Step 5)锚定可达。
- **类型一致**：`_gather_kg_graph` 返回五元组不变；`_active_kg_delta` 返回三元组不变；`_index_delta` 键一致。
- **已知**：`_active_kg_delta` 现被 `_scale_combined_graph`(缓存)消费,缓存键含 `_scale_index_version`(self)+base manifest 版本,self-delta 变化经 self 版本键反映,正确失效。

---

## 后续
- **Phase 1b-iii**：`_retrieve_scored`/`federated_retrieve` KG 对象 ⊕（indexed 核 ANN ⊕ delta 暴力）+ 孤立点预算。
- **Phase 2**：增量 fold（delta 收进 ANN/CSR）。
