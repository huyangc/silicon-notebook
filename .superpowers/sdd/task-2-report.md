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

## Review correction pass

### Blocker regression RED

Tests were added before the correction implementation for fail-closed route
normalization, overlapping same-runtime installation, rejected activation side
effects, identifier/provider bounds, concurrent lifecycle transitions, timed-out
stop retention, and process-global SIGUSR1 ownership. The existing focused
command was run unchanged:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py -q
```

Result: exit `1`, with the intended failures against the reviewed behavior:

```text
14 failed, 13 passed in 2.33s
```

The failures showed all seven private path variants retained verbatim, the
outer install clearing an overlapping worker scope, rejected activation
creating its directory, an unbounded 10,008-character request id, eight
snapshot writers from concurrent `start()`, timed-out `stop()` clearing its
live thread, and a second runtime claiming SIGUSR1.

### Blocker regression GREEN

After implementing the fail-closed normalizers, installation depth, exclusive
signal owner, install-before-start activation, and serialized lifecycle state,
the same focused command returned exit `0`:

```text
...........................                                              [100%]
27 passed in 2.22s
```

### Full-snapshot bound RED/GREEN

A follow-up regression test added oversized readiness counters and SQL
verb/table identifiers. Before their production caps, the focused command
returned exit `1`:

```text
1 failed, 29 passed in 2.29s
```

The precise failure was retention of the roughly 10,001-digit
`warmed_notebooks` and `total_notebooks` values. After bounding readiness
counts and active SQL names, the same command returned exit `0`:

```text
..............................                                           [100%]
30 passed in 2.25s
```

The focused suite also passed sequentially in one process, exercising cleanup
of the process-global install and signal-owner state without xdist isolation:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_diagnostics_runtime.py -q
```

```text
30 passed in 0.90s
```

### Correction design notes

- Request paths now use a fixed static-segment vocabulary and `{id}` for every
  other segment, cap structural depth, and discard query/fragment data.
  Notebook routes remain the exact `/api/notebooks/{id}` shape and retain an
  exact local notebook id only when it matches the bounded opaque-id contract.
- Request ids are capped at 200 characters. Concurrency accepts only the
  `kg`/`llm`/`embedding` groups, known counter names, and finite non-negative
  bounded numeric values. Readiness counts and active SQL names are also capped.
- Process-global installation now uses a guarded depth so out-of-order overlap
  of same-runtime lexical scopes cannot uninstall a still-active runtime.
- `activate_runtime()` installs before starting. Rejection therefore creates no
  directory, writer, dump handle, or signal mutation.
- SIGUSR1 has one guarded process owner. A second runtime remains unavailable
  for capture and cannot replace or unregister the original runtime's handler.
- One runtime lock serializes lifecycle transitions. Concurrent starts create
  one writer; stop is idempotent; a timed-out join retains the live writer,
  stop event, open dump handle, and `stopping` state. A later stop cleans up only
  after that exact writer exits.
- Additional tests cover concurrent readers of atomically replaced JSON,
  active-registry overflow, recovery after one `os.replace` failure, minimum
  interval coalescing, and termination of lifecycle writer threads.

### Final correction verification

Fresh focused command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py -q
```

```text
..............................                                           [100%]
30 passed in 3.33s
```

Fresh adjacent core/concurrency command:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_db_concurrency.py backend/tests/test_sqlite_database_component.py backend/tests/test_sqlite_connection_reuse.py backend/tests/test_repository_runtime_identity.py backend/tests/test_model_concurrency.py backend/tests/test_kg_scheduler.py backend/tests/test_embed_concurrency.py backend/tests/test_pipeline_concurrency.py -q
```

```text
.............................................................            [100%]
61 passed in 4.04s
```

Syntax and diff hygiene:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile backend/app/core/diagnostics_runtime.py backend/tests/test_diagnostics_runtime.py
git diff --check
```

Both returned exit `0` with no diagnostics.
