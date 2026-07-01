# Phase 1b-iii：KG 对象检索「索引核 ⊕ delta 暴力」 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps use checkbox。

**Goal:** 让 `_retrieve_scored`(KG 对象检索,federated/reasoning 都用它)在已索引大库上用 ANN 取候选核 ⊕ delta 对象暴力,把「全量矩阵 matmul + 全表扫 relations 算孤立 + 遍历全对象打分」压到候选集(O(recall+delta) 而非 O(N))。

**Architecture:** indexed 时:scale 索引的 `ann.bin`(knowledge_embeddings)knn 得候选核 + 其 cosine 直接当 knowledge_sims(免全量 matmul);⊕ delta 对象(水位后 source)暴力 query_sims。候选集内:`_knowledge_objects(id_filter=)` 只取候选行、element_sims 只算候选对象的证据元素、孤立集按候选查边。既有 score_knowledge/RRF/fold 不变,只喂有界候选。默认门控=有(含 stale)索引;小库/旧库无索引→现状全量(字节不变)。

**Tech Stack:** hnswlib/numpy/SQLite/pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试在 `backend/`。

## Global Constraints

- 依据 [spec §5](../specs/2026-07-01-index-lifecycle-redesign.md)(P0-0:index-backed base 成 Ask 统一入口)。
- **默认等价**:无索引(`_scale_index(nb,allow_stale=True)` 为 None)时 `_retrieve_scored` 逐字节走现有全量路径。
- **保守**:候选路径仅 indexed 时启用;fail-open(ANN 失败→退回全量)。
- **[0,1]/tau**:knowledge_sims/element_sims 仍∈[0,1](ANN `1-cosdist`、query_sims cosine)。
- 复用 `_index_delta`(Phase 1a)、`_gather_kg_graph` source 分域不需要(这里按 source_id 直接查对象)。

---

## File Structure

- `backend/app/services/sqlite_repository.py` — `_knowledge_objects`(加 id_filter)、新 `_kg_object_candidates`、`_retrieve_scored`(加 bounded 分支)。
- `backend/tests/test_retrieval.py` 或 `test_kg_search_api.py` — 测试(择既有 KG 检索测试所在文件)。

---

## Task 1: `_knowledge_objects` 加 id_filter + `_kg_object_candidates`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_knowledge_objects` L1056;新 `_kg_object_candidates`)
- Test: `backend/tests/test_retrieval.py`

