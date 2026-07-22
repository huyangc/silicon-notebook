# PostgreSQL Cutover and Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the formal `silicon-notebook` repository from SQLite to PostgreSQL through a short, observable read-only window, keep SQLite continuously recoverable through reverse replication, and provide a rehearsed lossless rollback with an exact operator command sequence.

**Architecture:** A backend-neutral write-admission component and database-level triggers provide layered freeze enforcement. The cutover state machine is the only component allowed to move authority. On single-host deployments, it atomically swaps the raw `DATABASE_URL` and `SHADOW_DATABASE_URL` assignments in `.env` while the backend is stopped; secret-manager deployments use a credential-free activation manifest and explicit confirmation. PostgreSQL stays frozen after restart until read smoke passes, reverse capture is enabled, a reverse worker is healthy, and `resume-writes` opens it. During observation PostgreSQL is the sole authority and SQLite accepts only reverse-applier transactions guarded by an in-transaction `apply_active` lease.

**Tech Stack:** Python 3.13, FastAPI middleware/dependencies, sqlite3 triggers, psycopg 3 triggers/transactions, atomic POSIX file replacement/fsync, pytest fault injection, Next.js readiness/maintenance UI, existing shell supervisors.

## Global Constraints

- Depends on both prior plans being merged and on a healthy forward shadow run with two clean full verifications.
- Use a dedicated `codex/postgresql-cutover-rollback` branch/worktree for code. Production execution is a separate explicitly approved operational event.
- Follow `docs/superpowers/specs/2026-07-22-postgresql-shadow-cutover-design.md` and TDD every transition/failure path.
- At every instant there is at most one business-writable backend. There is never dual-master or application dual-write.
- Phase/authority/write-gate transitions fail closed. No `--force`, skip-event, future-checkpoint, or “ignore verifier” option is permitted.
- Before `resume-writes`, a failed PostgreSQL activation may switch back without reverse catch-up because PostgreSQL has accepted no business writes. After `resume-writes`, changing `.env` alone is forbidden; rollback must drain reverse replication and verify SQLite.
- `DATABASE_URL` is the sole formal backend selector; `SHADOW_DATABASE_URL` is the other endpoint. Business factory code does not inspect phase or open the shadow URL.
- Do not print, log, persist in control tables, or place in process arguments either raw URL. Activation receipts contain only run id, redacted identities, file/config hashes, phases, and timestamps.
- The backend remains single-worker. pgvector and other retrieval/storage changes are excluded.
- Original files/storage root are shared. Reverse replication changes only database rows; it never uploads/deletes a file a second time.
- Maintenance is user-visible: reads remain usable; new writes receive a stable 503 code/message and are not endlessly retried.
- Update README/README_zh/AGENTS/architecture, frontend and backend in one change; do not update `fangan_done.md` as though this were a product feature.

---

## Authority matrix (normative)

| State | Formal backend | SQLite business writes | PostgreSQL business writes | Replication |
|---|---|---:|---:|---|
| `sqlite_to_postgres` | SQLite | open | frozen/not served | forward SQLite→PG |
| `cutover_readonly` before swap | SQLite | frozen | frozen | forward draining, then stopped |
| `cutover_readonly` after swap | PostgreSQL | frozen | frozen | none until reverse prepared |
| `postgres_to_sqlite` before `resume-writes` | PostgreSQL | applier lease only | frozen | reverse worker healthy |
| `postgres_to_sqlite` after `resume-writes` | PostgreSQL | applier lease only | open | reverse PG→SQLite |
| rollback `cutover_readonly` | PostgreSQL then SQLite | frozen | frozen | reverse drains, then stops |
| rollback complete `off` | SQLite | open | frozen/not served | none; new attempt needs new run |

Every command and startup path must assert this matrix from independent facts: active URL
identity, run phase, capture direction, forward checkpoint or reverse outbox/receipt state,
formal database durable gate, and worker lease/heartbeat.

---

## Target file map

