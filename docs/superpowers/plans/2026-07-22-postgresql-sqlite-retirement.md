# PostgreSQL SQLite Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After PostgreSQL has proved stable for the full observation window, irreversibly declare it the only authority, preserve a final audited SQLite snapshot and a standalone legacy import tool, then remove SQLite and shadow replication from the formal runtime and primary test architecture.

**Architecture:** Retirement has two separately reviewed releases. Release A adds evidence-backed retirement planning and performs a brief final freeze: reverse sync catches up, SQLite verifies, a final snapshot is sealed, reverse capture stops, and the run becomes `retired`. Release B starts only after that operational event is approved; it moves a minimal read-only legacy reader into `app/tools/sqlite_import`, deletes the dual-backend/shadow runtime, simplifies the formal repository factory to PostgreSQL only, and converts the supported test/deployment path to PostgreSQL.

**Tech Stack:** Python 3.13, PostgreSQL 17/psycopg 3, sqlite3 only inside the offline legacy-import tool, pytest, JSON evidence plans with SHA-256 handshakes, existing readiness/maintenance UI, GitHub Actions PostgreSQL service.

## Global Constraints

- Do not start this plan merely because cutover succeeded. Release A requires the exact 28-day and quantitative gates below; Release B requires a completed, explicitly approved `retired` operation in production.
- Use `codex/postgresql-retirement-gate` for Release A and a later `codex/postgresql-only-runtime` branch/worktree for Release B. Do not combine the irreversible operation and code deletion in one unreviewed deployment.
- TDD every evidence gate, rejection, importer behavior, deletion boundary, and PostgreSQL-only contract.
- A failing/missing/stale/manual evidence item fails closed and resets or blocks continuous observation as specified. CLI flags cannot waive a gate.
- `retire` never deletes a SQLite DB or backup. It seals and records them. Material deletion is a later human retention action outside this code plan.
- Once retired, SQLite is not a supported formal backend, shadow is not a dormant feature flag, and normal app code must not import sqlite3/SQLite adapters.
- The optional legacy importer is read-only toward its SQLite source, cannot serve API requests, cannot be selected by `DATABASE_URL`, and imports only into an explicitly empty/approved PostgreSQL target.
- pgvector and multi-worker remain independent projects; removal of SQLite must not delete current hnswlib/scale/viz functionality.
- Update README/README_zh/AGENTS/architecture, packaging, CI, and setup together. Do not mislabel infrastructure as a completed product-spec feature.

---

## Normative retirement gates

The earliest eligible instant is after **28 continuous days** in `postgres_to_sqlite` with PostgreSQL writes open. Any poison event, unexplained verifier drift, failed critical path, failed backup/restore/schema-migration/rollback drill, or breach of a required SLO restarts the continuous timer.

All of the following must be true at plan generation and revalidated at apply time:

- reverse outbox has no unconfirmed event and SQLite receipts/verifier agree, with no poison event or unexplained difference;
- DB-only p95 ≤ `max(sqlite_baseline_p95 × 1.20, 50ms)`;
- DB-only p99 ≤ `max(sqlite_baseline_p99 × 1.30, 100ms)`;
- pool wait p99 < 50ms;
- most recent continuous 24h has zero pool-acquisition timeout and zero unhandled deadlock/serialization failure;
- measured growth projected 90-day PostgreSQL disk usage <70%;
- every critical API, MCP, job, share/copy/join, delete cascade, Memory/Knowhow concurrency path has successful PostgreSQL runtime evidence;
- latest PostgreSQL backup/restore drill, schema migration drill, and full PG→SQLite rollback drill passed;
- an operator and a separate reviewer approve the reviewed retirement plan checksum.

---

### Task 1: Collect bounded, tamper-evident stability and critical-path evidence

**Files:**
- Create: `backend/app/migration/shadow/stability.py`
- Modify: `backend/app/migration/shadow/sql/postgres_control.sql`
- Modify: `backend/app/migration/shadow/metrics.py`
- Modify: `backend/app/migration/shadow/cli.py`
- Create: `backend/tests/postgres/shadow/test_stability_evidence.py`

