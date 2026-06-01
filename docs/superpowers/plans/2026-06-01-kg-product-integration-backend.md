# KG-Only Backend Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the knowledge-graph (KG) pipeline silicon-notebook's only extraction + knowledge model, supporting only `academic_paper`/`textbook`, auto-approving nodes into `knowledge_objects` + a new `knowledge_relations` table, with KG-native `/graph` and `/ask`, and delete all qiefen/legacy/other-doc-type code.

**Architecture:** Ingestion (`process_source`) calls a new `kg_ingest` adapter: read the source's raw text → `kg.windowing.make_windows` → concurrent `kg.extract.extract_window` (deepseek-v4-flash) → `kg.canonicalize` → validate → bind each node/edge's verbatim evidence to a `source_element` by substring match (dropping ungroundable) → write `knowledge_objects(status=approved)` + `knowledge_relations` + node-name embeddings. `/graph` and `/ask` read these tables. Old extraction paths and types are removed.

**Tech Stack:** Python 3 / FastAPI / SQLite (`sqlite_repository.py`), pydantic models (`app/models/schemas.py`), existing `app/services/kg/*` (windowing/extract/canonicalize/client/models/emit), pytest.

**Scope note:** Frontend changes (remove rules/methods/risks/glossary pages, KG-native browse/graph UI, doc-type picker) are a SEPARATE follow-up plan on the same branch. This plan delivers a working, testable backend on its own. The single `frontend/app/page.tsx` will have dead API calls until the frontend plan lands; that is expected and acceptable mid-branch.

**Reference spec:** `docs/superpowers/specs/2026-06-01-kg-product-integration-design.md`

---

## File Structure

**Create**
- `backend/app/services/kg_ingest.py` — adapter: raw text → KG graph → (knowledge_objects, knowledge_relations). The only bridge between `app/services/kg/*` and the product.
- `backend/tests/test_kg_ingest.py` — unit tests for the adapter (mock LLM client).
- `backend/tests/test_kg_repository.py` — tests for new repo methods (relations table, KG store, KG graph, KG retrieval).

**Modify**
- `backend/app/services/sqlite_repository.py` — add `knowledge_relations` table + methods; rewrite `_run_extraction`; rewrite `knowledge_graph`; add `_source_raw_text`; KG headline.
- `backend/app/services/retrieval.py` — KG node-type weights; keep `score_knowledge` shape.
- `backend/app/services/extraction_profiles.py` — collapse to `academic_paper`/`textbook` + 4 KG object schemas; drop other profiles/types/qiefen registration.
- `backend/app/api/routes.py` — delete `scenario-query`, `list_rules/methods/risks/glossary`, `doc-types` picker for removed types; keep `/graph`, `/ask`, `/knowledge`, `/knowledge-types`.
- `backend/app/models/schemas.py` — slim `AskResponse` to KG fields; keep `KnowledgeNode/KnowledgeEdge/KnowledgeGraph/KnowledgeRecord`.

**Delete**
- `backend/app/services/qiefen/` (whole package), `backend/app/services/qiefen_ingest.py`, `backend/app/services/extraction.py` (legacy extractor; move `CandidateRecord` usage away first).
- Tests: `backend/tests/test_qiefen_ingest.py`, `test_qiefen_registry.py`, `test_qiefen_cutover_integration.py`, `backend/tests/qiefen/`, and any legacy-extraction / removed-type tests.

---

## Task 1: `knowledge_relations` table + repository methods

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` (CREATE TABLE block near lines 197–268; add methods near `knowledge_graph`)
- Test: `backend/tests/test_kg_repository.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_kg_repository.py
import pytest
from app.services.sqlite_repository import SqliteRepository

@pytest.fixture
def repo(tmp_path):
    r = SqliteRepository(db_path=tmp_path / "t.db", storage_dir=tmp_path / "store")
    return r

def _notebook_source(repo):
    nb = repo.create_notebook(title="nb", template="academic_paper")
    # minimal source row via repo internals
    src = repo._insert_source_for_test(nb.id, title="doc", file_name="doc.md") \
        if hasattr(repo, "_insert_source_for_test") else None
    return nb, src

def test_add_and_read_relations(repo):
    nb = repo.create_notebook(title="nb", template="academic_paper")
    # two knowledge objects to connect
    a = repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    b = repo._test_insert_object(nb.id, "claim", {"name": "MOSFET has threshold voltage"})
    repo.add_relations(nb.id, "src1", [
        {"source_object_id": b, "target_object_id": a, "edge_type": "about",
         "evidence": [{"quote": "threshold voltage of the MOSFET"}]},
    ])
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1
    assert rels[0]["source_object_id"] == b
    assert rels[0]["target_object_id"] == a
    assert rels[0]["edge_type"] == "about"
