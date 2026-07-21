from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.common import Evidence
from app.models.identity import (
    AgentPrincipal,
    AgentProfile,
    AgentProfileCreate,
    AgentProfileUpdate,
    AgentTokenCreate,
    AgentTokenIssued,
    AgentTokenSummary,
    AuthRequest,
    AuthResult,
    UserProfile,
)
from app.models.kg import KgBuildJobStatus
from app.models.memory import (
    AnswerMemoryLinksRequest,
    AnswerMemoryLinksResponse,
    MemoryBulkDeleteRequest,
    MemoryCreateFromAnswer,
    MemoryHit,
    MemoryNotebookOption,
    MemoryOrigin,
    MemoryPreview,
    MemoryPromotionState,
    MemoryRecord,
    MemoryReviewRequest,
    MemoryStatus,
    MemoryTransferRequest,
    MemoryUpdate,
    PaginatedMemories,
)
from app.models.notebooks import (
    MountedBase,
    MountedByCount,
    NotebookAnalytics,
    NotebookCreate,
    NotebookRef,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    SetBasesRequest,
    SetTierRequest,
    ShareResponse,
    SharedByMeItem,
    SharedPreview,
)
from app.models.reports import (
    ReportCreate,
    ReportDetail,
    ReportExportRequest,
    ReportGenerateRequest,
    ReportOutlineUpdate,
    ReportSummary,
)
from app.models.sources import (
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
    DetectDocTypeItem,
    DetectDocTypesRequest,
    DetectedDocType,
    PaginatedSources,
    PaperAuthor,
    PaperMeta,
    RejectedUrl,
    SourceDetail,
    SourceElement,
    SourceImportFile,
    SourceImportRequest,
    SourceSummary,
)








class RuleCard(BaseModel):
    id: str
    title: str
    statement: str
    applies_to: List[str]
    recommendation: str
    risk_if_ignored: str
    severity: str
    status: str
    owner: str = ""
    last_reviewed: str = ""
    evidence: List[Evidence]


class CitationKnowhowRef(BaseModel):
    """Locator for a citation that resolves to a knowhow table cell (Task 12:
    引用跳转). Populated only when the cited element's
    ``source_elements.metadata`` carries a ``knowhow`` tag (written by
    ``KnowhowProjector._write_elements``) — lets the frontend jump straight
    into that row's drawer instead of a dead/hidden source link."""
    table_id: str
    row_id: str


class Citation(BaseModel):
    label: str
    source_id: str
    element_id: str
    location_label: str
    quoted_span: str
    # Source tier: 'base' (authoritative reference KG) or 'personal' (default,
    # user notes). Mirrors AnswerAnchor.tier — lets the "来源分布" badge count
    # citations, not just anchors.
    tier: str = "personal"
    # 多领域基准库(Task 14): 证据的真实来源 notebook id —— 只在跨库命中(federated
    # retrieval 从一个挂载的参考库找到、并非本次 ask 所在 notebook 的证据)才非空;
    # 本库内证据留空。前端据此查 id→name 映射,给引用徽章标"来自「某某库」",查不到
    # 就退回泛化的 tier 文案(不猜、不显示裸 id)。exclude_if 让绝大多数(同库)引用
    # 的 JSON payload 不多带这个键。
    notebook_id: str = Field(default="", exclude_if=lambda value: not value)
    memory_id: str = Field(default="", exclude_if=lambda value: not value)
    provenance: Dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    knowhow: Optional[CitationKnowhowRef] = Field(
        default=None, exclude_if=lambda value: value is None
    )


class TraceStep(BaseModel):
    """推理模式 agent 的一步轨迹(供前端折叠展示)。"""
    step_type: str            # plan | retrieve | reflect | expand | follow_chain | fallback | answer | skip
    summary: str              # 人话摘要
    detail: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[int] = None  # 该步墙钟耗时(相邻两步 record 之差),供前端展示


class AskRequest(BaseModel):
    question: str
    scenario: Dict[str, str] = Field(default_factory=dict)
    conversation_id: Optional[str] = None
    mode: str = "chunk"       # "chunk"(默认,通用问答) | "fast"(旧KG) | "reasoning" | "graph" | "global"


