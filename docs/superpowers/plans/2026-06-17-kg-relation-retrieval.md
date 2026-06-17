# KG 检索增强(关系向量化 + 双层关键词 + 检索度量)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **提交纪律:** 每个 git commit 信息末尾追加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。实现子代理「只 commit,不 push、不开 PR」(见 dev-flow 记忆)。

**Goal:** 让 KG「关系」可被 query 直接检索(关系向量索引 + 双层关键词),增强 `graph`/`reasoning` 的种子选择,并建立可证伪的 KG 检索度量(双轨 gold 集 + recall@k/MRR 扩到关系)。

**Architecture:** 纯检索路线——从已有 `knowledge_relations` 字段(edge_type + 两端实体名 + evidence)合成关系 embedding,**不动抽取 prompt**。新增 `relation_embeddings` 表镜像 `knowledge_embeddings`;`score_relations` 镜像 `score_knowledge` 守 `[0,1]`/tau;`expand_query` 多吐 high/low-level keywords;消费方先只接 `ask_graph` 种子(flag 门控,默认关,等价回退);度量扩 `retrieval_metrics`。

**Tech Stack:** Python 3 / SQLite(JSON 向量,Python 端 numpy 余弦)/ pytest / rustworkx(已有图)/ FakeEmbedder(测试,哈希确定性向量、无语义相似度——故检索测试走关键词路径)。

**Spec:** `docs/superpowers/specs/2026-06-17-kg-relation-retrieval-design.md`

---

## 文件结构(职责边界)

**修改:**
- `backend/app/services/retrieval.py` — 加纯函数 `relation_embed_text`、`RetrievedRelation` dataclass、`score_relations`(与 `score_knowledge`/`_fuse` 同尺)。
- `backend/app/services/sqlite_repository.py` — `relation_embeddings` DDL;`_embed_relations_batch` + `store_kg` 接线 + `_backfill_relation_embeddings`;`_relations_with_names`、`_retrieve_relations_scored`、`federated_retrieve_relations`;`_graph_seed_fusion` + `ask_graph` 接线。
- `backend/app/services/query_rewrite.py` — `SubQuerySpec`/`ExpandedQuery` 加 hl/ll keywords;`expand_query` 解析。
- `backend/app/services/prompts.py` — `EXPAND_SCHEMA_HINT` + `expand_query_prompt` 同步 hl/ll。
- `backend/app/core/config.py` — `relation_retrieval_enabled` 开关。
- `backend/app/eval/retrieval_metrics.py` — `run_recall` 扩关系 gold。
- `backend/app/eval/run_all.py` — recall 通道指向 `recall_gold.yaml`。

**新建:**
- `backend/app/eval/recall_gold.yaml` — 双轨 gold 集(反向出题 + 人工锚点)。
- `backend/scripts/backfill_relation_embeddings.py` — 旧库关系向量回填 CLI。
- `backend/scripts/gen_recall_gold.py` — KG 反向出题生成器 CLI(含泄漏体检)。
- 测试:`backend/tests/test_relation_embed.py`、`test_relation_retrieval.py`、`test_dual_keywords.py`、`test_graph_seed_fusion.py`、`test_recall_relations.py`、`test_gen_recall_gold.py`。

**测试 repo fixture 范式**(全程复用,见 `tests/test_two_tier_federated.py`):
```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r
```

---

## Phase 1 — 关系 embedding 基建

### Task 1: `relation_embed_text` 纯函数

**Files:**
- Modify: `backend/app/services/retrieval.py`(在 `_payload_text` 附近)
- Test: `backend/tests/test_relation_embed.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_relation_embed.py
from app.services.retrieval import relation_embed_text


def test_relation_embed_text_combines_fields():
    t = relation_embed_text("Regulated Cascode", "derived_from", "Cascode",
                            ["adds a gain stage to boost output resistance"])
    assert "Regulated Cascode" in t and "Cascode" in t
    assert "derived_from" in t
    assert "gain stage" in t


def test_relation_embed_text_truncates_evidence():
    t = relation_embed_text("A", "supports", "B", ["x" * 1000], max_evidence_chars=50)
    # evidence 截断到 50;头部 "A —supports→ B." 不计入截断额度
    assert t.count("x") <= 50


def test_relation_embed_text_handles_empty_evidence():
    t = relation_embed_text("A", "about", "B", [])
    assert t == "A —about→ B."
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_relation_embed.py -q`
Expected: FAIL(`ImportError: cannot import name 'relation_embed_text'`)

- [ ] **Step 3: 实现**

```python
# backend/app/services/retrieval.py —— 加在 _payload_text 之后
def relation_embed_text(src_name: str, edge_type: str, tgt_name: str,
                        evidence_spans: Sequence[str],
                        max_evidence_chars: int = 400) -> str:
    """关系的 embedding/关键词文本。纯检索:只用已有边字段(不依赖抽取改动)。
    格式 '<src> —<edge_type>→ <tgt>. <evidence...>';evidence 截断到上限。"""
    ev = " ".join(s.strip() for s in evidence_spans if s and s.strip())
    if len(ev) > max_evidence_chars:
        ev = ev[:max_evidence_chars]
    head = f"{src_name} —{edge_type}→ {tgt_name}."
    return f"{head} {ev}".strip()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_relation_embed.py -q`