```

> Note: `_test_insert_object` is a tiny test helper added in Step 3 alongside the table; it inserts a row into `knowledge_objects` and returns its id. If the repo already exposes a public approve/insert path, use that instead and delete the helper.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_add_and_read_relations -v`
Expected: FAIL with `AttributeError: 'SqliteRepository' object has no attribute 'add_relations'`

- [ ] **Step 3: Add the table + methods**

In `sqlite_repository.py`, add to the schema-creation block (alongside the other `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS knowledge_relations (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
  source_object_id TEXT NOT NULL,
  target_object_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
```

Add methods (place near `knowledge_graph`):

```python
def add_relations(self, notebook_id: str, source_id: str,
                  relations: List[dict]) -> int:
    now = _now()
    with self._connect() as db:
        for rel in relations:
            db.execute(
                """
                INSERT INTO knowledge_relations
                (id, notebook_id, source_id, source_object_id, target_object_id,
                 edge_type, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"rel-{uuid4().hex[:10]}", notebook_id, source_id,
                    rel["source_object_id"], rel["target_object_id"],
                    rel["edge_type"],
                    json.dumps(rel.get("evidence", []), ensure_ascii=False),
                    now,
                ),
            )
    return len(relations)

def relations_for_notebook(self, notebook_id: str) -> List[dict]:
    with self._connect() as db:
        rows = db.execute(
            "SELECT * FROM knowledge_relations WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
    return [
        {
            "id": r["id"], "source_object_id": r["source_object_id"],
            "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
            "evidence": json.loads(r["evidence"] or "[]"),
        }
        for r in rows
    ]

def _delete_relations_for_source(self, db, source_id: str) -> None:
    db.execute("DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,))

# test-only helper; remove once a public insert path is reused
def _test_insert_object(self, notebook_id: str, object_type: str, payload: dict) -> str:
    oid = f"ko-{uuid4().hex[:10]}"; now = _now()
    with self._connect() as db:
        db.execute(
            """INSERT INTO knowledge_objects
               (id, notebook_id, object_type, status, owner, payload, evidence,
                source_candidate_id, created_at, updated_at)
               VALUES (?, ?, ?, 'approved', '', ?, '[]', NULL, ?, ?)""",
            (oid, notebook_id, object_type, json.dumps(payload, ensure_ascii=False), now, now),
        )
    return oid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_add_and_read_relations -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): add knowledge_relations table + repo methods"
```

---

## Task 2: `kg_ingest.extract_graph` — raw text → validated KG

**Files:**
- Create: `backend/app/services/kg_ingest.py`
- Test: `backend/tests/test_kg_ingest.py` (Create)

- [ ] **Step 1: Write the failing test (mock LLM client)**

```python
# backend/tests/test_kg_ingest.py
from app.services import kg_ingest

class FakeClient:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, prompt: str, retries: int = 4) -> str: return self._p

ABS = "We propose Engram, a memory architecture. Engram improves perplexity."

def test_extract_graph_grounds_nodes():
    import json
    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram",
             "evidence": "Engram, a memory architecture"},
            {"local_id": "b", "type": "Claim", "name": "Engram improves perplexity",
             "evidence": "Engram improves perplexity"},
            {"local_id": "z", "type": "Concept", "name": "Ghost",
             "evidence": "text that does not appear"},
        ],
        "edges": [{"type": "about", "source": "b", "target": "a",
                   "evidence": "Engram improves perplexity"}],
    })
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    names = {n.name for n in g.nodes}
    assert "Engram" in names and "Engram improves perplexity" in names
    assert "Ghost" not in names           # ungroundable node dropped
    assert len(g.edges) == 1              # edge endpoints survived
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_ingest.py::test_extract_graph_grounds_nodes -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.kg_ingest'`

- [ ] **Step 3: Implement `extract_graph`**

```python
# backend/app/services/kg_ingest.py
"""Adapter: source raw text -> KG (nodes/edges/evidence) -> product knowledge.
The ONLY bridge between app.services.kg.* and the product. Extraction model is
the product LLM (deepseek-v4-flash via OPENAI_COMPAT_*)."""
from __future__ import annotations

import concurrent.futures as cf
from typing import Any, List, Tuple

from app.services.kg.windowing import make_windows
from app.services.kg.extract import extract_window
from app.services.kg.canonicalize import canonicalize
from app.services.kg.models import Edge, KnowledgeGraph, Node

DOC_TYPE_MAP = {"academic_paper": "academic", "article": "academic", "textbook": "textbook"}
_WORKERS = 16


def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450) -> KnowledgeGraph:
    """Window the text, extract a KG fragment per window concurrently, then
    canonicalize. Ungroundable nodes/edges are already dropped inside
    extract_window (evidence located verbatim in the window)."""
    wins = make_windows(raw_text, source_file, None, n, m)
    nodes: List[Node] = []
    edges: List[Edge] = []
    if wins:
        workers = max(1, min(_WORKERS, len(wins)))
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for ns, es in pool.map(
                lambda w: extract_window(client, raw_text, w.char_start, w.char_end,
                                         w.section_path, doc_type),
                wins,
            ):
                nodes += ns
                edges += es
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
    return KnowledgeGraph(doc_id=source_file, doc_type=doc_type, nodes=nodes, edges=edges)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_ingest.py::test_extract_graph_grounds_nodes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_ingest.py backend/tests/test_kg_ingest.py
git commit -m "feat(kg): kg_ingest.extract_graph (windows -> extract -> canonicalize)"
```

---

## Task 3: `kg_ingest.build_records` — bind evidence to source_elements

**Files:**
- Modify: `backend/app/services/kg_ingest.py`
- Test: `backend/tests/test_kg_ingest.py`

The KG carries char-precise evidence in the raw text. Product `Evidence` needs an `element_id`. Bind each node/edge evidence quote to the `source_element` whose `text` contains it (exact, then whitespace-flexible, then CJK token-overlap ≥ 0.6). Drop nodes whose evidence binds to no element; drop edges whose endpoint node was dropped.

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_kg_ingest.py
from app.models.schemas import SourceElement
from app.services.kg.models import Node, Edge, Evidence, KnowledgeGraph

def _el(i, text): return SourceElement(id=i, source_id="s1", element_type="paragraph",
                                       location_label=f"p{i}", text=text)

def test_build_records_binds_and_drops():
    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="C1", type="Concept", name="Engram",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=6,
                                    line_start=1, line_end=1, quote="Engram")]),
            Node(id="C2", type="Concept", name="Nowhere",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=3,
                                    line_start=1, line_end=1, quote="zzz")]),
        ],
        edges=[Edge(id="E1", type="about", source_id="C1", target_id="C2")])
    elements = [_el("e1", "Engram is a memory architecture.")]
    objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert [o["object_type"] for o in objects] == ["concept"]   # C2 dropped (unbound)
    assert objects[0]["payload"]["name"] == "Engram"
    assert objects[0]["evidence"][0]["element_id"] == "e1"
    assert objects[0]["local_id"] == "C1"                        # carried for edge wiring
    assert relations == []                                       # edge dropped: C2 gone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_ingest.py::test_build_records_binds_and_drops -v`