class AnswerAnchor(BaseModel):
    key: str                 # "k1" — matches [k1] marker in answer text
    object_id: str
    object_type: str
    label: str               # short display token (KG name, clipped)
    name: str = ""
    definition: Optional[str] = None
    snippet: Optional[str] = None      # element_text of the grounding sentence
    source_title: str = ""
    location_label: str = ""
    # Source tier: 'base' (authoritative reference KG) or 'personal' (default,
    # user notes). Lets the UI surface authority + supports conflict precedence.
    tier: str = "personal"
    # 多领域基准库(Task 14): 与 Citation.notebook_id 同一惯例——只在跨库命中时
    # 非空,供前端引用徽章标来源库名。见 Citation.notebook_id 的完整注释。
    notebook_id: str = Field(default="", exclude_if=lambda value: not value)
    provenance: Dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    # Task 12b（引用跳转扩面）: 与 Citation.knowhow 同一 exclude_if 惯例——只有
    # 命中单行 knowhow 格子的知识对象锚点才有值（evidence_context.py
    # knowledge_context/parse_anchors 填充），合并多行/非 knowhow 锚点整体从
    # JSON 缺席。这是「答案 [k] 标记命中」这条主路径（reasoning 模式）的引用
    # 跳转入口，与 Citation 侧的回退列表入口互补。
    knowhow: Optional[CitationKnowhowRef] = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ModelError(BaseModel):
    stage: str       # "embed" | "rerank" | "answer" | "rewrite"
    model: str = ""
    message: str


class AskResponse(BaseModel):
    answer_id: str = ""
    conclusion: str
    answer: str = ""
    grounded: bool = False
    # 相关度感知证据分档：grounded(有据) | overview(概述) | inferred(推断)
    evidence_level: str = "inferred"
    anchors: List[AnswerAnchor] = Field(default_factory=list)
    related_knowledge: List["KnowledgeRecord"] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    llm_mode: str = ""
    # 本轮实际使用的检索 mode（chunk/reasoning/graph/fast/global）。
    # 落库供 openSession 精确恢复引擎，替代旧的 reasoning_trace 猜测。
    mode: str = ""
    conversation_id: str = ""
    # 实际用于检索的 query（原问或改写后）+ 最高命中相关度，供排错/二期标定。
    retrieval_query: str = ""
    top_relevance: float = 0.0
    # 推理模式 agent 轨迹;fast 模式恒为 None。
    reasoning_trace: Optional[List["TraceStep"]] = None
    # 严格推理(reasoning/graph)无可用 KG(本 notebook 无图且无可用 base)时 True。
    kg_required: bool = False
    # 大库(not copyable)且完全无 scale 索引(从未建过)时 True:检索能力受限,
    # 驱动前端渲染「构建索引」提示。「建过但有 delta」不置此位(既有「N 源待索引」
    # 徽章覆盖那种最终一致态)。
    index_required: bool = False
    model_errors: List[ModelError] = Field(default_factory=list)


class ConversationRenameRequest(BaseModel):
    title: str


class ConversationSummary(BaseModel):
    id: str
    notebook_id: str
    title: str = ""
    updated_at: str = ""
    turn_count: int = 0
    used_reasoning: bool = False


class ConversationTurn(BaseModel):
    answer_id: str
    question: str
    response: AskResponse
    created_at: str = ""


class ActiveAskJob(BaseModel):
    job_id: str
    question: str = ""
    mode: str = ""
    trace: List[dict] = Field(default_factory=list)


class ConversationDetail(ConversationSummary):
    turns: List[ConversationTurn] = Field(default_factory=list)
    active_job: Optional["ActiveAskJob"] = None


class SearchHit(BaseModel):
    scope: str
    notebook_id: str
    label: str
    text: str
    source_id: str = ""
    element_id: str = ""
    memory_id: str = Field(default="", exclude_if=lambda value: not value)
    provenance: Dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )


class NotebookSearchResponse(BaseModel):
    query: str
    hits: List[SearchHit]


class KgSearchHit(BaseModel):
    object_id: str
    name: str
    object_type: str
    score: float
    match: str


class KgSearchResponse(BaseModel):
    query: str
    hits: List[KgSearchHit]


class KnowledgeUpdate(BaseModel):
    payload: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    owner: Optional[str] = None


class KnowledgeRef(BaseModel):
    id: str
    object_type: str
    headline: str
    status: str


class KnowledgeFieldValue(BaseModel):
    key: str
    value: str


class KnowledgeRecord(BaseModel):
    """Generic, type-agnostic view of one approved knowledge object, so any
    object type (including academic/textbook types without a bespoke card) can
    be browsed and curated uniformly."""

    id: str
    object_type: str
    headline: str
    fields: List[KnowledgeFieldValue]
    status: str
    owner: str = ""
    last_reviewed: str = ""
    evidence: List[Evidence]


