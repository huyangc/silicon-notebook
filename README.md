# silicon-notebook

[中文说明](./README_zh.md)

`silicon-notebook` is a knowhow notebook platform for semiconductor engineering teams. It turns uploaded technical documents into a queryable knowledge graph of Concept / Claim / Formula / Procedure objects, with element-level evidence citations and grounded multi-turn Q&A.

## Current Scope

This repository targets a local real-team beta loop built around a KG-native pipeline:

- Python FastAPI backend; SQLite persistence at `.local/silicon_notebook.db`
- Next.js / React / TypeScript frontend under `frontend/`
- OpenAI-compatible LLM endpoint for KG extraction, grounded answers, and deep reports; embeddings configured independently via `EMBED_*` variables
- Deterministic fallbacks when no LLM/embedder is configured — the whole pipeline runs offline
- Clean start: a fresh database seeds only the local user; no demo notebook or synthetic sources
- Multipart source upload for PDF, Markdown, DOCX, PPTX, CSV, and XLSX (async through the shared KG job scheduler)
- **KG-native ingestion**: structured Markdown parse → greedy-window KG extraction (Concept / Claim / Formula / Procedure) with concurrent embedding → extraction-first status (`extracted` = KG ready, does not wait for embedding)
- PDF parsing via MinerU (formulas as LaTeX, tables, layout) when configured; pypdf text fallback locally or when MinerU is off
- Hybrid retrieval: CJK-aware bi-gram keyword + float32 matrix semantic search with per-notebook cache
- KG-native grounded Q&A: sentence-level `[k_i]` citations (rendered as compact numbered references, including model-emitted numeric groups like `[1, 2, 3]` when they map to known references), multi-turn conversations, 1-hop KG neighbour expansion, and a live, expandable one-line agent trace for reasoning mode
- **Typed query-time inference in reasoning mode:** the agent can call `follow_chain` to compose an evidence-backed two-hop `A→B→C` path into a transient `A→C` inference for `derived_from / kind_of / prerequisite_of / precedes / part_of`. Both direct hops remain independently citable relation evidence, rejected/ungrounded/scope-conflicting paths fail closed, the inferred conclusion is explicitly marked as reasoning, and no inferred edge is written back to the KG. The feature adds no migration, new index, or historical backfill; bounded samples use the existing source/target relation indexes and ambiguous high-degree paths are skipped.
- Two-tier knowledge base: each notebook has a `tier` (`base` | `personal`, default `personal`). Baseline `chunk` retrieval reads chunks from the active notebook only; optional KG overlay/PPR can add federated KG context and base-backed chunks, while `graph` and `reasoning` use federated KG paths. The exact-score `base` tie-break applies only to knowledge-object hits returned by `federated_retrieve()`: scores stay unchanged and a higher-scoring personal hit still wins. `federated_retrieve_relations()` remains score-only. Separately, when base and personal evidence contradict during answer synthesis, the answer defers to the base position and surfaces the discrepancy. Citations carry their tier (`AnswerAnchor.tier`) and Ask renders a `base`/`personal` badge per cited anchor.
- **User accounts**: self-service registration (username rule: a single letter + `00` + 6 digits, e.g. `a00123456`; stored lower-cased) + password login with opaque Bearer session tokens. Each notebook is owned by its creator; a user's library contains owned notebooks plus large shared notebooks they explicitly joined read-only. On first boot the built-in `admin` account is created (login `admin`, password from `SILICON_NOTEBOOK_ADMIN_PASSWORD`, local default `admin`; production/non-loopback startup requires changing it); the admin owns pre-existing notebooks and is the only user who can mark a notebook as the base KG. Base notebooks are hidden from regular users' lists but are still used as authoritative retrieval context at ask time. Set `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` for local/no-auth testing. The frontend shows a login/register gate on first load; the topbar displays the logged-in username and a logout button.
- **Share links**: owners can publish an opaque notebook link. Small notebooks are copied into the recipient's account; large notebooks are joined as read-only membership. Write access stays with the owner, and there is no live collaborative editing or change-password flow.
- Optional graph-reasoning Ask mode (`mode="graph"`, opt-in/experimental): a rustworkx in-memory graph built from `knowledge_relations` is traversed for bounded multi-hop derivation/support chains, with answer-time adversarial chain verification and a weakest-link `chain_trust` score (the default Ask mode stays `chunk`)
- Deep report (two-phase background job): a notebook-level "深度报告" action turns one question into a multi-section technical report. **Phase 1 (seconds)**: a STORM-style multi-perspective planner — grounded in a zero-LLM corpus scout (source titles + KG hits + chunk provenance, so the outline is not planned blind) — pre-writes an outline where each section carries its expert perspectives, cross-perspective tensions, and an evidence-sufficiency verdict (充足/薄弱/缺失 + gap note, from a zero-LLM retrieval probe + a Judge on the rewrite model); the user reviews/edits it in an outline editor before committing. **Phase 2 (minutes, on confirm)**: each section runs a full `reasoning` deep-dive independently (sections run in parallel, each with its own retrieval budget), each is drafted with a three-tier evidence discipline (`[k]` in-corpus citation / `（推断）` in-corpus inference / `【通识】` general-knowledge, marked and flagged unverified), then a summary pass adds an executive summary, references, and — only when a section lacked in-corpus support — a one-line limitation note. A depth control (five named levels 概览/标准/深入/详尽/穷尽, default 标准 — the per-section reflect-step budget, popover next to the generate button) trades thoroughness for latency; sections deep-dive in parallel up to `KG_JOB_CONCURRENCY`, and the UI shows live per-section progress (`section_status`). Runs as a cancellable background job; each report downloads as `.md`, or multi-select for a `reports.zip`
- Edge trust & curation: per-edge trust signals (evidence / corroboration / type-validity) plus a curator review queue; reviewer-rejected edges are excluded from graph reasoning
- Knowledge governance: browse by type via `/knowledge-types` + `/knowledge?type=...`, status lifecycle, duplicate detection & merge, conflict detection; `deprecated` objects excluded from retrieval and 1-hop expansion. Personal→base node promotion (propose → under review → approve/reject) with dedup-on-approve and a curator promotion queue
- Unified KG: cross-document concept clustering (`concept_clusters`), pending-merges review
- Object-level KG visualization: Concept / Claim / Formula / Procedure nodes with type-specific shapes, edge labels, multi-select filters, and a type-grouped side panel
- Notebook collection (grid/compact/list, edit/delete); clicking `＋ 新建` creates an `Untitled notebook` and enters it immediately — no dialog
- No Docker in the first version