Expected: PASS(3 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/retrieval.py backend/tests/test_relation_embed.py
git commit -m "feat(kg): relation_embed_text — 关系检索文本合成(纯检索,不碰抽取)"
```

---

### Task 2: `relation_embeddings` 表

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(schema bootstrap,`knowledge_embeddings` DDL ~:374 之后)
- Test: `backend/tests/test_relation_embed.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_relation_embed.py —— 追加(顶部加 repo fixture,见文件结构范式)
def test_relation_embeddings_table_schema(repo):
    with repo._connect() as db:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(relation_embeddings)")]
    assert cols == ["relation_id", "notebook_id", "vector", "created_at"]


def test_relation_embeddings_idempotent_reinit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    SQLiteRepository(Settings())
    SQLiteRepository(Settings())  # 第二次 init 同库不应抛错(CREATE TABLE IF NOT EXISTS)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_relation_embed.py -q`
Expected: FAIL(`PRAGMA table_info(relation_embeddings)` 返回空 → `cols == []`)

- [ ] **Step 3: 实现(在 `knowledge_embeddings` 建表 DDL 之后插入)**

```sql
                CREATE TABLE IF NOT EXISTS relation_embeddings (
                  relation_id TEXT PRIMARY KEY REFERENCES knowledge_relations(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relation_embeddings_nb ON relation_embeddings(notebook_id);
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_relation_embed.py -q`
Expected: PASS(5 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_relation_embed.py
git commit -m "feat(kg): relation_embeddings 表(镜像 knowledge_embeddings + FK 级联清理)"
```

---

### Task 3: 关系向量写入 + `store_kg` 接线 + 回填

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`store_kg` ~:2234;加 `_embed_relations_batch`、`_backfill_relation_embeddings`)
- Create: `backend/scripts/backfill_relation_embeddings.py`
- Test: `backend/tests/test_relation_embed.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_relation_embed.py —— 追加
def _seed_two_node_relation(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b",
                  "edge_type": "derived_from",
                  "evidence": [{"quoted_span": "regulated cascode adds a gain stage"}]}]
    repo.store_kg(nb.id, None, objects, relations)
    return nb


def test_store_kg_embeds_relations(repo):
    nb = _seed_two_node_relation(repo)
    with repo._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS c FROM relation_embeddings WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert n == 1  # FakeEmbedder configured → 关系被 embed


def test_backfill_relation_embeddings_fills_missing(repo):
    nb = _seed_two_node_relation(repo)
    with repo._write() as db:
        db.execute("DELETE FROM relation_embeddings WHERE notebook_id=?", (nb.id,))
    repo._backfill_relation_embeddings(nb.id)
    with repo._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS c FROM relation_embeddings WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert n == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_relation_embed.py -q`
Expected: FAIL(`test_store_kg_embeds_relations`: n == 0;`_backfill_relation_embeddings` AttributeError)

- [ ] **Step 3: 实现**

3a. 加 `_embed_relations_batch`(镜像 `_embed_objects_batch`:1693,text 预构建):

```python
    def _embed_relations_batch(self, notebook_id: str, rel_items: List[dict]) -> None:
        """并发 COMPUTE 关系向量, 一次写事务持久化到 relation_embeddings。
        rel_items: [{"_rid": str, "text": str}]。best-effort,失败跳过。"""
        if not self.settings.embedder_configured:
            return
        pending = [(it["_rid"], it["text"][:2000]) for it in rel_items if it.get("text", "").strip()]
        if not pending:
            return
        import concurrent.futures as _cf
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]

        def _embed_only(batch) -> list:
            try:
                vectors = self.embedder.embed_texts([t for _, t in batch])
            except Exception as exc:  # noqa: BLE001 — best-effort per batch
                self.event_log.logger.warning(
                    "embed kg-relations batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc)
                return []
            return [(rid, vec) for (rid, _), vec in zip(batch, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-rel") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        if not rows:
            return
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO relation_embeddings (relation_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(rid, notebook_id, json.dumps(vec), now) for rid, vec in rows])
```

3b. `store_kg` 接线 —— 给 `db_relations` 预分配 id + 记名字,改用预分配 id 写入,末尾 embed。把 `store_kg` 里关系组装/写入段替换为:

```python
        from app.services.retrieval import relation_embed_text, _payload_text
        local_to_name = {o["local_id"]: _payload_text(o["payload"])[:80] for o in objects}
        db_relations = []
        for rel in relations:
            s = local_to_id.get(rel["source_local_id"])
            t = local_to_id.get(rel["target_local_id"])
            if not s or not t:
                continue
            spans = [e.get("quoted_span", "") for e in rel.get("evidence", [])
                     if isinstance(e, dict)]
            db_relations.append({
                "_rid": f"rel-{uuid4().hex[:10]}",
                "source_object_id": s, "target_object_id": t,
                "edge_type": rel["edge_type"], "evidence": rel.get("evidence", []),
                "text": relation_embed_text(
                    local_to_name.get(rel["source_local_id"], "?"), rel["edge_type"],
                    local_to_name.get(rel["target_local_id"], "?"), spans),
            })
```

关系 INSERT 改用 `r["_rid"]`(替换原 `f"rel-{uuid4().hex[:10]}"`):

```python
        for i in range(0, len(db_relations), CHUNK):
            chunk = db_relations[i:i + CHUNK]
            with self._write() as db:
                db.executemany(
                    "INSERT INTO knowledge_relations "
                    "(id, notebook_id, source_id, source_object_id, target_object_id, "
                    "edge_type, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r["_rid"], notebook_id, source_id,
                      r["source_object_id"], r["target_object_id"], r["edge_type"],
                      json.dumps(r["evidence"], ensure_ascii=False), now) for r in chunk])
