# Production DFX Incident Diagnostics Design

**Status:** Approved in conversation; ready for implementation planning

**Date:** 2026-07-21

**Scope:** Backend, SQLite, background work, external-model calls, and coarse frontend/backend liveness

## 1. Context

`silicon-notebook` currently provides several useful diagnostic scripts, but they do not yet form a reliable production incident workflow. The deployment in scope runs on Ubuntu 24.04 and is started with `npm run start`, which launches one Uvicorn backend process and the Next.js frontend. Operators can connect by SSH, but can only copy a bounded block of text out of the production environment.

The immediate motivating incident is a notebook deletion that appears to hang or take an unexpectedly long time. The same DFX capability must also help distinguish SQLite contention, slow SQL, background-job saturation, external-model latency, event-loop stalls, filesystem cleanup, and host pressure without restarting the service or transferring large files.

The existing diagnostic suite has three important blind spots:

1. Historical readers inspect legacy undated JSONL paths, while the current event logger writes daily files such as `requests-YYYY-MM-DD.jsonl` and archives older files as gzip. A diagnostic command can therefore report zero requests even when current request logs exist.
2. Request logging happens when a request completes. A request that is still hanging is absent from the log and cannot be correlated with its current phase, SQL work, or worker thread.
3. The process exposes no unified snapshot of active requests, SQLite write-lock ownership and waiters, background jobs, or active concurrency gates. The existing scripts consequently cannot identify which in-process operation is blocking progress.

This design turns the existing scripts into one coherent, copy-friendly production DFX suite while keeping all diagnostic operations read-only with respect to application data.

## 2. Goals

The implementation must:

- provide one primary live-incident command that produces a sanitized, ranked, copyable text report;
- make a currently hanging operation visible before it completes;
- correlate active requests, SQLite activity, write-lock contention, background jobs, model/concurrency pressure, process stacks, host state, logs, and database plans;
- diagnose notebook-delete stalls specifically, including cascade shape and missing foreign-key child indexes;
- repair the historical commands so they cover legacy, daily, compressed, and per-user logs;
- preserve current command behavior and direct invocation of the existing scripts;
- work on the stated Ubuntu deployment without root access, third-party diagnostic packages, a service restart, or a database mutation;
- keep normal-path overhead small and avoid persistent per-query tracing;
- keep the default report below approximately 32 KiB so it can be copied as one text block.

## 3. Non-goals

The first version will not:

- add Prometheus, OpenTelemetry, a hosted observability service, or a metrics dashboard;
- add a user-facing diagnostics UI;
- deeply instrument Next.js internals beyond process/port/HTTP liveness;
- capture Python local-variable values, request bodies, prompts, answers, source text, SQL parameters, or secrets;
- automatically kill, restart, checkpoint, reindex, migrate, or otherwise remediate the production service;
- replace focused profiling tools for offline CPU optimization;
- promise diagnosis of code outside the local deployment when no process, state snapshot, stack dump, or usable log evidence is available.

## 4. Operator Experience and Command Coverage

The unified entry point remains `python3 scripts/diag.py`. The following command surface covers the distinct diagnostic questions without overlapping responsibilities:

| Command | Primary question | Data sources | Application import |
| --- | --- | --- | --- |
| `incident` | What is blocking or saturated right now? | Runtime snapshot, `SIGUSR1` thread dump, `/proc`, health probes, bounded recent logs, fast DB checks | No |
| `slow` | Which completed requests and operations have been slow recently? | Legacy/daily/gzip/per-user request, event, and LLM logs | No |
| `latency` | Which Ask pipeline stages dominate latency? | Legacy/daily/gzip/per-user event and LLM logs | No |
| `open --notebook ID` | Why is opening one notebook slow? | Notebook-open request/event evidence and bounded DB reads | No |
| `db [--notebook ID]` | Is SQLite, WAL state, table scale, a scan, or a missing FK index the likely bottleneck? | Read-only SQLite metadata, query plans, file sizes | No |
| `base-recall ...` | Does configured base-recall behavior meet its diagnostic contract? | Existing application repositories/services | Yes, unchanged |

The existing standalone scripts remain directly runnable for compatibility. `scripts/diag.py` becomes the discoverable dispatcher and supplies consistent common flags such as `--since`, `--logs-dir`, `--db`, `--pid`, `--output-limit-kib`, and `--verbose` where applicable.

The recommended incident workflow is a single command:

