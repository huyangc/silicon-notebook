# Development and repository contracts

[Back to README](../README.md) · [中文说明](./development_zh.md)

This document preserves the contributor-facing architecture summary, verification gate, workflow, test architecture, and documentation-maintenance contract. [AGENTS.md](../AGENTS.md) is the short coding-agent entry point and document router; [architecture.md](../architecture.md) owns the detailed runtime architecture.

The external-Agent MCP surface has one API-owned registration host and a startup-frozen catalog: the fixed bundles under `app.api.mcp_tools` captured as the exact 23-tool surface. `mcp_server.PUBLIC_TOOLS` is that live catalog, and `CORE_TOOLS` is the same list under its structural name -- there is no second, hand-maintained copy. Core handlers preserve their validation/auth/I/O order and use the same live authority, owner-write, progress, and output boundaries. Exceptions map to stable public codes. Registration/listing performs zero repository/model work. The former external `agent.tool_provider` descriptor-append half had no consumer and has been removed.

Deep Report backend batch export is the first real single-Provider consumer. The repository owns authorization and completed-row selection, releases its connection, and passes a minimal immutable batch to the startup-frozen `report.exporter` host. The default built-in Markdown provider is the only default provider; there is no fallback formatter. Core validates the complete ordered result and retains filename collision policy and ZIP construction. The existing browser-only single-report Markdown download is deliberately unchanged and is not a backend provider path.

## Numeric limits and truncation

Production code must not hide result-changing literal slices or limits at a
call site. Reuse a named protocol constant for invariant wire/storage bounds;
use a validated `Settings` field for quality/cost budgets. A user-authored list
is validated and rejected when over its shared backend/browser rail, never
silently sliced. Embedding and other model-input truncation must use one
configuration source across online, batch, and backfill paths. Explicit numeric
fixtures in tests are outside this rule.

## Architecture Boundaries

