# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a knowhow notebook platform for semiconductor engineering teams. It turns uploaded technical documents into a queryable knowledge graph of Concept / Claim / Formula / Procedure objects, with element-level evidence citations and grounded multi-turn Q&A.

## Current Scope

This repository targets a local real-team beta loop built around a KG-native pipeline:

- Python FastAPI backend; SQLite persistence at `.local/silicon_notebook.db`
- Next.js / React / TypeScript frontend under `frontend/`
- OpenAI-compatible LLM endpoint for extraction, answers, and article research; embeddings configured independently via `EMBED_*` variables
- Deterministic fallbacks when no LLM/embedder is configured — the whole pipeline runs offline
- Clean start: a fresh database seeds only the local user; no demo notebook or synthetic sources
- Multipart source upload for PDF, Markdown, DOCX, PPTX, CSV, and XLSX (async via FastAPI `BackgroundTasks`)
- **KG-native ingestion**: structured Markdown parse → greedy-window KG extraction (Concept / Claim / Formula / Procedure) with concurrent embedding → extraction-first status (`extracted` = KG ready, does not wait for embedding)
- PDF parsing via MinerU (formulas as LaTeX, tables, layout) when configured; pypdf text fallback locally or when MinerU is off
- Hybrid retrieval: CJK-aware bi-gram keyword + float32 matrix semantic search with per-notebook cache
- KG-native grounded Q&A: sentence-level `[k_i]` citations, multi-turn conversations, 1-hop KG neighbour expansion
- Knowledge governance: browse by type via `/knowledge-types` + `/knowledge?type=...`, status lifecycle, duplicate detection & merge, conflict detection; `deprecated` objects excluded from retrieval and 1-hop expansion
- Unified KG: cross-document concept clustering (`concept_clusters`), pending-merges review
- Object-level KG visualization: Concept / Claim / Formula / Procedure nodes with type-specific shapes, edge labels, multi-select filters, and a type-grouped side panel
- Notebook collection (grid/compact/list, edit/delete); clicking `＋ 新建` creates an `Untitled notebook` and enters it immediately — no dialog
- No Docker in the first version

PostgreSQL + pgvector remain the future production/team-beta direction; local development does not require them.

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
npm run dev    # backend (no --reload) + Next.js frontend from repo root
```

Backend on `http://127.0.0.1:8000`, UI on `http://localhost:3000`. If you need
backend auto-reload for code-only iteration, run `npm run dev:backend:reload` in a
separate terminal and avoid it while processing uploads. If `frontend/node_modules`
is missing, run `npm install` in `frontend/` first.

## Product Flow

The outer page is a notebook collection/library (KG-native pipeline):

1. Click `＋ 新建` — the app creates an `Untitled notebook` and enters it immediately (no dialog).
2. Upload PDF, Markdown, DOCX, PPTX, CSV, or XLSX sources (multipart).
3. Backend: structured Markdown parse → KG extraction (Concept / Claim / Formula / Procedure objects, 16-worker concurrent windows) running in the foreground, while element embedding runs concurrently in a background daemon thread.
4. Source turns green (`extracted`) as soon as KG extraction completes — no need to wait for embedding.
5. Knowledge objects are stored in `knowledge_objects` + `knowledge_relations` with element-level evidence bindings.
6. Hybrid retrieval (bi-gram keyword + float32 matrix semantic) feeds KG-native Q&A: answers contain sentence-level `[k_i]` citations, support multi-turn conversations, and expand via 1-hop KG neighbours.
7. Unified KG aggregates concepts across documents; pending cross-document merges can be confirmed or rejected.

Inside a notebook:

- Left column: user-imported source files with live parse-status (green = `extracted` only; others shown in amber while processing), detail previews, and delete actions. Network source search is disabled for now.
- Center column: two tabs — **Ask** (KG-native grounded Q&A with `[k_i]` sentence citations, multi-turn conversation list, 👍/👎 feedback) and **Knowledge** (browse any object type dynamically from `/knowledge-types`, with status lifecycle, duplicate detection, and conflict detection).
- Knowledge Graph opens as a full-screen overlay: object-level KG nodes (Concept / Claim / Formula / Procedure) with type-specific shapes, edge relationship labels, multi-select type filters, and a type-grouped side panel that focuses the canvas on selection.
- Right column: Studio with articles, derived-rule candidates, and Knowledge Graph entry.

