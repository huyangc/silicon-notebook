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
- Collection/notebook search must include notebook metadata, source metadata, parsed source elements, and knowledge objects.

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
- Main column: source-grounded knowhow tools, exposed as four tabs: **Ask**, **Knowledge**, **Memory**, and **Deep Report**.
  - Ask provides KG-grounded Q&A with per-sentence `[k_i]` citations and multi-turn conversation. Knowledge browses/governs extracted objects by type. Deep Report exposes the current two-stage report workflow, outline review, progress, export, cancellation, and deletion. The earlier Scenario query / Case search / Checklist tools are retired.
  - Ask federation is mode-specific. Baseline `chunk` retrieval is active-notebook-only; optional KG overlay/PPR can add federated KG context and base-backed chunks. `graph` and `reasoning` use federated KG paths. Citations retain their tier, and answer synthesis defers to base on contradiction. The `mode` field selects `chunk` (default), `reasoning`, or the opt-in/experimental `graph`; retired `fast`/`global` ids map to `chunk` only for persisted-session compatibility.
  - Ask conversation history should use a compact session context bar and an expandable session manager instead of permanently splitting the already constrained center panel.
  - Reasoning mode should surface backend agent progress in the Ask panel while the request is running. Use `POST /notebooks/{id}/ask/stream` for live NDJSON `progress` events, render the trace as a streaming one-line summary by default, provide click-to-expand details, and keep the final `reasoning_trace` visible on the answer; do not regress to a bare static "thinking" placeholder for reasoning requests.
  - Reasoning mode exposes `follow_chain` as an internal agent action, not a separate Ask mode or button. It may compose only evidence-backed, same-type two-hop paths from the explicit transitive whitelist (`derived_from/kind_of/prerequisite_of/precedes/part_of`). The two stored relations are separately citable; the composed conclusion must be labelled as an inference and must never be persisted as a KG edge. Its live/final trace step must render with the concise `推导` label and bounded detail rather than raw evidence text.
  - Answers must stay evidence-grounded, render Markdown/code/formula/table content cleanly, use compact numbered citations (`[1]`, `[2]`, ...), including model-emitted numeric citation groups such as `[1, 2, 3]` when every number maps to a known answer reference, expand citation details inside the answer panel (not in an overflowing floating popover), and support lightweight 👍/👎/copy actions. Do not flatten all related knowledge under each answer; route deeper exploration from the citation area into the Knowledge Graph.
  - Ask input ergonomics: `Enter` submits, `Shift+Enter` inserts a newline. While a model response is running, lock the input and mode controls, prevent duplicate sends, and turn the send button into an interrupt control that restores the draft question for editing. Explicit interruption must call `POST /notebooks/{id}/ask/jobs/{job_id}/cancel`; that endpoint sets the backend cancellation event so the in-flight Ask worker / LLM path stops and does not save a cancelled final answer. A transport disconnect only stops delivery to that client; navigation, refresh, or loss of the `/ask/stream` connection leaves the detached worker running to completion.
  - Prompt chips should run useful first-version questions derived from the notebook's imported source titles/summaries when available; the menu should expose a real clear/reset action.
- Knowledge Graph opens as a full-screen workspace overlay.
  - On open: fetch the current graph data and `GET /unified-kg/status`; show a refresh button when the graph is dirty. Do not trigger an automatic rebuild on open.
  - Use the object-level unified graph so Concept / Claim / Formula / Procedure nodes can appear together; do not fall back to a concept-only graph when object-level relationships exist.
  - The main canvas should show node names, type-specific node marks, and relationship labels on edges.
  - Provide multi-select type filters for dense graphs. Selecting a node from either the canvas or the overview should focus/highlight that node and update the selected-node relation/source details.
  - Keep node type color/shape marks consistent between the canvas, node overview, selected-node detail, and related-node sections. In the detail panel, label evidence/source excerpts as `出处`, render them as structured evidence cards with separate source metadata and wrapped excerpt text, hide raw `section_path`, and show concept-mounted objects as `相关节点` grouped by type.
  - The side panel should provide a type-grouped node overview (Concept, Claim, Formula, Procedure, plus future types) and selected-node relation/evidence details.
