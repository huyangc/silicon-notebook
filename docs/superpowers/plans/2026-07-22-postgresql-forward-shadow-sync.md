# PostgreSQL Forward Shadow Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While SQLite remains the only formal read/write authority, build a resumable PostgreSQL baseline, capture every committed SQLite business mutation, continuously apply it to PostgreSQL, and produce barrier-aware consistency evidence without adding dual-write code to business services.

**Architecture:** A temporary, self-contained `migration/shadow` module owns a total table manifest, SQLite trigger capture, snapshot/COPY pipeline, forward worker, checkpoints, and verifier. SQLite business transactions append only dirty stable replication keys in the same transaction. A single forward worker hydrates current rows, applies idempotent PostgreSQL upserts/deletes, and commits each batch with its checkpoint. PostgreSQL shadow metadata lives in a removable `silicon_shadow` schema. The formal API still uses SQLite through `DATABASE_URL`; PostgreSQL is addressed only by `SHADOW_DATABASE_URL`.

**Tech Stack:** Python 3.13, sqlite3 backup API/FTS5, psycopg 3/COPY, PostgreSQL 17, SHA-256/BLAKE2b streaming hashes, pytest, shell process wrapper, existing repository conformance fixtures.

## Global Constraints

- This plan depends on the merged, protected PostgreSQL adapter from `2026-07-22-postgresql-repository-adapter.md`.
- Use a dedicated `codex/postgresql-forward-shadow` branch/worktree.
- Follow the approved design spec and TDD each behavior.
- SQLite is the sole business authority for this entire phase. `DATABASE_URL` remains SQLite; PostgreSQL must not serve production API requests.
- No service, API route, background job, MCP tool, or ordinary store writes two databases. Only `backend/app/migration/shadow/` may import both adapters.
- Replication is one-way (`sqlite_to_postgres`) and single-consumer. Reverse capture, formal cutover, rollback, pgvector, and multi-worker are out of scope.
- Change logs contain table, stable replication key, operation, schema epoch, run id, and time only. They never duplicate row payloads or embedding BLOBs.
- Target row apply and forward checkpoint commit in one PostgreSQL transaction. Poison events stop progress; there is no skip flag.
- Full copy and verification stream data in bounded batches; the 4.8 GB current database must not be materialized in memory.
- Shared files are referenced and verified, never copied into PostgreSQL.
- `scripts/check.sh` remains offline. Shadow E2E belongs to `scripts/check_postgres.sh`/the PostgreSQL CI lane.
- Update README/README_zh/AGENTS/architecture and packaging operations in sync; do not mark a product-spec feature complete.

---

## Target file map

- Create package `backend/app/migration/shadow/` with `types.py`, `manifest.py`, `identity.py`, `control.py`, `capture.py`, `snapshot.py`, `bulk_copy.py`, `transform.py`, `replicator.py`, `verifier.py`, `retention.py`, `metrics.py`, `cli.py`, `worker.py`.
- Create `backend/app/migration/shadow/sql/postgres_control.sql`.
- Modify SQLite migration files to add schema version 24 shadow control/log tables; trigger installation remains run-scoped in `capture.py`.
- Create thin entry `scripts/migrate_sqlite_to_postgres.py`.
- Create local process wrapper `scripts/shadow.sh`.
- Create unit tests under `backend/tests/shadow/` and PostgreSQL E2E tests under `backend/tests/postgres/shadow/`.

---

### Task 1: Establish the total replication manifest and schema compatibility pair

**Files:**
- Create: `backend/app/migration/shadow/types.py`
- Create: `backend/app/migration/shadow/manifest.py`
- Modify: `backend/app/repositories/postgres/schema_manifest.py`
- Create: `backend/tests/shadow/test_manifest.py`
- Create: `backend/tests/fixtures/shadow_manifest_contract.json`

**Interfaces:**

