# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a source-grounded knowhow notebook for semiconductor engineering teams. It turns PDF, Markdown, DOCX, PPTX, CSV, and XLSX material into searchable source elements, structured knowledge, cited answers, private Memory, knowhow tables, and deep reports.

The current target is a local real-team beta: FastAPI with a selectable SQLite or PostgreSQL repository backend, plus Next.js on the frontend. The shipped-default SQLite quick start requires no Docker, GPU, database server, or local model server; selecting PostgreSQL requires an accessible PostgreSQL server. OpenAI-compatible chat, embedding, rerank, and MinerU services are optional URL-based integrations; deterministic fallbacks keep the core pipeline usable when they are not configured.

## Highlights

- Structured source ingestion with element-level evidence, formulas, tables, and retained document images when MinerU is configured.
- Grounded multi-turn Ask with compact citations and `chunk`, `reasoning`, and experimental `graph` retrieval modes.
- Concept / Claim / Formula / Procedure knowledge extraction, governance, unified graph visualization, and personal-to-base promotion.
- Notebook-bound, creator-private Memory with explicit preview/confirmation and scoped external-Agent access over MCP.
- Free-form knowhow tables with Markdown cells, reasoning-backed library-wide empty-cell completion suggestions, deterministic graph projection, history, milestones, and isolated code attachments.
- Two-stage deep reports with editable outlines, per-section reasoning, live progress, cancellation, and Markdown/ZIP export.
- Multi-account ownership, public reference libraries, share links, copy/read-only membership, and admin controls.
- Structured JSONL logs, bounded production diagnostics, offline batch ingestion, replay, migration, and backfill tools.

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

For a self-contained target with no npm/node or root access, build an offline bundle with `bash scripts/pack.sh` and follow [packaging/DEPLOY.md](./packaging/DEPLOY.md).

### Verify

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

`scripts/check.sh` is the complete offline local gate: backend tests, smoke/contract checks, frontend tests and type checking, and the production frontend build.

## Product flow

1. Create a notebook. The app immediately opens an `Untitled notebook`; it does not ask for metadata first.
2. Import source files. Parsing creates structured source elements and searchable chunks.
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
- The candidate review queue has been retired; current knowledge governance operates on stored knowledge objects.
- DATABASE_URL selects the formal repository backend through one repository factory. Exactly one active repository backend is selected centrally from `DATABASE_URL`. SQLite and PostgreSQL are both available direct backends; SQLite remains the shipped default.

### SQLite / PostgreSQL switching

Shadow SQLite source-open classification is intentionally narrow: only a non-transient `sqlite3.OperationalError` raised by `open_fresh_live_sqlite` is a source-binding identity failure. Locked, busy, and interrupted opens remain transient whole-batch retries; later SQLite operational errors retain their schema/query classifications.

The application never dual-writes. `SHADOW_DATABASE_URL` is reserved for future migration tooling and cannot select or synchronize the active backend. Changing `DATABASE_URL` does not copy, migrate, or synchronize existing data.