```bash
python3 scripts/diag.py incident
```

If automatic PID discovery is ambiguous, the report must explain the ambiguity and show the exact retry form:

```bash
python3 scripts/diag.py incident --pid 12345
```

The command prints its result to stdout. Runtime artifacts remain local and are used only as evidence inputs; the operator does not need to transfer them.

## 5. Architecture

The design has three layers.

### 5.1 Shared offline diagnostic utilities

A new pure-standard-library `scripts/diag_common.py` module provides:

- discovery of legacy undated, current daily, gzip-archived, and per-user JSONL logs;
- streaming JSONL/gzip parsing with time-window filtering and a bounded retained sample;
- malformed-record counters instead of whole-command failure;
- record deduplication when the same event is discovered through more than one path;
- path, identifier, header, token, query-string, SQL, and free-text sanitization;
- output-section and whole-report size budgeting;
- common timestamp, percentile, table, and warning rendering.

Log files are never loaded in full. Discovery is deterministic, newest relevant files are considered first, and records outside the requested time window are discarded while streaming. A duplicate is identified from stable metadata such as channel, event/request identifier, event name, and timestamp; the diagnostic layer does not hash or compare sensitive message bodies.

`slow`, `latency`, and `open` use this common reader. This removes the current mismatch between the event logger's daily/archived naming and the scripts' legacy-path assumptions.

### 5.2 Lightweight in-process runtime state

The backend gains `backend/app/core/diagnostics_runtime.py`, a process-local diagnostics runtime with bounded registries and a small atomic snapshot writer. `backend/app/main.py`, `backend/app/repositories/sqlite/database.py`, `backend/app/services/background_jobs.py`, `backend/app/services/model_concurrency.py`, and `backend/app/services/kg/scheduler.py` supply narrow hooks into that runtime. It records metadata only:

- process start time, PID, diagnostics schema version, and application readiness;
- last state change and last successful snapshot timestamps;
- active requests: generated request id, method, route template, bounded opaque route identifiers needed for local correlation, start time, thread id when known, and current phase;
- active SQLite operations: read/write class, logical operation name, affected table when known, normalized SQL fingerprint, start time, thread id, and associated request/job id;
- SQLite process-wide write-lock state: holder thread, acquisition time, logical operation, associated request/job id, and bounded waiter entries with wait start times;
- active/recent background jobs: stable job name, thread id, start/end timestamps, status, and the bounded opaque notebook identifier needed for local correlation;
- existing concurrency snapshots for KG extraction, LLM/model calls, and embeddings, including active, configured maximum, and waiting where available;
- a monotonically changing state revision used to detect a stale snapshot.

All registries are bounded and protected by their own small lock. Recording must not acquire the application's SQLite write lock. Hooks must be exception-safe: diagnostics failure cannot fail a request, database operation, or background job.

Opaque database identifiers may remain exact inside the machine-local snapshot when they are required to correlate a request with a read-only database plan. They are never rendered directly: the offline reporter consistently pseudonymizes them before output. User-controlled names or content are not written to the snapshot.

Request middleware registers an entry before dispatch, updates its phase through a `ContextVar`, and removes it in `finally`. The context propagates into Starlette's synchronous thread-pool work. Database wrappers and background-job submission establish their own request/job correlation context when applicable.

SQLite instrumentation wraps the existing connection/operation boundaries. It stores a normalized fingerprint and operation metadata, never bound parameters or result values. It does not write one event per SQL statement to disk. The existing re-entrant write-lock behavior remains unchanged: the outer acquisition owns the holder record, nested acquisitions increase an in-memory depth, and only the matching outer release clears ownership. Threads blocked before acquisition appear as waiters.

A daemon thread writes a sanitized snapshot every two seconds and immediately after important state transitions when the minimum write interval allows it. The snapshot is written to a temporary file, flushed, and atomically replaced at `.local/diagnostics/runtime.json`. Atomic replacement ensures that `incident` observes either the previous complete snapshot or the next complete snapshot, never partial JSON.

At application startup on POSIX systems, `faulthandler` is registered for `SIGUSR1` and appends all Python thread stacks to `.local/diagnostics/thread-dumps.log`. This is a non-terminating diagnostic signal. The dump contains Python frames but not local-variable values. Registration opens and retains its own append-only file descriptor so a signal-time dump does not depend on application logging locks.

