# Task 5 Report: Complete Concurrency-Gate Evidence

## Scope

Implemented Task 5 only:

- added live queued/waiting counters to both existing KG scheduler pools;
- made queued accounting safe across worker start, pre-start cancellation, submit
  failure, callable failure, and normal completion;
- verified the existing diagnostics provider writes bounded numeric `kg`, `llm`,
  and `embedding` dictionaries while all applicable gates have active and waiting
  work;
- preserved the separate window/job pools and submission-time
  `contextvars.copy_context()` propagation.

No offline analyzer, operator command, projection diagnostics group, product lock,
application-data scan, frontend surface, or content-bearing diagnostic field was
added. The knowhow `ProjectionScheduler` has no authoritative diagnostics snapshot
API and is intentionally outside the design allow-list; its existing debounce and
single-flight regressions were run as compatibility evidence only.

## TDD Evidence

### RED

After adding the queued-work, cancellation, submit-failure, and task-failure tests,
but before changing the scheduler, this command was run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_kg_scheduler.py backend/tests/test_model_concurrency.py -q
```

Result: `8 failed, 16 passed in 3.50s`.

Every failure was the intended missing contract:

- exact live snapshots lacked `window_waiting` and `job_waiting`;
- cancellation, submit-failure, and exception cleanup assertions raised
  `KeyError` for those absent keys;
- all pre-existing model-concurrency tests remained green.

### GREEN (focused)

The same command after the minimal scheduler implementation produced:

```text
24 passed in 2.12s
```

### GREEN (required concurrency suite)

Fresh final command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_kg_scheduler.py backend/tests/test_model_concurrency.py backend/tests/test_embed_concurrency.py backend/tests/test_diagnostics_runtime.py -q
```

Result: `83 passed in 3.01s`.

The runtime integration test blocks one active task and one waiter in both KG
pools, the LLM gate, and the embedding gate, then reads `runtime.json` and asserts
the exact numeric-only allow-listed snapshot. Cleanup assertions prove all active
and waiting counters return to zero.

### GREEN (projection single-flight and pool-report compatibility)

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_pool_report.py backend/tests/test_knowhow_editing_api.py::test_scheduler_coalesces_rapid_schedule_calls_to_at_most_two_runs backend/tests/test_knowhow_editing_api.py::test_scheduler_running_flag_defers_concurrent_schedule_to_one_rerun backend/tests/test_knowhow_editing_api.py::test_scheduler_different_tables_are_independent backend/tests/test_knowhow_projection.py::test_get_scheduler_entry_does_not_pin_repo -q
```

Result: `16 passed in 2.09s`.

### GREEN (existing KG scheduler consumers)

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_batch_ingest.py backend/tests/test_kg_build_circuit_breaker.py backend/tests/test_parallel_extraction_wiring.py backend/tests/test_kg_job_user_context.py backend/tests/test_kg_object_embed_concurrency.py backend/tests/test_kg_ingest.py backend/tests/kg/test_sa_extraction.py -q
```

Result: `154 passed in 3.79s`.

## Static Verification

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile backend/app/services/kg/scheduler.py backend/tests/test_kg_scheduler.py backend/tests/test_diagnostics_runtime.py
git diff --check
```

Result: both commands exited successfully with no output.

## Implementation and Safety Notes

- Each submission increments its pool's waiting counter before
  `ThreadPoolExecutor.submit()`.
- Under the existing counter lock, the worker marks its private ticket started,
  moves exactly one unit from waiting to active, and decrements active in
  `finally`.
- A done callback removes waiting only when a future was cancelled before its
  ticket started. The ticket prevents double-decrement if cancellation is
  observed repeatedly.
- A synchronous submit failure rolls back waiting before re-raising.
- `stats()` remains a fast bounded read of process-local counters and does not
  call `_ensure()`, scan work, or acquire pool lifecycle/product locks.
- Runtime output contains only finite numeric utilization metadata. It includes
  no raw identifiers, filenames, prompts, secrets, model input/output, notebook
  content, source content, or knowledge content.
- No Task 6 work was started.
