# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a source-grounded knowledge notebook for semiconductor engineering teams. It turns PDF, Markdown, DOCX, PPTX, CSV, XLSX, and XLS material into searchable source elements, cited answers, structured knowledge, private Memory, knowhow tables, and deep reports.

The project is a local-team beta built with FastAPI and Next.js. SQLite works out of the box without Docker, a GPU, or external model services; PostgreSQL and OpenAI-compatible model or MinerU services are optional.

## Highlights

- Structured ingestion with element-level citations for text, tables, formulas, code, and retained document images.
- Multi-turn Ask with source selection, clickable evidence, conversation history, and `chunk`, `reasoning`, or experimental `graph` retrieval.
- Knowledge extraction and governance for concepts, claims, formulas, procedures, relations, and a unified graph.
- Private Memory, structured knowhow, deep reports, reference libraries, and controlled notebook/report sharing.
- External Agent access through authenticated MCP tools, including scoped Ask, source, Memory, and knowhow workflows.
- A startup-frozen Extension SDK for deployment-owned backend, UI, Ask-engine, parser, indexing, exporter, and observer contributions.

Backend deployment plugins are trusted same-process code loaded only from the TOML named by `EXTENSIONS_CONFIG`; changes require a restart, and their API extensions mount only below `/api/extensions/{plugin_id}`. Private UI packages are injected separately at frontend build time through `SILICON_NOTEBOOK_UI_PLUGINS` and require a rebuild.

The interface starts in auto mode for upload-and-ask use. Advanced mode exposes retrieval effort, report depth, and source/reference-library scope controls.

## Quick start

### Requirements

- Python 3.13+
- Node.js 20+ and npm
- git

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

### Configure and run

```bash
cp .env.example .env
mkdir -p .local
```

Before starting, choose one model setup:

- For deterministic/offline fallbacks, set `MODEL_SERVICES_CONFIG=` in `.env`.
- For model-backed features, copy `model-services.example.toml` to `.local/model-services.toml`, configure its services, and fill the referenced secrets in `.env`.

Then run:

```bash
npm run dev
```

Open <http://127.0.0.1:3000>. The API listens on <http://127.0.0.1:8000>.

A fresh local database creates `admin` / `admin`. Change it before exposing the service. Non-loopback binding requires a non-default `SILICON_NOTEBOOK_ADMIN_PASSWORD`.

SQLite is the default; set `DATABASE_URL` to a prepared PostgreSQL 16 database when needed. See [Deployment and configuration](./docs/deployment-and-configuration.md) for the complete settings and security guidance.

### Production

```bash
npm run start
npm run stop
```

Production uses one backend worker because model scheduling and cancellation state are process-local. Logs are written under `.local/logs/`; verify `/api/ready` after startup. For offline deployment bundles, run `bash scripts/pack.sh` and follow [packaging/DEPLOY.md](./packaging/DEPLOY.md).

### Verify

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

Extended and PostgreSQL-specific gates are documented in [Development and repository contracts](./docs/development.md).
CI lane timings are observational only; the gates themselves remain pass/fail contracts.

## Typical workflow

1. Create a notebook and import supported source files.
2. Select the active source and reference-library scope, then ask cited questions.
3. Build and review structured knowledge or inspect the graph.
4. Save useful results to private Memory or knowhow tables, or generate a deep report.
5. Share a notebook with controlled access or publish a revocable read-only report.

## Architecture at a glance

```text
Browser
  → Next.js frontend
  → FastAPI /api and Streamable HTTP /mcp
  → application services and repository ports
  → SQLite or PostgreSQL + local source/index/log storage

Optional services
  → OpenAI-compatible chat / embedding / rerank
  → MinerU HTTP, isolated CLI, or cloud parsing
```

Uploaded files and generated artifacts stay under `.local/` for either database backend. Ask and Deep Report use explicit, frozen source scopes; unavailable optional retrieval lanes fail back to the admitted baseline rather than hiding other results.

The Extension SDK's baseline-preserving retrieval host applies live capability decisions; modular-architecture changes require two independent subagent reviews and green CI. Ask and Report cross immutable application stage boundaries with a fresh retrieval run and no held database connection; `report.completed_observer` runs only after the durable `done` commit.

## Documentation

| Need | Document |
| --- | --- |
| Product behavior, retrieval, Memory/MCP, knowhow, APIs, limits | [Product and API reference](./docs/product-and-api.md) |
| User-visible Chinese terminology | [UI vocabulary contract](./docs/ui-vocabulary.md) |
| Installation, production deployment, model services, settings | [Deployment and configuration](./docs/deployment-and-configuration.md) |
| Logs, ingestion, indexing, migrations, backfills, incident handling | [Operations](./docs/operations.md) |
| Testing, CI, contribution and repository contracts | [Development](./docs/development.md) |
| External Agent setup and runnable MCP/Memory example | [Agent MCP and Memory onboarding](./docs/agent-mcp-memory-sop.md) |
| Deployment plugin development and operation | [Deployment extensions](./docs/deployment-extensions-sop.md) |
| Runtime boundaries | [architecture.md](./architecture.md) |
| Script command index | [scripts/README.md](./scripts/README.md) |
| Implemented product-spec status | [fangan_done.md](./fangan_done.md) |

Chinese counterparts are linked from the top of each split document.

## Current boundaries

- SQLite is the shipped default; PostgreSQL 16 is a supported alternative. Switching databases does not copy or synchronize existing data.
- Highest-fidelity scanned-PDF, formula, and image extraction requires MinerU; local parsers provide deterministic fallbacks.
- Model-backed answers and knowledge extraction require matching workload bindings; offline mode remains useful for ingestion and deterministic workflows.
- Graph Ask is opt-in and experimental; the default Ask mode is `chunk`. Generated-question recall is deployment opt-in and defaults to `off`.
- Memory is creator-private. Sharing supports copy, read-only membership, and groups, but not live collaborative editing.
- Web/network source search remains a disabled future affordance.

## Documentation maintenance

Keep this README as the concise project entry point. Detailed behavior belongs in the owning document listed above, and English/Chinese counterparts must remain aligned. Update this README pair only when entry-point material changes; update `AGENTS.md` only for repository-wide agent workflow/routing changes and `CLAUDE.md` only for Claude Code-specific resident rules. Ordinary product, architecture, deployment, operations, and development changes update their owning canonical documents instead of being copied into every entry file.