Concurrent `incident` collectors serialize on an advisory diagnostics lock. After a successful capture, the collector may truncate the thread-dump file in place when it crosses a configured multi-megabyte retention threshold; in-place truncation preserves the inode referenced by `faulthandler`'s retained descriptor. The file is also truncated on clean process startup. This bounds retained diagnostic stacks without rotating the live descriptor or affecting application data.

### 5.3 Incident and database analyzers

`scripts/diag_incident.py` orchestrates bounded read-only collection and ranking. A dedicated pure-standard-library `scripts/diag_db.py` analyzer supplies reusable SQLite evidence and can also run independently. `scripts/diag.py` dispatches both while preserving the current subcommands.

`db` opens SQLite in read-only mode with a short busy timeout. It collects:

- database, WAL, and shared-memory file sizes;
- journal mode and safe read-only pragma/metadata values;
- bounded table and index inventory, with estimated or exact counts only where they fit the probe budget;
- foreign-key relationships and whether each child-key prefix is backed by a usable index;
- targeted `EXPLAIN QUERY PLAN` output for known critical operations;
- notebook-delete cascade shape for a supplied notebook id, including child tables, FTS cleanup, and plan nodes containing `SCAN`;
- high-level recommendations tied to evidence rather than schema guesses.

The analyzer never executes a delete, checkpoint, vacuum, analyze, reindex, or schema change. If SQLite is busy, each probe stops within one second and the report records that the evidence is unavailable rather than waiting behind production work.

For a live `DELETE /notebooks/...` request, `incident` automatically asks the DB analyzer for the relevant fast notebook-delete checks. This joins the live holder/waiter/stack evidence to cascade plans and index coverage in one report.

## 6. Incident Collection Flow

`incident` follows a fixed order so the most time-sensitive state is captured first:

1. Resolve the repository/runtime paths and configuration without importing the application.
2. Discover the backend PID from `/proc/*/cmdline`, current working directory, and the expected Uvicorn command. If multiple candidates remain, stop automatic signaling and ask for `--pid`.
3. Read and validate `runtime.json`, including schema version, PID match, heartbeat age, and state-revision freshness.
4. Record the current thread-dump file offset, send `SIGUSR1` to the confirmed backend PID, wait briefly for append completion, and read only the newly appended dump.
5. Sample `/proc` for process state, CPU, RSS, thread count, file-descriptor count, I/O counters, uptime, and Linux task states. Probe backend health and frontend HTTP liveness with short timeouts.
6. Read a bounded recent window from request/event/LLM logs using the shared log reader.
7. Run fast read-only DB/WAL/index/plan probes. Add notebook-delete analysis when live request metadata identifies a delete target or when `--notebook` is supplied.
8. Apply deterministic diagnosis rules, rank the strongest three hypotheses, render supporting and contradicting evidence, list missing evidence, and enforce the output budget.

The normal command target is completion within ten seconds. Individual HTTP and DB operations use explicit short timeouts; no single SQLite probe may wait longer than one second.

## 7. Diagnosis Rules

Rules combine independent signals and must state uncertainty. A hypothesis is ranked higher when live state, stack frames, and a second source such as `/proc`, logs, or a query plan agree.

The initial rule set includes:

| Evidence combination | Classification |
| --- | --- |
| Write-lock holder has a long duration, waiters exist, and holder stack is in SQLite/application write work | SQLite write transaction blocking |
| Active SQL has a long duration and its targeted plan contains a large-table `SCAN` or an unindexed FK child lookup | Slow SQL / missing index / cascade scan |
| Stack is blocked in `urllib`, `httpx`, or OpenAI-compatible client code while model concurrency is active | External-model/network wait |
| Active count equals configured maximum and waiting grows for KG, model, or embedding work | Concurrency gate saturation |
| PID is alive, backend health times out, and the main event-loop thread remains in one blocking synchronous frame | Event-loop stall |
| Stack is in `unlink`, `rmtree`, or related file operations during a delete | Filesystem cleanup bottleneck |
| WAL grows materially while long reads remain active or checkpoint progress is blocked | WAL/checkpoint pressure |
| Repeated samples show high CPU and the same application frame | CPU-bound loop or computation |
| CPU is low, I/O counters grow, and a task is in Linux `D` state | Disk or network-filesystem wait |
| Frontend responds but backend health does not | Backend-specific outage |
| Backend responds but frontend does not | Frontend-specific outage |