Expected: FAIL with `AttributeError: module 'app.services.kg_ingest' has no attribute 'build_records'`

- [ ] **Step 3: Implement binding + record building**

```python
# add to backend/app/services/kg_ingest.py
import re

def _norm(s: str) -> str:
    return " ".join((s or "").split())

def _tokens(s: str) -> set:
    return set(re.findall(r"\w+", (s or "").lower()))

def _bind_quote(quote: str, elements) -> dict | None:
    """Return product-Evidence fields for the element that best contains `quote`."""
    q = _norm(quote)
    if len(q) < 3:
        return None
    for el in elements:                       # exact substring on normalized text
        if q and q in _norm(el.text):
            return _ev(el, quote)
    qt = _tokens(quote)                        # CJK / fuzzy fallback: token overlap >= 0.6
    if qt:
        best, best_ov = None, 0.0
        for el in elements:
            et = _tokens(el.text)
            if not et:
                continue
            ov = len(qt & et) / len(qt)
            if ov > best_ov:
                best, best_ov = el, ov
        if best is not None and best_ov >= 0.6:
            return _ev(best, quote)
    return None

def _ev(el, quote: str) -> dict:
    return {
        "source_id": el.source_id, "source_title": "", "element_id": el.id,
        "element_type": el.element_type, "location_label": el.location_label,
        "quoted_span": (quote or "")[:400], "confidence": 1.0,
    }

def build_records(graph: KnowledgeGraph, source_id: str, source_title: str,
                  elements) -> Tuple[List[dict], List[dict]]:
    """KG graph -> (objects, relations) with product evidence bound to elements.
    Nodes whose evidence binds to no element are dropped; edges referencing a
    dropped node are dropped. Each object dict carries `local_id` (= KG node id)
    so the caller can remap edges to DB ids after insert."""
    kept: dict = {}
    objects: List[dict] = []
    for node in graph.nodes:
        bound = []
        for ev in node.evidence:
            fields = _bind_quote(ev.quote, elements)
            if fields:
                fields["source_title"] = source_title
                bound.append(fields)
        if not bound:
            continue
        kept[node.id] = True
        objects.append({
            "local_id": node.id,
            "object_type": node.type.lower(),
            "payload": {"name": node.name, "section_path": node.section_path},
            "evidence": bound,
        })
    relations: List[dict] = []
    for edge in graph.edges:
        if edge.source_id in kept and edge.target_id in kept:
            relations.append({
                "source_local_id": edge.source_id,
                "target_local_id": edge.target_id,
                "edge_type": edge.type,
                "evidence": [{"quote": ev.quote} for ev in edge.evidence],
            })
    return objects, relations
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_ingest.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_ingest.py backend/tests/test_kg_ingest.py
git commit -m "feat(kg): bind KG evidence to source_elements; build object/relation records"
```

