from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.model_safety import (
    infer_model_error_service,
    safe_model_error_code,
    safe_model_error_service,
    safe_model_error_stage,
    safe_model_label,
)
from app.services.memory_inputs import (
    normalize_content,
    normalize_reason,
    normalize_tags,
    normalize_title,
)


class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    username: str = ""
    memory_mode: str = "manual"
    domain_focus: List[str] = Field(default_factory=list)


MemoryOrigin = Literal["ask_answer", "external_agent"]
MemoryStatus = Literal["candidate", "confirmed", "rejected", "deprecated"]
MemoryPromotionState = Literal["none", "proposed", "approved", "rejected"]


class MemoryRecord(BaseModel):
    id: str
    notebook_id: str
    created_by: str
    agent_profile_id: Optional[str] = None
    source_answer_id: Optional[str] = None
    origin: MemoryOrigin
    status: MemoryStatus
    promotion_state: MemoryPromotionState = "none"
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    embedding_status: str = "pending"
    embedding_error: str = ""
    created_at: str
    updated_at: str
    provenance: Dict[str, Any] = Field(default_factory=dict)


class MemoryHit(BaseModel):
    """A retrieval projection with relevance and authority kept separate."""

    memory_id: str
    title: str
    text: str
    status: Literal["candidate", "confirmed"]
    authority: int
    score: float
    provenance: Dict[str, Any] = Field(default_factory=dict)

    @property
    def relevance(self) -> float:
        return self.score

    @property
    def object_id(self) -> str:
        return self.memory_id


class MemoryNotebookOption(BaseModel):
    notebook_id: str
    name: str
    memory_count: int
    pending_count: int


class PaginatedMemories(BaseModel):
    items: List[MemoryRecord]
    total_count: int
    offset: int
    limit: int
    owner_total_count: int = 0
    owner_pending_count: int = 0
    notebook_options: List[MemoryNotebookOption] = Field(default_factory=list)
    # Notebook-scoped listing only (the gate needs a single notebook to
    # evaluate); the cross-notebook user-level listing leaves this None.
    kg_extract_eligible: Optional[bool] = None


class MemoryPreview(BaseModel):
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    provenance_summary: Dict[str, Any] = Field(default_factory=dict)
    kg_extract_eligible: bool = False


class MemoryCreateFromAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_id: str
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    extract_kg: bool = True

    _normalize_title = field_validator("title")(normalize_title)
    _normalize_content = field_validator("content_md")(normalize_content)
    _normalize_tags = field_validator("tags")(normalize_tags)


class AnswerMemoryLinksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_ids: List[str] = Field(default_factory=list, max_length=200)


class AnswerMemoryLinksResponse(BaseModel):
    links: Dict[str, str] = Field(default_factory=dict)


class MemoryBulkDeleteRequest(BaseModel):
    memory_ids: List[str] = Field(default_factory=list, max_length=200)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    content_md: Optional[str] = None
    tags: Optional[List[str]] = None

    @field_validator("title")
    @classmethod
    def _normalize_optional_title(cls, value):
        return normalize_title(value) if value is not None else None

    @field_validator("content_md")
    @classmethod
    def _normalize_optional_content(cls, value):
        return normalize_content(value) if value is not None else None

    @field_validator("tags")
    @classmethod
    def _normalize_optional_tags(cls, value):
        return normalize_tags(value) if value is not None else None


class MemoryReviewRequest(MemoryUpdate):
    reason: Optional[str] = None
    extract_kg: Optional[bool] = None

    @field_validator("reason")
    @classmethod
    def _normalize_optional_reason(cls, value):
        return normalize_reason(value) if value is not None else None


class MemoryTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_ids: List[str] = Field(..., min_length=1, max_length=200)
    target_notebook_id: str
    mode: Literal["copy", "move"]
    extract_kg: bool = True


class AgentProfile(BaseModel):
    id: str
    owner_id: str
    name: str
    description: str = ""
    status: Literal["active", "revoked"] = "active"
    created_at: str
    updated_at: str


class AgentProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class AgentProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal["active", "revoked"]] = None


class AgentTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_profile_id: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None


class AgentTokenSummary(BaseModel):
    id: str
    agent_profile_id: str
    profile_name: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None
    created_at: str


class AgentTokenIssued(BaseModel):
    id: str
    token: str
    agent_profile_id: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    created_at: str


class AgentPrincipal(BaseModel):
    profile_id: str
    profile_name: str
    owner_id: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    token_id: str


class AuthRequest(BaseModel):
    username: str
    password: str


class AuthResult(BaseModel):
    token: str
    user: UserProfile


class Evidence(BaseModel):
    source_id: str
    source_title: str
    element_id: str
    element_type: str
    location_label: str
    quoted_span: str
    confidence: float