```

在 `self._embed_objects_batch(notebook_id, objects)` 之后追加一行:

```python
        self._embed_relations_batch(notebook_id, db_relations)
```

3c. 加 `_backfill_relation_embeddings`(镜像 `_backfill_knowledge_embeddings`:1875):

```python
    def _backfill_relation_embeddings(self, notebook_id: str) -> None:
        """给缺向量的关系补 relation_embeddings(幂等,只补缺失)。无 embedder 则 no-op。"""
        if not self.settings.embedder_configured:
            return
        with self._connect() as db:
            relations = self._relations_with_names(db, notebook_id)
            have = {r["relation_id"] for r in db.execute(
                "SELECT relation_id FROM relation_embeddings WHERE notebook_id=?",
                (notebook_id,)).fetchall()}
        missing = [{"_rid": r["id"], "text": r["text"]} for r in relations
                   if r["id"] not in have]
        if missing:
            self._embed_relations_batch(notebook_id, missing)
```

> 注:`_relations_with_names` 在 Task 5 实现。本任务的 `_backfill` 测试依赖它,故 **Task 5 的 `_relations_with_names` 需先落**;若按序执行,把 3c + `test_backfill_*` 挪到 Task 5 之后,或在本任务内顺带实现 `_relations_with_names`(Task 5 再补 `_retrieve_relations_scored`)。推荐:本任务顺带实现 `_relations_with_names`(代码见 Task 5 Step 3),Task 5 只加检索。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_relation_embed.py tests/test_kg_repository.py -q`
Expected: PASS(关系 embed + 回填通过;`store_kg` 既有测试不回归)

- [ ] **Step 5: 写回填 CLI**

```python
# backend/scripts/backfill_relation_embeddings.py
"""回填关系向量。用法: PYTHONPATH=backend python -m scripts.backfill_relation_embeddings <notebook_id>"""
import sys
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: backfill_relation_embeddings <notebook_id>")
        return 2
    repo = SQLiteRepository(Settings())
    repo._backfill_relation_embeddings(sys.argv[1])
    print(f"[backfill] relation embeddings done for {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_relation_embed.py backend/scripts/backfill_relation_embeddings.py
git commit -m "feat(kg): 建图/回填时 embed 关系 + 回填 CLI"
```

---

## Phase 2 — 关系打分与检索

### Task 4: `RetrievedRelation` + `score_relations`

**Files:**
- Modify: `backend/app/services/retrieval.py`
- Test: `backend/tests/test_relation_retrieval.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_relation_retrieval.py
from app.services.retrieval import score_relations, RELEVANCE_FLOOR


def _rel(rid, text):
    return {"id": rid, "source_object_id": "s", "target_object_id": "t",
            "edge_type": "derived_from", "text": text}


def test_score_relations_keyword_only_full_match_is_one():
    # 无向量、关键词全命中 → _fuse 归一化后 relevance == 1.0(与 score_knowledge 同尺)
    hits = score_relations("cascode", [_rel("r1", "Regulated Cascode —derived_from→ Cascode.")])
    assert hits and hits[0].relation_id == "r1"
    assert abs(hits[0].relevance - 1.0) < 1e-9


def test_score_relations_uses_explicit_sims_and_stays_bounded():
    # 语义路径用显式 sims(FakeEmbedder 无语义);relevance ∈ [0,1]
    hits = score_relations("zzz no keyword overlap", [_rel("r1", "alpha beta gamma")],
                           query_vector=[0.1] * 4, relation_sims={"r1": 0.9})
    assert hits and 0.0 <= hits[0].relevance <= 1.0
    assert hits[0].relevance > 0.5  # 语义 0.9 主导


def test_score_relations_drops_below_floor():
    # 关键词 0、无向量 → relevance 0 < floor → 丢弃
    hits = score_relations("totally unrelated terms", [_rel("r1", "alpha beta")])
    assert hits == []


def test_score_relations_sorted_desc():
    rels = [_rel("r1", "cascode mirror"), _rel("r2", "cascode output resistance gain")]
    hits = score_relations("cascode output resistance", rels)
    assert [h.relation_id for h in hits] == sorted(
        [h.relation_id for h in hits], key=lambda x: -dict((h.relation_id, h.relevance) for h in hits)[x])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_relation_retrieval.py -q`
Expected: FAIL(`ImportError: cannot import name 'score_relations'`)

- [ ] **Step 3: 实现(retrieval.py,`RetrievedElement` 之后加 dataclass;`score_knowledge` 之后加函数)**

```python
@dataclass
class RetrievedRelation:
    relation_id: str
    source_object_id: str
    target_object_id: str
    edge_type: str
    text: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    score: float = 0.0
    relevance: float = 0.0
    notebook_id: str = ""
    tier: str = "personal"
```

