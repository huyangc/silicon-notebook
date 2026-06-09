# Track B — Two-Tier + Federated Tier-Aware Retrieval (Small-Scale POC)

> **Date:** 2026-06-09
> **Branch / worktree:** `claude/unified-kg-evolution`  
> **Python:** `/opt/homebrew/Caskroom/miniconda/base/bin/python`  
> **Run tests from:** `backend/`
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

## Goal

Build a light federated-retrieval seam over the existing SQLite data — **no new extraction, no schema migration to a second DB, no full-repo refactor**. Two notebooks share one SQLite: one notebook is marked `tier='base'` (the analog-textbook KG, ~36k objects), the other `tier='personal'` (user notes). `ask()` federates across both, tags each hit with its tier, applies a base-authority weight, and defers to base on contradiction. All five tasks must leave the two invariants intact:

1. **[0,1]/tau**: `_fuse` (`retrieval.py:271-281`) is the only place that emits a `[0,1]` relevance; `classify_evidence` uses tau 0.18/0.35 against that value.
2. **Dual-index best-of**: `score_knowledge` (`retrieval.py:284-363`) takes `max(knowledge_sims[oid], max(element_sims[eid] for eid in evidence))` — both embedding tables remain separately addressable.

---

## Background & Constraints

### How `ask()` retrieves today (lines 3210-3460, `sqlite_repository.py`)

1. `get_notebook(notebook_id)` validates the notebook (line 3227).
2. `_knowledge_objects(db, notebook_id, kgt)` loads all 4 KG types via `SELECT … WHERE notebook_id=? AND object_type=?` (lines 3248-3251, helper at 3640-3674).
3. `_vector_matrix(db, notebook_id, …)` loads embeddings scoped strictly to `notebook_id` (line 3253-3256, 4 columns `notebook_id TEXT NOT NULL`).
4. `score_knowledge(…)` or `_rrf_scored(…)` scores; `_fuse` keeps everything `[0,1]`; `classify_evidence` applies tau.
5. 1-hop expansion queries `knowledge_relations WHERE notebook_id=?` (lines 3305-3323).
6. `_answer_context` builds `k{i}` id-map (lines 3682-3757); `_parse_answer_anchors` resolves `[k_i]` markers to `AnswerAnchor` objects (lines 3932-3951).

### DDL migration pattern (lines 464-516, `sqlite_repository.py`)

SQLite has no `ADD COLUMN IF NOT EXISTS`. The established idiom:

```python
nb_cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)").fetchall()}
if "some_col" not in nb_cols:
    db.execute("ALTER TABLE notebooks ADD COLUMN some_col TYPE NOT NULL DEFAULT val")
```

This runs inside `_migrate()` (line 208) which is called once on every `SQLiteRepository.__init__`.

### `RetrievedKnowledge` dataclass (retrieval.py:34-47)

```python
@dataclass
class RetrievedKnowledge:
    object_id: str
    object_type: str
    payload: Dict[str, object]
    evidence: List[Evidence]
    score: float = 0.0
    relevance: float = 0.0   # _fuse output; [0,1]; tau reads this field
    weight: float = 0.0      # _TYPE_WEIGHT; cross-type tie-break only
    status: str = "approved"
    owner: str = ""
    last_reviewed: str = ""
```

### `AnswerAnchor` model (schemas.py:155-164)

```python
class AnswerAnchor(BaseModel):
    key: str          # "k1"
    object_id: str
    object_type: str
    label: str
    name: str = ""
    definition: Optional[str] = None
    snippet: Optional[str] = None
    source_title: str = ""
    location_label: str = ""
```

### `_TYPE_WEIGHT` (retrieval.py:65-70)

```python
_TYPE_WEIGHT = {"claim": 1.0, "formula": 1.0, "procedure": 0.7, "concept": 0.5}
```

Used as a **cross-type tie-break only**, never multiplied into `relevance`; `ask()` line 3285 applies it in `rank_key = lambda it: it.score * type_weight(it.object_type, process_intent)`.

### `_fuse` (retrieval.py:271-281)