---

## Task 4: repo `store_kg` — write approved objects + relations + embeddings

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_kg_repository.py
def test_store_kg_writes_objects_and_relations(repo):
    nb = repo.create_notebook(title="nb", template="academic_paper")
    objects = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "Abstract"},
         "evidence": [{"source_id": "s1", "source_title": "Doc", "element_id": "e1",
                       "element_type": "paragraph", "location_label": "p1",
                       "quoted_span": "Engram", "confidence": 1.0}]},
        {"local_id": "K1", "object_type": "claim",
         "payload": {"name": "Engram improves perplexity", "section_path": "Abstract"},
         "evidence": [{"source_id": "s1", "source_title": "Doc", "element_id": "e1",
                       "element_type": "paragraph", "location_label": "p1",
                       "quoted_span": "improves perplexity", "confidence": 1.0}]},
    ]
    relations = [{"source_local_id": "K1", "target_local_id": "C1",
                  "edge_type": "about", "evidence": [{"quote": "Engram improves perplexity"}]}]
    repo.store_kg(nb.id, "s1", objects, relations)
    recs = repo.list_knowledge(nb.id, "concept")
    assert len(recs) == 1 and recs[0].headline == "Engram"
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1 and rels[0]["edge_type"] == "about"
    # relation endpoints are real knowledge_object ids, not local ids
    ids = {r.id for r in repo.list_knowledge(nb.id, "concept")} | \
          {r.id for r in repo.list_knowledge(nb.id, "claim")}
    assert rels[0]["source_object_id"] in ids and rels[0]["target_object_id"] in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_store_kg_writes_objects_and_relations -v`
Expected: FAIL with `AttributeError: 'SqliteRepository' object has no attribute 'store_kg'`

- [ ] **Step 3: Implement `store_kg`**

```python
# in sqlite_repository.py, near knowledge_graph
def store_kg(self, notebook_id: str, source_id: str,
             objects: List[dict], relations: List[dict]) -> Tuple[int, int]:
    """Insert KG nodes as approved knowledge_objects and edges as
    knowledge_relations (remapping local ids to DB ids). Embeds node name."""
    now = _now()
    local_to_id: Dict[str, str] = {}
    with self._connect() as db:
        for obj in objects:
            oid = f"ko-{uuid4().hex[:10]}"
            local_to_id[obj["local_id"]] = oid
            db.execute(
                """INSERT INTO knowledge_objects
                   (id, notebook_id, object_type, status, owner, payload, evidence,
                    source_candidate_id, created_at, updated_at)
                   VALUES (?, ?, ?, 'approved', '', ?, ?, NULL, ?, ?)""",
                (oid, notebook_id, obj["object_type"],
                 json.dumps(obj["payload"], ensure_ascii=False),
                 json.dumps(obj["evidence"], ensure_ascii=False), now, now),
            )
    db_relations = []
    for rel in relations:
        s = local_to_id.get(rel["source_local_id"]); t = local_to_id.get(rel["target_local_id"])
        if not s or not t:
            continue
        db_relations.append({"source_object_id": s, "target_object_id": t,
                             "edge_type": rel["edge_type"], "evidence": rel.get("evidence", [])})
    if db_relations:
        self.add_relations(notebook_id, source_id, db_relations)
    for obj in objects:                              # payload-level embeddings (best-effort)
        try:
            self._embed_knowledge(local_to_id[obj["local_id"]], notebook_id, obj["payload"])
        except Exception:
            pass
    return len(objects), len(db_relations)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py -v`
Expected: PASS (both tests). If `list_knowledge` requires a registered schema for `concept`/`claim`, this passes after Task 8; for now assert on raw rows via a `db.execute` count instead, then tighten in Task 8.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): store_kg writes approved objects + relations + embeddings"
```

---