- Create `backend/app/core/write_admission.py` and `backend/app/api/maintenance_routes.py`.
- Modify `backend/app/repositories/sqlite/database.py` and `backend/app/repositories/postgres/database.py` to consult injected admission state before business write transactions.
- Extend shadow package with `admission.py`, `activation.py`, `backup.py`, `smoke.py`, `reverse_capture.py`, and rollback/control commands.
- Add SQLite v25 shadow operation/actor/reverse-event-receipt tables; keep these migration-only internals removable later.
- Modify readiness/system API and frontend system API/banner/mutation transport behavior.
- Extend `scripts/migrate_sqlite_to_postgres.py` and `scripts/shadow.sh` for freeze, backup, cutover, activation confirmation, reverse, resume, smoke, and rollback.

---

### Task 1: Centralize write admission and expose a user-visible maintenance state

**Files:**
- Create: `backend/app/core/write_admission.py`
- Create: `backend/app/api/maintenance_routes.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/readiness.py`
- Modify: `backend/app/services/startup_warmup.py`
- Modify: `backend/app/repositories/sqlite/database.py`
- Modify: `backend/app/repositories/sqlite/migrations.py`
- Modify: `backend/app/repositories/postgres/database.py`
- Modify: `backend/app/repositories/postgres/schema_manifest.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/api/mcp_server.py`
- Modify: `backend/app/services/background_jobs.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/ask_execution.py`
- Modify: `backend/app/services/report_execution.py`
- Modify: `backend/app/services/knowhow/projection.py`
- Modify: `frontend/app/system-api.ts`
- Modify: `frontend/app/api-client.ts`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/globals.css`
- Create: `backend/tests/test_write_admission.py`
- Create: `backend/tests/test_maintenance_routes.py`
- Create: `frontend/app/maintenance-mode.test.mjs`

**Interfaces:**

```python
class WriteMode(StrEnum):
    OPEN = "open"
    DRAINING = "draining"
    FROZEN = "frozen"

@dataclass(frozen=True)
class WriteAdmissionSnapshot:
    mode: WriteMode
    phase: ShadowPhase
    active_backend: Literal["sqlite", "postgresql"]
    revision: int
    reason: str | None
    retry_after_seconds: int | None

class WriteAdmission:
    def snapshot(self, *, fresh: bool = False) -> WriteAdmissionSnapshot: ...
    def require_open(self, operation: str) -> None: ...
    @contextmanager
    def track(self, operation: str) -> Iterator[None]: ...
```

Maintenance API response:

```json
{
  "write_mode": "frozen",
  "phase": "cutover_readonly",
  "active_backend": "sqlite",
  "reason": "database_cutover",
  "retry_after_seconds": 60
}
```

- [ ] **Step 1: Write failing admission coverage tests**

Cover mutating HTTP methods, Ask stream/final-save, uploads/deletes, MCP `propose_memory` and `put_knowhow_cell_code`, ingestion/KG/projection/report/background claims, offline direct repository writes, and session resolution. Reads, readiness, health, source detail, search, Ask-history reads, Knowledge/Memory/Knowhow/report reads remain available.

Auth sliding-expiry touch is skipped during freeze but a valid existing token still authenticates; no read request fails merely because a maintenance touch cannot write.

- [ ] **Step 2: Add frontend failing tests**

Assert a top-level banner for draining/frozen, stable Chinese copy identifying the active backend and read-only state, mutation controls disabled where globally available, and API client handling of error code `database_maintenance` without automatic mutation retry. Do not hide read navigation.

- [ ] **Step 3: Implement one admission component**

The formal repository/database receives `WriteAdmission` by injection. Every business `database.write()` calls `require_open()` immediately before transaction begin. Request/job wrappers call `track()` around the entire mutation so freeze can drain file work as well as SQL. Do not add per-service phase conditionals.

Shadow control/replication uses separately constructed maintenance connections; it cannot obtain a bypass from the formal repository.

Installing SQLite v25 updates the accepted compatibility pair to `(sqlite=25,
postgres=2, epoch=1)`; the business schema epoch remains unchanged. Startup and every
shadow command reject the earlier v24 pair once this release is deployed.

- [ ] **Step 4: Add v25 durable operation state and actor heartbeats**

Create `shadow_operation_control(mode, revision, reason, changed_at)` plus `shadow_actor_heartbeats(actor_id, actor_kind, pid/instance, accepting_writes, inflight, last_seen)`. The single API process and every separately supervised write-capable worker register/heartbeat. A stale heartbeat is not silently considered drained; `freeze` requires the actor be explicitly stopped/expired under the reviewed TTL policy.

- [ ] **Step 5: Implement maintenance route/readiness behavior**

Readiness may be `ready=true` while `write_mode!=open`. Add a `maintenance` detail/state without gating reads. Every blocked mutation returns 503 with the stable error code, phase, reason, and `Retry-After`; no credentials or internal exception appear.

- [ ] **Step 6: Run focused backend/frontend tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_write_admission.py backend/tests/test_maintenance_routes.py
cd frontend && node --test app/maintenance-mode.test.mjs
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/write_admission.py backend/app/api/maintenance_routes.py \
  backend/app/main.py backend/app/core/readiness.py backend/app/services/startup_warmup.py \
  backend/app/repositories/{sqlite,postgres}/database.py backend/app/api/deps.py \
  backend/app/repositories/sqlite/migrations.py \
  backend/app/repositories/postgres/schema_manifest.py \
  backend/app/api/mcp_server.py backend/app/services/background_jobs.py \
  backend/app/services/{source_ingestion,ask_execution,report_execution}.py \
  backend/app/services/knowhow/projection.py \
  backend/tests frontend/app
git commit -m "feat: enforce database maintenance write admission"
```