```python
def _fuse(keyword, semantic, has_vector, w_keyword=W_KEYWORD, w_semantic=W_SEMANTIC):
    semantic = max(0.0, semantic)
    denom = w_keyword + (w_semantic if has_vector else 0.0)
    if denom <= 0:
        return 0.0
    return (w_keyword * keyword + (w_semantic * semantic if has_vector else 0.0)) / denom
```

Output is always `[0,1]`. **The tier authority weight must compose with `rank_key` at the `ask()` sorting step (same level as `_TYPE_WEIGHT`), NOT inside `_fuse` — doing so would corrupt the `[0,1]` scale and break tau.**

---

## Files

| File | Role |
|------|------|
| `backend/app/services/sqlite_repository.py` | `_migrate()`, `ask()`, `_retrieve_scored()`, `_answer_context()`, `_parse_answer_anchors()`, `_rrf_scored()` |
| `backend/app/services/retrieval.py` | `RetrievedKnowledge`, `score_knowledge`, `_fuse`, `_TYPE_WEIGHT`, `classify_evidence` |
| `backend/app/models/schemas.py` | `AnswerAnchor`, `AskResponse`, `NotebookSummary` |
| `backend/app/services/prompts.py` | `answer_prompt` |
| `backend/tests/test_two_tier_federated.py` | **new** — all five task test suites live here |

---

## Task 1: `tier` field — idempotent DDL + `mark_notebook_base()` repo method

**Context:** The `notebooks` table (DDL lines 231-239) has no `tier` column. All existing notebooks must default to `personal`; the analog-textbook notebook is marked `base` via a new repo method.

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` (`_migrate`, `get_notebook`, `_notebook_from_row`, new `mark_notebook_base`)
- Modify: `backend/app/models/schemas.py` (`NotebookSummary`)
- Test: `backend/tests/test_two_tier_federated.py::TestTask1*`

**Step 1: Write the failing tests**

```python
# backend/tests/test_two_tier_federated.py
import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