```python
class TableClass(StrEnum):
    REPLICATED = "replicated"
    REBUILT = "rebuilt"
    SHARED_FILESYSTEM = "shared-filesystem"
    SHADOW_INTERNAL = "shadow-internal"

class ReplicationKeyKind(StrEnum):
    DECLARED_PK = "declared_pk"
    SHADOW_UNIQUE = "shadow_unique"

@dataclass(frozen=True)
class TableSpec:
    name: str
    table_class: TableClass
    replication_key: tuple[str, ...]
    key_kind: ReplicationKeyKind
    copy_rank: int
    blob_columns: tuple[str, ...] = ()
    path_columns: tuple[str, ...] = ()
    sqlite_to_postgres: str = "identity"
    postgres_to_sqlite: str = "identity"

@dataclass(frozen=True)
class SchemaPair:
    sqlite_version: int
    postgres_version: int
    epoch: int
```

The replicated set at epoch 1 is exactly these 55 tables:

```text
agent_access_tokens, agent_profiles, agent_token_notebooks, answers,
ask_jobs, ask_trace_steps, auth_sessions, canonical_relations,
chunk_embeddings, chunks, communities, community_members, concept_clusters,
concept_comentions, concept_merge_candidates, concept_whitelist, conversations,
element_embeddings, extraction_runs, feedback, kg_build_jobs, kg_cluster_scratch,
kg_conflict_candidates,
kg_rebuild_checkpoint, knowhow_cell_code, knowhow_cells, knowhow_columns,
knowhow_rows, knowhow_tables, knowledge_embeddings, knowledge_object_sources,
knowledge_objects, knowledge_relations, memory_embeddings, memory_items,
memory_provenance, memory_revisions, mention_edges, merge_review_jobs,
model_service_status, notebook_assets, notebook_bases, notebook_members, notebooks,
object_schemas, promotion_candidates, relation_embeddings, reports, source_authors,
source_elements, source_paper_meta, sources, unified_kg_state, user_profiles, users
```

FTS5 families `chunks_fts*`, `kg_objects_fts*`, and `memory_items_fts*` are `rebuilt`. Source/upload/asset/scale/viz paths are `shared-filesystem`; the rows that reference them remain replicated. SQLite `sqlite_*`, PostgreSQL catalogs, and all shadow tables are excluded/internal.

Most entries use their declared database primary key as the stable replication key
(`key_kind=declared_pk`). The three reviewed exceptions remain replicated and use
logical composite keys guarded for the life of shadow sync
(`key_kind=shadow_unique`):

- `community_members=(notebook_id, level, canonical_id)`;
- `kg_cluster_scratch=(object_id, notebook_id, run_id)` (the same unique tuple, ordered so its guard cannot replace the existing `(notebook_id, run_id)` access path);
- `knowledge_object_sources=(object_id, source_id)`.

Preflight must scan all rows in each exception and fail closed if any key is null or
duplicated; it must never silently deduplicate. The SQLite installation interface
performs that scan, creates the three guards, and installs its capture/freeze triggers
inside one `BEGIN IMMEDIATE`, leaving capture disabled. A separate PostgreSQL
preflight/guard transaction creates and verifies the corresponding indexes there.
Only after both independent transactions succeed may control perform a conditional
CAS that enables SQLite capture. There is no cross-database atomic installation:
failure on either side leaves capture disabled and both interfaces must be idempotently
retryable. The explicitly named, shadow-owned unique indexes are
`shadow_uq_community_members_replication_key`,
`shadow_uq_kg_cluster_scratch_replication_key`, and
`shadow_uq_knowledge_object_sources_replication_key` on both SQLite and PostgreSQL.
These observation-period guards prevent new duplicates without changing the reviewed
business schema pair. They are excluded from business-index parity and are removed
explicitly by shadow retirement. Updating any declared or logical replication-key
column emits delete-old followed by upsert-new.

After SQLite v24 is installed, `(sqlite=24, postgres=2, epoch=1)` is the narrowly
scoped COPY-ready staging pair: PostgreSQL has tables/constraints and six partial
unique integrity indexes, but no deferred operational/search indexes. After COPY,
reseed, and normal ledger migration, the accepted running pair is
`(sqlite=24, postgres=6, epoch=1)`. Installing the removable `silicon_shadow` schema
does not itself change either business schema version.

- [ ] **Step 1: Add a failing totality test**