**Interfaces:**

```python
class EvidenceKind(StrEnum):
    PERFORMANCE_SAMPLE = "performance_sample"
    CRITICAL_PATH = "critical_path"
    BACKUP_RESTORE_DRILL = "backup_restore_drill"
    SCHEMA_MIGRATION_DRILL = "schema_migration_drill"
    ROLLBACK_DRILL = "rollback_drill"

@dataclass(frozen=True)
class StabilityDecision:
    eligible: bool
    continuous_since: datetime | None
    earliest_retire_at: datetime | None
    passed: tuple[GateResult, ...]
    failed: tuple[GateResult, ...]
```

- [ ] **Step 1: Write gate arithmetic/window tests first**

Cover both latency threshold branches, percentile sample bounds, missing intervals, clock/timezone normalization, 24h error window, 28-day reset events, disk projection with insufficient/negative/step-change data, critical-path freshness, and stale drill receipts. Use database timestamps and monotonic sequence/revision facts, never client wall time alone.

- [ ] **Step 2: Add owned evidence tables and bounded retention**

Store aggregate performance/pool/disk samples and credential-free drill/path receipts in `silicon_shadow`; do not store request/source/Memory bodies or high-cardinality IDs. Hash artifact files and link run/build/schema versions. Retain enough evidence to cover the entire observation window plus audit retention.

- [ ] **Step 3: Instrument DB-only and pool metrics**

Measure repository transaction/query time excluding model/network/file parsing, pool wait, acquisition timeout, handled/unhandled retry categories, and database size. Persist minute/hour aggregates with count/p50/p95/p99/max; low sample counts fail the gate.

- [ ] **Step 4: Record critical-path evidence from real execution**

Map stable path IDs to the existing API/MCP/job/concurrency tests and production canary probes. A path receipt includes build SHA, PostgreSQL identity hash, run id, time, pass/fail, and artifact hash. No free-form “mark passed” command exists; receipts come from the signed test/probe runner.

- [ ] **Step 5: Run focused tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_stability_evidence.py
```

- [ ] **Step 6: Commit Release A evidence collection**

```bash
git add backend/app/migration/shadow/{stability,metrics,cli}.py \
  backend/app/migration/shadow/sql/postgres_control.sql \
  backend/tests/postgres/shadow/test_stability_evidence.py
git commit -m "feat: collect PostgreSQL retirement evidence"
```

### Task 2: Add reviewed retirement-plan/apply handshake and final sealed snapshot

**Files:**
- Create: `backend/app/migration/shadow/retirement.py`
- Modify: `backend/app/migration/shadow/cli.py`
- Create: `backend/tests/postgres/shadow/test_retirement_plan.py`
- Create: `backend/tests/postgres/shadow/test_retirement_apply.py`
- Create: `docs/operations/postgresql-retirement-runbook.md`

**Commands:**

```text
retire --run-id RUN --plan PATH
retire --run-id RUN --apply --plan PATH --operator NAME --reviewer NAME
```

The JSON plan contains run/build/schema identities, active/formal backend proof, every gate result/value/window/artifact hash, current waterlines, expected final actions, output snapshot path, creation/expiry timestamps, and its own canonical SHA-256. It contains no credentials or private content.

- [ ] **Step 1: Write fail-closed plan/apply tests**

Reject <28 days, a timer reset, threshold equality/breach cases, missing samples, lag/poison/drift, stale drill, same operator/reviewer, blank identity, plan edit/checksum mismatch, expired plan, changed build/schema/run/config/waterline, already retired, and an active unknown writer.

- [ ] **Step 2: Implement read-only plan generation**

Generating a plan must not freeze, mutate phase, stop capture, or touch data. It snapshots evidence and prints pass/fail plus earliest eligible time. An ineligible plan cannot be applied.

- [ ] **Step 3: Implement final retirement freeze**

On apply, revalidate all evidence; drain/freeze PostgreSQL; record final reverse waterline; catch SQLite up for ≥60 seconds; run cutover-level verifier; create/restore-test PostgreSQL backup; create a final SQLite backup with SHA-256, size, schema/run/checkpoint and `0600` permissions; verify storage references. Any failure reopens PostgreSQL writes with reverse replication intact and leaves phase unchanged.

- [ ] **Step 4: Make the irreversible transition explicit**

After artifacts pass, disable reverse capture, stop/release reverse worker, mark phase `retired`, record operator/reviewer/plan checksum/final artifacts, then reopen PostgreSQL writes. Do not delete source SQLite, shadow tables, or code in this operation. Emit a prominent message that lossless SQLite rollback is no longer maintained.

- [ ] **Step 5: Run apply fault injection tests**

Inject failures before/after freeze, catch-up, verify, each backup, capture disable, state commit, and reopen. Assert the only successful terminal ordering is sealed snapshot → capture stopped → `retired` → PG open.

- [ ] **Step 6: Document and rehearse Release A**

Run the exact plan/apply flow twice on disposable production-shaped data with two test identities. Record artifacts and prove the final SQLite backup restores read-only.

- [ ] **Step 7: Commit**

```bash
git add backend/app/migration/shadow/{retirement,cli}.py \
  backend/tests/postgres/shadow/test_retirement_*.py \
  docs/operations/postgresql-retirement-runbook.md
