# Task 3 Report: Requests, Jobs, Readiness, and Delete Phases

## Scope

Implemented Task 3 only:

- activated the Task 2 diagnostics runtime around the existing FastAPI startup/warm-up lifespan;
- sampled readiness and numeric KG/model concurrency metadata;
- exposed in-flight HTTP dispatches through the existing request middleware;
- exposed successful and failed `background_jobs.submit()` work;
- named notebook deletion's existing database and filesystem phases.

No SQL/write-lock instrumentation, operator commands, frontend surface, product-data writes, or transaction/lock changes were added.

## TDD Evidence

### RED

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py -q
```

Result before production changes: `4 failed, 50 passed in 2.73s`.

Expected missing-hook failures:

- FastAPI lifespan left `diagnostics.current_runtime()` as `None`;
- blocked successful background job was absent from `active_jobs`;
- blocked failing background job was absent from `active_jobs`;
- notebook deletion snapshots had no `notebook_delete.db` / `notebook_delete.files` phase.

### GREEN (focused)

Same command after the minimal integration changes:

```text
54 passed in 2.68s
```

### GREEN (required request/readiness regression)

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_request_user_ctx.py -q
```

Result: `56 passed in 6.73s`.

### GREEN (existing notebook deletion regressions)

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_knowhow_asset_gc.py backend/tests/test_notebook_store_component.py backend/tests/test_multi_domain_bases.py -q
```

Result: `83 passed in 6.84s`.

### GREEN (existing composed-lifespan/API regressions)

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_repository_api_contract.py backend/tests/test_kg_search_api.py -q
```

Result: `15 passed in 5.24s`.

## Static Verification

Command:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile backend/app/main.py backend/app/services/background_jobs.py backend/app/services/notebook_catalog.py backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py
git diff --check
```

Result: both commands passed with no output.

## Safety and Compatibility Notes

- Runtime activation begins before `_lifespan` can create the startup warm-up thread and exits after that lifespan unwinds.
- Request instrumentation records only generated request id, method, normalized path, phase, timing, and thread metadata. It does not add headers, body, query, authorization, client, or content to runtime snapshots.
- Existing completion JSONL emission, latency/status calculation, exception propagation, and `X-Request-Id` response behavior are unchanged.
- The background-job scope surrounds the submitted callable so failures reach `job_scope` and become `status="error"`; the pre-existing outer catch still isolates/logs the exception, and pending notification remains in the existing `finally` block.
- Notebook deletion keeps its original ordering and boundary: the committed database deletion completes before source/asset filesystem cleanup begins.
- All diagnostics calls use Task 2's best-effort, exception-safe module wrappers. No Task 4 SQL or write-lock integration was started.
