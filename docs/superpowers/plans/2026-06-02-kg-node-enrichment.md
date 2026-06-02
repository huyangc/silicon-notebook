# KG Node Content Enrichment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Surface the already-captured rich content of KG nodes — the **containing sentence** for any node (via `evidence.element_id → source_elements.text`), a Concept's **definition** (via its `defines` Claim), and a Procedure's **complete ordered steps** (document-order within its section) — at read time, plus a small extraction-prompt nudge so future extractions cite sentence-level evidence and connect procedure steps with `precedes`.

**Architecture:** A read-only backend `node_context(notebook_id, object_id)` joins evidence to `source_elements.text`, resolves the defining Claim for Concepts, and assembles ordered Procedure steps. Exposed via a new `/objects/{id}/context` endpoint and folded into `concept_detail`. The frontend surfaces element text + steps. No schema change; existing data benefits immediately. A separate one-paragraph prompt change fixes the root cause for new docs.

**Tech Stack:** Python/FastAPI/SQLite (`sqlite_repository.py`), the existing KG tables, `kg/extract.py` prompt, the single `frontend/app/page.tsx`. Gates: `pytest` (backend), `tsc --noEmit` + `next build` (frontend).

**Spec:** `docs/superpowers/specs/2026-06-02-kg-node-enrichment-design.md`.

**Key data facts (verified live):** product `Evidence` rows = `{source_id, source_title, element_id, element_type, location_label, quoted_span, confidence}` (NO char offsets — order Procedure steps by `source_elements` document order, i.e. the natural `ORDER BY created_at ASC, id ASC` the repo already uses). `knowledge_relations` has `defines`(Claim→Concept) and `precedes`(Procedure→Procedure). `concept_detail` already returns `members`/`attached`/`evidence` but does NOT join element text.

---

## Task 1: Backend `node_context` (element-text + definition + procedure steps)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_node_context.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_node_context.py
import json, pytest, datetime
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

def _src_with_elements(repo, nb, texts):
    """Insert a sources row + source_elements (in document order); return element ids."""
    from uuid import uuid4
    sid = f"src-{uuid4().hex[:8]}"; now = datetime.datetime.now().isoformat()
    ids = []
    with repo._connect() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) VALUES (?,?,?,'markdown','extracted','parsed','d.md','',0,'','','academic_paper',?,?)", (sid, nb, "Doc", now, now))
        for i, t in enumerate(texts):
            eid = f"el-{uuid4().hex[:8]}"; ids.append(eid)
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) VALUES (?,?,?,?,?, '{}', ?)", (eid, sid, "paragraph", f"p{i}", t, f"{now}-{i:03d}"))
    return sid, ids

