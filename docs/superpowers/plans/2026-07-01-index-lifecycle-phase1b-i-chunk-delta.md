# Phase 1b-i：chunk 检索「索引核 ⊕ delta 暴力」 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`) 语法。

**Goal:** 让已索引大库的 chunk ANN 检索也能召回 **build 之后新上传的 chunk**（当前 `_retrieve_chunks_ann` 只搜 `chunk_ann.bin` 里的存量，新上传搜不到）。

**Architecture:** 在 `_retrieve_chunks_ann` 里，ANN 取存量核候选后，再用 Phase 1a 的 `_index_delta` 找出「水位后新增 source 的 chunk」，对这批 delta 向量做**暴力 cosine**，把 delta 候选合并进候选池一起 `score_chunks`。delta 小（新上传），暴力便宜。门控沿用 `chunk_ann_enabled`（默认关，保守可回退）。

**Tech Stack:** hnswlib、numpy、SQLite、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`；测试在 worktree `backend/` 下。

## Global Constraints

- 设计依据 [spec §5](../specs/2026-07-01-index-lifecycle-redesign.md)。
- **正确性不变量**：delta 必被召回 —— build 后新上传的 chunk 必须能被 ANN 路径查到（「随传随查」）。
- **保守门控**：仅在 `chunk_ann_enabled` 开且有 chunk ANN 时走此路径；flag 关时 `_retrieve_chunks` 行为字节不变。
- **fail-open**：delta 分支任何异常不得让检索失败（退回「只有核候选」而非抛错）。
- 复用 `_index_delta(nb)`（Phase 1a，返回 `{delta_sources, delta_chunks, indexed}`）。
- [0,1] 不变量：delta 的 `query_sims` cosine ∈[0,1]，与核 ANN 的 `1-cosdist` 同尺度。

---

## File Structure

- `backend/app/services/sqlite_repository.py` — 扩展 `_retrieve_chunks_ann`（约 L7742）。
- `backend/tests/test_chunk_retrieval.py` — 测试。

---

## Task 1: `_retrieve_chunks_ann` 合并 delta 候选

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_retrieve_chunks_ann`，约 L7742–7784）
- Test: `backend/tests/test_chunk_retrieval.py`

**Interfaces:**
- Consumes: `_index_delta(nb) -> {"delta_sources": List[str], "delta_chunks": int, "indexed": bool}`（Phase 1a，已在基线）。
- Produces: `_retrieve_chunks_ann` 返回 `(scored, ids, matrix)` 形状不变；行为新增「delta chunk 也被召回」。

- [ ] **Step 1: 写失败测试 —— build 后新上传的 chunk 能被 ANN 路径召回**