class KnowledgeTypeCount(BaseModel):
    object_type: str
    label: str
    count: int


class PaginatedKnowledge(BaseModel):
    items: List[KnowledgeRecord]
    total_count: int
    offset: int
    limit: int


class ObjectSchemaModel(BaseModel):
    """An editable extraction-schema definition (a typed knowledge object)."""

    object_type: str
    plural: str
    fields: List[str] = Field(default_factory=list)
    primary: str = ""
    description: str = ""
    label: str = ""
    list_fields: List[str] = Field(default_factory=list)
    source: str = "builtin"  # builtin | custom | induced
    status: str = "active"  # active | proposed | disabled
    rationale: str = ""
    notebook_id: str = ""


class ObjectSchemaCreate(BaseModel):
    object_type: str
    plural: str = ""
    fields: List[str] = Field(default_factory=list)
    primary: str = ""
    description: str = ""
    label: str = ""
    list_fields: List[str] = Field(default_factory=list)


class ObjectSchemaUpdate(BaseModel):
    plural: Optional[str] = None
    fields: Optional[List[str]] = None
    primary: Optional[str] = None
    description: Optional[str] = None
    label: Optional[str] = None
    list_fields: Optional[List[str]] = None
    status: Optional[str] = None


class KnowledgeNode(BaseModel):
    id: str
    object_type: str
    headline: str
    status: str


class KnowledgeEdge(BaseModel):
    from_id: str
    to_id: str
    relation: str
    label: str


class KnowledgeGraph(BaseModel):
    nodes: List[KnowledgeNode]
    edges: List[KnowledgeEdge]


class EdgeReviewItem(BaseModel):
    """One item in the edge curation review queue."""
    rel_id: str
    notebook_id: str
    edge_type: str
    source_object_id: str
    target_object_id: str
    source_name: str = ""
    target_name: str = ""
    source_type: str = ""
    target_type: str = ""
    trust_score: float
    edge_centrality: float
    review_priority: float
    review_status: str = "pending"


class EdgeReviewRequest(BaseModel):
    """Payload for POST /relations/{rel_id}/review."""
    status: str   # "verified" | "rejected" | "pending"


class DuplicateGroup(BaseModel):
    object_type: str
    similarity: float
    members: List[KnowledgeRef]



class MergeRequest(BaseModel):
    into_id: str


class FeedbackRequest(BaseModel):
    rating: str
    comment: str = ""


class FeedbackResponse(BaseModel):
    id: str
    answer_id: str
    rating: str
    comment: str = ""


class MemoryOverviewItem(BaseModel):
    id: str
    title: str
    status: MemoryStatus
    updated_at: str


class MemoryOverviewSummary(BaseModel):
    total: int = 0
    confirmed: int = 0
    candidate: int = 0
    recent: List[MemoryOverviewItem] = Field(default_factory=list)


class KnowhowOverviewTable(BaseModel):
    id: str
    title: str
    row_count: int = 0
    last_activity_at: str = ""


class KnowhowOverviewSummary(BaseModel):
    table_count: int = 0
    row_count: int = 0
    projection_pending: int = 0
    projection_failed: int = 0
    stale_code_count: int = 0
    recent_tables: List[KnowhowOverviewTable] = Field(default_factory=list)


class NotebookContentOverview(BaseModel):
    memory: MemoryOverviewSummary = Field(default_factory=MemoryOverviewSummary)
    knowhow: KnowhowOverviewSummary = Field(default_factory=KnowhowOverviewSummary)


class UnifiedKgStatus(BaseModel):
    dirty: bool
    last_rebuild_at: str = ""
    objects: int = 0
    relations: int = 0
    clusters: int = 0
    viz_indexed: bool = False
    viz_nodes: int = 0
    viz_edges: int = 0
    viz_stale: bool = False
    viz_building: bool = False


class MergeReviewJob(BaseModel):
    status: str
    total: int = 0
    done: int = 0
    error: str = ""


class ScaleIndexStatus(BaseModel):
    exists: bool
    stale: bool
    building: bool
    eligible: bool
    n_nodes: int = 0
    n_chunks: int = 0
    n_ann: int = 0
    n_chunk_ann: int = 0
    has_chunk_ann: bool = False
    state: str = "unindexed"
    delta_chunks: int = 0
    total_chunks: int = 0
    unindexed_sources: int = 0
    has_unindexed_content: bool = False
    delta_searchable: bool = False
    last_built_at: str = ""