```python
def score_relations(
    query: str,
    relations: List[dict],
    query_vector: Optional[List[float]] = None,
    relation_sims: Optional[Dict[str, float]] = None,
    w_keyword: float = W_KEYWORD,
    w_semantic: float = W_SEMANTIC,
) -> List[RetrievedRelation]:
    """关系打分:关键词(关系 text)+ 可选语义(query vs 关系自有向量,来自
    relation_sims)。与 score_knowledge 同尺:max(0,cosine) 经 _fuse → relevance
    ∈[0,1],低于 RELEVANCE_FLOOR 丢弃。relation_sims 是独立关系索引(dual-index
    分离,不与节点矩阵合并)。每个 relations 项: {id, source_object_id,
    target_object_id, edge_type, text}。"""
    query_basis_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    scored: List[RetrievedRelation] = []
    for rel in relations:
        rid = rel["id"]
        text = rel.get("text", "")
        keyword = keyword_score_tokens(query_basis_tokens, set(_tokens(text)))
        semantic = 0.0
        has_vector = False
        if query_vector and relation_sims is not None:
            s = relation_sims.get(rid)
            if s is not None:
                has_vector = True
                semantic = max(semantic, s)
        relevance = _fuse(keyword, semantic, has_vector, w_keyword, w_semantic)
        if relevance < RELEVANCE_FLOOR:
            continue
        scored.append(RetrievedRelation(
            relation_id=rid,
            source_object_id=rel["source_object_id"],
            target_object_id=rel["target_object_id"],
            edge_type=rel["edge_type"],
            text=text,
            evidence=rel.get("evidence", []),
            score=relevance,
            relevance=relevance,
        ))
    scored.sort(key=lambda it: it.score, reverse=True)
    return scored
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_relation_retrieval.py -q`
Expected: PASS(4 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/retrieval.py backend/tests/test_relation_retrieval.py
git commit -m "feat(kg): score_relations + RetrievedRelation(守 [0,1]/tau,dual-index 分离)"
```

---

### Task 5: `_relations_with_names` + `_retrieve_relations_scored`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_relation_retrieval.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_relation_retrieval.py —— 追加(加 repo fixture + NotebookCreate import)
def test_retrieve_relations_scored_keyword_path(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "Current Mirror"}, "evidence": []},
    ]
    relations = [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "derived_from", "evidence": []},
        {"source_local_id": "c", "target_local_id": "b", "edge_type": "about", "evidence": []},
    ]
    repo.store_kg(nb.id, None, objects, relations)
    hits = repo._retrieve_relations_scored(nb.id, "regulated cascode")
    assert hits, "应至少命中一条关系"
    # 文本含 'Regulated Cascode' 的边排第一
    assert "Regulated Cascode" in hits[0].text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_relation_retrieval.py::test_retrieve_relations_scored_keyword_path -q`
Expected: FAIL(`AttributeError: _retrieve_relations_scored`)

- [ ] **Step 3: 实现**

`_relations_with_names`(若 Task 3 已顺带实现则跳过):

```python
    def _relations_with_names(self, db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        """关系 + 两端实体名 + evidence,预构建 keyword/embed 文本。JOIN 丢弃悬空边
        (端点不在 knowledge_objects),与图节点过滤一致。"""
        from app.services.retrieval import relation_embed_text, _payload_text
        rows = db.execute(
            "SELECT r.id AS id, r.source_object_id AS s, r.target_object_id AS t, "
            "r.edge_type AS et, r.evidence AS ev, so.payload AS sp, tp.payload AS tpl "
            "FROM knowledge_relations r "
            "JOIN knowledge_objects so ON so.id = r.source_object_id "
            "JOIN knowledge_objects tp ON tp.id = r.target_object_id "
            "WHERE r.notebook_id = ?", (notebook_id,)).fetchall()
        out = []
        for r in rows:
            spans = [e.get("quoted_span", "") for e in json.loads(r["ev"] or "[]")
                     if isinstance(e, dict)]
            src_name = _payload_text(json.loads(r["sp"] or "{}"))[:80]
            tgt_name = _payload_text(json.loads(r["tpl"] or "{}"))[:80]
            out.append({
                "id": r["id"], "source_object_id": r["s"], "target_object_id": r["t"],
                "edge_type": r["et"],
                "text": relation_embed_text(src_name, r["et"], tgt_name, spans),
            })
        return out
```

`_retrieve_relations_scored`:

```python
    def _retrieve_relations_scored(self, notebook_id: str, query: str) -> List["RetrievedRelation"]:
        """对 notebook 关系按 query 打分(关键词 + 关系索引语义)。镜像 _retrieve_scored;
        关系矩阵是独立索引(dual-index 分离)。"""
        from app.services.retrieval import score_relations
        from app.services.vector_index import query_sims
        with self._connect() as db:
            relations = self._relations_with_names(db, notebook_id)
            query_vector = self._embed_query(query)
            rel_ids, rel_mat = self._vector_matrix(
                db, notebook_id, "relation_embeddings", "relation_id")
        relation_sims = query_sims(query_vector, rel_ids, rel_mat) if query_vector else None
        return score_relations(query, relations, query_vector=query_vector,
                               relation_sims=relation_sims)
```

顶部 import 区确保 `from app.services.retrieval import RetrievedRelation`(若类型注解需要)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_relation_retrieval.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_relation_retrieval.py
git commit -m "feat(kg): _retrieve_relations_scored(关系索引检索,镜像 _retrieve_scored)"
```

---

### Task 6: `federated_retrieve_relations`(跨 base∪active)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`federated_retrieve`:4120 之后)
- Test: `backend/tests/test_relation_retrieval.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_relation_retrieval.py —— 追加
def test_federated_retrieve_relations_spans_base(repo):
    base = repo.create_notebook(NotebookCreate(name="textbook"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Bandgap Reference"}, "evidence": []},
         {"local_id": "b", "object_type": "concept", "payload": {"name": "PTAT Current"}, "evidence": []}],
        [{"source_local_id": "a", "target_local_id": "b", "edge_type": "depends_on", "evidence": []}])
    personal = repo.create_notebook(NotebookCreate(name="my notes"))
    hits = repo.federated_retrieve_relations(personal.id, "bandgap reference ptat")
    assert hits, "个人本应能联邦检索到 base 库的关系"
    assert hits[0].tier == "base"
    assert hits[0].notebook_id == base.id
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_relation_retrieval.py::test_federated_retrieve_relations_spans_base -q`
Expected: FAIL(`AttributeError: federated_retrieve_relations`)

- [ ] **Step 3: 实现(镜像 `federated_retrieve`:4120)**

```python
    def federated_retrieve_relations(self, active_notebook_id: str,
                                     query: str) -> List["RetrievedRelation"]:
        """跨 {base notebook(s)} ∪ {active} 检索关系,逐本 .notebook_id/.tier 标注。
        每本走 _retrieve_relations_scored(同尺),合并按 score 降序。"""
        notebook_ids: List[str] = [active_notebook_id]
        with self._connect() as db:
            base_rows = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,)).fetchall()
            notebook_ids.extend(r["id"] for r in base_rows)
            tier_map = {}
            for nid in notebook_ids:
                row = db.execute("SELECT tier FROM notebooks WHERE id=?", (nid,)).fetchone()
                tier_map[nid] = (row["tier"] if row else "personal")
        all_hits: List["RetrievedRelation"] = []
        for nid in notebook_ids:
            hits = self._retrieve_relations_scored(nid, query)
            tier = tier_map.get(nid, "personal")
            for h in hits:
                h.notebook_id = nid
                h.tier = tier
            all_hits.extend(hits)
        all_hits.sort(key=lambda it: it.score, reverse=True)
        return all_hits
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_relation_retrieval.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_relation_retrieval.py
git commit -m "feat(kg): federated_retrieve_relations(base∪active,tier 标注)"
```

---

## Phase 3 — 双层关键词

### Task 7: `expand_query` 多吐 high/low-level keywords

**Files:**
- Modify: `backend/app/services/query_rewrite.py`、`backend/app/services/prompts.py`
- Test: `backend/tests/test_dual_keywords.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_dual_keywords.py
from app.services.query_rewrite import expand_query, ExpandedQuery


