from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from app.models.source_scope import (
    BaseNotebookScope,
    RetrievalScopeReceipt,
    SourceScope,
)

from app.core.ask_retrieval_policy import (
    DEFAULT_RETRIEVAL_EFFORT,
    ResultScope,
    RetrievalEffort,
)
from app.core.internal_observability import public_trace_steps

from app.core.model_safety import (
    safe_model_display_name,
    safe_model_error_code,
    safe_model_metadata_id,
    safe_model_error_stage,
    safe_model_label,
    safe_model_support_id,
)
from app.domain.gap_consult import (
    GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GAP_SUGGESTION_URL_MAX_CHARS,
)
from app.models.common import Evidence
from app.models.knowledge import KnowledgeRecord


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


class CitationImage(BaseModel):
    """一张「本段附图」：绑定证据里带图注的图片元素（图注命中检索才带得出）。

    产品裁决（设计文档 §0）：**模型不看图**。这是纯响应装配层的增强——检索行为、
    引用文本、锚点解析逐字不变，附图只是「证据片段附近的图」，展示层必须把它与
    引证内容视觉区分，绝不冒充模型引用过的证据。

    ``caption`` 取 ``source_elements.metadata.caption``（解析器只在图注非空时写
    这个键），无图注时回退到 ``metadata.description``——markdown 的
    `> **图片描述**` 引用块，没有 alt 的图正是靠它进的检索，那段字是用户自己写下
    的描述。刻意**不**回退到元素 ``text``：两样都没有的图片元素 ``text`` 是
    「Markdown 图 3」/「PDF p.2 图 1」这类占位定位串，把它当图注渲染是在编造一个
    用户从没写过的说明。两样都没有时留空，由前端只渲染图。

    ``asset_id`` 只是句柄，不是内容：前端仍走 active-notebook 资产代理端点
    (``GET /api/notebooks/{active}/assets/{asset_id}``) 取图，那里每请求实时复验
    参与集权限。因此本字段不新增任何权限面。
    """
    element_id: str
    asset_id: str
    caption: str = ""


class Citation(BaseModel):
    label: str
    source_id: str
    element_id: str
    location_label: str
    quoted_span: str
    # Original user-uploaded file name.  This is deliberately separate from
    # ``label``/the source display title: grounded papers keep their parsed
    # paper title as the readable heading, while the UI can still show which
    # uploaded file the evidence came from.  MinerU output paths/names never
    # populate this field.
    source_file_name: str = Field(default="", exclude_if=lambda value: not value)
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
    # 本段附图（检索结果带图）: 与 knowhow/notebook_id 同一 exclude_if 惯例——空
    # 列表整体从 JSON 缺席，所以绝大多数（无图）引用的 payload 一个字节都不多带，
    # 旧持久化 payload 重开时缺这个键自然回退空列表，零 migration。
    images: List[CitationImage] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


# Agentic Memory P4 (T1): the hard cap on how many result-object ids a single
# "result_ids" trace-step detail may carry (retrieve/ppr/exact_lookup/expand
# steps — see the write sites in reasoning_retrieval.py). Four-point
# disclosure argument for why this is allowed onto ``TraceStep.detail`` at
# all:
#   ① these are opaque handles (a chunk_id / object_id), not content — the
#     step's own ``summary`` already tells a human what happened in plainer
#     language (a name, a hit count) than a bare id ever could;
#   ② the only surface that could otherwise leak these to an anonymous
#     visitor — report/conversation public sharing — structurally excludes
#     ``reasoning_trace`` from its projection entirely (public report/
#     conversation views never carry a trace field), so this never reaches
#     an unauthenticated reader;
#   ③ trace detail already carries bare ids at several existing emit sites
#     (``expand``'s ``object_id``, ``follow_chain``'s skip detail, the
#     outline step's bound-evidence ids) — this is the same disclosure
#     shape the trace already has, not a new one;
#   ④ the constants live here, beside ``TraceStep`` itself, rather than in
#     ``app.repositories.ports`` (where the read-side projection that
#     re-applies this cap lives) because ``ask_service`` — the module that
#     writes these details on the hot request path — does not import
#     ``app.repositories.ports`` at runtime, and this module is already its
#     dependency for ``TraceStep``.
# Precise values are documented in ``docs/product-and-api*.md``, not here.
TRACE_RESULT_IDS_MAX = 20