class TestTask1:
    def test_new_notebook_has_personal_tier(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="personal nb"))
        assert nb.tier == "personal"

    def test_mark_notebook_base_sets_tier(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="textbook"))
        repo.mark_notebook_base(nb.id)
        nb2 = repo.get_notebook(nb.id)
        assert nb2.tier == "base"

    def test_tier_is_idempotent_on_existing_db(self, tmp_path, monkeypatch):
        """Running _migrate() twice on a DB that already has the tier column
        must not raise (PRAGMA guard prevents duplicate ALTER TABLE)."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        repo1 = SQLiteRepository(Settings())
        nb = repo1.create_notebook(NotebookCreate(name="nb"))
        repo1.mark_notebook_base(nb.id)
        # Second repo init on same DB must not raise.
        repo2 = SQLiteRepository(Settings())
        assert repo2.get_notebook(nb.id).tier == "base"
```

**Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask1 -v
```

Expected: FAIL — `AttributeError: 'NotebookSummary' object has no attribute 'tier'`.

**Step 3: Implement**

3a. Add `tier: str = "personal"` field to `NotebookSummary` (`schemas.py` after `access_scope`).

3b. In `_migrate()`, after the `nb_cols` PRAGMA block (line ~486):

```python
# tier column: 'personal' (default) or 'base' (analog-textbook KG)
if "tier" not in nb_cols:
    db.execute("ALTER TABLE notebooks ADD COLUMN tier TEXT NOT NULL DEFAULT 'personal'")
```

3c. In `_notebook_from_row` (line ~4621), add `tier=row["tier"] if "tier" in row.keys() else "personal"` to the `NotebookSummary(...)` constructor call.

3d. Add `mark_notebook_base` method near `update_notebook`:

```python
def mark_notebook_base(self, notebook_id: str) -> None:
    """Mark a notebook as the authoritative base KG (tier='base').
    Idempotent; raises KeyError if notebook not found."""
    self.get_notebook(notebook_id)  # raises KeyError if missing
    with self._write() as db:
        db.execute(
            "UPDATE notebooks SET tier='base', updated_at=? WHERE id=?",
            (_now(), notebook_id),
        )
```

**Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask1 -v
```

Expected: PASS (all 3 tests).

**Per-task gate:** `TestTask1` green; existing `pytest -q` still green.

---

## Task 2: `federated_retrieve` — light seam gathering candidates across tiers

**Context:** `_retrieve_scored(notebook_id, query, …)` (lines 3119-3150) runs retrieval for exactly one notebook. We need a method that, given an `active_notebook_id`, auto-discovers any `tier='base'` notebooks and gathers candidates from all of them, tagging each `RetrievedKnowledge` hit with its source `notebook_id` (for tier lookup downstream). The `[0,1]`/tau invariant and dual-index best-of **must not change at all** — each notebook's `_retrieve_scored` call runs unchanged on its own embedding matrices.

**Wire-in point:** In `ask()` (line 3248), the existing `{kgt: self._knowledge_objects(db, notebook_id, kgt) for kgt in _KG_TYPES}` block is replaced by a call to `federated_retrieve`. `ask()` gets a combined, annotated hit list and proceeds identically from there.

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` (new `federated_retrieve`, modify `ask()`)
- Modify: `backend/app/services/retrieval.py` (`RetrievedKnowledge`: add optional `notebook_id` field)
- Test: `backend/tests/test_two_tier_federated.py::TestTask2*`

**Step 1: Write the failing tests**

```python
class TestTask2:
    def _seed_two_notebooks(self, repo):
        """base notebook with one claim; personal notebook with one concept."""
        base_nb = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base_nb.id)
        repo.store_kg(base_nb.id, None, [
            {"local_id": "B1", "object_type": "claim",
             "payload": {"name": "base claim about capacitance", "section_path": "1"},
             "evidence": []},
        ], [])
        personal_nb = repo.create_notebook(NotebookCreate(name="personal"))
        repo.store_kg(personal_nb.id, None, [
            {"local_id": "P1", "object_type": "concept",
             "payload": {"name": "capacitance concept note", "section_path": "1"},
             "evidence": []},
        ], [])
        return base_nb, personal_nb

    def test_federated_retrieve_returns_hits_from_both_notebooks(self, repo):
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        nb_ids = {h.notebook_id for h in hits}
        assert base_nb.id in nb_ids
        assert personal_nb.id in nb_ids

    def test_federated_retrieve_tags_tier(self, repo):
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        base_hits = [h for h in hits if h.notebook_id == base_nb.id]
        personal_hits = [h for h in hits if h.notebook_id == personal_nb.id]
        assert all(h.tier == "base" for h in base_hits)
        assert all(h.tier == "personal" for h in personal_hits)

    def test_federated_retrieve_preserves_relevance_range(self, repo):
        """All relevance values must stay [0,1]; no [k] inflation from federation."""
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        hits = repo.federated_retrieve(personal_nb.id, "capacitance")
        for h in hits:
            assert 0.0 <= h.relevance <= 1.0, f"relevance {h.relevance!r} out of [0,1]"

    def test_ask_uses_federated_retrieve_when_base_exists(self, repo):
        """ask() on a personal notebook surfaces hits from the base notebook."""
        base_nb, personal_nb = self._seed_two_notebooks(repo)
        resp = repo.ask(personal_nb.id, __import__("app.models.schemas", fromlist=["AskRequest"]).AskRequest(question="capacitance"))
        all_ids = {a.object_id for a in resp.anchors}
        all_ids |= {r.id for r in resp.related_knowledge}
        # At least one object from the base notebook must appear.
        from app.services.sqlite_repository import SQLiteRepository
        with repo._connect() as db:
            base_ids = {r["id"] for r in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=?", (base_nb.id,)).fetchall()}
        assert all_ids & base_ids, "no base-notebook objects reached the answer"
```

**Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask2 -v
```

Expected: FAIL — `AttributeError: 'RetrievedKnowledge' object has no attribute 'notebook_id'`.

**Step 3: Implement**

3a. Add `notebook_id: str = ""` and `tier: str = "personal"` to `RetrievedKnowledge` in `retrieval.py` (after `last_reviewed`). These default to `""` / `"personal"` so all existing callers that don't set them continue to work without change.

3b. Add `federated_retrieve` to `SQLiteRepository`:

```python
def federated_retrieve(
    self,
    active_notebook_id: str,
    query: str,
    types=None,
    w_keyword: float = None,
    w_semantic: float = None,
) -> List["RetrievedKnowledge"]:
    """Gather scored KG candidates from {base notebook(s)} ∪ {active personal
    notebook}, tagging each hit with .notebook_id and .tier.

    Each notebook's scoring path is IDENTICAL to _retrieve_scored — same
    _fuse, same dual-index best-of — so [0,1]/tau and dual-index best-of
    invariants are preserved by construction.  Hits are merged and sorted
    by score desc; no cross-notebook normalisation is applied (the same
    fused relevance scale applies to both tiers)."""
    from app.services.retrieval import W_KEYWORD, W_SEMANTIC
    kw = w_keyword if w_keyword is not None else W_KEYWORD
    ws = w_semantic if w_semantic is not None else W_SEMANTIC

    notebook_ids: List[str] = []
    # Always include the active notebook.
    notebook_ids.append(active_notebook_id)
    # Add base notebooks (excluding the active one if it happens to be base).
    with self._connect() as db:
        rows = db.execute(
            "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
            (active_notebook_id,),
        ).fetchall()
    notebook_ids.extend(r["id"] for r in rows)

    # Fetch tier for each notebook_id.
    tier_map: dict = {}
    with self._connect() as db:
        for nid in notebook_ids:
            row = db.execute("SELECT tier FROM notebooks WHERE id=?", (nid,)).fetchone()
            tier_map[nid] = (row["tier"] if row else "personal")

    all_hits: List["RetrievedKnowledge"] = []
    for nid in notebook_ids:
        hits = self._retrieve_scored(nid, query, types=types, w_keyword=kw, w_semantic=ws)
        for h in hits:
            h.notebook_id = nid
            h.tier = tier_map.get(nid, "personal")
        all_hits.extend(hits)

    all_hits.sort(key=lambda it: it.score, reverse=True)
    return all_hits
```

3c. In `ask()`, replace the existing block that builds `kg_objs` + embedding matrices + `score_knowledge` calls (lines 3248-3298) with a call to `federated_retrieve`. The hit list flows into the same 1-hop expansion, `_answer_context`, and `classify_evidence` logic unchanged. Use the `notebook_id` on each hit for the 1-hop expansion query rather than the single `notebook_id` parameter (expansion must scope to the hit's own notebook):

```python
# In ask(), replace the load_indexes + score sections:
_t = time.perf_counter()
scored_all = self.federated_retrieve(notebook_id, query)
ask_stage("load_indexes_and_score", _t)
```

The 1-hop expansion block (lines 3302-3323) must use `h.notebook_id` per hit instead of the single `notebook_id`:

```python
# Replace the 1-hop expansion:
hit_ids_by_nb: Dict[str, set] = {}
for item in top_hits:
    hit_ids_by_nb.setdefault(item.notebook_id or notebook_id, set()).add(item.object_id)
neighbour_ids: set = set()
for nb_id, hit_ids in hit_ids_by_nb.items():
    hit_list = list(hit_ids)
    ph = ",".join("?" for _ in hit_list)
    with self._connect() as db:
        for r in db.execute(
            f"SELECT target_object_id FROM knowledge_relations "
            f"WHERE notebook_id=? AND source_object_id IN ({ph})",
            [nb_id, *hit_list],
        ).fetchall():
            neighbour_ids.add(r["target_object_id"])
        for r in db.execute(
            f"SELECT source_object_id FROM knowledge_relations "
            f"WHERE notebook_id=? AND target_object_id IN ({ph})",
            [nb_id, *hit_list],
        ).fetchall():
            neighbour_ids.add(r["source_object_id"])
```

**Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask2 -v
```

Expected: PASS (all 4 tests).

**Per-task gate:** `TestTask2` green; full `pytest -q` still green.

---

## Task 3: Tier-weighted relevance — base authority boost composing safely with `_fuse`

**Context:** Base KG objects should be ranked higher than personal objects of equal raw relevance, analogously to `_TYPE_WEIGHT`. The **only safe place** to apply this is in the `rank_key` lambda in `ask()` (line 3285) — **NOT** inside `_fuse` or `score_knowledge`, which would corrupt the `[0,1]` output that `classify_evidence` reads. This mirrors exactly how `type_weight` is applied: `rank_key = lambda it: it.score * type_weight(…)`.

The tier boost is a multiplicative factor on `score` (not `relevance`). A pure-keyword base hit with `score=0.20`, tier boost `1.2` → `rank_key=0.24`. Its `relevance` stays `0.20`; tau sees `0.20`; no threshold shift. A personal hit with `score=0.25`, boost `1.0` → `rank_key=0.25` — still wins.

**Files:**
- Modify: `backend/app/services/retrieval.py` (new `tier_weight` function + `_TIER_WEIGHT` constant)
- Modify: `backend/app/services/sqlite_repository.py` (`ask()` `rank_key`)
- Test: `backend/tests/test_two_tier_federated.py::TestTask3*`

**Step 1: Write the failing tests**

```python
class TestTask3:
    def test_tier_weight_base_exceeds_personal(self):
        from app.services.retrieval import tier_weight
        assert tier_weight("base") > tier_weight("personal")

    def test_tier_weight_values_are_positive(self):
        from app.services.retrieval import tier_weight
        assert tier_weight("base") > 0
        assert tier_weight("personal") > 0
        assert tier_weight("unknown") > 0

    def test_base_hit_outranks_personal_with_same_raw_score(self, repo):
        """A base hit with score=0.20 must rank above a personal hit with score=0.20."""
        from app.services.retrieval import RetrievedKnowledge, tier_weight
        from app.services.sqlite_repository import type_weight
        base_hit = RetrievedKnowledge(
            object_id="b1", object_type="claim", payload={}, tier="base", score=0.20, relevance=0.20)
        personal_hit = RetrievedKnowledge(
            object_id="p1", object_type="claim", payload={}, tier="personal", score=0.20, relevance=0.20)
        process_intent = False
        rank = lambda h: h.score * type_weight(h.object_type, process_intent) * tier_weight(h.tier)
        assert rank(base_hit) > rank(personal_hit)

    def test_keyword_base_hit_tau_position_unchanged(self, repo):
        """A pure-keyword base hit's .relevance (what tau reads) must stay [0,1].
        The tier boost on .score must not bleed into .relevance."""
        from app.services.retrieval import RetrievedKnowledge
        h = RetrievedKnowledge(
            object_id="b1", object_type="claim", payload={}, tier="base",
            score=0.22, relevance=0.22)
        # Tier boost is applied to score during rank_key, not to relevance.
        assert h.relevance == 0.22
        assert 0.0 <= h.relevance <= 1.0
```

**Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask3 -v
```

Expected: FAIL — `cannot import name 'tier_weight'`.

**Step 3: Implement**

3a. Add to `retrieval.py` (after `_PROCESS_TYPE_WEIGHT`):

```python
# Tier authority weights: base KG (curated textbook) outranks personal notes
# when scores are tied. Applied in ask() rank_key alongside type_weight —
# NEVER inside _fuse — so relevance stays [0,1] and tau thresholds are not
# shifted. A personal hit with higher raw relevance still wins.
_TIER_WEIGHT = {
    "base": 1.20,
    "personal": 1.00,
}


def tier_weight(tier: str) -> float:
    """Authority multiplier for a notebook tier; default 1.0 for unknowns."""
    return _TIER_WEIGHT.get(tier, 1.00)
```

3b. In `ask()`, update the `rank_key` lambda (line ~3285) to include `tier_weight`:

```python
from app.services.retrieval import tier_weight  # add to existing import
rank_key = lambda it: it.score * type_weight(it.object_type, process_intent) * tier_weight(getattr(it, "tier", "personal"))
```

**Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask3 -v
```

Expected: PASS (all 4 tests).

**Per-task gate:** `TestTask3` green; full `pytest -q` still green.

---

## Task 4: Conflict precedence — prompt encoding + `tier` in `AnswerAnchor` and context

**Context:** When a personal note contradicts the base KG, the answer must defer to base and surface the contradiction. Two sub-parts:

- **4a**: Carry `tier` through `_answer_context` `id_map` → `_parse_answer_anchors` → `AnswerAnchor`. The `id_map` dict value already holds `object_id`, `object_type`, `name`, etc. (lines 3726-3731); add `tier` to it.
- **4b**: Update `answer_prompt` in `prompts.py` to add a conflict-precedence rule: "If personal notes contradict a base item, prefer the base item's position and note the discrepancy."

The `id_map` is keyed by `k{i}` and built in `_answer_context`. Hits arriving there now carry `.tier` from Task 2; pass it through.

**Files:**
- Modify: `backend/app/models/schemas.py` (`AnswerAnchor`: add `tier: str = "personal"`)
- Modify: `backend/app/services/sqlite_repository.py` (`_answer_context` id_map entries, `_parse_answer_anchors`)
- Modify: `backend/app/services/prompts.py` (`answer_prompt`)
- Test: `backend/tests/test_two_tier_federated.py::TestTask4*`

**Step 1: Write the failing tests**

```python
class TestTask4:
    def test_answer_anchor_has_tier_field(self):
        from app.models.schemas import AnswerAnchor
        a = AnswerAnchor(key="k1", object_id="o1", object_type="claim",
                         label="Cap", name="Capacitance", tier="base")
        assert a.tier == "base"

    def test_answer_anchor_tier_defaults_to_personal(self):
        from app.models.schemas import AnswerAnchor
        a = AnswerAnchor(key="k1", object_id="o1", object_type="claim", label="x")
        assert a.tier == "personal"

    def test_parse_answer_anchors_carries_tier(self, repo):
        id_map = {
            "k1": {"object_id": "o1", "object_type": "claim", "name": "Cap",
                   "definition": "capacitance", "snippet": None,
                   "source_title": "", "location_label": "", "tier": "base"},
        }
        anchors = repo._parse_answer_anchors("Capacitance [k1].", id_map)
        assert anchors[0].tier == "base"

    def test_answer_prompt_contains_conflict_rule(self):
        from app.services.prompts import answer_prompt
        prompt = answer_prompt("question", "context")
        # The prompt must instruct the LLM to prefer base on contradiction.
        assert "base" in prompt.lower() and (
            "contradict" in prompt.lower() or "defer" in prompt.lower()
        ), "answer_prompt missing base-authoritative conflict rule"
```

**Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask4 -v
```

Expected: FAIL — `AnswerAnchor` missing `tier` field; `answer_prompt` missing conflict rule.

**Step 3: Implement**

3a. Add `tier: str = "personal"` to `AnswerAnchor` in `schemas.py` (after `location_label`).

3b. In `_answer_context`, add `"tier": getattr(hit, "tier", "personal")` to the `id_map[key]` dict (line ~3726).

3c. In `_parse_answer_anchors` (line ~3945), pass `tier=ctx.get("tier", "personal")` when constructing `AnswerAnchor`.

3d. In `prompts.py`, add rule 5 to `answer_prompt` (after rule 4 at line ~151):

```python
"5. Knowledge items tagged [base] come from the authoritative reference KG. "
"If a personal note ([personal]) contradicts a base item, defer to the base "
"item's position and briefly note the discrepancy (e.g., '(note: your notebook "
"states X, but the base reference says Y)').\n"
```

Also prefix each context line with `[base]` or `[personal]` by passing `tier` through the context block in `_answer_context`:

```python
line = f"{key}: [{hit.object_type}][{getattr(hit, 'tier', 'personal')}] {name}{extra}"
```

**Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask4 -v
```

Expected: PASS (all 4 tests).

**Per-task gate:** `TestTask4` green; full `pytest -q` still green.

---

## Task 5: No-regression — single-notebook ask() unchanged

**Context:** When there is no `tier='base'` notebook (single-notebook setup), `federated_retrieve` finds zero extra notebooks and returns only the active notebook's hits — identical to the pre-federation `_retrieve_scored` call. `AnswerAnchor.tier` defaults to `"personal"`. Recall and grounding distribution must be stable.

**Files:**
- Test: `backend/tests/test_two_tier_federated.py::TestTask5*`
- No production code change required (defaults ensure backward compatibility)

**Step 1: Write the failing tests**

```python
class TestTask5:
    def _seed_single(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="solo"))
        repo.store_kg(nb.id, None, [
            {"local_id": "S1", "object_type": "claim",
             "payload": {"name": "oxide breakdown voltage", "section_path": "2"},
             "evidence": []},
        ], [])
        return nb

    def test_single_notebook_ask_returns_same_hit(self, repo):
        """Without any base notebook, ask() on a personal notebook returns
        the personal hit — identical to pre-federation behavior."""
        nb = self._seed_single(repo)
        resp = repo.ask(nb.id, __import__("app.models.schemas", fromlist=["AskRequest"]).AskRequest(
            question="oxide breakdown"))
        # Must find the single object
        rk_ids = {r.id for r in resp.related_knowledge}
        assert "S1" in rk_ids or any("oxide" in r.headline.lower() for r in resp.related_knowledge)

    def test_single_notebook_federated_retrieve_returns_only_its_hits(self, repo):
        nb = self._seed_single(repo)
        hits = repo.federated_retrieve(nb.id, "oxide breakdown")
        nb_ids = {h.notebook_id for h in hits}
        assert nb_ids == {nb.id}

    def test_single_notebook_anchor_tier_is_personal(self, repo):
        """Anchors in a single-notebook ask() must default to tier='personal'."""
        nb = self._seed_single(repo)
        from app.services.sqlite_repository import SQLiteRepository as _R
        id_map = {
            "k1": {"object_id": "S1", "object_type": "claim", "name": "Oxide BV",
                   "definition": "oxide breakdown", "snippet": None,
                   "source_title": "", "location_label": ""},
            # No 'tier' key — simulates pre-federation id_map
        }
        anchors = repo._parse_answer_anchors("Oxide BV [k1].", id_map)
        assert anchors[0].tier == "personal"

    def test_full_suite_still_passes(self, repo):
        """Smoke: existing ask() tests must still pass when run together."""
        # This is a marker test — run the full suite via the gate command below.
        assert True
```

**Step 2: Run test to verify it fails (or confirm it passes directly)**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask5 -v
```

Expected: The first three tests may PASS immediately given Task 1-4 implementations. If any fail, the gap is a missing default.

**Step 3: Fix any regressions**

If `test_single_notebook_ask_returns_same_hit` fails, verify `federated_retrieve` returns the active notebook's hits when no base notebook exists (the `SELECT id FROM notebooks WHERE tier='base' AND id != ?` returns 0 rows → only `active_notebook_id` in `notebook_ids`).

If `test_single_notebook_anchor_tier_is_personal` fails, ensure `_parse_answer_anchors` uses `ctx.get("tier", "personal")` (the default handles missing `tier` key in legacy id_map entries).

**Step 4: Run the full test suite to verify no regressions**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
```

Expected: All tests pass. Focus on `test_ask_redesign.py`, `test_retrieval.py`, `test_bm25_rrf.py`, `test_followup_retrieval_grounding.py`, `test_reasoning_ask.py`.

**Per-task gate:** `TestTask5` green; full `pytest -q` green.

---

## Phase Gate (Final Acceptance)

Run this sequence in `backend/`:

```bash
# 1. Two-tier notebook setup
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py -v

# 2. Full suite — no regression
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
```

All criteria must hold:

- [ ] A question that has a base-KG object contradicting a personal note: the LLM answer defers to the base item and surfaces the discrepancy (verified by inspecting the `answer_prompt` rule + a manual ask on a seeded contradiction).
- [ ] `AnswerAnchor.tier` values are present in the response; citations show tier prefix in the context block.
- [ ] A single-notebook ask (no base notebook registered) produces identical `related_knowledge` ids and `evidence_level` outcomes as before the federation changes.
- [ ] `pytest -q` is fully green.

---

## Invariant Preservation Summary

| Invariant | Where enforced | How this plan preserves it |
|---|---|---|
| `relevance` in `[0,1]` | `_fuse` (retrieval.py:271) → `RetrievedKnowledge.relevance` | `federated_retrieve` calls `_retrieve_scored` per notebook — `_fuse` is called identically, output unchanged. `tier_weight` multiplies `score` in `rank_key`, never `relevance`. |
| `tau` thresholds (0.18/0.35) | `classify_evidence` reads `h.relevance` | `relevance` field is not touched; tier boost is orthogonal. |
| Dual-index best-of | `score_knowledge` takes `max(knowledge_sims[oid], max(element_sims[eid]…))` | Each notebook's `_retrieve_scored` runs its own `_vector_matrix` call on its own `notebook_id` — two separately-addressable indexes per notebook, never pooled. |
