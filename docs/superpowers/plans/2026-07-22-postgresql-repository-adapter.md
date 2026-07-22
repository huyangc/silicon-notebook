# PostgreSQL Repository Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-complete PostgreSQL persistence adapter behind one repository factory while preserving the current SQLite runtime, public API contracts, retrieval semantics, and single-worker process model.

**Architecture:** Extract the backend-neutral repository facade/runtime composition from `SQLiteRepository`, inject a typed persistence bundle, and keep every SQL statement inside a backend-owned store. `DATABASE_URL` selects the one formal backend through `repositories/factory.py`; `SHADOW_DATABASE_URL` is reserved for the later shadow module and is never read by business services. PostgreSQL uses psycopg 3, a bounded synchronous pool, explicit transactions, checked-in migrations, `jsonb`, `bytea`, and `pg_trgm`; SQLite remains the default until the cutover plan changes the active URL.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, sqlite3, psycopg 3 (`psycopg[binary]`, `psycopg_pool`), PostgreSQL 17, pg_trgm, pytest/pytest-xdist, GitHub Actions, TypeScript/Next.js for readiness text only.

## Global Constraints

- Read and follow `docs/superpowers/specs/2026-07-22-postgresql-shadow-cutover-design.md` before implementation.
- Work on a dedicated `codex/postgresql-repository-adapter` branch/worktree. Do not mix shadow capture, cutover, reverse replication, pgvector, or multi-worker work into this branch.
- Use TDD for every behavior and architecture change: write the focused failing test, run it and confirm the intended failure, implement minimally, then rerun.
- Preserve the public methods and behavior of `SQLiteRepository`; keep `backend/app/services/sqlite_repository.py` as a compatibility import until the retirement plan.
- `repositories/postgres/` must never import `repositories/sqlite/`, and the reverse import is equally forbidden. Only the future `migration/shadow/` may open both.
- Application/API code must not branch on database dialect. `repositories/factory.py` is the only formal backend selection point.
- `DATABASE_URL` is the active formal backend. `SHADOW_DATABASE_URL` may be parsed by Settings but must not affect `repository()` or startup.
- Never log an unredacted PostgreSQL URL. Passwords, tokens, Memory text, source text, and connection options are not diagnostic output.
- Keep uvicorn at one worker. Retain hnswlib/scale/viz artifacts and store embeddings as PostgreSQL `bytea`; pgvector is explicitly out of scope.
- `scripts/check.sh` stays offline and PostgreSQL-independent. PostgreSQL tests live in a separate opt-in/CI lane.
- Schema and data migrations are forward-only and fail closed. Do not use destructive reset/recreate behavior against a non-test database.
- Update `README.md`, `README_zh.md`, `AGENTS.md`, `architecture.md`, `.env.example`, packaging instructions, and CI together. This is infrastructure rather than a completed `silicon_notebook_fangan.md` user feature, so do not falsely add it to `fangan_done.md`.

---

## Target file map

### Backend-neutral composition

- Create `backend/app/repositories/bundle.py`: typed `PersistenceBundle` and `PersistenceBundleFactory` protocols.
- Create `backend/app/services/repository_facade.py`: backend-neutral facade extracted from `SQLiteRepository`.
- Modify `backend/app/services/repository_runtime.py`: inject stores; do not construct SQLite stores.
- Modify `backend/app/services/sqlite_repository.py`: thin compatibility subclass/wrapper only.
- Create `backend/app/repositories/sqlite/bundle.py`: the only SQLite composition root.
- Create `backend/app/repositories/factory.py`: URL-scheme selection and redacted backend identity.
- Modify `backend/app/api/deps.py`, startup and approved scripts to use the factory.

### PostgreSQL adapter

- Create `backend/app/repositories/postgres/__init__.py`.
- Create `backend/app/repositories/postgres/database.py`, `migrator.py`, `bundle.py`, `repository.py`, `rows.py`, `search.py`.
- Create one PostgreSQL store for every SQLite store: `ask_state_store.py`, `chunk_store.py`, `embedding_store.py`, `governance_store.py`, `identity_store.py`, `index_projection_store.py`, `kg_build_job_store.py`, `knowhow_store.py`, `knowhow_transfer_store.py`, `knowledge_store.py`, `memory_store.py`, `notebook_store.py`, `query_store.py`, `report_store.py`, `sharing_store.py`, `source_store.py`, `unified_kg_store.py`.
- Create `backend/app/repositories/postgres/migrations/0001_initial.sql`, `0002_integrity_indexes.sql`, `0003_core_indexes.sql`, `0004_knowledge_indexes.sql`, `0005_memory_knowhow_governance_indexes.sql`, and `0006_search_gin.sql`.
- Create `backend/app/repositories/postgres/schema_manifest.py` pairing PostgreSQL migration version with SQLite schema version 23 at this phase.