### Task 2: Enforce the authority matrix and atomic active/shadow configuration swap

**Files:**
- Create: `backend/app/migration/shadow/activation.py`
- Modify: `backend/app/migration/shadow/control.py`
- Modify: `backend/app/migration/shadow/cli.py`
- Create: `backend/tests/shadow/test_activation.py`
- Create: `backend/tests/postgres/shadow/test_authority_matrix.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ActivationReceipt:
    run_id: str
    from_backend: Literal["sqlite", "postgresql"]
    to_backend: Literal["sqlite", "postgresql"]
    before_sha256: str
    after_sha256: str
    backup_path: Path
    redacted_from: DatabaseIdentity
    redacted_to: DatabaseIdentity

def swap_active_shadow_env(*, env_file: Path, run_id: str,
                           expected_from: str,
                           expected_to: str) -> ActivationReceipt: ...
def write_activation_manifest(..., destination: Path) -> Path: ...
def confirm_activation(..., formal_identity: DatabaseIdentity) -> None: ...
```

- [ ] **Step 1: Write atomic-file tests before implementation**

Cover unquoted/single-quoted/double-quoted raw assignments, comments/unrelated keys/newlines/file mode, duplicate/missing keys, `export`, multiline/interpolated values, symlink, concurrent edit/hash drift, non-regular file, disk/fsync/replace fault injection, and crash recovery from the receipt. Reject ambiguous formats rather than rewriting them.

Assert neither exception text, JSON receipt, audit row, captured stdout/stderr, nor process arguments contains user/password/query secret.

- [ ] **Step 2: Implement raw assignment swapping**

Validate resolved URL identities first, but swap the original right-hand-side byte slices so quoting/percent encoding is preserved. Write a run-specific `0600` backup in the same protected directory, a same-directory temp file with the original mode, fsync file, `os.replace`, fsync parent, then write a credential-free receipt. Never follow a symlink.

- [ ] **Step 3: Implement the authority invariant checker**

Before every transition and at formal backend startup, compare phase, intended/active backend, formal connection identity hash, capture flags, write gates, checkpoints, and worker direction. Startup mismatch reports maintenance/read-only and refuses business writes rather than guessing.

- [ ] **Step 4: Support secret-manager activation without exposing values**

`cutover --activation-manifest PATH` records expected from/to schemes, redacted identity hashes, run/revision, and one-time confirmation nonce. Deployment updates both secret references. `confirm-activation` connects using the new process Settings, proves identity, consumes the nonce, and leaves writes frozen. No command prints an `export`/`eval` string.

- [ ] **Step 5: Run tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/shadow/test_activation.py
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_authority_matrix.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/migration/shadow/{activation,control,cli}.py \
  backend/tests/shadow/test_activation.py \
  backend/tests/postgres/shadow/test_authority_matrix.py
