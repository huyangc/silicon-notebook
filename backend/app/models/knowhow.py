from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# --- knowhow-tables PR-1 Task 6 + PR-2+3 Task 3: import/table + editing API
# response models -------------------------------------------------------------
# Field names are snake_case verbatim (mirrors the store's own dict shapes in
# app/repositories/sqlite/knowhow_store.py) with ONE deliberate exception:
# a column's ``role`` DB/store field is exposed on the wire as ``kind`` (PR-2+3
# Task 3 renames the wire field to match the behavior-kind vocabulary; the
# already-delivered frontend model layer, frontend/app/knowhow-model.ts,
# already reads ``kind`` preferentially over the legacy ``role`` name). The
# reshaping (store dict role -> wire dict kind, table-level anchor_column_id
# derivation) lives in services/knowhow/api.py's to_wire_table/to_wire_column
# — these are pure wire shapes with no business logic of their own.


class KnowhowColumn(BaseModel):
    id: str
    name: str
    kind: str
    position: int


class KnowhowRow(BaseModel):
    id: str
    position: int
    projection_status: str
    created_at: str = ""
    updated_at: str = ""
    cells: Dict[str, str] = Field(default_factory=dict)


class KnowhowTableSummary(BaseModel):
    id: str
    notebook_id: str
    title: str
    description: str = ""
    row_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    projection_pending: int = 0
    projection_failed: int = 0
    stale_code_count: int = 0
    last_activity_at: str = ""


class KnowhowTableDetail(BaseModel):
    id: str
    notebook_id: str
    title: str
    description: str = ""
    mutation_seq: int = 0
    hidden_source_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    columns: List[KnowhowColumn] = Field(default_factory=list)
    rows: List[KnowhowRow] = Field(default_factory=list)
    # Table-level row-title-column designation (design doc §①: at most one,
    # optional). None = no anchor column (a "record-shaped" table that only
    # participates in retrieval, never the KG) — a real, load-bearing state,
    # not merely "field absent", so it is always present on the wire.
    anchor_column_id: Optional[str] = None


class KnowhowPreviewColumn(BaseModel):
    name: str
    guessed_kind: str


class KnowhowImportPreview(BaseModel):
    columns: List[KnowhowPreviewColumn]
    rows_preview: List[List[str]]
    total_rows: int
    anchor_suggestion: Optional[int] = None


# --- PR-2+3 Task 3: editing API request/response models -----------------------
# Every PATCH body field is Optional with a None default so FastAPI/pydantic
# populates ``model_fields_set`` with exactly the keys the client actually
# sent — the routes read that set (not the values) to distinguish "field
# omitted" from "field explicitly set to null", which matters for
# anchor_column_id's clear-vs-leave-alone semantics (see routes.py).


class KnowhowTablePatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    anchor_column_id: Optional[str] = None


class KnowhowColumnCreate(BaseModel):
    name: str
    kind: str
    position: Optional[int] = None


class KnowhowColumnPatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None


class KnowhowRowCreate(BaseModel):
    cells: Dict[str, str] = Field(default_factory=dict)
    position: Optional[int] = None


class KnowhowCellPatch(BaseModel):
    content_md: str
    # Concurrency P1 (fix b): when set, the write goes through a SERVER-SIDE
    # compare-and-write in one transaction (see routes.patch_knowhow_cell →
    # update_knowhow_cells_guarded_atomic) — the stored content_md must still
    # equal this baseline or the write is refused with 409, nothing written.
    # The batch-reformat save path sends it; the MANUAL cell editor omits it
    # (None) to keep its existing last-write-wins semantics.
    expected_before: Optional[str] = None
    # knowhow 表版本管理 Task 12 (design doc §8.1 tail): who/what produced this
    # write, recorded verbatim onto the knowhow_changes row the store already
    # creates (Task 9) so the history timeline can show an origin badge
    # ("LLM 优化" vs "手动改的" vs "从历史恢复"). Defaults to "user" (the manual
    # editor never sends this field, preserving today's behavior byte-for-byte).
    # MUST be validated against VALID_ORIGINS at the route — a lenient default
    # here would repeat the anchor-feature's `kind = column.get("kind") or
    # "attribute"` mistake (PR#281→#286: a wire typo silently downgraded to a
    # default instead of a 400, so the whole feature shipped inert).
    origin: str = "user"


