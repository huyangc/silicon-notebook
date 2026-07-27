# PostgreSQL Batch And Maintenance CLI Parity Implementation Plan

> **For agentic workers:** implement task-by-task with a fresh implementation subagent, then run specification and code-quality review before advancing.

**Goal:** Make the supported offline ingestion, KG, embedding, metadata, source-repair, and retrieval-index maintenance commands select SQLite or PostgreSQL through the central repository factory while preserving current resume, grounding, concurrency, and failure semantics.

**Architecture:** Keep orchestration backend-neutral. Replace direct `SQLiteRepository` construction in production maintenance CLIs with `create_repository(Settings())`; split the current oversized SQLite maintenance contract into a backend-neutral batch-maintenance port plus narrowly SQLite-only physical-format operations; implement PostgreSQL maintenance by delegating to existing PostgreSQL stores and shared services. Direct PostgreSQL mutation remains an offline boundary and requires an explicit stopped-service confirmation; online operation continues through the existing API/UI. `vectors-to-blob` remains SQLite-only because PostgreSQL vectors are already `bytea`.

**Tech stack:** Python 3.13, SQLite, PostgreSQL 16/psycopg 3, FastAPI repository facade, existing model-service scheduler, hnswlib/NumPy scale artifacts, pytest and the dedicated `postgres_integration` lane.

## Global constraints

- Do not add dialect checks or SQL to orchestration services. Backend selection exists only in the central factory; backend SQL stays in its adapter/store.
- Do not construct a `SQLiteRepository` after a PostgreSQL URL was selected.
- Preserve `batch_ingest` idempotency, partial-KG replacement, durable KG job single-flight, signal/drain, per-source failure isolation, and model-scheduler rules.
- PostgreSQL scans/backfills are bounded by keyset pages, server cursors, or existing bounded store APIs; never materialize a whole large notebook merely to drive a batch.
- Direct PostgreSQL mutation requires `--confirm-service-stopped`; it must also take a PostgreSQL advisory lock so two offline maintenance CLIs cannot overlap. Dry-run stays backend-independent. The live backend keeps using existing API/UI operations.
- Scale/viz artifacts remain under `SILICON_NOTEBOOK_STORAGE_DIR`; full rebuilds publish through the existing staged atomic swap.
- `vectors-to-blob` remains an explicit SQLite-only physical-format repair. PostgreSQL must reject it before mutation with actionable output.
- No new user-facing endpoint is required. If implementation adds one, it must ship with a frontend entry in the same change.
- Update `README.md`, `README_zh.md`, `AGENTS.md`, `CLAUDE.md`, the paired operations/development references, and `scripts/README.md`. Update `fangan_done.md` only after the feature is verified and only if the product spec owns the capability.

## Capability disposition

| Command | Result |
|---|---|
| `batch_ingest ingest` | SQLite + PostgreSQL |
| `batch_ingest kg` (`--retry-partial`, `--rebuild-only`, `--fresh`) | SQLite + PostgreSQL |
| `batch_ingest index` | SQLite + PostgreSQL |
| `batch_ingest all` | SQLite + PostgreSQL |
| `batch_ingest embed` | SQLite + PostgreSQL |
| `batch_ingest metadata` | SQLite + PostgreSQL |
| `batch_ingest reparse` | SQLite + PostgreSQL |
| `batch_ingest backfill-source-index` | SQLite + PostgreSQL |
| `batch_ingest vectors-to-blob` | SQLite-only, targeted PostgreSQL rejection |
| build/recluster/re-embed/re-extract/backfill production wrappers | SQLite + PostgreSQL via shared services |
| fixture generators, SQLite snapshot verifiers, SQLite benchmarks | intentionally unchanged |

### Task 1: Introduce backend-neutral CLI and maintenance contracts

**Files:**
- Modify: `backend/app/repositories/ports.py`
- Create: `backend/app/services/maintenance_cli.py`
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `backend/tests/test_batch_ingest.py`

- [x] Add the smallest backend-neutral `BatchMaintenancePort`; keep vector text→blob conversion on a separate SQLite-only port.
- [x] Add a context-managed CLI repository opener using `create_repository(Settings())`, with reliable PostgreSQL pool close.
- [x] Add backend/capability preflight: dry-run is neutral; PostgreSQL mutation requires `--confirm-service-stopped`; `vectors-to-blob` rejects PostgreSQL as not applicable.
- [x] Add an offline-maintenance advisory-lock seam owned by the PostgreSQL adapter and a no-op/SQLite equivalent that preserves current behavior.
- [x] Add tests proving PostgreSQL selection never constructs SQLite state and every error happens before mutation.

### Task 2: Implement PostgreSQL batch-maintenance adapter

**Files:**
- Modify: `backend/app/repositories/postgres/maintenance.py`
- Modify as needed: PostgreSQL source/knowledge/index stores
- Add: `backend/tests/postgres/test_batch_maintenance.py`