git commit -m "feat: atomically activate the formal database backend"
```

### Task 3: Implement freeze, backup receipts, read-only smoke, and pre-write cutback

**Files:**
- Create: `backend/app/migration/shadow/admission.py`
- Create: `backend/app/migration/shadow/backup.py`
- Create: `backend/app/migration/shadow/smoke.py`
- Modify: `backend/app/migration/shadow/cli.py`
- Create: `backend/tests/postgres/shadow/test_freeze.py`
- Create: `backend/tests/shadow/test_backup_receipts.py`
- Create: `backend/tests/postgres/shadow/test_cutover_readonly.py`

**Commands added:**

```text
backup --run-id RUN --side sqlite|postgres --output PATH
freeze --run-id RUN
cutover --run-id RUN (--env-file PATH | --activation-manifest PATH)
confirm-activation --run-id RUN
smoke --run-id RUN --mode read-only|post-open-canary
```

- [ ] **Step 1: Write freeze race tests**

Use deterministic barriers for a write transaction already open, a mutation admitted but not yet in SQL, an upload/file deletion, an Ask/report worker, a separately heartbeating job process, a stale actor, an unregistered direct SQLite writer, and a newly arriving request during drain. Assert already admitted work finishes or rolls back before the final waterline; no new mutation commits after `Hfinal`.

- [ ] **Step 2: Implement draining and durable freeze**

`freeze` transitions admission `open→draining`, waits every registered actor `accepting_writes=false,inflight=0`, rejects stale/unknown live actors, then sets SQLite `write_frozen=1`. Because setting the SQLite control row obtains the SQLite writer lock, all earlier SQLite transactions finish before it commits. Capture `Hfinal` afterward, wait forward checkpoint exactly through it for ≥60 seconds, run `CUTOVER` verification, and transition phase to `cutover_readonly`.

- [ ] **Step 3: Add backup receipts**

SQLite backup uses the tested backup API. PostgreSQL backup invokes `pg_dump --format=custom` without placing a password/URL on argv; use libpq service/password environment or pass credentials through a protected subprocess environment. Record exit status, tool/server version, size, SHA-256, path, run/identity hash, timestamp, and a restore-test receipt. `cutover` refuses missing/unverified receipts.

- [ ] **Step 4: Implement read-only smoke**

After PostgreSQL activation/restart and `confirm-activation`, exercise health/readiness, existing-session auth, library/notebook/search/source detail, Ask history, Knowledge graph/read, Memory/Knowhow/report reads, sharing reads, storage references, and assert representative HTTP/MCP/background/direct-DB writes are rejected. This smoke does not claim that writes work.

Add a shadow-only PostgreSQL transaction rehearsal using the dedicated shadow role and a
same-transaction `maintenance_leases` row, exercise reviewed constraint/trigger statements,
and always roll back; capture events and the lease roll back with it. Formal repository/API
cannot obtain the role or lease function.

- [ ] **Step 5: Implement safe pre-write cutback**

If activation/read smoke fails before `resume-writes`, require PostgreSQL business-write count/waterline unchanged, stop the backend, invoke `rollback --pre-write --env-file`, swap config back, restart SQLite still frozen, confirm identity/read smoke, then `resume-writes --rollback-pre-write`. No reverse worker is required because PostgreSQL accepted no business commit.

- [ ] **Step 6: Run tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_freeze.py \
  backend/tests/shadow/test_backup_receipts.py \
  backend/tests/postgres/shadow/test_cutover_readonly.py
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/migration/shadow/{admission,backup,smoke,cli}.py \
  backend/tests/postgres/shadow/test_freeze.py \
  backend/tests/shadow/test_backup_receipts.py \
  backend/tests/postgres/shadow/test_cutover_readonly.py
git commit -m "feat: freeze and validate database cutover"
```

### Task 4: Capture every PostgreSQL authority write for reverse safety sync

**Files:**
- Create: `backend/app/migration/shadow/reverse_capture.py`
- Modify: `backend/app/migration/shadow/sql/postgres_control.sql`
- Modify: `backend/app/migration/shadow/manifest.py`
- Create: `backend/tests/postgres/shadow/test_reverse_capture.py`
- Create: `backend/tests/postgres/shadow/test_reverse_capture_all_tables.py`

**PostgreSQL shadow tables/control:**

