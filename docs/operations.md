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

## SQLite / PostgreSQL cutover and rollback

There is exactly one active database and no application dual-write. The included importer is
one-way SQLite→PostgreSQL snapshot migration; changing `DATABASE_URL` alone only opens a
different datastore. It does not import MySQL, continuously capture later writes, replay
PostgreSQL→SQLite, or copy source/upload/asset files.

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
or writing the target. Ensure free space for a SQLite snapshot and upgrade working copy plus
the PostgreSQL database and indexes. `pg_trgm` must be creatable in `public`.

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
- After the first PostgreSQL business write, editing the URL back loses that write. This
  repository has no reverse importer or dual-write log. Freeze again, externally reconcile
  PostgreSQL→SQLite (including storage effects), verify both sides, and only then reopen
  SQLite. If that process has not been designed and rehearsed, PostgreSQL is the rollback
  boundary.
- Merely toggling the URL during development selects two independent histories. Neither side
  is kept synchronized. Never run SQLite-only maintenance or `scripts/batch_ingest.py`
  mutation phases while PostgreSQL is selected.

## PDF parsing with MinerU

PDF parsing is decoupled from the GPU. The backend never imports torch; it talks to MinerU only when configured, and otherwise uses the pypdf text fallback.

- **Local / no GPU**: keep `MINERU_MODE=off`. PDFs use pypdf (plain text only).
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

**URL sources ("Add link") prefer local MinerU.** A pasted public PDF URL is parsed by the local MinerU service whenever one is configured (`MINERU_MODE=http`/`cli`): the backend downloads the PDF and runs it through the same local-MinerU→pypdf path as file uploads. For SSRF protection, the downloader validates the initial target and every redirect and rejects localhost, private, link-local, and reserved addresses; import internal documents through file upload instead. The `MINERU_API_TOKEN` cloud (mineru.net) path is used only as a fallback when no local MinerU is configured — and once local is in use it is never silently called. Adding a URL requires *either* a local MinerU or the cloud token. Uploaded files follow the same rule: when local MinerU is off and only the cloud token is configured, uploads are parsed via that same cloud v4 path (images, formulas, and tables included), falling back to pypdf automatically if the cloud call fails.

MinerU output maps to structured `SourceElement`s: formulas become `formula` elements (LaTeX preserved), tables become `table` elements (HTML kept in metadata), and headings keep their level. The frontend renders these in the source detail view — formulas via KaTeX, tables from their HTML — so equations show typeset rather than as raw LaTeX. If MinerU is unreachable or errors, ingestion degrades to pypdf so uploads never block, while pipeline logs and the source `error_message` keep the fallback diagnostic; a PDF that parses to zero text (e.g. a scanned/image PDF) is flagged with a hint instead of looking like an empty success. On desktop, the source-detail window uses a conventional close control and can be dragged by its header — as can the app's other centered floating dialogs (the model-service status, add-source, knowledge/graph, report, and confirmation modals), all sharing one drag hook; narrow screens keep the fixed modal layout, and the detail body remains independently scrollable.

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

# backfill missing paper metadata (title/authors/affiliations/venue/year) for a notebook's
# already-parsed academic-paper sources (idempotent; requires `paper_metadata` bound, no embedding call)
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx --force