git commit -m "feat: retire SQLite with reviewed evidence"
```

### Operational checkpoint: perform and approve retirement before Release B

- [ ] Deploy Release A while reverse replication remains healthy.
- [ ] Accumulate/review the complete observation evidence.
- [ ] Generate the plan; operator and separate reviewer sign the checklist/checksum.
- [ ] Take an additional external PostgreSQL backup and confirm restore.
- [ ] Execute `retire --apply` in the approved window.
- [ ] Confirm phase `retired`, PostgreSQL write open, reverse capture/worker stopped, final SQLite snapshot hash/restore valid.
- [ ] Obtain explicit written approval to remove the formal SQLite/shadow runtime.

Stop here if any item is incomplete. Release B is not a normal continuation without this external-state evidence.

### Task 3: Isolate a minimal read-only legacy SQLite importer before deleting adapters

**Files:**
- Create: `backend/app/tools/sqlite_import/__init__.py`
- Create: `backend/app/tools/sqlite_import/reader.py`
- Create: `backend/app/tools/sqlite_import/manifest.py`
- Create: `backend/app/tools/sqlite_import/transform.py`
- Create: `backend/app/tools/sqlite_import/importer.py`
- Create: `scripts/import_legacy_sqlite_backup.py`
- Create: `backend/tests/tools/test_legacy_sqlite_import.py`
- Copy only reviewed historical schema fixtures into `backend/tests/fixtures/legacy_sqlite_import/`

**Interfaces:**

```python
def inspect_legacy_backup(path: Path) -> LegacyBackupReport: ...
def import_legacy_backup(*, source: Path, target_url: str,
                         plan: Path, apply: bool = False) -> ImportReport: ...
```

- [ ] **Step 1: Write read-only/source-safety tests**

Open SQLite with URI `mode=ro&immutable=1`, construct no `SqliteDatabase`/write repository, and prove file bytes/mtime/hash do not change on inspect, dry run, successful import, or failure. Reject live WAL/shm ambiguity unless supplied as a sealed backup, path symlinks/out-of-root artifacts, unsupported future schema, corrupt/incomplete backups, and credential output.

- [ ] **Step 2: Write target-safety/idempotency tests**

Require an explicitly empty PostgreSQL import target or a newly allocated import namespace; reject the live production database identity unless an independently reviewed import plan names it. Preview and apply share one checksummed plan. Crash/retry must not duplicate rows; validate all tables, FKs, embeddings, files, and retrieval quality before marking import complete.

- [ ] **Step 3: Move—not import—the minimal logic**

Copy the frozen manifest/transforms/reader needed for supported legacy schema versions into `app/tools/sqlite_import`. It must not import `app.migration.shadow`, the removed SQLite repository, API, or runtime Settings/factory. PostgreSQL target writes use a dedicated import adapter/service with transactions and explicit target ownership.

- [ ] **Step 4: Test with oldest/current sealed fixtures**

Upgrade/transform from each supported historical fixture directly in the importer and compare with the final PostgreSQL repository contract. Unknown versions fail with an actionable offline migration message.

- [ ] **Step 5: Commit on Release B branch**

```bash
git add backend/app/tools/sqlite_import scripts/import_legacy_sqlite_backup.py \
  backend/tests/tools backend/tests/fixtures/legacy_sqlite_import
