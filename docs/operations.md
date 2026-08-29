# Operations, diagnostics, and ingestion tools

[Back to README](../README.md) · [中文说明](./operations_zh.md)

This runbook covers logs, live diagnostics, MinerU, offline ingestion, retrieval replay, migrations, and backfills. Script-oriented shortcuts are also indexed in [scripts/README.md](../scripts/README.md).

## Observability

The backend emits structured logs through a single `EventLogger` (`app/core/event_logging.py`): one JSONL line per event under `.local/logs/` plus a brief console line. Logging is best-effort — it never breaks the request or pipeline it observes — and is a no-op for the LLM channel when no model is configured.

The browser/API debug log viewer (`/dev/logs` and `/api/debug/logs/...`) is opt-in:
set `DEBUG_LOGS_ENABLED=true` for local inspection. Full LLM records can include
prompt/response text from private source material, so the viewer is disabled by
default.

- `requests.jsonl` — every HTTP request (method, path, status, latency, `request_id`). Requests slower than `SLOW_REQUEST_MS` (default 3000ms) are flagged `SLOW`. Responses carry an `X-Request-Id` header to correlate browser and server.
- `events.jsonl` — async source pipeline: per-stage timings (`parse` / `embed` / `extract`) and every status-machine transition. A "stuck" upload shows exactly which stage is running and for how long; failures record the real exception (and the source's `error_message`).
- `llm.jsonl` — every LLM call: chat (prompt/response/tokens/latency, truncated to `LLM_LOG_MAX_CHARS`), embeddings (summary only, no raw vectors), and errors that deterministic fallback paths would otherwise make easy to miss.

In the browser, the DevTools console mirrors requests as `[api] METHOD /path -> status Nms (request_id)`; while polling, the UI shows the pending stage / elapsed time and names the source that failed. A source's `error_message` is written by the backend as a Python exception string, so it goes to the console rather than onto the screen.

Error messages are split by audience. What a user sees is always Chinese: the frontend maps the HTTP status code to a readable sentence ("没有权限进行这个操作", "没找到，可能已被删除"), so a raw status line or a backend exception string never reaches the interface. Nothing is shown verbatim unless the backend explicitly said it was written for a user — the API marks those responses with an `X-User-Message` header, and only they are passed through (for example "用户名已被占用", which is more specific than the generic message). Everything else is generalized, including backend text that happens to be in Chinese: a message like "解析失败：不支持的文件类型" may just as easily be a raw exception, and there is no way to tell the two apart by looking at them. 5xx is always generalized regardless, so internal failures cannot leak. Failures that never produce an HTTP response at all — a dropped connection, a stalled backend, an error string carried inside a streaming event, a background job record, a failed report, or a source's parse failure — follow the same rule instead of printing their raw text.

What a developer or an MCP agent sees is unchanged: backend `detail` stays as-is in the API response and in the logs, and the full diagnostic — status, status text, the raw response body, and the `X-Request-Id` that correlates with `requests.jsonl` — is written to the DevTools console on every failed request, alongside the original text of any error the UI replaced with a generic message. So "it says I have no permission" is answered by reading the console's request id, not by guessing which check rejected it.

### Live production incident capture

On an Ubuntu 24.04 deployment started with `npm run start`, SSH to the host and run the
primary command from the repository root **during the hang**:

```bash
ssh <production-host>
cd <silicon-notebook-repository>
python3 scripts/diag.py incident
```

The normal single Uvicorn worker is discovered automatically. If the report says process
discovery was missing, ambiguous, or incomplete, obtain the already-running backend PID
from the service supervisor or host listener metadata and retry without restarting it:

```bash
python3 scripts/diag.py incident --pid <backend-pid>
```

The default result is one copyable UTF-8 text block of at most **32 KiB**. Collection has
one shared deadline of at most 10 seconds; process sampling, two stack captures, loopback
health probes, bounded historical-log reads, and a DB probe whose own budget is at most
one second all consume that same deadline. `SIGUSR1` is registered by the backend as a
non-terminating, all-Python-thread faulthandler dump. It captures stack frames only—never
local-variable values—and a successful capture leaves the backend alive.

The live heartbeat is written atomically every two seconds to
`.local/diagnostics/runtime.json`; a snapshot older than six seconds is treated as stale
and its active-work fields are excluded from high-confidence findings. Stack capture uses
`.local/diagnostics/incident.lock` and appends to
`.local/diagnostics/thread-dumps.log`; the dump file is bounded to 8 MiB after a successful
capture. Read-only DB analysis uses bounded temporary snapshots below
`.local/diagnostics/db-snapshots/`. These diagnostic artifacts are the only files the
collector may create, replace, or truncate. The runtime accepts only an owner-controlled
`0700` diagnostics directory and owner-owned, single-link regular `0600` heartbeat/dump
files; unsafe pre-existing paths or path replacement degrade diagnostics without following
links or truncating the hostile target.

Read the output in this order:

- `Confidence-ranked diagnoses` lists at most three deterministic hypotheses. `high`,
  `medium`, and `low` describe evidence strength, not certainty; a lone weak signal is not
  presented as a root cause.
- `Observations`, `Relevant stacks`, `Database and host signals`, and `Log metadata` show
  the metadata chain behind the ranking. `Safe next commands/actions` recommends what to
  inspect next but never performs remediation.
- `Missing/degraded evidence` is part of the result, not a failure to hide. A stale
  snapshot usually means the command was run after the incident; a missing/ambiguous PID
  calls for the explicit `--pid` retry. DB busy/locked, permission denial, deadline,
  malformed/corrupt logs, an unavailable signal path, or a raced process/file causes that
  evidence source to be excluded while the remaining collectors continue.
- An idle deployment may correctly say that no multi-signal diagnosis reached a useful
  confidence level. Re-run it while the operation is visibly stuck; do not infer a cause
  from an idle capture.

Copyable output never prints raw opaque identifiers: allow-listed notebook/request/job
references are pseudonymized consistently, and other raw IDs are omitted. It also never
prints user-controlled filenames, request bodies, source text, Ask
questions/answers, prompts or model messages, Memory/Knowhow content, SQL text or
parameters, authorization headers, cookies, tokens, secrets, raw command lines, or local
variables. Even sanitized output should be reviewed before it is shared outside the
trusted team.

The incident path needs no root privileges or third-party Python packages, does not import
`app`, and never restarts or terminates a process. All diagnostic commands are read-only
with respect to application data: they do not execute deletes or other product writes,
checkpoint/vacuum/analyze/reindex SQLite, run migrations, or auto-remediate. `incident`
may only maintain its bounded `.local/diagnostics/` artifacts as described above.

### Seven-command diagnostics reference

`scripts/diag.py` exposes exactly these seven commands:

| Command | Intended use | Runtime boundary |
| --- | --- | --- |
| `python3 scripts/diag.py incident` | Primary live, bounded incident capture; add `--pid <backend-pid>` when automatic discovery cannot select exactly one worker. | Ubuntu/Linux live process evidence; stdlib-only, app-free. |
| `python3 scripts/diag.py slow --since 24 --deep` | Historical slow-path report from logs, DB aggregates, and scale-index manifests; `--deep` adds potentially minutes-long read-only DB checks. Bare `python3 scripts/diag.py` still means `slow`. | Offline, stdlib-only, app-free. |
| `python3 scripts/diag.py latency --last 500` | P50/P95/max by Ask stage from `ask_stage` events. | Offline, stdlib-only, app-free. |
| `python3 scripts/diag.py locks --top 20` | SQLite write-lock contention aggregated by call site from `db_write_lock_slow` / `db_write_lock_stats` events. | Offline, stdlib-only, app-free. |
| `python3 scripts/diag.py open --local .local` | Diagnose notebook-open query and endpoint latency, cache cold cost, and mutation-sequence churn. | Offline, stdlib-only, app-free. |
| `python3 scripts/diag.py db --db .local/silicon_notebook.db` | Bounded source-side-effect-free SQLite/WAL/table/FK-index/query-plan evidence. | Offline, stdlib-only, app-free. |
| `python3 scripts/diag.py base-recall [active_notebook_id] --db .local/silicon_notebook.db` | Diagnose mounted-base availability and the latest report's tier-reference counts using metadata only. | Bounded source-side-effect-free SQLite snapshot; stdlib-only, app-free; no retrieval, query/content echo, repository construction, migration, or source SQLite open. |

`base-recall` uses the same `O_NOATIME`-pinned, non-blocking, identity-validated DB/WAL copy as
`db`, then runs a fixed aggregate projection against the owned snapshot. If that safety boundary
is unavailable, it emits category-only degraded evidence instead of falling back to a live SQLite
connection. Its one UTF-8 report is capped at 32 KiB and contains only counts, fixed status labels,
and per-run pseudonyms—never raw notebook/user/report/object/chunk IDs, titles, questions, content,
filenames, paths, exceptions, credentials, or secrets.

Historical readers cover, deduplicate, and bound all supported layouts for the
`requests`, `events`, and `llm` channels: legacy `<channel>.jsonl`, daily
`<channel>-YYYY-MM-DD.jsonl`, daily gzip `<channel>-YYYY-MM-DD.jsonl.gz`, and one-level
per-user log directories. Malformed rows and byte/window truncation are reported as
degraded metadata. Existing standalone engine scripts remain runnable for established
operator notes and cron jobs; the seven-command dispatcher is the preferred entry point.

`python3 scripts/diag.py locks [--log PATH] [--top N]` aggregates SQLite write-lock
contention by call site. `wait` is how long a writer queued for the lock (what users feel
as a stalled page); `hold` is how long a writer held it (who caused it). Sorted by
`hold_max`, worst first. It prints two tables: threshold-crossing violations
(`db_write_lock_slow`, rate-limited, so only the tail above the threshold) and the periodic
full-distribution snapshot (`db_write_lock_stats`, unfiltered but a point-in-time cumulative
view) — a call site can be busy without ever crossing the threshold, and that only shows up
in the second table. Tune the capture threshold with `DB_WRITE_LOCK_WARN_MS` (default 200).

**Log viewer — `/dev/logs`.** A read-only debug page that visualizes these JSONL channels (LLM channel in v1). The left list is filterable by kind / status / model with full-text search; the detail pane shows exactly what was sent to the LLM (the `system` / `user` messages and the `schema_hint`) alongside the model's response, token usage, and latency. It is served by gated backend endpoints under `/api/debug/logs/...` — set `DEBUG_LOGS_ENABLED=false` to hide them.

## SQLite → PostgreSQL forward shadow

The delivered shadow path continuously copies SQLite into PostgreSQL without changing the
active application backend. It is one-way replication, not application dual-write and not
cutover: keep `DATABASE_URL` on SQLite for the entire procedure. `SHADOW_DATABASE_URL` only
names the PostgreSQL target; setting it alone starts nothing. Changing `DATABASE_URL` does
not copy, migrate, or synchronize existing data.

### Prepare and start

Before starting, restore-test current backups of the SQLite database **and storage tree** and
of the PostgreSQL target, record their evidence IDs, and confirm target free space. Use a
dedicated PostgreSQL 16 database with UTF-8 and `public.pg_trgm`; do not let applications or
other migration runs write to it. Run commands from the repository root with an owner-only
shell and keep their JSON/token output private.

```bash
export DATABASE_URL=sqlite:////srv/silicon-notebook/silicon_notebook.db
export SHADOW_DATABASE_URL=postgresql://shadow_user:secret@pg:5432/silicon_notebook_shadow
export SILICON_NOTEBOOK_STORAGE_DIR=/srv/silicon-notebook/storage
export RUN_ID=shadow-20260725
export WORK_DIR=/srv/silicon-notebook/shadow/$RUN_ID

umask 077
mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"

PYTHONPATH=backend python scripts/shadow_sqlite_to_postgres.py preflight \
  --run-id "$RUN_ID" --work-dir "$WORK_DIR" --json \
  --disk-evidence-id capacity-20260725 --available-target-bytes 500000000000 \
  --backup-evidence-id restore-test-20260725 \
  --confirm-source-backup --confirm-target-restore >"$WORK_DIR/preflight-output.json"

CONFIRMATION_TOKEN="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["confirmation_token"])' \
  "$WORK_DIR/preflight-output.json")"
PYTHONPATH=backend python scripts/shadow_sqlite_to_postgres.py start-forward \
  --run-id "$RUN_ID" --work-dir "$WORK_DIR" \
  --confirmation-token "$CONFIRMATION_TOKEN"

scripts/shadow.sh start "$RUN_ID" "$WORK_DIR"
```

`preflight` is read-only and binds the run to the exact live source and target identities,
schema pair, capacity evidence, and backup/restore confirmations. Re-running it for the same
private work directory revalidates those bindings and issues a fresh confirmation token.
`start-forward` is resumable: it installs run-scoped SQLite capture and logical-key guards,
runs formal PostgreSQL migrations/control setup, creates an atomic SQLite snapshot, copies
the 60-table baseline, publishes H0, and writes a one-hour worker-start token. If a worker
restart happens after that token expires, rerun `preflight` and the idempotent `start-forward`
before starting the supervisor again.

### Monitor and verify

```bash
PYTHONPATH=backend python scripts/shadow_sqlite_to_postgres.py status \
  --run-id "$RUN_ID" --work-dir "$WORK_DIR" --json

# Reissue a fresh confirmation token with preflight before a later verification.
PYTHONPATH=backend python scripts/shadow_sqlite_to_postgres.py verify \
  --run-id "$RUN_ID" --work-dir "$WORK_DIR" --level structural \
  --confirmation-token "$CONFIRMATION_TOKEN" --json
PYTHONPATH=backend python scripts/shadow_sqlite_to_postgres.py verify \
  --run-id "$RUN_ID" --work-dir "$WORK_DIR" --level full \
  --confirmation-token "$CONFIRMATION_TOKEN" --json
```

Healthy operation requires `worker_live=true`, `poison_count=0`, and a checkpoint that
continues to catch `source_high_water`. Before accepting the shadow as ready for a later,
separately reviewed cutover phase, observe zero lag for at least 60 seconds and retain two
consecutive `full/complete` verifier reports at 100% coverage. These results are evidence
only; this release provides no cutover command and does not authorize changing
`DATABASE_URL`.

The worker is single-consumer and foreground-only. `scripts/shadow.sh` provides a local
PID-identity-checked supervisor; production may use systemd or container lifecycle supervision,
but must still run exactly one worker for the run/direction and send SIGTERM for shutdown.
SIGTERM/SIGINT finish the current atomic batch, release the exact database-clock lease when
possible, and otherwise leave only a short expiring lease. Use:

```bash
scripts/shadow.sh status "$RUN_ID" "$WORK_DIR"
scripts/shadow.sh stop "$RUN_ID" "$WORK_DIR"
scripts/shadow.sh restart "$RUN_ID" "$WORK_DIR"
```

A minimal systemd unit runs that same foreground command. Keep the environment file and
work directory owned by `silicon-notebook` at `0600`/`0700`, respectively. Because the
worker-start token expires, use `Restart=no`: after any exit, inspect `status`, rerun
`preflight` plus idempotent `start-forward` to reissue the token, then start the unit again.

```ini
[Unit]
Description=silicon-notebook SQLite to PostgreSQL shadow worker
After=network-online.target

[Service]
Type=simple
User=silicon-notebook
WorkingDirectory=/opt/silicon-notebook
EnvironmentFile=/etc/silicon-notebook/shadow.env
UMask=0077
ExecStart=/opt/silicon-notebook/.venv/bin/python scripts/shadow_sqlite_to_postgres.py worker --run-id shadow-20260725 --direction forward --work-dir /srv/silicon-notebook/shadow/shadow-20260725 --confirmation-token-file /srv/silicon-notebook/shadow/shadow-20260725/worker.confirmation
KillSignal=SIGTERM
TimeoutStopSec=120
Restart=no
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Failures, retention, and rollback boundary

- Transient connection, serialization, deadlock, lock, and statement-timeout failures retry
  the whole target transaction with bounded backoff; the checkpoint advances only with the
  corresponding business rows.
- A deterministic identity/schema/continuity/conversion/constraint failure creates one
  redacted poison record and stops progress. Do not delete or skip the event. Stop the
  worker, preserve both databases and `$WORK_DIR`, diagnose the source/target drift, and use
  a new reviewed recovery procedure/run rather than mutating control tables by hand.
- Retention runs best-effort in the worker. It deletes only old, FULL-verified prefixes while
  respecting active verifier barriers, replay checkpoints, poison positions, and a minimum
  seven-day/100,000-event tail. Before a first successful FULL verification it retains the
  log. Retention failure is safe (extra rows remain) and does not stop replication.
- Keep SQLite and its storage tree backed up throughout shadowing. PostgreSQL contains
  database rows, not a second copy of uploaded/index files; a future cutover host must have
  the verified storage tree at the configured path.
- Stopping the shadow worker does not affect the active SQLite application. There is no
  PostgreSQL→SQLite replication. Never point application traffic at the shadow or switch
  `DATABASE_URL` as a recovery shortcut; discard/restore the PostgreSQL shadow or start a new
  run only after preserving evidence and reviewing the failure.

During SQLite-active shadowing, keep `scripts/batch_ingest.py` pointed at the active SQLite
source; never run it against the shadow target. After a completed cutover it supports the active
PostgreSQL backend under the stopped-service confirmation and advisory-lock boundary documented
in “Offline batch ingestion” below.

The authority swap and PostgreSQL→SQLite rollback mechanics belong to the separate
[formal cutover and rollback plan](./superpowers/plans/2026-07-22-postgresql-cutover-and-rollback.md);
do not execute that future plan as part of forward shadowing.

## SQLite → PostgreSQL stopped snapshot migration and cutover

This stopped-writer importer is separate from forward shadowing. It uses
`scripts/migrate_sqlite_to_postgres.py`, must target a different PostgreSQL database, and must
never run against a database owned by a shadow run.

There is exactly one active database and no application dual-write. The included importer is
one-way SQLite→PostgreSQL snapshot migration; changing `DATABASE_URL` alone only opens a
different datastore. It does not import MySQL, continuously capture later writes, replay
PostgreSQL→SQLite, or copy source/upload/asset files.

This section is the authority on *why* the migration behaves as it does and what it refuses to
do. For a step-by-step execution checklist — phases, per-step success criteria, the points that
require a human decision, and the failures that are meant to stay failures — follow
[docs/postgres-migration-runbook.md](postgres-migration-runbook.md), which defers to this
section wherever the two disagree. That runbook is written in Chinese, matching the other
runbooks under `docs/`; this section stays the complete English reference, so nothing here is
only available there.

### 1. Prepare an empty target and preview

Create a dedicated UTF-8 PostgreSQL database. Do not point the importer at an existing app
database: any business row fails preflight. Keep the URL out of command arguments:

```bash
export POSTGRES_MIGRATION_URL='postgresql://USER:PASSWORD@HOST:5432/EMPTY_DB'

python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/to/.local/silicon_notebook.db
```

The default is a read-only preflight. It validates the SQLite identity/schema and PostgreSQL
UTF-8/current-schema/emptiness/migration-ledger state, then exits without creating a snapshot
or writing the target. `pg_trgm` must be creatable in `public`.

Size the work directory for **at least twice the source file**, not once — three times if the
rehearsal and the cutover share one directory. The sealed snapshot stays for the whole import;
a source whose schema lags the code is additionally copied into a full upgrade working copy;
and activation unconditionally takes another complete snapshot as the write-freeze anchor,
written in full as a temporary file and only deduplicated against the sealed one afterwards, so
the peak is two copies even when their contents are identical. A rehearsal runs while SQLite is
still online, so by the cutover the source has usually changed and the two sealed snapshots hash
differently, get different filenames, and both remain. A 500 GB source therefore needs 1 TB with
a dedicated cutover directory and 1.5 TB with a shared one; prefer a separate rehearsal
directory and confirm it is archived or removed before the window opens. A 1× allocation fails
at activation after hours of successful copying. Size the
target separately: PostgreSQL data plus indexes normally exceed the SQLite file and the index
rebuild needs additional scratch, so take the measured figure from a rehearsal
(`SELECT pg_size_pretty(pg_database_size(current_database()));`) rather than the SQLite size.

### 2. Rehearse while SQLite remains online

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/to/.local/silicon_notebook.db \
  --work-dir /protected/path/postgres-migration \
  --apply
```

The tool uses SQLite's backup API, so committed WAL state is included without copying a live
`.db` file incorrectly. It upgrades only a private snapshot copy, streams bounded COPY in FK
order, preserves historical ordinals, converts legacy JSON vectors to float32 `bytea`, escapes
PostgreSQL-unrepresentable NUL codepoints to the literal text `\\u0000` (a one-way
normalization — PostgreSQL text/JSON cannot store a NUL), verifies every
table with a content checksum, and commits each verified table together with a per-table
checkpoint. A run header keyed to the sealed snapshot hash lets a stopped import resume from the
last completed table instead of restarting the whole copy; finalize (ordinal reseed, index
rebuild, `ANALYZE`) is idempotent. Full single-transaction atomicity is deliberately traded for
restartability — the final activation pass re-verifies every table's checksum before any cutover.
Its credential-free receipt records table counts/checksums, every NUL normalization, and the
tuning used. Empty retired SQLite tables are recorded but not copied; a non-empty retired table
fails closed.

The SQLite-only `shadow_capture_control` and `shadow_change_log` tables are operational state,
not business data: the stopped importer excludes them even when a prior shadow run left rows,
and records that reviewed exclusion in the receipt. This does not relax the empty-only rule for
retired user-data tables.

If an import stops partway (crash, dropped remote connection, machine reboot), re-run the same
command: the copier reuses the checkpoint keyed to the identical stopped source, skips the
already-committed tables, and continues. Avoid re-snapshotting the multi-gigabyte source on each
retry by naming the importer-owned sealed snapshot:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/to/.local/silicon_notebook.db \
  --work-dir /protected/path/postgres-migration \
  --snapshot /protected/path/postgres-migration/sqlite-vNN-HASH.snapshot.db \
  --apply
```

Name, directory, SHA-256 prefix, `quick_check`, schema version, and WAL/SHM absence are all
revalidated; the snapshot hash must match the recorded run; and the snapshot's recorded source
origin must match the selected `--source` — reusing a snapshot sealed from a different database (or
one missing its origin record) fails closed rather than importing the wrong data. Never use
`--snapshot` for the final cutover merely because an older rehearsal succeeded: SQLite commits
after that snapshot are absent.

### 2a. Tuning and prerequisites for a large database

For a large source, throughput and reliability are dominated by a few levers:

- **Run the CLI on (or on the same fast network as) the PostgreSQL host.** Every COPY and the
  read-back verification cross the connection; a remote link moves the whole dataset out and
  back twice.
- **Raise the index-rebuild budget.** Rebuilding indexes on fully loaded large tables is the
  slowest finalize step. Pass `--maintenance-work-mem 2GB` (or larger, within host memory) and
  `--max-parallel-index-workers N`; both are session-scoped and only affect this import.
- **Session bulk-load settings.** The import holds one connection with `synchronous_commit=off`,
  `statement_timeout=0`, and `idle_in_transaction_session_timeout=0` (pass
  `--keep-synchronous-commit` to opt out of the first). Confirm the server imposes no global
  `statement_timeout`/`idle_in_transaction_session_timeout` that would abort a multi-hour copy,
  that TCP keepalives keep a long remote connection alive, and that `pg_wal` has room — a large
  load produces a large volume of WAL before checkpoints recycle it.
- **Measure first.** Rehearse `--apply` against a copy of production data on the target host to
  measure real throughput before committing to a maintenance window. Per-table progress lines
  (`COPY i/N`, `VERIFY i/N`, `INDEX i/N`) show where time is spent, and a resumed run reports
  `SKIP i/N ... (checkpointed)` for tables it reuses.
- **`--batch-rows`** bounds the SQLite fetch / COPY batch (default 1000); raise it for narrow
  tables, lower it if very wide rows pressure memory.
- **`--source-timezone`** sets the timezone in which legacy *naive* SQLite timestamps are
  interpreted before conversion to UTC. It defaults to the importer host's local zone, which is
  correct only when the importer runs on a host in the same timezone as the SQLite deployment.
  If you run the importer on a PostgreSQL host in a different timezone, set the SQLite host's
  IANA zone explicitly (e.g. `--source-timezone Asia/Shanghai`) or every naive instant shifts by
  the offset. The zone used is recorded in the receipt.
- **Run the final activation as the deployment env file's owner** (or as root). Activation writes
  the credential `.env` `0600`; when it runs as a *different* user than the backend service
  account, it restores the original owner so the service can still read the file, and fails
  closed if it lacks the privilege to do so rather than locking the service out after cutover.

### 3. Perform the final SQLite→PostgreSQL cutover

1. Announce the maintenance window. Stop every API, background worker, MCP client writer,
   batch/maintenance process, and scheduler; then stop the backend after in-flight writes end.
2. Create a **new empty final PostgreSQL database**. A rehearsal target contains an older
   snapshot and is intentionally rejected. Take/restore-test the normal SQLite and PostgreSQL
   backups required by your deployment policy.
3. Run preview again against the stopped SQLite source. Then perform the final import and
   atomic local activation in one command (all paths must be absolute):

   ```bash
   python scripts/migrate_sqlite_to_postgres.py \
     --source /absolute/path/to/.local/silicon_notebook.db \
     --work-dir /protected/path/postgres-migration \
     --apply \
     --activate-env /absolute/path/to/.env \
     --confirm-service-stopped
   ```

   The activation pass repeats the SQLite snapshot and every PostgreSQL table checksum before
   it atomically replaces `.env`; a mismatch leaves `.env` unchanged. On a large target you may
   add `--fast-activation` to skip only that second full-table PostgreSQL checksum read — the
   import already checksum-verified and checkpointed every table — while the SQLite source
   re-snapshot (the cutover anchor proving no write slipped in) and the schema/manifest checks
   still run. Retain the final sealed
   snapshot, receipt, and restricted `.env.pre-postgres-*.bak` file. Verify that
   `SILICON_NOTEBOOK_STORAGE_DIR` points to the same files on the new deployment host, or copy
   that directory separately and verify it; database migration never copies those files.
   To activate a migration that already completed while the source has remained stopped, use
   `--activation-receipt /absolute/path/migration-*.receipt.json` instead of `--apply`; the same
   full verification still runs. The CLI sets PostgreSQL as the only `DATABASE_URL` and stores
   the prior SQLite URL as inert `SHADOW_DATABASE_URL`; it does not stop or start processes.
4. Restart the backend; deployment remains `--workers 1`. Do not manually edit the selector
   between receipt verification and startup.
5. Before traffic, require `curl -fsS http://127.0.0.1:8000/api/ready` to report
   `"ready": true`; log in as admin and a normal user; compare notebook/source counts with
   the receipt; exercise search and representative Ask/Knowledge/Memory/Knowhow/report reads;
   verify referenced files; then perform one explicitly approved canary write and its
   background-job completion. Only then reopen traffic.

### 4. Switch back safely

- Before PostgreSQL accepts any business write, rollback is safe: stop the backend, restore
  the SQLite `DATABASE_URL`, start one worker, and repeat the readiness/auth/count/read smoke.
- Note where that boundary actually falls. **Starting the backend always writes**, regardless of
  what the data contains: `_initialize()` (`postgres/bundle.py`) re-hashes the configured admin
  password with a fresh salt and unconditionally updates the built-in `user-local` row on every
  start. Two further writes are data-dependent: `recover_interrupted_jobs()` settles leftover
  running `ask_jobs`/`merge_review_jobs`/`extraction_runs`/`kg_build_jobs`, `knowhow_rows` in
  `syncing`/`pending`, and `sources` in `extracting`/`queued`/`parsing` before readiness, and
  clears both KG scratch tables; and `_reproject_legacy_knowhow_tables()` runs *after*
  `mark_ready()`, scheduling background cell-level reprojection that replaces KG objects for any
  knowhow table still carrying the older fixed KOs. A provably write-free rollback therefore has
  to be decided *before* the first PostgreSQL start — there is no "started but untouched" state.
  The bootstrap and recovery writes cost nothing to roll back (a SQLite start does the same),
  but the legacy reprojection is a business-data change. After that, `/auth/login` writes
  `auth_sessions` via `create_session()`, so rolling back past login costs the sessions.
- After the first PostgreSQL business write, editing the URL back loses that write. This
  repository has no reverse importer or dual-write log. Freeze again, externally reconcile
  PostgreSQL→SQLite (including storage effects), verify both sides, and only then reopen
  SQLite. If that process has not been designed and rehearsed, PostgreSQL is the rollback
  boundary.
- Merely toggling the URL during development selects two independent histories. Neither side
  is kept synchronized. Never run SQLite-only maintenance against PostgreSQL, and never run a
  direct batch mutation while live application/background writers still use that database.

## PostgreSQL notebook-aware lexical indexes

PostgreSQL lexical retrieval always keeps `notebook_id` in the SQL predicate, but the legacy
single-expression trigram indexes cannot use that boundary during index access. On a large
shared table, a common term can therefore produce a global bitmap and discard almost all rows
only after heap recheck. The two operational indexes prepend `notebook_id` with `btree_gin`:

- `idx_knowledge_objects_nb_name_trgm` on the active knowledge-object name predicate;
- `idx_chunks_nb_text_trgm` on chunk text.

The hot-path fix batch 2 payload index (`idx_knowledge_objects_nb_payload_trgm`,
migration 0042 / `scripts/build_hotpath_indexes.py`) follows this same composite shape
for the same reason; it ships through the hot-path channel documented in
`docs/deployment-and-configuration.md`, not through the script below.

From the repository root, first inspect without changing the database, then apply during a
controlled low-traffic window:

```bash
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py --apply
```

The URL is read from `DATABASE_URL` and is never printed. The apply path takes a dedicated
session advisory lock, installs/verifies `public.btree_gin`, builds one index at a time with
`CREATE INDEX CONCURRENTLY`, uses a short lock timeout, and leaves its build statement timeout
disabled. Reads and writes may continue, but the build can still consume substantial CPU, I/O,
temporary/free disk, WAL, and replica bandwidth; take a current backup, check free space, and
monitor it separately:

```sql
SELECT pid, relid::regclass, index_relid::regclass, phase,
       blocks_done, blocks_total, tuples_done, tuples_total
FROM pg_stat_progress_create_index;
```

An interrupted run is safe to rerun. A valid completed index is skipped; an invalid artifact
owned by this tool is dropped concurrently and rebuilt. Extension/index privileges are still
required, and a different existing index definition fails closed instead of being overwritten.
After the command succeeds, rerun the inspect command and use `EXPLAIN (ANALYZE, BUFFERS)` on
representative short/common and long/rare terms to confirm the new index is selected.

By default, the legacy global trigram indexes remain as a rollback/performance safety net. Only
after both new indexes are verified and the deployed application is confirmed to keep every
corresponding lexical query notebook-scoped may an operator opt into
`--apply --drop-legacy`; the tool verifies both replacements before dropping anything. This
reduces GIN write amplification but is not required for the read-path gain. Recreating or
dropping either index changes planner options only: the SQL predicates, similarity scores,
candidate limits, and ordering are unchanged, so retrieval quality must remain byte-for-byte
equivalent in the PostgreSQL conformance test.

### KNN early stop for the largest notebooks (`POSTGRES_LEXICAL_KNN_ENABLED`)

The composite indexes above insulate every *other* notebook from a giant one, but cannot make
the giant notebook's own probes cheap: `ORDER BY similarity` still recomputes similarity for
every trigram candidate before its LIMIT (measured on a 9.1M-object notebook: 7.4s for one
common short term; a multi-term question times out and the lexical arm dies fail-open). The
default-on `POSTGRES_LEXICAL_KNN_ENABLED` flag switches unscoped runs on notebooks at or
above `POSTGRES_LEXICAL_KNN_MIN_ROWS` (default 500,000 nodes+chunks) to a GiST `<->` scan
that stops at the LIMIT (measured 123ms, 60×). The floor matters: the GiST index has no
notebook key, so the KNN scan walks global distance order and only pays off for a notebook
that dominates the table — set the floor above your largest non-dominant notebook. Scores
stay `similarity()`; within equal-similarity tie classes the selected members may differ from
legacy and are not run-to-run stable (tie membership follows GiST traversal order), so a
recall A/B must sample the same questions repeatedly rather than compare one paired run.

Enablement needs one additive GiST index; availability is detected by SHAPE, so an index you
already built for benching counts and nothing is renamed or rebuilt:

```sql
CREATE INDEX CONCURRENTLY idx_knowledge_objects_name_knn_gist ON knowledge_objects
  USING gist ((((payload ->> 'name') COLLATE "C")) public.gist_trgm_ops(siglen=128))
  WHERE status != 'deprecated';
```

Then restart the backend — the flag defaults to on, so building the index is the only step
(availability is probed
once per process and never re-probed). Rollback order matters for the same reason: set the
flag off and restart FIRST, and only then drop the index — dropping it under a live flag
leaves the cached "available" verdict pointing at a vanished index, and every KNN statement
degrades to an unindexed distance sort until the process restarts. Terms whose KNN page comes
back short are re-probed through the legacy statement automatically, so rare/ILIKE-only
matches keep their legacy results.

## PDF parsing with MinerU

PDF parsing is decoupled from the GPU. The backend never imports torch; it talks to MinerU only when configured, and otherwise uses the local PyMuPDF4LLM layout/Markdown fallback (pypdf is the final parser-error fallback).

- **Local / no GPU**: keep `MINERU_MODE=off`. PDFs use PyMuPDF4LLM for page-aware Markdown, headings, reading order and reconstructed tables without a remote service.
- **GPU deployment host (recommended: HTTP service)**: run MinerU as its own service and point the backend at it:

  ```bash
  pip install -U "mineru[all]"      # on the GPU box
  mineru-api --host 0.0.0.0 --port 8000
  ```

  Then set on the backend:

  ```text
  MINERU_MODE=http
  MINERU_API_URL=http://<gpu-host>:8000
  MINERU_BACKEND=pipeline           # or a vlm-* backend
  MINERU_FORMULA_ENABLE=true
  MINERU_TABLE_ENABLE=true
  MINERU_TIMEOUT_SECONDS=600
  ```

- **Same-host Python API**: if the `mineru` Python package is installed alongside the backend, set `MINERU_MODE=cli` instead (no `MINERU_API_URL` needed). This mode runs `mineru.cli.common.do_parse/read_fn` in an isolated subprocess; it does not invoke the `mineru` shell command, because that command can start its own local API server on some MinerU versions.

- **Remote VLM inference server**: to offload only the VLM model to a standalone vllm/sglang server (instead of the full `mineru-api`), use a client backend and point it at that server:

  ```text
  MINERU_BACKEND=vlm-http-client        # or vlm-sglang-client
  MINERU_VLM_SERVER_URL=http://<vlm-host>:30000
  ```

  This works in both `http` and `cli` modes; the URL is ignored by non-client backends.

- **Apple Silicon local (MLX, offline)**: a Mac with Apple Silicon has no NVIDIA GPU but accelerates MinerU via MLX, so you can run the same high-fidelity parsing locally:

  ```bash
  python -m pip install -U "mineru[core]"
  mineru-models-download -s huggingface -m vlm     # one-time (~GB); use -s modelscope if HF is slow
  ```

  Then in your local `.env`:

  ```text
  MINERU_MODE=cli
  MINERU_BACKEND=vlm-auto-engine     # uses MLX on Apple Silicon
  MINERU_PARSE_METHOD=auto           # set txt/ocr when you need to match a manual MinerU run
  MINERU_LANG=en                     # optional; set when the PDF language is known
  MINERU_MODEL_SOURCE=huggingface
  MINERU_TIMEOUT_SECONDS=1800        # local VLM can need >10 min for full papers
  ```

  Keep `MINERU_MODE=off` in `.env.example` so other environments stay offline-safe by default.

**URL sources ("Add link") prefer local MinerU.** A pasted public PDF URL is parsed by the local MinerU service whenever one is configured (`MINERU_MODE=http`/`cli`): the backend downloads the PDF and runs it through the same local-MinerU→PyMuPDF4LLM path as file uploads. For SSRF protection, the downloader validates the initial target and every redirect and rejects localhost, private, link-local, and reserved addresses — except URLs whose origin is listed in the deployment's `URL_IMPORT_TRUSTED_PROXY_HOSTS` trusted-proxy whitelist (see the deployment guide; empty by default); import other internal documents through file upload instead. The `MINERU_API_TOKEN` cloud (mineru.net) path is used only when no local MinerU is configured — and once local is in use it is never silently called. Adding a URL requires *either* a local MinerU or the cloud token. Uploaded files follow the same rule: when local MinerU is off and only the cloud token is configured, uploads are parsed via that same cloud v4 path (images, formulas, and tables included) — but only for suffixes MinerU can actually parse (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xlsm`). Other formats (`.md`, `.csv`, plain text, and Markdown `.zip` bundles) are parsed locally and never leave the deployment: a ZIP remains one stored source, and the backend resolves each Markdown member's relative image paths and writes matched images into source assets without extracting onto the host filesystem. Uploading these formats to mineru.net would expose user content to a third party that cannot parse it anyway. The self-hosted HTTP and cloud transports retry transient failures according to `MINERU_MAX_RETRIES` (default: two retries after the first attempt). If the final cloud URL/file parse fails or yields no usable elements, the backend downloads/opens the PDF locally and completes through PyMuPDF4LLM; pypdf remains the last fallback if that parser itself fails.

MinerU output maps to structured `SourceElement`s: formulas become `formula` elements (LaTeX preserved), tables become `table` elements (HTML kept in metadata), and headings keep their level. PyMuPDF4LLM produces page-chunked Markdown that is converted into the same heading/paragraph/table element model. The Office fallbacks map onto that same model in stages: DOCX first tries mammoth (Word styles → semantic HTML → heading/paragraph/table elements, `table_html` retained) and only then flat python-docx extraction; PPTX first tries python-pptx (slide text, slide tables, chart titles, grouped shapes, speaker notes) and only then the raw slide-XML extractor, which sees `p:sp` shapes alone and therefore dropped slide tables and charts entirely. The frontend renders formulas via KaTeX and HTML tables directly. A successful MinerU-to-Python degradation remains `extracted`; raw diagnostics stay in pipeline logs and the private source `error_message`, while list/detail responses expose only `parse_quality_warning`. That warning covers the suffixes whose fallback is genuinely lossy (`.pdf`, `.docx`, `.pptx`), so a DOCX that quietly completed through mammoth (or, further down the chain, python-docx) is as visible as a degraded PDF. Workbooks (`.xlsx`, `.xlsm`) are deliberately excluded: openpyxl reads every cell value faithfully, so the degradation costs table structure, not content — and on a MinerU deployment that cannot parse workbooks the warning would be permanently lit with no reparse able to clear it. Instead, workbooks get a stricter guard on the way in: a non-empty MinerU result is compared against the workbook's own non-empty row count (a local, model-free, network-free streaming count) and discarded in full in favour of openpyxl when it covers too few rows, because MinerU renders workbooks to pages and can drop whole columns or sheets without raising. Formats with no MinerU path at all (Markdown, CSV, text) never raise the warning, because their ordinary parser is not a degradation. The source detail then warns that layout, formulas, tables, or OCR may differ and gives owners explicit **Reparse** and **Delete source** actions. A successful later MinerU reparse clears the warning. A PDF that still parses to zero text is flagged with a scanned/image-PDF hint instead of looking like an empty success. On desktop, the source-detail window uses a conventional close control and can be dragged by its header — as can the app's other centered floating dialogs; narrow screens keep the fixed modal layout, and the detail body remains independently scrollable.

### Verify a PDF parses (`scripts/mineru_probe.py`)

A single-file diagnostic that sends one file (`.pdf`/`.docx`/`.pptx`) through the **exact inline path** the app uses for uploads — the configured MinerU service (`MINERU_MODE=http` → `/file_parse`, or `MINERU_MODE=cli`) followed by the same `content_list` → `SourceElement` mapping — and reports whether it parses. Use it to confirm a MinerU deployment is reachable and actually parses a given file before wiring it into ingestion.

```bash
python scripts/mineru_probe.py /path/to/paper.pdf
python scripts/mineru_probe.py /path/to/paper.pdf --dump /tmp/content_list.json
```

It first prints the effective MinerU config read from the repo-root `.env` — including how `http_proxy`/`no_proxy` resolve for the MinerU URL, a common cause of `504`s when an internal call is silently routed through a forward proxy (note: `no_proxy` does not understand CIDR ranges like `10.0.0.0/8`, only exact hosts) — then the raw block counts/types and the number of mapped structured elements. Exit code `0` means it parsed (≥1 element); `1` means no request was sent (MinerU off/misconfigured, or the file is missing); `2` means it sent but failed (unreachable, timeout, HTTP error, or an empty/zero-element result), each with a one-line hint classifying the failure. It imports the backend and reads the repo-root `.env`, so run it from the main checkout root. This probe covers only the inline `MINERU_MODE` path — not the mineru.net cloud path (URL sources) or the async `/tasks` batch endpoint below.

### Batch PDF→Markdown parsing (`scripts/mineru_batch_parse.py`)

A standalone, backend-independent CLI for bulk/offline pre-parsing of a whole PDF directory (e.g. a book corpus) against your own MinerU deployment, feeding the offline ingestion below: PDF directory → `mineru_batch_parse.py` → Markdown directory → `batch_ingest.py` → KG. It recursively finds PDFs under `--src`, submits each to an internal MinerU server's **async** `/tasks` API (submit → poll → fetch result), distributing files round-robin across the configured servers with a per-server concurrency limit, and writes a mirrored tree of `.md` files under `--out`. This is separate from the app's inline per-upload parsing above (`MINERU_MODE=http`, MinerU's synchronous `/file_parse` endpoint) and from the mineru.net cloud path — point it at your own async-capable MinerU server(s).

Configure via `.env` (`MINERU_BATCH_*`, see `.env.example`) — `--env-file` picks which `.env` file to load (default `./.env`) — and any key can be overridden per invocation with a matching flag (`--servers`, `--src`, `--out`, `--list <file>` for an explicit path list instead of a recursive scan, `--limit N` to cap the file count). Re-running skips `.md` files already produced, and every file's outcome (`ok`/`skip`/`fail`, or `cancelled` for files a Ctrl-C stopped before they started) is appended to a JSONL manifest (default `{MINERU_BATCH_OUT_DIR}/_manifest.jsonl`) so a run can be resumed or audited; a Ctrl-C lets in-flight files finish but dispatches no new work, and a re-run retries anything left `fail`/`cancelled`. `--only-failed` re-processes just the files the last run recorded as `fail` (also listed in `failed.txt`).

```bash
# config in .env (MINERU_BATCH_SERVERS / _SRC_DIR / _OUT_DIR ...)
python scripts/mineru_batch_parse.py --dry-run      # preview the server assignment
python scripts/mineru_batch_parse.py                # run
python scripts/mineru_batch_parse.py --only-failed  # retry failures from the last run
```

The script imports no backend code — only the standard library plus `requests` (already a backend dependency) — and talks to the MinerU server(s) over plain HTTP, so it needs no GPU/torch on the machine that runs it.

### Offline batch ingestion (directory → KG)

The portable phases `ingest`, `kg`, `index`, `all`, `embed`, `metadata`, `question-index`, `reparse`,
`backfill-source-index`, `backfill-chunk-elements`, and `backfill-images` select SQLite or PostgreSQL from `DATABASE_URL`. Direct PostgreSQL
maintenance is strictly offline: stop the API and every background writer, then append
`--confirm-service-stopped`. The flag records an operator assertion; it does not stop services.
A fail-fast database-wide advisory lock prevents overlapping maintenance CLIs. Source, durable and
limited KG-target, metadata, re-extraction, vector, relation, and reverse-index drivers use bounded
keyset pages — including the `index` stage's whole-notebook embedding-matrix load, which reads a
bounded page of vectors per statement instead of one unbounded `SELECT`. Large offline maintenance
runs can still hit the online `POSTGRES_STATEMENT_TIMEOUT_SECONDS` default (`30`, sized for
interactive requests) on the *other* long-running statements in these flows; set a larger value
(e.g. `86400`) in the environment of the maintenance CLI process itself for a large database — the
matrix load is no longer the pipeline's largest single statement, but the rest of the offline
pipeline (and a paged matrix read on a slow disk) still runs under the same per-statement timeout. Online maintenance should
continue through the application/API, and `--dry-run` opens no repository — except for `backfill-images`, whose dry-run is a read-only *database* pass (it reads each source's elements and chunks to report what it could restore) and therefore still needs `--confirm-service-stopped` on PostgreSQL. `vectors-to-blob` is
intentionally SQLite-only because PostgreSQL vectors are already `bytea`; PostgreSQL rejects it
before opening a repository.

Ingest a directory of Markdown (and the occasional PDF) through the existing
pipeline, in two phases: `ingest` (no LLM, fast — chunk Q&A works immediately),
then `kg` (LLM extraction, separately resumable).

```bash
# 1) parse + chunk + embeddings (no LLM); --notebook-name is required when creating a notebook
PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir /path/to/md_dir --notebook-name "My KB"

# 2) validate KG quality on a subset first (extract only the first 50 un-extracted sources)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 50

# 3) extract KG for the whole notebook (idempotent; skips already-extracted; resumable)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx

# repair graph-bearing sources whose latest KG run left failed windows
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --retry-partial

# or run both phases in one command
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "My KB"

# build the scalable-retrieval index for a base-tier notebook (offline; re-run after rebuilding a static base)
PYTHONPATH=backend python scripts/batch_ingest.py index --notebook-id nb-xxxx

# backfill any missing chunk + node vectors (idempotent; requires `chunk_embedding` bound)
PYTHONPATH=backend python scripts/batch_ingest.py embed --notebook-id nb-xxxx

# one-time storage migration: convert legacy JSON-text vectors to float32 BLOB (idempotent, no model call)
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --all-notebooks --workers 8

# proactively backfill the source-deletion reverse index (idempotent, no model call)
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --all-notebooks
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --notebook-id nb-xxxx --force

# proactively backfill the element -> chunk reverse index (idempotent, no model call)
PYTHONPATH=backend python scripts/batch_ingest.py backfill-chunk-elements --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py backfill-chunk-elements --all-notebooks
PYTHONPATH=backend python scripts/batch_ingest.py backfill-chunk-elements --notebook-id nb-xxxx --force

# surgically restore source images a single-file Markdown import dropped
# (idempotent, no model call; read-only with --dry-run)
PYTHONPATH=backend python scripts/batch_ingest.py backfill-images --notebook-id nb-xxxx \
    --mineru-output /path/to/mineru/output --dry-run
PYTHONPATH=backend python scripts/batch_ingest.py backfill-images --notebook-id nb-xxxx \
    --mineru-output /path/to/mineru/output --mineru-output /path/to/other/output
PYTHONPATH=backend python scripts/batch_ingest.py backfill-images --notebook-id nb-xxxx \
    --mineru-output /path/to/mineru/output --source-id src-xxxx --report /tmp/backfill.jsonl
# resume after an interruption without rescanning what was already done
PYTHONPATH=backend python scripts/batch_ingest.py backfill-images --notebook-id nb-xxxx \
    --mineru-output /path/to/mineru/output --after-id src-last-processed

# backfill missing paper metadata (title/authors/affiliations/venue/year) for a notebook's
# already-parsed academic-paper sources (idempotent; requires `paper_metadata` bound, no embedding call)
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx --force

# build the optional generated-question → original-chunk index
# (requires rollout mode shadow/on and both model workload bindings)
PYTHONPATH=backend python scripts/batch_ingest.py question-index --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py question-index --notebook-id nb-xxxx --force

# fix historically empty sources: re-parse sources missing source_elements (a prior parse that
# never landed) to backfill elements, then re-extract KG
PYTHONPATH=backend python scripts/batch_ingest.py reparse --notebook-id nb-xxxx
```

The `embed` subcommand re-fills only the chunk and KG-node vectors that are *missing* (e.g. after a throttled run left gaps). It requires `--notebook-id` and a configured service binding for the `chunk_embedding` workload — being a vector-backfill command, it ignores `--allow-no-embed` and errors out if that workload is unbound.

The `vectors-to-blob` subcommand is a one-time storage migration: embedding vectors used to be stored as JSON text in SQLite, which means loading hundreds of thousands of rows into a matrix (index builds, retrieval cold start) spends most of its time in `json.loads`. New writes are now stored as raw float32 BLOBs (`np.frombuffer` reinterprets them with zero parsing), and every reader already accepts either format — so this command is optional but recommended after upgrading: it re-encodes any pre-existing JSON-text rows across all four embeddings tables (`chunk_embeddings`, `knowledge_embeddings`, `element_embeddings`, `relation_embeddings`) in place, in batched transactions (5,000 rows/commit) with progress printed per table. It does **not** compute new vectors (so it needs no model-service binding) and is idempotent/restartable — re-running it converts nothing further, since it only selects rows SQLite still types as `text`. Use `--notebook-id` to scope it to one library or `--all-notebooks` to convert every notebook in the database. The `json.loads`/re-encode step (the single-core bottleneck at millions-of-rows scale) is parallelized across `--workers` processes (default `min(32, cpu_count())`; `--workers 1` uses no process pool at all) — the main process still owns every DB read/write, so SQLite stays single-writer. If the worker pool crashes it falls back to a serial pass automatically rather than losing the run.

The `backfill-source-index` subcommand proactively populates `knowledge_object_sources`, the reverse-lookup table (`object_id, source_id`) used by source-scoped KG retrieval and when a source is deleted or reparsed. `source_index_backfilled=true` is a completeness certificate; `false` means a historical/imported notebook has not been certified, **not** that the table or graph is empty. New notebooks start certified with an empty index, and every online KG write maintains it transactionally. A certified notebook resolves affected KG objects and source-scoped lexical candidates directly through the index. An uncertified notebook deliberately does **not** pay a whole-notebook lazy-backfill cost inside an interactive request: deletion/reparse uses keyset-paged database-native evidence filters, and narrowed KG search applies an authoritative evidence predicate before its lexical `LIMIT`. Neither path deserializes every object in Python nor mutates the marker, so retrieval cannot silently become empty merely because the certificate is false. These compatibility reads can still scan legacy KG rows; large historical/imported libraries should run this command offline to certify and accelerate them. A default all-selected, non-drifted Ask does not require this certificate because it keeps the normal notebook ANN + lexical candidate path and enforces the frozen ceiling after hydration.

The explicit command remains the offline prebuild and repair path. It makes no model call and is idempotent/restartable. SQLite v42 / PostgreSQL v20 persist one `source_index_backfills` row per notebook. The initial transaction either skips an already completed current marker, resumes a running/failed ledger pinned to the same `kg_mutation_seq`, or clears stale index rows and starts a new generation. Every bounded keyset page commits its `knowledge_object_sources` rows and cursor/counters atomically, so a crash replays at most the uncommitted page instead of the whole notebook. Generation drift records the stable `kg_generation_changed` code, leaves the fast-path marker false, and causes the next invocation to reset from the new generation. The ledger contains no evidence or exception text. Use `--notebook-id` for one notebook or `--all-notebooks` for the whole database; add `--force` only when an operator intentionally needs to discard a completed current ledger and repair/rebuild its index rows. Regardless of index state, online cleanup locks the source row before projection cleanup, caps each affected-object delete statement at 500 ids (a SQL-parameter bound, not a constant-time/background-job guarantee), and fetches/deletes referenced image-asset rows in one database round trip before unlinking their files. The source row in the UI switches to a deleting state immediately and disables its delete action until the request settles; a notebook-scoped tombstone also prevents stale navigation/list responses from restoring the deleted row.

The `backfill-chunk-elements` subcommand populates `chunk_elements`, the element -> chunk reverse index. `chunks.element_ids` stores the forward direction, so answering "which chunks contain this evidence element" used to scan every chunk row of the notebook and JSON-decode each one, once per index generation. After a backfill that becomes an indexed point lookup bounded by the handful of evidence elements one query actually hit. Notebooks that have not been backfilled keep the legacy whole-notebook scan with identical results, so the command is optional but recommended for large libraries.

Like `backfill-source-index`, it is an explicit offline operation — never triggered from an interactive request — makes no model call, and is idempotent/restartable. SQLite v46 / PostgreSQL v24 persist one `chunk_element_backfills` row per notebook. The initial transaction either skips an already completed current marker, resumes a running/failed ledger pinned to the same `kg_mutation_seq`, or clears stale rows and starts a new generation. Every bounded keyset page commits its reverse rows and cursor/counters atomically, so a crash replays at most the uncommitted page. Generation drift records the stable `kg_generation_changed` code, leaves the `chunk_elements_indexed` fast-path marker false, and causes the next invocation to reset from the new generation. The ledger contains no chunk text or exception text. New writes need no backfill: every chunk write path a live notebook can reach maintains the reverse rows inside the same transaction as the chunk rows, and deleting a source, reparsing it, or rewriting a knowhow cell removes them through the chunks foreign-key cascade. (Whole-notebook deep copy is exempt: it does not copy `unified_kg_state`, so a copy always reads through the legacy scan.) Use `--notebook-id` for one notebook or `--all-notebooks` for the whole database; add `--force` only to intentionally discard a completed current ledger and rebuild its rows.

Registered cost: the reverse index stores one row per (chunk, element) pair, so it is strictly larger than the chunk table — a source whose chunks average N element ids produces roughly N reverse rows per chunk. Those rows are removed by the `chunks` cascade, which makes deleting or reparsing a source a heavier write than before: the interactive delete transaction now also cascades this side table (indexed by `chunk_id`, so it is a bounded indexed delete per chunk, not a scan). Expect a proportional increase in delete/reparse transaction size and in on-disk footprint for large libraries. The read-side win is per query; this is the write-side price.

The `backfill-images` subcommand surgically restores source images that a single-file Markdown import dropped. A deployment that converted PDFs with offline MinerU and then uploaded only the resulting `.md` has no image elements and no assets at all: the single-file Markdown parse path does not resolve relative image paths, and an alt-less `![](images/<sha>.jpg)` is discarded outright. The MinerU output tree still holds part of the original images, and their file names are content hashes, so they can be matched back by name. The command indexes one or more `--mineru-output` trees (only files directly under an `images/` directory, so `auto`/`ocr`/`txt` method directories all work), walks the notebook's `.md`/`.markdown` sources in keyset pages, aligns each document's lines against its existing elements with a monotone two-pointer walk, and inserts the matched images anchored to the element they physically follow. It makes no model call, recomputes no embedding, touches no KG table, and leaves chunk ids and chunk text byte-for-byte unchanged — a restored image is appended to the tail of its anchor chunk's `element_ids` only. Captions are harvested opportunistically from an adjacent `Figure`/`Table`/`图`/`表` + number line (falling back to the image's alt text); an image without one still displays, because the citation-image path admits any `image` element with a non-empty `metadata.asset_id`.

Alignment is refused rather than guessed at. Element types that no Markdown text line can ever match (`image`, `figure`, `table`, `code_block`) cost the lookahead window nothing, so a run of consecutive tables or captioned images cannot starve it and strand the pointer. Each structural block also advances the pointer as it is scanned — one Markdown or HTML table block, one fenced code block, one captioned stand-alone image each cross exactly one element, with block starts decided by explicit open/close state rather than by what the previous line looked like (two fenced blocks with no blank line between them, or a pipe table immediately followed by an HTML one, are two elements even though the preceding line is of the same kind) — so an image that physically follows a table or a code block anchors on that block rather than on the paragraph before it. Without that the misplacement is silent: coverage stays at 100% while the image lands in the wrong chunk. An alt-less stand-alone image is deliberately not crossed, because the parse path discards it and no element exists to cross. On top of that, two guards decide when an anchor is untrustworthy: a per-image freshness rule skips an image whose anchor has gone stale (too many unmatched text lines since it was matched, reason `anchor_stale`), and a whole-source coverage floor skips every candidate in a document whose alignment coverage fell below it (reason `alignment_drifted`). Both show up per source in the `--dry-run` output and in `--report`. Hand-written Markdown with hard-wrapped paragraphs (one paragraph spanning several lines) can trip the coverage floor and be skipped whole; that is the safe direction — the target corpus is MinerU output, whose paragraphs are single lines — and it is visible rather than silent. Only genuine image blocks are restored. Owning a whole line is necessary but not sufficient: images inside list items, table cells or the middle of a paragraph are skipped, and so are lines that only look stand-alone — inside an HTML table block, indented four columns or more (indented code, list and paragraph continuations all parse without an image element), or carrying two images at once. All of them report `inline_image_skipped`. This matches the online Markdown rule that keeps only alt text and stores no asset, and it matters because image syntax inside a table or code block is usually a literal example rather than a picture. The `Markdown image N` ordinal counts only those genuine blocks too, so an inline reference no longer inflates the label of a restored stand-alone image. That same rule is why line normalisation folds image syntax down to its **alt text** rather than deleting it outright: the parse path leaves an inline image's alt in the element body, so erasing it would stop that line from ever matching its own element — one such line can push a short document under the coverage floor and get the whole source skipped. Source files are opened through the product-wide path convention (absolute paths as-is, relative ones resolved against the repository root), so a historical `file_path` stored relative still reads no matter which working directory the command is launched from. Element ids under one anchor are fixed-width (`-gNNN`), so an anchor that would need a 1000th image skips it (`anchor_suffix_exhausted`) rather than minting an id that sorts out of order — `MINERU_MAX_IMAGES_PER_SOURCE` is a shared, unbounded deployment setting that the online parse path uses too, and it is deliberately not narrowed for this command's sake.

It is an explicit offline operation — never triggered from an interactive request — and idempotent: a restored element records the original `src`, so a rerun after recovering more of the output tree adds only the newly found images and rewrites nothing. Restarting after an interruption is therefore safe, but without `--after-id` it rescans every source from the beginning; pass the last source id of the previous run as a keyset start to skip that stretch. New elements are written with the source's existing element-batch `created_at`, which keeps both the source-detail `(created_at, id)` paging order and the command-catalog source generation stable; `sources.updated_at` advances in the same write transaction, together with the `chunk_elements` reverse rows. `chunked_at` is deliberately not cleared, so these sources are not re-flagged for reparse. Per-source image count and per-image byte ceilings reuse the deployment's existing `MINERU_MAX_IMAGES_PER_SOURCE` / `MINERU_MAX_IMAGE_BYTES` settings — insertions and in-place enrichments spend one shared per-source budget whose denominator is the existing image elements that already carry an asset, and either path reports `per_source_cap` once it is exhausted, and `MINERU_RETURN_IMAGES=false` refuses the run outright — as does an empty image index (every `--mineru-output` missing, or no file found under any `images/` directory), because running on with an empty index produces a normal-looking full pass in which every image is reported as not found. `--dry-run` is a read-only database pass that prints one line per source — coverage, candidates (split into new insertions and in-place enrichments), anchor failures, missing images and caption hits — and writes nothing; `--source-id` restricts the run to one source for a pilot (a miss prints which of the two reasons applies: not in this notebook, or not a Markdown source); `--limit` caps how many candidate sources are processed; `--report <path.jsonl>` appends per-source counts and stable reason codes only (never image bytes or document text), creating the parent directory if needed. Only PostgreSQL has a stopped-service preflight (`--confirm-service-stopped` is enforced before the repository is constructed); on SQLite this command can therefore run alongside a live service. Each source's write transaction opens with a compare-and-swap for exactly that reason: it re-reads the element generation signal (`COUNT` plus `MAX(created_at)`), every target chunk's current `element_ids`, and — for each in-place enrichment — that the target element's `asset_id` is still empty, aborting the whole transaction if any of the three has moved since the plan was made. That third check is written as an atomic conditional `UPDATE` judged by its affected-row count, not as a read followed by a write: PostgreSQL runs at READ COMMITTED and a bare `SELECT` takes no row lock, so two concurrent runs would both observe an empty `asset_id`, both pass, and the later commit would overwrite the earlier one. The third check is load-bearing on its own: a metadata-only enrichment changes neither the element generation nor any chunk, so two concurrent runs would otherwise both pass the first two checks and the second would overwrite the `asset_id` the first just wrote, stranding that asset row with no element referencing it and no reclaim path (reclaim only covers ids the current call minted). Without it a reparse landing between plan and apply would write the stale snapshot's `element_ids` back over the regenerated chunk and anchor images to element ids that no longer exist — silently, in all three cases. A source that loses the CAS is reported as `concurrent_change`, its pass assets are swept, and the run continues; it is counted separately from failures and does not affect the exit code, because the remedy is simply to run the command again. A run that had any genuinely failed source exits non-zero. A source whose write transaction fails is isolated and reported as failed, and the assets written for it in that pass — rows and files — are rolled back, because the orphan-asset sweeper deliberately never reclaims rows carrying a `source_id`. Each call also reclaims its own orphans on the way out, on both the success and the failure path, and reports the count as `orphan_assets_removed`. The rule is that it deletes only asset ids **it minted itself** and that no committed element references. That closes the one window this command can leak through — `save_source_image` commits the `notebook_assets` row *before* writing the file, so a disk-write failure raises without handing the caller a return value; the id is therefore captured by a callback the instant the row is committed. It is deliberately **not** any kind of set difference over the source's asset rows: on SQLite this command can run beside a live service, and the online parse path stores assets *before* it swaps elements, so a row appearing mid-run may well be legitimate data a concurrent reparse just created (the CAS covers elements and chunks, not assets). File names cannot tell them apart either — the online MinerU path stores `Path(img_path).name`, i.e. the very same `<sha>.jpg` shape. A deep copy is a second reason: it mints fresh `notebook_assets` ids without remapping `source_elements.metadata.asset_id`, so in a copied notebook every source-image asset row looks unreferenced. Residue from earlier passes — a hard kill inside that window — therefore still needs manual cleanup: rows in `notebook_assets` whose `source_id` is non-empty and which no `source_elements.metadata->>'asset_id'` points at, deleted together with their files under `<storage>/assets/<notebook_id>/`, and only in a notebook that is not a deep copy. A rerun rebuilds rather than reuses such an asset, so leaving them costs disk, not correctness.

The command does modify existing element rows in exactly one, registered way. The parse path does produce an `image` element for a *captioned* relative-path image (`![图 1 架构](images/a.jpg)`), recording `metadata.src` but no `asset_id` — that row is not in the "already restored" set, whose predicate is a non-empty `asset_id`. Inserting for it would create a second element for the same image. Instead the command enriches in place: after storing the asset it updates only that element's `metadata` to add `asset_id`, leaving `text`, `id` and `created_at` untouched, and appends the element to its anchor chunk only if it is not already in one. These are counted separately from insertions (`enriched` / `candidates_enrich`), and their captions count towards `captions` just as a restored image's does — the caption is read from the element itself, since enrichment never rewrites `text`. When several existing elements share one `src`, only the first by id order is enriched. Enrichment is **not** subject to the alignment guards: it finds its target by exact `src` equality and, when that element is in no chunk, walks back along element id order to the nearest chunked predecessor, so it never consults the Markdown alignment. That matters because a picture-only document has no text lines at all and therefore a coverage of 0 by construction — gating enrichment on coverage would mean those sources could never be repaired, which is exactly the population this command exists for. The coverage floor and the anchor-freshness rule apply to new insertions only.

Registered deviation: the standard chunking pipeline skips image elements that have neither caption nor description, while this command appends them to a chunk anyway. That is a deliberate, one-off exception for historical-data repair; its warrant is the physical adjacency between the image reference and that passage in the Markdown (the original PDF's layout order), not the "this image carries retrievable text of its own" rule chunking relies on.

The `metadata` subcommand backfills paper metadata (title, authors, affiliations, venue, year) for a notebook's sources that don't have it yet — useful for a library ingested before paper-metadata extraction existed, or after upgrading the extraction prompt/schema and wanting a refresh. It only targets sources that have already been parsed and look like an academic paper (empty or `academic_paper` doc type); it reads text from the already-stored parsed elements, so the original PDF doesn't need to still be on disk. It requires `--notebook-id` (this subcommand never creates a notebook) and a configured service binding for the `paper_metadata` workload; it errors out rather than silently skipping when that workload is unbound, and needs no embedding workload. It's idempotent and restartable: sources that already have a metadata row are skipped on a re-run; pass `--force` to re-extract everything in scope regardless (e.g. after a prompt/validation upgrade). Progress prints one line per completed source (`[meta <done>] <source-id> <status>`) followed by a final JSON summary of status counts.

The `question-index` subcommand is the only builder for the optional generated-question recall supplement. It requires `--notebook-id`, `GENERATED_QUESTION_INDEX_MODE=shadow|on`, and configured `chunk_question_generation` plus `chunk_embedding` workloads. It keyset-pages original chunks, generates bounded answerable questions, embeds each question independently, and atomically replaces that chunk's question rows plus completion marker. A successful empty result is marked complete so a rerun does not spend the model call again; failed chunks remain retryable. By default already completed chunks are skipped; `--force` deliberately regenerates them after a prompt/model change. Each stored row points to its original chunk, and online retrieval hydrates only that original text/evidence. Begin with `shadow`, inspect counts-only `chunk_question_index_query` events, and use `on` only after paired A/B shows value. Exact rollout rails are documented only in the product/API reference.
Its completion summary counts all persistently processed chunks in `indexed_chunks`, including successful empty/skipped results, and reports the subset with at least one stored question separately as `question_bearing_chunks`.

The `reparse` subcommand fixes a class of historical leftover: sources that were created and whose `parse_status` looks advanced, yet have no `source_elements` (a prior parse that was interrupted or never landed). KG extraction has a zero-LLM grounding check — every node the LLM emits must bind its quoted evidence back to one of the source's elements, or it is dropped; a source with no elements has *all* of its extracted nodes discarded, so `knowledge_objects` never grows (the extraction is wasted) and re-extracting directly never recovers it. Older `all` resume logic used "has KG?" as a proxy for "has been parsed?", routing these element-less sources straight to extraction — exactly this trap (that split is now fixed, so fresh imports no longer hit it). This command re-runs `process_source` (parse → generate elements) for every source in the notebook missing `source_elements`, then does one KG rebuild; sources that already have elements are skipped (idempotent, restartable). `--limit N` processes only the first N; `--no-rebuild` skips the closing clustering (batched runs). Requires `--notebook-id`.

`kg --retry-partial` repairs a different state: the source still has graph objects, but its latest KG extraction record reports `windows_failed>0`. Normal incremental `kg` intentionally treats a completed graph-bearing source as covered, so it does not revisit these rows. The explicit repair flag adds them to the normal missing-KG target set. For each partial source, the existing objects and relations remain readable while model windows run. If any retry window fails or the replacement is empty, the batch counts that attempt as failed/incomplete and the old graph remains untouched; only a non-empty, zero-failed-window result transactionally replaces that source's graph. `--limit N` bounds the combined missing + partial target set, `--no-rebuild` supports batching, and the final rebuild/index flow is unchanged. This is not a parser repair; sources without `source_elements` still require `reparse`.

**Interrupting a `kg` run, and the one-analysis-per-library guard.** A notebook may have only one analysis task in progress at a time (a partial unique index on the durable task table). A run that ends cleanly — success, model failure, Ctrl-C, or `kill` (SIGTERM) — settles its task row, so re-running the same command simply continues with what is left. Every `kg` extraction shape, including `--limit` and `--retry-partial`, stops in-flight model windows cooperatively and then waits for extraction or finalizer executors to return before releasing the library, so the command may take up to one model timeout to exit. The first SIGINT/SIGTERM/SIGHUP starts that cleanup; repeats are temporarily absorbed until the original handlers are restored, so they cannot break executor shutdown and leave old writers alive after the durable guard is released. What cleanup will not do is keep spending model budget on the remaining queue; the library then shows the analysis as interrupted, with everything already extracted kept. `nohup` runs are unaffected: an already-ignored SIGHUP stays ignored, so a dropped SSH session does not kill the batch. Non-durable pooled phases (`all`, `reparse`, `ingest`, and `metadata`) retain their existing signal behavior; on Ctrl-C/SystemExit they cancel queued work and drain every accepted task before releasing the offline lock and repository.

Only an uncatchable end (`kill -9`, OOM kill, power loss, host reboot) can leave the task row stuck in progress. The offline command deliberately does not clear it — it cannot tell whether that row belongs to a backend that is still running — so it reports the existing task (stage, completed/total, last update) and exits with status 2 instead of a database error. If the last-update timestamp has been frozen for a long time, the row is leftover: **restarting the backend clears it** (startup recovery settles every task left in progress by a previous process, along with stranded parses and projections). If instead it is genuinely running (another `batch_ingest` process, or an analysis started from the web UI), wait for it and re-run.

**MRL truncation quality spike (`app.eval.mrl_truncation`).** Answers "how much retrieval quality do we lose if we truncate stored embeddings to their first 1024/2048 dimensions (+ re-normalize)?" — the gate for both shrinking in-process vector memory (~4× at 4096→1024) and for pgvector HNSW indexing (which caps at 2000/4000 dims). Read-only, streams the DB in blocks (bounded memory on million-row tables), and always prints the per-table embedding row counts for the notebook first.

```bash
# neighbor-preservation mode (default): zero API calls, works on any notebook —
# samples stored vectors as queries and compares full-dim vs truncated top-K rankings
( cd backend && python -m app.eval.mrl_truncation )                          # auto-picks the biggest notebook
( cd backend && python -m app.eval.mrl_truncation --notebook nb-xxxx --tables knowledge,chunk,relation --dims 2048,1024 )
# very large tables (e.g. millions of relation rows): subsample the corpus side too —
# rankings are compared within the same subsample, so the full-vs-truncated relative
# comparison stays valid (slightly optimistic on sparse subsets; re-run full for borderline calls)
( cd backend && python -m app.eval.mrl_truncation --tables relation --sample-rows 50000 )

# gold mode (needs the `chunk_embedding` workload bound; embeds each question once at native dim):
# recall@12 / MRR relative degradation per truncation tier against the committed gold set
( cd backend && python -m app.eval.mrl_truncation --gold app/eval/recall_gold.yaml --notebook nb-b37185f4ae )
```

Decision thresholds (from the pgvector migration review spec): 2048 passes if recall@12 drops ≤1pt with top-10 overlap ≥0.9 (→ `halfvec 2048`); 1024 passes if the drop is ≤3pt (→ `vector 1024`); a drop >5pt fails the tier. Paste the full output back for a verdict.

**Large base KGs (10^5–10^6 objects).** The final unified clustering streams (bounded by the number of unique normalized concept names, not the total object count), so `kg` scales without materializing all vectors. For a very large corpus you can extract in batches and cluster once at the end:

```bash
# extract in chunks across runs without the (expensive) final clustering
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 1000 --no-rebuild   # repeat as needed
# then cluster + (re)build the scale index once, no extraction
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --rebuild-only
```

`--limit` bounds only how many sources are *extracted* this run; the final clustering always covers the whole notebook. After a `kg` rebuild on a large notebook (see `SCALE_INDEX_AUTO_ENABLED` above) the scalable-retrieval index is rebuilt automatically (so it never goes stale). `KG_CLUSTER_REP_ANN_MAX` (default 2,000,000) caps the rep-ANN size — above it the index is built in shards with a warning (never silently truncated).

All `kg` extraction variants use the same durable, notebook-scoped job as the page Analyze action: model probing, one-running-job enforcement, circuit breaking, progress, and interrupt draining are shared. `--limit` is applied while walking bounded raw-source keyset pages, so a sparse PostgreSQL library does not scan until it has accumulated a full eligible page. Batch-only node-vector backfill runs before unified clustering, and clustering plus conditional scale-index rebuild must finish before the durable job is marked successful. The page's continue action includes partial completed runs automatically; CLI operators opt in with `--retry-partial`. Both keep the old graph readable until a complete replacement commits.

**Batch concurrency.** `--workers` controls source/document jobs only and falls back to `KG_JOB_CONCURRENCY`. An explicit value is applied before repository construction, so the process-owned KG business scheduler used by `kg`/`all`/`reparse` receives it. It also dispatches file parsing in `ingest`; in `vectors-to-blob`, it instead selects the parse/re-encode process-pool size (default `min(32, cpu_count())`; `1` disables that pool).

Every model call made by `all`, `kg`, `reparse`, `metadata`, `ingest`, or `embed` goes through the same system model-service scheduler as online requests. The service bound to each workload supplies its sole model-capacity setting, `max_concurrency`; there are no batch CLI overrides for model concurrency, and increasing `--workers` never multiplies that service limit. If a throttled run leaves vectors missing, repair them later with the `embed` subcommand.

For a larger source pipeline whose model capacity is already declared in the deployment TOML:

```bash
PYTHONPATH=backend python scripts/batch_ingest.py reparse \
  --notebook-id nb-xxxx \
  --workers 32 \
  --pool-report-interval 5
```

- `--pool-report-interval` — in the `all`, `kg`, and `reparse` phases, print producer/source business-pool utilization every N seconds (default 15; `0` disables it). This is not the model-capacity authority; inspect the read-only Model Services status for per-service running/queued counts, health, and breaker state.

Options: `--owner` (notebook owner username, case-insensitive; defaults to the admin user), `--workers` (source-pipeline concurrency = `KG_JOB_CONCURRENCY`; in `vectors-to-blob`, parse/encode process-pool size, default `min(32, cpu_count())`, `1` = no pool), `--limit` (kg extraction subset — clustering still covers the whole notebook), `--retry-partial` (`kg` only: safely retry graph-bearing sources whose latest run has failed windows), `--no-rebuild` / `--rebuild-only` (split extraction from the final clustering for batched large builds), `--fresh` (clears the rebuild checkpoint to force a full re-run of merge-review + concept-description adjudication; use when you changed the KG model/thresholds but the data is unchanged — implies a forced rebuild, and also applies to the `all` phase's final clustering), `--allow-no-embed` (explicitly allow running when `chunk_embedding` is unbound; refused by default, never silent; ignored by the `embed` subcommand), `--pool-report-interval` (seconds between producer/source business-pool reports in `all`/`kg`/`reparse`; default 15, `0` off), `--all-notebooks` (`vectors-to-blob` / `backfill-source-index` / `backfill-chunk-elements` only: act on every notebook instead of one), `--force` (`metadata`, `question-index`, `backfill-source-index`, `backfill-chunk-elements`, or `backfill-source-facts`: intentionally rebuild completed state), `--mineru-output` / `--source-id` / `--after-id` / `--report` (`backfill-images` only: repeatable MinerU output roots, a single-source pilot restriction, a keyset resume point, and a per-source JSONL detail file carrying counts and stable reason codes only), `--dry-run` (scan & estimate only; for `backfill-images` this is a read-only database pass rather than an input-directory preview). The `embed` subcommand backfills only missing chunk + element + node vectors and requires `--notebook-id`. The `vectors-to-blob` subcommand migrates legacy JSON-text vectors to BLOB and requires `--notebook-id` or `--all-notebooks`. The `backfill-source-index` subcommand proactively builds the source-deletion reverse index and requires `--notebook-id` or `--all-notebooks`. The `backfill-chunk-elements` subcommand proactively builds the element -> chunk reverse index and has the same requirement. The `backfill-images` subcommand surgically restores dropped Markdown source images and requires `--notebook-id` plus at least one `--mineru-output` tree. The `metadata` subcommand backfills paper metadata (title/authors/venue/year) for already-parsed academic-paper sources and requires `--notebook-id` plus a `paper_metadata` binding. The `question-index` subcommand requires one notebook, an explicit rollout mode, and both generated-question model bindings.

Prereqs: point `MODEL_SERVICES_CONFIG` at the deployment TOML, bind the workloads required by the selected phase (notably `chunk_embedding`, `source_element_embedding`, `knowledge_object_embedding`, `kg_extract`, `paper_metadata`, and optional `chunk_question_generation`), and place only the referenced secrets in `.env`. If `chunk_embedding` is unbound, the CLI **refuses to run by default** — pass `--allow-no-embed` to import without vectors, never silently; phases whose required chat workload is unbound fail clearly. A re-run resumes from **database state**, not a progress file: `ingest` checks content hashes, `kg` checks the latest extraction run, and `embed` checks vector rows. Because a hash is stored before parsing completes, repair interrupted sources without elements using `reparse`. `<storage>/batch_ingest/<notebook>.jsonl` is a write-only run log.

### Large-library retrieval hot path

Indexed KG retrieval must remain bounded after ANN candidate generation. The isolated-node rank penalty probes each candidate with indexed `EXISTS` checks and returns only connected candidate ids; it never fetches a hub's complete adjacency list. Canonical folding reads mappings only for the scored ids through `cluster_fold_rows`. Concurrent reasoning subqueries share one lazy ANN load per scale-index instance and artifact kind. These optimizations preserve the retrieved ids, scores, thresholds, PPR behavior, and recall.

Incremental KG fusion likewise does not fetch every existing concept payload when a usable
object ANN is present; that full read remains only in the mutually exclusive no-ANN
brute-force/threshold branches. Scale-index PPR still evaluates every score needed for the
same global min/max normalization, but production hydration keeps only the configured stable
Top-K instead of sorting/materializing the complete chunk ranking. Ties preserve input order,
and the bounded sequence must be the exact prefix of the unbounded diagnostic result.

Current scale-index builds and delta folds also write `chunk_ann_source_names.npy`, `chunk_ann_source_codes.npy`, and `chunk_ann_source_counts.npy`. These compact, row-aligned files let source-narrowed chunk ANN reject excluded rows inside HNSW before Top-K. Published older indexes remain loadable, but narrowed chunk/element retrieval uses bounded source-filtered FTS until that notebook is rebuilt or folded; rebuild the scale index after deploying this version when immediate cross-language scoped semantic recall is required. A missing file when `has_chunk_ann_sources=true`, a row-count mismatch, or an out-of-range code makes the artifact unusable rather than silently weakening the source boundary.

The same `viz.npz` now carries the stable degree order and by-source edge order/indptr used by bounded graph views. Older compact and legacy JSON artifacts remain loadable and derive these arrays once on demand; rebuilding moves that cost into artifact publication. A bounded core request enumerates only the kept nodes' outgoing segments and restores the original edge stream order. Multi-library PPR remains lazy, but its ordered participant graph is assembled and normalized once rather than copying the cumulative CSR after every mounted library; differential tests pin the historical PPR scores and ranking. Maintenance submissions enter separate fixed heavy/light worker queues, so a burst increases queue entries rather than blocked OS threads. Queue wait disclosure remains metadata-only.

With the default `SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED=true`, the same offline index command also rebuilds `<storage>/kg_index_partitions/<notebook-id>` after the main scale artifact is published. The companion is staged and swapped atomically, contains one SHA-256-addressed directory per visible source, hashes every partition payload, and is bound to the current main manifest version. A failed, over-limit, or corrupt companion does not damage the legacy scale index, but its reader stays unavailable; older companion formats require a rebuild. Re-run the index command after repairing source-fact coverage or adjusting the documented source-subgraph rails; do not copy a companion root between scale-index generations. `SOURCE_PARTITIONED_PPR_ENABLED` may be turned off independently while retaining the files.

For a production regression, capture stacks and the slow-stage breakdown with `python3 scripts/diag.py incident` and `python3 scripts/diag.py slow --since 6 --deep`. In `_retrieve_scored` events, compare `ann_ms`, `hydrate_ms`, and `fold_ms`; a small candidate count must not cause hydration work proportional to total relation or cluster rows. Before/after acceptance should use the exact replay comparison below.

### Retrieval replay diff (`scripts/replay_retrieval.py`)

The acceptance tool for proving "retrieval quality is unchanged" across a performance-optimization change: run a fixed question set through the reasoning retrieval primitives (`federated_retrieve` + `ppr_retrieve`), **without calling any answer LLM**, and record the hit id/score sequences as JSON. Two runs' outputs can then be diffed question-by-question.

```bash
# record a run (needs the `chunk_embedding` workload bound for real query vectors; reads retrieval primitives only, no chat model required)
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt --out before.json

# --full: also runs the complete reasoning-orchestration layer once (plan/reflect are replaced with a
# fixed-sub-query + immediate-answer stub instead of the LLM, verifying the deterministic parts of the
# orchestration layer are equivalent); sub-queries come from plan.json
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt \
    --full --plan-file plan.json --out before.json

# record again after the change, then diff the two runs
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt --out after.json
python scripts/replay_retrieval.py --compare before.json after.json                     # --mode exact (default): id + score sequence must match position-for-position
python scripts/replay_retrieval.py --compare before.json after.json --mode topk --k 30  # only compares top-k id set overlap + order (tolerates score drift from e.g. float32 conversion)
```

`questions.txt` has one question per line; `plan.json` = `{"<question>": ["sub-query 1", "sub-query 2", ...]}`. **Must be run from the main checkout root** (`.env` is loaded relative to the current working directory, same as `batch_ingest.py`). `--owner` reuses the same owner-resolution as `batch_ingest.py` (case-insensitive username, defaults to `"admin"`).

Exit codes are the verdict — safe to wire directly into CI or a script gate: `0` success (recording) or all questions match (`--compare`); `1` `--compare` found a mismatch (runs differ); `2` a precondition failed before any comparison happened (`retrieval_query_embedding` unbound, notebook not found, or owner not found) — the CLI **errors out immediately** rather than silently producing a misleading "zero recall" comparison from zero vectors.

### One-shot selected-source shadow preparation (`scripts/prepare_selected_source_graph.py`)

For an existing deployment, stop the API and every background writer, then run from the repository root:

```bash
PYTHONPATH=backend python scripts/prepare_selected_source_graph.py \
  --env-file /path/to/deployment.env \
  --confirm-service-stopped
```

The env file must already exist; this prevents a mistyped production path from silently selecting the local default database. For this command it is the authoritative settings source: exported shell variables cannot redirect maintenance to a different database/storage root. The final atomic edit preserves the existing env file's mode, owner, group, and supported metadata; only a newly created receipt is forced to 0600. The confirmation is an operator assertion; the script does not stop services. It always covers every notebook in the configured database and holds the central offline-maintenance lock for the database phase. Its execution state is:

1. Open the repository, apply pending schema migrations, and inventory notebooks.
2. Resume each notebook's reverse source index from its durable page cursor. A current completed generation is skipped; KG generation drift fails closed and is restarted on the next attempt.
3. Resume source-fact projection through the existing per-source generation ledgers. Any busy, generation-less, failed, or incomplete source blocks activation.
4. Revalidate the main scale manifest and source-partition root against the current KG version and visible-source count. A current complete companion is skipped; otherwise the ordinary bounded builder republishes it and the script revalidates the result.
5. Independently reconcile source facts for every notebook while the maintenance lock is still held. Only counts and stable state codes enter the receipt.
6. Close the repository and release its lock. Only then atomically update the env file to `SOURCE_SUBGRAPH_PPR_ENABLED=true`, `SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED=true`, `SOURCE_PARTITIONED_PPR_ENABLED=true`, and `SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow`.

The default 0600 receipt is `STORAGE_DIR/maintenance/selected-source-graph-deployment.json`. It is informational and content-free; database ledgers and artifact manifests remain authoritative. Re-running the command revalidates everything, resumes committed pages, and skips current artifacts. A failure records a stable phase/code, leaves the four env assignments untouched, and exits nonzero. After success, restart the deployment so it reads the new env file. Shadow remains absent from public APIs, traces, streams, and UI, and cannot change retrieval results.

### Selected-source graph quality gate (`scripts/eval_selected_source_graph.py`)

Before enabling a user-visible selected-source graph lane, record the mandatory golden cases twice under one frozen model/corpus/source contract: once with the historical baseline and once with graph enrichment in shadow. Store that paired observation JSON in a deployment-owned trusted artifact directory, then run:

```bash
PYTHONPATH=backend python scripts/eval_selected_source_graph.py \
  /trusted/eval/paired.json \
  --output /trusted/eval/selected-source-attestation.json
```

Exit `0` means every hard isolation/baseline invariant, per-case and aggregate quality rail, and cost rail passed; exit `2` means rollout is blocked and prints each failure. The output attestation deliberately excludes questions, answer text, evidence/citation/source ids, and excerpts. Its SHA-256 digest detects accidental mutation only—it is not a signature or an authorization boundary. Keep both input and output in a trusted location, restrict writers, and pin the expected corpus signature and exact model/sampling contract before any active rollout. `shadow` remains safe without an attestation because it cannot change user-visible output.

`--golden` is a diagnostic-only override. Its output can be inspected locally, but production activation accepts only the canonical suite digest shipped with this release; weakening or replacing the cases cannot produce an activatable artifact.

The shipped defaults already run `SOURCE_SUBGRAPH_PPR_ENABLED`, partition publication/reading, and `SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow`; this control state stays in operator-only internal telemetry and is filtered from user-readable logs and UI. Build or refresh companions for oversized sources before judging shadow coverage. After the canonical gate passes, configure the trusted attestation path plus exact corpus/model pins, then move through `allowlist` or stable-hash `rollout` before `on`. Roll back with the single `SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=off` switch; this immediately restores the historical `B` path and does not require deleting artifacts. Never copy an attestation between a different corpus signature or model contract.

### Merging two shared-base deployments (`scripts/merge_dbs.py`)

Offline, non-destructive tool for consolidating two separately-deployed silicon-notebook instances that share exactly one common public knowledge base (same base notebook id) back into one. It keeps the fuller side's base — you pick with `--keep-base`, and the tool prints both sides' base stats (`sources`/`chunks`/`knowledge_objects` counts) up front so you can confirm the choice — while every other (personal) notebook from both sides is carried over untouched, including each one's own reference-library mounts (`notebook_bases`). Source `.db`/storage files are only read; the tool always writes new `--out` / `--out-storage` files. Either input may be on an older schema version — each is migrated to current (in a private temp copy) before merging. Multi-domain deployments can have more than one public knowledge base per side: this tool does not support that shape and refuses to guess — if either side has more than one `tier='base'` notebook, it aborts immediately, naming the side and every candidate, instead of picking one.

```bash
PYTHONPATH=backend python scripts/merge_dbs.py \
  --db-a A/silicon_notebook.db --storage-a A/storage \
  --db-b B/silicon_notebook.db --storage-b B/storage \
  --keep-base a \
  --out merged/silicon_notebook.db --out-storage merged/storage \
  --assume-same-users
```

- `--keep-base a|b` — which side's base notebook survives (normally the fuller one).
- `--assume-same-users` — required when both databases share user id(s); confirms it really is the same person's account on both sides, otherwise the tool aborts to avoid mis-attributing content.
- `--dry-run` — migrates, validates, and prints which notebooks would be imported, without writing anything; works even if `--out` already exists.
- `--force` — overwrite an existing `--out` file.

Preconditions: each side must have exactly one `tier='base'` public knowledge base (see above); and apart from that shared base, notebook ids must not overlap between the two databases — the tool aborts and lists the colliding ids if they do.

**Important:** point `--db-a`/`--db-b` at quiesced database files — stop each source deployment first. The tool only copies the `.db` file itself; a live deployment's pending `-wal` sidecar is not picked up, so merging straight from a running instance can silently miss recent writes. (The tool's own schema-migration writes are checkpointed back into the `.db` before it's used, so that part is safe — this caveat is about the source files you hand it.)

**Reference-library mounts on the losing base:** the surviving base notebook (the `--keep-base` side) keeps only its own reference-library mounts; if the *other* side's base notebook had mounted other reference libraries, those mounts are dropped, same as every other piece of notebook-scoped data the non-surviving base owns (its sources, chunks, knowledge objects, ...). Every other (personal) notebook's own mounts are carried over untouched from both sides.

**Groups and authorization edges (group knowledge sharing):** `groups` and `group_members` merge as a **global union with the primary side winning** — the same treatment as `users` / `agent_profiles` / `agent_access_tokens`, since neither table hangs off a notebook and there is no "excluded by the secondary notebook filter" case for them. De-duplication is by primary key (`groups.id`, and `(group_id, user_id)`), so a secondary-side row whose id collides with a primary-side one is discarded rather than merged field-by-field. Group ids are random uuids specifically so that cross-deployment collisions do not happen by accident.

`notebook_grants`, by contrast, is imported per notebook, and the two rules do not agree. That mismatch is the **only** way an orphan edge can arise (day-to-day group deletion clears its edges inside the same write transaction), so the merge sweeps them itself: `sweep_orphan_group_grants` runs **after** the global-union merge (which is what settles the final `groups` set) and **before** `PRAGMA foreign_key_check`, deleting rows whose `principal_type` is `group` or `group_admins` and whose `principal_id` is not in `groups`, and logging the count. It cannot be left to the database: `principal_id` is a deliberately foreign-key-free polymorphic column, so `foreign_key_check` never sees these rows. The predicate matches only the two *group* principals — `user` and `everyone` `principal_id`s do not point at `groups` at all, and sweeping them would delete two classes of perfectly normal grants. Leaving an orphan would not be an immediate privilege leak (the predicate joins `group_members` and simply fails), but it would permanently litter the owner's sharing list, and a later group created with a colliding id would **revive it as a real grant**.

Edges on the non-surviving base notebook are dropped with everything else that notebook owns, exactly like its mounts, sources and knowledge objects — the same caveat as the paragraph above. After merging, review each library's sharing list: any edge that survived but points at a group whose membership came from the other side should be re-confirmed.

**Graph state and graph-analysis artifacts of imported notebooks are reset:** for every notebook carried over from the secondary side, its graph build state and the precomputed artifacts behind the "Graph analysis" report are not preserved (the former is cleared after import, the latter is never imported). Both are derived data, and the version stamps they carry are only meaningful inside the database they were computed in — keeping them would make the analysis report compare the source database's stamp against the merged database's current state and raise a "this figure is newer than the current content" alarm that should never occur. Reset, those reports honestly read "never computed", and the rebuild below recomputes them along with the topic boards.

After merging, deploy the `merged/` output (db + storage) to whichever host keeps running, and on first start trigger an index rebuild in the app ("Rebuild index" / "Refresh graph") to regenerate the `kg_index`/`kg_viz`/ANN artifacts, which are intentionally not copied.

### Backfilling legacy promotion-candidate targets (`scripts/backfill_promotion_targets.py`)

Upgrading to `SCHEMA_VERSION>=20` (multi-domain reference libraries) adds `promotion_candidates.target_base_id`, but the migration only adds the column — it does not backfill existing rows. Any promotion candidate that was still `proposed`/`under_review` from before the upgrade keeps an empty `target_base_id`, and approval fails for it (target_base_id is otherwise only ever set when the candidate is first proposed). Run this offline tool once after upgrading if the deployment might have such candidates; it resolves each one the same way the propose-time flow does — via the candidate's notebook's mounted public knowledge bases (mounting 0 blocks it, exactly 1 auto-resolves, more than 1 needs an explicit target) — reusing the single shared `GovernanceStore.mounted_public_base_ids` rather than a second copy of that rule.

```bash
PYTHONPATH=backend python scripts/backfill_promotion_targets.py --db .local/silicon_notebook.db list
PYTHONPATH=backend python scripts/backfill_promotion_targets.py --db .local/silicon_notebook.db apply \
  [--set NOTEBOOK_ID=BASE_ID ...] [--dry-run]
```

- `list` — read-only report: every `proposed`/`under_review` candidate with an empty `target_base_id`, grouped by notebook, alongside that notebook's mounted public knowledge bases and how each candidate would resolve.
- `apply` — writes `target_base_id` for every candidate that resolves unambiguously (auto, or via `--set`); candidates that are still blocked (notebook has no mount) or ambiguous (multiple mounts and no matching `--set`) are left untouched and reported, so a second pass after mounting a base (or supplying `--set`) picks up exactly the remainder. Writes immediately by default, matching `merge_dbs.py`'s convention; pass `--dry-run` to preview without writing.
- `--set NOTEBOOK_ID=BASE_ID` — required to resolve a notebook that has more than one mounted public knowledge base; repeatable for several notebooks. A target outside that notebook's mounted set aborts the entire run before any row is written (no partial writes).

Unlike `merge_dbs.py`'s always-write-a-new-output convention, this tool patches the `--db` path you give it in place, and refuses to run against a database that has not yet migrated to `SCHEMA_VERSION>=20`.

**Important:** stop the backend service before running `apply` — this tool opens the `--db` file directly with no `busy_timeout`, so writing against it while the backend holds it open can collide with a live transaction.
### Backfilling knowhow-cell Markdown formatting (`scripts/backfill_knowhow_md.py`)

Knowhow tables normalize Excel-style formatting (Tab-indented `•` bullets, `A.`/`a.` section/sub markers, soft line breaks) into clean CommonMark automatically for new imports/appends and for the per-cell "reformat" action, but that normalization doesn't retroactively touch cells that already existed before it was turned on. This one-time CLI backfills those existing (存量) cells for a given notebook.

**Dry-run first, then apply a reviewed plan file.** A dry-run never writes to the database; it always saves the full plan to a JSON file (and prints its path) so you can review exactly what would change, then re-applies *that file* verbatim — so what lands is exactly what you reviewed.

**The default dry-run is read-only and safe to run anytime.** The default (rules-only) dry-run opens the database read-only and never constructs the write-capable repository, so it is safe to run against a live/busy backend. `--use-llm` (needs the rewrite model) and `--apply` (writes) instead open the database read-write, which may run any pending schema migrations and crash-recovery on open — the tool prints a one-line notice when it does this, so prefer running those when the backend is idle.

```bash
# dry-run (default): prints the per-cell diff + summary AND writes a plan file
# (default .local/backfill_plans/knowhow_md_<notebook>_<timestamp>.json) — no DB writes
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx

# after reviewing the plan file, apply it verbatim (deterministic rules, no LLM
# involved) — every --apply REQUIRES --plan
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --apply --plan <plan.json>

# LLM-backed reformatter (reformat -> content-invariance check -> rule fallback per cell):
# dry-run to review, then apply the reviewed plan (same --apply --plan handshake)
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --use-llm
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --use-llm --apply --plan <plan.json>
```

- `--notebook` (required) — the notebook to backfill.
- `--apply` — write the changes; it **requires** `--plan PATH` and applies that reviewed plan file verbatim. A cell whose stored content changed since the dry-run (someone edited it after review) is skipped and reported rather than overwritten on top of a moved target. Each written row is marked pending so the background projector recomputes its KG/steps (reprojection runs synchronously before the command exits). A plan-less `--apply` is a hard error: re-planning from the *current* database at apply time would pick up any cell edited after the reviewed dry-run and write it despite never being reviewed (and for `--use-llm` the stochastic rewrite model would produce different candidates entirely) — so run the dry-run, review its plan file, then apply *that*.
- `--use-llm` — reformat each cell through the system service bound to `knowhow_reformat` (with its own zero-LLM content-invariance check and automatic fallback to the deterministic rules) instead of the default, always-available rule-based normalizer. If that workload is unbound, or its output fails the invariance check and falls back to rules, the tool prints an explicit `WARNING` rather than silently pretending the LLM ran.
- `--save-plan PATH` — override where the dry-run writes the plan file.
- `--plan PATH` — the reviewed plan file to apply (see `--apply`).

The **anchor (row-title) column is never reformatted** by any bulk path (import, append, or this backfill) — it's a grouping key that must stay byte-stable, so normalizing it would split freshly-touched rows off from their existing concept group. (Only the explicit per-cell "reformat" action in the editor, where a human reviews the suggestion and all sibling rows are rewritten together, may touch it.)

Only an interactive row/table reformat batch's save unit has this guarded
concurrency contract; ordinary shared-cell edits and ordinary APIs do not gain
it. When the batch opens, it freezes the complete table snapshot, including the
exact member set only for each non-empty anchor group covered by a complete
anchor-group save unit (a merged shared-column fan-out or a singleton complete
group). One SQLite write transaction revalidates every write target's expected
content baseline, the active anchor designation, and the exact membership of
those covered frozen groups. A non-shared column in a multi-row anchor group is
a valid subset write: it checks only its write-target baselines, not the whole
group membership guard. Any applicable content, anchor, or membership drift
rejects the entire save unit with HTTP 409 and zero partial writes. The UI
retains the generated reformat candidates as stale, requires the user to rerun
the reformat, and refreshes the table after the batch dialog closes.

Schema v21 indexes `(column_id, JS-trim(content_md), row_id)`. The guarded
membership check uses that same exact normalization as an equality predicate,
so complete anchor-group verification remains fail-closed while it seeks the
group instead of scanning the entire anchor column inside its write transaction.

Must be run from the main checkout root (it needs the real `.env`/database configuration, same as `batch_ingest.py`/`replay_retrieval.py` above). Safe to re-run: applying the same plan again is a no-op (each already-applied cell no longer matches its recorded "before").