### Tests and operations

- Create `backend/tests/postgres/conftest.py`, `test_database.py`, `test_migrations.py`, `test_store_surface.py`, `test_repository_conformance.py`, `test_search_conformance.py`, `test_concurrency.py`.
- Create `scripts/check_postgres.sh`.
- Modify `.github/workflows/ci.yml`, `backend/requirements.txt`, `.env.example`, `scripts/backend.sh`, `scripts/prod.sh`, `packaging/start.sh`, `packaging/DEPLOY.md`.

---

### Task 1: Accept and redact PostgreSQL configuration without changing the default backend

**Files:**
- Modify: `backend/app/core/config.py`
- Create: `backend/app/core/database_url.py`
- Modify: `backend/tests/test_settings_path_anchor.py`
- Create: `backend/tests/test_database_url.py`
- Modify: `.env.example`

**Interfaces:**

```python
@dataclass(frozen=True)
class DatabaseIdentity:
    scheme: Literal["sqlite", "postgresql"]
    host: str | None
    port: int | None
    database: str

def normalize_database_url(raw: str) -> str: ...
def database_identity(raw: str) -> DatabaseIdentity: ...
def redact_database_url(raw: str) -> str: ...
```

`Settings.database_url` remains the active URL. Add `Settings.shadow_database_url: str | None = None`, PostgreSQL pool settings (`min=1`, `max=10`, acquisition timeout `10s`, statement timeout `30s`, lock timeout `5s`) and a validator that accepts only `sqlite`, `postgresql`, and legacy `postgres` (normalized to `postgresql`). `sqlite_path` must still raise a clear error for a non-SQLite active URL.

- [ ] **Step 1: Replace the current “PostgreSQL is rejected” expectation with parsing/redaction tests**

Test default SQLite, `postgresql://`, `postgres://` normalization, IPv6 hosts, percent-encoded credentials, query options, missing database, MySQL rejection, and `repr`/diagnostic redaction. Assert `DATABASE_URL` remains SQLite by default and that changing only `SHADOW_DATABASE_URL` does not change `database_identity(settings.database_url)`.

- [ ] **Step 2: Run the focused tests and observe the expected failures**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_database_url.py backend/tests/test_settings_path_anchor.py
```

Expected: FAIL because PostgreSQL is currently rejected and no neutral URL helper exists.

- [ ] **Step 3: Implement strict URL parsing and redaction**

Use `urllib.parse.urlsplit`; never redact with a regular expression. Preserve options in the connection value but omit password and userinfo from log identities. Normalize only the scheme, not credential bytes.

- [ ] **Step 4: Add documented environment keys**

Add commented examples to `.env.example`; keep the enabled default SQLite and make clear that `SHADOW_DATABASE_URL` is inert until the shadow plans.

- [ ] **Step 5: Re-run focused tests and the settings contract lane**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_database_url.py backend/tests/test_settings_path_anchor.py \
  backend/tests/test_architecture_documentation.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/config.py backend/app/core/database_url.py \
  backend/tests/test_database_url.py backend/tests/test_settings_path_anchor.py .env.example
git commit -m "feat: accept PostgreSQL repository configuration"
```

### Task 2: Remove SQLite row types from repository ports and define the persistence bundle

**Files:**
- Modify: `backend/app/repositories/ports.py`
- Create: `backend/app/repositories/bundle.py`
- Create: `backend/tests/test_persistence_bundle_contract.py`
- Modify: `backend/tests/test_repository_protocol_coverage.py`

**Interfaces:**

