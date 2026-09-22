# Development and repository contracts

[Back to README](../README.md) · [中文说明](./development_zh.md)

This document owns contributor guardrails, schema/migration authoring, verification,
workflow, and documentation maintenance. [AGENTS.md](../AGENTS.md) is the short agent
entry point; [architecture.md](../architecture.md) owns runtime architecture.

## Numeric limits and truncation

Production code must not hide result-changing literal slices or limits at a
call site. Reuse a named protocol constant for invariant wire/storage bounds;
use a validated `Settings` field for quality/cost budgets. A user-authored list
is validated and rejected when over its shared backend/browser rail, never
silently sliced. Embedding and other model-input truncation must use one
configuration source across online, batch, and backfill paths. Explicit numeric
fixtures in tests are outside this rule.

## Architecture Boundaries

Runtime ownership and data flow belong to [architecture.md](../architecture.md).
Use its [repository composition](../architecture.md#22-repository-组合与兼容-facade),
[frontend boundaries](../architecture.md#24-前端边界), and
[core data flows](../architecture.md#3-核心数据流) when changing those surfaces.
Public behavior and exact numeric rails belong to [product/API](./product-and-api.md);
operational procedures belong to [operations](./operations.md). The rules below are
contributor constraints, not a second implementation history.

### Dependencies, authority, and state ownership

- Preserve `factory/wrapper → facade → runtime → services → stores`. Stores own
  product SQL and raw rows; application/query components may assemble domain projections.
  Services neither inspect dialects nor import the opposite adapter. Stable cross-layer
  values live in `backend/app/domain`; repository ports cannot import services, and the
  static dependency graph stays acyclic. Domain routers own endpoint bodies;
  `app/api/routes.py` only composes them. Compatibility facades re-export existing objects
  rather than creating a second implementation.
- Keep extension SDK contracts dependency-light. `app.bootstrap` alone joins adapters
  to `backend/app/extensions`; workflows consume domain host ports, and plugins never
  import concrete repositories, facade, or runtime. Availability probes stay I/O-free,
  projected capability ports stay narrow, and an unavailable contributor cannot disable
  independent contributions. Preserve frozen routing/admission, the exact no-contribution
  baseline, bounded batch hydration, cancellation, and the no-held-connection boundary.
  Extension authoring and UI-package constraints live in the
  [deployment extension SOP](./deployment-extensions-sop.md). Modular-extension PRs
  require two independent subagent reviews as well as the
  [normal review and CI policy](#development-workflow).
- Application stage envelopes under `backend/app/application` stay immutable and
  dependency-light. Extend its explicit import allowlist deliberately; do not allow bare
  root imports that bind `app`. Ask/Report stage seams must preserve the exact source
  scope, retrieval run, actor, cancellation token, and connection probe. Revalidate
  authority at the existing boundaries; violations fail loudly. Stage wrappers hold
  neither a database connection nor an outer leaf-I/O slot. Core owns final audit and
  persistence. Post-terminal observers cannot rewrite committed artifacts or reverse
  `done`; `report.completed_observer` also cannot start retrieval/model work. Keep Ask's
  existing observer order, failure isolation and cost when changing its completion path.
- `scripts/check_architecture_boundaries.py` enforces the G1 ratchets in
  `scripts/architecture_boundary_baseline.json`: repository-to-service debt and facade
  surface cannot grow; core/model-to-service imports, listed hot-function lengths, and
  repository Protocol-method counts have zero slack. Lower the baseline in the same
  change whenever one shrinks. Retire facade members against the
  [caller ledger](./superpowers/plans/2026-08-23-facade-retirement-ledger.md), reproduced
  by `scripts/audit_facade_callers.py`; update the ownership/surface fixtures with
  `scripts/generate_repository_contract_fixtures.py --rebaseline-surface`.
- Keep both adapters' `access_sql.py` and `mount_sql.py` predicates aligned. New notebook
  write endpoints use `require_notebook_capability(...)`; body-resolved identities use
  the same capability table, including its independent mirror-write fence. Preserve
  authorization-before-mirror-error ordering and the registered creator-owned report
  exception. Do not infer a grant kind from a nullable/empty principal id or bypass
  live read authority. Update the Memory authorization lock-site allowlist whenever
  the read predicate widens. The access-SQL and notebook-capability guards own these checks.
- Participant replacement belongs to `retrieval_participants.py`, only at retrieval
  consumption boundaries; `global_run.py` is its only installer. Authorization still
  uses the real mount/read predicate, and `source_scope.py` can narrow but never widen
  the attested set. Keep the exact reader/writer allowlists, actor/run binding,
  content-free cache fingerprint, and `ParticipantOverrideError` propagation.
  Register any new fail-soft handler that can read the seat in `_SEAT_FAILSOFT_SITES`;
  `_bounded_participants` must validate outside those handlers. Mode checks
  (`subjectless_run_active`) and filtering checks (`peer_scope_ceiling_active`) remain
  separate. `test_participant_override_guard.py` owns this security boundary.
- `RepositoryRuntime` owns mutable operational state; `REPORT_CANCELLATIONS` is the
  explicit process-global exception shared by identity with the coordinator and
  compatibility functions. Domain builders take earlier frozen bundles, never the
  runtime itself; retain the narrow late-bound accessors and startup side-effect order.
  Supported post-composition replacements must reach every retained consumer.
- Keep KG edge definitions in `domain/kg/edge_schema.py`; the service shim declares no
  new `EdgeSpec`. Production never imports `app.eval`, reconstructs provenance from
  scores, or adds a second selected-source rollout parser/activation path. New
  retrieval contributions preserve the frozen baseline and authorization before
  hydration; artifact and source-scope identities remain server-owned.
- Stopped-service maintenance uses `open_maintenance_cli_repository`, acquires its
  independent-session lock after the pre-factory safety checks, releases page-read
  connections before model work, and always closes. The live scale CLI instead uses
  its own composition root with `migrate=False, seed=False`, verifies schema before
  composition, and holds the per-notebook claim through publication. Reuse the shared
  artifact staging/swap and claim checks for every root, including rollback/retirement;
  never patch a live tree in place. See the [operational SOP](./operations.md).

### Frontend implementation guardrails

- Workspace hooks own their domain state. The shell consumes readonly views and named
  commands, never another domain's setters. Preserve exact actor/notebook/generation
  ownership, late-response rejection, deletion tombstones, single-flight work and
  existing request budgets. Navigation detaches durable work; explicit Stop cancels it.
  Register new notebook owners in `notebookTransitionSteps` and use the single
  `notebook-transition.ts` begin/commit/settle path; keep root-modal cleanup first.
- Use `api-client.ts` for HTTP mechanics and domain API modules for endpoint policy;
  production `fetch` outside the shared transport is forbidden. Root dialogs use
  `use-root-modal-coordinator.ts` leases: issue before async work, publish only for
  the current owner, make covered dialogs inert/ARIA-hidden, and return focus only
  after commit when the underlying lease is still current. Do not release an action's
  in-flight guard merely because its dialog closed.
- Reuse shared source/graph renderers and readonly props; do not move domain state
  into presentation components. UI extension declarations remain metadata-only,
  startup/build-frozen, and gated by exact tuple, capability, UI mode and owner.
  After adding a contribution, regenerate `backend/tests/fixtures/ui_extension_contract.json`
  with `scripts/generate_ui_extension_contract.py` and pass its `--check` contract.
  Keep the built-in registry's import closure `.ts` for Node tests. Local UI packages
  follow the [extension SOP](./deployment-extensions-sop.md), including its separate
  deployment acceptance gate; do not weaken the base zero-plugin registry assertion.
- Changes to group or schema panels must preserve their style and behavior guards,
  rather than duplicating CSS or using undeclared classes. Schema selection/drafts
  use `(object_type, proposal-status)` identity, writes commit visible results only
  to the originating pane, and `SchemaWriteOutcome` keeps `confirmed`, `unconfirmed`
  and `failed` distinct. A committed but unconfirmed write must not be presented as
  a failed write that should be retried.
- Keep exactly one element-level press baseline in `globals.css`:
  `button:not(:disabled):not([aria-disabled="true"]):active`, using `opacity: .7` and
  `filter: brightness(.88)`. No `transform`, `translate`, `scale`, `rotate` or baseline
  `transition`: geometry changes can swallow edge clicks and override positioning.
  Show action outcomes on the pressed control or immediately beside it; a page banner
  alone is insufficient. Long-running controls also stay disabled/replaced in flight.
- Clipboard feedback uses `useCopyResult` from `copy-result.ts`, keyed to the copied
  token/item and reset when its identity changes; its shared timer returns to idle.
  Keep literal result classes in JSX and `button.copy-result-copied` /
  `button.copy-result-failed` background and hover rules. On failure, select an adjacent
  read-only input only if `input.value === link` still holds. The
  `button-press-feedback-guard`, `long-task-button-guard`,
  `command-catalog-button-guard`, and copy-result component tests enforce this contract.
- Secure-Context-only browser APIs require shared fallbacks: client ids use
  `newClientRequestId()` from `client-request-id.ts`; clipboard writes use
  `copyTextSafely()` from `copy-text.ts`. Include pre-request submission setup inside
  the error boundary so synchronous failures restore the draft and release busy state.
  `secure-context-api-guard.test.mjs` guards the supported HTTP LAN deployment.
- Composer stop controls use `STOP_CONTROL_CLASS` and `StopGlyph` from
  `stop-control.tsx`, remain icon-only with `aria-label`/`title`, and disable while
  stopping. Labelled report action rows reuse only the glyph. Stopped-turn detection
  and copy come from `stopped-turn.tsx`; a stopped notebook turn must not reappear as
  a resendable storage draft. Reuse the existing Global Ask conversation reconciliation
  and missing-job paths rather than calculating replacement/history state at callers.
- Shared streams use `task_stream.py::deliver_ask_events` and the browser
  `ndjson-stream.ts` line reader; route modules do not import each other.
  `yieldToPaint` retains its timer fallback for background tabs. Access changes use
  `use-notebook-collection.ts::refreshAfterAccessChange` and the shell's narrow
  `reconcileOpenNotebook` effect so an open workspace is reconciled with its list.

### Schema and migration authoring

- SQLite schema changes append `_migration_N` and bump `SCHEMA_VERSION` in
  [the migrator](../backend/app/repositories/sqlite/migrations.py); never modify a
  sealed migration. Startup recovery, stable seeds and administrator upgrades remain
  outside the version gate and run every boot.
- PostgreSQL migrations append a gap-free numbered SQL file under
  [migrations](../backend/app/repositories/postgres/migrations/) and update
  [POSTGRES_SCHEMA_MANIFEST](../backend/app/repositories/postgres/schema_manifest.py).
  The [migrator](../backend/app/repositories/postgres/migrator.py) validates checksums
  and the ledger under its migration lock; never rewrite an applied SQL file.
  Current version numbers and per-version DDL are owned by these executable sources.
- Preserve fresh-install and upgrade behavior, null/default semantics, ownership,
  keyset ordering/collation, FK/cascade/unique surfaces, and copy/cleanup classification.
  Record table-specific design reasons beside the migration that introduces them.
  Update affected schema/seed/snapshot fixtures and migration manifests in the same
  change; do not rebaseline an old compatibility fixture to hide upgrade breakage.
- SQLite migrations are one-way. A database's future `PRAGMA user_version` fails
  startup with `schema contains a future version`; PostgreSQL similarly rejects a
  future ledger version. Readiness exposes only the redacted initialization failure.
  Recovery requires a pre-upgrade backup or a compatible/newer binary, not a reverse
  migration. See [migration execution and cutover](./operations.md#sqlite--postgresql-stopped-snapshot-migration-and-cutover).
- The frozen [v9 fixture](../backend/tests/fixtures/repository_v9/) and
  `scripts/verify_repository_snapshot.py` retain upgrade compatibility. Snapshot
  verification constructs repositories only on a temporary backup, checks exact
  per-version migrations/stable seeds and preserves the original DB/WAL metadata
  plus SHM existence/size (only live-WAL SHM mtime may differ); it never logs private rows.

Earlier per-version narratives and delivery explanations remain available in the
[pre-consolidation history](https://github.com/huyangc/silicon-notebook/blob/403b796f/docs/development.md#architecture-boundaries).
They are historical evidence; current runtime rules, migration sources and operational
procedures linked above take precedence.

## Verification

Run:

```bash
bash scripts/check.sh
```

The verification gates are tiered:

| Grade | Scope | Frequency |
| --- | --- | --- |
| G0 targeted | Tests selected for the files and behavior being changed | During the edit loop |
| G1 standard | `scripts/check.sh`: stable backend, contracts/harness, frontend tests, typechecking production build, and the package typecheck for test files | Local handoff and every PR/push/manual CI run |
| G2 extended | `scripts/check_extended.sh`: G1 plus real-index/performance, cold graph/index contracts, and repository-wide semantic scans (heavy subset) | Once daily at `17 18 * * *` UTC (02:17 Asia/Shanghai), plus manual dispatch |
| G3 PostgreSQL | `scripts/check_postgres.sh`: direct PostgreSQL adapter integration | Independent PR/push/manual CI job |

G1 runs three bounded lanes concurrently: `check_backend.sh` executes the stable backend pytest suite with default 12 backend pytest workers (override with `BACKEND_PYTEST_WORKERS`); `check_contracts.sh` executes syntax/dependency preflight, hermetic smoke paths, contract checks, and the deterministic extraction-scoring harness; `check_frontend.sh` executes every recursively discovered `*.test.mjs`, every `*.component.test.tsx`, the production frontend build, and the package typecheck. Node's test runner and Vitest are each capped at four workers, leaving CPU headroom for the backend critical path. The Next build must keep `ignoreBuildErrors` unset and stays the fail-closed typecheck for production code, but Next's build-time type checker silently filters out every diagnostic reported in `*.test.*`/`*.spec.*` files and `__tests__`/`__mocks__` directories (the `ignoreRegex` in `next/dist/lib/typescript/runTypeCheck.js`, verified on Next 15.5), so a type error that only exists under `frontend/tests/**` never fails the build. The frontend lane therefore runs `npm run lint` (`tsc --noEmit`) after the build — after, so it type-checks the freshly regenerated `.next/types` rather than a stale tree — as the one pass that sees those files; with `incremental` the warm re-check costs under a second (~5s cold), so the duplicate parse the lane once avoided is no longer a budget concern. Its backend lane excludes `slow` real-index/performance tests, `graph_index_contract` cold graph/index contracts, `architecture_contract_heavy` (the eight repository-wide semantic scans in `_ARCHITECTURE_CONTRACT_HEAVY_TESTS`; the remaining lightweight `architecture_contract` tests run in G1), and the PostgreSQL tree. G2 first runs G1 and then the exact complementary backend marker set — `backend/tests/test_test_architecture_policy.py::test_verification_lane_markers_partition_every_architecture_contract_test` proves that split empirically via `--collect-only`, not just by pinning the two `-m` strings. Each lane has its own process group, so interrupting or terminating the controller also terminates and reaps pytest, npm, and Next.js descendants. The official-client MCP smoke pins exactly the 28 published tools: seven Memory/context, four knowhow, one citation point-read, seven source, three build, two notebook-understanding tools, and four independent global-Ask tools. Missing `frontend/node_modules` is a hard failure rather than a silent skip.

Use the project’s Homebrew/Miniconda interpreter for acceptance:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

The Apple Silicon warm gate hard target is at most 60 seconds. CI lane timings are observational only, so this measured local target is not a portable timeout assertion for every CI host.

Keep test-speed changes result-preserving. The G1 standard and G2 extended marker expressions are exact complements, while PostgreSQL stays independently authoritative; never make a committed test unreachable. Ordinary unit and standard-gate tests are hermetic: they do not bind host ports or depend on ambient services; self-contained subprocess/signal coverage is reserved for contracts that are intrinsically process-level. Cache repository-wide AST/protocol parsing once per test process (pytest worker or isolated Node guard process), and expose a membership-only projection when a frozen-fixture test does not need detailed sites, signatures, or ownership. An executable repository guard gets one real-tree invocation in its owning contracts lane; unit tests for argument parsing, failure modes, and extra roots redirect its default roots to minimal fixtures rather than rescanning the repository. Assertions over the same immutable behavior matrix belong in one labelled traversal so they do not rebuild an identical database world per row or assertion family. The frontend lane syncs its immutable local-plugin projection once, then suppresses only npm's redundant `pretest`/`prebuild`/`prelint` hooks; those hooks remain mandatory for each standalone developer command. Test cache/container policy through the policy object instead of constructing unrelated database and ANN artifacts; and derive autouse isolation paths from the worker's existing pytest base temp rather than allocating a new `tmp_path` directory for every pure test. Ordinary SQLite repository tests copy a current empty schema built once per pytest worker, but every test keeps an independent mutable database file; migration, upgrade, and repository-snapshot modules stay on the real migration ladder through `_REAL_SQLITE_MIGRATION_MODULES`. Repository-heavy tests may reduce only the default password-hash cost in the pytest autouse fixture: authentication helpers retain the production default, and credential-field snapshot modules remain in `_REAL_PASSWORD_HASH_MODULES`. Concurrency tests use events/barriers for ordering and fairness assertions rather than fixed sleeps or assumed thread wake-up order. When queued work runs in waves, a controller thread must release observed capacity with events instead of leaving a later wave alone in a cyclic barrier. Delayed process-global jobs must be cancelled and reaped in shared teardown before per-test repositories close; cleanup scoped to one repository object cannot contain route-owned work.

Test fixture cost follows the behavior under test. Scale-build lock admission and
handoff tests use an unindexed notebook; real build/fold/publication tests retain
their seeded or indexed artifacts. Scale CLI unknown-notebook refusals use a real
migrated database without ingestion, KG or vectors; the non-PostgreSQL URL gate
uses a reachable SQLite file. Full CLI artifact flows still ingest, extract and
embed, and their read-only notebook lookup does not repeat migration/seeding.
Normal stage-event and progress-callback
assertions share one facade build, while callback failures and the direct builder
artifact contract remain separate. The three notebook lifecycle literal scans
share one `xdist_group` so their process-local AST cache is actually reused;
the G1/G2 selection expressions remain unchanged. Frontend source-policy checks
reuse immutable module inputs and parsed trees within their isolated guard process.

Timer tests advance controlled clocks through the original deadline, including
the before/after state, instead of waiting in real time. Background-delete HTTP
tests hold and release the real runner with events; the startup sweeper test waits
for an observed periodic sweep and checks that its thread exits on close. UI
vocabulary non-vacuity on the real repository belongs to `check_ui_vocabulary.py`
in the contracts lane; unit tests retain the minimal empty-scan failure fixture
and mutation coverage without a second full-tree count.

Extension-service lifecycle tests share one `xdist_group` to bound competing
supervisor startups; each test retains its own real process/thread concurrency.
State-file regression tests force replacement after open: JSON readers may consume
the already-open, owner-owned regular-file snapshot after its final link disappears,
while lock/writable handles still require one link and unsafe files remain rejected.

### GitHub Actions CI

`.github/workflows/ci.yml` exposes G1 as `CI / level-1-standard` for pull
requests targeting `master`, pushes to `master`, and manual dispatches.
`.github/workflows/daily-extended.yml` exposes G2 as
`Daily Extended Gate / level-2-extended`, with one daily cron and a manual
dispatch only. Both use `ubuntu-24.04`, Python 3.13, Node.js 22, install from
the declared lock/requirements files, and delegate selection to their matching
wrapper script. G3 remains `CI / level-3-postgres-integration`.

`CI / level-1-frontend-node26` re-runs the frontend lane and the production build
on the current Node.js major, on the same triggers as G1. The documented floor is
"Node.js ≥ 20" while G1 pins 22, so without this lane the upper half of that promise
is unverified: Node ≥ 24 ships built-in Web Storage globals whose getters return
`undefined` unless `--localstorage-file` is passed, and vitest's jsdom environment
lets them shadow jsdom's own — every component test that touches `localStorage`
fails on a developer's machine while CI stays green.
`frontend/test-support/setup.ts` restores real jsdom storage, and the matching
`Storage` class so `vi.spyOn(Storage.prototype, …)` still intercepts, only when the
built-ins read as `undefined`; Node 22 behavior is byte-identical. The lane also runs
the build, which is what caught that fix's first attempt importing untyped `jsdom`.

The PR/push G1 workflow runs the backend, contracts, and Node 22 frontend lanes
on separate runners. Backend uses two shards of the same G1 collection, with four
pytest workers each. `check_backend.sh --shard-index 0 --shard-count 2` (or index 1)
enables sharding explicitly; no sharding environment leaks into nested pytest.
The plugin partitions after normal marker selection, keeps each `xdist_group`
together and ordinary modules intact, and balances deterministic units using
`backend/tests/fixtures/g1_module_timings.json` with a collected-count fallback.
Timing hints affect placement only: new modules are included automatically.
`check.sh` remains the complete local gate, and G2 selection is unchanged.
The existing `level-1-standard` check aggregates all three lane jobs and fails
unless every result is success, including both backend matrix entries; skipped,
cancelled, failed, or missing dependencies cannot pass. Measure G1 wall time from
the first lane start through aggregate completion, including installation and
runner scheduling, rather than reporting the aggregate's short runtime alone.
This isolation and sharding spend more runner minutes to reduce elapsed time.
Backend and contracts wrappers preserve offline environment isolation when run
directly, print slow pytest durations, and write JUnit reports under `backend/.local`;
CI uploads them for seven days, including on failure.

Six production-source guard scans share one worker and content-validated AST cache;
every lookup rereads the file, so edits cannot reuse a stale tree. Agent Profile
job base/overlay tests use independent copies of the current schema, while their
dedicated store/upgrade tests retain real migrations and old-schema assertions.

The committed OpenAPI contract is byte-semantically frozen, so
`backend/requirements.txt` pins FastAPI `0.135.3` and Pydantic `2.12.4`
exactly. Upgrade either framework only together with an intentional OpenAPI
contract regeneration and a clean-environment G2 extended-gate run.

The workflow is read-only, does not receive model or deployment secrets, and
uses four backend pytest workers per runner to avoid oversubscribing the hosted runner.
Backend installation sets `HNSWLIB_NO_NATIVE=1`; G1 Python lanes share G3's dedicated
portable-wheel cache with an exact OS/architecture/Python/requirements/policy key
and no fallback to another cache. G2 continues to disable pip's wheel cache:
`hnswlib` otherwise builds with `-march=native`, and a cached locally built
wheel can crash with `SIGILL` when restored on a hosted runner with different
CPU features. The portable build trades a small ANN speedup for deterministic
CI; production wheelhouses may still target their declared deployment CPU.
Each G1 execution lane's 20-minute timeout includes dependency installation and is intentionally
separate from the under-60-second local Apple Silicon warm-gate target.
`CI / level-1-standard` is initially observational; make it a required `master` check
only after stable green pull-request and post-merge runs have been observed
and the user explicitly approves the branch-protection change.

PostgreSQL coverage is deliberately separate from the offline gates. The
`level-3-postgres-integration` job starts PostgreSQL 16 and runs
`bash scripts/check_postgres.sh`, selecting `postgres_integration or postgres_lane_contract`.
The latter includes hermetic adapter/migration contracts and launcher/target safety
checks in the lane that owns them.
Only the CI service stores PGDATA on a bounded 4 GiB tmpfs to avoid hosted-disk
overhead from disposable schemas. WAL, `fsync`, `synchronous_commit`, and
`full_page_writes` retain their defaults; provisioning checks the three settings.
The job records storage usage and container peak memory. This lane tests live
database behavior, not persistence across container loss or host power failure;
production and local PostgreSQL storage are unchanged.
CI provisions four explicit groups of primary, non-C UTF8, and non-UTF targets
before handing off to the unchanged least-privilege application role. The
`TEST_POSTGRES_TARGETS_JSON` array contains four objects with `primary`, `non_c`,
and `non_utf` URL fields. All twelve databases must be distinct on one explicit
server endpoint. The launcher validates and preflights every target before starting
four pytest workers, maps each worker to only its own three databases, and keeps
passwords in the temporary pgpass file. Missing/duplicate targets, mixed serial and
parallel configuration, or an unexpected worker fail closed; worker restart is disabled.
Each test still creates its own schema and runs real migrations. This database-level
isolation avoids contention on the fixed migration advisory lock. Lock-observation
queries must filter the current database; a server-global activity view is not isolated
by schema. The batch4 read-only plan matrix shares one unchanged large corpus,
while online installation and migration mutation tests retain independent schemas.
The batch2 payload plan matrix seeds its 100k-row notebook once: it first checks
the rare term's natural plan, then adds 20k foreign-notebook hits and re-analyzes
before checking scoped bitmap access and zero local results. Both original plan
assertion sets remain reachable; neither corpus scale nor planner checks are reduced.

Local verification can still use an installed PostgreSQL 16 service and an explicit
`TEST_POSTGRES_URL` for the serial lane; do not also set the parallel JSON variable.
Auxiliary targets remain mandatory in authoritative CI. `scripts/check.sh` must never
start or contact PostgreSQL. The PG launcher always reports slow setup/call/teardown
durations and writes `backend/.local/postgres-junit.xml`, uploaded by CI even on failure.
It does not forward arbitrary `PYTEST_ADDOPTS` into its isolated child environment.
The PG dependency cache uses a dedicated portable-wheel directory and an exact
OS/architecture/Python/requirements/policy key, with no generic fallback cache;
`HNSWLIB_NO_NATIVE=1` remains mandatory. The whole PG job is measured against a
three-minute optimization target; a successful test result alone does not prove it met
that target, and cold-cache installation time is included in the observation.
The lane covers direct PostgreSQL behavior only; retired tests for the SQLite
backend implementation, SQLite-to-PostgreSQL import/forward-shadow, and
cross-backend parity are not active coverage.

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

Efficiency is a first-class engineering constraint. Before adding an LLM,
embedding, or database call, evaluate whether the work can be combined,
cached, deferred, made asynchronous, or gated until the user-visible surface
needs it. Strong consistency and eager computation are explicit opt-ins;
ordinary paths stay low-overhead by default.

Cross-cutting frontend interaction guardrails stay small but explicit:

- A control that starts a long-running action becomes disabled immediately and
  shows a busy label or spinner until that action settles; server-side
  single-flight is not a substitute for local double-submit protection. When a
  new entry point falls outside the bounded long-task guard, extend the guard
  or record the gap in review.
- New centered or draggable dialogs reuse `FloatingModalCard` and publish
  through the root modal coordinator instead of recreating overlay, dragging,
  focus, palette, border, or radius behavior.
- Source-activity anomalies render only through `AnomalyBadge` backed by
  `sourceAnomalies()`; do not hand-roll inline warning styles or symbols.
- Five-grade effort selectors reuse
  `frontend/app/effort-picker.tsx::EffortPicker`; do not duplicate the range
  control or recreate its popover. `frontend/tests/unit/effort-picker.test.mjs`
  guards the shared implementation and its callers.

## Development Workflow

For every task that will write repository code, tests, documentation, or configuration, create a new linked git worktree and branch before the first write, complete and verify the work there, and open any resulting PR from that branch. The main local checkout stays read-only for the task; tiny fixes are not exempt. If the current directory is already an isolated linked worktree, keep working there. Pure research, design, status, and review-only work does not require a worktree.

Identify the requested outcome and authorized scope before acting. Review and design tasks deliver findings or proposals; implementation tasks deliver verified changes. A review request does not authorize code changes or remote delivery, and a local implementation request does not authorize deployment, external messages, PR creation, merging, or other remote mutations. Within the authorized scope, make routine implementation decisions and continue until the requested outcome is complete; state material assumptions. Ask only when missing information materially changes correctness, scope, compatibility, or authorization and cannot be resolved from available evidence. Continue independent work while awaiting an answer. Existing authorization remains valid within its stated scope; do not request the same approval again.

For approved multi-step implementation plans, use subagent-driven development by default: assign each task to a fresh implementation subagent and require task-scoped specification and code-quality review before moving on. Small tasks, research, design, status, and review-only work do not require subagents. Delegate only bounded work with clear file/task ownership, acceptance criteria, and explicit verification responsibility. Independent tasks may run concurrently when their ownership does not conflict; do not split a small task merely to use more agents. Review delegated results against acceptance criteria and specific concerns instead of repeating the entire task.

Run focused checks while editing and the required standard gate before claiming implementation complete. Once both pass, stop verification unless subsequent edits, failures, or a specific unresolved risk justify another run. This stopping rule does not waive required verification lanes or the PR review and CI checks below. Do not add tests that merely mirror implementation details. Read-only review inspects the diff and the submitter's verification evidence; it does not mutate the tree or rerun the full gate unless explicitly requested.

Keep handoffs concise and outcome-first: report changes, checks run, checks that could not run, and remaining limitations without repeating the execution history. For reviews, state the reviewed revision and scope; tie each finding to a concrete trigger, impact, and code location, and distinguish verified behavior, static reasoning, and architectural debt.

Shared development, verification, and delivery rules are owned by this document and its Chinese pair. `AGENTS.md` provides the general agent entry point and canonical routing; `CLAUDE.md` supplements it with Claude Code-specific resident rules. Resolve conflicting shared rules against their canonical owner and correct the entry points; carrier-specific rules apply only to the carrier they name. Neither entry point is a duplicate product or architecture reference.

Claude Code auto-loads `CLAUDE.md`. Its hardest Claude-specific rule is that **spawning a subagent must state the model explicitly instead of inheriting the main agent's** — tiered by how much judgment the task needs: `opus` for judgment work (writing plans, review, architectural trade-offs, hard diagnoses), `sonnet` for transcription-shaped implementation whose spec is already pinned down, `haiku` for pure search and location. The PreToolUse gate `.claude/hooks/require-subagent-model.py` enforces it: a call that passes no `model` and whose `subagent_type` is not pinned to a model in `.claude/agents/` is denied. Three pinned roles ship in `.claude/agents/`: `impl-task` (sonnet), `spec-review` (opus), and `code-quality-review` (opus). `backend/tests/test_claude_subagent_model_hook.py` is the hook's regression net — it runs the real script over a subprocess boundary and covers both directions, the bypasses that would let an inherited-model call through and the false denials that would push people to work around the gate.

A pull request must be reviewed by codex before it is merged, and **every round's raw output is posted verbatim to the PR** — rounds that raise nothing included, rounds run by hand included — alongside the trigger, the exact command, the head SHA, and the exit code with output size, so a reader can confirm the run happened and was not paraphrased away. A round counts as successful only when the exit code is zero **and** the output is non-empty: a review killed by SIGTERM also exits zero, and trusting the exit code alone posts an empty comment that reads as a pass. P0/P1 findings block: verify them, fix what holds up, and re-review until the verdict is non-blocking — stop for a human decision only when the finding does not hold up (then follow the rejection rule below) or when the fix itself needs a human call; P2/P3 do not block and may be declined with a stated reason; output whose priority tags cannot be parsed blocks conservatively instead of defaulting to a pass. A finding may be rejected on the merits — codex reviews the diff and does not always know the runtime facts — but a rejection must carry its reasoning and evidence on the PR, a comment recording the trade-off in the code, and a regression test pinning the behavior that was kept. Merging does not require a fresh approval on every PR: once the review is non-blocking **and** CI is fully green, merge with `--rebase`. Never merge while findings block or the review output cannot be parsed — fix and re-review first — and never merge when CI is not green or the user has said they will merge it themselves. CI counts as green only when `gh pr checks` reports every check as `pass` — `mergeStateStatus: CLEAN` means nothing is blocking the merge, not that the checks ran green. Before merging, confirm on the PR itself that a review for the PR's **remote** head (`headRefOid`, never local `git rev-parse HEAD` — a stale local checkout matches an older review while the merge takes the unreviewed remote head) has been posted: review automation that silently never fired looks exactly like one that passed, and neither the agent's report nor the hook's local state is evidence — only the comment on the PR is. The review automation itself is a per-developer Claude Code hook rather than a repository artifact, so a fresh clone will not have it; the rule stands regardless, and `CLAUDE.md` documents the manual command.

### Test architecture

- Size-independent boundary branches may lower only a test-local threshold while separately pinning the production floor. Assertions over several views of one immutable index/artifact share one real build; arithmetic- or observability-only branches use a minimal owned seam, while adjacent integration coverage still builds, opens, and queries the real artifact.
- Backend and frontend static contracts use semantic identities such as module path, qualified scope, operation kind, target, and reviewed count. Source positions are diagnostic metadata only; line numbers, source offsets, CSS order, and source slices must never identify an expected site.
- Frontend tests never live beside production code: `frontend/tests/unit` contains `node:test` pure-logic cases, `frontend/tests/guards` contains architecture/security/vocabulary/entry contracts, and `frontend/tests/component` contains Vitest/jsdom/Testing Library behavior tests. Shared setup and semantic source adapters live in `frontend/test-support`; the runners recursively collect these directories, and a location guard rejects tests under `frontend/app` or `frontend/features`.
- Component behavior must not be pinned through CSS geometry or source layout. A routine feature refactor should change tests only when its observable contract changes.
- Every new static or semantic guard must be mutation-verified before landing: prove that both deleting the protected behavior and moving the violation to another relevant site make the guard fail, and first confirm that each mutation actually changed the intended site. Remove the mutations after recording the result; a green test against an ineffective mutation is not evidence.
- Committed tests may not be disabled with skip/xfail/todo/only. Repository policy tests enforce this across test entrypoints and their helper modules, and prevent direct production-source reads outside the shared semantic-source adapter.
- The frontend source policy is intentionally bounded: it rejects AST position/collection-order APIs and source-named text position operations syntactically, while the shared `semantic-source.mjs` adapter may expose AST semantics but may not use text slicing, splitting, indexing, or length as a contract. Do not replace this with whole-JavaScript data-flow interpretation; ordinary array operations stay valid.
- Backend test startup prewarms one repo-local Matplotlib font cache before xdist workers start. Keep that controller boundary: letting each graph worker enumerate macOS fonts independently adds avoidable multi-second cold starts.

## Documentation Maintenance

Configuration changes must identify their intended operator and owning reference. Keep
`.env.example` a curated starter for common deployment choices (connections, credentials,
model compatibility, capacity, and usage/retention policy), not a mirror of every
`Settings` field. Show optional default overrides as commented assignments so copied
environments can inherit improved code defaults. Advanced tuning, recovery controls,
experimental rollouts, and extension budgets belong in the paired deployment/operations
references; standalone tool settings belong in `scripts/README.md`. Settings defaults and
validation remain owned by `backend/app/core/config.py`; invariant protocol bounds use
named constants. On additions, changes, and removals, update the relevant reference and
only update the starter when that common deployment surface changes. Mark accepted but
unused compatibility fields explicitly and omit them from active configuration guidance.
Tests must check parsing/behavior and the owning reference, not demand full starter-field
parity. Quote empty values when adding inline comments (`KEY="" # explanation`) so shell
and dotenv readers agree. Never rewrite an existing deployment's `.env` as part of template
maintenance.

Update every canonical document whose owned surface actually changes; one change may affect product, deployment, operations, and development surfaces together. Maintain English/Chinese pairs together: `product-and-api`, `deployment-and-configuration`, `operations`, or `development`. Update the root README pair only when its quick start, high-level current boundaries, or navigation changes. Update `AGENTS.md` only for repository-wide agent workflow/routing rules and `CLAUDE.md` only for Claude Code-specific resident rules. Tests must validate each canonical owner rather than requiring detailed facts to be duplicated in entry files.

Because Claude Code auto-loads `CLAUDE.md`, `scripts/check_claude_md_budget.py` pins its total character count and longest line as exact baselines in the G1 contracts lane. Any size change must update those baselines in the same PR so no unaccounted headroom accumulates. Keep feature-level contracts in their canonical documents; change `CLAUDE.md` only when its Claude-specific resident rules or routing change.