class _FakeClient:
    configured = True
    def __init__(self, raw): self._raw = raw
    def chat_json(self, messages, schema_hint, **kw): return self._raw


def test_expand_query_parses_dual_keywords():
    raw = ('{"query_en":"how does cascode boost output resistance",'
           '"high_level_keywords":["output resistance","gain boosting"],'
           '"low_level_keywords":["cascode","r_ds"],'
           '"sub_queries":[{"query":"cascode output resistance"}]}')
    exp = expand_query(_FakeClient(raw), "cascode 怎么提高输出电阻")
    assert exp.high_level_keywords == ["output resistance", "gain boosting"]
    assert exp.low_level_keywords == ["cascode", "r_ds"]
    assert exp.sub_queries[0].query  # 子查询仍在


def test_expand_query_missing_keywords_defaults_empty():
    raw = '{"query_en":"x","sub_queries":[{"query":"x"}]}'
    exp = expand_query(_FakeClient(raw), "x")
    assert exp.high_level_keywords == [] and exp.low_level_keywords == []


def test_expand_query_unconfigured_fallback_has_empty_keywords():
    class Off: configured = False
    exp = expand_query(Off(), "anything")
    assert isinstance(exp, ExpandedQuery)
    assert exp.high_level_keywords == [] and exp.low_level_keywords == []
    assert exp.sub_queries  # 始终 >=1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_dual_keywords.py -q`
Expected: FAIL(`AttributeError: 'ExpandedQuery' object has no attribute 'high_level_keywords'`)

- [ ] **Step 3: 实现**

3a. `query_rewrite.py` — `ExpandedQuery` 加字段 + 解析 + fallback:

```python
@dataclass
class ExpandedQuery:
    query_en: str
    sub_queries: List[SubQuerySpec]
    high_level_keywords: List[str] = field(default_factory=list)
    low_level_keywords: List[str] = field(default_factory=list)
```

在 `expand_query` 内,把 `fallback` 定义保持不变(默认空列表已由 dataclass 给出)。在成功分支构造返回值前,加:

```python
        def _kw_list(v):
            if isinstance(v, str):
                return [x.strip() for x in re.split(r"[,;\n]", v) if x.strip()]
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []
        hl = _kw_list(data.get("high_level_keywords"))
        ll = _kw_list(data.get("low_level_keywords"))
        query_en = str(data.get("query_en", "")).strip() or question
        return ExpandedQuery(query_en=query_en, sub_queries=out,
                             high_level_keywords=hl, low_level_keywords=ll)
```

(原 `return ExpandedQuery(query_en=query_en, sub_queries=out)` 替换为上述。)

3b. `prompts.py` — 更新 schema 与 prompt:

```python
EXPAND_SCHEMA_HINT = ('{"query_en":"","high_level_keywords":[],"low_level_keywords":[],'
                      '"sub_queries":[{"query":"","types":[],"prefer":"balanced","reason":""}]}')
```

在 `expand_query_prompt` 的编号项里(`2. sub_queries` 之前)插入:

```python
        "2. high_level_keywords: themes / relationship types / abstract topics "
        "(used to retrieve RELATIONS).\n"
        "3. low_level_keywords: concrete entities / names / specifics (used to "
        "retrieve ENTITIES).\n"
```

并把后续 `sub_queries` 编号顺延为 4,返回行的 JSON 模板改为:

```python
        'Return JSON only: {"query_en":"","high_level_keywords":[],'
        '"low_level_keywords":[],"sub_queries":[{"query":""' + types_schema + "}]}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_dual_keywords.py tests/test_query_rewrite.py -q`