# Agentic Memory P4 (T1): the hard cap on ``anchor_evidence_ids`` in the
# final ``synthesis``/``answer`` trace step's detail — the answer's actually
# bound [k] anchors, by ``AnswerAnchor.object_id``. Set to 96, the largest
# ``ranked_final_cap`` across all retrieval-effort tiers (the exhaustive
# tier's), so a genuinely complete RANKED answer's anchor list is never
# truncated by this cap in practice under the existing per-tier retrieval
# budget — for that shape of run it is a protocol ceiling, not a real-world
# limit. Same four-point disclosure argument as ``TRACE_RESULT_IDS_MAX``
# above.
#
# ⚠ Registered exception (修复轮 96 上界披露): a COLLECTION-ENUMERATION run
# is not bound by ``ranked_final_cap`` at all — every row that enters the
# synthesis preview gets an isolated ``k5001+`` anchor id (see the citation
# contract in ``docs/product-and-api*.md``), and a large enumerated list can
# genuinely bind more than 96 of them. When that happens this cap DOES bind,
# the write side sets the sparse ``anchor_evidence_ids_truncated`` marker, and
# the read side (``retrieval_experience_projection.py``'s pass 1) treats a
# truncated anchor list as poison for the WHOLE run's step→anchor
# attribution, not just the excess tail. That is the deliberately SAFE
# direction: losing attribution signal for an oversized enumeration run costs
# nothing but a slightly thinner distillation sample, where silently
# accepting a truncated anchor set would teach the library "no hit" for
# actions whose real result may have been in the cut-off tail.
TRACE_ANCHOR_EVIDENCE_IDS_MAX = 96


class TraceStep(BaseModel):
    """推理模式 agent 的一步轨迹(供前端折叠展示)。"""
    step_type: str            # intent | plan | retrieve | enumerate | reflect | expand | follow_chain | fallback | answer | skip
    summary: str              # 人话摘要
    detail: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[int] = None  # 该步墙钟耗时(相邻两步 record 之差),供前端展示


class QueryIntentTopic(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=1000)
    retrieval_queries: List[str] = Field(default_factory=list, max_length=4)


class QueryIntentAmbiguity(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=300)
    required: bool = True
    options: List[str] = Field(default_factory=list, max_length=4)


class QueryIntentAnswer(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=2000)


class QueryIntentContract(BaseModel):
    """Corpus-blind understanding shared by report and reasoning retrieval."""
    # Internal provenance for automatic Ask routing. A PrivateAttr is required:
    # Field(exclude=True) would hide the value from response bodies while still
    # leaking it into the public OpenAPI schema for /ask/intent.
    _understanding_succeeded: bool = PrivateAttr(default=True)
    objective: str = Field(min_length=1, max_length=4000)
    resolved_question: str = Field(min_length=1, max_length=4000)
    intent_type: str = "other"
    # ``ranked`` may stop after the best evidence.  The other scopes require a
    # collection-aware executor and must never use a relevance top-N as proof
    # of completeness.
    result_scope: ResultScope = "ranked"
    completeness_required: bool = False
    entities: List[str] = Field(default_factory=list, max_length=8)
    mandatory_topics: List[QueryIntentTopic] = Field(default_factory=list, max_length=16)
    comparison_axes: List[str] = Field(default_factory=list, max_length=8)
    constraints: List[str] = Field(default_factory=list, max_length=8)
    excluded_topics: List[str] = Field(default_factory=list, max_length=8)
    expected_output: str = Field(default="", max_length=1000)
    assumptions: List[str] = Field(default_factory=list, max_length=8)
    ambiguities: List[QueryIntentAmbiguity] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_clarification: bool = False
    confirmed: bool = False
    clarification_answers: List[Dict[str, str]] = Field(
        default_factory=list, max_length=8
    )

    @model_validator(mode="after")
    def enforce_scope_completeness(self) -> "QueryIntentContract":
        # Exact aggregate counts and hybrid/complete collections all depend on
        # exhaustive collection coverage even when an older client omitted the
        # explicit boolean.
        if self.completeness_required and self.result_scope == "ranked":
            self.result_scope = "complete"
        elif self.result_scope != "ranked":
            self.completeness_required = True
        return self


