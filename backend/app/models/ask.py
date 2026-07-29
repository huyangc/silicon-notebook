from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.ask_retrieval_policy import (
    DEFAULT_RETRIEVAL_EFFORT,
    ResultScope,
    RetrievalEffort,
)

from app.core.model_safety import (
    safe_model_display_name,
    safe_model_error_code,
    safe_model_metadata_id,
    safe_model_error_stage,
    safe_model_label,
    safe_model_support_id,
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


class AskIntentPreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=200)


class AskIntentConfirmation(BaseModel):
    contract: QueryIntentContract
    resolved_question: str = Field(min_length=1, max_length=4000)
    answers: List[QueryIntentAnswer] = Field(default_factory=list, max_length=8)
    # Wall-clock of the understanding phase, measured by the client: it runs in
    # /ask/intent, before any durable job exists, so the server cannot time it.
    # Reported back only so the persisted trace keeps that phase; it never feeds
    # retrieval. Bounded so a bad client cannot inflate a run's reported total.
    understanding_ms: Optional[int] = Field(default=None, ge=0, le=3_600_000)


class AskRequest(BaseModel):
    question: str
    # Browser-captured submission instant. It is display metadata only and
    # never participates in ordering, authorization, retrieval, or scheduling.
    asked_at: str = Field(default="", max_length=64)
    scenario: Dict[str, str] = Field(default_factory=dict)
    conversation_id: Optional[str] = None
    mode: str = "chunk"       # "chunk"(默认,通用问答) | "fast"(旧KG) | "reasoning" | "graph" | "global"
    # User-controlled resource level.  It selects immutable hard ceilings from
    # ask_retrieval_policy; the model may stop early but cannot increase them.
    retrieval_effort: RetrievalEffort = DEFAULT_RETRIEVAL_EFFORT
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
    result_sets: List[StructuredKnowhowResult] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    result_coverage: Optional[StructuredBatchCoverage] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # 严格推理(reasoning/graph)无可用 KG(本 notebook 无图且无可用 base)时 True。
    kg_required: bool = False
    # 大库(not copyable)且完全无 scale 索引(从未建过)时 True:检索能力受限,
    # 驱动前端渲染「构建索引」提示。「建过但有 delta」不置此位(既有「N 源待索引」
    # 徽章覆盖那种最终一致态)。
    index_required: bool = False
    model_errors: List[ModelError] = Field(default_factory=list)


class ConversationRenameRequest(BaseModel):
    title: str


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


class FeedbackRequest(BaseModel):
    rating: str
    comment: str = ""


class FeedbackResponse(BaseModel):
    id: str
    answer_id: str
    rating: str
    comment: str = ""