## Task 5: Rewire `_run_extraction` to the KG path

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` (`_run_extraction` lines 1002–1081; replace `_extract_records` lines 1083–1103; add `_source_raw_text`)
- Test: `backend/tests/test_kg_repository.py`

- [ ] **Step 1: Write the failing test (end-to-end with mock client)**

```python
# add to backend/tests/test_kg_repository.py
import json

class _FakeLLM:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, prompt, retries=4): return self._p
    def embed(self, text): return [0.0, 0.0]

def test_run_extraction_kg_path(tmp_path):
    repo = SqliteRepository(db_path=tmp_path/"t.db", storage_dir=tmp_path/"store")
    repo.llm_client = _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []}))
    nb = repo.create_notebook(title="nb", template="academic_paper")
    src = repo._test_insert_source(nb.id, title="Doc", file_name="doc.md",
                                   doc_type="academic_paper",
                                   text="Engram is a memory architecture.")
    repo._run_extraction(src.id)
    recs = repo.list_knowledge(nb.id, "concept")
    assert any(r.headline == "Engram" for r in recs)
```

> `_test_insert_source` inserts a `sources` row + one `source_elements` row (text) + writes the raw file to the storage dir. Add it as a test helper next to `_test_insert_object` (mirror `upload_sources` minimally). If a public test path already exists, use it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_run_extraction_kg_path -v`
Expected: FAIL (current `_run_extraction` writes candidates via qiefen/legacy, not approved KG objects)

- [ ] **Step 3: Rewrite `_run_extraction` and delete `_extract_records`**

Replace the body of `_run_extraction` with the KG flow, and add `_source_raw_text` (adapted from the deleted `_source_text_for_qiefen`):

```python
def _run_extraction(self, source_id: str) -> None:
    source = self.get_source(source_id)
    elements = self.source_elements(source_id)
    now = _now()
    run_id = f"run-{uuid4().hex[:10]}"
    doc_type_id = _normalize_doc_type(getattr(source, "doc_type", "") or "") or "academic_paper"
    kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type_id, "academic")
    with self._connect() as db:
        self._clear_source_extraction_state(db, source_id, source.notebook_id, clear_embeddings=False)
        self._delete_relations_for_source(db, source_id)
        db.execute("DELETE FROM knowledge_objects WHERE notebook_id = ? AND id IN "
                   "(SELECT id FROM knowledge_objects WHERE notebook_id = ? )"
                   " AND source_candidate_id IS NULL AND id IN ("
                   "  SELECT id FROM knowledge_objects WHERE notebook_id = ?)",
                   (source.notebook_id, source.notebook_id, source.notebook_id))  # see note
        db.execute(
            """INSERT INTO extraction_runs
               (id, notebook_id, source_id, run_type, status, error_message, created_at, updated_at)
               VALUES (?, ?, ?, 'kg', 'running', '', ?, ?)""",
            (run_id, source.notebook_id, source_id, now, now))
    try:
        if not getattr(self.llm_client, "configured", False):
            with self._connect() as db:
                db.execute("UPDATE extraction_runs SET status='completed', error_message='no-llm', updated_at=? WHERE id=?", (_now(), run_id))
            return
        raw_text = self._source_raw_text(source, elements)
        graph = kg_ingest.extract_graph(self.llm_client, raw_text,
                                        source.file_name or "source.md", kg_doc_type)
        objects, relations = kg_ingest.build_records(graph, source.id, source.title, elements)
        n_obj, n_rel = self.store_kg(source.notebook_id, source.id, objects, relations)
        with self._connect() as db:
            db.execute("UPDATE extraction_runs SET status='completed', error_message=?, updated_at=? WHERE id=?",
                       (f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type}", _now(), run_id))
    except Exception as exc:
        with self._connect() as db:
            db.execute("UPDATE extraction_runs SET status='failed', error_message=?, updated_at=? WHERE id=?",
                       (str(exc), _now(), run_id))
        raise

def _source_raw_text(self, source, elements) -> str:
    """Raw document text for windowing: read the stored .md/.txt file when
    present, else reconstruct from element texts."""
    path = getattr(source, "file_path", "") or ""
    if path and (path.endswith(".md") or path.endswith(".markdown") or path.endswith(".txt")):
        try:
            from pathlib import Path
            return Path(path).read_text(encoding="utf-8")
        except Exception:
            pass
    return "\n\n".join(e.text for e in elements)
```

> **Implementation note for the executor:** the `_clear_source_extraction_state` call already clears this source's candidates. KG objects are notebook-scoped but produced per source; to make re-extraction idempotent, prefer storing `source_id` on `knowledge_objects` (add a column) OR delete the prior run's objects by tracking their ids. The placeholder DELETE above is a stand-in — replace it by adding a `source_id` column to `knowledge_objects` in this task (default '', set it in `store_kg`) and clearing with `DELETE FROM knowledge_objects WHERE source_id = ?`. Write a test `test_reextraction_is_idempotent` that runs `_run_extraction` twice and asserts counts don't double.

