# P0-1 base 侧 chunk ANN 检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让"有持久化 scale 索引的大库"(场景A)在默认 chunk-native 问答里用 ANN 取候选,而不是每查询对全部 chunk 暴力 matmul + 全量重分词。

**Architecture:** 离线 `build_scale_index` 额外为 `chunk_embeddings` 建一个 hnsw(`chunk_ann.bin`);查询期 `_retrieve_chunks` 在 flag 开且该 notebook 有 chunk ANN 时,ANN 取 top-recall 候选 → 只拉取/打分这些候选(把 matmul 和关键词重分词都压到 O(recall));否则回退现有暴力(小库/active 保持不变,永远最新、零构建延迟)。**行为改变**:候选来自语义 ANN,纯关键词命中可能漏——故 **默认关闭 flag `chunk_ann_enabled`**,待真机 recall 对照后再默认开(沿用 `retrieval_rrf_enabled`/`relation_retrieval_enabled` 惯例)。

**Tech Stack:** hnswlib(cosine)、numpy、scipy、SQLite、pytest。Python `/opt/homebrew/Caskroom/miniconda/base/bin/python`,测试在 worktree 的 `backend/` 下跑。

---

## File Structure

- `backend/app/services/kg/scale_index.py` — `ScaleIndex` 加 chunk ANN 字段;`save_scale_index`/`load_scale_index` 读写 chunk ANN 工件(Task 1)。
- `backend/app/services/sqlite_repository.py` — `build_scale_index` 增建 chunk ANN(Task 1);`_retrieve_chunks` 加 ANN 分派 + `_retrieve_chunks_ann`(Task 2)。
- `backend/app/core/config.py` — `chunk_ann_enabled` flag(Task 2)。
- 测试:`backend/tests/test_scale_index.py`、`test_scale_index_repo.py`(Task 1)、`test_chunk_retrieval.py`(Task 2)。

---

## Task 1: 离线为 chunk 建 ANN 工件

**Files:** Modify `backend/app/services/kg/scale_index.py`、`backend/app/services/sqlite_repository.py`;Test `backend/tests/test_scale_index_repo.py`。

- [ ] **Step 1: 写失败测试 — build 出 chunk_ann 工件、load 能读回**

追加到 `tests/test_scale_index_repo.py`(复用其 `repo` fixture,该 fixture 已配 dashscope + FakeEmbedder(dim=16)):
```python
def test_build_scale_index_writes_chunk_ann(repo):
    import os
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for cid, txt in [("c1", "MOSFET current mirror"), ("c2", "bandgap reference voltage")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s1", txt, "", "[]", now))
    # 给 chunk 补向量(走正常 embed 回填路径)
    repo.embed_missing_chunks(nb.id) if hasattr(repo, "embed_missing_chunks") else None
    # 若无该方法,直接插 chunk_embeddings:
    import json, numpy as np
    with repo._write() as db:
        if not db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()["c"]:
            for cid in ("c1", "c2"):
                v = repo.embedder.embed_documents([cid])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), "2026-07-01T00:00:00"))
    repo.rebuild_unified_kg(nb.id)
    manifest = repo.build_scale_index(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    assert os.path.exists(os.path.join(d, "chunk_ann.bin"))
    assert os.path.exists(os.path.join(d, "chunk_ann_labels.npy"))
    assert manifest.get("has_chunk_ann") is True
    assert manifest.get("n_chunk_ann") == 2
    # load 能读回 chunk ann
    idx = repo._scale_index(nb.id)
    assert idx is not None
    assert list(idx.chunk_ann_labels) == ["c1", "c2"] or set(idx.chunk_ann_labels) == {"c1", "c2"}
    assert idx.chunk_ann_path.endswith("chunk_ann.bin")
```
(注:若 repo 已有现成的 chunk 向量回填 API,用之;上面兜底直插。实现者按实际 API 调整,保持"chunk 有向量"这一前提即可。)

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_build_scale_index_writes_chunk_ann -q`
预期 FAIL(尚无 chunk_ann.bin / manifest 无 has_chunk_ann / ScaleIndex 无 chunk_ann_labels)。

- [ ] **Step 3: `ScaleIndex` 加字段**

在 `scale_index.py` 的 `@dataclass ScaleIndex` 末尾加(与既有可选字段同风格):
```python
    chunk_ann_labels: list = None   # chunk_id 列表(与 chunk_ann.bin 行对齐);无则 None
    chunk_ann_path: str = None      # chunk hnsw 文件路径;无则 None
