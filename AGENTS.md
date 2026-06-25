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
- Keep the notebook header compact: do not render the notebook description under the title; show that description in the Ask welcome state when no conversation is active. Top toolbar actions must keep their labels intact across desktop widths.
- Left column: user-imported source files only.
  - Show how many sources are in the current notebook.
  - Keep source cards compact and readable for long mixed Chinese/English titles and summaries.
  - Source cards should open a source detail preview with element-level parsed text and expose a delete action.
  - Source detail element text should wrap within the modal width, including long Markdown paths, LaTeX fragments, and mixed Chinese/English text; keep horizontal scrolling local to tables/formulas rather than the entire detail panel.
  - Do not enable web/network source search yet; keep it as a disabled future affordance only.
- Center column: source-grounded knowhow tools, exposed as tabs.
  - This is the main interaction area, exposed as two tabs: **Ask** (free, KG-grounded Q&A with per-sentence `[k_i]` citations + multi-turn conversation) and the **Knowledge browser** (browse/govern the extracted objects by type). The earlier Scenario query / Case search / Checklist tools were removed when the KG-native model (concept/claim/formula/procedure) replaced the rule/case/checklist types.
  - Ask is tier-aware: it federates across the active personal notebook plus any `base` notebook, tags citations with their tier (rendered as a `base`/`personal` badge on each cited anchor in the Ask panel), and defers to base on contradiction. The `mode` field selects the retrieval/answer path: `fast` (default), `reasoning`, `global`, and the opt-in/experimental `graph` (rustworkx multi-hop with `chain_trust`). The Ask panel toggle currently drives `fast`↔`reasoning`; `graph`/`global` are exercised via the API. Do not regress the default to graph mode or oversell it as on-by-default.
  - Ask conversation history should use a compact session context bar and an expandable session manager instead of permanently splitting the already constrained center panel.
  - Reasoning mode should surface backend agent progress in the Ask panel while the request is running. Use `POST /notebooks/{id}/ask/stream` for live NDJSON `progress` events, render the trace as a streaming one-line summary by default, provide click-to-expand details, and keep the final `reasoning_trace` visible on the answer; do not regress to a bare static "thinking" placeholder for reasoning requests.
  - Answers must stay evidence-grounded, render Markdown/code/formula/table content cleanly, use compact numbered citations (`[1]`, `[2]`, ...), expand citation details inside the answer panel (not in an overflowing floating popover), and support lightweight 👍/👎/copy actions. Do not flatten all related knowledge under each answer; route deeper exploration from the citation area into the Knowledge Graph.
  - Ask input ergonomics: `Enter` submits, `Shift+Enter` inserts a newline. While a model response is running, lock the input and mode controls, prevent duplicate sends, and turn the send button into an interrupt control that aborts the in-flight stream and restores the draft question for editing. This is a full-stack cancellation path: frontend abort/client disconnect on `/ask/stream` must set a backend cancellation event so the in-flight Ask worker / LLM path stops and does not save a cancelled final answer.
  - Prompt chips should run useful first-version questions derived from the notebook's imported source titles/summaries when available; the menu should expose a real clear/reset action.
- Knowledge Graph opens as a full-screen workspace overlay.
  - On open: fetch the current graph data and `GET /unified-kg/status`; show a refresh button when the graph is dirty. Do not trigger an automatic rebuild on open.
  - Use the object-level unified graph so Concept / Claim / Formula / Procedure nodes can appear together; do not fall back to a concept-only graph when object-level relationships exist.
  - The main canvas should show node names, type-specific node marks, and relationship labels on edges.
  - Provide multi-select type filters for dense graphs. Selecting a node from either the canvas or the overview should focus/highlight that node and update the selected-node relation/source details.
  - Keep node type color/shape marks consistent between the canvas, node overview, selected-node detail, and related-node sections. In the detail panel, label evidence/source excerpts as `出处`, render them as structured evidence cards with separate source metadata and wrapped excerpt text, hide raw `section_path`, and show concept-mounted objects as `相关节点` grouped by type.
  - The side panel should provide a type-grouped node overview (Concept, Claim, Formula, Procedure, plus future types) and selected-node relation/evidence details.