The notebook workspace hides the global collection top bar and keeps an engineering-console visual treatment.

## APIs

Key local beta APIs:

- `GET /api/notebooks`, `POST /api/notebooks`, `PATCH /api/notebooks/{id}`, `DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `POST /api/notebooks/{id}/sources` — multipart file upload (async parse/extract)
- `GET /api/sources/{id}`, `DELETE /api/sources/{id}`, `POST /api/sources/{id}/parse`, `GET /api/sources/{id}/elements`
- `GET /api/notebooks/{id}/knowledge-types`, `GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`, `PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask` — KG-native grounded Q&A with `[k_i]` citations
- `GET /api/notebooks/{id}/conversations`, `GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- `GET|POST /api/notebooks/{id}/articles`, `DELETE /api/articles/{id}`, `POST /api/articles/{id}/research`
- Unified KG: `POST .../unified-kg/rebuild`, `GET .../unified-kg`, `GET .../unified-kg/pending-merges`, `POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`, `GET .../objects/{object_id}/context`
- `GET /api/object-schemas`, `POST /api/object-schemas`, `PATCH /api/object-schemas/{type}`, `DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`, `POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- `GET /api/notebooks/{id}/derived-rules`, `POST /api/derived-rules/{id}/approve|reject`

## Configuration

All model services are reached over URL endpoints — no local model servers are started.

**LLM (OpenAI-compatible):**

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS   # default 60
OPENAI_COMPAT_MAX_RETRIES       # default 2
```

**Embeddings:**

```text
EMBED_PROVIDER          # ""=off (keyword-only) | dashscope
EMBED_MODEL             # required with EMBED_PROVIDER=dashscope, e.g. text-embedding-v4
EMBED_BASE_URL          # required embedding endpoint URL
EMBED_API_KEY
EMBED_DIM               # must match model output dimension (default 1024)
EMBED_TRUNCATE_CHARS    # max chars fed to embedder per text (default 2000)
EMBED_BATCH_SIZE        # elements per embedding call (default 10)
EMBED_PERSIST_CHUNK     # rows written to DB per batch (default 200)
EMBED_CONCURRENCY       # concurrent embedding threads (default 50)
```

**KG extraction windowing:**

```text
KG_WINDOW_TARGET_CHARS      # greedy-pack target window size (default 9000)
KG_WINDOW_OVERLAP_CHARS     # overlap between adjacent windows (default 450)
KG_EXTRACT_WORKERS          # ThreadPoolExecutor size for window extraction (default 16)
KG_WINDOW_WARN_THRESHOLD    # log WARNING when window count exceeds this (default 1200)
```

**Database:**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout in ms (default 30000)
DATABASE_URL            # SQLite path (default .local/silicon_notebook.db)
STORAGE_DIR             # uploaded file storage directory
```

**Retrieval:**

```text
RETRIEVAL_TOP_N         # top-N hits before 1-hop expansion (default 12)
```

**MinerU (PDF parsing):**

```text
MINERU_MODE             # off (default) | http | cli
MINERU_API_URL          # remote mineru-api endpoint (http mode)
MINERU_BACKEND          # pipeline | vlm-auto-engine | vlm-http-client | vlm-sglang-client
MINERU_VLM_SERVER_URL   # standalone VLM inference server URL
MINERU_PARSE_METHOD     # auto | txt | ocr
MINERU_LANG             # e.g. en, ch
MINERU_MODEL_SOURCE     # huggingface | modelscope
MINERU_TIMEOUT_SECONDS  # MinerU call timeout
MINERU_FORMULA_ENABLE   # true/false
MINERU_TABLE_ENABLE     # true/false
```

**Logging:**

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS
EVENT_LOG_ENABLED / EVENT_LOG_DIR
SLOW_REQUEST_MS         # requests slower than this (ms) are flagged SLOW (default 3000)
SILICON_NOTEBOOK_CORS_ORIGINS
```

When LLM settings are not configured, summaries and answers fall back to deterministic behavior. Source parsing still completes offline, and KG extraction records a completed `no-llm` run without generating synthetic knowledge.

## Observability