Create a fresh SQLite v24 database, enumerate every ordinary user table and FTS family, and compare with the manifest. Query PostgreSQL business tables and enforce the same reverse totality. Fail on a new table, a missing/nullable/duplicate stable replication key, duplicate copy rank, FK rank inversion, unknown transform, or unclassified path/BLOB column. The three `shadow_unique` entries require full-table duplicate preflight on both databases before capture can be enabled. The manifest recognizes PG v2 only as COPY-ready staging and requires PG v6 for normal running verification.

- [ ] **Step 2: Run the unit test and observe failure**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/shadow/test_manifest.py
```

- [ ] **Step 3: Implement immutable manifest entries**

Declare each table once. Resolve each exact replication-key tuple: the declared PK for ordinary entries and the reviewed logical composite key for the three `shadow_unique` entries. Order composite fields exactly as capture/hash/apply will use them. Add transform names for timestamptz, JSONB, boolean, and bytea fields; no callable lambdas in the data declaration.

- [ ] **Step 4: Generate and review the manifest fixture**

The fixture contains table/class/replication key/key kind/copy rank/transform names only, no data. Review the diff and commit it as a schema-change tripwire.

- [ ] **Step 5: Run SQLite unit and PostgreSQL parity cases**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 backend/tests/shadow/test_manifest.py
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration backend/tests/shadow/test_manifest.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/migration/shadow/{types,manifest}.py \
  backend/app/repositories/postgres/schema_manifest.py \
  backend/tests/shadow/test_manifest.py \
  backend/tests/fixtures/shadow_manifest_contract.json
git commit -m "feat: define total shadow replication manifest"
```

### Task 2: Add transactional SQLite capture and fail-closed write-gate primitives

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`
- Create: `backend/app/migration/shadow/capture.py`
- Create: `backend/tests/shadow/test_sqlite_capture.py`
- Create: `backend/tests/shadow/test_sqlite_capture_all_tables.py`

**SQLite v24 schema:**

```sql
CREATE TABLE shadow_change_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  table_name TEXT NOT NULL,
  key_json TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
  schema_epoch INTEGER NOT NULL,
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE shadow_capture_control (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  write_frozen INTEGER NOT NULL CHECK (write_frozen IN (0, 1)),
  apply_active INTEGER NOT NULL DEFAULT 0 CHECK (apply_active IN (0, 1)),
  run_id TEXT NOT NULL,
  schema_epoch INTEGER NOT NULL
);
```

**Interfaces:**

```python
def install_sqlite_capture(conn: sqlite3.Connection, *, run_id: str,
                           schema_epoch: int, manifest: Manifest) -> None: ...
def validate_sqlite_capture(conn: sqlite3.Connection, manifest: Manifest) -> None: ...
def disable_sqlite_capture(conn: sqlite3.Connection, *, run_id: str) -> None: ...
```

- [ ] **Step 1: Write trigger behavior tests first**

For every replicated table create a minimally valid row via existing repository fixtures, then exercise INSERT, non-key UPDATE, replication-key UPDATE where legal, DELETE, and applicable FK cascade. Assert canonical `key_json`, exact event order, run/epoch, no events for rebuilt/internal tables, and no BLOB payload in the log.

Test a rolled-back business transaction leaves neither row nor visible log. Test `enabled=0` creates no log. Test `write_frozen=1, apply_active=0` aborts a direct SQL mutation, while the later controlled lease (`apply_active=1` in the same write transaction) permits it.

For each `shadow_unique` table, seed a duplicate and prove installation fails without
leaving an index, trigger, or enabled control row; then remove it and prove the full
duplicate scan, three named SQLite guards, and all triggers commit in one
`BEGIN IMMEDIATE`. Inject a failure after guard creation and assert the whole SQLite
transaction rolls back and an idempotent retry succeeds.

- [ ] **Step 2: Confirm the tests fail before v24/capture exists**

- [ ] **Step 3: Add v24 and generated triggers**

Generate a `BEFORE INSERT/UPDATE/DELETE` freeze trigger and `AFTER` capture triggers per manifest entry. Quote only manifest-owned identifiers; values remain bound. A replication-key UPDATE emits OLD delete then NEW upsert. The SQLite-side install transaction uses one `BEGIN IMMEDIATE` for duplicate scan + named guards + triggers and commits with capture disabled. PostgreSQL duplicate scan + guards use a separate target transaction. Only after both are verified does a run/epoch/revision CAS enable capture; any failure remains disabled and can be retried idempotently. Installation refuses an active different run and never claims cross-database atomicity.

Before fixing any guard column order, audit every unordered read of the three tables and
lock the result with behavior plus `EXPLAIN QUERY PLAN` tests. In particular,
`stream_scratch_rows WHERE notebook_id=? AND run_id=?` must keep using
`idx_kg_cluster_scratch_nb_run` (or return the identical pre-guard stream); therefore
the scratch logical key/unique guard is ordered
`(object_id, notebook_id, run_id)`, whose left prefix cannot steal that access path.
Audit `community_members` and `knowledge_object_sources` likewise and prove their
selected guard orders either preserve result order where it matters or that every
consumer is order-insensitive. Do not add another ordinal without evidence that an
observable cross-backend ordering contract exists.

- [ ] **Step 4: Validate trigger SQL structurally**

`validate_sqlite_capture` compares normalized trigger SQL/name/table/events to the manifest and rejects missing, extra, manually changed, or stale-epoch triggers before a worker can start.

- [ ] **Step 5: Run focused tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/shadow/test_sqlite_capture.py \
  backend/tests/shadow/test_sqlite_capture_all_tables.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/repositories/sqlite/migrations.py \
  backend/app/migration/shadow/capture.py backend/tests/shadow
git commit -m "feat: capture SQLite changes transactionally"
```

