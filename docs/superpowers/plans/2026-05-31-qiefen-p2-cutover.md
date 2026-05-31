# qiefen P2 — Live Product Cutover Plan

**Goal:** Make the live ingestion run the qiefen pipeline for paper/textbook sources, so uploaded papers/textbooks produce qiefen typed objects (with atom-grounded evidence) that flow through the existing candidate → approve → knowledge_objects → browse/Q&A surfaces — replacing the old `run_extraction` for those document types.

**Seam (from the architecture map):** `process_source` (`sqlite_repository.py:754`) → `_run_extraction(source_id)` (`:843`/`:1002`) → `run_extraction(...)` → `CandidateRecord[]` → `extraction_candidates`. Approve (`:1253`) → `knowledge_objects`. Browse (`list_knowledge`/`knowledge_types`) and Q&A (`ask`/`score_knowledge`) are generic over `object_type` + `payload` + `evidence`. So if qiefen objects are written as candidates with the same shape, the whole downstream works unchanged.

## Design decisions

1. **Scope — paper/textbook only (non-breaking).** When the resolved profile is `academic_paper`/`article`→`article_research` or `textbook`, ingestion uses qiefen. Other doc types (design_spec/postmortem/review/general) keep the existing `run_extraction`. This delivers the qiefen cutover for its built domains without breaking rule/case notebooks.

2. **Source text bridge.** qiefen S1 needs markdown-ish text with char offsets. For `.md`/`.txt`, use the raw file at `source.file_path`. For PDF/docx (parsed to `SourceElement`s), **reconstruct** a markdown string from the stored elements (heading→`# t`, paragraph→`t`, formula→`$$ latex $$`, table→`metadata.table_html` or text, caption→`Figure …`). qiefen computes offsets into the text it is given, so the span invariant holds against the reconstructed text. Evidence shows `atom.raw_text` (real content) regardless.

3. **Adapter** `qiefen_objects_to_candidates(doc, source) -> List[CandidateRecord]`: for each qiefen object → `CandidateRecord(candidate_type=object.type, payload=object.payload, evidence=[Evidence per local_evidence atom])`. Each Evidence: `{source_id, source_title, element_id=atom.id, element_type=atom.atom_type, location_label=atom.section_path or line, quoted_span=atom.raw_text[:400], confidence=1.0}`. Relations attach as `payload.related_*`/a relations candidate is out of v1.

4. **Schema registry.** Add the qiefen object types (ArticleClaim, ArticleMethod, ScalingLaw, ExperimentResult, MechanisticExplanation, SystemDesignClaim, Limitation, Implication, ArchitectureComponent; Concept, Definition, Formula, Variable, Derivation, ExampleProblem, ExampleSolution, TechnologyProcess, ProcessFlow, ComponentModel, PhysicalEffect, DesignPrinciple, DesignRule, ProblemStatement) to `OBJECT_SCHEMAS` (so `effective_schemas`/`object_schemas` seed them) with their payload fields from `qiefen/profiles.py` + zh labels. This makes `knowledge_types` tabs + generic `list_knowledge` show them.

5. **No DB migration needed for v1** — reuse `extraction_candidates`/`knowledge_objects` (object_type is free text; payload/evidence are JSON). Atoms/chunks/relations are NOT persisted in v1 (only objects→candidates). A later v2 can add `q_atoms`/`q_relations` tables.

## Tasks

### P2-1: Register qiefen object types in the schema registry
- Modify `backend/app/services/extraction_profiles.py`: add the qiefen types to `OBJECT_SCHEMAS` (type, plural, fields from qiefen profiles, primary, description) + `OBJECT_TYPE_LABELS` (zh). Map profile `academic_paper`→article types, `textbook`→textbook types in the `PROFILES` object_type_keys.
- Test `backend/tests/test_schema_registry.py`: assert `OBJECT_SCHEMAS["ArticleClaim"].fields == ["statement","problem_addressed","novelty"]` and labels exist.
- Verify `effective_schemas()` (DB seed) includes them after migrate.

### P2-2: qiefen → CandidateRecord adapter
- Create `backend/app/services/qiefen_ingest.py` with:
  - `reconstruct_markdown(elements) -> str` (heading/paragraph/formula/table/caption → md).
  - `qiefen_doc_to_candidates(doc, source_id, source_title) -> List[CandidateRecord]` (objects → candidates with atom-grounded Evidence; atom lookup from `doc.evidence_atoms`).
- Test `backend/tests/test_qiefen_ingest.py` with a small QiefenDocument fixture: assert candidate_type/payload/evidence shapes; evidence.quoted_span == atom.raw_text; reconstruct_markdown round-trips a heading+paragraph+formula.

### P2-3: Wire ingestion
- Modify `_run_extraction` (`sqlite_repository.py`): resolve profile; if article_research/textbook AND llm configured → build source_text (raw for md, else reconstruct) → `qiefen.run(source_text, profile, client=llm)` → `qiefen_doc_to_candidates` → write to `extraction_candidates` (reuse existing candidate-writing code). Else → existing `run_extraction`.
- Keep the existing candidate row-writing block; only swap the records source.
- Manual/integration check: ingest one of the gold source files through `process_source` in a throwaway test DB; assert candidates rows have qiefen types + atom-grounded evidence.

### P2-4: Frontend sanity
- `frontend/app/page.tsx`: confirm candidate review + `knowledge_types` tabs + generic `list_knowledge` render qiefen types (they are generic). If the candidate card hard-codes rule/case fields, add a generic field renderer fallback. Minimal change only.

### P2-5: Verify end-to-end + document
- Run backend; upload a paper; confirm candidates appear, approve one, see it in knowledge browse with citation. Document the cutover + the paper/textbook scope in `architecture.md` or a cutover note.

## Out of scope (v2)
- Persisting atoms/chunks/relations as first-class tables.
- Q&A/retrieval semantic re-point beyond the generic `related_knowledge` path.
- Replacing extraction for non-paper/textbook doc types.