```python
type RepositoryRow = Mapping[str, object]
type VectorBatchEncoder = Callable[[Sequence[RepositoryRow], str, str], None]

@runtime_checkable
class PersistenceBundle(Protocol):
    database: RepositoryDatabasePort
    identity: IdentityStorePort
    notebooks: NotebookStorePort
    sharing: SharingStorePort
    sources: SourceStorePort
    chunks: ChunkStorePort
    embeddings: EmbeddingStorePort
    knowledge: KnowledgeStorePort
    governance: GovernanceStorePort
    index_projection: IndexProjectionStorePort
    kg_build_jobs: KgBuildJobStorePort
    knowhow: KnowhowStorePort
    knowhow_transfer: KnowhowTransferStorePort
    memory: MemoryStorePort
    queries: QueryStorePort
    reports: ReportStorePort
    ask_state: AskStateStorePort
    unified_kg: UnifiedKgStorePort

class PersistenceBundleFactory(Protocol):
    def create(self, *, settings: Settings, root_dir: Path,
               seams: RepositorySeams,
               model_config_cache: dict[str, object]) -> PersistenceBundle: ...
```

- [ ] **Step 1: Add an architecture test that fails on `sqlite3` in neutral ports**

The test parses imports in `ports.py`, `bundle.py`, `repository_runtime.py`, and `repository_facade.py` and rejects `sqlite3`, psycopg, or backend store imports.

- [ ] **Step 2: Run it and confirm failure on the current `sqlite3.Row` alias**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_persistence_bundle_contract.py
```

- [ ] **Step 3: Introduce backend-neutral row/store protocols**

Use `Mapping[str, object]`; do not build a fake cross-dialect SQL connection protocol. Add missing store protocols by copying the actual public method signatures used by `RepositoryRuntime`, then keep the existing consumer-facing repository ports unchanged.

- [ ] **Step 4: Prove protocol coverage**

Update the coverage test so every attribute consumed from the bundle exists in a protocol and every current SQLite store satisfies its matching protocol structurally.

- [ ] **Step 5: Run repository architecture tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_persistence_bundle_contract.py \
  backend/tests/test_repository_protocol_coverage.py \
  backend/tests/test_repository_dependency_contract.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/repositories/ports.py backend/app/repositories/bundle.py \
  backend/tests/test_persistence_bundle_contract.py \
  backend/tests/test_repository_protocol_coverage.py
git commit -m "refactor: define backend-neutral persistence bundle"
```

### Task 3: Extract the generic facade/runtime and centralize backend selection

**Files:**
- Create: `backend/app/services/repository_facade.py`
- Modify: `backend/app/services/repository_runtime.py`
- Create: `backend/app/repositories/sqlite/bundle.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Create: `backend/app/repositories/factory.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/tests/conftest.py`
- Create: `backend/tests/test_repository_factory.py`
- Modify: `backend/tests/test_repository_dependency_contract.py`
- Modify: `backend/tests/fixtures/repository_contract/caller_boundaries.json`

**Interfaces:**

```python
class RepositoryFacade:
    def __init__(self, settings: Settings,
                 persistence_factory: PersistenceBundleFactory) -> None: ...

class SQLiteRepository(RepositoryFacade):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, SqlitePersistenceBundleFactory())

def create_repository(settings: Settings) -> NotebookRepository:
    scheme = database_identity(settings.database_url).scheme
    if scheme == "sqlite":
        return SQLiteRepository(settings)
    if scheme == "postgresql":
        from app.repositories.postgres.repository import PostgresRepository
        return PostgresRepository(settings)
    raise AssertionError("validated settings returned an unsupported scheme")
```

- [ ] **Step 1: Characterize the complete `SQLiteRepository` public surface**

Extend `test_repository_api_contract.py` to record public callable names and signatures before moving code. The expected surface must not be regenerated after extraction without review.

- [ ] **Step 2: Add failing injection/factory tests**

Use a recording bundle factory to assert that runtime composition consumes injected stores. Assert SQLite selection, lazy PostgreSQL import, unsupported-scheme fail close, and that `SHADOW_DATABASE_URL` cannot select the formal repository.

- [ ] **Step 3: Extract without changing behavior**

Move backend-neutral methods from the 3,000-line facade into `repository_facade.py`. Leave SQLite migration/maintenance-only methods on `SQLiteRepository`; if a shared caller uses them, first move that caller behind an explicit maintenance port rather than adding an optional dialect check.

Change `RepositoryRuntime.__init__` to create `model_config_cache`, request a bundle, and assign all stores from it. `SqlitePersistenceBundleFactory` is the only place that constructs `SqliteDatabase` and the 18 SQLite stores.

- [ ] **Step 4: Switch the API composition root**

`app.api.deps.repository()` remains `@lru_cache`, but returns `create_repository(get_settings())`. Keep direct SQLite construction in SQLite-specific tests and historical offline tools until later tasks classify them.

- [ ] **Step 5: Regenerate and review the caller-boundary fixture**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} scripts/generate_repository_contract_fixtures.py
git diff -- backend/tests/fixtures/repository_contract/caller_boundaries.json
```

