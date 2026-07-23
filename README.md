# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a source-grounded knowhow notebook for semiconductor engineering teams. It turns PDF, Markdown, DOCX, PPTX, CSV, and XLSX material into searchable source elements, structured knowledge, cited answers, private Memory, knowhow tables, and deep reports.

The current target is a local real-team beta: FastAPI + SQLite on the backend and Next.js on the frontend. It requires no Docker, GPU, database server, or local model server. OpenAI-compatible chat, embedding, rerank, and MinerU services are optional URL-based integrations; deterministic fallbacks keep the core pipeline usable when they are not configured.

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
  → SQLite + local source/index/log storage

Optional external services
  → OpenAI-compatible chat / embedding / rerank
  → MinerU HTTP, isolated CLI, or cloud fallback
```

- SQLite defaults to `.local/silicon_notebook.db`; uploaded files and generated artifacts stay under `.local/`.
- The production backend is deliberately single-worker because model queues, breakers, health, and cancellation state are process-local.
- Baseline `chunk` retrieval is active-notebook-only. KG-assisted and reasoning paths may federate through explicitly mounted base notebooks.
- The candidate review queue has been retired; current knowledge governance operates on stored knowledge objects.
- PostgreSQL/pgvector remain a future direction. Non-SQLite `DATABASE_URL` values currently fail fast.

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

- Local persistence is SQLite; PostgreSQL/pgvector are not yet the production store.
- No Docker is required or provided as the default first-version workflow.
- High-fidelity formulas, tables, layout, and scanned PDFs require MinerU; `MINERU_MODE=off` uses pypdf text fallback.
- Knowledge extraction and model-backed answers require the relevant workload bindings; offline mode does not synthesize knowledge.
- Graph Ask remains opt-in/experimental; `chunk` is the default.
- Memory is manual opt-in and creator-private.
- Sharing is copy or read-only membership, not live collaborative editing.
- Web/network source search remains a disabled future affordance.

## Documentation maintenance

Keep this README as the concise project entry point. Detailed behavior belongs in the owning document listed above, and English/Chinese counterparts must remain aligned. Changes to setup, product behavior, architecture, or development constraints still update `README.md`, `README_zh.md`, `AGENTS.md`, and `CLAUDE.md` together, plus the relevant canonical detail document.
