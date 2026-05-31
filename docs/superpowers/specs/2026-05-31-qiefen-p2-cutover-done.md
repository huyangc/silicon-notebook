# qiefen P2 cutover — done

- Date: 2026-05-31
- Scope: paper/textbook sources only (confirmed). Other doc types keep the legacy extractor.

## What changed (live ingestion now runs qiefen for papers/textbooks)

| layer | change | file |
| --- | --- | --- |
| schema registry | 25 qiefen object types (ArticleClaim, Concept, Formula, …) + zh labels, generated from `qiefen/profiles.py` | `extraction_profiles.py` |
| adapter | `reconstruct_markdown(elements)` + `qiefen_doc_to_candidates(doc, source_id, title)` (objects → CandidateRecord with atom-grounded Evidence) | `qiefen_ingest.py` (new) |
| ingestion | `_run_extraction` → `_extract_records`: academic_paper/textbook + LLM configured → `qiefen.run(...)` → candidates; else legacy `run_extraction` | `sqlite_repository.py` |
| frontend | none needed — qiefen types render via the generic candidate-payload + `KnowledgeRecord` + `knowledge-types` path (only 4 legacy types have bespoke cards) | `page.tsx` |

## Data flow (unchanged downstream)

```
upload paper -> process_source -> _run_extraction
  -> (paper/textbook) reconstruct/raw text -> qiefen.run(client) -> objects
  -> qiefen_doc_to_candidates -> extraction_candidates (candidate_type=ArticleClaim/..., evidence=atom raw_text)
  -> approve_candidate -> knowledge_objects (object_type=ArticleClaim/...)
  -> list_knowledge / knowledge_types / ask(related_knowledge) [generic, unchanged]
```

## Verified

- Unit: `test_qiefen_registry.py`, `test_qiefen_ingest.py` (adapter shapes, evidence binding, markdown reconstruct).
- Integration (real DeepSeek, 33s): `test_qiefen_cutover_integration.py` — markdown paper through `process_source` yields qiefen-typed candidates with atom-grounded evidence, approvable into knowledge objects browsable by type.
- Full backend suite: 50 passed.

## Source-text bridge

qiefen S1 needs text + char offsets. `.md`/`.txt` → raw file (`source.file_path`). PDF/docx (already parsed to elements) → `reconstruct_markdown(elements)` (heading→`#`, formula→`$$`, table→`metadata.table_html`). qiefen computes offsets against that text; citations show real `atom.raw_text`.

## Not in this cutover (future v2)

- Persisting atoms/chunks/relations as first-class tables (only objects→candidates today).
- Q&A/retrieval semantic re-point beyond the existing generic `related_knowledge` path.
- qiefen for non-paper/textbook doc types (needs new profiles).
- Relations surfaced in the UI (extracted by the pipeline, not yet written to `knowledge_relations`).
