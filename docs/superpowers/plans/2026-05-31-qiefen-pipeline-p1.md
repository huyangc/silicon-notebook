# qiefen Pipeline P1 (LLM objects / relations / mentions) Implementation Plan

> **For agentic workers:** execute task-by-task. Deterministic tasks (P1-0) follow TDD; LLM tasks lock plumbing with a mock client (deterministic unit tests) and tune prompt quality against the live harness.

**Goal:** Fill the 50%-of-weight extraction stages — typed `objects` (+payload +local_evidence), `relations`, `mentions` — via per-`ContextPackage` LLM calls against the configured DeepSeek endpoint, lifting the harness score above the 35.41 deterministic baseline.

**Architecture:** Add S6–S8 to `backend/app/services/qiefen/`. First refine the chunker (anchor boundaries) so a section becomes several small knowledge-unit chunks — this bounds each LLM call's input and lifts the chunk bucket. Then `objects.py`/`relations.py`/`mentions.py` each take a `client` (the existing `OpenAICompatibleClient`) and call `chat_json` with a profile-specific schema prompt; the package's atoms are sent WITH their ids so the model returns `local_evidence_atom_ids` chosen from them. The pipeline runs the LLM stages only when `client.configured`; offline it degrades to the P0 deterministic output.

**Tech Stack:** Python, pydantic v2, the project's `OpenAICompatibleClient` (DeepSeek `deepseek-v4-flash`, OpenAI-compatible JSON mode), pytest with a fake client for deterministic unit tests.

**Spec:** `docs/superpowers/specs/2026-05-31-qiefen-pipeline-design.md` §5. **Baseline:** `2026-05-31-qiefen-p0-baseline.md` (35.41/100).

**LLM is verified working:** base_url `https://api.deepseek.com`, model `deepseek-v4-flash`, `client.configured == True`, `chat_json` round-trips. `.env` is git-ignored.

---

## Testing discipline for LLM stages

LLM output is non-deterministic, so:
- **Plumbing (unit tests, offline):** every LLM stage function takes a `client` argument. Tests pass a `FakeClient` whose `chat_json(messages, schema_hint)` returns canned JSON. These assert: prompt contains the package atoms+ids; returned objects are parsed into `KnowledgeObjectQ`; `local_evidence_atom_ids` are filtered to ids present in the package (hallucinated ids dropped); types are constrained to the profile vocab; bad JSON degrades to `[]` not a crash.
- **Quality (live, against harness):** run `scripts/qiefen_score.py` with the real client on a small chapter set first (engram/ch00 + cmos/ch01), tune prompts, then full 14-chapter run. Prompt wording is tuned against the harness, like the P0.5 atom cues.

Cost control: P1-0 keeps each package small; iterate prompts on 2 chapters before full runs.

---

## File Structure

```
backend/app/services/qiefen/
  chunker.py        # MODIFY: anchor-based sub-section chunks (P1-0)
  profiles.py       # MODIFY: gold object + relation type vocab + payload templates (P1-1)
  llm_extract.py    # NEW: shared prompt builder + JSON parsing + a FakeClient-friendly seam
  objects.py        # NEW: S7 objects (P1-2)
  relations.py      # NEW: S8 relations (P1-3)
  mentions.py       # NEW: S6 mentions + canonicalization (P1-4)
  pipeline.py       # MODIFY: run LLM stages when client.configured; backfill expected_objects (P1-5)
backend/tests/qiefen/
  test_chunker.py        # MODIFY
  test_objects.py        # NEW (FakeClient)
  test_relations.py      # NEW (FakeClient)
  test_mentions.py       # NEW (FakeClient)
  test_llm_extract.py    # NEW (FakeClient)
```

---

## Task P1-0: Anchor-based chunker refinement (deterministic)

**Goal:** split a section's atom run into several knowledge-unit chunks instead of one, so (a) each chunk/package is small (bounds LLM input) and (b) chunk-set Jaccard vs gold improves.

**Boundary rule (within a same-section atom run), start a new chunk when:**
- the atom_type changes *category* (prose ↔ formula ↔ table ↔ example/problem), OR
- a structural anchor atom appears (`formula_atom`, `table_header_atom`, `example_problem_atom`, `problem_statement_atom`), keeping that anchor with the atoms that immediately follow it (formula+explanation, header+rows, example stem+steps), OR
- the running chunk reaches a size cap (`MAX_ATOMS_PER_CHUNK = 12`).
Keep formula+following-prose, table header+rows, example+steps together (do NOT split those).

**Files:** Modify `backend/app/services/qiefen/chunker.py`; Modify `backend/tests/qiefen/test_chunker.py`.

- [ ] **Step 1: Add a failing test** for sub-section splitting — append to `test_chunker.py`:

```python
def test_section_splits_into_units_by_anchor_and_cap():
    from app.services.qiefen.chunker import build_chunks, MAX_ATOMS_PER_CHUNK
    # 14 prose atoms then a formula then prose -> not one chunk.
    atoms = [_atom(f"P{i}", "SEC1", "concept_definition_atom") for i in range(14)]
    atoms += [_atom("F1", "SEC1", "formula_atom"),
              _atom("E1", "SEC1", "concept_definition_atom")]
    chunks = build_chunks(atoms, "textbook", {"SEC1": "1 > 1.1"})
    assert len(chunks) >= 2               # cap + anchor forced splits
    for c in chunks:
        assert len(c.atom_ids) <= MAX_ATOMS_PER_CHUNK
    # every atom still in exactly one chunk
    seen = [a for c in chunks for a in c.atom_ids]
    assert sorted(seen) == sorted(a.id for a in atoms)
    assert MAX_ATOMS_PER_CHUNK == 12
```

- [ ] **Step 2:** Run `cd backend && python -m pytest tests/qiefen/test_chunker.py -q` → FAIL (no `MAX_ATOMS_PER_CHUNK`, one chunk).

- [ ] **Step 3:** Rewrite the run-accumulation loop in `build_chunks` to flush on (a) section change (existing), (b) category change, (c) anchor-after-content, (d) `len(run) >= MAX_ATOMS_PER_CHUNK`. Add `MAX_ATOMS_PER_CHUNK = 12` and a `_category(atom_type)` helper mapping types to {prose, formula, table, example, problem}. Keep `chunk_type` assignment from the dominant atom_type (existing `_chunk_type`). Preserve the "every atom in exactly one chunk" invariant.

- [ ] **Step 4:** Run the full suite `cd backend && python -m pytest tests/qiefen -q` → all pass (update any chunk test that assumed one-chunk-per-section).

- [ ] **Step 5:** Re-score: `PYTHONPATH=backend python scripts/qiefen_score.py --out harness_out/qiefen_pred` and record the `semantic_chunks` mean delta. Commit:
```bash
git add backend/app/services/qiefen/chunker.py backend/tests/qiefen/test_chunker.py
git commit -m "feat(qiefen): anchor-based sub-section chunks (P1-0)"
```

---

## Task P1-1: Profiles — gold object/relation vocab + payload templates

**Files:** Modify `backend/app/services/qiefen/profiles.py`.

- [ ] **Step 1:** Add, per profile, the object type list, a per-type ordered payload-field list (from gold `objects[].payload` keys), and the relation type list. Concretely:

```python
ARTICLE_OBJECTS = {
    "ArticleClaim": ["statement", "problem_addressed", "novelty"],
    "ArticleMethod": ["name", "description", "mechanism", "scale"],
    "ArchitectureComponent": ["name", "role", "mechanism"],
    "ScalingLaw": ["name", "statement", "governs"],
    "ExperimentSetup": ["setup", "controls", "metric"],
    "ExperimentResult": ["setup", "finding", "metric", "before", "after"],
    "AblationFinding": ["component", "finding", "evidence"],
    "MechanisticExplanation": ["mechanism", "explains"],
    "SystemDesignClaim": ["claim", "mechanism", "benefit"],
    "Limitation": ["statement"],
    "Implication": ["statement"],
}
ARTICLE_RELATIONS = [
    "method_has_component", "component_mitigates_risk", "method_addresses_problem",
    "result_supports_claim", "experiment_tests_claim",
    "ablation_supports_component_importance", "mechanism_explains_result",
    "system_design_enables_efficiency", "claim_guided_by_scaling_law",
]
TEXTBOOK_OBJECTS = {
    "Concept": ["term", "definition", "contrasts_with"],
    "Definition": ["term", "definition"],
    "Formula": ["name", "expression", "variables", "applies_to"],
    "Variable": ["symbol", "meaning"],
    "Derivation": ["name", "from", "to", "steps"],
    "ExampleProblem": ["title", "problem", "given"],
    "ExampleSolution": ["title", "approach", "result"],
    "TechnologyProcess": ["name", "purpose"],
    "ProcessFlow": ["name", "steps"],
    "ComponentModel": ["name", "properties"],
    "PhysicalEffect": ["name", "description"],
    "DesignPrinciple": ["statement", "rationale", "applies_to"],
    "DesignRule": ["statement", "condition"],
    "ProblemStatement": ["statement"],
}
TEXTBOOK_RELATIONS = [
    "concept_defines_term", "concept_contrasts_with_concept", "formula_defines_variable",
    "formula_depends_on_variable", "formula_derived_from_formula", "formula_used_in_example",
    "example_uses_formula", "process_flow_has_step", "process_step_precedes_step",
    "circuit_block_composed_of_block", "component_has_property",
    "design_principle_applies_to_scenario",
]

def object_types(profile): return ARTICLE_OBJECTS if profile=="article_research" else TEXTBOOK_OBJECTS
def relation_types(profile): return ARTICLE_RELATIONS if profile=="article_research" else TEXTBOOK_RELATIONS
```
Keep the existing `extraction_targets(profile)` (used by chunks/packages); set it to `list(object_types(profile))`.