# The one rail on the length of *the question a client asks*, shared by every
# entry point that accepts one: the intent preflight, the confirmed question it
# hands back, and the ask request itself.  It is a protocol boundary, not a
# tunable, so it lives as a named constant rather than a literal repeated per
# field; `frontend/app/ask-api.ts::ASK_INPUT_LIMITS` mirrors it.
#
# It is load-bearing for the *public* share page: `conversation_public_view`
# serves each turn's question WHOLE (truncating a user's own artifact with no
# disclosure violates AGENTS.md 用户编辑的数据不得静默截断, which is why the old
# 2,000-char public cap came out in codex #522 R1).  "Serve it whole" is only
# bounded while the write side refuses an over-length question in the first
# place -- that is the other half of the same red line (前端显示同一护栏, API 超限
# 明确拒绝), and without it an anonymous response would be unbounded by client
# input.  Same finding the report side closed in codex #525 R1 P2, where
# `REPORT_QUESTION_MAX_CHARS` plays exactly this part; the two hold the same
# value because they bound the same kind of text, but they stay separate
# constants because they are separate protocol surfaces.
#
# Deliberately NOT applied to `QueryIntentContract`: its fields are the
# understanding model's OUTPUT (already individually capped) rather than the
# question a user typed, and folding them in here would mean a future change to
# the asked-question rail silently moved the contract's caps too.
#
# Residual, deliberately out of scope and recorded rather than hidden: a
# conversation whose turns predate this rail can still carry a longer question
# (the *title* half of this note is closed -- see
# `CONVERSATION_TITLE_MAX_CHARS`).  Bounding a pre-rail row inside the projection
# needs a disclosure field on `PublicTurn` plus a public-page change (what
# `PublicReport.question_truncated` cost on the report side), which is a separate
# deliverable, not something to slip in silently here.
ASK_QUESTION_MAX_CHARS = 4000


class AskIntentPreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_CHARS)
    conversation_id: Optional[str] = Field(default=None, max_length=200)
    source_scope: Optional[SourceScope] = None
    # None preserves the historical behavior of every mounted base notebook
    # participating unconditionally. Independent dimension from source_scope:
    # this selects whole mounted reference libraries, never sources within them.
    base_scope: Optional[BaseNotebookScope] = None


class AskIntentConfirmation(BaseModel):
    contract: QueryIntentContract
    resolved_question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_CHARS)
    answers: List[QueryIntentAnswer] = Field(default_factory=list, max_length=8)
    # Wall-clock of the understanding phase, measured by the client: it runs in
    # /ask/intent, before any durable job exists, so the server cannot time it.
    # Reported back only so the persisted trace keeps that phase; it never feeds
    # retrieval. Bounded so a bad client cannot inflate a run's reported total.
    understanding_ms: Optional[int] = Field(default=None, ge=0, le=3_600_000)


class AskRequest(BaseModel):
    # No `min_length`: an empty question already reaches the engine today and is
    # handled downstream, so adding one here would change behaviour at every
    # entry point at once -- a different change from bounding the top end.
    # `max_length` is the half the public conversation projection leans on; see
    # `ASK_QUESTION_MAX_CHARS`.
    question: str = Field(max_length=ASK_QUESTION_MAX_CHARS)
    # Browser-captured submission instant. It is display metadata only and
    # never participates in ordering, authorization, retrieval, or scheduling.
    asked_at: str = Field(default="", max_length=64)
    scenario: Dict[str, str] = Field(default_factory=dict)
    conversation_id: Optional[str] = None
    # ``auto`` is a request-only backend selector resolved to chunk/reasoning
    # before durable state; it is never a persisted engine id.
    mode: str = "chunk"       # "chunk"(默认) | "reasoning" | "auto" | retired aliases(fast/global/graph)
    # User-controlled resource level.  It selects immutable hard ceilings from
    # ask_retrieval_policy; the model may stop early but cannot increase them.
    retrieval_effort: RetrievalEffort = DEFAULT_RETRIEVAL_EFFORT
    # None preserves the historical whole-notebook behavior. include=[] is an
    # explicit empty local scope; mounted base notebooks remain participants.
    source_scope: Optional[SourceScope] = None
    # None preserves the historical behavior of every mounted base notebook
    # participating unconditionally. include=[] is an explicit "no mounted base
    # library participates" selection. Independent dimension from source_scope
    # -- see BaseNotebookScope's docstring.
    base_scope: Optional[BaseNotebookScope] = None
    # reasoning only: returned by /ask/intent and confirmed by the user (or
    # auto-confirmed by the UI when no blocking ambiguity exists).
    intent: Optional[AskIntentConfirmation] = None

    @field_validator("asked_at")
    @classmethod
    def validate_asked_at(cls, value: str) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("asked_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("asked_at must include a timezone offset")
        return value


class AnswerAnchor(BaseModel):
    key: str                 # "k1" — matches [k1] marker in answer text
    object_id: str
    object_type: str
    label: str               # short display token (KG name, clipped)
    name: str = ""
    definition: Optional[str] = None
    snippet: Optional[str] = None      # element_text of the grounding sentence
    source_title: str = ""
    # Same contract as Citation.source_file_name.  Anchors are authoritative
    # whenever the answer contains valid [k] markers, so the field must live on
    # both response shapes rather than only on the fallback Citation list.
    source_file_name: str = Field(default="", exclude_if=lambda value: not value)
    location_label: str = ""
    # Exact source locator for anchors backed by one SourceElement.  Chunk/KG
    # anchors may leave either value empty; enumerated collection rows fill
    # both whenever their bounded evidence reference still exists.
    source_id: str = Field(default="", exclude_if=lambda value: not value)
    element_id: str = Field(default="", exclude_if=lambda value: not value)
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
    # 本段附图（检索结果带图）: 字段必须同时活在 AnswerAnchor 上——reasoning 模式
    # 的权威显示路径是 `[k]` 锚点（前端 buildAnswerReferences 是 anchor 优先的
    # 全有全无），只加在 Citation 上就只覆盖到「模型一个锚点都没吐出来」的回退
    # 列表，主路径永远看不到图。同 Citation.images 的 exclude_if 惯例。
    images: List[CitationImage] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )


class ModelError(BaseModel):
    service_id: str = ""
    service_name: str = ""
    workload_id: str = ""
    workload_label: str = ""
    stage: str       # "embed" | "rerank" | "answer" | "rewrite"
    model: str = ""
    message: str
    support_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        # Old persisted rows exposed a logical role in ``service``.  It is not
        # a physical system service id, so replay must not invent one.
        return {
            **value,
            "service_id": value.get("service_id", ""),
            "service_name": value.get("service_name", ""),
            "workload_id": value.get("workload_id", ""),
            "workload_label": value.get("workload_label", ""),
            "support_id": value.get("support_id", ""),
        }

    @field_validator("service_id", "workload_id", mode="before")
    @classmethod
    def validate_metadata_id(cls, value: object) -> str:
        return safe_model_metadata_id(value)

    @field_validator("service_name", "workload_label", mode="before")
    @classmethod
    def validate_display_name(cls, value: object) -> str:
        return safe_model_display_name(value)

    @field_validator("stage", mode="before")
    @classmethod
    def validate_stage(cls, value: object) -> str:
        return safe_model_error_stage(value)

    @field_validator("model", mode="before")
    @classmethod
    def validate_model(cls, value: object) -> str:
        return safe_model_label(value)

    @field_validator("message", mode="before")
    @classmethod
    def validate_message(cls, value: object) -> str:
        return safe_model_error_code(value)

    @field_validator("support_id", mode="before")
    @classmethod
    def validate_support_id(cls, value: object) -> str:
        return safe_model_support_id(value)


class StructuredResultColumn(BaseModel):
    """A stable Knowhow column descriptor carried with exhaustive results."""

    id: str
    name: str
    role: str = "attribute"


class StructuredResultRow(BaseModel):
    """One directly addressable row; cells are keyed by column id."""

    row_id: str
    position: int
    cells: Dict[str, str] = Field(default_factory=dict)


class StructuredResultCoverage(BaseModel):
    """Proof of what an enumeration did and did not cover."""

    total_rows: int = Field(default=0, ge=0)
    scanned_rows: int = Field(default=0, ge=0)
    returned_rows: int = Field(default=0, ge=0)
    complete: bool = False
    truncated_reason: str = Field(default="", max_length=80)
    overflow_semantics: str = Field(default="", max_length=40)


class StructuredKnowhowResult(BaseModel):
    """Bounded, pageable collection result kept outside ranked evidence top-N."""

    kind: Literal["knowhow"] = "knowhow"
    table_id: str
    title: str
    columns: List[StructuredResultColumn] = Field(default_factory=list)
    rows: List[StructuredResultRow] = Field(default_factory=list)
    coverage: StructuredResultCoverage


# Deliberately NOT a reuse of ``StructuredResultCoverage``: that model's
# ``total_rows`` defaults to ``0``, which cannot express "denominator
# unknown" (see ``EnumerationCoverage.total`` in
# ``app.services.collection_enumeration`` — ``None`` means the map could not
# establish a count, NOT zero). A renderer that defaulted an absent total to
# ``0`` would print "N/0" for exactly the large-base-library case this field
# exists to cover honestly instead.
class TypedCollectionCoverage(BaseModel):
    """Coverage proof for one enumerated element or knowledge-object list:
    how many rows were returned against the collection's known size, and
    whether the listing is complete or was cut short. ``total=None`` means
    the collection's overall size could not be determined — display this as
    an unknown denominator, never as zero."""

    returned_total: int = Field(default=0, ge=0)
    total: Optional[int] = None
    complete: bool = False
    truncated_reason: str = Field(default="", max_length=80)
    overflow_semantics: str = Field(default="", max_length=40)