- [x] Implement owner/notebook resolution, notebook/source iteration, content-hash lookup, parsed/KG/partial-run status, extraction status, and counts by delegating to existing stores.
- [x] Implement missing chunk/element/node/relation embedding reads and idempotent upserts without holding database locks while waiting on model calls.
- [x] Implement `knowledge_object_sources` clear/rebuild/mark in bounded keyset batches.
- [x] Delegate scale-index existence/build and unified dirty marking to shared runtime services.
- [x] Preserve timestamp, JSON, vector `bytea`, status lifecycle, and transaction parity with SQLite.
- [x] Add SQLite/PostgreSQL conformance coverage for every method in `BatchMaintenancePort`.

### Task 3: Enable every supported `batch_ingest` phase

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `scripts/batch_ingest.py`
- Modify: `backend/tests/test_batch_ingest.py`
- Add: `backend/tests/postgres/test_batch_ingest_cli.py`

- [x] Replace the blanket PostgreSQL rejection with phase-specific capability checks.
- [x] Enable `index` and `metadata` first through existing backend-neutral facade calls.
- [x] Enable `embed` and `backfill-source-index` with bounded/idempotent PostgreSQL operations.
- [x] Enable `ingest` and `reparse`, retaining same-notebook content-hash dedup and per-source failure isolation.
- [x] Enable `kg`, including durable full-build single-flight, partial retry graph preservation, `--limit`, `--no-rebuild`, `--rebuild-only`, and `--fresh`.
- [x] Enable `all` only after its component phases pass on both backends.
- [x] Preserve exact signal behavior: only the durable full-notebook build installs drain-aware termination handlers.

### Task 4: Convert production maintenance wrappers

**Files:**
- Modify: `backend/app/scripts/build_kg.py`
- Modify: `backend/app/scripts/recluster_kg.py`
- Modify: `backend/app/scripts/reembed_kg.py`
- Modify: `backend/app/scripts/backfill_relation_embeddings.py`
- Modify: `scripts/build_chunks.py`
- Modify: `scripts/backfill_kg_embeddings.py`
- Modify: `scripts/reextract_notebook.py`
- Modify/generalize: `scripts/denoise_reextract_nb.py`
- Add/modify focused CLI tests

- [x] Replace direct repository construction with the shared CLI opener.
- [x] Reuse batch/shared services instead of duplicating orchestration or SQL.
- [x] Preserve legacy command names as thin compatibility entrypoints with actionable usage.
- [x] Remove hard-coded notebook ids from production utilities.
- [x] Add an architecture guard forbidding direct `SQLiteRepository` imports in production maintenance commands.

### Task 5: Verify migrated PostgreSQL execution and failure boundaries

**Files:**
- Add: PostgreSQL integration tests under `backend/tests/postgres/`
- Modify: existing repository/CLI contract tests as needed

- [x] Run the existing SQLite-to-PostgreSQL fixture-import lane, then exercise the portable batch phases through real-PostgreSQL CLI and adapter integration tests.
- [x] Assert no SQLite database is opened or modified on the PostgreSQL path.
- [x] Verify dedup, source/element/chunk/KG/reference/embedding counts and status lifecycle.
- [x] Verify unified rebuild clears dirty state and a built scale index can load its manifest and enabled ANN handles.
- [x] Verify repeated-run idempotence and the interruption, retry-partial, already-running, advisory-lock, and wrong-owner failure boundaries across the focused SQLite/CLI and real-PostgreSQL suites.
- [x] Verify PostgreSQL `vectors-to-blob` rejection and SQLite compatibility.

### Task 6: Synchronize documentation and run gates

**Files:**
- Modify together: `README.md`, `README_zh.md`, `AGENTS.md`, `CLAUDE.md`
- Modify together: `docs/operations.md`, `docs/operations_zh.md`
- Modify together: `docs/development.md`, `docs/development_zh.md`
- Modify: `scripts/README.md`
- Modify when applicable: `fangan_done.md`

- [x] Replace the old “all mutation phases are SQLite-only” constraint with the exact supported matrix and stopped-service boundary.
- [x] Document post-migration storage/index behavior and the SQLite-only vector-format exception.
- [x] Run focused backend tests during each task.
- [x] Run the complete offline gate: `bash scripts/check.sh`.
- [x] Run the real PostgreSQL lane: `bash scripts/check_postgres.sh` with `TEST_POSTGRES_URL`.
- [x] Confirm `cd frontend && npm run build` passes (also covered by the full gate).
- [ ] Run final specification and code-quality review, merge latest `master`, rerun gates, then push and open a PR.

## Definition of done

- Every supported production maintenance command selects the active formal backend through the central factory; command families are exercised by SQLite/CLI behavior tests plus real-PostgreSQL CLI, adapter, and conformance tests.
- PostgreSQL execution does not create or touch a SQLite application database.
- Large-library scans remain bounded and scale-index publication remains atomic.
- Existing online UI/API operations still work; no half-feature endpoint is introduced.
- Documentation, architecture contracts, offline gate, PostgreSQL integration lane, and frontend build are green.
