# Product and API reference

[Back to README](../README.md) · [中文说明](./product-and-api_zh.md)

This document preserves the detailed product behavior and HTTP/MCP contracts. The root README is the short project entry point; implementation and architecture details remain in [architecture.md](../architecture.md) and [AGENTS.md](../AGENTS.md).

## Current Scope

This repository targets a local real-team beta loop built around a KG-native pipeline:

- Python FastAPI backend; SQLite persistence at `.local/silicon_notebook.db`
- Next.js / React / TypeScript frontend under `frontend/`
- Deployment-owned OpenAI-compatible chat, embedding, and rerank services, with workload bindings and per-service `max_concurrency` declared in one TOML file
- Deterministic fallbacks when no LLM/embedder is configured — the whole pipeline runs offline
- Clean start: a fresh database seeds only the local user; no demo notebook or synthetic sources
- Multipart source upload for PDF, Markdown, DOCX, PPTX, CSV, and XLSX (async through the shared KG job scheduler)
- **KG-native ingestion**: structured Markdown parse → greedy-window KG extraction (Concept / Claim / Formula / Procedure) with concurrent embedding → extraction-first status (`extracted` = KG ready, does not wait for embedding)
- PDF/DOCX/PPTX parsing via MinerU (formulas as LaTeX, tables, layout, embedded images) when configured; pypdf text fallback locally or when MinerU is off
- MinerU-extracted embedded images are retained and shown inline in the source view; captions and text remain searchable
- Hybrid retrieval: CJK-aware bi-gram keyword + float32 semantic search with per-notebook caches. SQLite FTS5 keeps an exact-phrase bonus but also ORs safely quoted Latin/number terms and overlapping CJK trigrams; PostgreSQL decomposes the same bounded terms before native trigram candidate generation. Indexed Chunk/KG paths merge bounded ANN and lexical candidate windows; indexed Relation retrieval adds direction-balanced relations adjacent to lexically matched KG endpoints while preserving endpoint rank. Lexical-only candidates remain keyword-only during fusion rather than receiving a synthetic zero semantic score.
- KG-native grounded Q&A: sentence-level `[k_i]` citations (rendered as compact numbered references, including model-emitted numeric groups like `[1, 2, 3]` when they map to known references), multi-turn conversations, 1-hop KG neighbour expansion, and a live, expandable one-line agent trace for reasoning mode
- **Intent-first reasoning Ask:** before the official UI starts a `reasoning` job, `POST /api/notebooks/{id}/ask/intent` interprets the question without reading notebook/reference-library content. It may use the latest prior user questions, but never corpus-derived assistant answers, and creates neither a conversation nor a job. Clear intent auto-continues; because no human reviewed that normalization, the original wording stays the authoritative first retrieval seed and the model rewrite is supplemental. Direction-changing ambiguity pauses for confirmation, after which the reviewed wording is authoritative. The frozen topics/directions, entities, axes, constraints, exclusions, assumptions, expected output, and answers govern Memory, PPR, evidence retrieval, and synthesis. The whole authoritative question runs first, then confirmed directions are seeded round-robin within the topic budget; no second planner may replace them. Invalid confirmations return 422 before durable state is created, and aborting preflight signals the model call to cancel.
- **Typed query-time inference in reasoning mode:** the agent can call `follow_chain` to compose an evidence-backed two-hop `A→B→C` path into a transient `A→C` inference for `derived_from / kind_of / prerequisite_of / precedes / part_of`. Both direct hops remain independently citable relation evidence, rejected/ungrounded/scope-conflicting paths fail closed, the inferred conclusion is explicitly marked as reasoning, and no inferred edge is written back to the KG. The feature adds no migration, new index, or historical backfill; bounded samples use the existing source/target relation indexes and ambiguous high-degree paths are skipped.
- Two-tier knowledge base: each notebook has a `tier` (`base` | `personal`, default `personal`). Baseline `chunk` retrieval reads chunks from the active notebook only; optional KG overlay/PPR can add federated KG context and base-backed chunks, while `graph` and `reasoning` use federated KG paths. The exact-score `base` tie-break applies only to knowledge-object hits returned by `federated_retrieve()`: scores stay unchanged and a higher-scoring personal hit still wins. `federated_retrieve_relations()` remains score-only. Separately, when base and personal evidence contradict during answer synthesis, the answer defers to the base position and surfaces the discrepancy. Citations carry their tier (`AnswerAnchor.tier`) and Ask renders a `base`/`personal` badge per cited anchor.
- **User accounts**: self-service registration (username rule: a single letter + `00` + 6 digits, e.g. `a00123456`; stored lower-cased) + password login with opaque Bearer session tokens. Each notebook is owned by its creator; a user's library contains owned notebooks plus large shared notebooks they explicitly joined read-only. On first boot the built-in `admin` account is created (login `admin`, password from `SILICON_NOTEBOOK_ADMIN_PASSWORD`, local default `admin`; production/non-loopback startup requires changing it) and owns pre-existing notebooks. Administrators can grant or revoke the `admin` role from the user-usage page through `PATCH /api/admin/users/{user_id}/role`; the built-in administrator and the active administrator's own role cannot be revoked. Role changes are observed by existing sessions on their next request. Each notebook caps the number of user-uploaded documents (default 20, `USER_UPLOAD_DOCUMENT_LIMIT`); administrators tune it from the user-usage page — a global default (`PATCH /api/admin/settings/upload-limit-default`) plus per-user overrides (`PATCH /api/admin/users/{user_id}/upload-limit`, `null` clears the override and falls back to the global default). Administrator-owned notebooks are exempt. Any administrator can publish a notebook as a public knowledge base. Base notebooks are hidden from regular users' lists but are discoverable through each notebook's reference-library picker, and participate in retrieval only for notebooks that explicitly mount them. Upgrading an existing deployment to schema 20 does not backfill mounts: every pre-existing notebook starts with zero mounted reference libraries, and federation stays off for it until a user explicitly mounts one. Set `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` for local/no-auth testing. The frontend shows a login/register gate on first load; the topbar displays the logged-in username and a logout button.
- **Share links**: owners can publish an opaque notebook link. Small notebooks are copied into the recipient's account; large notebooks are joined as read-only membership. Write access stays with the owner, and there is no live collaborative editing or change-password flow.
- **Notebook-bound private Memory**: users can manually turn an Ask answer into an editable preview and confirm it as reusable Memory. The collection has a user-level Memory page; notebook cards show the current user's count, and each workspace exposes **问答** (Ask) | **知识库** (Knowledge) | **记忆** (Memory) | **深度报告** (Deep Report). External Agents can submit `candidate` Memory through MCP; candidates are shared only among that same user's authorized Agents in the same notebook and do not enter formal Ask/search/report retrieval until the user confirms them.
- Optional graph-reasoning Ask mode (`mode="graph"`, opt-in/experimental): a rustworkx in-memory graph built from `knowledge_relations` is traversed for bounded multi-hop derivation/support chains, with answer-time adversarial chain verification and a weakest-link `chain_trust` score (the default Ask mode stays `chunk`)
- Deep report (two-phase background job): a notebook-level "深度报告" action turns one question into a multi-section technical report. **Phase 1a is corpus-blind question understanding**: it extracts an editable resolved question, objective, mandatory topics, entities, comparison axes, constraints, exclusions, expected output, assumptions, confidence, and at most eight blocking ambiguities without calling notebook search. The report always pauses at `intent_ready`; required ambiguities must be answered, while a clear question still asks the owner to confirm the resolved wording. Read-only members cannot confirm it. `auto_generate` is remembered but cannot bypass this gate. Confirmation atomically claims `intent_ready → planning` and deterministically freezes the contract already shown to the user; it does not run a hidden second interpretation pass. Clarification answers enrich the internal retrieval/drafting question but never the visible report heading. **Phase 1b begins only after intent confirmation**: the confirmed wording and answers become authoritative, a bounded zero-LLM probe measures both federated KG and direct parsed-`SourceElement` coverage for every mandatory topic, and only then does the STORM-style planner use source titles, KG hits, and chunk provenance to refine vocabulary, ordering, perspectives, and tensions. Corpus availability may expose a gap but cannot replace or narrow a required topic; code validates the mapping and restores any omitted mandatory topic. The outline editor shows each section's mandatory question, editable retrieval directions, and raw-element/KG/base coverage before confirmation; the last section binding a mandatory topic cannot be deleted, and the API enforces the same invariant. **Phase 2 (minutes, on outline confirm)** runs every approved retrieval direction as well as the full `reasoning` deep-dive, in parallel by section. Chunks, KG objects, typed relation hops, confirmed Memory, and direct `SourceElement` hits share the same `[k]` binding path. `SourceElement` is a first-class citation rather than uncitable prompt decoration: small libraries may score element rows directly, while non-copyable large libraries derive a bounded candidate set from chunk ANN/FTS hits and hydrate only those chunks' exact `element_ids`, never the full element table. Report references deduplicate by exact evidence anchor rather than source title, and selecting a report citation reveals its bound source/location excerpt. Ask and report citations prefer `source_paper_meta.paper_title` only for a grounded paper row (`is_paper=true`) with a nonblank parsed title; all other sources keep their ordinary source title/file name. The model's `grounded` boolean is advisory: the backend reparses emitted anchors and requires cited evidence to meet the configured relevance threshold. A read-only final editor creates the executive summary and flags incomplete mandatory intent or cross-section contradictions without rewriting sections or adding facts. The existing `（推断）`/`【通识】` discipline, five depth levels, `KG_JOB_CONCURRENCY` parallelism, live `section_status`, cancellation, `.md`, and `reports.zip` export remain unchanged.
- Edge trust & curation: per-edge trust signals (evidence / corroboration / type-validity) plus a curator review queue; reviewer-rejected edges are excluded from graph reasoning
- Knowledge governance: browse by type via `/knowledge-types` + `/knowledge?type=...`, status lifecycle, duplicate detection & merge, conflict detection; `deprecated` objects excluded from retrieval and 1-hop expansion. Personal→base node promotion (propose → under review → approve/reject) with dedup-on-approve and a curator promotion queue
- Unified KG: cross-document concept clustering (`concept_clusters`), pending-merges review
- Object-level KG visualization: Concept / Claim / Formula / Procedure nodes with type-specific shapes, edge labels, multi-select filters, and a type-grouped side panel
- Notebook collection (grid/compact/list, edit/delete); clicking `＋ 新建` creates an `Untitled notebook` and enters it immediately — no dialog
- No Docker in the first version

