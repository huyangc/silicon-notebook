# Phase 2：增量 fold(delta 收进索引,O(delta) 不全量重建) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps use checkbox。

**Goal:** 当 base 大到全量 `build_scale_index` 都嫌贵时,用 **O(delta)** 的增量 fold 把水位后新增(delta)收进现有索引:ANN 用 `add_items(delta)`(不重建整个 ANN——这是全量重建最贵处)、CSR 用 splice、idf/chunk_index 数组扩展、水位前移。写 tmp 目录后锁内原子交换,查询期零中断。

**Architecture:** 新 `fold_scale_index_delta(nb)`:load 现有(含 stale)ScaleIndex → `_gather_kg_graph(nb, source_ids=delta_sources)` 取 delta → `splice_active` 拼 CSR → 扩 node_ids/chunk_index/idf → hnswlib `load_index+add_items(delta 向量)+save` 增量扩 ann.bin/chunk_ann.bin → 写 `{dir}.tmp` → `_scale_building_lock` 内 rename 交换 → 水位=当前全部 source、version bump。viz 保持旧(UI-only,可 stale)。无现有索引→退回全量 `build_scale_index`。

**Tech Stack:** hnswlib(add_items/resize_index)、scipy(splice)、numpy、SQLite、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。

## Global Constraints

- 依据 [spec §4](../specs/2026-07-01-index-lifecycle-redesign.md) 两档合并的「fold」档。
- **等价目标**:fold 后的索引用于检索(chunk/KG/PPR)结果与「全量重建」**近似**(ANN 增量插入 recall 允许小差;节点/边集合、chunk 可召回性一致)。
- **原子**:fold 写 tmp 目录,`_scale_building_lock` 内两步 rename 交换;交换瞬间若并发 load 读到无 manifest → 返回 None → 检索临时回退(暴力/rustworkx),不报错。
- **fail-open / 幂等**:fold 失败保留旧索引(tmp 未交换即丢弃);无 delta → no-op;无现有索引 → 全量。
- 复用 `_index_delta`、`_gather_kg_graph(source_ids=)`、`splice_active`、`_vector_matrix`。
- 与既有 `_scale_building` in-flight 守卫串行(fold 与 build 互斥)。

---

## File Structure

- `backend/app/services/kg/scale_index.py` — `fold_arrays(...)` 纯函数(拼 CSR/扩数组,可单测)+ `add_items_to_ann(...)` 增量 ANN 辅助。
- `backend/app/services/sqlite_repository.py` — `fold_scale_index_delta(nb)` 编排 + 原子交换。
- `backend/tests/test_scale_index.py`(纯函数)+ `test_scale_index_repo.py`(端到端 fold vs 全量)。

---

## Task 1: `scale_index.fold_arrays` 纯函数(CSR splice + 数组扩展)

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`（新 `fold_arrays`）
- Test: `backend/tests/test_scale_index.py`

**Interfaces:**
- Produces: `fold_arrays(base_node_ids, base_transition, base_idf, base_chunk_index, delta_node_ids, delta_edges, delta_chunk_ids, delta_idf_map) -> (node_ids, transition, idf, chunk_index)`。
  - `splice_active` 拼 CSR;node_ids=base + 新 delta(splice 顺序);idf=base.idf ⊕ 新节点 idf(delta_idf_map.get(id,1.0));chunk_index=base chunk 位置(前缀不变)+ 新 delta chunk 位置。

- [ ] **Step 1: 写失败测试**

```python
def test_fold_arrays_extends_base():
    import numpy as np
    from app.services.kg import scale_index as si
    base_ids = ["a", "b", "cA"]           # cA = chunk
    base_edges = [("a", "b", 1.0), ("b", "a", 1.0), ("a", "cA", 1.0), ("cA", "a", 1.0)]
    base_A, _ = si.build_transition(base_ids, base_edges)
    base_idf = np.array([0.5, 1.0, 1.0])  # a,b,cA
    base_chunk_index = np.array([2])       # cA at pos 2
    d_ids = ["c", "cB"]                    # new kg node c + new chunk cB
    d_edges = [("c", "cB", 1.0), ("cB", "c", 1.0), ("c", "a", 1.0), ("a", "c", 1.0)]
    node_ids, A, idf, chunk_index = si.fold_arrays(
        base_ids, base_A, base_idf, base_chunk_index, d_ids, d_edges, ["cB"], {"c": 0.25})
    assert node_ids == ["a", "b", "cA", "c", "cB"]      # 前缀不变 + 追加
    assert list(chunk_index) == [2, 4]                  # cA(2) + cB(4)
    assert abs(idf[3] - 0.25) < 1e-9 and idf[4] == 1.0  # c 用 map,cB 默认 1.0
    cs = np.asarray(A.sum(axis=0)).ravel()
    assert np.allclose(cs[cs > 0], 1.0)                 # 列随机守恒