git commit -m "feat: isolate legacy SQLite backup import"
```

### Task 4: Remove SQLite/shadow from the formal runtime and require PostgreSQL

**Files:**
- Delete: `backend/app/migration/shadow/`
- Delete: `backend/app/repositories/sqlite/`
- Delete: `backend/app/services/sqlite_repository.py`
- Delete: `scripts/migrate_sqlite_to_postgres.py`
- Delete: `scripts/shadow_sqlite_to_postgres.py`
- Delete: `scripts/shadow.sh`
- Modify: `backend/app/repositories/factory.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/api/deps.py`
- Modify: startup/readiness/maintenance API and frontend banner to remove migration phases while retaining generic planned-maintenance support if otherwise useful
- Modify: all production and test imports found by `rg 'SQLiteRepository|SqliteDatabase|repositories\.sqlite|migration\.shadow|shadow_database_url|sqlite3' backend`
- Create: `backend/tests/test_postgresql_only_architecture.py`

**Final factory:**

```python
def create_repository(settings: Settings) -> NotebookRepository:
    identity = database_identity(settings.database_url)
    if identity.scheme != "postgresql":
        raise ValueError("silicon-notebook requires PostgreSQL")
    return PostgresRepository(settings)
```

- [ ] **Step 1: Add the PostgreSQL-only architecture test and observe failures**

Reject SQLite schemes in runtime Settings. Reject `sqlite3`, `SQLiteRepository`, `SqliteDatabase`, `repositories.sqlite`, `migration.shadow`, or `SHADOW_DATABASE_URL` imports/references anywhere under `backend/app` except `backend/app/tools/sqlite_import`. Reject any backend/dialect selection branch outside the now-single factory.

- [ ] **Step 2: Convert all supported tests to PostgreSQL fixtures**

Replace direct SQLite repository construction with the shared formal repository fixture. Give each parallel test worker/test a unique PostgreSQL schema and isolated storage root; clean only validated test schemas. Preserve every current behavior assertion, historical fixture import coverage, concurrency test, MCP smoke, and frontend contract. Do not delete tests merely because they were SQLite-backed.

- [ ] **Step 3: Remove runtime code in one mechanical commit**

Delete shadow and SQLite repository packages/scripts only after tests compile against PG. Simplify Settings to require `postgresql://`; remove `sqlite_path` and `shadow_database_url` from runtime. Keep Python `sqlite3` imports only in the isolated importer.

- [ ] **Step 4: Remove migration-only UI/state**

The global database-cutover banner and phases disappear after the release is fully PG-only; retain a backend-neutral maintenance banner only if other operations use it. Remove dead CLI help, config keys, metrics, packaging units, and control-schema bootstrap. Do not drop the production `silicon_shadow` schema automatically from application startup.

- [ ] **Step 5: Run targeted architecture/import tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_postgresql_only_architecture.py \
  backend/tests/tools/test_legacy_sqlite_import.py \
  backend/tests/test_repository_dependency_contract.py \
  backend/tests/test_repository_api_contract.py
```

- [ ] **Step 6: Commit**

```bash
git add -A backend/app backend/tests scripts frontend/app
git commit -m "refactor: make PostgreSQL the only formal repository"
```

### Task 5: Make PostgreSQL the primary full gate and synchronize final operations/docs

**Files:**
- Modify: `scripts/check.sh`
- Modify: `scripts/check_postgres.sh` (merge/remove after callers migrate)
- Modify: `.github/workflows/ci.yml`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/pytest.ini`
- Modify: `.env.example`
- Modify: `scripts/backend.sh`
- Modify: `scripts/prod.sh`
- Modify: `packaging/start.sh`
- Modify: `packaging/DEPLOY.md`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `backend/tests/test_architecture_documentation.py`

