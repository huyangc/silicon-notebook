# PR4:chunk 侧 FTS(词法 ∪ 语义)Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**Goal:** 补 chunk ANN 默认开后**纯关键词命中(字面匹配但语义远)可能漏召**的缺口——给 chunks 建 FTS5 trigram 索引,已索引库的 chunk 检索里把**词法候选**并进**语义(ANN)候选**,和 `kg_search` 的词法∪语义同构。

**Tech Stack:** SQLite FTS5(trigram)、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试在 worktree `backend/`。

## Global Constraints
- 仿既有 `kg_objects_fts`(schema L748、`backfill_kg_fts` L1752、写入维护 L3508)。
- **加性 schema**:新虚表 `chunks_fts`,不改现有表;`CREATE VIRTUAL TABLE IF NOT EXISTS` 幂等,老库自动获得。
- **FTS 陈旧 benign**:chunks_fts 命中 id 在真 chunks 表 JOIN 时过滤(删除的 chunk 命中→拉不到行→无害,同 P0-4 孤儿处理);漏(未 backfill)= 词法召回暂缺,不影响正确性。
- 仅在**有索引**(chunk ANN 路径)时并入词法;未索引小库走暴力,本就 keyword+semantic 全量打分,无需 FTS。

---

## Task 1: `chunks_fts` schema + 维护 + backfill + search helper

**Files:** Modify `sqlite_repository.py`(schema/insert/backfill)、`kg/search.py`(chunk_fts_search);Test `test_chunk_retrieval.py`。

- [ ] **Step 1: 写测试**
```python
def test_chunk_fts_backfill_and_search(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="b"))
    with repo._write() as db:
        now="2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",("s1",nb.id,"t","md","ready",now,now))
        for cid,txt in [("c1","XZQW9000 special widget spec"),("c2","unrelated bandgap text")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",(cid,nb.id,"s1",txt,"","[]",now))
    n = repo.backfill_chunk_fts(nb.id)
    assert n == 2
    from app.services.kg.search import chunk_fts_search
    with repo._connect() as db:
        hits = chunk_fts_search(db, nb.id, "XZQW9000", k=10)
    assert "c1" in {h["chunk_id"] for h in hits}   # 罕见词法词命中
    assert "c2" not in {h["chunk_id"] for h in hits}
```

- [ ] **Step 2: 跑测试确认失败**(no `chunks_fts` / `backfill_chunk_fts` / `chunk_fts_search`)。

- [ ] **Step 3: schema**(`sqlite_repository.py` L748 `kg_objects_fts` 建表附近):
```python
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                    USING fts5(chunk_id UNINDEXED, notebook_id UNINDEXED, text,
                               tokenize='trigram');
```

- [ ] **Step 4: 写入维护**——chunk INSERT 处(L2908)之后补一条 `INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)`(同事务)。

- [ ] **Step 5: `backfill_chunk_fts(nb)`**(仿 `backfill_kg_fts` L1752):
```python
    def backfill_chunk_fts(self, notebook_id: str) -> int:
        """从 chunks 重建 chunks_fts(DELETE+re-INSERT)。返回写入行数。"""
        with self._write() as db:
            db.execute("DELETE FROM chunks_fts WHERE notebook_id=?", (notebook_id,))
            rows = db.execute("SELECT id, text FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchall()
            for r in rows:
                db.execute("INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                           (r["id"], notebook_id, r["text"] or ""))
        return len(rows)
```

- [ ] **Step 6: `chunk_fts_search` helper**(`kg/search.py`,仿 `fts_search`):
```python
def chunk_fts_search(db, notebook_id: str, q: str, k: int = 30):
    """FTS5 MATCH(chunks_fts, trigram),notebook 维度过滤。返回
    [{chunk_id, score, match:'lexical'}]。q 空→[]。"""
    needle = (q or "").strip()
    if not needle:
        return []
    rows = db.execute(
        "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts "
        "WHERE notebook_id=? AND chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (notebook_id, '"' + needle.replace('"', '""') + '"', k)).fetchall()
    return [{"chunk_id": r["chunk_id"], "score": -float(r["rank"]), "match": "lexical"} for r in rows]
```