```

- [ ] **Step 4: `save_scale_index` 增建 chunk hnsw**

给 `save_scale_index` 增加两个关键字参数 `chunk_ann_vectors=None, chunk_ann_labels=None`(放在现有参数之后,默认 None 保持既有调用兼容)。在写完 KG ann.bin、写 manifest **之前**加:
```python
    if chunk_ann_labels:
        c_vecs = np.asarray(chunk_ann_vectors, dtype=np.float32)
        c_dim = int(c_vecs.shape[1]) if c_vecs.shape[0] > 0 else dim
        c_idx = hnswlib.Index(space="cosine", dim=c_dim)
        c_idx.init_index(max_elements=max(1, c_vecs.shape[0]), ef_construction=200, M=16, random_seed=42)
        if c_vecs.shape[0] > 0:
            c_idx.add_items(c_vecs, np.arange(c_vecs.shape[0]))
        c_idx.save_index(os.path.join(out_dir, "chunk_ann.bin"))
        np.save(os.path.join(out_dir, "chunk_ann_labels.npy"), np.asarray(chunk_ann_labels, dtype=object))
        manifest = {**manifest, "has_chunk_ann": True, "n_chunk_ann": len(chunk_ann_labels)}
```
(`dim`/`hnswlib` 变量在函数内已存在——参考既有 KG ann 段;若名字不同按实际改。)

- [ ] **Step 5: `load_scale_index` 读回 chunk ann**

在 `load_scale_index` 里,构造 `ScaleIndex(...)` 前加:
```python
    chunk_ann_labels = None
    chunk_ann_path = None
    if manifest.get("has_chunk_ann"):
        labpath = os.path.join(out_dir, "chunk_ann_labels.npy")
        if os.path.exists(labpath):
            chunk_ann_labels = list(np.load(labpath, allow_pickle=True))
            chunk_ann_path = os.path.join(out_dir, "chunk_ann.bin")
```
并在 `return ScaleIndex(...)` 里补 `chunk_ann_labels=chunk_ann_labels, chunk_ann_path=chunk_ann_path`。

- [ ] **Step 6: `build_scale_index` 传 chunk 向量**

在 `build_scale_index` 里(现已用 `_vector_matrix(db, notebook_id, "knowledge_embeddings", ...)` 取 KG 向量的地方附近)加取 chunk 向量:
```python
        with self._connect() as db:
            c_ids_raw, c_mat_raw = self._vector_matrix(db, notebook_id, "chunk_embeddings", "chunk_id")
        chunk_ann_labels = list(c_ids_raw) if c_ids_raw else []
        chunk_ann_vectors = (np.asarray(c_mat_raw, dtype=np.float32)
                             if chunk_ann_labels and c_mat_raw is not None else None)
```
并把它们传给 `save_scale_index(..., chunk_ann_vectors=chunk_ann_vectors, chunk_ann_labels=chunk_ann_labels)`。manifest 里可选加 `"n_chunk_ann"` 由 save 覆盖即可(save 已写)。

- [ ] **Step 7: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py tests/test_scale_index_repo.py -q`
预期全 PASS(含新测试;既有工件测试 `test_build_scale_index_writes_artifacts` 仍绿——旧 7 文件不受影响,chunk_ann 是新增可选工件)。