SQLite remains the shipped default, while PostgreSQL 16 is also a supported direct repository backend. The application selects exactly one through `DATABASE_URL`; it does not dual-write or move existing data. PostgreSQL stores vectors in `bytea`, so pgvector is not required.

## Product Flow

The outer page is a notebook collection/library (KG-native pipeline):

1. Click `＋ 新建` — the app creates an `Untitled notebook` and enters it immediately (no dialog).
2. Upload PDF, Markdown, DOCX, PPTX, CSV, or XLSX sources (multipart).
3. Backend (async background job): structured Markdown parse → chunking + embeddings — chunk-native Q&A is ready as soon as the source finishes processing.
4. **KG extraction is conditional** (see [KG extraction trigger](#kg-extraction-trigger)): on ingest it runs only when the notebook already has a KG, or when `KG_AUTO_EXTRACT=true`. `KG_JOB_CONCURRENCY` controls concurrent source jobs; every extraction model call is admitted by the system model scheduler for the service bound to the `kg_extract` workload, so the service's TOML `max_concurrency` remains the only model-capacity limit. The new source is then incrementally fused into the unified KG.
5. Knowledge objects are stored in `knowledge_objects` + `knowledge_relations` with element-level evidence bindings.
6. Hybrid retrieval (bi-gram keyword + float32 matrix semantic) feeds KG-native Q&A: answers contain sentence-level `[k_i]` citations, support multi-turn conversations, and expand via 1-hop KG neighbours.
7. Unified KG aggregates concepts across documents; pending cross-document merges can be confirmed or rejected.

Inside a notebook:

- Header: the editable notebook title stays compact by itself; the notebook description is shown in the Ask welcome state when no conversation is active, and toolbar actions keep their labels intact across desktop widths.
- Left column: user-imported source files with live parse-status (green = `extracted` only; others shown in amber while processing) plus per-source anomaly badges graded by consequence (red for integrity problems such as a parse failure, amber for retrieval-only problems such as partially unanalyzed content; neutral pending states like 待补全 appear only in the source detail view), detail previews, and delete actions. Every user-facing source count uses this visible imported-source set and excludes hidden `memory` / `knowhow` projection sources. Network source search is disabled for now.
- Main column: four tabs — **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report). Ask provides grounded Q&A with clickable `[k_i]` sentence citations, three retrieval modes, multi-turn conversations, a live expandable reasoning trace, and feedback. Conversation history uses a single-row `历史 N` entry in the Ask header plus an expandable manager; the adjacent `+` starts a new session directly. It is ordered and timestamped by sub-second latest activity, and a submitted first turn appears immediately and remains reopenable while the model answer is still running—even if the user switches sessions before `started` arrives. Terminal history summaries refresh for the active notebook independently of which session owns the answer panel; same-notebook refresh callers converge on the newest request. The composer and mode controls remain disabled while the selected notebook/session's latest detail is loading. Knowledge browses and governs dynamic object types. Memory shows only the current user's private records bound to this notebook. Deep Report exposes the two-stage report lifecycle, outline review, progress, export, cancellation, and deletion. In Ask, `Enter` submits, `Shift+Enter` keeps a newline, and while a model response is running the input/mode controls are locked while the send button becomes an interrupt control. A transport disconnect stops delivery to that client only; navigation, refresh, or transport loss leaves the detached Ask job running and it may persist its final answer. Clicking interrupt is a distinct cancellation action: the client calls `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel`, which sets the backend cancellation event so the worker/LLM path stops and does not save a cancelled final answer. If clicked before the first `started` event is readable, the client restores the draft immediately but keeps that run's transport only long enough to obtain its job id, cancels that job, then aborts it. The workspace remains two columns and has no fixed Studio sidebar.
- Knowledge Graph opens as a full-screen overlay: object-level KG nodes (Concept / Claim / Formula / Procedure) with type-specific shapes, edge relationship labels, multi-select type filters, and a type-grouped side panel that focuses the canvas on selection. The side panel renders source excerpts as structured evidence cards so long titles, locations, formulas, and mixed Chinese/English text stay inside the panel; excerpts whose source element type is `formula` use the shared block KaTeX renderer instead of showing raw commands.
- The Analysis menu itself contains only the promotion queue (admin), publish / unpublish public knowledge base (admin), and edge-review queue. Dashboard and the full-screen Knowledge Graph are separate top-toolbar actions; the graph Schema (object-type/field management, admin-only, surfaced as 「图谱 Schema」) is entered from a button inside the Knowledge Graph view header rather than a separate top-toolbar action; no retired content-generation or derived-rule actions are exposed. The existing notebook analytics view includes separate Memory and Knowhow content-asset cards: Memory metrics are restricted to the signed-in user and current notebook (including for admins), while Knowhow metrics follow notebook read access. The cards show counts, health/recency summaries, and links only; they navigate to the existing Memory and Knowhow pages/editors rather than duplicating a browser or editor.

Knowledge object types have a single source of truth for their display name: the backend `OBJECT_TYPE_LABELS` in `app/services/extraction_profiles.py`, delivered to the client as `KnowledgeTypeCount.label` by `GET /notebooks/{id}/knowledge-types`. Every call site that can reach that API label — the Knowledge browser's type tabs and object entries — renders it directly, so user-defined object types (for example the column names projected from knowhow tables) also show their proper Chinese name. Call sites that only hold an `object_type` string — the citation popover and the knowledge-graph canvas/side panel — fall back to the small built-in front-end table `KG_TYPE_LABELS` in `frontend/app/kg-type-model.ts`; `kg-type-mark.tsx` consumes and re-exports that model for shared rendering. The table is character-for-character identical to the backend constant; `scripts/check_object_type_labels_contract.py` runs inside `scripts/check.sh` as a hard gate that fails the build when the two copies drift. An unknown or custom type is displayed verbatim as its `object_type`, never Title-Cased into invented English. Because both tables are keyed by user-controlled strings, look them up with `Object.hasOwn(...)` rather than bare indexing: `constructor` and `__proto__` resolve through the prototype chain and yield inherited functions/objects instead of a miss.

User-facing copy is under a vocabulary contract of its own, and `AGENTS.md`「界面词汇表」is its single source of truth: each row maps an internal term (基准库, chunk, KG, 抽取, 投影, 晋升, schema, deprecated, …) to the one word the interface may use. Internal names stay in code, types, comments, and the architecture docs — only strings rendered to a user get rewritten, and values that are *persisted* rather than rendered (the `Untitled notebook` default name, enum ids on the wire) are contracts, not copy, so they are never touched by a wording pass. `scripts/check_ui_vocabulary.py` enforces the table inside `scripts/check.sh`, and its scope follows the **trust boundary rather than the directory tree**: it scans the rendered text of every `frontend/app` source — string literals plus JSX text nodes, with comments, identifiers, regex bodies, and `${…}` / `{…}` interpolations stripped — *and* the message literals of every backend `user_error(status, "…")` call, because `api/deps.py` marks exactly those 4xx `detail` strings with `X-User-Message: 1` and the deny-by-default front end then displays them verbatim. Marking a string is a promise that it is user copy, so it inherits the copy rules; scoping the guard to `frontend/app` is what let 「基准库」and 「晋升队列」ship inside marked 403s while the guard stayed green. Bare `HTTPException(detail=str(exc))` stays outside the scan on purpose — it is never displayed and its detail is a diagnostics/MCP contract, a split guarded by `backend/tests/test_user_error.py`. A blacklisted term on either side fails the build. A second, independent guard — `frontend/app/raw-enum-fallback.test.mjs`, collected by `npm run test` and therefore also gated by `scripts/check.sh` — rejects raw enum fallbacks (`MAP[x] ?? x`, and `label(map, x, x)` which defeats the same design through the sanctioned API), because a lookup that falls back to its own key starts rendering the backend's English enum id the moment the backend grows a value; use `label(MAP, value, fallback)` from `frontend/app/vocabulary.ts`, whose mandatory neutral fallback makes that bug unwritable. That check runs on a real TypeScript AST rather than a regex: `M[x] ?? x` in a rendered position and `ALIASES[v] ?? v` in internal normalisation are the *same* syntactic shape, so only the surrounding context distinguishes a leak from correct code — a regex flagged the second and missed `M?.[x] ?? x`, `getLabels()[x] ?? x`, and `label(m, x, x)` entirely. Its own docstring records what it still cannot see (a value computed into a variable before being rendered, non-JSX sinks such as `alert(...)`), since honest scope beats a check that fakes completeness. Deliberately echoing a *user-authored* string (a custom `object_type`, a user-defined schema field name) is written as an explicit `Object.hasOwn(...) ? ... : raw` instead, which also avoids the prototype-chain hazard above. The guard is a word blacklist, not a semantic checker: two rows are covered only in their unambiguous compound forms, since bare 节点 / 边 are legitimate in the graph view and 边 is a substring of 旁边 / 边框. `backend/tests/test_ui_vocabulary_guard.py` holds its positive and negative examples and additionally fails when a vocabulary-table row gains neither a matching rule nor a recorded exemption, so the blacklist cannot quietly drift back into covering only a subset of the table.

Reparse preserves the source row and original file: it replaces source elements/chunks and their embeddings, and removes extraction runs plus source-derived knowledge before rebuilding. Delete performs the same source-derived cleanup, then deletes the source row (cascading source-owned records) and the local file.

Visible imported-source counts deliberately differ from physical bookkeeping: hidden Memory/Knowhow projection sources do not appear in the source rail or user-facing counts, but `size.sources`, copy thresholds, storage accounting, and background scheduling retain physical-row semantics. Likewise, `has_unindexed_content` keeps the scale-index update decision true when derived content changed even if the visible imported-source delta is zero.

Scale-index scheduling exposes immediate and off-peak operations. When no build is already running, `when=now` atomically supersedes an older idle entry for the same notebook before claiming the immediate build; a later idle request is preserved, and a worker-start failure restores the displaced entry. Scheduler ticks claim queued notebooks independently, leaving busy follow-ups queued and isolating per-item launch failures. `AskResponse.index_required` remains an answer-time diagnostic snapshot, while the Ask UI observes live `ScaleIndexStatus.exists`; an `index_done` event refreshes the active notebook even after bounded foreground polling stops, so a published index removes that warning from historical answers without rewriting them.

The notebook workspace hides the global collection top bar and keeps an engineering-console visual treatment. Markdown shown in Ask, reports, Memory, and Knowhow treats a whole-line one-line `$$...$$` as display math even when it is adjacent to prose; wide display equations scroll inside their own content block. Source-detail, knowledge-object, and Knowledge Graph evidence formula views remove full-value Markdown math delimiters before direct KaTeX rendering and show the original text if parsing still fails, so malformed formula input never becomes a blank visualization.

## Knowhow tables

A notebook's **Knowhow 表** action (opened as its own panel, alongside Knowledge Graph) manages **knowhow tables**: structured domain know-how captured as rows of experience entries under free-form column names. The shipped example is semiconductor timing-violation triage (one row per violation type; columns for symptom identification, root-cause analysis, fix method, tooling), but columns are plain user-defined text, not a fixed vocabulary. A table starts either from an import (xlsx/csv/Markdown, with a column-to-kind mapping preview) or from a **create-table wizard** (define the column headers first, then fill in rows). New-table import asks whether attributes are arranged by column (the default: first row is the header) or by row (first column contains attribute names); row-oriented input is transposed on the backend before preview and commit, so the internal grid, append-import contract, and projection pipeline remain column-oriented. Structural validation failures are returned as safe, actionable wizard copy. In particular, an attribute-row workbook whose record groups use horizontally merged cells is recognized after merge expansion and directs the user to select **属性按行**; duplicate/blank headers, unsupported files, invalid encodings, and stale column settings explain what to change instead of collapsing to a retry-only message. Values can be entered two ways, freely mixed: in-app through a **cell editor** — a Markdown editor that defaults to a single focused column and toggles to a side-by-side or full preview (choice remembered per session), with paste-or-drag image upload, local autosave drafts (every exit persists unsaved edits as a restorable local draft first and refuses to leave if that write fails, and leaving via Esc/backdrop/× or switching cells asks first), and a *save and move to the next cell* flow for fast sequential entry — or offline through an **Excel template round-trip**: download the table's current header as an `.xlsx` template (header row frozen), fill it in bulk, then upload it to append rows (a preview reports unmatched columns and rows whose title duplicates an existing one before you commit).

At most one column can be designated the table's **行标题列 / row-title column** (a table-level choice, not a per-column tag). With one set, every non-empty cell becomes a knowledge-graph node whose *type is its column name*, linked by an `about` edge back to that row's title-column node, and identical values in the same column across different rows merge into one node (ten rows citing the same tool become one tool node with ten incoming edges). Leave it unset and the table stays retrieval-only — cells still become searchable chunks for Ask, but nothing is added to the graph, which is the right shape for log-like tables where a row is a record rather than a named thing.

Projected cell knowledge objects enter reasoning/graph KG-node retrieval by default (`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED=true`), so a matching cell can seed graph traversal and its citation retains the direct row-drawer jump. Setting the flag to `false` is a reversible rollback of only this direct-object path; per-cell chunks remain searchable in Ask. Default-on type widening is limited to objects owned by the table's `hidden_source_id`—unrelated custom Schema types are never swept in—and the scoped type set plus normalized chunk-vector→object bridge are version-cached/single-flight across reasoning subqueries. The bridge generation includes both KG mutation state and the scoped chunk-vector count/timestamp, so vector-only repair refreshes it even though that repair intentionally does not mutate KG state; KG mutations also evict it explicitly.

Projection status is a table-completion contract, not a per-row progress shortcut: rows remain `pending`/`syncing` until the table-wide chunks, embeddings, knowledge objects/relations, mutation sequence, and graph-cache notifications have finished. A row is published as `synced` only after that terminal work succeeds, so callers that observe every row settled can immediately read the completed graph without a scheduling race. Publication is conditional on the table mutation sequence captured by the pass: an older pass can never overwrite the `pending` marker from a newer concurrent edit while its scheduler rerun is queued.

Each column also carries a **content kind** — a deterministic parsing hint, never an LLM call: **方法步骤 / procedure** cells parse as an ordered list of steps, **工具/事物 / entity** cells split on list items/newlines into one deduplicated node per item, and **普通 / attribute** cells stay a single node. Both the cell editor and the row-detail drawer expose an explicit **优化表达 / optimize wording** button (never triggered automatically): it uses the system service bound to `knowhow_optimize` to tidy structure and phrasing while preserving meaning, shows the rewrite side-by-side with the original, and only replaces the cell after you accept it, one cell at a time.

Row/table **one-click reformat** freezes one whole-table snapshot, then generates candidates with bounded client concurrency rather than an unbounded `Promise.all`: the cap is the smaller of three and the live `knowhow_reformat` service capacity, with a safe fallback of two when status cannot be read. Equal `(column_id, trimmed original Markdown)` inputs share a single in-flight request; only a successful, still-fresh result is reused. Cancellation or closing stops launching work and ignores late responses. Progress counts physical cells, partial failures remain retryable, and confirmation still saves complete physical/shared units serially. Every save keeps `expected_before`, anchor designation, exact complete-group membership and HTTP 409 stale guards; stale candidates stay visible for rerun and the parent table reload waits until the dialog closes. Observing any stale result records that pending reload immediately, even if the user then aborts other slow requests. Each changed or saved queue entry opens an in-dialog raw Markdown diff (line additions/deletions plus bounded inline token highlighting for Chinese, Latin text, punctuation and whitespace), with rendered preview as an optional view. Oversized inputs fall back to a bounded prefix/suffix summary. A saved entry can close the batch dialog safely and open the existing detail view for its physical cell; if the batch also observed stale data, the parent first awaits an epoch-guarded detail reload and recomputes the target from those fresh rows. A failed/invalidated reload or a row/column that disappeared opens nothing and surfaces the existing recoverable table-action error. A shared/merged value uses the stable representative with the smallest row position and then row id. This remains one overall confirm operation, not per-item accept/reject.

The main grid keeps `table-layout: fixed`, horizontal scrolling and its sticky first column, but emits a `colgroup` whose widths come from a bounded pure summary of the header plus at most 64 visible rows (first 48 and last 16). Each sampled cell is truncated to a fixed code-unit prefix before newline normalization, Markdown regexes/splitting, or grapheme segmentation; estimation then examines at most eight visible lines and 120 graphemes, discounts Markdown control syntax, and weights CJK/full-width/emoji above ASCII before applying per-column min/max clamps. Status/action columns remain fixed. Narrow screens use tighter clamps. The calculation is memoized from table identity, columns and visible rows, so rendering never performs an unbounded R×C or whole-cell scan. Manual resizing and width persistence are not part of this behavior.

Knowhow ownership and authorization continue to use stable user ids (`created_by`, owners and permission checks). Human-facing audit snapshots (`knowhow_changes.actor`, milestone creators and cell-code updater labels) use the session user's trimmed `username`, then trimmed `display_name`, then user id; Agent writes keep the Agent `profile_name`. All ordinary Knowhow write entry points use the same actor-label helper, while copy/import/transfer paths pass identity ids and audit labels separately. Existing id-shaped audit values are not rewritten: read APIs resolve a bounded set of recognizable legacy user ids to current usernames in bulk and otherwise return the stored value, without N+1 queries, and async routes run this synchronous identity projection in a threadpool. For an `origin=agent` history change, only legacy human updater ids inside the semantic `payload.before` snapshot are resolved; the Agent actor and `payload.after`/`current` updater labels remain opaque profile text even if they resemble a user id. Wire field names remain compatible. In particular, stored `knowhow_cell_code.updated_by` participates in the table fingerprint and is never batch-rewritten merely for display; new writes store the label, single-cell GET/PUT responses include that readable `updated_by`, and reads project display values without changing history.

The row-detail drawer, and each physical branch in a row-title-group matrix, provide an explicit **智能补全空列** action. It produces suggestions only for stored-empty cells in that row (a missing value or exact empty string; whitespace-only stored content remains existing content). One request gathers two evidence channels: at most eight rows from the same table that fill a requested target column, preferring the same row-title group and then known-column similarity/coverage; and one bounded `ReasoningRetriever` run over the active notebook plus its currently valid explicit reference-library mounts. The latter follows the Ask `reasoning` planning, federated retrieval, reflection, graph expansion, and evidence-backed query-time chain-traversal family, but its completion-specific policy removes private Memory and the current table's own projection before candidates reach model reflection, and disables provenance-opaque PPR/community expansion. It never invokes Ask answer synthesis, creates a conversation/job, or saves an Ask answer. The structured response contains a suggestion or abstention for each requested column, confidence, basis, accepted table-row ids and server-issued library-evidence keys, plus the final reasoning trace and bounded evidence cards. Unknown evidence keys are removed, and a suggestion left without a valid table or library citation becomes an abstention. When personal and base evidence conflict, synthesis follows the base evidence and says so. The draggable review dialog shows same-table references and inert library-evidence Markdown (no links or images) separately; users accept entries individually, and nothing is written automatically. Accepting a suggestion uses the normal cell update with `expected_before=""` and `origin="llm_complete"`, so a cell filled while the suggestion was being prepared is never overwritten and normal history and synchronization continue to apply. Both `reasoning_agent` and `knowhow_complete` must be configured and treat evidence as untrusted data through system-level instructions. Invalid reasoning responses, unavailable providers, retrieval/synthesis failures, and unparseable or unusable top-level synthesis responses return an explicit failure; malformed individual suggestions are filtered, downgraded, or converted to abstentions. No path returns a table-only or fabricated offline substitute.

Ask citations that resolve to a knowhow cell jump straight to that row's detail drawer instead of the generic source view. A notebook's deep copy carries knowhow tables over in full — every table, column, row, cell, and code attachment gets a remapped id in the copy — without re-running embeddings, since cell text that didn't change keeps its existing vectors.

The external-Agent surface (HTTP + MCP, discrimination sets, code attachments) is documented under [Memory and Agent MCP](#memory-and-agent-mcp); the HTTP paths are listed under [APIs](#apis).

## Memory and Agent MCP

Memory is manual opt-in, creator-private, and always bound to exactly one notebook. From
an Ask answer, choose **Save to Memory**: the backend prepares a title/body/tag preview,
the user may edit it, and only the final confirmation writes a `confirmed` Memory. If the
preview model is unavailable or fails, the preview deterministically uses the question as
the title and the answer with display citations removed. When that Memory's notebook
already extracts a knowledge graph (the same eligibility gate as uploaded sources) and is
not a base library, confirmation — and the Save-to-Memory dialog — shows a default-on
checkbox that also ingests the confirmed Memory into that notebook's own KG through the
same extraction pipeline as an upload, recorded as a hidden synthetic source that never
appears in user-facing source lists or counts; it can be unchecked per confirmation, and
base libraries reach the KG only through the promotion review below. The global Memory page aggregates
only the signed-in user's records; a notebook's count and Memory tab are the same data
filtered to that notebook. Its owner-wide total and pending counts do not change when
status, search, or notebook filters change; the notebook selector comes from a bounded
owner aggregate rather than per-notebook queries.

The lifecycle is `candidate | confirmed | rejected | deprecated`. An Agent can create only
`candidate`; all authorized Agent profiles belonging to the same user and selected notebook
may retrieve it when the token includes `memory:read_candidates`. A candidate is never
returned by formal notebook Ask, notebook search, Deep Report, or
`search_notebook_context`. Confirmation moves it into that formal plane. Rejected and
deprecated records are excluded from both planes. Retrieval first requires relevance;
authority only resolves equally relevant/conflicting evidence in this order:
`candidate < personal source < confirmed Memory < base KG/base source`.

Candidate provenance snapshots the creating Agent profile id/name and every submitted
evidence reference, but never the bearer token. The server validates each reference against
the candidate's owner and notebook and records a per-reference `validated` or `invalid`
result with a bounded reason. Legacy/unverified and invalid references remain visible to the
owner but are never marked trusted or eligible promotion evidence. Candidate review and
provenance remain owner-only. Saving an Ask answer rechecks live owner/member access inside
the same `BEGIN IMMEDIATE` transaction that writes the Memory, revision, and provenance, so
a concurrent share revocation cannot leave a partial Memory.

Memory inputs are normalized and fail closed at both API and service boundaries. Titles and
content must be nonblank after trimming. Current caps are: title 80 characters, content
40,000 characters, at most 20 tags of 80 characters each, review/candidate reason 1,000
characters, task context 8,192 serialized UTF-8 bytes, at most 50 evidence references and
32,768 serialized UTF-8 bytes, and client request id 200 characters. HTTP validation errors
return 422; MCP/internal calls use the same service validation. Nested NaN or positive/negative
infinity is rejected before persistence, while legitimate JSON null round-trips unchanged.
The MCP proposal envelope uses these exact Core limits and does not impose narrower duplicates.
The raw tag list is capped before trimming/deduplication, and blank tags are rejected.

The Memory page's **Agent access** area creates stable Agent profiles and one-time plaintext
tokens. A token has an expiry, a default notebook, a notebook allowlist, and the smallest
needed subset of `knowledge:read`, `memory:read`, `memory:read_candidates`,
`memory:propose`, `ask:execute`, and `knowhow:code`; it can be revoked immediately. Install the backend
requirements (which include the official `mcp>=1.26.0` client/server SDK), start the backend,
then connect to the Streamable HTTP server at `/mcp` (`/mcp/` is handled through redirect).
By default MCP allows remote plain HTTP and relaxes Host/Origin (DNS-rebinding)
checks — intended for a trusted private network — and prints a startup warning
because the Agent token then travels in cleartext. On any public deployment set
`MCP_REQUIRE_HTTPS=1` to enforce HTTPS (and restore Host/Origin validation), and
set `MCP_PUBLIC_URL` to the public HTTPS `/mcp` URL.
Expiry values must include an explicit timezone offset; the browser converts its local
datetime input to UTC and the backend stores a normalized UTC instant. Naive datetimes are
rejected rather than interpreted in the server's local timezone.

For Codex, place the issued token in an environment variable and register the server:

```bash
export SILICON_NOTEBOOK_AGENT_TOKEN='<one-time-issued-token>'
codex mcp add silicon-notebook --url http://127.0.0.1:8000/mcp \
  --bearer-token-env-var SILICON_NOTEBOOK_AGENT_TOKEN
```

For Claude Code, the currently installed CLI accepts an HTTP transport and an explicit
Authorization header:

```bash
claude mcp add --transport http silicon-notebook http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <one-time-issued-token>"
```

Claude Code may persist that raw header in its local configuration. Use least-privilege
scopes, a short expiry, protect the local config, and revoke/rotate the token after use.
Do not assume shell environment interpolation in that header.

Every new MCP session must call `select_notebook` before a data tool. The exact tool set is:
`list_notebooks`, `select_notebook`, `search_agent_memory`,
`search_notebook_context`, `get_memory`, `ask_notebook`, `propose_memory`,
`list_knowhow_tables`, `get_knowhow_discrimination`, `get_knowhow_row`, and
`put_knowhow_cell_code`.
The server rechecks scope, allowlist, token state, and notebook access on data calls;
retrieved text is untrusted evidence, not executable Agent instructions.

The four knowhow tools mirror the HTTP surface at `/api/agent/knowhow/...` (see
[APIs](#apis)) through the same service functions, so HTTP and MCP never drift on
response shape. `list_knowhow_tables`, `get_knowhow_discrimination`, and
`get_knowhow_row` need `knowledge:read`. `get_knowhow_discrimination` returns, for a
table with a row-title column (400 otherwise), every row's title plus each
procedure-kind column's `{column_id, column_name, text, code_status}` — enough for an
Agent to run its own discrimination logic and pick which fix applies. `get_knowhow_row`
returns one row's full cell text (`steps`/`items` for procedure/entity columns) plus
that row's **code attachments** in full. A code attachment is code an external Agent
already wrote for one cell's method — never generated or executed by the notebook, and
never embedded/chunked/indexed into any KG projection — whose freshness (`implemented`
/ `stale` / `none`) is derived at read time from a content hash of the cell; the
discrimination set carries only that status, never the code body, to stay small.
Reading code still only needs `knowledge:read` — only writing it
(`put_knowhow_cell_code`, and the mirrored HTTP `PUT`/`DELETE .../code`) needs
`knowhow:code`, so a token that must read existing code before writing a new version
needs both scopes.

Only a `confirmed` Memory can be proposed for KG promotion. The creator proposes it; the
admin queue shows sanitized extraction candidates and server-validated evidence, not a raw
Memory revision/provenance browser. The proposal pins the exact source revision, sanitized
candidate snapshot, and validated evidence shown to the reviewer. Editing or deprecating a
proposed Memory atomically supersedes that proposal and resets its promotion state; edits can
then be re-proposed. The current provenance clears the proposal pointer; the pinned proposal remains
only in snapshot and queue history. Approval revalidates the Memory's current confirmed status and creator access,
plus the pinned revision and notebook binding, in the write transaction before reusing KG dedupe/merge to create or merge one
or more Base KG objects. Approval/rejection records the authenticated admin reviewer; the API
and promotion audit record the complete `base_object_ids` result. This does not change or
expose the private Memory row.
Deleting a notebook cascades all members' private Memory bound to it, so the delete dialog
warns about that lifecycle consequence without exposing member identities or counts.

The committed deterministic Memory evaluation reports Recall@5, MRR, nDCG, and three
zero-tolerance counters: candidate-to-formal-plane leakage, cross-user leakage, and
cross-notebook leakage. The A/B harness compares no-Memory, KB-only, and
KB+confirmed-Memory retrieval.

## KG extraction trigger

Chunk-native retrieval is ready as soon as a source is parsed + embedded, so **KG extraction is opt-in per notebook** rather than run on every upload:

| Notebook state on upload | KG extraction | How it happens |
|---|---|---|
| No KG yet (fresh notebook) | **Not** auto-run | Build on demand: `POST /api/notebooks/{id}/kg/build` (UI: a notebook's **构建知识图谱 / Build KG** action; also surfaced when you pick the **深入分析** group — the `strict` modes `reasoning` / `graph` — on a KG-less notebook) |
| Already has a KG | **Auto-run** in the background for each new source | No manual trigger — keeps the KG complete; the new source is then incrementally fused into the unified cross-document KG |

The ingest-time decision is `KG_AUTO_EXTRACT or notebook-already-has-KG`:

- `KG_AUTO_EXTRACT` (default `false`) — when `true`, every upload extracts KG for **all** notebooks.
- Otherwise extraction runs on upload only if the notebook already contains KG objects.

So you **opt in once** (build the KG, or set `KG_AUTO_EXTRACT=true`); after that, new documents are auto-extracted and fused. Re-extract a whole notebook from scratch with `POST /api/notebooks/{id}/kg/rebuild`. For bulk/offline builds, see [Offline batch ingestion](./operations.md#offline-batch-ingestion-directory--kg).

### KG build failure isolation

Manual notebook builds/rebuilds create a durable, task-scoped `kg_build_jobs`
row and allow only one running KG task per notebook. The notebook and index
status APIs expose `probing → extracting → stopping → finished`, source counts,
and a safe user-facing failure message; the frontend shows the same state after
refresh and offers **继续分析未完成内容** after a failure.

An interrupted task settles into that same failed state: a Ctrl-C or termination
signal on an offline batch run stops in-flight windows cooperatively, drains them
before the task settles (the guard is released with that row), and records
`worker_interrupted`, so a killed run never leaves a notebook displaying an
analysis that never finishes. Only an uncatchable end (SIGKILL, OOM kill, power
loss) leaves the row in progress, and that case is settled by startup recovery.

Each KG model request uses `KG_LLM_TIMEOUT_SECONDS` (default `60`) and at most
`KG_LLM_MAX_RETRIES` retries (default `2`, allowed `0..3`). If transient
unavailability persists, or the service rejects/authenticates the request
permanently, the shared control for that one job stops new requests, cancels
queued source/window work, publishes `stopping` from the first failing window
before either window- or source-level draining, and then drains calls already
in flight. Other notebooks and later tasks are unaffected. The availability
probe explicitly bypasses the LLM response cache and does not populate it, so a
stale successful probe cannot authorize destructive rebuild work during a live
outage.

Completed sources remain committed; an interrupted source does not persist a
partial extraction: object/relation chunks for one source share one SQLite
transaction, and a legacy leftover graph whose latest extraction run failed is
still classified as unfinished. A later normal build analyzes only unfinished
sources. Explicit rebuild is the only action that clears existing KG data, and
it probes the model before deleting anything. If the process restarts with a
running job, startup recovery marks that job and its running extraction rows
failed and restores every orphan `extracting` source to `parsed`, including a
source interrupted before its extraction-run row was created. Completing or
failing an extraction run also invalidates that notebook's pending-source memo,
so a status poll cannot keep reporting a completed source as unfinished.

The frontend guards start responses by notebook, workspace epoch, and request
epoch, and keeps polling while the durable job remains `running`; it does not
invent local completion after a fixed time cap. Safe structured events cover
`kg_build_started`, `kg_build_progress`, `kg_build_circuit_opened`,
`kg_build_stopping`, `kg_build_succeeded`, and `kg_build_failed` without
provider diagnostics, prompts, source text, tokens, or credentials.

## Retrieval modes (Ask)

`POST /ask` dispatches on `mode` — the registry `backend/app/services/ask_modes.py` is the single source of truth (default `chunk`). Federation is path-specific: baseline `chunk` is active-notebook-only; its optional KG overlay/PPR can add federated KG context and base-backed chunks; `graph` and `reasoning` use federated KG paths. Knowledge-object hits from `federated_retrieve()` keep tier-blind scores and use `base` only as the secondary key on an exact tie; `federated_retrieve_relations()` remains score-only. These ordering signals never feed grounding thresholds.

| Mode | Group | Needs KG | One-liner |
|------|-------|----------|-----------|
| **`chunk`** (default) | general | no | Chunk-native general Q&A: large recall → selection → long-context synthesis → citations bound to source chunks. |
| **`graph`** | strict | yes | Single-pass Personalized-PageRank propagation across the cross-document knowledge graph. |
| **`reasoning`** | strict | yes | Agentic, iterative plan → retrieve → reflect → answer (streams a live trace). |

### Ids vs. display names

The ids above (`chunk` / `reasoning` / `graph`, and the group ids `general` / `strict`) are the **protocol**: they are what `POST /ask` accepts, what persisted sessions and bookmarks store, and what the backend registry `backend/app/services/ask_modes.py` declares. They are stable and are not renamed for cosmetic reasons.

What the Ask panel *shows* is a separate, UI-only layer owned by the front-end registry `frontend/app/ask-modes.ts`:

| Protocol id | Ask-panel display name |
|---|---|
| `chunk` | 通用问答 |
| group `strict` (what the picker offers; its default engine is `reasoning`) | 深入分析 |
| `reasoning` | 逐步推理 |
| `graph` | 关联追溯 |

`groupLabel()` / `modeLabel()` in that registry are the only read path: no other front-end file may hardcode a display name, and prose that mentions one interpolates it. `ask-modes.test.mjs` enforces both halves — it recursively scans `frontend/app` and fails if a current display name appears outside the registry, or if a retired name (严格推理 / 深挖推理 / 图谱多跳) reappears. Renaming a display name is therefore a one-line registry edit that changes no id, request/response payload, or stored session; `scripts/check_ask_modes_contract.py` separately pins the id set across the two stacks.

**`chunk` — chunk-native, with optional chunk×graph mix.**
- *Baseline:* large chunk recall (`CHUNK_RECALL`) → MMR / multi-sub-query quota diversity selection (`CHUNK_MMR_K`) → long-context synthesis. The KG is not touched.
- *Mix* (active only when `CHUNK_KG_OVERLAY_ENABLED=true` **and** qwen3-rerank is configured **and** a KG is available): three sources are pooled — (a) vector chunks, (b) the KG local structure around the query seeds (entities + their 1-hop relations, retrieved once), (c) the source chunks behind those KG objects — round-robin merged, reranked by a qwen3 cross-encoder, then packed to a token budget (`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`). The answer cites chunks and KG items in one unified `[k]` map, and grounding spans chunk ∪ KG. When rerank is unconfigured or no KG exists, it falls back byte-for-byte to the baseline. (Faithful to LightRAG's `mix` mode.)

**`graph` — PPR over the cross-document KG.** Seeds via `federated_retrieve` (KG entities + their source chunks; optionally fused with relation-index hits when `RELATION_RETRIEVAL_ENABLED=true`) become the personalization vector for HippoRAG-style **Personalized PageRank** (`GRAPH_PPR_ENABLED`, on by default), which propagates relevance across documents through the shared knowledge graph; the top-ranked chunks feed a grounded answer whose `[k]` anchors point at KG objects/relations. With `GRAPH_PPR_ENABLED=false` it falls back to bounded BFS along reasoning edges.

**`reasoning` — intent-first agentic deep retrieval.** The official UI first calls `/ask/intent`. This corpus-blind preflight may use up to the latest five user questions, never corpus-derived assistant answers, but it does not create a durable conversation/job. Clear requests auto-confirm; blocking ambiguity opens an inline review. `/ask` and `/ask/stream` accept the reviewed `intent` alongside the original `question`; the backend deterministically freezes it and builds one authoritative internal research question used by Memory retrieval, PPR, evidence retrieval, and answer synthesis. Its approved retrieval directions directly seed the initial subqueries, bypassing the old second planning pass; reflection can add evidence-driven queries but cannot replace the contract. The response persists the confirmed `intent`, exposes the internal `retrieval_query`, and starts the engine trace with `intent` before retrieval while the saved user turn remains the original wording. Direct compatibility callers that omit `intent` retain the old clear-question path, but deterministic unresolved/generic ambiguity fails closed. The remaining loop delegates to `ReasoningRetriever`: retrieve (using the same PPR propagation as `graph`), reflect on sufficiency, and expand the graph or add subqueries until answerable, with live `reasoning_trace` over the NDJSON stream (`/ask/stream`). For explicit derivation questions it may call `follow_chain`: two bounded adjacency samples reuse the existing source/target indexes, then deterministically check types, status, review, evidence, and `validity_scope`. The two stored relations remain citable premises; `A→C` is only a query-time conclusion marked as an inference. If a high-degree sample is truncated and cannot prove the absence of a direct edge, it abstains. Strict / KG-grounded.

### Reasoning effort and complete collection requests

The grade is picked in the Ask composer through the same graded-effort control as a deep report's research depth — one shared component, so the two never drift apart: a chip carrying the current grade name, opening a slider popover that shows that grade plus one neutral sentence about it. The interface deliberately exposes only the grade name and that sentence; the exact ceilings live in this table (mirrored by `frontend/app/ask-retrieval-effort.ts` and `backend/app/core/ask_retrieval_policy.py`) rather than being printed onto the control.

Reasoning Ask accepts the stable `retrieval_effort` ids below; the default is `standard`. The model may stop before a ceiling when evidence is sufficient, but it may not raise a ceiling. “Final floor/aspect/cap” means `min(cap, max(floor, aspect × executed query count))`. The context values are hard evidence-character ceilings: the source partition contains structured preview, chunks, and direct source elements; the KG partition contains KG objects/relations, confirmed Memory, and query-time chains. Their combined evidence block cannot exceed the sum of the two values.

| Effort id | UI label | Per-query ranked take | Final floor / aspect / cap | Max reasoning steps / initial subqueries | KG / chunk context characters |
|---|---|---:|---:|---:|---:|
| `overview` | 概览 | 4 | 8 / 2 / 12 | 4 / 2 | 4,000 / 12,000 |
| `standard` | 标准 | 8 | 20 / 3 / 36 | 8 / 5 | 6,000 / 30,000 |
| `deep` | 深入 | 8 | 24 / 4 / 48 | 16 / 6 | 8,000 / 50,000 |
| `thorough` | 详尽 | 12 | 32 / 5 / 64 | 32 / 8 | 12,000 / 80,000 |
| `exhaustive` | 穷尽 | 16 | 40 / 6 / 96 | 50 / 10 | 16,000 / 120,000 |

Candidate generation does not grow with effort; deployment settings control it independently. `CHUNK_RECALL` defaults to **200** and separately bounds each indexed Chunk/KG ANN and lexical window (at most 400 identities before deduplication at the default). `RELATION_RECALL` defaults to **200** and separately bounds Relation ANN and the total lexically expanded relation-id window (at most 400 before deduplication at the default); source and target directions still receive reserved shares inside that lexical total. Changing either deployment setting changes the effective candidate window, so the UI does not present those defaults as request-level hard ceilings.

Intent preflight classifies result scope as `ranked`, `complete`, `aggregate`, or `hybrid`, with an explicit completeness flag; confirmation deterministically recomputes that scope from both the final edited wording and authoritative clarification answers. A request such as “list all methods in this Knowhow table” does not turn into a larger relevance Top-N. The deterministic executor admits only semantics it can prove from physical rows: a direct whole-table row/method list or a direct count of rows/records, optionally followed by analysis. Conditional subsets, distinct/type counts such as “how many kinds”, and grouped aggregation currently fall back to ranked retrieval with an explicit statement that exact completeness is unsupported. A supported 100-row table can return `100/100`. The shared enumeration safety contract is identical at every effort level:

| Complete-enumeration threshold | Exact limit |
|---|---:|
| Rows per cursor page | 25 |
| Pages per request | 50 |
| Physical rows scanned/returned per request | 1,250 |
| Tables per request | 8 |
| Selected columns per table | 8 |
| Cell excerpt available to the model | 1,000 characters |
| Structured result payload | 256,000 characters |
| Rows rendered inline in answer prose | 100 |
| Rows initially visible in each UI result card | 20 |

The 100-row inline ceiling is also the maximum row preview available to hybrid model synthesis; it does not delete rows from the authoritative structured result. Responses therefore separate per-table coverage, request/batch coverage (`selected_tables/known_tables`, returned/known rows), and hybrid synthesis coverage. For example, enumerating 200/200 rows while synthesizing 100/200 is “enumeration complete, analysis partial”. The 20-row initial UI view is presentation-only and remains expandable. A lightweight catalog query returns at most eight table descriptors and never hydrates cells, code attachments, or health payloads; it prioritizes an explicitly named table before applying that window, so a ninth-or-later table remains addressable. Aggregate counts and sequence sums still cover the whole notebook. Coverage says complete only after cursor exhaustion and stable before/after catalog values: projection `mutation_seq`, history-backed `enumeration_seq`, row count, column metadata, and selected/global scope. Every row add/delete records history in the same transaction, so an equal-count delete-then-add still changes `enumeration_seq`. Reaching the row/page/table/column/payload safety rail, or detecting a concurrent table change, produces `complete=false` with `explicit_partial` batch coverage and must never be worded as “all”; an individually exhausted selected table may remain complete when the overall batch omitted tables at the eight-table ceiling. Selecting a lower effort level does not reduce these explicit-completeness limits. This executor currently applies only to the direct physical-row Knowhow semantics above; other Knowhow semantics and complete sets of KG objects, source elements, Memory, or other collections disclose that complete enumeration is unsupported.

Retired ids `fast` and `global` are transparently remapped to `chunk` (old sessions/bookmarks never 422); any other unknown mode is rejected with HTTP 422.

## APIs

Key local beta APIs:

- `GET /api/notebooks`, `POST /api/notebooks`, `PATCH /api/notebooks/{id}`, `DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `GET /api/notebooks/{id}/analytics/content-overview` — viewer-aware content assets: `memory` (`total`, `confirmed`, `candidate`, up to three recent `id`/`title`/`status`/`updated_at`) and `knowhow` (`table_count`, `row_count`, `projection_pending`, `projection_failed`, `stale_code_count`, up to three recent table summaries)
- `GET /api/notebooks/{id}/checkup` — read-only pipeline health check (dashboard hot path): aggregates source/index damage-and-pending signals — empty source, missing retrieval segments, missing retrieval vectors, sources pending analysis, stale/corrupt retrieval index — each with a count, a bounded sample, and a suggested repair action; all zero when healthy. Consumed by the dashboard's source-status and index blocks plus the bell; a healthy notebook stays neutral and undisturbed.
- `POST /api/notebooks/{id}/sources/reparse` — checkup repair: batch re-parse the given sources (empty/missing-segment damage), scheduled through the existing pipeline in the background, filtered to the notebook scope.
- `POST /api/notebooks/{id}/backfill-vectors` — checkup repair: backfill the notebook's missing retrieval vectors in the background (missing-only, idempotent, embedding-only — never re-parses).
- `POST /api/notebooks/{id}/sources` — multipart file upload (async parse/extract)
- `GET /api/sources/{id}`, `DELETE /api/sources/{id}`, `POST /api/sources/{id}/parse`, `GET /api/sources/{id}/elements`
- `GET /api/notebooks/{id}/knowledge-types`, `GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`, `PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- Knowhow tables: `GET|POST /api/notebooks/{id}/knowhow`, `GET|PATCH|DELETE .../knowhow/{table_id}`, `POST .../knowhow/{table_id}/reproject` — plus import (`POST .../knowhow/import/preview`, `POST .../knowhow/import`), column/row/cell editing (`POST .../knowhow/{table_id}/columns`, `PATCH|DELETE .../columns/{column_id}`, `POST .../knowhow/{table_id}/rows`, `DELETE .../rows/{row_id}`, `PATCH .../rows/{row_id}/cells/{column_id}`), the Excel template round-trip (`GET .../knowhow/{table_id}/template`, `POST .../knowhow/{table_id}/append` with `mode=preview|commit`), an explicit suggestion-only wording rewrite (`POST .../rows/{row_id}/cells/{column_id}/optimize`), and reasoning-backed row completion suggestions (`POST .../knowhow/{table_id}/rows/{row_id}/complete`, optional `target_column_ids`, response `retrieval_mode` + `retrieval_scope` + `retrieval_status` + `reasoning_trace` + `evidence` + `suggestions`)
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask/intent` — corpus-blind `reasoning` intent preview; accepts `{question, conversation_id?}`, reads at most the latest five user questions, creates no conversation/job, returns the editable intent contract plus any blocking ambiguity, and signals the model cancellation event when its client disconnects
- `POST /api/notebooks/{id}/ask` — grounded Q&A with `[k_i]` citations (`mode`: `chunk` default | `graph` | `reasoning`; `reasoning` accepts `retrieval_effort`, default `standard`; collection-aware responses may include structured `result_sets` plus exact coverage; federation follows the mode-specific boundaries above)
- `POST /api/notebooks/{id}/ask/stream` — NDJSON Ask progress stream (first a `started` event with the durable `job_id` and `conversation_id`, then progress/final events). The frontend uses that conversation id to publish/reopen the in-flight session before an answer exists. A transport disconnect stops delivery to that client only; the detached job keeps running and can persist its answer
- `GET /api/notebooks/{id}/ask/jobs/{job_id}` — read detached Ask job `status`, `trace`, and `answer_id`; the job must belong to the path notebook and current user; on `done`, the frontend reloads the conversation to obtain the final `AskResponse`
- `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel` — explicit interrupt endpoint; the job must belong to the path notebook and current user; sets the cancellation event and stops the worker before a cancelled final answer is saved
- `GET /api/notebooks/{id}/conversations`, `GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- Memory: `GET /api/memories`, `GET /api/notebooks/{id}/memories`, `GET|PATCH /api/memories/{memory_id}`, `POST /api/memories/{memory_id}/confirm|reject|deprecate|promote`, `POST /api/answers/{answer_id}/memory-preview`, `POST /api/notebooks/{id}/memories/from-answer`
- Agent access: `GET|POST /api/agent-profiles`, `PATCH /api/agent-profiles/{profile_id}`, `POST /api/agent-profiles/{profile_id}/tokens`, `GET /api/agent-tokens`, `DELETE /api/agent-tokens/{token_id}`; Streamable HTTP MCP is mounted at `/mcp`
- Knowhow agent surface: `GET /api/agent/knowhow/tables?notebook_id=`, `GET /api/agent/knowhow/tables/{table_id}/discrimination`, `GET /api/agent/knowhow/rows/{row_id}`, `GET|PUT|DELETE /api/agent/knowhow/rows/{row_id}/cells/{column_id}/code` — reachable by either a signed-in session or an Agent Bearer token; reads need `knowledge:read`, code writes need `knowhow:code` (see [Memory and Agent MCP](#memory-and-agent-mcp))
- Unified KG: `POST .../unified-kg/rebuild`, `GET .../unified-kg`, `GET .../unified-kg/pending-merges`, `POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`, `GET .../objects/{object_id}/context`
- `GET /api/object-schemas`, `POST /api/object-schemas`, `PATCH /api/object-schemas/{type}`, `DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`, `POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- Two-tier: `POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → returns the updated `NotebookSummary` (400 on bad tier, 404 on missing notebook). Sets the notebook's federation tier (base = publishable as a public knowledge base, personal = default user notes); a `base` notebook only participates in another notebook's retrieval once that notebook explicitly mounts it (`GET`/`PUT /api/notebooks/{id}/bases`, candidates via `GET /api/notebooks/{id}/mountable`).
- Reference-library mounts: `GET /api/notebooks/{id}/bases` → `MountedBase[]` (this notebook's mount edges, including greyed-out inactive ones); `PUT /api/notebooks/{id}/bases` body `{base_notebook_ids}` → full replace, returns the updated `MountedBase[]` (400 if any id is outside the mountable candidate set; owner-only); `GET /api/notebooks/{id}/mountable` → `NotebookRef[]` (mountable candidates: every public knowledge base plus this notebook's own same-owner libraries).
- Edge trust & curation: `GET /api/notebooks/{id}/edge-review-queue`, `POST /api/notebooks/{id}/relations/{rel_id}/review`
- Governance / promotion: `POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`, `GET /api/promotion-queue`, `POST /api/promotion-queue/{candidate_id}/approve|reject`
- Deep report (two-phase): `POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`; performs corpus-blind understanding and always stops at `status=intent_ready`. `GET /api/notebooks/{id}/reports/{rid}` exposes durable `understanding` plus status/progress. `POST .../reports/{rid}/intent` body `{resolved_question, answers:[{id,answer}]}` validates every required ambiguity and atomically claims the only transition into corpus-backed planning; it returns `{status:"planning"}`, while a duplicate/stale confirmation returns 409 without launching another job. Planning then stops at `outline_ready`, or proceeds directly to generation when the original request had `auto_generate=true`. The enriched `outline` carries per-section `intent_ids`, `intent_questions`, editable `sub_queries`, objective `coverage`, perspectives / tensions / sufficiency; `content_md` and live `section_status` remain on detail. `PATCH .../reports/{rid}/outline` body `{sections}` edits the draft only while `outline_ready`; it preserves the server intent catalog, accepts at most `REPORT_MAX_SECTIONS`, retains at most four nonblank retrieval directions per section, and returns 422 if there is no valid section or a mandatory intent loses its final section binding. `POST .../reports/{rid}/generate` body `{depth?}` launches **phase-2 generation** (only from `outline_ready`, else 409). Generated sections include backend-derived `evidence_level`/`grounded`; references can carry exact `source_id`/`element_id` metadata. Also `GET /reports` (list), `POST .../cancel`, `DELETE`, `POST .../reports/export` body `{report_ids}` → `reports.zip`. Sections deep-dive in parallel up to `KG_JOB_CONCURRENCY`.

The current persistence/API contract is the `reports` table and `/reports` APIs; retired content-studio storage and routes are not part of the current runtime.

## Current Limitations

- Retrieval on SQLite uses keyword/FTS-compatible CJK handling plus a bounded float32 matrix/scale index. PostgreSQL uses `pg_trgm`/`ILIKE` and the same byte-oriented float32 vectors behind repository ports; pgvector remains a future scale option, not a runtime prerequisite.
- Large-document ingestion is hardened: greedy-window KG extraction (cost scales linearly with document size), concurrent embedding with per-batch DB writes, and extraction-first pipeline. For very large corpora, adding `sqlite-vec` is a natural next step.
- Ask no longer performs synchronous embedding backfill or a full source-element scan; it uses available keyword/vector indexes and stays responsive while maintenance jobs run. Ask emits per-stage timing (`ask_stage` events).
- Unified KG rebuild is explicit and observable via `GET /notebooks/{id}/unified-kg/status`; ingesting a source marks the graph dirty instead of rebuilding synchronously, and opening the graph overlay no longer auto-rebuilds (refresh on demand).
- Cross-document concept merge uses deterministic alias normalization plus bounded top-k vector candidates (scales past thousands of concepts); optional LLM pre-review (`POST /notebooks/{id}/unified-kg/merges/review`) confirms/rejects high-confidence near-synonym merges in small batches.
- LLM-backed KG extraction requires the `kg_extract` workload to be bound in the system model-service TOML; offline smoke tests seed KG objects explicitly when retrieval/governance assertions are needed.
- Two-tier and deep reasoning are early: the graph-reasoning Ask mode (`mode="graph"`) is opt-in/experimental (the Ask panel toggle still drives the default `chunk`/`reasoning` paths). Marking a notebook `base`/`personal` (via `POST /notebooks/{id}/tier`), the edge-trust review queue, and promotion (personal→base) all now have dedicated front-end controls in the analysis toolbar; publishing a notebook as a public knowledge base only makes it mountable — tier-aware federation and the base-wins conflict rule activate only for notebooks that explicitly mount it as a reference library.
- Notebook sharing is link-based copy/read-only membership, not live collaborative editing; owners retain write authority.
- SQLite/PostgreSQL selection is direct and atomic through the repository factory. Changing `DATABASE_URL` does not synchronize existing rows; cutover and rollback therefore require stopped writers, verified backups, an external data migration when data already exists, and post-start consistency checks.
- The `off`-mode PDF fallback uses pypdf layout extraction (decent reading order, no new deps) — formulas, tables, and scanned/image PDFs still need MinerU; see [PDF parsing with MinerU](./operations.md#pdf-parsing-with-mineru).
- User memory remains manual opt-in only; no automatic memory behavior has been added.
