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
- Multipart source upload for PDF, Markdown, Markdown ZIP bundles, DOCX, PPTX, CSV, XLSX, and legacy binary XLS (async through the shared KG job scheduler). The add-source dialog middle-compacts overlong staged filenames while retaining the ending/extension and full-name tooltip. Drag-drop and the file picker share one staging path; files skipped at staging time (unsupported type — with a save-as hint for legacy Office formats, over the per-file size cap, or over the batch cap) are listed persistently inside the dialog with per-file reasons instead of being silently filtered by the browser's `accept` attribute or flashed in a transient toast. It includes the whole staged batch in the effective document-limit check: a batch larger than the remaining allowance is disabled before submission and accompanied by the remaining/excess counts and an actionable remedy.
- **KG-native ingestion**: structured Markdown parse → greedy-window KG extraction (Concept / Claim / Formula / Procedure) with concurrent embedding → extraction-first status (`extracted` = KG ready, does not wait for embedding)
- PDF/DOCX/PPTX/XLSX parsing via MinerU (formulas as LaTeX, tables, layout, embedded images) when configured; each of those formats falls back to its local library parser when MinerU is unavailable or returns nothing usable. Each local chain degrades in stages rather than dropping straight to the crudest extractor: PDF uses PyMuPDF4LLM layout-aware Markdown with pypdf as the last resort; DOCX uses mammoth, whose semantic HTML preserves heading levels, list markers and table structure (with `table_html` kept in metadata), and keeps flat python-docx paragraph/row extraction as the last resort; PPTX uses python-pptx, which also recovers slide tables, chart titles, grouped shapes and speaker notes that the raw slide-XML extractor dropped entirely, and keeps that raw-XML extractor as the last resort; XLSX/XLSM uses openpyxl. Inline DOCX images and PPTX pictures are not persisted by the fallbacks (a downloaded URL source may be a short-lived temp file); mammoth's base64 data URIs are discarded rather than inlined into element text. That degradation is disclosed for every *lossy* fallback — PDF, DOCX and PPTX, not only PDF — while workbooks are deliberately excluded because the openpyxl fallback keeps every cell value (warning there is noise a user could never clear on a MinerU that cannot parse workbooks). A non-empty MinerU workbook result is additionally accepted only after a local, model-free coverage reconciliation against the workbook's own non-empty row **and cell** counts; an under-covering result on *either* dimension is discarded in full for openpyxl. Both dimensions are required because counting rows alone misses the common wide-table failure where MinerU keeps every `<tr>` but drops the columns past the rendered page width; non-table text elements contribute at most one row and one cell each, and image blocks contribute to neither numerator. The acceptance threshold is 0.8, shared by both dimensions (`MINERU_WORKBOOK_MIN_ROW_COVERAGE` in `backend/app/services/parsers.py`; this section is the only place the number is registered). The same reconciliation gates the cloud-upload path for workbooks, and embedded images are persisted only after a result has been accepted, so a rejected result never leaves orphaned assets behind. When a warning is raised, the source stays `extracted` and clients receive only `parse_quality_warning`. Legacy binary `.xls` (pre-OOXML BIFF) has no MinerU path at all — MinerU cannot parse that container — so it always goes straight to `xlrd`, the only pure-Python reader for the format; other legacy binary Office formats (`.doc`, `.ppt`) remain unsupported and the UI directs users to save as `.docx`/`.pptx`.
- MinerU-extracted embedded images are retained and shown inline in the source view; captions and text remain searchable. **Markdown sources' embedded images share the same guardrails**: an `![alt](src)` with alt text writes that alt as the element's `metadata.caption`, which enters retrieval on the same footing as a MinerU-parsed PDF caption (indexed into chunks); an alt-less plain-path image still produces no element (unless an image-description block follows it, see below). When `src` is a `data:image/{png,jpeg,gif,webp};base64,...` URI (the same mime allowlist MinerU's image assets use — unrecognised mimes such as svg/bmp/avif never parse as an image — the `![alt](data:...)` literal is stripped down to just its alt text wherever it appears: alone in a paragraph, mixed with other paragraph text, or inside a list item, heading, or table cell, so the base64 payload never reaches element text), the image bytes are decoded and persisted as a source image asset, shown normally in source detail; per-image byte and per-source count ceilings reuse the MinerU image settings (`MINERU_MAX_IMAGE_BYTES`, default 5MB; `MINERU_MAX_IMAGES_PER_SOURCE`, default 200), and `MINERU_RETURN_IMAGES=false` likewise skips persistence — that switch now gates image persistence for every source, not only MinerU-parsed documents. The data URI itself never enters element metadata; a data-URI image with no alt that fails to persist and has no image-description block produces no element. Known boundary: only images occupying an entire paragraph are handled — an image embedded inside a list item or table cell keeps only its alt text and is never persisted as an asset. **Image-description blocks**: when a blockquote follows an image line (a blank line between them is optional — a blockquote interrupts a paragraph) and its first line is nothing but the `**图片描述**` marker, **every** quoted line in that blockquote is that image's description: the text folds into the image element's `metadata.description` and joins the caption to form the element text that enters chunking. **A described image is therefore retrievable even with no alt caption** (exported images usually have no alt, and the description block is their only entry point), and a citation's attached-image caption falls back to that description when there is no caption (truncated by the same caption cap). What the fold removes is only "the blockquote also becomes its own paragraph element" — one passage never occupies two retrieval slots; KG extraction still reads its verbatim source slice as an ordinary paragraph. Four shape rules keep an ordinary quote from being swallowed: the marker line must contain nothing but the marker (bold and a trailing colon are both optional, and text may follow it only after a colon — `> **图片描述**：text` — so prose like 「图片描述如下：……」 does not qualify); the blockquote must contain no fenced/indented code block and no HTML block (their content does not hang off inline nodes, so folding them would silently drop it), while lists, headings, tables and nested quotes **are** folded — the convention says "**every** quoted line", and image descriptions routinely use bullets; quoted text that still **renders** non-blank must follow the marker (a marker-only quote, or one holding just `<br>` or an empty-alt image, is left as-is); and nothing may sit between the image and the blockquote **in the source** (a link reference definition emits no token but is still content). A multi-paragraph description folds into **one** element and is therefore no longer split by the 600-char chunker — a very long description is embedded only up to the embedding truncation length (a registered trade-off; lexical retrieval is unaffected). Markdown referencing local image file paths can be pre-converted to data URIs with `scripts/embed_md_images.py` before upload (see the README's "Product Flow" section).
- **Markdown + image bundle upload**: `.zip` is a first-class built-in parser format. The browser uploads the raw archive unchanged; it is stored as one source and parsed in the background, independent of MinerU. Every `.md`/`.markdown` member is parsed in stable archive-path order and tagged with `metadata.bundle_path`. A local image target is URL-decoded, stripped of query/fragment suffixes, resolved relative to the Markdown member that references it (archive-root paths also work), then admitted by png/jpeg/gif/webp magic bytes and persisted through the ordinary source-image asset port. The archive is never extracted to the host filesystem. Unsafe or duplicate paths, encryption, unsupported compression, no Markdown member, too many retained files, corruption, and an aggregate uncompressed size over the ordinary per-source upload ceiling reject the source as a whole. A missing, remote, corrupt, or unsupported image is a per-image fail-open: its original `src` and caption/description text remain, so the image element is searchable when it has caption/description but renders without an `<img>`. The existing image-storage switch and per-image/per-source asset rails still apply at persistence time. A dropped **folder** remains the compatibility path through the dependency-free browser pipeline (`frontend/app/md-bundle.ts` / `bundle-intake.ts`): it resolves relative paths, magic-sniffs and data-URI-inlines supported images, and keeps its persistent five-category pairing receipt and deliberately conservative whole-paragraph/image-description checks. Exact backend-ZIP and browser-folder guardrails are registered in the [table below](#markdown-bundle-upload-guardrails).
- **Exact phrases (the user-facing search syntax)**: anything wrapped in **ASCII double quotes** is matched whole and never tokenized. In `什么是 "static timing analysis" 的原理` that span enters lexical candidate generation as one indivisible term (a quoted FTS5 term on SQLite, an escaped `ILIKE` substring on PostgreSQL) and counts as a **single** unit of keyword coverage — a document has to carry the whole phrase to score for it, so one that merely scatters `static`, `timing` and `analysis` earns nothing and ranks below one that does not. The span also earns an unconditional exact-locate probe (the exact-identifier channel described below), pulling in the whole section it heads. Quoting is a strong preference, not a hard filter: semantic retrieval still runs, and a document is never dropped merely for lacking the phrase. Scoring (keyword coverage and BM25/RRF ranking) normalizes whitespace, so a phrase the document breaks across a line still counts; candidate generation cannot — an FTS5 trigram phrase and an escaped `ILIKE` pattern are literal contiguous matches, so a document writing `static   timing\n analysis` will not be surfaced by that phrase alone (closing it needs a whitespace-normalized indexed column; an unindexed regex scan is the notebook-wide scan this layer forbids). The query's remaining terms and semantic recall still apply there. Three boundaries apply: ASCII quotes only (typographic `“…”` is ordinary quotation in Chinese prose, so honouring it would silently constrain a large share of existing questions), at least 3 characters inside the quotes (SQLite's trigram index cannot match anything shorter), and more than 4 DISTINCT quoted spans in one text disables the syntax for that text (that shape is machine material such as a JSON envelope, where quotes are punctuation rather than a constraint). Distinct rather than occurrences: the internal research query for reasoning and reports repeats one phrase across the objective, the normalized question and every mandatory topic. The ask composer and the deep-report input echo the recognised phrases — or why none were recognised — as you type, so a constraint that did not take effect never passes silently. Planning and reflection prompts gain one line about preserving quoted spans verbatim, and only when the question actually contains one, so model-rewritten sub-queries cannot split the phrase. The notebook search box is already a whole-string substring match, so the markers around RECOGNISED spans are dropped there (unrecognised quotes stay verbatim, so literal JSON/code remains searchable). Private Memory probes its candidates as one whole-string phrase, so each recognised span is OR-ed into that same bounded query as an independent probe — otherwise a memory holding the phrase but not the surrounding sentence could never become a candidate; scoring still uses the original query, so the phrase must match whole.
- Hybrid retrieval: CJK-aware bi-gram keyword + float32 semantic search with per-notebook caches. SQLite FTS5 keeps an exact-phrase bonus but also ORs safely quoted Latin/number terms, overlapping CJK trigrams, and separator-joined identifiers (`set_db`-style runs, bounded by a letter requirement, 4-char floor, and 16-term quota) as whole terms; PostgreSQL decomposes the same bounded terms before native trigram candidate generation and escapes LIKE metacharacters in its `ILIKE` arm, so an identifier such as `set_db` stays literal instead of widening into `setXdb`. Indexed Chunk/KG paths merge bounded ANN and lexical candidate windows; indexed Relation retrieval adds direction-balanced relations adjacent to lexically matched KG endpoints while preserving endpoint rank. Lexical-only candidates remain keyword-only during fusion rather than receiving a synthetic zero semantic score.
- Built-in relations share one directed endpoint contract across extraction and graph consumers. Historical rows that violate a core-type pair remain auditable but cannot affect graph/PPR/canonical/relation retrieval. Administrator-defined object types remain extensible for known edge ids. Optional cross-element completion advances bounded source-generation keyset pages using indexed same-source candidates, is double-verified, rollout-gated, and disabled by default; it never performs document- or book-wide full scans.
- KG-native grounded Q&A: sentence-level `[k_i]` citations (ASCII `[k1]` and localized `【k1】`, including comma-separated groups with ASCII or Chinese commas, share the same binding and render as compact numbered references; model-emitted numeric groups like `[1, 2, 3]` are also linked when they map to known references), multi-turn conversations, 1-hop KG neighbour expansion, and a live, expandable one-line agent trace for reasoning mode
- **Intent-first reasoning Ask:** before the official UI starts a `reasoning` job, `POST /api/notebooks/{id}/ask/intent` interprets the question without reading notebook/reference-library content. It may use the latest prior user questions, but never corpus-derived assistant answers, and creates neither a conversation nor a job. Clear intent auto-continues; because no human reviewed that normalization, the original wording stays the authoritative first retrieval seed and the model rewrite is supplemental. Direction-changing ambiguity pauses for confirmation, after which the reviewed wording is authoritative. The frozen topics/directions, entities, axes, constraints, exclusions, assumptions, expected output, and answers govern Memory, PPR, evidence retrieval, and synthesis. The whole authoritative question runs first, then confirmed directions are seeded round-robin so every mandatory topic gets a seed; directions that exceed the effort's first-round width are executed by a bounded coverage pass inside the same step budget, and any the budget cannot reach are disclosed in the trace and fed back into reflection rather than dropped. No second planner may replace them. Invalid confirmations return 422 before durable state is created, and aborting preflight signals the model call to cancel.
- **Typed query-time inference in reasoning mode:** the agent can call `follow_chain` to compose an evidence-backed two-hop `A→B→C` path into a transient `A→C` inference for `derived_from / kind_of / prerequisite_of / precedes / part_of`. Both direct hops remain independently citable relation evidence, rejected/ungrounded/scope-conflicting paths fail closed, the inferred conclusion is explicitly marked as reasoning, and no inferred edge is written back to the KG. The feature adds no migration, new index, or historical backfill; bounded samples use the existing source/target relation indexes and ambiguous high-degree paths are skipped.
- Two-tier knowledge base: each notebook has a `tier` (`base` | `personal`, default `personal`). Baseline `chunk` retrieval reads chunks from the active notebook only; optional KG overlay/PPR can add federated KG context and base-backed chunks, while `graph` and `reasoning` use federated KG paths. The exact-score `base` tie-break applies only to knowledge-object hits returned by `federated_retrieve()`: scores stay unchanged and a higher-scoring personal hit still wins. `federated_retrieve_relations()` remains score-only. Separately, when base and personal evidence contradict during answer synthesis, the answer defers to the base position and surfaces the discrepancy. Citations carry their tier (`AnswerAnchor.tier`) and Ask renders a `base`/`personal` badge per cited anchor.
- **User accounts**: self-service registration (username rule: a single letter + `00` + 6 digits, e.g. `a00123456`; stored lower-cased) + password login with opaque Bearer session tokens. Each notebook is owned by its creator; a user's library contains owned notebooks plus large shared notebooks they explicitly joined read-only. On first boot the built-in `admin` account is created (login `admin`, password from `SILICON_NOTEBOOK_ADMIN_PASSWORD`, local default `admin`; production/non-loopback startup requires changing it) and owns pre-existing notebooks. Administrators can grant or revoke the `admin` role from the user-usage page through `PATCH /api/admin/users/{user_id}/role`; the built-in administrator and the active administrator's own role cannot be revoked. Role changes are observed by existing sessions on their next request. The user-usage table sorts the complete `/api/admin/users` result before pagination, defaults to 20 rows per page, provides 20/50/100-row page sizes, and supports ascending/descending sorting from its data-column headers. Its `questions` value (also returned by `GET /api/admin/users/{user_id}/notebooks`) counts durable `ask_jobs` submissions owned by the target user, including failed or cancelled jobs; it does not count `conversations` containers, so repeated questions in one conversation remain distinct and another member's question is not charged to the notebook owner. The user total includes submissions in joined read-only notebooks. `GET /api/admin/users/{user_id}/notebooks` intentionally remains an owner-only notebook inventory, so its per-notebook question counts break down only the user's owned notebooks and need not sum to the user total. The per-user report count follows the same shape: the overview total counts reports by their creator (including reports created in shared notebooks), while the owner-only breakdown likewise need not sum to it. The legacy `conversations` field is retained and marked deprecated for API compatibility. Each notebook caps the number of user-uploaded documents (default 20, `USER_UPLOAD_DOCUMENT_LIMIT`); administrators tune it from the user-usage page — a global default (`PATCH /api/admin/settings/upload-limit-default`) plus per-user overrides (`PATCH /api/admin/users/{user_id}/upload-limit`, `null` clears the override and falls back to the global default). Administrator-owned notebooks are exempt. Any administrator can publish a notebook as a public knowledge base. Base notebooks are hidden from regular users' lists but are discoverable through each notebook's reference-library picker, and participate in retrieval only for notebooks that explicitly mount them. Upgrading an existing deployment to schema 20 does not backfill mounts: every pre-existing notebook starts with zero mounted reference libraries, and federation stays off for it until a user explicitly mounts one. Every non-built-in user can change their own password from the avatar menu through `PATCH /api/me/password` (body `{"old_password", "new_password"}`; a wrong current password or a blank new password returns 400): on success the requesting session stays signed in while every other browser session of that user is revoked. Administrators can reset a user's password from the user-usage page through `POST /api/admin/users/{user_id}/reset-password` (body `{"new_password"}`); all of the target's browser sessions are revoked so they must sign in with the new password, and the request must come from a real signed-in administrator session — the `auth_optional` anonymous fallback is refused (403). Agent long-lived credentials are outside both revocation scopes. The built-in `admin` account is rejected by both paths (409) because its password is re-seeded from `SILICON_NOTEBOOK_ADMIN_PASSWORD` on every startup — change the environment variable and restart instead; the UI hides the change-password entry for it and marks its user-usage row as protected. Set `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` for local/no-auth testing. The frontend shows a login/register gate on first load; the topbar displays the logged-in username and a logout button.
- **Share links**: owners can publish an opaque notebook link. Small notebooks are copied into the recipient's account; large notebooks are joined as read-only membership. Write access stays with the owner, and there is no live collaborative editing.
- **Group sharing**: users are organized into groups (`project` | `department` | `domain`) with a two-level in-group role, and a group admin shares a library with the whole group through an authorization edge. Group-shared libraries appear in a **群组** partition of the member's notebook list, members may ask questions, write their own deep reports and mount the library, and read access now implies mountability. See 群组知识共享 below.
- **Public report links**: one **finished** deep report can be published by a write-capable member as a sign-in-free read-only page (`POST /notebooks/{nb}/reports/{rid}/share` issues a token, `DELETE` revokes, `GET /public/reports/{token}` reads it anonymously). Issuing is idempotent — re-sharing returns the same token, so a link already handed out never starts 404ing; after revocation it is indistinguishable from a token that never existed. An unfinished report cannot be shared (409). The anonymous endpoint lives on a **separate router**: the main API router carries a router-level `Depends(get_current_user)`, which would 401 exactly the visitors this page exists for. That also means no request user is bound, so it may only call repository methods that do not consult the current user — `current_user` falls back to the seeded admin when the ContextVar is unset. The payload is an **allowlist**, not a redaction: body, question, timing, and per citation the title, original filename, location, and stored excerpt. `source_id`, `element_id`, `object_id`, `notebook_id`, and the whole `understanding` contract (intent and the frozen source scope) never cross — the public page cannot open sources anyway, so those handles would only enable probing of the authenticated API. Corpus-basis disclosure is already frozen into `content_md` at generation time. **The rendering pipeline is shared with the in-app view**: the body runs through the same `remarkCitations`, so `[k]` / 【k】 markers become clickable numbers taken from the number inside the key (the backend already renumbered globally; the public projection drops entries with neither title nor excerpt, so positional numbering would disagree with the body). Clicking one jumps to and highlights the matching entry in this page's citation list — the public page cannot open sources, so a marker leads to the excerpt, not the original. Tables and code blocks reuse `.answer-table-wrap` / `.answer-code` so wide content scrolls inside its own block, and the page must import `katex/dist/katex.min.css` itself: without it rehype-katex's MathML is never clipped and every formula renders twice, once as character-by-character MathML text. **Truncation is always disclosed, never silent**: the research question is served whole, and a reference title/original-filename/excerpt stays bounded but sets `title_truncated`/`file_name_truncated`/`snippet_truncated` when the cap bites. Exact bounds are in the [table below](#public-report-share-guardrails).
- **Public conversation links**: a **finished** multi-turn Ask conversation can be published by its creator as a sign-in-free read-only page at `/c/{token}`, mirroring Public report links end to end (`POST /notebooks/{nb}/conversations/{cid}/share` issues a token, `GET /notebooks/{nb}/conversations/{cid}/share` reads back the token/watermark for the creator, `DELETE /notebooks/{nb}/conversations/{cid}/share` revokes, `GET /public/conversations/{token}` reads it anonymously, `GET /public/conversations/{token}/assets/{alias}` serves a cited image) — the same separate anonymous router with no `Depends(get_current_user)`, the same row-level creator gate (`_own_conversation_or_404`; "exists but is not yours" is the same 404 as "does not exist"), the same allowlist shape, and the same shared rendering pipeline (`remarkCitations`, `.answer-table-wrap`/`.answer-code`, its own `katex/dist/katex.min.css` import). This entry registers only what the conversation shape adds on top. Issuing is idempotent **and** simultaneously advances the `shared_through_at`/`shared_through_id` watermark; the request body's `expected_through_id` (the newest answer the client saw in the same turns it computed its disclosure from) pins the watermark to **exactly that answer**, so a newer answer that landed between the client's disclosure read and this POST is **not** published — closing the disclosure TOCTOU — while a deleted disclosed boundary is refused (409, reload and re-review) and an empty/absent body falls back to the current latest answer; "share" and "update to the latest answer" are the same call. The public page renders only turns written before that watermark (an in-flight turn — a job with no answer row yet — is excluded by construction); the boundary is a **keyset** on the watermark answer's `(created_at, rowid/ordinal)` tuple (exact tie-break, so a same-instant answer that sorts *after* the watermark is not pulled in), falling back to the pure `created_at` interval only when `shared_through_id` no longer resolves. Sharing is refused for a blank-`created_by` conversation and for one with zero answers (409 either way; the latter rolls back the token it just issued). The payload additionally drops `reasoning_trace`, `intent`, `retrieval_scope`/`retrieval_query`, and every addressable id including `memory_id` — a cited Memory excerpt is still disclosed (a self-publish: the creator can only ever cite their own Memory), but the share dialog must state the excerpt count before the link goes out. A cited image resolves through a **per-token HMAC alias** (`conversation_asset_alias`, never the real `asset_id`), so revoking the link kills its images too and the same image behind two different conversation links stays unlinkable; the asset endpoint serves only assets referenced by that frozen snapshot and answers `Cache-Control: no-store`. Result-set cards (`result_sets`) are not projected in v1, but the count is disclosed rather than silently dropped (`PublicTurn.omitted_result_sets`). The dialog is reachable from two places, both issuing that same call: the share button on a session card in the Ask history popover publishes the whole conversation (boundary = its current last answer), and the share button in each answer's action row — placed after the copy button — publishes through that answer by sending its id as `expected_through_id`. The bounded mode adds three UI-side rules: the clicked id is sent verbatim and never degrades to an empty body when the conversation detail fails to load (an empty body publishes the current latest, i.e. more than the dialog promised); a boundary that sorts *before* the published watermark is refused by the advance-only store, so the dialog offers no publish action there and instead reports that the link already covers this answer plus N later turns, with revoke-then-reshare as the only way to narrow it; and in that same branch the disclosure counts are computed over the full turn list rather than the truncated one, since they describe what the live link exposes. Exact turn/reference/snippet/question/caption/alias bounds are in the [table below](#public-conversation-share-guardrails).
- **Notebook-bound private Memory**: users can manually turn an Ask answer into an editable preview and confirm it as reusable Memory. The collection has a user-level Memory page; notebook cards show the current user's count, and each workspace exposes **问答** (Ask) | **知识库** (Knowledge) | **记忆** (Memory) | **深度报告** (Deep Report). External Agents can submit `candidate` Memory through MCP; candidates are shared only among that same user's authorized Agents in the same notebook and do not enter formal Ask/search/report retrieval until the user confirms them.
- Optional graph-reasoning Ask mode (`mode="graph"`, opt-in/experimental): a rustworkx in-memory graph built from `knowledge_relations` is traversed for bounded multi-hop derivation/support chains, with answer-time adversarial chain verification and a weakest-link `chain_trust` score (the default Ask mode stays `chunk`)
- Deep report (two-phase background job): a notebook-level "深度报告" action turns one question into a multi-section technical report. **Phase 1a is corpus-blind question understanding**: it extracts an editable resolved question, objective, mandatory topics, entities, comparison axes, constraints, exclusions, expected output, assumptions, confidence, and at most eight blocking ambiguities without calling notebook search. The report pauses at `intent_ready` for its **creator's** confirmation, unless the create request has `auto_generate=true` and the question is clear (no blocking ambiguity), in which case the server auto-confirms through the same deterministic freeze the manual endpoint uses (no second LLM call) and proceeds directly into planning; a question with blocking ambiguities always pauses regardless of `auto_generate`, and required ambiguities must be answered before either a human or the server can confirm. A report carrying a source/reference-library scope also reruns the manual endpoint's scope revalidation before auto-confirming; when that recheck fails (sources deleted or libraries unmounted while intent understanding ran) the report stays at the confirmation gate and the skip event carries reason `scope_invalid`; when it passes, the refreshed freeze is adopted for the claimed understanding and the later planning/generation scope context, exactly as manual confirmation would persist it. Confirmation belongs to whoever created the report, not to the notebook owner: a member who created a report in a shared notebook confirms it themselves, and nobody — the owner included — can confirm or advance somebody else's report (row-level `created_by` isolation; see 群组知识共享). "Can only wait" is therefore true only of *other people's* reports. A failure or race while auto-advancing fails open — the report is left at its prior status with the manual confirmation gate still usable — and emits only the body-free `report_intent_auto_confirm_skipped` event. Confirmation (manual or auto) atomically claims `intent_ready → planning` and deterministically freezes the contract already shown to the user; it does not run a hidden second interpretation pass. Clarification answers enrich the internal retrieval/drafting question but never the visible report heading. **Phase 1b begins only after intent confirmation**: the confirmed wording and answers become authoritative, a bounded zero-LLM probe measures both federated KG and direct parsed-`SourceElement` coverage for every mandatory topic, and only then does the STORM-style planner use source titles, KG hits, and chunk provenance to refine vocabulary, ordering, perspectives, and tensions. Corpus availability may expose a gap but cannot replace or narrow a required topic; code validates the mapping and restores any omitted mandatory topic. The outline editor shows each section's mandatory question, editable retrieval directions, and raw-element/KG/base coverage before confirmation; the last section binding a mandatory topic cannot be deleted, and the API enforces the same invariant. **Phase 2 (minutes, on outline confirm)** runs every approved retrieval direction as well as the full `reasoning` deep-dive, in parallel by section. Chunks, KG objects, typed relation hops, confirmed Memory, and direct `SourceElement` hits share the same `[k]` binding path. `SourceElement` is a first-class citation rather than uncitable prompt decoration: small libraries may score element rows directly, while non-copyable large libraries derive a bounded candidate set from chunk ANN/FTS hits and hydrate only those chunks' exact `element_ids`, never the full element table. Report references deduplicate by exact evidence anchor rather than source title, and selecting a report citation reveals its bound source/location excerpt. Ask and report citations prefer `source_paper_meta.paper_title` only for a grounded paper row (`is_paper=true`) with a nonblank parsed title; all other sources keep their ordinary source title/file name. Citation responses also carry the persisted upload name as `source_file_name`; Ask/report citation cards show it as `原始文件` when it differs from the display title, including mounted public-reference-library evidence. That value comes only from `sources.file_name`, never a MinerU temporary/output Markdown name. The model's `grounded` boolean is advisory: the backend reparses emitted anchors and requires cited evidence to meet the configured relevance threshold. A read-only final editor creates the executive summary and flags incomplete mandatory intent or cross-section contradictions without rewriting sections or adding facts. The existing `（推断）`/`【通识】` discipline, five depth levels, live `section_status`, cancellation, `.md`, and `reports.zip` export remain unchanged. `ReportSummary` and `ReportDetail` expose `updated_at` plus `generation_started_at`, the latter stored atomically inside report state at the successful `outline_ready → generating` claim. Completed-report list and detail metadata use `updated_at` as the exact browser-local generation time, retain the relative age, and show the wall-clock duration from `generation_started_at` through that final write. Intent and outline confirmation waits are excluded; legacy completed reports without a generation-start stamp show no invented duration. Non-completed reports display creation time only and never claim a final duration.
- **Deep-report capacity, output, and retry rails:** whole-report generation is admitted through `REPORT_GENERATION_CONCURRENCY` (default 1 per backend process). One admitted report runs at most `REPORT_SECTION_CONCURRENCY` sections concurrently (default 5), also capped by the bound model service and by `POSTGRES_POOL_MAX_SIZE - 2`. That last cap bounds section-level fan-out only — each section's own sub-query fan-out can briefly borrow further pool connections, with waiters bounded by the pool acquire timeout — so it limits pool pressure rather than reserving fixed slots for online work; a queued report holds no database connection. Section drafting uses `REPORT_SECTION_MAX_TOKENS=65536`; the detailed-tier report-wide blueprint and final read-only editor each use an independent `102400` completion ceiling through `REPORT_SYNTHESIS_MAX_TOKENS` and `REPORT_SUMMARY_MAX_TOKENS`. These settings are completion ceilings, not application-side total-context declarations or reserved output; the bound provider/model must accept each ceiling together with that workload's prompt. The blueprint prompt selects only load-bearing claims, at most 12 per section and 60 report-wide. A claim's facet tag accepts an `id:value` composite and is deterministically narrowed to the legal prefix; a tag written as a facet's name, one of its declared values, or a case variant of the facet's id, name, or value is deterministically repaired to the owning facet id when the spelling is unambiguous (declared ids win over other facets' names and values; a spelling that two facets share is never guessed). An unrepairable tag is cleared on that claim alone — the tag is organizational, so no facet tag ever discards the blueprint, while evidence bindings, section ownership, and structure remain atomically validated; repair/clear counts surface as the counts-only `report_synthesis_facet_tags` event (two counters plus opaque report identity, never a facet spelling), and the synthesis prompt enumerates the frame's legal facet ids verbatim. Synthesis-failure disclosure semantics are unchanged. A run with zero non-failed, non-empty section bodies terminates as `failed`; a partial run with at least one valid body remains fail-open and discloses failed sections. Failed generation that retains a confirmed outline can atomically re-enter `generating`: retry keeps the frozen intent/outline, resets `generation_started_at`, clears stale generated artifacts, and never reruns intent understanding or planning. Queue time after that claim is included in generation duration.
- **Retrieval-run, sufficiency, and reasoning-action rails:** one report planning/generation run shares `REPORT_RETRIEVAL_FANOUT=8` leaf KG/chunk/element/PPR slots across all section workers; independent planning KG/raw-element probes use `REPORT_PROBE_CHANNEL_CONCURRENCY=2` (validated `1..2`). Waiting for a report leaf slot observes cancellation at bounded intervals and rechecks it after acquisition before starting I/O; a leaf already inside a backend/database call finishes safely instead of being detached. Ask uses the same request-local successful query-embedding single-flight without changing its historical fan-out. Report sufficiency requires at least `REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS=3` relevant evidence units and `REPORT_SUFFICIENCY_MIN_FAMILIES=2` distinguishable families (`REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES=3` for complete scope), with `REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE=0.8`. The reasoning run admits at most `REASONING_MAX_PPR_RETRIEVES=3`, `REASONING_MAX_EXACT_LOOKUPS=3`, `REASONING_MAX_FOLLOW_CHAIN_ACTIONS=3`, a cross-library community-peer cap of `REASONING_COMMUNITY_PEERS_CAP_FACTOR=2 × COMMUNITY_PEERS_TOPK`, and `REASONING_MAX_OUTLINE_UPDATES=6`. These defaults exactly preserve the former inline rules; deployments should change them only against a frozen evaluation set. Planning sub-stages, retrieve/synthesis/draft/final-editor stages, outer section attempts, and retrieval-run cache/fan-out counters emit fail-open, content-free events containing only stage/run kind, opaque report/run ids, indices, counts, statuses, and milliseconds.
- **Bounded neighbour expansion and background job concurrency pools:** one reasoning `expand_graph` action expands at most `REASONING_NEIGHBOR_EXPAND_LIMIT=1000` **distinct qualifying neighbours per direction** (outgoing and incoming are budgeted separately), so hydration of the neighbouring knowledge objects stays bounded even on a pathological hub node. The budget counts neighbours, not relation rows: one neighbour often carries several duplicate or corroborating relations, and some rows are dropped as non-queryable edge pairs, so the database read is bounded at four times the neighbour budget (a deliberate over-scan that absorbs those rows instead of letting them consume the budget and silently skip later, legitimate neighbours). Object-status eligibility is applied in SQL **before** the read bound (on the neighbour side of the join only), so relations pointing at deprecated objects cannot consume the bounded window and hide usable neighbours that sort after them. Rows are still taken in stable relation-id order from the existing `(notebook_id, source/target_object_id, id)` indexes. Truncation is never silent: the expand trace step carries `neighbor_truncated` plus the limit, and the reflect loop is told which nodes were only partially expanded so the model can retarget instead of assuming it has seen every neighbour. Separately, background jobs use **two independent fixed worker queues**, split by order of magnitude rather than by importance: heavy jobs (KG analysis/rebuild, relink, unified rebuild, conflict detection, merge pre-review) use at most `BACKGROUND_MAINTENANCE_CONCURRENCY=4` workers per backend process, while second-scale light jobs (paper-metadata backfill, command-catalog recognition, knowhow projection and orphan-asset sweep) get their own `BACKGROUND_LIGHT_JOB_CONCURRENCY=4` workers. A single pool would let a handful of hour-long rebuilds starve a table projection the user expects back in seconds. Submission enqueues work and returns immediately; it never creates one waiting thread per queued maintenance job. Interactive `ask-*` jobs and `report-*` jobs (already bounded by the whole-report admission gate) are deliberately outside both queues. Consequence to be aware of: while a job waits its stored status is still `running`/`queued` and the UI does not distinguish queued from executing; the wait is disclosed in the backend log only (a warning once the wait crosses the threshold, then an info line stating how long it queued, carrying only the pool and job category — never an id).
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
2. Upload PDF, Markdown, DOCX, PPTX, CSV, XLSX, or legacy binary XLS sources (multipart). The authenticated system configuration returns one sanitized parser-capability registry that drives upload validation and the supported-format hints in the import UI; endpoints, paths, credentials, and raw errors never leave the server. The routing pipeline itself (ordered self-hosted MinerU → MinerU public cloud → built-in fallback, execution boundary, availability) is deliberately not surfaced to users — they only see which formats are supported. Selection remains automatic, and a configured self-hosted path is never silently replaced by public cloud.
3. Backend (async background job): structured Markdown parse → chunking + embeddings — chunk-native Q&A is ready as soon as the source finishes processing.
4. **KG extraction is conditional** (see [KG extraction trigger](#kg-extraction-trigger)): on ingest it runs only when the notebook already has a KG, or when `KG_AUTO_EXTRACT=true`. `KG_JOB_CONCURRENCY` controls concurrent source jobs; every extraction model call is admitted by the system model scheduler for the service bound to the `kg_extract` workload, so the service's TOML `max_concurrency` remains the only model-capacity limit. The new source is then incrementally fused into the unified KG.
5. Knowledge objects are stored in `knowledge_objects` + `knowledge_relations` with element-level evidence bindings.
6. Hybrid retrieval (bi-gram keyword + float32 matrix semantic) feeds KG-native Q&A: answers contain sentence-level `[k_i]` citations, support multi-turn conversations, and expand via 1-hop KG neighbours.
7. Unified KG aggregates concepts across documents; pending cross-document merges can be confirmed or rejected.

Inside a notebook:

- Header: the editable notebook title stays compact by itself; the notebook description is shown in the Ask welcome state when no conversation is active, and toolbar actions keep their labels intact across desktop widths.
- Left column: user-imported source files with live parse-status (green = `extracted` only; others shown in amber while processing) plus per-source anomaly badges graded by consequence (red for integrity problems such as a parse failure, amber for retrieval-only problems such as partially unanalyzed content; neutral pending states like 待补全 appear only in the source detail view), detail previews, delete actions, and retrieval-scope checkboxes shared by Ask and new Deep Reports. Source detail fetches and renders a bounded 40-element page (the API caps a request at 100), loads earlier/later pages on demand, and resolves a citation target to its containing page, so opening a large document does not hydrate or mount every element. Every user-facing source count uses this visible imported-source set and excludes hidden `memory` / `knowhow` projection sources. Network source search is disabled for now.
- Main column: four tabs — **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report). Ask provides grounded Q&A with clickable `[k_i]` sentence citations, three retrieval modes, multi-turn conversations, a live expandable reasoning trace, and feedback. Hovering a question or answer reveals its time below the bubble/card; clicking pins it until the next outside click. Questions use the persisted browser-submission instant, while answers use the authoritative answer-write instant returned as `AskResponse.answered_at` (legacy payloads are projected from `answers.created_at`). Browser-local formatting shows time only today, weekday plus time elsewhere in the current Monday-based week, date plus time outside that week (never both weekday and date), omits the year within the current year, and includes it for other years. Conversation history uses a single-row `历史 N` entry in the Ask header plus an expandable manager; the adjacent `+` starts a new session directly. It is ordered and timestamped by sub-second latest activity, and a submitted first turn appears immediately and remains reopenable while the model answer is still running—even if the user switches sessions before `started` arrives. Terminal history summaries refresh for the active notebook independently of which session owns the answer panel; same-notebook refresh callers converge on the newest request. The composer and mode controls remain disabled while the selected notebook/session's latest detail is loading. Knowledge browses and governs dynamic object types. Memory shows only the current user's private records bound to this notebook. Deep Report exposes the two-stage report lifecycle, outline review, progress, export, cancellation, and deletion. In Ask, `Enter` submits, `Shift+Enter` keeps a newline, and while a model response is running the input/mode controls are locked while the send button becomes an interrupt control. A transport disconnect stops delivery to that client only; navigation, refresh, or transport loss leaves the detached Ask job running and it may persist its final answer. Clicking interrupt is a distinct cancellation action: the client calls `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel`, which sets the backend cancellation event so the worker/LLM path stops and does not save a cancelled final answer. If clicked before the first `started` event is readable, the client restores the draft immediately but keeps that run's transport only long enough to obtain its job id, cancels that job, then aborts it. The workspace remains two columns and has no fixed Studio sidebar.
- Knowledge Graph opens as a full-screen overlay: object-level KG nodes (Concept / Claim / Formula / Procedure) with type-specific shapes, edge relationship labels, multi-select type filters, and a type-grouped side panel that focuses the canvas on selection. Opening it from an Ask knowledge-object citation deep-links to the exact node: if the node is outside the bounded high-degree core, the client overlays its bounded one-hop neighborhood from the citation's real source notebook, including a mounted base notebook; graph-BFS anchors retain that owner id. Browser reads remain authorized by the active notebook, while the backend validates or resolves the object only within its effective participant set and internally proxies mounted-base neighborhood/detail/context reads; mounting a public base never grants direct notebook membership. Raw cited Concept ids are resolved by the neighbors endpoint to a canonical `focus_id` through a single-id cluster lookup, while their raw object id is retained for context reads instead of querying the synthetic graph id from `knowledge_objects`. If a large notebook's viz artifact is still building, the endpoint explicitly reports that location is temporarily unavailable instead of entering the full cluster-map fallback, and the client leaves no impossible pending focus. The side panel renders source excerpts as structured evidence cards so long titles, locations, formulas, and mixed Chinese/English text stay inside the panel; excerpts whose source element type is `formula` use the shared block KaTeX renderer instead of showing raw commands.
- A **KG quality analysis** panel (batch 1 of 4; see `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`) opens from a `图谱分析` button inside the Knowledge Graph view header, next to `图谱 Schema`. Its report reads require only notebook read access. The panel begins with actionable interpretations rather than a raw metric wall: report reliability, per-type merge signals, topic-structure signals, and the first source-review candidate. It explicitly says that convergence and connectivity are diagnostic signals rather than monotonic quality scores, and gives a visible red/yellow/gray/current status legend. The five-row artifact ledger explains what question each product answers, while the largest-concept groups and relation-provenance products are rendered as full diagnostic sections instead of appearing only as ledger receipts. Editors additionally get a `生成分析` / `更新分析` action. It reuses `POST /notebooks/{id}/unified-kg/rebuild`, the existing confirmation and per-notebook single-flight background job, and `job_id`-matched completion polling; it does not re-extract sources, and the report refreshes automatically when the job finishes. Read-only members retain the same report without the write action. `GET /notebooks/{id}/kg-analysis` returns object-type composition, merge-convergence rates broken down **per object type** (concept/claim/formula/procedure computed separately — mixing them dilutes the true concept convergence rate roughly 3x), the topic-community list, and cross-community edges for an overview map. `GET /notebooks/{id}/kg-analysis/sources` paginates per-source profiles, ordered by how sparsely (default) or densely each source connects to the notebook's mainstream communities. Like every other user-facing source count, source profiles cover only the visible imported-source set: objects belonging to hidden `memory` / `knowhow` projection sources are excluded when the profiles are precomputed, so those internal titles never reach the report and never distort the sparse-connection ranking. Orphan references (a `source_id` whose source row no longer exists) are deliberately *kept* and flagged `source_missing` — that is a diagnostic signal, not a hidden source. Both endpoints only read three precompute product tables (`kg_community_edges`, `kg_source_profiles`, `kg_analysis_artifacts`) that `POST /unified-kg/rebuild` writes wholesale in the **same** transaction that publishes the community graph itself, so a report can never pair recast boards with the previous ledger; they never scan the full object/edge/cluster tables online. Every reported number carries the generations it was built at and how far behind the live graph it is, per metric rather than as one page-level staleness banner. There are **two independent generation lines**: `kg_mutation_seq` (object/relation writes) and `cluster_mutation_seq` (merge results). Cluster writes deliberately leave `kg_mutation_seq` untouched, so the four artifacts computed from `concept_clusters` (size histogram, largest clusters, cross-community edges, source profiles) also carry `built_at_cluster_seq`/`cluster_seq_behind` and go stale on a merge-only write; `relation_provenance` reads only the relation table, carries no cluster stamp, and is deliberately not invalidated by a merge — recomputing it would mean a needless full relation scan. The two board-dependent artifacts (cross-community edges, source profiles) stamp the merge generation **their board partition was built on**: an integer when the same round rebuilt the partition, and an explicit `null` when the round only backfilled the ledger (the partition is then whatever the library already had, and nothing records which merge generation that was). `stale` is therefore tri-state — `true` when a line is definitely behind, `false` when both are aligned, and `null` when the merge generation is unknowable; `null` is not `false`, and the panel shows it as its own caveat instead of "up to date". The product ledger has its own freshness gate over both lines, independent of the community layer's, so a library that already has communities backfills the ledger on its next ordinary rebuild instead of requiring a forced one. The topic-board list itself is judged by the **same** tri-state rule, not a second one: its KG line is `community_seq`, and its merge line reads the very stamp described above — that stamp *is* the record of which merge generation the partition was built on, and the two board-dependent ledger rows are voided in the same transaction that remints board ids, so a present row always describes the current partition. A merge-only write therefore makes the board list report "merge generation unknowable" exactly when the two artifacts drawn from that partition report it, instead of claiming "up to date" just because `kg_mutation_seq` happened not to move. The action refreshes derived analysis state only; the panel still performs no delete/quarantine governance.
- The Analysis menu itself contains only the promotion queue (admin), publish / unpublish public knowledge base (admin), and edge-review queue. Dashboard and the full-screen Knowledge Graph are separate top-toolbar actions; 「图谱 Schema」 is entered from the Knowledge Graph view header rather than a separate top-toolbar action. Administrators manage the global object-type baseline. Notebook owners can inspect the effective baseline, copy-on-write an inherited definition, disable it only in that notebook, or add a notebook-only type; deleting an override restores the global definition. Read-only members see the same effective definitions without write controls. An administrator can switch to the global-baseline view, whose changes affect only notebooks that have not overridden the same type. No retired content-generation or derived-rule actions are exposed. The existing notebook analytics view includes separate Memory and Knowhow content-asset cards: Memory metrics are restricted to the signed-in user and current notebook (including for admins), while Knowhow metrics follow notebook read access. The cards show counts, health/recency summaries, and links only; they navigate to the existing Memory and Knowhow pages/editors rather than duplicating a browser or editor.

Knowledge object types have a single source of truth for their display name: the backend `OBJECT_TYPE_LABELS` in `app/domain/extraction_profiles.py` (`app/services/extraction_profiles.py` is a re-export shim), delivered to the client as `KnowledgeTypeCount.label` by `GET /notebooks/{id}/knowledge-types`. Every call site that can reach that API label — the Knowledge browser's type tabs and object entries — renders it directly, so user-defined object types (for example the column names projected from knowhow tables) also show their proper Chinese name. Call sites that only hold an `object_type` string — the citation popover and the knowledge-graph canvas/side panel — fall back to the small built-in front-end table `KG_TYPE_LABELS` in `frontend/app/kg-type-model.ts`; `kg-type-mark.tsx` consumes and re-exports that model for shared rendering. The table is character-for-character identical to the backend constant; `scripts/check_object_type_labels_contract.py` runs inside `scripts/check.sh` as a hard gate that fails the build when the two copies drift. An unknown or custom type is displayed verbatim as its `object_type`, never Title-Cased into invented English. Because both tables are keyed by user-controlled strings, look them up with `Object.hasOwn(...)` rather than bare indexing: `constructor` and `__proto__` resolve through the prototype chain and yield inherited functions/objects instead of a miss.

User-facing copy is under a vocabulary contract of its own, and `AGENTS.md`「界面词汇表」is its single source of truth: each row maps an internal term (基准库, chunk, KG, 抽取, 投影, 晋升, schema, deprecated, …) to the one word the interface may use. Internal names stay in code, types, comments, and the architecture docs — only strings rendered to a user get rewritten, and values that are *persisted* rather than rendered (the `Untitled notebook` default name, enum ids on the wire) are contracts, not copy, so they are never touched by a wording pass. `scripts/check_ui_vocabulary.py` enforces the table inside `scripts/check.sh`, and its scope follows the **trust boundary rather than the directory tree**: it scans the rendered text of every `frontend/app` source — string literals plus JSX text nodes, with comments, identifiers, regex bodies, and `${…}` / `{…}` interpolations stripped — *and* the message literals of every backend `user_error(status, "…")` call, because `api/deps.py` marks exactly those 4xx `detail` strings with `X-User-Message: 1` and the deny-by-default front end then displays them verbatim. Marking a string is a promise that it is user copy, so it inherits the copy rules; scoping the guard to `frontend/app` is what let 「基准库」and 「晋升队列」ship inside marked 403s while the guard stayed green. Bare `HTTPException(detail=str(exc))` stays outside the scan on purpose — it is never displayed and its detail is a diagnostics/MCP contract, a split guarded by `backend/tests/test_user_error.py`. A blacklisted term on either side fails the build. A second, independent guard — `frontend/app/raw-enum-fallback.test.mjs`, collected by `npm run test` and therefore also gated by `scripts/check.sh` — rejects raw enum fallbacks (`MAP[x] ?? x`, and `label(map, x, x)` which defeats the same design through the sanctioned API), because a lookup that falls back to its own key starts rendering the backend's English enum id the moment the backend grows a value; use `label(MAP, value, fallback)` from `frontend/app/vocabulary.ts`, whose mandatory neutral fallback makes that bug unwritable. That check runs on a real TypeScript AST rather than a regex: `M[x] ?? x` in a rendered position and `ALIASES[v] ?? v` in internal normalisation are the *same* syntactic shape, so only the surrounding context distinguishes a leak from correct code — a regex flagged the second and missed `M?.[x] ?? x`, `getLabels()[x] ?? x`, and `label(m, x, x)` entirely. Its own docstring records what it still cannot see (a value computed into a variable before being rendered, non-JSX sinks such as `alert(...)`), since honest scope beats a check that fakes completeness. Deliberately echoing a *user-authored* string (a custom `object_type`, a user-defined schema field name) is written as an explicit `Object.hasOwn(...) ? ... : raw` instead, which also avoids the prototype-chain hazard above. The guard is a word blacklist, not a semantic checker: two rows are covered only in their unambiguous compound forms, since bare 节点 / 边 are legitimate in the graph view and 边 is a substring of 旁边 / 边框. `backend/tests/test_ui_vocabulary_guard.py` holds its positive and negative examples and additionally fails when a vocabulary-table row gains neither a matching rule nor a recorded exemption, so the blacklist cannot quietly drift back into covering only a subset of the table.

Reparse preserves the source row and original file: it replaces source elements/chunks and their embeddings, and removes extraction runs plus source-derived knowledge before rebuilding. Delete performs the same source-derived cleanup, then deletes the source row (cascading source-owned records) and the local file.

Before a newly parsed or reparsed source publishes elements, chunks, embeddings, or source-derived knowledge, ingestion removes high-confidence running-header/footer artifacts. A normalized nonblank `heading`, `paragraph`, or built-in PDF `page_text` must occur on at least 3 represented pages and on at least half of all represented pages, either as the page's first/last textual element or with a parser-explicit `header`, `footer`, `page_header`, or `page_footer` block type; only occurrences after the first are suppressed. Non-boundary copies, tables, formulas, images, code, same-page repetitions, and lower-coverage repetitions remain untouched. Historical rows and external-parser output receive an independent retrieval-time defense: chunks and direct source elements collapse by `(source_id, NFKC + whitespace-folded + case-folded text)` before candidate/result caps, cross-query accumulation, reflection summaries, and final Ask/Report context budgets. The highest-scoring representative wins in ranked paths; exact-section context keeps the first document-order representative. Matching text from different sources is never collapsed because it represents independent provenance. This two-layer rule prevents duplicates from consuming upstream retrieval slots or downstream synthesis slots; it does not silently rewrite or globally deduplicate authored source data.

Visible imported-source counts deliberately differ from physical bookkeeping: hidden Memory/Knowhow projection sources do not appear in the source rail or user-facing counts, but `size.sources`, copy thresholds, storage accounting, and background scheduling retain physical-row semantics. Likewise, `has_unindexed_content` keeps the scale-index update decision true when derived content changed even if the visible imported-source delta is zero.

Scale-index scheduling exposes immediate and off-peak operations. When no build is already running, `when=now` atomically supersedes an older idle entry for the same notebook before claiming the immediate build; a later idle request is preserved, and a worker-start failure restores the displaced entry. Scheduler ticks claim queued notebooks independently, leaving busy follow-ups queued and isolating per-item launch failures. `AskResponse.index_required` remains an answer-time diagnostic snapshot, while the Ask UI observes live `ScaleIndexStatus.exists`; an `index_done` event refreshes the active notebook even after bounded foreground polling stops, so a published index removes that warning from historical answers without rewriting them.

When a notebook's `ScaleIndexStatus.state` is `"queued"`, the status response discloses why the wait is happening rather than a bare "queued" flag: `queue_position` (1-based) and `queue_length` describe standing in the off-peak queue, `queued_at` gives the UTC ISO timestamp the entry was **first** enqueued (re-queuing updates the mode but keeps the original timestamp, so it stays anchored to the same instant as the insertion-order position), `offpeak_in_window` reports whether the server is already inside an idle window, `offpeak_next_start_at` gives the UTC ISO timestamp of the next window when it is not, and `last_build_ms` (0 = unknown) carries the previous build's wall-clock duration so the UI can set expectations. All six fields are optional so older backends degrade gracefully to the pre-existing generic queued copy. A queued notebook that already has a published index also still carries `last_built_at`, so the dashboard card can keep showing "last built X ago" while it waits. `queue_position` is first-enqueue order (derived server-side by sorting on the first-enqueue timestamp, so a worker-start-failure restore cannot silently move it), not wait order — once the off-peak window opens, `_process_idle_queue` starts every queued notebook's build concurrently, so it is not a promise of turn-taking. The frontend composes the disclosed fields into one sentence in the browser's local timezone (`frontend/app/scale-index.ts::queuedScheduleHint`), which reports queue length (when ≥ 2) rather than position for that reason; numeric tiers and internal terminology are never surfaced verbatim. The pending-actions bell also now passes through the underlying `"queued"` state for `type:"index"` items (previously rewritten to `"building"`) and shows a distinct "queued, will build during an idle window" label without a progress percentage.

The notebook workspace hides the global collection top bar and keeps an engineering-console visual treatment. Markdown shown in Ask, reports, Memory, and Knowhow treats a whole-line one-line `$$...$$` as display math even when it is adjacent to prose; wide display equations scroll inside their own content block. Source-detail, knowledge-object, and Knowledge Graph evidence formula views remove full-value Markdown math delimiters before direct KaTeX rendering and show the original text if parsing still fails, so malformed formula input never becomes a blank visualization. Across the three surfaces that render **model-produced text** (Ask answers and Memory cards, deep reports, the public report page) a **single `~` is a literal, not strikethrough**: GFM strikethrough must be written as the spec's `~~text~~`. This is not a style preference — with single tildes pairing, Chinese technical answers routinely put two of them in one paragraph (ranges such as `7~5nm`, `80~90%`, `2~3 周`; approximations such as `~3GHz`), and everything between them is struck through. The Knowhow cell preview deliberately still pairs single tildes: its Markdown-normalization guard reasons from that behavior (see `AGENTS.md`).

### Source-selected retrieval scope

The source sidebar starts with every visible imported source selected and offers per-row checkboxes plus `全选` / `清空`. The same current selection is sent to corpus-blind Ask intent preview, Ask execution, and new Deep Report creation. It constrains active-notebook chunks, source elements, KG evidence and relations, graph expansion output, PPR output, and report retrieval; a narrowed scope also excludes hidden Memory/Knowhow projection evidence because those internal sources have no user-facing checkbox. Inside that hidden half the two kinds are scoped differently: Knowhow projections are notebook-wide and reach every member's ceiling, while a Memory projection is private to the user who created it and enters only that user's ceiling, filtered in the read itself so a shared notebook never exposes one member's private Memory to another. Mounted reference libraries are separate participants and remain in scope.

`AskIntentPreviewRequest`, `AskRequest`, and `ReportCreate` accept an optional top-level `source_scope` object: `{ "mode": "include" | "exclude", "source_ids": string[] }`. `include` admits only listed visible sources from the active notebook; `exclude` admits every visible active-notebook source except those listed. Omitting the field preserves the historical whole-scope behavior, and `exclude` with an empty list is the frontend's compact representation of all currently visible imported sources. At the API boundary, every explicit scope is validated and frozen into an explicit include-list. The server also computes `narrowed` against the current visible-source count and overwrites any submitted value: freezing an all-selected snapshot must not be confused with actually excluding a source. Therefore an all-selected run—including a one-source notebook—keeps conversation history and normal graph/reasoning channels; only a genuinely smaller set activates restricted-mode skips. The server privately snapshots the current hidden Memory/Knowhow participant ids for that all-selected run, while a narrowed run excludes them; those ids are neither exposed nor persisted in the public scope object. Both modes retain the frozen snapshot in source-partitioned candidate generation and result checks, so concurrent uploads cannot widen an in-flight run. If the current visible-source universe no longer equals an all-selected snapshot, unsafe whole-graph channels are disabled before I/O. The backend rejects foreign, hidden, or stale source ids with 422. If the effective local scope is empty and no reference library is mounted, it rejects Ask/intent/report creation with 409; the browser mirrors that authority by disabling the Ask composer and new-report controls while still allowing existing reports to be viewed. A report persists the resolved public scope in its understanding contract, revalidates it before intent confirmation and generation, and rehydrates the private participant snapshot for planning and drafting.

Retrieval scope has **two independent dimensions**, both shared by Ask intent preview/execution and new Deep Report creation, and both defaulting to fully selected: one checkbox per visible imported source of the active notebook (`source_scope`), and one checkbox per *whole* mounted reference library (`base_scope` — never expanded into the sources inside it). `AskIntentPreviewRequest`, `AskRequest`, and `ReportCreate` accept an optional top-level `base_scope` object alongside `source_scope`: `{ "mode": "include" | "exclude", "notebook_ids": string[] }`. `include` admits only the listed mounted libraries; `exclude` admits every mounted library except those listed; omitting the field preserves the historical behavior of every mounted library participating unconditionally. `notebook_ids` may name only libraries currently mounted on the active notebook (422 otherwise), and — exactly like `source_scope` — the API boundary freezes every submitted scope, including `exclude` with an empty list, into an explicit include snapshot and recomputes `narrowed` server-side, ignoring any client-supplied value. Freezing matters most for reports: the resolved scope is persisted in the understanding contract and re-applied at intent confirmation and at generation, so a library mounted after report creation cannot silently join it.

The two dimensions are **orthogonal**. `source_scope` narrowing gates the *active notebook's own* channels (PPR, private Memory, community reports, weak-support relations, exact-section lookup, the report corpus profile); unchecking a reference library must never switch any of those off, or a user who declined to borrow one library would pay for it with the current notebook's retrieval quality. Cross-library channels each consult the library dimension for themselves: typed collection enumeration and the collection map, federated candidate retrieval, community expansion, graph traversal, `follow_chain`, evidence assembly, and the KG availability gate. The last of those is a *judgement*, not a result filter — with the active notebook carrying no graph of its own and the only graph-bearing library unchecked, availability must answer "no graph", or `kg_required` never flips and the graph path runs a round over a KG this run may not read; it resolves participants through the same `resolve_participants` seam candidate retrieval uses, gated on the frozen selection rather than on whether that selection narrowed anything. Conversation history is the one gate that consults both dimensions, because a prior answer can quote content from a library the user has just unchecked.

Library-level narrowing is applied at **one** boundary — the participant list, filtered once and read by the plan, the walk, the denominator, and the closing fingerprint — because enumeration requires rows and counts to come from a single predicate; filtering rows while the denominator still summed every mounted library would report a walk that in fact finished as `concurrent_change`. `enumeration_active()` is unaffected: the tool stays available and only its scope shrinks. The leak surface includes the query terms themselves: community expansion takes sibling *entity names* out of a library and those names reach the visible trace, the used-query ledger, and the reflection prompt, so narrowing happens where the names are read, not where the hits come back. `resolve_participants` / `mount_sql.py` are untouched — that predicate is shared with permission checks (cross-library source detail proxying, citation resolution, asset reads), and a per-request retrieval checkbox has no business narrowing an authorization set.

Two second-order costs are registered and accepted. Graph traversal and PPR are filtered *after* traversal/truncation, because the federated graph is memoised process-wide under a scope-blind key: an excluded library's nodes still occupy diffusion budget and can act as transit hops, but none of their content reaches the rendered prompt or becomes citable. The whole-graph size guard likewise stays scope-blind, so unchecking a very large library does not turn that guard back off.

The **409 "empty scope"** judgement is now the conjunction of both dimensions — "which libraries did this request check", not "which libraries are mounted" — and applies at all three Ask entry points and at report create, confirm, and generate. A dimension the request did not *narrow* answers from the notebook's real evidence universe; a request that scoped neither dimension is left to the existing `ask_available` gate. The local evidence universe is **not** the visible-source count: Knowhow cells, that user's confirmed Memory and the local graph carry no visible source row, so `NotebookSummary` exposes `local_evidence_available` next to `ask_available` (computed by the catalog from the three local availability predicates it already evaluates, at zero extra queries) and the emptiness test ORs it with the source count, so the signal can only widen the answer, never narrow it. A genuine narrowing still wins: clearing every local source is empty even when local evidence exists.

`AskResponse.retrieval_scope` is a read-only receipt: `{ local: { selected, total }, bases: [{ notebook_id, name, included }] }`. Library names are a snapshot taken while the run was authorized, never re-derived from current mounts, so a reopened answer still names a library that has since been unmounted. Retrieval never reads it back. It is absent — and the payload therefore byte-identical to every historical answer — when the request narrowed neither dimension, because the browser submits both scopes on every request. Its disclosure surface is deliberately narrower than the cross-library source proxy: names and counts only, no file path, no error text, no source identity.

The browser surfaces both dimensions as two checkbox groups in the source panel: a `检索范围 · 本库 N/M · 参考库 K/L` toolbar whose 全选/清空 buttons drive both dimensions together, a `参考库` group with one row per mounted library (name only, never expanded to the sources inside it), and a `本库来源` group that owns the source search box — the box searches the active notebook only, and sitting above the reference libraries made users expect it to reach inside them. The same two-segment count is repeated above the ask composer from one shared computation. Ask input and report creation are disabled when both dimensions are empty, mirroring the backend 409 condition (the local half reads `local_evidence_available` rather than counting visible sources). When `retrieval_scope` is present the answer card renders a collapsed `检索范围：…` line above the answer body, expanding to each reference library's participation; when absent nothing is rendered, and the browser never re-derives "was this narrowed" from the receipt's numbers — that judgment has exactly one home, on the server.

The strict-reasoning gate in the browser is likewise **per-selection**, not per-mount. `NotebookSummary` carries `base_kg_notebook_ids` — the decomposition of `base_kg_available`, listing which of the mounted libraries actually have a graph. It costs zero extra queries (`mounted_bases_row` already returns a `has_kg` column per row; the aggregate boolean was just `any(...)` over it), it is filled on exactly the paths `base_kg_available` is filled, and the two must stay self-consistent (non-empty ⟺ true) because they are two projections of the same read. The browser gates 深入分析 / 知识图谱 on "this notebook has a graph **or** the checked libraries include one that does", and the `将借用参考库「…」推理` hint names only the libraries that are both checked and graph-bearing — reading the aggregate boolean would let the UI admit a mode that cannot reach a graph this round (the server-side KG availability gate already narrows by library) and would name a library that is not participating. When the field is absent (version skew) the browser falls back to the aggregate boolean intersected with the selection, which can only be more conservative than the pre-existing test, never less. Because that gate now blocks a mode it used to admit, the "no graph" hint splits into two causes with different remedies: a graph-bearing library is mounted but unchecked → the hint says to re-check it in the source panel and deliberately offers **no** build button; no graph-bearing library is mounted at all → the existing 「整理知识图谱」 hint and button stand. Collapsing the two would push a user toward a whole-notebook graph build (real model spend) when re-checking one box is the fix.

This top-level checkbox scope is a hard active-notebook ceiling and the *only* source of retrieval scope. The model never proposes, narrows, or widens it: the corpus-blind intent planner emits no source identities, and no Agent action inside a run can change which sources are in scope. An all-selected include snapshot keeps normal graph channels, privately snapshots current hidden Memory/Knowhow participants, and freezes source-partitioned candidates and result checks. A visible-source or hidden-participant addition/deletion after validation disables unsafe graph channels before I/O; source-partitioned retrieval continues against the frozen ceiling. Mounted reference libraries remain independent participants. For indexed chunk/element retrieval, the scale artifact stores compact row-aligned source codes; HNSW applies the allowed-source predicate before Top-K, and hydrated results are checked again before scoring/synthesis. An older published index without this optional sidecar remains loadable but uses bounded source-filtered FTS until a rebuild or delta fold writes the map. After KG, PPR, and exact deterministic seeds, a completely empty evidence state performs one bounded raw-element search before asking the reflect model whether evidence is sufficient. Channels whose persisted artifacts cannot safely apply the active-source predicate before traversal (whole-graph/PPR/relation expansion, exact-section lookup, and whole-corpus report profiling) are skipped for the active notebook during a narrowed checkbox run or participant-universe drift; post-filtering alone is not authority because excluded candidates can consume Top-K or supply hidden graph premises. Direct source-bounded chunk, element, and KG retrieval remains available, and base-backed KG seeds can still supply base chunks without traversing the combined graph.

The internal `SourceSubgraphSnapshot` is the read-side preparation for replacing those active-notebook skips, but it is now consumed only through the shared, quality-gated Ask and Deep Report activation seam. It opens one repeatable read, resolves only the frozen visible-source ids, and applies source predicates before every `LIMIT`; SQLite pins source-first access for object, chunk, and cluster reads so excluded sources are not traversed before filtering. A relation is admitted only when its own source and both endpoint objects belong to the selected source set. Object-to-chunk memberships come only from current-generation source facts, normalized evidence-element bindings, and selected chunks—never the whole-notebook entity/chunk map. Cache identity uses the O(1) notebook KG/cluster mutation sequences plus bounded source/run/backfill state; sanctioned live projection writes advance the KG sequence and historical repair advances its source ledger. Cache hits therefore do not recount facts, chunks, or evidence bindings. Current-generation live-fact completeness is revalidated from the already bounded fact window while a snapshot is built. Missing reverse-index state, source deletion, generation drift, or an unsupported fact projection version fails closed before dependent graph use; crossing a row rail disables only the capabilities that depend on that incomplete leg. An incomplete live or backfilled projection may still supply names and evidence from the source-local rows it did prove, but fact-completeness and PPR/membership capabilities stay disabled. Cached payload and evidence trees are recursively immutable. Whole-scope, off, and shadow runs preserve the historical response, prompt, trace, candidate-order, evidence-budget, and citation contracts; only an attested active run may append source-local G after B.

The positive internal rails are: `SOURCE_SUBGRAPH_MAX_SOURCES=32`, `SOURCE_SUBGRAPH_MAX_OBJECTS=20000`, `SOURCE_SUBGRAPH_MAX_RELATIONS=40000`, `SOURCE_SUBGRAPH_MAX_CHUNKS=20000`, `SOURCE_SUBGRAPH_MAX_FACTS=20000`, `SOURCE_SUBGRAPH_MAX_FACT_ELEMENTS=60000`, `SOURCE_SUBGRAPH_MAX_MEMBERSHIPS=60000`, `SOURCE_SUBGRAPH_MAX_CLUSTER_MEMBERSHIPS=20000`, and `SOURCE_SUBGRAPH_CACHE_MAX_ENTRIES=64`. Each database leg probes at most its configured limit plus one row to detect overflow; non-positive values are rejected during settings validation.

The selected-source primitive layer is consumed only through the shared Ask/Report activation service. It rechecks capability, allowed relation source, both endpoints, evidence, and review state for every neighbors/expansion, relation-search, and two-hop-chain result; excluded sources cannot consume a result slot or act as an intermediate node. Exact lookup reuses the established identifier grouping/subtree semantics over only the snapshot's selected chunks, and cursor enumeration derives its total and pages only from the complete selected snapshot rather than a notebook collection map. Primitive hard bounds are fan-out 16, expansion depth 3, expansion nodes 128, chain results 16, relation results 32, and enumeration page size 100; exact lookup retains the existing `EXACT_LOOKUP_*` rails.

The internal protected-enrichment service now freezes the historical final baseline before invoking a graph provider. Graph-only chunks are selected under a separate caller-supplied token budget and appended only to an isolated enrichment proposal; budget pressure drops graph candidates without re-truncating baseline evidence. A chunk already in the baseline keeps its text, source and citation handles, score, relevance, and position while an isolated copy merges producer provenance. Off and shadow keep the user-visible baseline unchanged; active may publish only the protected appended proposal after quality approval. Graph failure or timeout returns the same baseline and manifest, and a nonzero `baseline_evicted_count` discards the entire graph proposal. Baseline and enrichment reasoning actions use non-borrowable step ledgers. Disabled shadow mode or a zero graph budget issues no provider call. Ask and Deep Report consume this graph lane through the shared activation service; off and shadow keep calls, prompts, responses, and citations on the historical baseline.

Small and medium selected-source snapshots also have an internal sparse-PPR producer, enabled by default for invisible shadow observation and independently controlled by `SOURCE_SUBGRAPH_PPR_ENABLED`. It constructs a reciprocal column-stochastic CSR directly from authorized snapshot nodes, relations, object-to-chunk memberships, and cluster routers; ownership, endpoint, evidence, review-state, and allowed-member checks happen before edge insertion and degree normalization. Reset weights whose object/chunk ids are absent from that scoped graph are ignored. Min-max normalization, Top-K, and hydrated hit payloads range only over the snapshot's authorized chunks, so an excluded B/C source cannot affect A's degree, reset normalization, score, ordering, or memory limit. Transition cache identity is the frozen scope plus KG/cluster/source generations; its LRU holds at most 8 entries and a cold build is single-flight across the service. Online rails are 40,000 total graph vertices (KG nodes + chunks + cluster routers), 100,000 logical undirected edges (at most 200,000 reciprocal transition entries before CSR duplicate folding), and 100 returned chunks. The builder is fixed O(vertices + edges); crossing either construction rail returns `ppr_node_limit_exceeded` or `ppr_edge_limit_exceeded`, while build/run exceptions return `ppr_build_failed` / `ppr_run_failed`. No whole-notebook CSR fallback is permitted. This producer ranks `G` only for the shared Ask/Deep Report activation service; it never mutates `B`.

In a large notebook, the source-partitioned scale companion provides the same selected-source authorization contract without opening the notebook-wide CSR. An offline full build or fold reads one visible source at a time from `knowledge_object_sources`, current-generation source facts/elements, source-owned chunks/relations, and source-bounded cluster memberships; every branch uses the corresponding snapshot LIMIT+1 rail and SQLite pins chunk access to `idx_chunks_source`. It publishes one hashed, directly addressable partition plus a constant-size root manifest bound to the main scale manifest version. Each partition additionally binds the complete content-free source/run/backfill signature and SHA-256 for every payload file, so provenance repair or parseable file corruption invalidates it even when counts do not change; request-time identity probing remains O(selected) and reads no graph rows. A partition stores its local column-stochastic CSR, row-aligned object types and chunk identities, and source-owned cross-partition relation endpoints. Before payload I/O, a multi-source cold read validates all selected constant-size manifests and conservatively reserves local transition entries plus two entries per stored cross relation against cumulative rails of 60,000 nodes and 240,000 transition entries. A single-source request then reuses its verified CSR directly; a multi-source request unions authorized identities through sparse arrays and fills one bounded cross-edge allocation. It admits a cross-partition relation only when its owning source was selected, its evidence was source-local at build time, both endpoints are in that selected union, and the central edge registry accepts their types. PPR runs at most `SOURCE_PARTITIONED_PPR_MAX_ITERATIONS=30` full-CSR iterations and uses partial Top-K before deterministically ordering at most 100 returned chunk candidates; it never materializes or sorts every chunk result as Python tuples. Legacy/missing/corrupt/over-limit artifacts or any parent/source-identity mismatch return a specific unavailable reason before graph use; no whole-graph or post-filter fallback exists. The runtime holds at most 2 combined partition handles and cold load/composition is single-flight. `SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED` controls publication and `SOURCE_PARTITIONED_PPR_ENABLED` controls the runtime reader; both default on for invisible shadow observation and remain independent rollback switches. The reader feeds only the shared Ask/Deep Report activation service and fails closed to `B` when unavailable.

The versioned selected-source rollout gate uses paired baseline/shadow observations over five mandatory scenarios: single source, a few sources, a single source carrying at least 10,000 chunks, mounted-base evidence, and same-name objects whose gold evidence must bind to different source aliases. Both lanes must use the same provider/model/prompt version, temperature, Top-P, seed, corpus signature, local/base source ids, alias-to-id binding, and scenario size. The EnergAIzer/PDAgent case must execute `expand_graph`, retain at least 20 baseline KG candidates, preserve the exact baseline candidate list/manifest and citation keys, and emit no baseline eviction. Any unauthorized evidence/citation, malformed metric, scope drift, missing graph action, or baseline mutation is a hard failure. Evidence Recall@20, citation coverage, citation validity, grounded-sentence coverage, and non-empty-answer rate may not decrease; no-answer, erroneous-refusal, and outline-drop rates may not increase—both per case and in aggregate. A baseline non-empty structural denominator may not become zero in shadow. Shadow latency and database rows may be at most 1.5× baseline, peak memory at most 2×, and each case may add at most 4,000 prompt tokens and one model call. The content-free attestation contains only run/corpus/model/policy identifiers, the pinned canonical-golden digest, scenario counts, content-free per-case and aggregate metrics, failures, and an integrity digest; it never contains questions, answers, evidence/citation/source ids, or excerpts. Production recomputes every per-case and aggregate quality/cost rail instead of trusting `approved`. The digest is not a signature, so an active rollout must load it from a trusted deployment artifact and match both the expected corpus and model; either missing pin fails closed. `off` stays inert, `shadow` may collect paired data without an attestation, while `allowlist`, stable-hash rollout, and `on` fail closed unless the verified default-policy attestation is approved. This gate is the mandatory control point for every user-visible Ask/Deep Report activation.

The selected-source activation contract is shared by Ask and Deep Report but remains an internal retrieval implementation detail. For a genuinely narrowed local source scope, Ask `chunk`, `reasoning`, experimental `graph`, and Deep Report call the same selected-source activation service after their historical baseline is frozen. An omitted scope or an all-selected snapshot—including the sole selected source in a one-source notebook—does not enter this service and retains the historical response shape. `SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow` is the default: it builds and measures `G` but returns `B`; `allowlist`, stable-hash `rollout`, and `on` require the trusted content-free attestation plus exact corpus/model pins. Active output is `B` followed by `G`; `G` has a separate 4,000-token default budget and cannot evict, reorder, or re-score `B`. Snapshot/partition failure, scope drift, any post-scope candidate, or nonzero baseline eviction fails closed to `B`. Mounted-base evidence remains in its independent historical lane and is never traversed by the active notebook's selected-source graph. Rollout state never enters a public Ask/Report field, reasoning trace, progress stream, or UI; only the content-free internal `selected_source_graph` event carries it, and the browser filters legacy persisted `source_subgraph` steps.

The content-free `selected_source_graph` event is operator-only telemetry and is filtered from every user-readable debug-log list, statistic, search, pagination, and detail response.

### Interface mode (auto/advanced)

Every user carries a persisted `user_profiles.ui_mode` preference, `"auto"` (the default) or `"advanced"`, returned as `UserProfile.ui_mode` by `GET /me` and self-service-editable through `PATCH /me/ui-mode` (body `{"ui_mode": "auto" | "advanced"}`; any other literal returns 422). A backend that predates this field omits it, and the client treats a missing value as `auto`. Advanced mode is the complete interface described throughout this document, byte-for-byte identical to prior behavior. Auto mode is a deliberately reduced surface toggled from the avatar menu: it keeps only the **通用问答** and **深入分析** Ask tabs and hides the engine sub-switch (深入分析 pins to `reasoning`), the retrieval-effort picker (pinned to `standard`), the Deep Report research-depth picker (pinned to `standard`, `depth=2`), and the source/reference-library scope checkboxes described above — a control hidden in auto mode always sends the unnarrowed request default (every visible source, every mounted reference library), never whatever a prior advanced-mode session happened to leave narrowed. Deep Reports created from auto mode always set `auto_generate=true` (see the Deep Report entry above and the `report_intent_auto_confirm_skipped` event it can emit). The Ask composer locks only when the notebook has **no usable evidence yet** and some source is still mid-parse — a notebook that already has evidence never locks the composer just because a new upload is parsing. When the pending-source count can be trusted (the source search box is empty and every visible source page is already loaded), the lock shows `N 篇文档处理中，完成后即可提问`; otherwise it shows the number-free `文档处理中，完成后即可提问`. Switching modes takes effect immediately and only changes which controls the client renders and which request defaults it sends; it never changes backend retrieval or generation logic, and advanced-mode behavior is unaffected by auto mode's existence.

## Group knowledge sharing

Three real situations — a **project** team sharing one knowledge base, a
**department** sharing several, and a wider **domain** library — are served by a
single model. Their differences live entirely in configuration, never in the
mechanism.

### The model: groups, grants, mounts

- A **group** is a set of users plus an in-group role. `kind ∈ {project,
  department, domain}` is only a classification label: it decides who may create
  the group and how the interface words it, and it changes no permission
  mechanism whatsoever. Anyone may create a `project` group; `department` and
  `domain` groups are administrator-only, which is what makes the label
  trustworthy. `kind` is therefore immutable after creation — a request that
  sends it to the update endpoint is rejected rather than silently ignored,
  because a user who could relabel their own project group as a "department"
  would defeat the very gate that makes the label mean something.
- Every group has exactly one live **owner** (`groups.owner_id`). The creator is
  the initial owner and also an admin. Ownership may be transferred only to an
  existing member; the target is atomically promoted to admin, while the former
  owner remains an admin. The owner cannot be demoted, removed, or leave until
  ownership is transferred. `created_by` remains immutable creation audit.
- An **authorization edge** is `(notebook, principal, role)` with `principal ∈
  {user, group, group_admins, everyone}` and `role ∈ {viewer, admin}`. Every row
  in the table is a *live* grant — the decision predicate applies no status
  filter at all, so there is no "forgot to exclude pending" failure mode. The
  notebook's owner (`created_by`) remains the implicit highest authority.
- **Mounting** is unchanged except for its validity predicate. Read access now
  implies mountability, subject to the borrowed-mount gate below.

Membership in a group and the "group members share this library" edge are two
separate facts, and both are evaluated live: removing someone from a group, or
deleting the grant, or deleting the group, all take effect on the next request.

### Roles

Two in-group levels plus the notebook owner:

| Capability | member | group admin | owner |
| --- | :-: | :-: | :-: |
| Open the notebook, read sources/graph, ask questions (conversations stay per-asker), keep own Memory | ✓ | ✓ | ✓ |
| Create **their own** deep report in the notebook (counts toward their own usage; invisible to others) | ✓ | ✓ | ✓ |
| Mount the notebook as a reference library of their own notebook | ✓ | ✓ | ✓ |
| Add/delete/re-parse sources, trigger graph and retrieval index builds | | ✓ | ✓ |
| Manage authorization edges, rename, graph-schema overrides | | ✓ | ✓ |
| Mount configuration, `share_token` link sharing (unsharing also drops all read-only members) | | | ✓ |
| Delete the notebook, transfer ownership | | | ✓ |

P2 delivers those two cells. Content-management capabilities (source add/delete/
re-parse, build triggers, knowhow/knowledge-governance/command-catalog writes) and
`notebook:manage` now resolve to the **admin
tier** — owner ∪ an effective `role='admin'` grant edge (predicate definition point
`access_sql.NOTEBOOK_ADMIN_SQL`, which reuses the read predicate's restricted three
arms plus `role='admin'` and excludes `everyone`). A group admin can therefore
manage content and sharing through the browser.

**What `notebook:manage` actually covers** — it is `PATCH /notebooks/{id}` plus the
three grant endpoints, and the PATCH edits the notebook's whole **descriptive
profile**, not just its name: `NotebookUpdate` accepts `name`, `purpose`,
`primary_domain`, `target_users`, `expected_questions`, `source_types`, `taxonomy`
and `access_scope`. Earlier revisions of this page said "rename", which was
shorthand for "the PATCH endpoint" and should never have been read as the field
list. Two properties make that safe, and both are load-bearing rather than
incidental:

- **None of those fields participates in any authorization decision.** Access is
  decided exclusively by the three predicates in `access_sql.py` (plus
  `mount_sql.py` for mountability), which reference `notebooks.created_by`,
  `notebooks.tier`, `notebook_members` and `notebook_grants` — and none of the
  eight. In particular `access_scope` is *descriptive prose about who the library
  is for*, not an access-control column: it is written by the notebook store and
  read back into the catalog projection, nothing else. So editing them cannot
  escalate privilege. A regression guard freezes this
  (`backend/tests/test_notebook_update_authorization_free.py`) so that adding a
  field to `NotebookUpdate` forces someone to re-answer the question rather than
  inheriting the answer silently.
- **Lifecycle state is repository-private.** `NotebookUpdate` sets
  `model_config = ConfigDict(extra="forbid")`, so `status` (notably the internal
  `copying` sentinel), `tier`, `created_by` and `is_shared` are not writable
  through this endpoint at all — an unknown key is a 422, not a silent no-op.

These fields are ordinary user-visible content: `primary_domain` is matchable in
the in-notebook search box, and `purpose` is surfaced to external agents by the
MCP `list_notebooks` / `select_notebook` tools (capped at 500 characters). They are
therefore *content-adjacent* — they describe what the library is about — which is
exactly the scope a content manager already holds. Splitting the endpoint or
per-field validation so that a non-owner could only rename was considered and
rejected: it would add a real seam for purely descriptive metadata while the same
group admin can already add, delete and re-parse every source in the library.

The share dialog gains a "group
admins may manage this notebook" checkbox accordingly: ticking it appends a
`(group_admins, admin)` edge beside `(group, viewer)`; unsharing removes both
same-group rows together, and the share list folds them into one entry marked with
management rights. **But two owner-only capabilities deliberately do not flip**:
`notebook:delete` (deleting a whole library, un-revocable by the owner) and
`notebook:configure` (mount configuration + `share_token` link sharing) stay owner
— see "Mount configuration and link sharing stay owner-only" below. **The Agent/MCP
surface is also untouched**: `sources:write` / `sources:delete` /
`maintenance:execute` remain owner-only — a long-lived token is a separate
credential whose owner may have been granted admin long after it was issued, and
the MCP write tools' blast radius (deleting documents) is what that owner gate was
created for. The browser HTTP surface widened to admin while the Agent token surface
did not; that is a deliberate divergence, not an oversight, so group-admin write
authority exists in the browser UI only.

Administrators additionally hold an **operations bypass on the group dimension**
— they may read any group's detail and manage any group's members and edges
without being a member. This mirrors the existing "an administrator may transfer
`notebooks.created_by`" bypass and exists for two concrete reasons: the
"keep at least one group admin" rule can still, under a concurrency window, leave
a group with zero admins, and since every management endpoint demands group-admin
identity such a group would be unrecoverable through the API; and
`GET /groups?scope=all` is the administrator's global management view, which
without the bypass would be a table whose every row 404s. The bypass does **not**
cover self-service leave (leaving presupposes actually being a member), does not
fabricate membership (`my_role` still reports empty truthfully), and relaxes no
notebook-dimension read or write guard.

### Group workspace

The avatar-menu entry opens a collection-level page, not a modal. Its left rail
selects a group; the workspace has Knowledge libraries, Members, Share requests,
and Settings tabs. Every member can inspect and open the notebooks their current
group role actually grants them; a `group_admins`-only library is not disclosed
to a plain member. Owners and group admins can add notebooks they have management rights
on, revoke group visibility, and optionally grant group admins content-management
rights. In Members, they may also create or reopen one reusable invitation link,
copy it, rotate it (which atomically invalidates the old link), or revoke it.
Opening the link preserves its bearer token through the sign-in/register gate;
after authentication the browser removes the token from its URL history and the
server atomically adds the caller as an ordinary member. Repeated redemption is
idempotent and never demotes an existing admin. An unknown, revoked, rotated, or
group-deleted token has the same 404 response. The link has no automatic expiry,
so admins must treat it as a bearer credential and revoke or rotate it when its
audience should change. Owner transfer and group deletion are separate confirmed settings actions.
The page reuses the collection shell, typography, controls, spacing, colors, and
responsive breakpoints; its group/tab selection is addressable in the URL hash.

### Endpoints

Twenty-seven endpoints in `group_routes.py` (including the seven P2 approval-flow
endpoints), plus one read-only addition on the notebook router.

| Endpoint | Who | Notes |
| --- | --- | --- |
| `POST /groups` | any user (`project`); admin (`department`/`domain`) | creator becomes owner and group admin in the same transaction |
| `GET /groups` | any user | groups I am in; `?scope=all` is admin-only |
| `GET /groups/{id}` | group member (admin bypass) | detail + member roster |
| `PATCH /groups/{id}` | group admin | `name` / `description` only |
| `DELETE /groups/{id}` | group owner (admin recovery bypass) | clears grants pointing at the group in the same write transaction |
| `POST /groups/{id}/transfer` | group owner (admin recovery bypass) | target must be a current member; promotes the target to admin and keeps the former owner as admin |
| `PUT /groups/{id}/members/{user_id}` | group admin | add or change role in one call |
| `DELETE /groups/{id}/members/{user_id}` | group admin | 409 for the owner (transfer first) or if it would remove the last admin |
| `DELETE /groups/{id}/membership` | member | self-service leave; the owner must transfer first; 409 for the last remaining admin |
| `GET` / `POST` / `DELETE /groups/{id}/invite-link` | group admin (admin recovery bypass) | inspect without side effects, create/reuse, or revoke the group's current reusable bearer invitation |
| `POST /groups/{id}/invite-link/rotate` | group admin (admin recovery bypass) | atomically replace the token; the old link stops resolving immediately |
| `POST /group-invites/{token}/join` | signed in | atomically join as `member`; idempotently preserves an existing role; invalid/revoked/deleted tokens are 404 |
| `GET /users/resolve?username=` | any signed-in user | exact username lookup, returns id/username/display name only |
| `GET /notebooks/{id}/grants` | `notebook:manage` | every edge on the library, all four principal types |
| `POST /notebooks/{id}/grants` | `notebook:manage` **and** group admin | only `group` / `group_admins` principals |
| `DELETE /notebooks/{id}/grants/{grant_id}` | `notebook:manage` | notebook-side revocation |
| `GET /groups/{id}/shared-notebooks` | group member (admin bypass) | member-visible inventory of libraries shared with this group |
| `DELETE /groups/{id}/shared-notebooks/{nb}` | group admin | group-side revocation; removes every edge pointing at this group |
| `POST /notebooks/{id}/share-requests` | `notebook:manage` **and** target-group **plain member** | **P2** submit a share request; a group's *admin* is refused with 403 — he shares directly via `POST /notebooks/{id}/grants` and never goes through this table. Idempotent (an in-flight request returns the existing pending row, not a 409) |
| `GET /notebooks/{id}/share-requests` | `notebook:manage` | **P2** the requester's own requests on this library (dialog echoes pending/rejected) |
| `GET /me/share-requests` | signed in | **P2** every **pending** request *I* filed, across all notebooks. The counterpart of the withdraw endpoint: same authorization axis (`requested_by`), no notebook capability at all, so a requester who has since lost management rights can still find and withdraw their own proposal. Deliberately **not** mounted under `/notebooks/{id}/…` — that dimension already has a manage-gated list and must keep one meaning. Pending only: a decided request cannot be withdrawn, so listing it would only widen disclosure |
| `DELETE /notebooks/{id}/share-requests/{rid}` | signed in, **and the request is yours** | **P2** withdraw a **pending** request (whole-row delete, not a third status); already-decided is 409, missing is 404. ⚠ **Deliberately carries no notebook capability dependency**: the authorization axis is request ownership, not current library rights. Since approval refuses a requester who has since lost manage rights, requiring manage here too would make such a request neither approvable nor withdrawable — permanently stuck in the reviewer's queue |
| `GET /groups/{id}/share-requests` | group admin | **P2** the review queue: pending requests to share into this group |
| `POST /groups/{id}/share-requests/{rid}/approve` | group admin | **P2** write the `(group, viewer)` edge and mark approved in one transaction; idempotent if already shared; missing/decided is 404 |
| `POST /groups/{id}/share-requests/{rid}/reject` | group admin | **P2** mark rejected, write no edge; the requester may re-submit for the same (library, group) |
| `GET /notebooks/{id}/share` | `notebook:configure` | read-only; see below. ⚠ **owner-only, not `notebook:manage`** — link sharing is the owner's disposition of the library toward the outside world and does not travel with content-management rights |

Several boundaries are worth stating explicitly:

- **Group visibility is 404, not 403.** A non-member asking about a group gets
  exactly the same answer as for a group that does not exist. Group names are
  themselves probeable information (which departments use this system, whether
  some project exists). The single deliberate exception is `POST
  /notebooks/{id}/grants`: there "the group does not exist" (404) and "the group
  exists but you do not administer it" (403) are distinguishable, because
  reaching that endpoint already proves you hold management rights on the library
  and you must already possess the 128-bit random group id — so the distinction
  buys two actionable error messages without opening an enumeration channel.
- **Creating a group edge is a double condition** (design decision 9): the caller
  must both hold management rights on the notebook *and* be an administrator of
  the target group. The group half is decided **inside the store's write
  transaction**, not by a pre-flight query: the edge grants an entire group read
  access the instant it lands, and a check-then-insert window is long enough for
  the group to be deleted or the caller to be demoted while the edge ships anyway.
- **Revocation is asymmetric.** The library's manager may delete any edge from
  the notebook side; a group admin may delete every edge pointing at their group
  from the group side. Each entrance needs only its own half — a group admin
  governs everything shared with their group, and a library owner may always take
  their library back.
- **A requester must be able to reach their own request without any rights on the
  notebook.** Withdrawal is deliberately gated on request ownership alone, so the
  list that surfaces the request id must be too — otherwise the escape hatch is
  unreachable in the one situation it exists for (the requester lost manage rights;
  approval now refuses the request; the notebook-side list 404s). `GET
  /me/share-requests` is that global entrance, and the UI puts it in the groups
  panel rather than the notebook workspace, because the requester may have lost
  read access as well and cannot open the workspace at all. Its **disclosure
  surface is chosen field by field**, against "did he already know this?" — and,
  just as importantly, **"does he still know it?"**: what he learned was the label
  *at the moment he filed*. `notebook_id` is included permanently (he filed with
  it, and `notebooks.created_by` is written only at creation and deep-copy — there
  is no ownership transfer, and the column is absent from `NotebookUpdate` with a
  guard pinning that — so an id cannot be used to probe a *new* owner later). The
  two **display labels are conditional on current access, evaluated separately**:
  `notebook_name` only while the read predicate still holds for him, `group_name`
  only while he is still a member of that group. Otherwise the label comes back
  empty and the UI renders a neutral placeholder. Without this the list would be a
  *live* channel: the counterparty renames, and the rename keeps being delivered to
  someone who may no longer observe that object — the same line held by `no-store`
  on cross-library assets, by unmounting turning proxied reads into 404s, and by the
  public report page re-checking the creator's read access on every request. The two
  halves are judged **independently** on purpose: judged together, losing just one
  would blank both, and several pending requests would all read "library → group"
  with no way to tell which to withdraw. Withdrawal itself is unaffected — its axis
  is request ownership, not label visibility.
  `group_id` is the group he chose while a member of it. `status` is
  always `pending`, so `decided_by`/`decided_at` are always null — **no approver
  identity leaves through this path**, because decided requests are not returned at
  all. Nothing about the library's current state (source counts, whether it is
  still shared, its present members) is included.
- **`user` and `everyone` principals do not go through these endpoints.** The
  `user` principal keeps using the existing read-only share-token flow, and
  `everyone` keeps using `POST /notebooks/{id}/tier`. Two write entrances for one
  fact will eventually leave one of them missing the other's validation.
- **An invalid `scope` is a 422, never a silent fall back to `mine`** — a
  mistyped `?scope=al` returning a narrowed list under a 200 reads to the caller
  as the complete answer.
- **`GET /users/resolve` is callable by any signed-in user**, which is a
  registered, accepted trade-off for an internal deployment: it makes usernames
  probeable one at a time. The alternatives are worse — restricting it to group
  admins makes adding the first member impossible, and a fuzzy search replaces
  one-at-a-time probing with bulk enumeration. The response is deliberately only
  id, username and display name: no email, role, or usage.
- **Orphan edges are labelled, not hidden.** An edge pointing at a group that no
  longer exists comes back with `principal_kind="missing"`. Deleting a group
  clears its edges in the same transaction, but `principal_id` carries no foreign
  key, so a database merge can resurrect them; `scripts/merge_dbs.py` sweeps
  those, and the label is what lets a library owner understand and delete any
  that survive.

### Member-contribution approval flow (P2)

A plain member who wants to share **their own** library with a group cannot issue
the grant directly (they are only a plain member of that group), so they go through
"request → group-admin approval". **Watch the direction axis**: the requester holds
manage rights on the library (owner/admin) but is only a **plain member** of the
target group; a group admin sharing into a group **they administer** always uses the
existing grants endpoint and never touches this table.

- The request is a **double condition**: manage rights on the library (enforced by
  the `notebook:manage` dependency) **and** membership of the target group (checked
  in the endpoint body as a non-empty `user_group_role`; a plain member is enough).
  A non-member gets the same **404** as "the group does not exist" (group visibility
  rule — the existence of the group is not disclosed).
- The state machine is **one-directional** `pending → approved/rejected`.
  **Withdrawal is not a third status**: the requester `DELETE`s a **pending** request
  as a whole row. An approved/rejected request is a settled decision and withdrawing
  it is meaningless — the store decides on exact status, an already-decided withdrawal
  maps to **409**, and a request that does not exist (or does not belong to this
  library) is 404. `decided_by`/`decided_at` therefore stay purely "the group admin's
  decision" and withdrawal writes neither.
- **Approval writes the `(group, viewer)` edge and marks the request `approved` in
  one write transaction**; it is **idempotent** when already shared (same library,
  same group) — approval never fails because the edge already exists, which would
  strand an un-approvable request. Concurrent double-review is blocked by the store's
  row lock plus exact status matching. Rejection only marks `rejected` and writes no
  edge; the requester sees "rejected" and may **re-submit** for the same (library,
  group) (a `rejected` row does not occupy the `WHERE status='pending'` partial unique
  index).
- **Idempotent submission**: when a pending request already exists for this
  (library, group), the create endpoint **returns that existing row** rather than a
  409 — a requester refreshing the page and re-submitting is a common action that
  should not raise an error (`uq_share_requests_one_pending` caps one in-flight
  request).
- A request grants no permission: `pending` enters no decision predicate, and the
  grant table stays purely live grants. Deleting the group or the library carries the
  requests away through FK CASCADE. The bell: a group admin's pending-request count
  enters the pending-actions center (reusing `pending_actions` with the same read
  predicate).
- `status` is exact-matched against `pending`/`approved`/`rejected` and never used
  as `!=` for a "decided" test; `decided_at` is only ever written as SQL `NULL` or an
  ISO timestamp, never the empty string (it is this table's only nullable time column
  entering the forward shadow, and `''` would type-error PostgreSQL's `timestamptz`
  and poison the replication channel).

### Mount configuration and link sharing stay owner-only (P2)

P2 flipped content management to group admins, but **mount configuration and
`share_token` link sharing** deliberately stay with the owner — they configure the
owner's own retrieval scope and outward disposal, do not transfer with content
management, and get their own capability cell `notebook:configure` (owner-only, not
folded into `notebook:manage`). Two hard reasons:

- **Mount configuration**: `mount_sql`'s "same-owner candidate" is resolved by the
  *mounted* library's owner. A group admin who could edit mounts would enumerate the
  owner's **never-shared** private libraries through `GET /notebooks/{id}/mountable`,
  `PUT .../bases` them into this shared library, and read them whole through the
  active-notebook proxy endpoints — a privilege-escalation read channel.
- **Link sharing**: `share_token` can mint a sign-in-free public link on the owner's
  behalf that lets anyone outside the group copy the whole library. In particular
  `DELETE /notebooks/{id}/share` (unsharing the link) **also drops every read-only
  member** (`clear_share` clears `notebook_members`), a blast radius beyond content
  management, so it deliberately stays under `notebook:configure`.

So "a group admin can manage sharing" means managing **grant edges**, **not** touching
mounts or links: `notebook:manage` covers rename (`PATCH /notebooks/{id}`) plus
grant-edge management (`GET`/`POST`/`DELETE /notebooks/{id}/grants`), while
`notebook:configure` covers mounts (`bases` / `mountable`) and link sharing (`share` /
`mounted-by-count`).

### Read access implies mountability, and the borrowed-mount gate

Mount validity was historically "public library ∨ same owner", specifically
*excluding* read-only shares, on the grounds that a mount edge would outlive a
revoked share. That concern is answered by the live predicate — revoke the grant
and the edge stops being valid — so **read access now implies mountability**,
which is exactly the "project members mount the project library" requirement.
`GET /notebooks/{id}/mountable` widens accordingly.

But the historical worry had a second half that liveness does *not* answer:
**re-sharing what you borrowed**. Carol shares Y with Alice; Alice mounts Y into
her X; Alice shares X with Bob — and Bob now reads Y through X's proxy reads and
federated retrieval, though Carol never authorized him. Nothing was revoked here;
a *new* share on the mounting side conjured a new set of readers.

So the restricted-read branch of the mount predicate carries an extra condition:
a borrowed mount is valid only while **the mounting notebook itself has not been
shared** (no `notebook_members` rows and no `notebook_grants` rows). Share the
mounting notebook and the borrowed edge goes inactive immediately (the edge is
kept and greyed out, matching the existing inactive-edge convention); un-share it
and the edge recovers on its own. `tier='base'` and `everyone` grants are exempt
— their audience is already everybody, so passing them on adds no exposure, and
this is the only reason `access_sql` splits the edge test into a restricted and
an `everyone` fragment (the read predicate itself does not distinguish them). The
same-owner branch is exempt too: sharing your own X is disposing of your own
content. The predicate looks only at "has this been shared", never comparing the
two audiences, because an audience comparison would expand membership/group/edge
tables across two libraries on a path that runs at every participant-set
resolution. The product reading matches "mounts do not cascade": **what you
borrowed, you do not lend on.**

Two UI consequences: the share dialog warns *before* the sharing action that
borrowed reference libraries will pause participating in retrieval, and an edge
disabled by this gate gets its own explanation ("this notebook has been shared,
so borrowed reference libraries have paused; un-share it to restore") instead of
the older fixed "this library is no longer public and is not yours", which is
simply untrue for a borrowed edge. The gate's own predicate is derived from the
mount predicate in `MOUNT_GATE_CLOSED_EXPR`, never re-spelled at the consumer,
and it also keeps the library's real name visible, since the mounting owner still
legitimately holds read access.

### Group libraries in the notebook list

A library readable through a live group edge, where the viewer is neither owner
nor a `notebook_members` row, forms the notebook list's **群组** partition.

- `NotebookSummary.access` stays `"reader"` — no new enum value (decision 7).
  Group members get exactly the reader permission tier and behave identically:
  hidden write buttons, the same read guard on Ask, default-all retrieval scope,
  hidden-participant snapshots. Where the access *came from* is an orthogonal
  dimension, so it gets its own field.
- `NotebookSummary.granted_via` is a list of `{group_id, group_name, kind}` and
  drives the card's "来自群组《X》" label. It is empty for owned and
  share-token-joined libraries, so old behavior is unchanged verbatim. Both the
  list **and the detail** path fill it in; the de-duplication rule is identical on
  both: **the membership row wins** — somebody who both joined by share link and
  sits in a granted group gets an empty `granted_via` and keeps a working "退出
  共享" button, because that button deletes precisely that membership row.
- Conversely, a card whose `granted_via` is non-empty **must not** show "退出
  共享": that button only deletes a `notebook_members` row and does nothing to an
  authorization edge, so pressing it while the library stays in the list is a
  guaranteed false failure. It is replaced by a static explanation that the group
  admin governs this access.
- `GET /notebooks/{id}/mountable` now returns `MountableNotebook`, which adds
  `origin ∈ {base, mine, shared}`, projected from columns `MOUNT_VALID_EXPR`
  already reads (`tier` and `created_by`) at zero extra queries. The mount picker
  groups by it into 公共知识库 / 我的笔记本 / 共享给我的. Without it, the
  read-access widening would file other people's libraries under "我的笔记本" — a
  plainly false label. Priority is base → mine → shared, so a public library you
  own is still `base` and the picker's pre-existing grouping is byte-for-byte
  unchanged; only the newly admitted rows land in the third group. The field is
  deliberately on a **new model** rather than on `NotebookRef`, which is also
  `MountedBase`'s base class and the response model of a query that does not
  compute this flag.
- Owner-side "已分享" is now a **union**: a read-only share (`notebooks.is_shared`)
  **or** at least one edge pointing at a group. The card badge and the
  `shared-by-me` overview use the same criterion, and `SharedByMeItem.group_count`
  carries the number of distinct groups. A row with an empty `share_token` and a
  non-zero `group_count` is one that exists purely because of group sharing —
  there is no link to hand out. Rows without a link are not size-counted and their
  members are not queried, because `mode` / `size` / `members` all describe the
  link.
- The pending-actions bell resolves the notebook name for reports created in
  libraries the user does not own (previously blank), and its report half is
  evaluated outside the owned-notebook gate, since its predicate consumes only
  `created_by` and not a single notebook id — a member with no libraries of their
  own otherwise had a permanently-zero bell while a report of theirs waited at
  `intent_ready`.

### `GET /notebooks/{id}/share` has no side effect

Opening the share dialog used to `POST .../share`, so a user who only wanted to
share with a group was issued a share link as a by-product: a purely
informational action with a persistent side effect. The new `GET` returns the
current link state without minting a token, and the link is created only when the
user explicitly asks for it (that `POST` remains idempotent and returns an
existing token as-is). With no token, `share_token` is the empty string and
copy-statistics are **not** computed — that is a real size measurement, not cheap
on a large library, and a library with no link has no use for it. `copyable` and
`size` are therefore meaningful only when `share_token` is non-empty, and
consumers must test that first.

### Deep reports in a shared notebook

Report creation follows **read** access, and reports are isolated **per creator**.

- Creating a report needs read access on the notebook. Every endpoint that
  touches an **existing** report — detail, confirm intent, edit outline,
  generate, cancel, delete, share, read share state, unshare: nine of them —
  additionally goes through `report_routes.py::_own_report_or_404`, the row-level
  check that `reports.created_by` is the calling user (an AST guard pins that
  every such endpoint calls it). List and export narrow by the same predicate in
  SQL. Somebody else's report is indistinguishable from a nonexistent one (404).
- **The notebook owner is no exception**: they see only reports they created
  themselves. This deliberately avoids introducing an "owner sees everything" new
  disclosure. To show a report to someone else, use the existing public link.
- Because the capability follows read access rather than *how* that access was
  obtained, a plain read-only member who joined through a share token gains
  report creation too. This is **intentional**, not a group-feature spillover —
  the capability expresses "who may read this library" — and it is an outward
  behavior change from previous versions.
- A member's public report link is a **delegated surface whose lifetime equals
  their read access**. `GET /public/reports/{token}` re-checks, on every request,
  that the report's *creator* still has read access to the notebook
  (`user_can_read_notebook(notebook_id, created_by)`, both ids passed explicitly
  — the anonymous router binds no request user and must never touch the
  `current_user` ContextVar, which falls back to the seeded administrator when
  unset). Failure returns the **same 404 as an unknown token**, because a
  distinguishable response would report somebody's group membership to an
  anonymous caller. Restoring access revives the link, matching the token's
  existing idempotent semantics. This is the same philosophy as live mount
  validity, and for the same reason: there are several ways to lose read access
  (edge deleted, left the group, group deleted, membership row dropped, notebook
  re-tiered) and a cascade would have to be re-derived at each one — miss one and
  a permanent back door remains. Concretely, once a member loses access, neither
  the member (stopped by the read guard) nor the owner (stopped by the row-level
  creator check) can reach `unshare`, so a stale token would otherwise serve the
  owner's corpus forever. Owner-authored reports and all historical reports are
  unaffected (creator = owner, whose read access always holds).
- Administrative usage attribution follows the same rule: `GET /admin/users`
  counts reports by `reports.created_by`, not by notebook owner, matching the
  existing `questions` predicate (counted per submitter, including submissions in
  shared notebooks) and the per-notebook breakdown and activity feed, which were
  already creator-scoped.

### Registered limits and trade-offs

- **Group name: 120 characters. Group description: 1,000 characters.** Both are
  user-edited data, so an over-limit value is **explicitly rejected** and never
  silently truncated.
- **The list endpoints are unpaginated** — the member roster in `GET
  /groups/{id}`, the full inventory under `?scope=all`, `GET
  /notebooks/{id}/grants`, and the two P2 approval-flow lists (`GET
  /groups/{id}/share-requests`, the group's pending review queue, and `GET
  /notebooks/{id}/share-requests`, the requester's own echo). This is a **settled
  trade-off, not an omission**: the design targets at most a few hundred people per
  group (decision 11), a scale at which returning everything in one response holds;
  the request lists carry only **pending** rows and at most one in-flight per
  (library, group) (see below), so they are smaller still. It is written down here so
  nobody later reads "no pagination" as a bug.
- **One pending request per pair.** `uq_share_requests_one_pending` caps a single
  in-flight request per (library, group). The create endpoint hitting it **returns
  the existing pending row idempotently**, not a 409: a requester refreshing the page
  and re-submitting is a common action. This is a **product-behavior contract, not a
  quota**, recorded here because it bounds the request lists' size.
- **The 群组 partition amplifies a known N+1**, kept as registered debt.
  `granted_notebook_rows` is a single query, but every row then goes through
  `NotebookSummaryQuery.from_row`, which issues several count/mount queries per
  library — roughly 7 statements per library, so on the order of 3,500 statements
  for 500 granted libraries. The "own libraries" and "joined libraries" sections
  have the same existing shape, so this is not a defect introduced by group
  sharing, but the group partition raises the plausible row count by an order of
  magnitude. Batch count pre-fetching is the optimization direction, scheduled
  separately.
- **No decision cache.** A single decision is a handful of indexed `EXISTS`
  probes (grants by `notebook_id` or `(principal_type, principal_id)`,
  `group_members` by primary key or `user_id`), which is the same order as the
  previous predicate; hot paths add no full-table scan.

## Knowhow tables

A notebook's **Knowhow 表** action (opened as its own panel, alongside Knowledge Graph) manages **knowhow tables**: structured domain know-how captured as rows of experience entries under free-form column names. The shipped example is semiconductor timing-violation triage (one row per violation type; columns for symptom identification, root-cause analysis, fix method, tooling), but columns are plain user-defined text, not a fixed vocabulary. A table starts either from an import (xlsx/csv/Markdown, with a column-to-kind mapping preview) or from a **create-table wizard** (define the column headers first, then fill in rows). New-table import asks whether attributes are arranged by column (the default: first row is the header) or by row (first column contains attribute names); row-oriented input is transposed on the backend before preview and commit, so the internal grid, append-import contract, and projection pipeline remain column-oriented. Structural validation failures are returned as safe, actionable wizard copy. In particular, an attribute-row workbook whose record groups use horizontally merged cells is recognized after merge expansion and directs the user to select **属性按行**; duplicate/blank headers, unsupported files, invalid encodings, and stale column settings explain what to change instead of collapsing to a retry-only message. Values can be entered two ways, freely mixed: in-app through a **cell editor** — a Markdown editor that defaults to a single focused column and toggles to a side-by-side or full preview (choice remembered per session), with paste-or-drag image upload, local autosave drafts (every exit persists unsaved edits as a restorable local draft first and refuses to leave if that write fails, and leaving via Esc/backdrop/× or switching cells asks first), and a *save and move to the next cell* flow for fast sequential entry — or offline through an **Excel template round-trip**: download the table's current header as an `.xlsx` template (header row frozen), fill it in bulk, then upload it to append rows (a preview reports unmatched columns and rows whose title duplicates an existing one before you commit).

At most one column can be designated the table's **行标题列 / row-title column** (a table-level choice, not a per-column tag). With one set, every non-empty cell becomes a knowledge-graph node whose *type is its column name*, linked by an `about` edge back to that row's title-column node, and identical values in the same column across different rows merge into one node (ten rows citing the same tool become one tool node with ten incoming edges). Leave it unset and the table stays retrieval-only — cells still become searchable chunks for Ask, but nothing is added to the graph, which is the right shape for log-like tables where a row is a record rather than a named thing.

Projected cell knowledge objects enter reasoning/graph KG-node retrieval by default (`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED=true`), so a matching cell can seed graph traversal and its citation retains the direct row-drawer jump. Setting the flag to `false` is a reversible rollback of only this direct-object path; per-cell chunks remain searchable in Ask. Default-on type widening is limited to objects owned by the table's `hidden_source_id`—unrelated custom Schema types are never swept in—and the scoped type set plus normalized chunk-vector→object bridge are version-cached/single-flight across reasoning subqueries. The bridge generation includes both KG mutation state and the scoped chunk-vector count/timestamp, so vector-only repair refreshes it even though that repair intentionally does not mutate KG state; KG mutations also evict it explicitly.

Projection status is a table-completion contract, not a per-row progress shortcut: rows remain `pending`/`syncing` until the table-wide chunks, embeddings, knowledge objects/relations, mutation sequence, and graph-cache notifications have finished. A row is published as `synced` only after that terminal work succeeds, so callers that observe every row settled can immediately read the completed graph without a scheduling race. Publication is conditional on the table mutation sequence captured by the pass: an older pass can never overwrite the `pending` marker from a newer concurrent edit while its scheduler rerun is queued.

Each column also carries a **content kind** — a deterministic parsing hint, never an LLM call: **方法步骤 / procedure** cells parse as an ordered list of steps, **工具/事物 / entity** cells split on list items/newlines into one deduplicated node per item, and **普通 / attribute** cells stay a single node. Both the cell editor and the row-detail drawer expose an explicit **优化表达 / optimize wording** button (never triggered automatically): it uses the system service bound to `knowhow_optimize` to tidy structure and phrasing while preserving meaning, shows the rewrite side-by-side with the original, and only replaces the cell after you accept it, one cell at a time.

Row/table **one-click reformat** freezes one whole-table snapshot, then generates candidates with bounded client concurrency rather than an unbounded `Promise.all`: the cap is the smaller of three and the live `knowhow_reformat` service capacity, with a safe fallback of two when status cannot be read. Equal `(column_id, trimmed original Markdown)` inputs share a single in-flight request; only a successful, still-fresh result is reused. Cancellation or closing stops launching work and ignores late responses. Progress counts physical cells, partial failures remain retryable, and confirmation still saves complete physical/shared units serially. Every save keeps `expected_before`, anchor designation, exact complete-group membership and HTTP 409 stale guards; stale candidates stay visible for rerun and the parent table reload waits until the dialog closes. Observing any stale result records that pending reload immediately, even if the user then aborts other slow requests. Each changed or saved queue entry opens an in-dialog raw Markdown diff (line additions/deletions plus bounded inline token highlighting for Chinese, Latin text, punctuation and whitespace), with rendered preview as an optional view. Oversized inputs fall back to a bounded prefix/suffix summary. A saved entry can close the batch dialog safely and open the existing detail view for its physical cell; if the batch also observed stale data, the parent first awaits an epoch-guarded detail reload and recomputes the target from those fresh rows. A failed/invalidated reload or a row/column that disappeared opens nothing and surfaces the existing recoverable table-action error. A shared/merged value uses the stable representative with the smallest row position and then row id. This remains one overall confirm operation, not per-item accept/reject.

The main grid keeps `table-layout: fixed`, horizontal scrolling and its sticky first column, but emits a `colgroup` whose widths come from a bounded pure summary of the header plus at most 64 visible rows (first 48 and last 16). Each sampled cell is truncated to a fixed code-unit prefix before newline normalization, Markdown regexes/splitting, or grapheme segmentation; estimation then examines at most eight visible lines and 120 graphemes, discounts Markdown control syntax, and weights CJK/full-width/emoji above ASCII before applying per-column min/max clamps. Status/action columns remain fixed. Narrow screens use tighter clamps. The calculation is memoized from table identity, columns and visible rows, so rendering never performs an unbounded R×C or whole-cell scan. Manual resizing and width persistence are not part of this behavior.

Knowhow ownership and authorization continue to use stable user ids (`created_by`, owners and permission checks). Human-facing audit snapshots (`knowhow_changes.actor`, milestone creators and cell-code updater labels) use the session user's trimmed `username`, then trimmed `display_name`, then user id; Agent writes keep the Agent `profile_name`. All ordinary Knowhow write entry points use the same actor-label helper, while copy/import/transfer paths pass identity ids and audit labels separately. Existing id-shaped audit values are not rewritten: read APIs resolve a bounded set of recognizable legacy user ids to current usernames in bulk and otherwise return the stored value, without N+1 queries, and async routes run this synchronous identity projection in a threadpool. For an `origin=agent` history change, only legacy human updater ids inside the semantic `payload.before` snapshot are resolved; the Agent actor and `payload.after`/`current` updater labels remain opaque profile text even if they resemble a user id. Wire field names remain compatible. In particular, stored `knowhow_cell_code.updated_by` participates in the table fingerprint and is never batch-rewritten merely for display; new writes store the label, single-cell GET/PUT responses include that readable `updated_by`, and reads project display values without changing history.

The row-detail drawer, and each physical branch in a row-title-group matrix, provide an explicit **智能补全空列** action. It produces suggestions only for stored-empty cells in that row (a missing value or exact empty string; whitespace-only stored content remains existing content). One request gathers two evidence channels: at most eight rows from the same table that fill a requested target column, preferring the same row-title group and then known-column similarity/coverage; and one bounded `ReasoningRetriever` run over the active notebook plus its currently valid explicit reference-library mounts. The latter follows the Ask `reasoning` planning, federated retrieval, reflection, graph expansion, and evidence-backed query-time chain-traversal family, but its completion-specific policy removes private Memory and the current table's own projection before candidates reach model reflection, and disables provenance-opaque PPR/community expansion as well as the exact-lookup identifier channel (its query is a JSON envelope, not a question, and would otherwise probe the envelope's own field names). It never invokes Ask answer synthesis, creates a conversation/job, or saves an Ask answer. The structured response contains a suggestion or abstention for each requested column, confidence, basis, accepted table-row ids and server-issued library-evidence keys, plus the final reasoning trace and bounded evidence cards. Unknown evidence keys are removed, and a suggestion left without a valid table or library citation becomes an abstention. When personal and base evidence conflict, synthesis follows the base evidence and says so. The draggable review dialog shows same-table references and inert library-evidence Markdown (no links or images) separately; users accept entries individually, and nothing is written automatically. Accepting a suggestion uses the normal cell update with `expected_before=""` and `origin="llm_complete"`, so a cell filled while the suggestion was being prepared is never overwritten and normal history and synchronization continue to apply. Both `reasoning_agent` and `knowhow_complete` must be configured and treat evidence as untrusted data through system-level instructions. Invalid reasoning responses, unavailable providers, retrieval/synthesis failures, and unparseable or unusable top-level synthesis responses return an explicit failure; malformed individual suggestions are filtered, downgraded, or converted to abstentions. No path returns a table-only or fabricated offline substitute.

Ask citations that resolve to a knowhow cell jump straight to that row's detail drawer instead of the generic source view. A notebook's deep copy carries knowhow tables over in full — every table, column, row, cell, and code attachment gets a remapped id in the copy — without re-running embeddings, since cell text that didn't change keeps its existing vectors.

The external-Agent surface (HTTP + MCP, discrimination sets, code attachments) is documented under [Memory and Agent MCP](#memory-and-agent-mcp); the HTTP paths are listed under [APIs](#apis).

## Memory and Agent MCP

For a UI-to-CLI operational walkthrough and a runnable official-client example, see the
[Agent MCP and Memory onboarding SOP](./agent-mcp-memory-sop.md). This section remains the
authoritative product/API contract; the SOP owns day-one setup and verification steps.

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

Ask-born Memory persists the answer's citation provenance and exposes it in each Memory
card as the source display title, distinct original uploaded file name when present,
location, and quoted span. In the notebook-local Memory tab a live citation can reopen the
exact source element through the active notebook participant scope. A copied or moved
Memory keeps the nested original citations as an explicitly archival record; those ids are
not treated as live navigation authority in the destination notebook.

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
`memory:propose`, `ask:execute`, `knowhow:code`, `sources:write`, `sources:delete`, and
`maintenance:execute`; it can be revoked immediately. Install the backend
requirements (which include the official `mcp>=1.26.0` client/server SDK), start the backend,
then connect to the Streamable HTTP server at `/mcp/` (`/mcp` reaches it through a 307).
The one-time token receipt also links to anonymous `GET /api/agent-mcp/onboarding`, a
machine-readable Markdown handoff that prints `MCP_PUBLIC_URL` verbatim as the endpoint to
configure — never a rewritten variant, since a proxy may publish only that exact route — while
stating that a backend-direct `POST /mcp` is a 307 to `/mcp/`, so an Agent whose client does not
preserve method, body and Authorization across a redirect knows the remedy. Its tool list comes
from the deployed server's frozen catalog; `mcp_server.PUBLIC_TOOLS` derives from the same
default frozen combined catalog. The user gives this link and token to
the Agent separately; the endpoint never accepts, embeds, or reflects a bearer token and is
available even while repository warm-up is still running.
Requests carrying any query string or Authorization header are rejected. Startup likewise
rejects an `MCP_PUBLIC_URL` that is not absolute `http(s)` with exact `/mcp` path, or that
contains userinfo, a query/fragment, whitespace/control characters, or a backtick.
By default MCP allows remote plain HTTP and relaxes Host/Origin (DNS-rebinding)
checks — intended for a trusted private network — and prints a startup warning
because the Agent token then travels in cleartext. On any public deployment set
`MCP_REQUIRE_HTTPS=1` to enforce HTTPS (and restore Host/Origin validation), and
set `MCP_PUBLIC_URL` to the public HTTPS `/mcp` URL.

**Long-running tools heartbeat, and the transport answers over SSE.** MCP clients do
not wait indefinitely for a tool call: Claude Code applies an *idle* timeout — it aborts a
call that has produced neither a response nor a progress notification for a while — and
other clients apply a flat per-call ceiling instead. `ask_notebook` in `reasoning` mode
routinely runs for minutes (plan, federated retrieval, reflect loop, synthesis) and
`build_kg` can take longer still, so without a heartbeat the client abandons a call the
server is still executing successfully and the Agent sees a transport error where the
answer was about to arrive. Every one of the 23 core tools therefore runs its blocking body
under one progress heartbeat that fires every **5 seconds** and carries only the tool name
and elapsed wall-clock seconds — never the question, a notebook or source name, or any
other notebook content, the same rule the observability events follow. It is free where it
is not wanted: the notification is a no-op unless the client asked for progress with a
`progressToken` in the request's `_meta`, and the first beat is a whole interval away, so a
tool that answers in milliseconds never sends one. It never fails a call either — if the
notification cannot be written (the client hung up) beating stops and the work runs to
completion. The heartbeat is not a timeout of ours: nothing on this surface gives up on
work that is still running.

Delivering those beats requires the Streamable HTTP transport to answer over
`text/event-stream` rather than a buffered JSON body, because in JSON mode the SDK drains
the per-request stream looking for the response and **discards every notification it passes
on the way** — `report_progress` still "succeeds" and the client receives nothing. The one
user-visible cost, stated rather than left to be discovered: a `POST /mcp/` must send
`Accept: application/json, text/event-stream` or the transport answers **406 Not
Acceptable**. The Streamable HTTP specification already requires clients to send both, and
the official SDK, the tests and the SOP's hand-rolled `curl` all do. Responses carry
`X-Accel-Buffering: no` and a 15-second SSE keep-alive comment, so an nginx in front does
not buffer the stream and silently defeat the heartbeat; a proxy that ignores that header
needs response buffering turned off for the `/mcp` location and a read timeout above the
longest answer the deployment expects. Note the two layers are different: the keep-alive
comment is raw bytes that keep HTTP and proxy timers alive, while only the progress
notification resets a client's MCP-level idle timer.

The client's own ceiling remains the outer bound and is the client's to configure — a
server cannot raise it. For Claude Code, set `"timeout": <milliseconds>` on the server's
entry in `~/.claude.json` (or the project's `.mcp.json`) and restart it; the environment
variables `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` and `MCP_TOOL_TIMEOUT` are the global
equivalents. Codex uses `tool_timeout_sec` on the server entry. Defaults differ by client
and version, so raise the ceiling for a deployment whose `reasoning` answers or builds run
long instead of relying on any particular default.
Expiry values must include an explicit timezone offset; the browser converts its local
datetime input to UTC and the backend stores a normalized UTC instant. Naive datetimes are
rejected rather than interpreted in the server's local timezone.

For Codex, place the issued token in an environment variable and register the server:

```bash
export SILICON_NOTEBOOK_AGENT_TOKEN='<one-time-issued-token>'
codex mcp add silicon-notebook --url 'http://127.0.0.1:8000/mcp/' \
  --bearer-token-env-var SILICON_NOTEBOOK_AGENT_TOKEN
```

Codex persists the environment-variable name, not the token value. A transient `export`
inside an Agent shell subprocess cannot change the running client's parent environment.
An Agent may save the URL/configuration, but without an approved persistent secret mechanism
it must ask the user to set the variable in the environment that launches Codex and restart.
`codex mcp list` proves configuration presence only; authenticated success requires a new
session to discover the MCP and complete `list_notebooks` plus `select_notebook`.

For Claude Code, the currently installed CLI accepts an HTTP transport and an explicit
Authorization header, and resolves `${VAR}` inside that header at connect time from the
environment of the process that launched it:

```bash
claude mcp add --transport http silicon-notebook 'http://127.0.0.1:8000/mcp/' \
  --header 'Authorization: Bearer ${SILICON_NOTEBOOK_AGENT_TOKEN}'
```

Single-quote the header so the shell does not expand it first; the stored configuration then
holds the variable name instead of the credential. An undefined variable is sent verbatim and
fails as a bad token with no configuration-time error, so only a real connection proves it
resolved. Without `-s user` the entry is registered for the current directory only. A client
that cannot interpolate persists the raw header instead: use least-privilege scopes, a short
expiry, protect the local config, and revoke/rotate the token after use.

Every new MCP session must call `select_notebook` before a data tool. The default core tool set
is these 23 tools; `mcp_server.PUBLIC_TOOLS` is exactly these 23 tools, not a larger combined
catalog -- it is the same list as `mcp_server.CORE_TOOLS`:

| Group | Tools | Scope |
| --- | --- | --- |
| Memory / context | `list_notebooks`, `select_notebook`, `search_agent_memory`, `search_notebook_context`, `get_memory`, `ask_notebook`, `propose_memory` | `knowledge:read` / `memory:read` / `memory:read_candidates` / `memory:propose` / `ask:execute` |
| Knowhow | `list_knowhow_tables`, `get_knowhow_discrimination`, `get_knowhow_row` | `knowledge:read` |
| Knowhow code write | `put_knowhow_cell_code` | `knowhow:code` |
| Citation point-read | `get_cited_element` | `knowledge:read` |
| Source management | `add_source_text`, `add_source_file`, `add_source_url`, `reparse_source` | `sources:write` (owner-only) |
| Source deletion | `delete_source` | `sources:delete` (owner-only, Agent-added rows only) |
| Source read | `get_source_status` | `knowledge:read` |
| Build | `build_kg`, `build_retrieval_index` | `maintenance:execute` (owner-only) |
| Build read | `get_build_status` | `knowledge:read` |
| Notebook understanding (agent) | `get_notebook_profile`, `add_observation` | `agent_profile:read` / `agent_observation:write` |

The deployed server-local frozen catalog is authoritative for discovery and onboarding: it is
exactly the 23 tools above, derived live from the seven core registrars, and
`mcp_server.PUBLIC_TOOLS` is that same list rather than a second hand-kept copy. Every call
repeats live token/scope/allowlist/membership checks, and every write scope is forced through the
owner-only notebook gate. Results are copied into a bounded shape while being built -- no deeper than 5 levels, with
per-field/map/list limits applied one entry at a time -- so an oversized container is never fully
materialized before being cut down. That bounded copy is then progressively and visibly shrunk
(longest strings, then map entries, then list items, then identifiers as a last resort) until it
fits the 12,000 UTF-8 byte total budget; every cut is reported back in a `truncation` field
(`truncated`/`omitted_items`/`omitted_map_entries`/`omitted_characters`/`omitted_fields`).
Exceptions surface only as stable error codes; FastMCP schema errors occur before the tool body
and remain transport/request audit events. Only when the copy truly cannot be shrunk any further
does the call fail outright, rather than returning a silently truncated result.

`list_notebooks` and `select_notebook` require **no scope at all**: the entire check is a
live token, a notebook inside its allowlist, and read access to that notebook. Every session
can therefore bootstrap regardless of how narrow the token is.

The server rechecks scope, allowlist, token state, and notebook access on data calls;
retrieved text is untrusted evidence, not executable Agent instructions.

`ask_notebook` accepts an optional `conversation_id` (at most 200 characters, mirroring
`AskIntentPreviewRequest.conversation_id`) and returns the `conversation_id` the answer was
actually recorded under. Passing an id continues that conversation across turns, including
one started by another Agent profile or in the web UI, as long as it belongs to the same
owner and the same selected notebook. An id from a different notebook or owner does **not**
error: the server silently starts a new conversation, which the caller detects by comparing
the returned id against the one it sent. Each anchor additionally carries `source_id`,
`element_id`, and, for a knowhow-projected node, `knowhow: {table_id, row_id}`. A separate
`citations` list carries the fallback (non-anchor) evidence with `label`, `source_id`,
`element_id`, `location_label`, `quoted_span`, `source_file_name`, and `tier`; `notebook_id`
and `memory_id` are emitted only when non-empty, and a knowhow-projected citation carries the
same `knowhow: {table_id, row_id}` pair anchors use. Rows whose `memory_id` is set require
`memory:read` — without that scope the whole row is filtered out **before** the result cap
and does not contribute to the truncation count, because reporting it would leak the private
Memory count by arithmetic. Anchors and citations are each capped at 20 rows. The response
budget applies in two stages: each anchor's `provenance` is fitted to 500 characters
individually first, and only then is the anchors list as a whole compressed to 3,500;
citations are pre-fitted to 1,800 characters, so a large citation set cannot crowd out the
answer text.

`get_cited_element` dereferences one citation back to its source text: pass `source_id` and
`element_id` exactly as `ask_notebook` or `search_notebook_context` returned them and get
that element's own text, its location inside the document, and the document's display title.
It discloses nothing beyond what an answer in the selected notebook may already cite — the
notebook's own sources plus the reference libraries it currently mounts.

**Source management.** Every tool here that names a `source_id` resolves it inside the
**selected notebook only** — never the mounted participant set, and never a hidden
`memory`/`knowhow` projection row. That is narrower than `get_cited_element`, which
deliberately spans the mounted reference libraries because an answer's citations already do.

`add_source_text` files a Markdown document from text the Agent
provides: `title` is at most 200 characters and `content_md` must be
non-blank and within this deployment's `SOURCE_UPLOAD_MAX_MB` per-source ceiling, measured on
the stored UTF-8 bytes. The submitted title is stored verbatim as the source's title; the
on-disk file name is a separate, derived value — sanitized, capped at 200 UTF-8 bytes (so the
`{source_id}_` prefix and the `.md` suffix still fit a 255-byte path component) and suffixed —
and an over-long title shortens that file name only, never the stored title. Re-adding
byte-identical content returns the existing source with `reused: true` instead of creating a
duplicate. `add_source_file` accepts exact local-file bytes as strict standard base64 in
`content_base64` (no whitespace and no data-URI prefix), with the original `file_name` and an
optional title. Its suffix admission comes directly from the backend parser registry, so it
supports the same local PDF, Markdown, DOCX, PPTX, CSV, XLSX/XLS, and Markdown-ZIP formats as
the browser. Decoded bytes must be non-empty and fit the deployment's ordinary per-source upload
ceiling; the file name must fit one 255-byte UTF-8 filesystem component and the optional title
uses the same 200-character source-title rail. A ZIP is not unpacked by the MCP layer: the raw
bytes enter the ordinary upload/dedup/background scheduler, then the built-in Markdown-bundle
parser persists its relative images exactly as a browser upload does. `add_source_url` adds a
PDF by URL and refuses anything the server cannot reach or probe as a PDF. All three respect the notebook's document limit, except that a re-add which
resolves to an existing source is still allowed at the limit — it adds no document, and
refusing it would break the idempotence above exactly where a retry needs it most. Parsing runs in the background, so poll `get_source_status`, which returns
`parse_status`, `status`, `element_count`, `kg_extracted` (whether the source has knowledge
objects in the graph), `kg_analyzed_empty` (analysis DID complete and this document legitimately
yielded no knowledge objects — a text-poor or image-only scan), `agent_created`, and — instead
of the raw `error_message`, which is `str(exc)` stored
verbatim and routinely carries server-side absolute paths — a derived `parse_failed` boolean
plus `parse_quality_warning`. The latter is the MinerU degradation signal: layout, formulas,
and tables may be wrong even though the source reached `extracted`, which an Agent about to
cite it needs to know. `reparse_source` re-runs parsing and extraction for one source and
refuses while that source's parse lock is held (a bounded ~0.5-second probe, not a wait:
that lock spans two LLM calls, so a parse genuinely in flight will still be in flight a
second later).

`delete_source` is irreversible and deliberately narrow. It needs `sources:delete`, which
`sources:write` does not imply, **and** the row must have been added by an Agent. The
criterion is the `agent_created` boolean — the projection of the v48 `sources.agent_profile_id`
provenance column being non-NULL — so a document a person uploaded can never be removed
through this surface, no matter which scopes a token carries. The criterion is "some Agent
added this row", not "this profile did": Agent identities get rotated and revoked, and a
source left by a retired profile would otherwise be undeletable forever. Provenance is
written on the INSERT branch only, so re-uploading a person's bytes reuses their row and
leaves it user-added, and a notebook deep copy clears the column outright — every copied
source counts as user-added. The `sources` list and detail responses expose the same
`agent_created` boolean, and the browser's source list renders it as a neutral 「Agent 添加」
badge.

**Builds.** `build_kg` triggers an incremental knowledge-graph extraction (already-extracted
sources are skipped; a previously partial source is retried) and `build_retrieval_index`
triggers a retrieval-index rebuild with `when="now"` (default) or `when="idle"` for the next
low-traffic window. Both are owner-only, return immediately, and are polled through
`get_build_status`, which reports KG state (ready/building, pending source count, current
job stage and progress) together with retrieval-index state (exists/building/queued, queue
position, next idle window). `build_kg` refusing because a build is already running for that
notebook is a **queueing signal, not an error**: the notebook-scoped single-flight guard is
working, and the caller should poll `get_build_status` until it clears rather than retry
immediately. `build_retrieval_index` refuses when the notebook is too small to need an index.
`get_build_status` is a pure read and any member of the notebook may call it.

Writes are owner-only across this whole surface. A token's allowlist may name a notebook its
owner merely joined as a read-only member; adding, re-parsing, deleting a source, or starting
a background build there would hand that share's read side a write it was never granted, so
those calls are refused. Reads keep the member-readable rule their HTTP twins use.

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

Unlike the source-management and build writes above, `knowhow:code` is deliberately
**not** owner-only: an Agent's write capability here is entirely scope-driven (design
doc §⑥-4), so a token whose owner joined a shared notebook as a read-only member can
still save a cell code attachment there. A code attachment is inert — never executed,
indexed, embedded, or projected into retrieval or the KG — while deleting or
re-parsing a document reaches every member's retrieval, which is why the two surfaces
carry different authority models. The divergence is a recorded decision, pinned on
both sides by `backend/tests/test_memory_mcp.py`.

**Notebook understanding (Agentic Memory P3).** `get_notebook_profile` (scope
`agent_profile:read`) reads the same [notebook understanding blocks](#notebook-understanding-blocks)
the web UI's "AI 对这个库的理解" panel shows: the shared `base` layer plus the caller's own
`mine` overlay (never another member's), each block projected down to `{label, value,
updated_at}` only — no `evidence` source ids, no `revision`, no change history, so a token
holding only this scope cannot use the response to probe source ids it has no other way to
read. Every value is marked `content_is_untrusted_evidence: true` and `citable: false` in the
response; it is prompt scaffolding for planning, never something to cite. When
`AGENT_PROFILE_ENABLED` is off, or the notebook has no consolidated understanding yet, the
tool returns `enabled: false` with empty blocks rather than erroring. `add_observation`
(scope `agent_observation:write`) appends one short line — at most
`AGENT_OBSERVATION_TEXT_MAX_CHARS` (500) characters — to the caller's own observation log for
this notebook, deduplicated per `client_request_id` the same way `propose_memory` is. The
idempotency window is **bounded by ring retention** (a registered contract, not a defect):
once `AGENT_OBSERVATION_RING_MAX` newer observations have evicted a row, retrying its old
`client_request_id` writes a fresh row — an everlasting key table is not worth a migration
for a retry contract measured in seconds. It
returns immediately; the write itself is a bounded insert plus a bounded ring-eviction delete
in one transaction, with zero model calls, so there is no async status to poll. It is the
**second** Agent write that bypasses `_writable_notebook`'s owner-only gate — the first is
`put_knowhow_cell_code` — because its blast radius is structurally capped at the token
holder's own overlay rather than the whole notebook's retrieval, so a read-only member's own
Agent can still use it; see [Notebook understanding blocks](#notebook-understanding-blocks)
for what an observation is and is not used for. When the feature is off, `add_observation`
raises rather than silently accepting data no consolidation job will ever read.

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
in flight. Probe and extraction calls forward that control's cancellation signal
to the shared streaming JSON transport. The timeout therefore guards inactivity
between received chunks (and initial response latency), rather than imposing a
wall-clock cap on a long completion that continues to make progress; a gateway
that buffers the stream or enforces its own hard request deadline remains outside
this guarantee. The transport requests the optional OpenAI-compatible usage
trailer and records exact prompt, completion, and total token counts from its
final empty-choice chunk in the existing per-user LLM log. A provider that
explicitly rejects that option falls back to ordinary streaming; the physical
client remembers the rejection so later calls do not repeat it. If a provider
accepts or ignores the option but emits no usage, the log leaves token counts
unavailable rather than estimating them locally. Other notebooks and later tasks
are unaffected. The availability
probe explicitly bypasses the LLM response cache and does not populate it, so a
stale successful probe cannot authorize destructive rebuild work during a live
outage. It reuses the configured short-output budget instead of imposing a
smaller probe-only cap, because reasoning-capable providers may consume a tiny
cap before emitting visible JSON. An HTTP-success response with empty,
truncated, or otherwise invalid JSON is recorded as `model_response_invalid`;
the UI identifies it as an unusable model response rather than an operator
interruption and preserves the retry action.

**Offline skip mode (opt-in, CLI only).** Stopping the whole task is the right
default for an interactive build, but it is the wrong trade for an unattended
run over thousands of sources: a single flaky window kills a multi-hour batch.
`scripts/batch_ingest.py kg --skip-model-failures` narrows the blast radius of
"model unavailable" from the task to **one source**. That source gets its own
child of the run control, so only its own in-flight windows are cancelled and
drained; it returns to the unanalyzed state (so simply re-running the same
command retries exactly the skipped sources) and the task moves on. The backstop
is kept: `--max-consecutive-model-failures` consecutive source-level model
failures with no success in between escalate to the ordinary task-level circuit,
publishing the same `stopping`/`kg_build_circuit_opened`. The threshold is
**enforced** to stay above `--workers`, because one transient blip hits every
in-flight source at once: omitting the flag derives `max(32, 2 × workers)`, and an
explicit value at or below the worker count is rejected rather than silently
raised. Skip mode deliberately does **not** cover the startup availability probe —
a probe failure means the service is down right now, before the operator has
invested anything, so failing fast is both cheaper and more honest than burning a
retry budget per source only to trip the threshold anyway. That counter is task-scoped and survives target paging
— extraction targets advance over 500-row raw-source pages, and a sparse
notebook can yield one target per page, so a per-page counter would never reach
the threshold. The flag is off by default and API-driven builds cannot set it;
enabling it prints a warning, and the run reports how many sources were skipped
(and their bounded ids) on success, on escalation, and on interrupt alike.

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

### Conflict-detection size boundaries

`POST /api/notebooks/{id}/kg/conflicts/resolve` is a background pass: a
zero-model detector proposes possibly contradictory pairs, then one LLM
adjudication runs per surviving pair. Both costs are bounded.

**Admission and single flight.** A notebook whose active knowledge-object count
exceeds `KG_CONFLICT_MAX_OBJECTS` (default `200000`), or whose relation count
exceeds `KG_CONFLICT_MAX_RELATIONS` (default `1000000`), is refused with a
user-readable message — accepting one only buys a background job that never
finishes and a bill that keeps growing. The object default follows from the
memory shape: vectors load as one `(N, dim)` float32 block, about 0.8 GB at
200k × 1024; the independent relation rail protects dense or repeatedly
corroborated graphs whose edge count is not bounded by the object count. The
relation admission query itself stops after the rail plus one row. The worker
also reads at most that sentinel window, closing the concurrent-write race; if
the sentinel is present, it skips the entire pass and never presents a relation
prefix as a complete conflict scan. One detection per notebook may run at a
time; a repeated click returns 409. That slot is **its own**: it does not exclude
补上关联 / 重新合并,
which share a slot with each other because they rewrite the same derived
products, whereas conflict detection writes the conflict review queue. **The
automatic pass at the end of a KG build goes through all gates too** (same
slot, same object/relation predicates): it is skipped when a detection is
already running and skipped when the notebook is oversized, each recorded as
one reason-only event, and it never fails the build.

**Candidates and adjudication.** At most `KG_CONFLICT_MAX_CANDIDATES` (default
`800`) candidates reach adjudication, allocated as **a quota per signal class**:
the edge strategies (shared head/tail, same pair different edge) and the node
strategies (discriminative + semantic) each get half, whatever one class cannot
use goes to the other, and the detector's emission order is kept within each
class. This is not a detail: the detector emits every edge candidate first, and
a single high-degree node can produce thousands of shared-head pairs, so a plain
prefix cut made the discriminative (nmos/pmos-style) candidates disappear as a
class. Whatever was dropped is reported per class (`truncated`,
`truncated_edge`, `truncated_node` in the job result, plus a counts-only event)
rather than presented as the complete picture.

**Semantic size dispatch.** The semantic strategy compares vectors of
same-type objects. A group of at most
`KG_CONFLICT_SEMANTIC_BRUTEFORCE_MAX` objects (default `512`) keeps the exact
pairwise comparison; a larger group switches to approximate nearest neighbours
taking `KG_CONFLICT_SEMANTIC_ANN_K` (default `10`) neighbours per object. That is
**approximate recall** and a registered, accepted behaviour difference: below the
threshold the original exact path still runs, and above it the exact pass was
never going to finish. One numerical change is registered alongside it: the exact
branch now accumulates a float64 dot product instead of summing boxed Python
floats, a ~1e-12 difference that can only move a pair sitting exactly on the
threshold. If the neighbour index is unavailable, only that group's semantic
strategy is skipped — recorded as one event carrying just the group size — and
the other strategies are unaffected.

## Command catalog (tool manuals)

A tool's **command reference** is the shape ordinary ingestion handles worst.
The chunker splits one command's description, arguments and examples into
unrelated segments, and the KG extractor turns a parameter table into
free-floating claims — so a question about a command's contract comes back as
prose *about* the command. The command catalog ingests that source as
structured entries instead (name, syntax, arguments, defaults, examples) and
lands them, after explicit human confirmation, in an ordinary knowhow table.

Everything is opt-in and per source. Nothing runs on upload.

**Precondition: the source must already be parsed.** Both the cost preview and
the start endpoint require `parse_status` to be in the repository-wide
"parsed" whitelist (`parsed` / `extracting` / `extracted` — the last two are
KG-extraction stages that happen after the elements have landed), and return a
`409` with a user-readable message otherwise: wait for parsing to finish, or, on
a failed parse, reparse or re-upload. A cost preview over a source with no
elements yet reports "about 0 segments", which reads as "this document
has nothing to extract"; starting a run against one records a fraction of a
manual as a complete extraction.

**The whole document is read; nothing is pre-selected by rule.** v1 picked out
"command sections" by rule and sent only those to the model. Measured, the
picking was not accurate — what it missed was never read, and what it picked
wrongly was paid for. There is no shape judgement left: the document is packed
in document order into bounded **segments** (windows in the code, at most
`WINDOW_CHARS` characters each). An element goes into the current segment whole
and starts the next one if it does not fit; an element longer than a whole
segment's budget is cut into consecutive pieces that land in adjacent segments.
That cut backs up to the nearest whitespace (a newline for preference, within
`SPLIT_BOUNDARY_LOOKBACK_CHARS`): neither a command name nor a flag contains
whitespace, so a cut that lands on one cannot split `global_placement` into
`global_pl` + `acement` — which would leave the name in neither segment, a
candidate in neither, and the command gone. A run with no whitespace to find
(a compressed blob, a minified line) is cut at the budget, best effort.
**Nothing is discarded** — v1's "drop what does not fit" truncation is gone
(measured, a 120-parameter table vanished whole and left a section that was
nothing but its own heading, reporting a 0.0 veto ratio because there was no
longer anything left to fail). A segment's provenance label is its **first**
heading; a segment with no heading of its own inherits the previous segment's
**last** one — a segment that opens under `set_a` and ends under `set_e` leaves
the document positioned in `set_e`, and labelling its continuation `set_a`
would point a reviewer several commands back.

**Each segment's candidate list, and why it relays across segments.** The names
a segment's entries may claim come from three scans: identifiers in the
segment's headings, the leading token of usage lines (code blocks ahead of
prose), and identifiers in inline code. The identifier shape rule is unchanged
(`_`/`.` separators pass; a hyphen-only name must carry a digit). The scans read
EVERY line of the segment, with no line cap: the segment is already bounded by
characters, one element can be a 300-line flattened options table, and a line
the scan never reaches hides the command documented there from the list and
from the density split alike — a name that is never served can never be claimed
and leaves no rejection or ratio movement behind. The list is a
**constraint**, not a menu: a name off it vetoes the whole entry. Its cap is
`MAX_CANDIDATES`, raised from v1's, because a segment is a slab of the document
rather than one command's section — several commands routinely share one, and a
list that truncates before the last of them vetoes a real command out of
existence.

**Over the cap the segment is split, not the list.** Segments do not overlap and
are never revisited, so a truncated list is not "the 33rd command is asked about
later" — it is asked about nowhere, in this run or any other, and nothing
downstream can see it go (an entry can only be *wrong* about a name it was
served). Packing is therefore two passes: pack to the character budget, then
split any segment whose candidate count exceeds `MAX_CANDIDATES` roughly in half
at an element boundary (inside the element, at the same token-safe cut, when the
segment holds only one), recursing until every piece fits. The pieces are
ordinary segments, no character crosses a boundary, and the only cost is one
more model call. Recursion stops at `WINDOW_SPLIT_FLOOR_CHARS`: a segment that
small still naming more commands than the cap is not documentation whose
commands got crowded out, it is an *index of names* (a "see also" block, a
whole-tool command table), where every piece would still overflow and each would
cost its own call. Truncation returns there — and is **disclosed**: the dropped
count rides on each window as `candidates_overflowed`, totals into the run
ledger, and rides the `catalog_job_finished` event when non-zero. It is 0 on
every ordinary document, so the field's presence is itself the signal.

A command's documentation also routinely outlives the segment it
starts in: a 120-parameter table spans several, and every segment after the
first holds parameters with no command name anywhere in them. So the list
**relays**: a segment hands its own candidates forward, or, when it has none of
its own, passes on what it received; a segment that names something resets the
chain (a new command's heading is the old command's end). A relayed name may be
claimed in a continuation segment — list membership is still checked, and only
the "appears verbatim in this segment" half is waived, because that name was
scanned verbatim out of the segment it came from, so the relay carries a witness
rather than a guess. `syntax`, parameter names and `default` must still ground
in **this** segment. **A relayed entry cannot clear a suspect mark.** The
cross-segment merge folds "possibly only mentioned" with AND, so any segment
that documents the command properly clears it — but an entry claiming through
the relay gets no vote: its clean flag is the relay's exemption, not a finding
(a continuation segment has no heading and no usage line and is not being asked
whether the command is documented there), and folding it in would erase the
earlier segment's warning on exactly the shape the relay exists to produce, i.e.
every time. The test is **this segment's own evidence** (the name appearing
verbatim in a heading or a usage line), not membership of the relayed list: a
relayed name that does carry direct evidence here clears the mark as usual.
**Registered trade-off:** a segment's candidate list holds
every command-shaped name the segment *mentions*, not only the one it documents,
so the relay can hand forward a merely cross-referenced name and an orphaned
parameter table can be keyed onto it. The name is real, the parameter is real,
both are in the document, and no grounding rule can catch it — human review is
the backstop for this one.

**The zero-model-call gate.** Whether a segment costs a call at all is
deterministic: a call happens when the segment **has candidates of its own**, or
when it **has flag-shaped parameters and received a relayed name**. Three cost
consequences, by shape: a page of prose following a command is skipped for free
(a relay alone does not open the gate — the relay never empties once a command
has been seen, so charging on it bills every prose page of the book for the one
sentence of description it might hold); an orphaned parameter table with no
relay, like one that opens the document, is also free (there is no claimable
name at all, and grounding guarantees the output would be empty); and a
continuation table that really does follow its command is paid for, which is
what the relay exists for. A skipped segment makes no call, runs no liveness
probe and emits no `catalog_section_done` event (a mostly-prose PDF has
thousands of them) but is still counted off the progress bar — a denominator
that counts it and a numerator that does not never finishes — and the relay
still passes through it. That progress write's RETURN VALUE is therefore the
skip path's only liveness signal: deleting the source or the notebook cascades
the job row away, and ignoring it lets the worker write past every remaining
segment and then settle a job that no longer exists (an all-prose document
reports a plain success nobody can open the results of). A write that matched
nothing ends the run through the existing "job deleted" outcome; an explicit
cancel outranks it, so when both are true the run ends as cancelled.

**Extraction.** One background job per source, guarded by a partial unique index
covering `queued` and `running` — the row is written before the worker starts,
so a duplicate request in that window is rejected rather than scheduling a
second writer. One model call per segment to begin with; a segment whose
flag-shaped parameters exceed `SLICE_PARAM_LIMIT` is split into slices, one call
each (a large parameter table overruns the output budget in one call, so slicing
is mandatory rather than an optimisation). **Every slice sees the whole
segment.** v1's narrow per-slice view (a head excerpt plus the lines holding
that batch's parameters) was correct when a slice was one command's section; now
that a slice is a chunk of a multi-command slab, the same clipping becomes
systematic blindness — thirty candidates with only the first one in view. The
cost is that a multi-slice segment repeats its whole text per call (at most
`WINDOW_CHARS` characters), accepted deliberately. There is no second-opinion
pass and no refinement pass.

**The answer is a list of entries.** The reply is `{"entries": [...]}` — one
entry per command the segment documents, rather than one command per call.
`entries: []` is a **legal** answer (a segment that really documents none) and
does not count as "nothing usable": a prose-heavy manual must not be killed by
the breaker for it. Only a missing `entries`, or one that is not a list at all,
is treated as unusable and triggers the halving remedy. A non-object item inside
the list becomes a **visible rejected row** rather than a silent gap.

**Grounding, and why entries get rejected.** Every extracted entry is checked
against the segment's own text before it is stored: the command name must be on
the server-supplied candidate list *and* appear verbatim (a relayed name is
exempt from the second half only); every parameter name
must appear in its original form (a `-density` documented with its dash is
rejected when the answer drops it); `syntax` must be a
contiguous copy of a usage line; a `default` that is not in the text is cleared.
A name failure vetoes the whole entry; the others drop just that field. **The
rejected entries are stored too**, with the reason and a bounded look at the
text they were searched for in — when a run produces little, those rows are the
only way to tell "the model went wrong" from "this source is not a manual".

**Parameters, `syntax` and `default` ground against the command's own evidence
segment, not the whole segment of document.** A segment is a slab that routinely
documents several commands, so "is this flag somewhere in here" is the wrong
question — it accepts and it deletes, in the same breath. Given
`foo_cmd density` and `bar_cmd -density` in one window, `-density` filed under
`foo_cmd` passes (the flag really is present, in the other command's table),
while `foo_cmd`'s own legitimate positional `density` is rejected by the
dropped-dash rule (the window does contain `-density`). Both are the same defect
from opposite sides. So each segment is cut into per-command **evidence
segments**: a line that structurally anchors a candidate — it belongs to a
heading element, or the candidate is the leading token of that usage line —
opens that command's segment until the next other candidate's anchor; a command
anchored several times owns the union of its runs; an **inline-code mention does
not open a segment** ("see also `bar_cmd`" is precisely where the
mis-attribution came from). Everything before the first anchor is the
**prelude**, which belongs to the relay — a continuation window's orphaned
parameter table is exactly that, and a window with no anchor at all is all
prelude, so relayed claims still ground. Registered consequence: a command this
window only name-drops has no segment and no relay, so its parameters and syntax
are all rejected — it keeps its name (the candidate list vouched for that) and
loses its body, the same reading `suspect_related` reports. **What was ASKED
stays window-level**: the slice assignment and the coverage ledger still come
from the whole window's flag list, because segmenting those would turn "the
model never answered this parameter" into "the model filed it elsewhere".

**Commands without flags still get their arguments.** A segment with no `-flag`
anywhere is not a segment without parameters: a **positional** argument
(`set_dont_use lib_cells`) is how a one-line command is usually documented. No
parameter list can be served for those, so the ask is different (copy the
positional arguments off the usage line, and return none only when there really
are none) while the grounding is identical — the name has to be verbatim in the
segment text, and an invented one is rejected and stored like any other.

**Each slice is judged against its own assignment.** A slice asks for a
specific list of parameters, and its answer is held to that list in both
directions. A parameter that grounds perfectly but was never asked for belongs
to another slice and is dropped (all of a segment's parameters live in the
same segment text, so grounding alone cannot tell them apart); a parameter that
was asked for and never came back is recorded on the row as missing. Both show
up in the review panel next to that command. The second one is also what keeps
the keep-rate honest: its denominator is what the run was asked for, not what
it chose to return, so answering one parameter out of twenty scores 5% rather
than 100%. A slice that comes back covering less than half of a large
assignment is re-asked once, split in two — the same remedy an over-long answer
gets, since it is the same complaint — while an answer that returned as many
parameters as it was assigned and simply got them wrong is not re-asked, since
asking for fewer cannot help.

**One command, one row, across segments.** When a parameter table crosses a
segment boundary, or a later part of the manual (SEE ALSO, an examples chapter)
names the same command again, one command produces an accepted entry in several
segments. The catalog still holds one row per command: a later segment merges
back into the row an earlier one already wrote — arguments dedupe by name with
the **first writer winning**, `syntax` and the description only fill blanks,
provenance elements union within their cap, and the excerpt stays the **first**
segment's (the place the command is introduced is the useful one). The one
exception is a row a person has already acted on: a confirmed or dismissed row
is never rewritten, and this segment's parameters are **appended as a second
row** instead — a duplicate a reviewer can see and skip beats a silent loss,
since the parameters this segment found exist nowhere else.

**Circuit breaker.** After ten segments that **actually made a call** (skipped
segments are not part of the sample) the run fails, with a user-readable
reason, if any of three things is true: a command-name veto rate above 20%, an
argument keep-rate below 50%, or more than 20% of slices producing nothing
usable at all. Three axes rather than one because each is blind to the others:
an entry can name the right command and still invent every parameter, and a
model that answers nothing at all is reported as its own cause rather than as a
missing-parameter symptom — that run would otherwise pay for the whole manual
and report success with an empty catalog. A legal `entries: []` is **not**
counted as unusable; only an unparseable reply is. A transient provider failure
(rate limit, upstream error) is not treated as an extraction result at all: it
fails the job instead of being recorded as "this segment had no commands".

**A run that produced literally nothing is not a success.** When a run **made**
at least one model call, the model never even attempted an entry, and both the
candidate and rejected row counts are zero, the job settles as `failed` with a
user-readable reason ("no commands were recognised; this source may not be a
command manual, or its commands may be written in a way the rules do not
match"). Every other empty-ish outcome stays a success: a run with rejected rows
has already shown the user *why* nothing was kept, and a run whose every segment
was skipped never called a model at all (a source that is simply not a manual,
correctly costing nothing). This is a verdict on a run that has already
finished, so it is deliberately not a fourth breaker axis — the breaker aborts
mid-run on a ratio, while this cannot be evaluated until the last segment is
done.

**Model-authored fields are bounded and labelled.** `description`, `examples`
and each parameter's own description are the fields grounding deliberately does
not check (prose cannot be matched verbatim), so each is capped before the row
is written — per field, and, for parameter descriptions, per row as well, with
the number of descriptions the row budget cut reported alongside the other
rejections. Examples are shown in the review panel under a note saying they are
model-generated and not checked against the source.

**Cost preview.** `.../command-catalog/preview` returns two numbers with **zero
model calls**, each bounded a different way. **How many segments** cannot be
answered by arithmetic alone: `⌈total characters ÷ WINDOW_CHARS⌉` under-counts
twice over, because an element goes into a segment WHOLE (so one that does not
fit leaves the rest of that segment's budget unspent — three 7,000-character
elements are three segments, and the arithmetic says two) and because a segment
too dense for one candidate list is split again. Both only ever ADD segments, so
the arithmetic is a floor rather than a count. So when the bounded prefix turns
out to cover the whole document (`sampled=false`), `estimated_windows` is the
number the real packer produced over it — **exact**, and free, since the prefix
is read anyway for the call estimate. When the prefix runs out it is the
tightest of several **lower bounds**, reported as one — the UI words it "at
least about N segments". Reading the whole document to count them exactly is the
failure a cost preview must not have.

Two non-obvious conditions make that bound hold. First, **characters are counted
the way the packer counts them**: every element is stripped before packing, so
summing raw `LENGTH` describes a document the packer never sees — 2,001 elements
of "one character plus twenty trailing spaces" is 42,021 raw characters (four
segments by arithmetic) and one segment in reality, and a "lower bound" above
the truth is worse than none. Both SQL reads therefore sum stripped lengths; the
join separators the packer inserts are deliberately not counted, which can only
shrink the total and so keeps the bound safe. **Both sides must strip the same
characters**: SQL's `TRIM`/`BTRIM` removes four ASCII ones (space, tab, newline,
carriage return), so the packer is pinned to those four rather than
`str.strip()`'s Unicode-wide set — otherwise a document padded with U+3000 (the
ideographic space CJK typesetting is full of) or NBSP holds fewer real characters
than SQL reports and the arithmetic floor climbs above the truth again. The cost
is that such whitespace counts as content and occupies window budget, which is
the conservative direction. Choosing WHERE to cut a long element still uses the
Unicode-wide test — that is picking a boundary, not counting how much there is.
Second, **the prefix's segment
count cannot simply be added to the remainder**: the packer closes a segment only
when the next element does not fit, so the prefix's LAST segment is still open
and the unread elements keep filling it rather than starting a new one (four
short elements with the row cap at three is one segment, and adding would quote
two). Only closed segments count as certain; the open one's characters go back
into the pot with the remainder, and the remainder is computed from each prefix
row's OWN full length rather than the clipped text that was transmitted —
subtracting what came back would leave every clipped tail in the remainder to be
counted twice.

**How many calls** cannot be arithmetic either: it depends on the zero-model-call gate
(a prose segment is free) and on each segment's parameter count (a hundred-flag
segment is several slices), and both need the text. Those are measured exactly
over a bounded prefix — including the relay, since a segment's gate reads the
previous segment's candidates, and dropping it would make the preview's gate
answer differently from the run's. **Segments past the prefix are not priced at
all.** They used to be charged one call each, and that figure was wrong in both
directions at once: the skip gate makes a prose segment free (a mostly-narrative
book was quoted for calls it will never make) while a parameter-dense one is
several slices (a manual was quoted far too little), and either way it described
text this preview never read while sitting next to copy promising the real total
could only be higher. So `estimated_calls` covers the prefix and nothing else,
`windows_in_prefix` says how much of the document that is, and the UI says "about
M calls for the first X segments; the rest depends on what is in them". **That X
counts CLOSED segments only.** The packer closes a segment only when the next
element does not fit, so the measured prefix's last segment is still open: the
unread elements keep filling it, and one more can flip its gate answer (prose
becomes a command segment) or push its flag list past the slice limit into an
extra call. Pricing a segment that can still change makes "the first X segments"
a claim about text the preview has not finished reading — the same over-reach
that leaving the tail beyond the prefix unpriced avoids. So both the price and X
cover the closed segments alone, and the open one's characters go back into the
pot with the remainder (one shared judgement of which segment is open, not two).
A prefix that packs into a single segment therefore reports X = 0 and the UI
falls back to the segment floor alone.
**The measured prefix stops at the first truncated element.** The bounded read
keeps returning later elements' heads after it clips one, and packing that whole
list splices content from beyond the missing tail directly behind it — segments
that exist in no document, measured under the name "the first X segments" (an
11,990-character prose element followed by a command really is two segments
whose first one is free prose; spliced, it reads as "the first segment costs a
call"). Truncation is judged as "this element's stripped length exceeds what was
transmitted", i.e. CONTENT was lost — an element clipped only through its
trailing whitespace lost nothing and is still measured. From the first such row
onward nothing is measured; it all goes into the arithmetic remainder. When the
very FIRST element is oversized nothing is measured at all, `windows_in_prefix`
and `estimated_calls` are both 0, and the UI gives the segment floor with "the
number of calls depends on the content" — never "about 0 calls for the first 0
segments", which would report "not measured" as "measured to be nothing".
`sampled` is `true` when the prefix ran out, so the
numbers are a floor; both of the prefix's bounds set it, and **per-element
truncation** is the one that actually distorts the estimate (clipping an options
table drops parameter names, which drops slices). The row bound is read as
"element count exceeds the rows returned", not "the rows returned reached the
cap": a document holding exactly the cap's worth of elements was fully read, and
calling that sampled downgrades an exact count to a lower bound — and words the
UI "at least" — on the one document where the estimate is perfect. That
comparison is only legitimate because both numbers come from **one generation**:
the two reads are separate statements, and a reparse committing between them
pairs one generation's character total with another's prefix, which does not
look wrong so much as describe a document that never existed. The preview
therefore reads the source's element generation (the same
`MAX(source_elements.created_at)` the confirm path checks) on both sides of the
pair and re-reads the WHOLE pair once when they differ — re-checking the token
alone would merely confirm the drift and then report the mixed numbers anyway.
A second drift answers `409` (a reparse is actively running; more reads will not
win that race).
`skipped_windows_in_prefix` is how many segments in the prefix the gate skipped
— the only explanation for why the call count is far below the segment count,
without which a mostly-prose manual reading "about 40 segments / about 3 calls"
looks like a miscount. v1's `signal` (shape detection), `is_manual` and
`estimated_sections` retired along with rule-based sectioning, deliberately
**without** compatibility aliases: v2 has no "command section" to count, and a
field pinned at 0 reads as "this document has no commands" more easily than a
missing one does.

**Two progress field names kept, their meaning changed.**
`sections_total`/`sections_done` now count **segments** (skipped ones included);
the database column names stay, because renaming them would need a migration and
break the existing observability surface for nothing. `truncated_sections` is
gone from the transport layer: v2 has no truncation (an oversized element is cut
across adjacent segments), and a field pinned at 0 only makes the interface
render a warning that can never happen.

**Confirmation and merge.** Candidates are unconfirmed until a person applies
them. **Neither apply nor dismiss is available while the run is unfinished** —
both endpoints answer a non-terminal job (`queued`/`running`) with a
user-readable `409`, which is also what the review panel already does (it opens
no review action before the run settles). That is not caution, it closes a path
that manufactures a **permanently unconfirmable candidate**: confirm a command
mid-run, and when a later segment merges that command's continuation table back
it finds the row already applied and degrades to appending a second candidate
for the same command (deliberate — visible beats lost); confirming THAT row then
finds the command already in the target table, and the "same name, never
overwrite, report a conflict" merge rule skips it, so the late-found parameters
are permanently visible and permanently unconfirmable. The gate sits at the API
boundary; the service's lock and degrade path stay as they are, as defence in
depth for the races that remain legitimate (a cancel interleaving with the last
segment's write-back). Apply creates a knowhow table named `命令目录：<source title>` with fixed
columns (命令 / 语法 / 参数 / 说明 / 示例 / 出处, with 命令 as the row-title
column) if it does not exist, and otherwise **only appends commands the table
does not already have**. `<source title>` is the same canonical display name
used everywhere else a source is named — a grounded paper's parsed title, not
its upload file name — so a manual that also happens to be a grounded paper is
never called two different things across citations and its own catalog table.
The title is resolved once, at the moment a target table is first created for
a job; a job that already landed rows keeps writing to that SAME table via its
remembered `applied_table_id` even if the canonical title changes later (a
paper title arriving through a later metadata backfill does not rename or
split the table). Re-running extraction (a fresh job whose own
`applied_table_id` starts empty) reuses that same table too, by inheriting
the most recent prior job's confirmed target for this source — the derived
title is only used to create or find a table when that inherited target has
since been deleted. A candidate whose command already has a row is reported
in `conflicts` and the existing row is left untouched — v1 deliberately never
overwrites content a person may have corrected by hand. A full diff/merge is a
later task. Columns are addressed by name, so editing the target table's columns
cannot silently shift content into the wrong one; a table that has lost its 命令
column is refused rather than written to. One `all_pending` call confirms at most
a page and reports the rest in `pending_remaining`. Writes go through the
ordinary knowhow service layer, so the table's change history records them like
any other edit.

The 出处 column carries the source name followed by the breadcrumb from the
document. When a segment has no breadcrumb to inherit, the column holds the
source name **alone**: the candidate row's internal ordinal label (which the
review panel renders as 「第 N 段」) names a boundary the character budget put
somewhere in the document, and a person keeps this cell, reads it months later
and sees it in a graph, where that says nothing. A real breadcrumb
(`Global Placement > Commands`) is kept — that one genuinely says where in the
document the command lives.

Every exit path settles the job row, including `Ctrl-C`/`SIGTERM`; a row left
`queued`/`running` would hold that source's guard until the next backend
restart, so startup recovery settles anything the process could not.

**Re-running extraction is blocked while candidates remain unreviewed, and
dismiss is the explicit way out.** A source's latest job is the only one
`.../job` ever returns, so starting a new run while the previous run's
candidates are still `candidate`-state would orphan them — reachable by
nobody, forever. Both the frontend and `.../command-catalog`'s own 409 refuse
a new run in that case (covering every terminal status the previous run could
have reached — succeeded, failed, or cancelled), and the guard's own copy says
"confirm or dismiss". Apply only ever moves a candidate out of `candidate`
state without landing a row when it CONFLICTS with an existing row in the
target table; a candidate a reviewer simply does not want otherwise had no
route out at all. `.../command-catalog/dismiss` is that route: the review
panel's "跳过所选" / "跳过全部待审阅" actions, mirroring apply's own selection
contract, per-notebook catalog lock and `catalog:write` authorization (owner ∪
group admin since P2), but touching no knowhow table at all.

**A reparse invalidates the run.** Each job records the **source generation**
it was created against (the single landing instant every element of that source
shares; a reparse replaces the whole batch), and `apply` / `dismiss` compare it
before doing anything: a mismatch returns a `409` with a user-readable message
and, in the same call, marks every remaining candidate of that job `dismissed`
with reason `source_reparsed`. Candidate rows carry command names, excerpts and
section paths taken from the elements the run actually read, so confirming one
after a reparse would write content the document no longer contains. Expiring
them is not a courtesy: the unreviewed-candidates guard above reads that same
set, and leaving it in place would deadlock the source — every confirm refused
as stale, every re-run refused as unreviewed. The same comparison lets a new run
start (sweeping the dead candidates on the way) when the source has been
reparsed, which is precisely when a person wants to re-run. The generation is
deliberately NOT `sources.updated_at`: that is the intentionally coarse change
signal and also moves on lifecycle transitions (a KG re-extraction, a summary
write) that never touch the elements, so keying on it would claim a reparse that
never happened and charge the user for a whole re-extraction.

Endpoints (all scoped to a source of the notebook in the path; reads need
notebook read, writes need owner):

- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/preview` — cost estimate: `estimated_windows` (exact, from the real packing, when the prefix covered the whole document; an explicit lower bound when `sampled`), `estimated_calls` (**covers the read prefix only**; segments past it are not priced), `windows_in_prefix` (how many segments that price covers), `skipped_windows_in_prefix`, plus `sampled` when the bounded read hit its cap (judged on the element count exceeding the rows returned, not on the rows reaching the cap); `409` with a user-readable message when the source is not parsed yet or its parse failed, and a second `409` when the pair of reads straddled a reparse (re-read once and still drifting)
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog` — start extraction; `409` with a user-readable message when this source already has an active job (fetch it from `.../job`), the extraction model is unconfigured, the source is not parsed yet or its parse failed, or the previous run still has unreviewed candidates (confirm or dismiss them first). Candidates already expired by a reparse do not block: they are swept and the run proceeds
- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/job` — the source's latest job: `status` plus `progress` (`sections_total`, `sections_done` — names kept, counting segments — `entries`, `rejected`, `uncovered`, `pending_candidates`) and, on failure, `failure_reason`. The internal `diagnostic` column is deliberately not exposed
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/cancel` — `cancelling` (the worker stops at its next slice boundary, or as soon as an in-flight model call notices the cancellation — it does not wait for that call to return), `cancelled` (no worker in this process; the row is settled directly), or `not_running`
- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/candidates` — keyset page (`job_id?`, `state=candidate|rejected|applied|dismissed`, `cursor`, `limit`), plus per-state `counts`. `next_cursor` is the last row's `position`, not an offset: applying candidates changes their state, and an offset would skip or repeat rows. A `dismissed` candidate carries `dismiss_reason`: `conflict_existing_row` (apply found an existing row), `user_dismissed` (dismissed explicitly), or `source_reparsed` (the source was reparsed and the whole run expired)
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/apply` — a user-readable `409` while the job is not terminal (`queued`/`running`; see "Confirmation and merge" above). Body `{candidate_ids}` **xor** `{all_pending: true}` — sending both is a user-readable `422` (the two used to silently resolve to `all_pending`, a wider write than a caller who also listed explicit ids asked for); returns `table_id`, `created`, `applied`, `rows_added`, `conflicts` and `pending_remaining` (one call confirms at most a page). `409` with a user-readable message when the source was reparsed after this run, which also expires the job's remaining candidates; a reparse still IN FLIGHT (elements not swapped yet) is a second, differently worded `409` that expires nothing — a parse can fail before the swap, leaving those candidates valid
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/dismiss` — the same non-terminal `409` as apply, worded for "skip". Body `{candidate_ids}` **xor** `{all_pending: true}`, same selection contract (including the `422` on both fields together) and page cap as apply; marks the selected `candidate`-state rows `dismissed` (reason `user_dismissed`) without touching any knowhow table; returns `dismissed` (the ids actually moved) and `pending_remaining`. Both reparse `409`s behave as they do on apply (the completed one expires the whole run, the in-flight one expires nothing)

Numeric limits (registered only here; each has a like-named constant in the code):

| Limit | Constant | Value |
|-------|----------|-------|
| Per-segment character budget | `WINDOW_CHARS` | 12,000 |
| Candidate list per segment | `MAX_CANDIDATES` | 32 (v1: 16; the relayed list shares the cap) |
| Density-split recursion floor | `WINDOW_SPLIT_FLOOR_CHARS` | 750 (`WINDOW_CHARS / 16`; still over the cap there → truncate and disclose) |
| Token-safe cut lookback | `SPLIT_BOUNDARY_LOOKBACK_CHARS` | 200 (back up to whitespace/newline; cut at the budget when there is none) |
| Parameters per slice | `SLICE_PARAM_LIMIT` | 20 |
| Model calls per slice | `MAX_CALLS_PER_SLICE` | 11 (both remedies included, `1 + 2·(1 + 2·2)`) |
| Rejection records per row | `MAX_WINDOW_REJECTIONS` | 24 (overflow counted, never silently dropped) |
| Provenance elements per row | `MAX_ANCHOR_ELEMENTS` | 12 (also the cap on the cross-segment union) |
| Breaker sample floor | `MIN_WINDOWS_BEFORE_ALERT` | 10 segments that **actually made a call** |
| Breaker thresholds | `COMMAND_REJECT_ALERT_RATIO` / `ARGS_KEEP_ALERT_RATIO` / `SLICE_FAILURE_ALERT_RATIO` | >20% / <50% / >20% |
| Preview's bounded prefix | `PREVIEW_ELEMENT_LIMIT` / `PREVIEW_ELEMENT_CHARS` | 2,000 elements / 1,200 characters each |
| Model-authored fields | `MODEL_DESCRIPTION_CHARS` / `MODEL_EXAMPLE_CHARS` / `MAX_MODEL_EXAMPLES` | 1,000 / 500 / 8 |
| Argument descriptions, two bounds | `MODEL_ARG_DESC_CHARS` / `MODEL_ARG_DESC_TOTAL_CHARS` | 400 each / 8,000 per row |

Retired: v1's 4,000-character per-slice view and 600-character head excerpt
(`MAX_SLICE_WINDOW_CHARS` / `OVERVIEW_HEAD_CHARS`) — a slice's view is now the
whole segment; see "Extraction" above.

## Retrieval modes (Ask)

`POST /ask` dispatches on `mode` — the registry `backend/app/services/ask_modes.py` is the single source of truth (default `chunk`). Federation is path-specific: baseline `chunk` is active-notebook-only; its optional KG overlay/PPR can add federated KG context and base-backed chunks; `graph` and `reasoning` use federated KG paths. Knowledge-object hits from `federated_retrieve()` keep tier-blind scores and use `base` only as the secondary key on an exact tie; `federated_retrieve_relations()` remains score-only. These ordering signals never feed grounding thresholds.

Streaming Ask runs post-completion observers only after the durable answer and browser final event. The point gets a cooperative wall-clock budget from `ASK_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` (default `30`, valid range `>0..300` seconds): a synchronous callback already in progress is allowed to finish safely, but the host starts no later contribution once the deadline has passed. This is a deployment/internal extension rail and does not alter retrieval, answer text, citations, or the user-visible final event.

Deep Report post-completion uses its own deployment rail. `REPORT_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` defaults to `30` and accepts `>0..300` seconds, and the deadline is cooperative. The point runs only after the report row atomically commits from `generating` to `done` and every generation execution scope is released; it cannot alter section prose, citations, references, retrieval output, or terminal status.

| Mode | Group | Needs KG | One-liner |
|------|-------|----------|-----------|
| **`chunk`** (default) | general | no | Chunk-native general Q&A: large recall → selection → long-context synthesis → citations bound to source chunks. |
| **`graph`** | strict | yes | Single-pass Personalized-PageRank propagation across the cross-document knowledge graph. |
| **`reasoning`** | strict | yes | Agentic, iterative plan → retrieve → reflect → answer (streams a live trace). |

### Optional generated-question recall supplement

`GENERATED_QUESTION_INDEX_MODE` is a deployment-only rollout with values `off` (default), `shadow`, or `on`. It is not a user retrieval-scope control. Operators first run `scripts/batch_ingest.py question-index --notebook-id ...`; every accepted generated question gets its own embedding row but addresses one immutable original chunk. Generated text is never evidence, never enters citations, and is not returned to the browser. Reparse/delete cascades it; notebook deep-copy and SQLite→PostgreSQL migration remap or preserve it with the original chunk.

The online path runs only when baseline chunk recall returns fewer than `GENERATED_QUESTION_TRIGGER_HITS` (default `5`, minimum `1`). It reads at most `GENERATED_QUESTION_MAX_SCAN_ROWS + 1` rows (default scan cap `10,000`, minimum `1`); crossing the cap skips this supplement rather than performing an unbounded scan. It ranks at most `GENERATED_QUESTION_RECALL × GENERATED_QUESTION_QUESTIONS_PER_CHUNK` question rows, then retains at most `GENERATED_QUESTION_RECALL` original chunks (defaults `40` and `3`; recall minimum `1`, questions per chunk range `1..8`). The frozen source ceiling and the retrieval-run actor's private-Memory predicate are applied in SQLite/PostgreSQL before `LIMIT`, so excluded or another member's Memory questions cannot consume the scan cap or influence ranking. Orphaned Memory projections fail closed, while visible sources and notebook-wide Knowhow remain eligible. `shadow` executes this comparison through the shared candidate contributor and emits counts-only internal telemetry but returns the exact baseline tuple. Only `on` may append matching original chunks, after baseline hits and before MMR/fusion; it does not evict or reorder them. `off` incurs no table read or extra embedding call.

The offline builder requires chat workload `chunk_question_generation` and embedding workload `chunk_embedding`. A per-chunk completion timestamp makes successful empty model output idempotent; per-chunk failures remain unmarked and retryable. `--force` intentionally regenerates already completed chunks. Because the first implementation uses a bounded matrix scan rather than a separate ANN artifact, libraries above the scan cap remain baseline-only; rollout must start in `shadow` and use its content-free hit/add/skip counts for A/B evaluation before `on`.

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
- *Mix* (active only when `CHUNK_KG_OVERLAY_ENABLED=true` **and** qwen3-rerank is configured **and** a KG is available): three sources are pooled — (a) vector chunks, (b) the KG local structure around the query seeds (entities + their 1-hop relations, retrieved once), (c) the source chunks behind those KG objects — round-robin merged, reranked by a qwen3 cross-encoder, then packed to a token budget (`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`). Every candidate accumulates its producer supports instead of inferring origin from its fused score. Final selection may reserve `CHUNK_GRAPH_RESERVE` seats (default 0/off) for graph-only chunks that already pass the existing relevance floor, and `EXACT_SECTION_RESERVE` seats (default 4) for the exact-identifier fast path below; neither expands item/token budgets or the oversized-first-chunk exception, and neither may evict what the other is holding. The answer cites chunks and KG items in one unified `[k]` map, and grounding spans chunk ∪ KG. When rerank is unconfigured or no KG exists, it falls back byte-for-byte to the baseline. (Faithful to LightRAG's `mix` mode.)

**Exact-identifier fast path (`EXACT_LOOKUP_ENABLED`, on by default).** Ranking cannot fix a question that is really an identity lookup: a reference manual's `set_db` section is split by the chunker into main description / Arguments / Examples, and relevance scoring keeps whichever part wins. When a question names something exactly lookup-able, retrieval therefore runs one extra zero-model channel first — exact substring match to locate the section, then fetch that section (and its subsections) whole — and merges the result into whichever candidate branch runs. The gate is deliberately narrower than the lexical layer's identifier extraction: `_`- or `.`-joined names (`set_db`, `config.yaml`) always qualify, while a purely hyphen-joined word must carry a digit, so model/version names (`GPT-4`, `v1-2`) qualify and ordinary English compounds (`state-of-the-art`, `real-time`, `end-to-end`) do not. Those words are why: they appear in almost every analytical question — a deep report's per-section question contains one nearly every time — and each would buy a real probe whose hit promotes a whole chapter into the evidence budget. They stay in lexical recall, where one extra OR-ed term is free. Hits are then folded onto the command they belong to before section slots are handed out, so one command's Arguments/Examples sub-sections cannot consume the budget a second named command needs. Those chunks score keyword-only — coverage of the *name* that addressed the section, not relevance to the whole question, so both callers report the same number for the same evidence — carry ordinary `lexical` provenance, and hold `EXACT_SECTION_RESERVE` seats in the final mix selection so the reranker cannot truncate the parameter table back off. Every query is bounded (`EXACT_LOOKUP_MAX_IDENTIFIERS` / `EXACT_LOOKUP_FTS_K` / `EXACT_LOOKUP_MAX_SECTIONS` / `EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION`), and a question with no such name issues no additional queries at all, so those asks are unaffected. Scoped to the active notebook; mounted reference libraries are deliberately out of scope. **Heading breadcrumbs are a Markdown-parsing property**, so "fetch the whole section" is only available for sources parsed that way; MinerU-parsed PDF/DOCX sources carry no breadcrumbs, and there the channel falls back to returning exactly the chunks that matched, which still recovers the parameter table an ordinary ranking would have dropped.

**`graph` — PPR over the cross-document KG.** Seeds via `federated_retrieve` (KG entities + their source chunks; optionally fused with relation-index hits when `RELATION_RETRIEVAL_ENABLED=true`) become the personalization vector for HippoRAG-style **Personalized PageRank** (`GRAPH_PPR_ENABLED`, on by default), which propagates relevance across documents through the shared knowledge graph; the top-ranked chunks feed a grounded answer whose `[k]` anchors point at KG objects/relations. With `GRAPH_PPR_ENABLED=false` it falls back to bounded BFS along reasoning edges.

**`reasoning` — intent-first agentic deep retrieval.** The official UI first calls `/ask/intent`. This corpus-blind preflight may use up to the latest five user questions, never corpus-derived assistant answers, but it does not create a durable conversation/job. Clear requests auto-confirm; blocking ambiguity opens an inline review. `/ask` and `/ask/stream` accept the reviewed `intent` alongside the original `question`; the backend deterministically freezes it and builds one authoritative internal research question used by Memory retrieval, PPR, evidence retrieval, and answer synthesis. Its approved retrieval directions directly seed the initial subqueries, bypassing the old second planning pass; reflection can add evidence-driven queries but cannot replace the contract. Directions beyond the effort's first-round width are not discarded: a deterministic coverage pass executes them in contract order after the PPR/exact seed passes and before the reflect loop, taking at most half of the shared reasoning-step budget, and each executed direction produces a normal `retrieve` trace step. A per-run registry maps every reviewed direction to a unique short label (widening the truncation window, then a numeric suffix, for directions whose default labels collide) since the model only ever sees and resubmits the label, never the seed's full contract text; an `add_subquery` submission matched against that registry either executes an uncovered direction's own contract text (recorded under the direction's identity, not the bare label) or is deduplicated against one already covered — by the coverage pass or an earlier `add_subquery` — instead of being re-run. Every later reflection prompt is fed the current uncovered set so the model can spend its own budget on them first, and a single `skip` step naming whatever directions remain uncovered is recorded once the reflect loop ends — reflecting the run's final coverage rather than the moment the coverage pass ran out of budget — and is omitted entirely if reflection ends up covering them all. The response persists the confirmed `intent`, exposes the internal `retrieval_query`, and starts the engine trace with `intent` before retrieval while the saved user turn remains the original wording. The trace covers the whole run, not just retrieval: question understanding happens before a durable job exists, so the UI synthesizes its own leading steps (understanding in flight → understood or awaiting clarification → confirmed) ahead of the backend's, instead of giving that phase a separate status line outside the trace. Its client-measured wall clock comes back as the optional, bounded `intent.understanding_ms` (never an input to retrieval) and becomes the persisted `intent` step's `duration_ms`. The engine streams `intent` before Memory retrieval and records a `memory` step when private memories were recalled; that step reports recall rather than attribution — attribution is carried by the answer's `[k]` citations — and a zero-hit lookup is recorded as a timed `skip` so its candidate query and embedding call stay in the total. It records a `synthesis` step after answer generation — usually the longest single slice of a run, and therefore neither invisible nor excluded from the trace total — whose citation count comes from bound anchors rather than retrieved-evidence cards. Direct compatibility callers that omit `intent` retain the old clear-question path, but deterministic unresolved/generic ambiguity fails closed. The remaining loop delegates to `ReasoningRetriever`: retrieve (using the same PPR propagation as `graph`), reflect on sufficiency, and expand the graph or add subqueries until answerable, with live `reasoning_trace` over the NDJSON stream (`/ask/stream`). For explicit derivation questions it may call `follow_chain`: two bounded adjacency samples reuse the existing source/target indexes, then deterministically check types, status, review, evidence, and `validity_scope`. The two stored relations remain citable premises; `A→C` is only a query-time conclusion marked as an inference. If a high-degree sample is truncated and cannot prove the absence of a direct edge, it abstains. The exact-identifier fast path above is wired into this loop twice, both zero-model, and reaches both the `reasoning` ask mode and every deep-report section retrieval (the report engine reuses `ReasoningRetriever` verbatim); `graph` mode does not wire it in, and knowhow completion turns it off (its query is a JSON envelope, not a question, and would otherwise probe the envelope's own field names on every request). A deterministic seed pass runs once right after the initial retrieval whenever the authoritative question itself names an identifier (recorded as an `exact_lookup` trace step, rendered 「精查」), scored by the identifier names it actually probed rather than the raw question — scoring the exact match against every unrelated token in a long question sank a genuine hit low enough to lose the character budget and the grounded/overview threshold — and the reflect model may additionally choose the `exact_lookup` action with an `exact_term`, scored the same way, when a named command's full definition is still uncovered. The action obeys the same identifier gate as the seed pass — an arbitrary low-selectivity string is refused rather than turned into a library-wide substring scan — probes each name at most once per run (the seed pass shares that ledger), is capped at 3 agent invocations per run, and feeds every skipped/duplicate/zero-yield attempt back into the next reflection with a reason the model can act on (deduplicated so a repeated invalid input does not grow the ledger unbounded). A question containing no identifier issues no call and adds no trace step. Strict / KG-grounded.

#### Source scope is user-selected only

Retrieval source scope comes exclusively from the visible-source checkboxes described under [Source-selected retrieval scope](#source-selected-retrieval-scope); the model never infers which sources a question names. The corpus-blind intent planner emits no source identities, and there is no reviewed source-confirmation step: a reasoning run inherits the request's checkbox ceiling and nothing inside the run can change it.

An earlier revision let the intent planner emit a `source_refs` list that `/ask/intent` resolved against a bounded identity-only catalog by exact normalized match on stable id, display title, or original file name, gated behind a `source_scope_confirmation` review and a signed preview capability, with a `search_evidence(query, source_refs?)` Agent action able to narrow further inside a confirmed run. That whole contract is removed. Exact equality could not honor an abbreviation — asking about "pdagent" when the source is titled "PDAGENT-BENCH: Characterizing, Grounding, and Architecting LLM/VLM Agents for VLSI Physical Design" resolved to zero matches — and because the design failed closed, an ordinary question died with a deterministic 422 that no retry could clear. Since users already own the checkboxes, a second model-side guess added failure modes without adding reach.

#### Model JSON recovery and stream liveness

`reasoning_agent` decisions and `ask_answer` synthesis are strict-JSON-first. If strict parsing fails, the shared repair seam may accept only a complete object-shaped response with recoverable syntax faults such as missing quotes/commas. The repaired object must stay within the schema example's top-level keys, actual booleans remain booleans, enum-like example values remain in vocabulary, non-finite values are refused, and every nonempty string value must still appear verbatim in the raw response. Truncated objects, arrays/scalars, unknown keys, type confusion, and string reconstruction remain malformed responses. A retriever failure degrades to a terminal persisted Ask response instead of aborting the orchestration on an absent result.

`/ask/stream` sends a content-free blank NDJSON line every **5 seconds** while its delivery queue is idle and disables common proxy buffering. Existing clients ignore blank lines. This keeps the transport active during a slow reflection or synthesis call without fabricating a reasoning step; disconnect still stops only that client's delivery and the detached job still runs. The heartbeat protects against idle timeouts only—an ingress/CDN with an absolute total-request ceiling must be configured by the operator.

### Reasoning effort and complete collection requests

The grade is picked in the Ask composer through the same graded-effort control as a deep report's research depth — one shared component, so the two never drift apart: a chip carrying the current grade name, opening a slider popover that shows that grade plus one neutral sentence about it. The interface deliberately exposes only the grade name and that sentence; the exact ceilings live in this table (mirrored by `frontend/app/ask-retrieval-effort.ts` and `backend/app/core/ask_retrieval_policy.py`) rather than being printed onto the control. `answer_element_items` and the three `enum_*` columns below are the exceptions to that mirror: they are backend-only fields with no frontend consumer, since they only shape the final synthesis prompt and the [collection-enumeration tools](#collection-enumeration-tools) budget server-side.

Reasoning Ask accepts the stable `retrieval_effort` ids below; the default is `standard`. The model may stop before a ceiling when evidence is sufficient, but it may not raise a ceiling. “Final floor/aspect/cap” means `min(cap, max(floor, aspect × executed query count))`. The context values are hard evidence-character ceilings: the source partition contains structured preview, chunks, and direct source elements; the KG partition contains KG objects/relations, confirmed Memory, and query-time chains. Their combined evidence block cannot exceed the sum of the two values. `answer_element_items` bounds how many direct source elements (formulas, tables, images, …) the final synthesis prompt admits; same-source normalized-text duplicates collapse before this cap, the remaining representatives are chosen by retrieval relevance descending rather than insertion order, and they still consume the shared chunk-context budget above. Reflection receives this same capped diverse element view, so it cannot judge sufficiency from passages that final synthesis cannot see. `enum_page_size` / `enum_pages_per_run` / `enum_rows_per_run` bound the separate collection-enumeration tools (`enumerate_elements` / `enumerate_kg_objects` and their `collection="sources"` parameter value, described below): a page size shared by every effort, plus a per-run pool of extra page round trips and total listed rows that grows with effort the same way the other ceilings do.

| Effort id | UI label | Per-query ranked take | Final floor / aspect / cap | Max reasoning steps / initial subqueries | KG / chunk context characters | Direct source elements in synthesis | Enum page size | Enum extra pages/run | Enum rows/run |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `overview` | 概览 | 4 | 8 / 2 / 12 | 4 / 2 | 4,000 / 12,000 | 4 | 50 | 2 | 100 |
| `standard` | 标准 | 8 | 20 / 3 / 36 | 8 / 5 | 6,000 / 30,000 | 6 | 50 | 4 | 200 |
| `deep` | 深入 | 8 | 24 / 4 / 48 | 16 / 6 | 8,000 / 50,000 | 8 | 50 | 6 | 300 |
| `thorough` | 详尽 | 12 | 32 / 5 / 64 | 32 / 8 | 12,000 / 80,000 | 12 | 50 | 8 | 400 |
| `exhaustive` | 穷尽 | 16 | 40 / 6 / 96 | 50 / 10 | 16,000 / 120,000 | 16 | 50 | 12 | 600 |

The initial-subquery ceiling bounds **first-round concurrency only**; it is not a decision to drop the confirmed directions that do not fit. Every reviewed direction beyond that width is deferred into a bounded coverage pass that runs after the deterministic seed passes and before the reflect loop, sharing the same reasoning-step budget (the coverage pass may consume at most half of it, so the reflect loop always keeps a working share). Directions the step budget cannot reach are fed back into reflection every round so the model can prioritize them, and whichever remain uncovered once the reflect loop ends are disclosed in a single skipped trace step reflecting that final state; they are never silently discarded, and resubmitting a direction's short label — whether or not it has already been covered — is resolved against the direction's own identity rather than treated as an unrelated new query.

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

The 100-row inline ceiling is also the maximum row preview available to hybrid model synthesis; it does not delete rows from the authoritative structured result. Responses therefore separate per-table coverage, request/batch coverage (`selected_tables/known_tables`, returned/known rows), and hybrid synthesis coverage. For example, enumerating 200/200 rows while synthesizing 100/200 is “enumeration complete, analysis partial”. The 20-row initial UI view is presentation-only and remains expandable. A lightweight catalog query returns at most eight table descriptors and never hydrates cells, code attachments, or health payloads; it prioritizes an explicitly named table before applying that window, so a ninth-or-later table remains addressable. Aggregate counts and sequence sums still cover the whole notebook. Coverage says complete only after cursor exhaustion and stable before/after catalog values: projection `mutation_seq`, history-backed `enumeration_seq`, row count, column metadata, and selected/global scope. Every row add/delete records history in the same transaction, so an equal-count delete-then-add still changes `enumeration_seq`. Reaching the row/page/table/column/payload safety rail, or detecting a concurrent table change, produces `complete=false` with `explicit_partial` batch coverage and must never be worded as “all”; an individually exhausted selected table may remain complete when the overall batch omitted tables at the eight-table ceiling. Selecting a lower effort level does not reduce these explicit-completeness limits. This executor currently applies only to the direct physical-row Knowhow semantics above; other Knowhow semantics still disclose that complete enumeration is unsupported. Source-element, KG-object and document collections have their own bounded enumeration path — the [collection enumeration tools](#collection-enumeration-tools) below — which the model invokes explicitly rather than the intent-scope executor above; Memory and any other collection still disclose that complete enumeration is unsupported.

Retired ids `fast` and `global` are transparently remapped to `chunk` (old sessions/bookmarks never 422); any other unknown mode is rejected with HTTP 422.

### Collection enumeration tools

Reasoning Ask can also list, rather than only rank, bounded families of collections through the model-invoked reflect actions `enumerate_elements` (source elements) and `enumerate_kg_objects` (knowledge objects), plus the `enumerate.collection` parameter value `"sources"` on either of them, which lists the library's own documents instead. The model-facing action space therefore stayed at ten ids through this feature: the third collection arrives as a parameter, not as a third action (the outline scratchpad described [below](#outline-scratchpad-and-section-by-section-synthesis) later added a genuine eleventh action, `update_outline`, unrelated to this one). All three collections are served by zero-LLM executors: the model decides whether and how much to enumerate; the executor computes coverage as a fact, never from the model's own assertion. This is a separate mechanism from the Knowhow physical-row executor above — it shares the same effort-scaled budget table but not the same intent-scope gate, and it is model-invoked rather than automatically triggered by `result_scope`.

**Enumerable collections.** Source elements: `formula` / `table` / `image` / `code_block` (paragraph, heading, and other high-volume/low-signal kinds are deliberately excluded). Knowledge objects: `concept` / `claim` / `formula` / `procedure`, restricted to usable statuses. Both whitelists have exactly one source of truth (`app.services.collection_catalog`); nothing else may hold a second copy. The sources collection has no sub-type — a library's document roster is one whole collection.

**Source listing (which documents are in here).** Requesting `enumerate.collection:"sources"` (the other enumerate parameters are then ignored — the roster has no sub-type, and any other value of the field falls back to the action's own type) lists every document in scope, in the order the source tab shows them (by creation time, then id — so a truncated listing's prefix is the documents added first, the only reading of "the first N" the interface supports): display title (a grounded paper title wins over the upload name, the same rule citations follow), document type (the interface label from the upload picker's own type registry, omitted when the type was never detected), and an excerpt of the summary the library has already stored. It lists exactly what the source tab lists — the same user-visible-source predicate — so an answer can never show a document card the user cannot see in the source tab (Memory synthetic rows and a Knowhow table's hidden projection row are in neither). Its purpose is to let "analyse the articles in this notebook" or "which papers are in here" start from the roster and then go deeper per title through the ordinary follow-up-subquery mechanism, rather than sampling passages from whichever documents matched. It reads no document text and makes no model call; its cost is one bounded read per participating library plus one batched lookup per page. When a roster reaches answer synthesis, the prompt also carries a granularity instruction: a roster small enough to treat individually is answered document by document, a larger one is organized by theme with the documents named under each dimension. That judgement is the model's — it holds the exact count and knows how much the question wants per document — and no numeric threshold is applied anywhere.

**A listing never contains private Memory.** A confirmed Memory belongs to one user, and every other channel already treats it that way: Ask reaches it only through the owner-scoped Memory retriever, and Knowhow completion excludes it from its candidate set by contract. A collection listing is scoped to a notebook's participants and has no owner filter of its own, so a Memory left in scope would be readable by every member of a shared notebook through its formulas, tables, images, code blocks, and the knowledge objects extracted from it. Both the map's counts and both executors therefore exclude the hidden Memory source and its objects — unconditionally, in a one-person notebook exactly as in a shared one, so one listing always means one thing. (The Knowhow projection source stays in: it is notebook-wide content already visible through the table itself.) This is deliberately narrower than the board's own knowledge count, which answers "how much does this notebook hold" and legitimately includes Memory-derived objects.

**Collection map (count before list).** Before any of these tools is worth calling, a single line such as `[Collections in scope] elements: formula 12 (3 sources), table 5, image 0, code_block 0 | KG objects: concept 1234, claim 567, formula 89, procedure 0 | knowhow tables: 2 | sources: 7` (every whitelisted kind/type is always present, zero-valued when absent, so the model can tell "counted zero" apart from "not reported at all"; capped at 600 characters) is built once per run and injected into the plan/reflect context, scoped to the active notebook plus its currently valid mounted reference libraries. Counting is index-assisted and carries no source titles, file names, or excerpted text — it is prompt scaffolding, not evidence. The map's per-source count set is exactly the set the enumeration executor walks, by construction, so a discrepancy between "the map said N" and "the list returned M" cannot occur. The trailing `sources` count is the user-visible document count (the same figure the source tab and the document-limit check use) and comes out of the same helper as the source listing's denominator.

The same line also reaches the model that writes the final answer, as a short server-computed block at the head of the source evidence partition (a fixed header plus the capped map line, under 800 characters, carrying no `[k]` id because it is deterministic server output rather than a retrieved item). This closes a real gap: the reflect prompt tells the model that a collection far larger than the run's listing allowance should be answered with its count instead of paged through, and answer synthesis is a separate call that would otherwise never see that number — in the extreme case (a large collection, nothing else retrieved) synthesis would not have run at all. The block is injected whenever the tools are on and the map built, whether or not anything was enumerated, and it is placed ahead of every other block so a full evidence budget cannot make it the first casualty.

**Coverage contract.** Every enumerated collection carries a `TypedCollectionCoverage`: `returned_total` (rows returned so far across a resumed chain), `total` (the map's known size, or `null` when the size could not be established — rendered as an unknown denominator, never as `/0`), `complete`, `truncated_reason` (`budget` / `payload` / `concurrent_change`), and `overflow_semantics` (the shared `explicit_partial`). `complete=true` requires cursor exhaustion, a stable scope identity taken before the first page and after the last, and — across a resumed chain — that the running total matches the known size; anything else is `complete=false`. That scope identity includes the set of participating libraries itself, re-resolved when the walk closes: mounting, unmounting or invalidating a reference library part-way through changes what “the whole scope” means, so the result is reported as a concurrent change rather than as “all”. Alongside coverage, the result also carries `synthesis_rows` / `synthesis_complete`: the enumerated list and the bounded preview that actually entered the answer-synthesis prompt are tracked separately, so "enumerated 200/200, previewed 100" is disclosed as "enumeration complete, analysis partial" — mirroring the Knowhow batch's own enumeration/synthesis split. That preview allowance is shared across a run's collections and split rather than handed out first-come-first-served: each list is reserved an equal share first, then whatever a short list cannot fill is passed along in order. A run that enumerated two collections therefore previews both, instead of spending the whole allowance on the first and answering a multi-collection question from one list.

**Citation and attribution contract.** Every delivered source-element or KG-object row carries at most one bounded citation to a live original source element; for a KG object this is the first still-live id from its already bounded evidence-reference list. Mounted-library evidence is resolved server-side inside the active notebook's authorized participant set. The browser receives only the source title, location, locator and excerpt and never calls a mounted library's member-only source endpoint. Every row that actually enters the synthesis preview receives an isolated `k5001+` id and reverse binding. The model must cite that id whenever it uses the row, and only those bound row anchors count as attribution for the final answer. A source-backed exact deterministic row can therefore be grounded without pretending it had a semantic relevance score; an orphaned row may still be listed and cited as a row, but cannot make the answer grounded. Conversely, if an enumeration answer binds no anchor, the response exposes no unrelated ranked-retrieval fallback citations as though they sourced the list. The result card shows each row's original-source citation; both local and mounted-library rows may open the exact source element through the active-notebook proxy, while mounted sources remain read-only. A historical KG row whose referenced elements have all disappeared is labeled as having no available original source rather than receiving a fabricated citation.
**UI presentation.** The Ask result card always shows its coverage badge, collection title, and optional single-source scope, but starts with its item content collapsed. Opening it mounts the grouped source elements or KG objects and shows the existing 20-item preview; a second control expands any additional loaded items. Partial-result and synthesis-preview disclosures live inside the collapsible content, and image rows are not mounted or fetched while the card remains closed.

**Budget and resumption.** One action pages automatically within its run-wide budget (`enum_rows_per_run` total rows, `enum_pages_per_run` extra page round trips, and the shared `structured_payload_chars` structured-payload ceiling) until the cursor is exhausted or a ceiling fires; only the second and later page of each visited partition (a source, for elements; a participating notebook, for KG objects) counts against the page ceiling, since a partition's first page is the only way to see it at all. All three ceilings are **per request, not per action**: each action receives only what the run has left, so several enumerations in one deep run can never return more structured payload in total than the documented request-wide ceiling. The payload ceiling is enforced twice, on two different measurements, because they answer two questions: the executor charges the compact internal row while it walks (that rail stops the traversal from reading more than the request may produce), and the response mapping then charges the *serialized* row — the shape that is actually streamed and persisted, which is materially wider. A row the second rail has to stop short reports its own `truncated_reason=payload` with `returned_total` equal to what was delivered, and the synthesis preview is rendered from the delivered list, so the prompt's coverage header and the result card always state the same number. That cursor is a run-internal handle — it never appears in the response; the client sees only the coverage badge and, when truncated, that the same reasoning run can keep listing further. `complete=false` always implies a resumable cursor, with exactly one exception: `truncated_reason=concurrent_change`, where the scope moved between calls and the chain must not silently restart. A repeated request for a not-yet-complete collection resumes from that cursor when the run's budget still has rows/pages/payload left; when the run's budget is exhausted instead, the request is skipped as budget-exhausted (still reported as a partial result) rather than as already enumerated. Only a collection whose chain already reached `complete=true` is skipped as already enumerated, since asking again could only repeat rows already reported.

**Scoping to one named source.** Internal source ids are never shown to the model — candidate summaries and citations carry source titles — so an element enumeration is narrowed to a single document by title. The server resolves that title deterministically against the sources the map already plans to visit for the requested kind: an exact match after trimming and case folding, never a fuzzy or ranked match, and bounded so a large mounted library cannot turn one reflection into a full label sweep. A title that matches nothing, or more than one source, skips the action and says so in the trace; it never silently widens into a whole-scope enumeration of a different question, and it never guesses one of several same-titled documents. When the scope holds more sources of that kind than the resolver is allowed to examine, it declines outright rather than answering from the part it did read: uniqueness is a property of the whole scope, and a second source with the same title could sit anywhere beyond the examined range.

**Knowledge-object listing on notebooks with a long governance history.** The knowledge-object page walk is a pure keyset read over `(notebook, type, created_at, id)`; deprecated and other unusable objects are filtered after the read, by the very same usable-status definition the map's counts use. Filtering after the read is what keeps one page O(page size) on a notebook where most objects of a type have been deprecated — a status predicate inside that query would have no index to ride and would visit an unbounded number of rows. The extra reading that filtering costs is itself bounded per action; when that ceiling fires, the result is an ordinary honest partial (`truncated_reason=budget`) with a resumable cursor, never a quietly shortened list presented as complete.

**Notebooks without a knowledge graph.** These tools do not need a graph. A notebook whose sources have been parsed but never analysed into a knowledge graph — the ordinary state, since automatic extraction is off by default — can still be asked to list its formulas, tables, images, or code blocks, and reasoning Ask now runs normally for it whenever the scope holds at least one enumerable **element or knowledge-object** collection. The response still reports that no knowledge graph exists (so the “build a knowledge graph” prompt keeps showing), it simply no longer refuses to answer. A notebook with neither a graph nor any element/knowledge-object collection still gets the direct “build a knowledge graph first, or mount an already-analysed reference library” answer, because those tools would return empty for it. The document count is deliberately **not** part of that gate: it is ≥1 for any non-empty library, so counting it would remove the gate altogether and with it that explicit guidance for every text-only library. Once a run is admitted, the source listing is available like the other two.

**Scope words are not search terms.** Phrases like “当前notebook”, “这个库”, “本库”, “整个库”, “the current notebook”, “this library”, “知识图谱 / KG” name the notebook the user has open together with its mounted scope — the scope every retrieval already runs in, not content that can be found inside it; no document contains the name of the library holding it. Intent understanding, both planning prompts and the reflect loop are therefore all told to resolve such a phrase into the scope and then **drop** it: never into a sub-query, a keyword or an exact term, while keeping the rest of the question intact (“what do the articles in this notebook say” is about those documents, and dropping the scope words must not turn it into a different question). A question about the library itself — how much it holds, what kinds of material, how many documents — is answered from the count line above and these enumerate actions, not by searching for the words. This is deliberately prompt-level only, with no deterministic strip list: a phrase table that edited user text would be lexical routing, and it would mangle the legitimate case of a document that genuinely discusses knowledge graphs.

**Counts follow a parse immediately.** Writing a source's elements advances that source's change signal in the same database transaction, so the per-source counts behind the map are invalidated the instant new elements commit. A source parsed a moment ago is therefore countable, plannable and listable right away — there is no window in which it reads as empty while its elements are already stored, and no wait for the status write that ends the parse. (An explicitly named `source_id` is still queried directly rather than inferred from the map, since one source the user pointed at is worth an index-seeked read.)

**Round-trip cost.** One enumeration action's page queries are bounded by its own budget, and the bound is enforced rather than assumed: sources with no items of the requested kind are never visited, and a source is only in the plan because items were counted in it, so visiting one produces rows — which the row allowance caps. Page allowance and row allowance therefore bound the round trips together. The first page of each visited partition stays free of the page allowance on purpose: charging it would make the ordinary shape of a real corpus — one formula each across a hundred sources — unable to reach complete coverage at any effort level.

**Cross-library items.** An item whose collection lives in a mounted reference library (not the active notebook) is labeled “来自参考库《名》”. Source-detail jumps and images use active-notebook proxy endpoints: the browser authorizes only against the notebook the user opened, while the server resolves the resource inside its current participant set. The mounted source remains read-only, and the browser never calls the reference library's member-only endpoint. The server may also include the bounded original-source citation described above, so cards and answer citation details can show the title, location and excerpt before a jump. A cross-library row of the SOURCE listing follows the same rule: it is labeled and it opens through the same proxy, because the document it names is exactly the resource that proxy resolves. One remaining honesty boundary is the disclosed count of sources parsed through a plain-text fallback (no structural parse), whose elements the element-kind enumeration cannot see at all: coverage states completeness only over elements that were actually stored, not over every source in scope.

**When the “complete enumeration is unsupported” disclaimer is dropped.** A completeness request that no executor can serve exactly is answered with a leading disclaimer. It is suppressed only when four deterministic conditions hold together: the intent scope is not `aggregate`, the intent contract records no constraints, exclusions, or assumptions, at least one collection result card returned rows, and that card's coverage is `complete`. Anything else keeps the disclaimer. The bias is intentionally toward warning too often: a card's coverage proves one physical collection was walked end to end, never that the physical collection is the filtered/grouped/deduplicated subset the question actually asked for — and there is no deterministic test for the latter, only a guess.

Set `REASONING_ENUM_TOOLS_ENABLED=false` to disable the whole family entirely: no map is built, neither action nor the sources parameter is offered, the no-knowledge-graph early return applies again, and reasoning Ask returns to its pre-tool behavior at zero extra query cost.

### Notebook understanding blocks

The agent keeps a small, low-cost, LLM-consolidated summary of what it has learned about a notebook — "AI 对这个库的理解" ("what the AI understands about this library") — surfaced through five fixed label blocks and injected as prompt scaffolding into reasoning Ask's plan and reflect context (and, identically, into each section's deep-dive during Deep Report generation). It is never evidence: `ReasoningResult` gains no field for it, so it can never enter answer synthesis, be cited with `[k]`, or appear in a deep report's body.

**The five blocks and two ownership layers.** Three blocks form a notebook-wide **shared base layer** that every member reads: `corpus_shape` (what kind of material this library broadly holds), `key_entities` (names/topics that recur often enough to be worth knowing up front), and `corpus_gaps` (gaps the agent has already noticed — no tables/formulas/images/code blocks, or parse-quality warnings; parse failures and not-yet-parsed documents — the latter including in-flight queued/parsing ones — feed two separate counts — worth avoiding when phrasing a question). Two blocks form a **personal overlay** private to each member: `retrieval_notes` (that member's own accumulated phrasing/query experience, visible and effective only for their own questions) and `usage_gaps` (directions that member has asked about but the library did not answer, tracked as a zero-hit-query count rather than as source ids). The base layer is refreshed by a per-notebook consolidation job triggered by accumulated source changes; each overlay is refreshed by a per-`(notebook, user)` job triggered by that member's own completed Ask jobs or deep reports (a completed report reaches the threshold immediately, registered as a deliberately simpler trigger than counting reports toward the same accumulator as questions). A report is now **both a trigger and an input** (Agentic Memory P2): the refresh it schedules reads that member's recent Ask traces **and** their own recently completed (`status='done'`) deep reports, projected from each report's persisted `sections_json[i].attempted` account — per confirmed retrieval direction, that direction's own wording plus whether executing it errored, and nothing else. Never section markdown, citations, step type, duration or step sequence (that shape was never persisted, so it cannot be recovered). A member whose only activity is one completed report therefore no longer gets `no_usage_sample`; the report sample alone is enough.

**The report sample feeds `retrieval_notes` only, and contributes nothing to `usage_gaps`.** This is a decision, not an omission. The `attempted` account's one counter, `new`, looks like a hit count but is not: it counts knowledge objects newly added to the *run's shared candidate pool*, so it reads 0 whenever a direction returns material an earlier direction already collected (which a report's same-section directions do by design, since the section question seeds the pool before they run), reads 0 for every direction in a notebook with no knowledge graph, and never counts chunk or element hits at all. Reading `new == 0` as "this direction came back empty" would write "this library has nothing on X" into a member's private notes on the strength of a counter measuring something else, so zero-hit evidence (the `usage_gaps` count and the empty-search samples) stays **Ask-only**, exactly as before P2. What the report sample contributes is *wording* — how this member phrases research directions — which is what `retrieval_notes` is about. For the same reason the rendered report section never asserts a direction count: the attempt-row cap below is indistinguishable from "this report ran no directions" once the rows are gone, so a report whose directions were all truncated away discloses that they were not sampled rather than claiming a number.

The report sample's ownership predicate is `reports.created_by`, which is the report's *original* creator rather than necessarily the member who triggered this particular run — in a shared notebook, any writable member can (re)trigger generation of a report someone else created, so that trigger's own overlay refresh can legitimately come back empty (a normal `no_usage_sample` outcome, not an error) rather than summarise a report they did not create. This asymmetry is registered and deliberately the safe direction: only the member's own rows ever enter their own sample.

**Isolation is structural, not a filter.** The base chain's consolidation input is restricted, by construction, to corpus statistics and KG-object aggregates — its SQL literally cannot reference any usage/query/answer table (`ask_jobs`, `ask_trace_steps`, `answers`, `memory_items`, `conversations`, `reports`), enforced by a semantic allowlist guard (every function in the consolidation module must be classified, each class may only call the port methods on its own allowlist, and trace-read SQL must carry the ownership predicate) rather than a runtime check. The overlay chain's consolidation input is that one member's own retrieval trace **and, since Agentic Memory P2 (T4), that member's own completed deep reports** — never anyone else's — with the ownership predicate (`created_by`/`user_id`) written into the SQL that reads either one rather than filtered afterward in application code — the same shape as the `memory_items.created_by` boundary elsewhere in this document. A member who loses read access to the notebook loses their overlay the same way every other per-member read does (participant-set re-evaluation), and it is physically cleared when that member is removed from the notebook — job/status row first, block rows second, because the in-flight consolidation worker's revocation guards key off that row: deleting blocks first left a window where a still-running worker recreated them and settled green before the marker vanished. **A completion notification that arrives after that removal (P1's registered R4/R5 residuals, closed):** `note_ask_completed`/`note_report_completed`/`start_overlay` (the manual-rebuild entry point) each re-check the same live participant-set predicate immediately before bumping the signal counter or claiming the chain, rather than after — bumping first would recreate exactly the job row the check exists to keep from being recreated. The check fails open (an exception is treated as permission granted, matching every other fail-open rule in this feature), so the residual is narrowed from "always resurrects" to "only resurrects if that one bounded read itself fails"; the job/status-row-generation guard above (`claim_token`, Agentic Memory P2/T2) closes the companion race where the row was already recreated through some other path — a stale worker's `settle()` can no longer silently overwrite a newer claim's blocks, because `settle()` now distinguishes "the row is gone" (member really was removed — wipe) from "the row belongs to a later claim" (never wipe). The two base-chain aggregates that can see private Memory (KG-object type counts, recurring concept names) exclude the Memory synthetic sources *inside their own statements* rather than reading a Memory source-id roster first and then subtracting it / passing it back in as an exclusion list: those reads share no snapshot, so a Memory created or deleted between them makes the subtraction miss and the exclusion list drop a row — and what the second one drops is a **concept name**, written into a block every member reads.

**Blocks can be withdrawn — but never a user-written one.** "Omission keeps the previous value" is deliberate (a model with nothing to say must not clear existing content), but with only that rule a stale claim whose evidence has since been deleted or reparsed away stays in the block forever and rides in every planning prompt. The consolidation reply therefore carries an explicit retirement marker: the model may name a block to withdraw, and the server blanks its value while keeping the row and its change history, recording the origin as the job rather than as a person. Refusal stops at **user-authored** blocks and covers the ordinary update too, not just retirement: a job rewrite of a person's block would flip its provenance to `job` and open their words to retirement on the very next run, so while a user-authored block still has text the server refuses **any** job write to it — update or retire alike — records the refusal, and leaves the rest of the same reply untouched. A person hands a block back to the agent by clearing it: a cleared block keeps `updated_origin='user'` but an empty user block is deliberately not authoritative (otherwise clearing would freeze the label forever instead of meaning "let the agent fill this in again"). Retiring a block that is already empty (or was never written) is not a write.

**The retirement trigger is a rendered evidence-liveness reconciliation, not a guess.** Each refresh renders, beside every base-layer job-written block that carries evidence, a bracketed note reconciling that block's stored evidence ids against this same read's aggregates: ids still in the sampled statistics are named (`supported by: s1, s2`), ids that are still in the library but merely fell outside the sampled top-40 (or are healthy prose with nothing to list) are counted separately (`+N more still in the library`) and never reported as gone, ids no longer in the library at all are counted (`M no longer in the library`), and a block whose evidence has vanished entirely gets the unambiguous `[all supporting documents are gone]` marker instead of a count. Liveness is judged against the **full** user-visible document set from this same read, never the sampled statistics list — the two are deliberately different sets, and judging by the sample alone would report a perfectly healthy document as gone. The prompt is told this note is for reconciliation only: an id spelled out in it must never be copied into a *new* claim's evidence, which may only be drawn from the document ids in the statistics section. User-authored blocks never render this note (they carry no evidence, and can never be retired regardless).

**Evidence is clickable in the UI.** Each job-written block shows what it rests on beneath its text: the three base blocks list the documents behind the claim as clickable chips that open that document's detail (the same open path Ask citation cards use), while `usage_gaps` shows the server's own count — "N searches that came back empty" — as inert text, because its evidence is retrieval behaviour rather than a document. User-written blocks carry no evidence row.

**Values, numeric limits.**

| Setting | Value |
| --- | --- |
| Blocks | 5 (`corpus_shape`, `key_entities`, `corpus_gaps` in the base layer; `retrieval_notes`, `usage_gaps` per-member overlay) |
| Characters per block value | 400 |
| Characters per rendered block (all blocks concatenated) | 1,200 |
| Change-history ring buffer per block | 20 entries |
| Base-chain trigger | 5 accumulated source changes |
| Overlay-chain trigger | 10 completed Ask jobs for that member, or 1 completed deep report (direct threshold) |
| Overlay trace sample read per consolidation | last 40 asks |
| Overlay trace step rows read per consolidation | ≤600 (a separate cap from the 40-ask sample — 40 asks × however many steps each is not itself a bound; when this cap binds, the oldest asks' steps are dropped first) |
| Overlay report sample read per consolidation | last 10 completed (`status='done'`) reports (Agentic Memory P2, T4) |
| Overlay report attempt rows read per consolidation | ≤200 (a separate cap from the 10-report sample, same reason as the trace-step cap above; when it binds, the oldest report's tail directions are dropped first). A report left with zero direction rows is rendered as "directions not sampled" — the cap and "this report ran none" are indistinguishable by then, and with these defaults a full sample overruns the cap routinely |
| Report direction wording clip | 120 characters (the same per-item clip the ask question and step summary use) |
| Consolidation output budget | 2,048 tokens |
| Corpus-stats documents sampled | 40 |
| Evidence ids retained per claim (base chain) | 8 (also the cap on how many are individually named in the evidence-liveness note) |
| Top concept names surfaced to the base prompt | 24, ≤48 characters each, ≤600 characters total |
| Overlay prompt's usage section | ≤3,000 characters shared by BOTH lists (ask questions and report directions), plus ≤12 zero-hit-query samples at ≤120 characters each, plus fixed headers. One budget, allocated rather than first-come-first-served: while there are reports to render the ask list may spend at most half of it and whatever it leaves unspent rolls over to the report section, so neither can starve the other. A member with no reports is unaffected by the split and renders exactly as before this section existed |
| Observation ring per `(notebook, owner)` (Agentic Memory P3) | 200 rows — `append_observation` evicts the oldest beyond this bound in the same write transaction as the insert |
| Characters per observation | 500 (`add_observation`'s `text`) |
| Observation sample read per consolidation | ≤20 most recent, on its own separate query from the ask/report sample above |
| Rendered observation section budget | ≤600 characters — its OWN budget, never a slice of the 3,000-character usage-section budget above |

**Injection surface.** A run that has `AGENT_PROFILE_ENABLED` on (and a bound profile store) injects the base layer plus the current user's own overlay into both the planning prompt and the reflect loop's context, ahead of the collection map section in each. A notebook the agent has not yet consolidated anything for injects nothing and records no trace step — this is a deliberately different rule from Memory's zero-hit `skip` step, because a Memory miss carries the cost of an embedding round trip and a vector scan worth disclosing, while a profile read is a sub-millisecond bounded primary-key lookup whose absence is pure noise on every single run of a fresh notebook. The block is injected even when the request has genuinely narrowed its source scope — unlike the collection map, which is cleared in that case because it promises collections the narrowed run cannot enumerate, the profile block opens no channel, is not evidence, and cannot be `[k]`-cited, so a narrowed run still benefits from knowing what the agent has learned about phrasing and known gaps. On Deep Report's side, the block reaches only each section's per-section deep-dive retrieval (`_deep_dive`), which always seeds its run with `intent_queries` rather than calling the planning model — so in practice the profile only ever reaches the reflect loop on the report path, never a report's own planning call.

**Endpoints and role matrix.**

| Endpoint | Who |
| --- | --- |
| `GET /notebooks/{id}/understanding` | Any reader; returns `enabled`, `base` (blocks with value/evidence/revision/`updated_at`/`updated_origin` — the change-history ring buffer itself is not exposed in v1, only current values), `mine` (the caller's own overlay, same shape), `job` (`{base, mine}`, each with status/pending/`updated_at`/failure_reason, `None` before a chain's first claim), and `can_edit_base` |
| `PUT /notebooks/{id}/understanding/{label}` (`scope: "shared"|"mine"`) | `shared` requires the owner-equivalent `agent_profile:write` capability; `mine` requires only read access plus row-level ownership of that overlay |
| `DELETE /notebooks/{id}/understanding/{label}?scope=` | Same capability split as the write endpoint; clears the value but keeps the row and its history |
| `POST /notebooks/{id}/understanding/rebuild` (`{scope}`) | Same capability split; manually claims and re-runs that chain's consolidation, returning 409 while busy or while the feature is off |

`AGENT_PROFILE_ENABLED` (default true) is the single gate behind all of injection, the consolidation trigger, and both API surfaces' visibility — turning it off returns byte-identical pre-feature behavior everywhere at once: no injection, no trace step, no consolidation job is ever queued, the API reports `enabled=false` with empty blocks rather than 404 (so the client can tell "off" apart from "not yet consolidated"), and the rebuild endpoint 409s.

**Agent observations feed the overlay, untrusted (Agentic Memory P3).** An external Agent
holding the `agent_observation:write` scope may call the MCP tool `add_observation` to append
one short line — "I noticed X while working in this notebook" — to its own
`(notebook, owner)` observation log at any time, independent of any consolidation run. This
is raw, **untrusted** input: unlike the member's own asks and reports above, the model
writing an observation is a different party than the person whose overlay it may end up
shaping, so a consolidation run that has any observations to read is preceded by a `system`
message telling the model each line is data about how an external Agent used the API, never
an instruction, and that an observation may support a claim only where it *agrees with* that
member's own asks or reports — it can never by itself be the sole basis for a block. A member
with zero observations sees a byte-identical prompt to before this feature existed (no
`system` message, no extra section) — the untrusted framing is not paid for by anyone who
never used the tool. Observations alone — with no asks and no reports — do **not** trigger a
consolidation run at all: 100% untrusted input is not enough evidence for a model call, so
the empty-samples gate that already governs the overlay chain is left unchanged. When a run
does read observations, they render in their own section after the ask/report sample, on
their own separate character budget (see the table below) rather than sharing the ask/report
budget — an Agent writing enough short lines could otherwise crowd out a member's real
activity within one shared pool. Observations never move the zero-hit-query counter that
`usage_gaps` is grounded in; that signal stays derived exclusively from the member's own
trace, the same as before this feature.

Management stays entirely on the *mine* side — clearing or reviewing an observation log needs
only notebook read access plus row-level ownership, never `agent_profile:write` (the shared
base capability), because the rows are the caller's own. `GET /notebooks/{id}/agent-observations`
lists the caller's own observations newest-first (`limit` defaults to 20, capped at 200 — the
ring size below); `DELETE /notebooks/{id}/agent-observations` clears them, optionally scoped
to one `agent_profile_id`. Both 409 while the feature is off. The web panel surfaces this as
"Agent 记录" under "我的检索心得", explicitly stating observations only ever feed the
member's own understanding and are never evidence or citable.

**Registered trade-offs.** Notebook deep copy carries neither table forward — a copy starts with no consolidated understanding of its own, by design, since the profile describes how the agent has come to understand *this* library's usage, not a fact about the source material that a copy should inherit. A synchronous `POST /ask` call (which creates no durable `ask_jobs` row) does not advance the overlay-chain counter, matching the existing rule that usage accounting counts durable submitted `ask_jobs`. The base and overlay chains for a single-person notebook are deliberately **not** merged into one execution — each still queues and runs on its own, registered as a P1 simplification rather than a correctness requirement. A consolidation job's failure — of either chain — still consumes the `pending_signal` count captured at claim time, so a failed run requires the trigger threshold to refill again before it retries, capping cost at one model call per threshold batch rather than retrying every subsequent change. Removing a member from a shared notebook clears their overlay through the membership-removal path (`kick_all_members` deliberately does not — a documented exception, since read-side participant-set gating already keeps a removed member's overlay unreachable through every consumer).

### Retrieval strategy experience

A second, independent memory (Agentic Memory P2) sits beside the notebook understanding blocks above: a deployment-GLOBAL library of short, closed-vocabulary retrieval tactics, distilled offline from finished Ask runs and injected as its own bounded prompt block into the same plan/reflect prompts. Where the understanding blocks describe *this notebook*, an experience entry describes *a shape of question* — "in a question with these characteristics, this retrieval action is/is not worth reaching for" — and carries no notebook, source, or user identity at all: the table has no `notebook_id` and no owner column, and every reader sees the same rows.

**Form.** One entry is a closed IF→THEN pair: an eight-key **situation** fingerprint (Ask mode; `result_scope`; `retrieval_effort`; whether the run required completeness; entity-count and mandatory-topic-count bands — `none`/`few`(≤2)/`many`; whether the confirmed intent carried constraints; whether it carried exclusions — every value drawn from a closed enum, none of it the question's own words) mapped to one **action** from an eight-word closed vocabulary (the reflect loop's own retrieval actions, collapsed to their trace-step spellings: `retrieve`/`ppr`/`exact_lookup`/`expand`/`expand_community`/`follow_chain`/`enumerate`/`outline` — `enumerate` is a deliberate wildcard standing for either `enumerate_elements` or `enumerate_kg_objects`, because a finished run's persisted trace cannot distinguish which one it reached for), a **polarity** (`good`/`bad`), and one model-written sentence of **rationale**. A `support` count records how many distilled runs back the conclusion; an `adopted` count records how many times a run that was shown this entry then actually chose that action, and is the primary key of eviction once the table is full.

**Outcome granularity — recovered in P4 for four of the eight actions.** v1 could observe **failure at step granularity** (a zero-hit action) but **success only at run granularity** (this run's own citation count/`evidence_level`), because nothing persisted which step's results the answer actually cited. Agentic Memory P4 (T1/T2) closes that gap on the write side without touching what a step *does*: the four action branches whose results are individually addressable object/chunk ids — `retrieve` (the initial candidate pull and every `add_subquery` turn), `ppr`, `exact_lookup`, `expand` — now write `result_ids` into their own trace-step detail unconditionally, at every call site (both the deterministic seed pass and the agent-chosen turn), an empty list on a genuine zero-hit result being the signal itself, distinguishing "ran, found nothing" from "old trace row, field absent"; the list is bounded to `TRACE_RESULT_IDS_MAX` (see the values table below). `expand_community`/`follow_chain`/`enumerate`/`outline` are deliberately **not** touched — their results are not simple id lists in the same shape, and `search_elements` lands on a separate `fallback` step outside the eight-word action vocabulary entirely, so element-level anchors stay structurally unattributable (a registered boundary, not an oversight). The final `synthesis`/`answer` step separately writes `anchor_evidence_ids`: the answer's actually-bound `[k]` anchors by `object_id`, bounded to `TRACE_ANCHOR_EVIDENCE_IDS_MAX` (the protocol ceiling for a **ranked** answer's anchor list — the largest per-tier `ranked_final_cap` across all five retrieval-effort tiers — that should never bind in practice under the existing budgets for that shape of run; a **collection-enumeration** run is not bound by `ranked_final_cap` at all, since every row entering the synthesis preview gets its own isolated `k5001+` anchor id, so a large enumerated list genuinely can exceed 96 and bind this cap. When it does, the sparse `anchor_evidence_ids_truncated` marker fires and the projection's pass 1 treats the whole run as unattributable — the deliberately safe direction: an oversized enumeration run silently losing its distillation signal costs nothing but a thinner sample, where accepting a truncated anchor set would teach the library false "no hit" outcomes for actions whose real result may have been in the cut tail). Per-action success is now `RunObservation.ActionObservation.anchored_hits` — how many of that action's own `result_ids` (across every invocation in the run) turned out to be an id the answer actually cited — paired with an `attributable` boolean rather than folded into one nullable count (the privacy guard's type-annotation scanner accepts only `int`/`bool`/closed `Literal`, not `Optional[int]`); `attributable=False` means "no evidence either way" (an old trace row, or an action that produced no `result_ids` this run), never "this action did not help". The raw id-to-id intersection happens once, locally, inside the distillation projection's own loop, using function-local variables that never become a field on `RunObservation` — the ids themselves never reach the model that writes an entry's rationale (see the privacy paragraph below). On the trace-display side both keys are opaque handles nothing renders: `getTraceStepDetail` on the frontend has no branch reading `result_ids` or `anchor_evidence_ids` under any step type, by construction (its generic detail-rendering fallbacks read `count`/`found`/`anchors`/etc., never an id-list key). A trace row written before this change simply lacks both fields and falls back to the v1, run-granularity-only observation — `zero_hits`/`citations`/`answered` remain the fallback for exactly that case, not a redundant copy of the same signal; there is no backfill and none is needed, since distillation only ever reads a recent bounded window of finished runs. These two keys' own contribution to one persisted trace row is modest and deliberately has no on/off switch: a typical row (a handful of `result_ids` entries per instrumented step plus one bounded `anchor_evidence_ids` list) adds on the order of 2–4 KB to the row's JSON, and the worst case — every eligible step at its own cap — stays under roughly 10 KB. No switch exists to suppress this because a switch would break the read side's own key-presence rule: an absent key means "this step never wrote a result" (the pre-P4 case), a present key means "this step ran and this is what it found" (even when that is an empty list), and a switch that sometimes wrote and sometimes withheld the key would make both readings indistinguishable from each other.

**Privacy is structural, not a request.** This is the only store in the repository with no tenancy predicate anywhere — no `notebook_id`, no `created_by`, no `owner_id` — because it holds tactics contributed by, and read by, every user across every notebook. The isolation guarantee therefore cannot live in a SQL predicate the way every other store's does; it lives one layer earlier, in the *shape* of what the distillation model is ever allowed to see. A finished run is projected to a frozen `RunObservation` (and every type reachable from it) whose every field is an `int`, a `bool`, or a closed `Literal` — there is no free-text field anywhere in its reachable shape, so the model writing an entry's rationale has never seen a question, an answer, a document title, or an id. That is a property a test can check by reading type annotations rather than trusting a prompt instruction to "parameterize" identifiers, which was the design's original sketch and was rejected precisely because a leak in that shape produces no error and no failing test. A dedicated privacy guard scans both the projection module and the distillation-prompt module together (not either alone — moving a `question`-shaped read from one file to the other would otherwise defeat a guard that watched only one), asserting the closed-field property, a forbidden-identifier-name scan naming exactly why each name is dangerous, and a reverse guard that the action vocabulary contains no scope-shaped word. The cost of the guarantee is expressiveness: an entry cannot say "list the table of contents first, then drill in by title" — only "`enumerate` is worth reaching for on this shape of question, `exact_lookup` is not." Two situation features the design sketch originally proposed are deliberately **not** collected in v1, both registered rather than forgotten: anything derived from the question's own text (whether it contains a quoted phrase or a look-uppable identifier — collecting either would require the projection to touch `question`), and notebook shape (document-count band, corpus language, whether a KG exists) — the latter because, with `support` often equal to 1, a distinctive corpus fingerprint on a global row every user reads would identify one person's one run in one library. The design doc's original "notebook-weighted" retrieval-experience selection (§12-Q3) is therefore, in v1, weakened to intent-*shape* matching alone (registered deviation; see the design doc's phase table).

**The rule that never changes: an experience influences HOW a run searches, never WHAT it may read.** The action vocabulary is structurally incapable of naming a scope-changing action — it contains no source-scope or reference-library action — and a rendered entry carries no source, notebook, or library name to render. The "user's checkbox selection is the only source of retrieval scope" rule is untouched.

**Distillation** is a separate, deployment-wide, low-frequency offline job (own workload id, `retrieval_experience_distill`, so it can be pointed at a different model or disabled independently of notebook-understanding consolidation) gated by its own accumulated-completed-Ask-count threshold, reading a bounded batch of the most recently completed asks (bounded twice, like the understanding overlay's trace sample: an ask count and a separate step-row ceiling). It needs no cursor table: re-processing an overlapping batch is idempotent because each entry's `support` only increments for a run id not already in its own bounded, newest-first `provenance` list — an invariant the batch size and the provenance cap are pinned together to guarantee (one batch must never carry more runs than one entry can remember, or evicted-from-the-tail ids come back and get counted twice). The model sees the batch's most frequently observed situations (bounded per call, to keep the prompt bounded without capping the batch's run count) plus, for each one, the library's own existing entries for similar situations (Mem0-style local update) and returns `ADD`/`UPDATE`/`NOOP` per situation; a malformed reply is rejected whole rather than repaired, and the library keeps its prior state. Eviction, once the table's hard cap is reached, is ascending `(adopted, support, updated_at)` — unused entries first, then thinly supported ones, then oldest.

**Passive injection is task-level** (auto-pushed into every plan/reflect prompt of a run, whether or not the model asked for it), gated by its own flag independent of the distillation flag — a deployment can distill and observe without ever injecting, exactly the risk mitigation the design doc registered for "the effect may not be worth it." When on, a run scores the whole (hard-capped, in-memory, pure-function) table against its own current situation — computed before intent even exists for the deep-report per-section case, in which case unavailable keys fall back to their `unknown`/`none`/`false` default — and injects up to three closest entries (similarity floor 0.5; below it an entry is judged about a different shape of question, which is worse than no entry, since the model cannot tell the two apart) as one small block, rendered immediately after the notebook-understanding block and before the collection map in both the planning prompt and every reflect-loop turn. Like the understanding block, it is prompt scaffolding only — `ReasoningResult` gains no field for it, it is never evidence, and it can never be `[k]`-cited. On Deep Report's side it reaches only each section's per-section deep-dive (which always seeds with `intent_queries` rather than calling the planning model), using that section's own persisted `result_scope`/`completeness_required` where a full intent contract does not exist. `adopted` is credited only for entries whose polarity is `good`, only for the entries actually **delivered** within the block's own character cap (not merely selected — a row that did not fit is never shown to the model and so could not have been adopted), and only when the model's own reflect decision names that action on a turn where the block was non-empty.

**Two costs disclosed rather than assumed away.** First, worst-case delivery: the block's whole-block cap (600 characters, including its fixed header and framing sentence) leaves roughly 380 characters for rows; when a delivered rationale rides near its own 160-character cap, only about one of the selected top-3 entries actually fits — "top-3" is a selection width, not a delivery guarantee, and the caller reports the block's actual delivered count rather than the selected count for exactly this reason. Second, cumulative reflect cost: the block is re-appended to the reflect prompt's own context on **every** reflect turn, not once per run, and its size is not scaled down at lower effort tiers — at the `exhaustive` tier's 50-step ceiling that is on the order of 30,000 characters of repeated block text over one run's lifetime, which is why the injection flag defaults to **off** independent of the distillation flag defaulting to **on**.

**Model-pulled recall: `consult_memory` (Agentic Memory P4, T5).** A second, genuinely step-level companion to the passive block above: a zero-parameter reflect ACTION — `consult_memory` — the model may choose to spend one reflect turn on, rather than an auto-push. It exists in the reflect schema's `next_action` enum only when **three** conditions hold at once: the deployment's own kill switch (`REASONING_CONSULT_MEMORY_ENABLED`, default on), the run's `retrieval_effort` tier is `deep`/`thorough`/`exhaustive` (never `overview`/`standard` — the same "not worth the cost at a cheap tier" reasoning as the outline scratchpad, but with a lower bar, since a `consult_memory` call costs one reflect turn rather than triggering per-section synthesis), **and** the passive-injection flag (`RETRIEVAL_EXPERIENCE_INJECT_ENABLED`) is also on. That third condition is a deliberate narrowing from the design sketch's independent "kill switch + tier" gate, registered in the design doc: when the injection flag is off, the experience library is unreachable for the whole deployment, so offering an action that can never return anything would spend a real reflect-turn budget on a channel that is provably always empty. Deep Report's per-section deep-dive inherits the same gate automatically — depth maps to a retrieval-effort tier the same way it does for the outline scratchpad (PR-5), and the deep-dive's own `limits`/`retrieval_experiences` wiring is unchanged, so no new plumbing was needed for the action to become available there once depth resolves to `deep` or above.

`consult_memory` reads the same table the passive block reads, under the same similarity floor and closed vocabularies, but the **selection** differs on purpose, in two ways that make each call worth a turn rather than a second copy of the same three rows: it excludes whatever the passive block (or an earlier `consult_memory` call this run) already delivered — the model has already seen those rows every round, so repeating them would spend a turn for no new information — and it sorts entries about an action that has gone quiet **this run** first, because that is precisely the moment a model deciding whether to keep pushing on a stalled channel would reach for this. A call also merges in, at most once per run, the caller's own not-yet-delivered "检索心得" overlay line from the P2/P3 notebook-understanding block (`agent_notebook_profile`'s per-member `retrieval_notes` row) when the shared understanding block's own whole-block character cap happened to clip it off before the model ever saw it — the same class of "attempt once, then stop repeating" delta the row-selection half already applies. Everything the run has accumulated across every `consult_memory` call so far is re-rendered as ONE block on every call (not appended as a second capped block per call), so two calls in the same run never together cost more prompt budget than one passive block would; the whole rendered block, across the run's lifetime, is hard-capped at `CONSULT_MEMORY_BLOCK_MAX_CHARS` (see the values table). It is zero-LLM and zero-embedding — a pure, in-memory scored selection over rows already held in process memory, the same cost shape as the passive block's own scoring.

**Rendering priority and delivered-only bookkeeping (修复轮 spec④/Q-P1-3).** The 600-character cap drops whole rows/lines that do not fit, exactly like the passive block, but *which* content wins the remaining budget when both a library row and the overlay note are in play is not arbitrary: the overlay note renders FIRST, ahead of every library row. It is a single, bounded, personal signal — this member's own retrieval notes, surfaced nowhere else — where a library row is one of possibly many shared tactics that can simply be offered again on a later call if it gets crowded out this time; when budget is tight, the scarcer signal wins the seat. The renderer reports back exactly which rows and whether the overlay note actually made it into the rendered text, and the caller's bookkeeping follows that report rather than the pre-render selection: `consult_delivered_ids` only gains the ids that were truly rendered (a row selected but crowded out by the cap stays eligible for a future call — marking it "delivered" anyway would permanently exclude it from ever being shown), and the overlay note is only recorded as delivered (and thus stops being re-offered as "new") once it has actually appeared in rendered text at least once. The trace step's `entries` count is this call's own newly-delivered row count post-budget, not the pre-render selection count — the same "report what was actually shown, not what was chosen" discipline `rendered_row_count` already applies to the passive block; `chars` remains the whole accumulated block's current length. A call that selects nothing new at all (every matching row already delivered, and no fresh overlay line either) records a `skip` step with reason `consult_memory_nothing_new`, exactly as before; a call that DOES select something new but the 600-character cap crowds all of it out records a second, distinct `skip` step with reason `consult_memory_block_full` — the budget (`consult_used`, and hence `REASONING_MAX_CONSULT_MEMORY`) is still spent either way, since a real attempt was made, but the two reasons let a trace reader tell "nothing to offer" apart from "found something, couldn't fit it" at a glance. Calls are capped at `REASONING_MAX_CONSULT_MEMORY` per run (see the values table); a call past the cap also records a `skip` step naming the reason.

**Server-driven step-level nudge (Agentic Memory P4, T6).** A separate, zero-LLM mechanism, gated **only** by the injection flag (`RETRIEVAL_EXPERIENCE_INJECT_ENABLED`) — no effort-tier requirement, because it costs no extra turn: the server itself appends one sentence to the reflect loop's own account-back-to-the-model summary, immediately after any of four instrumented action branches (`ppr`/`exact_lookup`/`expand`/`follow_chain` — the same four whose dispatch code already computes a deterministic "how many new results this turn" count as part of its own bookkeeping) records its **second** consecutive zero-new-result invocation within one run. The nudge names the action, states the zero-hit streak length, and quotes — verbatim, not paraphrased — the rationale of the best-matching `bad`-polarity library entry about that same action and the run's own current situation (same similarity floor as everywhere else in this feature; if the library has no matching `bad` entry, no nudge fires for that action — a nudge with nothing to say is worse than silence). It is capped at two nudges per run, total, across all four tracked actions combined (not two per action) — the cap exists so four simultaneous stalled channels cannot quadruple the length of one reflect turn's account-back. Once an action has been nudged, it is never nudged again this run, regardless of how many further zero-hit invocations it racks up. The wrapper sentence is a fixed Chinese template, quoted here exactly as the code writes it — `（提示:「{动作}」这类动作在当前场景已连续 {N} 次未拿到新证据;以往打法经验:「{rationale}」。可考虑改用其他动作。）` — with the quoted rationale embedded exactly as the library stored it (already capped to `RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS`, the same 160-character write-side cap the passive block relies on — this reuses that existing cap rather than introducing a second one), reusing the model's own stated shape (`_ACTION_IDS`) so the action name matches what the schema calls it, not the internal trace-step spelling. Fail-open throughout: reading the library, scoring the situation, and formatting the sentence are all pure functions over data already held in memory this reflect turn, so a read failure anywhere in the chain simply produces no nudge for that turn rather than interrupting the reflect loop.

**Visible only through the trace.** v1 adds no `AskResponse` field and no management panel — a global, cross-user, closed-vocabulary table with `support`/`adopted` counts is still usage information about other people's runs, so the only user-visible surface is one lightweight trace step (UI label "打法") reporting the delivered entry count and character count, the same "collection-map-shaped internal scaffolding" register as the collection map itself. P4's `consult_memory` gets its own trace step (UI label "回想", deliberately distinct from both "打法" and the Memory-recall step's "记忆") reporting this call's own newly-delivered entry count (post-render, per 修复轮 spec④ above — not a running total); P4's step-level nudge adds no new step type at all — it rides inside the existing reflect account-back text, exactly as the "unexecuted confirmed directions" disclosure already does. An administrator-facing view of the library's contents is left to a later phase.

| Setting | Value |
| --- | --- |
| Entries the deployment keeps | 300 (quality bound, not a storage one — eviction is `(adopted, support, updated_at)` ascending) |
| Provenance run ids retained per entry | 60 (newest-first; invariant with the batch size below) |
| Distillation batch — completed asks | 40 |
| Distillation batch — trace-step rows | ≤600 (separate cap from the 40-ask sample, same reason as the notebook-understanding overlay's step cap) |
| Distillation trigger threshold | 40 accumulated completed asks (deliberately equal to the batch's ask count) |
| Rationale characters | 160 (over-length is a rejection of that one entry, not a silent clip) |
| Distillation output budget | 1,024 tokens |
| Situations offered to the model per distillation call | 4 (the most frequently observed in the batch) |
| Existing similar entries offered per situation | 3, similarity ≥ 0.5 |
| Count-band boundary (`entity_count`/`topic_count`) | `none`=0, `few`≤2, `many`>2 |
| Situation-key vocabulary | 8 keys (mode, result_scope, retrieval_effort, completeness_required, entity_count, topic_count, has_constraints, has_exclusions) |
| Action vocabulary | 8 words (`enumerate` is a wildcard for either enumeration action) |
| Polarity vocabulary | 2 (`good`/`bad`) |
| Injection — entries delivered | ≤3, similarity floor 0.5 |
| Injection — whole-block character cap | 600 (header + framing + rows; rows that do not fit are dropped whole) |
| Step→anchor attribution — `result_ids` per trace step (`TRACE_RESULT_IDS_MAX`) | 20 |
| Step→anchor attribution — `anchor_evidence_ids` per run (`TRACE_ANCHOR_EVIDENCE_IDS_MAX`) | 96 (protocol ceiling for a **ranked** answer — the largest per-tier `ranked_final_cap` — so it should never bind there; a collection-enumeration run's `k5001+` anchors can exceed it, in which case it truncates and the whole run becomes unattributable — the safe direction) |
| Step→anchor attribution — one trace row's own storage footprint | typically 2–4 KB (`result_ids` + `anchor_evidence_ids` combined); worst case (every eligible step at its own cap) ≈10 KB — no separate on/off switch (would break the read side's key-presence rule) |
| `consult_memory` — calls per run (`REASONING_MAX_CONSULT_MEMORY`) | 2 |
| `consult_memory` — entries returned per call (`CONSULT_MEMORY_TOP_K`) | 3 (same order of magnitude as the passive block's own top-K, still "a few tactical hints") |
| `consult_memory` — whole-block character cap across the run (`CONSULT_MEMORY_BLOCK_MAX_CHARS`) | 600 (same shape and value as the passive block's own cap; the run's accumulated selection is re-rendered under this one cap on every call, not appended per call) |
| `consult_memory` — offered effort tiers | `deep`/`thorough`/`exhaustive` only (never `overview`/`standard`) |
| Step-level zero-hit nudge — zero-hit streak before a nudge fires | 2 consecutive zero-new-result invocations of the same action |
| Step-level zero-hit nudge — nudges per run | 2 (total across all four tracked actions combined, not per action) |
| Step-level zero-hit nudge — quoted rationale character cap | reuses the existing `RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS` write-side cap (160) — not a new, separate cap |

### User search profile

Agentic Memory P3's second, independent B-line: a small per-user preference document,
`user_profiles.search_profile_json` (`NULL` = the user has never set a preference and no
inference job has ever written one — the same contract `ui_mode` already uses), edited from
"我的回答偏好" in the account menu and injected as one bounded line into Ask's planning and
answer-synthesis prompts. It never touches retrieval — no source, notebook, or scope field
exists in its shape — only how the answer is organized and worded.

**Shape.** Four closed fields, each carrying `{value, origin, updated_at}`: `answer_language`
(`auto`/`zh`/`en`), `answer_shape` (`auto`/`bullets`/`table_first`/`prose`), `answer_detail`
(`auto`/`concise`/`detailed`), and `domain_terms` (a free-text list, ≤10 terms of ≤32
characters each). `origin` is `"user"` when a person explicitly set the field or `"job"` when
the T7 inference job filled it in. A field's absence — not a stored `"auto"` — is what makes
it re-inferable and what the renderer treats as "say nothing here"; a person clearing a field
back to automatic deletes its entry rather than storing an explicit `auto`, the same "clearing
hands it back to inference" contract the notebook-understanding blocks use for a withdrawn
block. A job write can never overwrite a field whose current stored origin is `"user"` — the
same rule `agent_profile_job`'s `user_authoritative` already enforces, applied here to
preference fields instead of prose blocks.

**v1 infers exactly one field, deterministically, with zero model calls.** A background job
(own per-user trigger, distinct from the notebook-understanding chains) reads that user's most
recent completed asks' language and, when the sample is large enough and one language is a
clear majority, writes `answer_language` with `origin="job"`. Below the sample floor, or when
no language is dominant, the job writes nothing — writing a guessed `auto` would block a
better later sample from ever filling the field. **Two other candidate signals are
deliberately NOT inferred in v1** (registered, not an oversight): a person's most-used
retrieval-effort tier (there is no consumer that would safely act on it yet — pure risk with
no benefit), and frequently-used domain terms (fixing a user's own past wording into every
future prompt as if they had asked for that permanently is a different kind of claim than
"you tend to ask in Chinese"; v1 leaves `domain_terms` entirely user-authored). **A job-written
value is never injected into a prompt on its own** — mirroring the P2 experience library's
"attach the machinery, gate the injection behind validation" posture: an inferred
`answer_language` directly contradicts the answer prompt's own "answer in the question's
language" default the instant the inference is wrong, so the settings UI shows the inferred
value with an "inferred" badge and a person must explicitly accept it (which is exactly the
`origin="user"` write) before it can reach a model call. `domain_terms` and any
explicitly-chosen `answer_shape`/`answer_detail`/`answer_language` (`origin="user"`) inject
immediately — those are the fields a person actually asked for.

**Injection surface: Ask only, v1 — Deep Report is deliberately not wired.** When
`search_profile_wiring_active` (gated by `USER_SEARCH_PROFILE_ENABLED`) is on and the user has
at least one user-authored field, the rendered block appears in both the planning prompt and
every answer-synthesis call, with an explicit boundary sentence: it affects wording and
organization only, never evidence availability or `[k]` binding. It is not injected into the
reflect loop — wording preference has no bearing on which retrieval action to take next, unlike
the notebook-understanding blocks and retrieval-experience entries that do inform reflect.
Report generation (`report_engine.py`) does not read it at all in v1; a later phase may extend
it there.

**Values, numeric limits.**

| Setting | Value |
| --- | --- |
| Fields | 4 (`answer_language`, `answer_shape`, `answer_detail`, `domain_terms`) |
| `domain_terms` cap | ≤10 terms, ≤32 characters each |
| Rendered style-block character cap | 600 (`SEARCH_PROFILE_BLOCK_MAX_CHARS`) — sized so the maximal legal profile always renders completely (codex #535 R5: a budget smaller than the combined input caps silently drops choices the UI reported as saved); per-term packing is kept as defence in depth |
| Inference sample size | last 30 completed asks (`SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT`) |
| Inference minimum sample floor | 10 (`SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES`) — below this the job writes nothing |
| Inference majority threshold | 0.7 of the full sample (`SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO`; the "other"-language share stays in the denominator) |
| Inference job trigger | 20 completed asks for that user (`USER_SEARCH_PROFILE_TRIGGER`, default) |
| Deployment gate | `USER_SEARCH_PROFILE_ENABLED` (default true) — off reverts everywhere to byte-identical pre-feature behavior; `GET /me` still returns any existing value already on the row rather than forging `search_profile: null`, but `PATCH /me/search-profile` 409s |

### Outline scratchpad and section-by-section synthesis

Reasoning Ask gated to the `exhaustive` effort tier can maintain a bounded, model-authored outline scratchpad across its reflect loop, and — when the final outline resolves into two or more sections that still carry live evidence — synthesizes the answer section by section from it rather than as one pass over everything. This is controlled by `REASONING_OUTLINE_ENABLED` (default true); when off, or at any lower effort tier, the mechanism is entirely absent — no action, no schema branch, no trace step, byte-identical to the feature not existing.

**The `update_outline` action.** The reflect loop gains an eleventh action id, `update_outline`, offered only at `exhaustive`. Each call submits the whole section structure (full-replacement, not an incremental structural patch): at most 12 sections, two levels deep (a section may name a parent), each title capped at 60 characters, and each section binding at most 8 evidence keys. Evidence has a separate citation-persistence rule: a submitted section with the same stable id unions its legal `evidence` keys with the old bindings, so omission never deletes evidence; `remove_evidence` explicitly removes named old bindings and wins if the same key also appears in `evidence`. Old bindings have priority at the eight-key cap. Newly submitted keys that do not fit are named in the next scratchpad rather than silently evicting old evidence, so the model can explicitly free space and retry. Pending overflow alone never freezes a later ordinary update: while ordinary allowance remains, the model may still add, remove, reorder, or retitle sections and every legal binding in that full payload is merged normally. A `sufficient` round that submits `update_outline` and still leaves pending overflow may receive at most one evidence-only correction if a normal reflect step remains; the stale breaker grants the same single correction on pending overflow alone — no same-round submission is required — while a terminal `answer` never adds one; the sixth ordinary outline update may likewise expose one evidence-only attempt after its own allowance is exhausted. Those correction submissions must preserve every section id/title/parent, cannot execute retrieval, and the post-sixth qualification is consumed by the first submitted attempt even when its structure is rejected. The overall reasoning `max_steps` ceiling is absolute: overflow on its last step is disclosed rather than creating step 51 of 50. The stale breaker is recorded before any in-budget terminal correction because the breaker already happened. Any keys still rejected afterwards are disclosed in a closing trace step and the answer uses only accepted bindings. A structurally omitted section is still removed. A run may ordinarily call the action at most 6 times; further non-repair submissions are skipped and the model is told the outline now stands as final. A submission with no usable section is skipped and leaves the previous outline untouched — the worst outcome would be a malformed reply wiping out a working outline, so the mechanism never lets one bad round erase established structure.

**Evidence bindings are server-validated, not model-asserted.** A binding key is legal only if it is still in the run's live candidate pool and either appeared in at least one candidate-summary window or is already held by the current outline. The run keeps that ever-shown set monotonically, so a previously visible binding remains legal after sliding into the summary's omitted middle, while an id from that never-rendered middle cannot pass merely because the model guessed it. Enumeration-listing item ids and source ids are deliberately excluded: the model is never shown the former (listings return counts, not ids), and one document is not one piece of citable evidence. Illegal keys are dropped without comment; a section that ends up with no legal bindings is recorded as an empty section rather than discarded — empty sections are precisely the retrieval gaps the model should target next.

**Rejected-key persistence.** An overflow key is server-held pending state, not a one-round diagnostic. It remains named across correction attempts until it is accepted into the section, the model explicitly names that pending key in `remove_evidence` to abandon it, or the full-replacement outline removes the section itself. Freeing an old slot but accidentally omitting the pending new key therefore leaves an explicit unresolved-overflow trace instead of silently losing the new evidence. The complete pending state is structurally capped at 56 keys per section (six ordinary submissions plus one correction, eight keys each), but each reflect prompt exposes only the first eight plus a remaining count; processing or abandoning that batch reveals the next one, matching the eight-key `remove_evidence` input rail without growing the prompt.

**The outline feeds back whole, not as a diff.** Because each reflect round is a fresh prompt with no conversation history, and the section structure is full-replacement, every round re-attaches the entire current outline plus a per-section list of missing bindings, so the model always sees exactly what it produced instead of reconstructing it from memory. Evidence omission is nevertheless harmless because the server unions bindings by stable section id. Purely cosmetic re-submissions of the outline are stale-neutral: they never count as progress toward the reasoning loop's no-progress circuit breaker (genuine retrieval rounds continue to reset it on their own), so alternating two differently-worded versions of the same outline cannot be used to dodge that breaker.

**Adoption nudge.** Offering the action turned out not to be the same as having it used: on real corpora the model repeatedly listed a notebook's entire document roster to completion and then spent its remaining rounds on undirected retrieval instead of turning that roster into sections. So when the outline is still empty and the server already holds a structural reason to open one, one deterministic line is appended to the reflect context alongside the other ledgers. It appears only while all of these hold: the outline gate is on; the current outline is **empty** (once the model opens one, the scratchpad itself takes over that slot); the run has not yet used its nudge allowance of **2 rounds**; and one of two structural reasons exists — either this run already listed the **source roster to completion** with at least **2** documents, or the confirmed intent supplied at least **2** mandatory retrieval directions. The roster wording wins when both hold, because it carries a real count. A roster that is not complete never counts: an outline built from a half-listed roster is missing sections the model does not yet know are missing. The line states the fact, names the count, and explicitly offers an opt-out — it is a ledger entry, not an instruction, and the model remains free to answer in one pass. It triggers no extra query or model call, adds no action id, and is byte-for-byte absent below `exhaustive` or with the feature switch off. A round that actually emits it marks the existing reflect trace step with `outline_nudged: true`; rounds that do not emit it leave that step's detail keys unchanged.

**Section-by-section synthesis.** At the end of the run, if resolving the final outline's bindings against the live candidate pool still leaves two or more sections with at least one legal binding — and the run produced no collection listing (typed enumeration or structured whole-table batch: a listing run stays on the single-pass path, because listing previews and coverage disclosures enter only the single-pass synthesis context and section slices cannot carry them; sectioning such a run would synthesize prose from a ranked sample while a complete listing sat unused) — the answer is generated once per section, each call seeing only that section's bound-evidence slice assembled through the same evidence-context machinery used elsewhere in reasoning Ask. Key ranges are offset per section so no two sections' citation numbers collide; each section's citation markers are resolved against that section's own id map before the sections are merged — a marker naming another section's range can only be a hallucination, since the model never saw that range, and is discarded rather than silently attributed to the wrong section's evidence. The answer text is the sections' own `##`/`###` headings stitched together in outline order (rendered at the chat-answer heading scale, kept distinct from the Deep Report heading scale). The collection map, enumeration-tool previews, private Memory, and query-time derived chains never enter a section's slice — none of them are legal binding targets, so a section could never have "asked" for them — and remain exactly as they were on the ordinary single-pass path, which any section failure falls back to. When sectioning is bypassed — fewer than two resolvable sections, or a listing run — the outline disclosure does not vanish with it: whenever outline planning ran, the closing synthesis detail still carries the outline keys (`outline_sections` 0, `outline_fallback` false) including the skipped-section titles, so a one-aspect answer cannot look complete while silently dropping the aspect that found nothing. Each section is classified against its own slice and anchors. The closing synthesis detail's `section_grounded` value is a bounded list of section records (`id`, `title`, classified `evidence_level`, and a boolean `grounded`), not the whole-answer flag; it is accompanied by bounded ungrounded-section titles. The existing three-level response `evidence_level` remains the contract: all synthesized sections grounded leaves the ordinary global `classify_evidence` result unchanged; otherwise that global result is capped at `overview`, never raised. Thus zero sections with the exact `grounded` level can still honestly remain `overview` when every section has relevant cited evidence but the model reports conservatively; it is not forcibly mislabeled `inferred`/“no notebook evidence.” Any section whose synthesis call fails (after its own retry) discards the entire partial product and falls back to the ordinary single-pass answer instead of shipping a silently incomplete document; if that fallback succeeds, the section failure is removed from the user-visible error banner (event logs still record it), since the run recovered.

**A recovered synthesis retry no longer raises a banner.** Answer synthesis retries once when the first call fails or returns empty content. That bounded retry now follows the same rule as the fallback above, on every Ask path rather than only inside sectioning: when the second attempt produces the answer, the failures recorded during that call are dropped from the response's model-error list, so a run that recovered does not display "本次回答可能不完整" over a complete answer. When both attempts fail, every alarm is kept — including the terminal empty-content one, because "retrieved but could not answer" must stay visible. Only that call's own answer-synthesis entries are dropped — the removal filters by workload identity rather than by position, so alarms other workloads record in the same run (evidence refinement, embedding, reranking), whether before the call or from inside it, are untouched, and `events.jsonl` records everything either way.

**Visible only through the trace and the answer's own heading structure.** v1 deliberately adds no new `AskResponse` field for the outline: each outline update lands as its own `outline`-typed trace step (UI label "大纲"), each completed section lands as a lightweight `synthesis`-typed progress step, and the final answer shows the sections' own Markdown headings. Deep Report's own per-section deep-dive shares this same mechanism at its exhaustive depth tier — see "Deep Report outline co-evolution" below. The report's own confirm-then-freeze research-question contract is unaffected: outline co-evolution there only shapes a section's internal retrieval and drafting structure, never the confirmed section/topic bindings.

**KG weak-support gap feedback.** When the outline mechanism is active, the server also feeds the model a directed retrieval hint drawn from the knowledge graph itself: after each *accepted* `update_outline` call, it looks at the KG objects the model just bound as evidence and finds canonical relations leading out of them (outbound only — the reverse direction has no index and is a registered residual) that only one or two sources corroborate — for a survey-shaped question, exactly the direction most worth chasing further. This is controlled by `REASONING_OUTLINE_KG_GAP_ENABLED` (default true), layered on top of the outline gate itself; either off means zero extra queries and a byte-identical prompt. The hint is offered through the model's existing actions (`add_subquery` / `follow_chain` / `expand_graph`) — no new action id, no new model call, no schema change. It never enters the answer text or its citations; it is scratchpad guidance for retrieval only. Numeric ceilings — the source-count threshold, probe limit, seed cap, per-round line count, and per-line/segment character limits — are contract values kept in a single table below, not repeated here. Each round's `outline`-typed trace step gains a `kg_gap_candidates` integer whenever the accepted apply queues new candidates (a terminal correction round's candidates are recorded but never rendered, since that round's own scratchpad text says not to run retrieval). If the canonical-relation layer is absent (never built) or the probe raises, the feature is silently and harmlessly absent for that round — a KG hint is optional, never a reason to fail a run.

| Setting | Value |
| --- | --- |
| Weak-support threshold (source count) | ≤ 2 |
| Probe result limit per apply | 24 |
| Seed cap per apply | 96 |
| Hint lines per reflect round | ≤ 6 |
| Characters per hint line | ≤ 80 |
| Characters per hint segment | ≤ 520 |

### Deep Report outline co-evolution (research depth ↔ retrieval effort, PR-5)

Deep Report's "研究深度" (research depth) slider and reasoning Ask's "检索档位" (retrieval effort) picker share the same five tier names. Each section's deep-dive (`_deep_dive`) now maps its own `depth` value onto the identical effort-tier retrieval budget `ask_retrieval_limits` uses, instead of always running at the `standard` budget regardless of slider position (the pre-PR-5 behavior). The mapping is threshold-based rather than a fixed dictionary, since the API clamps `depth` to any integer in `[1, 16]`, not just the five slider stops:

| Depth ≥ | Effort tier |
| --- | --- |
| 1 | overview |
| 2 | standard |
| 4 | deep |
| 8 | thorough |
| 16 | exhaustive |

An in-between depth value (e.g. 3, 5, 7, 15) resolves to the next **lower** threshold's tier — it does not round up. Each section's own reflect-step ceiling (`max_steps`) still equals the report's own depth value (1/2/4/8/16), never the effort tier's own step ceiling (4/8/16/32/50): a report's cost scales by section count, so multiplying a per-tier step ceiling across every section is not the cost the user agreed to on the depth slider; the tighter of the two numbers always wins.

**Behavior change, registered explicitly.** Because the retrieval budget was previously fixed at `standard` regardless of depth, the low depths (1, 2) now retrieve with a smaller budget than before, and the high depths (8, 16) retrieve with a larger one. This is the intended alignment fix — the same tier name now buys the same relevance/context budget in both Ask and Deep Report — not a regression.

**The tier also scales the outline stage's 0-LLM probe width.** The coverage probe (per mandatory topic) and the sufficiency probe (per drafted section) each execute up to a per-tier number of retrieval queries — `overview` 2, `standard`/`deep` 3, `thorough`/`exhaustive` 4 (the pre-feature width was a fixed 4 at every depth; a report row without a depth keeps that historical width). The default tier deliberately keeps 3: the confirmed question heads every topic's query list, so a width of 2 leaves one topic-specific probe, and the sufficiency verdict's thresholds are sample-size sensitive. Registered consequence: lower tiers feed fewer probe hits into the same 充足/薄弱/缺失 thresholds, so their outline sufficiency verdicts read more conservatively than the historical width-4 ones. Probe output feeds only the coverage counts shown to the STORM planner and the sufficiency judge, never the report's body evidence, so a lower tier trades grounding granularity — not evidence — for planning speed on large libraries. Within one planning run, repeated probe queries (the confirmed question leads every topic's list by design, and topics/sections share retrieval directions) are retrieved once and memoized; the memo never outlives the run and never caches failures. The outline stage's LLM calls also scale down: intent understanding and the STORM call run at every tier, while the sufficiency judge's LLM refinement half runs only at `deep`/`thorough`/`exhaustive`. At `overview`/`standard` each section keeps the judge's deterministic half — the per-section coverage counts and the 充足/薄弱/缺失 verdict the outline editor displays — which is byte-identical to the judge's existing fail-open result (the LLM half only ever refines downward and is skipped entirely on model failure), with the human outline-confirmation gate immediately after. Between the "多视角规划大纲中" and "大纲就绪" progress states the planner now also reports "检查各节证据充分性", so the post-STORM probe phase is visible instead of reading as a stall. Per-section deep dives additionally seed the reasoning run with the section's composite question as the first seed (mirroring Ask, where the full authoritative question is always the first confirmed seed) followed by the section's user-confirmed retrieval directions (`intent_queries`), which skips the per-section planning LLM call — a reviewed direction set is authoritative and is not reinterpreted by a second model; the reflect loop and its evidence-driven follow-ups are unchanged, directions the run could not fit within its step budget are still executed by the post-run merge, and a direction whose in-run retrieval itself failed (a sparse `failed` marker on the run's attempt ledger, distinct from a legitimate zero-hit probe) is re-executed by that merge rather than silently losing its evidence.

**The tier governs the whole section, not just `run()`.** Two stages downstream of retrieval used to run on fixed numbers regardless of the slider, which gave the tier away again after `run()` had honoured it. Both now scale with the selected tier: (a) the per-direction top-up merge — every confirmed retrieval direction is still executed, but each direction retrieves at the tier's own `ranked_per_query_take` and element allowance rather than a fixed 20 + 8, and the merged evidence is re-clamped to the tier's `ranked_final_cap` and `answer_element_items` (by relevance descending, elements tie-broken on `element_id`), so four directions can no longer multiply an overview report past its own ceiling. Objects the outline bound but that the selection already contained take their cap slots first rather than being exempt from the cap (exempting them would let the total drift past the ceiling); only the supplemental `outline_evidence` objects sit outside it, exactly as they sit outside `top_hits` on the Ask side. (b) The section writing context — the KG block now uses `kg_context_chars`, and the chunk block plus the direct source elements **share** `chunk_context_chars` (elements take what the chunks leave, and at most `answer_element_items` of them enter the prompt, chosen by relevance rather than insertion order), instead of the fixed `ANSWER_CONTEXT_BUDGET_CHARS`/`REPORT_SECTION_CHUNK_BUDGET` pair plus a separate one-third element allowance on top. The shared source partition also keeps room for the outline: bound chunks are rendered first (that renderer is per-chunk, so reordering is safe) and bound elements reserve their own measured length from the partition, capped at half of it — otherwise chunks fill the shared budget first and every bound element is handed zero. Outline-bound **elements** take the element cap's slots first and are assembled first (the cap itself stays closed — an outline may bind far more keys than the tier admits), the same rule the KG side applies to outline-bound objects — a binding spans all three candidate id spaces, and a bound element dropped by the cap loses its `[k]` in the discovered-structure block exactly as a dropped object would. Query-time chains and confirmed Memory are admitted against what the KG block leaves of `kg_context_chars` (whole-block: both are self-bounded, and truncating one would cut a `[k]` marker in half); a block that is not admitted does not enter the evidence map either. Outline-bound objects get their priority slice of the KG budget inside a **single** `knowledge_context` call (`priority_object_ids`/`priority_budget_chars`), never by splitting the hits across two calls: that block's `relations:` line is computed over one call's own evidence map, so a split silently drops every edge whose endpoints land on opposite sides.

**Outline scratchpad and KG weak-support gap feedback activate automatically at depth 16 (exhaustive).** Because `outline_wiring_active` is gated purely on `limits.effort == "exhaustive"` plus `REASONING_OUTLINE_ENABLED` (see above), reaching the exhaustive tier through the report's depth mapping activates the same outline scratchpad, `update_outline` reflect action, and (when `REASONING_OUTLINE_KG_GAP_ENABLED`) weak-support relation hint inside each section's deep-dive — no new flag, no report-specific wiring. Collection enumeration tools remain unreachable on this path: the report constructs its `ReasoningRetriever` without a `collection_catalog`/`collection_enumeration`, so the enumeration gate stays closed regardless of tier.

**Discovered structure block (section-local, never rewrites the confirmed outline).** When a section's deep-dive produces a non-empty outline scratchpad, `_deep_dive` folds the finalized sub-outline plus each sub-section's bound evidence keys into a bounded "discovered structure" block (≤12 lines, ≤80 characters per line, ≤1200 characters total; truncation is recorded with an explicit "(+N sub-sections omitted)" suffix rather than silently dropping lines) and passes it to `report_section_prompt` as `discovered_structure`. The prompt instructs the drafting model that this is a **suggestion, not a contract**: it may organize the section body with `###` sub-headings along this structure, must silently skip any sub-topic whose evidence is missing, and must never step outside the section's own scope. It never adds, removes, or renames a user-confirmed section, and it never touches `reports.outline_json` — the report's own confirmed outline (mandatory topics, section bindings) is unaffected. The block is absent below depth 16 or whenever a section's deep-dive produces no outline, so those runs receive no discovered-structure instruction.

**Section-level progress text.** While a section is deep-diving, its live `section_status` phase text is refined from the generic "深挖" to "深挖中（已整理大纲 N 节）" as soon as the outline scratchpad holds at least one section — updated and persisted the moment an `outline`-typed trace step is observed (that write is forced past the throttle: the outline step follows a reflect step that just advanced it, and it is often a section's last reasoning action, so a throttled write would be overwritten by the forced 撰写 update and never reach the user) — with no new table column and no new SSE event; the existing 2-second-throttled persistence is reused.

### Deep Report credibility and synthesis

This report contract prevents a relevance-ranked technical scan from being presented as a complete, independent, or report-wide conclusion. The frozen report understanding uses the shared intent result, including `result_scope` and `completeness_required`. Pending a true report collection enumerator, a request whose scope is `complete`, `aggregate`, or `hybrid` must say that it used relevance retrieval and did not enumerate the collection completely. Assumptions remain visible scope defaults, but never count as evidence and are kept out of the retrieval query.

The same persisted, bounded **资料基础** profile is used by planning, the report disclosure, and the interface. It is built from database aggregates plus a bounded representative page, rather than loading one row per source into application memory. It reports visible and represented source counts, source-type/year distributions (with an unknown-year bucket), a conservative lower bound on duplicate inflation/identity uncertainty, and representatives stratified by available type and year metadata. The complete source-to-family map is never copied into the intent contract, outline, polling response, or model prompt. Only source ids actually touched by probes, claims, or citations are resolved in one bounded batch; that resolver may merge equal nonblank file hashes and equal grounded paper titles among the resolved rows, while every unresolved/uncertain identity stays separate. It does not promise DOI/arXiv/title/file-family canonicalization or a false exact family count for the whole corpus. Sufficiency uses relevant distinguishable-document groups, relevance, and distribution across approved directions; extracted-object and element hit counts are diagnostics, not independent authority. A profile can be absent for two unrelated reasons, and the reader must be able to tell them apart: a run that narrowed the source scope deliberately skips the whole-collection aggregate, while aggregation itself can fail. Both persist an `unavailable_reason` (`scope_restricted` or `failed`) instead of a bare empty profile, the report body and the interface state the matching reason, and only the failure emits the safe `report_corpus_profile_failed` operations event. Generation stays fail-open in both cases. An unavailable marker is a non-empty object, so every consumer tests it explicitly rather than by truthiness — formatting one as statistics would report every count as 0 and feed a corpus summary that was never measured into planning. Legacy reports stored a bare empty profile, cannot be classified after the fact, and keep the original failure wording. The profile counts the **current notebook only** while retrieval is federated over mounted reference libraries, so the disclosure separately states how many distinct reference-library sources the body actually cited. That count is derived from the already-assembled references (so it costs no query) and counts sources rather than anchors; without it, "based on the N visible sources" reads as the whole evidence basis even when most citations came from a mounted library.

References retain their exact clickable anchors. Bibliography grouping is presentation only: every unresolved source stays in its own visible source-id group, rather than disappearing into a shared “unknown” entry. The prose disclosure therefore reports anchor count and **visible source-group count**; the credibility receipt separately reports the smaller identity-verified **distinguishable-document count**, together with a conservative Top-1 anchor-share upper bound and duplicate inflation. Unresolved identities do not increase the independent-document count; for the Top-1 upper bound, every unresolved anchor is assigned to the largest resolved family before dividing by all anchors. `引证覆盖率` is explicitly labelled **high-risk assertion citation coverage**: a deterministic scanner checks only observable forms (numbers with units, `O(...)`, explicit rankings/superlatives, and absolute comparisons) for a valid `[k]` in the same sentence or table row. Headings, code, formula-only blocks, chapter/figure numbers, and marked `（推断）`/`【通识】` prose are excluded; English sentences are audited at their own full-stop boundaries. It cannot establish whether a citation semantically entails the statement. The audit and disclosure always run, but evidence-level downgrade is separately gated by `REPORT_HIGH_RISK_DOWNGRADE_ENABLED` and defaults off until production distributions calibrate the threshold; when enabled, an uncited ratio strictly above `REPORT_HIGH_RISK_UNSUPPORTED_RATIO` caps a grounded section at `overview`.

For comparison/review/classification-shaped requests, planning may return a bounded optional frame of orthogonal facets and conditional axes. The user sees and may edit it alongside the outline; once confirmed the section-level copy is authoritative and the embedded intent copy is only a compatibility mirror. Section execution is `parallel retrieval → at most one global synthesis call → parallel drafting → audit-only final editor` at **every** report depth. The sole gate is section count: cross-section consistency is undefined for a single section, so a one-section report skips synthesis and keeps the streaming `retrieve → draft` pipeline. Depth selects retrieval budgets only. This deliberately trades per-section streaming at the lower depths — a ready section now waits for the slowest retrieval — for one synthesis pass per multi-section report. The synthesis blueprint assigns a central answer, shared definitions, evidence-keyed claims (with conditions/counterevidence), and section ownership/no-repeat handoffs. Writers receive their claims rather than a document-ordered pile of evidence and must lead with a conclusion, distinguish agreement/disagreement and conditions, and avoid paper-by-paper narration or incomparable cross-paper rankings. A claim ledger binds each emitted claim statement and its anchors to the same sentence/table row. Grounding is row-level, not section-level: this is an audit layer over prose the section has already emitted, not a drafting input, so a row whose statement is not verbatim emitted prose, whose evidence key is illegal or absent from that same statement, or that otherwise fails a per-row check is dropped and the rest of the ledger is kept — nothing downstream joins claim rows to each other, so a dropped row cannot poison a kept one. A claim id needs no prior declaration by the synthesis blueprint: a row covering prose the writer added beyond any commitment is a legitimate new claim, not a violation. Submissions past the 24-claim cap are truncated to the first 24 rows before row-level checks run, so an overflow row never drags down the rows ahead of it. Ledger status is one of `missing` (the model did not return a list), `invalid` (the list was empty, or every row was dropped), `partial` (some rows were kept and some were dropped or truncated away), or `available` (every submitted row, after any truncation, was kept). The optional `frame_assignments` tags are organizational rather than evidentiary and degrade per entry instead: an unknown facet key, a value outside that facet's declared vocabulary, or a non-object payload loses only that tag and leaves the rest of the row usable. An off-vocabulary value is dropped, never snapped to the nearest declared one. Trend claims are deterministically capped by cited distinguishable-document count: one remains research-level, two may be labelled developing, and three or more are eligible for high-confidence wording; any cited anchor whose source identity is missing caps the claim at research-level rather than being counted as an independent document. Stronger prose is disclosed as a limitation. The final editor sees the frame, validated blueprint, a valid bounded context that reserves a share for every section and truncates claims as whole JSON records, the high-risk audit, and conflicts on exclusive frame facets; it audits consistency, trend wording, and limitations but never rewrites body sections or adds facts. The interface exposes synthesis status (`available`, `skipped_no_evidence`, `failed_model`, or `failed_validation`) and the number of usable section ledgers (`available` plus `partial`) when those signals carry information, alongside how many of those usable ledgers were `partial` (had rows dropped or truncated). A single-section report suppresses the expected pure no-op receipt (`not_requested` with `0/N` ledgers), as does a legacy report generated while synthesis was gated at depth ≥ 8 — nothing in the payload dates a report. A multi-section no-op, any skip/failure, and any usable ledger remain visible. A model/validation failure is logged and fails open to independent drafting; an evidence-free skip is not reported as a model error.

Missing or malformed frame, blueprint, or claim-ledger data is discarded and falls back to the preceding report path; it never fails the report. There is no dedicated frame-validation call. The global synthesis is the only added model call and runs once per multi-section report at every depth.

| Bound | Value |
| --- | ---: |
| Frame facets / values per facet / axes | 8 / 12 / 8 |
| Blueprint shared definitions / claims | 24 / 60 |
| Writer ledger claims per section | 24 |
| Synthesis evidence characters | 36,000 |
| Final-editor input characters | 24,000 |
| Corpus representatives / type-or-year buckets | 20 / 32 |
| On-demand source identity resolution | ≤ 1,024 actually touched source ids |
| `REPORT_HIGH_RISK_UNSUPPORTED_RATIO` | 0.25 (strictly greater exceeds) |
| `REPORT_HIGH_RISK_DOWNGRADE_ENABLED` | false (audit disclosure remains active) |
| Added model calls per report | ≤ 1, any depth, ≥ 2 sections |
| Minimum sections for report-wide synthesis | 2 |

## Citation images (本段附图)

**A caption (or an image description) is the only entry point.** An image element only enters chunking/retrieval when it carries a non-blank caption (`metadata.caption`) or a non-blank image description (`metadata.description`, i.e. markdown's `> **图片描述**` blockquote; both are covered by the ingestion rules above) — an image with neither is not retrievable at all, by design, and this feature adds no "nearby image" fallback for it. When a caption *does* cause a chunk/element/KG hit to be selected as grounding evidence, the answer's citation surfaces the captioned image next to it, in a panel visually separated from the quoted evidence text and labelled 「本段附图」 — the model never looked at the image; this is response-assembly enrichment on top of an unchanged retrieval/grounding pipeline, not a new form of evidence.

**Response shape.** `CitationImage` (`{element_id, asset_id, caption}`, caption truncated) is a bounded list field on **both** `AnswerAnchor.images` and `Citation.images` (`exclude_if` the list is empty, so an unaffected answer's JSON payload gains not a single byte). Both response shapes need it: reasoning mode's authoritative display path is the `[k]` anchor, not the `Citation` fallback list, so a field added only to `Citation` would leave the primary path with no image. A legacy persisted answer without the field simply renders as plain text — no migration.

**Enrichment, not a new retrieval channel.** One shared batch primary-key read (`evidence_context.py`) takes a batch of candidate element ids per answer, filters to `element_type='image'` rows carrying a non-blank `metadata.asset_id`, and returns the match; it costs zero additional model/embedding calls. It is wired at all four assembly points that already hold complete evidence objects: `ask_chunk`'s chunk citations (via the chunk's full `element_ids`, not just a possibly-empty `anchor.element_id` — a chunk holding "one paragraph + one figure" is exactly the multi-element case where only the id list carries the image), `ask_reasoning`'s anchor assembly (chunk/element/KG anchors each supply their own candidate ids), and `ask_graph`'s two chunk-citation sites. Deliberately excluded without special-casing: knowhow-cell projection rows are typed `knowhow_cell` and never match the `element_type='image'` filter; Memory-derived sources parse their markdown body but the ingestion path never wires a `persist_image` closure for them, so their image rows never carry `metadata.asset_id`.

**Budget is a protocol boundary, not a deployment setting.** It scales with response size and on-screen clutter, not with corpus size or retrieval effort, so — unlike `Settings`-backed deployment knobs — the ceilings are named constants: a per-anchor/per-citation cap, a per-answer cap (counted as **allocated slots**, so the same image cited by five anchors occupies five slots — an upper bound on response bytes, not on distinct images shown), a larger per-**report** cap (a report's reference list is naturally longer than one answer's anchor count), and a caption-truncation length. All four candidates are deduplicated then sorted by ascending element id before truncation, so the same question asked twice returns the same images. Exact values are in the table below.

**Deep Report assembly is deliberately its own call site.** `report_engine.py` attaches images **once per report**, after every section has been drafted and `references` reaches its final globally-renumbered `k1, k2, …` shape (never per-section — that would let each section spend a full per-report budget) — and the call is wrapped fail-open: an image-lookup exception drops only the images, never the already-finished report body (mirroring the existing `_resolve_source_families` fail-open convention). Candidate ids per reference are the union of the reference's own `element_id` and its underlying chunk's full `element_ids`, exactly mirroring the Ask-side rule; a Memory-derived reference context carries no `element_id` key at all, so it is naturally excluded without an explicit check (report reference contexts have no `memory_id` concept to filter on in the first place).

**Excluded by design.** The public report share page's citation allowlist (see 群组知识共享/Public report links above) never projects `asset_id` or `element_id` — the same boundary that already keeps every other internal handle off that page — so a publicly shared report never carries images even though the underlying `ReportDetail.references` does. The admin activity log's citation detail view (`/dev/logs` → Activity) reuses the same citation-detail component and its existing defensive fallback (no images render without a resolvable `notebookId` for the asset-proxy URL), so it degrades to a plain citation with no code change of its own.

**Frontend.** The panel reuses the existing authenticated, viewport-lazy `AuthedImage` fetch: a closed citation popover/reference-detail panel issues no image request. Ask's anchor popover and citation-detail panel let a click open the source detail or a lightbox; Deep Report's reference-detail panel deliberately does not wire that click-through (the report reference panel has no "open source" affordance at all in v1 — an intentional asymmetry with Ask, not an omission).

| Bound | Value |
| --- | ---: |
| `CITATION_IMAGES_PER_ANCHOR` (images per anchor/citation) | 3 |
| `CITATION_IMAGES_PER_ANSWER` (allocated slots per Ask answer) | 12 |
| `CITATION_IMAGES_PER_REPORT` (allocated slots per Deep Report) | 24 |
| `CITATION_IMAGE_CAPTION_CHARS` (caption truncation) | 200 |

### Markdown bundle upload guardrails

| Bound | Value |
| --- | ---: |
| `MARKDOWN_BUNDLE_MAX_ENTRIES` (backend-retained non-directory files after dropping `__MACOSX`) | 2,000 |
| Raw ZIP upload size | the deployment's ordinary `source_upload_max_bytes` per-source ceiling |
| Aggregate declared uncompressed bytes | the same `source_upload_max_bytes` ceiling; every member read is additionally bounded to its declared size plus one byte |
| Accepted ZIP compression | stored or deflate only; encrypted members are rejected |
| Accepted document/image members | `.md` / `.markdown`; png/jpeg/gif/webp by magic bytes |
| `BUNDLE_DIR_MAX_DEPTH` (browser compatibility path for a dropped folder) | 16 |
| `BUNDLE_DIR_MAX_FILES` (dropped-folder file count) | 2,000 |
| Dropped-folder total-byte cap (checked before reading content): `min(source_upload_max_bytes × 4, 1,024 MiB)` | formula shown |
| `MD_BUNDLE_MAX_SUGGESTIONS` (near-miss path suggestions per unmatched folder image) | 3 |
| `INLINE_TOO_LARGE_IMAGE_LINES` (oversized-image detail lines shown per over-limit md, largest first) | 3 |
| `BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH` (per-batch slot budget used **before** inlining while `source_upload_max_files_per_batch` has not arrived; equals the backend's fixed `SOURCE_UPLOAD_MAX_FILES_PER_BATCH`) | 20 |

### Public report share guardrails

| Bound | Value |
| --- | ---: |
| `REPORT_QUESTION_MAX_CHARS` (research question at **creation**; over-limit is refused with 422, never stored clipped — the composer enforces the same limit by blocking submission and saying so, counted in Unicode code points to match Pydantic rather than clipping the text) | 4,000 |
| `MAX_REFERENCES` (citations projected per report; excess is disclosed as `truncated_references`) | 500 |
| `MAX_REFERENCE_TITLE_CHARS` (per-reference title / original file name) | 400 |
| `MAX_SNIPPET_CHARS` (per-citation excerpt) | 1,200 |

The report body (`content_md`) is served **whole**. So is the research question up to `REPORT_QUESTION_MAX_CHARS` — and since creation refuses anything longer, **every report creatable today carries its whole question**. It used to be clipped at 2,000 chars, silently dropping the tail of the very text that produced the report (the public page serves `reports.question`, the **create-time** value — confirmation writes its edited `resolved_question` into `understanding` and never rewrites that column).

Exceeding the cap is only reachable for a report created **before** that rail existed, whose share link is already out: the projection then bounds the question and sets `question_truncated`, and the public page shows a 「（研究问题过长，已截断）」 hint. That keeps the projection **self-bounded** (otherwise the anonymous response would be unbounded by client input) without either dropping the tail silently or rewriting the user's stored data (no migration, by design). The bound's justification is **not** "the body is bigger anyway" — `content_md` is model-generated and bounded by the generation budget, whereas the question is raw client input.

The composer half of that rail measures in **Unicode code points** (the same unit Pydantic counts; `<textarea maxLength>` counts UTF-16 code units and would stop at roughly half the limit on emoji, so the two sides would not be the same rail), and it **blocks submission rather than touching the input**: pasted text stays intact in the box, the hint states 「超出 N 字上限（当前 M 字）」 and the create button is disabled. Clipping the tail for the user would be the very silent truncation this rule exists to prevent. A reference title/original-filename/excerpt is evidence metadata and stays bounded, but an over-length value **sets `title_truncated`/`file_name_truncated`/`snippet_truncated`** (the public page shows a "已截断" hint) rather than dropping the tail silently. `key` (24), `location` (200), and the timestamps (64) deliberately disclose nothing: they are server-derived labels (`kN`, `PDF p.3`, an ISO instant) with no user-authored tail for a cap to eat.

The last three live in `backend/app/services/report_public_view.py`; the creation bound lives in `backend/app/models/reports.py` (mirrored by `frontend/app/report-api.ts::REPORT_INPUT_LIMITS`). They are independent of the same-named constants in the conversation table below (the two share links each own their contract), but the truncation-disclosure rule must stay identical across both.

### Public conversation share guardrails

| Bound | Value |
| --- | ---: |
| `ASSET_ALIAS_HEX_CHARS` (per-asset alias length: hex chars of the truncated HMAC-SHA256) | 32 |
| `MAX_REFERENCED_ASSETS` (distinct assets the asset endpoint scans per request; must stay ≥ `MAX_TURNS × CITATION_IMAGES_PER_ANSWER` — enforced by `test_endpoint_scan_cap_covers_every_alias_the_projection_can_emit`, since a later-turn image's alias would otherwise never resolve) | 6,000 |
| `MAX_TURNS` (turns rendered on one public page; excess turns are disclosed as `truncated_turns`) | 500 |
| `MAX_REFERENCES` (citations per turn) | 500 |
| `MAX_REFERENCE_TITLE_CHARS` (per-reference title / original file name) | 400 |
| `MAX_SNIPPET_CHARS` (per-citation excerpt) | 1,200 |
| `MAX_CAPTION_CHARS` (per-image caption) | 500 |
| `ASK_QUESTION_MAX_CHARS` (question at **submission**, `backend/app/models/ask.py`; over-limit is refused with 422 and never stored clipped) | 4,000 |
| `CONVERSATION_TITLE_MAX_CHARS` (title at **rename**, `backend/app/models/ask.py`; over-limit is refused with 422 and never stored clipped) | 200 |

The per-turn question **and the conversation title** are served **whole** (no cap): like `answer_md`, they are the user's own artifacts, and truncating them silently would drop the very text that produced the answer or names the conversation. A reference title/excerpt/original-filename is evidence metadata and stays bounded, but an over-length value **sets `title_truncated`/`snippet_truncated`/`file_name_truncated`** (the public page shows a "已截断" hint) rather than dropping the tail silently (codex #522 R3/R4).

"Served whole" is only a **bounded** promise because the write side refuses over-length text, and the question and the title each have their own rail:

- **Question**: `AskRequest.question` carries `max_length=ASK_QUESTION_MAX_CHARS`, so `POST /notebooks/{id}/ask`, `POST /notebooks/{id}/ask/stream` and `POST /notebooks/{id}/ask/intent` all answer 422 above it. **The MCP tool `ask_notebook` enforces it too**, with its own message rather than a raw validation error: a long-lived Agent token could previously submit a question of any length and now receives `question too long: … the maximum is 4,000 …`, a deliberate behaviour change, since an MCP client is a write side like any other and material that long belongs in an uploaded source.
- **Title**: `ConversationRenameRequest.title` carries `max_length=CONVERSATION_TITLE_MAX_CHARS`, so `PATCH /conversations/{id}` answers 422 above it. Renaming is the only way a title grows past the 60 characters the server slices off the first question, so that one endpoint is the whole write side of the field; there is no rename tool on the MCP surface, so nothing to mirror. 200 rather than 4,000: that value bounds question-length prose, while a conversation title is a one-line label.

On both rails the client blocks submission at the same limit (counted in Unicode code points to match Pydantic) and never clips the text for the user (`ASK_INPUT_LIMITS`). Without the write side an anonymous response would be unbounded by client input — the finding codex #525 R1 P2 raised against the report projection.

One knowingly-unbounded leftover remains, recorded rather than papered over: rows written *before* these rails (an older turn may carry a longer question, a title renamed earlier may be longer). Bounding one inside the projection needs a disclosure field on the turn plus a public-page change — what `PublicReport.question_truncated` cost on the report side — so it is tracked as separate work rather than fixed by a silent clip.

The first seven live in `backend/app/services/conversation_public_view.py`.

## APIs

Key local beta APIs:

- `GET /api/notebooks`, `POST /api/notebooks`, `PATCH /api/notebooks/{id}`, `DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `GET /api/notebooks/{id}/analytics/content-overview` — viewer-aware content assets: `memory` (`total`, `confirmed`, `candidate`, up to three recent `id`/`title`/`status`/`updated_at`) and `knowhow` (`table_count`, `row_count`, `projection_pending`, `projection_failed`, `stale_code_count`, up to three recent table summaries)
- `GET /api/notebooks/{id}/checkup` — read-only pipeline health check (dashboard hot path): aggregates source/index damage-and-pending signals — empty source, missing retrieval segments, missing retrieval vectors, sources pending analysis, stale/corrupt retrieval index — each with a count, a bounded sample, and a suggested repair action; all zero when healthy. Consumed by the dashboard's source-status and index blocks plus the bell; a healthy notebook stays neutral and undisturbed. The two missing-retrieval-vector counts are served from a short-lived per-notebook memo and may therefore be **up to 30 seconds stale** — after a backfill finishes, they can take one more poll cycle to drop to zero (the repair button stays busy that much longer, by design).
- `POST /api/notebooks/{id}/sources/reparse` — checkup repair: batch re-parse the given sources (empty/missing-segment damage), scheduled through the existing pipeline in the background, filtered to the notebook scope.
- `POST /api/notebooks/{id}/backfill-vectors` — checkup repair: backfill the notebook's missing retrieval vectors in the background (missing-only, idempotent, embedding-only — never re-parses).
- `POST /api/notebooks/{id}/paper-meta/backfill` — owner-triggered paper-metadata backfill (background, idempotent/resumable), returns `{queued}`; 409 when the LLM is not configured. The source panel's 「补全论文信息」 button is **shown only when there is work to do**: it hides when `NotebookSummary.paper_meta_missing` (filled precisely only by the single-notebook `GET /api/notebooks/{id}`, an EXISTS probe over the same predicate the backfill queue uses; `null` = not computed on list projections and older backends) is `false` **and** the visible source page has no `paper_meta_status="missing"` row. `null`/absent keeps the legacy always-visible behavior — hiding is triggered only by an explicit `false` — and the button stays visible while a backfill is running to host its 「补全中…」 state.
- `GET /api/system/config` — authenticated, non-sensitive browser configuration; currently returns `source_upload_max_bytes`, the parsed deployment cap used by the source picker; `source_upload_max_files_per_batch`, the fixed request-count guard; and, for the markdown-bundle upload pairing pre-flight (see "Citation images (本段附图)" above), `source_image_max_bytes` / `source_image_max_per_source` (mirroring `MINERU_MAX_IMAGE_BYTES` / `MINERU_MAX_IMAGES_PER_SOURCE`; `null` on an older backend that omits them means "skip local pre-flight, the server guardrail decides", while an explicit `0` is a legal value meaning "persist no images at all" and is equivalent to image storage being off) and `source_images_enabled` (mirroring `MINERU_RETURN_IMAGES`; a missing field defaults to `true`, since the switch never previously existed and a stale frontend must not manufacture a warning against a normal deployment)
- `GET /api/system/extensions` — authenticated metadata-only projection for build-time workspace UI contributions. It returns API version plus stable plugin/display/version/contribution identifiers, live availability, and only `disabled | unavailable | null` as the unavailable reason. It never returns capability names, dependency/trust topology, endpoints, paths, credentials, or exception text. Production registers the existing Agent Profile launcher as `builtin.ask_agent_profile.workspace_panel` in `workspace.side_panel`. The browser reads this projection once per authenticated actor generation after a workspace commits; collection and signed-out views do not call it, notebook switches reuse it, and unavailable/missing/older servers fail closed. The contribution itself performs no Agent Profile read until the user opens the existing panel.
- `GET /api/admin/extensions` — system-admin-only, read-only projection of the loaded deployment-plugin topology (six whitelisted fields per extension); see [Deployment extensions](#deployment-extensions).
- `/api/extensions/{plugin_id}/…` — the sole mount point for a deployment plugin's own HTTP routes, behind router-level session auth; see [Deployment extensions](#deployment-extensions).
- `SILICON_NOTEBOOK_UI_PLUGINS` is a build-time-only frontend input (a `:`-separated list of local plugin package directories, unset/empty by default); it is consumed entirely by `frontend/scripts/sync-ui-plugins.mjs` and never reaches the running backend process. That script writes `frontend/.local/ui-extension-contract.json`, a deployment-time reconciliation input — not a runtime dependency of either the frontend or the backend — shaped `{api_version, contributions: [{plugin_id, version, contribution_id, slot, capability}, ...]}`, the same shape and sort key `(plugin_id, version, contribution_id, slot, capability)` as `backend/tests/fixtures/ui_extension_contract.json`: it is that fixture's built-in rows concatenated with each configured package's `ui-plugin.json` rows. On the browser, a plugin's injected `actions.api` port confines every request under `/api/extensions/{plugin_id}/` and strips any `authorization`/`cookie` header the plugin supplies, fixing `tag`/`auth`/`unauthorized` so a plugin cannot override them. A plugin's own backend route must never return 401 for anything other than a genuine session invalidation: the port always sets `unauthorized: "clear-and-reload"`, so a bare upstream 401 (e.g. an expired third-party credential the plugin's own backend depends on) would clear the signed-in user's token and reload the page; the plugin's backend must translate an upstream 401 into `502`/`424` before returning it. A response with no body must be read through `requestVoid`, not `requestJson` — the latter calls `.json()` on the body and throws a parse error on `204`/empty responses. `GET /api/admin/extensions` (admin-only) backs a new read-only admin page at `/admin/extensions` that lists the extension topology the running backend actually loaded — built-in and deployment-registered contributions, retrieval and UI alike — for operational visibility; it is unrelated to `SILICON_NOTEBOOK_UI_PLUGINS`, which only ever affects the frontend build.
- `POST /api/notebooks/{id}/sources` — multipart file upload (async parse/extract). Each file is bounded while the multipart stream is spooled and is rejected with 413 above `SOURCE_UPLOAD_MAX_MB` (default 50 MiB); each request is also rejected above 20 files. The browser reads both guards above, blocks its file inputs until they are known, rejects oversized selections immediately, and rechecks staged files before sending. Each accepted file lands on disk as `{source_id}_{sanitized client name}`; that component is clamped to the filesystem's 255-byte path-component limit (stem clipped on UTF-8 bytes, extension preserved — a browser may legally submit a 255-byte file name, which composed with the 37-byte id prefix would otherwise exceed the cap on ext4/XFS/NTFS and fail the upload). Only the derived disk name is shortened; the stored file name and title keep the client's value whole
- `GET /api/sources/{id}`, `DELETE /api/sources/{id}`, `POST /api/sources/{id}/parse`, `GET /api/sources/{id}/elements`, `GET /api/sources/{id}/elements-page?offset=&limit=&anchor_element_id=` — owner-or-member scope, keyed on the source's own notebook. The paged reader returns `{items,total_count,offset,limit}`, caps `limit` at 100, and moves `offset` to the page containing a valid anchor.
- `GET /api/notebooks/{id}/sources/{source_id}/elements-page?offset=&limit=&anchor_element_id=` — the bounded source-detail reader under active-notebook participant authorization. The browser uses this endpoint; the proxied unpaged element endpoint remains backward compatible.
- `GET /api/notebooks/{id}/sources/{source_id}`, `GET /api/notebooks/{id}/sources/{source_id}/elements` — the same two reads, authorized on the **active** notebook in the path and resolved inside its valid participant set (itself plus the reference libraries it has effectively mounted). Mounting a reference library never grants direct membership in it, so the browser keeps filtering on the active notebook only and the backend proxies the read internally; the participant set is re-evaluated per request, so a demoted/transferred/mid-copy library or an unmounted edge returns 404 immediately. Same-notebook sources take this identical path (the participant set always starts with the active notebook), and the response reports the source's real owning notebook so the client can render it read-only. Writes are deliberately not proxied — re-parse and delete stay direct operations on `/api/sources/{id}` under the `sources:write` capability (owner ∪ group admin since P2). The detail response is a narrower model than `/api/sources/{id}`'s: it drops `file_path` and the raw `error_message` (both can carry server-side absolute paths) and reports a `parse_failed` boolean instead; a cross-library source of a hidden synthetic type (`memory`/`knowhow` projection rows, which the collection map deliberately counts) is refused outright
- `GET /api/notebooks/{id}/assets/{asset_id}` — image assets (knowhow cell images, source figures) under the same participant-set rule: the notebook in the path is the viewer's active notebook, the asset declares its own owning notebook, and any asset outside the active notebook's valid participant set is 404. An asset served from a mounted library is `Cache-Control: no-store` so unmounting takes effect immediately; assets of the active notebook itself keep the long private cache
- Command catalog: `GET .../sources/{sid}/command-catalog/preview` (zero-model cost estimate), `POST .../sources/{sid}/command-catalog` (start; 409 when a job is already active for that source, or the previous run still has unreviewed candidates), `GET .../command-catalog/job`, `POST .../command-catalog/cancel`, `GET .../command-catalog/candidates?job_id=&state=&cursor=&limit=` (keyset page + per-state counts), `POST .../command-catalog/apply` body `{candidate_ids}` or `{all_pending}` (creates or appends to `命令目录：<source>`, never overwrites an existing row), `POST .../command-catalog/dismiss` body `{candidate_ids}` or `{all_pending}` (marks candidates skipped without writing any table — the only way to clear the unreviewed-candidates guard for a candidate that does not conflict — see [Command catalog](#command-catalog-tool-manuals))
- `GET /api/notebooks/{id}/understanding` — notebook understanding ("AI 对这个库的理解"); any reader; returns `enabled`, `base`, `mine`, `job`, `can_edit_base` — see [Notebook understanding blocks](#notebook-understanding-blocks)
- `PUT /api/notebooks/{id}/understanding/{label}` body `{scope: "shared"|"mine", value, expected_revision}` — `shared` needs the `agent_profile:write` capability, `mine` needs only read access plus row-level ownership; 422 over the 400-character limit, 409 on a stale `expected_revision`
- `DELETE /api/notebooks/{id}/understanding/{label}?scope=&expected_revision=` — same capability split as the write endpoint and the same optimistic concurrency: `expected_revision` is the revision the browser displayed (required; stale → 409); clears the value, keeps the row and history
- `POST /api/notebooks/{id}/understanding/rebuild` body `{scope}` — same capability split; 409 while busy or while `AGENT_PROFILE_ENABLED` is off
- `GET /api/notebooks/{id}/knowledge-types`, `GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`, `PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- Knowhow tables: `GET|POST /api/notebooks/{id}/knowhow`, `GET|PATCH|DELETE .../knowhow/{table_id}`, `POST .../knowhow/{table_id}/reproject` — plus import (`POST .../knowhow/import/preview`, `POST .../knowhow/import`), column/row/cell editing (`POST .../knowhow/{table_id}/columns`, `PATCH|DELETE .../columns/{column_id}`, `POST .../knowhow/{table_id}/rows`, `DELETE .../rows/{row_id}`, `PATCH .../rows/{row_id}/cells/{column_id}`), the Excel template round-trip (`GET .../knowhow/{table_id}/template`, `POST .../knowhow/{table_id}/append` with `mode=preview|commit`), an explicit suggestion-only wording rewrite (`POST .../rows/{row_id}/cells/{column_id}/optimize`), and reasoning-backed row completion suggestions (`POST .../knowhow/{table_id}/rows/{row_id}/complete`, optional `target_column_ids`, response `retrieval_mode` + `retrieval_scope` + `retrieval_status` + `reasoning_trace` + `evidence` + `suggestions`)
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask/intent` — corpus-blind `reasoning` intent preview; accepts `{question, conversation_id?}`, reads at most the latest five user questions, creates no conversation/job, returns the editable intent contract plus any blocking ambiguity, and signals the model cancellation event when its client disconnects
- `POST /api/notebooks/{id}/ask` — grounded Q&A with `[k_i]` citations (`mode`: `chunk` default | `graph` | `reasoning`; `reasoning` accepts `retrieval_effort`, default `standard`; the official browser sends timezone-aware `asked_at` as display-only submission metadata; responses expose the authoritative persisted completion instant as `answered_at`; collection-aware responses may include structured `result_sets` plus exact coverage; a reasoning response may additionally carry `gap_suggestions` — non-evidence pointers outside the notebook, see [Gap consultation](#gap-consultation-askgap_consult) — empty and absent from the JSON payload on a deployment with no such plugin; federation follows the mode-specific boundaries above)
- `POST /api/notebooks/{id}/ask/stream` — NDJSON Ask progress stream (same optional timezone-aware `asked_at` request field; first a `started` event with the durable `job_id` and `conversation_id`, then progress/final events). The frontend uses that conversation id to publish/reopen the in-flight session before an answer exists. A transport disconnect stops delivery to that client only; the detached job keeps running and can persist its answer
- `GET /api/notebooks/{id}/ask/jobs/{job_id}` — read detached Ask job `status`, `trace`, and `answer_id`; the job must belong to the path notebook and current user; on `done`, the frontend reloads the conversation to obtain the final `AskResponse`
- `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel` — explicit interrupt endpoint; the job must belong to the path notebook and current user; sets the cancellation event and stops the worker before a cancelled final answer is saved
- `GET /api/notebooks/{id}/conversations`, `GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- Memory: `GET /api/memories`, `GET /api/notebooks/{id}/memories`, `GET|PATCH /api/memories/{memory_id}`, `POST /api/memories/{memory_id}/confirm|reject|deprecate|promote`, `POST /api/answers/{answer_id}/memory-preview`, `POST /api/notebooks/{id}/memories/from-answer`
- Agent access: anonymous machine-readable onboarding at `GET /api/agent-mcp/onboarding`; authenticated `GET|POST /api/agent-profiles`, `PATCH /api/agent-profiles/{profile_id}`, `POST /api/agent-profiles/{profile_id}/tokens`, `GET /api/agent-tokens`, `DELETE /api/agent-tokens/{token_id}`; Streamable HTTP MCP is mounted at `/mcp`
- Knowhow agent surface: `GET /api/agent/knowhow/tables?notebook_id=`, `GET /api/agent/knowhow/tables/{table_id}/discrimination`, `GET /api/agent/knowhow/rows/{row_id}`, `GET|PUT|DELETE /api/agent/knowhow/rows/{row_id}/cells/{column_id}/code` — reachable by either a signed-in session or an Agent Bearer token; reads need `knowledge:read`, code writes need `knowhow:code` (see [Memory and Agent MCP](#memory-and-agent-mcp))
- Unified KG: `POST .../unified-kg/rebuild`, `GET .../unified-kg`, `GET .../unified-kg/pending-merges`, `POST .../unified-kg/merges/{id}/confirm|reject`
- Concept rebuild (UI: **重新合并** in the Knowledge Graph view and the 「索引与构建」 panel): `POST /api/notebooks/{id}/unified-kg/rebuild` starts a **background** pass and returns `{status: "rebuilding", notebook_id, job_id}` — it no longer returns `{clusters: N}`, because the work is proportional to the notebook rather than to the click: the content-version gate still answers an unchanged notebook in milliseconds, but a real recluster streams seed representatives over the whole graph (minutes to hours on a base-tier library, past PostgreSQL's statement timeout, with a request worker pinned for the duration). It has no LLM precondition — but it is not strictly zero-model: when `kg_merge_review` / `kg_concept_description` are configured, the pass invokes them as fail-open enrichment. Single flight is **shared with isolated-node relink**: one per-notebook slot covers both passes, because a rebuild rewrites `concept_clusters` and the community partition wholesale while relink appends edges to the graph clustering reads — overlapping them lets one publish over inputs the other is still consuming. A second click, or a click while the other pass runs, returns 409 naming the action that actually holds the slot. Like relink, the claim is per serving **process** (production pins `--workers 1`); offline CLI runs (`scripts/recluster_kg.py`, `batch_ingest`) are separate processes, call the pass directly and are not covered. `GET /api/notebooks/{id}/unified-kg/rebuild/status` (notebook read) is the completion signal, returning `{job_id, notebook_id, status, running, clusters}` where `status` is `running` / `succeeded` / `failed` / `idle`; `idle` covers "never ran here", "the process restarted" AND "the shared slot is held by a relink pass", so a bounded client poll always terminates and neither poll can be parked on the other's job. The browser refreshes the graph, pending merges and unified status at the current range on any terminal state, and its busy indicator is scoped to the notebook being rebuilt. This is deliberately separate from `GET .../unified-kg/status`, whose `building` flag is about the visualization artefact. Forced full recluster (changed clustering *settings*, which the content-version gate cannot see) remains CLI-only. Pending review has canonical-component-pair semantics: rebuild keeps one deterministic highest-score representative for each displayed pair; stable rejected/deferred seed decisions are projected through confirmed unions as a component-level cannot-link, so another seed in the same group cannot recreate that visible pair. One manual decision locks the complete displayed-pair row set in deterministic order and atomically settles every duplicate row left by older deployments to the latest status; if any sibling was already confirmed, a rejection is treated as a materialized-union reversal. Before publishing a replacement pending generation, rebuild deletes the stale one and reapplies live decisions in the same refresh transaction, so a decision that landed after clustering cannot be republished from the old snapshot. Rejecting a wholly **pending** pair changes neither current clusters nor retrieval products, so it immediately leaves the queue without marking the graph dirty or starting a rebuild; reversing a pair with any already-confirmed row still invalidates and dirties because that union may already be materialized. Only confirming a pending pair starts reclustering immediately. A confirmed decision that races the shared slot (409) is remembered client-side and auto-resubmitted once the occupying task's terminal poll observes it (also retried, without dropping the mark, on a transient non-409 resubmit failure), bounded by the same poll's attempt cap; this is a **best-effort promise scoped to the browser tab while it stays open**. A reload or reopen drops the client-side mark and nothing resubmits the confirmation automatically — deliberately: the mark cannot be reconstructed from the generic dirty flag, because an ordinary relink also dirties the graph, and inferring a pending rebuild from it would auto-launch an unrequested, potentially hours-long recluster. The durable fallback is the existing 「待重建」 dirty label plus a manual **重新合并** click. A server-persisted retry queue that would close this gap is a candidate for a future iteration, not implemented here.
- Isolated-node relink (UI: **补上关联** in the Knowledge Graph view): `POST /api/notebooks/{id}/kg/relink` starts a **background** pass and returns `{status: "relinking", notebook_id, job_id}` — it never returns counts, because the work is proportional to the notebook rather than to the click. Deterministic and zero-model, so it has no LLM precondition. Single flight is per notebook and covers **both** entry points — this endpoint and the deterministic relink tail of a successful KG build, which run the same pass over the same notebook. A second click while one is running returns 409 with a user-facing message; a build tail that cannot claim the slot skips (fail-open, body-free event, `{kind: "kg_relink_skipped", notebook_id, holder}`) rather than starting a duplicate read of every source. When the `holder` is another relink, the skip loses nothing (same pass, same work). When the `holder` is a concurrent **重新合并** rebuild, the skip is a deliberately accepted drop, not deferred work: a rebuild does not append the edges this tail exists to add, so the connections a build completed during a running rebuild's window simply do not happen this round and stay unconnected until a manual **补上关联** click. The claim is per serving **process**: production pins `--workers 1`, so it is the deployment-wide guard there, while under multiple workers the endpoint and the build tail only coordinate within one worker, and `GET .../kg/relink/status` likewise only reports what the worker answering that request knows. Offline CLI runs are separate processes and are not covered. `GET /api/notebooks/{id}/kg/relink/status` (notebook read) is the completion signal, returning `{job_id, notebook_id, status, running, isolated_before, edges_added, isolated_after}` where `status` is `running` / `succeeded` / `failed` / `idle`. Progress lives in the serving process only, so `idle` is the honest answer both before any run and after a restart, and a bounded client poll always terminates; the browser refreshes the graph at the current range on any terminal state, and its busy indicator is scoped to the notebook being relinked, so switching notebooks neither disables the other notebook's button nor refreshes the wrong graph. The pass itself walks one source at a time — driven by a keyset over `sources` plus one query per run that discovers the partitions no source row can name (objects stored with an empty `source_id`, or pointing at a deleted source) — and reads, per source, that source's objects plus every relation adjacent to them (cross-source relations included — a node connected only across sources is not isolated). Edges written are committed per source, so the KG change signal is published whenever any edge has been written, including on a pass that fails mid-notebook. The resulting edge set is the one the previous whole-notebook implementation produced, with one registered exception: the per-source read pins insertion order where the old query inherited the planner's `updated_at` order, so where the per-node edge cap binds an isolated node may be bound to a different, equally valid same-source partner. Edge counts and isolation counts are unaffected.
- `GET .../concepts/{canonical_id}/detail`, `GET .../objects/{object_id}/context`
- KG quality-analysis report reads (notebook-read gated): `GET /api/notebooks/{id}/kg-analysis` — optional `boards`/`top_members`/`edges` limits, returns object composition, per-object-type convergence, the topic-community list, and cross-community edges, each stamped with the `kg_mutation_seq` it was built at; `GET /api/notebooks/{id}/kg-analysis/sources` — optional `limit`/`offset`/`order=sparse|connected`, paginates per-source profiles. Both only read the `kg_community_edges` / `kg_source_profiles` / `kg_analysis_artifacts` precompute product tables written by `unified-kg/rebuild`. The editor-only in-panel generate/update control calls the existing `POST /api/notebooks/{id}/unified-kg/rebuild`; it is not a third analysis endpoint.
- Global graph-type baseline (admin writes): `GET /api/object-schemas`, `POST /api/object-schemas`, `PATCH /api/object-schemas/{type}`, `DELETE /api/object-schemas/{type}`.
- Notebook-effective graph types: `GET /api/notebooks/{id}/object-schemas` requires notebook read access; `POST /api/notebooks/{id}/object-schemas`, `PATCH /api/notebooks/{id}/object-schemas/{type}`, and `DELETE /api/notebooks/{id}/object-schemas/{type}` require notebook owner access. Patching an inherited global type creates a notebook override. Deleting an override restores inheritance; deleting a notebook-only type removes it only when no knowledge object of that type remains. `POST /api/notebooks/{id}/schema-proposals` stores review-only proposed types in that notebook rather than the global baseline; a proposal never suppresses an inherited type until the owner explicitly activates it. Global and notebook writes are serialized across both registries and proposal results are rechecked after the model call. Database merge preflight rejects same-name global definitions whose semantic columns differ rather than silently keeping the destination row. `object_type` and every field key must start with a lowercase ASCII letter, contain only lowercase ASCII letters, digits, and underscores, and contain at most 80 characters. A definition may contain at most 64 unique fields and 64 unique list fields; `primary` must be one of `fields`, and every list field must also be in `fields`. Each human-facing schema text value (`plural`, `label`, `description`, and proposal `rationale`) is limited to 2,000 characters. The browser applies the same creation/editing rails before submission; the API remains authoritative.
- `GET /api/notebooks/{id}/duplicates`, `POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- Two-tier: `POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → returns the updated `NotebookSummary` (400 on bad tier, 404 on missing notebook). Sets the notebook's federation tier (base = publishable as a public knowledge base, personal = default user notes); a `base` notebook only participates in another notebook's retrieval once that notebook explicitly mounts it (`GET`/`PUT /api/notebooks/{id}/bases`, candidates via `GET /api/notebooks/{id}/mountable`).
- Reference-library mounts: `GET /api/notebooks/{id}/bases` → `MountedBase[]` (this notebook's mount edges, including greyed-out inactive ones); `PUT /api/notebooks/{id}/bases` body `{base_notebook_ids}` → full replace, returns the updated `MountedBase[]` (400 if any id is outside the mountable candidate set; owner-only); `GET /api/notebooks/{id}/mountable` → `MountableNotebook[]` (mountable candidates: every public knowledge base, this notebook's own same-owner libraries, and — since group knowledge sharing — every library this notebook's owner can read, subject to the borrowed-mount gate; each candidate carries `origin ∈ {base, mine, shared}` so the picker can group them truthfully).
- Groups and authorization edges: `POST`/`GET /api/groups`, `GET`/`PATCH`/`DELETE /api/groups/{id}`, `POST /api/groups/{id}/transfer`, `PUT`/`DELETE /api/groups/{id}/members/{user_id}`, `DELETE /api/groups/{id}/membership`, `GET`/`POST`/`DELETE /api/groups/{id}/invite-link`, `POST /api/groups/{id}/invite-link/rotate`, `POST /api/group-invites/{token}/join`, `GET /api/users/resolve?username=`, `GET`/`POST /api/notebooks/{id}/grants`, `DELETE /api/notebooks/{id}/grants/{grant_id}`, `GET`/`DELETE /api/groups/{id}/shared-notebooks[/{notebook_id}]`, plus the read-only `GET /api/notebooks/{id}/share`. Roles, unique ownership, invitation-link lifecycle, visibility (404 not 403), the double condition on edge creation, asymmetric revocation and the registered limits are all in Group workspace above.
- Edge trust & curation: `GET /api/notebooks/{id}/edge-review-queue`, `POST /api/notebooks/{id}/relations/{rel_id}/review`
- Governance / promotion: `POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`, `GET /api/promotion-queue`, `POST /api/promotion-queue/{candidate_id}/approve|reject`
- Deep report (two-phase): `POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`; performs corpus-blind understanding and stops at `status=intent_ready` for manual confirmation, unless the request has `auto_generate=true` and the question is clear (no blocking ambiguity), in which case intent is also auto-confirmed — through the same deterministic freeze and no second LLM call — before planning starts. `GET /api/notebooks/{id}/reports/{rid}` exposes durable `understanding` plus status/progress. `POST .../reports/{rid}/intent` body `{resolved_question, answers:[{id,answer}]}` validates every required ambiguity and atomically claims the only transition into corpus-backed planning; it returns `{status:"planning"}`, while a duplicate/stale confirmation returns 409 without launching another job. Planning then stops at `outline_ready`, or proceeds directly to generation when the original request had `auto_generate=true` (the intent stage above already auto-confirmed under the same condition — no blocking ambiguity). The enriched `outline` carries per-section `intent_ids`, `intent_questions`, editable `sub_queries`, objective `coverage`, perspectives / tensions / sufficiency; `content_md` and live `section_status` remain on detail. `PATCH .../reports/{rid}/outline` body `{sections}` edits the draft only while `outline_ready`; it preserves the server intent catalog, accepts at most `REPORT_MAX_SECTIONS`, and accepts at most `REPORT_MAX_SUBQUERIES_PER_SECTION` nonblank retrieval directions per section. The browser mirrors both rails from `/api/system/config` and blocks over-limit submission; direct clients receive 422 instead of having excess sections or directions silently truncated. The endpoint also returns 422 if there is no valid section or a mandatory intent loses its final section binding. `POST .../reports/{rid}/generate` body `{depth?}` atomically launches **phase-2 generation** from either `outline_ready` or a `failed` report that still has an outline; all other states return 409. Retry preserves the confirmed intent/outline and clears prior generated artifacts in the claim transaction. Generated sections include backend-derived `evidence_level`/`grounded`; references can carry exact `source_id`/`element_id` metadata. Also `GET /reports` (list), `POST .../cancel`, `DELETE`, `POST .../reports/export` body `{report_ids}` → `reports.zip`. Batch export first filters completed, non-empty reports by notebook and creator in repository SQL, releases the connection, and invokes the startup-frozen single `report.exporter` Provider once; the default built-in Markdown provider preserves the existing ZIP names and bytes, while core owns the archive and rejects malformed partial output. The browser's single-report Markdown download remains local and adds no request. Section concurrency follows the report-specific database-protection rails above, not `KG_JOB_CONCURRENCY`.

  **"Pending analysis" excludes a source that was analysed and legitimately produced nothing.** The predicate is not "this source has no knowledge objects" — a document with very little text, or one that is entirely uncaptioned images, completes analysis with zero objects, and treating that as pending made the count un-clearable: the badge stayed 待分析 forever, the dashboard kept offering 「继续分析」, and every run re-analysed those sources at full model cost only to get zero again. A source counts as analysed-and-empty when its latest `kg` extraction run is `completed`, its message carries the zero-object marker written by the success path, and it has no failed windows and is not a partial retry. `no-llm` runs (extraction model unconfigured) deliberately do NOT qualify — those really have not been analysed, and must be picked up once a model is configured. The single authoritative statement of the predicate is `kg_analyzed_without_objects` in `backend/app/models/sources.py`; the two pending-count queries mirror it in SQL (they must be one `COUNT`) and are reconciled case-by-case against it by `backend/tests/test_kg_empty_extraction_marker.py`. Incremental analysis skips these sources; a full rebuild still picks them up, which is the path back after switching models or re-parsing with OCR.

The default report-planning rails are four retrieval directions per section,
eight direct-element candidates, and corpus-map scout widths of 12 KG records,
eight PPR chunks, and eight confirmed Memory records. They are independently
deployment-configurable through `REPORT_MAX_SUBQUERIES_PER_SECTION`,
`REPORT_PROBE_ELEMENT_LIMIT`, `REPORT_SCOUT_KG_LIMIT`,
`REPORT_SCOUT_CHUNK_LIMIT`, and `REPORT_SCOUT_MEMORY_LIMIT`; changing a scout
width changes planning context only, never the final section evidence budget.

The current persistence/API contract is the `reports` table and `/reports` APIs; retired content-studio storage and routes are not part of the current runtime.

### Deployment extensions

`GET /api/admin/extensions` is system-admin-only and returns a whitelist projection of the startup-frozen registry topology: exactly six fields per extension — `id`, `version`, `trust`, `display_name`, `contributions[{id,point,kind}]`, `ui_contributions[{id,slot,capability}]` — never a module path, file path, settings value, internal capability reason, or exception text; a plugin entry the deployment marked `enabled=false` never registers and so never appears here. `GET /api/system/extensions` (any authenticated user, live availability only) is unchanged.

`/api/extensions/{plugin_id}/…` is the only mount point for a deployment plugin's own HTTP routes: a router-level session dependency means no anonymous face, and the router factory receives an eight-field `PluginRouteContext` — `plugin_id`, `settings`, `require_notebook_capability`, `require_notebook_read`, `current_actor`, `user_error`, `url_sources`, `emit_event` — never the repository, global `Settings`, a model client, the FastMCP host, or a raw bearer token. **Every core port reached through those seams authorizes the request's own user itself** — e.g. `url_sources.import_urls` checks the `sources:write` capability for the calling user and 404s otherwise — so the mount's own `{notebook_id}` path-shape guard is defence in depth, not the authorization boundary. The URL import port has two call shapes for one implementation: a sync (`def`) handler is already in FastAPI's threadpool and calls `import_urls`, while an `async def` handler is on the event loop thread and must `await import_urls_async(...)`, which offloads the same blocking work (database writes plus one serial remote probe per URL) to the threadpool along with the request context, so authorization is identical on both paths. Calling `import_urls` from an async handler raises `RuntimeError` before doing any work — a developer error surfaced as a `500` with a traceback naming the method to await, never as user-facing copy. A 401 plugin code raises — as either `fastapi.HTTPException` or `starlette.exceptions.HTTPException` (the former is a subclass of the latter; both are caught identically) — *or returns* is translated to 424 (with a logged event) so it cannot be mistaken for a dead session. "Plugin code" covers the handler **and the plugin's own `Depends(...)` callables at any nesting depth**: FastAPI solves dependencies before it calls the endpoint, so an upstream check written as a dependency — the ordinary shape — would otherwise leak a real 401 to the browser and sign the user out. Dependencies get the raised half only, since a dependency's return value is injected as a parameter and never becomes the response. Core's own dependencies are excluded by object identity *and* by defining module, so a genuine 401 from core's router-level session gate still surfaces as 401; generator (`yield`) dependencies and security schemes are left untouched.

Registered limits: the plugin observability-event whitelist accepts exactly four fields (`event`/`outcome`/`count`/`elapsed_ms`); `count`/`elapsed_ms` must be integers in `0..1e9`; a stable code (event name, outcome, or discovery/mount rejection reason) is at most 64 characters; each plugin may declare at most one HTTP route contribution. New-plugin onboarding SOP — writing the backend bundle and the build-time UI package, local integration, packaging, install, startup validation, upgrade/rollback, and the full rejection-code table: [Deployment extensions SOP](./deployment-extensions-sop.md).

### Gap consultation (`ask.gap_consult`)

A new production extension point lets a deployment plugin offer pointers to material **outside** the notebook when a reasoning Ask run's own retrieval came up short. What it returns is never evidence: it is not retrieved, not scored, not bound to a `[k]` anchor, and cannot have moved a single word of the answer — a reader who wants it has to import the URL first, which is an ordinary source add with its own parsing and its own permissions.

**Trigger.** Reasoning Ask only — `ask_graph`, deep reports, and Knowhow's row-completion retrieval never reach this call site, because none of them route through `_run_reasoning_stage` (reports and Knowhow both construct their own `ReasoningRetriever` and call `run()` directly) — at every retrieval effort tier, exactly once per run: **after the response-draft stage has returned** and before persistence, reading its trigger off the pre-draft retrieval facts (selected-source-graph activation and the retriever's own fail-open degradation both included). Running after drafting is what makes the isolation structural rather than behavioural — an injected `ResponseDraftStage` sees no gap-derived trace step, count, or suggestion in its envelope, so the prose cannot vary on any of it. One of two conditions, both read off what the run already produced — no extra query, no extra model call: the run's terminal disclosure step (the same `intent_coverage_incomplete` skip step the trace already shows the reader) still lists a confirmed direction it never executed, **or** the evidence pool (`top_hits + chunks + elements`) is thinner than the current effort tier's own `ranked_final_floor`. Neither condition holding means zero calls. A degraded/fail-open retrieval also reaches the thin-evidence branch — the trace step's wording deliberately does not attribute a cause, since the identical summary fires whether the notebook is genuinely thin or retrieval itself degraded.

**What leaves the deployment.** One bounded object, `GapConsultQuery` — nothing else is ever handed to a plugin. `question` is wording the user has actually seen: the reviewed final question only when the run really went through the clarification gate (`needs_clarification` on the confirmed contract — the user answers the ambiguities there and can edit the final wording); a clear-intent run auto-confirms without pausing, so its `resolved_question` is an unreviewed model rewrite (it may fold in earlier turns' phrasing) and is never sent — the raw question as typed goes instead, as it also does when no intent was confirmed. Truncated to `GAP_CONSULT_QUESTION_MAX_CHARS`; `gaps` is at most `GAP_CONSULT_MAX_GAP_PHRASES` short labels for confirmed-but-unexecuted directions, each truncated to `GAP_CONSULT_PHRASE_MAX_CHARS`. `[k]`/【k】 citation markers are stripped from both before egress. The intent contract's composite `research_question` (objective plus mandatory topics plus constraints plus assumptions) never leaves — only the user's own reviewed words do. `GapConsultCallContext` carries no notebook id, actor id, source id, evidence, or retrieval scope: a plugin at this point is handed identity it was never given, by field-set construction rather than a filter someone has to remember to apply.

**Budget and fail-open.** The frozen `GapConsultHost` runs a contribution's availability probe and its `consult` call together on one private `daemon=True` thread — no thread pool, so one hung plugin leaks one thread on the affected request rather than occupying a shared worker the whole deployment depends on; no `contextvars.copy_context()`, so a plugin cannot inherit this request's frozen retrieval scope, retrieval run, or a leaf-I/O fan-out slot simply by running underneath it — joined in 50ms slices against a hard wall-clock deadline (`ASK_GAP_CONSULT_TIMEOUT_SECONDS`, default 4.0s, `0 < x ≤ 30`) that covers the **whole** call, both halves. Cancellation and that deadline are re-read on **every** pass of the join loop, the pass that observes the worker finish included: a contributor that answers *after* its budget is spent has answered late, and a late answer is abandoned exactly like a hung one — reading the deadline only while the thread was still alive would make the outcome depend on which side of the thread's death the 50ms slice happened to land. This budget is spent **before** the reader has an answer — unlike the cooperative post-completion deadlines above, every second here is a second of answer latency — hence the small default and the tight ceiling. **A genuinely hung plugin leaks one daemon thread per affected request; this is a registered, accepted cost, not a defect — a circuit breaker is deferred work.** Anything short of a clean, on-time, well-shaped response is fail-open: no registered host, a dormant point (`has_contributions()` is `False`), a malformed call context, an empty caller-supplied question (the host does not trust its own caller either — this is unreachable in practice today, since `AskRequest.question` already enforces `min_length=1` before a run's own question can reach this call site), a raising contributor, an exhausted budget, or a host answering a shape the port never promised — all return `()` and leave the answer exactly as it would otherwise have been. Cancellation is the sole exception and keeps propagating (`AskCancelled`). "Fail-open" means the answer is unchanged and no error banner or diagnostic is surfaced — it does **not** mean the `gap_consult` disclosure trace step disappears: that step's whole job is telling the reader a bounded egress attempt happened, so a timeout, a raising contributor, or a malformed result still records the attempt with a suggestion count of `0`, never silence about the attempt itself (a codex #584 R4 suggestion to suppress the step on failure was deliberately rejected; see `ask_service.py::_consult_gap_sources`). Sanitization — non-empty title; `url` restricted to `http`/`https` with a non-empty host and no control characters, and never truncated (a shortened URL is a silently wrong destination, so an over-long one is dropped instead); summary/source-label stripped and truncated — happens in the host, after the plugin returns, never inside the plugin. A result whose `status` is `UNAVAILABLE` contributes **nothing**, items or not: that status is the contributor's own statement that it could not serve this call, so its payload does not get to contradict it (and its URLs never enter the cross-contributor de-duplication set, so a contributor that really can serve the call is not blocked by a withdrawn link); `PARTIAL` means "some of it", not "none of it", and is still admitted. The *work* of admission is itself bounded in both dimensions — a bounded multiple of the slots still to fill is the most items ever examined, and every string is cut to its own limit plus headroom before anything walks it — so an unbounded returned payload costs a bounded scan rather than an unbounded one on the critical path, after the deadline above has already been honoured. The cut lands on plugin output, never on user data.

**Availability.** A plugin declares the capability it probes through its own manifest `provides`, exactly like every other contributor kind; core adds no dedicated switch beyond the shared deployment-plugin machinery. A deployment with no `ask.gap_consult` plugin registered pays zero clock reads, zero probe calls, and emits zero events — `consult()` is a strict no-op before any validation runs.

**Import.** The suggestion panel's 「导入」 button routes through the same core URL-import endpoint every other link import uses (`POST /notebooks/{id}/sources/url`, `sources:write` capability — a read-only member never sees the button). The endpoint only accepts a direct PDF link (`remote_sources.probe_pdf`, which follows redirects but requires `application/pdf`/`%PDF-` at the final URL): a plugin must point at the file itself, not a landing or abstract page. A rejection reason is shown verbatim in the panel, mirroring the existing link-import UX; the plugin itself never learns whether the import succeeded. A writable notebook that has already reached its document-count limit disables every suggestion's import button up front and shows why (reusing the same `document_limit` capacity check the "add source" dialog already gates on), rather than spending a live PDF probe on an import that is guaranteed to be rejected.

**Non-evidence, end to end.** `AskGapSuggestion` (`title`/`url`/`summary`/`source_label` — no `source_id`, `element_id`, relevance score, or citation key) lives on `AskResponse.gap_suggestions`, `exclude_if` empty, so a deployment with no plugin serializes byte-identically to every historical payload. It is filled by core **after** the response-draft stage returns — never through `ResponseDraftInput` — so the answering model never sees it, it enters neither `anchors` nor `citations`, and it survives persistence/reopen like any other response field. `conversation_public_view`'s whitelist projection does not carry it, so a publicly shared conversation never discloses it. A `gap_consult` trace step (front-end label 「外扩」) records the trigger reason (`uncovered_directions`/`thin_evidence`), how many gap phrases were sent, and how many suggestions came back — never the question text or a suggestion's title/URL.

| Bound | Value |
| --- | ---: |
| `GAP_CONSULT_MAX_GAP_PHRASES` | 2 |
| `GAP_CONSULT_MAX_SUGGESTIONS` | 5 |
| `GAP_CONSULT_QUESTION_MAX_CHARS` | 300 |
| `GAP_CONSULT_PHRASE_MAX_CHARS` | 60 |
| `GAP_SUGGESTION_TITLE_MAX_CHARS` | 200 |
| `GAP_SUGGESTION_SUMMARY_MAX_CHARS` | 400 |
| `GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS` | 40 |
| `GAP_SUGGESTION_URL_MAX_CHARS` | 2,048 |
| `ASK_GAP_CONSULT_TIMEOUT_SECONDS` (default; deployment-configurable, `0 < x ≤ 30`) | 4.0 |

## Admin observability: user activity (`/dev/logs`)

`/dev/logs` shares one top scope bar (viewed user, date range) across two view tabs: **Activity** (default) and **Model calls** (the original per-day LLM-call viewer, unchanged byte-for-byte — `kind`/`status`/`model` facets, full-text search, per-day dropdown, and auto-refresh all behave exactly as before; its API remains `/api/debug/logs/...`, still gated by `DEBUG_LOGS_ENABLED`).

The Activity tab is three columns:

- **Scope** — the viewed user's own notebooks (name plus source/ask/report counts), each expandable to that notebook's source list (display name plus a parse-status badge). Selecting a notebook filters the middle stream to it; selecting a source only opens it in the right Detail column (it does **not** filter the stream — see "not yet built" below).
- **Activity stream** — asks, sources, and reports merged into one newest-first feed, `(created_at DESC, id DESC)` keyset-paginated ("加载更多"/Load more), 50 items per page by default (`limit`, capped at 200). A native date picker plus "全部时间"/all-time drives a half-open `[since, until)` window. Its bounds carry the **browser's UTC offset** (`2026-08-04T00:00:00+08:00`), so "a day" means the viewer's local calendar day — the same timezone every rendered timestamp already uses; each bound computes its own offset, so a DST-transition day is not off by an hour. A malformed bound fails the request with `400` plus a user-facing message (`X-User-Message`) rather than silently widening the window or 500-ing. All three item types are normalized through the same `parse_activity_instant` helper (`app/core/activity_time.py`) on both database backends: SQLite compares on an absolute instant via `julianday()` (mixed naive/offset-aware `created_at` text — historical rows are naive, newer rows carry an offset — is unified by reading a naive value as UTC), while PostgreSQL compares natively on `timestamptz` with the same `COALESCE` fallback SQLite applies (`ask_jobs.created_at` is nullable there, and PostgreSQL's default `DESC` ordering is `NULLS FIRST`, so an unset value would otherwise pin a broken row to page 1 and break the Python merge). Both backends fold an unparseable or null value onto one shared sentinel instant that sorts last **and** round-trips through the cursor, so `next_cursor` never emits a value the next page cannot parse. The three sources are queried independently with their own keyset pages and merged in Python rather than a three-way `UNION ALL`, so a large deployment gets an honest, bounded "this page covers up to HH:MM" rather than a full sort blow-up.
- **Detail** — an ask shows the full question, answer, citations, and reasoning trace (the existing read-only Ask-panel renderers); a source shows its display name, parse status, and derived diagnostics; a model-call row (Model calls tab only) reuses the existing prompt/response transcript viewer, unchanged.

Three new endpoints (self-or-admin reads; permission mirrors `debug_logs._resolve_owner`'s owner-or-admin check — the viewed `user_id` may be the caller's own id, or the caller must have `role="admin"`, otherwise 403). They're also gated by a dedicated deploy-time switch, `USER_ACTIVITY_VIEW_ENABLED` (default **true**): Activity is the default `/dev/logs` tab, so gating it behind the default-off `DEBUG_LOGS_ENABLED` would 404 every ordinary deployment's default tab; the two switches are independent — `DEBUG_LOGS_ENABLED` continues to gate only `/api/debug/logs/...` behind the Model calls tab. Because the frontend has no other way to learn this deploy-time value, `GET /system/config`'s `SystemConfiguration` response carries it as `user_activity_view_enabled`; `/dev/logs` reads it there and, when `false`, hides the Activity tab and defaults `view` to `llm` instead (a `?view=activity` deep link is normalized away rather than opening a tab whose three endpoints all 404). A missing field (older backend, newer frontend) is treated as `true`, matching the backend's own default rather than hiding an available tab. `GET /admin/users/{user_id}/notebooks` (reused by the Scope column) is likewise self-or-admin rather than admin-only, so a user viewing their own activity can read their own notebook list:

- `GET /admin/users/{user_id}/activity?notebook_id=&since=&until=&before_ts=&before_id=&limit=` — the merged ask/source/report feed above.
- `GET /admin/users/{user_id}/notebooks/{notebook_id}/sources?offset=&limit=` — that notebook's source list (default `limit` 50, capped 200; the Scope panel only fetches the first page, with no further pagination); 404 if the notebook is not owned by `user_id`.
- `GET /admin/users/{user_id}/asks/{job_id}` — one ask's full detail (question/answer/trace); 404 if the job is not owned by `user_id`.

All three types are attributed **owner-only**: they break down only notebooks the viewed user created (`created_by`, excluding notebooks mid-copy), matching the existing convention that the admin usage overview's expanded per-notebook inventory stays owner-only even though its aggregate totals include submissions in joined shared notebooks. Hidden synthetic sources (`memory`/`knowhow`) are excluded from the source list, the activity feed, **and** the Scope column's per-notebook source count — that count now applies the same single-source-of-truth visibility predicate the list itself uses, so the header and the expanded list can no longer disagree. (Because `GET /admin/users/{user_id}/notebooks` backs both screens, the admin usage overview's per-notebook source count drops to the same visible-source figure for any user who has saved a Memory record or built a Knowhow table; it was over-counting before.) The same notebook's report count is likewise scoped to `created_by = user_id`, matching `questions`'s existing predicate and the Activity stream's own report entries: a report another writable member created in a shared notebook is no longer counted toward the notebook owner's header (it was over-counting before, and the count would silently disagree with the expanded stream, which never showed that entry). A source activity entry never carries the raw `error_message` (it can hold a server-side absolute path); it exposes a derived `parse_failed` boolean plus `extraction_warning`/`parse_quality_warning`/`paper_meta_status`, mirroring `ScopedSourceDetail`'s existing redaction. Its display name is `SourceSummary`'s new, additive `display_title` field (paper title preferred, the same single implementation citation cards already use — `source_display.py::source_display_title`); the existing `title` field is untouched, so the Sources sidebar has zero regression. `SourceSummary` also gained an additive raw `created_at` timestamp: both entry points into the Detail column (Scope list and Activity stream) render it in the **browser's** timezone, where the pre-existing `created_label` is a server-side pre-formatted calendar day and would otherwise show the same source under two different dates depending on which column was clicked. `created_label` remains for the existing Sources sidebar. A report entry's duration reuses the Deep Report rule verbatim: `generation_started_at → updated_at`, no duration shown when the start stamp is missing, unfinished reports show only their creation time.

Explicitly not yet built (a phased sequencing choice, not a silent gap): correlating a model call to the ask/report that triggered it — this needs a new write-side log-context propagation and cannot be backfilled onto history recorded before it ships; surfacing per-stage retrieval/parsing timings inside ask/source detail; clicking a source in the Scope column narrowing the Activity stream (today it only opens Detail); pagination of a notebook's source list past its first page.

## Current Limitations

- Retrieval on SQLite uses keyword/FTS-compatible CJK handling plus a bounded float32 matrix/scale index. PostgreSQL uses `pg_trgm`/`ILIKE` and the same byte-oriented float32 vectors behind repository ports; pgvector remains a future scale option, not a runtime prerequisite.
- Large-document ingestion is hardened: greedy-window KG extraction (cost scales linearly with document size), concurrent embedding with per-batch DB writes, and extraction-first pipeline. For very large corpora, adding `sqlite-vec` is a natural next step.
- Ask no longer performs synchronous embedding backfill or a full source-element scan; it uses available keyword/vector indexes and stays responsive while maintenance jobs run. Ask emits per-stage timing (`ask_stage` events).
- Unified KG rebuild is explicit and observable via `GET /notebooks/{id}/unified-kg/status`; ingesting a source marks the graph dirty instead of rebuilding synchronously, and opening the graph overlay no longer auto-rebuilds (refresh on demand).
- Cross-document concept merge uses deterministic alias normalization plus bounded top-k vector candidates (scales past thousands of concepts); optional LLM pre-review (`POST /notebooks/{id}/unified-kg/merges/review`) confirms/rejects high-confidence near-synonym merges in small batches.
- LLM-backed KG extraction requires the `kg_extract` workload to be bound in the system model-service TOML; offline smoke tests seed KG objects explicitly when retrieval/governance assertions are needed.
- Two-tier and deep reasoning are early: the graph-reasoning Ask mode (`mode="graph"`) is opt-in/experimental (the Ask panel toggle still drives the default `chunk`/`reasoning` paths). Marking a notebook `base`/`personal` (via `POST /notebooks/{id}/tier`), the edge-trust review queue, and promotion (personal→base) all now have dedicated front-end controls in the analysis toolbar; publishing a notebook as a public knowledge base only makes it mountable — tier-aware federation and the base-wins conflict rule activate only for notebooks that explicitly mount it as a reference library.
- Notebook sharing is link-based copy, read-only membership, or group sharing — not live collaborative editing. Group members may ask questions and write their own deep reports; content management (sources, graph, authorization edges) stays with the owner, and the group-admin write tier is P2.
- SQLite/PostgreSQL selection is direct and atomic through the repository factory. Changing `DATABASE_URL` does not synchronize existing rows; cutover and rollback therefore require stopped writers, verified backups, an external data migration when data already exists, and post-start consistency checks.
- The `off`-mode PDF fallback uses PyMuPDF4LLM page-chunked Markdown to retain headings, multi-column reading order and reconstructed tables; pypdf is only the last resort if that parser is unavailable or errors. MinerU still provides the authoritative high-fidelity formula/image/complex-scan path. A cloud URL/file parse that fails after retries uses the same local fallback and returns an extracted source with `parse_quality_warning=true`; the source detail explains the risk and offers reparse/delete actions. A later successful MinerU reparse clears the warning. See [PDF parsing with MinerU](./operations.md#pdf-parsing-with-mineru).
- User memory remains manual opt-in only; no automatic memory behavior has been added.
