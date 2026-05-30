# Agent Development Instructions

This file is the working contract for future agents and developers in this repository. Keep it updated whenever setup, product flow, architecture, or constraints change.

## Documentation Sync

When making changes that affect setup, product behavior, architecture, or development constraints, update all three files together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`

Do not update only one language README when the same information should be available in both.

## Tracking Completed Spec Features

`silicon_notebook_fangan.md` is the product spec; `fangan_done.md` records what has actually been implemented against it.

**Whenever you complete a feature defined in `silicon_notebook_fangan.md`, you MUST append/update the corresponding entry in `fangan_done.md`** in the same change. Specifically:

- Add the now-working capability under the relevant section (cite the spec section, e.g. §6.7, §12, when applicable).
- Move any item you just finished out of `fangan_done.md`'s "当前边界 / 未完成" list.
- Keep it factual and in sync with the code — describe what is real and verified, note where deterministic/offline fallbacks apply, and don't mark something done until `scripts/check.sh` passes for it.

Treat `fangan_done.md` as a required deliverable of finishing spec work, not an afterthought.

## Full-Stack Parity (Backend ⇄ Frontend)

**No half-features.** In this product, every user-facing backend capability MUST ship with a corresponding frontend UI in the same change. It is not allowed to implement only one side.

- If you add a backend endpoint or data type that produces something a user should see or act on (a new knowledge type, list, action, status, field, analytics view, etc.), you MUST also add the frontend surface to view/use it — and vice versa (no frontend control that calls a missing endpoint).
- A feature is "done" only when: backend endpoint exists, `frontend/app/page.tsx` (or its components) exposes it, `scripts/check.sh` is green, and `cd frontend && npm run build` passes.
- Concretely, do NOT leave any object type "approvable but not browsable", any endpoint with no UI entry point, or any UI button wired to a non-existent route.
- Purely internal/infrastructure endpoints (health checks, migrations, observability logs) are exempt — they are not user-facing. When in doubt, treat it as user-facing and build the UI.

## Product Name

Use `silicon-notebook` as both the product name and project name.

Do not introduce a separate Chinese product name unless the user explicitly changes this decision.

## Product Flow

The app uses a two-level notebook structure:

1. Outer page: notebook collection/library.
2. Inner page: individual notebook workspace.

The notebook collection page should keep the current NotebookLM-inspired structure while staying visually distinct:

- Tabs such as `全部`, `我的笔记本`, `精选笔记本`.
- Search, view switch, sort, and `＋ 新建` controls.
- Grid, compact, and table-like list preview modes.
- The first card is `新建笔记本` when not searching.
- Notebook card upper-right menus must open real edit/delete actions.
- Collection search must include notebook metadata plus backend-searchable source metadata, parsed source elements, and article summaries.

When the user creates a notebook:

- Do not ask for title, purpose, or description up front.
- Immediately create an `Untitled notebook`.
- Open the notebook.
- Show the source selection/import UI immediately.
- Later ingestion and LLM analysis should infer title, description, domain, summary, and knowledge objects from imported sources.

Inside a notebook:

- Hide the global collection top bar.
- Keep visible `silicon-notebook` differences with a more engineering-console feel rather than a direct NotebookLM copy.
- Reset scroll to the top when switching into or back from the notebook workspace.
- The upper-left notebook title should be editable in place and save through the notebook update API.
- Left column: user-imported source files only.
  - Show how many sources are in the current notebook.
  - Keep source cards compact and readable for long mixed Chinese/English titles and summaries.
  - Source cards should open a source detail preview with element-level parsed text and expose a delete action.
  - Do not enable web/network source search yet; keep it as a disabled future affordance only.
- Center column: source-grounded knowhow tools, exposed as tabs.
  - This is the main interaction area: Ask (free question), Scenario query (structured form), Case search, Checklist generator, and Rule browser.
  - Answers must stay evidence-grounded (related rules/cases/checklist/risks, missing-information, citations) and support 👍/👎 feedback.
  - Prompt chips should run useful first-version questions; the menu should expose a real clear/reset action.
- Right column: Studio.
  - Keep Mind Map, New Article, and Infographic entries.
  - Article research drives the Mind Map / Infographic output; created articles must be listed with delete actions, and the lower Studio output area stays for generated outputs.
- Left column also hosts the curator Review Queue for extracted candidates.

## MVP Scope

The MVP is a real-team local beta, not a throwaway mockup.

Confirmed scope:

- Mixed Chinese/English source material.
- First supported file types: PDF, Markdown, DOCX, PPTX.
- Real multipart upload and local storage of original files.
- Parser-generated `SourceElement` records with element-level citation granularity.
- Source summary after parsing. Use the OpenAI-compatible client when configured; otherwise use deterministic fallback.
- Notebook-internal search over notebook metadata, source metadata, source element text, and article summaries.
- Automatic extraction (rule/method/risk/case/checklist/glossary candidates) with evidence binding, a curator review queue (approve/reject/edit), and a `knowledge_objects` store for approved knowledge are implemented.
- Ask, Scenario query, Case search, Checklist, Rule browser, and Article research are real and data-driven (hybrid keyword + embedding retrieval, citation validation, deterministic fallback offline). They are no longer demo-backed.
- Knowledge governance: `knowledge_objects` has a status lifecycle (`reviewed/approved/deprecated/conflict/project_specific`) plus `owner`/`last_reviewed`. Only USABLE statuses (approved/reviewed/project_specific/conflict) feed answers; `deprecated` is excluded. Browse via `GET /notebooks/{id}/{rules|methods|risks|glossary}`, edit via `PATCH /knowledge/{id}`, dedupe via `GET .../duplicates` + `POST /knowledge/{id}/merge`, conflicts via `GET .../conflicts`.
- Article research persists `article_claims` with relation metadata and draft `derived_rule_candidates`; feedback supports `useful` / `not_useful` plus an optional comment.
- User-created notebooks, imported sources, and Studio articles must expose delete actions. Source deletion removes parsed elements, extraction runs/candidates, embeddings, stale source-derived knowledge, and the stored local file; article deletion removes article claims and derived-rule candidates.
- Single-user mode for now, but keep user/system structure ready for future expansion.
- User memory must be manual opt-in. Do not add automatic memory behavior.
- Current demo data may be synthetic because no real semiconductor demo documents are available yet.
- PDF parsing is decoupled from the GPU via a MinerU adapter (`mineru_client.py`): `MINERU_MODE=http` calls a remote `mineru-api` service, `cli` runs MinerU's Python API (`do_parse/read_fn`) in an isolated subprocess, and `off` (default) uses the pypdf text fallback. The FastAPI backend process must never import torch/MinerU directly; keep it behind the adapter with pypdf fallback so no-GPU dev stays offline. MinerU fallback diagnostics must be kept in pipeline logs/source `error_message`. Formulas are preserved as LaTeX (`formula` elements), tables as HTML in metadata (`table` elements).
- On Apple Silicon (no NVIDIA, but MLX-capable) you can get high-fidelity parsing locally/offline: `pip install "mineru[core]"`, `mineru-models-download -m vlm`, then set `MINERU_MODE=cli` + `MINERU_BACKEND=vlm-auto-engine` in the local `.env` (gitignored). Use `MINERU_PARSE_METHOD=txt|ocr|auto` and `MINERU_LANG=en|ch|...` when you need to match a manual MinerU run; use a longer `MINERU_TIMEOUT_SECONDS` such as `1800` for full papers because local VLM can exceed 10 minutes. Keep `.env.example` default `off`. `check.sh`/smoke must stay offline and never require MinerU or model weights.

## Architecture Baseline

- `frontend/` is the only frontend path. (The former static `web/` fallback has been removed.)
- Backend uses FastAPI.
- Default local persistence is SQLite at `.local/silicon_notebook.db`, implemented with the Python standard library `sqlite3`.
- Source files are stored under `SILICON_NOTEBOOK_STORAGE_DIR`, defaulting to `.local/storage`.
- Default CORS origins include local frontend ports `3000` and `3001`; preserve this unless the frontend dev flow changes.
- Repository access should go through a repository boundary. `SQLiteRepository` is the current implementation; keep the interface clear for a future PostgreSQL repository.
- PostgreSQL + pgvector remain the future production/team-beta direction. Do not require them for the current local beta.
- Extraction runs/candidates, `knowledge_objects`, `element_embeddings`, `answers`, `feedback`, `article_claims`, and `derived_rule_candidates` tables are live. Embedding vectors are stored as JSON and cosine is computed in Python (no pgvector locally).
- Upload runs the parse → embed → extract pipeline asynchronously via FastAPI `BackgroundTasks`; the repository accepts a `scheduler` callback so scripts/tests can run it synchronously.
- Re-running parse/extraction for a source invalidates stale source-derived candidates and approved knowledge before writing the new extraction result.
- Deleting a source uses the same stale source-derived cleanup boundary, removes its local file, and clears any article research artifacts that depended on that source. Deleting an article cascades its claims and derived-rule candidates.

## Python Environment

Use the existing Miniconda Python environment for all backend development:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python
```

