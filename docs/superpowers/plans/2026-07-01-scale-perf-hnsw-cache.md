# PR1:检索性能 — hnsw handle 缓存(P0-4)+ 版本探针 O(1)(P1-8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps use checkbox。

**Goal:** 砍掉已索引大库查询里 mix_rerank 的检索开销:①**P0-4** 不再每查询从磁盘重载 base 的 177MB `ann.bin`/30MB `chunk_ann.bin`——在进程缓存的 ScaleIndex 上惰性缓存 hnswlib handle;②**P1-8** 把 `_scale_index_version` 从 5 个 COUNT/MAX 换成 O(1) 读 `kg_mutation_seq`(**前提:审计确认该计数器覆盖全部检索相关写入**,否则保留聚合)。

**Architecture:** ScaleIndex 加两个非持久 handle 字段;repo `_open_scale_ann(idx, kind)` 惰性 open+memoize,5 处 load_index 调用点改走它。`_scale_index_version` 若 kg_mutation_seq 覆盖完整则改 O(1)。

**Tech Stack:** hnswlib、SQLite、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试在 worktree `backend/`。

## Global Constraints
- 依据 [review P0-4/P1-8](../../kg-scale-retrieval-review.md)。
- **零正确性回归**:P0-4 handle 生命周期绑 ScaleIndex 实例(版本变→新实例→重开),不跨版本复用旧 handle;P1-8 版本键**必须**在任一检索相关写入后变化(否则服务陈旧索引=正确性 bug)。
- fail-open:open 失败返回 None,调用方回退(现有行为)。
- hnswlib `knn_query` 并发读安全;`set_ef` 每查询设(单 int,benign race)可接受。

---

## File Structure
- `backend/app/services/kg/scale_index.py` — `ScaleIndex` 加 `ann_handle`/`chunk_ann_handle` 字段(默认 None,不落盘)。
- `backend/app/services/sqlite_repository.py` — `_open_scale_ann` + 改 5 处调用点 + `_scale_index_version`(P1-8)。
- `backend/tests/test_scale_index_repo.py` / `test_ppr_retrieve.py` — 测试。

---

## Task 1: P0-4 hnsw handle 进程缓存

**Files:** Modify `scale_index.py`、`sqlite_repository.py`;Test `test_scale_index_repo.py`。

**Interfaces:**
- Produces: `_open_scale_ann(self, idx, kind: str)` — kind∈{"kg","chunk"};惰性 open hnswlib 并 memoize 到 `idx.ann_handle`/`idx.chunk_ann_handle`;返回 handle 或 None(fail-open)。

- [ ] **Step 1: 写失败测试 —— 连续多次检索只 load_index 一次**
```python
def test_scale_ann_handle_cached(repo, monkeypatch):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now="2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",("s1",nb.id,"t","md","ready",now,now))
        for oid in ("o1","o2"):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(oid,nb.id,"concept","approved","",json.dumps({"name":oid}),"[]","s1",now,now))
            v=repo.embedder.embed_texts([oid])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",(oid,nb.id,json.dumps(v),now))
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id)
    assert idx is not None
    import hnswlib
    calls={"n":0}; real=hnswlib.Index.load_index
    def spy(self,*a,**k): calls["n"]+=1; return real(self,*a,**k)
    monkeypatch.setattr(hnswlib.Index,"load_index",spy)
    h1=repo._open_scale_ann(idx,"kg"); h2=repo._open_scale_ann(idx,"kg")
    assert h1 is not None and h1 is h2      # 同一 handle 复用
    assert calls["n"]==1                    # 只 load 一次
```

- [ ] **Step 2: 跑测试确认失败** → `pytest ...::test_scale_ann_handle_cached` FAIL(no `_open_scale_ann`)。

- [ ] **Step 3: ScaleIndex 加 handle 字段**（`scale_index.py`,dataclass 末尾）:
```python
    ann_handle: object = None        # 惰性缓存的 hnswlib KG ANN handle(不落盘)
    chunk_ann_handle: object = None  # 惰性缓存的 chunk ANN handle(不落盘)
```

- [ ] **Step 4: `_open_scale_ann` 辅助**（`sqlite_repository.py`,`_scale_index` 附近）:
```python
    def _open_scale_ann(self, idx, kind: str):
        """惰性 open + memoize hnswlib handle 到 ScaleIndex 实例(进程缓存,版本变→新实例→重开)。
        kind='kg'→ann.bin/ann_labels;'chunk'→chunk_ann.bin/chunk_ann_labels。失败/无工件→None。"""
        import hnswlib
        attr = "ann_handle" if kind == "kg" else "chunk_ann_handle"
        cached = getattr(idx, attr, None)
        if cached is not None:
            return cached
        path = idx.ann_path if kind == "kg" else getattr(idx, "chunk_ann_path", None)
        labels = idx.ann_labels if kind == "kg" else getattr(idx, "chunk_ann_labels", None)
        if not path or not labels:
            return None
        dim = int(idx.manifest.get("dim", self.settings.embed_dim))
        try:
            h = hnswlib.Index(space="cosine", dim=dim)
            h.load_index(path, max_elements=len(labels))
        except Exception as exc:  # noqa: BLE001 — fail-open
            self._note_model_error(f"scale_ann_open_{kind}", self.settings.embed_model, exc)
            return None
        setattr(idx, attr, h)
        return h
```

- [ ] **Step 5: 改 5 处 load_index 调用点**