SQLite schema v31 contains only the inert, payload-free internal tables needed by run-scoped forward-shadow capture. Capture/freeze triggers and the four logical-key guards are absent by default and are installed only by explicit migration control primitives. Once installed, the guards immediately enforce uniqueness; capture/freeze behavior remains disabled until the run control state enables it. Setting `SHADOW_DATABASE_URL` alone still enables nothing. PostgreSQL schema v9 is the paired business schema. The temporary shadow module now has preflight/control/guard primitives, atomic SQLite snapshots, bounded resumable 60-table baseline COPY/H0, and a fail-stop single-consumer forward apply engine. It consumes the global SQLite sequence contiguously, hydrates current rows only for upserts under a short read snapshot, keeps deletes key-only with zero hydrated bytes, preserves historical ordinals, and commits target convergence with the checkpoint. Repeated stable keys coalesce to their last accepted event in global last-seq order while raw sequence validation/checkpointing stays contiguous; For each identity, the final actual apply overrides any synthetic dependency contribution; only dependency-only identities contribute one reference-counted synthetic row and its bytes. A short read window ending below the allocated high-water is an immediate suffix gap before hydration/apply; a full window below high-water probes the adjacent sequence in the same snapshot and fails on its absence. Each batch is hard-capped at 4,096 events/64 MiB; only one final bundle may exceed the byte cap, and a same-key replacement that grows past the cap is rolled back and deferred when another actual bundle is already accepted. Current-row FK parents come only from the same verified source snapshot through a 64-row-per-event closure (the fixed v9 graph needs at most nine row slots), count toward bytes, and are deduplicated across the batch—future change-log metadata is never scanned to authorize them. Target savepoints defer only FK/UNIQUE ordering SQLSTATEs; CHECK/NOT NULL failures poison immediately. Exact PG9 catalog-derived parking plans cover all 82 unique surfaces: nullable columns park at NULL; non-FK/non-CHECK text and bigint columns use deterministic candidates scoped by indexable equality for non-NULL values and `IS NULL` for NULL values on the other unique columns plus the fixed predicate (`C`-collated text max plus `chr(1)`, or an indexable bigint MIN/MAX fast path choosing min−1/max+1 and scanning the first gap only when both int64 bounds are occupied); same-transaction delete/reinsert is limited to statically proven leaf tables with an accepted current-final row. Each stagnant pass parks every independently parkable conflict. Deferred work allows at most eight passes, 32 actual statements per apply, and 16,384 actual statements overall; every candidate query counts toward that budget, and `ProgramLimitExceeded`/`DataError` candidate failures and candidate-search, candidate-update, or ordering capacity exhaustion remain non-poison; `QueryCanceled` remains transient and retries the whole transaction. `run_forever` doubles its 256-event/8-MiB window through the hard caps after ordering-blocked; hard-cap exhaustion remains non-poison. After claiming the worker, the apply transaction rechecks existing run/direction poison before any business DML. Poison publication similarly locks and checks every existing run/direction poison after binding/checkpoint validation: an exact replay is ACK-loss success, while a differing record is stale and never creates a second poison. Apply, ambiguous-commit recognition, and poison publication all bind both source and live target identities; snapshot and pre-apply gates also require `progress.applied_seq` to equal the checkpoint. Every valid batch outcome emits exactly one redacted metric using the actual accepted raw-event count and observable retries. Target apply locks the migration ledger and every business table and revalidates the exact catalog; SQLite path/file binding failures use a dedicated identity error instead of message-based conversion classification; proven deterministic failures create one blocking redacted poison record and transient database failures use bounded whole-transaction retries. There is still no operator CLI or end-to-end migration worker, so `SHADOW_DATABASE_URL` remains inert and these primitives are not a usable migration command.

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
- Switch with a stop/change/start boundary: quiesce writes, stop the backend, take and verify a consistent backup, change the single `DATABASE_URL`, start with `--workers 1`, then verify status, `/api/ready`, login, counts, and representative reads before sending traffic.
- Returning to SQLite cannot replay PostgreSQL-only writes. Lossless rollback therefore requires no post-cutover writes or an externally reconciled and verified migration in both directions.
- `scripts/batch_ingest.py` mutation phases are SQLite-only; on PostgreSQL use the normal application/API ingestion and KG/index flows.

The complete decision table, backup rules, and rollback procedure are in [Deployment and configuration](./docs/deployment-and-configuration.md) and [Operations](./docs/operations.md).

See [architecture.md](./architecture.md) for runtime boundaries and [Development and repository contracts](./docs/development.md) for contributor-facing constraints.

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

- SQLite is the shipped default; PostgreSQL 16 is a supported direct backend. Existing data still requires an externally controlled, verified migration between them.
- No Docker is required or provided as the default first-version workflow.
- High-fidelity formulas, tables, layout, and scanned PDFs require MinerU; `MINERU_MODE=off` uses pypdf text fallback.
- Knowledge extraction and model-backed answers require the relevant workload bindings; offline mode does not synthesize knowledge.
- Graph Ask remains opt-in/experimental; `chunk` is the default.
- Memory is manual opt-in and creator-private.
- Sharing is copy or read-only membership, not live collaborative editing.
- Web/network source search remains a disabled future affordance.

## Documentation maintenance

Keep this README as the concise project entry point. Detailed behavior belongs in the owning document listed above, and English/Chinese counterparts must remain aligned. Changes to setup, product behavior, architecture, or development constraints still update `README.md`, `README_zh.md`, `AGENTS.md`, and `CLAUDE.md` together, plus the relevant canonical detail document.