- [ ] **Step 2:** Add a unit test `test_profiles.py` asserting `object_types("article_research")["ArticleClaim"]` and `relation_types("textbook")` contain expected entries, and `extraction_targets` returns the object-type names. Run it, commit:
```bash
git add backend/app/services/qiefen/profiles.py backend/tests/qiefen/test_profiles.py
git commit -m "feat(qiefen): gold object/relation vocab + payload templates (P1-1)"
```

---

## Task P1-2: Objects stage (LLM, mock-tested)

**Files:** New `backend/app/services/qiefen/llm_extract.py`, `objects.py`; New `test_llm_extract.py`, `test_objects.py`.

- [ ] **Step 1 — FakeClient + failing test** (`test_objects.py`):

```python
import json
from app.services.qiefen.models import ContextPackage
from app.services.qiefen.objects import extract_objects


class FakeClient:
    configured = True
    def __init__(self, payload): self._payload = payload
    def chat_json(self, messages, schema_hint):
        self.last_prompt = messages[-1]["content"]
        return json.dumps(self._payload)


def _pkg():
    return ContextPackage(id="PKG-1", profile="article_research", chunk_id="C1",
                          section_path="Abstract", document_title="Engram",
                          atoms=[{"atom_id": "A1", "atom_type": "claim_sentence"},
                                 {"atom_id": "A2", "atom_type": "method_sentence"}])


def test_objects_parsed_and_evidence_filtered_to_package():
    fake = FakeClient({"objects": [
        {"type": "ArticleClaim", "payload": {"statement": "conditional memory complements MoE"},
         "local_evidence_atom_ids": ["A1", "A99"]},          # A99 hallucinated -> dropped
        {"type": "NotAType", "payload": {"x": "y"}, "local_evidence_atom_ids": ["A2"]},  # bad type -> dropped
    ]})
    objs = extract_objects(fake, _pkg(), "article_research")
    assert len(objs) == 1
    o = objs[0]
    assert o.type == "ArticleClaim"
    assert o.local_evidence_atom_ids == ["A1"]               # A99 filtered
    assert o.home_package == "PKG-1"
    assert o.section_path == "Abstract"
    # prompt actually carried the package atom ids
    assert "A1" in fake.last_prompt and "A2" in fake.last_prompt


def test_bad_json_degrades_to_empty():
    class Boom:
        configured = True
        def chat_json(self, m, s): return "not json{"
    assert extract_objects(Boom(), _pkg(), "article_research") == []
```

- [ ] **Step 2:** Run → FAIL (modules missing).

- [ ] **Step 3 — `llm_extract.py`:** a prompt builder `build_object_prompt(pkg, profile)` that renders document title, section path, the allowed object types + their payload fields (from profiles), the package atoms as `[A1|claim_sentence] <text?>` lines (atom text is not in ContextPackage; include just id+type — the model grounds on ids), and instructions: only listed types, choose `local_evidence_atom_ids` ONLY from the listed atom ids, every payload field supported, return `{"objects":[{type,payload,local_evidence_atom_ids,supporting_context_atom_ids?}]}`. Plus `safe_json(raw)` returning `{}` on parse error.

  NOTE: ContextPackage carries atom ids+types but not atom text. Add an `atom_text` lookup param to `extract_objects(client, pkg, profile, atom_text=None)` so the pipeline can pass `{atom_id: raw_text}`; the prompt includes the text when available (greatly improves quality). Tests omit it (id+type only).

- [ ] **Step 4 — `objects.py`:** `extract_objects(client, pkg, profile, atom_text=None) -> List[KnowledgeObjectQ]`: build prompt, `client.chat_json(...)`, `safe_json`, for each item: drop if `type` not in `object_types(profile)`; filter `local_evidence_atom_ids` to the package's atom ids; set `home_package=pkg.id`, `section_path=pkg.section_path`; id `f"OBJ-{pkg.id}-{i}"`. Return list.

- [ ] **Step 5:** Run `test_objects.py` + `test_llm_extract.py` → PASS. Commit:
```bash
git add backend/app/services/qiefen/llm_extract.py backend/app/services/qiefen/objects.py \
        backend/tests/qiefen/test_objects.py backend/tests/qiefen/test_llm_extract.py
git commit -m "feat(qiefen): S7 LLM object extraction with evidence filtering (P1-2)"
```

---

## Task P1-3: Relations stage (LLM, mock-tested)

**Files:** New `relations.py`, `test_relations.py`.