class KnowhowCellPatchResult(BaseModel):
    row_id: str
    column_id: str
    content_md: str
    projection_status: str


# --- followup A: batch cell write, one DB transaction (anchor-grouping spec
# §6「整组批量写单事务，不半改」) — replaces the frontend's best-effort
# Promise.all of N independent single-cell PATCHes for a merged/shared cell
# with ONE request the backend commits atomically. Response reuses
# KnowhowCellPatchResult, one per written row (same shape the single-cell
# PATCH already returns, just as a list).
class KnowhowCellsBatchPatch(BaseModel):
    column_id: str
    row_ids: List[str]
    content_md: str
    # Concurrency P1 (fix b): OPTIONAL per-row baseline, POSITIONALLY PARALLEL to
    # row_ids (expected_before[i] is row_ids[i]'s snapshot). When set, the whole
    # fan-out is written through update_knowhow_cells_guarded_atomic as ONE
    # all-or-nothing compare-and-write: if ANY target's stored content no longer
    # equals its baseline the entire group is refused (409, nothing written) —
    # never a half-written concept group. Omitted (None) → legacy
    # update_knowhow_cells last-write-wins (the manual editor's shared-cell save).
    expected_before: Optional[List[str]] = None
    # Concurrency P1 (round-4): OPTIONAL anchor baseline guard, POSITIONALLY
    # PARALLEL to row_ids (expected_anchor[i] is row_ids[i]'s snapshot anchor
    # value). Provided together with expected_before by the batch-reformat fan-out
    # so update_knowhow_cells_guarded_atomic ALSO re-reads each target row's
    # anchor cell in-transaction: a sibling row whose anchor moved OUT of the
    # shared group after the modal froze its row ids (edited cell unchanged, so
    # expected_before still matches) refuses the whole group (409). Omitted (None)
    # → the anchor is never re-read (byte-identical to expected_before-only).
    anchor_column_id: Optional[str] = None
    expected_anchor: Optional[List[str]] = None
    # knowhow 表版本管理 Task 12: same field/same validation/same default as
    # KnowhowCellPatch.origin above — this is the SECOND of the design doc's
    # "两条既有回填路径" (§8.1 tail): a merged/shared cell's batch-reformat-
    # accept fan-out writes through THIS endpoint, not the single-cell one, so
    # it needs its own origin to ever be attributable as "llm_reformat" rather
    # than silently defaulting to "user" forever.
    origin: str = "user"


# --- PR-2+3 Task 3: create-empty-table wizard backend --------------------------
# Mirrors the import endpoint's column/anchor wire shape exactly
# (columns:[{name,kind}] + a separate anchor_index) but as a JSON body
# instead of import's multipart form, and with no grid/rows to parse.


class KnowhowNewColumnInput(BaseModel):
    name: str
    # Optional (unlike the single-column editing endpoint's KnowhowColumnCreate.
    # kind, which IS required): this feeds services.knowhow.api's
    # _columns_with_anchor merge, shared with the import wire, which defaults
    # an omitted kind to 'attribute' rather than erroring — the same
    # leniency PR-1's import wire always had for an unspecified role.
    kind: Optional[str] = None


class KnowhowTableCreate(BaseModel):
    title: str
    columns: List[KnowhowNewColumnInput] = Field(default_factory=list)
    anchor_index: Optional[int] = None