### Task 3: Create shadow run identity, control state, checkpoints, and preflight

**Files:**
- Create: `backend/app/migration/shadow/sql/postgres_control.sql`
- Create: `backend/app/migration/shadow/identity.py`
- Create: `backend/app/migration/shadow/control.py`
- Create: `backend/tests/postgres/shadow/test_control.py`
- Create: `backend/tests/shadow/test_identity.py`

**PostgreSQL shadow objects:**

```text
silicon_shadow.runs
silicon_shadow.checkpoints
silicon_shadow.workers
silicon_shadow.poison_events
silicon_shadow.copy_progress
silicon_shadow.verification_runs
silicon_shadow.verification_differences
silicon_shadow.verifier_barriers
silicon_shadow.retention_checkpoints
```

`runs` stores run id, source/target redacted identities and identity hashes, schema epoch/pair, phase, active backend, created/updated times, and terminal reason. It never stores a URL or credential.

**Target-guard interface:**

```python
def prepare_postgres_replication_key_guards(
    target: PostgresDatabase, *, manifest: Manifest, run_id: str
) -> GuardReport: ...
```

This independently scans all three logical-key tables, fails on NULL/duplicates, and
creates/verifies the three named shadow-owned PostgreSQL unique indexes in one target
transaction. It does not enable SQLite capture and is idempotent only for the same
reviewed definitions.

- [ ] **Step 1: Write failing state/identity tests**

Cover database identity fingerprints, same-database rejection, non-empty/unowned target rejection, source path/storage root validation, PostgreSQL UTF8 server-encoding/extension/privilege checks, schema pair mismatch, run reuse, illegal phase transition, and concurrent control updates. A SQL_ASCII/LATIN1 target must fail before `--prepare-target`, capture/guard installation, snapshot creation, or any other write. Cover PostgreSQL logical-key duplicate preflight/guard rollback and retry separately from SQLite installation, then prove capture-enable CAS remains disabled until both side-specific reports are committed and verified.

- [ ] **Step 2: Implement shadow schema installation**

Use a removable dedicated schema and a transaction-scoped advisory lock. Refuse if an unknown `silicon_shadow` schema already exists. Checksum the SQL just like formal migrations.

- [ ] **Step 3: Implement state CAS**

Every transition is a conditional update on run id + expected phase + revision. The phase enum is exactly `off`, `sqlite_to_postgres`, `cutover_readonly`, `postgres_to_sqlite`, `retired`. This phase only permits `off -> sqlite_to_postgres` and same-phase progress updates.

- [ ] **Step 4: Implement `preflight`**