Add the import at top of `sqlite_repository.py`: `from app.services import kg_ingest`. Delete `_extract_records` and `_source_text_for_qiefen`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py -v`
Expected: PASS (incl. `test_reextraction_is_idempotent`)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): _run_extraction uses KG path, auto-approves nodes + relations"
```

---

## Task 6: KG-native `knowledge_graph()`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` (`knowledge_graph` lines 1717–1745; add `_kg_headline`)
- Test: `backend/tests/test_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_kg_repository.py
def test_knowledge_graph_from_kg_tables(repo):
    nb = repo.create_notebook(title="nb", template="academic_paper")
    c = repo._test_insert_object(nb.id, "concept", {"name": "Engram"})
    k = repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    repo.add_relations(nb.id, "s1", [{"source_object_id": k, "target_object_id": c,
                                      "edge_type": "about", "evidence": []}])
    g = repo.knowledge_graph(nb.id)
    assert {n.object_type for n in g.nodes} == {"concept", "claim"}
    assert any(n.headline == "Engram" for n in g.nodes)
    assert len(g.edges) == 1
    e = g.edges[0]
    assert e.from_id == k and e.to_id == c and e.relation == "about"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_knowledge_graph_from_kg_tables -v`
Expected: FAIL (current `knowledge_graph` builds edges from `_relation_edges` over `related_*` payload fields, not `knowledge_relations`)

- [ ] **Step 3: Rewrite `knowledge_graph`**

```python
def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph:
    """KG-native graph: nodes = non-deprecated knowledge objects (4 KG types),
    edges = knowledge_relations rows."""
    self.get_notebook(notebook_id)
    with self._connect() as db:
        rows = db.execute(
            "SELECT id, object_type, status, payload FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated'", (notebook_id,)).fetchall()
    nodes = [
        KnowledgeNode(id=r["id"], object_type=r["object_type"],
                      headline=self._kg_headline(json.loads(r["payload"] or "{}")),
                      status=r["status"])
        for r in rows]
    valid = {n.id for n in nodes}
    edges = [
        KnowledgeEdge(from_id=rel["source_object_id"], to_id=rel["target_object_id"],
                      relation=rel["edge_type"], label=rel["edge_type"])
        for rel in self.relations_for_notebook(notebook_id)
        if rel["source_object_id"] in valid and rel["target_object_id"] in valid]
    return KnowledgeGraph(nodes=nodes, edges=edges)

def _kg_headline(self, payload: dict) -> str:
    name = (payload.get("name") or "").strip()
    return name[:120] if len(name) > 120 else name
```

Delete the now-unused `_relation_edges` and `_knowledge_headline` if nothing else references them (grep first).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_knowledge_graph_from_kg_tables -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): /graph reads KG nodes + knowledge_relations"
```

---

## Task 7: KG node-type weights + KG-native `ask`

**Files:**
- Modify: `backend/app/services/retrieval.py` (`_TYPE_WEIGHT` lines 79–86)
- Modify: `backend/app/models/schemas.py` (`AskResponse`)
- Modify: `backend/app/api/routes.py` (`ask`) + `sqlite_repository.py` ask implementation (1-hop expand)
- Test: `backend/tests/test_kg_repository.py` (or existing retrieval test file)

- [ ] **Step 1: Write the failing test (weights)**

```python
# add to backend/tests/test_kg_repository.py
def test_kg_type_weights():
    from app.services.retrieval import _TYPE_WEIGHT
    assert _TYPE_WEIGHT["claim"] == _TYPE_WEIGHT["formula"] == 1.0
    assert _TYPE_WEIGHT["procedure"] == 0.7
    assert _TYPE_WEIGHT["concept"] == 0.5
    for legacy in ("rule", "case", "checklist", "risk", "glossary"):
        assert legacy not in _TYPE_WEIGHT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_kg_type_weights -v`
Expected: FAIL (current dict has rule/case/... weights)

- [ ] **Step 3: Replace `_TYPE_WEIGHT` and slim `AskResponse`**

```python
# retrieval.py
_TYPE_WEIGHT = {
    "claim": 1.0,
    "formula": 1.0,
    "procedure": 0.7,
    "concept": 0.5,
}
```

In `schemas.py`, replace `AskResponse` with a KG-only shape:

```python
class AskResponse(BaseModel):
    answer_id: str = ""
    conclusion: str
    related_knowledge: List["KnowledgeRecord"] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    llm_mode: str = ""