- Do not show a fixed right-column Studio sidebar in the primary notebook workspace.
  - Keep the source column + four-tab (`Ask | Knowledge | Memory | Deep Report`) main column as the two-column workspace.
  - The Analysis menu itself contains only the promotion queue (admin), tier toggle (admin), and edge-review queue. Dashboard, Schema, and the full-screen Knowledge Graph are separate top-toolbar actions.
  - Do not document or reintroduce retired content-generation, article, or derived-rule controls as current UI.
- The candidate Review Queue was removed: the KG extractor writes approved knowledge objects directly (no candidate staging). Knowledge governance is dedupe/merge over the objects in the Knowledge browser.

## MVP Scope

The MVP is a real-team local beta, not a throwaway mockup.

Confirmed scope:

- Mixed Chinese/English source material.
- First supported file types: PDF, Markdown, DOCX, PPTX.
- Real multipart upload and local storage of original files.
- Parser-generated `SourceElement` records with element-level citation granularity.
- Source summary after parsing. Use the OpenAI-compatible client when configured; otherwise use deterministic fallback.
- Notebook-internal search over notebook metadata, source metadata, source element text, and knowledge-object payloads.
- LLM-backed KG extraction for Concept / Claim / Formula / Procedure objects with evidence binding is implemented. Legacy candidate tables/endpoints still exist for governance compatibility, but the current no-LLM offline extraction path records `error_message='no-llm'` and does not synthesize rule/method/risk candidates.
- Current user-facing knowledge capabilities are Ask (`chunk` / `graph` / `reasoning`), the Knowledge browser, creator-private Memory, and Deep Reports. The current report contract is the `reports` table and `/reports` APIs; retired Scenario/Case/Checklist and content-studio endpoints are not current capabilities.
- Knowledge governance: `knowledge_objects` has a status lifecycle (`reviewed/approved/deprecated/conflict/project_specific`) plus `owner`/`last_reviewed`. Only USABLE statuses (approved/reviewed/project_specific/conflict) feed answers; `deprecated` is excluded, including during Ask's one-hop KG neighbour expansion. Browse dynamically via `GET /notebooks/{id}/knowledge-types` + `GET /notebooks/{id}/knowledge?type=...`, edit via `PATCH /notebooks/{id}/knowledge/{knowledge_id}`, dedupe via `GET .../duplicates` + `POST /notebooks/{id}/knowledge/{knowledge_id}/merge`.
- Two-tier KB & tier-aware retrieval (Wave 1+2): `notebooks.tier` is `base` | `personal` (default `personal`), set through the tier repository methods and `POST /notebooks/{id}/tier`. Baseline chunk retrieval remains active-only; optional KG overlay/PPR may add federated KG context and base-backed chunks, while graph/reasoning use federated KG paths. The exact-score `base` tie-break applies only to knowledge-object hits returned by `federated_retrieve()`; `federated_retrieve_relations()` sorts relation hits by score only. This is separate from the answer-prompt rule that defers to base and discloses a contradiction.
- Graph-reasoning Ask mode (`mode="graph"`, opt-in/experimental — default stays `chunk`): `ask_graph` uses PPR when available and otherwise builds a rustworkx in-memory graph from `knowledge_relations` (`app/services/kg/graph_reason.py`), traverses bounded multi-hop chains (`max_depth=3`, `max_fan_out=8` defaults), runs answer-time adversarial chain verification, and reports a weakest-link, authority-weighted `chain_trust` (personal hop factor `0.85`). The graph is cached with the same VectorCache version-key pattern as the vector matrix.
- Edge trust & curation (Track E): per-edge trust signals (evidence/corroboration/type-validity) plus a curator review queue (`review_queue`/`set_edge_review`); reviewer-rejected edges are excluded from graph reasoning. Endpoints: `GET /notebooks/{id}/edge-review-queue`, `POST /notebooks/{id}/relations/{rel_id}/review`. Front-end exposes an edge-review queue modal (from the analysis actions menu) that lists relations ranked by review priority with confirm / reject actions, mirroring the promotion-queue modal pattern.
- Governance / promotion (Track F): owner-triggered personal→base node promotion state machine (`propose_promotion`/`list_promotion_queue`/`approve_promotion`/`reject_promotion`), dedup-on-approve reusing the merge clustering. Backend `POST /notebooks/{id}/knowledge/{knowledge_id}/promote`, `GET /promotion-queue`, `POST /promotion-queue/{id}/approve|reject`; front-end exposes a promotion queue modal + per-object "propose promotion" affordance (personal-tier objects only).
- User-created notebooks, imported sources, and deep reports expose delete actions. Reparse preserves the source row and original file: it replaces source elements/chunks and their embeddings, and removes extraction runs plus source-derived knowledge before rebuilding. Delete performs the same cleanup, then deletes the source row (cascading source-owned records) and local file.
- User accounts: self-service registration (username rule: exactly one ASCII letter + literal `00` + 6 digits, e.g. `a00123456`; stored lower-cased) + password login (PBKDF2-SHA256). Auth uses opaque DB session tokens (`auth_sessions` table) passed as `Authorization: Bearer`; resolution is read-mostly and sliding-expiry writes are throttled by `AUTH_SESSION_TOUCH_INTERVAL_SECONDS` (default 300). Each notebook is owned by its creator. The built-in `admin` owns pre-existing notebooks and is the only user who can toggle the base-KG tier. Base notebooks are excluded from regular users' own-library lists but remain authoritative Ask context. Auth is enforced through `get_current_user`, and synchronous SQLite authorization work must stay off the event loop. Share links are implemented: small notebooks can be copied; large notebooks can be joined read-only. Owners retain write authority. There is no live collaborative editing or change-password flow.
- Public URL source import is an outbound-network security boundary: only public `http/https` targets are allowed; validate DNS addresses on the initial request and every redirect, reject userinfo, localhost, private/link-local/reserved ranges, and keep response-size/time limits. Internal documents use file upload.
- The beta is multi-account with owner isolation and link sharing, but it does not provide live co-editing.
- User Memory is manual opt-in and creator-private. Every Memory is bound to exactly one notebook; there is no orphan/global row (the global Memory page is only an owner-scoped aggregate). Ask answers use preview → user edit → confirm; an unconfigured/failing preview model must use the deterministic question-title/cleaned-answer fallback. The notebook workspace tab order is `Ask | Knowledge | Memory | Deep Report`, notebook summaries batch-count the current user's Memory, and the collection-level Memory page provides the cross-notebook view.
- Memory has `candidate/confirmed/rejected/deprecated` lifecycle states. External Agents may create only `candidate`. Same-user, same-notebook authorized Agent profiles may share candidate recall when their token has `memory:read_candidates`; other users and notebooks must never see it. Candidate is excluded from formal notebook Ask, notebook search, Deep Report, and `search_notebook_context` until the user confirms it; rejected/deprecated are excluded from both retrieval planes. Apply relevance before authority, then use `candidate < personal source < confirmed Memory < base KG/base source` only for exact-score/conflict handling.
- Memory inputs share one fail-closed validation policy across Pydantic/API, service/internal, and MCP paths. Trim title/content and reject blank values. Enforce title 80 chars, content 40,000 chars, at most 20 tags of 80 chars each, reason 1,000 chars, task-context serialized UTF-8 size 8,192 bytes, at most 50 evidence refs with a 32,768-byte serialized UTF-8 cap, and client request id 200 chars. API violations return 422; service callers must not bypass the caps. Keep frontend constraints and messages aligned with these constants.
- Candidate provenance snapshots the creating Agent profile id/name and every submitted evidence ref, never the bearer token. Validate each ref against the candidate owner/notebook and persist a per-ref validated/invalid result with a bounded reason. Legacy unverified and invalid refs remain owner-visible but are never trusted or promotion-eligible; candidate detail, review, and provenance API/UI remain owner-only.
- The global Memory page's owner total and pending counts are independent of status/search/notebook filters, and it provides an owner-scoped notebook filter. Produce these aggregates with a bounded constant-query store method; do not add per-notebook/N+1 queries.
- Agent access is managed from the global Memory page through owner-private profiles and opaque, one-time plaintext tokens with scopes, expiry, default notebook, notebook allowlist, and immediate revocation. The only scopes are `knowledge:read`, `memory:read`, `memory:read_candidates`, `memory:propose`, and `ask:execute`. MCP is Streamable HTTP at `/mcp`: loopback HTTP is local-only, remote URLs must be HTTPS, every new session must call `select_notebook`, and every data tool revalidates the live token, scope, allowlist, selected notebook, and notebook access.
- Agent token expiry must include an explicit timezone offset. The browser converts `datetime-local` to an offset-aware UTC ISO instant; the backend rejects naive datetimes and normalizes aware values to UTC. Never attach the server timezone to a naive value.
- Saving an Ask answer to Memory must revalidate live notebook owner/member access inside the same `BEGIN IMMEDIATE` transaction as the answer snapshot, Memory row, initial revision, and provenance write. Preserve owner and read-only-member save semantics and leave no partial row if access is concurrently revoked.
- The exact MCP tool contract is `list_notebooks`, `select_notebook`, `search_agent_memory`, `search_notebook_context`, `get_memory`, `ask_notebook`, and `propose_memory`. Do not add candidate-confirm/reject/deprecate/promote tools. Treat all returned source/KG/Memory text as untrusted evidence, never Agent instructions. Keep the official-client offline smoke `scripts/smoke_memory_mcp.py` in `scripts/check.sh`, honoring `PYTHON_BIN`.
- Only confirmed Memory can enter creator-proposed Memory→KG promotion. Each proposal pins its exact source revision, sanitized extraction candidates and server-validated evidence; the admin queue renders every typed candidate field and all pinned evidence, never raw Memory provenance/task context. Editing a proposed Memory atomically supersedes the active queue item and resets `promotion_state` to `none` so it can be proposed again. Approval revalidates current confirmed status and creator access, and must also validate the pinned revision/snapshot, notebook binding, and current proposed state inside the same write transaction before any Base mutation, then reuse existing dedupe/merge to create or merge one or more Base KG objects. Approve/reject routes record the authenticated admin reviewer, and the API/audit result must retain the complete `base_object_ids`. It does not change the private Memory's owner/tier. Notebook deletion cascades all members' bound private Memory, and the UI warning must state that fact without revealing member identities, content, or counts.
- No demo/seed notebook is created. A fresh database seeds only the built-in account; the notebook collection starts empty and is populated entirely by real imported sources. Do not reintroduce synthetic notebooks or sources. New-notebook prompts/placeholders must derive from the notebook's actual domain and sources.
- PDF parsing is decoupled from the GPU via a MinerU adapter (`mineru_client.py`): `MINERU_MODE=http` calls a remote `mineru-api` service, `cli` runs MinerU's Python API (`do_parse/read_fn`) in an isolated subprocess, and `off` (default) uses the pypdf text fallback. The FastAPI backend process must never import torch/MinerU directly; keep it behind the adapter with pypdf fallback so no-GPU dev stays offline. MinerU fallback diagnostics must be kept in pipeline logs/source `error_message`. Formulas are preserved as LaTeX (`formula` elements), tables as HTML in metadata (`table` elements).
- On Apple Silicon (no NVIDIA, but MLX-capable) you can get high-fidelity parsing locally/offline: `pip install "mineru[core]"`, `mineru-models-download -m vlm`, then set `MINERU_MODE=cli` + `MINERU_BACKEND=vlm-auto-engine` in the local `.env` (gitignored). Use `MINERU_PARSE_METHOD=txt|ocr|auto` and `MINERU_LANG=en|ch|...` when you need to match a manual MinerU run; use a longer `MINERU_TIMEOUT_SECONDS` such as `1800` for full papers because local VLM can exceed 10 minutes. Keep `.env.example` default `off`. `check.sh`/smoke must stay offline and never require MinerU or model weights.