- [ ] **Step 1 — failing test** (`test_relations.py`): a `FakeClient` returns `{"relations":[{relation_type, source_object_id, target_object_id, evidence_atom_ids}]}`. `extract_relations(client, objects, profile, atom_text=None)` must: drop relations whose endpoints aren't in the provided object ids; drop relation_types not in `relation_types(profile)`; filter `evidence_atom_ids` to atoms present on the endpoint objects' evidence; assign `id=f"R-{i}"`. Assert a relation with a hallucinated endpoint is dropped and a valid one is kept with type preserved.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3:** Implement `build_relation_prompt(objects, profile)` (lists object ids+types+identity payload + allowed relation types) and `extract_relations(...)` with the filtering above. One call per chapter over all its objects (not per package), so cross-package relations are possible.

- [ ] **Step 4:** Run → PASS. Commit:
```bash
git add backend/app/services/qiefen/relations.py backend/tests/qiefen/test_relations.py
git commit -m "feat(qiefen): S8 LLM relation extraction over chapter objects (P1-3)"
```

---

## Task P1-4: Mentions + canonicalization (LLM, mock-tested)

**Files:** New `mentions.py`, `test_mentions.py`.

- [ ] **Step 1 — failing test:** `FakeClient` returns `{"mentions":[{text,type,atom_id,canonical_key}]}`. `extract_mentions(client, pkg, profile) -> List[Mention]` filters `atom_id` to the package atoms, keeps `type` free (mentions are scored on (atom_id, normalized text), not type). Assert hallucinated atom_id dropped. (Canonicalization can be a thin deterministic group-by-`canonical_key` returning the `canonicalization` list; test that aliases group.)

- [ ] **Step 2–4:** Implement, pass, commit:
```bash
git add backend/app/services/qiefen/mentions.py backend/tests/qiefen/test_mentions.py
git commit -m "feat(qiefen): S6 mentions + canonicalization (P1-4)"
```

---

## Task P1-5: Wire into pipeline + backfill + live baseline

**Files:** Modify `pipeline.py`; Modify `test_pipeline.py` (offline path unchanged); New `docs/superpowers/specs/2026-05-31-qiefen-p1-baseline.md`.

- [ ] **Step 1:** Extend `run(...)` with an optional `client=None`. After packages are built: if `client is not None and client.configured`: build `atom_text = {a.id: a.raw_text for a in atoms}`; for each package call `extract_objects` (+`extract_mentions`); collect objects; set each package's `expected_objects = [o.id for o in objs homed there]`; call `extract_relations` once over all chapter objects; attach objects/relations/mentions/canonicalization to the document. If no client, leave them empty (current P0 behavior). Add a module-level helper `default_client()` building `OpenAICompatibleClient(Settings())` so the script can pass a real one.

- [ ] **Step 2:** Offline test: `test_pipeline.py` still passes (run with `client=None`). Add a test that passes a `FakeClient` and asserts `doc.objects` is populated and `context_packages[*].expected_objects` references real object ids.

- [ ] **Step 3:** `scripts/qiefen_score.py`: build a real client via `default_client()` and pass it to `run(...)`. Add `--chapters` filter (comma-separated `doc/chapter`) so iteration can run a subset (cost). Default = all.

- [ ] **Step 4 — live iterate (cost-aware):** run on two chapters first:
`PYTHONPATH=backend python scripts/qiefen_score.py --out harness_out/qiefen_pred --chapters engram/ch00_abstract,cmos/ch01_introduction`. Inspect `objects/relations` columns; tune prompts in `llm_extract.py`. Repeat until objects/relations are non-trivial.

- [ ] **Step 5 — full run + record:** run all 14 chapters once, write `2026-05-31-qiefen-p1-baseline.md` (mean, per-stage means incl objects/payload/relations, per-chapter, token/cost note, prompt-tuning notes). Commit code + baseline (NOT `harness_out/`).
```bash
git add backend/app/services/qiefen/pipeline.py scripts/qiefen_score.py \
        backend/tests/qiefen/test_pipeline.py docs/superpowers/specs/2026-05-31-qiefen-p1-baseline.md
git commit -m "feat(qiefen): wire LLM stages into pipeline + P1 live baseline (P1-5)"
```

---

## Self-Review notes
- Spec coverage: §5 objects (P1-2), relations (P1-3), mentions/canon (P1-4), profiles vocab (P1-1), package-size control via chunk refinement (P1-0), pipeline wiring + expected_objects backfill (P1-5).
- Determinism: every LLM stage takes `client`; unit tests use `FakeClient` (offline, deterministic). Hallucinated ids/types are filtered in code, not trusted from the model.
- Cost: P1-0 caps chunk size; `--chapters` enables 2-chapter iteration before the full run.
- The offline pipeline path (`client=None`) is unchanged, so P0 tests and the deterministic baseline remain valid.