Its first target compatibility check requires `current_setting('server_encoding')='UTF8'`; collation does not substitute for encoding. It performs no snapshot or source/target write until that check succeeds. The remaining preflight is read-only except installing the owned shadow schema when explicitly passed `--prepare-target`. It reports disk estimates, connection/pool settings, schema identities, target ownership/emptiness, storage references, and backup prerequisites using redacted identities.

- [ ] **Step 5: Run tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_control.py backend/tests/shadow/test_identity.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/migration/shadow/{sql,identity.py,control.py} \
  backend/tests/postgres/shadow/test_control.py backend/tests/shadow/test_identity.py
git commit -m "feat: add shadow migration control plane"
```

### Task 4: Build a consistent SQLite snapshot and resumable PostgreSQL bulk copy

**Files:**
- Create: `backend/app/migration/shadow/snapshot.py`
- Create: `backend/app/migration/shadow/transform.py`
- Create: `backend/app/migration/shadow/bulk_copy.py`
- Create: `backend/tests/shadow/test_snapshot.py`
- Create: `backend/tests/postgres/shadow/test_bulk_copy.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    run_id: str
    source_identity_hash: str
    schema_epoch: int
    baseline_seq: int
    size_bytes: int
    sha256: str

def create_snapshot(source: SqliteDatabase, destination: Path, ...) -> SnapshotInfo: ...
def copy_snapshot(snapshot: SnapshotInfo, target: PostgresDatabase,
                  manifest: Manifest, batch_rows: int = 2_000) -> CopyReport: ...
```

- [ ] **Step 1: Test snapshot atomicity under active writes**

Use sqlite backup progress hooks and a writer thread. Assert the snapshot is internally consistent, `H0=MAX(shadow_change_log.seq)` is read from the snapshot itself, the live DB remains writable, and a partial snapshot is never published after cancellation/error.

- [ ] **Step 2: Test type conversions and bounded COPY**

Cover JSONB canonical values/null, boolean integers, timestamptz, empty/nonempty bytea, non-ASCII text, large Markdown, and paths. Instrument reads so no fetch exceeds `batch_rows`; assert embedding BLOBs are streamed and never converted to Python float arrays. For all seven manifest-declared ordinal tables, COPY explicit historical ordinals, reseed, and assert the next insert receives `MAX(ordinal)+1`; for an empty table assert the first generated ordinal is `1`. Inject reseed failure and assert the table/COPY run cannot be marked complete or advance a checkpoint.

- [ ] **Step 3: Implement temp-file snapshot publication**

Write under the configured migration work directory, fsync, validate SQLite integrity/schema/run, hash, then `os.replace` to the final run-specific path. Set mode `0600`. Never overwrite another run's snapshot.

- [ ] **Step 4: Implement resumable COPY**

Use the formal checksummed migrator to prepare the target at PostgreSQL v2 and verify that only tables/constraints plus the six partial unique integrity indexes are present. Copy by manifest rank and stable replication-key order into that target. Save the last replication key, rows, bytes, and rolling hash per table. Resume only when run/snapshot hash/schema/checkpoint match. For a failed partial table, delete only rows tagged/owned by the current pre-cutover run or restart that table inside a transaction; never truncate an unrelated target.

- [ ] **Step 5: Reseed identities, migrate through v3-v6, and analyze**

After all rows for a table are copied, reseed every one of the seven
`POSTGRES_ROWID_ORDINAL_TABLES`. Resolve each owned identity sequence through bound
manifest table/column values and `pg_catalog` dependency metadata to a sequence
OID/`regclass`; never interpolate an untrusted identifier. For a non-empty table set
the sequence state so the first new value is `MAX(ordinal)+1`; for an empty table set
it so the first new value is `1`. Reseed success is part of table completion and a
precondition for any copy/checkpoint promotion.

Then invoke the normal checksummed migrator from v2 through individually checkpointed
v3-v6 using a shadow-owned, long but finite migration statement-timeout setting; do
not manually run, drop/recreate, or bypass the ledger for an index group. Keep the
pool's `lock_timeout`, allow external cancellation, and record each migration
version's start/failure/success in later CLI status. The transaction-local override
must reset before the connection returns to ordinary borrowers. Keep PK/FK/unique
constraints and the six v2 integrity indexes active throughout COPY; v3-v5 create
the 73 deferred non-unique operational indexes and v6 creates five GIN indexes.
Validate all FKs and analyze before setting the forward checkpoint atomically to H0.

- [ ] **Step 6: Run E2E copy tests including a frozen historical SQLite fixture**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/shadow/test_snapshot.py \
  backend/tests/postgres/shadow/test_bulk_copy.py
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/migration/shadow/{snapshot,transform,bulk_copy}.py \
  backend/tests/shadow/test_snapshot.py backend/tests/postgres/shadow/test_bulk_copy.py
git commit -m "feat: copy a consistent SQLite baseline to PostgreSQL"
```