把这 5 处的 `ann = hnswlib.Index(space="cosine", dim=dim); ann.load_index(idx.ann_path/chunk_ann_path, max_elements=...)` 换成 `ann = self._open_scale_ann(idx, "kg"|"chunk"); if ann is None: continue/return None`(保留各处后续 `ann.set_ef(...)` + `ann.knn_query(...)`;删掉本地 dim 构造与 load,dim 校验仍保留——不匹配则不调 open 或 open 后 knn 维度自然报错被 except 捕获,更简洁是保留调用点原有的 `dim != qarr.shape[0]` 提前 continue):
- `_semantic_search`(~L1803):`kind="kg"`。
- `_scale_xlayer_bridge_edges`(~L7497,变量名 `_ann`/`_bidx`):`self._open_scale_ann(_bidx, "kg")`。
- `scale_ppr` 种子(~L7660):`kind="kg"`。
- `_kg_object_candidates`(~L7928):`kind="kg"`。
- `_retrieve_chunks_ann`(~L8208):`kind="chunk"`。
各处保留 `set_ef` 每查询设(handle 复用,ef 每次设无妨)。

- [ ] **Step 6: 跑测试 + 回归**

`pytest tests/test_scale_index_repo.py tests/test_ppr_retrieve.py tests/test_chunk_retrieval.py tests/test_kg_search_api.py -q` 全绿(检索结果不变,只是不重复 load)。

- [ ] **Step 7: 提交**
```bash
git add backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "perf(scale): cache hnswlib ANN handles on ScaleIndex (P0-4: no per-query 177MB reload)"
```

---

## Task 2: P1-8 版本探针 O(1)(审计后再改)

**Files:** Modify `sqlite_repository.py`(`_scale_index_version`);Test `test_scale_index_repo.py`。

**Interfaces:** `_scale_index_version(nb)` 返回值语义不变(任一检索相关写入后必变),但改为 O(1)。

- [ ] **Step 1: 审计 `kg_mutation_seq` 覆盖面(先做,决定能否改)**

grep 全部改动 5 类表(knowledge_objects/knowledge_relations/chunks/concept_clusters/knowledge_embeddings)的写入路径,确认每条都最终调用 `_mark_unified_kg_dirty`(bump kg_mutation_seq)。**特别核对**:chunk 插入(process_source/ingest)、embedding 写入/re-embed、relation 增删。把结论写进汇报。
- 若**全覆盖** → 执行 Step 2/3(改 O(1))。
- 若**有缺口** → 有两条路:(a)在缺口写入点补 `_mark_unified_kg_dirty`(使其成真正 choke point);(b)保守:该表仍保留 COUNT/MAX、其余用 seq。**择 (a) 更干净**,但若缺口点多/风险大,则本 Task 只做能安全覆盖的部分并如实汇报保留项。

- [ ] **Step 2: 写测试 —— 每类写入后版本都变**
```python
def test_scale_index_version_changes_on_each_write(repo):
    from app.models.schemas import NotebookCreate
    import json
    nb=repo.create_notebook(NotebookCreate(name="b"))
    v0=repo._scale_index_version(nb.id)
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",("o1",nb.id,"concept","approved","","{}","[]","",  "t","t"))
    repo._mark_unified_kg_dirty(nb.id)   # 若走真实写入路径应已自动 bump;此处显式确保
    v1=repo._scale_index_version(nb.id)
    assert v1!=v0
```
(实现者按审计结论调整:若走真实 store_kg/ingest API 更佳,断言不同写入类型都改变版本。)

- [ ] **Step 3: 改 `_scale_index_version` 为 O(1)(仅当审计全覆盖)**

读 `unified_kg_state` 的 `kg_mutation_seq`(单行)替代 5 个 COUNT/MAX;保留结尾的 settings 旋钮:
```python
    def _scale_index_version(self, notebook_id: str) -> list:
        with self._connect() as db:
            row = db.execute("SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?", (notebook_id,)).fetchone()
        seq = int(row["kg_mutation_seq"]) if row else 0
        return [notebook_id, seq,
                self.settings.ppr_variant_edge_weight,
                self.settings.ppr_emb_synonym_enabled,
                self.settings.ppr_emb_synonym_threshold,
                self.settings.ppr_emb_synonym_topk]
```
**注意**:改版本键格式会使**现有磁盘索引的 manifest.version 全部失配→判 stale**(一次性全部需重建/或 allow_stale 兜)。这是可接受的一次性代价(格式变),但要在汇报里点明:合并后已建索引会被判 stale,首次查询走 allow_stale 核 ⊕ delta,直到下次 rebuild/fold 写入新格式 version。若不希望一次性失配,可保留旧格式仅内部加速(不改 manifest 写入)——实现者权衡后择一并说明。

- [ ] **Step 4: 跑测试 + 回归** —— `test_scale_index_repo.py test_ppr_retrieve.py test_kg_search_api.py` 全绿。
- [ ] **Step 5: 提交** —— `perf(scale): O(1) scale-index version probe via kg_mutation_seq (P1-8)`。

---

## Self-Review
- **P0-4 无正确性风险**:handle 绑实例生命周期;fail-open;检索结果不变(测试锚定 load 次数,不改召回)。
- **P1-8 正确性优先**:先审计覆盖面,不全覆盖不盲改;版本键语义(任一写入后变)是硬约束。manifest 格式变的一次性 stale 代价需明示(且 allow_stale 已兜,不会报错)。
- **stale 路径**:allow_stale 返回的未缓存实例每查询新建 → handle 不跨查询复用(可接受,stale 瞬态;fresh-cached 大库是主场景,已覆盖)。
