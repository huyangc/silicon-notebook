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


class SourceImportFile(BaseModel):
    file_name: str
    file_size: int = 0
    mime_type: str = ""


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


class AskResponse(BaseModel):
    answer_id: str = ""
    conclusion: str
    applicable_scenario: List[str]
    recommended_methods: List[str]
    related_rules: List[RuleCard]
    potential_risks: List[str]
    related_cases: List[CaseCard]
    checklist: List[str]
    missing_information: List[str]
    citations: List[Citation]
    llm_mode: str


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
