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
  static dependency graph stays acyclic. The backend factory and both backend wrappers
  live in `backend/app/services` (`repository_factory.py`, `sqlite_repository.py`,
  `postgres_repository.py`): services may import repositories, never the reverse, and the
  remaining repositories→services edges (the filesystem scale artifact store's
  `services.kg` imports) are debt pinned by `repository_service_import_ceiling`.
  Domain routers own endpoint bodies;
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
  scope, retrieval run, actor, cancellation token, and connection probe. Evidence-only Ask
  (`output="evidence"`) is refused at the commit boundary: it never saves an answer or conversation. Revalidate
  authority at the existing boundaries; violations fail loudly. Stage wrappers hold
  neither a database connection nor an outer leaf-I/O slot. Core owns final audit and
  persistence. Post-terminal observers cannot rewrite committed artifacts or reverse
  `done`; `report.completed_observer` also cannot start retrieval/model work. Keep Ask's
  existing observer order, failure isolation and cost when changing its completion path.
- `scripts/check_architecture_boundaries.py` enforces the G1 ratchets in
  `scripts/architecture_boundary_baseline.json`: repository-to-service debt cannot grow;
  the facade public surface, core/model-to-service imports, listed hot-function lengths,
  and repository Protocol-method counts have zero slack. Lower the baseline in the same
  change whenever one shrinks. The same script fails when a facade attribute that is
  named only at run time no longer exists: a `_repository_from_weakref(...)` seam in
  `repository_facade.py`, or a literal `getattr`/`hasattr` probe on a facade-shaped
  receiver in `backend/app` or `scripts`. Those callers degrade silently instead of
  raising, so the audit cannot see them. It also fails when a protocol the facade is
  handed out as (`cast(P, repository())`, or `-> P` returning `repository()`)
  declares a method the facade lacks. The gaps that predated the check were listed in
  `facade_protocol_binding_gaps` with zero slack and are all closed, so that list is
  empty and any gap fails. Retire facade members against the
  [caller ledger](./superpowers/plans/2026-08-23-facade-retirement-ledger.md), reproduced
  by `scripts/audit_facade_callers.py`; update the ownership/surface fixtures with
  `scripts/generate_repository_contract_fixtures.py --rebaseline-surface`.
- Keep both adapters' `access_sql.py`, `mount_sql.py` and `memory_sql.py` predicates
  aligned. `memory_sql.py` is the shared definition point of who may read a
  Memory-derived source and of which KG rows count as Memory-derived; new readers import
  it (`source_store.hidden_source_ids` and the source-read gate
  `sharing_store.source_notebook_id(viewer_id=...)` already do). Readers that predate it
  (the Memory-owner predicates in `postgres/chunk_store.py`) still carry their own
  predicate and move onto it in the change that touches them.