The rendered result contains at most three primary hypotheses by default. Each shows confidence, the shortest useful evidence chain, and a next action. `--verbose` may include the full bounded evidence tables, but it still obeys sanitization and an explicit output limit.

No rule may claim a root cause from a single weak signal such as one slow completed request. When signals conflict, the report says so.

## 8. Report Shape and Sanitization

The default report is designed to be pasted into a support conversation as one block:

```text
silicon-notebook DFX incident report
Captured: ...   PID: ...   Snapshot age: ...

Top findings
1. [high] SQLite write transaction blocking
   Evidence: ...
   Next check: ...

Active work
...

Relevant stacks
...

Database and host signals
...

Missing/degraded evidence
...
```

Sanitization is fail-closed. The report includes route templates, operation names, table names, fingerprints, durations, counts, status codes, and repository-relative stack locations. It excludes:

- bearer/session/agent tokens, cookies, authorization headers, and environment secrets;
- source names/content, Ask questions/answers, prompts, model messages, Memory/Knowhow cell content, and report bodies;
- SQL bound values and raw SQL literals;
- raw query strings, usernames, full user-controlled filenames, and absolute home paths;
- arbitrary exception payloads unless passed through a strict length and secret scrubber.

Notebook, request, user, job, and source identifiers are shortened or consistently pseudonymized within the report. SQL is normalized to an operation/table/fingerprint representation. Stack frames outside the repository or an allow-listed standard/client-library set are collapsed. Section budgets preserve the summary and missing-evidence warnings first; verbose evidence is truncated before the whole report crosses the default approximately 32 KiB cap.

## 9. Failure and Degradation Behavior

Diagnostics must remain useful when evidence sources fail:

- No PID: collect offline logs/DB/host evidence, do not signal any process, and show the `--pid` retry form.
- Ambiguous PID: list only sanitized candidate metadata and require `--pid`; never guess.
- `SIGUSR1` rejected or dump missing: continue with runtime snapshot, `/proc`, logs, and DB data; mark stacks unavailable.
- Runtime snapshot missing, malformed, PID-mismatched, or stale: ignore unsafe fields, report its age/problem, and continue.
- Database locked/busy: stop the affected probe within one second and report the timeout as evidence of possible contention, without treating it alone as proof.
- Corrupt/truncated JSONL or gzip member: count skipped records, preserve good records, and show a data-quality warning.
- Health probe timeout: classify only with process and stack evidence; a timeout alone is not a root cause.
- Unsupported platform: offline commands remain available; `incident` reports which Linux `/proc` signals are unavailable. The production target remains Ubuntu 24.04.
- Snapshot writer failure: increment an in-memory failure counter and retry later; never propagate the exception into product code.

The tooling never restarts the backend, sends a terminating signal, mutates the database, or changes filesystem ownership/permissions.

## 10. Notebook Delete Coverage

Notebook deletion is a first-class diagnostic scenario because it crosses several layers:

1. A synchronous HTTP request enters the delete route.
2. The backend acquires the process-wide SQLite write lock and performs application cleanup plus database cascades/FTS cleanup.
3. Child-table foreign-key lookups can become full scans if their FK columns lack indexes.
4. The transaction can block unrelated writers, which appear as lock waiters.
5. Local source/storage cleanup can add synchronous filesystem latency after or around database work.

During such an incident, the report must answer:

- Which request/job currently owns the write lock, for how long, and which threads are waiting?
- Is the holder executing SQL, cascading through child tables, or cleaning files?
- Which delete-related plan nodes scan tables or FTS structures?
- Which foreign-key child columns lack supporting indexes?
- How large are the implicated tables and the DB/WAL files?
- Is the bottleneck database work, lock contention, filesystem work, or insufficient evidence?

A synthetic acceptance scenario will hold the write lock in a notebook-delete-shaped operation while another writer waits. The resulting report must identify the holder, waiters, relevant stack, scan/index evidence, and a targeted recommendation.

## 11. Testing Strategy

Tests are divided by boundary so production-only behavior remains deterministic in local development and CI.

### 11.1 Log discovery and sanitization

- legacy undated JSONL;
- current daily JSONL;
- gzip archives;
- per-user directories;
- mixed time windows and deterministic newest-first ordering;
- duplicate records appearing through overlapping paths;
- malformed/truncated JSONL and gzip data;
- bounded memory/sample behavior;
- sentinel secrets, prompts, answers, SQL values, source content, and absolute home paths absent from rendered output.

### 11.2 Runtime registry