### Task 5: Implement the fail-stop forward replicator and checkpoint protocol

**Files:**
- Create: `backend/app/migration/shadow/replicator.py`
- Create: `backend/app/migration/shadow/metrics.py`
- Create: `backend/tests/postgres/shadow/test_forward_replicator.py`
- Create: `backend/tests/postgres/shadow/test_replicator_crashes.py`

**Interfaces:**

```python
class ShadowReplicator:
    def run_batch(self, *, run_id: str, direction: Direction,
                  max_events: int, max_bytes: int) -> BatchResult: ...
    def run_forever(self, *, run_id: str, direction: Direction,
                    stop: threading.Event) -> None: ...
```

- [ ] **Step 1: Write operation/order/idempotency tests**

Cover insert/update/delete, replication-key update, repeated key coalescence without reordering delete→upsert semantics, cascade events, current-row hydration, a row already deleted by a later event, exact checkpoint continuity, duplicate replay, and no source mutation.

- [ ] **Step 2: Add crash injection at every transaction boundary**

Inject before first target statement, mid-batch, before checkpoint update, after checkpoint update/before commit, and after commit/before next source read. Assert either no target effect/checkpoint or both target/checkpoint; rerun converges.

- [ ] **Step 3: Implement bounded sequential apply**

Read contiguous events after checkpoint; reject a gap, wrong run/epoch/table/op/replication-key shape. Hydrate source rows using manifest-bound SQL. In one target transaction apply statements in seq order and advance checkpoint to the last contiguous seq. Size batches by event count and hydrated bytes.

- [ ] **Step 4: Implement error taxonomy**

Retry connection loss, pool timeout, deadlock, serialization failure, lock timeout, and statement timeout with capped exponential backoff/jitter. Record conversion/schema/constraint/identity errors as one poison event and stop. Never advance past poison and expose no skip option.

- [ ] **Step 5: Export structured metrics**

Emit run/direction/checkpoints/lag/batch rows/bytes/duration/retries/poison count with redacted identities. Do not label metrics with table replication keys or user data.

- [ ] **Step 6: Run tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_forward_replicator.py \
  backend/tests/postgres/shadow/test_replicator_crashes.py
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/migration/shadow/{replicator,metrics}.py \
  backend/tests/postgres/shadow/test_forward_replicator.py \
  backend/tests/postgres/shadow/test_replicator_crashes.py
git commit -m "feat: replicate SQLite changes to PostgreSQL"
```

### Task 6: Implement barrier-aware structural, domain, embedding, and retrieval verification

**Files:**
- Create: `backend/app/migration/shadow/verifier.py`
- Create: `backend/tests/shadow/test_normalization.py`
- Create: `backend/tests/postgres/shadow/test_verifier.py`
- Create: `backend/tests/fixtures/shadow_retrieval_golden.json`

**Interfaces:**

```python
class VerificationLevel(StrEnum):
    STRUCTURAL = "structural"
    FULL = "full"
    CUTOVER = "cutover"