# fix historically empty sources: re-parse sources missing source_elements (a prior parse that
# never landed) to backfill elements, then re-extract KG
PYTHONPATH=backend python scripts/batch_ingest.py reparse --notebook-id nb-xxxx
```

The `embed` subcommand re-fills only the chunk and KG-node vectors that are *missing* (e.g. after a throttled run left gaps). It requires `--notebook-id` and a configured service binding for the `chunk_embedding` workload — being a vector-backfill command, it ignores `--allow-no-embed` and errors out if that workload is unbound.

The `vectors-to-blob` subcommand is a one-time storage migration: embedding vectors used to be stored as JSON text in SQLite, which means loading hundreds of thousands of rows into a matrix (index builds, retrieval cold start) spends most of its time in `json.loads`. New writes are now stored as raw float32 BLOBs (`np.frombuffer` reinterprets them with zero parsing), and every reader already accepts either format — so this command is optional but recommended after upgrading: it re-encodes any pre-existing JSON-text rows across all four embeddings tables (`chunk_embeddings`, `knowledge_embeddings`, `element_embeddings`, `relation_embeddings`) in place, in batched transactions (5,000 rows/commit) with progress printed per table. It does **not** compute new vectors (so it needs no model-service binding) and is idempotent/restartable — re-running it converts nothing further, since it only selects rows SQLite still types as `text`. Use `--notebook-id` to scope it to one library or `--all-notebooks` to convert every notebook in the database. The `json.loads`/re-encode step (the single-core bottleneck at millions-of-rows scale) is parallelized across `--workers` processes (default `min(32, cpu_count())`; `--workers 1` uses no process pool at all) — the main process still owns every DB read/write, so SQLite stays single-writer. If the worker pool crashes it falls back to a serial pass automatically rather than losing the run.

The `backfill-source-index` subcommand proactively populates `knowledge_object_sources`, a reverse-lookup table (`object_id, source_id`) used when a source is deleted or reparsed to find which KG objects reference it. Without it, that lookup has to scan every object's evidence JSON in the notebook (`json.loads` over the whole table) just to find matches for one source — expensive at hundreds of thousands of objects. The table is normally populated lazily (the first source delete/reparse on an un-migrated notebook pays the scan once, populates the table while it's already reading every row, and marks the notebook so every subsequent operation is an indexed lookup instead) — this command lets you pay that cost up front, in bounded-memory batches with progress printed, instead of on a user-facing delete. It makes no model call and is idempotent/restartable (each run clears and rebuilds the notebook's rows from the current evidence, then re-marks it). Use `--notebook-id` to scope it to one library or `--all-notebooks` to cover every notebook in the database. If you ever suspect a notebook's reverse index has drifted from its actual evidence (e.g. after an abnormal interruption), re-running this command is the remediation — it always rebuilds from the current evidence.

The `metadata` subcommand backfills paper metadata (title, authors, affiliations, venue, year) for a notebook's sources that don't have it yet — useful for a library ingested before paper-metadata extraction existed, or after upgrading the extraction prompt/schema and wanting a refresh. It only targets sources that have already been parsed and look like an academic paper (empty or `academic_paper` doc type); it reads text from the already-stored parsed elements, so the original PDF doesn't need to still be on disk. It requires `--notebook-id` (this subcommand never creates a notebook) and a configured service binding for the `paper_metadata` workload; it errors out rather than silently skipping when that workload is unbound, and needs no embedding workload. It's idempotent and restartable: sources that already have a metadata row are skipped on a re-run; pass `--force` to re-extract everything in scope regardless (e.g. after a prompt/validation upgrade). Progress prints one line per source (`[meta <done>/<total>] <source-id> <status>`) followed by a final JSON summary of status counts.

The `reparse` subcommand fixes a class of historical leftover: sources that were created and whose `parse_status` looks advanced, yet have no `source_elements` (a prior parse that was interrupted or never landed). KG extraction has a zero-LLM grounding check — every node the LLM emits must bind its quoted evidence back to one of the source's elements, or it is dropped; a source with no elements has *all* of its extracted nodes discarded, so `knowledge_objects` never grows (the extraction is wasted) and re-extracting directly never recovers it. Older `all` resume logic used "has KG?" as a proxy for "has been parsed?", routing these element-less sources straight to extraction — exactly this trap (that split is now fixed, so fresh imports no longer hit it). This command re-runs `process_source` (parse → generate elements) for every source in the notebook missing `source_elements`, then does one KG rebuild; sources that already have elements are skipped (idempotent, restartable). `--limit N` processes only the first N; `--no-rebuild` skips the closing clustering (batched runs). Requires `--notebook-id`.

`kg --retry-partial` repairs a different state: the source still has graph objects, but its latest KG extraction record reports `windows_failed>0`. Normal incremental `kg` intentionally treats a completed graph-bearing source as covered, so it does not revisit these rows. The explicit repair flag adds them to the normal missing-KG target set. For each partial source, the existing objects and relations remain readable while model windows run. If any retry window fails or the replacement is empty, the batch counts that attempt as failed/incomplete and the old graph remains untouched; only a non-empty, zero-failed-window result transactionally replaces that source's graph. `--limit N` bounds the combined missing + partial target set, `--no-rebuild` supports batching, and the final rebuild/index flow is unchanged. This is not a parser repair; sources without `source_elements` still require `reparse`.

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

**Batch concurrency.** `--workers` controls source/document jobs only and falls back to `KG_JOB_CONCURRENCY`. It dispatches source jobs in `all` and file parsing in `ingest`; in `vectors-to-blob`, it instead selects the parse/re-encode process-pool size (default `min(32, cpu_count())`; `1` disables that pool).

Every model call made by `all`, `kg`, `reparse`, `metadata`, `ingest`, or `embed` goes through the same system model-service scheduler as online requests. The service bound to each workload supplies its sole model-capacity setting, `max_concurrency`; there are no batch CLI overrides for model concurrency, and increasing `--workers` never multiplies that service limit. If a throttled run leaves vectors missing, repair them later with the `embed` subcommand.

For a larger source pipeline whose model capacity is already declared in the deployment TOML:

```bash
PYTHONPATH=backend python scripts/batch_ingest.py reparse \
  --notebook-id nb-xxxx \
  --workers 32 \
  --pool-report-interval 5
