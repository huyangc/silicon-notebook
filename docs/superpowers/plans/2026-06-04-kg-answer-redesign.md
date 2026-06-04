# KG Answer Redesign Implementation Plan (Phases 0–2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/ask` ground what the notebook covers (with clickable per-sentence citations), reason forward where it doesn't (clearly labelled as inference), and never dead-end with a canned refusal.

**Architecture:** Backend retrieval recall tidy-up (Phase 0) → answer synthesis redesign where the LLM tags grounded sentences with stable ids `[k1]`, leaves inferred sentences unmarked + self-labelled, and the backend resolves ids → citation anchors (Phase 1) → frontend renders the marked answer with clickable popovers (Phase 2). Spec: `docs/superpowers/specs/2026-06-04-kg-answer-redesign-design.md` (decisions D1–D5 confirmed).

**Tech Stack:** FastAPI + SQLite repo (`backend/app/services/sqlite_repository.py`), retrieval scoring (`backend/app/services/retrieval.py`), prompts (`backend/app/services/prompts.py`), Pydantic schemas (`backend/app/models/schemas.py`), Next.js single-file frontend (`frontend/app/page.tsx`). Tests: pytest (`backend/tests/`), all LLM/embeddings mocked.

**Run from:** ROOT checkout `/Users/hzf/workspace/silicon_notebook` on `master` (per service-restart-prefs). Backend test gate: `cd backend && python -m pytest -q`. Frontend gate: `cd frontend && npm run lint`.

---

## File Structure (what changes and why)

- `backend/app/services/retrieval.py` — scoring. Add stopword filtering to `keyword_score`; no structural split.
- `backend/app/services/sqlite_repository.py` — `ask()` (retrieval orchestration + synthesis). Drop scenario from query; global top-N ranking; build id-tagged enriched context; parse `[k_i]` → anchors; ungrounded fallback. Add a private `_answer_context()` helper and `_parse_answer_anchors()` helper next to `ask`.
- `backend/app/services/prompts.py` — new `answer_prompt` + `ANSWER_SCHEMA_HINT` (markers + inference rule).
- `backend/app/models/schemas.py` — extend `AskResponse` (+`answer`, `grounded`, `anchors`), add `AnswerAnchor` model.
- `frontend/app/page.tsx` — render `answer` with clickable `[k_i]` chips + popover from `anchors`.
- Tests: `backend/tests/test_retrieval.py` (new or existing), `backend/tests/test_ask_redesign.py` (new).

Reference reading before starting: `ask()` at `sqlite_repository.py:2560`, `_answer_with_llm_kg` at `:2710`, `score_knowledge` at `retrieval.py:208`, `node_context` at `sqlite_repository.py:1961`, constants `_KG_TYPES`/`_TOP_PER_TYPE` at `sqlite_repository.py:114-117`, `_TYPE_WEIGHT`/`RELEVANCE_FLOOR`/`W_KEYWORD` at `retrieval.py:28-83`.

---

# PHASE 0 — Retrieval recall tidy-up

### Task 0.1: Drop `scenario` from the ask query (D-§4-11)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — `ask()` (~2560-2596)
- Test: `backend/tests/test_ask_redesign.py` (new)

- [ ] **Step 1: Write the failing test** (uses the repo fixture pattern from `tests/test_unified_kg_repository.py` — temp DB + injected `FakeEmbedder`, `EMBED_PROVIDER=dashscope`). Inject a fake `llm_client` capturing the prompt.

```python
# backend/tests/test_ask_redesign.py
import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest

class FakeLLM:
    configured = True
    def __init__(self): self.last_prompt = None
    def chat_json(self, messages, schema_hint):
        self.last_prompt = messages[0]["content"]
        return json.dumps({"answer": "Engram is a memory module [k1].", "grounded": True})

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    r.llm_client = FakeLLM()
    return r

def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"}, "evidence": []},
    ], [])
    return nb

def test_ask_query_excludes_scenario(repo):
    nb = _seed(repo)
    repo.ask(nb.id, AskRequest(question="what is engram", scenario={"domain": "ZZZUNIQUE"}))
    # scenario value must NOT leak into the retrieval/answer prompt
    assert "ZZZUNIQUE" not in (repo.llm_client.last_prompt or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_ask_redesign.py::test_ask_query_excludes_scenario -v`