```text
silicon_shadow.reverse_change_log(
  seq bigint generated always as identity primary key,
  run_id text,
  source_txid bigint,
  table_name text,
  pk_json jsonb,
  operation text,
  schema_epoch integer,
  captured_at timestamptz,
  applied_at timestamptz null
)
silicon_shadow.capture_control(direction, enabled, write_frozen, run_id, schema_epoch)
```

`seq` is an event identity, not a commit-order checkpoint. Sequence rollback gaps and
late commits are expected. The reverse worker must query every committed
`applied_at IS NULL` event group; it may not advance a maximum-seq cursor past an
unconfirmed row.

Business freeze triggers read `silicon_shadow.capture_control`. Controlled maintenance
rehearsal requires both `session_user` to be the dedicated shadow role and a same-transaction
row in `silicon_shadow.maintenance_leases(txid, run_id, purpose)` created through a function
not executable by the application role. A custom GUC alone is not an authorization boundary.

- [ ] **Step 1: Parameterize capture tests over all manifest tables**

Exercise INSERT/UPDATE/PK UPDATE/DELETE/cascade, transaction rollback, capture disabled, business freeze, wrong run/epoch, and change-log payload absence. Assert one monotonic total seq and canonical PK JSON.

- [ ] **Step 2: Add least-privilege tests**

The application role can operate business tables and triggers can append through a security-definer function with a fixed search path. It may execute only a fixed
`silicon_shadow.current_admission()` function returning non-secret gate fields; it cannot
directly read/write shadow metadata, disable capture, create maintenance leases, or alter
triggers. The shadow role can manage only owned shadow objects and required
hydration/apply operations.

- [ ] **Step 3: Generate and validate reverse triggers from the same manifest**

Use properly quoted manifest identifiers and a fixed schema-qualified function. Validate trigger/function definitions before opening writes; unknown/modified definitions fail closed.

- [ ] **Step 4: Run tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_reverse_capture.py \
  backend/tests/postgres/shadow/test_reverse_capture_all_tables.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/migration/shadow/{reverse_capture,manifest}.py \
  backend/app/migration/shadow/sql/postgres_control.sql \
  backend/tests/postgres/shadow/test_reverse_capture*.py
git commit -m "feat: capture PostgreSQL changes for rollback"
```

### Task 5: Implement reverse apply with the SQLite in-transaction lease

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`
- Modify: `backend/app/migration/shadow/replicator.py`
- Modify: `backend/app/migration/shadow/worker.py`
- Modify: `backend/app/migration/shadow/transform.py`
- Create: `backend/tests/postgres/shadow/test_reverse_replicator.py`
- Create: `backend/tests/postgres/shadow/test_reverse_crashes.py`

**SQLite v25 addition:**

```sql
CREATE TABLE shadow_reverse_applied_events (
  run_id TEXT NOT NULL,
  source_seq INTEGER NOT NULL,
  applied_at TEXT NOT NULL,
  PRIMARY KEY (run_id, source_seq)
);
```

- [ ] **Step 1: Write reverse and no-loop tests first**

Cover all operation types, current PostgreSQL row hydration, PostgreSQL row deleted after earlier upsert, JSONB/timestamp/boolean/bytea reverse transforms, shared-file references, replay, sequence gaps, transaction rollback, a lower sequence committing after a higher sequence, wrong phase, and explicit proof that applying to SQLite creates no forward event.

- [ ] **Step 2: Write SQLite lease/crash tests**

For each injected failure, assert one SQLite `BEGIN IMMEDIATE` transaction sets `apply_active=1`, applies all rows, inserts every `shadow_reverse_applied_events` receipt, resets `apply_active=0`, and commits—or rolls all four effects back. Concurrent direct writers remain rejected before, during, and after the lease.

- [ ] **Step 3: Parameterize `ShadowReplicator` by direction**

Reuse shared transforms/apply primitives but not the forward contiguous-checkpoint algorithm.
Reverse batches select committed `applied_at IS NULL` rows grouped by `source_txid`, hydrate
current PG rows in a repeatable-read snapshot, and order current-state upserts/deletes by
manifest FK topology. The SQLite transaction first ignores any event whose receipt already
exists, applies the rest, and inserts receipts. Only after SQLite commits may the worker mark
those PG outbox rows `applied_at`; a crash in between replays safely. Never call the formal
SQLite repository and never perform filesystem operations.