# One model, two uses, with each arm's fields left at their zero value when
# not applicable (design choice): the two dataclasses this mirrors —
# ``ElementItem`` and ``KgObjectItem`` in
# ``app.services.collection_enumeration`` — share ``notebook_id``/``tier``
# and are otherwise disjoint, but a shared wire shape lets ``collection``
# alone tell a consumer which arm is populated, rather than needing a second
# per-field union just to read one of two possible attribute sets.
class TypedCollectionItem(BaseModel):
    """One row of an enumerated source-element, knowledge-object or document
    list.  Element rows populate ``source_id``/``source_title``/
    ``element_type``/``location_label``/``text``/``asset_id``;
    knowledge-object rows populate ``name``/``section_path``/
    ``evidence_element_ids`` instead; document rows (``collection ==
    "sources"``) reuse the element arm's ``source_id``/``source_title`` plus
    ``location_label`` for the document TYPE (already in interface words) and
    ``text`` for the stored summary excerpt, leaving ``element_type`` empty."""

    item_id: str
    # element-only (``collection == "elements"``); mirrors ``ElementItem``.
    source_id: str = ""
    source_title: str = ""
    element_type: str = ""
    location_label: str = ""
    text: str = ""
    asset_id: str = ""
    # kg_object-only (``collection == "kg_objects"``); mirrors ``KgObjectItem``.
    name: str = ""
    section_path: str = ""
    # Bounded to MAX_EVIDENCE_REFS=3 in app.services.collection_enumeration
    # (that constant carries a reverse-pointing comment back to this field).
    # Duplicated as a literal rather than imported: app.models must not import
    # app.services (layering — models sit below services). Kept in sync by
    # test_typed_collection_result_sets.py::
    # test_max_evidence_refs_parity_between_executor_and_wire_model.
    evidence_element_ids: List[str] = Field(default_factory=list, max_length=3)
    # First live source occurrence, resolved server-side inside the active
    # notebook's participant set.  This is intentionally one bounded Citation
    # rather than an unbounded evidence expansion; it gives every delivered
    # checklist row an original-source locator and excerpt.
    citation: Optional[Citation] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # shared
    notebook_id: str = ""
    tier: str = "personal"


# Kept outside ranked evidence top-N for the same reason as
# StructuredKnowhowResult: its ``coverage``, not its position in a relevance
# ranking, is the authority on complete/partial.
class TypedCollectionResult(BaseModel):
    """One enumerated element, knowledge-object or document collection, kept
    alongside ranked evidence in the response rather than folded into it. Its
    ``coverage`` — not its position in a relevance ranking — is the
    authority on whether the list is complete."""

    kind: Literal["collection"] = "collection"
    collection: Literal["elements", "kg_objects", "sources"]
    element_kind: str = ""     # non-empty when collection == "elements"
    object_type: str = ""      # non-empty when collection == "kg_objects"
    # ``sources`` has no sub-type: the library's document list is one whole
    # collection, so both of the fields above stay empty for it.
    source_id: str = ""        # non-empty when scoped to a single source
    items: List[TypedCollectionItem] = Field(default_factory=list)
    coverage: TypedCollectionCoverage
    # Rows that actually entered the answer-synthesis prompt preview (bounded
    # separately from ``coverage.returned_total`` by inline-row/character
    # budgets — see ``enumeration_prompt_block``), and whether that preview
    # covered the whole listed set. Mirrors ``StructuredBatchCoverage``'s
    # synthesis_rows/synthesis_complete split between "what was enumerated"
    # and "what the model actually saw". ``None`` means no synthesis preview
    # was attempted at all (model unconfigured, or synthesis never ran).
    synthesis_rows: int = Field(default=0, ge=0)
    synthesis_complete: Optional[bool] = None


class StructuredBatchCoverage(BaseModel):
    """Collection-level coverage, distinct from each selected table's status."""

    known_tables: int = Field(default=0, ge=0)
    selected_tables: int = Field(default=0, ge=0)
    known_total_rows: int = Field(default=0, ge=0)
    scanned_rows: int = Field(default=0, ge=0)
    returned_rows: int = Field(default=0, ge=0)
    complete: bool = False
    truncated_reason: str = Field(default="", max_length=80)
    overflow_semantics: str = Field(default="", max_length=40)
    # Hybrid synthesis may see only a bounded preview even when enumeration is
    # complete.  ``None`` means no model synthesis was attempted.
    synthesis_rows: int = Field(default=0, ge=0)
    synthesis_complete: Optional[bool] = None