- [ ] **Step 8: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(scale): build+load chunk_embeddings ANN artifact in scale index"
```

---

## Task 2: 查询期 chunk ANN 分派(flag 默认关)

**Files:** Modify `backend/app/core/config.py`、`backend/app/services/sqlite_repository.py`;Test `backend/tests/test_chunk_retrieval.py`。

- [ ] **Step 1: 写失败测试 — flag 开且有 chunk ANN 时走 ANN、只打分候选**

追加到 `tests/test_chunk_retrieval.py`(若无 repo fixture 则内联一个,参考 test_scale_index_repo.py 的 fixture)。核心断言:开 flag 后 `_retrieve_chunks` 不再对全表 `_gather_chunks`,而是只取 ANN 候选;monkeypatch `_gather_chunks` 计数/或 monkeypatch `score_chunks` 收到的 chunks 数 ≤ recall:
```python
def test_retrieve_chunks_uses_ann_when_enabled(repo, monkeypatch):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for i in range(10):
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"c{i}", nb.id, "s1", f"topic {i} content", "", "[]", now))
        for i in range(10):
            v = repo.embedder.embed_documents([f"c{i}"])[0]
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (f"c{i}", nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(repo.settings, "chunk_recall", 3)

    seen = {}
    import app.services.retrieval as rmod
    real = rmod.score_chunks
    def spy(query, chunks, *a, **k):
        seen["n"] = len(chunks)
        return real(query, chunks, *a, **k)
    monkeypatch.setattr(rmod, "score_chunks", spy)
    # 也要 patch sqlite_repository 内引用点(其内部 from app.services.retrieval import score_chunks)
    import app.services.sqlite_repository as srepo
    if hasattr(srepo, "score_chunks"):
        monkeypatch.setattr(srepo, "score_chunks", spy)

    scored, ids, mat = repo._retrieve_chunks(nb.id, "topic 3 content")
    assert seen["n"] <= 3            # 只对 ANN 候选打分,非全部 10 条
    assert isinstance(scored, list)
```
(实现者:`_retrieve_chunks` 内部是 `from app.services.retrieval import score_chunks` 局部导入,则 monkeypatch 目标要对准该绑定处。若不便拦截,改断言"返回的 ids 数 ≤ recall 且是候选子集"。)

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_chunk_retrieval.py::test_retrieve_chunks_uses_ann_when_enabled -q`
预期 FAIL(当前无 flag / 走全表暴力,`seen["n"]==10`)。

- [ ] **Step 3: 加 flag**

`config.py` 检索相关 flag 区(`retrieval_rrf_enabled` 附近)加:
```python
    chunk_ann_enabled: bool = Field(False, env="CHUNK_ANN_ENABLED")  # 大库 chunk 检索走 ANN 候选(默认关,待真机 recall 对照)
```

- [ ] **Step 4: `_retrieve_chunks` 加 ANN 分派**

改 `_retrieve_chunks`(约 L6981)顶部,`query_vector` 求出后加:
```python
        if self.settings.chunk_ann_enabled and query_vector is not None:
            idx = self._scale_index(notebook_id)
            if idx is not None and getattr(idx, "chunk_ann_labels", None):
                ann = self._retrieve_chunks_ann(notebook_id, query, query_vector, idx, recall)
                if ann is not None:
                    return ann
        # ↓ 现有暴力路径保持不变
```

- [ ] **Step 5: 实现 `_retrieve_chunks_ann`**

新增方法。ANN 取候选 → 只拉候选 chunk 行 + 候选向量 → chunk_sims 用 ANN 距离 → `score_chunks` 只打分候选 → 候选矩阵供 MMR。失败(维度不符/加载异常)返回 None 让上层回退:
```python
    def _retrieve_chunks_ann(self, notebook_id, query, query_vector, idx, recall):
        """ANN 候选版 chunk 检索:只对 top-recall 候选打分,避免全表 matmul+重分词。
        返回 (scored, ids, matrix) 同 _retrieve_chunks;失败返回 None(上层回退暴力)。"""
        import numpy as np, hnswlib
        from app.services.retrieval import score_chunks
        from app.services.vector_index import build_matrix
        labels = idx.chunk_ann_labels
        if not labels:
            return None
        qarr = np.asarray(query_vector, dtype=np.float32)
        dim = int(idx.manifest.get("dim", qarr.shape[0]))
        if dim != qarr.shape[0]:
            return None
        try:
            ann = hnswlib.Index(space="cosine", dim=dim)
            ann.load_index(idx.chunk_ann_path, max_elements=len(labels))
            ann.set_ef(max(recall + 1, 64))
            k = min(recall, len(labels))
            labs, dists = ann.knn_query(qarr, k=k)
        except Exception as exc:  # noqa: BLE001 — fail-open, 回退暴力
            self._note_model_error("chunk_ann_query", self.settings.embed_model, exc)
            return None
        cand_ids = [labels[int(l)] for l in labs[0]]
        chunk_sims = {labels[int(l)]: max(0.0, 1.0 - float(d)) for l, d in zip(labs[0], dists[0])}
        if not cand_ids:
            return [], [], None
        ph = ",".join("?" for _ in cand_ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
                f"s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
                f"WHERE c.id IN ({ph})", cand_ids).fetchall()
            vrows = db.execute(
                f"SELECT chunk_id AS vid, vector FROM chunk_embeddings WHERE chunk_id IN ({ph})",
                cand_ids).fetchall()
        chunks = [{
            "chunk_id": r["id"], "source_id": r["source_id"], "source_title": r["source_title"],
            "section_path": r["section_path"], "text": r["text"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        ids, mat = build_matrix((r["vid"], r["vector"]) for r in vrows)
        return scored, ids, mat
```
注意 `_retrieve_chunks` 原返回的 chunk dict 结构(键名 `chunk_id`/`source_id`/`source_title`/`section_path`/`text`/`element_ids`)必须与 `_gather_chunks` 一致——实现者对照 `_gather_chunks` 的 return(L5735 附近)核对键名,不一致就以 `_gather_chunks` 为准。

- [ ] **Step 6: 跑新测试 + chunk/检索回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_chunk_retrieval.py tests/test_scale_index_repo.py tests/test_ppr_retrieve.py tests/test_ask_vector_matrix.py -q`
预期全 PASS。特别确认 flag **默认关**时既有 chunk 检索行为字节不变(旧用例不因本 Task 改变)。

- [ ] **Step 7: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_chunk_retrieval.py
git commit -m "feat(retrieval): ANN-candidate chunk retrieval for indexed notebooks (chunk_ann_enabled, default off)"
```

---

## Self-Review

- **Spec 覆盖**:离线工件(Task 1)+ 查询分派(Task 2)覆盖 P0-1 的"base chunk 检索走 ANN"。行为改变经默认关 flag 隔离,回退路径完整(维度不符/无索引/加载异常→暴力)。
- **向后兼容**:`save/load_scale_index` 新参数默认 None,旧调用与旧磁盘索引(无 chunk_ann.bin)不受影响、优雅退化为暴力;需 rebuild(刷新图谱)才产出 chunk_ann。
- **不变量**:`score_chunks` 融合与 [0,1] 打分不变;chunk_sims 用 `1-cosdist`∈[0,1];MMR 矩阵来自候选向量、L2 归一(build_matrix 保证)。
- **已知取舍(需真机验证)**:候选仅语义 ANN,纯关键词命中可能漏——默认关,待 recall 对照;后续可加 chunk 侧 FTS 做词法∪语义(Increment 3)。
- **类型一致**:`ScaleIndex.chunk_ann_labels/chunk_ann_path` 在 save/load/build/retrieve 一致;`_retrieve_chunks_ann` 返回 `(scored, ids, mat)` 与 `_retrieve_chunks` 同形。
```