PostgreSQL + pgvector remain the future production/team-beta direction; local development does not require them.

## Architecture Boundaries

- `SQLiteRepository` is the compatibility facade over a composed `RepositoryRuntime`. Application services do not assemble product SQL. Stores own product SQL and raw row selection; established application/query components may assemble domain/application projections such as `NotebookSummaryQuery.from_row`. Stores share one `SqliteDatabase` connection factory/write lock and one version-gated `SqliteMigrator`, while services retain sequencing and policy. Every facade operation is an explicit compatibility adapter or one of the source-checked one-hop delegates whose real target must match the ownership manifest. Consumers use executable, consumer-specific Protocols from `backend/app/repositories/ports.py`; dependencies point one way — facade → runtime → services → stores → SQLite — so a future PostgreSQL adapter replaces the store layer behind the same ports without touching callers. `sqlite_identity.py` and `sqlite_notebook_sharing.py` remain compatibility re-export shims, and the legacy request-context, `_COPY_CHUNK`, and `_remap_json_ids` exports stay importable.
- `RepositoryRuntime` owns or references composed runtime state; `REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner, and the runtime, report coordinator, and module compatibility functions share that same identity reference. Other mutable operational state (storage root, embedder, language caches, build sets, Ask cancellation registry, and artifact caches) is runtime-owned; replacing supported compatibility properties after composition updates every retained consumer. Synchronous Ask/report submission failures mark the already-created durable job/report failed, unregister the cancellation entry, and re-raise the submission error; successful worker ordering and the existing Ask transaction checkpoints remain unchanged.
- Databases created before the refactor keep loading unchanged. `scripts/verify_repository_snapshot.py` uses exact per-version migration and stable-seed manifests, percent-encodes SQLite URI paths, constructs the repository only on a temporary backup, and reports the retained backup path if cleanup fails without printing private rows. It guards the original database/WAL metadata plus SHM existence and size; for a live WAL attachment only SHM mtime is exempt because SQLite may rebuild it.

The refactor does not change the schema version present on its master baseline
(SCHEMA_VERSION = 10). The committed v9 compatibility fixture upgrades through
the existing v10 migration and remains readable.
- `frontend/app/page.tsx` is the notebook-workspace orchestrator, not the owner of every shared view model or panel. API/view types and constants live in `workspace-model.ts`, the answer/citation/reasoning-trace surface lives in `answer-panel.tsx`, and graph/answer type marks share `kg-type-mark.tsx`.
- Boundary regression tests prevent these responsibilities from being copied back into the monoliths. Future extraction should follow the same incremental pattern: preserve endpoints and user behavior, move one cohesive domain, then run the complete offline gate.

## Deployment

silicon-notebook runs as two processes — a FastAPI backend and a Next.js frontend — over
a local SQLite database. It requires **no GPU, no database server, and no local model
server**. LLM, embeddings, and rerank stay URL-based; MinerU separately supports remote
HTTP (`MINERU_MODE=http`), an isolated same-host subprocess (`MINERU_MODE=cli`), or the
pypdf fallback (`MINERU_MODE=off`). The pipeline runs offline with deterministic fallbacks
when no model service or MinerU parser is configured.

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

- **LLM** (KG extraction, answers, deep reports) — `OPENAI_COMPAT_BASE_URL` /
  `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_MODEL`; any OpenAI-compatible endpoint.
- **Embeddings** (semantic retrieval; otherwise keyword-only) — `EMBED_PROVIDER=dashscope`
  plus `EMBED_MODEL` / `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_DIM` (must equal the
  model's output dimension). Optional `EMBED_RUNTIME_DIM` (default `0` = off) truncates
  the similarity space to its first N dimensions + re-normalize (MRL) — cuts in-process
  matrix / ANN memory ~`EMBED_DIM/N`× while keeping the native vectors on disk as the
  source of truth. Switching it on/off requires rebuilding scale indexes; see
  [docs/runtime-dim-truncation-runbook.md](docs/runtime-dim-truncation-runbook.md). Never
  lower `EMBED_DIM` to shrink vectors — that discards every stored vector as wrong-dim.
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
ingestion background tasks and leaves uploads stuck at `extracting`.

All relative paths (database, storage, logs, `.env`) are **anchored to the repo root in
code**, regardless of which directory a script `cd`s into to launch a process — the
launch directory no longer matters. The backend's first log line prints the resolved
absolute paths (`paths: db=... storage=... log_dir=...`); check it if you're ever unsure
which `.local/` a given launch is actually using. The offline CLI (`scripts/batch_ingest.py`)
and both servers below all resolve to the same repo-root `.local/`.

**The launch scripts require a repo-root `.env`** (`npm run dev` / `npm run start`): if it
is missing they abort with an explicit error instead of silently booting on blank defaults,
and a renamed leftover such as `.env.local` is called out by name — note that Next.js's own
"Environments: .env.local" line only means the *frontend* read it; the backend reads `.env`
only. The backend applies the same check at startup (hard error only when a lookalike file
exists; a plainly missing `.env` logs a warning and boots, so fresh checkouts and containers
injecting real environment variables keep working). Set `ALLOW_NO_ENV_FILE=1` to skip the
check for env-var-only deployments.

```bash
# Development — backend (reload-friendly) and frontend together
npm run dev
```

```bash
# Production — builds the frontend, then serves both (single backend worker)
npm run start
```

`npm run start` runs `scripts/prod.sh`: `next build` + `next start` for the frontend,
`uvicorn --workers 1` for the backend, both logging to `.local/logs/`. Set `SKIP_BUILD=1`
to reuse an already-built `frontend/.next` (e.g. a prebuilt image). Override
`BACKEND_HOST` / `PORT` / `FRONTEND_PORT` to change bind address/ports. The backend
defaults to `127.0.0.1`; binding it to a non-loopback address requires a non-default
`SILICON_NOTEBOOK_ADMIN_PASSWORD` and fails fast otherwise.

> **One-time migration** — if you previously launched with `npm run dev` (or manually `cd
> backend && uvicorn ...`) on a version before path-anchoring landed, your data may be
> sitting under `backend/.local` instead of the repo-root `.local`. Either merge it in
> (`mv backend/.local/* .local/` from the repo root, checking for conflicts first) or keep
> the old location by pointing at it explicitly with absolute-path env vars
> (`SILICON_NOTEBOOK_STORAGE_DIR=/abs/path/storage`,
> `DATABASE_URL=sqlite:////abs/path/silicon_notebook.db` — note the four slashes for an
> absolute sqlite path) — absolute env values are always respected as-is and never
> re-anchored.

### 4 · Verify

```bash
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok","llm_configured":...}
bash scripts/check.sh                        # hermetic smoke + full pytest + frontend test/tsc/build
```

The backend writes structured JSONL logs under `.local/logs/` (`requests` / `events` /
`llm`); see [Observability](#observability) to follow an upload or diagnose a stuck source.

## Product Flow

The outer page is a notebook collection/library (KG-native pipeline):

1. Click `＋ 新建` — the app creates an `Untitled notebook` and enters it immediately (no dialog).
2. Upload PDF, Markdown, DOCX, PPTX, CSV, or XLSX sources (multipart).
3. Backend (async background job): structured Markdown parse → chunking + embeddings — chunk-native Q&A is ready as soon as the source finishes processing.
4. **KG extraction is conditional** (see [KG extraction trigger](#kg-extraction-trigger)): on ingest it runs only when the notebook already has a KG, or when `KG_AUTO_EXTRACT=true`. When it runs it uses a shared global extraction pool — window concurrency capped by `KG_EXTRACT_WORKERS` across all documents, document concurrency by `KG_JOB_CONCURRENCY` — and the new source is then incrementally fused into the unified KG.
5. Knowledge objects are stored in `knowledge_objects` + `knowledge_relations` with element-level evidence bindings.
6. Hybrid retrieval (bi-gram keyword + float32 matrix semantic) feeds KG-native Q&A: answers contain sentence-level `[k_i]` citations, support multi-turn conversations, and expand via 1-hop KG neighbours.
7. Unified KG aggregates concepts across documents; pending cross-document merges can be confirmed or rejected.

Inside a notebook:

- Header: the editable notebook title stays compact by itself; the notebook description is shown in the Ask welcome state when no conversation is active, and toolbar actions keep their labels intact across desktop widths.
- Left column: user-imported source files with live parse-status (green = `extracted` only; others shown in amber while processing), detail previews, and delete actions. Network source search is disabled for now.
- Main column: three tabs — **Ask**, **Knowledge**, and **Deep Report**. Ask provides grounded Q&A with clickable `[k_i]` sentence citations, three retrieval modes, multi-turn conversations, a live expandable reasoning trace, and feedback. Knowledge browses and governs dynamic object types. Deep Report exposes the two-stage report lifecycle, outline review, progress, export, cancellation, and deletion. In Ask, `Enter` submits, `Shift+Enter` keeps a newline, and while a model response is running the input/mode controls are locked while the send button becomes an interrupt control. A transport disconnect stops delivery to that client only; navigation, refresh, or transport loss leaves the detached Ask job running and it may persist its final answer. Clicking interrupt is a distinct cancellation action: the client calls `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel`, which sets the backend cancellation event so the worker/LLM path stops and does not save a cancelled final answer. The workspace remains two columns and has no fixed Studio sidebar.
- Knowledge Graph opens as a full-screen overlay: object-level KG nodes (Concept / Claim / Formula / Procedure) with type-specific shapes, edge relationship labels, multi-select type filters, and a type-grouped side panel that focuses the canvas on selection. The side panel renders source excerpts as structured evidence cards so long titles, locations, formulas, and mixed Chinese/English text wrap inside the panel.
- The Analysis menu itself contains only the promotion queue (admin), mark-base / mark-personal tier toggle (admin), and edge-review queue. Dashboard, Schema, and the full-screen Knowledge Graph are separate top-toolbar actions; no retired content-generation or derived-rule actions are exposed.

Reparse preserves the source row and original file: it replaces source elements/chunks and their embeddings, and removes extraction runs plus source-derived knowledge before rebuilding. Delete performs the same source-derived cleanup, then deletes the source row (cascading source-owned records) and the local file.

The notebook workspace hides the global collection top bar and keeps an engineering-console visual treatment.

## KG extraction trigger

Chunk-native retrieval is ready as soon as a source is parsed + embedded, so **KG extraction is opt-in per notebook** rather than run on every upload:

| Notebook state on upload | KG extraction | How it happens |
|---|---|---|
| No KG yet (fresh notebook) | **Not** auto-run | Build on demand: `POST /api/notebooks/{id}/kg/build` (UI: a notebook's **构建知识图谱 / Build KG** action; also surfaced when you pick a strict-reasoning mode on a KG-less notebook) |
| Already has a KG | **Auto-run** in the background for each new source | No manual trigger — keeps the KG complete; the new source is then incrementally fused into the unified cross-document KG |

The ingest-time decision is `KG_AUTO_EXTRACT or notebook-already-has-KG`:

- `KG_AUTO_EXTRACT` (default `false`) — when `true`, every upload extracts KG for **all** notebooks.
- Otherwise extraction runs on upload only if the notebook already contains KG objects.

So you **opt in once** (build the KG, or set `KG_AUTO_EXTRACT=true`); after that, new documents are auto-extracted and fused. Re-extract a whole notebook from scratch with `POST /api/notebooks/{id}/kg/rebuild`. For bulk/offline builds, see the **Offline batch ingestion** section.

## Retrieval modes (Ask)

`POST /ask` dispatches on `mode` — the registry `backend/app/services/ask_modes.py` is the single source of truth (default `chunk`). Federation is path-specific: baseline `chunk` is active-notebook-only; its optional KG overlay/PPR can add federated KG context and base-backed chunks; `graph` and `reasoning` use federated KG paths. Knowledge-object hits from `federated_retrieve()` keep tier-blind scores and use `base` only as the secondary key on an exact tie; `federated_retrieve_relations()` remains score-only. These ordering signals never feed grounding thresholds.

| Mode | Group | Needs KG | One-liner |
|------|-------|----------|-----------|
| **`chunk`** (default) | general | no | Chunk-native general Q&A: large recall → selection → long-context synthesis → citations bound to source chunks. |
| **`graph`** | strict | yes | Single-pass Personalized-PageRank propagation across the cross-document knowledge graph. |
| **`reasoning`** | strict | yes | Agentic, iterative plan → retrieve → reflect → answer (streams a live trace). |

**`chunk` — chunk-native, with optional chunk×graph mix.**
- *Baseline:* large chunk recall (`CHUNK_RECALL`) → MMR / multi-sub-query quota diversity selection (`CHUNK_MMR_K`) → long-context synthesis. The KG is not touched.
- *Mix* (active only when `CHUNK_KG_OVERLAY_ENABLED=true` **and** qwen3-rerank is configured **and** a KG is available): three sources are pooled — (a) vector chunks, (b) the KG local structure around the query seeds (entities + their 1-hop relations, retrieved once), (c) the source chunks behind those KG objects — round-robin merged, reranked by a qwen3 cross-encoder, then packed to a token budget (`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`). The answer cites chunks and KG items in one unified `[k]` map, and grounding spans chunk ∪ KG. When rerank is unconfigured or no KG exists, it falls back byte-for-byte to the baseline. (Faithful to LightRAG's `mix` mode.)

**`graph` — PPR over the cross-document KG.** Seeds via `federated_retrieve` (KG entities + their source chunks; optionally fused with relation-index hits when `RELATION_RETRIEVAL_ENABLED=true`) become the personalization vector for HippoRAG-style **Personalized PageRank** (`GRAPH_PPR_ENABLED`, on by default), which propagates relevance across documents through the shared knowledge graph; the top-ranked chunks feed a grounded answer whose `[k]` anchors point at KG objects/relations. With `GRAPH_PPR_ENABLED=false` it falls back to bounded BFS along reasoning edges.

**`reasoning` — agentic deep retrieval.** Delegates to `ReasoningRetriever`: it decomposes the question, retrieves (via the same PPR propagation as `graph`), reflects on sufficiency, and expands the graph / adds sub-queries until it can answer — emitting a `reasoning_trace` over the NDJSON stream (`/ask/stream`). For explicit derivations it may invoke `follow_chain`: two bounded adjacency rounds sample the existing source/target indexes, then deterministic type/status/review/evidence/`validity_scope` gates validate same-type transitive paths. The two stored relations become citable premises while the resulting `A→C` remains a labelled, query-only inference. A truncated high-degree endpoint is treated as unknown and suppresses inference rather than triggering an unbounded scan. Strict / KG-grounded.

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
- `POST /api/notebooks/{id}/ask` — grounded Q&A with `[k_i]` citations (`mode`: `chunk` default | `graph` | `reasoning`; federation follows the mode-specific boundaries above)
- `POST /api/notebooks/{id}/ask/stream` — NDJSON Ask progress stream (first a `started` event with `job_id`, then progress/final events). A transport disconnect stops delivery to that client only; the detached job keeps running and can persist its answer
- `GET /api/notebooks/{id}/ask/jobs/{job_id}` — read detached Ask job `status`, `trace`, and `answer_id`; on `done`, the frontend reloads the conversation to obtain the final `AskResponse`
- `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel` — explicit interrupt endpoint; sets the cancellation event and stops the worker before a cancelled final answer is saved
- `GET /api/notebooks/{id}/conversations`, `GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- Unified KG: `POST .../unified-kg/rebuild`, `GET .../unified-kg`, `GET .../unified-kg/pending-merges`, `POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`, `GET .../objects/{object_id}/context`
- `GET /api/object-schemas`, `POST /api/object-schemas`, `PATCH /api/object-schemas/{type}`, `DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`, `POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- Two-tier: `POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → returns the updated `NotebookSummary` (400 on bad tier, 404 on missing notebook). Sets the notebook's federation tier (base = authoritative reference KG, personal = default user notes).
- Edge trust & curation: `GET /api/notebooks/{id}/edge-review-queue`, `POST /api/notebooks/{id}/relations/{rel_id}/review`
- Governance / promotion: `POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`, `GET /api/promotion-queue`, `POST /api/promotion-queue/{candidate_id}/approve|reject`
- Deep report (two-phase): `POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`; runs **phase-1 planning** and stops at `status=outline_ready` (unless `auto_generate=true`, which chains straight through). `GET /api/notebooks/{id}/reports/{rid}` polls status + the enriched `outline` (per-section perspectives / tensions / sufficiency) + `content_md` + live `section_status`. `PATCH .../reports/{rid}/outline` body `{sections}` edits the draft outline (only while `outline_ready`; 422 if no valid section). `POST .../reports/{rid}/generate` body `{depth?}` launches **phase-2 generation** (only from `outline_ready`, else 409). Also `GET /reports` (list), `POST .../cancel`, `DELETE`, `POST .../reports/export` body `{report_ids}` → `reports.zip`. Sections deep-dive in parallel up to `KG_JOB_CONCURRENCY`.

The current persistence/API contract is the `reports` table and `/reports` APIs; retired content-studio storage and routes are not part of the current runtime.

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
KG_AUTO_EXTRACT             # extract KG on every upload for ALL notebooks (default false);
                            # when false, a new source is still auto-extracted if its
                            # notebook already has a KG (opt in once → auto-maintained)
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

**Core-aware autotune:** only knobs that are actually CPU-bound scale with the machine's
core count automatically — everything else stays fixed regardless of hardware:

```text
KG_CLUSTER_ANN_THREADS   # hnswlib ANN index-build threads for concept clustering/merge.
                         # 0 (default) = auto min(cpu_count, 32); the only retrieval/KG
                         # knob that is core-derived.
```

`scripts/dev.sh` / `scripts/prod.sh` also source `scripts/autotune.sh`, which sets
`OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` (and `NUMEXPR_NUM_THREADS`)
to `min(cpu_count, 8)` when they are not already set in the environment, and the offline
backfill CLI's process-pool worker default is `min(cpu_count, 32)`. Set `AUTOTUNE=0` to
disable the shell-level tuning entirely. Any of these values can still be overridden with
an explicit env var, which always wins over the auto-derived default. The resolved values
are printed once at backend startup (an `autotune: kg_cluster_ann_threads=... backfill_default_workers=...`
console line) so you can confirm what a given process actually picked up.

By deliberate contrast, `KG_EXTRACT_WORKERS` / `KG_JOB_CONCURRENCY` / `EMBED_CONCURRENCY`
are concurrency limits against a remote LLM/embedding endpoint, not the local machine —
they are **not** scaled by core count, and adding cores will not change them. To raise
throughput there, scale the model/embedding service instead.

Running multiple backend workers (`--workers N`) is a manual opt-in, not something
autotune enables for you — the default stays a single worker. Each additional worker
duplicates in-memory state (a large KG/ANN index can be GB-scale), multiplying memory
use roughly N×, and background KG jobs may land on any worker, which complicates status
tracking. Only turn it on if you have understood and accounted for both costs.

**Database:**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout in ms (default 30000)
DATABASE_URL            # SQLite path (default .local/silicon_notebook.db)
SILICON_NOTEBOOK_STORAGE_DIR   # uploaded file storage directory (default .local/storage)
```

**Retrieval:**

```text
RETRIEVAL_TOP_N         # reasoning/report synthesis evidence-budget floor (default 20)
REASONING_TOP_N_PER_QUERY  # adaptive budget: seats reserved per aspect/sub-query (default 3)
REASONING_TOP_N_CAP        # adaptive budget cap; comparison Qs scale by #aspects (default 36)
```

**Scalable-retrieval index:** notebooks large enough to be non-copyable (the same size
threshold used to gate notebook copy/sharing — bytes or chunk+node count over the
configured limit) get their scale index built or refreshed automatically, no manual
button/CLI step required: on source extraction, on KG rebuild, and as a fallback the first
time a query finds no index. By default the build is queued for a low-traffic off-peak
window rather than run immediately.

```text
SCALE_INDEX_AUTO_ENABLED   # auto-build/refresh the scale index for large notebooks (default true)
SCALE_INDEX_AUTO_WHEN      # "idle"=queue for the off-peak window (default) | "now"=build immediately
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
KG_COMMUNITY_SUMMARY_ENABLED # LLM community reports during rebuild (community layer; default false)
ANSWER_CONTEXT_BUDGET_CHARS  # answer-context assembly char budget (default 6000)
ANSWER_CONTEXT_MIN_ITEMS     # keep >= N items regardless of budget (default 3)
RETRIEVAL_RRF_ENABLED        # BM25(Okapi)+RRF ranking vs keyword+semantic fusion (default false)
RETRIEVAL_RRF_K              # reciprocal-rank-fusion k (default 60)
KG_QUERY_REFINE_ENABLED      # question-aware evidence refinement before answering (default true)
QUERY_REFINE_MAX_CHARS       # max chars of evidence fed to refinement (default 4000)
GLOBAL_MAX_COMMUNITIES       # accepted for compatibility; the retired `global` mode is a `chunk` alias, so this is currently unused (default 20)
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
REPORT_MAX_SECTIONS          # deep-report outline: max sections (default 6)
REPORT_SECTION_CHUNK_BUDGET  # deep-report: per-section chunk-context char budget (default 20000)
REPORT_SECTION_MAX_TOKENS    # deep-report: per-section drafting max_tokens (default 8192)
REPORT_ALLOW_PARAMETRIC      # deep-report: allow 【通识】/general-knowledge tier, marked & unverified (default true)
```

**Two-tier KB & graph reasoning (Wave 1+2):** these have no `.env` toggles today.
A notebook's `tier` (`base` | `personal`, default `personal`) is data on the notebook
row, set via the repository's `mark_notebook_base()`. Tier-aware federation leaves
retrieval scores unchanged: relevance is the primary ordering key, with `base` used
only as the secondary key for an exact score tie. The base-wins contradiction rule is
a separate answer-synthesis policy that remains active once a notebook is marked `base`. The opt-in
graph-reasoning Ask mode (`mode="graph"`) bounds its multi-hop traversal with fixed
defaults `max_depth=3` and `max_fan_out=8` (read via `getattr` on settings, so a future
`GRAPH_MAX_DEPTH` / `GRAPH_MAX_FAN_OUT` env override would slot in without code changes).
Edge-trust scoring, the curator review queue, and personal→base promotion are likewise
behavior, not env-gated.

**User accounts:**

```text
SILICON_NOTEBOOK_ADMIN_PASSWORD   # admin login password (local default "admin"; production/non-loopback
                                  # startup requires a non-default value)
SILICON_NOTEBOOK_AUTH_OPTIONAL    # true = no-token requests act as admin (local/testing only);
                                  # false (default) = login required for all requests
AUTH_SESSION_TOUCH_INTERVAL_SECONDS # sliding-session DB write interval (default 300)
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

For deployment slow-path triage, run `python3 scripts/diag_slow.py` on the host that owns
`.local/`. Besides request/event/LLM summaries, it prints a strict-reasoning / PPR audit
from DB aggregates and scale-index manifests so large libraries can be checked for
indexed-core coverage, chunk/relation ANN availability, delta policy, and cross-base paths
that may still touch full active vectors.

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

**URL sources ("Add link") prefer local MinerU.** A pasted public PDF URL is parsed by the local MinerU service whenever one is configured (`MINERU_MODE=http`/`cli`): the backend downloads the PDF and runs it through the same local-MinerU→pypdf path as file uploads. For SSRF protection, the downloader validates the initial target and every redirect and rejects localhost, private, link-local, and reserved addresses; import internal documents through file upload instead. The `MINERU_API_TOKEN` cloud (mineru.net) path is used only as a fallback when no local MinerU is configured — and once local is in use it is never silently called. Adding a URL requires *either* a local MinerU or the cloud token.

MinerU output maps to structured `SourceElement`s: formulas become `formula` elements (LaTeX preserved), tables become `table` elements (HTML kept in metadata), and headings keep their level. The frontend renders these in the source detail view — formulas via KaTeX, tables from their HTML — so equations show typeset rather than as raw LaTeX. If MinerU is unreachable or errors, ingestion degrades to pypdf so uploads never block, while pipeline logs and the source `error_message` keep the fallback diagnostic; a PDF that parses to zero text (e.g. a scanned/image PDF) is flagged with a hint instead of looking like an empty success.

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

# or run both phases in one command
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "My KB"

# build the scalable-retrieval index for a base-tier notebook (offline; re-run after rebuilding a static base)
PYTHONPATH=backend python scripts/batch_ingest.py index --notebook-id nb-xxxx

# backfill any missing chunk + node vectors for a notebook (idempotent; requires EMBED configured)
PYTHONPATH=backend python scripts/batch_ingest.py embed --notebook-id nb-xxxx

# one-time storage migration: convert legacy JSON-text vectors to float32 BLOB (idempotent, no EMBED needed)
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --all-notebooks --workers 8

# proactively backfill the source-deletion reverse index (idempotent, no EMBED needed)
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --all-notebooks
```

The `embed` subcommand re-fills only the chunk and KG-node vectors that are *missing* (e.g. after a 429-throttled run left gaps). It requires `--notebook-id` and a configured EMBED endpoint — being a vector-backfill command, it ignores `--allow-no-embed` and errors out if EMBED is unconfigured.

The `vectors-to-blob` subcommand is a one-time storage migration: embedding vectors used to be stored as JSON text in SQLite, which means loading hundreds of thousands of rows into a matrix (index builds, retrieval cold start) spends most of its time in `json.loads`. New writes are now stored as raw float32 BLOBs (`np.frombuffer` reinterprets them with zero parsing), and every reader already accepts either format — so this command is optional but recommended after upgrading: it re-encodes any pre-existing JSON-text rows across all four embeddings tables (`chunk_embeddings`, `knowledge_embeddings`, `element_embeddings`, `relation_embeddings`) in place, in batched transactions (5,000 rows/commit) with progress printed per table. It does **not** compute new vectors (so it needs no EMBED configuration) and is idempotent/restartable — re-running it converts nothing further, since it only selects rows SQLite still types as `text`. Use `--notebook-id` to scope it to one library or `--all-notebooks` to convert every notebook in the database. The `json.loads`/re-encode step (the single-core bottleneck at millions-of-rows scale) is parallelized across `--workers` processes (default `min(8, cpu_count())`; `--workers 1` uses no process pool at all) — the main process still owns every DB read/write, so SQLite stays single-writer. If the worker pool crashes it falls back to a serial pass automatically rather than losing the run.

The `backfill-source-index` subcommand proactively populates `knowledge_object_sources`, a reverse-lookup table (`object_id, source_id`) used when a source is deleted or reparsed to find which KG objects reference it. Without it, that lookup has to scan every object's evidence JSON in the notebook (`json.loads` over the whole table) just to find matches for one source — expensive at hundreds of thousands of objects. The table is normally populated lazily (the first source delete/reparse on an un-migrated notebook pays the scan once, populates the table while it's already reading every row, and marks the notebook so every subsequent operation is an indexed lookup instead) — this command lets you pay that cost up front, in bounded-memory batches with progress printed, instead of on a user-facing delete. It needs no EMBED configuration and is idempotent/restartable (each run clears and rebuilds the notebook's rows from the current evidence, then re-marks it). Use `--notebook-id` to scope it to one library or `--all-notebooks` to cover every notebook in the database. If you ever suspect a notebook's reverse index has drifted from its actual evidence (e.g. after an abnormal interruption), re-running this command is the remediation — it always rebuilds from the current evidence.

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

# gold mode (needs a configured EMBED endpoint; embeds each question once at native dim):
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

**Concurrency tuning.** Three knobs control throughput (and 429 pressure):

- `--workers` — in the `all` phase, how many *documents* are extracted at once (overrides `KG_JOB_CONCURRENCY`). In `ingest` it is the file-parse concurrency. In `vectors-to-blob` it's the number of processes used to parallelize `json.loads`/re-encode (default `min(8, cpu_count())`; `1` disables the process pool entirely).
- `--embed-conc` — embedding concurrency (overrides `EMBED_CONCURRENCY`). In `all`, chunk embedding runs in the background of each document's pipeline.
- `KG_EXTRACT_WORKERS` (`.env`, default 16) — the global cap on concurrent KG-extraction LLM windows, shared across all documents (intra- and inter-document).
- `--pool-report-interval` — in the `all` and `kg` phases, print a live pool-utilization line every N seconds (default 15; `0` disables it). It reports the KG-LLM (extraction-window) pool vs the embedding threads side by side — e.g. `[pool 17:52:33] KG-LLM(window) 14/16 · 源(job) 8/8 · embed 6bg+20pool · 源完成 5/40` — so you can confirm the embedding model and the KG-LLM are saturating a shared-compute model service *at the same time*.

In the `all` phase, peak embedding concurrency is roughly `--workers × --embed-conc`, so raise both cautiously to avoid provider 429s. If a throttled run leaves vectors missing, repair them later with the `embed` subcommand.

Options: `--owner` (notebook owner username, case-insensitive; defaults to the admin user), `--workers` (documents extracted concurrently in `all` = `KG_JOB_CONCURRENCY`, file concurrency in `ingest`; in `vectors-to-blob` it's the parse/encode process-pool size, default `min(8, cpu_count())`, `1` = no pool), `--embed-conc` (embedding concurrency = `EMBED_CONCURRENCY`; throttles 429s), `--limit` (kg extraction subset — clustering still covers the whole notebook), `--no-rebuild` / `--rebuild-only` (split extraction from the final clustering for batched large builds), `--fresh` (clears the rebuild checkpoint to force a full re-run of merge-review + concept-description adjudication; use when you changed the KG model/thresholds but the data is unchanged — implies a forced rebuild, and also applies to the `all` phase's final clustering), `--allow-no-embed` (explicitly allow running without embeddings when EMBED is unconfigured; refused by default — never silent; ignored by the `embed` subcommand), `--pool-report-interval` (seconds between live pool-utilization self-reports in `all`/`kg`, showing KG-LLM vs embed concurrency to verify multi-model saturation; default 15, `0` off), `--all-notebooks` (`vectors-to-blob` / `backfill-source-index` only: act on every notebook instead of one), `--dry-run` (scan & estimate only). The `embed` subcommand backfills only missing chunk + node vectors and requires `--notebook-id`. The `vectors-to-blob` subcommand migrates legacy JSON-text vectors to BLOB and requires `--notebook-id` or `--all-notebooks`. The `backfill-source-index` subcommand proactively builds the source-deletion reverse index and requires `--notebook-id` or `--all-notebooks`.

Prereqs: configure EMBED and `KG_LLM` (KG extraction falls back to the global `OPENAI_COMPAT_*`) in `.env`. With EMBED unconfigured the CLI **refuses to run by default** — pass `--allow-no-embed` to import without vectors (chunk/KG vectors are then skipped), never silently; KG extraction errors if no LLM is reachable. Duplicate files are skipped by content hash; progress is written to `<storage>/batch_ingest/<notebook>.jsonl` and a re-run resumes automatically.

### Retrieval replay diff (`scripts/replay_retrieval.py`)

The acceptance tool for proving "retrieval quality is unchanged" across a performance-optimization change: run a fixed question set through the reasoning retrieval primitives (`federated_retrieve` + `ppr_retrieve`), **without calling any answer LLM**, and record the hit id/score sequences as JSON. Two runs' outputs can then be diffed question-by-question.

```bash
# record a run (needs a configured EMBED endpoint for real query vectors; reads retrieval primitives only, no LLM required)
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

Exit codes are the verdict — safe to wire directly into CI or a script gate: `0` success (recording) or all questions match (`--compare`); `1` `--compare` found a mismatch (runs differ); `2` a precondition failed before any comparison happened (EMBED unconfigured, notebook not found, or owner not found) — the CLI **errors out immediately** rather than silently producing a misleading "zero recall" comparison from zero vectors.

## Current Limitations

- Retrieval uses SQLite keyword (CJK bi-gram) + float32 matrix semantic search with per-notebook cache. Memory is bounded (~hundreds of MB vs the old ~1.3 GB Python-list approach). BM25/FTS5 and pgvector are deferred for larger scale.
- Large-document ingestion is hardened: greedy-window KG extraction (cost scales linearly with document size), concurrent embedding with per-batch DB writes, and extraction-first pipeline. For very large corpora, adding `sqlite-vec` is a natural next step.
- Ask no longer performs synchronous embedding backfill or a full source-element scan; it uses available keyword/vector indexes and stays responsive while maintenance jobs run. Ask emits per-stage timing (`ask_stage` events).
- Unified KG rebuild is explicit and observable via `GET /notebooks/{id}/unified-kg/status`; ingesting a source marks the graph dirty instead of rebuilding synchronously, and opening the graph overlay no longer auto-rebuilds (refresh on demand).
- Cross-document concept merge uses deterministic alias normalization plus bounded top-k vector candidates (scales past thousands of concepts); optional LLM pre-review (`POST /notebooks/{id}/unified-kg/merges/review`) confirms/rejects high-confidence near-synonym merges in small batches.
- LLM-backed KG extraction requires configured `OPENAI_COMPAT_*`; offline smoke tests seed KG objects explicitly when retrieval/governance assertions are needed.
- Two-tier and deep reasoning are early: the graph-reasoning Ask mode (`mode="graph"`) is opt-in/experimental (the Ask panel toggle still drives the default `chunk`/`reasoning` paths). Marking a notebook `base`/`personal` (via `POST /notebooks/{id}/tier`), the edge-trust review queue, and promotion (personal→base) all now have dedicated front-end controls in the analysis toolbar; tier-aware federation and the base-wins conflict rule activate automatically once a notebook is marked `base`.
- Notebook sharing is link-based copy/read-only membership, not live collaborative editing; owners retain write authority.
- PostgreSQL + pgvector are not required for the local beta and are deferred. Until a PostgreSQL repository exists, non-`sqlite:///` `DATABASE_URL` values fail fast instead of silently falling back to a local database.
- The `off`-mode PDF fallback uses pypdf layout extraction (decent reading order, no new deps) — formulas, tables, and scanned/image PDFs still need MinerU; see "PDF parsing with MinerU".
- User memory remains manual opt-in only; no automatic memory behavior has been added.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks backend syntax (`py_compile`), a hermetic offline smoke path (`smoke_backend.py` — the repository `.env` is disabled and no real LLM/embedding key is inherited), the complete backend pytest suite, every recursively discovered frontend `*.test.mjs`, Next.js `tsc --noEmit`, and the production frontend build. Missing `frontend/node_modules` is a hard failure rather than a silent skip.

## Development Workflow

For every new feature development task, create a new git worktree by default, start a new feature branch inside that worktree, complete the work there, and open a PR from that branch. Do not switch branches directly in the main local checkout for feature work. If the current directory is already an isolated linked worktree, keep working there.

For approved multi-step implementation plans, use subagent-driven development by default: assign each task to a fresh implementation subagent and require task-scoped specification and code-quality review before moving on. Research, design, status, and review-only work does not require a worktree or subagents.

## Documentation Maintenance

When product behavior, setup, architecture, or development constraints change, update all of these files together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`