class AskGapSuggestion(BaseModel):
    """One pointer to material OUTSIDE this notebook.

    Not evidence, and the field set says so: there is no ``source_id``, no
    ``element_id``, no relevance score and no citation key, because nothing
    here was retrieved, cited, anchored, or shown to the answering model.  A
    reader who wants it has to import the URL first, which is an ordinary
    source add with its own parsing and its own permissions.

    The ``max_length`` values are imported from ``app.domain.gap_consult`` —
    the same constants the host truncates with — so the wire rail and the
    admission rail cannot drift apart.
    """

    title: str = Field(max_length=GAP_SUGGESTION_TITLE_MAX_CHARS)
    url: str = Field(max_length=GAP_SUGGESTION_URL_MAX_CHARS)
    summary: str = Field(default="", max_length=GAP_SUGGESTION_SUMMARY_MAX_CHARS)
    source_label: str = Field(
        default="", max_length=GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS
    )


class AskResponse(BaseModel):
    answer_id: str = ""
    asked_at: str = Field(default="", exclude_if=lambda value: not value)
    # Server-side answer completion instant.  AskStateStore stamps this from
    # the same clock value persisted as answers.created_at so live stream
    # responses and reopened conversations expose one authoritative time.
    answered_at: str = Field(default="", exclude_if=lambda value: not value)
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
    # Persist the exact confirmed understanding used by reasoning so reopened
    # turns can explain what the system actually searched for.
    intent: Optional[QueryIntentContract] = None
    # The stable user-selected resource profile and any collection-aware result
    # sets are persisted with the turn.  ``result_sets`` are not relevance
    # evidence: their coverage object is the authority for complete/partial.
    retrieval_effort: RetrievalEffort = DEFAULT_RETRIEVAL_EFFORT
    # kind-discriminated union (PR-2 T5): Knowhow's whole-table batches
    # (kind="knowhow") and typed element/KG-object enumerations
    # (kind="collection") both live here, appended in that order by the
    # reasoning handler. ``kind`` has carried a non-empty default since the
    # commit that introduced ``result_sets`` (#371), so every persisted turn
    # already has it; ``_default_legacy_result_kind`` below is a defensive
    # backstop for any hand-built/legacy payload that does not, not evidence
    # that one has ever been observed.
    result_sets: List[
        Annotated[
            Union[StructuredKnowhowResult, TypedCollectionResult],
            Field(discriminator="kind"),
        ]
    ] = Field(default_factory=list, exclude_if=lambda value: not value)
    result_coverage: Optional[StructuredBatchCoverage] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Read-only receipt of the scope this run actually ran under (checkbox
    # path). Absent -- and therefore serialized byte-identically to every
    # historical payload -- when the request narrowed NEITHER dimension: a run
    # that scoped nothing made no selection to report, and manufacturing a
    # "looks limited" receipt for it would be a lie about the run. Retrieval
    # never reads it back; the authoritative ceiling is the frozen
    # SourceScope/BaseNotebookScope on the request.
    retrieval_scope: Optional[RetrievalScopeReceipt] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # 严格推理(reasoning/graph)无可用 KG(本 notebook 无图且无可用 base)时 True。
    kg_required: bool = False
    # 大库(not copyable)且完全无 scale 索引(从未建过)时 True:检索能力受限,
    # 驱动前端渲染「构建索引」提示。「建过但有 delta」不置此位(既有「N 源待索引」
    # 徽章覆盖那种最终一致态)。
    index_required: bool = False
    # Gap consultation (``ask.gap_consult``): pointers to material outside the
    # notebook, offered when this run came up thin or left a confirmed
    # direction uncovered.  Three things it deliberately is NOT:
    #   * not evidence — never retrieved, never scored, never in the synthesis
    #     prompt, so it cannot have moved a single word of ``answer``;
    #   * not citable — it takes no ``[k]`` key and appears in neither
    #     ``anchors`` nor ``citations``;
    #   * not public — ``conversation_public_view`` projects a whitelist and
    #     this field is not on it, so a shared link never carries it.
    # Empty by the ``exclude_if`` convention, so a deployment with no
    # gap-consult plugin serializes byte-identically to every historical
    # payload and a reopened legacy turn simply has none.
    gap_suggestions: List[AskGapSuggestion] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    model_errors: List[ModelError] = Field(default_factory=list)

    @field_validator("reasoning_trace", mode="before")
    @classmethod
    def _hide_internal_trace_steps(cls, value: object) -> object:
        if value is None:
            return None
        return public_trace_steps(value)

    @field_validator("result_sets", mode="before")
    @classmethod
    def _default_legacy_result_kind(cls, value: object) -> object:
        """Backfill a missing/falsy ``kind`` to ``"knowhow"`` on raw dict
        entries before the discriminated union routes them.

        ``result_sets`` was always ``List[StructuredKnowhowResult]`` before
        this union existed, and ``kind`` has defaulted to ``"knowhow"`` since
        that field's introduction, so a real gap has not been observed. This
        exists purely so a row that somehow lacks the key (a hand-built
        fixture, an external caller, a future migration) still resolves to
        the only kind that ever existed prior to PR-2, instead of a
        discriminator-tag lookup error.
        """
        if not isinstance(value, list):
            return value
        normalized = []
        for entry in value:
            if isinstance(entry, dict) and not entry.get("kind"):
                entry = {**entry, "kind": "knowhow"}
            normalized.append(entry)
        return normalized


# The rail on a conversation TITLE, the other half of what the public share page
# serves verbatim.  Renaming is the only way a title exceeds the 60 characters
# `ensure_conversation` slices off the first question, so this endpoint is the
# whole write side of that field.
#
# It is load-bearing for the *public* page for exactly the reason
# `ASK_QUESTION_MAX_CHARS` is: `conversation_public_view._title_text` serves the
# title WHOLE, because clipping a user's own title with no disclosure is what
# AGENTS.md 用户编辑的数据不得静默截断 forbids (the old 400-char public cap came
# out in codex #522 R2 for that reason).  "Serve it whole" is only a *bounded*
# promise while the write side refuses an over-length title -- otherwise an
# anonymous response stays unbounded by client input, which is the finding codex
# #525 R1 P2 raised and #526 closed for the question half.
#
# 200, not `ASK_QUESTION_MAX_CHARS`: that value bounds question-length prose,
# and reusing it here would say a 4,000-character conversation *label* is a
# shape we intend to serve.  200 matches `QueryIntentTopic.title` in this module
# and leaves better than 3x headroom over the 60 characters the server itself
# generates, while staying a plausible one-line label.
#
# No `min_length`, mirroring `AskRequest.question`: an empty title is accepted
# today and the route stores it, so adding one would change behaviour at the
# bottom end -- a different change from bounding the top.
CONVERSATION_TITLE_MAX_CHARS = 200


class ConversationRenameRequest(BaseModel):
    title: str = Field(max_length=CONVERSATION_TITLE_MAX_CHARS)


class ConversationBulkDeleteResult(BaseModel):
    """Authoritative conversations removed by one bulk-cleanup transaction."""

    deleted: int = 0
    deleted_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_authoritative_count(self) -> "ConversationBulkDeleteResult":
        if self.deleted != len(self.deleted_ids):
            raise ValueError("deleted must equal len(deleted_ids)")
        return self


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
    asked_at: str = ""
    created_at: str = ""


class ActiveAskJob(BaseModel):
    job_id: str
    question: str = ""
    asked_at: str = ""
    mode: str = ""
    trace: List[dict] = Field(default_factory=list)

    @field_validator("trace", mode="before")
    @classmethod
    def _hide_internal_trace_steps(cls, value: object) -> object:
        return public_trace_steps(value)


class ConversationDetail(ConversationSummary):
    turns: List[ConversationTurn] = Field(default_factory=list)
    active_job: Optional["ActiveAskJob"] = None


class ConversationShareRequest(BaseModel):
    """The client's disclosure boundary for a share / "update to latest" (codex
    #522 R2 P1).

    ``expected_through_id`` is the newest answer id the client saw in the SAME
    turns it computed its disclosure from. The store pins the watermark to
    exactly that answer, so the published snapshot equals the disclosed one —
    even if a newer answer landed between the client's read and this POST (the
    disclosure TOCTOU). Empty (a legacy / no-body client) falls back to
    "current latest". The body itself is optional so an empty POST still works.
    """

    expected_through_id: str = ""


class ConversationShareResponse(BaseModel):
    """The public link + read watermark for one shared conversation (T2).

    Mirrors ``ReportShareResponse`` but also carries the watermark: "share" and
    "update to latest" are the same call, so the caller always sees which answer
    the public snapshot currently ends at. Deliberately excludes the
    conversation's ``created_by`` — that is a server-side gate field, never
    handed to the client — and every id (``notebook_id``/``conversation_id``/
    ``shared_through_id`` is the watermark answer's id, which is a bounded
    internal cursor the owner already holds, not a cross-notebook handle)."""

    share_token: str
    shared_through_at: str = ""
    shared_through_id: str = ""


class PublicReference(BaseModel):
    """One reference in a shared conversation, as an anonymous reader sees it
    (T3). Same shape as ``PublicReportReference``.

    No ``source_id`` / ``element_id`` / ``object_id`` / ``notebook_id`` /
    ``memory_id`` / ``provenance`` / ``knowhow`` / ``images``: a public link must
    not hand out handles into the authenticated API. Built by
    ``services/conversation_public_view.py`` as an allowlist — a Memory citation
    keeps its title/snippet but loses its ``memory_id``. ``key`` (``"k1"`` or a
    positional ``"1"``) is preserved so the page can number ``[k]`` markers from
    it rather than from a list position the reference filter may have shifted.

    ``title_truncated``/``snippet_truncated`` disclose that an over-length title
    or excerpt was clipped to its bounded prefix, so the page can mark it rather
    than drop the tail silently (codex #522 R3)."""

    key: str = ""
    title: str = ""
    file_name: str = ""
    location: str = ""
    snippet: str = ""
    title_truncated: bool = False
    snippet_truncated: bool = False
    file_name_truncated: bool = False
    # Safe presentation flag only. True when the selected evidence element is
    # itself one of the attached images, so anonymous UI can hide the parser's
    # duplicated caption/description without receiving either internal id.
    is_image_reference: bool = False


class PublicImage(BaseModel):
    """One answer-attached image an anonymous reader can fetch (T4).

    Carries NO addressable id. The ``asset_id`` is replaced by an opaque,
    token-scoped ``alias`` (``services/conversation_public_view.py``'s
    ``conversation_asset_alias`` = HMAC-SHA256 of the asset_id under the share
    token), and the ``element_id`` is dropped entirely. The bytes are served by
    ``GET /public/conversations/{token}/assets/{alias}``, which reverses the
    alias against the snapshot's referenced assets only — so the alias is the
    only handle a public reader ever gets, and it is meaningless once the token
    is revoked. ``caption`` is the public half of ``CitationImage`` (empty when
    the image had no caption)."""

    alias: str = ""
    caption: str = ""
    # Visible public reference keys (``k1`` or positional ``1``) that bind this
    # image. They are already present on PublicReference and disclose no
    # addressable internal id; the browser uses them only to place the image
    # beside the matching marker in ``answer_md``.
    reference_keys: List[str] = Field(default_factory=list)


class PublicTurn(BaseModel):
    """One Q&A turn in a shared conversation, readable without a session (T3).

    Excludes the whole reasoning surface (``reasoning_trace`` / ``intent`` /
    ``retrieval_scope`` / ``retrieval_query`` / ``top_relevance`` / ``mode`` /
    ``llm_mode`` / ``retrieval_effort`` / ``index_required``) and every
    addressable id. ``evidence_level`` IS exposed — it is part of the answer's
    stated credibility, not an internal flag. Answer-attached images are
    surfaced as token-scoped ``PublicImage`` aliases (T4), never raw
    ``asset_id``/``element_id``."""

    question: str = ""
    answer_md: str = ""
    asked_at: str = ""
    answered_at: str = ""
    # grounded / overview / inferred (答案可信度分档).
    evidence_level: str = "inferred"
    references: List[PublicReference] = Field(default_factory=list)
    reference_count: int = 0
    truncated_references: bool = False
    # C-1: collection result cards are out of v1, but never silently dropped.
    # Content-free count so the page can disclose that something was withheld
    # at this turn (design §五 / §十). The judgement lives on the projection
    # side, not the renderer, so a legacy payload's format cannot mute it.
    omitted_result_sets: int = 0
    # T4 — answer-attached images as token-derived aliases (see PublicImage).
    # Absent list when the deployment stores no images (MINERU_RETURN_IMAGES).
    images: List[PublicImage] = Field(default_factory=list)


class PublicConversation(BaseModel):
    """A shared conversation (multi-turn Q&A), readable without a session (T3).

    ``shared_at`` is the read watermark — "内容截至何时" — so the page can state
    that new turns after it are not included."""

    title: str = ""
    created_at: str = ""
    shared_at: str = ""
    turns: List[PublicTurn] = Field(default_factory=list)
    truncated_turns: bool = False


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


# How many hits `GET /notebooks/{id}/search` returns, and — the same number, on
# purpose — the per-leg SQL LIMIT each adapter uses.
#
# One definition rather than the two literals each adapter used to spell, because
# the legs are ordered (Notebook/Domain → Source → Element → Knowledge) and the
# response is truncated to this cap at the end.  That makes a leg whose turn comes
# up with the cap already filled contribute nothing observable, so both adapters
# skip its query outright.  The skip is only sound while the per-leg limit and the
# response cap are the same value; splitting them back into separate literals
# would let one drift and silently turn the skip into dropped hits.
SEARCH_HIT_CAP = 20


class KgSearchHit(BaseModel):
    object_id: str
    name: str
    object_type: str
    score: float
    match: str


class KgSearchResponse(BaseModel):
    query: str
    hits: List[KgSearchHit]


class FeedbackRequest(BaseModel):
    rating: str
    comment: str = ""


class FeedbackResponse(BaseModel):
    id: str
    answer_id: str
    rating: str
    comment: str = ""