Expected: formal runtime callers move from `services.sqlite_repository` to `repositories.factory`; no new private SQL or direct connections appear.

- [ ] **Step 6: Run the complete repository contract suite**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_repository_factory.py \
  backend/tests/test_repository_api_contract.py \
  backend/tests/test_repository_phase_contracts.py \
  backend/tests/test_repository_surface_contract.py \
  backend/tests/test_repository_dependency_contract.py \
  backend/tests/test_architecture_hardening.py
```

Expected: PASS with SQLite behavior unchanged.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/repository_facade.py \
  backend/app/services/repository_runtime.py \
  backend/app/repositories/sqlite/bundle.py \
  backend/app/services/sqlite_repository.py backend/app/repositories/factory.py \
  backend/app/api/deps.py backend/tests
git commit -m "refactor: centralize repository backend selection"
```

### Task 4: Build the PostgreSQL pool, transaction API, and migrator

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/pytest.ini`
- Create: `backend/app/repositories/postgres/__init__.py`
- Create: `backend/app/repositories/postgres/database.py`
- Create: `backend/app/repositories/postgres/rows.py`
- Create: `backend/app/repositories/postgres/migrator.py`
- Create: `backend/app/repositories/postgres/schema_manifest.py`
- Create: `backend/tests/postgres/conftest.py`
- Create: `backend/tests/postgres/test_database.py`
- Create: `backend/tests/postgres/test_migrations.py`

**Interfaces:**

```python
class PostgresDatabase:
    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection[dict[str, object]]]: ...

    @contextmanager
    def write(self, *, isolation_level: str = "read committed") \
            -> Iterator[psycopg.Connection[dict[str, object]]]: ...

    def close(self) -> None: ...
    def resolve_path(self, value: str | Path) -> Path: ...

class PostgresMigrator:
    def migrate(self) -> int: ...
    def current_version(self) -> int: ...
```

- [ ] **Step 1: Add the opt-in PostgreSQL test fixture**

Require `TEST_POSTGRES_URL`; skip the `postgres_integration` marker when it is absent. Give each test a unique schema, set `search_path`, and drop only that schema in teardown. Assert the URL database identity is a dedicated test database before dropping anything.

- [ ] **Step 2: Write failing pool/transaction tests**

Cover commit, rollback, nested use rejection, dict rows, UTC timestamp mapping, `statement_timeout`, `lock_timeout`, pool acquisition timeout, close idempotence, redacted diagnostics, and a two-connection row-lock test proving unrelated rows can update concurrently.

- [ ] **Step 3: Add psycopg dependencies and implement the pool**

Pin compatible major ranges, not a floating Git dependency. Configure `dict_row`, UTC, `application_name=silicon-notebook`, pool min/max, connection check, and server-side timeouts. There is no process-wide Python write lock.

- [ ] **Step 4: Implement migration locking/versioning**

Use a transaction-scoped advisory lock derived from the fixed string `silicon-notebook:postgres-migrations`, a `silicon_schema_migrations(version integer primary key, checksum text, applied_at timestamptz)` table, SHA-256 checksum validation, and one transaction per migration. A changed checksum or unknown future version must abort startup.

- [ ] **Step 5: Run integration tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend \
  ${PYTHON_BIN:-python3} -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/test_database.py backend/tests/postgres/test_migrations.py
```

Expected: PASS against a disposable PostgreSQL 17 database.

- [ ] **Step 6: Commit**

```bash
git add backend/requirements.txt backend/app/repositories/postgres \
  backend/tests/postgres backend/pytest.ini
git commit -m "feat: add PostgreSQL transaction substrate"
```

### Task 5: Create a schema-complete PostgreSQL baseline and parity guard