- Do not show a fixed right-column Studio sidebar in the primary notebook workspace while its tools are still early.
  - Keep Ask / Knowledge as the main workspace surface and let the Ask panel use the freed width.
  - Studio-style actions (Mind Map, New Article, Infographic, derived-rule review) should live in the top analysis toolbar or dialogs until they are mature enough for a dedicated workspace surface.
  - Article research drives the Mind Map / Infographic output; created articles must still be listed with delete actions wherever the article surface is exposed, and generated outputs should be visible in dialogs or a future dedicated surface, not hidden state.
- The candidate Review Queue was removed: the KG extractor writes approved knowledge objects directly (no candidate staging). Knowledge governance is dedupe/merge over the objects in the Knowledge browser.

## MVP Scope

The MVP is a real-team local beta, not a throwaway mockup.

Confirmed scope:

- Mixed Chinese/English source material.
- First supported file types: PDF, Markdown, DOCX, PPTX.
- Real multipart upload and local storage of original files.
- Parser-generated `SourceElement` records with element-level citation granularity.
- Source summary after parsing. Use the OpenAI-compatible client when configured; otherwise use deterministic fallback.
- Notebook-internal search over notebook metadata, source metadata, source element text, and article summaries.
- LLM-backed KG extraction for Concept / Claim / Formula / Procedure objects with evidence binding is implemented. Legacy candidate tables/endpoints still exist for governance compatibility, but the current no-LLM offline extraction path records `error_message='no-llm'` and does not synthesize rule/method/risk candidates.
- Ask, Scenario query, Case search, Checklist, Rule browser, and Article research are real and data-driven (hybrid keyword + embedding retrieval, citation validation, deterministic fallback offline). They are no longer demo-backed.
- Knowledge governance: `knowledge_objects` has a status lifecycle (`reviewed/approved/deprecated/conflict/project_specific`) plus `owner`/`last_reviewed`. Only USABLE statuses (approved/reviewed/project_specific/conflict) feed answers; `deprecated` is excluded, including during Ask's one-hop KG neighbour expansion. Browse dynamically via `GET /notebooks/{id}/knowledge-types` + `GET /notebooks/{id}/knowledge?type=...`, edit via `PATCH /notebooks/{id}/knowledge/{knowledge_id}`, dedupe via `GET .../duplicates` + `POST /notebooks/{id}/knowledge/{knowledge_id}/merge`.
- Two-tier KB & tier-aware retrieval (Wave 1+2): `notebooks.tier` is `base` | `personal` (default `personal`), set via `SQLiteRepository.mark_notebook_base()` / `set_notebook_personal()` and the `POST /notebooks/{id}/tier` route (body `{tier}`; returns the updated `NotebookSummary`, 400 on bad tier, 404 on missing notebook). The notebook actions menu surfaces a "设为基准库 / 取消基准库" toggle that calls it. `federated_retrieve` (`sqlite_repository.py`) gathers candidates across `base ∪ active personal`, stamps each hit/anchor with its tier (`AnswerAnchor.tier`), and applies a base-authority weight (`tier_weight`/`_TIER_WEIGHT` in `retrieval.py`, base `1.20` > personal `1.00`) — applied OUTSIDE `_fuse` so the `[0,1]`/tau scoring invariant and dual-index best-of stay intact. `answer_prompt` (`prompts.py`) carries the base-wins rule: on a personal↔base contradiction, defer to base and note the discrepancy.
- Graph-reasoning Ask mode (`mode="graph"`, opt-in/experimental — default stays `fast`): `ask_graph` builds a rustworkx in-memory graph from `knowledge_relations` (`app/services/kg/graph_reason.py`), traverses bounded multi-hop chains (`max_depth=3`, `max_fan_out=8` defaults), runs answer-time adversarial chain verification, and reports a weakest-link, authority-weighted `chain_trust` (personal hop factor `0.85`). The graph is cached with the same VectorCache version-key pattern as the vector matrix.
- Edge trust & curation (Track E): per-edge trust signals (evidence/corroboration/type-validity) plus a curator review queue (`review_queue`/`set_edge_review`); reviewer-rejected edges are excluded from graph reasoning. Endpoints: `GET /notebooks/{id}/edge-review-queue`, `POST /notebooks/{id}/relations/{rel_id}/review`. Front-end exposes an edge-review queue modal (from the analysis actions menu) that lists relations ranked by review priority with confirm / reject actions, mirroring the promotion-queue modal pattern.
- Governance / promotion (Track F): owner-triggered personal→base node promotion state machine (`propose_promotion`/`list_promotion_queue`/`approve_promotion`/`reject_promotion`), dedup-on-approve reusing the merge clustering. Backend `POST /notebooks/{id}/knowledge/{knowledge_id}/promote`, `GET /promotion-queue`, `POST /promotion-queue/{id}/approve|reject`; front-end exposes a promotion queue modal + per-object "propose promotion" affordance (personal-tier objects only).
- Article research persists `article_claims` with relation metadata and draft `derived_rule_candidates`; feedback supports `useful` / `not_useful` plus an optional comment.
- User-created notebooks, imported sources, and Studio articles must expose delete actions. Source deletion removes parsed elements, extraction runs, embeddings, stale source-derived knowledge, and the stored local file; article deletion removes article claims and derived-rule candidates.
- Single-user mode for now, but keep user/system structure ready for future expansion.
- User memory must be manual opt-in. Do not add automatic memory behavior.
- No demo/seed notebook is created. A fresh database seeds only the local user; the notebook collection starts empty and is populated entirely by real imported sources. Do not reintroduce synthetic seed notebooks/sources/articles. New-notebook example prompts/placeholders must derive from the chosen template / the notebook's domain, never from a hardcoded demo scenario.
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
- KG-native tables are live: `knowledge_objects` (object types `concept/claim/formula/procedure`), `knowledge_relations`, `knowledge_embeddings`, `element_embeddings`, `concept_clusters`, `extraction_runs`, `answers`, `conversations`, `feedback`; plus still-live legacy `article_claims`/`derived_rule_candidates`. `notebooks` carries a `tier` column (`base`/`personal`, default `personal`, added by idempotent `ALTER TABLE` migration); Wave 2 governance adds a promotion-queue table (Track F). Embedding vectors are stored as JSON; at query time `ask()` streams them into per-notebook L2-normalized **float32 numpy matrices** (`vector_index`) cached by `vector_cache` (version-keyed) — similarity is one matmul with bounded memory (no pgvector locally; sqlite-vec is the future scale path). For `mode="graph"`, `knowledge_relations` is loaded into a rustworkx `PyDiGraph` (`graph_reason.build_rx_graph` / `_federated_rx_graph`), cached with the same version-key pattern; edges are tier-stamped from the owning notebook's `tier`.
- Upload runs `process_source` asynchronously via FastAPI `BackgroundTasks` (status `queued→parsing→parsed→extracting→extracted`). After parsing, **element embedding runs in a background daemon thread concurrently with foreground KG extraction**; `extracted` (UI green) is gated on extraction completion only. SQLite uses WAL + `busy_timeout` so the concurrent writers do not lock. The repository accepts a `scheduler` callback so scripts/tests can run it synchronously.
- Ask must stay off heavy maintenance work: no synchronous whole-notebook embedding backfill, no synchronous unified-KG rebuild, and no full source-element scan for citation validation.
- Streaming Ask (`/ask/stream`) must remain progress-first: emit an immediate `start` progress event, stream each agent trace step as it is recorded, then emit one final `AskResponse`. Client disconnect / abort must propagate to backend cancellation so the worker stops before saving a cancelled response. The normal `/ask` endpoint remains the non-streaming compatibility path.
- Unified-KG rebuild is explicit/observable. Opening the Knowledge Graph overlay fetches the current graph + `/unified-kg/status` and offers refresh when dirty; it must not block on rebuild.
- Cross-document concept-merge candidates must be bounded and reviewable. LLM merge review operates on small pending candidate batches, never the entire concept set at once.
- Re-running parse/extraction for a source invalidates stale source-derived candidates and approved knowledge before writing the new extraction result.
- Deleting a source uses the same stale source-derived cleanup boundary, removes its local file, and clears any article research artifacts that depended on that source. Deleting an article cascades its claims and derived-rule candidates.