- [ ] **Step 4: Run crash and convergence tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_reverse_replicator.py \
  backend/tests/postgres/shadow/test_reverse_crashes.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/migrations.py \
  backend/app/migration/shadow/{replicator,worker,transform}.py \
  backend/tests/postgres/shadow/test_reverse_*.py
git commit -m "feat: replicate PostgreSQL authority changes back to SQLite"
```

### Task 6: Open PostgreSQL writes only after reverse safety is healthy

**Files:**
- Modify: `backend/app/migration/shadow/control.py`
- Modify: `backend/app/migration/shadow/cli.py`
- Modify: `backend/app/migration/shadow/smoke.py`
- Create: `backend/tests/postgres/shadow/test_resume_writes.py`

**Commands:**

```text
start-reverse --run-id RUN
worker --run-id RUN --direction reverse
resume-writes --run-id RUN
smoke --run-id RUN --mode post-open-canary
```

- [ ] **Step 1: Add a complete prerequisite matrix test**

`start-reverse` requires confirmed PostgreSQL activation, frozen PG writes, disabled/stopped
forward capture/worker, final clean verifier, valid backups, and valid reverse triggers. It
requires an empty/unacknowledged-free reverse outbox and no SQLite event receipts for this
new direction, enables reverse capture, and moves to `postgres_to_sqlite` while keeping PG
frozen.

`resume-writes` additionally requires a live unique reverse worker heartbeat, zero
`applied_at IS NULL` reverse events for ≥60 seconds, zero poison/difference, SQLite
`write_frozen=1/apply_active=0`, and a passed read-only smoke. A max-seq equality is not a
substitute. Any missing fact rejects the transition.

- [ ] **Step 2: Implement ordered start/reverse/open transitions**

Enable reverse capture before any PG business write. Start/confirm the reverse worker. Only `resume-writes` changes PG admission/durable gate to open. Record actor, run, revision, checks, and time in the audit table without secrets.

- [ ] **Step 3: Add a post-open canary**

Through real HTTP/API behavior, create a temporary notebook, update its title, add/delete a small supported source fixture, create/confirm/deprecate a Memory, create/edit/project/delete a Knowhow table, create/cancel/delete a report, exercise share membership, then delete the notebook. Wait reverse caught up and verify all final rows/cascades plus shared-file state. Mark the canary IDs with the run and guarantee cleanup/retry idempotency.

- [ ] **Step 4: Run tests**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_resume_writes.py
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/migration/shadow/{control,cli,smoke}.py \
  backend/tests/postgres/shadow/test_resume_writes.py
git commit -m "feat: open PostgreSQL writes behind reverse safety gates"
```

### Task 7: Implement lossless post-write rollback and a new-run boundary

**Files:**
- Modify: `backend/app/migration/shadow/control.py`
- Modify: `backend/app/migration/shadow/cli.py`
- Modify: `backend/app/migration/shadow/activation.py`
- Modify: `backend/app/migration/shadow/verifier.py`
- Create: `backend/tests/postgres/shadow/test_rollback.py`
- Create: `backend/tests/postgres/shadow/test_rollback_faults.py`

- [ ] **Step 1: Write rollback state/fault tests**

Test rollback with writes at each stage, PG transaction in flight, reverse lag, poison event, drift, worker death, SQLite apply lease open, missing backups, config edit drift, backend still running, activation swap crash, SQLite read-smoke failure, and a new mutation attempt during rollback. No path may open SQLite before final verification.

- [ ] **Step 2: Implement rollback drain**

Transition PostgreSQL admission open→draining; wait registered work; set PG durable write
gate frozen; wait for all application-role transactions to end; capture the final visible
reverse event set; process until `applied_at IS NULL` is zero for ≥60 seconds; run
cutover-level PG→SQLite verification including storage references; save PG and SQLite backup
receipts; stop reverse worker/capture; transition to rollback `cutover_readonly`.

- [ ] **Step 3: Reuse atomic activation in reverse**

