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
- KG-native grounded Q&A: sentence-level `[k_i]` citations, multi-turn conversations, 1-hop KG neighbour expansion, and a live, expandable one-line agent trace for reasoning mode
- Two-tier knowledge base: each notebook has a `tier` (`base` | `personal`, default `personal`). `base` is the authoritative reference KG (e.g. an analog-design textbook); `personal` is the user's own notes. `federated_retrieve` gathers candidates across `base ∪ active personal`, tags each hit with its tier, applies a base-authority weight in ranking, and on a base↔personal contradiction the answer defers to the base position and surfaces the discrepancy. Citations carry their tier (`AnswerAnchor.tier`) and Ask renders a `base`/`personal` badge per cited anchor. The notebook actions menu ("分析") offers "设为基准库 / 取消基准库" to mark a notebook as the base KG and back (via `POST /api/notebooks/{id}/tier`)
- **User accounts**: self-service registration (username rule: a single letter + `00` + 6 digits, e.g. `a00123456`; stored lower-cased) + password login with opaque Bearer session tokens. Each notebook is owned by its creator; users see only their own notebooks. On first boot the built-in `admin` account is created (login `admin`, password from `SILICON_NOTEBOOK_ADMIN_PASSWORD`, default `admin`); the admin owns pre-existing notebooks and is the only user who can mark a notebook as the base KG. Base notebooks are hidden from regular users' lists but are still used as authoritative retrieval context at ask time. Set `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` for local/no-auth testing. The frontend shows a login/register gate on first load; the topbar displays the logged-in username and a logout button.
- Optional graph-reasoning Ask mode (`mode="graph"`, opt-in/experimental): a rustworkx in-memory graph built from `knowledge_relations` is traversed for bounded multi-hop derivation/support chains, with answer-time adversarial chain verification and a weakest-link `chain_trust` score (the default Ask mode stays `chunk`)
- Edge trust & curation: per-edge trust signals (evidence / corroboration / type-validity) plus a curator review queue; reviewer-rejected edges are excluded from graph reasoning
- Knowledge governance: browse by type via `/knowledge-types` + `/knowledge?type=...`, status lifecycle, duplicate detection & merge, conflict detection; `deprecated` objects excluded from retrieval and 1-hop expansion. Personal→base node promotion (propose → under review → approve/reject) with dedup-on-approve and a curator promotion queue
- Unified KG: cross-document concept clustering (`concept_clusters`), pending-merges review
- Object-level KG visualization: Concept / Claim / Formula / Procedure nodes with type-specific shapes, edge labels, multi-select filters, and a type-grouped side panel
- Notebook collection (grid/compact/list, edit/delete); clicking `＋ 新建` creates an `Untitled notebook` and enters it immediately — no dialog
- No Docker in the first version

PostgreSQL + pgvector remain the future production/team-beta direction; local development does not require them.

## Deployment

silicon-notebook runs as two processes — a FastAPI backend and a Next.js frontend — over
a local SQLite database. It requires **no GPU, no database server, and no local model
server**: every model (LLM, embeddings, rerank, MinerU) is reached over a URL endpoint,
and the pipeline runs offline with deterministic fallbacks when none are configured.

### Prerequisites

- **Python ≥ 3.11**
- **Node.js ≥ 20** and npm
- **git**
- A C/C++ toolchain is needed *only as a fallback* — `numpy`, `rustworkx`, and `hnswlib`
  ship prebuilt wheels for common platforms; install Xcode Command Line Tools (macOS) or
  `build-essential` (Debian/Ubuntu) only if pip has to build one from source.

### 1 · Install

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

# Backend — into an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt

# Frontend
( cd frontend && npm install )
```

### 2 · Configure

```bash
cp .env.example .env
```

The service boots with every value blank — deterministic offline mode (keyword-only
retrieval, no LLM extraction or answers). To enable full functionality, set at minimum:

- **LLM** (extraction, answers, article research) — `OPENAI_COMPAT_BASE_URL` /
  `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_MODEL`; any OpenAI-compatible endpoint.
- **Embeddings** (semantic retrieval; otherwise keyword-only) — `EMBED_PROVIDER=dashscope`
  plus `EMBED_MODEL` / `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_DIM` (must equal the
  model's output dimension).
- **PDF fidelity** (optional) — a MinerU endpoint, see [PDF parsing with MinerU](#pdf-parsing-with-mineru);
  leave `MINERU_MODE=off` for the pypdf text fallback.

`.env.example` is the authoritative, fully-commented list of every variable;
[Configuration](#configuration) groups the common ones.

**Remote access — note the browser is on a *different* machine** than the server, so it
cannot use `localhost`/`127.0.0.1` (those resolve to each visitor's own machine).

**Recommended for a single co-located server (same-origin proxy):** the Next.js frontend
proxies `/api/*` to the local backend (`frontend/next.config.mjs`), so browsers only ever
talk to the frontend's origin. Point the frontend at a relative `/api` — then you need **no
CORS config** and **don't expose the backend port**:

```bash
# frontend/.env.local  (NEXT_PUBLIC_* is baked at BUILD time → rebuild after changing)
NEXT_PUBLIC_API_BASE_URL=/api
```

The backend can stay on `127.0.0.1:8000` (the proxy reaches it locally); only the frontend
port needs to be network-reachable. Set `BACKEND_PROXY_TARGET` if the backend isn't on
`127.0.0.1:8000`.

**Alternative — frontend and backend on different hosts (two-origin, direct):** point the
frontend at the backend's reachable URL and allow its origin on the backend:

```bash
# frontend/.env.local  (baked at build time)
NEXT_PUBLIC_API_BASE_URL=http://<backend-host>:8000/api
# backend repo-root .env — comma-separated allowed origins; `*` not allowed (credentials on)
SILICON_NOTEBOOK_CORS_ORIGINS=http://<frontend-host>:3000
```

Then run uvicorn with `--host 0.0.0.0` (or `BACKEND_HOST=0.0.0.0 npm run dev`) so the API is
reachable from other machines.

### 3 · Run

There is **no migration or seed step** — on first boot the backend creates the SQLite
schema and the `.local/storage` and `.local/logs` directories, and seeds only the local
user. Always run the backend **without `--reload`**: a reload restart kills in-flight
ingestion background tasks and leaves uploads stuck at `extracting`. Start it from
`backend/` so it loads `.env` and resolves the database to the repo-root `.local/`.

```bash
# Development — backend and frontend together
npm run dev
```

```bash
# Production
( cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 )   # behind a reverse proxy
( cd frontend && npm run build && npm run start )                              # serves the UI on :3000
```

### 4 · Verify

```bash
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok","llm_configured":...}
bash scripts/check.sh                        # backend offline smoke + frontend tests + tsc
```

The backend writes structured JSONL logs under `.local/logs/` (`requests` / `events` /
`llm`); see [Observability](#observability) to follow an upload or diagnose a stuck source.

## Product Flow

The outer page is a notebook collection/library (KG-native pipeline):

1. Click `＋ 新建` — the app creates an `Untitled notebook` and enters it immediately (no dialog).
2. Upload PDF, Markdown, DOCX, PPTX, CSV, or XLSX sources (multipart).
3. Backend: structured Markdown parse → KG extraction (Concept / Claim / Formula / Procedure objects) via a shared global extraction pool — window concurrency capped by `KG_EXTRACT_WORKERS` across all documents, document concurrency by `KG_JOB_CONCURRENCY` — while element embedding runs concurrently in a background daemon thread.
4. Source turns green (`extracted`) as soon as KG extraction completes — no need to wait for embedding.
5. Knowledge objects are stored in `knowledge_objects` + `knowledge_relations` with element-level evidence bindings.
6. Hybrid retrieval (bi-gram keyword + float32 matrix semantic) feeds KG-native Q&A: answers contain sentence-level `[k_i]` citations, support multi-turn conversations, and expand via 1-hop KG neighbours.
7. Unified KG aggregates concepts across documents; pending cross-document merges can be confirmed or rejected.

Inside a notebook:

- Header: the editable notebook title stays compact by itself; the notebook description is shown in the Ask welcome state when no conversation is active, and toolbar actions keep their labels intact across desktop widths.
- Left column: user-imported source files with live parse-status (green = `extracted` only; others shown in amber while processing), detail previews, and delete actions. Network source search is disabled for now.
- Main column: two tabs — **Ask** (grounded Q&A with `[k_i]` sentence citations across three retrieval modes — see [Retrieval modes](#retrieval-modes-ask) — multi-turn conversation list, live collapsed reasoning trace with expandable details, 👍/👎 feedback) and **Knowledge** (browse any object type dynamically from `/knowledge-types`, with status lifecycle, duplicate detection, and conflict detection). In Ask, `Enter` submits, `Shift+Enter` keeps a newline, and while a model response is running the input/mode controls are locked while the send button becomes an interrupt control. Interrupting aborts the `/ask/stream` request and propagates a backend cancel signal so the Ask worker/LLM path stops and does not save a cancelled final answer. The inactive Studio right sidebar is not shown in the primary workspace, so the Ask panel can use the freed width.
- Knowledge Graph opens as a full-screen overlay: object-level KG nodes (Concept / Claim / Formula / Procedure) with type-specific shapes, edge relationship labels, multi-select type filters, and a type-grouped side panel that focuses the canvas on selection. The side panel renders source excerpts as structured evidence cards so long titles, locations, formulas, and mixed Chinese/English text wrap inside the panel.
- Studio-style article research, mind map / infographic generation, derived-rule review, the governance **promotion queue** (propose a personal-KG node for promotion to the base corpus, then approve/reject pending requests), the **mark-base / mark-personal** tier toggle, and the **edge-review queue** (confirm / reject relations ranked by high-centrality × low-trust; rejected edges are excluded from graph reasoning) remain reachable from the top analysis toolbar and show their output in dialogs rather than a fixed right column.

The notebook workspace hides the global collection top bar and keeps an engineering-console visual treatment.

## Retrieval modes (Ask)

`POST /ask` dispatches on `mode` — the registry `backend/app/services/ask_modes.py` is the single source of truth (default `chunk`). Every mode federates retrieval across `tier=base` ∪ the active personal notebook, emits sentence-level `[k_i]` anchors, and grades grounding identically: `classify_evidence` → `grounded` / `overview` / `inferred` against the calibrated `EVIDENCE_TAU_*` thresholds. **Ranking signals (rerank / RRF / tier weights) only reorder candidates — they never feed the grounding threshold** (which reads each item's fused keyword+semantic relevance).

| Mode | Group | Needs KG | One-liner |
|------|-------|----------|-----------|
| **`chunk`** (default) | general | no | Chunk-native general Q&A: large recall → selection → long-context synthesis → citations bound to source chunks. |
| **`graph`** | strict | yes | Single-pass Personalized-PageRank propagation across the cross-document knowledge graph. |
| **`reasoning`** | strict | yes | Agentic, iterative plan → retrieve → reflect → answer (streams a live trace). |

**`chunk` — chunk-native, with optional chunk×graph mix.**
- *Baseline:* large chunk recall (`CHUNK_RECALL`) → MMR / multi-sub-query quota diversity selection (`CHUNK_MMR_K`) → long-context synthesis. The KG is not touched.
- *Mix* (active only when `CHUNK_KG_OVERLAY_ENABLED=true` **and** qwen3-rerank is configured **and** a KG is available): three sources are pooled — (a) vector chunks, (b) the KG local structure around the query seeds (entities + their 1-hop relations, retrieved once), (c) the source chunks behind those KG objects — round-robin merged, reranked by a qwen3 cross-encoder, then packed to a token budget (`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`). The answer cites chunks and KG items in one unified `[k]` map, and grounding spans chunk ∪ KG. When rerank is unconfigured or no KG exists, it falls back byte-for-byte to the baseline. (Faithful to LightRAG's `mix` mode.)

**`graph` — PPR over the cross-document KG.** Seeds via `federated_retrieve` (KG entities + their source chunks; optionally fused with relation-index hits when `RELATION_RETRIEVAL_ENABLED=true`) become the personalization vector for HippoRAG-style **Personalized PageRank** (`GRAPH_PPR_ENABLED`, on by default), which propagates relevance across documents through the shared knowledge graph; the top-ranked chunks feed a grounded answer whose `[k]` anchors point at KG objects/relations. With `GRAPH_PPR_ENABLED=false` it falls back to bounded BFS along reasoning edges.

**`reasoning` — agentic deep retrieval.** Delegates to `ReasoningRetriever`: it decomposes the question, retrieves (via the same PPR propagation as `graph`), reflects on sufficiency, and expands the graph / adds sub-queries until it can answer — emitting a `reasoning_trace` over the NDJSON stream (`/ask/stream`). Strict / KG-grounded.

Retired ids `fast` and `global` are transparently remapped to `chunk` (old sessions/bookmarks never 422); any other unknown mode is rejected with HTTP 422.

## APIs

Key local beta APIs:

- `GET /api/notebooks`, `POST /api/notebooks`, `PATCH /api/notebooks/{id}`, `DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `POST /api/notebooks/{id}/sources` — multipart file upload (async parse/extract)
- `GET /api/sources/{id}`, `DELETE /api/sources/{id}`, `POST /api/sources/{id}/parse`, `GET /api/sources/{id}/elements`
- `GET /api/notebooks/{id}/knowledge-types`, `GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`, `PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask` — grounded Q&A with `[k_i]` citations (`mode`: `chunk` default | `graph` | `reasoning`, see [Retrieval modes](#retrieval-modes-ask); tier-aware, federates across base + active personal)
- `POST /api/notebooks/{id}/ask/stream` — NDJSON stream for reasoning-mode Ask progress (`progress` trace events rendered as a live collapsed trace row, then final `AskResponse`); client disconnect/abort sets a backend cancellation event that stops the in-flight Ask path before it writes a final answer
- `GET /api/notebooks/{id}/conversations`, `GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- `GET|POST /api/notebooks/{id}/articles`, `DELETE /api/articles/{id}`, `POST /api/articles/{id}/research`
- Unified KG: `POST .../unified-kg/rebuild`, `GET .../unified-kg`, `GET .../unified-kg/pending-merges`, `POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`, `GET .../objects/{object_id}/context`
- `GET /api/object-schemas`, `POST /api/object-schemas`, `PATCH /api/object-schemas/{type}`, `DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`, `POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- `GET /api/notebooks/{id}/derived-rules`, `POST /api/notebooks/{id}/derived-rules/{candidate_id}/approve|reject`
- Two-tier: `POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → returns the updated `NotebookSummary` (400 on bad tier, 404 on missing notebook). Sets the notebook's federation tier (base = authoritative reference KG, personal = default user notes).
- Edge trust & curation: `GET /api/notebooks/{id}/edge-review-queue`, `POST /api/notebooks/{id}/relations/{rel_id}/review`
- Governance / promotion: `POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`, `GET /api/promotion-queue`, `POST /api/promotion-queue/{candidate_id}/approve|reject`

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
EMBED_CONCURRENCY       # concurrent embedding threads (default 8; mild, avoids 429)
```

**KG extraction concurrency & windowing:**

```text
KG_EXTRACT_WORKERS          # GLOBAL cap on concurrent extraction LLM calls (windows),
                            # shared across all documents, intra- + inter-doc (default 16)
KG_JOB_CONCURRENCY          # how many documents extract concurrently; their windows
                            # share the global KG_EXTRACT_WORKERS budget (default 8)
KG_ASK_RESERVE              # LLM connections reserved for interactive Ask so it is not
                            # starved during extraction; pool = WORKERS + RESERVE (default 64)
KG_WINDOW_TARGET_CHARS      # 0 = adaptive window size (default); >0 forces a fixed size
KG_WINDOW_MIN_CHARS         # adaptive window lower bound (default 4000)
KG_WINDOW_MAX_CHARS         # adaptive window upper bound (default 8000)
KG_WINDOW_OVERLAP_CHARS     # overlap between adjacent windows (default 450)
KG_WINDOW_WARN_THRESHOLD    # log WARNING when window count exceeds this (default 1200)
```

**Database:**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout in ms (default 30000)
DATABASE_URL            # SQLite path (default .local/silicon_notebook.db)
SILICON_NOTEBOOK_STORAGE_DIR   # uploaded file storage directory (default .local/storage)
```

**Retrieval:**

```text
RETRIEVAL_TOP_N         # top-N hits before 1-hop expansion (default 12)
```

**Retrieval / KG enhancements (GraphRAG + ToG-3 borrow, Phase 1+2):**

A mix of opt-in (default off) and on-by-default knobs. On by default: `ANSWER_CONTEXT_*`,
`KG_QUERY_REFINE_ENABLED`, and the KG-quality passes `KG_REFINE` / `KG_GLEANING` /
`KG_CONCEPT_DESC`. Enable other extras **one at a time** and validate with the eval
harness (`backend/app/eval`) — turning RRF + rerank + refinement on together regressed
answer quality in eval.

```text
LLM_CACHE_ENABLED            # cache LLM responses in a separate sqlite (default false)
LLM_CACHE_PATH               # cache DB path (default .local/llm_cache.db)
KG_REFINE_ENABLED            # extraction self-verify: drop hallucinated nodes (default true)
KG_GLEANING_ENABLED          # extra rounds asking the LLM for MISSED nodes (default true)
KG_GLEANING_ROUNDS           # gleaning rounds when enabled (default 1)
KG_CONCEPT_DESC_ENABLED      # LLM-fuse cross-doc concept-cluster descriptions (default true)
KG_COMMUNITY_SUMMARY_ENABLED # LLM community reports; required for Global QA (default false)
ANSWER_CONTEXT_BUDGET_CHARS  # answer-context assembly char budget (default 6000)
ANSWER_CONTEXT_MIN_ITEMS     # keep >= N items regardless of budget (default 3)
RETRIEVAL_RRF_ENABLED        # BM25(Okapi)+RRF ranking vs keyword+semantic fusion (default false)
RETRIEVAL_RRF_K              # reciprocal-rank-fusion k (default 60)
KG_QUERY_REFINE_ENABLED      # question-aware evidence refinement before answering (default true)
QUERY_REFINE_MAX_CHARS       # max chars of evidence fed to refinement (default 4000)
GLOBAL_MAX_COMMUNITIES       # max community reports for Global QA, ask mode="global" (default 20)
RELATION_RETRIEVAL_ENABLED   # relation-vector retrieval for graph/reasoning seeds (default false, opt-in pending eval)
RELATION_SEED_TOP_N          # top relation/node hits fed as graph seeds when enabled (default 8)
KG_CANONICAL_FOLD_ENABLED    # fold same-canonical fragmented KG nodes at retrieval (default false)
KG_ABOUT_DOWNWEIGHT_ENABLED  # rank-down-weight weak `about` edges in relation retrieval (default false)
CHUNK_RECALL                 # chunk 大召回数 (default 200; mix 候选池 / MMR 候选)
CHUNK_MMR_K                  # MMR-selected chunks when rerank is off (default 16)
CHUNK_KG_OVERLAY_ENABLED     # chunk×graph mix: 叠加 KG 局部结构+源 chunk (default true; 需配 qwen3-rerank 才生效)
RERANK_MODEL                 # qwen3-rerank model name; 空=关 mix 回退 MMR (default empty)
RERANK_BASE_URL              # DashScope native text-rerank base (default dashscope api/v1; NOT compatible-mode)
RERANK_API_KEY               # DashScope key for rerank (required to enable mix rerank)
RERANK_MAX_DOCS              # max docs per rerank request, auto-batched beyond (default 500)
MAX_ENTITY_TOKENS            # mix KG entity-segment token budget (default 6000)
MAX_RELATION_TOKENS          # mix KG relation-segment token budget (default 8000)
MAX_TOTAL_TOKENS             # mix total context token budget (default 30000)
```

**Two-tier KB & graph reasoning (Wave 1+2):** these have no `.env` toggles today.
A notebook's `tier` (`base` | `personal`, default `personal`) is data on the notebook
row, set via the repository's `mark_notebook_base()`; tier-aware federation, the
base-authority ranking weight (base `1.20` vs personal `1.00`), and the base-wins
conflict rule in answers are always on once a notebook is marked `base`. The opt-in
graph-reasoning Ask mode (`mode="graph"`) bounds its multi-hop traversal with fixed
defaults `max_depth=3` and `max_fan_out=8` (read via `getattr` on settings, so a future
`GRAPH_MAX_DEPTH` / `GRAPH_MAX_FAN_OUT` env override would slot in without code changes).
Edge-trust scoring, the curator review queue, and personal→base promotion are likewise
behavior, not env-gated.

**User accounts:**

```text
SILICON_NOTEBOOK_ADMIN_PASSWORD   # admin login password (reset on every boot; default "admin")
SILICON_NOTEBOOK_AUTH_OPTIONAL    # true = no-token requests act as admin (local/testing only);
                                  # false (default) = login required for all requests
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

`.env.example` is the authoritative, complete list of every variable with its default
and an inline comment — the groups above highlight the common ones. Other documented
knobs include the optional dedicated reasoning LLM (`REASONING_LLM_BASE_URL` /
`REASONING_LLM_API_KEY` / `REASONING_LLM_MODEL`) and its guardrails (`REASONING_MAX_STEPS`,
`REASONING_MAX_SUBQUERIES`, `REASONING_TIMEOUT_SECONDS`, `REASONING_MAX_RETRIES`),
retrieval/grounding tuning (`PROC_MIN`, `EVIDENCE_TAU_LOW`,
`EVIDENCE_TAU_HIGH`), the opt-in debug log viewer (`DEBUG_LOGS_ENABLED`), and runtime
identity (`SILICON_NOTEBOOK_ENV`, `SILICON_NOTEBOOK_SINGLE_USER_EMAIL`,
`SILICON_NOTEBOOK_SINGLE_USER_NAME`).

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

MinerU output maps to structured `SourceElement`s: formulas become `formula` elements (LaTeX preserved), tables become `table` elements (HTML kept in metadata), and headings keep their level. The frontend renders these in the source detail view — formulas via KaTeX, tables from their HTML — so equations show typeset rather than as raw LaTeX. If MinerU is unreachable or errors, ingestion degrades to pypdf so uploads never block, while pipeline logs and the source `error_message` keep the fallback diagnostic; a PDF that parses to zero text (e.g. a scanned/image PDF) is flagged with a hint instead of looking like an empty success.

### Offline batch ingestion (directory → KG)

Ingest a directory of Markdown (and the occasional PDF) through the existing
pipeline, in two phases: `ingest` (no LLM, fast — chunk Q&A works immediately),
then `kg` (LLM extraction, separately resumable).

```bash
# 1) parse + chunk + embeddings (no LLM); creates a notebook named after the dir
PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir /path/to/md_dir

# 2) validate KG quality on a subset first (extract only the first 50 un-extracted sources)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 50

# 3) extract KG for the whole notebook (idempotent; skips already-extracted; resumable)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx

# or run both phases in one command
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "My KB"
```

Options: `--workers` (file concurrency), `--embed-conc` (embedding concurrency, throttles 429s), `--limit` (kg subset), `--dry-run` (scan & estimate only).

Prereqs: configure EMBED and KG_LLM in `.env` (otherwise embedding/KG steps skip or error). Duplicate files are skipped by content hash; progress is written to `<storage>/batch_ingest/<notebook>.jsonl` and a re-run resumes automatically.

## Current Limitations

- Retrieval uses SQLite keyword (CJK bi-gram) + float32 matrix semantic search with per-notebook cache. Memory is bounded (~hundreds of MB vs the old ~1.3 GB Python-list approach). BM25/FTS5 and pgvector are deferred for larger scale.
- Large-document ingestion is hardened: greedy-window KG extraction (cost scales linearly with document size), concurrent embedding with per-batch DB writes, and extraction-first pipeline. For very large corpora, adding `sqlite-vec` is a natural next step.
- Ask no longer performs synchronous embedding backfill or a full source-element scan; it uses available keyword/vector indexes and stays responsive while maintenance jobs run. Ask emits per-stage timing (`ask_stage` events).
- Unified KG rebuild is explicit and observable via `GET /notebooks/{id}/unified-kg/status`; ingesting a source marks the graph dirty instead of rebuilding synchronously, and opening the graph overlay no longer auto-rebuilds (refresh on demand).
- Cross-document concept merge uses deterministic alias normalization plus bounded top-k vector candidates (scales past thousands of concepts); optional LLM pre-review (`POST /notebooks/{id}/unified-kg/merges/review`) confirms/rejects high-confidence near-synonym merges in small batches.
- LLM-backed KG extraction requires configured `OPENAI_COMPAT_*`; offline smoke tests seed KG objects explicitly when retrieval/governance assertions are needed.
- Two-tier and deep reasoning are early: the graph-reasoning Ask mode (`mode="graph"`) is opt-in/experimental (the Ask panel toggle still drives the default `chunk`/`reasoning` paths). Marking a notebook `base`/`personal` (via `POST /notebooks/{id}/tier`), the edge-trust review queue, and promotion (personal→base) all now have dedicated front-end controls in the analysis toolbar; tier-aware federation and the base-wins conflict rule activate automatically once a notebook is marked `base`.
- Article Studio works from title/abstract text and linked source elements; first-class article full-text upload and richer relation scoring are next (Tier 3).
- PostgreSQL + pgvector are not required for the local beta and are deferred.
- The `off`-mode PDF fallback uses pypdf layout extraction (decent reading order, no new deps) — formulas, tables, and scanned/image PDFs still need MinerU; see "PDF parsing with MinerU".
- User memory remains manual opt-in only; no automatic memory behavior has been added.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks backend syntax (`py_compile`), a hermetic offline smoke path (`smoke_backend.py` — `mineru_mode=off`, no real LLM/embedding keys) covering upload/parse, structural Markdown parsing, KG windowing, concurrent embedding with per-batch DB writes, float32 vector matrix build and cache, hybrid retrieval (keyword/vector/None modes), multi-turn `ask`, status machine (`extracted` = green), article research, feedback, JSON fence cleanup, and restart persistence. Also runs frontend `node --test app/*.test.mjs` and Next.js `tsc --noEmit` when `frontend/node_modules` is present.

## Development Workflow

For every new feature development task, create a new git worktree by default, start a new feature branch inside that worktree, complete the work there, and open a PR from that branch. Do not switch branches directly in the main local checkout for feature work. If the current directory is already an isolated linked worktree, keep working there.

## Documentation Maintenance

When product behavior, setup, architecture, or development constraints change, update all of these files together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`
