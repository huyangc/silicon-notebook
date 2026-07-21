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

## Review Follow-up

The initial Task 3 review identified cross-thread and privacy gaps. The follow-up remained within Task 3 and changed no SQL/write-lock behavior.

### Review RED

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_request_user_ctx.py -q
```

Result before review fixes: `15 failed, 57 passed, 1 warning in 7.73s`.

The failures demonstrated that:

- a real synchronous FastAPI route executing in AnyIO's worker thread remained reported as `http.dispatch` on the event-loop thread;
- pending notification ran after the job had already moved to `recent_jobs`;
- production labels exposed notebook/report/knowhow identifiers;
- nested notebook routes collapsed to `/api/notebooks/{id}`;
- the request-correlated production job still exposed its raw label.

Additional best-effort/privacy RED commands:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_background_jobs.py::test_diagnostic_label_derivation_cannot_block_a_named_job -q
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_background_jobs.py::test_failed_job_log_uses_safe_operation_instead_of_raw_entity_id -q
```

Each failed once for the expected missing guard (`1 failed in 0.34s` and `1 failed in 0.33s`).

### Review GREEN

Single-process focused command (explicitly exercises the real thread handoff without xdist scheduling):

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_request_user_ctx.py -q
```

Result: `75 passed in 1.94s`.

Existing deletion and composed-lifespan/API regressions:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_knowhow_asset_gc.py backend/tests/test_notebook_store_component.py backend/tests/test_multi_domain_bases.py backend/tests/test_repository_api_contract.py backend/tests/test_kg_search_api.py -q
```

Result: `98 passed in 4.45s`.

### Corrected Behavior

- Request phase overlays correlate a propagated request id across the event-loop/worker boundary. While a synchronous delete phase executes, the active request shows the worker thread id and exact `notebook_delete.db` or `notebook_delete.files` phase.
- Each phase overlay has an identity. Out-of-order scope exit removes only its own overlay; a still-active newer worker phase remains visible, and final exit restores the original request phase/thread.
- Application exceptions and ASGI task cancellation both remove the active request entry.
- Pending notification remains inside the active job lifetime. A callable failure still reaches `job_scope` as `error`, is logged once, and is swallowed by the existing fire-and-forget isolation boundary.
- Caller-visible `threading.Thread.name` behavior is preserved. Diagnostic job entries and failure logs use only allowlisted stable operations or a bounded callable-code identifier; production entity ids are not included.
- Notebook route normalization now preserves only allowlisted static route shape and `{id}` placeholders for dynamic positions, including source, Ask-cancel, report, and knowledge routes. `/api/notebooks/shared-by-me` is treated as a static collection route, not as a notebook id.
- Hidden phase-overlay bookkeeping is excluded from snapshots. No request body/query/header/auth data, source/model/Memory/Knowhow content, raw entity parameter, or raw filename was added.

## Second Review Follow-up

The second Task 3 review found incomplete live-route coverage and an overly broad callable-name fallback. No Task 4 behavior was changed.

### Second Review RED

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_diagnostics_runtime.py::test_every_registered_notebook_route_has_exact_safe_normalization backend/tests/test_diagnostics_runtime.py::test_unknown_notebook_suffix_fails_closed_without_claiming_root backend/tests/test_background_jobs.py -q
```

Result before fixes: `5 failed, 14 passed in 0.73s`.

The failures proved that:

- `/api/notebooks` was incorrectly normalized as `/api/{id}`;
- unmatched notebook suffixes misleadingly claimed the notebook-root route;
- explicit unallowlisted job names still derived and exposed callable names;
- an explicit name caused an arbitrary callable object's `__name__` property to execute.

The live-route contract also identified the missing registered shapes for notebook memories, answer-memory links, answer-to-memory creation, and analytics content overview.

### Second Review GREEN

Focused single-process command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_request_user_ctx.py -q
```

Result: `79 passed in 1.91s`.

Existing deletion and composed-lifespan/API regressions:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_knowhow_asset_gc.py backend/tests/test_notebook_store_component.py backend/tests/test_multi_domain_bases.py backend/tests/test_repository_api_contract.py backend/tests/test_kg_search_api.py -q
```

Result: `98 passed in 4.09s`.

Memory/content-overview/transfer route regressions:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_memory_api.py backend/tests/test_memory_transfer_routes.py backend/tests/test_content_overview_api.py backend/tests/test_knowhow_transfer_routes.py -q
```

Result: `38 passed in 3.43s`.

### Second Review Corrected Behavior

- A contract test enumerates the live FastAPI `app.routes` notebook surface, substitutes opaque values into every dynamic position, and requires the exact registered static template with `{id}` placeholders. Collection GET/POST remain `/api/notebooks`.
- The notebook route allowlist now includes `/memories`, `/answer-memory-links`, `/memories/from-answer`, and `/analytics/content-overview` alongside every other currently registered notebook route.
- Unknown notebook suffixes fail closed as `/api/notebooks/{id}/{redacted}` instead of impersonating notebook root. Their suffix content never enters the snapshot.
- Exact opaque notebook ids remain only in the dedicated machine-local `notebook_id` correlation field; every other dynamic route value is absent from snapshot metadata.
- Explicit allowlisted job names map to stable operations. Explicit unallowlisted names map directly to `background_job` without accessing the callable. Only unnamed ordinary Python functions/methods may contribute a bounded code-defined `__name__`; arbitrary callable objects are never introspected.