```

In the `ask` implementation (repo): after `score_knowledge` returns top KG nodes, expand 1 hop over `relations_for_notebook` to pull neighbor objects, include them in `related_knowledge`, and build `citations` from each object's evidence. Remove references to `related_rules`/`related_cases`/`recommended_methods`/`potential_risks`/`checklist`.

- [ ] **Step 4: Write + run the ask test**

```python
# add to backend/tests/test_kg_repository.py
def test_ask_returns_kg_knowledge(tmp_path):
    repo = SqliteRepository(db_path=tmp_path/"t.db", storage_dir=tmp_path/"store")
    repo.llm_client = _FakeLLM("{}")
    nb = repo.create_notebook(title="nb", template="academic_paper")
    repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    resp = repo.ask(nb.id, "does engram improve perplexity?")
    assert any("Engram" in r.headline for r in resp.related_knowledge)
```

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_ask_returns_kg_knowledge tests/test_kg_repository.py::test_kg_type_weights -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/test_kg_repository.py
git commit -m "feat(kg): KG node-type weights + KG-native ask (1-hop expand)"
```

---

## Task 8: Collapse `extraction_profiles.py` to KG types

**Files:**
- Modify: `backend/app/services/extraction_profiles.py`
- Test: `backend/tests/test_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_kg_repository.py
def test_only_kg_profiles_and_schemas():
    from app.services.extraction_profiles import PROFILES, OBJECT_SCHEMAS
    assert set(PROFILES) == {"academic_paper", "textbook"}
    for t in ("concept", "claim", "formula", "procedure"):
        assert t in OBJECT_SCHEMAS
    for legacy in ("rule", "method", "risk", "case", "checklist", "glossary",
                   "finding", "principle", "example"):
        assert legacy not in OBJECT_SCHEMAS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_repository.py::test_only_kg_profiles_and_schemas -v`
Expected: FAIL

- [ ] **Step 3: Rewrite the registry**

Replace `PROFILES`, `OBJECT_SCHEMAS`, `OBJECT_TYPE_LABELS` with KG-only content; delete `TEMPLATE_PROFILE` entries for removed templates (keep `article`→`academic_paper`, add `textbook`→`textbook`); delete `_register_qiefen_types` (and its call) and any `_QIEFEN_*` constants; trim `_DETECTORS`/`detect_doc_type` to only academic_paper/textbook cues.

```python
OBJECT_SCHEMAS: Dict[str, ObjectSchema] = {
    "concept":   ObjectSchema(type="concept",   plural="concepts",   fields=["name", "section_path"], primary="name", description="a named entity (term/method/component/device/material)", list_fields=[]),
    "claim":     ObjectSchema(type="claim",     plural="claims",     fields=["name", "section_path"], primary="name", description="a truth-evaluable assertion about concepts", list_fields=[]),
    "formula":   ObjectSchema(type="formula",   plural="formulas",   fields=["name", "section_path"], primary="name", description="an equation / expression", list_fields=[]),
    "procedure": ObjectSchema(type="procedure", plural="procedures", fields=["name", "section_path"], primary="name", description="an ordered process / derivation / worked example", list_fields=[]),
}
OBJECT_TYPE_LABELS: Dict[str, str] = {
    "concept": "概念 Concept", "claim": "论断 Claim",
    "formula": "公式 Formula", "procedure": "过程 Procedure",
}
PROFILES: Dict[str, ExtractionProfile] = {
    "academic_paper": ExtractionProfile("academic_paper", "学术论文",
        ["concept", "claim", "formula", "procedure"], "an academic paper"),
    "textbook": ExtractionProfile("textbook", "教材 / 课本",
        ["concept", "claim", "formula", "procedure"], "a textbook / course material"),
}
DEFAULT_PROFILE_ID = "academic_paper"
```

- [ ] **Step 4: Run the full backend test suite**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/ -q`
Expected: the KG tests pass; legacy tests that import removed symbols now error — they are deleted in Task 9. Run `pytest tests/test_kg_repository.py tests/test_kg_ingest.py tests/kg -q` and expect PASS for these.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/extraction_profiles.py backend/tests/test_kg_repository.py
git commit -m "refactor: collapse profiles/schemas to KG types (concept/claim/formula/procedure)"
```

---

## Task 9: Delete qiefen / legacy / removed endpoints + tests

