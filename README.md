# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a source-grounded knowhow notebook for semiconductor engineering teams. It turns PDF, Markdown, DOCX, PPTX, CSV, and XLSX material into searchable source elements, structured knowledge, cited answers, private Memory, knowhow tables, and deep reports.

The current target is a local real-team beta: FastAPI with a selectable SQLite or PostgreSQL repository backend, plus Next.js on the frontend. The shipped-default SQLite quick start requires no Docker, GPU, database server, or local model server; selecting PostgreSQL requires an accessible PostgreSQL server. OpenAI-compatible chat, embedding, rerank, and MinerU services are optional URL-based integrations; deterministic fallbacks keep the core pipeline usable when they are not configured.

## Highlights

- Structured source ingestion with element-level evidence, formulas, tables, and retained document images when MinerU is configured; formula evidence is typeset in source details and Knowledge Graph source cards, LaTeX stays visible on render failure, and wide display equations scroll inside their owning panel.
- Grounded multi-turn Ask with compact citations, last-activity conversation history (including in-flight first turns that remain reopenable across immediate session switches), and browser-local question/answer timestamps revealed on hover or pinned by click. Question time is the browser submission instant; answer time is the authoritative persisted completion instant. Ask supports `chunk`, `reasoning`, and experimental `graph` retrieval modes. Reasoning questions are understood without corpus influence before retrieval; clear intent auto-continues, while direction-changing ambiguity pauses for confirmation and then governs every retrieval stage. Confirmed mandatory-topic seeds beyond the first-round width are executed later within the step budget instead of being discarded, and any still-uncovered directions are disclosed in the trace. Questions that map one named tool's capability onto another get one mandatory topic per named tool, with target-side retrieval pairing the target tool's name with functional wording. The live reasoning trace covers the whole run — from question understanding through answer generation — not just the retrieval stages.
- User-selectable Reasoning Ask effort (`overview` / `standard` / `deep` / `thorough` / `exhaustive`) controls bounded ranked-retrieval work. Direct whole-table Knowhow lists and physical row/record counts use cursor enumeration with coverage such as `100/100`; conditional, distinct/type counts (for example “how many kinds”), and grouped requests disclose that exact completeness is unsupported, while safety ceilings and bounded hybrid synthesis are reported as partial rather than “all”. The exact limits are listed in the [Product and API reference](./docs/product-and-api.md#reasoning-effort-and-complete-collection-requests).
- Reasoning Ask can also list, not just rank, whole-library formula / table / image / code-block collections and concept / claim / formula / procedure knowledge-object collections on request, each carrying a returned/known-total completeness badge; a truncated list is clearly marked partial and can keep being listed within the same reasoning run. Details in the [Product and API reference](./docs/product-and-api.md#collection-enumeration-tools).
- Concept / Claim / Formula / Procedure extraction governed by one typed edge contract, historical-edge filtering, a read-only contract audit, unified graph visualization, citation-to-node deep links (including nodes outside the bounded core view), and personal-to-base promotion. Optional bounded cross-element relation completion uses mode-specific persistent source-generation keyset watermarks plus indexed same-source candidates; unfinished pages resume through bounded jobs and after restart, and a mode change atomically publishes the new recoverable cursor before retiring the old one. The feature remains rollout-gated and disabled by default.
- Notebook-bound, creator-private Memory with explicit preview/confirmation and scoped external-Agent access over MCP.
- Free-form knowhow tables with Markdown cells, column- or row-oriented spreadsheet import with actionable validation, bounded batch-reformat review, readable audit actors, content-aware stable columns, reasoning-backed library-wide empty-cell completion suggestions, deterministic projection whose cell knowledge objects enter graph/reasoning retrieval by default, history, milestones, and isolated code attachments with immediate save attribution.
- Intent-first two-stage deep reports with corpus-blind question clarification, an atomically frozen confirmation contract before retrieval, bounded exact-element recovery for large libraries, editable coverage-aware outlines, paper-title-aware citations, verified grounding, per-section reasoning, live progress, cancellation, and Markdown/ZIP export.
- Multi-account ownership, public reference libraries, share links, copy/read-only membership, and admin controls, including a paginated, sortable user-usage table.
- Structured JSONL logs, bounded production diagnostics, offline batch ingestion, replay, migration, and backfill tools.
- Retrieval candidates retain all producer provenance (semantic, lexical, PPR, KG source, or community); mixed chunk/graph selection can reserve a bounded graph-only seat without increasing the answer budget.

The complete behavior and endpoint contracts live in [Product and API reference](./docs/product-and-api.md).

## Quick start

### Requirements

- Python 3.13 or newer
- Node.js 20 or newer and npm
- git

A C/C++ toolchain is needed only when pip cannot use prebuilt wheels for packages such as `numpy`, `rustworkx`, or `hnswlib`.

### Install

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt

( cd frontend && npm install )
```

### Configure

```bash
cp .env.example .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
```

For model-backed answers and extraction, edit `.local/model-services.toml`, bind workloads to physical services, set each service's `max_concurrency`, and place only the secrets named by `api_key_env` in `.env`.

To run explicitly with deterministic/offline fallbacks, leave this empty in `.env`:

```text
MODEL_SERVICES_CONFIG=
```

`.env.example` is the authoritative list of non-service settings and secret slots. `model-services.example.toml` is the service, binding, and capacity template. See [Deployment and configuration](./docs/deployment-and-configuration.md) for remote access, CORS, model scheduling, authentication, MinerU settings, and upgrade guidance.

### Run

```bash
npm run dev
```

Open <http://127.0.0.1:3000>. A fresh database creates the built-in `admin` account; the local default password is `admin`. Binding to a non-loopback address requires a non-default `SILICON_NOTEBOOK_ADMIN_PASSWORD`.

Startup migrates the selected datastore. The default is `DATABASE_URL=sqlite:///.local/silicon_notebook.db`; a provisioned PostgreSQL 16 database may instead use `DATABASE_URL=postgresql://user:password@host:5432/database`.

Production uses one backend worker so the in-process model scheduler remains the deployment-wide capacity boundary:

```bash
npm run start
npm run stop
```

`npm run start` installs backend and locked frontend dependencies, builds in the foreground, launches both services as terminal-independent background processes, waits for backend readiness and frontend HTTP access, then exits. Logs remain under `.local/logs/`; use `npm run stop` to stop the services. Prebuilt deployments may set `SKIP_INSTALL=1`.

For a self-contained target with no npm/node or root access, build an offline bundle with `bash scripts/pack.sh` and follow [packaging/DEPLOY.md](./packaging/DEPLOY.md).

### Verify

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

Verification is tiered: G0 runs change-focused tests; G1 `scripts/check.sh` is the edit-time and PR/push offline gate (stable backend, contracts, frontend tests/typecheck/build) with default 12 backend workers and an Apple Silicon warm target of at most 60 seconds; G2 `scripts/check_extended.sh` adds real-index/performance tests and repository-wide semantic scans and runs once daily at 18:17 UTC (02:17 Asia/Shanghai) or manually; G3 `scripts/check_postgres.sh` remains the independent PostgreSQL integration gate. CI lane timings are observational only.

Codex-only execution note: run `scripts/check.sh` outside the Codex sandbox on the first attempt because lifecycle tests bind loopback ports and manage subprocesses. GitHub network operations (`git fetch`, `git push`, and `gh auth/repo/pr`) must likewise request outside-sandbox execution directly instead of first failing inside the sandbox; ordinary local read-only Git inspection stays sandboxed.

Database-specific coverage now targets the direct PostgreSQL backend. Retired tests for the SQLite backend implementation, SQLite-to-PostgreSQL import/forward-shadow, and cross-backend parity are no longer part of the active suite.

## Product flow

1. Create a notebook. The app immediately opens an `Untitled notebook`; it does not ask for metadata first.
2. Import source files. The dialog shortens long staged filenames, keeps action-area breathing room, and disables over-quota batches with an actionable reason before upload; parsing then creates structured source elements and searchable chunks.
3. Ask questions immediately through chunk-native retrieval. Build a KG on demand, or enable automatic extraction for every upload.
4. Browse and govern extracted knowledge, inspect the full-screen graph, and mount public reference libraries when federation is needed.
5. Save useful answers into private notebook-bound Memory, maintain structured knowhow tables, or generate a deep report.
6. Share a notebook by link: small notebooks are copied; large notebooks are joined read-only. Live collaborative editing is not part of the beta.

Inside a notebook, the workspace stays two-column: imported sources on the left and **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report) in the main area.

Detailed product behavior, retrieval semantics, MCP tools, and endpoint paths are documented in [Product and API reference](./docs/product-and-api.md).

## Architecture at a glance

```text
Browser
  → Next.js frontend
  → FastAPI /api and Streamable HTTP /mcp
  → application services and repository ports
  → SQLite or PostgreSQL + local source/index/log storage

Optional external services
  → OpenAI-compatible chat / embedding / rerank
  → MinerU HTTP, isolated CLI, or cloud fallback
```

- SQLite defaults to `.local/silicon_notebook.db`; PostgreSQL is a direct alternative. Uploaded files and generated artifacts stay under `.local/` for either database.
- The production backend is deliberately single-worker because model queues, breakers, health, and cancellation state are process-local.
- Baseline `chunk` retrieval is active-notebook-only. KG-assisted and reasoning paths may federate through explicitly mounted base notebooks.
- Lexical retrieval keeps the exact query as a ranking bonus but recalls independent Latin/number terms, overlapping CJK trigrams, and separator-joined identifiers (e.g. `set_db`, `config.yaml`) as whole terms instead of forcing the whole query to be contiguous; SQLite safely quotes FTS5 clauses and PostgreSQL applies the same bounded term union with LIKE metacharacters escaped so an identifier like `set_db` stays literal. Indexed Chunk and KG retrieval use bounded `ANN ∪ FTS` candidates; indexed Relation retrieval adds bounded, direction-balanced relations adjacent to FTS-matched KG endpoints.
- Indexed large-library retrieval keeps post-ANN database hydration bounded by the candidate window and single-flights ANN handle loading across concurrent reasoning subqueries. By default, every published scale index, enabled ANN handle, and safely reusable single-index PPR core is loaded behind `/api/ready` before user traffic is admitted; cross-notebook combined graphs remain lazy to avoid multiplying 10M-node graph copies.
- Scale-index actions distinguish immediate builds from off-peak scheduling. A manual immediate build supersedes an older queued build for the same notebook without dropping later follow-up work; completion events refresh the live notebook status even after foreground polling ends, so historical Ask warnings disappear when the index is published.
- The candidate review queue has been retired; current knowledge governance operates on stored knowledge objects.
- DATABASE_URL selects the formal repository backend through one repository factory. Exactly one active repository backend is selected centrally from `DATABASE_URL`. SQLite and PostgreSQL are both available direct backends; SQLite remains the shipped default.

### SQLite / PostgreSQL switching

Shadow SQLite source-open classification is intentionally narrow: only a non-transient `sqlite3.OperationalError` raised by `open_fresh_live_sqlite` is a source-binding identity failure. Locked, busy, and interrupted opens remain transient whole-batch retries; later SQLite operational errors retain their schema/query classifications.

The application never dual-writes through its normal repository path. `SHADOW_DATABASE_URL`
names only the PostgreSQL target used by the explicit forward-shadow migration CLI; setting it
alone starts nothing and never changes the active backend. Changing `DATABASE_URL` does not copy, migrate, or synchronize existing data.

While `DATABASE_URL` remains SQLite, the operator can run a guarded, one-way
SQLite→PostgreSQL shadow: preflight binds and confirms both database identities, `start-forward`
installs run-scoped capture/guards and copies a consistent 64-table baseline, and one supervised
foreground worker continuously applies the retained SQLite change log. `status` exposes redacted
lag/lease/poison state and `verify --level full` performs a barrier-aware consistency check. The
worker uses an exclusive database-clock lease, retries transient PostgreSQL failures, stops on a
deterministic poison event, and conservatively retains at least seven days and 100,000 events
behind verified progress.

This phase does **not** implement cutover, reverse replication, or automatic `DATABASE_URL`
changes. Keep SQLite active, keep both backups current, and treat PostgreSQL as a read-disabled
shadow until the separately reviewed cutover phase. The complete command sequence and failure
rules are in [Operations](./docs/operations.md).

The separate, dry-run-first `scripts/migrate_sqlite_to_postgres.py` remains the owned
stopped-snapshot importer and local-activation tool. It is not continuous replication; use
`scripts/shadow_sqlite_to_postgres.py` only for the SQLite-active forward shadow described
above, and never run the two workflows against the same target.

Baseline snapshot/COPY additionally requires an owner-only real snapshot directory, qualifies every business statement to the run-bound schema, revalidates enabled live SQLite capture under a short write fence at critical bindings, uses bounded named server cursors and statement timeouts, and validates the complete migration-derived v9 table/column/constraint/operational+GIN-index/extension catalog at start and finalization. Snapshot/fence reads use a dedicated fresh connection to the current SQLite path—not the repository's thread-cached connection—and bind the resolved path plus device/inode before/after open and again before publication/PG commit. The final SQLite fence is acquired only after long PG proof/ANALYZE work and remains held until the PG H0 transaction commits.

- On the shipped SQLite default, search uses SQLite FTS/vector storage. The PostgreSQL backend uses `pg_trgm`/`ILIKE`; float32 vectors remain `bytea`, so pgvector is not installed or required.
- `pg_trgm` must be installed in the `public` schema. Check it without exposing credentials:

  ```sql
  SELECT e.extname, n.nspname
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'pg_trgm';
  ```

  `pg_trgm | public` means the prerequisite is ready. If the query returns no row, the first migration automatically attempts `CREATE EXTENSION pg_trgm`; an existing `pg_trgm` in any other schema fails closed.
- The importer requires an empty UTF-8 PostgreSQL target, reads its URL from `POSTGRES_MIGRATION_URL` (never a URL CLI argument), takes an online SQLite backup-API snapshot including committed WAL state, upgrades only a working copy to the paired schema, streams bounded `COPY`, preserves ordinal values, converts legacy JSON vectors to float32 `bytea`, verifies every table with content checksums, and commits each verified table with a per-table checkpoint so a stopped import (crash, dropped remote connection, reboot) resumes from the last completed table instead of restarting; finalize (ordinal reseed, index rebuild, `ANALYZE`) is idempotent. It explicitly excludes the SQLite-only shadow control/change-log tables and records that exclusion in its receipt. It accepts bounded session bulk-load tuning (`--maintenance-work-mem`, `--max-parallel-index-workers`) for large targets. Its default preview/apply modes do not change `DATABASE_URL` and it never copies `.local/storage`.
- An online import is a rehearsal snapshot only: SQLite writes committed after its snapshot are not synchronized. For a stopped local deployment, the explicit `--activate-env ... --confirm-service-stopped` mode re-snapshots SQLite and rechecks every PostgreSQL table against the credential-free receipt before atomically replacing `.env`; it preserves the former SQLite URL as inert `SHADOW_DATABASE_URL` and writes a restricted rollback copy. The CLI does not stop or restart services. Start with `--workers 1`, then verify `/api/ready`, login, counts, search, representative reads, and one canary write before sending traffic.
- Returning to SQLite cannot replay PostgreSQL-only writes. Lossless rollback therefore requires no post-cutover writes or an externally reconciled and verified migration in both directions.
- `scripts/batch_ingest.py` supports SQLite and PostgreSQL for `ingest`, `kg`, `index`, `all`, `embed`, `metadata`, `reparse`, and `backfill-source-index`. Direct PostgreSQL maintenance is offline-only: stop the API/background writers and pass `--confirm-service-stopped`; the flag is an operator assertion, not a service-stop mechanism. A database-wide advisory lock prevents overlapping PostgreSQL maintenance CLIs. `vectors-to-blob` remains SQLite-only because PostgreSQL vectors are already `bytea`. Historical partial KG runs can use `kg --retry-partial`, which retains the old graph until a non-empty, zero-failed-window replacement commits.

Exact preview/apply/retry commands, the SQLite↔PostgreSQL selector values, the final cutover checklist, storage handling, and rollback limits are in [Operations](./docs/operations.md#sqlite--postgresql-cutover-and-rollback); the step-by-step execution checklist is the [migration runbook](./docs/postgres-migration-runbook.md) (Chinese, like the other runbooks in `docs/`; this English section remains the complete reference). Deployment settings are in [Deployment and configuration](./docs/deployment-and-configuration.md).

See [architecture.md](./architecture.md) for runtime boundaries and [Development and repository contracts](./docs/development.md) for contributor-facing constraints.

Contributor safety: any task that will write repository code, tests, documentation, or configuration starts in an isolated linked git worktree and branch; the main checkout remains read-only for that task. Read-only research, status, and review are exempt.

## Documentation

| Need | Document |
| --- | --- |
| Product behavior, retrieval modes, Memory/MCP, knowhow, APIs, current limitations | [Product and API reference](./docs/product-and-api.md) |
| Installation, source/production deployment, model services, settings | [Deployment and configuration](./docs/deployment-and-configuration.md) |
| Logs, incident capture, MinerU, batch ingestion, replay, migrations, backfills | [Operations, diagnostics, and ingestion tools](./docs/operations.md) |
| Verification, CI, development workflow, test and documentation contracts | [Development and repository contracts](./docs/development.md) |
| Detailed runtime architecture | [architecture.md](./architecture.md) |
| Script-oriented command index | [scripts/README.md](./scripts/README.md) |
| Offline bundle target instructions | [packaging/DEPLOY.md](./packaging/DEPLOY.md) |
| KG schema | [schema/README.md](./schema/README.md) |
| Implemented product-spec status | [fangan_done.md](./fangan_done.md) |

Chinese counterparts are linked from the top of each split document.

## Current boundaries

- SQLite is the shipped default; PostgreSQL 16 is a supported direct backend. The repository includes a verified one-way SQLite→PostgreSQL snapshot importer; it does not provide live synchronization, PostgreSQL→SQLite replay, or MySQL migration.
- No Docker is required or provided as the default first-version workflow.
- High-fidelity formulas, tables, layout, and scanned PDFs require MinerU; `MINERU_MODE=off` uses pypdf text fallback.
- Knowledge extraction and model-backed answers require the relevant workload bindings; offline mode does not synthesize knowledge.
- Graph Ask remains opt-in/experimental; `chunk` is the default.
- Memory is manual opt-in and creator-private.
- Sharing is copy or read-only membership, not live collaborative editing.
- Web/network source search remains a disabled future affordance.

## Documentation maintenance

Keep this README as the concise project entry point. Detailed behavior belongs in the owning document listed above, and English/Chinese counterparts must remain aligned. Changes to setup, product behavior, architecture, or development constraints still update `README.md`, `README_zh.md`, `AGENTS.md`, and `CLAUDE.md` together, plus the relevant canonical detail document.