class PaperAuthor(BaseModel):
    name: str
    affiliation: str = ""  # 多机构以 "; " 连接;接地校验不过则为空


class PaperMeta(BaseModel):
    """论文元数据(接地校验后)。非论文源/未抽取时整个对象缺省。"""
    is_paper: bool = False
    title: Optional[str] = None
    venue: Optional[str] = None
    year: Optional[int] = None
    doi: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    authors: List[PaperAuthor] = Field(default_factory=list)


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
    source_url: str = ""  # 非空表示这是「在线 URL」来源，由 mineru.net 云端解析
    parse_status: str = ""
    created_label: str = ""
    doc_type: str = ""  # "" = auto-detect; else an extraction profile id
    # Non-empty when the latest KG extraction had network-failed windows that
    # silently contributed zero nodes (degraded run, not a clean "completed").
    extraction_warning: Optional[str] = None
    # 该 source 是否已抽取 KG / 已入图
    kg_extracted: bool = False
    # 论文元数据投影:作者姓名按署名序;非论文/未抽取为空(paper-metadata)。
    authors: List[str] = Field(default_factory=list)
    pub_year: Optional[int] = None
    venue: Optional[str] = None
    # 派生态(零新查询,见 SourceStore._paper_meta_status_for):
    # "has_meta"=已判定是论文且有元数据 | "not_paper"=已判定非论文(标记行)
    # | "missing"=合规候选但尚未跑过论文元数据抽取 | None=不适用(memory/knowhow/
    # 非论文 doc_type/未解析完成)。
    paper_meta_status: Optional[str] = None


class PaginatedSources(BaseModel):
    items: List[SourceSummary]
    total_count: int
    offset: int
    limit: int


class SourceImportFile(BaseModel):
    file_name: str
    file_size: int = 0
    mime_type: str = ""
    doc_type: str = ""


class SourceImportRequest(BaseModel):
    files: List[SourceImportFile]


class AddUrlSourcesRequest(BaseModel):
    urls: List[str]


class RejectedUrl(BaseModel):
    url: str
    reason: str


class AddUrlSourcesResult(BaseModel):
    created: List[SourceSummary]
    rejected: List[RejectedUrl]


class SourceDetail(SourceSummary):
    file_path: str = ""
    error_message: str = ""
    paper_meta: Optional[PaperMeta] = None


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
    # Notebook lifecycle states (notably the internal ``copying`` sentinel)
    # are repository-owned and must never be writable through the public API.
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    purpose: Optional[str] = None
    primary_domain: Optional[str] = None
    target_users: Optional[str] = None
    expected_questions: Optional[List[str]] = None
    source_types: Optional[List[str]] = None
    taxonomy: Optional[List[str]] = None
    access_scope: Optional[str] = None


class KgBuildJobStatus(BaseModel):
    job_id: str
    mode: Literal["incremental", "rebuild"]
    status: Literal["running", "succeeded", "failed"]
    stage: Literal["probing", "extracting", "stopping", "finished"]
    total_sources: int = 0
    completed_sources: int = 0
    failed_sources: int = 0
    error_code: str = ""
    user_message: str = ""
    updated_at: str = ""


class NotebookRef(BaseModel):
    """轻量 notebook 引用 —— 参考库挂载相关接口共用。"""
    id: str
    name: str
    tier: str = "personal"


class MountedBase(NotebookRef):
    """一条挂载边。active=False 表示边还在但当前不生效(被挂库易主 / 公共库被降级),
    前端须置灰并说明,不能假装它还在工作。"""
    active: bool = True
    inactive_reason: str = ""


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
    # 该 notebook 是否已构建知识图谱（有任意 knowledge_objects）。
    # 驱动前端严格推理(reasoning/graph)的可用门控。
    kg_ready: bool = False
    # 该 notebook 此刻是否正在构建/重抽 KG（进程内内存标志，get_notebook 实时回填）。
    # 前端刷新/切库后据此把「构建中…」进度接回（后台 daemon 线程本就在跑）。
    kg_building: bool = False
    # 最近一次 KG 构建任务的持久化状态；仅单笔记本详情投影，列表保持 None
    # 以避免逐笔记本附加查询。
    kg_build: Optional[KgBuildJobStatus] = None
    # 该 notebook 此刻是否正在跑论文元数据补抽（进程内内存标志，get_notebook 实时
    # 回填，镜像 kg_building 的 wiring）。重启即 False——补抽本身幂等可重触发。
    paper_meta_backfilling: bool = False
    # 本 notebook 挂载的参考库中是否有任一已建 KG。即便本 notebook 无图,挂了有图的
    # 参考库也可进行严格推理(reasoning/graph)。前端门控:requiresKg → (kg_ready 或
    # base_kg_available)。未挂载 → False。
    base_kg_available: bool = False
    # 本 notebook 挂载的参考库列表(0..N)。基准库不再全局唯一,也不再隐式参与检索。
    base_notebooks: List[NotebookRef] = Field(default_factory=list)
    # 已解析但尚未抽取 KG 的 source 数,驱动前端「补抽 N 篇」
    kg_pending_sources: int = 0
    # Phase 2 只读共享:本用户对该库的访问权。"owner" = 自有(可写);
    # "reader" = 经只读共享加入(仅读)。默认 owner 向后兼容。
    access: str = "owner"
    # reader 时 = 原 owner 的用户名(前端展示「来自 X」);owner 时空串。
    shared_from: str = ""
    # owner 视角:本 notebook 是否已开启分享(存在有效 share_token 或 notebook_members)。
    # 驱动前端卡片右下角的「已分享」小人徽标(仿 NotebookLM);reader 看到的原库 is_shared
    # 也为 True,但 reader 卡片本身已带「来自 X」不再重复标记。
    is_shared: bool = False