**Files:**
- Delete: `backend/app/services/qiefen/`, `backend/app/services/qiefen_ingest.py`, `backend/app/services/extraction.py`
- Delete tests: `backend/tests/test_qiefen_ingest.py`, `test_qiefen_registry.py`, `test_qiefen_cutover_integration.py`, `backend/tests/qiefen/`, plus any legacy-extraction / removed-type test modules.
- Modify: `backend/app/api/routes.py` (remove `scenario-query`, `list_rules/methods/risks/glossary`, removed-type schema endpoints as needed), and any imports of deleted modules.

- [ ] **Step 1: Find all references to deleted modules**

Run:
```bash
cd backend && grep -rn "qiefen\|from app.services.extraction import\|run_extraction\|CandidateRecord\|scenario_query\|list_rules\|list_methods\|list_risks\|list_glossary" app/ tests/
```
Expected: a finite list. Each hit must be removed or repointed.

- [ ] **Step 2: Delete modules + dead routes + dead tests**

```bash
cd backend
git rm -r app/services/qiefen app/services/qiefen_ingest.py app/services/extraction.py
git rm tests/test_qiefen_ingest.py tests/test_qiefen_registry.py tests/test_qiefen_cutover_integration.py
git rm -r tests/qiefen
```
Then edit `routes.py` to delete the `scenario-query`, `rules/methods/risks/glossary` endpoints and any `CaseCard/RuleCard`-only routes; delete corresponding `schemas.py` models only if unused elsewhere (grep first).

- [ ] **Step 3: Run the full suite**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/ -q`
Expected: PASS (no import errors). Fix any remaining references until green.

- [ ] **Step 4: Verify the app imports + starts**

Run: `cd backend && PYTHONPATH=. python -c "from app.api.routes import router; from app.main import app; print('ok')"`
Expected: prints `ok` with no ImportError.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete qiefen, legacy extractor, removed-type endpoints + tests"
```

---

## Task 10: Real-LLM end-to-end smoke (main session only)

> Subagent sandboxes have NO network to the deepseek endpoint — run this in the MAIN session, not a subagent.

**Files:** none (verification only)

- [ ] **Step 1: Pick two local source files**

Use `engram_paper_mineru.md` (academic) and the CMOS markdown (textbook) from `/Users/hzf/workspace/pdf_parser` (paths in `scripts/kg_goldgen_all.py`).

- [ ] **Step 2: Drive `_run_extraction` against a temp DB with the real client**

```bash
cd backend && PYTHONPATH=. OPENAI_COMPAT_MODEL=deepseek-v4-flash python - <<'PY'
import os, pathlib
from dotenv import load_dotenv
load_dotenv(pathlib.Path("..")/".env")
from app.services.sqlite_repository import SqliteRepository
from app.services.llm_client import make_default_client   # adjust to actual factory
repo = SqliteRepository(db_path=pathlib.Path("/tmp/kg_smoke.db"),
                        storage_dir=pathlib.Path("/tmp/kg_store"))
repo.llm_client = make_default_client()
nb = repo.create_notebook(title="smoke", template="academic_paper")
src = repo._test_insert_source(nb.id, title="Engram", file_name="engram_paper_mineru.md",
        doc_type="academic_paper",
        text=pathlib.Path("/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md").read_text())
repo._run_extraction(src.id)
g = repo.knowledge_graph(nb.id)
print("nodes", len(g.nodes), "edges", len(g.edges))
print("types", sorted({n.object_type for n in g.nodes}))
PY
```
Expected: non-zero nodes/edges; types ⊆ {concept, claim, formula, procedure}; every node headline non-empty.

- [ ] **Step 3: Assert evidence is grounded**

For 5 random objects, confirm each evidence `element_id` exists in `source_elements` and `quoted_span` is a substring of that element's text (print + eyeball).

- [ ] **Step 4: Record the result**

Append node/edge counts for engram + cmos to `fangan_todo.md` under "KG 重构 / 产品抽取流水线落地" as the landed-baseline.

- [ ] **Step 5: Commit**

```bash
git add fangan_todo.md
git commit -m "chore(kg): record product KG extraction baseline (engram + cmos smoke)"
```

---

## Self-Review Notes (for the implementer)

- **Idempotent re-extraction** (Task 5): the cleanest fix is a `source_id` column on `knowledge_objects`; do it in Task 5 and clear by source before re-storing. The placeholder DELETE must be replaced.
- **`list_knowledge` for KG types** depends on Task 8 registering `concept/claim/formula/procedure` schemas — Task 4/5 tests that read via `list_knowledge` only fully pass after Task 8; until then assert via raw row counts.
- **`make_default_client` / `llm_client` factory name** in Task 10 is illustrative — use the real client factory the repo already uses (grep `self.llm_client =`).
- **AskResponse consumers** (frontend) will break until the frontend plan; backend tests must not import the removed `AskResponse` fields.
