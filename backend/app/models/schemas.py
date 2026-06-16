from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    memory_mode: str = "manual"
    domain_focus: List[str] = Field(default_factory=list)


class Evidence(BaseModel):
    source_id: str
    source_title: str
    element_id: str
    element_type: str
    location_label: str
    quoted_span: str
    confidence: float


class SourceElement(BaseModel):
    id: str
    source_id: str
    element_type: str
    location_label: str
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SourceSummary(BaseModel):
    id: str
    notebook_id: str
    title: str
    type: str
    status: str
    summary: str
    element_count: int
    file_name: str = ""
    file_size: int = 0
    file_hash: str = ""
    parse_status: str = ""
    created_label: str = ""
    doc_type: str = ""  # "" = auto-detect; else an extraction profile id
    # Non-empty when the latest KG extraction had network-failed windows that
    # silently contributed zero nodes (degraded run, not a clean "completed").
    extraction_warning: Optional[str] = None


class SourceImportFile(BaseModel):
    file_name: str
    file_size: int = 0
    mime_type: str = ""
    doc_type: str = ""


class SourceImportRequest(BaseModel):
    files: List[SourceImportFile]


class SourceDetail(SourceSummary):
    file_path: str = ""
    error_message: str = ""


class NotebookCreate(BaseModel):
    name: str = "Untitled notebook"
    purpose: str = ""
    primary_domain: str = "Semiconductor"
    target_users: str = ""
    expected_questions: List[str] = Field(default_factory=list)
    source_types: List[str] = Field(default_factory=list)
    taxonomy: List[str] = Field(default_factory=list)
    access_scope: str = ""
    template: str = ""  # optional template id to apply preset defaults (§6.2)


class NotebookUpdate(BaseModel):
    name: Optional[str] = None
    purpose: Optional[str] = None
    primary_domain: Optional[str] = None
    status: Optional[str] = None
    target_users: Optional[str] = None
    expected_questions: Optional[List[str]] = None
    source_types: Optional[List[str]] = None
    taxonomy: Optional[List[str]] = None
    access_scope: Optional[str] = None


class NotebookSummary(BaseModel):
    id: str
    name: str
    purpose: str
    primary_domain: str
    status: str
    counts: Dict[str, int]
    created_label: str = ""
    target_users: str = ""
    expected_questions: List[str] = Field(default_factory=list)
    source_types: List[str] = Field(default_factory=list)
    taxonomy: List[str] = Field(default_factory=list)
    access_scope: str = ""
    # Two-tier federation: 'base' = authoritative reference KG (analog textbook),
    # 'personal' = user notes (default). Drives tier-weighted relevance + conflict
    # precedence in ask().
    tier: str = "personal"


class NotebookTemplate(BaseModel):
    id: str
    label: str
    purpose: str = ""
    primary_domain: str = "Semiconductor"
    target_users: str = ""
    expected_questions: List[str] = Field(default_factory=list)
    source_types: List[str] = Field(default_factory=list)
    taxonomy: List[str] = Field(default_factory=list)


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


class Citation(BaseModel):
    label: str
    source_id: str
    element_id: str
    location_label: str
    quoted_span: str


class TraceStep(BaseModel):
    """推理模式 agent 的一步轨迹(供前端折叠展示)。"""
    step_type: str            # plan | retrieve | reflect | expand | fallback | answer | skip
    summary: str              # 人话摘要
    detail: Dict[str, Any] = Field(default_factory=dict)


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


class ConversationDetail(ConversationSummary):
    turns: List[ConversationTurn] = Field(default_factory=list)


class ArticleCreate(BaseModel):
    title: str
    abstract: str = ""
    source_id: str = ""


class ArticleSummary(BaseModel):
    id: str
    notebook_id: str
    source_id: str = ""
    title: str
    status: str
    summary: str


class ArticleResearchBrief(BaseModel):
    article: ArticleSummary
    core_contribution: str
    claims: List[str]
    limitations: List[str]
    notebook_relationships: List[str]
    derived_rule_candidates: List[str]
    validation_plan: List[str]
    citations: List[Citation]


class SearchHit(BaseModel):
    scope: str
    notebook_id: str
    label: str
    text: str
    source_id: str = ""
    element_id: str = ""


class NotebookSearchResponse(BaseModel):
    query: str
    hits: List[SearchHit]



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


class SetTierRequest(BaseModel):
    """Payload for POST /notebooks/{id}/tier."""
    tier: str   # "base" | "personal"


class DetectDocTypeItem(BaseModel):
    """One file's name + leading text sample for upload-time type detection."""
    name: str
    sample: str = ""


class DetectDocTypesRequest(BaseModel):
    """Payload for POST /detect-doc-types (batched, one request for many files)."""
    items: List[DetectDocTypeItem] = Field(default_factory=list)


class DetectedDocType(BaseModel):
    """Detection result per file; doc_type_id '' means undetected (-> auto)."""
    name: str
    doc_type_id: str


class DuplicateGroup(BaseModel):
    object_type: str
    similarity: float
    members: List[KnowledgeRef]



class MergeRequest(BaseModel):
    into_id: str


class DerivedRuleCandidate(BaseModel):
    id: str
    notebook_id: str
    article_id: str = ""
    title: str
    proposed_rule: str
    rationale: str = ""
    status: str
    evidence: List[Evidence] = Field(default_factory=list)
    created_label: str = ""


class FeedbackRequest(BaseModel):
    rating: str
    comment: str = ""


class FeedbackResponse(BaseModel):
    id: str
    answer_id: str
    rating: str
    comment: str = ""


class NotebookAnalytics(BaseModel):
    answers_total: int = 0
    feedback_useful: int = 0
    feedback_not_useful: int = 0
    usefulness_rate: float = 0.0
    low_rated_questions: List[str] = Field(default_factory=list)
    knowledge_counts: Dict[str, int] = Field(default_factory=dict)
    source_status_counts: Dict[str, int] = Field(default_factory=dict)


class UnifiedKgStatus(BaseModel):
    dirty: bool
    last_rebuild_at: str = ""
    objects: int = 0
    relations: int = 0
    clusters: int = 0


class MergeReviewRequest(BaseModel):
    limit: int = 50
    auto_confirm_threshold: float = 0.95


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


class PromotionApproveResult(BaseModel):
    candidate_id: str
    base_object_id: str
    merged_into: str = ""   # non-empty if deduped into an existing base object


class PromotionRejectRequest(BaseModel):
    reason: str = ""


class ConceptWhitelistEntry(BaseModel):
    term: str
    note: str = ""
    created_at: str = ""


class ConceptWhitelistAdd(BaseModel):
    term: str
    note: str = ""
