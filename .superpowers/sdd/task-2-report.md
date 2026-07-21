# Task 2 report: process-local diagnostics runtime

## Scope

- Added `backend/app/core/diagnostics_runtime.py`.
- Added `backend/tests/test_diagnostics_runtime.py`.
- Did not wire FastAPI, background jobs, SQLite, or any Task 3+ call sites.

## Strict TDD evidence

### RED

Command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py -q
```

Result: exit `1`, expected collection error because production code did not yet
exist.

```text
ImportError: cannot import name 'diagnostics_runtime' from 'app.core'
ERROR backend/tests/test_diagnostics_runtime.py
1 error in 1.35s
```

No production runtime file existed when this command ran.

### First GREEN

After implementing only `backend/app/core/diagnostics_runtime.py`, the same
focused command returned exit `0`:

```text
............                                                             [100%]
12 passed in 4.03s
```

### Compatibility verification and test-race correction

The first broader compatibility command was:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_db_concurrency.py backend/tests/test_sqlite_database_component.py backend/tests/test_sqlite_connection_reuse.py backend/tests/test_repository_runtime_identity.py backend/tests/test_model_concurrency.py backend/tests/test_kg_scheduler.py -q
```

It returned exit `1`: `66 passed`, with one diagnostics test failure. The
SIGUSR1 test observed the first flushed thread header before faulthandler had
finished appending the remaining thread frames. The child process remained
alive and the partial log already contained its worker frame. The poll was
corrected to wait at 20 ms intervals for the required two thread headers rather
than merely a non-empty file.

The unchanged broader command was rerun and returned exit `0`:

```text
...................................................................      [100%]
67 passed in 3.90s
```

## Design notes

- Runtime installation is guarded process-global state, not a `ContextVar`, so
  worker threads observe the same installed registry. Nested installation of
  the same object is allowed; a different installed runtime is rejected.
- The diagnostic `ContextVar` contains exactly request id, job id, exact
  notebook id, and phase. Notebook ids are extracted only from the allow-listed
  `/api/notebooks/{id}` shape; stored request paths discard queries and notebook
  suffixes.
- Active request, SQL, job, lock-holder, and waiter registries are bounded.
  Recent jobs use `deque(maxlen=100)`. Entries retain only metadata and use UTC
  timestamps plus monotonic duration calculations.
- SQL observation stores only the exact normalized verb/table/fingerprint
  contract. SQL text, values, and parameters never enter a registry or
  snapshot.
- Write-lock observation preserves same-thread re-entry through a depth count,
  does not register an owner as its own waiter, and cancels only the matching
  waiter token.
- Readiness is allow-listed to status/count metadata and concurrency is limited
  to a bounded numeric tree. Provider calls occur outside the registry lock.
- Snapshot writes use `allow_nan=False`, a temporary file, flush, `fsync`, and
  `os.replace`. Wakeups are coalesced behind the configured minimum interval;
  provider/filesystem exceptions only increment `snapshot_failures`.
- Clean startup truncates the dump file, then opens it for append. SIGUSR1 is
  registered only from the POSIX main thread with `all_threads=True` and
  `chain=False`; faulthandler emits stacks without local values and the handler
  is non-terminating. Non-main-thread startup records capture as unavailable.
- Module-level instrumentation helpers isolate diagnostics failures and become
  no-op context managers/functions when no runtime is installed.

## Final verification

Fresh pre-commit focused runtime test:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py -q
```

```text
............                                                             [100%]
12 passed in 1.82s
```

Fresh adjacent core/concurrency compatibility test:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_db_concurrency.py backend/tests/test_sqlite_database_component.py backend/tests/test_sqlite_connection_reuse.py backend/tests/test_repository_runtime_identity.py backend/tests/test_model_concurrency.py backend/tests/test_kg_scheduler.py -q
```

```text
.......................................................                  [100%]
55 passed in 3.36s
```

Syntax and diff hygiene:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile backend/app/core/diagnostics_runtime.py backend/tests/test_diagnostics_runtime.py
git diff --check
```

Both returned exit `0` with no diagnostics.
