# Development and repository contracts

[Back to README](../README.md) · [中文说明](./development_zh.md)

This document preserves the contributor-facing architecture summary, verification gate, workflow, test architecture, and documentation-maintenance contract. [AGENTS.md](../AGENTS.md) remains the full agent/developer contract and [architecture.md](../architecture.md) the detailed runtime architecture.

## Architecture Boundaries

- Backend endpoint bodies live in domain FastAPI routers composed by `backend/app/api/routes.py`; the aggregate is composition-only and owns router order, not product handlers or compatibility exports. Boundary tests inspect endpoint ownership on the domain routers and verify the aggregate's composition declaration semantically; they do not assume `include_router()` flattens child routes, because newer FastAPI versions retain lazy included-router nodes. Domain Pydantic models live under `backend/app/models/`; `backend/app/models/schemas.py` is a legacy compatibility facade that re-exports the same model objects for old imports.
- One repository factory selects `SQLiteRepository` or `PostgresRepository` from `DATABASE_URL`; both compose the same runtime boundary. `RepositoryFacade` is backend-neutral over an injected `RepositoryRuntime` bundle. Application services do not assemble product SQL, inspect dialects, or import the opposite adapter. Stores own product SQL and raw row selection; established application/query components may assemble domain/application projections such as `NotebookSummaryQuery.from_row`. SQLite retains its compatibility migration/maintenance wrapper and PostgreSQL owns a bounded Psycopg pool plus checksummed migrations. Every facade operation is an explicit compatibility adapter or belongs to the source-checked one-hop delegates whose real targets match the ownership manifest. The dependency direction is factory/wrapper → facade → runtime → services → stores. `sqlite_identity.py` and `sqlite_notebook_sharing.py` remain compatibility re-export shims, and the legacy request-context, `_COPY_CHUNK`, and `_remap_json_ids` exports stay importable.
- `RepositoryRuntime` owns or references composed runtime state; `REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner, and the runtime, report coordinator, and module compatibility functions share that same identity reference. Other mutable operational state (storage root, embedder, language caches, build sets, Ask cancellation registry, and artifact caches) is runtime-owned; replacing supported compatibility properties after composition updates every retained consumer. Synchronous Ask/report submission failures mark the already-created durable job/report failed, unregister the cancellation entry, and re-raise the submission error; successful worker ordering and the existing Ask transaction checkpoints remain unchanged.
- Databases created before the refactor keep loading unchanged. `scripts/verify_repository_snapshot.py` uses exact per-version migration and stable-seed manifests, percent-encodes SQLite URI paths, constructs the repository only on a temporary backup, and reports the retained backup path if cleanup fails without printing private rows. It guards the original database/WAL metadata plus SHM existence and size; for a live WAL attachment only SHM mtime is exempt because SQLite may rebuild it.

The current schema version is 31. This is the SQLite schema version. The committed v9 compatibility fixture
upgrades through migrations v10–v31 and remains readable. Those migrations
cover compatibility and SQLite hot-path indexes (v10–v12), Memory/Agent and
Memory-derived source links/indexes (v13–v15), knowhow tables and cell code
(v16/v18), paper metadata (v17), source-linked assets (v19), and multi-domain
reference-library mounts plus promotion targets (v20), and the normalized
interactive-reformat anchor-membership expression index (v21); v22 adds durable
notebook-scoped KG build jobs; v23 added per-user latest model-service status;
v24 adds the kg_canonical_scratch table for the write-lock-slimming cluster-map
swap; v25 irreversibly scrubs stored per-user model credentials and legacy
status, then adds deployment-wide model-service health persistence keyed by
service ID; v26 adds knowhow table change history and named milestones; v27 adds
the sources.chunked_at completion marker so an extracted-but-chunkless source's
history is decidable (a legitimate zero-chunk parse versus an interrupted chunk
build); v28 adds the app_settings key/value table and the nullable
user_profiles.upload_document_limit column backing the per-notebook document
limit; v29 deterministically deduplicates cluster memberships and installs the
unique membership index; v30 adds the sources(notebook_id, file_hash) index
backing content-hash upload dedup and batch_ingest resume. PostgreSQL's
checksummed schema manifest targets migration v9. SQLite v31 adds only the
inert, payload-free shadow_change_log and shadow_capture_control internal
tables; run-scoped guard/capture/freeze DDL is installed separately. Guards
enforce uniqueness immediately after installation, while capture/freeze
behavior stays disabled until the run control state enables it. PostgreSQL v9
remains the paired business schema. The temporary
shadow boundary now includes a SELECT-only UTF8-first preflight, redacted
identity-bound confirmation, an owned/checksummed removable PostgreSQL control
schema, revision CAS, and two independently committed reports for the four
logical-key guards across the exact 60-table epoch-1 manifest. It also includes
run-bound atomic SQLite snapshots and bounded resumable baseline COPY: each
batch commits with its prefix checkpoint, resume proves that exact target
prefix without truncating or deleting business rows, seven historical rowids
copy as explicit ordinals and their catalog-resolved identity sequences reseed,
and the final forward checkpoint advances atomically to snapshot H0 after the
v9 ledger, FK, guard, and ANALYZE checks. Snapshot publication requires an
owner-only real directory and exclusive 0600 temporary creation. COPY fully
qualifies business SQL to the run-bound schema, revalidates enabled live SQLite
capture under a short `BEGIN IMMEDIATE` at every critical binding, uses a fresh
dedicated connection to the currently named SQLite file rather than the
repository thread cache, and binds/rechecks its resolved path and device/inode
across open and immediately before publication/PG commit. JSONB prefix proof
normalizes only JSON numeric leaves to exact finite decimal semantics; ordinary
SQL numeric columns remain type-distinct. It uses bounded
named server cursors plus statement timeouts/cancellation polls, and performs
full initial/final migration-derived validation of v9 tables, columns,
constraints, operational/GIN indexes, and `public.pg_trgm`; per-batch validation
is intentionally lightweight. The final SQLite fence is acquired only after
the long PG proof/ANALYZE phase and is retained until the PG H0 checkpoint and
run-progress transaction has actually committed; PG failure publishes no H0
and releases SQLite. A fail-stop forward replicator primitive now consumes the
global SQLite sequence contiguously, hydrates current rows only for upserts
under a short read snapshot, keeps deletes key-only with zero hydrated bytes,
and commits ordered target rows with its checkpoint after re-locking the
ledger/all business tables and revalidating the exact catalog. Repeated
stable keys in the accepted prefix coalesce to the last event and are emitted
in global last-seq order; raw seq/checkpoint continuity remains unchanged. For
each identity, the final actual apply overrides any synthetic dependency
contribution; only dependency-only identities contribute one reference-counted
synthetic row and its bytes. A short read window ending below the allocated
high-water is an immediate suffix gap before hydration/apply; a full window
below high-water probes the adjacent sequence in the same snapshot and fails
if it is absent. Snapshot and pre-apply gates both require
`progress.applied_seq` to equal the checkpoint. It uses a
single lease, capped whole-transaction retries, actual-seq poison records, and
redacted metrics. Batches are hard-capped at 4,096 events/64 MiB: only one final
bundle may exceed the byte cap, and a same-key replacement that grows past the
cap rolls back and defers when another actual bundle is already accepted. FK
parents come only from the verified current source snapshot through a
64-row-per-event, byte-counted, batch-deduplicated closure;
the fixed v9 graph has a branch-counted bound of exactly 9 row slots and no
suffix-log evidence scan is used. Savepoints defer only FK/UNIQUE ordering
SQLSTATEs; CHECK/NOT NULL poison immediately. Exact PG9 catalog plans cover all
82 unique surfaces using NULL; deterministic candidates scoped by indexable
equality for non-NULL values and `IS NULL` for NULL values on the other unique
columns plus the fixed predicate (`C`-collated text max plus `chr(1)`, or an
indexable bigint MIN/MAX fast path choosing min−1/max+1 and scanning the first
gap only when both int64 bounds are occupied); or same-transaction
delete/reinsert only for no-incoming-FK leaves with an accepted current-final
restore row. Parked state is tracked per unique surface and row identity; each
stagnant pass parks every independently parkable conflict, and a successful
final apply clears all surfaces parked for that identity. Deferred work is
capped at 8 passes, 32 actual statements per apply, and 16,384 actual
statements total. Every SAVEPOINT/ROLLBACK/RELEASE, DML, and candidate query
counts toward that budget. Ordering, statement, pass, and
`ProgramLimitExceeded`/`DataError` candidate-search or candidate-update
capacity exhaustion stays non-poison; `QueryCanceled` remains transient and
retries the whole transaction. An unparkable UNIQUE at the final source window
poisons its earliest actual event seq. The worker
doubles its 256-event/8-MiB window through the hard caps after ordering-blocked; hard-cap
exhaustion remains non-poison. After claiming the worker, the apply transaction
rechecks existing poison for that run/direction before any business DML. Poison
publication also locks and inspects every existing run/direction poison after
binding/checkpoint validation: an exact replay is ACK-loss success, while a
differing record is stale and never creates a second poison. SQLite path/file
binding failures use a dedicated identity error instead of message-based
conversion classification. At the `open_fresh_live_sqlite` call boundary,
non-transient `sqlite3.OperationalError` is also a binding failure; locked,
busy, and interrupted opens remain transient whole-batch retries, and later
SQLite operational errors keep their existing schema/query classifications.
Apply, ambiguous commit recognition, and poison
publication bind snapshot source/target plus the live target identity. It does
not yet include an operator CLI or end-to-end worker. Every valid batch outcome
emits exactly one redacted metric; batch events use the actual accepted/observed
raw-event count rather than lag, and retries are retained whenever observable.
`SHADOW_DATABASE_URL` remains inert by itself. Safety-critical PG
control mutations always take the migration lock, then the control lock, then
validate the exact live control catalog. A live SQLite transition acquires the
PG pool, both locks, and the run row before its short `BEGIN IMMEDIATE`, so it
never waits for a PG pool or advisory lock while holding SQLite.
- `frontend/app/page.tsx` is the notebook-workspace orchestrator, not the owner of every shared view model or panel. API/view types and constants live in `workspace-model.ts`, the answer/citation/reasoning-trace surface lives in `answer-panel.tsx`, built-in KG labels/styles live in `kg-type-model.ts`, and graph/answer rendering shares `kg-type-mark.tsx`.
- Workspace HTTP ownership is split into `system-api.ts`, `notebook-api.ts`, `source-api.ts`, `ask-api.ts`, `knowledge-api.ts`, `report-api.ts`, and `kg-api.ts`. The shared `frontend/app/api-client.ts` transport owns HTTP mechanics; domain modules retain endpoint policy. `page.tsx` retains state, stale-result guards, polling, and Blob URL lifecycle. `api-boundary.test.mjs` semantically forbids production `fetch` outside the transport core.
- Boundary regression tests use public HTTP contracts or explicit domain seams, never private aggregate helpers, source positions, line counts, or total route/model counts. Workspace-state hook extraction and FastAPI lifespan/application lifecycle composition remain separate debt.

## Verification

Run:

```bash
bash scripts/check.sh
```

This is the complete offline local gate. It runs three bounded lanes concurrently: `check_backend.sh` executes the complete backend pytest suite with default 9 backend pytest workers (override with `BACKEND_PYTEST_WORKERS`); `check_contracts.sh` executes syntax/dependency preflight, hermetic smoke paths, contract checks, and the deterministic extraction-scoring harness; `check_frontend.sh` executes every recursively discovered `*.test.mjs`, every `*.component.test.tsx`, `tsc --noEmit`, and the production frontend build. Each lane has its own process group, so interrupting or terminating the controller also terminates and reaps pytest, npm, and Next.js descendants. The official-client MCP smoke pins exactly eleven tools: seven Memory tools plus four knowhow tools. Missing `frontend/node_modules` is a hard failure rather than a silent skip.

Use the project’s Homebrew/Miniconda interpreter for acceptance:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

The Apple Silicon warm gate hard target is at most 60 seconds. CI lane timings are observational only, so this measured local target is not a portable timeout assertion for every CI host.

### GitHub Actions CI

`.github/workflows/ci.yml` exposes the same complete gate as the single
`CI / full-gate` check. It runs for pull requests targeting `master`, pushes
to `master`, and manual dispatches on `ubuntu-24.04` with Python 3.13 and
Node.js 22. The workflow installs from `backend/requirements.txt` and
`frontend/package-lock.json`, then delegates test selection entirely to
`scripts/check.sh`.

The committed OpenAPI contract is byte-semantically frozen, so
`backend/requirements.txt` pins FastAPI `0.135.3` and Pydantic `2.12.4`
exactly. Upgrade either framework only together with an intentional OpenAPI
contract regeneration and a clean-environment full-gate run.

The workflow is read-only, does not receive model or deployment secrets, and
uses four backend pytest workers to avoid oversubscribing the hosted runner.
Backend installation sets `HNSWLIB_NO_NATIVE=1` and disables pip's wheel cache:
`hnswlib` otherwise builds with `-march=native`, and a cached locally built
wheel can crash with `SIGILL` when restored on a hosted runner with different
CPU features. The portable build trades a small ANN speedup for deterministic
CI; production wheelhouses may still target their declared deployment CPU.
Its 20-minute timeout includes dependency installation and is intentionally
separate from the under-60-second local Apple Silicon warm-gate target.
`CI / full-gate` is initially observational; make it a required `master` check
only after stable green pull-request and post-merge runs have been observed
and the user explicitly approves the branch-protection change.

PostgreSQL coverage is deliberately separate from the offline full gate. The
`postgres-integration` job starts PostgreSQL 16, provisions least-privilege and
auxiliary encoding/locale targets, and runs `bash scripts/check_postgres.sh` with
only the `postgres_integration` marker. Local verification uses an installed
PostgreSQL 16 service and an explicit `TEST_POSTGRES_URL`; `scripts/check.sh`
must never start or contact PostgreSQL.

CI portability is part of the gate contract: every filesystem, data, and
dependency path used by a CI-executed test is repository-relative and
independent of the process cwd. Committed fixtures are located relative to
their repository files, never through a developer checkout path or `HOME`,
and tests never read repository-external source documents. Every third-party
package imported during test startup is declared in `backend/requirements.txt`;
a clean hosted runner installs from that file and `frontend/package-lock.json`,
then passes from those declarations alone. Lane timings remain visible for
observation, while the under-60-second target applies only to the verified
Apple Silicon Homebrew warm gate.

Developer-only gold-generation/build/validation scripts that consume external
PDF parse output remain outside `scripts/check.sh`; that exception never
applies to committed tests.

## Development Workflow

For every new feature development task, create a new git worktree by default, start a new feature branch inside that worktree, complete the work there, and open a PR from that branch. Do not switch branches directly in the main local checkout for feature work. If the current directory is already an isolated linked worktree, keep working there.

For approved multi-step implementation plans, use subagent-driven development by default: assign each task to a fresh implementation subagent and require task-scoped specification and code-quality review before moving on. Research, design, status, and review-only work does not require a worktree or subagents.

`CLAUDE.md` is the Claude Code operating standard for this repository. Claude Code auto-loads only `CLAUDE.md` and `.claude/rules/`, never `AGENTS.md`, so that file inlines the red lines that must stay resident and indexes the `AGENTS.md` sections to consult on demand; `AGENTS.md` remains the source of truth where the two disagree, and `CLAUDE.md` enumerates the few deliberate exceptions. Because Claude Code reads it and not `AGENTS.md`, `CLAUDE.md` is part of the four-file documentation-sync set. Its hardest rule is that **spawning a subagent must state the model explicitly instead of inheriting the main agent's** — tiered by how much judgment the task needs: `opus` for judgment work (writing plans, review, architectural trade-offs, hard diagnoses), `sonnet` for transcription-shaped implementation whose spec is already pinned down, `haiku` for pure search and location. The PreToolUse gate `.claude/hooks/require-subagent-model.py` enforces it: a call that passes no `model` and whose `subagent_type` is not pinned to a model in `.claude/agents/` is denied. Three pinned roles ship in `.claude/agents/`: `impl-task` (sonnet), `spec-review` (opus), and `code-quality-review` (opus). `backend/tests/test_claude_subagent_model_hook.py` is the hook's regression net — it runs the real script over a subprocess boundary and covers both directions, the bypasses that would let an inherited-model call through and the false denials that would push people to work around the gate.

A pull request must be reviewed by codex before it is merged, and **every round's raw output is posted verbatim to the PR** — rounds that raise nothing included, rounds run by hand included — alongside the trigger, the exact command, the head SHA, and the exit code with output size, so a reader can confirm the run happened and was not paraphrased away. A round counts as successful only when the exit code is zero **and** the output is non-empty: a review killed by SIGTERM also exits zero, and trusting the exit code alone posts an empty comment that reads as a pass. P0/P1 findings block and stop for a human decision; P2/P3 do not block and may be declined with a stated reason; output whose priority tags cannot be parsed blocks conservatively instead of defaulting to a pass. A finding may be rejected on the merits — codex reviews the diff and does not always know the runtime facts — but a rejection must carry its reasoning and evidence on the PR, a comment recording the trade-off in the code, and a regression test pinning the behavior that was kept. Merging always requires explicit human approval. The review automation itself is a per-developer Claude Code hook rather than a repository artifact, so a fresh clone will not have it; the rule stands regardless, and `CLAUDE.md` documents the manual command.

### Test architecture

- Backend and frontend static contracts use semantic identities such as module path, qualified scope, operation kind, target, and reviewed count. Source positions are diagnostic metadata only; line numbers, source offsets, CSS order, and source slices must never identify an expected site.
- Frontend `*.test.mjs` files cover pure logic and the small set of justified architecture/security/vocabulary/entry contracts with `node:test`. Frontend `*.component.test.tsx` files use Vitest, jsdom, and Testing Library to exercise user-visible behavior through roles, actions, and state.
- Component behavior must not be pinned through CSS geometry or source layout. A routine feature refactor should change tests only when its observable contract changes.
- Committed tests may not be disabled with skip/xfail/todo/only. Repository policy tests enforce this across test entrypoints and their helper modules, and prevent direct production-source reads outside the shared semantic-source adapter.
- The frontend source policy is intentionally bounded: it rejects AST position/collection-order APIs and source-named text position operations syntactically, while the shared `semantic-source.mjs` adapter may expose AST semantics but may not use text slicing, splitting, indexing, or length as a contract. Do not replace this with whole-JavaScript data-flow interpretation; ordinary array operations stay valid.
- Backend test startup prewarms one repo-local Matplotlib font cache before xdist workers start. Keep that controller boundary: letting each graph worker enumerate macOS fonts independently adds avoidable multi-second cold starts.

## Documentation Maintenance

When product behavior, setup, architecture, or development constraints change, update all of these files together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`
- `CLAUDE.md`

Keep the root READMEs concise. Also update the owning English/Chinese canonical pair under `docs/`: `product-and-api`, `deployment-and-configuration`, `operations`, or `development`.