Expected: PASS(双层关键词通过;既有 `test_query_rewrite.py` 不回归)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/query_rewrite.py backend/app/services/prompts.py backend/tests/test_dual_keywords.py
git commit -m "feat(retrieval): expand_query 多吐 high/low-level keywords(复用同一次调用)"
```

---

## Phase 4 — 消费方:graph 种子融合(flag 门控 + 等价回退)

### Task 8: `relation_retrieval_enabled` 开关 + `_graph_seed_fusion` + `ask_graph` 接线

**Files:**
- Modify: `backend/app/core/config.py`、`backend/app/services/sqlite_repository.py`(`ask_graph`:4854)
- Test: `backend/tests/test_graph_seed_fusion.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_graph_seed_fusion.py
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


def _make_repo(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("RELATION_RETRIEVAL_ENABLED", "true" if flag else "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_bridge(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b",
                  "edge_type": "derived_from", "evidence": []}]
    repo.store_kg(nb.id, None, objects, relations)
    return nb


def test_seed_fusion_off_returns_base_unchanged(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch, flag=False)
    nb = _seed_bridge(repo)
    base = ["ko-x", "ko-y"]
    assert repo._graph_seed_fusion(nb.id, "regulated cascode", base) == base


def test_seed_fusion_on_adds_relation_endpoints(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch, flag=True)
    nb = _seed_bridge(repo)
    # 取关系两端真实 object_id
    with repo._connect() as db:
        row = db.execute(
            "SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?",
            (nb.id,)).fetchone()
    base = ["ko-seed"]
    fused = repo._graph_seed_fusion(nb.id, "regulated cascode", base)
    assert "ko-seed" in fused                       # 不丢原种子(只增不减)
    assert row["source_object_id"] in fused or row["target_object_id"] in fused
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_graph_seed_fusion.py -q`
Expected: FAIL(`AttributeError: _graph_seed_fusion`)

- [ ] **Step 3: 实现**

3a. `config.py`(`rerank_enabled` 附近 ~:103):

```python
    relation_retrieval_enabled: bool = Field(False, env="RELATION_RETRIEVAL_ENABLED")
    relation_seed_top_n: int = Field(8, env="RELATION_SEED_TOP_N")
```

3b. `sqlite_repository.py` — 加 `_graph_seed_fusion`:

```python
    def _graph_seed_fusion(self, notebook_id: str, question: str,
                           base_seeds: List[str]) -> List[str]:
        """flag 关 → 原样返回 base_seeds(等价护栏:node recall 不降由「只增不减」保证)。
        flag 开 → 用 high-level keywords 查关系索引,两端 object 并入;low-level
        keywords 额外查节点并入。去重保序,cap 到 base + relation_seed_top_n。"""
        if not self.settings.relation_retrieval_enabled:
            return base_seeds
        from app.services.query_rewrite import expand_query
        exp = expand_query(self.rewrite_llm_client, question,
                           timeout=getattr(self.settings, "rewrite_timeout_seconds", None))
        hl = " ".join(exp.high_level_keywords) or exp.query_en or question
        ll = " ".join(exp.low_level_keywords)
        extra: List[str] = []
        rel_hits = self.federated_retrieve_relations(notebook_id, hl)[
            : self.settings.relation_seed_top_n]
        for h in rel_hits:
            extra.extend((h.source_object_id, h.target_object_id))
        if ll:
            node_hits = self.federated_retrieve(notebook_id, ll)[
                : self.settings.relation_seed_top_n]
            extra.extend(h.object_id for h in node_hits)
        seen, fused = set(), []
        for oid in list(base_seeds) + extra:   # base 优先保序,只增不减
            if oid and oid not in seen:
                seen.add(oid)
                fused.append(oid)
        return fused
```

3c. `ask_graph` 接线 —— 把 `use_seeds = ...`(:4854)替换为:

```python
        base_seeds = seed_ids if seed_ids else [h.object_id for h in top_hits[:5]]
        use_seeds = self._graph_seed_fusion(notebook_id, question, base_seeds)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_graph_seed_fusion.py tests/test_graph_reason.py tests/test_ask_modes.py -q`
Expected: PASS(融合通过;graph/ask 既有路径不回归)

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_graph_seed_fusion.py
git commit -m "feat(kg): graph 种子融合关系检索(RELATION_RETRIEVAL_ENABLED,默认关,等价回退)"
```

---

## Phase 5 — 度量(双轨 gold 集 + recall 扩关系)

### Task 9: `run_recall` 扩关系 gold + `recall_gold.yaml` + `run_all` 接线

**Files:**
- Modify: `backend/app/eval/retrieval_metrics.py`、`backend/app/eval/run_all.py`
- Create: `backend/app/eval/recall_gold.yaml`
- Test: `backend/tests/test_recall_relations.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_recall_relations.py
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.eval.retrieval_metrics import run_recall


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_run_recall_reports_relation_metrics(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b", "edge_type": "derived_from", "evidence": []}]
    repo.store_kg(nb.id, None, objects, relations)
    with repo._connect() as db:
        rid = db.execute("SELECT id FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchone()["id"]
    questions = [{"id": "g1", "question": "regulated cascode derived from cascode",
                  "gold_relation_ids": [rid]}]
    rows = run_recall(repo, nb.id, questions)
    assert rows and rows[0]["id"] == "g1"
    assert rows[0]["relation_recall_at_k"] == 1.0   # 关系被检索到
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_recall_relations.py -q`
Expected: FAIL(`KeyError: 'relation_recall_at_k'`)