- [ ] **Step 7: 跑测试 + 回归**——`test_chunk_retrieval.py test_kg_search.py test_notebook_share*.py(拷贝路径若涉及 fts)` 全绿。**注意**:notebook copy/share 若重建派生 FTS(见 L1535 backfill_kg_fts 调用点),同处补 `backfill_chunk_fts`(可选,本 Task 或 Task 2 收尾)。
- [ ] **Step 8: 提交**——`feat(retrieval): chunks_fts FTS5 index + backfill + chunk_fts_search`。

---

## Task 2: chunk ANN 路径并入词法候选(词法 ∪ 语义)

**Files:** Modify `sqlite_repository.py`(`_retrieve_chunks_ann`);Test `test_chunk_retrieval.py`。

- [ ] **Step 1: 写测试 —— 纯词法命中的 chunk 经 FTS 被召回(ANN 语义漏它)**
```python
def test_chunk_ann_unions_lexical(repo, monkeypatch):
    # 建索引;造一个语义远但含罕见词法词的 chunk,确认 ANN⊕delta⊕FTS 能召回它
    ...(建 nb + chunks + embeddings + build_scale_index + backfill_chunk_fts)
    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    idx = repo._scale_index(nb.id, allow_stale=True)
    out = repo._retrieve_chunks_ann(nb.id, "XZQW9000", repo._embed_query("XZQW9000"), idx, recall=5)
    assert out and "c_lex" in {c.chunk_id for c in out[0]}   # 词法命中被并入
```

- [ ] **Step 2: 跑测试确认失败**(当前 `_retrieve_chunks_ann` 只有 ANN 语义候选 + delta,无词法)。

- [ ] **Step 3: `_retrieve_chunks_ann` 并入词法**——在 ANN 核候选 + delta 暴力之后、拉候选行之前,加 FTS 词法候选并进 `cand_ids`/`chunk_sims`:
```python
        # ∪ 词法:FTS5 命中补召回(ANN 是语义候选,纯关键词命中可能漏)
        try:
            from app.services.kg.search import chunk_fts_search
            with self._connect() as db:
                lex = chunk_fts_search(db, notebook_id, query, k=recall)
            for h in lex:
                cid = h["chunk_id"]
                if cid not in chunk_sims:
                    cand_ids.append(cid)
                    chunk_sims[cid] = 0.0   # 词法命中无语义分;score_chunks 的 keyword 分兜底
        except Exception as exc:  # noqa: BLE001 — 词法失败不拖垮检索
            self._note_model_error("chunk_fts", self.settings.embed_model, exc)
```
（`chunk_sims[cid]=0.0` 让语义分为 0,但 `score_chunks` 里 keyword_score 会给词法命中打分——最终融合分正确。若这些 cid 的向量存在,后续拉候选向量时会一并取到,MMR 矩阵覆盖。）

- [ ] **Step 4: 跑测试 + 回归**——`test_chunk_retrieval.py test_ask_vector_matrix.py test_ppr_retrieve.py` 全绿;flag 关时 `_retrieve_chunks` 字节不变(FTS 只在 ANN 路径内)。
- [ ] **Step 5: 提交**——`feat(retrieval): chunk ANN retrieval unions lexical FTS candidates (词法∪语义)`。

---

## Self-Review
- **补漏召**:纯关键词命中经 FTS 进候选池,`score_chunks` 的 keyword 分给它排序——补上 ANN 语义候选的盲区。
- **benign 陈旧**:FTS 命中在真 chunks JOIN 时过滤;漏 = 词法召回暂缺(backfill 后补),不影响正确性。
- **保守**:仅 chunk ANN 路径内并入(未索引小库不受影响,本就全量 keyword+semantic)。
- **maintain**:chunk 插入即写 chunks_fts;copy/backfill 路径补 `backfill_chunk_fts`。