Do not create a new virtual environment, Conda environment, or project-local Python environment unless the user explicitly asks for it.

The temporary `.venv` created during early exploration was removed. Do not recreate it as the default workflow.

## Backend Commands

Run backend commands with the shared Python interpreter:

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app --reload --port 8000
```

For dependency checks or installs, use the same interpreter:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install -r backend/requirements.txt
```

Prefer setting `PYTHON_BIN` when a script supports it:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

## Frontend/UI

`scripts/dev.sh` starts the backend and the Next.js frontend from `frontend/`. It requires `frontend/node_modules`; run `npm install` in `frontend/` first if missing. The normal mainline is:

```bash
cd frontend
npm run dev
```

## No Docker In First Version

Do not add Docker or Docker Compose as the default first-version workflow.

Local beta should run directly on the shared Miniconda Python environment and the local Next.js frontend.

Docker can be introduced later only when the user asks for deployment packaging or when the project moves beyond the local beta workflow.

## Dependency Policy

- Treat the Miniconda base environment as the canonical local Python environment.
- Keep backend dependencies compatible with Python 3.13.
- Default backend requirements should not require PostgreSQL, pgvector, or SQLAlchemy while SQLite is the local baseline.
- Do not add `.venv/`, `venv/`, or new Conda environment setup as the default workflow.
- Ask for approval before installing new packages when network or environment mutation is required.