Expected: FAIL (scenario currently concatenated into `query` and reaches the prompt).

- [ ] **Step 3: Implement** — in `ask()`, replace the scenario-weaving block (the `scenario_tags`/`query = " ".join([question, *scenario_tags])` lines) with `query = question.strip()`. Remove the `scenario` argument passed into `score_knowledge(...)` (pass `None`) and stop computing `scenario_tags`. Leave `AskRequest.scenario` field in place (accepted but ignored) for frontend back-compat.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_ask_redesign.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_redesign.py
git commit -m "feat(ask): query = question only; ignore legacy scenario in retrieval"
```

### Task 0.2: Stopword filtering in `keyword_score` (D2)

**Files:**
- Modify: `backend/app/services/retrieval.py` — `keyword_score` (~152) + add `_STOPWORDS`
- Test: `backend/tests/test_retrieval.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_retrieval.py
from app.services.retrieval import keyword_score

def test_keyword_score_ignores_stopwords():
    # Verbose phrasing must not dilute the score: only content tokens count.
    concise = keyword_score("engram", "Engram is a memory module")
    verbose = keyword_score("what is engram and what are its problems", "Engram is a memory module")
    assert concise == 1.0
    assert verbose >= 0.9   # stopwords (what/is/and/are/its) dropped -> "engram"/"problems" basis
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_retrieval.py::test_keyword_score_ignores_stopwords -v`
Expected: FAIL (verbose ≈ 1/8, far below 0.9).

- [ ] **Step 3: Implement** — add a module-level stopword set and filter query tokens in `keyword_score`:

```python
_STOPWORDS = {
    # en
    "the","a","an","is","are","was","were","be","of","to","in","on","for","and",
    "or","what","which","how","why","its","it","this","that","these","those","do",
    "does","with","as","by","at","from","has","have","can","you","your","i","we",
    # zh (function words)
    "的","了","是","有","和","与","它","这","那","什么","怎么","哪些","以及","并",
    "吗","呢","在","对","把","及","或",
}

def keyword_score(query: str, text: str) -> float:
    query_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    if not query_tokens:
        return 0.0
    haystack = set(_tokens(text))
    hits = sum(1 for token in query_tokens if token in haystack)
    return hits / len(query_tokens)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_retrieval.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_retrieval.py
git commit -m "feat(retrieval): stopword-filter query tokens so verbose questions aren't diluted"
```

### Task 0.3: Global top-N ranking with type soft-prior (D3)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — `ask()` top-hits selection (~2585-2594); add module const `_TOP_N = 12`
- Test: `backend/tests/test_ask_redesign.py` (append)

- [ ] **Step 1: Write the failing test** — seed many claims + few concepts; assert selection is by global relevance×type-weight, not a fixed per-type quota (e.g. >5 claims can be returned if they are the most relevant).

```python
def test_ask_global_topn_not_fixed_quota(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"M{i}", "object_type": "claim",
             "payload": {"name": f"engram claim number {i}", "section_path": "1"}, "evidence": []}
            for i in range(8)]
    repo.store_kg(nb.id, None, objs, [])
    resp = repo.ask(nb.id, AskRequest(question="engram claim", scenario={}))
    claim_hits = [r for r in resp.related_knowledge if r.object_type == "claim"]
    assert len(claim_hits) > 5   # old code capped claims at _TOP_PER_TYPE=5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_ask_redesign.py::test_ask_global_topn_not_fixed_quota -v`
Expected: FAIL (capped at 5 by `_TOP_PER_TYPE`).

- [ ] **Step 3: Implement** — replace the per-type `[: _TOP_PER_TYPE...]` slicing. Score each type (still call `score_knowledge` per type so the right vectors are used), collect ALL hits into one list, sort globally by `item.score * _TYPE_WEIGHT[type]`, take the top `_TOP_N`:

```python
from app.services.retrieval import _TYPE_WEIGHT  # add to imports
_TOP_N = 12
...
scored_all = []
for t in _KG_TYPES:
    objs = kg_objs[t]
    if not objs: continue
    scored_all.extend(score_knowledge(query, objs, t, query_vector, element_vectors, knowledge_vectors, None))
scored_all.sort(key=lambda it: it.score * _TYPE_WEIGHT.get(it.object_type, 0.5), reverse=True)
top_hits = scored_all[:_TOP_N]
```
Remove the now-unused `_TOP_PER_TYPE` only if no other reference (grep first; keep otherwise). `related_knowledge` cap stays at 12.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_ask_redesign.py -q`
Expected: PASS. Then full suite `python -m pytest -q` (no regressions).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_redesign.py
git commit -m "feat(ask): global top-N ranking with type weight as soft prior (drop fixed per-type quota)"
```

---

# PHASE 1 — Answer synthesis redesign

### Task 1.1: `AnswerAnchor` schema + extend `AskResponse`

**Files:**
- Modify: `backend/app/models/schemas.py` — add `AnswerAnchor`, extend `AskResponse`
- Test: `backend/tests/test_ask_redesign.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_askresponse_has_answer_and_anchors():
    from app.models.schemas import AskResponse, AnswerAnchor
    a = AnswerAnchor(key="k1", object_id="o1", object_type="concept", label="Engram", name="Engram")
    r = AskResponse(conclusion="x", answer="Engram [k1].", grounded=True, anchors=[a])
    assert r.answer == "Engram [k1]." and r.grounded and r.anchors[0].key == "k1"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_ask_redesign.py::test_askresponse_has_answer_and_anchors -v` → FAIL (no `AnswerAnchor`).

- [ ] **Step 3: Implement** in `schemas.py`:

```python
class AnswerAnchor(BaseModel):
    key: str                 # "k1" — matches [k1] marker in answer text
    object_id: str
    object_type: str
    label: str               # short display token (KG name, clipped)
    name: str = ""
    definition: Optional[str] = None
    snippet: Optional[str] = None      # element_text of the grounding sentence
    source_title: str = ""
    location_label: str = ""
```
Add to `AskResponse`: `answer: str = ""`, `grounded: bool = False`, `anchors: List[AnswerAnchor] = Field(default_factory=list)`. Keep existing fields. (Ensure `Optional` is imported.)

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/schemas.py backend/tests/test_ask_redesign.py
git commit -m "feat(schema): AnswerAnchor + AskResponse.answer/grounded/anchors"
```

### Task 1.2: New `answer_prompt` + schema hint (D1 inference rule)

**Files:**
- Modify: `backend/app/services/prompts.py` — `answer_prompt`, `ANSWER_SCHEMA_HINT` (~71-88)
- Test: `backend/tests/test_prompts.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_prompts.py
from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
import json

def test_answer_prompt_states_marker_and_inference_rules():
    p = answer_prompt("q?", "k1: [concept] Engram — def: ...")
    assert "[k1]" in p or "[k_i]" in p              # marker convention present
    assert "推断" in p or "inference" in p.lower()   # inference must be self-labelled
    assert "k1: [concept] Engram" in p               # context block embedded
    assert "answer" in ANSWER_SCHEMA_HINT and "grounded" in ANSWER_SCHEMA_HINT
```

- [ ] **Step 2: Run** `python -m pytest tests/test_prompts.py::test_answer_prompt_states_marker_and_inference_rules -v` → FAIL.

- [ ] **Step 3: Implement** — change `answer_prompt` signature to `answer_prompt(question: str, context_block: str) -> str` (drop `scenario_block`) and `ANSWER_SCHEMA_HINT = '{"answer":"","grounded":true}'`:

```python
ANSWER_SCHEMA_HINT = '{"answer":"","grounded":true}'