## Architecture Baseline

- `frontend/` is the only frontend path. (The former static `web/` fallback has been removed.)
- `frontend/app/page.tsx` is the workspace orchestrator. Keep shared workspace API/view models in `frontend/app/workspace-model.ts`, answer/citation/reasoning-trace UI in `frontend/app/answer-panel.tsx`, and shared KG type marks in `frontend/app/kg-type-mark.tsx`; do not copy those implementations back into `page.tsx`.
- Backend uses FastAPI.
- Default local persistence is SQLite at `.local/silicon_notebook.db`, implemented with the Python standard library `sqlite3`.
- Source files are stored under `SILICON_NOTEBOOK_STORAGE_DIR`, defaulting to `.local/storage`.
- Default CORS origins include local frontend ports `3000` and `3001`; preserve this unless the frontend dev flow changes. Override at deploy time with a comma-separated `SILICON_NOTEBOOK_CORS_ORIGINS` (backend `.env`). Note: that variable is wired via pydantic-settings `validation_alias` + `NoDecode` — plain `Field(env=...)` is silently ignored in pydantic v2, so any new env-overridable setting must use `validation_alias` too.
- Repository access goes through the composed repository boundary. `SQLiteRepository` is the compatibility facade over `RepositoryRuntime`. Application services do not assemble product SQL. Stores own product SQL and raw row selection; established application/query components may assemble domain/application projections such as `NotebookSummaryQuery.from_row`. The explicitly SQLite-only maintenance adapter remains the maintenance boundary, while services own orchestration. Facade methods must remain explicit compatibility adapters or source-checked one-hop delegates whose actual targets match the ownership manifest. New consumers depend on the executable, consumer-specific Protocols in `app/repositories/ports.py`. Dependencies point facade → runtime → services → stores → `SqliteDatabase`; extracted services must never import the facade back, and a future PostgreSQL repository swaps the store layer behind the same ports. `app/services/sqlite_identity.py` and `app/services/sqlite_notebook_sharing.py` are compatibility re-export shims (no mixin inheritance); keep them and the legacy request-context, `_COPY_CHUNK`, and `_remap_json_ids` exports from `sqlite_repository.py` compatible.
- `RepositoryRuntime` owns or references composed runtime state; `REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner, and the runtime, report coordinator, and module compatibility functions share that same identity reference. Other mutable operational state is runtime-owned, and supported post-composition replacements must update every retained consumer. Synchronous Ask/report submission failures must mark the durable job/report failed, unregister its cancellation entry, and re-raise; preserve the successful worker order and existing Ask transaction checkpoints.
- Schema changes stay version-gated behind `SqliteMigrator` (append `_migration_N` + bump `SCHEMA_VERSION`); startup recovery/seed/admin-upgrade run every boot outside the version gate. Pre-refactor databases must keep loading: the frozen v9 fixture replay (`backend/tests/fixtures/repository_v9/`, `test_legacy_db_compat.py`) and the backup-only real-database verifier `scripts/verify_repository_snapshot.py` are the guards. The verifier uses exact per-version migration and stable-seed manifests, percent-encodes SQLite URI paths, never constructs the repository on an original database/storage path, and reports a retained temporary backup on cleanup failure without private row data. Original DB/WAL metadata and SHM existence/size are guarded; on a live WAL attachment only SHM mtime is exempt.

The current schema version is 11. The committed v9 compatibility fixture
upgrades through the existing v10 migration and the v11 Memory/Agent migration,
and remains readable.
- PostgreSQL + pgvector remain the future production/team-beta direction. Do not require them for the current local beta.
- Until a PostgreSQL repository exists, reject every non-`sqlite:///` `DATABASE_URL` at settings construction; never silently fall back to `.local` for an unsupported database scheme.
- KG-native tables are live: `knowledge_objects` (object types `concept/claim/formula/procedure`), `knowledge_relations`, knowledge/element/chunk/relation embeddings, `concept_clusters`, `extraction_runs`, `answers`, `conversations`, `feedback`, `ask_jobs`, `ask_trace_steps`, and `reports`; sharing uses notebook share fields plus `notebook_members`. The independent Memory layer uses `memory_items`, `memory_revisions`, `memory_provenance`, `memory_embeddings`, `agent_profiles`, `agent_access_tokens`, and `agent_token_notebooks`; Memory is never inserted into source/chunk/KG tables, and KG promotion creates a separate governed object. Embedding vectors are persisted locally and assembled into versioned float32 matrices or scale indexes. Graph/reasoning paths build or load federated graph state while preserving tier provenance.
- All graph consumers (federated traversal, PPR, scale-index build/delta) must share the same usable-relation rule and exclude `review_status='rejected'`. Direction rendered to the LLM is always stored `source_object_id → target_object_id`. In-memory graph size guards cover the active notebook plus every participating base notebook.
- Notebook copy lifecycle states are repository-owned, not public `NotebookUpdate` fields. A failed copy compensates only its own destination; crash recovery sweeps only expired `copying` rows (`NOTEBOOK_COPY_STALE_SECONDS`), never every live sentinel.
- Frontend async state is resource-owned: Ask callbacks validate run + workspace epochs; notebook/share/deep-link transitions go through the atomic notebook opener; logout aborts local streams and remounts user state.
- Upload registers `process_source` asynchronously through the shared KG job scheduler (`kg_scheduler.submit_job`; status `queued→parsing→parsed→extracting→extracted`). After parsing, **element embedding runs in a background daemon thread concurrently with foreground KG extraction**; `extracted` (UI green) is gated on extraction completion only. SQLite uses WAL + `busy_timeout` so concurrent writers do not lock. The repository still accepts a `scheduler` callback so scripts/tests can run synchronously.
- Ask must stay off heavy maintenance work: no synchronous whole-notebook embedding backfill, no synchronous unified-KG rebuild, and no full source-element scan for citation validation.
- `follow_chain` must remain a bounded, production-compatible query-time primitive: add no startup migration, new index, or historical backfill; reuse the existing `(notebook_id, source_object_id)` / `(notebook_id, target_object_id)` indexes, and hard-limit the raw rows read per endpoint before filtering. Two frontier rounds have per-node fan-out/result caps, no whole-graph load, and no writes. A truncated endpoint whose direct-edge absence cannot be proven must suppress that inference. It also fails closed on a start id outside the current candidates, non-whitelisted/mixed relations, missing relation quote, rejected/unknown-review edge, unusable node, cycle, direct-edge duplicate, `chain_trust < 0.5`, or incompatible `validity_scope`; trust is weakest-hop × tier/review/primary-evidence × hop penalty and stays separate from candidate query relevance. Relation anchors preserve stored `source→target` direction. New relation evidence should bind to `SourceElement` when possible, while legacy quote-only rows degrade to source-level display and all older KG data remains usable by existing retrieval paths.
- Streaming Ask (`/ask/stream`) must remain progress-first: emit an immediate `started` event with the detached job id, stream each agent trace step as it is recorded, then emit one final `AskResponse`. A transport disconnect only stops delivery to that client; the worker continues and may persist its answer. Only the explicit interrupt path calls `POST /notebooks/{id}/ask/jobs/{job_id}/cancel`, sets the cancellation event, and stops the worker before saving a cancelled response. The normal `/ask` endpoint remains the non-streaming compatibility path.
- Unified-KG rebuild is explicit/observable. Opening the Knowledge Graph overlay fetches the current graph + `/unified-kg/status` and offers refresh when dirty; it must not block on rebuild.
- Cross-document concept-merge candidates must be bounded and reviewable. LLM merge review operates on small pending candidate batches, never the entire concept set at once.
- Re-running parse/extraction preserves the source row and original file, replaces source elements/chunks and their embeddings, and removes extraction runs plus source-derived knowledge before writing rebuilt state.
- Deleting a source uses the same source-derived cleanup, then deletes the source row (cascading source-owned records) and local file. Do not claim unrelated artifact cleanup that the repository does not perform.

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