## Python Environment

Run all backend work with an isolated Python environment that has
`backend/requirements.txt` installed. Activate it — or point `PYTHON_BIN` at its
interpreter — so the helper scripts (`scripts/dev.sh`, `scripts/backend.sh`) and the
root `package.json` use it; they fall back to `python3` when `PYTHON_BIN` is unset.

Keep **machine-specific** details — absolute interpreter paths, which local port a
service happens to occupy, and similar per-developer facts — in that machine's local
config / memory, **not** in committed files (this file, the READMEs, etc.). Committed
docs describe the generic procedure only; `README.md` → "Deployment" is the canonical
setup runbook.

## Backend Commands

Run backend commands with the active environment's interpreter:

```bash
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

For dependency checks or installs, use the same interpreter:

```bash
python -m pip install -r backend/requirements.txt
```

Set `PYTHON_BIN` for the helper scripts that support it:

```bash
PYTHON_BIN=/path/to/python bash scripts/check.sh
```

## Frontend/UI

`scripts/dev.sh` starts the backend and the Next.js frontend from `frontend/`. It requires `frontend/node_modules`; run `npm install` in `frontend/` first if missing. The normal mainline is:

```bash
cd frontend
npm run dev
```

## No Docker In First Version

Do not add Docker or Docker Compose as the default first-version workflow.

Local beta should run directly on a local Python environment and the local Next.js frontend.

Docker can be introduced later only when the user asks for deployment packaging or when the project moves beyond the local beta workflow.

## Dependency Policy

- Treat the project's isolated Python environment (with `backend/requirements.txt` installed) as canonical; activate it or set `PYTHON_BIN`.
- Keep backend dependencies compatible with Python 3.11+.
- Default backend requirements should not require PostgreSQL, pgvector, or SQLAlchemy while SQLite is the local baseline.
- Keep dependency setup reproducible from `backend/requirements.txt`.
- Ask for approval before installing new packages when network or environment mutation is required.

## Current Backend Baseline

The backend has been exercised on:

```text
Python 3.13.11
fastapi
uvicorn
pydantic
openai
python-multipart
python-docx
pypdf
markdown-it-py   # 结构化 Markdown 解析
numpy            # float32 向量矩阵检索
openpyxl         # XLSX 解析
```

If any dependency is missing, install `backend/requirements.txt` into the active environment.

## LLM Configuration

LLM access must be OpenAI-compatible and configurable through environment variables:

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS
```