- `mount_sql.py`'s participant set follows the viewer (M3): the store methods that
  resolve a mount (`participant_*`, `resolve_participants`,
  `notebook_has_usable_base_kg`, `mounted_bases_row`, `mounted_base_ids`,
  `any_mounted_has_kg*`, `follow_start_row`) take a required `viewer_id`. Service code
  passes `retrieval_run.current_viewer_id()` (the run's actor, else the request user,
  else `''`, which leaves only public and `everyone` libraries) or an explicit viewer
  at a site registered in `backend/tests/test_mount_viewer_guard.py`; never a literal.
  Process caches built over a participant set key on the viewer's effective set
  (`graph_retrieval._participant_graph_cache_key`; the viewer digest has its own
  BLAKE2s personalisation, apart from a participant override's, and
  `vector_cache.quota_bucket` drops the 16-hex fingerprint segment so every
  variant counts against its family's quota).
- Retrieval scopes have one installation per entry kind: every Ask entry goes through
  `AskService._retrieval_ceiling` (`source_scope.default_ceiling_context` over
  `RepositoryRuntime.ceiling_readers()`), report phases through
  `ReportExecutionCoordinator._default_ceiling` / `ReportEngine._refreshed_ceiling`, and
  global runs through `global_run.global_ask_run`; `backend/tests/test_default_ceiling_guard.py`
  pins that with equality assertions, so a route, MCP tool or engine that enters
  `source_scope_context` itself fails it. Both constructors and the global run set
  `ceilings_total`: a library without a per-library entry participates in nothing. A new
  producer that reads a scope's own notebook must take its source list from
  `scoped_allowed_source_ids` (which returns `None` when the run's `ceiling_binds` verdict
  proves nothing can be excluded) and verify an unbound read with `verify_unbound_read`.
- New notebook
  write endpoints use `require_notebook_capability(...)`; body-resolved identities use
  the same capability table, including its independent mirror-write fence. Preserve
  authorization-before-mirror-error ordering and the registered creator-owned report
  exception. Do not infer a grant kind from a nullable/empty principal id or bypass
  live read authority. Update the Memory authorization lock-site allowlist whenever
  the read predicate widens. The access-SQL and notebook-capability guards own these checks.
- Knowledge-graph rows derived from a member's Memory belong to that member (ruling M1); keep
  the readers, builders and guards below in step:
  - Graph-build readers use `memory_derived_in_notebook` (the notebook-bound classifier) and
    bind every by-id object probe to the referring row's notebook; the unbound
    `memory_derived_object` makes PostgreSQL hash every Memory source of the deployment.
  - KG readers take `viewer_id` with one meaning (`memory_sql.memory_viewer_filter`): `None` =
    every row, `""` = no Memory-derived row, a user id = no row derived from another member's
    Memory. For row readers `None` keeps the statement text unchanged; for the count readers
    `None` keeps the results unchanged with zero live reads. Probes are bound to the row's
    notebook (`foreign_memory_in_notebook_excluded`, `memory_derived_in_notebook`). The
    type-count memo holds, per `(kg_reset_epoch, kg_mutation_seq)`, the shared half and every
    member's Memory half from one statement (total minus the Memory-source-driven count);
    `None` sums them, `""` reads the shared half, a user id adds one live read of the viewer's
    own half (`own_memory_source`). Reads driven by Memory sources bind the source ids as an
    `= ANY(ARRAY(subquery))` InitPlan on PostgreSQL; on SQLite they pin the join order
    (`CROSS JOIN`) or use an uncorrelated `IN` / `NOT IN` list, and write
    `hidden_type_index_term`.
  - The five `memory_sql` fragments and their consumers (both backends unless named):
    `memory_derived_in_notebook` (no bound parameter) — the shared tooling
    `duplicate_member_rows`, `edge_centrality_source_rows`, the `""`
    branch of `memory_viewer_filter` and the graph-build readers;
    `foreign_memory_in_notebook_excluded` (one parameter) — only through
    `memory_viewer_filter`; `memory_viewer_filter` (three-valued) — the row readers of both
    `knowledge_store` modules, PostgreSQL `search.py`'s `_candidate_rows_for_terms` and
    `notebook_knowledge_rows` (PostgreSQL `query_store.search_notebook` reaches it through the
    latter) and SQLite `query_store.search_notebook`; `own_memory_source` (one parameter) — both
    `knowledge_counts_cache.memory_type_status_counts`, both `knowledge_store` `own_memory_only`
    overlay reads and both `query_store.notebook_has_kg` own-Memory EXISTS;
    `hidden_type_index_term` (no bound parameter; a SQLite planner hint, named alike on both
    backends) — SQLite's two-halves count statement and own half, `knowledge_store`'s overlay
    read and `_fts_viewer_filter`, and `query_store.notebook_has_kg`'s own-Memory EXISTS. The
    earlier fragments gain consumers too: SQLite `knowledge_store._fts_viewer_filter` uses
    `memory_source_readable` and `memory_source_type_predicate`; both `knowledge_counts_cache`
    modules use `memory_source_type_predicate` for the Memory CTE of the two-halves statement.
  - `kg_viewer_scope.KgViewerScope` is the Python twin of the `memory_sql` viewer fragments (an
    object or relation is hidden only by its own source; each evidence item by its own), pinned
    on both backends by `test_kg_viewer_scope_twins*`; services pass `viewer_id` to a KG store
    reader only through `store_viewer_kwargs`, i.e. only when the notebook holds Memory the
    viewer may not read. The assembly tripwires in `test_kg_viewer_scope_assembly.py` verify
    that the switch (`STORE_READERS_TAKE_VIEWER_ID`) matches the keywords the stores take and
    that the isolation-marker reader is wired.
  - For a notebook without a Memory source, scale and viz artifacts are byte-identical to those
    built before the Memory isolation except in three manifests: the main and viz manifests gain
    `memory_isolation`, `memory_sources_digest` and `shared_content_digest`, and every version
    list — the main, the viz and the source-partition companion's `parent_version` — carries the
    pair (`memory_isolation`, 1), which every notebook's version now has. Their builds run the
    same statements, with three differences: every object and relation read carries a
    per-statement Memory exclusion (a hashed `NOT IN` subquery; on PostgreSQL it measured the
    same as an unfiltered read, on SQLite a whole-table read costs 13–30 % more); there are
    Memory-source reads and Memory object / relation / cluster id reads driven by the (empty)
    Memory source set; and the viz derive reads a three-column projection instead of whole
    rows. No statement is chosen by an earlier Memory probe: under per-statement isolation a
    first Memory committed between the two would outdate the probe, so the source list (the
    watermark and the fold delta) excludes Memory in the statement for every notebook, and on a
    notebook without Memory returns the unfiltered read's rows through the same plan nodes
    (plan pins on both backends). The version signal does not probe for Memory. The ANN feeds
    and the membership leg filter by Memory id sets read up front; the build keeps a running
    union of the Memory rows it observes and retries, then discards, only when newly appeared
    rows are held by the artifact. A fold observes Memory before and after its reads; if any
    node, ANN label, relation label or delta source it would publish is Memory, it publishes
    nothing, emits `scale_fold_refused` (`memory_reached_fold`) and runs the full build.
  - `backend/tests/test_memory_chunk_write_guard.py` scans `backend/app`, `scripts/` and
    `examples/` (and every `.sql` file there outside a `migrations` directory): every write of
    `chunks` must refuse a Memory source before the write or be listed with a checked reason,
    and `sources.source_type` is written only by INSERT.
  - `backend/tests/test_memory_reader_guard.py` keeps the reader inventory closed: every
    function or SQL constant under `backend/app/repositories` that reads `knowledge_objects`,
    `knowledge_relations` or `concept_clusters` by notebook is registered by name in one
    category (viewer-taking reader, own-Memory read, shared tooling or build reader that
    excludes all Memory, the count cache, key-, ceiling- or caller-bounded read, derived layer,
    state probe, Memory side, copy, maintenance). An unregistered reader fails with its
    `file:line`, a registered name that no longer reads fails too, and the viewer, own-Memory,
    exclusion and count-cache categories are checked against the code.
- Participant replacement belongs to `retrieval_participants.py`, only at retrieval
  consumption boundaries; `global_run.py` is its only installer. Authorization still
  uses the real mount/read predicate, and `source_scope.py` can narrow but never widen
  the attested set. Keep the exact reader/writer allowlists, actor/run binding,
  content-free cache fingerprint, and `ParticipantOverrideError` propagation.
  Register any new fail-soft handler that can read the seat in `_SEAT_FAILSOFT_SITES`;
  `_bounded_participants` must validate outside those handlers. Mode checks
  (`subjectless_run_active`) and filtering checks (`peer_scope_ceiling_active`) remain
  separate. `test_participant_override_guard.py` owns this security boundary.
  Enumerated-item citations in a subjectless run use the run's frozen participant
  set, which the global-ask manager authorised with that read predicate.
- Every caller of the enumeration and catalog entry points (`enumerate_elements`,
  `enumerate_kg_objects`, `enumerate_sources`, `resolve_source_title`,
  `collection_map`, `collection_map_text`) passes `ceiling_binds` explicitly, with
  the run's verdict from `reasoning_retrieval.ceiling_binds_for_run`; the default
  binds and over-filters. `test_every_collection_entry_call_passes_the_ceiling_verdict`
  pins the Ask, reasoning and catalog-overview callers. Its knowledge re-read twin,
  `source_scope.ceiling_binds`, has one more arm (another member's Memory in the
  library); listings do not need it because they exclude private Memory
  unconditionally. Keep the other arms of the two verdicts in step. Both are
  memoised per run only together with verify-on-read: a collection read on the
  un-bound fast path checks the source rows it holds (signal rows, each KG row by
  the bound path's criterion, the Knowhow catalog's projection sources) against the
  frozen ceiling, and one outside it records the drift
  (`source_scope.record_collection_ceiling_drift`) so that read and every later one
  binds.
- Retrieval legs that may read without the source list -- the chunk lanes and
  contribution hydration, exact lookup, the mix walk and its overlay backstop, the
  weak-support hint -- read the run's verdict (`source_scope.run_ceiling_binds`) ONCE,
  before the read: `unbound_ceiling(nb)` returns the frozen ceiling the read is then
  verified against (`verify_unbound_read`), or `None` (bind: take the list) -- never
  "no list" from one reading and the ceiling from a later one. A store that binds the
  ceiling gets the run's `CeilingSet` (`bindable_library_ceiling`) uncopied and reads
  its `bound_forms` memo, keyed by backend and SQL form, before normalising the ids.
- Relation lines of the answer context bind no source list: a library covered by a
  source ceiling reads its relation rows with `source_id` (one row per edge and source)
  and judges them in Python against the run's frozen ceiling — filtering and counting
  when the ceiling binds, verifying on read (a row outside records the drift) when it
  does not. Every HTTP question with a frozen include ceiling therefore pays this read
  (measured +0.3–9 ms on a 49k-source library, growing with sources per edge). A
  cheaper verification (comparing `MAX(created_at)` of the rows with the freeze
  instant) would need the scope to carry its freeze timestamp; it is not implemented.
- Per-notebook retrieval caches shared by every asker (keyword tokens, the relation
  vector matrix) are built only from content no Memory source derives, so who builds
  them first never changes what they hold and their keys stay the notebook's alone.
  Memory-derived relations live in the same cache entry, apart, with their source, and
  are masked per request by the asker's ceiling; objects whose evidence a ceiling
  trimmed are tokenised live. The cold build maps Memory relations to their sources
  with one `relation_delta_rows(with_source_id=True)` read per 900 Memory sources
  (1,000 Memory sources beside 20k shared relations: PostgreSQL 0.90 s → 0.70 s,
  SQLite 6.1 s → 3.7 s against one read per source).
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
- Every non-serving entry point is a schema guest: open the database through
  `app.services.maintenance_cli` (`open_cli_repository`, `open_maintenance_cli_repository`,
  `create_schema_guest_repository`, or `create_bare_schema_guest_repository` for a tool that
  never had extension seats), never `create_application_repository(settings)` /
  `SQLiteRepository(settings)` with their seeding defaults. The version gate
  (`app.repositories.schema_currency`) and its single refusal wording are shared; refusals
  exit 3 via `MaintenanceCliError.exit_code`. Only server startup and `database migrate`
  own the schema. `backend/tests/test_schema_guest_guard.py` fails any construction outside
  the serving process, the listed guest entries and the listed schema owners — writing the
  guest keywords yourself counts too, because it skips the version gate; a tool that really
  owns a scratch database is listed there with its reason. Connection failures surface only
  the fixed credential-free wording (exit 2).

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
- `KnowhowPanel` (`knowhow-panel.tsx`) keeps the panel's top-level state machine
  (selected table and row, which modal is open, table detail, citation jumps,
  table-level writes, `handleCellSave`) and every mount point. Domain state that
  is not shared lives in three owner hooks: `use-knowhow-table-list.ts`,
  `use-knowhow-row-code.ts` and `use-knowhow-concept-matrix.ts`. The list, grid,
  row drawer and badges are presentation modules, the self-contained modals have
  their own modules, and the stylesheet is `knowhow-panel-styles.tsx`, rendered
  once as the section's last child. Call each owner hook once into one binding and
  use it only through property access: do not destructure or flatten it, put it in
  a dependency array, or pass it whole, and the same applies to its `view`. The
  hook result and its `view` are new objects on every render. Call positions are effect order, which is behaviour. The table-list hook
  comes first, the row-code hook sits between the detail-load and notebook-reset
  effects, and the concept-matrix hook is the last hook call. The notebook reset
  must run before the citation jump. Moving code must not change any callback's
  `useCallback` status or dependency array; for example, `handleCodeSaved` stays a
  plain per-render function. `owner-view-no-reflatten-guard` and
  `knowhow-panel-boundary-guard` enforce these rules. Request-sequence goldens in
  `knowhow-panel-golden` are permanent; commit counts and stylesheet hashes were
  split-period only.
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

### Binding id lists in SQL

A statement that binds an id list whose size is set by the data — above all a
run's frozen source ceiling, which can hold every source of a notebook — binds it
through its backend's `id_binding` module:
[PostgreSQL](../backend/app/repositories/postgres/id_binding.py) `bind_ids` +
`member_of` / `not_member_of`, executed by `execute_ids` (or `execute_bound`, which
keeps the plain execute when no list is bound);
[SQLite](../backend/app/repositories/sqlite/id_binding.py) `bind_ids` +
`member_of` / `not_member_of` / `drive_by`. Both return `BoundIds(sql, param)`: one
parameter per list, whatever its length. The modules' docstrings hold the full
measurements; the facts that shape the rule, measured on a 49,000-source notebook:

- PostgreSQL: psycopg prepares a statement on its 5th execution on a connection and
  PostgreSQL may switch it to a generic plan from about the 11th, where a bound
  array is an opaque parameter estimated at 10 elements. Chunk FTS went from 1.1 s
  to 3.1 s at 49k ids — past its 3 s timeout, whose rollback clears the prepared
  cache so the cycle repeats every ~11 executions and opens the lexical circuit
  breaker; a rare term 7 ms vs 7.7 s; the KG evidence gate 169 ms vs 6.7 s.
  `execute_ids` sends `prepare=False`, so every execution is planned with the real
  list. Planning is then paid every time: 5–11 ms for 49k ids on a column without
  most-common values, 62–81 ms on one with them (skewed chunks per source; about
  0.2 s per question on rare terms) — a bounded cost instead of a cliff. The
  semi-join form `col IN (SELECT unnest(…))` plans in milliseconds but lets the list
  drive and lost somewhere on every statement (common-term chunk FTS 1.8 s → 5–7 s),
  so it is not used.
- SQLite: one `?` per id stops at the deployment build's 32,766 variables
  (`SQLITE_LIMIT_VARIABLE_NUMBER`; the local build allows 250,000, so the failure
  only shows in production), and a fail-open caller turns the error into a silently
  empty channel. `col IN (list)` on an indexed column also lets the list drive:
  inside a correlated probe of the composite index `(object_id, source_id)` it seeks
  once per id per candidate row (6.6–8.7 s at 49k ids). `member_of` writes
  `+col IN (…)` so the list only filters (14–20 ms); `drive_by` is the explicit form
  for "seek every listed id". Production databases carry no planner statistics, so a
  plan must hold with and without `sqlite_stat1`, and the plan pins run in both.
- Where a bounded primary-key window drives a statement and a large ceiling only
  filters it, the PostgreSQL predicate may test a statistics-free expression
  (`(c.source_id||'')`, as `graph_hydrate_rows` does) so the custom plan skips the
  per-element most-common-value estimate (49k ids: 146 ms → 5 ms planning) — the
  counterpart of SQLite's unary `+`.

The binding layer never changes membership: ids are bound exactly as given (empty
and blank ids included) and a non-string id raises `TypeError`; de-duplication is the
store's decision, identical on both backends. A list may be bound directly only in
one of three classes: **bounded by construction** (a page or `LIMIT` window, a
ranked primary-key window, one run's notebooks, a fixed status set, one source's
element ids); **batched key probe** (the ids are the keys the statement reads or
writes, its only selective predicate, at most 1024 per statement on PostgreSQL and
900 placeholders on SQLite; e.g. `relation_delta_rows`, whose callers pass delta or
Memory source ids in batches of at most 900); **driven by the list, one parameter** (the whole list is
the set of keys to read, e.g. `visible_source_scope_snapshot`). A list that filters a
statement driven by something else is never exempt. `backend/tests/test_id_list_binding_guard.py`
scans both repository directories and fails on any id-list binding that is neither
converted nor listed there with its class and reason. Deliberately not adopted: a
complement list (a frozen ceiling must not admit sources added after the freeze), an
identity cache in the binder (it belongs to whoever owns a long-lived list), and
statement-local planner settings (measured in both directions).

### Schema and migration authoring

- SQLite and PostgreSQL are both kept long-term (product decision of 2026-10-19).
  PostgreSQL is the canonical definition; SQLite is the mirror supported for offline,
  single-machine and test use. The support scope and the capability parity matrix are owned
  by [the architecture document](../architecture.md#222-双后端长期并存与能力对等矩阵). A storage
  change (schema, store method, SQL predicate or lock order) lands on both adapters in the
  same PR: settle the PostgreSQL form first, mirror the same semantics on SQLite, and register
  the paired migrations in `POSTGRES_SCHEMA_MANIFEST`. A structure that exists only for
  performance and whose absence leaves SQLite's results unchanged may be PostgreSQL-only, but
  it is registered in the matrix. Behavior tests come in pairs (the SQLite one in G1, its
  PostgreSQL twin in the PG lane); `backend/tests/test_dual_backend_parity.py` statically checks
  the version pairing, port implementation, class-level public member sets and that every
  matrix citation still resolves (existence only: a PR that changes a cited symbol's behavior
  re-checks its matrix row by hand).
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
| G1 standard | `scripts/check.sh`: stable backend, contracts/harness, backend ruff against its baseline, frontend tests, frontend ESLint against its baseline, typechecking production build, and the package typecheck for test files | Local handoff and every PR/push/manual CI run |
| G2 extended | `scripts/check_extended.sh`: G1 plus real-index/performance, cold graph/index contracts, repository-wide semantic scans (heavy subset), real child-process contracts, and the old-version upgrade matrix | Once daily at `17 18 * * *` UTC (02:17 Asia/Shanghai), plus manual dispatch |
| G3 PostgreSQL | `scripts/check_postgres.sh`: direct PostgreSQL adapter integration | Independent PR/push/manual CI job |

G1 runs three bounded lanes concurrently: `check_backend.sh` executes the stable backend pytest suite with default 16 backend pytest workers (override with `BACKEND_PYTEST_WORKERS`; CI pins 4); `check_contracts.sh` executes syntax/dependency preflight, hermetic smoke paths, contract checks, the backend lint gate (see [Backend lint gate](#backend-lint-gate)), and the deterministic extraction-scoring harness; `check_frontend.sh` executes every recursively discovered `*.test.mjs`, every `*.component.test.tsx`, ESLint (`npm run lint`, see [Frontend ESLint gate](#frontend-eslint-gate)), the production frontend build, and the package typecheck. On a host with at least eight CPUs, after the one plugin sync, four groups run concurrently — node tests, component tests, ESLint, and the build followed by the typecheck — each into its own log, printed in a fixed order with every step's exit status once all finish; smaller hosts (CI runners) keep the sequential order and stop at the first failure, and `FRONTEND_LANE_PARALLEL=1`/`0` overrides the CPU check. Node's test runner and Vitest are each capped at four workers, leaving CPU headroom for the backend critical path. The Next build must keep `ignoreBuildErrors` unset and stays the fail-closed typecheck for production code, but Next's build-time type checker silently filters out every diagnostic reported in `*.test.*`/`*.spec.*` files and `__tests__`/`__mocks__` directories (the `ignoreRegex` in `next/dist/lib/typescript/runTypeCheck.js`, verified on Next 15.5), so a type error that only exists under `frontend/tests/**` never fails the build. The frontend lane therefore runs `npm run typecheck` (`tsc --noEmit`) after the build — after, so it type-checks the freshly regenerated `.next/types` rather than a stale tree — as the one pass that sees those files; with `incremental` the warm re-check costs under a second (~5s cold), so the duplicate parse the lane once avoided is no longer a budget concern. Its backend lane excludes `slow` real-index/performance tests, `graph_index_contract` cold graph/index contracts, `architecture_contract_heavy` (the eight repository-wide semantic scans in `_ARCHITECTURE_CONTRACT_HEAVY_TESTS`; the remaining lightweight `architecture_contract` tests run in G1), `process_contract` (tests that start a real child process — subprocess, multiprocessing spawn or fork — to check a process-level contract: service start/stop and launcher signal handoff, release-manifest git history and packaging, the batch-ingest process pool, real-process startup refusal, script and CLI entry points), `legacy_upgrade_contract` (tests that upgrade a deployed old-version database: the snapshot verifier's deployed-vNN cases, the old-version migration matrix, and the memory-isolation rebuild that such an upgrade schedules; current-schema, fresh-database and source-consistency checks in the same modules stay in G1), and the PostgreSQL tree. By the 2026-10-14 decision that set the local target to 80 seconds, these process-level and old-version contracts are reported by the daily G2 gate rather than on every pull request. `process_contract` and `legacy_upgrade_contract` are applied per test, so one file can mix them with G1 tests. The lane-partition guard below starts a child process but stays unmarked in G1, because a G2-only copy would stop running the moment a broken expression deselected it. G2 first runs G1 and then the exact complementary backend marker set — `backend/tests/test_test_architecture_policy.py::test_verification_lane_markers_partition_every_architecture_contract_test` proves that split empirically via `--collect-only`, not just by pinning the two `-m` strings. Each lane has its own process group, so interrupting or terminating the controller also terminates and reaps pytest, npm, and Next.js descendants. The official-client MCP smoke pins exactly the 21 published tools: two discovery/overview, two search/point-read, two ask, four Q&A-history and report, two contribute, four source, one build, and four knowhow tools. Missing `frontend/node_modules` is a hard failure rather than a silent skip.

Use the project’s Homebrew/Miniconda interpreter for acceptance:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

The Apple Silicon warm gate hard target is at most 100 seconds (relaxed from 80 seconds by the 2026-10-19 decision, which also keeps the contract tests moved to G2 on 2026-10-14 where they are); 60 seconds remains the long-term aim. CI lane timings are observational only, so this measured local target is not a portable timeout assertion for every CI host. When it finishes, `check.sh` prints the gate's wall time, each lane's seconds and the ten slowest backend test files and tests from that run's JUnit report (`scripts/check_timing_summary.py`), and a prominent warning when the wall time is over 100 seconds; the summary is a local warning only and never changes the gate's exit status. The gate scripts write Python bytecode, including pytest's rewritten test modules, to the gitignored `.local/pycache` (`PYTHONPYCACHEPREFIX`) and unset any `PYTHONDONTWRITEBYTECODE` the calling shell exports, so a fresh worktree or an agent session reuses the bytecode cache instead of recompiling every module in every worker. Backend tests that start a child process with a hand-built environment spread `BYTECODE_ENV` (`backend/tests/child_env_testkit.py`, required by a policy test), so a G1 run writes no bytecode into the source tree. Because the prefix mirrors absolute paths, it would also keep the bytecode of every module tests write under temporary directories; the backend conftest removes that one subtree when a pytest session starts, which keeps the cache bounded while the repository's and the interpreter's own modules stay warm.

Keep test-speed changes result-preserving. The G1 standard and G2 extended marker expressions are exact complements, while PostgreSQL stays independently authoritative; never make a committed test unreachable. Ordinary unit and standard-gate tests are hermetic: they do not bind host ports or depend on ambient services; self-contained subprocess/signal coverage is reserved for contracts that are intrinsically process-level, and a test that starts a real child process carries `process_contract` so it runs in G2. `test_test_architecture_policy.py` fails on a G1 test whose own code (or a module helper or fixture it uses) starts a process through `subprocess`, `multiprocessing`, a `ProcessPoolExecutor`, `os.fork`/`os.spawn*` or `asyncio.create_subprocess_*`; the lane-partition guard is the one reviewed exception, and a process started inside application code is not seen by that scan. Cache repository-wide AST/protocol parsing once per test process (pytest worker or isolated Node guard process), and expose a membership-only projection when a frozen-fixture test does not need detailed sites, signatures, or ownership. An executable repository guard gets one real-tree invocation in its owning contracts lane; unit tests for argument parsing, failure modes, and extra roots redirect its default roots to minimal fixtures rather than rescanning the repository. Assertions over the same immutable behavior matrix belong in one labelled traversal so they do not rebuild an identical database world per row or assertion family. The frontend lane syncs its immutable local-plugin projection once, then suppresses only npm's redundant `pretest`/`prebuild`/`pretypecheck` hooks; those hooks remain mandatory for each standalone developer command. Test cache/container policy through the policy object instead of constructing unrelated database and ANN artifacts; and derive autouse isolation paths from the worker's existing pytest base temp rather than allocating a new `tmp_path` directory for every pure test. Ordinary SQLite repository tests copy a current empty schema built once per pytest worker (that build is itself a real from-scratch run of the whole ladder in every worker), but every test keeps an independent mutable database file. The real from-scratch ladder is chosen per test, not per module: a test carries `@pytest.mark.real_sqlite_migration` (or its module's `pytestmark` does) when the ladder is what it checks. `test_test_architecture_policy.py` requires the marker on every test whose own body drives `SqliteMigrator` (directly or through a module helper) in a module that owns a migrator, and on the startup-migration wiring test; the upgrade cases of the migration and repository-snapshot modules carry it per test, so a test added to those modules starts on the template and in G1. Fixtures that only create an empty database at the current schema use the copy, and the autouse fixture fails a copy for a test that changed the ladder (`SCHEMA_VERSION` or a `_migration_N` step, looked up through the migrator's class hierarchy) without the marker; a patched helper that the steps call is not detected, so such a test carries the marker itself. A database file that already exists, such as an old-version fixture or a rolled-back copy, is always migrated for real. Repository-heavy tests may reduce only the default password-hash cost in the pytest autouse fixture: authentication helpers retain the production default, and credential-field snapshot modules remain in `_REAL_PASSWORD_HASH_MODULES`. Concurrency tests use events/barriers for ordering and fairness assertions rather than fixed sleeps or assumed thread wake-up order. When queued work runs in waves, a controller thread must release observed capacity with events instead of leaving a later wave alone in a cyclic barrier. Delayed process-global jobs must be cancelled and reaped in shared teardown before per-test repositories close; cleanup scoped to one repository object cannot contain route-owned work. A test that removes an `app.*` module from `sys.modules` must put it back through `monkeypatch` (`delitem`, with the parent package attribute pinned) rather than a bare `pop`: the parent package keeps binding the old module object, so the next import splits attribute lookups from `sys.modules` lookups and a patch applied through one path is invisible to the other; the backend conftest checks that invariant after every test and errors the polluting test, not the later victim. Once collection finishes, the backend conftest runs one full collection and `gc.freeze()`s the survivors (the imported app, test modules and collected items), so the cyclic collector stops re-walking that resident heap on every full collection; everything a test allocates afterwards is collected as before.

Test processes reuse the fixed core routers' routes across `create_app()` calls. `backend/tests/conftest.py` installs the test-only speedups in `tests/speedups/` from `pytest_configure`, before any test module imports `app.main`. `tests/speedups/app_routes.py` wraps `APIRouter.include_router`: when a FastAPI app's own router includes one of the `APIRouter` objects bound as globals of `app.main`, with only `prefix`/`dependencies` and no startup/shutdown handlers, every app after the first receives copies of the routes built the first time. Each copy has its own containers and dependant tree, `dependency_overrides_provider` bound to that app, and a freshly built handler; the real call's lifespan merge runs through the real implementation with the source router's own lifespan object. The onboarding router and deployment-plugin routers are not `app.main` globals, so they always take the real path (even when a plugin factory returns the same router object every time), and a source router whose routes are added, removed or edited in place is rebuilt. Any other call (another prefix or include-level dependencies, other `include_router` arguments, a target app or router with its own dependencies or configuration, a source router with startup/shutdown handlers) also takes the real path. A template cannot see one input: a real build re-resolves string annotations against the endpoint module's globals, so tests must not monkeypatch type-like names on the `app.api` modules that define endpoints; a source guard in the pin file rejects such patches. Product `create_app()` is unchanged, and every app still gets its own FastAPI object, MCP server, middleware stack and state. `backend/tests/test_app_route_templates.py` pins that a templated app equals a real build (OpenAPI, ordered route attributes, lifespan chain), that overrides on one app never reach another and no route container or dependant is shared between apps, the invalidation and whitelist rules, and that every boundary of the hit conditions builds what the real call builds. The shim installs only on the FastAPI version it was verified against (the `requirements.txt` pin), so an upgrade turns that pin red until the internals are re-checked. Set `SILICON_NOTEBOOK_TEST_ROUTE_TEMPLATES=0` to run a whole suite on the real path; `app_routes.disabled()` does the same inside one test.

The UI-vocabulary guard's `guard.main()` self-tests (`backend/tests/test_ui_vocabulary_guard.py`) run on a minimal fixture tree rather than the real repository: `_minimal_guard_tree` redirects every scan root and registered module of every channel to a small, clean tree in which each channel is non-empty, and lowers the `MIN_*` non-vacuity floors to that tree's site counts. `test_最小树上守卫全绿` pins the baseline (`main() == 0`), and each mutation test asserts both its own channel's message and that no other channel reports a failure, so a collapsed neighbour cannot make a test pass by accident. The whole-repository scan of the real sources stays in the contracts lane (`scripts/check_ui_vocabulary.py`, invoked from `check_contracts.sh`), and the real-tree tests that remain (registered display-field modules exist, the shipped service labels, the contracts-lane wiring) read the repository directly.

Python source guards that scan `backend/app` read it through `app_source_trees()` (or `read_source_tree()` for a single file) in `backend/tests/architecture/source_trees.py`, and new guards of that kind should do the same instead of parsing the tree themselves. The parse cache is keyed by file content alone (an AST carries no file name), so every pytest worker parses each distinct source text once however many guards run there, and an edited file is always seen as a new tree; callers must not mutate the returned trees. Guards that have not been moved onto it still parse privately: `PythonSourceIndex` in `backend/tests/architecture/semantic_source.py` (used by the default-ceiling and participant-override guards) and `scripts/check_architecture_boundaries.py` (used by the phase-0 guard). The guards that scan the whole tree and parse through the shared entry are co-located in an `xdist_group` (`source_scans_citation` for the citation guards, `source_scans_core` for the others) so they land on one worker and reuse that cache instead of re-parsing in several workers. The groups only schedule tests: they change no marker and no G1/G2 selection, a module that already has a group keeps it instead of gaining a second one, and a module that gains nothing from sharing stays ungrouped. Keep each group's in-gate busy time at roughly 20 seconds or less.

Seeded world templates (`backend/tests/world_templates.py`) let a module build an expensive seeded world once per pytest process and copy it per test, while every test still mutates its own copy. The builder runs inside `MonkeyPatch.context()` from the process baseline environment rather than the requesting test's, sets every setting the world depends on through `env=` (which is part of the template identity; reusing a key with different settings fails), and returns pure JSON data, never live objects. The build must close everything it opened: each SQLite file is checkpointed with `PRAGMA wal_checkpoint(TRUNCATE)`, its WAL must be empty, and no descriptor may still point into the world. A world must be reopen-stable (only the seeded admin's per-open credential re-derivation is masked), so open-time normalisation such as interrupted-job recovery or seed inserts fails at build time. `relocatable` worlds may not contain their build root in any row, storage file, or payload; worlds that store absolute paths (uploaded sources) use `pinned`, which rebuilds a fixed `live` directory from an untouched `pristine` copy for every test. The pristine copy's files are re-verified on every materialize, and identity-keyed process caches are reset first because the tests of one worker now share notebook and user ids, then given back exactly the entries the build left for the world's own ids (count memos from the world's version history, the asset-sweep throttle at the same age), so every copy starts with the warm state a self-built world had; `test_world_templates.py` fails when a new module-level cache in `backend/app` is not classified in the helper's inventory.

The sync tests (`test_sync_incremental_import.py`, `test_sync_import.py`, `test_sync_export.py`, `test_sync_incremental.py`) take their seeded libraries, full export and baseline import from `pinned` world templates: uploaded sources store an absolute `file_path`, and the import report is written beside the package. Export and import reports travel in the payload as pure data (`backend/tests/sync_world_cases.py`); the build first proves that the rebuilt report equals the original field for field with every type kept, and each test then rebuilds its own. `test_sync_import.py` also has a second-level world taken after the default import: only tests whose body is "import once with the default arguments, then read-only assertions" read that outcome, produced by the real code at build time, through `first_import`. Tests that patch, observe the import's progress or events, pass other arguments, or act again after the import still run the import in their own body; collecting the module rejects a `first_import` test that also uses `monkeypatch`, `MonkeyPatch`, `setattr`, or calls the import again. Before a test is switched to that world, a deliberate bug injected into the import code must be shown to still turn it red.

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

Performance pins count work instead of timing it: a wall-clock bound fails on a
loaded gate (a 35 ms share-copy snapshot measured 3.19 s on CI) and misses a real
regression alike. The share-copy pins compare the statements, the Memory-member
key-set reads and SQLite VM instructions at n and 2n (`_snapshot_work`); a
quadratic walk shows ~3.9x. PostgreSQL plan pins whose choice moves with the
visibility map (an Index Only Scan is costed as heap visits while a same-database
snapshot older than the seed blocks VACUUM) run in both `bystander_snapshot`
states: the exact plan only after `vacuum_until_all_visible` has confirmed a full
map, the invariants that hold for any access method (no sequential scan, every scan
keyed on the probed row) in both. The helper only weighs sessions whose snapshot
predates the table's newest row: an ordinary one still blocking after 10 s fails the
call and is named, while maintenance (an autovacuum worker, any ANALYZE or VACUUM
in progress -- an autoanalyze keeps its snapshot for its whole sample) is waited out
for up to 180 s. It never returns on an incomplete map.

Extension-service lifecycle tests share one `xdist_group` to bound competing
supervisor startups; each test retains its own real process/thread concurrency.
State-file regression tests force replacement after open: JSON readers may consume
the already-open, owner-owned regular-file snapshot after its final link disappears,
while lock/writable handles still require one link and unsafe files remain rejected.
Launcher signal tests start every launcher with default signal dispositions
(`preexec_fn`): a non-interactive shell cannot trap a signal that was ignored when
it started, and a gate run under `nohup` or as a background lane hands its children
SIGHUP or SIGINT ignored. Their waits end on handshakes (a pid file published by
rename, or the launcher's exit) under a 20 s backstop.
The launcher wind-down cases (`dev.sh` and `prod.sh` share `scripts/launcher_cleanup.sh`)
run fake uvicorn / `next` / `node` children in `backend/tests/launcher_checkout_testkit.py`
that record the SIGTERM they receive (`TERM_EXITS`) or ignore it (`TERM_IGNORED`). They pin:
TERM, then grace, then KILL, with the exit codes 130 / 143 / 129 for both launchers; a
second Ctrl-C, TERM or HUP during the grace wait ending it at once while the companion
services are still stopped (prod.sh is held before its hand-off by a fake `rm`, `RM_BLOCK`); the same
signals sent to the launcher's whole process group, as a terminal's Ctrl-C does (the launcher runs
as a session leader, `new_session`, and the test uses `os.killpg`), which also interrupt its own
`sleep` and must not abort the wind-down under `set -e`;
that the backend's parent is the launcher itself (the `exec` guard; for `( one simple
command ) &` bash forks no subshell, so only a second command in that subshell, not a
dropped `exec`, shows); a child whose pid a pending trap left unassigned still being
stopped (reproduced deterministically with `kill` between the fork and `PID=$!`); and that
both launchers source the shared script and bracket every fork. Because `dev.sh` starts
`next dev` directly, it also runs the `predev` UI plugin sync by hand: the cases pin that
the sync finishes before next starts, that a failed sync starts no frontend and stops the
backend, that a cancellation or a backend exit during the sync leaves no process behind,
and that `frontend/package.json` still has exactly the hooks `dev.sh` reproduces.
Operations behaviour: [operations.md](./operations.md#plugin-companion-services).

### Stress scripts

Some regressions live in a window no deterministic test can aim at. They are
guarded by stress scripts under `scripts/`, not by pytest cases: a committed
test is always collected, and no environment switch hides one from a gate.
Neither G1 nor G2 runs these scripts. Run one by hand, on a loaded machine,
after touching the code it guards.

- `scripts/stress_launcher_signals.py` guards three macOS `/bin/bash` 3.2 paths
  next to the companion-controller fork in `scripts/extension_services.sh`:
  - `extension_services_start` must poll the controller instead of blocking in
    `wait`;
  - `extension_services_cleanup` must repeat its TERM;
  - cleanup must find, through `$!`, a controller forked just before a pending
    trap ran.

  Each attempt cancels a real launcher in a fake checkout
  (`backend/tests/launcher_checkout_testkit.py`, shared with the launcher tests)
  0–4 ms after the launcher creates its receipt. A launcher still alive 20 s
  later counts as held. A launcher that exited but left its controller running
  counts as leaked; that controller is killed by exact pid.

  ```bash
  python scripts/stress_launcher_signals.py --load auto
  ```

  `--load auto` runs one busy loop per core alongside. By default the script
  runs 2 launchers (`--launchers dev.sh,backend.sh`) × 4 parallel shards
  (`--shards`) × 1500 attempts (`--runs`). `--seed` fixes the offsets. Exit 0
  means nothing was held or leaked. Exit 1 means a cancellation was held or a
  controller leaked. Exit 2 means bad usage or a broken shard.

  Measured with `--load auto`:
  - The fixed launchers: 0 held and 0 leaked in 12000 attempts.
  - Launchers without the `$!` fallback: 92 of 400 attempts leaked (unloaded).
  - Launchers reverted to a blocking `wait`: 2 and then 4 held in two runs of
    12000. That race hits about once in 3000–6000 attempts, so a default run
    catches it with roughly 85–98% probability. Raise `--runs` or repeat the
    run when you need more confidence. The `$!` path leaks so often that a
    default run always catches it.

### Python dependencies

Python dependencies are hash-locked, and installing never resolves anything.
`backend/requirements.in` declares the runtime ranges and
`backend/requirements-dev.in` the test and gate tools (pytest, pytest-xdist, ruff
and matplotlib, which the pytest controller prewarms); its `-c requirements.txt` line
keeps every package both sides need at the runtime lock's version.
`scripts/lock_python_deps.sh` compiles them with pip-tools `--generate-hashes`
into `backend/requirements.txt` (runtime lock) and
`backend/requirements-dev.txt` (test lock). It resolves with CPython 3.13, the
version CI and production run (`LOCK_PYTHON`, default `python3.13`); every locked
version also publishes wheels for 3.14, so a local 3.14 environment installs the
same locks (hnswlib builds from source on either). It runs from a
private venv under `.local/` with pinned pip and pip-tools versions. The hashes
cover every file PyPI publishes for each pinned version, so one lock installs on
every supported platform — Apple Silicon macOS 14+ and Linux x86_64/aarch64 with
glibc 2.28+, the floors onnxruntime, igraph, PyMuPDF and pymupdf-layout set (see
`docs/deployment-and-configuration.md`); Intel Macs cannot install it. Resolving
on macOS or Linux gives the same files while no
dependency is platform-conditional between them, and a lock that missed such a
dependency fails `pip install --require-hashes` loudly rather than installing
something else.

| Install | Command |
| --- | --- |
| CI, and a development environment that runs the gates | `python -m pip install --require-hashes -r backend/requirements.txt -r backend/requirements-dev.txt` |
| Deployment (`scripts/prod.sh`, README/deployment instructions) | `python -m pip install --require-hashes -r backend/requirements.txt` |
| Offline bundle (`scripts/pack.sh` → `packaging/install.sh`) | `pip wheel --require-hashes` from the runtime lock, then `pip install --no-index --require-hashes -r wheelhouse/locked-wheels.txt` |

Changing dependencies is an explicit, reviewed edit:

- Add, remove or re-range a dependency: edit the `.in` file, run
  `bash scripts/lock_python_deps.sh`, and commit the `.in` file together with
  both lock files. Every existing pin that still satisfies the ranges is kept,
  so the diff shows only what the edit requires.
- Upgrade: `bash scripts/lock_python_deps.sh --upgrade-package NAME` (repeatable,
  `NAME==VERSION` allowed) upgrades just those packages; `--upgrade` upgrades
  everything the ranges allow. The FastAPI/Pydantic exact pins below still
  require an OpenAPI regeneration.
- `bash scripts/lock_python_deps.sh --check` relocks into a temporary copy and
  fails if a committed lock would change (needs the package index).

`backend/tests/test_python_lockfiles.py` is the offline gate in the backend lane.
Each lock carries the fingerprint of the `.in` file it was compiled from
(`scripts/python_lockfiles.py`; comments, order and spacing do not count), so an
edited `.in` file that was never relocked fails it. It also fails when a direct
requirement is missing from its lock or pinned outside its range, a pin is
unhashed, no longer required or required by something unlocked, a lock is not
the lock script's Python 3.13 output, the test lock disagrees with the runtime
lock on a shared package, or a test tool reaches the runtime lock. It also walks
each locked package's own `Requires-Dist` (markers and requested extras
evaluated) and fails when a requirement is missing from the lock; that check
reads installed metadata, so it covers every package where the environment was
installed from the locks, and under `CI=true` it also asserts that it read every
locked package. `backend/tests/test_third_party_imports_resolve.py`
imports every third-party module `backend/app` and `scripts/` import, including
the lazy, fail-open ones inside functions and the ones under `TYPE_CHECKING` or
`try`/`except ImportError` (no allowlist; sibling `scripts/` modules count as
first-party), checks every imported name and every
attribute read on a lazily imported module, and requires the providing
distribution to be locked; `test_parsers_pdf_python.py` converts a real PDF with
the installed pymupdf4llm. Together they keep an upgrade from locking a broken
release that a fail-open fallback would hide.

The first lock (2026-10-21) kept the versions the `master` CI run of `3132d4667`
installed, with one exception: that run, like every fresh install since PyPI
published it on 2026-10-19, got `pymupdf4llm` 2.0, an empty placeholder with no module, so
the PDF fallback had silently dropped to pypdf plain text. `requirements.in`
caps it below 2 and the lock pins 1.28.2 with its dependencies (PyMuPDF,
pymupdf-layout, onnxruntime and four more), which adds about 200 MB to a
backend environment.

### Frontend ESLint gate

`npm run lint` (in `frontend/`) runs ESLint 9 with `frontend/eslint.config.mjs`:
Next's own `next/core-web-vitals` and `next/typescript` sets, loaded through
`FlatCompat` because eslint-config-next 15 still ships eslintrc configs, with
`react-hooks/rules-of-hooks` and `react-hooks/exhaustive-deps` as errors. Every rule
is an error: ESLint's bulk suppressions record only errors, so a warning could be
neither baselined nor gated, and `--max-warnings 0` plus
`reportUnusedDisableDirectives: "error"` leave no warning tier at all. A leading
underscore marks a deliberately unused binding. Under `tests/**` the two Next rules
that protect what Next bundles and serves (`@next/next/no-img-element`,
`@next/next/no-assign-module-variable`) are off. ESLint does not read `.gitignore`, so
the config's one global `ignores` list names what is not linted: build and tool output
(`.next`, `node_modules`, `.local`, `coverage`, `dist`, `build`, `next-env.d.ts`) and
the plugin projections that `sync-ui-plugins.mjs` generates (`features/ext-*`,
`registry.local.ts`). The lint step is separate from the type check: `npm run typecheck` is
`tsc --noEmit` (before ESLint it was named `npm run lint`), and `next build` does not
lint (`eslint.ignoreDuringBuilds`), because Next's build-time pass would lint without
the baseline.

Violations that predate the gate are recorded in `frontend/eslint-suppressions.json`,
counted per file and rule. For every file it lints, ESLint fails when a rule's
violations exceed that file's entry, and when an entry is larger than what still occurs
(exit 2, "suppressions left that do not occur anymore"). It checks only the files it
lints. `frontend/tests/guards/eslint-baseline-guard.test.mjs` covers what that leaves
open: each rule's total count and number of files are pinned in `BASELINE_CEILING`
with zero slack; every listed file must exist and must not be ignored (ESLint never
checks entries for deleted, renamed or newly ignored files); the global `ignores` list
and `reportUnusedDisableDirectives` are pinned; every source file under `app`,
`features`, `scripts`, `test-support` and `tests` must be linted, with both hooks
rules resolving to errors and no rule left at warn; and `react-hooks/rules-of-hooks`
is never exempted, neither in the baseline nor by an `eslint-disable` comment (a
violation is a real bug or a helper misnamed `use*`, both fixed on the spot). The pins
are per-rule totals, so debt moved between files under one rule (one violation fixed
in file A, one added in file B) keeps every number and is caught only by reviewing the
`eslint-suppressions.json` diff. To lower the baseline, fix violations, run
`npm run lint:prune` (`eslint --prune-suppressions .`), and lower `BASELINE_CEILING`
in the same change. Do not absorb a new violation with `eslint --suppress-all` or
`--suppress-rule`: fix it, or, when the code is deliberately written that way, put an
`// eslint-disable-next-line <rule> -- <reason>` comment directly above the reported
line. ESLint is its own group on the parallel path (about 5 s cold on the reference
machine, under the build), so it does not lengthen the frontend lane.

### Backend lint gate

`scripts/check_backend_lint.py` runs ruff, at the exact version pinned in
`backend/requirements-dev.in`, with the repository's `ruff.toml` over
`backend/app`, `backend/tests` and `scripts`: every Python file git sees there,
tracked or untracked but not ignored. Nothing is excluded, because no generated
Python lives under those roots. It runs in the contracts lane and takes under a
second, so it is off the backend critical path. Run it locally from the
development environment (the dev lock installs ruff) with
`python scripts/check_backend_lint.py`.

The rules are lint rules plus import order; there is no formatter and no other
style rule:

- pyflakes (`F`), `E9` (files the parser cannot read) and `E722` (bare
  `except`);
- pylint errors (`PLE`);
- the flake8-bugbear rules that flag likely bugs: mutable argument and
  `ContextVar` defaults, `return`/`break`/`continue` in `finally`, duplicate or
  empty exception tuples, `pytest.raises(Exception)`, pointless comparisons and
  expressions, `lru_cache` on methods, duplicate set items and the like. The
  full list is in `ruff.toml`;
- import order (`I`, isort's default profile). The sorter's settings follow how
  the repository already writes imports: `src = ["backend", "scripts"]` makes
  `app`, `tests` and the sibling script modules (`import diag_common`) first
  party, which cuts the existing findings from 1836 to 1450; leaving out the
  repository root keeps the third-party `packaging` library from being taken for
  the `packaging/` directory. `line-length = 120` is read only by the sorter (no
  line-length rule is selected): 99% of single-line imports already fit in 88
  columns, but the 133 that do not would each be reported for wrapping, and 120
  leaves 4 (1395 findings in 1009 files). `force-sort-within-sections` and
  `order-by-type = false` both match the existing code worse (1715 and 1623).
  The existing findings were recorded, not fixed: turning the rule on reordered
  no import. The sorter only orders contiguous import blocks, so it never moves
  an import across a statement such as `sys.path.insert`, and the gate never
  runs `--fix`. A new finding means that new or edited imports are out of order
  within their block, which is fixed by placing them correctly. Imports already
  out of order in a recorded block do not change the count, so a new finding
  never forces existing imports to move.

Left out on purpose:

- `B008`: FastAPI's `Depends()` defaults.
- `B904`, `B905`, `B007`, `B009`/`B010`: exception chaining, `zip(strict=)`,
  unused loop variables, constant `getattr`/`setattr`. These are style or a
  behavior change.
- `B023`: loop variables in closures; most hits are closures called inside the
  loop.
- `UP` (decided 2026-10-22): 6324 findings in 537 files, almost all
  annotation rewrites (`List` → `list`, `Optional[X]` → `X | None`, unquoted
  annotations, `datetime.UTC`). These are style churn rather than bugs, and
  `UP042` (`(str, Enum)` → `StrEnum`) changes what `str()` returns.

Violations that predate the gate are recorded in
`scripts/backend_lint_baseline.json`, as file → rule → count. The gate fails when:

- a (file, rule) count is higher than its entry (a new violation);
- an entry is higher than what still occurs (stale: a fixed violation, an
  import block that is now sorted, a deleted or renamed file);
- the baseline names a rule that is never baselined (`F821`, `F822`, `F823`, `E902` and syntax errors (`invalid-syntax`) are always fixed);
- ruff lints a different set of files from the one git sees under the roots
  (`git ls-files -z`, so any file name works), a Python file there is a
  symbolic link, or a ruff configuration file (`ruff.toml`, `.ruff.toml`,
  `pyproject.toml`) sits under the roots;
- line suppressions that name a gated rule differ from `SUPPRESSIONS` in the
  script (per rule, count and files; the existing ones predate the gate:
  pytest fixtures imported for injection, re-exports and side-effect imports),
  or a forbidden directive appears: a blanket `# noqa`, the file-level
  `# ruff: noqa` and `# flake8: noqa`, or any `# isort:` action comment
  (`skip`, `skip_file`, `off`, `split`);
- the installed ruff is not the pinned version.

Environment problems (ruff not installed, git missing or not a checkout) exit
with status 2 and say what to install or where to run it.

`backend/tests/test_backend_lint_gate.py` (backend lane, no subprocess) covers
what that leaves open:

- Each rule's total count and number of files are pinned in `BASELINE_CEILING`
  with zero slack, and `SUPPRESSION_CEILING` must equal the script's
  `SUPPRESSIONS`.
- `ruff.toml` must equal the expected configuration: no `ignore`,
  `per-file-ignores`, `exclude`, preview or `extend-*` keys. The roots and the
  empty exclusion list are pinned.
- `check_contracts.sh` runs the gate as exactly one effective command line,
  `"$PYTHON_BIN" "$ROOT_DIR/scripts/check_backend_lint.py"`, under
  `set -euo pipefail` and before any `exit`: commenting it out, appending
  `|| true` or `; true`, or moving it after an `exit` is red.

The pins are per-rule totals. Debt moved between files under one rule (one
violation fixed in file A, one added in file B) keeps every number, and only
review of the baseline diff catches it.

To lower the baseline:

1. Fix violations.
2. Run `python scripts/check_backend_lint.py --prune`. It only lowers entries
   and drops the ones that reach zero, including those of deleted files, and it
   writes nothing while any new violation exists. It prints how far each rule's
   pinned totals fall.
3. Lower `BASELINE_CEILING` by those amounts, and `SUPPRESSION_CEILING` together
   with `SUPPRESSIONS` if a suppression went, in the same change.

Renaming or moving a file that has entries: the gate reports the old path as
no longer linted and the new path's findings as new. Run
`python scripts/check_backend_lint.py --rename OLD NEW`. It moves the old
path's entries to the new one, each capped at what the new path still has,
only when the old path is gone and the new one has no entries yet, and writes
nothing while any other new violation exists. It prints the ceiling change
(none for a plain rename).

The stale error says the same. Import order makes it more likely than the
other rules: an edit that leaves a recorded import block sorted, or deletes a
file with entries, needs a prune. Replaying the last 300 commits on `master`
(220 touched gated Python files), 4 would have needed a prune (9 files, mostly
deleted or rewritten test modules) and 37 would have written new imports out of
order.

No command adds an entry. Do not absorb a new violation by editing the baseline
or adding a `# noqa`: fix it. A name kept only as a re-export belongs in the
module's `__all__`. Upgrading ruff is a reviewed change of its own: bump the
pin, relock, and recompute the baseline and both ceilings together, because rule
behavior changes between versions.

### GitHub Actions CI

Each workflow has at most three jobs, because every job shows up as a check on
a pull request. No job only summarizes the others: each job's name says what it
gates, every job must pass, and a workflow passes only when all of its jobs do.

`.github/workflows/ci.yml` runs on pull requests targeting `master`, pushes to
`master`, and manual dispatches. G1 is two of its jobs: `CI / level-1-backend`
runs `scripts/check_backend.sh` (the whole backend lane, unsharded, four pytest
workers) on one runner, and `CI / level-1-contracts-frontend` runs
`bash scripts/check.sh --lane contracts --lane frontend`, the other two lanes run
concurrently exactly as `check.sh` runs them locally. G3 is the third job,
`CI / level-3-postgres-integration`. `check.sh` with no arguments remains the
complete local G1; `--lane <name>` (repeatable) runs only the named lanes and
exists so CI can give the backend lane a runner of its own.

`.github/workflows/daily-extended.yml` (`Daily Extended Gate`, one daily cron plus
manual dispatch) is G2 in three jobs. `level-2-backend` and
`level-2-contracts-frontend` run G1 command for command as `ci.yml` does; only the
dependency install differs, keeping pip's wheel cache off as described below.
`level-2-contracts-frontend` then also runs `scripts/check_frontend.sh` with
`FRONTEND_LANE_PARALLEL=1`, even when the G1 step failed: CI runners have fewer
than eight CPUs, so this is the only CI run of the frontend lane's parallel path,
and PR CI never forces it. `level-2-complement` runs
`check_extended.sh --complement-only`: the extended backend marker set, the sample
plugin's UI lane and the facade-retirement ratchet, without re-running G1.
`--complement-only` exists for this workflow alone; a local run with it is not a
G2 run, and `check_extended.sh` with no arguments remains the complete local G2
(G1, then the complement, in order). Until 2026-10-07 the daily workflow was one
job running `check_extended.sh` on one 4-vCPU runner within 25 minutes; the last
green runs took 23–25 minutes, G1's backend alone grew past 20 minutes there, and
every run from 2026-10-07 was cancelled at the timeout without any G2 result.

Both workflows use `ubuntu-24.04`, Python 3.13 and one Node.js major, install from
the declared lock/requirements files, and delegate selection to the wrapper
scripts. `test_ci_workflow_contract.py` pins the job sets and names, the at-most-
three rule, that the G1 jobs together run every lane in `check.sh`'s `LANES`
exactly once, that the daily G1 half mirrors `ci.yml`, and it executes
`check.sh` and `check_extended.sh` with and without their arguments.

The repository supports exactly one Node.js major, 26, pinned in `.nvmrc`. Every
`actions/setup-node` step reads `node-version-file: .nvmrc`;
`frontend/package.json` declares `"engines": {"node": ">=26 <27"}` and
`frontend/.npmrc` sets `engine-strict=true`, so `npm ci`/`npm install` refuse any
other major; and `scripts/check_frontend.sh`, `scripts/check_sample_plugin.sh` and
`scripts/pack.sh` stop before doing any work when the Node they would use is not
that major (`scripts/node_version.sh`). Node 26 ships built-in Web Storage globals
whose getters return `undefined` unless `--localstorage-file` is passed, and
vitest's jsdom environment lets them shadow jsdom's own;
`frontend/test-support/setup.ts` restores real jsdom storage, and the matching
`Storage` class so `vi.spyOn(Storage.prototype, …)` still intercepts, whenever the
built-ins read as `undefined`.

The backend and contracts wrappers preserve offline environment isolation when run
directly, print slow pytest durations, and write JUnit reports under
`backend/.local`; CI uploads them, and the extended backend lane's
`backend/.local/backend-extended-junit.xml`, for seven days, including on failure.
The extended backend lane uses the same offline isolation and bytecode prefix as
the other lanes.

Six production-source guard scans share one worker and content-validated AST cache;
every lookup rereads the file, so edits cannot reuse a stale tree. Agent Profile
job base/overlay tests and the store tests use independent copies of the current
schema, while the store's deployed-v52 upgrade test keeps the real ladder and its
old-schema assertions.

The committed OpenAPI contract is byte-semantically frozen, so
`backend/requirements.in` pins FastAPI `0.135.3` and Pydantic `2.12.4`
exactly. Upgrade either framework only together with an intentional OpenAPI
contract regeneration and a clean-environment G2 extended-gate run.

The workflow is read-only, does not receive model or deployment secrets, and
uses four backend pytest workers per runner to avoid oversubscribing the hosted runner.
Backend installation sets `HNSWLIB_NO_NATIVE=1`; G1 Python lanes share G3's dedicated
portable-wheel cache with an exact OS/architecture/Python/lock-file/policy key
and no fallback to another cache. G2 continues to disable pip's wheel cache:
`hnswlib` otherwise builds with `-march=native`, and a cached locally built
wheel can crash with `SIGILL` when restored on a hosted runner with different
CPU features. The portable build trades a small ANN speedup for deterministic
CI; production wheelhouses may still target their declared deployment CPU.
Job timeouts include dependency installation and are intentionally separate from
the under-100-second local Apple Silicon warm-gate target. The two backend jobs
(`level-1-backend`, `level-2-backend`) get 30 minutes: on 2026-10-14 the unsharded
backend lane took 8:05 in PR CI and 11:59 in the daily run on four workers (jobs
8:41 and 13:03), and a single job has no other runner to spread over, so the limit
leaves more than twice the measured time for growth and slow runners. Every other
offline job keeps 20 minutes (measured: contracts and frontend 5:00 in PR CI and
7:30 daily including the parallel path; the complement 3:50); G3 keeps 35. None of
these checks is a required `master` status check; making one required needs the
user's explicit approval of the branch-protection change.

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
OS/architecture/Python/lock-file/policy key, with no generic fallback cache;
`HNSWLIB_NO_NATIVE=1` remains mandatory. The whole PG job is measured against a
three-minute optimization target; a successful test result alone does not prove it met
that target, and cold-cache installation time is included in the observation.
The lane covers direct PostgreSQL behavior only. The dedicated suites removed in
2026-07 (SQLite-adapter implementation tests, SQLite-to-PostgreSQL import/forward-shadow
tests and a standalone cross-backend parity suite) are not active coverage; removing them did
not retire the SQLite backend, which is kept long-term. Dual-backend parity is held by each
feature's SQLite tests in G1 and their PostgreSQL twins in this lane, plus the database-free
`backend/tests/test_dual_backend_parity.py` in G1.

CI portability is part of the gate contract: every filesystem, data, and
dependency path used by a CI-executed test is repository-relative and
independent of the process cwd. Committed fixtures are located relative to
their repository files, never through a developer checkout path or `HOME`,
and tests never read repository-external source documents. Every third-party
package imported during test startup is declared in `backend/requirements.in` or
`backend/requirements-dev.in` and locked; a clean hosted runner installs from the
two lock files and `frontend/package-lock.json`, then passes from those
declarations alone. Lane timings remain visible for
observation, while the under-100-second target applies only to the verified
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
- A dialog that must open on top of another root dialog without replacing it
  (for example the image preview over the global Ask window) declares the pair
  in `ROOT_MODAL_POLICIES[slot].stacksOver` and opens with
  `rootModals.openAbove(slot, parent)`. Escape, backdrop and focus return then
  follow the coordinator's topmost rule; do not special-case the child's Escape
  inside the parent. One Escape key press closes at most one layer through
  `requestClose(slot, "escape")` (the coordinator counts presses; holding Escape
  is one press — auto-repeat keydowns never start a new one), so a close
  request the browser sends after the keydown cannot close the revealed parent.
  A layer that eats Escape outside the coordinator (capture-phase listener)
  calls `claimEscapePress()` for the press it consumes.
- Source-activity anomalies render only through `AnomalyBadge` backed by
  `sourceAnomalies()`; do not hand-roll inline warning styles or symbols.
- Five-grade effort selectors reuse
  `frontend/app/effort-picker.tsx::EffortPicker`; do not duplicate the range
  control or recreate its popover. `frontend/tests/unit/effort-picker.test.mjs`
  guards the shared implementation and its callers.

## Development Workflow

For every task that will write repository code, tests, documentation, or configuration, create a new linked git worktree and branch before the first write, complete and verify the work there, and open any resulting PR from that branch. The main local checkout stays read-only for the task; tiny fixes are not exempt. If the current directory is already an isolated linked worktree, keep working there. Pure research, design, status, and review-only work does not require a worktree.

Identify the requested outcome and authorized scope before acting. Review and design tasks deliver findings or proposals; implementation tasks deliver verified changes. A review request does not authorize code changes or remote delivery, and a local implementation request does not authorize deployment, external messages, PR creation, merging, or other remote mutations. Within the authorized scope, make routine implementation decisions and continue until the requested outcome is complete; state material assumptions. Ask only when missing information materially changes correctness, scope, compatibility, or authorization and cannot be resolved from available evidence. Continue independent work while awaiting an answer. Existing authorization remains valid within its stated scope; do not request the same approval again.

**When implementing a feature, no part of it may be left to be implemented later.**

- The scope a plan writes in is finished in that round; a large scope is split into several PRs **delivered back to back**, never cut.
- A gap found during implementation or review that belongs to closing the feature's effect loop is filled in the same round, not registered in `fangan_todo.md`.
- Points that need a product ruling are settled before work starts, never bypassed by "doing half first".
- A subagent must not register an in-scope gap as deferred on its own; it reports the gap on discovery and fills it.

For approved multi-step implementation plans, use subagent-driven development by default: assign each task to a fresh implementation subagent and require task-scoped specification and code-quality review before moving on. Small tasks, research, design, status, and review-only work do not require subagents. Delegate only bounded work with clear file/task ownership, acceptance criteria, and explicit verification responsibility. Independent tasks may run concurrently when their ownership does not conflict; do not split a small task merely to use more agents. Review delegated results against acceptance criteria and specific concerns instead of repeating the entire task.

Run focused checks while editing and the required standard gate before claiming implementation complete. Once both pass, stop verification unless subsequent edits, failures, or a specific unresolved risk justify another run. This stopping rule does not waive required verification lanes or the PR review and CI checks below. Do not add tests that merely mirror implementation details. Read-only review inspects the diff and the submitter's verification evidence; it does not mutate the tree or rerun the full gate unless explicitly requested.

Keep handoffs concise and outcome-first: report changes, checks run, checks that could not run, and remaining limitations without repeating the execution history. For reviews, state the reviewed revision and scope; tie each finding to a concrete trigger, impact, and code location, and distinguish verified behavior, static reasoning, and architectural debt.

Shared development, verification, and delivery rules are owned by this document and its Chinese pair. `AGENTS.md` provides the general agent entry point and canonical routing; `CLAUDE.md` supplements it with Claude Code-specific resident rules. Resolve conflicting shared rules against their canonical owner and correct the entry points; carrier-specific rules apply only to the carrier they name. Neither entry point is a duplicate product or architecture reference.

Claude Code auto-loads `CLAUDE.md`. Its hardest Claude-specific rule is that **spawning a subagent must state the model explicitly instead of inheriting the main agent's** — tiered by how much judgment the task needs: `opus` for judgment work (writing plans, review, architectural trade-offs, hard diagnoses), `sonnet` for transcription-shaped implementation whose spec is already pinned down, `haiku` for pure search and location. The PreToolUse gate `.claude/hooks/require-subagent-model.py` enforces it: a call that passes no `model` and whose `subagent_type` is not pinned to a model in `.claude/agents/` is denied. Three pinned roles ship in `.claude/agents/`: `impl-task` (sonnet), `spec-review` (opus), and `code-quality-review` (opus). `backend/tests/test_claude_subagent_model_hook.py` is the hook's regression net — it runs the real script over a subprocess boundary and covers both directions, the bypasses that would let an inherited-model call through and the false denials that would push people to work around the gate.

A change that users need to know about, or need to act on, adds one `release-notes/<slug>.md` in the same PR: a front matter with `level` (`change` / `feature` / `fix` / `internal`), `audience` (`all` / `admin`) and a one-line Chinese `title`, plus an optional one- or two-sentence body, written for ordinary users in the [UI vocabulary](./ui-vocabulary.md). Only `change` and `feature` reach the post-upgrade dialog; `fix` is counted there and `internal` appears only on the update-history page. One feature gets one note: a follow-up PR extends a note that has not shipped instead of adding another. Internal-only changes (refactors, tests, documentation) add none. A backend guard parses every real note and enforces the length limits registered in [System update notice](./product-and-api.md#system-update-notice). The authoring rules live in [`release-notes/README.md`](../release-notes/README.md); `scripts/check_ui_vocabulary.py` scans these notes like any other user-facing copy.

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