def answer_prompt(question: str, context_block: str) -> str:
    return (
        "You answer an engineer's question using the notebook knowledge below, "
        "and you may reason beyond it.\n"
        "Rules:\n"
        "1. When a sentence uses a knowledge item, append its id marker like [k1] "
        "(multiple allowed: [k1][k3]) at the end of that sentence.\n"
        "2. When a sentence is your own inference (not supported by the items), do "
        "NOT add any [k] marker, and make clear it is your reasoning (e.g. prefix "
        "with '（推断）' / 'Likely,'). Never attach a marker to an unsupported claim.\n"
        "3. If the items don't cover the question, still answer from general "
        "knowledge and set grounded=false; otherwise grounded=true.\n"
        "4. Answer in the question's language. Be concrete.\n\n"
        f"Question: {question}\n\n"
        f"Knowledge items (id: [type] name — context):\n{context_block}\n\n"
        'Return JSON only: {"answer":"<text with [k] markers>","grounded":true|false}'
    )
```

- [ ] **Step 4: Run** → PASS. (Also run the existing prompts/llm tests; fix any caller of the old `answer_prompt(question, scenario_block, context_block)` — only `_answer_with_llm_kg` calls it, updated in Task 1.4.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prompts.py backend/tests/test_prompts.py
git commit -m "feat(prompts): answer prompt with [k] grounding markers + inference-labelling rule"
```

### Task 1.3: `_parse_answer_anchors` — resolve `[k_i]` → anchors

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — add helper near `ask`
- Test: `backend/tests/test_ask_redesign.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_parse_answer_anchors_keeps_only_cited(repo):
    # id_map: k1->ctx dict; only markers present in text become anchors
    id_map = {
        "k1": {"object_id": "o1", "object_type": "concept", "name": "Engram",
               "definition": "a memory module", "snippet": "Engram is ...",
               "source_title": "paper", "location_label": "2.1"},
        "k2": {"object_id": "o2", "object_type": "claim", "name": "unused",
               "definition": None, "snippet": None, "source_title": "", "location_label": ""},
    }
    anchors = repo._parse_answer_anchors("Engram is a module [k1]. Improving it is open.", id_map)
    keys = {a.key for a in anchors}
    assert keys == {"k1"}                    # k2 not cited -> excluded
    assert anchors[0].label == "Engram"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_ask_redesign.py::test_parse_answer_anchors_keeps_only_cited -v` → FAIL (no method).

- [ ] **Step 3: Implement** the helper on `SQLiteRepository`:

```python
import re
_MARKER_RE = re.compile(r"\[(k\d+)\]")

def _parse_answer_anchors(self, answer: str, id_map: dict) -> list:
    from app.models.schemas import AnswerAnchor
    cited = []
    seen = set()
    for key in _MARKER_RE.findall(answer or ""):
        if key in seen or key not in id_map:
            continue
        seen.add(key)
        ctx = id_map[key]
        name = str(ctx.get("name", ""))
        cited.append(AnswerAnchor(
            key=key, object_id=ctx["object_id"], object_type=ctx["object_type"],
            label=(name[:40] or key), name=name,
            definition=ctx.get("definition"), snippet=ctx.get("snippet"),
            source_title=ctx.get("source_title", ""), location_label=ctx.get("location_label", ""),
        ))
    return cited
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_redesign.py
git commit -m "feat(ask): _parse_answer_anchors resolves [k] markers to citation anchors"
```

### Task 1.4: `_answer_context` (id-tagged enriched context) + concept cluster dedup (D4) + rewire `ask`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — add `_answer_context`, rewrite `_answer_with_llm_kg` + the synthesis section of `ask`
- Test: `backend/tests/test_ask_redesign.py` (append)

- [ ] **Step 1: Write the failing test** (grounded answer carries anchors; ungrounded fallback still answers).

```python
def test_ask_grounded_answer_has_anchors(repo):
    nb = _seed(repo)   # one concept "Engram"
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}))
    assert resp.grounded is True
    assert resp.answer and "[k1]" in resp.answer
    assert any(a.object_type == "concept" for a in resp.anchors)
    assert resp.conclusion and "[k1]" not in resp.conclusion   # conclusion = markers stripped

def test_ask_ungrounded_when_no_hits(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    repo.llm_client.chat_json = lambda m, s: __import__("json").dumps(
        {"answer": "（推断）Engram is likely a memory mechanism.", "grounded": False})
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}))
    assert resp.llm_mode == "ungrounded"
    assert "not yet contain approved knowledge" not in resp.conclusion   # no canned dead-end
    assert resp.answer
```