Embeddings are configured separately via the `EMBED_*` vars (`EMBED_PROVIDER` ""=off / dashscope, plus required `EMBED_MODEL`, `EMBED_BASE_URL`, `EMBED_API_KEY`, and matching `EMBED_DIM`) and accessed through the `Embedder` abstraction (`app/services/embedding.py`), not `LLMClient`.

**Principle — model services are URL-based only.** Every model the product depends on (chat LLM, embeddings, and any future reranker/parser model) is reached over an HTTP/OpenAI-compatible **URL endpoint** (`*_BASE_URL` + `*_API_KEY` + `*_MODEL`). This project does **not** start or host local model servers (no in-process model loading like sentence-transformers, no spawning a local inference server) to perform tasks. Prefer adding a configurable endpoint over bundling a model. (The former `LocalBGEEmbedder` was removed for this reason; re-add behind this same URL principle only if explicitly requested.)

Business logic should call a provider adapter/client, not hard-code a specific model vendor.

When no LLM key is configured, endpoints may return deterministic fallback data so the local beta remains usable.

## Logging / Observability

Structured logs go to `.local/logs/*.jsonl` (gitignored) plus brief console lines, via a single `EventLogger` (`backend/app/core/event_logging.py`). Add observability at the existing chokepoints, not at each call site:

- `requests.jsonl` — HTTP middleware in `app/main.py` (method/path/status/latency/`request_id`; slow calls flagged via `SLOW_REQUEST_MS`; `X-Request-Id` response header).
- `events.jsonl` — source pipeline in `SQLiteRepository.process_source` / `_set_source_status` (per-stage timings + status transitions + failure stack).
- `llm.jsonl` — `LLMInteractionLogger` wrapping `OpenAICompatibleClient` (`app/core/llm.py`); chat detailed, embeddings summarized, errors recorded.

Rules: reuse `EventLogger` for any new structured log (it handles JSONL append + console + never raising); never log raw embedding vectors; chat prompt/response are truncated to `LLM_LOG_MAX_CHARS`. The browser/API debug log viewer (`/dev/logs`, `/api/debug/logs/...`) is opt-in because full LLM records may contain prompt/response text from private sources; enable only with `DEBUG_LOGS_ENABLED=true`. Config env vars: `LLM_LOG_ENABLED`, `LLM_LOG_PATH`, `LLM_LOG_MAX_CHARS`, `EVENT_LOG_ENABLED`, `EVENT_LOG_DIR`, `SLOW_REQUEST_MS`, `DEBUG_LOGS_ENABLED` — keep `.env.example` aligned.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks:

- Backend Python syntax.
- SQLite initialization and persistence smoke path.
- Markdown, DOCX, PPTX, and PDF upload/parse smoke path (sync and async-scheduler paths).
- KG extraction boundary (`no-llm` offline), explicit KG/rule knowledge storage, delete → ask → feedback (with comment) → conversation APIs → article research smoke path, plus retrieval scoring (keyword/vector, CJK tokenization, hybrid fusion, scenario boost, payload-level embeddings), derived-rule persistence, and stale-source knowledge invalidation assertions. Fresh DB must still have no demo notebook.
- Logging: LLM interaction log, generic event log (parseable/disable/never-raise), and pipeline stage events + `error_message` regression.
- Source summary fallback and notebook-internal search.
- Frontend `node --test app/*.test.mjs` and Next.js TypeScript when `frontend/node_modules` exists.

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

- For every new feature development task, create a new git worktree by default and do the work there on a new feature branch. Do not switch branches directly in the main local checkout for feature work. If the current directory is already an isolated linked worktree, continue there; otherwise create a worktree first, then branch, develop, verify, and open the PR from that branch.
- Do not revert user changes.
- Do not remove generated or user-provided files unless the user explicitly asks.
- Keep changes scoped to the requested product or engineering task.
- Use `apply_patch` for manual file edits.

## Feature Completion (Finish With a PR)

Every completed feature ends with a pull request — do not merge straight to `master`, and do not hand the wind-down back to the user.

Standard wind-down, once the feature branch (usually a worktree) is done, the full test suite is green, and final review passes:

1. 3-way merge the latest `master` into the feature branch (so the PR diff shows only the feature, not a spurious revert of newer `master` commits); resolve conflicts; re-run the suite green.
2. `git push -u origin <branch>`.
3. `gh pr create --base master --head <branch>` with a body covering background / approach / measured results / testing (end with the Generated-with-Claude-Code line).
4. Report the PR link to the user.

Never squash- or force-overwrite `master`'s newer commits — use a real merge, or rebase first.