- [ ] **Step 3: 实现(`retrieval_metrics.py` 的 `run_recall` 改为同时支持对象与关系 gold)**

```python
def run_recall(repo: Any, notebook_id: str, questions: List[Dict[str, Any]],
               k: int = 12) -> List[Dict[str, Any]]:
    """对带 gold_object_ids 或 gold_relation_ids 的题分别跑节点/关系检索,
    各算 recall@k + MRR。两者皆缺的题跳过。无 LLM 答案调用,便宜。"""
    rows: List[Dict[str, Any]] = []
    for q in questions:
        gold_obj = q.get("gold_object_ids")
        gold_rel = q.get("gold_relation_ids")
        if not gold_obj and not gold_rel:
            continue
        row: Dict[str, Any] = {"id": q.get("id", ""),
                               "track": q.get("track", ""), "bucket": q.get("bucket", "")}
        if gold_obj:
            ids = [h.object_id for h in repo._retrieve_scored(notebook_id, q["question"])]
            row["recall_at_k"] = recall_at_k(ids, gold_obj, k)
            row["mrr"] = mrr(ids, gold_obj)
            row["n_gold"] = len(gold_obj)
        if gold_rel:
            rids = [h.relation_id for h in repo._retrieve_relations_scored(notebook_id, q["question"])]
            row["relation_recall_at_k"] = recall_at_k(rids, gold_rel, k)
            row["relation_mrr"] = mrr(rids, gold_rel)
            row["n_gold_rel"] = len(gold_rel)
        rows.append(row)
    return rows
```

`recall_gold.yaml` 脚手架(人工锚点示例 + 字段约定,反向出题由 Task 10 追加):

```yaml
# KG 检索 gold 集(双轨)。track: reverse(KG 反向出题) | anchor(人工锚点)。
# bucket: node | bridge。gold_object_ids / gold_relation_ids 至少一项。
- {id: a01, track: anchor, bucket: node, question: "什么是 regulated cascode?", gold_object_ids: []}
- {id: a02, track: anchor, bucket: bridge, question: "regulated cascode 与 standard cascode 的关系?", gold_relation_ids: []}
```

`run_all.py` 的 recall 分支改为读 `recall_gold.yaml`:

```python
    if "recall" in only:
        import yaml, pathlib as _pl
        from app.core.config import Settings
        from app.eval.retrieval_metrics import run_recall
        from app.services.sqlite_repository import SQLiteRepository
        gold_path = _pl.Path(__file__).resolve().parent / "recall_gold.yaml"
        gold = yaml.safe_load(open(gold_path, encoding="utf-8")) or []
        repo = SQLiteRepository(Settings())
        rows = run_recall(repo, a.notebook, gold)
        (out / "recall_report.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[eval] recall_report.json done ({len(rows)} graded)")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_recall_relations.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/eval/retrieval_metrics.py backend/app/eval/run_all.py backend/app/eval/recall_gold.yaml backend/tests/test_recall_relations.py
git commit -m "feat(eval): recall 扩关系 gold(recall_gold.yaml 双轨 + node/relation 双指标)"
```

---

### Task 10: KG 反向出题生成器(含泄漏体检)

**Files:**
- Create: `backend/scripts/gen_recall_gold.py`
- Modify: `backend/app/eval/retrieval_metrics.py`(加纯函数 `leakage_ratio`)
- Test: `backend/tests/test_gen_recall_gold.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_gen_recall_gold.py
from app.eval.retrieval_metrics import leakage_ratio


def test_leakage_ratio_high_when_question_quotes_source():
    # 问题逐字复用源文本 → 高泄漏
    r = leakage_ratio("regulated cascode adds a gain stage",
                      "regulated cascode adds a gain stage to boost output resistance")
    assert r > 0.8


def test_leakage_ratio_low_when_paraphrased():
    r = leakage_ratio("如何在不堆叠太多管子的前提下进一步提高输出阻抗?",
                      "regulated cascode adds a gain stage to boost output resistance")
    assert r < 0.3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gen_recall_gold.py -q`
Expected: FAIL(`ImportError: cannot import name 'leakage_ratio'`)

- [ ] **Step 3: 实现**

3a. `retrieval_metrics.py` 加纯函数(复用检索 tokenizer,保证与召回同口径):

```python
def leakage_ratio(question: str, source_text: str) -> float:
    """问题与源文本的字面 token 重合占问题 token 的比例(0..1)。用于 KG 反向出题
    防泄漏体检:过高说明问题复用了源文本原话,召回会虚高,应剔除或要求改写。"""
    from app.services.retrieval import _tokens, _STOPWORDS
    q = {t for t in _tokens(question) if t not in _STOPWORDS}
    if not q:
        return 0.0
    src = set(_tokens(source_text))
    return sum(1 for t in q if t in src) / len(q)
```

3b. `scripts/gen_recall_gold.py`(采样 KG → LLM 生题 → 泄漏体检 → 写 yaml):