**Files:**
- Create: `backend/app/repositories/postgres/migrations/0001_initial.sql`
- Create: `backend/app/repositories/postgres/migrations/0002_integrity_indexes.sql`
- Create: `backend/app/repositories/postgres/migrations/0003_core_indexes.sql`
- Create: `backend/app/repositories/postgres/migrations/0004_knowledge_indexes.sql`
- Create: `backend/app/repositories/postgres/migrations/0005_memory_knowhow_governance_indexes.sql`
- Create: `backend/app/repositories/postgres/migrations/0006_search_gin.sql`
- Modify: `backend/app/repositories/postgres/schema_manifest.py`
- Create: `backend/tests/postgres/test_schema_parity.py`
- Create: `backend/tests/fixtures/postgres_schema_contract.json`

**Required table coverage:**

All 55 current ordinary SQLite tables in a freshly migrated v23 database must exist with equivalent keys, nullability, unique constraints, FK actions, and application-visible defaults. The excluded rebuilt set is exactly three FTS5 virtual roots plus fourteen FTS5 internal tables. The PostgreSQL-only migration/version tables are explicitly classified internal. Add `ordinal bigint generated by default as identity` only where current behavior actually depends on implicit SQLite `rowid`; queries must then sort on this explicit field. Every mapped PostgreSQL `text` column must explicitly use `COLLATE "C"`, matching SQLite's default `BINARY` ordering independently of the target database's default locale. PostgreSQL `server_encoding` must be `UTF8`; `0001` checks it before any business DDL and fails transactionally so neither ledger nor business tables survive an incompatible target.

- [ ] **Step 1: Generate a reviewed SQLite semantic schema fixture**

The generator reads `sqlite_master`, `PRAGMA table_info`, `foreign_key_list`, and `index_list` from a freshly migrated test DB and emits tables/columns/PK/FK/unique/default facts. It must not translate SQL automatically.

- [ ] **Step 2: Add a failing PostgreSQL parity test**

Compare `information_schema`/`pg_catalog` facts with an explicit mapping for type differences (`TEXT`→`text COLLATE "C"`, owned JSON→`jsonb`, embeddings→`bytea`, timestamps→`timestamptz`). Assert every ordinary SQLite table has one classification and every PG business table maps back. Assert mapped text columns report collation `C`, non-text columns report no collation, and text-producing expression indexes used for search/order carry or explicitly apply `C`.

- [ ] **Step 3: Write checked-in DDL**

Use application-owned string IDs with explicit per-column `COLLATE "C"`, deferred FKs only where multi-row transactions require them, explicit `ON DELETE` actions, server UTC timestamps, and named constraints. The target database may use any default locale because business text semantics are schema-local, but its server encoding must be exactly `UTF8`; collation is not an encoding substitute. `0001` checks encoding before creating its first business table. `0001` owns tables and declared constraints; `0002` installs/verifies `pg_trgm` plus only the six partial unique integrity indexes that must remain active during bulk COPY. Split the remaining 73 non-unique SQLite operational indexes into resumable checksummed domain migrations `0003`-`0005`, and put only the five GIN search indexes in `0006`; a failure reruns at most one bounded group, not the complete 4.8 GB index build. Text-producing expressions whose collation does not propagate from a text column explicitly apply `C`. Do not install pgvector.

- [ ] **Step 4: Test migrations from empty and checksum drift**

Add a strictly bounded `migrate(target_version=..., statement_timeout_seconds=...)` API: accept only integer targets in `1..manifest.postgres_version`, validate the complete ledger/checksums/future-version guard, and never downgrade. The optional migration-only timeout must be finite, positive, capped, applied with transactional `SET LOCAL` while preserving `lock_timeout`, and reset automatically on commit/rollback; ordinary `migrate()` keeps the pool's bounded default. Test that a deliberately low pool timeout fails a slow migration, a longer explicit override succeeds, the next ordinary borrower sees the pool default again, and invalid/zero/infinite/over-cap overrides fail before connecting. From empty, migrate to v2 and assert COPY-ready state contains the six partial unique integrity indexes but none of the 73 operational or five GIN indexes; then migrate through individually observable ledger versions v3-v6, verify final parity and idempotence, mutate a copied migration, and assert checksum refusal. Ordinary startup still calls `migrate()` and reaches v6.

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/postgres/migrations \
  backend/app/repositories/postgres/schema_manifest.py \
  backend/tests/postgres/test_schema_parity.py \
  backend/tests/fixtures/postgres_schema_contract.json