With backend stopped, `rollback --env-file` verifies active=PostgreSQL/shadow=SQLite and swaps the raw assignments atomically. Start SQLite still frozen, confirm formal identity and read smoke. Only `resume-writes --rollback` sets SQLite open and marks the run terminal/rolled-back (`off` authority state).

- [ ] **Step 4: Enforce a new run for any retry**

Old snapshots/checkpoints/logs remain audit material but `start-forward` rejects the terminal run id. A new migration uses a new run id and fresh baseline/checkpoints; no operator can rewind/relabel the old run.

- [ ] **Step 5: Run rollback suites**

```bash
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHONPATH=backend ${PYTHON_BIN:-python3} \
  -m pytest -q -n0 -m postgres_integration \
  backend/tests/postgres/shadow/test_rollback.py \
  backend/tests/postgres/shadow/test_rollback_faults.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/migration/shadow/{control,cli,activation,verifier}.py \
  backend/tests/postgres/shadow/test_rollback*.py
git commit -m "feat: provide lossless PostgreSQL rollback"
```

### Task 8: Prove cutover/rollback E2E and publish the exact operator runbook

**Files:**
- Create: `backend/tests/postgres/shadow/test_cutover_rollback_e2e.py`
- Create: `docs/operations/postgresql-cutover-runbook.md`
- Create: `docs/operations/postgresql-cutover-checklist.md`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `packaging/DEPLOY.md`
- Modify: `scripts/check_postgres.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `backend/tests/test_architecture_documentation.py`

- [ ] **Step 1: Add a black-box E2E**

Use real subprocesses and an isolated `.env`, SQLite DB/storage, PostgreSQL schema, API server, forward/reverse workers, and HTTP client. Execute forward catch-up, freeze, server stop, config swap, PG read-only boot, reverse start, resume writes, canary, PG writes, rollback drain, config swap, SQLite boot, and final equality. Fault-inject after every numbered runbook step and assert the documented recovery step works.

- [ ] **Step 2: Put the following normal cutover sequence verbatim in the runbook**

Run from the repository root; use absolute paths. Operator substitutes the reviewed run id and work directory once:

```bash
export PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python
export SN_RUN_ID=pg-cutover-20260722-01
export SN_ENV_FILE=/Users/hzf/workspace/silicon_notebook/.env
export SN_WORK_DIR=/Users/hzf/workspace/silicon_notebook/.local/shadow/$SN_RUN_ID

$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-phase sqlite_to_postgres
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py verify \
  --run-id "$SN_RUN_ID" --level full
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py verify \
  --run-id "$SN_RUN_ID" --level full
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py freeze --run-id "$SN_RUN_ID"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-phase cutover_readonly --require-caught-up
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py backup \
  --run-id "$SN_RUN_ID" --side sqlite --output "$SN_WORK_DIR/pre-cutover.sqlite3"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py backup \
  --run-id "$SN_RUN_ID" --side postgres --output "$SN_WORK_DIR/pre-cutover.pgdump"

bash scripts/backend.sh stop
bash scripts/shadow.sh stop forward --run-id "$SN_RUN_ID"

$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py cutover \
  --run-id "$SN_RUN_ID" --env-file "$SN_ENV_FILE"

bash scripts/backend.sh start
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py confirm-activation \
  --run-id "$SN_RUN_ID"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py smoke \
  --run-id "$SN_RUN_ID" --mode read-only

$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py start-reverse \
  --run-id "$SN_RUN_ID"
bash scripts/shadow.sh start reverse --run-id "$SN_RUN_ID"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-reverse-healthy --wait-seconds 120
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py resume-writes \
  --run-id "$SN_RUN_ID"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py smoke \
  --run-id "$SN_RUN_ID" --mode post-open-canary
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-phase postgres_to_sqlite --require-caught-up
```

The runbook must state after each command: expected phase, formal backend, writable backend, expected worker, success output fields, stop condition, and the one allowed recovery. It must explicitly say not to edit `.env` manually.

- [ ] **Step 3: Put the following post-write rollback sequence verbatim in the runbook**

```bash
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-phase postgres_to_sqlite
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py freeze --run-id "$SN_RUN_ID"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-reverse-caught-up --wait-seconds 120
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py verify \
  --run-id "$SN_RUN_ID" --level cutover
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py backup \
  --run-id "$SN_RUN_ID" --side postgres --output "$SN_WORK_DIR/pre-rollback.pgdump"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py backup \
  --run-id "$SN_RUN_ID" --side sqlite --output "$SN_WORK_DIR/pre-rollback.sqlite3"