def test_node_context_concept_sentence_and_definition(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, [
        "As shown in Figure 1, Engram is a conditional memory module.",   # concept occurrence
        "Engram is defined as a structured store separating memory.",     # defining claim
    ])
    def ev(eid, span): return {"source_id": sid, "source_title": "Doc", "element_id": eid, "element_type": "paragraph", "location_label": "p", "quoted_span": span, "confidence": 1.0}
    repo.store_kg(nb.id, sid, [
        {"local_id":"c","object_type":"concept","payload":{"name":"Engram","section_path":"1"},"evidence":[ev(eids[0],"Engram")]},
        {"local_id":"k","object_type":"claim","payload":{"name":"Engram is a structured store","section_path":"1"},"evidence":[ev(eids[1],"Engram is defined as")]},
    ], [{"source_local_id":"k","target_local_id":"c","edge_type":"defines","evidence":[]}])
    cid = next(o["id"] for o in [_ for _ in [dict(r) for r in repo._connect().execute("SELECT id,object_type FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchall()]] if o["object_type"]=="concept")
    ctx = repo.node_context(nb.id, cid)
    assert ctx["object_type"] == "concept"
    assert "conditional memory module" in ctx["occurrences"][0]["element_text"]   # full sentence, not "Engram"
    assert "structured store" in (ctx["definition"] or "")                        # via defines claim

def test_node_context_procedure_steps_doc_order(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, [
        "First, we extract and compress suffix N-grams.",
        "Subsequently, embeddings are modulated by the hidden state.",
        "Finally, the result is refined via a lightweight convolution.",
    ])
    def ev(eid): return {"source_id": sid, "source_title": "Doc", "element_id": eid, "element_type": "paragraph", "location_label": "p", "quoted_span": "x", "confidence": 1.0}
    repo.store_kg(nb.id, sid, [
        {"local_id":"p2","object_type":"procedure","payload":{"name":"modulate","section_path":"2.2"},"evidence":[ev(eids[1])]},
        {"local_id":"p1","object_type":"procedure","payload":{"name":"extract","section_path":"2.2"},"evidence":[ev(eids[0])]},
        {"local_id":"p3","object_type":"procedure","payload":{"name":"refine","section_path":"2.2"},"evidence":[ev(eids[2])]},
    ], [])   # NOTE: no precedes edges -> must fall back to document order
    pid = next(r["id"] for r in repo._connect().execute("SELECT id FROM knowledge_objects WHERE notebook_id=? AND json_extract(payload,'$.name')='extract'", (nb.id,)).fetchall())
    ctx = repo.node_context(nb.id, pid)
    names = [s["name"] for s in ctx["steps"]]
    assert names == ["extract", "modulate", "refine"]                              # ordered by document position
    assert "suffix N-grams" in ctx["steps"][0]["element_text"]
```

- [ ] **Step 2: Run to verify FAIL** — `cd backend && PYTHONPATH=. python -m pytest tests/test_node_context.py -v` → FAIL (`node_context` missing).

- [ ] **Step 3: Implement** (place near `concept_detail` in `sqlite_repository.py`)

```python
def _element_texts(self, db, element_ids):
    ids = [e for e in element_ids if e]
    if not ids:
        return {}, {}
    ph = ",".join("?" for _ in ids)
    rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
    texts = {r["id"]: r["text"] for r in rows}
    # document order ordinal across the whole notebook's elements is the repo's
    # natural source_elements order (created_at ASC, id ASC).
    order_rows = db.execute(
        "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
        "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
        "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
        "ORDER BY se.created_at ASC, se.id ASC", (ids[0],)).fetchall()
    ordinal = {r["id"]: i for i, r in enumerate(order_rows)}
    return texts, ordinal

def _enrich_evidence(self, db, evidence):
    texts, _ = self._element_texts(db, [e.get("element_id") for e in evidence])
    out = []
    for e in evidence:
        out.append({"quoted_span": e.get("quoted_span", ""), "source_title": e.get("source_title", "") or e.get("source_id", ""),
                    "element_text": texts.get(e.get("element_id", ""), e.get("quoted_span", "")), "section_path": ""})
    return out

def node_context(self, notebook_id, object_id):
    self.get_notebook(notebook_id)
    with self._connect() as db:
        row = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE id=? AND notebook_id=?", (object_id, notebook_id)).fetchone()
        if row is None:
            raise KeyError(object_id)
        obj_type = row["object_type"]; payload = json.loads(row["payload"] or "{}")
        section = payload.get("section_path", "")
        occurrences = self._enrich_evidence(db, json.loads(row["evidence"] or "[]"))
        result = {"id": object_id, "object_type": obj_type, "name": payload.get("name", ""),
                  "section_path": section, "occurrences": occurrences, "definition": None, "steps": None}

        if obj_type == "concept":
            # definition = a Claim with a `defines` edge to this concept
            drow = db.execute(
                "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                "WHERE r.notebook_id=? AND r.target_object_id=? AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
            if drow is not None:
                dpay = json.loads(drow["payload"] or "{}")
                den = self._enrich_evidence(db, json.loads(drow["evidence"] or "[]"))
                result["definition"] = (den[0]["element_text"] if den else dpay.get("name", ""))

        if obj_type == "procedure":
            # steps = procedure nodes sharing this section_path, ordered by document position.
            prows = db.execute(
                "SELECT id, payload, evidence FROM knowledge_objects WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated'", (notebook_id,)).fetchall()
            steps = []
            for pr in prows:
                ppay = json.loads(pr["payload"] or "{}")
                if ppay.get("section_path", "") != section:
                    continue
                ev = json.loads(pr["evidence"] or "[]")
                texts, ordinal = self._element_texts(db, [e.get("element_id") for e in ev])
                first_eid = ev[0].get("element_id") if ev else ""
                steps.append({"name": ppay.get("name", ""), "element_text": texts.get(first_eid, ""),
                              "section_path": section, "_ord": ordinal.get(first_eid, 1_000_000)})
            steps.sort(key=lambda s: s["_ord"])
            for s in steps:
                s.pop("_ord", None)
            result["steps"] = steps
        return result
```

- [ ] **Step 4: Run to verify PASS** — `cd backend && PYTHONPATH=. python -m pytest tests/test_node_context.py -v` → PASS. Then no-regression `tests/test_unified_kg_repository.py tests/kg -q`.

- [ ] **Step 5: Commit** — `git add backend/app/services/sqlite_repository.py backend/tests/test_node_context.py && git commit -m "feat(kg): node_context (containing-sentence + concept definition + procedure steps)"`.

---

## Task 2: API endpoint + enrich `concept_detail`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` (concept_detail), `backend/app/api/routes.py`
- Test: `backend/tests/test_node_context.py`, `backend/tests/test_unified_kg_api.py`

- [ ] **Step 1: Write failing tests**

```python
# add to backend/tests/test_node_context.py
def test_concept_detail_includes_element_text(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, ["As shown, Engram is a conditional memory module."])
    ev = {"source_id": sid, "source_title": "Doc", "element_id": eids[0], "element_type": "paragraph", "location_label": "p", "quoted_span": "Engram", "confidence": 1.0}
    repo.store_kg(nb.id, sid, [{"local_id":"c","object_type":"concept","payload":{"name":"Engram","section_path":"1"},"evidence":[ev]}], [])
    repo.rebuild_unified_kg(nb.id)
    cid = list(repo.cluster_map(nb.id).values())[0]
    d = repo.concept_detail(nb.id, cid)
    assert any("conditional memory module" in (e.get("element_text") or "") for e in d["evidence"])
```
```python
# add to backend/tests/test_unified_kg_api.py
def test_object_context_endpoint_404_unknown(client):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # unknown object -> 404 (notebook exists, object doesn't)
    assert client.get(f"/api/notebooks/{nb}/objects/nope/context").status_code == 404
```

- [ ] **Step 2: Verify FAIL** — `cd backend && PYTHONPATH=. python -m pytest tests/test_node_context.py::test_concept_detail_includes_element_text tests/test_unified_kg_api.py::test_object_context_endpoint_404_unknown -v`.

- [ ] **Step 3: Implement**

In `concept_detail`, replace the `evidence = [ev for ...]` aggregation so each evidence item is enriched with `element_text`: after gathering member evidence list `evidence`, run `evidence = self._enrich_evidence(db, evidence)` (reuse the helper from Task 1; make sure it's called within a `with self._connect() as db` block — open one if `concept_detail` already closed its connection).

In `routes.py` add (matching the `repository()` accessor used by the other unified-kg routes):
```python
@router.get("/notebooks/{notebook_id}/objects/{object_id}/context")
def object_context(notebook_id: str, object_id: str):
    return repository().node_context(notebook_id, object_id)
```
`node_context` raises `KeyError` for an unknown object → the route layer maps it to 404 like the other routes (verify the file's KeyError→404 handling; if routes don't auto-map, wrap as the others do).

- [ ] **Step 4: Verify PASS** — the two tests + `cd backend && PYTHONPATH=. python -m pytest tests/ -q` (full suite green) + `PYTHONPATH=. python -c "from app.main import app; print('ok')"`.

- [ ] **Step 5: Commit** — `git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/ && git commit -m "feat(kg): /objects/{id}/context endpoint + element-text in concept_detail"`.

---

## Task 3: Extraction prompt nudge (sentence-level evidence + procedure `precedes`)

**Files:**
- Modify: `backend/app/services/kg/extract.py` (`_prompt`)
- Test: `backend/tests/kg/test_extract.py`

- [ ] **Step 1: Write/extend a test that the prompt mentions the two requirements** (a light guard — the prompt is a template):

```python
# add to backend/tests/kg/test_extract.py
from app.services.kg.extract import _prompt
def test_prompt_requests_sentence_evidence_and_precedes():
    p = _prompt("some passage", "1.1", "academic")
    low = p.lower()
    assert "sentence" in low                      # evidence should be the full sentence
    assert "precedes" in low and "step" in low    # connect ordered procedure steps
```

- [ ] **Step 2: Verify FAIL** — `cd backend && PYTHONPATH=. python -m pytest tests/kg/test_extract.py::test_prompt_requests_sentence_evidence_and_precedes -v`.

- [ ] **Step 3: Edit `_prompt`** — change the evidence instruction and add a procedure-chain instruction. Replace the line:
> `Every node and edge MUST include "evidence": an EXACT verbatim substring copied from the passage. Give each node a "local_id" you reuse in edges. "name" carries the node's text ... Skip narrative/filler.`

with:
```
Every node and edge MUST include "evidence": the EXACT verbatim **full sentence**
from the passage that contains the node (not a bare term). Give each node a
"local_id" you reuse in edges. "name" carries the node's text (Concept/Procedure
name, Claim statement, Formula expression). For an ordered Procedure, connect its
consecutive steps with `precedes` edges (step_i -> step_{i+1}). Skip narrative/filler.
```
Keep the JSON return template unchanged. Do NOT change the node schema.

- [ ] **Step 4: Verify PASS** — the new test + `cd backend && PYTHONPATH=. python -m pytest tests/kg -q` (no regression; the extraction tests use mock payloads and don't depend on prompt wording).

- [ ] **Step 5: Commit** — `git add backend/app/services/kg/extract.py backend/tests/kg/test_extract.py && git commit -m "feat(kg): prompt nudge — sentence-level evidence + procedure precedes chains"`.

---

## Task 4: Frontend — surface sentences + procedure steps

**Files:**
- Modify: `frontend/app/page.tsx`
- Gate: `cd frontend && npm run lint` (0 errors), then `npm run build`.

- [ ] **Step 1: Add a type + fetch for node context** (with the other unified-KG types/helpers added earlier)

```tsx
type NodeContext = { id: string; object_type: string; name: string; section_path: string; occurrences: { quoted_span: string; source_title: string; element_text: string }[]; definition: string | null; steps: { name: string; element_text: string }[] | null };
const fetchNodeContext = (nb: string, oid: string) => api<NodeContext>(`/notebooks/${nb}/objects/${encodeURIComponent(oid)}/context`);
```

- [ ] **Step 2: In the KG view right panel (concept detail), show the element_text** — the detail panel already maps `conceptDetail.evidence`. Change the evidence render to prefer `element_text` (the full sentence) over `quoted_span`:
```tsx
{conceptDetail.evidence.slice(0, 20).map((ev: any, i: number) => (
  <div className="kg-evidence" key={i}><span className="tag">{ev.source_title || ev.source_id}</span> <span>{ev.element_text || ev.quoted_span}</span></div>
))}
```
And if `conceptDetail` exposes a `definition` (after Task 2 you may also enrich concept_detail to include it — OR fetch via `fetchNodeContext(currentNotebookId, selectedConcept)` on select and render `nodeCtx.definition` + `nodeCtx.steps`). Implement the simplest path: on `selectConcept`, ALSO call `fetchNodeContext` and store it in a `nodeCtx` state; render `nodeCtx.definition` (if present) as a "定义" block and, for attached procedures, render their steps.

- [ ] **Step 3: Knowledge browser — show element_text for evidence + steps for procedures.** Find where the browser renders a `KnowledgeRecord`'s evidence (grep for the record/evidence rendering in the 知识库 list). For each evidence item, prefer the containing-sentence: fetch `node_context` for the opened record (or, if the browser already has the record's evidence, the quoted_span is what's there — to get element_text you must call `fetchNodeContext(nb, record.id)` when a record is expanded). Add an on-expand fetch that stores `NodeContext` for the open record and renders: occurrences' `element_text`, and for `object_type==="procedure"`, the ordered `steps` (each `name` + `element_text`).
  - Keep it minimal: a single "展开" affordance per record that loads + shows the context block. Match existing browser styling.

- [ ] **Step 4: Gate + commit** — `cd frontend && npm run lint` (0 errors) and `npm run build` (success). `git add frontend/app/page.tsx && git commit -m "feat(kg-viz): surface containing sentences + procedure steps in detail/browser"`.

---

## Task 5: Gates + real smoke (MAIN SESSION)

- [ ] **Step 1** — full backend suite `cd backend && PYTHONPATH=. python -m pytest tests/ -q` green; app imports.
- [ ] **Step 2 (main session, real LLM)** — re-extract one source (e.g. re-upload engram) and verify in the DB: (a) concept evidence `quoted_span` is now a fuller sentence; (b) the `precedes` edge count among procedures increased materially vs the old 1/12; (c) `node_context` on a concept returns the occurrence sentence + definition, and on a procedure returns ≥2 ordered steps with element_text.
- [ ] **Step 3** — browser check: open 知识图谱, click a concept → see its sentence + definition; open the 知识库 browser, expand a procedure → see ordered steps.
- [ ] **Step 4** — record the result in `fangan_todo.md`; commit.

---

## Self-Review notes (for the implementer)
- `_element_texts`/`_enrich_evidence` are read-only helpers — reuse across `node_context` and `concept_detail`; ensure each is called inside an open `_connect()` block.
- **Procedure ordering** uses `section_path` grouping + document order (the repo's natural `source_elements` order) because `precedes` is currently sparse; once Task 3's prompt lands and docs are re-extracted, `precedes` becomes reliable — a future refinement can prefer the `precedes` chain when present (note it, don't build it now).
- **No schema change**; existing data benefits from Tasks 1–2 immediately. Task 3 only affects newly-extracted docs.
- Frontend has no tests — `tsc` + `next build` + manual check are the gates.