- [ ] **Step 1: Add final setup/CI contract tests**

Assert the primary backend/full gate has a PostgreSQL service URL, CI provides PostgreSQL 17 before the backend lane, `.env.example` has no SQLite/shadow formal config, scripts print only redacted PG identity, production remains one worker, and docs describe PostgreSQL backup/migration/pool operations plus the isolated legacy importer.

- [ ] **Step 2: Promote PostgreSQL integration to the main backend gate**

Require `TEST_POSTGRES_URL` (or a documented local disposable PostgreSQL service) for DB-backed tests. Preserve a network-offline guarantee: tests must not access external internet, MinerU, or model endpoints. CI starts a local PostgreSQL service. If retaining a quick no-DB unit lane, it supplements rather than replaces the full PG gate.

- [ ] **Step 3: Update developer/deployment setup**

README/README_zh include exact PostgreSQL create-role/create-database/migrate/start/check commands, connection/pool settings, backup/restore, schema migration policy, and legacy import dry-run/apply plan handshake. AGENTS states PostgreSQL is the sole supported backend and bans reintroducing SQLite runtime code. Architecture removes transitional diagrams and shows only PG plus local file/vector artifacts.

- [ ] **Step 4: Document post-code cleanup of shadow DB objects**

Provide a separate reviewed SQL/CLI procedure to archive audit rows, verify the final SQLite artifact, then drop the `silicon_shadow` PostgreSQL schema. Do not run that drop from startup/migrations or this code change. Record artifact retention/ownership and recovery limitations.

- [ ] **Step 5: Run the final full gate twice**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHON_BIN=${PYTHON_BIN:-python3} \
  bash scripts/check.sh
cd frontend && npm run build
```

Expected: two consecutive PASS runs against clean PostgreSQL schemas, plus a PASS legacy-import test from the sealed SQLite fixture.

- [ ] **Step 6: Confirm no forbidden references**

```bash
rg -n 'SQLiteRepository|SqliteDatabase|repositories\.sqlite|migration\.shadow|SHADOW_DATABASE_URL|sqlite:///' \
  backend/app frontend scripts README.md README_zh.md AGENTS.md architecture.md packaging \
  -g '!backend/app/tools/sqlite_import/**'
```

Expected: no runtime/config/documentation references except clearly labeled historical retirement/import material where intentionally retained.

- [ ] **Step 7: Commit**

```bash
git add scripts .github backend frontend .env.example README.md README_zh.md \
  AGENTS.md architecture.md packaging
git commit -m "docs: complete the PostgreSQL-only transition"
```

---

## Final acceptance gate

- [ ] Production shadow run is durably `retired`; final PG backup and sealed SQLite snapshot hashes/restores are independently verified.
- [ ] Formal runtime, API, jobs, MCP, packaging, and primary tests construct only `PostgresRepository`.
- [ ] No production code imports SQLite/shadow; the sole exception is the standalone read-only legacy importer.
- [ ] PostgreSQL is the primary full-gate backend and all previous behavior/concurrency assertions remain covered.
- [ ] `DATABASE_URL` accepts only PostgreSQL; `SHADOW_DATABASE_URL` and migration state are gone from runtime configuration.
- [ ] hnswlib/scale/viz remain intact until a separate pgvector project passes its own retrieval gates.
- [ ] README/README_zh/AGENTS/architecture/packaging are synchronized and describe the final, non-transitional architecture.
- [ ] Request code review with `superpowers:requesting-code-review`; resolve every Critical/Important finding and rerun both exact-head full gates.

After this plan, SQLite is not a selectable backend. Restoring an old SQLite backup is an explicit offline import into PostgreSQL, not a runtime switch.