**Principle — generative/retrieval model services are URL-based only.** Chat LLM, embeddings, and rerankers are reached over configurable HTTP/OpenAI-compatible endpoints; the backend does not load them in-process or start an inference server. MinerU is a separate document-parser adapter and may use its explicitly supported isolated `cli` subprocess in addition to remote `http` and pypdf `off` modes.

Business logic should call a provider adapter/client, not hard-code a specific model vendor.

When no LLM key is configured, endpoints may return deterministic fallback data so the local beta remains usable.

## Logging / Observability

Structured logs go to `.local/logs/*.jsonl` (gitignored) plus brief console lines, via a single `EventLogger` (`backend/app/core/event_logging.py`). Add observability at the existing chokepoints, not at each call site:

- `requests.jsonl` — HTTP middleware in `app/main.py` (method/path/status/latency/`request_id`; slow calls flagged via `SLOW_REQUEST_MS`; `X-Request-Id` response header).
- `events.jsonl` — source pipeline in `SQLiteRepository.process_source` / `_set_source_status` (per-stage timings + status transitions + failure stack).
- `llm.jsonl` — `LLMInteractionLogger` wrapping `OpenAICompatibleClient` (`app/core/llm.py`); chat detailed, embeddings summarized, errors recorded.

Rules: reuse `EventLogger` for any new structured log (it handles JSONL append + console + never raising); never log raw embedding vectors; chat prompt/response are truncated to `LLM_LOG_MAX_CHARS`. The browser/API debug log viewer (`/dev/logs`, `/api/debug/logs/...`) is opt-in because full LLM records may contain prompt/response text from private sources; enable only with `DEBUG_LOGS_ENABLED=true`. Config env vars: `LLM_LOG_ENABLED`, `LLM_LOG_PATH`, `LLM_LOG_MAX_CHARS`, `EVENT_LOG_ENABLED`, `EVENT_LOG_DIR`, `SLOW_REQUEST_MS`, `DEBUG_LOGS_ENABLED` — keep `.env.example` aligned.