class RebuildScaleIndexRequest(BaseModel):
    when: str = "now"    # now(立即后台) | idle(低峰调度)
    mode: str = "auto"   # auto(有索引→fold 否则→full) | fold | full


class MergeReviewRequest(BaseModel):
    limit: int = 50
    # 非对称自动落地阈值;省略时由后端 settings 决定(KG_MERGE_CONFIRM/SEPARATE_THRESHOLD)。
    confirm_threshold: Optional[float] = None    # auto-merge 最低置信
    separate_threshold: Optional[float] = None   # auto keep-separate(reject)最低置信


class MergeReviewSummary(BaseModel):
    reviewed: int = 0
    confirmed: int = 0
    rejected: int = 0
    unsure: int = 0


class PromotionCandidate(BaseModel):
    """A personal-KG node proposed for promotion into the base corpus (Track F)."""

    id: str
    notebook_id: str
    object_id: str
    object_type: str
    status: str
    reason: str = ""
    reviewed_by: str = ""
    base_match_id: str = ""
    created_at: str = ""
    # Denormalised fields populated by the repo from knowledge_objects:
    payload: dict = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)
    source_kind: Literal["knowledge", "memory"] = "knowledge"
    memory_id: str = ""
    # Memory-backed proposals are reviewed against this immutable source revision.
    source_revision: int = 0
    # 多领域基准库(Task 7/8):这条候选要晋升进哪个公共知识库。挂 >1 个公共库时
    # 由提交方显式指定;队列里暴露出来是策展人审核"该进哪个库"的唯一依据。
    target_base_id: str = ""
    # Task 13 审查 #4:target_base_id 对应的库名,由后端 join notebooks 给出——
    # 策展人不一定是目标库的 owner,前端自己的 notebooks 列表(自有∪只读加入)
    # 覆盖不到"别人创建的公共知识库",猜不出真名。目标为空或库已不存在时是
    # 空串(list_promotion_queue 批量解析,不逐行查询)。
    target_base_name: str = ""


class PromotionApproveResult(BaseModel):
    candidate_id: str
    base_object_id: str
    base_object_ids: List[str] = Field(default_factory=list)
    merged_into: str = ""   # non-empty if deduped into an existing base object


class PromotionRejectRequest(BaseModel):
    reason: str = ""


class PromoteRequest(BaseModel):
    """POST .../promote 的可选请求体(知识对象晋升 + Memory 晋升共用)。挂载了
    多个公共知识库时必须显式指定 target_base_id,否则服务层拒绝(400)。"""
    target_base_id: str = ""


class ConceptWhitelistEntry(BaseModel):
    term: str
    note: str = ""
    created_at: str = ""


class ConceptWhitelistAdd(BaseModel):
    term: str
    note: str = ""


class ModelServiceView(BaseModel):
    base_url: str = ""
    model: str = ""
    has_key: bool = False
    key_hint: str = ""          # 打码尾段，如 "…t123"；绝不含完整 key
    source: str = "system"      # user | system | none

class ModelServiceUpdate(BaseModel):
    # 三态：字段缺省=不变；""=清除；非空=设置。api_key 同理(缺省=保留原 key)。
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None

class ModelSettingsUpdate(BaseModel):
    llm: Optional[ModelServiceUpdate] = None
    reasoning_llm: Optional[ModelServiceUpdate] = None
    rewrite_llm: Optional[ModelServiceUpdate] = None
    kg_llm: Optional[ModelServiceUpdate] = None
    rerank: Optional[ModelServiceUpdate] = None

class ModelTestRequest(BaseModel):
    service: str
    base_url: str = ""
    api_key: Optional[str] = None   # 省略 → 用已存 key
    model: str = ""

class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: int = 0
    # 诊断字段:失败分支写 f"{type(exc).__name__}: {exc}",给日志和排查,前端不上屏。
    error: str = ""
    # 失败原因的稳定枚举。200 响应挂不上 X-User-Message 头,所以出处由 schema 承载;
    # 这里刻意存 **code 而不是中文文案**——文案归前端(vocabulary.ts 的
    # MODEL_TEST_ERROR),这样它才落在界面词汇守卫的作用域里。后端存中文会绕开那道
    # 守卫,「缺少 base_url / model / api_key」这种把字段名甩给用户的文案正是这么漏的。
    # 取值:unknown_service | missing_config | upstream_error(未知 code 前端走兜底)。
    code: str = ""


class AdminUserUsage(BaseModel):
    id: str
    username: str
    role: str
    created_at: str
    notebooks: int
    sources: int
    conversations: int
    reports: int
    last_active: Optional[str] = None
    is_online: bool = False


