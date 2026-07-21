# Task 3 report — neutral validation and first domain models

## Status

Complete. Commit: `refactor: establish core domain model boundaries` (the final identifier is recorded in the task handoff).

## Delivered files and categories

- Neutral validation: added `backend/app/core/memory_inputs.py` as the byte-preserving move of the former service implementation; deleted `backend/app/services/memory_inputs.py`.
- Domain ownership: added `common.py`, `identity.py`, `kg.py`, `notebooks.py`, `reports.py`, and `sources.py`; expanded the pre-existing `memory.py` with the moved Pydantic models and aliases while retaining `MemoryWrite` / `MemoryRevision`.
- Compatibility facade: changed `schemas.py` to explicitly import every moved legacy object and fixed `__all__` to the one-time `legacy_schema_exports.json` snapshot.
- Consumers: updated the Task-3 API, core, eval, repository/store, and service imports to their owner modules; the remaining schema imports in those files are for models deliberately not moved by this task.
- Tests: created the frozen legacy-export fixture and semantic ownership/identity test; updated Memory validation test imports.

## TDD evidence

Step 1 fixture freeze ran with the prescribed Homebrew Python command before definitions moved.

RED command:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 backend/tests/test_model_domain_boundaries.py
FF
AssertionError: common
AttributeError: module 'app.models.schemas' has no attribute '__all__'
```

GREEN command:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 backend/tests/test_model_domain_boundaries.py
3 passed
```

The first Step 7 run found one mechanical test-import indentation error in `test_memory_service.py`; its complete stack trace identified line 214 and the source was the moved local import. The smallest indentation-only correction reproduced green for that file (`30 passed`) before rerunning Step 7.

## Step 7 result

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_model_domain_boundaries.py \
  backend/tests/test_memory_api.py \
  backend/tests/test_memory_service.py \
  backend/tests/test_auth.py \
  backend/tests/test_url_sources_schemas.py \
  backend/tests/test_notebook_store_component.py \
  backend/tests/test_report_api.py

76 passed
```

## Boundary and facade proof

- `rg -n 'app\.services\.memory_inputs' backend` produced no matches after the move.
- The ownership test parses all seven domain files and rejects reverse imports from `app.api`, `app.services`, and `app.repositories`.
- `test_moved_legacy_exports_preserve_object_identity` checks every moved name with `is`; an independent final probe printed `facade object identity: OK`.
- `git diff --check` completed with no whitespace errors.

## Self-review and concerns

Reviewed every moved group against the task brief: no duplicate/subclass compatibility shims, no API route behavior changes, and `NotebookSummary` directly imports `KgBuildJobStatus` from `app.models.kg`. The only concern was the transient test indentation error caused by a local-import relocation; it was corrected and the exact focused suite is green. No dependency cycle was observed.