- [ ] **Step 2: Run** `python -m pytest tests/test_ask_redesign.py -k "grounded or ungrounded" -v` → FAIL.

- [ ] **Step 3: Implement.**
  (a) Add `_answer_context(self, top_hits) -> tuple[str, dict]` building the id-tagged block + id_map. For each hit assign `k{i}`; pull enrichment via existing `node_context(notebook_id, object_id)` (definition/first occurrence snippet/steps). Concept dedup (D4): collapse hits whose concept belongs to the same unified cluster — look up cluster membership (reuse the unified-KG `concept_clusters` lookup the repo already has, e.g. the canonical id used in `concept_detail`); keep the first/highest-scored per cluster, drop later duplicates before assigning ids. Block line format: `k1: [concept] Engram — def: <definition or first sentence>` (procedures append `; steps: s1 -> s2`).

```python
def _answer_context(self, notebook_id, top_hits):
    lines, id_map = [], {}
    seen_concept_clusters = set()
    i = 0
    for hit in top_hits:
        if hit.object_type == "concept":
            cid = self._concept_cluster_id(notebook_id, hit.object_id)  # canonical or object_id
            if cid in seen_concept_clusters:
                continue
            seen_concept_clusters.add(cid)
        try:
            ctx = self.node_context(notebook_id, hit.object_id)
        except KeyError:
            continue
        i += 1
        key = f"k{i}"
        name = str(hit.payload.get("name", "")).strip()
        occ = ctx.get("occurrences") or []
        snippet = occ[0].get("element_text") if occ else ""
        definition = ctx.get("definition") or snippet
        extra = f" — def: {definition[:200]}" if definition else ""
        if ctx.get("steps"):
            extra += "; steps: " + " -> ".join(s.get("name","") for s in ctx["steps"][:8])
        lines.append(f"{key}: [{hit.object_type}] {name}{extra}")
        id_map[key] = {"object_id": hit.object_id, "object_type": hit.object_type,
                       "name": name, "definition": definition, "snippet": snippet,
                       "source_title": (occ[0].get("source_title","") if occ else ""),
                       "location_label": (occ[0].get("section_path","") if occ else "")}
    return ("\n".join(lines) if lines else "(none)"), id_map
```
Add `_concept_cluster_id(self, notebook_id, object_id)` returning the canonical cluster id if the object is a clustered concept, else `object_id` (reuse the same query `concept_detail`/`rebuild_unified_kg` uses against `concept_clusters`; if no cluster row, return `object_id`).

  (b) Rewrite `_answer_with_llm_kg` → `_answer_kg(self, notebook_id, question, top_hits) -> tuple[str, bool, str, list]` returning `(answer_text, grounded, llm_mode, anchors)`:
```python
def _answer_kg(self, notebook_id, question, top_hits):
    context_block, id_map = self._answer_context(notebook_id, top_hits)
    raw = self.llm_client.chat_json(
        [{"role": "user", "content": answer_prompt(question, context_block)}], ANSWER_SCHEMA_HINT)
    data = json.loads(raw)
    answer = str(data.get("answer", "")).strip()
    grounded = bool(data.get("grounded", False)) and bool(top_hits)
    anchors = self._parse_answer_anchors(answer, id_map)
    return answer, grounded, ("grounded" if grounded else "ungrounded"), anchors
```
  (c) In `ask()`: always call `_answer_kg` when `self.llm_client.configured` (drop the `has_knowledge or scored_elements` gate). Set `conclusion = _MARKER_RE.sub("", answer).strip()` (back-compat, markers stripped). Populate `response.answer`, `response.grounded`, `response.anchors`. Keep `related_knowledge` + element-level `citations` as today. Deterministic fallback (no LLM configured) stays: `answer=""`, `conclusion=` existing canned string, `llm_mode="deterministic"`.