- The modular-extension foundation and Phase-1 retrieval host contract are enforced. Stable cross-layer values live in `backend/app/domain`; repository ports cannot import services, the static graph stays acyclic, and repository-to-service debt ceilings may only fall. `ReasoningRetriever.run` is split at the first round only: `_new_run_state` builds the explicit `_ReasoningRunState`, and `_run_first_round` drives the `_first_round_*` phases in the contract order (exact-lookup seed after the PPR seed, coverage pass budget `max_steps // 2`). Focused intent, PPR, exact-lookup, fallback, coverage-ledger, enumeration, and synthesis tests own the durable behavior; the refactor-only byte-for-byte golden snapshot has been retired. The dependency-light SDK owns typed contracts; `backend/app/extensions` owns the frozen registry, live capability decisions, and shared host; `app.bootstrap` is the only outer root that joins extensions to adapters. Workflows depend on the domain host port, not the registry. Availability probes are I/O-free, manifest declarations plus live decisions project only narrow capability ports, and an unavailable capability disables only its contribution while independent contributors remain active. Invocation routing and core admission policy are startup snapshots. The empty/no-applicable path returns the exact baseline before any work. Bounded proposals use request-memory authority for built-ins and at most one core-owned batch hydrate for unresolved peers, never per-hit N+1; malformed contributions fail open and strong lanes may use atomic admission. Selected-source graph and generated-question recall are the first built-ins, on `selected_evidence` and `chunk_candidates`. The graph bridge retains the legacy activation result, preserving attestation, rollout, scope drift, duplicate-support overlay, independent budget, status, and fail-closed behavior; Ask/Report no longer call the graph service directly. The generated-question bridge owns query/index/settings and `(scored, ids, matrix)` privately, remains before MMR/fusion, stages collision supports on an isolated copy, and commits only after host admission. `off`, satisfied-trigger, empty, overflow, failure, and `shadow` keep the exact baseline; `on` only appends original chunks. Its SQLite/PostgreSQL scan applies notebook/source and retrieval-run actor private-Memory predicates before `LIMIT`, while actor identity stays out of events. Connection probes block host fan-out while a transaction/pooled lease is held; PostgreSQL conformance forces pool size 1. Core cancellation propagates through single and multi-query paths. Plugin implementations may not import concrete repositories, facade, or runtime. The G1 architecture guard enforces these rules and facade surface may shrink but not grow. See [the design](./modular-plugin-architecture-design-2026-08-21.md) and [delivery pipeline](./modular-plugin-architecture-delivery-plan-2026-08-21.md); each PR needs two independent subagent reviews and green CI before squash merge. `scripts/check_architecture_boundaries.py` (the G1 contracts lane) additionally holds three zero-slack ratchets recorded in `scripts/architecture_boundary_baseline.json`: the `app.core`/`app.models` → `app.services` import allowlist (currently empty; bare `import app`, `from app import services` and `import app.services` all count), the per-function line ceilings of the listed hot functions, and the total Protocol-method count of `repositories/ports.py` (resolved through the module's own `Protocol` import aliases, so `from typing import Protocol as P` cannot dodge it). Shrinking any of them requires lowering the baseline in the same change. Both production retrieval lanes (selected-source graph and generated-question recall) are dormant-aware: when the workflow already knows the feature is unconfigured and `RetrievalContributorHostPort.has_contributions` reports nothing else surviving on that invocation, the lane returns the baseline before any call or context is built; a registered-but-unavailable contributor still goes through the host.
- Production source ingestion uses one startup-frozen self-hosted MinerU → MinerU cloud → built-in ProviderChain. Links declare `after`/`before` edges and stable-ID ties instead of integer priority. The complete core route plan is frozen before live availability or provider I/O, so a failed configured self-hosted link can never open public cloud. Probes may perform one remote parse and pure mapping but receive no persistence/asset port; workbook reconciliation and core admission precede the accepted materializer. URL local fallbacks reuse one request-local download, and the per-source lock covers asset replacement through element/chunk-marker publication. The legacy dispatcher and facade parser patch seam are retired; do not recreate a dual route.
- `backend/app/application` owns dependency-light immutable stage envelopes and a module-level import allowlist currently limited to `application`, `core.ask_retrieval_policy`, `domain.cancellation`, and `models.ask`; bare root imports and unaliased allowed-submodule imports are forbidden because both bind `app`, while explicit submodule aliases remain legal. Future contracts extend the list deliberately rather than opening a package with implementation back-edges. Ask reasoning crosses explicit prepare, retrieval-evidence, response-draft, and committed-answer boundaries; frozen response envelopes transfer the exclusive typed response graph without JSON/deep-copy identity loss. The response draft is produced by an injectable `ResponseDraftStage` (entered through `execute_response_draft_stage`, which rejects any other output type); the shipped `DefaultResponseDraftStage` is the previously inline synthesis/binding logic, receives one frozen `ResponseDraftInput`, and reaches no repository, model client, settings, or persistence seat *through that envelope* -- the shipped default still uses `AskService`'s own seats via `self`, since it is a method of the service, while an injected implementation gets only the envelope and the runtime, and both are admitted to the seam only after the orchestrator re-verifies authority immediately beforehand. Cancellation is checked once before the seam and once at the commit boundary. The commit boundary re-verifies `mode` and every identity field the draft envelope carries (`notebook_id`, `question`, `conversation_id`, `user_id`, `job_id`, `asked_at`) against the same frozen `PreparedReasoningAsk` the stage was handed, so an injected stage cannot silently persist the answer into a different job, conversation, or notebook than the one it was asked to serve; `model_errors` is filled by core from the request-local model-error sink right after the stage returns and before the retrieval ContextVars reset, so a stage does not need to (and should not) set it itself. Its runtime authority cross-binds the exact source scope, `ask_reasoning` request-local run (and therefore its leaf-I/O semaphore), cancellation token, non-empty persistence actor, trace sink, and the injected I/O-free connection probe by identity; the service and typed retrieval seam independently validate run kind/actor. Stage-boundary violations are loud core failures, not optional retrieval misses. The legacy mutable `ReasoningResult` remains available to Report working copies, and only existing KG/chunk/element/PPR leaf calls acquire the run slot; no stage wrapper holds a database connection or an outer slot.
- Deep Report uses its own immutable application envelopes for confirmed planning, generated sections, core final audit, and committed completion. Planning and generation each retain a fresh retrieval run; exact scope/run/cancellation/actor/probe authorities are checked at every typed boundary without moving any leaf slot. Mutable evidence/id maps transfer by exclusive ownership rather than JSON/deep copy. Multi-section all-retrieval → one synthesis → parallel drafting, the single-section zero-synthesis path, final editor, claim ledger, citation remap, report-wide image batch, zero-body failure, and retry remain core-owned and order-equivalent; final audit cannot rewrite section Markdown. SQLite/PostgreSQL publish `done` through one `status='generating'` CAS, cancellation likewise CASes only a non-terminal row, and only a successful completion yields a `CommittedReport`. Manual and auto-generation converge in the coordinator after generation gate and scope/retrieval/model contexts are released. Then the built-in `report.completed_observer` invokes the historical agent-profile signal through one opaque at-most-once access; cancellation registration is removed afterward to preserve the existing active-job window. That point may not rewrite durable artifacts or start retrieval/model work.
- Streaming Ask post-completion uses one startup-frozen `ask.completed_observer` host. The durable answer, terminal job row, cancellation unregister, and browser final event precede it; the sentinel follows. Three built-in observers preserve the existing sequential agent-profile → retrieval-experience (reasoning only) → search-profile behavior and cost, with independent failure isolation and notebook+actor / zero-identity / actor-only capability projections. Connection probes reject held transactions or pooled leases before plugin execution. Ordinary failures cannot reverse `done`; post-terminal hooks do not inspect request cancellation. Synchronous POST Ask and MCP Ask remain outside this completion count, and services depend only on domain host ports rather than SDK/registry/concrete hosts.
- Point-specific proposal sources and the generic admission reader are separate domain ports. Selected-source graph authority resolves from its request-local map with zero added DB/leaf work; only unresolved proposals call the repository-runtime reader, once, with notebook/source predicates applied in SQL before rows are returned. Report fallback reads acquire the shared retrieval leaf gate. The SQL always filters Memory sources by actor, including compatibility calls with no frozen scope; visible sources and notebook-wide Knowhow remain eligible.
- Backend endpoint bodies live in domain FastAPI routers composed by `backend/app/api/routes.py`; the aggregate is composition-only and owns router order, not product handlers or compatibility exports. Boundary tests inspect endpoint ownership on the domain routers and verify the aggregate's composition declaration semantically; they do not assume `include_router()` flattens child routes, because newer FastAPI versions retain lazy included-router nodes. Domain Pydantic models live under `backend/app/models/`; `backend/app/models/schemas.py` is a legacy compatibility facade that re-exports the same model objects for old imports.
- One repository factory selects `SQLiteRepository` or `PostgresRepository` from `DATABASE_URL`; both compose the same runtime boundary. `RepositoryFacade` is backend-neutral over an injected `RepositoryRuntime` bundle. Application services do not assemble product SQL, inspect dialects, or import the opposite adapter. Stores own product SQL and raw row selection; established application/query components may assemble domain/application projections such as `NotebookSummaryQuery.from_row`. SQLite retains its compatibility migration/maintenance wrapper and PostgreSQL owns a bounded Psycopg pool plus checksummed migrations. Every facade operation is an explicit compatibility adapter or belongs to the source-checked one-hop delegates whose real targets match the ownership manifest. The dependency direction is factory/wrapper → facade → runtime → services → stores. `sqlite_identity.py` and `sqlite_notebook_sharing.py` remain compatibility re-export shims, and the legacy request-context, `_COPY_CHUNK`, and `_remap_json_ids` exports stay importable. The facade's public surface has a per-method caller ledger (`docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md`, reproducible via the read-only `scripts/audit_facade_callers.py`) classifying every public member `keep`/`test-only`/`ambiguous`/`retire-now`; retirement proceeds batch by batch off that ledger, syncing `scripts/architecture_boundary_baseline.json::facade_public_surface` and `scripts/generate_repository_contract_fixtures.py --rebaseline-surface`'s output each time.
- Notebook authorization predicates have one definition point per backend: `backend/app/repositories/{sqlite,postgres}/access_sql.py` (mirroring `mount_sql.py`; placeholder-style mirrors, always changed together). Write is owner-only, management is owner ∪ an effective `role='admin'` grant edge (`NOTEBOOK_ADMIN_SQL`, reusing the read predicate's restricted three arms plus `role='admin'` and excluding `everyone`), and read is owner ∪ read-only member ∪ an effective `notebook_grants` edge (`user`/`group`/`group_admins`/`everyone`, matched against that exact four-value whitelist so shadow-parked rows fail safe) — that `write ⊆ management ⊆ read` asymmetry is a security boundary, and the owner-or-member clauses embedded in Memory read/search SQL derive from the same fragments; the deliberately kept three-step `FOR SHARE`/three-state sites in the memory stores are pinned by an allowlist and must be extended by hand whenever the read predicate widens. Read access implies mountability, but a *restricted* grant (anything short of `everyone`) only mounts while the mounting notebook itself is unshared — that unshared gate stops a borrowed library from being re-shared onward; `tier='base'`/`everyone` bases are exempt. API write endpoints declare a named capability via `app/api/deps.py::require_notebook_capability(...)` (nine names across the frozen `{owner, admin}` value domain — P2 flipped the six content-management capabilities `sources:write`/`kg:write`/`knowhow:write`/`knowledge:write`/`catalog:write` + `notebook:manage` to admin, while `notebook:configure` (mount config + link sharing), `notebook:delete`, and `reports:write` stay owner-only; an unregistered name raises `KeyError` at import time), and body-level checks that resolve `notebook_id` from another id go through `notebook_capability_allowed(capability, ...)` against the same table. New write endpoints must use the capability factory, never a bare owner guard. Guards: `backend/tests/test_access_sql_contract.py` (introspective cross-backend parity, placeholder-direction checks, inline-shape scan, two-step allowlist) and `test_notebook_capability_guard.py` (AST identifier scan with empty-scan protection). Group knowledge sharing (design: `docs/superpowers/specs/2026-08-17-group-knowledge-sharing-design_zh.md`) extended the read predicate and flipped those capability levels without touching endpoint declarations (the Agent/MCP surface deliberately did not flip, and P2 adds the `notebook_share_requests` member-contribution approval flow: a member requests sharing a library they manage into a group they only belong to, a group admin approves by inserting the `(group, viewer)` edge in one transaction, and `status` is exact-matched `pending`/`approved`/`rejected` with withdrawal a whole-row `DELETE`) — **except deep reports**, the one registered exception: members of a shared notebook create their own reports and reports are private to their creator, so the nine report write endpoints declare `require_notebook_read` plus an in-body row-level `reports.created_by == current user` check (`report_routes.py::_own_report_or_404`, required by an AST guard on every `{report_id}` route), list/export narrow by the same predicate in SQL, and another member's report answers 404 exactly like a missing one. `reports:write` remains registered with no consumer, reserved for P2 group-admin management actions; the anonymous share page re-checks the creator's live read access per request.
- Offline production maintenance uses `open_maintenance_cli_repository`: PostgreSQL confirmation/capability rejection happens before factory construction, the command then owns an independent-session fail-fast advisory lock, and repository close is unconditional. `BatchMaintenancePort` is the portable orchestration contract; SQLite text-vector conversion is a separate physical-format port. PostgreSQL keyset predicates and ordering both use `COLLATE "C"`, and model calls happen after page reads release database connections. Batch source inventories exclude hidden projection sources according to phase. The offline full gate never contacts PostgreSQL; real coverage belongs to the dedicated PostgreSQL 16 lane.
- `prepare_selected_source_graph.py` composes the portable maintenance operations as an all-notebook deployment state machine: durable reverse-index pages, durable source-fact generations, cheap version/count artifact probes with bounded rebuild on mismatch, and an independent fact audit all complete under the offline-maintenance lock. The receipt is content-free and non-authoritative. Only after repository close may the script atomically write the four invisible-shadow env assignments; any phase failure preserves the prior env file. Re-entry revalidates authoritative state and skips current generations/artifacts instead of replaying large-library work.
- `RepositoryRuntime` owns or references composed runtime state; `REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner, and the runtime, report coordinator, and module compatibility functions share that same identity reference. Other mutable operational state (storage root, embedder, language caches, build sets, Ask cancellation registry, and artifact caches) is runtime-owned; replacing supported compatibility properties after composition updates every retained consumer. Synchronous Ask/report submission failures mark the already-created durable job/report failed, unregister the cancellation entry, and re-raise the submission error; successful worker ordering and the existing Ask transaction checkpoints remain unchanged. Its composition is split by domain: `RepositoryRuntime.__init__` only calls the module-level `_build_*` domain constructors (plus its own two `threading.Lock()`s) in order and mounts each returned frozen bundle's fields on itself with one explicit line per seat. Call order is the dependency topology — a builder takes earlier bundles and never the runtime itself, so a cycle back into the composition root cannot be spelled; the only permitted runtime-bound inputs are narrow late-bound callables (the current-user accessor, the `ask_service` accessor and `_note_ask_completed`). Process-level side effects (scheduler validation, event logger, `kg_scheduler.initialize`) and the one persistence bundle keep their exact historical order; `backend/tests/test_repository_runtime_composition.py` freezes the mounted attribute set and both rules.
- Built-in KG relations are governed by one typed registry in `backend/app/domain/kg/edge_schema.py` (`backend/app/services/kg/edge_schema.py` is a re-export shim; new `EdgeSpec` entries must not be declared there). Core extraction is fail-closed; graph/PPR/canonical/relation and Ask evidence-context consumers filter invalid historical core pairs while preserving known edges attached to administrator-defined extension types. `EDGE_SCHEMA_VERSION` participates in scale/PPR artifact identities. Optional completion advances mode-specific persistent source-generation keyset pages, prioritizes anchors through indexed contract-valid relation `EXISTS`, and uses only bounded same-source FTS/ANN candidates plus section/pair/batch/character rails. Each job hydrates only its bounded objects and their capped evidence IDs; unfinished watermarks re-enqueue and startup recovers current pending generations. A mode change atomically publishes the newly active mode's recoverable cursor before retiring the old cursor as `stale`. Proposal and verification run outside database transactions; a short final write rechecks generation/ownership/existence, persists the exact server excerpt seen by the verifier, and inserts idempotently. Invalid zero rails fail closed without advancing. Retrieval origin is represented as accumulated producer support records; selection never reconstructs provenance from scores.
- Large selected-source graph companions are separate from the legacy scale directory. The offline builder reads and publishes one visible source partition at a time through source-first bounded projections, binds the constant-size companion root and every partition to the main manifest version, hashes every payload file, and uses a deterministic hash path so runtime can open only selected sources. Before payload I/O, the reader preflights every selected small manifest against cumulative node/nnz/cross-edge rails. Per-partition local CSR rows carry object types/chunk identities; one selected source reuses its persisted CSR, while a selected union uses array-oriented sparse composition and one bounded cross-edge allocation. Source-owned cross-partition relations are admitted only after the union revalidates both endpoints and the central edge registry. Candidate ranking applies partial Top-K rather than a full Python sort. Legacy/missing/corrupt/over-limit/mismatched companions are capability-unavailable and never authorize whole-graph post-filtering. Full rebuild and delta fold both republish the companion and invalidate its dedicated single-flight LRU. The runtime reader is consumed only by the shared Ask/Report activation service and fails closed to B when unavailable.
- The selected-source quality boundary is split deliberately: `app.eval.selected_source_graph` owns golden-case evaluation and observation parsing, while `app.services.source_graph_quality` owns the versioned content-free attestation schema/verification used by production, and `app.services.source_graph_rollout` owns pure off/shadow/allowlist/hash/on decisions. Production modules never import `app.eval`. The suite freezes model/sampling/corpus/scope/source aliases, binds citation anchors to evidence provenance, evaluates hard isolation and baseline preservation before quality/cost deltas, and checks both each case and the aggregate. Activation pins the canonical golden digest; custom golden files remain diagnostic only. Production recomputes every content-free case/aggregate rail, and missing corpus/model pins fail closed. The attestation digest detects accidental mutation only; trusted-path ownership remains a deployment responsibility. Only the shared activation service imports the rollout decision; Ask/Report consumers do not implement a second gate.
- `SelectedSourceGraphActivationService` remains the only graph-activation algorithm, but Ask/Report reach it only through the built-in selected-source graph contributor and its core-private request bridge. Callers must finish and freeze historical `B` before invoking the host; the service reads only a server-frozen, genuinely narrowed `include` scope, builds the bounded snapshot, tries online scoped PPR/neighbor memberships and then the source-partition companion when needed, rechecks every returned source id, and passes `G` through `BaselineProtectedEnrichmentService`. Whole/all-selected scopes return before snapshot I/O. Default invisible shadow returns `B`; approved active modes return `B + G`; every failure returns `B`. Status is internal observability only and may not enter public payloads, traces, streams, or UI. Do not add a second rollout parser, a workflow-level service call, another graph consumer, or a client-computed narrowing rule.
- Databases created before the refactor keep loading unchanged. `scripts/verify_repository_snapshot.py` uses exact per-version migration and stable-seed manifests, percent-encodes SQLite URI paths, constructs the repository only on a temporary backup, and reports the retained backup path if cleanup fails without printing private rows. It guards the original database/WAL metadata plus SHM existence and size; for a live WAL attachment only SHM mtime is exempt because SQLite may rebuild it.
- Reasoning source identity lookup is an identity-only repository operation: it reads no source text, summaries, elements, KG payloads, or embeddings. Both adapters page the visible authorized roster in stable `(created_at,id)` order through the partial `idx_sources_visible_identity` index on `(notebook_id, created_at, id) WHERE source_type NOT IN ('memory','knowhow')`. The service resolver that consumed this roster is gone with the model-inferred source scope, so `visible_source_identity_rows_bounded` currently has no production caller; the index and both implementations are kept because retrieval scope is still expressed as `(notebook_id,source_id)` keys and an empty source-id set means empty rather than unrestricted.

Schema changes remain version-gated behind `SqliteMigrator`: append a new
`_migration_N`, bump `SCHEMA_VERSION`, and never modify a sealed migration.
Startup recovery, stable seeds, and administrator upgrades run every boot
outside that version gate.

The current schema version is 59. This is the SQLite schema version. The committed v9 compatibility fixture
upgrades through migrations v10–v59 and remains readable. Those migrations
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
backing content-hash upload dedup and batch_ingest resume. SQLite v31 adds only the
inert, payload-free shadow_change_log and shadow_capture_control internal
tables; run-scoped guard/capture/freeze DDL is installed separately. Guards
enforce uniqueness immediately after installation, while capture/freeze
behavior stays disabled until the run control state enables it. SQLite v32 adds
reports.understanding_json for the durable question-understanding contract;
SQLite v33 adds covering `(notebook_id, source_object_id/target_object_id, id)`
relation indexes for stable, bounded lexical-relation keyset recall. SQLite v34
adds the indexed `kg_relation_completion_state` source-generation watermark and
the `(source_id,id)` object keyset index. SQLite v35 adds the browser-captured
`ask_jobs.asked_at` instant for reconnecting to in-flight questions. SQLite v36
adds the three KG-quality-analysis precompute product tables
(kg_community_edges, kg_source_profiles and the kg_analysis_artifacts product
ledger); rebuild_communities rewrites all three wholesale and stamps every ledger
row with the kg_mutation_seq it was built at. Publication is atomic across the
community layer too: the board partition, its community_seq stamp and all three
product tables commit in one write transaction, while every full-table read that
feeds them stays outside it (the SQLite write lock is process-wide). None of the
three carries a level column: the community layer's freshness gate is not
level-scoped, so the level a product set describes is recorded in the ledger
payload instead. SQLite v37 adds the indexed `(source_id, element_type,
created_at, id)` ordering on `source_elements` for bounded, per-type collection
enumeration (formula/table/image/code_block listings). SQLite v38 adds the
partial visible-source identity index `idx_sources_visible_identity` on
`sources(notebook_id, created_at, id)` excluding hidden Memory/Knowhow projections.
SQLite v39 adds the
command-catalog extraction tables `catalog_jobs` (one row per run, carrying the
per-source `queued`/`running` partial unique index that is the cross-process
single-flight guard) and `catalog_candidates` (one reviewable row per extracted
or grounding-rejected entry, keyset-ordered by a per-job `position`).
`catalog_jobs.source_generation` records the source element generation the run
was created against, so a reparse expires that run's candidates rather than
letting them be confirmed into content the document no longer holds.
v39 also installs `idx_knowhow_tables_nb_title` on
`knowhow_tables(notebook_id, title, created_at, id)` — the migration's only
index on a pre-existing table — so by-title target resolution seeks on
`(notebook_id, title)` and takes its `(created_at, id)` tie-break straight from
the index, rather than reading every table row in the notebook inside the
locked apply window.
`catalog_candidates.job_id` deliberately carries no foreign key: the rows
cascade from notebooks/sources directly, and an incoming foreign key would make
`catalog_jobs` a non-leaf table, leaving its single-column `source_id` guard
with no static parking strategy for the forward shadow.
SQLite v40 adds immutable `knowledge_source_facts` rows plus normalized
`knowledge_source_fact_elements` bindings. The ingestion writer validates the
current running extraction generation and every cited element's source inside
the global-KG transaction; replacement clears the prior generation in that
same transaction. The global object id is intentionally not a foreign key, so
later fusion/governance cannot erase source truth. This migration adds storage
and write lifecycle only; retrieval reads are activated by later PRs.
SQLite v41 adds `knowledge_source_fact_backfills`, a per-visible-source,
source-generation ledger for an explicit offline historical projection. The
backfill first builds the source reverse index once per notebook (and reuses
its completed marker on later runs), then uses
bounded source-first object pages and one short write transaction per page.
Only objects whose owner and every cited element are provably from that source
are projected; ambiguous legacy provenance is counted as `incomplete` and is
never guessed. An explicit `projection_origin` distinguishes live ingestion
facts from historical projections; a live fact remains counted even if its
fused global object has since been deleted. Cursor, counts, stable incomplete
reason, separate operational failure code, projection version, and terminal
status are restartable and auditable without exposing evidence text.
The audit independently reconciles the effective KG generation, projection
version, and persisted-fact count instead of trusting a `complete` ledger row.
Deep notebook copies remap facts, bindings, and terminal ledgers through one
source-generation map and synthesize a copy-local completed KG run. The copy
can therefore be audited or force-repaired without retaining the original
notebook's operational extraction history.
SQLite v42 adds `source_index_backfills`, the notebook-level execution ledger
for rebuilding `knowledge_object_sources`. Each bounded keyset page writes its
index rows and advances the cursor/counters in the same short transaction, so
a process restart resumes the last committed page instead of clearing the
notebook and starting again. The row is pinned to `kg_mutation_seq`; drift
records the stable `kg_generation_changed` code and leaves the fast-path marker
false, while the next invocation resets against the new generation. A current
completed marker is normalized into a completed ledger without rewriting index
rows. The ledger stores no evidence text or raw exception. This remains a
write-only preparation step; online Ask behavior is unchanged. SQLite v43 adds
revocable public-report sharing tokens. SQLite v44 adds
`chunks.question_indexed_at` and the source-owned `chunk_questions` table for
the optional generated-question retrieval supplement; question rows cascade
with their original chunk and notebook copies remap their
chunk/source/notebook identities. PostgreSQL migration v22 is the paired
schema. SQLite v45 adds the nullable `user_profiles.ui_mode` column backing the
per-user interface mode preference (`auto` default / `advanced`); readers fall
back to `auto` when the column or profile row is absent. PostgreSQL migration
v23 is the paired schema.
SQLite v46 adds `chunk_elements`, the element -> chunk reverse index, its
notebook-level execution ledger `chunk_element_backfills`, and the
`unified_kg_state.chunk_elements_indexed` marker that forks the read path.
`chunks.element_ids` stores the forward direction, so the per-query "which
chunks contain this evidence element" lookup used to scan every chunk row of
the notebook and JSON-decode each one per index generation; the composite
primary key `(notebook_id, element_id, chunk_id)` turns that into a bounded
point lookup, and the extra `chunk_id` index exists only to serve the cascade
from `chunks`. Every chunk write path a live notebook can reach maintains the reverse rows
inside the same write transaction as the chunk rows, and source
delete/reparse/knowhow cell rewrite removes them through that cascade. Whole-
notebook deep copy is the one registered exemption: it does not copy
`unified_kg_state`, so a copy's marker is always absent and it reads through
the legacy scan. The migration creates empty
tables only; historical rows are projected exclusively by the explicit offline
`backfill-chunk-elements` phase, whose ledger has the same shape and the same
`kg_generation_changed` fail-closed rule as `source_index_backfills` and stores
no chunk text or raw exception. Notebooks whose marker is still false keep the
legacy whole-notebook scan byte-for-byte. PostgreSQL migration v24 is the
paired schema.

SQLite v47 adds `notebook_object_schemas`, keyed by
`(notebook_id, object_type)`, for notebook-local graph-type definitions. The
global `object_schemas` table remains the administrator-managed baseline.
Effective registries overlay the notebook row on the same global type; a local
`disabled` row therefore suppresses that type only in its notebook. Local rows
also retain their creator for ownership/audit, while authorization continues
to be enforced by live notebook owner/read guards. PostgreSQL migration v25 is
the paired schema, and the forward-shadow manifest includes the new business
table.

SQLite v48 adds the nullable `sources.agent_profile_id` provenance column, which
records that an Agent (rather than a person) added a source. NULL is the
load-bearing value — it means "a person added this" — so nothing is backfilled:
every already-deployed row is user-added by definition. It deliberately carries
no index, no unique constraint, and no foreign key to `agent_profiles`: the
permission check behind the MCP `delete_source` tool is a single-row primary-key
read, nothing enumerates "sources of this agent", provenance must outlive the
profile row, and an incoming FK would add an edge to the forward-shadow parent
closure. The column is written on the INSERT branch only, so content-hash dedupe
that reuses an existing row keeps the first writer's provenance and a notebook
deep copy clears it outright. `SourceSummary` and the source detail models
project it as the `agent_created` boolean. PostgreSQL migration v26 is the paired
schema; because the column adds no table, index, constraint, or FK edge, it left
the forward-shadow invariants of its generation (74 business tables, 100 unique
surfaces, a branch-counted bound of 12 row slots) unchanged.

SQLite v49 adds the three group-knowledge-sharing tables. `groups` carries a
group's name, `kind` (`project` | `department` | `domain` — a classification
label that changes who may create the group and the interface wording, never the
permission mechanism) and description. `group_members` maps users to groups with
a two-level in-group role (`member` | `admin`), keyed by `(group_id, user_id)`
and indexed on `user_id` for the "which groups am I in" direction.
`notebook_grants` holds one row per live authorization edge —
`(notebook_id, principal_type, principal_id, role)` with `principal_type ∈
{user, group, group_admins, everyone}` and `role ∈ {viewer, admin}`. Every enum
is validated in the application layer, deliberately without CHECK constraints,
and `principal_id` is a **polymorphic** reference (user id, group id, or the
empty string for `everyone`) that intentionally carries no principal foreign key:
the forward shadow's static parking strategy requires at least one of those two
columns to stay a bare text column.

Two consequences of that shape are load-bearing. First, `principal_id` must stay
`NOT NULL DEFAULT ''`: NULL does not participate in unique comparison, so an
`everyone` row would escape `UNIQUE (notebook_id, principal_type, principal_id)`
altogether — duplicate grants would accumulate and a revocation would not fully
revoke — and NOT NULL is also what hands the shadow's parking column to
`principal_type` (SENTINEL_TEXT). Second, the `everyone` test must be the exact
four-value match `principal_type = 'everyone'` and must never be inferred from
`principal_id` (neither `IS NULL` nor `= ''`), because parking temporarily writes
a sentinel string into a conflicting row's `principal_type`, and exact matching
is what makes a parked row fail safe (it matches nothing). The `UNIQUE` implicit
index already covers `notebook_id` prefix lookups, so no separate notebook index
exists; `idx_notebook_grants_principal` on `(principal_type, principal_id)`
serves the "which notebooks is this group granted" direction. Notebook deep copy
deliberately does **not** carry authorization edges, following the
`notebook_members` precedent — access-control state is not knowledge, and the
copy's new owner re-grants it. Deleting a group clears the grant rows pointing at
it inside the same write transaction, because `principal_id` has no foreign key
to enforce that; `scripts/merge_dbs.py` sweeps the orphan edges a union merge can
otherwise resurrect.

PostgreSQL migration v27 is the paired schema. Because v49/v27 adds three tables
and one UNIQUE constraint, the forward-shadow invariants move to 77 business
tables and 104 unique surfaces; the branch-counted bound stays at exactly 12 row
slots (all three tables are shallow).

SQLite v50 adds `notebook_share_requests`, the member-contribution approval-flow
table — a sibling of `notebook_grants` deliberately kept out of the grant table
so the decision predicate stays status-filter-free. A plain member requests
sharing a library **they manage** into a group they are only a **member** of; a
group admin approves, inserting the `(group, viewer)` edge and updating the row
status in one write transaction. The state machine is one-directional
`pending → approved/rejected` (withdrawal is a whole-row `DELETE` by the requester
while `pending`, never a third status), both FKs cascade, and deep copy carries no
requests. `decided_at` may only be written as SQL `NULL` or an ISO timestamp,
never the empty string, because it is the one nullable time column this table
contributes to the forward shadow and PostgreSQL's `timestamptz` would type-error
on `''`; it is deliberately not in `POSTGRES_EMPTY_TIME_SENTINELS`. The partial
unique index `uq_share_requests_one_pending`
(`(notebook_id, group_id, status) WHERE status = 'pending'`) caps one in-flight
request per (library, group), and the create endpoint returns the existing pending
row idempotently on conflict rather than 409ing; `status` is exact-matched against
`pending`/`approved`/`rejected`, never `!=`. PostgreSQL migration v28 is the paired
schema; because v50/v28 adds one table and one partial UNIQUE index, the
forward-shadow invariants moved to 78 business tables and 106 unique surfaces, the
branch-counted bound staying at exactly 12 row slots (the new table is shallow
too).

SQLite v51 adds the two agent-understanding tables `agent_notebook_profile` and
`agent_profile_jobs` backing "AI 对这个库的理解" — a low-cost, LLM-consolidated
summary of what the agent has learned about a notebook. `agent_notebook_profile`
holds five label blocks keyed by `(notebook_id, owner_id, label)`: three shared
base-layer blocks (`corpus_shape`/`key_entities`/`corpus_gaps`, `owner_id=''`,
refreshed by a per-notebook consolidation job once accumulated source changes
cross a threshold) and two per-member overlay blocks (`retrieval_notes`/
`usage_gaps`, `owner_id` = that member's user id, refreshed once that member
completes enough Ask jobs or a deep report). `owner_id` follows the
`notebook_grants.principal_id` precedent from v49/v27: `NOT NULL DEFAULT ''`
rather than nullable, with no foreign key to `users` and no CHECK constraint on
it or on `label`. `agent_profile_jobs` is a one-row-per-chain status/counter
table keyed by `(notebook_id, owner_id)`; single-flight is a primary-key-row
compare-and-swap rather than a separate unique index. Both tables' replication
key equals their declared primary key exactly, so the forward shadow parks them
by `REPLICATION_KEY` with no sentinel column and no `_UNIQUE_PREDICATES` entry.
`agent_notebook_profile.history_json` is a bounded ring buffer of before/after
entries appended in the same write transaction as the block update, in place of
a separate change-history table — v1 offers no history-browsing UI, so a
queryable table would add manifest/copy-rank/parking overhead for no reachable
capability. Notebook deep copy carries neither table: a copy starts with no
consolidated understanding of its own, and job rows are transient process state
like `catalog_jobs`. PostgreSQL migration v29 is the paired schema. Because
v51/v29 adds two more (shallow) tables, the forward-shadow invariants move to
80 business tables and 108 unique surfaces; the branch-counted bound remains
exactly 12 row slots.

SQLite v52 adds three conversation public-sharing columns to `conversations`:
`share_token` (nullable, partial unique index `idx_conversations_share_token
WHERE share_token IS NOT NULL` covering issued tokens only, NULL-parking like
`notebooks.share_token`/`reports.share_token`), the read watermark
`shared_through_at` (a literal timestamp value, not a foreign key — storing an
answer id would go meaningless once that answer is deleted), and the
display-only `shared_through_id`. The token lives on the conversation row rather
than a side table (the `_migration_43` report-token precedent; a deleted
conversation takes its public link with it). Notebook deep copy needs no
handling for these columns: `_COPY_VALIDATED_TABLES` does not include
`conversations`, so they never travel with a copy and there is nothing to
clear — the migration comment records this so nobody adds a redundant clear by
analogy with the notebooks/reports siblings. PostgreSQL migration v30 is the
paired schema. Because v52/v30 only adds columns to an existing table, with no
new table and no foreign key, the business-table count is unchanged (still 80);
the new partial unique index alone raises the unique-surface count from 108 to
109, and the branch-counted bound remains exactly 12 row slots.

SQLite v53 adds `agent_profile_jobs.claim_token` (Agentic Memory P2): the
consolidation chain's claim GENERATION, a `TEXT NOT NULL DEFAULT ''` column
minted afresh on every `claim` and carried as part of the compare-and-swap by
both `settle` and `write_block`. It closes an ABA in P1's status-only
single-flight — a member removed and re-added gets a job row with the same
primary key and a `runs` counter back at 0, so a stale worker's settle used to
land on the replacement row (consuming the new run's snapshot) and its writes
used to pass a bare existence check. Because a delete plus recreate always
changes the token, "the row I claimed" and "the row that is here now" are now
distinguishable. `settle` consequently returns three outcomes rather than a
bool: `settled`, `gone` (no row — only member removal deletes it, so the
caller wipes the blocks it just recreated) and `superseded` (a row, but a later
claim's — the caller must NOT wipe, since the newer generation may already have
written its own blocks). No new table, index or unique surface, so the
forward-shadow invariants are unchanged at 80 business tables, 109 unique
surfaces and 12 row slots. PostgreSQL migration v31 is the paired schema.

SQLite v54 adds `retrieval_experiences` (Agentic Memory P2): the
deployment-GLOBAL retrieval-strategy experience library. One entry says "in
this shape of question, this retrieval action is / is not worth reaching for",
plus a short model-written rationale, a `support` count of the runs backing it
and an `adopted` count of the times the model actually picked that action after
the entry was injected. The table deliberately carries no `notebook_id`, no
owner column and no foreign key in either direction: it stores general tactics
for HOW to search, never anyone's content, so notebook deep copy cannot reach
it (the same structural sentence that covers `groups`/`group_members`) and
`scripts/merge_dbs.py` classifies it as a global union table. Its primary key
is a single CONTENT-ADDRESSED `TEXT` column — the deterministic hash of
(situation fingerprint, action) — which is what makes that union safe across
independent deployments (an incrementing id would silently drop rows on a
primary-key collision) and, because the declared replication key equals it
verbatim, also what parks its one unique surface on `REPLICATION_KEY` with no
sentinel column and no `_UNIQUE_PREDICATES` entry. It creates no index: the row
count is hard-capped, and the only two read paths are a primary-key point
lookup and a bounded-return, unbounded-scan read (index deferred to the next schema hop). Because v54/v32 adds one more (leaf, parentless)
table, the forward-shadow invariants move to 81 business tables and 110 unique
surfaces; the branch-counted bound remains exactly 12 row slots. PostgreSQL
migration v32 is the paired schema, and the current pairing is
SQLite 54 / PostgreSQL 32 / epoch 1.

SQLite v55 adds two things in one migration (Agentic Memory P3): the leaf
table `agent_observations` (one outgoing FK to `notebooks`, no incoming FK —
an external Agent's per-`(notebook, owner)` observation log, ring-bounded and
consumed only by the untrusted overlay-consolidation prompt) and a nullable
`user_profiles.search_profile_json` column (the per-user search/answer style
preference document; NULL means "never set", same contract as `ui_mode`).
`agent_observations`'s idempotency unique index,
`idx_agent_observations_request` on
`(notebook_id, owner_id, agent_profile_id, client_request_id)
WHERE client_request_id IS NOT NULL`, parks on NULL the same way
`idx_conversations_share_token` does; a second, non-unique index,
`idx_agent_observations_scope`, supports the ring-eviction delete and the
bounded reads but adds nothing to the unique surface count.
`user_profiles.search_profile_json` adds no unique surface, FK, or JSON
column registration (same treatment as `ui_mode`). Because v55/v33 adds one
more leaf table with only an outgoing FK, the forward-shadow invariants move
to 82 business tables and 112 unique surfaces (the new table's declared PK
plus its one partial index); the branch-counted bound remains exactly 12 row
slots. PostgreSQL migration v33 is the paired schema, and the current
pairing is SQLite 55 / PostgreSQL 33 / epoch 1.

SQLite v56 / PostgreSQL v34 adds the live `groups.owner_id` pointer. Existing
groups choose a current admin deterministically (preferring `created_by` only
when that creator is still an admin); new groups write creator and owner together. Owner
transfer promotes the target member to admin and keeps the former owner as admin
inside the same group-root transaction. The owner member row cannot be demoted,
removed, or used for self-leave until transfer completes. This adds no table,
index, foreign key, or unique surface, so the forward-shadow invariants remain
82 business tables, 112 unique surfaces, and 12 row slots. The current pairing
is SQLite 56 / PostgreSQL 34 / epoch 1.

SQLite v57 / PostgreSQL v35 adds the group's reusable invitation capability on
the aggregate root: nullable `invite_token`, `invite_created_at`, and
`invite_created_by`, plus partial unique index `idx_groups_invite_token WHERE
invite_token IS NOT NULL`. Keeping the token on `groups` lets an authorized
administrator reopen and copy the same live link; rotating or revoking clears
the previous authority atomically, and deleting the group removes it with the
root row. The timestamp is SQL NULL or an ISO instant, never an empty string.
No table or foreign key is added; the forward-shadow invariants are 82 business
tables, 113 unique surfaces, and 12 row slots. The current pairing is SQLite 57
/ PostgreSQL 35 / epoch 1.

SQLite v58 / PostgreSQL v36 adds the pluggable-indexing-pipeline columns:
`notebooks.indexing_pipeline` is the nullable desired selection (`NULL` means
builtin), while `indexing_pipeline_version`, `indexing_pipeline_generation`,
and `indexing_pipeline_job_id` are non-null authority columns. The published
product identity pair `(indexing_pipeline_id, indexing_pipeline_version)` is
also added to `unified_kg_state` and `extraction_runs`. This migration adds no
table, foreign key, or unique surface.

SQLite v59 / PostgreSQL v37 adds durable `indexing_pipeline_stages` and
`indexing_pipeline_stage_sources`. Both are `TableClass.LOCAL_EPHEMERAL`: they
exist in both schemas and count toward schema totality, but forward-shadow
copy/capture/apply never transfers a backend-local worker's lease state. The
current pairing is SQLite 59 / PostgreSQL 37 / epoch 1, with 84 application
tables (82 replicated plus 2 local staging tables), 113 replicated unique
surfaces, and a 12-row-slot branch-counted foreign-key bound.

Run it only while application/background writers are stopped:

```bash
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-facts \
  --notebook-id nb-... [--force] [--confirm-service-stopped]
PYTHONPATH=backend python scripts/audit_source_facts.py \
  --db .local/silicon_notebook.db --notebook nb-...
```

Use `--all-notebooks` instead of `--notebook-id` for a whole deployment.
`--confirm-service-stopped` is required for PostgreSQL and only records the
operator assertion; it does not stop services. The PostgreSQL audit alternative
is `--database-url`, and both audit paths are transaction/read-only. The audit
exits nonzero while any visible source is missing, running, failed,
incomplete, or fails an integrity reconciliation.

PostgreSQL migration v30 is the current paired
business schema. The temporary
shadow boundary now includes a SELECT-only UTF8-first preflight, redacted
identity-bound confirmation, an owned/checksummed removable PostgreSQL control
schema, revision CAS, and two independently committed reports for the four
logical-key guards across the exact 81-table epoch-1 manifest. It also includes
run-bound atomic SQLite snapshots and bounded resumable baseline COPY: each
batch commits with its prefix checkpoint, resume proves that exact target
prefix without truncating or deleting business rows, seven historical rowids
copy as explicit ordinals and their catalog-resolved identity sequences reseed,
and the final forward checkpoint advances atomically to snapshot H0 after the
v30 ledger, FK, guard, and ANALYZE checks. Snapshot publication requires an
owner-only real directory and exclusive 0600 temporary creation. COPY fully
qualifies business SQL to the run-bound schema, revalidates enabled live SQLite
capture under a short `BEGIN IMMEDIATE` at every critical binding, uses a fresh
dedicated connection to the currently named SQLite file rather than the
repository thread cache, and binds/rechecks its resolved path and device/inode
across open and immediately before publication/PG commit. JSONB prefix proof
normalizes only JSON numeric leaves to exact finite decimal semantics; ordinary
SQL numeric columns remain type-distinct. It uses bounded
named server cursors plus statement timeouts/cancellation polls, and performs
full initial/final migration-derived validation of v32 tables, columns,
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
the fixed v32 graph has a branch-counted bound of exactly 12 row slots and no
suffix-log evidence scan is used. Savepoints defer only FK/UNIQUE ordering
SQLSTATEs; CHECK/NOT NULL poison immediately. Exact PG32 catalog plans cover all
110 unique surfaces using NULL; deterministic candidates scoped by indexable
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
publication bind snapshot source/target plus the live target identity.

The verifier opens a SQLite read snapshot at `Hv`, streams normalized facts to
an owner-private disposable spool, releases SQLite before waiting for the PG
checkpoint, then pins a PostgreSQL `REPEATABLE READ, READ ONLY` snapshot at
`Ht`. A second SQLite transaction scans every retained dirty key in
`(Hv, Hseen]`; only those keys are excluded from strict comparison, and the PG
retention barrier remains live until the report commits. Structural checks
cover the exact catalog, stable key sets and normalized hashes, source/target
foreign keys, cascade/unique semantics, and storage-root-confined file
references. Full checks add selected domain projections, float32
byte/dimension/norm and sampled-cosine invariants, plus the fixed mixed
Chinese/English retrieval set with recall@12 loss at most one percentage
point, top-10 overlap at least 0.90, and exact citation/source-id sets. Cutover
additionally rechecks that SQLite is still write-frozen, requires
`Hv=Ht=MAX(seq)`, zero concurrent keys, 100% coverage, and a preceding complete
full/cutover report. Persistent reports contain only safe table names, hashed
stable keys, categories, counts, and fixed summaries; a clean report
supersedes drift only at the same or a stronger verification level.

The explicit operator CLI now owns preflight/start-forward/status/verify and
the foreground worker lifecycle. The worker holds one database-clock lease,
finishes its current atomic batch on SIGTERM/INT, and performs conservative
retention only behind FULL verification, verifier/replay/poison barriers, seven
days, and 100,000 tail events. Every valid batch outcome emits exactly one
redacted metric; batch events use the actual accepted/observed raw-event count
rather than lag, and retries are retained whenever observable.
`SHADOW_DATABASE_URL` remains inert by itself and is read only by that CLI.
Cutover, reverse replication, and automatic active-URL changes are not part of
this phase. Safety-critical PG
control mutations always take the migration lock, then the control lock, then
validate the exact live control catalog. A live SQLite transition acquires the
PG pool, both locks, and the run row before its short `BEGIN IMMEDIATE`, so it
never waits for a PG pool or advisory lock while holding SQLite.
- `frontend/app/page.tsx` is the notebook-workspace orchestrator, not the owner of every shared view model or panel. API/view types and constants live in `workspace-model.ts`, the answer/citation/reasoning-trace surface lives in `answer-panel.tsx`, built-in KG labels/styles live in `kg-type-model.ts`, and graph/answer rendering shares `kg-type-mark.tsx`. `use-source-library.ts` now exclusively owns source rows/scope, paging, detail elements, reparse/delete state, tombstones, and parse polling. The shell commits the existing paired notebook/source opening snapshot and consumes only readonly state, named commands, and narrow refresh effects; the hook never receives another workspace domain's setters.
- `frontend/app/notebook-transition.ts` is the single transition that orchestrates opening a notebook (pure logic — no React, DOM, or network). Every step's `begin` runs in declaration order and refusal is decided only afterwards (short-circuiting would leave later owners un-begun); then `enter` → `load` → `isCurrent` → `apply` → each step's optional `commit` in declaration order → `conclude`; then every successfully begun step is settled exactly once, in **reverse** begin order, with the committed outcome or `null` for a rollback. Refusal, a failed parallel read, supersession at either guard, and a thrown error converge on that one rollback path (the error is rethrown verbatim), and a superseded transition settles only the tickets it minted itself. `page.tsx::notebookTransitionSteps` is the single registration point for every owner's `begin`/optional `commit`/`settle`, so adding an owner is one list entry; the root-modal step must stay first because its close sink is the only cleanup path for staged intake. `openNotebook` keeps only its prologue and the plan declaration, with the four phases in named helpers. Request counts, workspace-epoch semantics, late-response rejection, tombstones, history handling, and the failure landing spot are unchanged. Gates: `frontend/tests/guards/notebook-transition-guard.test.mjs` and `frontend/tests/unit/notebook-transition.test.mjs`.
- `frontend/app/use-ask-session.ts` exclusively owns the Ask draft/transcript, intent preview/confirmation, durable stream and reconnect, conversation history/tombstones, session mutations, and answer feedback. Notebook opening keeps the existing paired notebook/source read in the shell, then performs exactly one conversation-list read and at most one latest-detail read through an explicit hook command. Navigation detaches durable work without cancellation; explicit Stop waits for `started` when necessary, cancels exactly once, then aborts the local transport. The synchronous cancellation endpoint has no enforceable whole-request database deadline, so the browser keeps one authoritative cancellation request in flight until the server answers instead of releasing retry authority on a client timer. Intent preview and execution share one frozen source/base scope. The hook admits visible state by exact actor/notebook/workspace owner, reconciles durable history by actor/notebook identity, exposes no raw setters, and does not own the shell's Memory answer-link batching or cross-domain callbacks.
- `frontend/app/use-report-workspace.ts` exclusively owns report list/detail state, lazy entry loading, mutually exclusive list/detail polling, intent and outline submissions, generation/cancel/retry, sharing, export selection, and deletion tombstones. Notebook opening and non-report tabs perform zero report I/O; entering Reports performs one list read, and a pending-center focus reads one detail after the list settles. Navigation detaches background work without cancellation. Exact actor/notebook/view owners reject stale visible commits, while successful deletes always record an actor+notebook tombstone for A→B→A convergence. Creation freezes source/base scopes and every write command rechecks live manage authority. The hook exposes readonly views and named commands, imports only report/pure contracts, and preserves existing request counts and the six-second poll cadence.
- `frontend/app/use-kg-workspace.ts` is a thin composition layer over three independently testable domain owners — `use-kg-knowledge.ts` (Knowledge rows/types/filter/page/duplicates/context), `use-kg-schema.ts` (Schema views and mutations) and `use-kg-graph.ts` (unified-graph search/range/node state, merge review/tombstones, and durable KG build/relink/rebuild tracking) — over the single actor/notebook/generation gate in `use-kg-owner.ts`; together they exclusively own all of it. The three domains never import each other, and the gate holds no domain state and imports no API module: clear-on-owner-change, invalidate and adopt-new-owner are the composition layer routing the gate's fan-out into each domain's own named command, never a setter handed between domains. `page.tsx` still sees one namespaced view (`.knowledge` / `.schema` / `.graph`) plus the flat command surface. Notebook opening keeps Knowledge, Schema, and graph content lazy and performs only the pre-existing maintenance-status recovery probes. Exact actor/notebook/generation owners reject stale visible commits; actor+notebook identity preserves durable maintenance claims and merge-decision tombstones across A→B→A. Every write command rechecks live policy, readers perform no merge-review recovery/write I/O, and all graph/review/build/maintenance polls are single-flight at the existing cadences. The hook exposes readonly views and named commands, imports only Knowledge/KG/pure contracts, and preserves existing request counts.
- `frontend/app/use-notebook-collection.ts` exclusively owns actor-scoped notebook rows, bounded collection search, filter/sort/view/menu state, issued/published list watermarks, access reconciliation, editor/delete state, default creation single-flight, and notebook deletion tombstones. The shell keeps the existing model-status + health + notebook-list + system-configuration composite bundle and commits its list through an opaque hook ticket only after the sidecars settle; notebook opening still adds no collection read. Search retains the 250 ms gate and the module-wide four-request server-work ceiling. Actor replacement synchronously hides the prior rows and dialogs, staged writes recheck live row authority, and a successful delete records its actor tombstone before derived refresh so A→B→A and pre-delete list responses cannot resurrect it. The hook exposes readonly views, named commands, and narrow shell effects.
- `frontend/app/use-root-modal-coordinator.ts` exclusively owns root-dialog presentation leases, typed slot conflicts/layers, actor/workspace/source generations, topmost arbitration, and safe focus return. It imports React only and owns no domain payload, permission, busy state, API, repository, timeout, interval, or poll. Domain code issues a frozen lease before an asynchronous opener and publishes it only if the exact owner and issue remain current; workspace transitions and actor replacement synchronously hide old slots, while legal info overlays remain independent of the primary-dialog conflict group. The coordinator adds no request, timer, or mount-time read.
- `frontend/features/extension-sdk` is the sole build-time workspace UI registry/host. Only `workspace.side_panel` and `source.detail_section` are canonical. The first production entry is the existing Agent Profile launcher: it renders as a compact entry row inside the sources panel's fixed area (above the scrolling source list) and never adds a dedicated workspace column; its styling reuses existing button classes and `:root` tokens with no colour literals. The plugin performs no profile I/O until click and delegates an exact-owner `openUnderstanding` action to the existing root modal/data owner. A committed workspace loads `/system/extensions` once per actor generation; collection, signed-out, and empty-registry paths short-circuit before requests/controllers/timers, while same-actor notebook transitions reuse the projection and synchronously invalidate old visible entries/actions. Rendering requires exact static tuple, live availability, core `workspaceCapabilities`, normalized UI mode, and the current actor/notebook/workspace owner. The projection remains metadata-only and closed; props remain readonly summaries plus reviewed narrow actions. Parity tests remain non-vacuous. After adding a contribution, run `scripts/generate_ui_extension_contract.py` to refresh `backend/tests/fixtures/ui_extension_contract.json`; the contracts lane reconciles it with `--check`.
- Deployment-time UI plugin packages load through three registry modules under `frontend/features/extension-sdk`: `registry.ts` (the built-in catalog; its whole import closure stays `.ts`, because `node --test` reports `Unknown file extension` for `.tsx` and two node-lane test files import this module directly), `workspace-registry.ts` (merges built-in with local through `defineWorkspaceUiRegistry`, so a local entry gets no exemption from the built-in's id/shape/dedup validation), and the generated, gitignored `registry.local.ts` (an empty-array stub when no plugin is configured). `SILICON_NOTEBOOK_UI_PLUGINS` — a `:`-separated list of local package directories — and `npm run sync:ui-plugins`, wired into `postinstall` plus the five `pre*` hooks (`predev`/`prebuild`/`prestart`/`pretest`/`prelint`), drive `frontend/scripts/sync-ui-plugins.mjs`: it copies each validated package into `frontend/features/ext-<name>/` (also gitignored, marked with a `.ui-plugin-origin` file so a later sync can safely remove it) and regenerates `registry.local.ts`. A plugin package is a flat directory (no subdirectories, dotfiles skipped) whose name matches `^[a-z][a-z0-9-]*$` and does not start with `ext-`; it contains only `.ts`/`.tsx` files plus exactly one `ui-plugin.json` manifest and exactly one `workspace-plugin.ts`/`.tsx` entry file naming the exported component the manifest's `component` field points at. A `package.json` or `node_modules` directory is rejected outright — the base `tsconfig.json`'s `exclude: ["node_modules"]` only covers `frontend/node_modules`, so a plugin's own dependency tree would otherwise be swept whole into `next build`'s type-check, and it would introduce a second React instance — and so is any CSS file, since a plugin's visuals must reuse existing classes and `:root` tokens. Plugin source may import only `../extension-sdk/contracts.ts`, `../extension-sdk/ui.tsx`, bare `react`, bare `lucide-react`, and same-package sibling `./x.ts(x)` files — never `../extension-sdk/api.ts`: the host injects a port scoped to the contribution's own `pluginId` via `actions.api`, and a plugin able to import `api.ts` directly could construct another plugin's port. Copied plugin code is swept by the same repository-wide guards that scan `app`/`features`, including `scripts/check_ui_vocabulary.py` and the nine `appSourceModules()`-based guards — most notably `errors-guard.test.mjs`, an exact-count sweep whose `APPROVED_*` allowlists a package outside this repository cannot register into, so a plugin must never read `error.message`/`.error`/`.error_message` or `throw new Error("...")` with Chinese text; it calls `api.userMessage(error, fallback)` for user-facing text instead. A companion guard — a semantic AST scan covering *every* `.ts`/`.tsx` in a plugin package, not just its entry — forbids side channels inside sibling plugin modules: `setTimeout`/`setInterval`, dynamic `import(`, `WebSocket`, `EventSource`, `XMLHttpRequest` and `navigator.sendBeacon`. Direct `fetch(` belongs to the repository-wide `api-boundary` guard instead, which already sweeps every module; spelling it in both places would only mean that loosening one of them goes unnoticed while the other still blocks. The injected `api` port remains the only sanctioned I/O surface, and its three request methods are `async`, so a confined-path violation arrives as a **rejection** rather than a synchronous throw — both `await` and `.catch()` catch it. A plugin that opens a dialog must use `ExtensionModal`, passing its whole `context` and `actions` through: the window-position memory key is `extension.<pluginId>.<storageKey>.window`, and both leading segments are composed by the SDK and are not the plugin's to change — the `extension.` prefix separates plugins from core dialogs, and the `pluginId` segment separates plugins from each other (without it, two packages each choosing `storageKey="search"` would share one memory slot and overwrite each other's position). Plugin dialogs go through the core root-dialog coordinator via one **generic** `"extension"` slot: core's slot union names no plugin (that would mean patching the public tree per install), so the claim is recorded by the shell as a **contribution id** and the host projects `context.dialog` — `{ open, topmost, zIndex }` — only onto the contribution that holds it. A plugin therefore keeps no `open` state of its own: `actions.openDialog()` asks, `context.dialog.open` answers, `actions.closeDialog()` releases, and core closes the dialog on a conflicting primary, a notebook switch, or sign-out. One plugin dialog is visible at a time (two contributions of the same plugin do not both open), a covered dialog becomes `inert`/`aria-hidden` with focus return owned by the coordinator, and `closeDialog` is refused for anything that is not the current holder so a stale callback cannot close someone else's dialog. A different contribution calling `openDialog()` closes the current holder's dialog first and re-registers the slot to itself (codex #578 R7 P2), so the focus-return target moves to the new holder's own trigger instead of staying on the previous holder's. `source.detail_section` contributions cannot open dialogs at all — the host refuses this structurally, not by convention (codex #578 R4 P2): in that slot `openDialog`/`closeDialog` are refusal functions that never reach the shell (a dev-mode `console.warn` fires), `context.dialog` is unconditionally the closed view regardless of what the shell reports as the holder, and `ExtensionModal` throws a `TypeError` if a plugin renders it there anyway — that slot's host is itself a `FloatingModalCard` (so `position: fixed` would resolve against it) and holds the conflicting `source-detail` primary lease. Gates: `frontend/tests/component/extension-plugin-surface.component.test.tsx`, `tests/component/use-root-modal-coordinator.component.test.tsx`, and the wiring assertions in `tests/guards/extension-ui-boundary.test.mjs`. `actions` and `context` are fresh objects on every render (the owner gate is re-frozen each pass) and must never sit inside a `useEffect`/`useMemo` dependency array, while `actions.api` is memoized per `pluginId` and is safe there; `refreshSources()` resolves silently once the owner gate has since closed and otherwise rejects on a genuine load failure, so a plugin must call it at most once, after its own action completes, and must `catch` the rejection rather than let it go unhandled. A checked-out tree with private plugins configured does not pass the base repository's `npm run test`: `extension-ui-host.component.test.tsx` pins "the merged registry equals the built-in catalog, length 1, with zero plugins configured" as the one property the registry split exists to prove, and it must never be loosened to `>= 1` to accommodate a local plugin. A private deployment's acceptance gate is instead reconciling `frontend/.local/ui-extension-contract.json` against the backend `GET /api/system/extensions` projection, plus a clean `npm run build` and `npm run lint` (both type-check `features/ext-*/`; the build's pass silently skips `*.test.*`/`*.spec.*`-named files, `npm run lint` does not). The end-to-end plugin procedure lives in [`docs/deployment-extensions-sop.md`](deployment-extensions-sop.md).
- Source detail participates in the same frozen primary issue watermark. Its source catalog review is the only compatible primary overlay; every covered root dialog, including source detail and KG analysis, is inert and ARIA-hidden until it becomes topmost again. Focus return runs without a timer in the post-commit layout phase and rechecks both the expected underlying lease and absence of an inert ancestor.
- Workspace HTTP ownership is split into domain modules. The shared `frontend/app/api-client.ts` transport owns HTTP mechanics; domain modules retain endpoint policy. Source reads and mutations are coordinated by the source-library owner, whose exact user/notebook/workspace generation rejects late UI commits while allowing already-started writes to finish safely. The initial notebook open still performs one paired notebook + first-source-page read, unchanged polling keeps the same point-read cadence, and no global state library was added. `frontend/tests/guards/api-boundary.test.mjs` semantically forbids production `fetch` outside the transport core.
- Group management is the collection-level `frontend/app/groups-page.tsx` workspace; its `.group-page-*` shell reuses the collection page's tokens, controls, typography, spacing, and responsive breakpoints. Five page-level invariants hold it there. (0) **The page title lives in the top bar, and the page itself no longer has one.** "群组" used to sit at 28px below 44px of blank space while the top bar simultaneously read "群组工作台" — the same sentence twice with an empty band between them. It is now the top bar's `.brand-title` slot next to the SN mark (unchanged 16px, replacing `silicon-notebook` on this view), with the subtitle changed to `成员、共享与审批`. On this view that slot must actually render as an `<h1>`, not a heading-shaped `div`: the page has no other heading left, so the whole heading level would vanish. `.brand-title` therefore needs `margin: 0` — a browser's default `0.67em` h1 margin deforms the 56px bar (measured: 56px in both the h1 and div forms). The page's top padding also drops from `.page`'s 44px to 24px, because those 44px buy breathing room for an opening 28px title and the first row is now a 42px toolbar. (0′) **"返回主页" is one control with one style.** The workspace and the group page both call `showCollection()` and both return to the notebook list, so they share `.back-home-button` (formerly `.notebook-home`, declarations byte-identical); the group page used to grow its own grey text link, so one action looked like two different things. Renaming the class means changing both call sites (`page.tsx` and `groups-page.tsx`). (1) **The 1200px measure is applied to a padding-free box.** The cap lives on `.group-page > *`, mirroring `.collection-title` / `.notebook-grid`, which are children of `.page` and carry no padding of their own. It must not move onto `.group-page`: that element inherits `.page`'s `padding: 44px 24px`, and the global `* { box-sizing: border-box }` then eats those 48px out of the cap, leaving 1152px of content — visibly 24px narrower per side than the sibling page at viewports of 1248px and up. Each child also needs an explicit `width: 100%`, because auto inline margins switch off a grid item's stretch and a bare `max-width` collapses it to fit-content. (2) `.group-page` itself needs `align-content: start`: it is stretched to the full viewport height by `.app`'s `1fr` row, and auto grid rows stretch by default, which inflates the header of short tabs (members / settings) by tens of pixels. (3) **No class may be attached that has no rule in `globals.css`.** The page previously carried seven decorative `<span className="eyebrow">` labels for which no `.eyebrow` rule has ever existed, so they rendered at the inherited 16px body size above every heading; `tsc` does not check className strings and testing-library only reads text, so nothing failed. Neither of these three fails loudly — they just render wrong. The notebook share dialog remains `frontend/app/notebook-group-share.tsx` and uses compact `.group-*` rows. In those rows horizontal layout belongs to `.group-row`, never an inline style; read-only labels use `.group-chip`, not the 42px primary `.new-pill`. `frontend/tests/guards/group-layout-guard.test.mjs` remains the compact-row regression gate, `frontend/tests/guards/group-page-style-guard.test.mjs` gates the three page-level invariants above (it walks the whole AST for every static `className` token — deliberately not a per-tag list, which lets a new tag slip through — and requires each one to exist as a class selector in `globals.css`, then pins the `.group-page > *` declarations, `.group-page`'s `align-content: start`, the absence of `max-width` on `.group-page` itself, that both call sites use the shared `.back-home-button` and that `.group-page-back` never comes back, and that groups-page.tsx has no `<h1>` while the top-bar slot is one), while `groups-page.component.test.tsx` covers the independent workspace. Neither gate checks spacing or colour values — those are design choices, not invariants.
- The reader/group-shared notebook's workspace header identity row (`ReaderNotebookBadge`) is laid out by `globals.css`'s `.reader-badge-row` and **never wraps**. It used to use `.tag-row` (`flex-wrap: wrap`) while `.workspace-header` is a fixed 72px single row, so title + badge + a long explanation wrapped onto three lines centred inside 72px and pushed the title line *above* the visible area (measured: 141px of content in a 72px box) — opening a group-shared notebook showed none of its name, and the explanation bled out over the content below. The title uses `.reader-badge-title` (`width:auto` undoes `.notebook-title-input`'s `width:100%`, plus `min-width:0` and an ellipsis, so the title is what compresses, not the badge); the identity marker is a **status**, not a primary action, so it uses the light `.reader-badge-chip` rather than the 42px solid `.new-pill`, which next to a 26px title outshouts the name itself. The explanation lives only in a tooltip — especially the "how to stop access" guidance, which group sharing has no self-service path for anyway. Gates: `frontend/tests/guards/reader-badge-layout-guard.test.mjs` (the no-wrap/ellipsis contract in CSS, since jsdom has no layout engine) and `frontend/tests/component/notebook-reader-actions.component.test.tsx` (the structural premises).
- **Press feedback is an element-level baseline, never patched class by class.** `globals.css`'s `button:not(:disabled):not([aria-disabled="true"]):active` gives every button the same pressed state (`translate: 0 1px` plus `scale: 0.98` plus `opacity: .8`), and `:active` restores it on release. The defect that produced this rule was the invite-link 「复制」 button on the group members page: the button did not move at all, and its only feedback was the notice banner at the top of the page, which scrolls out of view — so the copy read as having done nothing. The selector is deliberately pinned to the element rather than to classes: the frontend has more than forty button classes (`.new-pill`, `.sort-button`, `.icon-button`, `.index-cta`, `.kh-*-button`, …), per-class styling reliably misses one, and the one it misses is the next instance of this same defect; narrowing it to `.new-pill:active` silently drops the guarantee for every button written afterwards. The offset is written with the individual `translate` / `scale` properties rather than the `transform` shorthand: a repository-wide baseline necessarily hits buttons that use `transform` to position themselves, and the shorthand replaces that positioning outright — `.answer-image-preview-step` (previous/next in the multi-image preview) centres itself with `top: 50%` plus `transform: translateY(-50%)`, so the 44px absolutely positioned control jumped about 22px downward the moment it was pressed, and pressing near its upper edge moved it out from under the pointer and cancelled the click outright (codex #612 R1 P2). The individual properties compose in the order translate → rotate → scale → transform, leaving each button's own transform intact. It shifts and fades rather than changing background colour, because a background has to be tuned per class and `.new-pill` is near-black (#050505), where darkening is invisible; offset and opacity hold for light and dark fills alike and for both a 42px pill and a whole card button. It deliberately declares no `transition`: the individual button classes carry their own `transition` declarations, which would override it wholesale and leave the animation working on only some buttons. `:disabled` is excluded — press feedback on a disabled control claims it is live. Press feedback only answers "did the click register"; the action's *outcome* must land on the control itself or right beside it: label 复制 → 已复制 / 复制失败 and colour `button.copy-result-copied` / `button.copy-result-failed`. Those are written on `button.` rather than per shell, because (0,1,1) already outranks the (0,1,0) fills of `.new-pill`, `.sort-button`, and `.report-action` regardless of source order; each also needs a `:hover` variant, since `.report-action:hover:not(:disabled)` is (0,3,0) and the pointer is necessarily still on the button for the second after the click, so without it the hover state eats the result colour. Call sites that have a read-only link input next to them also focus+select it on failure, so ⌘C works immediately. Because that outcome is JS state and does not restore itself the way `:active` does, it must clear itself on a timer; otherwise 「已复制」 stays pinned to the button and the next click shows no change at all. That timer exists once, in `frontend/app/copy-result.ts`'s `useCopyResult`, shared by all five call sites (group invite link, the notebook share dialog's link, each row of the 「已分享」 dialog, the report share link, and the report body). It is deliberately a hook and not a component: the result classes must stay literal in each JSX site, because wrapping them in a component or interpolating them into a template string leaves the className collectors seeing only a variable, which silently empties both group-page-style-guard and button-press-feedback-guard — and the three shells and their busy labels differ anyway. Its `key` must be the identity of the thing being copied (invite token, share token, report id) rather than a slot name, for two reasons. Lists need separating: the 「已分享」 dialog renders one copy button per row, and a single unkeyed state would light the whole column green. And the result is held for 1.6s, during which the content behind the button can change — switching groups, regenerating the invite, moving between reports — so a fixed key makes the new link appear already wearing the previous link's 「已复制」 even though it was never copied (codex #612 R2 P2); keying by token or id falls back to idle automatically. What is deliberately *not* done is clearing the result at the start of each attempt: making that clear actually paint needs an empty frame, and React coalesces the two setState calls either side of the await into one render, so it would take an animation to see reliably — while "did this click register" is already answered by `:active`, and the result state answers a different question ("is this link in the clipboard now"), which stays true on a repeat click. Long-running actions keep their existing constraint of disabling or replacing the control while in flight (`long-task-button-guard` / `command-catalog-button-guard`). Gates: `frontend/tests/guards/button-press-feedback-guard.test.mjs` (exactly one element-level selector, `:disabled` excluded, at least two visual properties, no `transform` shorthand; a table keyed on onClick source text pins the result classes of all five copy entry points; the result colours must declare `background` and carry a `:hover` variant; the reset timer exists only in copy-result.ts and every call-site module imports it), plus `groups-page.component.test.tsx` and `copy-result-feedback.component.test.tsx` (real clicks: success → 「已复制」 then automatic restore, failure → 「复制失败」, no result flash when the owner returns null, and the result landing only on the button that was pressed).
- An access-rights change must reconcile the **open workspace**, not just the list. The independent group page can remove membership, a group, or a grant and thereby pull the notebook the user had open out from under them. `use-notebook-collection.ts::refreshAfterAccessChange` owns the one-list read and its issued/published watermarks, then invokes the shell's narrow `reconcileOpenNotebook(remaining)` effect; read-only-share leave and the group page's `onChanged` use that same command. A revocation made elsewhere has no push channel and is re-checked when the tab becomes visible again (throttled; a failed fetch skips reconciliation) — best effort, not a guarantee. Gate: `frontend/tests/guards/group-sharing-guard.test.mjs`.
- Boundary regression tests use public HTTP contracts or explicit domain seams, never private aggregate helpers, source positions, line counts, or total route/model counts. FastAPI lifespan/application lifecycle composition remains separate debt.

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

G1 runs three bounded lanes concurrently: `check_backend.sh` executes the stable backend pytest suite with default 12 backend pytest workers (override with `BACKEND_PYTEST_WORKERS`); `check_contracts.sh` executes syntax/dependency preflight, hermetic smoke paths, contract checks, and the deterministic extraction-scoring harness; `check_frontend.sh` executes every recursively discovered `*.test.mjs`, every `*.component.test.tsx`, the production frontend build, and the package typecheck. Node's test runner and Vitest are each capped at four workers, leaving CPU headroom for the backend critical path. The Next build must keep `ignoreBuildErrors` unset and stays the fail-closed typecheck for production code, but Next's build-time type checker silently filters out every diagnostic reported in `*.test.*`/`*.spec.*` files and `__tests__`/`__mocks__` directories (the `ignoreRegex` in `next/dist/lib/typescript/runTypeCheck.js`, verified on Next 15.5), so a type error that only exists under `frontend/tests/**` never fails the build. The frontend lane therefore runs `npm run lint` (`tsc --noEmit`) after the build — after, so it type-checks the freshly regenerated `.next/types` rather than a stale tree — as the one pass that sees those files; with `incremental` the warm re-check costs under a second (~5s cold), so the duplicate parse the lane once avoided is no longer a budget concern. Its backend lane excludes `slow` real-index/performance tests, `graph_index_contract` cold graph/index contracts, `architecture_contract_heavy` (the 8 of the 64 `architecture_contract` repository-wide semantic scans that cost more than 2s per test; the other 56 run in G1), and the PostgreSQL tree. G2 first runs G1 and then the exact complementary backend marker set — `backend/tests/test_test_architecture_policy.py::test_verification_lane_markers_partition_every_architecture_contract_test` proves that split empirically via `--collect-only`, not just by pinning the two `-m` strings. Each lane has its own process group, so interrupting or terminating the controller also terminates and reaps pytest, npm, and Next.js descendants. The official-client MCP smoke pins exactly the 23 published tools: seven Memory/context, four knowhow, one citation point-read, six source-management, three build, and two notebook-understanding tools. Missing `frontend/node_modules` is a hard failure rather than a silent skip.

Use the project’s Homebrew/Miniconda interpreter for acceptance:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

The Apple Silicon warm gate hard target is at most 60 seconds. CI lane timings are observational only, so this measured local target is not a portable timeout assertion for every CI host.

Keep test-speed changes result-preserving. The G1 standard and G2 extended marker expressions are exact complements, while PostgreSQL stays independently authoritative; never make a committed test unreachable. Ordinary unit and standard-gate tests are hermetic: they do not bind host ports or depend on ambient services; self-contained subprocess/signal coverage is reserved for contracts that are intrinsically process-level. Cache repository-wide AST/protocol parsing once per pytest process; test cache/container policy through the policy object instead of constructing unrelated database and ANN artifacts; and derive autouse isolation paths from the worker's existing pytest base temp rather than allocating a new `tmp_path` directory for every pure test. Ordinary SQLite repository tests copy a current empty schema built once per pytest worker, but every test keeps an independent mutable database file; migration, upgrade, and repository-snapshot modules stay on the real migration ladder through `_REAL_SQLITE_MIGRATION_MODULES`. Repository-heavy tests may reduce only the default password-hash cost in the pytest autouse fixture: authentication helpers retain the production default, and credential-field snapshot modules remain in `_REAL_PASSWORD_HASH_MODULES`. Concurrency tests use events/barriers for ordering and fairness assertions rather than fixed sleeps or assumed thread wake-up order. When queued work runs in waves, a controller thread must release observed capacity with events instead of leaving a later wave alone in a cyclic barrier. Delayed process-global jobs must be cancelled and reaped in shared teardown before per-test repositories close; cleanup scoped to one repository object cannot contain route-owned work.

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

The committed OpenAPI contract is byte-semantically frozen, so
`backend/requirements.txt` pins FastAPI `0.135.3` and Pydantic `2.12.4`
exactly. Upgrade either framework only together with an intentional OpenAPI
contract regeneration and a clean-environment G2 extended-gate run.

The workflow is read-only, does not receive model or deployment secrets, and
uses four backend pytest workers to avoid oversubscribing the hosted runner.
Backend installation sets `HNSWLIB_NO_NATIVE=1` and disables pip's wheel cache:
`hnswlib` otherwise builds with `-march=native`, and a cached locally built
wheel can crash with `SIGILL` when restored on a hosted runner with different
CPU features. The portable build trades a small ANN speedup for deterministic
CI; production wheelhouses may still target their declared deployment CPU.
Its 20-minute timeout includes dependency installation and is intentionally
separate from the under-60-second local Apple Silicon warm-gate target.
`CI / level-1-standard` is initially observational; make it a required `master` check
only after stable green pull-request and post-merge runs have been observed
and the user explicitly approves the branch-protection change.

PostgreSQL coverage is deliberately separate from the offline gates. The
`level-3-postgres-integration` job starts PostgreSQL 16, provisions least-privilege and
auxiliary encoding/locale targets, and runs `bash scripts/check_postgres.sh` with
only the `postgres_integration` marker. Local verification uses an installed
PostgreSQL 16 service and an explicit `TEST_POSTGRES_URL`; `scripts/check.sh`
must never start or contact PostgreSQL.
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

For approved multi-step implementation plans, use subagent-driven development by default: assign each task to a fresh implementation subagent and require task-scoped specification and code-quality review before moving on. Research, design, status, and review-only work does not require a worktree or subagents.

`CLAUDE.md` is the Claude Code operating standard for this repository. Claude Code auto-loads only `CLAUDE.md` and `.claude/rules/`, so it keeps Claude-specific resident rules and routes product, architecture, deployment, operations, and development questions to the same canonical documents listed by `AGENTS.md`. Neither file is a duplicate product or architecture reference. Its hardest rule is that **spawning a subagent must state the model explicitly instead of inheriting the main agent's** — tiered by how much judgment the task needs: `opus` for judgment work (writing plans, review, architectural trade-offs, hard diagnoses), `sonnet` for transcription-shaped implementation whose spec is already pinned down, `haiku` for pure search and location. The PreToolUse gate `.claude/hooks/require-subagent-model.py` enforces it: a call that passes no `model` and whose `subagent_type` is not pinned to a model in `.claude/agents/` is denied. Three pinned roles ship in `.claude/agents/`: `impl-task` (sonnet), `spec-review` (opus), and `code-quality-review` (opus). `backend/tests/test_claude_subagent_model_hook.py` is the hook's regression net — it runs the real script over a subprocess boundary and covers both directions, the bypasses that would let an inherited-model call through and the false denials that would push people to work around the gate.

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

Update every canonical document whose owned surface actually changes; one change may affect product, deployment, operations, and development surfaces together. Maintain English/Chinese pairs together: `product-and-api`, `deployment-and-configuration`, `operations`, or `development`. Update the root README pair only when its quick start, high-level current boundaries, or navigation changes. Update `AGENTS.md` only for repository-wide agent workflow/routing rules and `CLAUDE.md` only for Claude Code-specific resident rules. Tests must validate each canonical owner rather than requiring detailed facts to be duplicated in entry files.

Because Claude Code auto-loads `CLAUDE.md`, `scripts/check_claude_md_budget.py` pins its total character count and longest line as exact baselines in the G1 contracts lane. Any size change must update those baselines in the same PR so no unaccounted headroom accumulates. Keep feature-level contracts in their canonical documents; change `CLAUDE.md` only when its Claude-specific resident rules or routing change.