def verify(run_id: str, level: VerificationLevel) -> VerificationReport: ...
```

**Barrier algorithm:**

1. Open a SQLite read snapshot, record `Hv`, and stream source facts/hashes.
2. Wait for PG checkpoint ≥ `Hv`.
3. Open a PostgreSQL `REPEATABLE READ, READ ONLY` transaction; read its applied checkpoint `Ht` and pin target state.
4. In a new SQLite read transaction collect every dirty replication key with `seq > Hv` through its current `Hseen`. A change committed after this collection cannot have influenced the already pinned PG snapshot; a future state hydrated into PG before the snapshot necessarily has a visible dirty event by `Hseen`.
5. Exclude those concurrent keys from strict drift; compare all stable keys. Save the barrier until report commit so retention cannot cross it.
6. `CUTOVER` additionally requires the source frozen, `Hv=Ht=source MAX(seq)`, zero concurrent keys, and 100% coverage.

- [ ] **Step 1: Test canonical normalization**

Cover ordered composite replication-key JSON, JSON key ordering/null/NaN/Infinity, timezone instants, booleans, text bytes, bytea length/hash, float32 dimension/norm/cosine tolerance, and paths relative to storage root.

- [ ] **Step 2: Test active-write barriers deterministically**

Use explicit barriers to commit changes before/after each verifier step. Assert no false drift for concurrent keys, no missed stable drift, and retention protection while verification is active.

- [ ] **Step 3: Implement verification layers**

Structural: schema pair, row counts, stable replication-key sets, chunked normalized hashes, FKs/uniques/cascades, file references. For ordinary tables this is the declared-PK set check; for the three reviewed exceptions it is the guarded logical-key set check.

Full: selected repository contract reads against both adapters, embedding invariants, mixed Chinese/English retrieval golden set.

Cutover: all full checks plus frozen/caught-up/zero-concurrent/100% and two consecutive complete reports.

- [ ] **Step 4: Store safe reports**

Persist run/table/replication-key hash or chunk bounds/category/redacted summary. Never store raw source/Memory/token/password text. Differences stay red until a later verified run supersedes them; there is no manual green flag.

- [ ] **Step 5: Enforce quality gates**

Recall@12 loss ≤1 percentage point, top-10 overlap ≥0.90, deterministic citation/source ID sets 100%, embeddings dimension/bytes and sampled cosine within the recorded tolerance.

- [ ] **Step 6: Run tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/shadow/test_normalization.py \
  backend/tests/postgres/shadow/test_verifier.py
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/migration/shadow/verifier.py backend/tests/shadow/test_normalization.py \
  backend/tests/postgres/shadow/test_verifier.py \
  backend/tests/fixtures/shadow_retrieval_golden.json
git commit -m "feat: verify PostgreSQL shadow consistency"
```

### Task 7: Add safe retention, the thin CLI, and a supervised forward worker

**Files:**
- Create: `backend/app/migration/shadow/retention.py`
- Create: `backend/app/migration/shadow/cli.py`
- Create: `backend/app/migration/shadow/worker.py`
- Create: `scripts/migrate_sqlite_to_postgres.py`
- Create: `scripts/shadow.sh`
- Create: `backend/tests/shadow/test_cli.py`
- Create: `backend/tests/shadow/test_retention.py`
- Create: `backend/tests/shadow/test_worker_lease.py`

**CLI delivered in this phase:**

```text
status --run-id RUN
preflight --run-id RUN [--prepare-target]
start-forward --run-id RUN --work-dir PATH
verify --run-id RUN --level structural|full
worker --run-id RUN --direction forward
```

- [ ] **Step 1: Write parser/state/error/redaction tests**

Assert every mutating command requires an explicit run id and confirmation token from preflight, JSON output is machine-readable, human output is redacted, illegal phase/direction fails, and unknown checkpoint/poison cannot be forced.

- [ ] **Step 2: Write worker lease tests**

Only one live forward worker may hold the direction lease. Heartbeats use database time, leases expire conservatively, and takeover is rejected while an existing worker is healthy. SIGTERM finishes or rolls back the current batch and releases/lets expire the lease.

- [ ] **Step 3: Implement `start-forward` orchestration**

It validates preflight, commits the SQLite and PostgreSQL guard/install interfaces independently while capture remains disabled, enables capture only through the two-report CAS, creates the snapshot, prepares PG v2, copies/resumes the baseline, reseeds all seven ordinal identities, migrates through resumable v3-v6 index groups with the configured bounded timeout, sets H0, transitions to `sqlite_to_postgres`, and prints the exact foreground worker command. Any guard, reseed, or index-group failure remains visible and cannot mark COPY complete or advance H0. It does not daemonize invisibly.