- [ ] **Step 4: Run** `python -m pytest tests/test_ask_redesign.py -q` then full `python -m pytest -q`. Expected: PASS, no regressions. (Update/remove the old `_answer_with_llm_kg` test if one exists.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_redesign.py
git commit -m "feat(ask): groundable+reasoned synthesis — enriched id-tagged context, [k] anchors, ungrounded fallback, concept cluster dedup"
```

---

# PHASE 2 — Frontend inline citations

### Task 2.1: Render `answer` with clickable `[k_i]` chips + anchor popover

**Files:**
- Modify: `frontend/app/page.tsx` — `AskResponse` type (~95), the answer render block (~3027-3090)
- Gate: `cd frontend && npm run lint`

- [ ] **Step 1: Add types** — extend the `AskResponse` type with `answer: string; grounded: boolean; anchors: AnswerAnchor[];` and add:
```ts
type AnswerAnchor = { key: string; object_id: string; object_type: string; label: string;
  name: string; definition?: string | null; snippet?: string | null;
  source_title: string; location_label: string; };
```

- [ ] **Step 2: Implement render** — replace `<p>{answer.conclusion}</p>` (line ~3035) with a renderer that splits `answer.answer` on `/\[(k\d+)\]/`, maps each `k_i` to `answer.anchors`, and renders matched markers as a clickable chip (e.g. `<button class="cite-chip">{anchor.label}</button>`) that toggles a popover showing `name / definition / snippet / source_title · location_label`. Non-marker text renders as plain spans. Fall back to `answer.conclusion` when `answer.answer` is empty (deterministic mode). When `answer.grounded === false`, show a small "未基于笔记本来源" badge near the answer.

```tsx
function renderAnswer(a: AskResponse) {
  const text = a.answer || a.conclusion || "";
  if (!a.answer) return <p>{text}</p>;
  const byKey = Object.fromEntries(a.anchors.map((x) => [x.key, x]));
  const parts = text.split(/(\[k\d+\])/g);
  return (
    <p>
      {!a.grounded && <span className="tag">未基于笔记本来源</span>}
      {parts.map((seg, i) => {
        const m = seg.match(/^\[(k\d+)\]$/);
        const anc = m && byKey[m[1]];
        if (!anc) return <span key={i}>{seg}</span>;
        return <CiteChip key={i} anchor={anc} />;   // button + popover (definition/snippet/source)
      })}
    </p>
  );
}
```
Add a small `CiteChip` component (local state `open`, renders `anchor.label`, popover with `definition`/`snippet`/`source_title · location_label`). Keep the existing `related_knowledge` + `citations` blocks below.

- [ ] **Step 3: Verify** — `cd frontend && npm run lint` (0 errors). Manual eyeball: ask "engram是什么，有哪些优点和问题，怎么改进" → grounded sentences show clickable chips; clicking shows evidence; inference sentences have no chip; empty-coverage question shows the "未基于来源" badge with a general answer.

- [ ] **Step 4: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): render answer with clickable [k] citation chips + anchor popovers"
```

---

## Self-Review (against the spec)

- **Spec coverage:** §2 provenance mechanism → Tasks 1.2/1.3/2.1. §3 Phase 0 → Tasks 0.1–0.3. Phase 1 → Tasks 1.1–1.4. Phase 2 → Task 2.1. U1 grounded+inference → 1.4/2.1; U2 popover → 2.1; U3 ungrounded fallback → 1.4; U4 multi-turn → **out of scope (Phase 3, deferred per D5)**. D1 (inference rule) → 1.2; D2 (stopwords) → 0.2; D3 (global top-N) → 0.3; D4 (concept cluster dedup) → 1.4; D5 (phasing) → this plan stops at Phase 2.
- **Verification:** every backend task is TDD with concrete mocked tests (no network); frontend gated by lint + manual eyeball. After all tasks: real-LLM smoke in the main session (subagents have no network) — re-ask the engram question on the live root server and confirm grounded chips + ungrounded fallback.
- **Watch-item for the implementer:** confirm the exact `concept_clusters` lookup used by `concept_detail`/`rebuild_unified_kg` when writing `_concept_cluster_id` (read those methods first); if clustering isn't populated for a notebook, fall back to `object_id` (no dedup) — must not crash.

## Non-goals (this plan)
Multi-turn conversation (Phase 3), graph multi-hop reasoning, FTS5/BM25, any local model. Extraction/KG build unchanged.