- request register/phase/finally cleanup;
- correlation across synchronous route/thread-pool execution;
- background-job start/success/failure lifecycle and bounded history;
- SQL metadata registration without parameters or row values;
- re-entrant write-lock holder depth and correct outer release;
- waiter registration/removal under contention;
- concurrent snapshot reads/writes and atomic replacement;
- stale heartbeat and snapshot-writer failure counters;
- instrumentation exceptions never changing product behavior.

### 11.3 Signal and process collection

- child Python process registers `SIGUSR1`, exposes multiple thread stacks, remains alive, and appends only the new dump segment;
- PID discovery succeeds for one matching process and fails safely for zero or multiple matches;
- replaceable `/proc` adapter fixtures cover CPU, memory, I/O, FD, thread, and `D`-state classifications on macOS development and Linux CI;
- dump timeout and permission failure degrade without command failure.

### 11.4 Diagnosis and command contracts

- simulated SQLite holder/waiter/scan incident;
- external-model network wait;
- concurrency-gate saturation;
- filesystem-delete wait;
- event-loop stall;
- missing PID, stale state, DB busy, and corrupt logs;
- output ranking is stable and does not overclaim on single-source evidence;
- default output stays within the configured limit;
- all six `diag.py` commands expose help and dispatch correctly;
- all commands except `base-recall` remain pure-standard-library and do not import the application;
- existing standalone scripts remain runnable.

Repository verification after implementation includes `scripts/check.sh` and `cd frontend && npm run build`, even though this infrastructure change adds no user-facing UI.

## 12. Documentation Changes During Implementation

The implementation change must update these files together:

- `README.md` with the production incident workflow and safety properties;
- `README_zh.md` with the equivalent Chinese guidance;
- `AGENTS.md` with the maintained diagnostics architecture/constraints;
- `scripts/README.md` with the full command matrix, flags, example output, and troubleshooting notes.

Documentation must explain that `SIGUSR1` is intentionally registered as a non-terminating Python thread-dump signal for the backend process, that the command is read-only with respect to application data, and that report text is sanitized but should still be reviewed before sharing outside the trusted team.

This is internal DFX infrastructure rather than a completed product-spec feature, so it does not add an entry to `fangan_done.md` unless a corresponding requirement is later added to `silicon_notebook_fangan.md`.

## 13. Rollout and Compatibility

The runtime instrumentation is enabled by default because it is required for a useful live incident capture. Its persistent footprint is one atomically replaced snapshot plus an append-only thread-dump file. The in-place truncation policy described above must keep the dump file bounded so repeated incidents cannot grow it indefinitely.

Rollout preserves:

- the current `npm run start` process model;
- the existing database lock and transaction semantics;
- existing JSONL logger formats;
- current `diag.py slow`, `latency`, and `base-recall` semantics, except that historical readers become complete for current daily/gzip/per-user layouts;
- direct use of `scripts/diag_slow.py` and `scripts/diag_open_latency.py`.

If runtime snapshot schema changes later, the writer increments a schema version and the offline reader reports unsupported versions instead of interpreting fields incorrectly.

## 14. Acceptance Criteria

The DFX strengthening is complete when all of the following are true:

1. `python3 scripts/diag.py incident` completes within ten seconds under normal diagnostic conditions and prints one ranked, sanitized report.
2. The default report is at most approximately 32 KiB and contains no configured sensitive sentinels.
3. A live hanging request appears before completion with its phase and correlated request/job identity.
4. SQLite write-lock holder and waiters are visible without changing re-entrant locking behavior.
5. `SIGUSR1` appends all Python thread stacks without terminating or restarting the backend.
6. Every DB probe is read-only and has a maximum one-second busy wait.
7. A notebook-delete contention fixture identifies holder, waiters, stack location, scan/missing-index evidence, and a targeted next action.
8. `slow`, `latency`, and `open` correctly read legacy, daily, gzip, and per-user logs and report malformed-record counts.
9. `db` reports WAL/file scale, FK child-index coverage, and targeted query-plan scans without importing application code.
10. Existing standalone diagnostic scripts and `base-recall` remain compatible.
11. No third-party runtime dependency, root privilege, database write, restart, or automatic remediation is required.
12. Unit/integration tests, `scripts/check.sh`, and the frontend production build pass.
13. `README.md`, `README_zh.md`, `AGENTS.md`, and `scripts/README.md` describe the final verified behavior consistently.
