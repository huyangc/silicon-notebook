# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a knowhow notebook platform for semiconductor engineering teams. It turns historical rules, debug cases, checklists, and technical articles into scenario-aware knowledge with element-level evidence citations.

## Current Scope

This repository now targets a local real-team beta loop:

- Python FastAPI backend
- SQLite persistence by default at `.local/silicon_notebook.db`
- Next.js / React / TypeScript frontend under `frontend/`
- OpenAI-compatible LLM configuration for summaries, extraction, answers, and article research; embeddings can be configured independently with the same compatible API endpoint
- Deterministic summary and answer fallbacks when no LLM key is configured
- Clean start for real teams: a fresh database seeds only the local user — no demo notebook or synthetic sources. New-notebook example prompts adapt to the chosen template / notebook domain instead of hardcoded samples
- Real multipart source upload (async parsing via FastAPI `BackgroundTasks`) for PDF, Markdown, DOCX, and PPTX
- PDF parsing via MinerU (formulas as LaTeX, tables, layout) when configured on a GPU host; pypdf text fallback locally / when MinerU is off
- Parsed `SourceElement` records with `element_type`, `location_label`, `text`, and `metadata`
- Automatic knowledge extraction (rule / method / risk / case / checklist / glossary candidates) with element-level evidence binding, plus a curator review queue (approve / reject / edit)
- Hybrid retrieval: keyword + optional embedding cosine over both source elements and approved knowledge
- Real source-grounded answers with citation validation: Ask, Scenario query, Case search, Checklist generator, Article research, and 👍/👎 feedback
- Knowledge governance: browse rules/methods/risks/glossary, status lifecycle (reviewed/approved/deprecated/conflict/project_specific) + owner/last_reviewed, duplicate detection & merge, and conflict detection (deprecated knowledge is excluded from answers) with optional comments
- Notebook collection page (grid/compact/list views, edit/delete), workspace title editing, source detail previews/delete, article delete, internal search
- No Docker in the first version

PostgreSQL + pgvector remain the target production/team-beta direction, with schema notes kept under `database/`, but local development does not require PostgreSQL.

## Local Setup

Copy the environment template:

```bash
cp .env.example .env
```

The default local database is:

```text
DATABASE_URL=sqlite:///.local/silicon_notebook.db
```

Default CORS origins include `localhost:3000` and `localhost:3001`, because Next.js may move to `3001` when `3000` is already occupied.

Use the Miniconda Python that is already available on this machine:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python --version
```

Install backend dependencies into that shared environment:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install -r backend/requirements.txt
```

Install frontend dependencies:

```bash
cd frontend
npm install
```

### Manual startup (recommended for agents / real processing)

Start the backend **without `--reload`** so async ingestion (parse → embed → extract)
runs to completion. With `--reload`, any file change restarts the worker and **kills
in-flight `BackgroundTask`s**, leaving uploaded sources stuck at `parse_status=extracting`.

```bash
# Backend (no --reload): foreground, or use `&` / nohup to background it
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
# Frontend (separate terminal)
cd frontend
npm run dev
```

Run the backend in the background and capture logs (handy for agents):

```bash
cd backend
nohup /opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 > /tmp/sn-backend.log 2>&1 &
```

Health check / open the UI:

```bash
curl -s http://127.0.0.1:8000/api/health      # {"status":"ok", "llm_configured":...}
open http://localhost:3000
```

The backend writes structured JSONL logs under `.local/logs/` (plus brief console lines),
so you can see what the app is doing and where an upload is stuck:

```bash
tail -f .local/logs/requests.jsonl   # every HTTP request: method, path, status, latency, request_id (SLOW flagged)
tail -f .local/logs/events.jsonl     # async pipeline stages (parse/embed/extract) + status transitions + failures
tail -f .local/logs/llm.jsonl        # LLM calls: chat (prompt/response/tokens/latency) + embedding summaries + errors
```

The `X-Request-Id` response header correlates a browser action with its server log line; the
DevTools console also prints `[api] METHOD /path -> status Nms (request_id)`. See "Observability"
below for details.

### Fast Path (dev iteration only)

```bash
npm run dev    # backend (uvicorn --reload) + Next.js frontend from repo root
```

Backend on `http://localhost:8000`, UI on `http://localhost:3000`. This path uses
`--reload`, so **only use it for UI/code iteration, not while processing uploads** (see
the warning above). If `frontend/node_modules` is missing, run `npm install` in `frontend/` first.

## Product Flow

The outer page is a notebook collection/library:

1. Click `＋ 新建` or the `新建笔记本` card.
2. The app creates an `Untitled notebook` immediately.
3. The notebook opens directly to source selection.
4. Import PDF, Markdown, DOCX, or PPTX files.
5. The backend stores the original file, parses source elements, creates a summary, and makes the source searchable.

Inside a notebook:

- Left column: user-imported source files (live parse-status while parsing/extracting) with detail previews and delete actions, plus the curator Review Queue. Network source search is intentionally disabled for now.
- Center column: source-grounded knowhow tools as tabs — Ask (free question), Scenario query (structured scenario form), Case search, Checklist generator, and Rule browser. Answers carry related rules/cases/checklist/risks, missing-information, citations, and 👍/👎 feedback.
- Right column: Studio with Mind Map, New Article, and Infographic; article research drives the Mind Map / Infographic output, and created articles can be deleted.