```

- [ ] **Step 2: 跑测试确认失败** → `pytest tests/test_scale_index.py::test_fold_arrays_extends_base -q`(FAIL: no fold_arrays)。

- [ ] **Step 3: 实现 `fold_arrays`**

```python
def fold_arrays(base_node_ids, base_transition, base_idf, base_chunk_index,
                delta_node_ids, delta_edges, delta_chunk_ids, delta_idf_map):
    """把 delta splice 进 base 索引数组。base_node_ids 是 combined 的前缀,故 base
    的 chunk_index 位置不变;新节点追加在后。返回 (node_ids, transition, idf, chunk_index)。"""
    node_ids, transition = splice_active(list(base_node_ids), base_transition,
                                         list(delta_node_ids), list(delta_edges))
    base_n = len(base_node_ids)
    # idf:前缀复用 base.idf,新节点用 delta_idf_map(缺省 1.0)
    idf = np.ones(len(node_ids), dtype=np.float64)
    idf[:base_n] = np.asarray(base_idf, dtype=np.float64)[:base_n]
    for i in range(base_n, len(node_ids)):
        idf[i] = float(delta_idf_map.get(node_ids[i], 1.0))
    # chunk_index:base chunk 位置(前缀不变)+ 新 delta chunk 位置
    pos = {nid: i for i, nid in enumerate(node_ids)}
    chunk_index = list(np.asarray(base_chunk_index, dtype=np.int64)) + \
        [pos[c] for c in delta_chunk_ids if c in pos and pos[c] >= base_n]
    return node_ids, transition, idf, np.asarray(chunk_index, dtype=np.int32)
```
(`import numpy as np` 已在文件顶部。)

- [ ] **Step 4: 跑测试** → PASS。

- [ ] **Step 5: 提交** → `git add scale_index.py test_scale_index.py; git commit -m "feat(scale): fold_arrays — splice delta into base index arrays (O(delta))"`

---

## Task 2: `fold_scale_index_delta` 编排 + 增量 ANN + 原子交换

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`(`add_items_to_ann` 辅助)、`backend/app/services/sqlite_repository.py`（`fold_scale_index_delta`）
- Test: `backend/tests/test_scale_index_repo.py`

**Interfaces:**
- Consumes: `fold_arrays`(Task1)、`_index_delta`、`_gather_kg_graph(source_ids=)`、`_scale_index(allow_stale=True)`、`_scale_index_version`。
- Produces: `add_items_to_ann(src_bin, dim, add_vectors, base_count) -> hnswlib.Index`(load src + resize + add_items,labels 从 base_count 递增);`fold_scale_index_delta(nb) -> manifest`。

- [ ] **Step 1: 写失败测试 —— fold 后 delta 可召回、水位前移、节点数增长**

```python
def test_fold_scale_index_delta(repo):
    import json, os, numpy as np
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, cid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, name, "", "[]", now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            for tbl, key in [("chunk_embeddings", cid), ("knowledge_embeddings", oid)]:
                v = repo.embedder.embed_texts([name])[0]
                col = "chunk_id" if tbl == "chunk_embeddings" else "object_id"
                db.execute(f"INSERT INTO {tbl} ({col},notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (key, nb.id, json.dumps(v), now))
    add("s1", "o1", "c1", "current mirror", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    m0 = repo.scale_index_status(nb.id)
    add("s2", "o2", "c2", "bandgap reference special", 2)   # delta
    assert repo._index_delta(nb.id)["delta_chunks"] == 1
    # fold
    repo.fold_scale_index_delta(nb.id)
    # 水位前移 → delta 清零、state 回 indexed
    d = repo._index_delta(nb.id)
    assert d["delta_chunks"] == 0 and d["delta_sources"] == []
    # 折叠后 ann 含新对象、chunk_ann 含新 chunk、CSR 节点增长
    idx = repo._scale_index(nb.id)
    assert idx is not None                       # 版本新鲜(fold 更新了 manifest version)
    assert "o2" in set(idx.ann_labels) and "c2" in set(idx.chunk_ann_labels)
    assert len(idx.node_ids) > len(np.load(...)) or idx.manifest["n_nodes"] > m0["n_nodes"]
    # 检索能召回 fold 进来的 c2(经 ANN,非 delta 暴力)
    monkey_recall = idx  # via _retrieve_chunks_ann
    out = repo._retrieve_chunks_ann(nb.id, "bandgap reference special",
                                    repo._embed_query("bandgap reference special"), idx, recall=10)
    assert out and "c2" in {c.chunk_id for c in out[0]}
```
(实现者可精简断言，核心:fold 后 delta 归零、ann/chunk_ann 含新 id、n_nodes 增长、新内容经 ANN 可召回。)