# --- PR-2+3 Task 6: Excel template round-trip (append preview/commit) --------
# GET .../template streams raw xlsx bytes (StreamingResponse — see routes.py),
# so it has no response model of its own. POST .../append's response shape
# depends on the ``mode`` form field: preview returns KnowhowAppendPreview,
# commit returns KnowhowAppendResult — the route's response_model is their
# Union; the two models share no field names, so a given response is never
# ambiguous about which one it actually is.


class KnowhowAppendDuplicateTitle(BaseModel):
    row_index: int
    title: str


class KnowhowAppendPreview(BaseModel):
    rows_preview: List[List[str]]
    total_rows: int
    unmatched_columns: List[str] = Field(default_factory=list)
    duplicate_titles: List[KnowhowAppendDuplicateTitle] = Field(default_factory=list)


class KnowhowAppendResult(BaseModel):
    added: int


# --- PR-2+3 Task 8: LLM cell rewrite (explicit trigger, suggestion-only) -----
# POST .../optimize returns only the suggestion — never writes the cell
# itself (design doc §③: 用户逐格确认 → 回填走既有 PATCH cell 端点).


class KnowhowCellOptimizeResult(BaseModel):
    suggestion_md: str


# --- knowhow row completion: suggestion-only inference from sibling rows ---


class KnowhowRowCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Omitted means every currently blank, non-anchor column. Validation and
    # de-duplication are intentionally performed by the service against the
    # live table schema, where anchor/non-empty/unknown targets can be rejected
    # together with a user-readable 400 instead of becoming a generic 422.
    target_column_ids: Optional[List[str]] = None


class KnowhowRowCompletionSuggestion(BaseModel):
    column_id: str
    suggestion_md: Optional[str]
    confidence: Literal["high", "medium", "low"]
    based_on_row_ids: List[str] = Field(default_factory=list)
    basis: str
    abstain_reason: str


class KnowhowRowCompleteResult(BaseModel):
    suggestions: List[KnowhowRowCompletionSuggestion] = Field(default_factory=list)


# --- knowhow-md-normalize Task 4: /reformat HTTP endpoint result ------------
# Same suggestion-only contract as KnowhowCellOptimizeResult above, but
# reformat_cell (Task 3) is internally graceful about an unconfigured LLM
# (never raises ModelNotConfiguredError — falls back to rule_normalize
# instead), so the wire shape carries `source`/`changed` rather than an
# unconditional single suggestion string: the caller reads `source` to decide
# how to present the candidate (llm / rule/llm-failed / rule/no-llm) and
# `changed` to skip a no-op diff.


class KnowhowCellReformatResult(BaseModel):
    candidate_md: str
    source: str
    changed: bool
    # Concurrency P1 (fix a): the EXACT saved content_md the server read and fed
    # to reformat_cell. /reformat reads the LIVE cell, so if another tab edited
    # it after the batch modal snapshotted originalMd, source_md != that snapshot
    # → the candidate derives from content the client never saw. The batch client
    # compares source_md to its originalMd snapshot: mismatch routes that cell
    # straight to stale-skipped and refuses to cache the candidate for duplicates.
    source_md: str


# --- PR-2+3 Task 10: Agent surface (HTTP+MCP shared core) --------------------
# The agent-facing read/write surface — table list, discrimination set, row
# detail, and cell-level code attachments — served under /agent/knowhow/...
# by knowhow_agent_routes.py, reachable by EITHER a signed-in session or an
# Agent Bearer token carrying the knowledge:read (reads) / knowhow:code (code
# writes) scope (see app.api.deps.require_user_or_agent/user_or_agent_scope).
# MCP tools in app.api.mcp_server share the exact same
# app.services.knowhow.api functions and wrap their dict output in
# _budget_response instead of validating it against these response models.


class KnowhowAgentColumn(BaseModel):
    id: str
    name: str
    kind: str


class KnowhowAgentTable(BaseModel):
    id: str
    title: str
    description: str = ""
    row_count: int = 0
    anchor_column_id: Optional[str] = None
    columns: List[KnowhowAgentColumn] = Field(default_factory=list)