追加到 `tests/test_chunk_retrieval.py`（复用其 `repo` fixture / `_seed_chunks`；若无则参考 `test_scale_index_repo.py` 的 fixture 内联）：
```python
def test_retrieve_chunks_ann_includes_post_build_delta(repo, monkeypatch):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add_source(sid, pairs, day):  # pairs: [(chunk_id, text)]
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            for cid, txt in pairs:
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, txt, "", "[]", now))
                v = repo.embedder.embed_texts([txt])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    # 建索引时的存量
    add_source("s1", [("c1", "alpha topic"), ("c2", "beta topic")], 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    # build 之后新上传一个 source(delta)——它不在 chunk_ann.bin 里
    add_source("s2", [("c3", "gamma delta topic")], 2)
    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)

    idx = repo._scale_index(nb.id)
    assert idx is not None and getattr(idx, "chunk_ann_labels", None)
    assert "c3" not in set(idx.chunk_ann_labels)  # 前提:c3 确实不在存量 ANN
    out = repo._retrieve_chunks_ann(nb.id, "gamma delta topic", repo._embed_query("gamma delta topic"), idx, recall=10)
    assert out is not None
    scored, ids, mat = out
    assert "c3" in {c.chunk_id for c in scored}   # ⊕ delta:新上传的 c3 被召回
```

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_chunk_retrieval.py::test_retrieve_chunks_ann_includes_post_build_delta -q`
预期 FAIL（当前 `_retrieve_chunks_ann` 只搜存量 ANN，`c3` 不在结果里）。

- [ ] **Step 3: 扩展实现**

改 `_retrieve_chunks_ann`。在 ANN 得到 `chunk_sims`/`cand_ids` 之后、拉候选行之前，插入 delta 暴力合并。改写为（保留原 ANN 段与末尾拉行/score 段，仅新增中间 delta 段并把 `cand_ids` 改成随 delta 增长）：
```python
    def _retrieve_chunks_ann(self, notebook_id, query, query_vector, idx, recall):
        """ANN 存量核 ⊕ delta 暴力。返回 (scored, ids, matrix) 同 _retrieve_chunks;
        失败(核 ANN 加载/维度)返回 None 让上层回退暴力。"""
        import numpy as np, hnswlib
        from app.services.retrieval import score_chunks
        from app.services.vector_index import build_matrix, query_sims
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
        chunk_sims = {labels[int(l)]: max(0.0, 1.0 - float(d)) for l, d in zip(labs[0], dists[0])}
        cand_ids = list(chunk_sims.keys())

        # ⊕ delta:水位后新增 source 的 chunk 不在存量 ANN → 暴力补召回(delta 小)。
        try:
            delta = self._index_delta(notebook_id)
            if delta["delta_sources"]:
                ph_s = ",".join("?" for _ in delta["delta_sources"])
                with self._connect() as db:
                    drows = db.execute(
                        f"SELECT chunk_id AS vid, vector FROM chunk_embeddings "
                        f"WHERE notebook_id=? AND chunk_id IN "
                        f"(SELECT id FROM chunks WHERE notebook_id=? AND source_id IN ({ph_s}))",
                        (notebook_id, notebook_id, *delta["delta_sources"])).fetchall()
                d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                d_sims = query_sims(query_vector, d_ids, d_mat) if d_ids else {}
                for cid, s in d_sims.items():
                    if cid not in chunk_sims:
                        cand_ids.append(cid)
                    chunk_sims[cid] = s
        except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮检索,退回仅核候选
            self._note_model_error("chunk_ann_delta", self.settings.embed_model, exc)

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
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        ids, mat = build_matrix((r["vid"], r["vector"]) for r in vrows)
        return scored, ids, mat
```

- [ ] **Step 4: 跑测试确认通过**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_chunk_retrieval.py::test_retrieve_chunks_ann_includes_post_build_delta -q`
预期 PASS。

- [ ] **Step 5: 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_chunk_retrieval.py tests/test_scale_index_repo.py tests/test_ppr_retrieve.py tests/test_ask_vector_matrix.py -q`
预期全 PASS（含 PR#130 的 `test_retrieve_chunks_uses_ann_when_enabled` 仍绿——它断言候选数 ≤recall；本改动在无 delta 时行为不变，有 delta 时候选 = 核∪delta，若该测试的 fixture 全部 chunk 都在 build 时纳入则无 delta、断言不变）。若该既有测试因 delta 合并而候选数变化，检查其 fixture 是否有 build 后新增 chunk；正常 seed 都在 build 前，delta=0，不受影响。

- [ ] **Step 6: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/services/sqlite_repository.py backend/tests/test_chunk_retrieval.py
git commit -m "feat(retrieval): chunk ANN retrieval merges post-watermark delta (index core ⊕ brute delta)"
```

---

## Self-Review

- **Spec 覆盖**：实现 spec §5 的 chunk 路径「索引核 ⊕ 暴力 delta」。KG 对象/federated ⊕（1b-iii）与 scale_ppr P0-00（1b-ii）为后续独立计划。
- **不变量**：build 后新上传必被召回（测试锚定 `c3`）；delta cosine 与核 `1-cosdist` 同∈[0,1];fail-open 两层(核 ANN 失败→None 回退暴力;delta 失败→仅核候选)。
- **保守**：仅 `chunk_ann_enabled` 开 + 有索引时生效;flag 关字节不变。
- **类型一致**：`_index_delta` 返回键与消费一致;返回 `(scored, ids, mat)` 同形;chunk dict 键与既有一致。
- **有界**：候选 = 核(recall) + delta(post-watermark 新上传,小);delta 太大是「stale 该重建」信号(Phase 3 surface),不在本 Task 兜。

---

## 后续（不在本计划内）

- **Phase 1b-ii**：`scale_ppr` 修 P0-00（self index 核 ⊕ self-delta splice），牵动 `_scale_combined_graph` 缓存 builder + delta 域 KG gather，单独设计。
- **Phase 1b-iii**：`_retrieve_scored`/`federated_retrieve` KG 对象 ⊕ + 孤立点集合预算。