## Current Backend Baseline

The Miniconda environment has been used for:

```text
Python 3.13.11
fastapi
uvicorn
pydantic
openai
python-multipart
python-docx
pypdf
```

If any dependency is missing, install `backend/requirements.txt` into the shared Miniconda environment.

## LLM Configuration

LLM access must be OpenAI-compatible and configurable through environment variables:

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_EMBEDDING_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS
```

Business logic should call a provider adapter/client, not hard-code a specific model vendor.

When no LLM key is configured, endpoints may return deterministic fallback data so the local beta remains usable.

## Logging / Observability

Structured logs go to `.local/logs/*.jsonl` (gitignored) plus brief console lines, via a single `EventLogger` (`backend/app/core/event_logging.py`). Add observability at the existing chokepoints, not at each call site:

- `requests.jsonl` — HTTP middleware in `app/main.py` (method/path/status/latency/`request_id`; slow calls flagged via `SLOW_REQUEST_MS`; `X-Request-Id` response header).
- `events.jsonl` — source pipeline in `SQLiteRepository.process_source` / `_set_source_status` (per-stage timings + status transitions + failure stack).
- `llm.jsonl` — `LLMInteractionLogger` wrapping `OpenAICompatibleClient` (`app/core/llm.py`); chat detailed, embeddings summarized, errors recorded.

Rules: reuse `EventLogger` for any new structured log (it handles JSONL append + console + never raising); never log raw embedding vectors; chat prompt/response are truncated to `LLM_LOG_MAX_CHARS`. Config env vars: `LLM_LOG_ENABLED`, `LLM_LOG_PATH`, `LLM_LOG_MAX_CHARS`, `EVENT_LOG_ENABLED`, `EVENT_LOG_DIR`, `SLOW_REQUEST_MS` — keep `.env.example` aligned.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks:

- Backend Python syntax with the shared Miniconda Python.
- SQLite initialization and persistence smoke path.
- Markdown, DOCX, PPTX, and PDF upload/parse smoke path (sync and async-scheduler paths).
- Extraction → approve → delete → ask → feedback (with comment) → article research smoke path, plus retrieval scoring (keyword/vector, CJK tokenization, hybrid fusion, scenario boost, payload-level embeddings), demo-seed knowledge, derived-rule persistence, and stale-knowledge invalidation assertions.
- Logging: LLM interaction log, generic event log (parseable/disable/never-raise), and pipeline stage events + `error_message` regression.
- Source summary fallback and notebook-internal search.
- Next.js TypeScript when `frontend/node_modules` exists.

For local API checks, use:

```text
http://localhost:8000/api/health
http://localhost:8000/docs
```

For local UI checks, use:

```text
http://localhost:3000
```

## Git And File Safety

- Do not revert user changes.
- Do not remove generated or user-provided files unless the user explicitly asks.
- Keep changes scoped to the requested product or engineering task.
- Use `apply_patch` for manual file edits.