git commit -m "feat: define PostgreSQL application schema"
```

### Task 6: Port identity, notebook, sharing, source, chunk, and job stores

**Files:**
- Create: `backend/app/repositories/postgres/identity_store.py`
- Create: `backend/app/repositories/postgres/notebook_store.py`
- Create: `backend/app/repositories/postgres/sharing_store.py`
- Create: `backend/app/repositories/postgres/source_store.py`
- Create: `backend/app/repositories/postgres/chunk_store.py`
- Create: `backend/app/repositories/postgres/kg_build_job_store.py`
- Create: `backend/tests/postgres/test_core_store_conformance.py`

**Semantic matrix:**

- Identity: username normalization/uniqueness, PBKDF2 payload preservation, auth session expiry/touch throttling, agent token scope/allowlist/revocation, no existence oracle.
- Notebook/sharing: owner isolation, base visibility, members, links, copy/join, mounted bases, tier rules, delete cascades.
- Source/chunk: multipart metadata, element/chunk stable order, parse state, reparse/delete cleanup, physical vs visible source counts, file path mapping.
- KG build jobs: claim/heartbeat/cancel/terminal transitions and concurrent claim exclusion.

- [ ] **Step 1: Parameterize existing behavior tests over a repository factory fixture**

Keep SQLite as the fast default. For PostgreSQL, run the same selected test functions using `postgres_repository` and a temporary storage root. Do not fork/duplicate assertions.

- [ ] **Step 2: Confirm failures because the PG stores do not exist**

- [ ] **Step 3: Implement independent PostgreSQL SQL**

Use `%s` parameters, `RETURNING`, `ON CONFLICT`, conditional `UPDATE ... WHERE`, `FOR UPDATE SKIP LOCKED` for job claims, and explicit ordering. Never mechanically transform SQLite SQL at runtime.

- [ ] **Step 4: Run the group conformance suite**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend \
  ${PYTHON_BIN:-python3} -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/test_core_store_conformance.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/postgres/{identity_store,notebook_store,sharing_store,source_store,chunk_store,kg_build_job_store}.py \
  backend/tests/postgres/test_core_store_conformance.py
git commit -m "feat: port core repositories to PostgreSQL"
```

### Task 7: Port knowledge, governance, embeddings, search, and graph stores

**Files:**
- Create: `backend/app/repositories/postgres/embedding_store.py`
- Create: `backend/app/repositories/postgres/knowledge_store.py`
- Create: `backend/app/repositories/postgres/governance_store.py`
- Create: `backend/app/repositories/postgres/index_projection_store.py`
- Create: `backend/app/repositories/postgres/query_store.py`
- Create: `backend/app/repositories/postgres/unified_kg_store.py`
- Create: `backend/app/repositories/postgres/search.py`
- Create: `backend/tests/postgres/test_knowledge_store_conformance.py`
- Create: `backend/tests/postgres/test_search_conformance.py`

**Semantic matrix:**

- Preserve knowledge lifecycle/USABLE filtering, evidence, merge, conflict, promotion, edge review, canonical relations, and object-level unified graph.
- Store current float32 embedding bytes unchanged in `bytea`; validate byte length/dimension and preserve current runtime truncation/cache-version behavior.
- Implement mixed Chinese/English lexical candidate search with `pg_trgm` plus deterministic secondary keys. Do not expose `similarity()` scores as a public cross-backend contract.
- Preserve notebook/base federation, exact-score base tie-break only where currently specified, and relation sorting by score only.

- [ ] **Step 1: Freeze the current SQLite candidate/output golden set**

Use deterministic model substitutes and a compact mixed Chinese/English notebook fixture. Record IDs, relevance order, citation/source IDs, not raw private text.

- [ ] **Step 2: Add failing PostgreSQL conformance and quality tests**

Require recall@12 loss ≤1 percentage point, top-10 overlap ≥0.90, and 100% deterministic citation/source ID set equality. Test JSON null/NaN rejection and embedding round trips separately.

- [ ] **Step 3: Implement the six stores and search helper**

Keep all PG search expressions in `postgres/search.py`; stores call typed helpers rather than scattering trigrams. JSON-derived query expressions `(payload ->> 'name') COLLATE "C"` and `(tags_json::text) COLLATE "C"` must match their `0006` expression GIN indexes exactly; direct text-column expressions inherit the column-level `C` collation. Lock these expression/index definitions with catalog and search-conformance tests. Use `jsonb` containment only for application-owned JSON and preserve the existing normalized payload mapper.