The backend emits structured logs through a single `EventLogger` (`app/core/event_logging.py`): one JSONL line per event under `.local/logs/` plus a brief console line. Logging is best-effort — it never breaks the request or pipeline it observes — and is a no-op for the LLM channel when no model is configured.

The browser/API debug log viewer (`/dev/logs` and `/api/debug/logs/...`) is opt-in:
set `DEBUG_LOGS_ENABLED=true` for local inspection. Full LLM records can include
prompt/response text from private source material, so the viewer is disabled by
default.

- `requests.jsonl` — every HTTP request (method, path, status, latency, `request_id`). Requests slower than `SLOW_REQUEST_MS` (default 3000ms) are flagged `SLOW`. Responses carry an `X-Request-Id` header to correlate browser and server.
- `events.jsonl` — async source pipeline: per-stage timings (`parse` / `embed` / `extract`) and every status-machine transition. A "stuck" upload shows exactly which stage is running and for how long; failures record the real exception (and the source's `error_message`).
- `llm.jsonl` — every LLM call: chat (prompt/response/tokens/latency, truncated to `LLM_LOG_MAX_CHARS`), embeddings (summary only, no raw vectors), and errors that deterministic fallback paths would otherwise make easy to miss.

In the browser, the DevTools console mirrors requests as `[api] METHOD /path -> status Nms (request_id)`; while polling, the UI shows the pending stage / elapsed time and surfaces a source's `error_message` on failure.

**Log viewer — `/dev/logs`.** A read-only debug page that visualizes these JSONL channels (LLM channel in v1). The left list is filterable by kind / status / model with full-text search; the detail pane shows exactly what was sent to the LLM (the `system` / `user` messages and the `schema_hint`) alongside the model's response, token usage, and latency. It is served by gated backend endpoints under `/api/debug/logs/...` — set `DEBUG_LOGS_ENABLED=false` to hide them.

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

- Retrieval uses SQLite keyword (CJK bi-gram) + float32 matrix semantic search with per-notebook cache. Memory is bounded (~hundreds of MB vs the old ~1.3 GB Python-list approach). BM25/FTS5 and pgvector are deferred for larger scale.
- Large-document ingestion is hardened: greedy-window KG extraction (cost scales linearly with document size), concurrent embedding with per-batch DB writes, and extraction-first pipeline. For very large corpora, adding `sqlite-vec` is a natural next step.
- Ask no longer performs synchronous embedding backfill or a full source-element scan; it uses available keyword/vector indexes and stays responsive while maintenance jobs run. Ask emits per-stage timing (`ask_stage` events).
- Unified KG rebuild is explicit and observable via `GET /notebooks/{id}/unified-kg/status`; ingesting a source marks the graph dirty instead of rebuilding synchronously, and opening the graph overlay no longer auto-rebuilds (refresh on demand).
- Cross-document concept merge uses deterministic alias normalization plus bounded top-k vector candidates (scales past thousands of concepts); optional LLM pre-review (`POST /notebooks/{id}/unified-kg/merges/review`) confirms/rejects high-confidence near-synonym merges in small batches.
- LLM-backed KG extraction requires configured `OPENAI_COMPAT_*`; offline smoke tests seed KG objects explicitly when retrieval/governance assertions are needed.
- Article Studio works from title/abstract text and linked source elements; first-class article full-text upload and richer relation scoring are next (Tier 3).
- PostgreSQL + pgvector are not required for the local beta and are deferred.
- The `off`-mode PDF fallback uses pypdf layout extraction (decent reading order, no new deps) — formulas, tables, and scanned/image PDFs still need MinerU; see "PDF parsing with MinerU".
- User memory remains manual opt-in only; no automatic memory behavior has been added.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks backend syntax (`py_compile`), a hermetic offline smoke path (`smoke_backend.py` — `mineru_mode=off`, no real LLM/embedding keys) covering upload/parse, structural Markdown parsing, KG windowing, concurrent embedding with per-batch DB writes, float32 vector matrix build and cache, hybrid retrieval (keyword/vector/None modes), multi-turn `ask`, status machine (`extracted` = green), article research, feedback, JSON fence cleanup, and restart persistence. Also runs Next.js `tsc --noEmit` when `frontend/node_modules` is present.

## Documentation Maintenance

When product behavior, setup, architecture, or development constraints change, update all of these files together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`