bash scripts/backend.sh stop
bash scripts/shadow.sh stop reverse --run-id "$SN_RUN_ID"

$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py rollback \
  --run-id "$SN_RUN_ID" --env-file "$SN_ENV_FILE"

bash scripts/backend.sh start
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py confirm-activation \
  --run-id "$SN_RUN_ID"
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py smoke \
  --run-id "$SN_RUN_ID" --mode read-only
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py resume-writes \
  --run-id "$SN_RUN_ID" --rollback
$PYTHON_BIN scripts/migrate_sqlite_to_postgres.py status \
  --run-id "$SN_RUN_ID" --require-active-backend sqlite --require-write-open
```

The runbook must separately show the shorter **pre-write cutback** allowed only before `resume-writes`, and state that the above catch-up sequence is mandatory afterward.

- [ ] **Step 4: Add a printable two-person checklist**

Include operator/reviewer initials, timestamps, command output artifact paths, verifier IDs, backup receipt IDs, source/target waterlines, active URL identity hashes, worker heartbeats, go/no-go boxes, and explicit abort/rollback decision boxes. No credential field is permitted.

- [ ] **Step 5: Synchronize user/developer docs**

README/README_zh explain simply: before cutover SQLite is active; the tool swaps `DATABASE_URL` and `SHADOW_DATABASE_URL` atomically; restart is read-only; reverse sync must be healthy before PG writes; after PG writes, rollback must first catch SQLite up. AGENTS/architecture pin the authority matrix and forbid manual URL-only rollback.

- [ ] **Step 6: Run all gates and a timed rehearsal twice**

```bash
PYTHON_BIN=${PYTHON_BIN:-python3} bash scripts/check.sh
cd frontend && npm run build
TEST_POSTGRES_URL="$TEST_POSTGRES_URL" PYTHON_BIN=${PYTHON_BIN:-python3} \
  bash scripts/check_postgres.sh
```

Then run the exact checklist twice on disposable but production-shaped data. Record freeze duration, final verify duration, restart/read-smoke duration, reverse-health wait, and rollback duration. Expected: two complete lossless PASS rehearsals.

- [ ] **Step 7: Commit**

```bash
git add backend/tests/postgres/shadow/test_cutover_rollback_e2e.py \
  docs/operations/postgresql-cutover-runbook.md \
  docs/operations/postgresql-cutover-checklist.md \
  README.md README_zh.md AGENTS.md architecture.md packaging/DEPLOY.md \
  scripts/check_postgres.sh .github/workflows/ci.yml \
  backend/tests/test_architecture_documentation.py
git commit -m "docs: make PostgreSQL cutover and rollback operational"
```

---

## Phase acceptance gate

- [ ] Authority-matrix tests prove exactly one or zero writable business backends at every state.
- [ ] Freeze blocks HTTP, MCP, background, offline repository, and direct SQL writes while preserving reads and existing-token auth.
- [ ] `.env` swap is atomic, credential-safe, crash-recoverable, guarded by backend-stop/identity/config-hash checks, and never performed manually.
- [ ] PostgreSQL cannot open writes until activation/read-smoke/reverse-capture/worker/checkpoint/verifier prerequisites pass.
- [ ] Reverse apply and SQLite checkpoint share one `BEGIN IMMEDIATE` transaction and use the bounded `apply_active` lease without capture loops.
- [ ] Pre-write cutback and post-write rollback are separately tested and documented; post-write rollback loses no PG-authority write.
- [ ] Maintenance UI/frontend build, offline check, PG check, and black-box cutover/rollback E2E all pass.
- [ ] Two independent production-shaped rehearsals use the exact published commands and produce reviewed artifacts.
- [ ] Request code review with `superpowers:requesting-code-review`; resolve all Critical/Important findings and rerun exact-head verification.

Only after this phase is deployed, PostgreSQL has remained authoritative through the full observation gate, and an operator explicitly approves irreversible retirement should `2026-07-22-postgresql-sqlite-retirement.md` begin.