class KnowhowDiscriminationMethod(BaseModel):
    column_id: str
    column_name: str
    text: str
    # Design doc §⑥-4's three-state freshness derivation, surfaced per method
    # so a batch code-generation agent can skip already-implemented/non-stale
    # cells without a separate get_knowhow_row round trip per row. Not part
    # of the task brief's terse endpoint-shape listing, but explicitly called
    # for by the design doc's own "判别集只带 code_status 三态不带代码（控体
    # 积）" — included here (never the code body itself, keeping the
    # discrimination payload lean).
    code_status: str


class KnowhowDiscriminationRow(BaseModel):
    row_id: str
    title: str
    methods: List[KnowhowDiscriminationMethod] = Field(default_factory=list)


class KnowhowDiscriminationSet(BaseModel):
    rows: List[KnowhowDiscriminationRow] = Field(default_factory=list)


class KnowhowRowCell(BaseModel):
    column_id: str
    column_name: str
    kind: str
    text: str
    steps: Optional[List[str]] = None
    items: Optional[List[str]] = None


class KnowhowRowCode(BaseModel):
    column_id: str
    language: str
    code_text: str
    status: str
    updated_at: str
    updated_by: str


class KnowhowRowDetail(BaseModel):
    title: str
    cells: List[KnowhowRowCell] = Field(default_factory=list)
    code: List[KnowhowRowCode] = Field(default_factory=list)


class KnowhowCellCodePut(BaseModel):
    code_text: str
    language: str = ""


class KnowhowCellCodeResult(BaseModel):
    code_text: Optional[str] = None
    language: Optional[str] = None
    status: str
    updated_at: Optional[str] = None


class KnowhowTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_notebook_id: str
    mode: Literal["copy", "move"]


# --- knowhow 表版本管理 Task 12: HTTP 端点的请求模型 -------------------------
# design doc `2026-07-22-knowhow-table-version-control-design.md` §8.1/§4.1.
# The four read endpoints (timeline / single change / diff / cell history)
# return the store's own dict shapes verbatim — no dedicated response models,
# same convention already used by this file's other thin pass-through routes
# (e.g. reproject_knowhow_table's `-> dict`). Only the three WRITE bodies below
# are new wire shapes, plus the `origin` field added to KnowhowCellPatch/
# KnowhowCellsBatchPatch above.
#
# VALID_ORIGINS is the §4.1 whitelist. It is validated at the route with a
# hard 400 for anything outside it — NEVER a lenient fallback. This codebase
# has a concrete cautionary tale for the alternative: the anchor feature's
# `kind = column.get("kind") or "attribute"` turned a client wire typo into a
# silent default instead of a 400, and the whole feature shipped inert
# (PR#281→#286). origin exists specifically so the history timeline can show
# an honest "who/what changed this" badge; swallowing a bad value would make
# that badge quietly lie instead of failing loudly.
VALID_ORIGINS = frozenset({
    "user", "llm_optimize", "llm_reformat", "llm_complete", "import", "agent",
    "revert", "backfill",
})


class KnowhowRevertRequest(BaseModel):
    target_seq: int
    expected_head_seq: int


class KnowhowMilestoneCreate(BaseModel):
    seq: int
    name: str
    note: str = ""


class KnowhowHistoryPruneRequest(BaseModel):
    # 纵深防御（codex P2）：must be a positive, bounded day count. 负数会算出
    # 一个未来的截止时间 → cutoff_seq 落到 head → 除 head 外整段历史被静默删光；
    # 0 天会把刚刚发生的编辑也当作"N 天前"清掉。上限 36500（100 年）与前端
    # parseHistoryPruneDays 对齐，同时挡住超大值。前端本就拦了这些，后端作为
    # 契约边界必须自己再挡一次（非法值 → 422）。
    before_days: int = Field(gt=0, le=36500)