**Interfaces:**
- Produces:
  - `_knowledge_objects(db, nb, type, statuses=..., id_filter=None)` — id_filter(set/list)给定时加 `AND id IN (...)`;None=现状。
  - `_kg_object_candidates(nb, query_vector, idx, recall) -> dict{object_id: sim}` — ANN 核候选(idx.ann_path knn,sim=1-cosdist)⊕ delta 对象(水位后 source 的对象,暴力 query_sims)。fail-open 返回 {} 让上层退回全量。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_retrieval.py`(若无 repo fixture 参考 `test_scale_index_repo.py` 内联;需 embedder=FakeEmbedder)。断言 id_filter 生效 + 候选含 ANN 核与 delta:
```python
def test_kg_object_candidates_core_and_delta(repo):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            v = repo.embedder.embed_texts([name])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                       "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(v), now))
    add("s1", "o1", "current mirror", 1)
    add("s1", "o2", "bandgap reference", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    add("s2", "o3", "MOSFET amplifier", 2)   # build 后新增 = delta
    # id_filter
    with repo._connect() as db:
        objs = repo._knowledge_objects(db, nb.id, "concept", id_filter={"o1"})
    assert {o["id"] for o in objs} == {"o1"}
    # 候选:ANN 核(o1/o2)⊕ delta(o3)
    idx = repo._scale_index(nb.id, allow_stale=True)
    cand = repo._kg_object_candidates(nb.id, repo._embed_query("MOSFET amplifier"), idx, recall=10)
    assert "o3" in cand                        # delta 对象在候选
    assert set(cand.keys()) & {"o1", "o2"}     # ANN 核也在候选
    assert all(0.0 <= s <= 1.0 for s in cand.values())
```

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_retrieval.py::test_kg_object_candidates_core_and_delta -q`
预期 FAIL(`id_filter` 不接受 / `_kg_object_candidates` 不存在)。

- [ ] **Step 3: `_knowledge_objects` 加 id_filter**

在 `_knowledge_objects`(L1063 附近)`query` 组装里,statuses 处理之后加:
```python
        if id_filter is not None:
            id_list = list(id_filter)
            if not id_list:
                return []
            phid = ",".join("?" for _ in id_list)
            query += f" AND id IN ({phid})"
            params.extend(id_list)
```
签名加 `id_filter: Optional[Iterable[str]] = None`。

- [ ] **Step 4: `_kg_object_candidates`**

新增(放 `_retrieve_scored` 附近):
```python
    def _kg_object_candidates(self, notebook_id, query_vector, idx, recall) -> dict:
        """ANN 核候选(idx.ann_path=knowledge_embeddings)⊕ delta 对象暴力。
        返回 {object_id: sim∈[0,1]}。fail-open 返回 {} 让上层退回全量。"""
        import numpy as np, hnswlib
        from app.services.vector_index import build_matrix, query_sims
        sims: dict = {}
        labels = getattr(idx, "ann_labels", None)
        if labels and query_vector is not None:
            qarr = np.asarray(query_vector, dtype=np.float32)
            dim = int(idx.manifest.get("dim", qarr.shape[0]))
            if dim == qarr.shape[0]:
                try:
                    ann = hnswlib.Index(space="cosine", dim=dim)
                    ann.load_index(idx.ann_path, max_elements=len(labels))
                    ann.set_ef(max(recall + 1, 64))
                    k = min(recall, len(labels))
                    labs, dists = ann.knn_query(qarr, k=k)
                    for l, d in zip(labs[0], dists[0]):
                        sims[labels[int(l)]] = max(0.0, 1.0 - float(d))
                except Exception as exc:  # noqa: BLE001 — fail-open
                    self._note_model_error("kg_obj_ann", self.settings.embed_model, exc)
                    return {}
        # ⊕ delta 对象(水位后 source)暴力
        try:
            delta = self._index_delta(notebook_id)
            if delta["delta_sources"] and query_vector is not None:
                ph_s = ",".join("?" for _ in delta["delta_sources"])
                with self._connect() as db:
                    drows = db.execute(
                        f"SELECT object_id AS vid, vector FROM knowledge_embeddings "
                        f"WHERE notebook_id=? AND object_id IN "
                        f"(SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id IN ({ph_s}))",
                        (notebook_id, notebook_id, *delta["delta_sources"])).fetchall()
                d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                for oid, s in (query_sims(query_vector, d_ids, d_mat) if d_ids else {}).items():
                    sims[oid] = s
        except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮
            self._note_model_error("kg_obj_delta", self.settings.embed_model, exc)
        return sims
```

- [ ] **Step 5: 跑测试确认通过 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_retrieval.py tests/test_scale_index_repo.py -q`
预期全 PASS(id_filter 默认 None 等价 → 既有 `_knowledge_objects` 调用不变)。

- [ ] **Step 6: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/services/sqlite_repository.py backend/tests/test_retrieval.py
git commit -m "feat(scale): _knowledge_objects id_filter + _kg_object_candidates (ANN core ⊕ delta)"
```

---

## Task 2: `_retrieve_scored` 有界候选分支

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_retrieve_scored` L7577）
- Test: `backend/tests/test_retrieval.py`

**Interfaces:**
- Consumes: `_kg_object_candidates`、`_knowledge_objects(id_filter=)`、`_index_delta`。
- Produces: `_retrieve_scored` 在 indexed 时只对候选打分(≤recall+delta),否则全量(现状)。

- [ ] **Step 1: 写失败测试 —— indexed 时只打分候选、delta 对象可召回**

```python
def test_retrieve_scored_bounded_when_indexed(repo, monkeypatch):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            v = repo.embedder.embed_texts([name])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                       "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(v), now))
    for i in range(6):
        add("s1", f"o{i}", f"concept {i}", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    add("s2", "odelta", "delta concept special", 2)
    monkeypatch.setattr(repo.settings, "chunk_recall", 3)
    out = repo._retrieve_scored(nb.id, "delta concept special")
    ids = {o.object_id for o in out}
    assert "odelta" in ids                # delta 对象被召回
    assert len(ids) <= 3 + 1              # 有界:≤ recall(3)核 + delta(1),远小于 7 全量
```

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_retrieval.py::test_retrieve_scored_bounded_when_indexed -q`
预期 FAIL(当前全量,`len(ids)`=7)。

- [ ] **Step 3: `_retrieve_scored` 加 bounded 分支**

在 `_retrieve_scored` 开头(`type_list` 之后)加候选计算;有候选时用 id_filter 取对象、knowledge_sims 用候选 sim、element_sims 只算候选证据元素、isolated 按候选查边:
```python
        type_list = [t for t in (list(types) if types else list(_KG_TYPES)) if t in _KG_TYPES]
        query_vector = self._embed_query(query)
        cand_sims = None
        if query_vector is not None:
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is not None and getattr(idx, "ann_labels", None):
                cand_sims = self._kg_object_candidates(notebook_id, query_vector, idx, self.settings.chunk_recall)
                if not cand_sims:
                    cand_sims = None   # fail-open → 全量
        with self._connect() as db:
            id_filter = set(cand_sims.keys()) if cand_sims is not None else None
            kg_objs = {t: self._knowledge_objects(db, notebook_id, t, id_filter=id_filter) for t in type_list}
            all_kg_objs = [o for objs in kg_objs.values() for o in objs]
            token_sets = self._keyword_token_sets(db, notebook_id, all_kg_objs)
            candidate_ids = {o["id"] for o in all_kg_objs}
            if cand_sims is not None:
                # 孤立集:仅按候选对象查边(有界),避免全表扫
                if candidate_ids:
                    phc = ",".join("?" for _ in candidate_ids)
                    rel_rows = db.execute(
                        f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND (source_object_id IN ({phc}) OR target_object_id IN ({phc}))",
                        (notebook_id, *candidate_ids, *candidate_ids)).fetchall()
                else:
                    rel_rows = []
                # element_sims:仅候选对象的证据元素
                elem_id_set = {ev.element_id for o in all_kg_objs for ev in o.get("evidence", []) if getattr(ev, "element_id", None)}
                if elem_id_set:
                    phe = ",".join("?" for _ in elem_id_set)
                    erows = db.execute(
                        f"SELECT element_id AS vid, vector FROM element_embeddings "
                        f"WHERE notebook_id=? AND element_id IN ({phe})",
                        (notebook_id, *elem_id_set)).fetchall()
                else:
                    erows = []
            else:
                rel_rows = db.execute(
                    "SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id = ?",
                    (notebook_id,)).fetchall()
            connected_ids: set = set()
            for r in rel_rows:
                connected_ids.add(r["source_object_id"]); connected_ids.add(r["target_object_id"])
            isolated_ids = candidate_ids - connected_ids
        from app.services.vector_index import query_sims, build_matrix
        if cand_sims is not None:
            knowledge_sims = cand_sims
            e_ids, e_mat = build_matrix((r["vid"], r["vector"]) for r in erows)
            element_sims = query_sims(query_vector, e_ids, e_mat) if e_ids else {}
        else:
            with self._connect() as db:
                elem_ids, elem_mat = self._vector_matrix(db, notebook_id, "element_embeddings", "element_id")
                kn_ids, kn_mat = self._vector_matrix(db, notebook_id, "knowledge_embeddings", "object_id")
            element_sims = query_sims(query_vector, elem_ids, elem_mat) if query_vector else None
            knowledge_sims = query_sims(query_vector, kn_ids, kn_mat) if query_vector else None
```
其后(penalty / RRF-or-score_knowledge / fold / return)**原样不变**——它们消费 `kg_objs`/`knowledge_sims`/`element_sims`/`isolated_ids`/`token_sets`,现在都是有界候选版。删掉原来那段被替换的全量加载(原 L7586–7608)。

- [ ] **Step 4: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_retrieval.py tests/test_scale_index_repo.py tests/test_reasoning_retrieval.py tests/test_relation_retrieval.py tests/test_global_search.py -q`
预期全 PASS(无索引库走全量分支字节不变;indexed 库走有界)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_retrieval.py
git commit -m "feat(retrieval): bounded KG-object retrieval on indexed notebooks (ANN core ⊕ delta; scoped isolated/element)"
```

---

## Self-Review

- **Spec 覆盖**:实现 spec §5 KG 对象路径 ⊕ + P0-0(indexed base 不走全量)。federated_retrieve 自动受益(它调 `_retrieve_scored`)。
- **默认等价**:无索引→ `cand_sims=None` → 完全走原全量路径(含原 `_vector_matrix`/全表 isolated 扫描)字节不变。
- **有界**:indexed 时 knowledge_sims=ANN 核 sim(免全量 matmul)、element_sims/isolated/token 全按候选。
- **fail-open**:ANN/delta 异常 → cand_sims 空 → 退回全量。
- **[0,1]**:ANN `1-cosdist`、query_sims cosine 均∈[0,1];score_knowledge/RRF/fold 逻辑未改。
- **已知**:候选=语义 ANN,纯关键词命中缺口(与 chunk 侧同),后续 KG-FTS 补;delta 无上界(Phase 2 fold)。

---

## 后续
- **Phase 2**：增量 fold（chunk/KG ANN add_items + 原子替换 + CSR/cluster）。
- **Phase 3**：摄取阈值自动 surface + now/idle 二选 + 低峰调度器 + 前端四态。