The notebook workspace hides the global collection top bar and keeps a more engineering-console visual treatment rather than copying NotebookLM exactly.

## APIs

Key local beta APIs:

- `GET /api/notebooks`
- `POST /api/notebooks`
- `PATCH /api/notebooks/{notebook_id}`
- `DELETE /api/notebooks/{notebook_id}`
- `POST /api/notebooks/{notebook_id}/sources` for multipart file upload (async parse/extract)
- `GET /api/sources/{source_id}`, `DELETE /api/sources/{source_id}`, and `POST /api/sources/{source_id}/parse` (poll status / re-run pipeline)
- `GET /api/sources/{source_id}/elements`, `POST /api/sources/{source_id}/extract`
- `GET /api/notebooks/{notebook_id}/candidates[/{type}]`, `PATCH /api/candidates/{id}`, `POST /api/candidates/{id}/approve|reject`
- `GET /api/notebooks/{notebook_id}/rules`
- `GET /api/notebooks/{notebook_id}/search?q=`
- `POST /api/notebooks/{notebook_id}/ask`, `.../scenario-query`, `.../case-search`, `.../checklist`
- `GET|POST /api/notebooks/{notebook_id}/articles`, `DELETE /api/articles/{id}`, `POST /api/articles/{id}/research`
- `POST /api/answers/{answer_id}/feedback`

## Configuration

LLM access uses OpenAI-compatible settings:

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS
SILICON_NOTEBOOK_CORS_ORIGINS
```

Embeddings (semantic recall) are configured separately:

```text
EMBED_PROVIDER          # ""=off (keyword only) | local | dashscope
EMBED_MODEL             # e.g. BAAI/bge-m3 (local) or the API model name
EMBED_BASE_URL          # dashscope / OpenAI-compatible embedding endpoint
EMBED_API_KEY
EMBED_DIM               # must match the model's output dimension (default 1024)
```

Logging is configured via:

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS   # LLM interaction log (chat truncated to MAX_CHARS)
EVENT_LOG_ENABLED / EVENT_LOG_DIR                     # HTTP + pipeline event logs
SLOW_REQUEST_MS                                       # requests slower than this (ms) are flagged SLOW
```

When LLM settings are not configured, extraction, summaries, and answers fall back to deterministic heuristics so the local beta remains usable end-to-end (offline).

## Observability

The backend emits structured logs through a single `EventLogger` (`app/core/event_logging.py`): one JSONL line per event under `.local/logs/` plus a brief console line. Logging is best-effort — it never breaks the request or pipeline it observes — and is a no-op for the LLM channel when no model is configured.

- `requests.jsonl` — every HTTP request (method, path, status, latency, `request_id`). Requests slower than `SLOW_REQUEST_MS` (default 3000ms) are flagged `SLOW`. Responses carry an `X-Request-Id` header to correlate browser and server.
- `events.jsonl` — async source pipeline: per-stage timings (`parse` / `embed` / `extract`) and every status-machine transition. A "stuck" upload shows exactly which stage is running and for how long; failures record the real exception (and the source's `error_message`).
- `llm.jsonl` — every LLM call: chat (prompt/response/tokens/latency, truncated to `LLM_LOG_MAX_CHARS`), embeddings (summary only, no raw vectors), and errors that the heuristic fallback would otherwise hide.

In the browser, the DevTools console mirrors requests as `[api] METHOD /path -> status Nms (request_id)`; while polling, the UI shows the pending stage / elapsed time and surfaces a source's `error_message` on failure.

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
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install -U "mineru[core]"
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

MinerU output maps to structured `SourceElement`s: formulas become `formula` elements (LaTeX preserved), tables become `table` elements (HTML kept in metadata), and headings keep their level. The frontend renders these in the source detail view — formulas via KaTeX, tables from their HTML — so equations show typeset rather than as raw LaTeX. If MinerU is unreachable or errors, ingestion degrades to pypdf so uploads never block, while pipeline logs and the source `error_message` keep the fallback diagnostic; a PDF that parses to zero text (e.g. a scanned/image PDF) is flagged with a hint instead of looking like an empty success.

## Current Limitations

- Retrieval is SQLite keyword + in-Python embedding cosine; BM25/FTS5 and pgvector are deferred.
- Rule governance is basic (approve/reject); status lifecycle, duplicate merge, and conflict detection are next (Tier 2).
- Article Studio works from title/abstract text and linked source elements when present; first-class article full-text upload and richer relation scoring are next (Tier 3).
- PostgreSQL + pgvector are not required for the local beta and are deferred.
- The `off`-mode PDF fallback uses pypdf layout extraction with heading/paragraph segmentation (decent reading order, no new deps) — but formulas, tables, and scanned/image PDFs still need MinerU; see "PDF parsing with MinerU".
- User memory remains manual opt-in only; no automatic memory behavior has been added.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks backend syntax, a SQLite/upload/parser/extract/approve/delete/ask/feedback/article smoke path (including the retrieval scoring and async-upload paths), and Next.js TypeScript when dependencies are installed.

## Documentation Maintenance

When product behavior, setup, architecture, or development constraints change, update all of these files together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`