```

- `--pool-report-interval` — in the `all`, `kg`, and `reparse` phases, print producer/source business-pool utilization every N seconds (default 15; `0` disables it). This is not the model-capacity authority; inspect the read-only Model Services status for per-service running/queued counts, health, and breaker state.

Options: `--owner` (notebook owner username, case-insensitive; defaults to the admin user), `--workers` (source-pipeline concurrency = `KG_JOB_CONCURRENCY`; in `vectors-to-blob`, parse/encode process-pool size, default `min(32, cpu_count())`, `1` = no pool), `--limit` (kg extraction subset — clustering still covers the whole notebook), `--retry-partial` (`kg` only: safely retry graph-bearing sources whose latest run has failed windows), `--no-rebuild` / `--rebuild-only` (split extraction from the final clustering for batched large builds), `--fresh` (clears the rebuild checkpoint to force a full re-run of merge-review + concept-description adjudication; use when you changed the KG model/thresholds but the data is unchanged — implies a forced rebuild, and also applies to the `all` phase's final clustering), `--allow-no-embed` (explicitly allow running when `chunk_embedding` is unbound; refused by default, never silent; ignored by the `embed` subcommand), `--pool-report-interval` (seconds between producer/source business-pool reports in `all`/`kg`/`reparse`; default 15, `0` off), `--all-notebooks` (`vectors-to-blob` / `backfill-source-index` only: act on every notebook instead of one), `--force` (`metadata` only: re-extract sources that already have a metadata row), `--dry-run` (scan & estimate only). The `embed` subcommand backfills only missing chunk + element + node vectors and requires `--notebook-id`. The `vectors-to-blob` subcommand migrates legacy JSON-text vectors to BLOB and requires `--notebook-id` or `--all-notebooks`. The `backfill-source-index` subcommand proactively builds the source-deletion reverse index and requires `--notebook-id` or `--all-notebooks`. The `metadata` subcommand backfills paper metadata (title/authors/venue/year) for already-parsed academic-paper sources and requires `--notebook-id` plus a `paper_metadata` binding.

Prereqs: point `MODEL_SERVICES_CONFIG` at the deployment TOML, bind the workloads required by the selected phase (notably `chunk_embedding`, `source_element_embedding`, `knowledge_object_embedding`, `kg_extract`, and `paper_metadata`), and place only the referenced secrets in `.env`. If `chunk_embedding` is unbound, the CLI **refuses to run by default** — pass `--allow-no-embed` to import without vectors, never silently; phases whose required chat workload is unbound fail clearly. A re-run resumes from **database state**, not a progress file: `ingest` checks content hashes, `kg` checks the latest extraction run, and `embed` checks vector rows. Because a hash is stored before parsing completes, repair interrupted sources without elements using `reparse`. `<storage>/batch_ingest/<notebook>.jsonl` is a write-only run log.

### Large-library retrieval hot path

Indexed KG retrieval must remain bounded after ANN candidate generation. The isolated-node rank penalty probes each candidate with indexed `EXISTS` checks and returns only connected candidate ids; it never fetches a hub's complete adjacency list. Canonical folding reads mappings only for the scored ids through `cluster_fold_rows`. Concurrent reasoning subqueries share one lazy ANN load per scale-index instance and artifact kind. These optimizations preserve the retrieved ids, scores, thresholds, PPR behavior, and recall.

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