class ShareResponse(BaseModel):
    share_token: str
    copyable: bool
    size: Dict[str, int]


class SharedPreview(BaseModel):
    name: str
    owner_display: str
    source_count: int
    node_count: int
    edge_count: int
    source_titles: List[str]
    mode: str
    size: Dict[str, int]


class SharedByMeItem(BaseModel):
    id: str
    name: str
    share_token: str
    mode: str                          # "copy" | "readonly"
    size: Dict[str, int]
    members: List[Dict[str, str]]      # [{username, added_at}];仅 readonly 有值


class ReportCreate(BaseModel):
    question: str
    history: str = ""
    depth: int = 2
    auto_generate: bool = False


class ReportOutlineUpdate(BaseModel):
    sections: List[dict] = Field(default_factory=list)


class ReportGenerateRequest(BaseModel):
    depth: int | None = None


class ReportSummary(BaseModel):
    id: str
    question: str
    status: str
    progress: str = ""
    section_count: int = 0
    created_at: str = ""
    created_by: str = ""


class ReportExportRequest(BaseModel):
    report_ids: List[str] = Field(default_factory=list)


class ReportDetail(ReportSummary):
    outline: List[dict] = Field(default_factory=list)
    sections: List[dict] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    references: List[dict] = Field(default_factory=list)
    depth: int = 2
    section_status: List[dict] = Field(default_factory=list)
    content_md: str = ""
    error: str = ""


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
    service: str = "llm"
    stage: str       # "embed" | "rerank" | "answer" | "rewrite"
    model: str = ""
    message: str

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_service(cls, value: object) -> object:
        if isinstance(value, dict) and "service" not in value:
            value = {**value, "service": infer_model_error_service(value.get("stage"))}
        return value

    @field_validator("service", mode="before")
    @classmethod
    def validate_service(cls, value: object) -> str:
        return safe_model_error_service(value)

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


class SetTierRequest(BaseModel):
    """Payload for POST /notebooks/{id}/tier."""
    tier: str   # "base" | "personal"


class SetBasesRequest(BaseModel):
    """全量替换本 notebook 的参考库挂载集合。空数组 = 取消全部挂载。"""
    base_notebook_ids: List[str] = Field(default_factory=list)


class MountedByCount(BaseModel):
    """有多少笔记本正在把本 notebook 挂为参考库(不论边当前是否生效)——删除确认
    弹窗专用(spec §6):CASCADE 会连同这些边一起清空且不可撤销,用户点删除前必须
    看到影响面。"""
    count: int = 0


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
    paper_meta_counts: Dict[str, int] = Field(default_factory=dict)


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
    # Deprecated compatibility field. Raw diagnostics stay in server logs.
    error: str = ""
    # 失败原因的稳定枚举。200 响应挂不上 X-User-Message 头,所以出处由 schema 承载;
    # 这里刻意存 **code 而不是中文文案**——文案归前端(vocabulary.ts 的
    # MODEL_TEST_ERROR),这样它才落在界面词汇守卫的作用域里。后端存中文会绕开那道
    # 守卫,「缺少 base_url / model / api_key」这种把字段名甩给用户的文案正是这么漏的。
    # 取值:unknown_service | missing_config | upstream_error(未知 code 前端走兜底)。
    code: str = ""


class ModelServiceStatusItem(BaseModel):
    service: str
    model: str = ""
    source: str = "none"
    kind: str = "llm"
    configured: bool = False
    required: bool = False
    status: str = "unconfigured"
    latency_ms: int = 0
    checked_at: str = ""
    trigger: str = ""
    code: str = ""


class ModelServicesStatus(BaseModel):
    services: List[ModelServiceStatusItem] = Field(default_factory=list)


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