- [ ] **Step 2: 跑测试确认失败** → FAIL(no `fold_scale_index_delta`)。

- [ ] **Step 3: `add_items_to_ann` 辅助**（scale_index.py）

```python
def add_items_to_ann(src_bin, dim, add_vectors, base_count):
    """load 现有 hnsw(base_count 个)→ resize 容纳 base_count+len(add)→ add_items
    (labels 从 base_count 递增)→ 返回 index(调用方 save)。add_vectors: (m,dim) float32。"""
    import hnswlib, numpy as np
    idx = hnswlib.Index(space="cosine", dim=dim)
    idx.load_index(src_bin, max_elements=base_count + len(add_vectors))
    if len(add_vectors):
        idx.add_items(np.asarray(add_vectors, dtype=np.float32),
                      np.arange(base_count, base_count + len(add_vectors)))
    return idx
```

- [ ] **Step 4: `fold_scale_index_delta`**（sqlite_repository.py,`build_scale_index` 附近）

```python
    def fold_scale_index_delta(self, notebook_id: str) -> dict:
        """O(delta) 增量 fold:delta splice 进现有索引(ANN add_items、CSR splice),
        写 tmp 目录后锁内原子交换。无现有索引→全量 build;无 delta→no-op。"""
        import os, shutil, numpy as np
        from app.services.kg import scale_index as si
        idx = self._scale_index(notebook_id, allow_stale=True)
        if idx is None:
            return self.build_scale_index(notebook_id)
        delta = self._index_delta(notebook_id)
        if not delta["delta_sources"]:
            return idx.manifest
        with self._scale_building_lock:
            if notebook_id in self._scale_building:
                return {"status": "already_building"}
            self._scale_building.add(notebook_id)
        try:
            d_nodes, d_edges, d_chunks, d_kg_ids, d_membership = \
                self._gather_kg_graph(notebook_id, source_ids=delta["delta_sources"])
            kg_set = set(d_kg_ids)
            d_idf_map = {oid: (1.0 / c if c > 0 else 1.0)
                         for oid, c in d_membership.items()}
            node_ids, transition, idf, chunk_index = si.fold_arrays(
                list(idx.node_ids), idx.transition, idx.idf, idx.chunk_index,
                d_nodes, d_edges, d_chunks, d_idf_map)
            out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
            tmp_dir = out_dir + ".tmp"
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
            os.makedirs(tmp_dir, exist_ok=True)
            # 非 ANN 工件:node_ids/idf/chunk_index/graph
            import scipy.sparse as sp
            sp.save_npz(os.path.join(tmp_dir, "graph.npz"), transition)
            np.save(os.path.join(tmp_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
            np.save(os.path.join(tmp_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
            np.save(os.path.join(tmp_dir, "chunk_index.npy"), np.asarray(chunk_index, dtype=np.int32))
            # ANN(KG 对象):增量 add delta 对象向量
            dim = int(idx.manifest.get("dim", self.settings.embed_dim))
            def _delta_vecs(table, col, ids):
                if not ids:
                    return [], []
                ph = ",".join("?" for _ in ids)
                with self._connect() as db:
                    rows = db.execute(
                        f"SELECT {col} AS vid, vector FROM {table} WHERE notebook_id=? AND {col} IN ({ph})",
                        (notebook_id, *ids)).fetchall()
                from app.services.vector_index import build_matrix
                vids, mat = build_matrix((r["vid"], r["vector"]) for r in rows)
                return vids, mat
            kg_vids, kg_mat = _delta_vecs("knowledge_embeddings", "object_id", list(kg_set))
            ann = si.add_items_to_ann(idx.ann_path, dim, kg_mat if len(kg_mat) else [], len(idx.ann_labels))
            ann.save_index(os.path.join(tmp_dir, "ann.bin"))
            ann_labels = list(idx.ann_labels) + list(kg_vids)
            np.save(os.path.join(tmp_dir, "ann_labels.npy"), np.asarray(ann_labels, dtype=object))
            # chunk ANN:增量 add delta chunk 向量(若原有 chunk_ann)
            manifest = dict(idx.manifest)
            if idx.chunk_ann_path and idx.chunk_ann_labels is not None:
                ch_vids, ch_mat = _delta_vecs("chunk_embeddings", "chunk_id", list(d_chunks))
                cann = si.add_items_to_ann(idx.chunk_ann_path, dim, ch_mat if len(ch_mat) else [], len(idx.chunk_ann_labels))
                cann.save_index(os.path.join(tmp_dir, "chunk_ann.bin"))
                ch_labels = list(idx.chunk_ann_labels) + list(ch_vids)
                np.save(os.path.join(tmp_dir, "chunk_ann_labels.npy"), np.asarray(ch_labels, dtype=object))
                manifest["has_chunk_ann"] = True
                manifest["n_chunk_ann"] = len(ch_labels)
            # viz:保持旧(UI-only,可 stale)——从旧目录拷 viz 文件到 tmp(若有)
            for f in ("viz.npz", "viz_adj.npz"):
                src = os.path.join(out_dir, f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(tmp_dir, f))
            # manifest:水位=当前全部 source、version bump、counts
            with self._connect() as db:
                watermark = sorted(r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall())
                total_chunks = db.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()["c"]
            manifest.update({
                "version": self._scale_index_version(notebook_id),
                "watermark_sources": watermark,
                "n_nodes": len(node_ids),
                "n_chunks": int(total_chunks),
                "n_ann": len(ann_labels),
            })
            with open(os.path.join(tmp_dir, "manifest.json"), "w") as fh:
                json.dump(manifest, fh)
            # 原子交换(锁内):out_dir → .old,tmp → out_dir,rm .old
            old_dir = out_dir + ".old"
            if os.path.exists(old_dir):
                shutil.rmtree(old_dir)
            os.rename(out_dir, old_dir)
            os.rename(tmp_dir, out_dir)
            shutil.rmtree(old_dir, ignore_errors=True)
            self._scale_idx_cache.pop(notebook_id, None)   # 失效进程缓存 → 下次 reload
            return manifest
        finally:
            with self._scale_building_lock:
                self._scale_building.discard(notebook_id)
```