- [ ] **Step 4: Run conformance and quality tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend \
  ${PYTHON_BIN:-python3} -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/test_knowledge_store_conformance.py \
  backend/tests/postgres/test_search_conformance.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/postgres/{embedding_store,knowledge_store,governance_store,index_projection_store,query_store,unified_kg_store,search}.py \
  backend/tests/postgres/test_knowledge_store_conformance.py \
  backend/tests/postgres/test_search_conformance.py
git commit -m "feat: port knowledge retrieval to PostgreSQL"
```

### Task 8: Port Ask, reports, Memory, and Knowhow stores with concurrency semantics

**Files:**
- Create: `backend/app/repositories/postgres/ask_state_store.py`
- Create: `backend/app/repositories/postgres/report_store.py`
- Create: `backend/app/repositories/postgres/memory_store.py`
- Create: `backend/app/repositories/postgres/knowhow_store.py`
- Create: `backend/app/repositories/postgres/knowhow_transfer_store.py`
- Create: `backend/tests/postgres/test_content_store_conformance.py`
- Create: `backend/tests/postgres/test_concurrency.py`

**Semantic matrix:**

- Ask: job creation/progress/cancel/final-save atomicity, detached completion, no final answer after explicit cancellation.
- Reports: outline/generation state machine, ownership, cancellation/deletion, citation persistence.
- Memory: all validation caps remain in neutral core; owner/notebook isolation, candidate authority, revisions/provenance, save-answer transaction, promotion pin/supersede/admin approval atomicity.
- Knowhow: mutation sequence, single-flight scheduler state, projection-completion contract, code isolation, anchor-group exact membership and content baseline 409 with zero partial writes, transfers/imports.

- [ ] **Step 1: Add shared state-machine tests plus real two-connection races**

Test cancelled Ask vs final save, revoked notebook access vs save-to-Memory, competing KG promotion approval, stale projection pass vs newer edit, and batch reformat membership drift. Use barriers/events, not sleeps.

- [ ] **Step 2: Confirm the PostgreSQL cases fail before implementation**

- [ ] **Step 3: Implement conditional writes and locks per use case**

Use row locks for aggregate state, unique constraints for idempotency, advisory locks only for existing table-wide single-flight semantics, and `SERIALIZABLE` only for the exact save units that require predicate protection. Map serialization/deadlock errors to the current retry/conflict policy; do not leak psycopg exceptions through API routes.

- [ ] **Step 4: Run state and concurrency tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend \
  ${PYTHON_BIN:-python3} -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/test_content_store_conformance.py \
  backend/tests/postgres/test_concurrency.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/postgres/{ask_state_store,report_store,memory_store,knowhow_store,knowhow_transfer_store}.py \
  backend/tests/postgres/test_content_store_conformance.py \
  backend/tests/postgres/test_concurrency.py
git commit -m "feat: port stateful content stores to PostgreSQL"
```

### Task 9: Compose `PostgresRepository` and make startup/backend scripts backend-neutral

**Files:**
- Create: `backend/app/repositories/postgres/bundle.py`
- Create: `backend/app/repositories/postgres/repository.py`
- Modify: `backend/app/services/startup_warmup.py`
- Modify: `backend/app/core/readiness.py`
- Modify: `scripts/backend.sh`
- Modify: `scripts/prod.sh`
- Modify: `packaging/start.sh`
- Create: `backend/tests/postgres/test_repository_conformance.py`
- Modify: `backend/tests/test_repository_factory.py`

**Interfaces:**

```python
class PostgresPersistenceBundleFactory:
    def create(...) -> PersistenceBundle: ...

class PostgresRepository(RepositoryFacade):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, PostgresPersistenceBundleFactory())
```

- [ ] **Step 1: Add a complete repository boot and smoke test**

Start from an empty test schema, migrate, seed only admin, create/authenticate a user, create/import a minimal notebook, perform search/Ask fallback, Memory, Knowhow projection, report lifecycle, sharing, and deletion. Assert no demo notebook is seeded.

- [ ] **Step 2: Compose all PG stores**

Run migrator before stores, then construct the same shared services/caches as SQLite. Close the pool on app shutdown using the existing lifespan hook. If startup migration fails, readiness reports a redacted error and does not serve routes.

- [ ] **Step 3: Make shell output backend-neutral**

