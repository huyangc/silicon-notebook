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


class CaseCard(BaseModel):
    id: str
    symptom: str
    context: str
    root_cause: str
    resolution: str
    lesson_learned: str
    evidence: List[Evidence]


class Citation(BaseModel):
    label: str
    source_id: str
    element_id: str
    location_label: str
    quoted_span: str


class AskRequest(BaseModel):
    question: str
    scenario: Dict[str, str] = Field(default_factory=dict)
    conversation_id: Optional[str] = None


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


class AskResponse(BaseModel):
    answer_id: str = ""
    conclusion: str
    answer: str = ""
    grounded: bool = False
    anchors: List[AnswerAnchor] = Field(default_factory=list)
    related_knowledge: List["KnowledgeRecord"] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    llm_mode: str = ""
    conversation_id: str = ""


class ConversationSummary(BaseModel):
    id: str
    notebook_id: str
    title: str = ""
    updated_at: str = ""
    turn_count: int = 0


class ConversationTurn(BaseModel):
    answer_id: str
    question: str
    response: AskResponse
    created_at: str = ""


class ConversationDetail(ConversationSummary):
    turns: List[ConversationTurn] = Field(default_factory=list)


class ScenarioQueryRequest(BaseModel):
    domain: str = ""
    block_type: str = ""
    design_stage: str = ""
    package_type: str = ""
    signal_type: str = ""
    concern: str = ""
    constraint: str = ""
    process_or_node: str = ""
    application: str = ""


class ChecklistRequest(BaseModel):
    scenario: str


class ChecklistItem(BaseModel):
    question: str
    severity: str
    required_evidence: str
    related_rule_ids: List[str]
    citations: List[Citation]


class CaseSearchRequest(BaseModel):
    query: str
    context: Dict[str, str] = Field(default_factory=dict)


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


class ExtractionCandidate(BaseModel):
    id: str
    extraction_run_id: str
    notebook_id: str
    source_id: str = ""
    candidate_type: str
    status: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)


class Candidate(BaseModel):
    id: str
    notebook_id: str
    source_id: str = ""
    source_title: str = ""
    candidate_type: str
    status: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)
    created_label: str = ""


class CandidateUpdate(BaseModel):
    payload: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


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


class DuplicateGroup(BaseModel):
    object_type: str
    similarity: float
    members: List[KnowledgeRef]


class ConflictPair(BaseModel):
    object_type: str
    reason: str
    a: KnowledgeRef
    b: KnowledgeRef


class MergeRequest(BaseModel):
    into_id: str


class MethodCard(BaseModel):
    id: str
    name: str
    use_when: str
    benefit: str
    limitation: str
    status: str
    evidence: List[Evidence] = Field(default_factory=list)


class RiskItemCard(BaseModel):
    id: str
    title: str
    description: str
    severity: str
    status: str
    evidence: List[Evidence] = Field(default_factory=list)


class GlossaryTermCard(BaseModel):
    id: str
    term: str
    definition: str
    status: str
    evidence: List[Evidence] = Field(default_factory=list)


class ArticleClaimCard(BaseModel):
    id: str
    article_id: str
    statement: str
    claim_type: str
    relation_type: str = ""
    related_rule_id: str = ""
    implication: str = ""
    evidence: List[Evidence] = Field(default_factory=list)


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


class RuleExplanation(BaseModel):
    rule: RuleCard
    origin: List[Citation] = Field(default_factory=list)
    applicable_scenario: List[str] = Field(default_factory=list)
    exception: str = ""
    related_cases: List[CaseCard] = Field(default_factory=list)
    related_risks: List[RiskItemCard] = Field(default_factory=list)
    related_checklist: List[str] = Field(default_factory=list)
    # Neighbors derived from the rule's explicit relation fields (edges layer).
    related_knowledge: List[KnowledgeRef] = Field(default_factory=list)


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
    candidate_counts: Dict[str, int] = Field(default_factory=dict)
    knowledge_counts: Dict[str, int] = Field(default_factory=dict)
    source_status_counts: Dict[str, int] = Field(default_factory=dict)