- [ ] **Step 4: Implement `scripts/shadow.sh` as an optional local supervisor**

Store PID, redacted status, and logs under a run-specific `.local/shadow/RUN/` directory with `0700`/`0600` permissions. Validate the PID command before stop; never kill a reused PID or use a broad pattern. Production docs prefer systemd/container supervision of the same foreground worker command.

- [ ] **Step 5: Implement safe retention**

Default 7 days and at least 100,000 newest events. Delete only rows satisfying all five bounds from the design: verified checkpoint, age, tail count, oldest active verifier barrier, oldest replay/rollback checkpoint. Poison and later audit material are retained.

- [ ] **Step 6: Run tests and CLI help**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 backend/tests/shadow
${PYTHON_BIN:-python3} scripts/migrate_sqlite_to_postgres.py --help
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/migration/shadow/{retention,cli,worker}.py \
  scripts/migrate_sqlite_to_postgres.py scripts/shadow.sh backend/tests/shadow
git commit -m "feat: operate forward PostgreSQL shadow sync"
```

### Task 8: Prove the complete forward-shadow path and document operation

**Files:**
- Create: `backend/tests/postgres/shadow/test_forward_e2e.py`
- Modify: `scripts/check_postgres.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `packaging/DEPLOY.md`
- Modify: `backend/tests/test_architecture_documentation.py`

- [ ] **Step 1: Add the E2E scenario**

Migrate a frozen historical SQLite fixture to current v24; start capture; create a baseline while writes continue; COPY; run forward worker; exercise identity/notebook/source/Ask/Knowledge/Memory/Knowhow/report/share/delete through the SQLite formal repository; wait caught up; run two full verifications. Inject one target outage and prove retry/catch-up.

- [ ] **Step 2: Add architecture guards**

AST tests allow dual-adapter imports only under `migration/shadow`; reject `SHADOW_DATABASE_URL` reads elsewhere; reject shadow calls from application services/stores/routes; assert the CLI entry is thin and manifest totality remains enforced.

- [ ] **Step 3: Extend only the PostgreSQL lane**

Run shadow E2E serially after adapter tests. Keep the default offline gate unchanged.

- [ ] **Step 4: Synchronize docs**

Document preparation, disk sizing, `preflight`, `start-forward`, foreground/systemd worker operation, `status`, verification interpretation, poison fail-stop, retention, backups, and the explicit statement: **at this phase SQLite is still the only live database; do not change `DATABASE_URL`.** Link the formal cutover plan.

- [ ] **Step 5: Run gates**

```bash
PYTHON_BIN=${PYTHON_BIN:-python3} bash scripts/check.sh
cd frontend && npm run build
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHON_BIN=${PYTHON_BIN:-python3} \
  bash scripts/check_postgres.sh
```

Expected: all PASS; `scripts/check.sh` succeeds with PostgreSQL absent.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/postgres/shadow/test_forward_e2e.py scripts/check_postgres.sh \
  .github/workflows/ci.yml README.md README_zh.md AGENTS.md architecture.md \
  packaging/DEPLOY.md backend/tests/test_architecture_documentation.py
git commit -m "test: prove forward PostgreSQL shadow synchronization"
```

---

## Phase acceptance gate

- [ ] Formal `DATABASE_URL` is still SQLite and all production business reads/writes still use only SQLite.
- [ ] PostgreSQL contains a complete baseline plus continuously applied changes; the worker is caught up for at least 60 seconds.
- [ ] Two independent full verifier runs pass with no poison event or unexplained stable-key difference.
- [ ] Capture tests cover every manifest table and transaction rollback; change-log rows contain no business payload/BLOB.
- [ ] Crash tests prove target apply + checkpoint atomicity and idempotent replay.
- [ ] Manifest/schema totality tests fail on any unclassified new table.
- [ ] `scripts/check.sh`, frontend build, and PostgreSQL integration/shadow E2E all pass.
- [ ] Request code review with `superpowers:requesting-code-review`, resolve all Critical/Important findings, and rerun all affected gates.

The next plan is `docs/superpowers/plans/2026-07-22-postgresql-cutover-and-rollback.md`. Do not freeze or switch production as part of this phase.