```python
"""KG 反向出题生成器(铺量 gold)。用法:
PYTHONPATH=backend python -m scripts.gen_recall_gold --notebook nb-xxx --n-obj 30 --n-rel 30 --out backend/app/eval/recall_gold.gen.yaml

每个采样的对象/关系让 LLM 写一道自然问题(强制改写、禁逐字引用),gold=源 id;
leakage_ratio > 0.6 的题剔除(泄漏)。生成后人工抽检并入 recall_gold.yaml。"""
import argparse, json, yaml, random
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.retrieval import _payload_text
from app.eval.retrieval_metrics import leakage_ratio

_GEN_SCHEMA = '{"question":""}'
_LEAK_MAX = 0.6


def _gen_question(client, source_text: str) -> str:
    msg = [{"role": "user", "content":
            "根据下面的知识片段,写一道工程师会问的自然问题,其答案需要用到该片段。"
            "要求:改写表述、不要逐字照抄原文、不要直接点名片段里的专有名词原样串联。\n"
            f"知识片段:{source_text}\n只返回 JSON: {{\"question\":\"...\"}}"}]
    try:
        return str(json.loads(client.chat_json(msg, _GEN_SCHEMA)).get("question", "")).strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook", required=True)
    ap.add_argument("--n-obj", type=int, default=30)
    ap.add_argument("--n-rel", type=int, default=30)
    ap.add_argument("--out", default="backend/app/eval/recall_gold.gen.yaml")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    repo = SQLiteRepository(Settings())
    assert repo.llm_client.configured, "LLM 未配置(.env)"
    out, dropped = [], 0
    with repo._connect() as db:
        objs = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND status IN ('approved','reviewed')",
            (a.notebook,)).fetchall()
        rels = repo._relations_with_names(db, a.notebook)
    for r in rng.sample(objs, min(a.n_obj, len(objs))):
        src = _payload_text(json.loads(r["payload"] or "{}"))
        q = _gen_question(repo.llm_client, src)
        if q and leakage_ratio(q, src) <= _LEAK_MAX:
            out.append({"id": f"r-obj-{r['id'][-6:]}", "track": "reverse", "bucket": "node",
                        "question": q, "gold_object_ids": [r["id"]]})
        else:
            dropped += 1
    for r in rng.sample(rels, min(a.n_rel, len(rels))):
        q = _gen_question(repo.llm_client, r["text"])
        if q and leakage_ratio(q, r["text"]) <= _LEAK_MAX:
            out.append({"id": f"r-rel-{r['id'][-6:]}", "track": "reverse", "bucket": "bridge",
                        "question": q, "gold_relation_ids": [r["id"]],
                        "gold_object_ids": [r["source_object_id"], r["target_object_id"]]})
        else:
            dropped += 1
    yaml.safe_dump(out, open(a.out, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    print(f"[gen] wrote {len(out)} questions ({dropped} dropped as leakage) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_gen_recall_gold.py -q`
Expected: PASS(2 passed;`leakage_ratio` 纯函数可测,生成器靠真 LLM,不进单测)

- [ ] **Step 5: 提交**

```bash
git add backend/app/eval/retrieval_metrics.py backend/scripts/gen_recall_gold.py backend/tests/test_gen_recall_gold.py
git commit -m "feat(eval): KG 反向出题生成器 + leakage_ratio 防泄漏体检"
```

---

## 收尾:全量验证 + 文档

- [ ] **全量测试**: `cd backend && python -m pytest -q` → 全绿(新增 6 个测试文件 + 既有不回归)。
- [ ] **check.sh**(若存在): `./check.sh` → EXIT=0。
- [ ] **环境变量文档**: 在 `.env.example` 加 `RELATION_RETRIEVAL_ENABLED=false`、`RELATION_SEED_TOP_N=8`;README/README_zh 的检索增强开关块补一行。
- [ ] **真机度量(用户跑,沿用 eval 纪律,不动 prod):**
  1. 生成 gold:`python -m scripts.gen_recall_gold --notebook <prod副本nb> --n-obj 30 --n-rel 30`,人工抽检并并入 `recall_gold.yaml`(补几条 anchor)。
  2. 回填关系向量:`python -m scripts.backfill_relation_embeddings <nb>`。
  3. baseline(`RELATION_RETRIEVAL_ENABLED=false`)对照 treatment(`=true`)跑 `python -m app.eval.run_all --notebook <nb> --only recall`,比对 `relation_recall_at_k` ↑ 且 `recall_at_k` 不降。
- [ ] **按 dev-flow 提 PR**(3-way 并最新 master → push → `gh pr create --base master`)。

---

## Self-Review(写计划后自查)

- **Spec 覆盖**:关系向量索引(T1-3,5,6)✓;双层关键词(T7)✓;graph 种子融合 + flag + 等价(T8)✓;双轨 gold + recall 扩关系(T9-10)✓;不变量 [0,1]/tau/dual-index(T4 测试 + score_relations 同尺)✓;回退/旧库回填(T3,8)✓。**out-of-scope**(chunk×graph 叠加 / 构建侧 / token 预算)未建任务 ✓。
- **占位符**:无 TBD/TODO;每个 code step 给完整代码与命令。`recall_gold.yaml` 脚手架的两条 anchor 是 `gold_*: []` 占位待用户填——已在收尾步骤显式说明由真机生成/人工补,非计划缺口。
- **类型/命名一致**:`relation_embed_text`、`RetrievedRelation`、`score_relations`、`_relations_with_names`、`_retrieve_relations_scored`、`federated_retrieve_relations`、`_graph_seed_fusion`、`high_level_keywords`/`low_level_keywords`、`RELATION_RETRIEVAL_ENABLED`/`relation_seed_top_n`、`relation_recall_at_k`/`relation_mrr`、`leakage_ratio` —— 跨任务一致。
- **依赖顺序注记**:`_relations_with_names`(T5 定义)被 T3 的 `_backfill_relation_embeddings` 引用 → 已在 T3 Step 3 注明「本任务顺带实现 `_relations_with_names`」,T5 仅加 `_retrieve_relations_scored`。执行时遵此顺序。