For deployment slow-path triage, `scripts/diag_slow.py` is the read-only report script. It must remain safe to run on the host that owns `.local/`, avoid printing private prompt/source text, and include strict reasoning / PPR path evidence from DB aggregates and scale-index manifests so large-library indexed-core coverage, delta policy, chunk/relation ANN availability, and cross-base full-vector risks are visible.

## Verification

Run:

```bash
bash scripts/check.sh
```

This checks:

- Backend Python syntax.
- SQLite initialization and persistence smoke path.
- Markdown, DOCX, PPTX, and PDF upload/parse smoke path (sync and async-scheduler paths).
- KG extraction boundary (`no-llm` offline), explicit KG storage, source cleanup → Ask → feedback → conversation and report paths, retrieval scoring, stale-source knowledge invalidation, sharing, and fresh-database assertions.
- Logging: LLM interaction log, generic event log (parseable/disable/never-raise), and pipeline stage events + `error_message` regression.
- Official MCP client smoke for the seven Memory tools, session notebook selection, candidate exclusion from formal context, and same-user/same-notebook cross-Agent candidate recall.
- Source summary fallback and notebook-internal search.
- Complete backend `pytest` suite.
- Every recursively discovered frontend `*.test.mjs`, Next.js TypeScript, and production build. Missing `frontend/node_modules` is a hard failure.

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
- Execute approved multi-step implementation plans with subagent-driven development by default: use a fresh implementation subagent per task, then run task-scoped specification and code-quality review before advancing. Pure research, design, status, and review-only work does not require a worktree or subagents.
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