- [ ] **Step 5: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py tests/test_scale_index_repo.py tests/test_chunk_retrieval.py tests/test_ppr_retrieve.py -q`
预期全 PASS(既有 build/检索不受影响;fold 端到端绿)。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(scale): fold_scale_index_delta — O(delta) incremental fold (ANN add_items + CSR splice + atomic dir swap)"
```

---

## Self-Review

- **Spec 覆盖**:实现 spec §4「fold」档 —— O(delta) 收 delta 入索引,ANN add_items 免全量重建,原子交换。
- **正确性**:fold 后 delta 归零(水位前移)、新内容进 ANN(可经 ANN 召回而非 delta 暴力)、CSR splice 保列随机(`fold_arrays` 测试锚定);检索三路径(chunk/KG/PPR)因水位前移自动只用索引核。
- **原子/并发**:写 tmp + 锁内两步 rename;交换瞬间并发 load 至多读到 None → 检索临时回退不报错;`_scale_building` 与 build 互斥;进程缓存交换后失效。
- **fail-open/幂等**:无索引→全量;无 delta→no-op;fold 抛错→tmp 丢弃、旧索引不动(finally 只清 building 标记,未交换即无损)。
- **viz** 保持旧(UI-only 可 stale),不阻塞。
- **已知**:hnswlib 多次 add_items 后 recall 缓降 → 仍需偶尔全量重建(质量维护,Phase 3 阈值③/手动);fold 不回收删除。

---

## 后续
- **Phase 3**：摄取阈值自动 surface + now/idle 二选 + 低峰调度器(驱动 fold 或全量)+ 前端四态。