`scripts/backend.sh status` prints `database=sqlite path=...` or `database=postgresql host=... db=...` using the redaction helper. Do not parse PostgreSQL URLs in shell or print credentials. Production/packaging still pass `--workers 1`.

- [ ] **Step 4: Run boot/conformance tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend \
  ${PYTHON_BIN:-python3} -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/test_repository_conformance.py \
  backend/tests/test_repository_factory.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/postgres/{bundle,repository}.py \
  backend/app/services/startup_warmup.py backend/app/core/readiness.py \
  scripts/backend.sh scripts/prod.sh packaging/start.sh backend/tests
git commit -m "feat: boot silicon-notebook on PostgreSQL"
```

### Task 10: Add the protected PostgreSQL integration lane and synchronized documentation

**Files:**
- Create: `scripts/check_postgres.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `backend/pytest.ini`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `packaging/DEPLOY.md`
- Modify: `backend/tests/test_architecture_documentation.py`

- [ ] **Step 1: Add failing documentation/CI contract tests**

Assert `scripts/check.sh` does not invoke PostgreSQL, `check_postgres.sh` requires `TEST_POSTGRES_URL`, CI runs a separate `postgres-integration` job with a PostgreSQL 17 service (including schema parity in a UTF8 database whose default collation is not `C`, plus a dedicated non-UTF target that must fail before ledger/business DDL), and all three required docs describe `DATABASE_URL`/`SHADOW_DATABASE_URL`, single active backend, bytea/no-pgvector, single worker, and redaction.

- [ ] **Step 2: Implement the isolated test lane**

`scripts/check_postgres.sh` runs only `-m postgres_integration` after a connection preflight. CI service has a health check and a dedicated non-superuser application role/database. In the PostgreSQL 17 lane, create a separate UTF8 ICU/libc non-`C`-default database and run schema parity there; do not reject its default locale, because every business text column and non-propagating text expression is fixed to `C` in the schema. Also create an isolated SQL_ASCII/LATIN1 target solely for the negative migration test and assert `0001` leaves no ledger/business DDL. Do not add Docker/PostgreSQL startup to `scripts/check.sh`.

- [ ] **Step 3: Synchronize docs**

Document how to boot directly on PostgreSQL for development, how to return to SQLite before shadow mode, dependency installation, pool/timeouts, backups, and the explicit statement that changing `DATABASE_URL` alone is not a safe live-data migration. Link the design and the next forward-shadow plan.

- [ ] **Step 4: Run all local offline gates**

```bash
PYTHON_BIN=${PYTHON_BIN:-python3} bash scripts/check.sh
cd frontend && npm run build
```

Expected: both PASS without a running PostgreSQL server.

- [ ] **Step 5: Run the PostgreSQL lane twice**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHON_BIN=${PYTHON_BIN:-python3} \
  bash scripts/check_postgres.sh
```

Expected: two consecutive PASS runs against a clean disposable database.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_postgres.sh .github/workflows/ci.yml backend/pytest.ini \
  README.md README_zh.md AGENTS.md architecture.md packaging/DEPLOY.md \
  backend/tests/test_architecture_documentation.py
git commit -m "ci: verify PostgreSQL repository integration"
```

---

## Phase acceptance gate

- [ ] `git status --short` contains only intentional changes.
- [ ] `scripts/check.sh` passes offline.
- [ ] `cd frontend && npm run build` passes.
- [ ] `scripts/check_postgres.sh` passes twice against a clean PostgreSQL 17 service, including schema parity in a UTF8/non-`C`-default database and transactional refusal of a non-UTF target.
- [ ] Shared repository conformance covers both adapters; PostgreSQL store surface exactly matches the injected store protocols.
- [ ] Architecture tests prove no SQLite↔PostgreSQL adapter imports and no dialect branches outside the factory.
- [ ] `DATABASE_URL=sqlite://...` remains the shipped default; no production cutover has occurred.
- [ ] A direct test deployment using `DATABASE_URL=postgresql://...` boots, completes the full repository smoke, and keeps uvicorn at one worker.
- [ ] Request code review using `superpowers:requesting-code-review`; fix every Critical/Important finding, then rerun the affected focused tests plus all gates above.

The next plan is `docs/superpowers/plans/2026-07-22-postgresql-forward-shadow-sync.md`. Do not begin it until this phase is merged and the PostgreSQL integration check is protected.