class AdminUserNotebook(BaseModel):
    id: str
    name: str
    status: str
    sources: int
    conversations: int
    reports: int
    created_at: str
    updated_at: str


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


__all__ = [
    "ActiveAskJob", "AddUrlSourcesRequest", "AddUrlSourcesResult", "AdminUserNotebook",
    "AdminUserUsage", "AgentPrincipal", "AgentProfile", "AgentProfileCreate",
    "AgentProfileUpdate", "AgentTokenCreate", "AgentTokenIssued", "AgentTokenSummary",
    "AnswerAnchor", "AnswerMemoryLinksRequest", "AnswerMemoryLinksResponse", "AskRequest",
    "AskResponse", "AuthRequest", "AuthResult", "Citation", "CitationKnowhowRef",
    "ConceptWhitelistAdd", "ConceptWhitelistEntry", "ConversationDetail",
    "ConversationRenameRequest", "ConversationSummary", "ConversationTurn", "DetectDocTypeItem",
    "DetectDocTypesRequest", "DetectedDocType", "DuplicateGroup", "EdgeReviewItem",
    "EdgeReviewRequest", "Evidence", "FeedbackRequest", "FeedbackResponse", "KgBuildJobStatus",
    "KgSearchHit", "KgSearchResponse", "KnowhowAgentColumn", "KnowhowAgentTable",
    "KnowhowAppendDuplicateTitle", "KnowhowAppendPreview", "KnowhowAppendResult",
    "KnowhowCellCodePut", "KnowhowCellCodeResult", "KnowhowCellOptimizeResult",
    "KnowhowCellPatch", "KnowhowCellPatchResult", "KnowhowCellReformatResult",
    "KnowhowCellsBatchPatch", "KnowhowColumn", "KnowhowColumnCreate", "KnowhowColumnPatch",
    "KnowhowDiscriminationMethod", "KnowhowDiscriminationRow", "KnowhowDiscriminationSet",
    "KnowhowImportPreview", "KnowhowNewColumnInput", "KnowhowOverviewSummary",
    "KnowhowOverviewTable", "KnowhowPreviewColumn", "KnowhowRow", "KnowhowRowCell",
    "KnowhowRowCode", "KnowhowRowCreate", "KnowhowRowDetail", "KnowhowTableCreate",
    "KnowhowTableDetail", "KnowhowTablePatch", "KnowhowTableSummary", "KnowhowTransferRequest",
    "KnowledgeEdge", "KnowledgeFieldValue", "KnowledgeGraph", "KnowledgeNode", "KnowledgeRecord",
    "KnowledgeRef", "KnowledgeTypeCount", "KnowledgeUpdate", "MemoryBulkDeleteRequest",
    "MemoryCreateFromAnswer", "MemoryHit", "MemoryNotebookOption", "MemoryOrigin",
    "MemoryOverviewItem", "MemoryOverviewSummary", "MemoryPreview", "MemoryPromotionState",
    "MemoryRecord", "MemoryReviewRequest", "MemoryStatus", "MemoryTransferRequest", "MemoryUpdate",
    "MergeRequest", "MergeReviewJob", "MergeReviewRequest", "MergeReviewSummary", "ModelError",
    "ModelServiceUpdate", "ModelServiceView", "ModelSettingsUpdate", "ModelTestRequest",
    "ModelTestResult", "MountedBase", "MountedByCount", "NotebookAnalytics",
    "NotebookContentOverview", "NotebookCreate", "NotebookRef", "NotebookSearchResponse",
    "NotebookSummary", "NotebookTemplate", "NotebookUpdate", "ObjectSchemaCreate",
    "ObjectSchemaModel", "ObjectSchemaUpdate", "PaginatedKnowledge", "PaginatedMemories",
    "PaginatedSources", "PaperAuthor", "PaperMeta", "PromoteRequest", "PromotionApproveResult",
    "PromotionCandidate", "PromotionRejectRequest", "RebuildScaleIndexRequest", "RejectedUrl",
    "ReportCreate", "ReportDetail", "ReportExportRequest", "ReportGenerateRequest",
    "ReportOutlineUpdate", "ReportSummary", "RuleCard", "ScaleIndexStatus", "SearchHit",
    "SetBasesRequest", "SetTierRequest", "ShareResponse", "SharedByMeItem", "SharedPreview",
    "SourceDetail", "SourceElement", "SourceImportFile", "SourceImportRequest", "SourceSummary",
    "TraceStep", "UnifiedKgStatus", "UserProfile",
]
