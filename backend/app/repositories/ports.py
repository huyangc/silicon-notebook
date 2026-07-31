"""Consumer-owned repository contracts.

The concrete SQLite facade remains the compatibility implementation.  These
protocols intentionally contain no business logic and are structural seams for
the later store/service extraction.
"""
from __future__ import annotations
import threading
import queue

from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Literal,
    Optional,
    Protocol,
    Sequence,
    TypedDict,
    runtime_checkable,
)

if TYPE_CHECKING:
    import numpy as np
    from app.services.ask_modes import AskMode
    from app.services.kg.scale_index import ScaleIndex
    from app.services.retrieval_candidates import ChunkRetrievalPlan

from app.core.config import Settings
from app.models.identity import (
    AgentPrincipal,
    AgentProfile,
    AgentTokenIssued,
    AgentTokenSummary,
    UserProfile,
)
from app.models.memory import MemoryHit, MemoryRecord, MemoryUpdate, PaginatedMemories
from app.models.notebooks import (
    NotebookAnalytics,
    NotebookCreate,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
)
from app.models.sources import (
    AddUrlSourcesResult,
    PaginatedSources,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UploadedSourceSummary,
)
from app.models.ask import (
    AnswerAnchor, AskRequest, AskResponse, Citation, ConversationDetail,
    ConversationBulkDeleteResult, ConversationSummary, FeedbackRequest,
    FeedbackResponse, NotebookSearchResponse, QueryIntentContract, RuleCard,
)
from app.models.knowledge import (
    DuplicateGroup, KnowledgeGraph, KnowledgeTypeCount, KnowledgeUpdate, MergeRequest,
    ObjectSchemaCreate, ObjectSchemaModel, ObjectSchemaUpdate, PaginatedKnowledge,
)
from app.services.cancellation import CancelEvent
from app.services.notebook_scale import NotebookScaleFacts
from app.services.retrieval import (
    GapRelationRow, RetrievedChunk, RetrievedElement, RetrievedKnowledge,
    RetrievedRelation, W_KEYWORD, W_SEMANTIC,
)
from app.services.kg.follow_chain import FollowChainResult


KNOWHOW_COLUMN_KINDS = frozenset({"anchor", "procedure", "entity", "attribute"})


@dataclass
class UploadedSourceFile:
    file_name: str
    content_type: str
    content: bytes
    doc_type: str = ""
    # 用户是否在 UI 里**显式**设过这一项的文档类型下拉框（改成某类型、或选回「自动检测」）。
    # 只有显式时，reuse 路径才允许改/重置既有源的类型；auto-detect 的建议值、以及不发此
    # 信号的调用方（老前端、batch_ingest）都视为「没表态」、绝不动既有源的类型。缺省 False。
    doc_type_explicit: bool = False


@dataclass(frozen=True)
class PreparedAskTurn:
    """The durable conversation id and history prepared for one Ask turn."""

    conversation_id: str
    history: str


class ConversationBusyError(RuntimeError):
    """Explicit deletion cannot remove a conversation used by a running Ask."""

    def __init__(self) -> None:
        super().__init__("conversation has a running Ask job")


class KgBuildAlreadyRunning(RuntimeError):
    """One notebook already has a durable running KG build job."""


@dataclass(frozen=True)
class ChunkWrite:
    id: str
    text: str
    section_path: str
    element_ids: tuple


@dataclass(frozen=True)
class SourceElementWrite:
    id: str
    element_type: str
    location_label: str
    text: str
    metadata: Mapping[str, Any]


# Distinguishes an omitted source-paper metadata value from a deliberate None.
# It is neutral because both persistence adapters must preserve this API shape.
SOURCE_PAPER_META_UNSET = object()


SourceScheduler = Callable[[str], None]
ExtractionProgress = Callable[[int, int, str, bool], None]
RebuildProgress = Callable[[str, int, int], None]
EmbeddingProgress = Callable[[int, int], None]
IndexStageProgress = Callable[[str, int], None]
RepositoryRow = Mapping[str, object]
EncodedVectorUpdate = tuple[bytes, str, str]
VectorBatchEncoder = Callable[[Sequence[RepositoryRow]], Sequence[EncodedVectorUpdate]]


@runtime_checkable
class RepositoryDatabasePort(Protocol):
    """Opaque backend database boundary.

    Store operations receive their backend-owned transaction handles directly;
    this port intentionally does not pretend that SQLite and PostgreSQL share
    a connection API.
    """

    def resolve_path(self, value: str) -> Path: ...
    def connect(self) -> object: ...
    def write(self) -> object: ...
    def close(self) -> None: ...


class RepositorySeams(Protocol):
    """Compatibility callbacks supplied by the backend-neutral facade."""

    new_id: Callable[[str], str]
    now: Callable[[], str]
    copy_chunk_size: Callable[[], int]
    remap_json_ids: Callable[[Any, dict], Any]
    in_chunk_size: Callable[[], int]


class KGBuildResult(TypedDict):
    built: list[str]
    failed: list[str]
    skipped: list[str]


class ScaleBuildManifest(TypedDict, total=False):
    n_nodes: int


class JsonChatClientPort(Protocol):
    model: str

    @property
    def configured(self) -> bool: ...

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        response_schema_hint: str,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        temperature: float = 1.0,
        top_p: float = 1.0,
        max_tokens: Optional[int] = None,
        cancel_event: CancelEvent = None,
        bypass_cache: bool = False,
        response_validator: Optional[Callable[[str], bool]] = None,
    ) -> str: ...


class RerankClientPort(Protocol):
    @property
    def configured(self) -> bool: ...

    def rerank(
        self,
        query: str,
        documents: List[str],
        on_error: Callable[[Exception], None] | None = None,
    ) -> list[int]: ...


class DirectedGraphPort(Protocol):
    def __getitem__(self, index: int) -> dict[str, object]: ...
    def successor_indices(self, index: int) -> Sequence[int]: ...
    def get_edge_data(self, source: int, target: int) -> dict[str, object]: ...


class IdentityRepository(Protocol):
    def current_user(self) -> UserProfile: ...
    def create_user(self, username: str, password: str) -> UserProfile: ...
    def authenticate_user(self, username: str, password: str) -> UserProfile | None: ...
    def create_session(self, user_id: str) -> str: ...
    def resolve_session(self, token: str) -> UserProfile | None: ...
    def delete_session(self, token: str) -> None: ...
    def audit_labels_for_user_ids(self, user_ids: Sequence[str]) -> dict[str, str]: ...
    def set_user_role(self, actor_id: str, user_id: str, role: str) -> dict[str, str]: ...
    def global_document_limit_default(self) -> int: ...
    def set_global_document_limit_default(self, actor_id: str, value: int) -> dict[str, int]: ...
    def set_user_document_limit_override(
        self, actor_id: str, user_id: str, value: int | None
    ) -> dict: ...


@runtime_checkable
class ModelStatusStorePort(Protocol):
    def get_all(self) -> dict[str, dict[str, object]]: ...

    def record(
        self,
        *,
        service_id: str,
        config_fingerprint: str,
        status: Literal["ok", "error"],
        latency_ms: int,
        code: str,
        trigger: Literal["manual_test", "observed_failure", "recovery_probe"],
        support_id: str,
        checked_at: str,
    ) -> None: ...


@runtime_checkable
class KnowhowHistoryStorePort(Protocol):
    def head_seq(self, table_id: str) -> int: ...
    def list_changes(
        self, table_id: str, limit: int = 50, before_seq: int | None = None
    ) -> list[dict]: ...
    def history_page(
        self, table_id: str, limit: int = 50, before_seq: int | None = None
    ) -> dict: ...
    def get_change(self, table_id: str, seq: int) -> dict | None: ...
    def changes_between(
        self, table_id: str, from_seq: int, to_seq: int
    ) -> list[dict]: ...
    def cell_history(
        self, table_id: str, row_id: str, column_id: str, limit: int = 50
    ) -> list[dict]: ...
    def revert_to(
        self, table_id: str, target_seq: int, expected_head_seq: int,
        actor: str = "",
    ) -> dict: ...
    def create_milestone(
        self, table_id: str, seq: int, name: str, note: str, created_by: str
    ) -> dict: ...
    def delete_milestone(self, table_id: str, milestone_id: str) -> None: ...
    def list_milestones(self, table_id: str) -> list[dict]: ...
    def prune(self, table_id: str, before_iso: str) -> dict: ...


@runtime_checkable
class IdentityStorePort(IdentityRepository, Protocol):
    """Identity-store surface used by backend-neutral composition."""

    @staticmethod
    def _user_profile(user: object, profile: object) -> UserProfile: ...
    def user_document_limit_override(self, user_id: str) -> "int | None": ...
    def effective_document_limit(self, user_id: "str | None") -> int: ...
    def notebook_owner(self, notebook_id: str) -> "tuple[str | None, str | None]": ...


class NotebookAccessRepository(Protocol):
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def is_member(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_source(self, source_id: str, user_id: str) -> bool: ...
    def source_owner(self, source_id: str) -> str | None: ...
    def conversation_owner(self, conversation_id: str) -> str | None: ...
    def answer_owner(self, answer_id: str) -> str | None: ...
    def user_can_read_answer(self, answer_id: str, user_id: str) -> bool: ...


class MemoryRepository(Protocol):
    def create_agent_profile(
        self, owner_id: str, name: str, description: str = ""
    ) -> AgentProfile: ...
    def list_agent_profiles(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentProfile]: ...
    def update_agent_profile(
        self, profile_id: str, owner_id: str, patch: Mapping[str, Any] | Any
    ) -> AgentProfile: ...
    def issue_agent_token(
        self, owner_id: str, agent_profile_id: str, scopes: Sequence[str],
        default_notebook_id: str, notebook_ids: Sequence[str],
        expires_at: str | None,
    ) -> AgentTokenIssued: ...
    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]: ...
    def revoke_agent_token(
        self, owner_id: str, token_id: str
    ) -> AgentTokenSummary: ...
    def resolve_agent_token(self, raw_token: str) -> AgentPrincipal | None: ...
    def refresh_agent_principal(self, token_id: str) -> AgentPrincipal | None: ...
    def require_agent_access(
        self, principal: AgentPrincipal, scope: str, notebook_id: str
    ) -> None: ...
    def create_memory_candidate(
        self, notebook_id: str, user_id: str, agent_profile_id: str | None,
        client_request_id: str, title: str, content_md: str, tags: Sequence[str],
        reason: str, task_context: Mapping[str, Any] | None = None,
        evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    ) -> MemoryRecord: ...
    def create_memory_from_answer(
        self, notebook_id: str, user_id: str, answer_id: str, title: str,
        content_md: str, tags: Sequence[str], extract_kg: bool = True,
    ) -> MemoryRecord: ...
    def memory_kg_eligible(self, notebook_id: str) -> bool: ...
    def update_memory(
        self, memory_id: str, user_id: str, patch: MemoryUpdate
    ) -> MemoryRecord: ...
    def confirm_memory(
        self, memory_id: str, user_id: str, patch: MemoryUpdate | None = None
    ) -> MemoryRecord: ...
    def reject_memory(self, memory_id: str, user_id: str) -> MemoryRecord: ...
    def deprecate_memory(self, memory_id: str, user_id: str) -> MemoryRecord: ...
    def delete_memory(self, memory_id: str, user_id: str) -> None: ...
    def bulk_delete_memories(
        self, user_id: str, memory_ids: Sequence[str]
    ) -> int: ...
    def get_memory(self, memory_id: str, user_id: str) -> MemoryRecord: ...
    def list_memories(
        self, user_id: str, notebook_id: str | None = None,
        status: str | None = None, origin: str | None = None, query: str = "",
        offset: int = 0, limit: int = 50,
    ) -> PaginatedMemories: ...
    def answer_memory_links(
        self, notebook_id: str, user_id: str, answer_ids: Sequence[str]
    ) -> dict[str, str]: ...
    def memory_revisions(self, memory_id: str, user_id: str) -> list[Any]: ...
    def propose_memory_promotion(
        self, memory_id: str, user_id: str, *, target_base_id: str = ""
    ) -> dict: ...
    def transfer_memories(
        self, user_id: str, memory_ids: list[str], target_notebook_id: str,
        mode: str, extract_kg: bool = True,
    ) -> list[dict]: ...


class NotebookCatalogRepository(Protocol):
    def list_notebook_templates(self) -> list[NotebookTemplate]: ...
    def list_notebooks(self) -> list[NotebookSummary]: ...
    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary: ...
    def get_notebook(self, notebook_id: str) -> NotebookSummary: ...
    def update_notebook(self, notebook_id: str, payload: NotebookUpdate) -> NotebookSummary: ...
    def delete_notebook(self, notebook_id: str) -> None: ...
    def mark_notebook_base(self, notebook_id: str) -> None: ...
    def set_notebook_personal(self, notebook_id: str) -> None: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse: ...


class NotebookSharingRepository(Protocol):
    def share_notebook(self, notebook_id: str) -> dict: ...
    def unshare_notebook(self, notebook_id: str) -> None: ...
    def find_notebook_by_share_token(self, token: str) -> str | None: ...
    def notebook_copy_stats(self, notebook_id: str) -> dict: ...
    def shared_preview(self, notebook_id: str) -> dict: ...
    def shared_by_me(self, user_id: str) -> list: ...
    def copy_notebook(self, source_notebook_id: str, *, new_owner_id: str, actor_label: str | None = None, new_name: str | None = None) -> NotebookSummary: ...
    def add_member(self, notebook_id: str, user_id: str) -> None: ...
    def remove_member(self, notebook_id: str, user_id: str) -> None: ...
    def kick_all_members(self, notebook_id: str) -> None: ...
    def list_members(self, notebook_id: str) -> list: ...
    def join_shared(self, notebook_id: str, user_id: str) -> NotebookSummary: ...
    def leave_notebook(self, notebook_id: str, user_id: str) -> None: ...


class SourceRepository(Protocol):
    def list_sources(self, notebook_id: str) -> list[SourceSummary]: ...
    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = "") -> PaginatedSources: ...
    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> list[SourceSummary]: ...
    def add_url_sources(self, notebook_id: str, urls: Iterable[str], scheduler: SourceScheduler | None = None, capacity: "int | None" = None) -> AddUrlSourcesResult: ...
    def upload_sources(self, notebook_id: str, files: Iterable[UploadedSourceFile], scheduler: SourceScheduler | None = None) -> list[UploadedSourceSummary]: ...
    def get_source(self, source_id: str) -> SourceDetail: ...
    def process_source(self, source_id: str) -> SourceSummary: ...
    def parse_source(self, source_id: str) -> SourceSummary: ...
    def source_elements(self, source_id: str) -> list[SourceElement]: ...
    def delete_source(self, source_id: str) -> None: ...
    def extract_source(self, source_id: str) -> None: ...


class KnowledgeReadRepository(Protocol):
    def knowledge_types(self, notebook_id: str) -> list[KnowledgeTypeCount]: ...
    def list_knowledge(self, notebook_id: str, object_type: str, status: str | None = None, offset: int = 0, limit: int = 50) -> PaginatedKnowledge: ...
    def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph: ...
    def kg_search(self, notebook_id: str, q: str, k: int = 30) -> list: ...
    def unified_graph(self, notebook_id: str, level: str = "concept", limit: int | None = None) -> dict: ...
    def concept_detail(self, notebook_id: str, canonical_id: str, *, source_notebook_id: str = "") -> dict: ...
    def node_context(self, notebook_id: str, object_id: str, *, source_notebook_id: str = "") -> dict: ...
    def kg_neighbors(self, notebook_id: str, object_id: str, cap: int = 50, *, source_notebook_id: str = "") -> dict: ...


class SchemaRegistryRepository(Protocol):
    def list_object_schemas(self) -> list[ObjectSchemaModel]: ...
    def create_object_schema(self, payload: ObjectSchemaCreate) -> ObjectSchemaModel: ...
    def update_object_schema(self, object_type: str, payload: ObjectSchemaUpdate) -> ObjectSchemaModel: ...
    def delete_object_schema(self, object_type: str) -> None: ...
    def propose_schemas(self, notebook_id: str) -> list[ObjectSchemaModel]: ...


class KnowledgeGovernanceRepository(Protocol):
    def update_knowledge(self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate) -> RuleCard: ...
    def find_duplicates(self, notebook_id: str, object_type: str) -> list[DuplicateGroup]: ...
    def merge_knowledge(self, notebook_id: str, source_id: str, payload: MergeRequest) -> RuleCard: ...
    def review_queue(self, notebook_id: str, limit: int = 200) -> list[dict]: ...
    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None: ...
    def pending_merges(self, notebook_id: str) -> list[dict]: ...
    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None: ...
    def reject_merge(self, notebook_id: str, candidate_id: str) -> None: ...
    def pending_conflicts(self, notebook_id: str) -> list[dict]: ...
    def resolve_notebook_conflicts(self, notebook_id: str) -> dict: ...
    def confirm_conflict(self, notebook_id: str, candidate_id: str) -> dict: ...
    def reject_conflict(self, notebook_id: str, candidate_id: str) -> None: ...
    def concept_whitelist_list(self) -> list[dict]: ...
    def concept_whitelist_add(self, term: str, note: str = "") -> dict: ...
    def concept_whitelist_remove(self, term: str) -> None: ...
    def review_pending_merges(self, notebook_id: str, limit: int = 50, confirm_threshold: float | None = None, separate_threshold: float | None = None) -> dict: ...
    def merge_review_job_status(self, notebook_id: str) -> dict: ...
    def run_merge_review_job(self, notebook_id: str, *, batch: int = 100) -> dict: ...
    def propose_promotion(self, notebook_id: str, object_id: str, *, target_base_id: str = "") -> dict: ...
    def list_promotion_queue(self, status_filter: str | None = None) -> list[dict]: ...
    def approve_promotion(self, candidate_id: str) -> dict: ...
    def approve_promotion_as_reviewer(
        self, candidate_id: str, reviewer_id: str
    ) -> dict: ...
    def reject_promotion(self, candidate_id: str, reason: str = "") -> dict: ...
    def reject_promotion_as_reviewer(
        self, candidate_id: str, reason: str, reviewer_id: str
    ) -> dict: ...


class KgMutationPort(Protocol):
    """The KG mutation side-effect coordinator's contract (Task 14).

    Every online KG write funnels its post-commit side effects through this
    port in the frozen per-operation order (mutation_phases.json); deep-copy,
    migration/recovery/seed and streaming-ask writes are exempt and never call
    it. ``mark_unified_kg_dirty`` is the ONLY entry that advances
    kg_mutation_seq; ``bump_cluster_mutation_seq`` runs inside the caller's
    open write transaction (cluster write + bump commit atomically) and never
    touches kg_mutation_seq (rebuild keeps it stable for idempotency).
    """

    def invalidate_unified_cache(self, notebook_id: str) -> None: ...
    def mark_unified_kg_dirty(self, notebook_id: str) -> None: ...
    def bump_cluster_mutation_seq(self, connection: object, notebook_id: str) -> None: ...


class KnowledgeLifecycleRepository(Protocol):
    def prepare_notebook_kg_job(
        self,
        notebook_id: str,
        mode: str,
        *,
        target_limit: int | None = None,
        retry_partial: bool = False,
    ) -> dict: ...
    def fail_notebook_kg_job_submission(self, job_id: str) -> bool: ...
    def execute_notebook_kg_job(
        self,
        notebook_id: str,
        job_id: str,
        mode: str,
        *,
        progress: Callable | None = None,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize: Callable[[dict], dict | None] | None = None,
    ) -> dict: ...
    def build_notebook_kg(
        self,
        notebook_id: str,
        *,
        progress: Callable | None = None,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize: Callable[[dict], dict | None] | None = None,
    ) -> dict: ...
    def rebuild_notebook_kg(self, notebook_id: str) -> dict: ...
    def relink_notebook_kg(self, notebook_id: str) -> dict: ...
    def rebuild_unified_kg(self, notebook_id: str, progress: Callable[[str, int, int], None] | None = None, force: bool = False) -> int: ...
    def unified_kg_status(self, notebook_id: str) -> dict: ...


class IndexLifecycleRepository(Protocol):
    def trigger_scale_index_rebuild(self, notebook_id: str, when: str = "now", mode: str = "auto") -> dict: ...
    def cancel_scale_index(self, notebook_id: str) -> dict: ...
    def scale_index_status(self, notebook_id: str) -> dict: ...
    def index_status(self, notebook_id: str) -> dict: ...


class AskStateRepository(Protocol):
    def begin_ask_job(self, notebook_id: str, payload: AskRequest, mode: str, cancel_event: threading.Event) -> tuple[str, str]: ...
    def finish_ask_job(self, job_id: str, status: str, *, answer_id: str = "", error: str = "") -> None: ...
    def cancel_ask_job(self, job_id: str, user_id: str) -> dict: ...
    def ask_job_status(self, job_id: str) -> dict: ...
    def append_ask_trace(self, job_id: str, step: dict) -> None: ...
    def ask_job_detail(self, job_id: str) -> dict: ...
    def list_conversations(self, notebook_id: str) -> list[ConversationSummary]: ...
    def get_conversation(self, conversation_id: str) -> ConversationDetail: ...
    def rename_conversation(self, conversation_id: str, title: str) -> None: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int
    ) -> ConversationBulkDeleteResult: ...
    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse: ...


class ReportRepository(Protocol):
    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str: ...
    def update_report(self, notebook_id: str, report_id: str, *, status=None, progress=None, error=None, outline=None, sections=None, gaps=None, references=None, content_md=None, section_status=None, understanding=None) -> None: ...
    def claim_report_intent(self, notebook_id: str, report_id: str, understanding: dict) -> bool: ...
    def claim_report_generation(self, notebook_id: str, report_id: str) -> bool: ...
    def get_report(self, notebook_id: str, report_id: str) -> dict: ...
    def list_reports(self, notebook_id: str) -> list: ...
    def delete_report(self, notebook_id: str, report_id: str) -> None: ...
    def export_reports(self, notebook_id: str, report_ids: list) -> list: ...


class AdminQueryRepository(Protocol):
    def list_user_usage(self) -> list[dict[str, Any]]: ...
    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def pending_actions_projection_rows(self, user_id: str) -> dict: ...
    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse: ...
    def load_notebook_scale_facts(self, notebook_id: str) -> NotebookScaleFacts: ...


class AskExecutionPort(Protocol):
    def preview_reasoning_intent(
        self, question: str, history: str = "", cancel_event: CancelEvent = None
    ) -> QueryIntentContract: ...
    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse: ...
    def ask_chunk(self, notebook_id: str, payload: AskRequest, cancel_event: CancelEvent = None) -> AskResponse: ...
    def ask_reasoning(self, notebook_id: str, payload: AskRequest, on_trace: Callable[[Any], None] | None = None, cancel_event: CancelEvent = None) -> AskResponse: ...
    def ask_graph(self, notebook_id: str, payload: AskRequest, seed_ids: list[str] | None = None, cancel_event: CancelEvent = None) -> AskResponse: ...


class McpMemoryRepository(
    MemoryRepository,
    NotebookAccessRepository,
    NotebookCatalogRepository,
    KnowledgeLifecycleRepository,
    AskExecutionPort,
    Protocol,
):
    """Consumer-owned MCP contract over existing composed services."""

    def agent_memory_hits(
        self,
        user_id: str,
        notebook_id: str,
        query: str,
        include_candidates: bool,
        limit: int = 8,
    ) -> list[MemoryHit]: ...


@runtime_checkable
class NotebookStorePort(Protocol):
    def tier_map(self, notebook_ids: Sequence[str]) -> dict[str, str]: ...
    def participant_notebook_ids(self, active_notebook_id: str) -> list[str]: ...
    @staticmethod
    def participant_ids(db: object, active_notebook_id: str) -> list[str]: ...
    @staticmethod
    def participant_rows(db: object, active_notebook_id: str) -> tuple[Any, list[Any]]: ...
    @staticmethod
    def participant_tiers(db: object, active_notebook_id: str) -> tuple[list[str], dict[str, str]]: ...
    def list_mount_edges_for_notebook(self, notebook_id: str) -> list[dict]: ...
    def mounted_by_count_for_notebook(self, notebook_id: str) -> int: ...
    def mountable_for_notebook(self, notebook_id: str) -> list[dict]: ...
    def replace_mounts(self, notebook_id: str, base_notebook_ids: Sequence[str], created_by: str) -> None: ...
    def meta_for_notebook(self, notebook_id: str) -> dict | None: ...
    def apply_meta_for_notebook(self, notebook_id: str, *, guard_name: str, name: str, purpose: str) -> None: ...
    def tier(self, notebook_id: str) -> str: ...


@runtime_checkable
class KgBuildJobStorePort(Protocol):
    def create_job(
        self,
        notebook_id: str,
        created_by: str,
        mode: str,
        total_sources: int,
    ) -> dict: ...
    def get(self, job_id: str) -> dict: ...
    def latest(self, notebook_id: str) -> dict | None: ...
    def latest_on(
        self, db: object, notebook_id: str
    ) -> dict | None: ...
    def set_stage(
        self,
        job_id: str,
        stage: str,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> bool: ...
    def record_source_result(
        self, job_id: str, *, succeeded: bool
    ) -> bool: ...
    def finish(
        self,
        job_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> bool: ...
    def fail_submission(self, job_id: str) -> bool: ...


@runtime_checkable
class SourceStorePort(Protocol):
    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...
    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...
    @staticmethod
    def retrieval_element_rows(db: object, notebook_id: str) -> list[Any]: ...
    def source_titles(self, source_ids: list[str]) -> dict[str, str]: ...
    def get_source(self, source_id: str) -> SourceDetail: ...
    @staticmethod
    def source_exists_tx(connection: object, source_id: str) -> bool: ...
    @staticmethod
    def source_exists_for_update_tx(connection: object, source_id: str) -> bool: ...
    def source_elements(self, source_id: str) -> list[SourceElement]: ...
    def source_change_signal_rows(
        self, db: object, notebook_id: str
    ) -> list[tuple[str, str, str, bool]]:
        """``[(source_id, change_signal, created_at_key, user_visible)]`` for
        every physical source row EXCEPT the notebook's private Memory synthetic
        rows.

        ``change_signal`` is an OPAQUE backend-formatted token: equal tokens
        mean "this source's ``source_elements`` cannot have changed since the
        token was taken".  Adapters build it from the source row alone so the
        whole notebook costs ONE query; the collection catalog keys its
        per-source element-count cache on it (see
        ``app.services.collection_catalog``).  Never parse it, never compare
        it across backends or processes — only for equality against a token
        taken from the same store.

        ``created_at_key`` is a SORT key, not a timestamp to display or to
        compare across backends: text that orders the notebook's sources the
        way ``list_sources`` orders them (``ORDER BY created_at, id``), so the
        sources collection can walk the roster in the order the source tab
        shows it.  Adapters normalize it (PostgreSQL hands back ``datetime``
        where SQLite hands back the stored text) such that lexicographic
        comparison within one backend equals that backend's own SQL ordering.
        It rides along on a row the query already visits — the column is in the
        same row as the three signal columns, so carrying it costs nothing —
        and it is deliberately NOT part of the change signal: a source's
        creation time never changes, so folding it into the token would only
        make the token wider.

        ``user_visible`` says whether the USER-FACING source list shows this row
        — i.e. each adapter evaluates its own visible-source predicate (the one
        ``list_sources`` / ``list_sources_page`` / ``visible_document_count``
        share) as a projected column.  The sources collection is defined as "what
        the source tab lists", so it must consume that predicate rather than a
        second spelling of it, and it must NOT pay a separate query to learn it:
        nothing indexes ``source_type``, so a "which ids are hidden" query has to
        scan every source row of the notebook — on the request path, right after
        this query walked the same rows.  Evaluated in the projection it is free.
        Like ``created_at_key`` it stays out of the change signal: a source's
        type does not change, and widening the token would invalidate every
        cached count for a reason that cannot affect any of them.

        The Memory exclusion is part of the contract, not an adapter detail: a
        typed-collection listing is scoped to a notebook's participants and has
        no owner filter, so leaving Memory in would let any member of a shared
        notebook read another member's confirmed Memory through its formulas /
        tables / images / code blocks.  ``memory_source_ids`` returns exactly
        the rows this one drops.
        """
        ...
    def memory_source_ids(self, db: object, notebook_id: str) -> list[str]:
        """The notebook's private Memory synthetic source ids — the exact
        complement of ``source_change_signal_rows``' exclusion, and the single
        definition of "which sources are Memory" that the services layer
        consumes.

        Bounded by the notebook's Memory count (``idx_sources_memory_id``
        allows one derived source per Memory) and index-seeked on
        ``notebook_id``.  The KG enumeration path needs the ids rather than a
        predicate because ``knowledge_objects`` carries no source type: it
        filters the rows it reads against this set, and subtracts the objects
        those sources own from the enumeration denominator.
        """
        ...
    def element_type_count_rows(
        self, db: object, source_ids: Sequence[str], element_types: Sequence[str]
    ) -> list[tuple[str, str, int]]:
        """``[(source_id, element_type, count)]`` for the requested sources,
        restricted to the requested element types.

        Bounded and index-assisted by contract: the query shape is
        ``WHERE source_id IN (...) AND element_type IN (...)
        GROUP BY source_id, element_type``, which
        ``idx_source_elements_source_type`` (source_id, element_type,
        created_at, id) covers — SQLite resolves it as a covering index search
        on both key columns.  PostgreSQL's planner keeps the choice (an index
        or bitmap scan by selectivity); what the contract fixes is that the
        type restriction stays IN THE QUERY, never a post-filter in Python,
        so a source's prose elements are never read to count its formulas.
        Both id lists are deduplicated and batched by the adapter, so callers
        may pass a whole notebook's source list; types absent from a source
        simply do not appear in the result.
        """
        ...
    def element_page_rows(
        self,
        db: object,
        source_id: str,
        element_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> list[Any]:
        """One keyset page of ONE source's elements of ONE type, ordered by
        ``(created_at, id)`` ascending and bounded by ``limit``.

        ``after`` is the ``(created_at, id)`` pair of the last row the caller
        consumed, or ``None`` for the first page; ``created_at`` must be the
        value THIS store handed back (SQLite text / PostgreSQL ``datetime``),
        never a reformatted one — the cursor is opaque and process-local for
        exactly that reason (see ``app.services.collection_enumeration``).

        Index path is part of the contract: ``idx_source_elements_source_type``
        (source_id, element_type, created_at, id) makes this an equality seek
        on the first two columns plus a range on the rest, so the page cost is
        O(limit) regardless of how many prose elements the source holds and no
        ORDER BY sort is materialized.  Rows carry id / source_id /
        element_type / location_label / text / created_at / asset_id.

        ``asset_id`` is projected out of ``metadata`` IN SQL, never by
        selecting the column and picking a key in Python: a table element's
        metadata holds its full rendered HTML, so selecting it would move
        megabytes across a page of tables to read one short id.  The row is
        visited either way; what the projection saves is transfer and memory.
        """
        ...
    def source_display_rows(
        self, db: object, source_ids: Sequence[str]
    ) -> list[Any]:
        """``id / notebook_id / title / file_name / is_paper / paper_title``
        for the given sources, on the CALLER's connection.

        ``notebook_id`` rides along because an explicitly requested source has
        to be proven in-scope, and doing that here is one primary-key read
        instead of re-listing every notebook's sources to look for the id.

        A bounded primary-key lookup (plus the 1:1 ``source_paper_meta``
        outer join) used to label enumerated items.  The narrow twin of
        ``source_listing_rows``, which adds summary/doc_type; a per-element
        walk needs neither, and one label window covers up to 256 sources —
        carrying a summary for each of them would move a page of prose to
        render a page of titles.
        """
        ...
    def source_listing_rows(
        self, db: object, source_ids: Sequence[str]
    ) -> list[Any]:
        """``id / notebook_id / title / file_name / summary / doc_type /
        source_type / is_paper / paper_title`` for the given sources, on the
        CALLER's connection.

        The projection the SOURCE-CARD shape needs: the sources collection
        (design doc §6.2) lists a document by display title, document type and
        its already-stored summary, and ``source_metadata`` — which wants the
        identical columns — is implemented on top of this method so the two can
        never drift into two spellings of one projection.  The difference
        between them is only the connection: this one runs on the caller's, so
        an enumeration keeps its whole walk on a single connection.

        Bounded primary-key lookup plus the 1:1 ``source_paper_meta`` outer
        join, batched by the adapter.  Callers page it (one window per
        enumeration page); it must never be handed a whole library's ids just
        because it can batch them.
        """
        ...
    def source_from_row(self, db: object, row: object, *, paper_meta: object = SOURCE_PAPER_META_UNSET) -> SourceSummary: ...
    def sources_from_rows(self, db: object, rows: list[object]) -> list[SourceSummary]: ...
    def extraction_warning(self, db: object, source_id: str) -> str | None: ...
    def meta_sources(self, notebook_id: str, pending_source_id: str = "") -> list[dict]: ...
    def get_paper_meta(self, source_id: str) -> dict | None: ...
    def sources_missing_paper_meta(self, notebook_id: str, include_existing: bool = False) -> list[str]: ...
    def visible_document_count(self, notebook_id: str) -> int: ...


@runtime_checkable
class ChunkStorePort(Protocol):
    @staticmethod
    def language_probe_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def retrieval_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def count_row(db: object, notebook_id: str) -> Any: ...
    @staticmethod
    def hydrate_rows(db: object, chunk_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def graph_hydrate_rows(db: object, chunk_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def id_element_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def rows_by_ids(db: object, chunk_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def id_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def knowhow_chunk_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def knowhow_bridge_version_row(db: object, notebook_id: str) -> Any: ...
    @staticmethod
    def chunks_by_section(db: object, notebook_id: str, source_id: str, section_path: str, limit: int) -> list[Any]: ...


@runtime_checkable
class EmbeddingStorePort(Protocol):
    def bind_write(self, write: Callable) -> None: ...
    @staticmethod
    def embedded_object_ids(db: object, notebook_id: str) -> set[str]: ...
    @staticmethod
    def version_row(db: object, notebook_id: str, table: str) -> Any: ...
    @staticmethod
    def vector_rows(db: object, notebook_id: str, table: str, id_col: str) -> list[Any]: ...
    @staticmethod
    def vector_rows_for_ids(db: object, notebook_id: str, table: str, id_col: str, ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def relation_delta_rows(db: object, notebook_id: str, source_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def knowledge_delta_rows(db: object, notebook_id: str, source_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def element_delta_rows(db: object, notebook_id: str, source_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def chunk_delta_rows(db: object, notebook_id: str, source_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def rows_by_ids(db: object, table: str, id_col: str, ids: Sequence[str]) -> list[Any]: ...


@runtime_checkable
class KnowledgeStorePort(Protocol):
    @staticmethod
    def legacy_typed_table_ids(
        connection: object, object_types: Sequence[str], id_prefix: str
    ) -> list[str]: ...
    @staticmethod
    def active_object_count(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def community_context_rows(db: object, notebook_id: str, members: object) -> list[Any]: ...
    @staticmethod
    def delete_notebook_graph_rows(db: object, notebook_id: str) -> None: ...
    @staticmethod
    def embedding_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def embedding_rows_for_objects(db: object, notebook_id: str, object_ids: object) -> list[Any]: ...
    @staticmethod
    def fts_search(db: object, notebook_id: str, q: str, k: int = 30) -> list[dict]: ...
    @staticmethod
    def incremental_object_rows(db: object, notebook_id: str, source_id: str, object_type: str, *, exclude_source: bool = False) -> list[Any]: ...
    @staticmethod
    def insert_kg_fts_rows(connection: object, rows: object) -> None: ...
    @staticmethod
    def insert_object_chunk(connection: object, rows: object) -> None: ...
    @staticmethod
    def insert_object_source_rows(connection: object, rows: object) -> None: ...
    @staticmethod
    def insert_relation_chunk(connection: object, rows: object) -> None: ...
    @staticmethod
    def completion_generation_is_current(
        connection: object, notebook_id: str, source_id: str, run_id: str
    ) -> bool: ...
    @staticmethod
    def completion_validate_scope(
        connection: object, notebook_id: str, source_id: str, run_id: str,
        object_ids: Sequence[str], element_ids: Sequence[str]
    ) -> bool: ...
    @staticmethod
    def completion_existing_keys(
        connection: object, notebook_id: str, object_ids: Sequence[str]
    ) -> set[tuple[str, str, str]]: ...
    @staticmethod
    def completion_page(
        connection: object, notebook_id: str, source_id: str, run_id: str,
        mode: str, schema_version: int, reasoning_edge_types: Sequence[str],
        edge_contract_rows: Sequence[tuple[str, str, str]],
        known_edge_types: Sequence[str], core_node_types: Sequence[str],
        limit: int, now: str
    ) -> dict: ...
    @staticmethod
    def completion_candidate_rows(
        connection: object, notebook_id: str, source_id: str,
        object_ids: Sequence[str]
    ) -> list[Any]: ...
    @staticmethod
    def completion_element_rows(
        connection: object, source_id: str, element_ids: Sequence[str]
    ) -> list[Any]: ...
    @staticmethod
    def completion_pending_states(
        connection: object, after_source_id: str, after_mode: str, limit: int
    ) -> list[Any]: ...
    @staticmethod
    def completion_mark_state_stale(
        connection: object, notebook_id: str, source_id: str,
        run_id: str, mode: str, now: str
    ) -> bool: ...
    @staticmethod
    def completion_transition_mode_state(
        connection: object, notebook_id: str, source_id: str, run_id: str,
        old_mode: str, new_mode: str, schema_version: int, now: str
    ) -> bool: ...
    @staticmethod
    def completion_advance_state(
        connection: object, notebook_id: str, source_id: str, run_id: str,
        mode: str, schema_version: int, expected_cursor: str,
        next_cursor: str, status: str, now: str
    ) -> bool: ...
    @staticmethod
    def insert_completion_relations(connection: object, rows: object) -> int: ...
    @staticmethod
    def neighbor_relation_rows(db: object, notebook_id: str, object_ids: object) -> list[Any]: ...
    @staticmethod
    def knowledge_object_page_rows(
        db: object,
        notebook_id: str,
        object_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> list[Any]:
        """One keyset page of one notebook's knowledge objects of ONE type,
        ordered by ``(created_at, id)`` ascending and bounded by ``limit``.

        RAW rows: this page carries NO usability predicate, and every row it
        returns carries its ``status`` so the caller can apply one.  That is
        deliberate.  ``idx_knowledge_objects_nb_type_created``
        (notebook_id, object_type, created_at, id) does not contain ``status``,
        so a status predicate in this query would be a residual filter over an
        unbounded number of visited index entries — on a notebook whose history
        is mostly deprecated objects, "one page" would stop being O(limit) and
        the engine would walk as far as it had to.  Keeping the query purely
        keyset-ordered makes the cost exactly ``limit`` rows, and the caller
        (``app.services.collection_enumeration``) filters with the SAME
        ``USABLE_STATUSES`` object the counting path uses, over-scans within an
        explicit ceiling, and reports an honest partial when that ceiling
        fires.  The predicate therefore still has exactly one definition; only
        the layer that evaluates it moved.

        The private-Memory exclusion travels the same road and for the same
        reason: ``source_id`` rides on every row, and the caller drops the
        objects whose source is in ``memory_source_ids`` inside that same
        ceiling.  A ``NOT EXISTS`` against ``sources`` here would be a second
        unindexed residual with the identical unbounded-skip hazard.

        ``after`` is the opaque ``(created_at, id)`` pair of the last consumed
        row (see ``element_page_rows``).  Rows carry id / object_type /
        source_id / payload / evidence / status / created_at.
        """
        ...
    @staticmethod
    def notebook_tier_row(db: object, notebook_id: str) -> Any: ...
    @staticmethod
    def object_meta_rows_for_notebook(db: object, notebook_id: str, object_ids: object) -> list[Any]: ...
    @staticmethod
    def relink_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def source_build_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def source_build_state_page(
        db: object,
        notebook_id: str,
        after_created_at: object | None,
        after_id: str,
        limit: int,
    ) -> list[Any]: ...
    @staticmethod
    def sources_with_elements(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def unified_graph_rows(db: object, notebook_id: str) -> object: ...
    @staticmethod
    def source_ids_from_evidence(evidence_json: str | None) -> set: ...
    @staticmethod
    def delete_object_sources(connection: object, object_ids: Sequence[str]) -> None: ...
    def usable_object_rows(
        self,
        notebook_id: str,
        object_ids: Sequence[str],
    ) -> list[dict[str, Any]]: ...
    def has_kg(self, notebook_id: str) -> bool: ...
    def any_mounted_has_kg_compat(self, notebook_id: str, db: object | None = None) -> bool: ...
    def begin_extraction(
        self,
        source_id: str,
        notebook_id: str,
        run_id: str,
        created_at: str,
        *,
        preserve_existing: bool = False,
    ) -> None: ...
    def finish_extraction(self, run_id: str, status: str, message: str) -> None: ...
    def add_relations_current(self, notebook_id: str, source_id: str, relations: list[dict]) -> int: ...
    def _element_texts(self, db: object, element_ids: object, *, with_ordinal: bool = False) -> object: ...
    def _enrich_evidence(self, db: object, evidence: object) -> object: ...
    def node_context(self, notebook_id: object, object_id: object, *, check_access: bool = True) -> object: ...
    @staticmethod
    def count_knowledge(db: object, notebook_id: str, object_type: str, statuses: object) -> int: ...
    @staticmethod
    def source_has_kg(db: object, source_id: str) -> bool: ...
    @classmethod
    def replace_object_sources(cls, connection: object, object_id: str, notebook_id: str, evidence_json: str | None) -> None: ...
    @staticmethod
    def source_index_backfilled(db: object, notebook_id: str) -> bool: ...
    def mark_source_index_backfilled(self, db: object, notebook_id: str) -> None: ...
    def stale_object_ids_for_source(self, db: object, source_id: str, notebook_id: str) -> list[str]: ...
    def clear_source_graph_state(self, db: object, source_id: str, notebook_id: str) -> None: ...
    def clear_source_extraction_state(self, db: object, source_id: str, notebook_id: str, *, clear_embeddings: bool) -> None: ...
    def retrieval_objects_compat(self, db: object, notebook_id: str, object_type: str, statuses: object, id_filter: object) -> list[dict]: ...
    @staticmethod
    def delete_relations_for_source(db: object, source_id: str) -> None: ...


@runtime_checkable
class EvidenceKnowledgeContextPort(Protocol):
    def cluster_map(self, notebook_id: str) -> dict[str, str]: ...
    def node_context(self, notebook_id: str, object_id: str) -> dict[str, Any]: ...
    def in_network_relations(
        self, participant_ids: Sequence[str], object_ids: Sequence[str]
    ) -> list[dict[str, Any]]: ...
    def relation_support_count(
        self, notebook_id: str, source_id: str, edge_type: str, target_id: str
    ) -> int: ...


class RetrievalKnowledgeStorePort(KnowledgeStorePort, Protocol):
    def fts_search(self, db: object, notebook_id: str, q: str, k: int = 30) -> list[dict[str, Any]]: ...
    def chunk_fts_search(self, db: object, notebook_id: str, q: str, k: int = 30) -> list[dict[str, Any]]: ...
    def chunk_exact_search(self, db: object, notebook_id: str, needle: str, k: int = 50) -> list[dict[str, Any]]: ...
    def retrieval_objects(self, db: object, notebook_id: str,
                          object_type: str, statuses: Iterable[str] | None,
                          id_filter: Iterable[str] | None, *,
                          batch_size: int = 900) -> list[dict[str, Any]]: ...
    def any_mounted_has_kg_on(self, db: object, notebook_id: str) -> bool: ...
    def object_version_row(self, db: object, notebook_id: str) -> Any: ...
    def relation_context_rows(self, db: object, notebook_id: str, relation_ids: Sequence[str] | None = None, *, batch_size: int = 900) -> list[Any]: ...
    def relation_id_rows_for_objects(self, db: object, notebook_id: str, object_ids: Sequence[str], limit: int, *, batch_size: int = 900) -> list[Any]: ...
    def relation_exists(self, db: object, notebook_id: str) -> bool: ...
    def relation_endpoint_rows(self, db: object, notebook_id: str, source_ids: Sequence[str] | None = None) -> list[Any]: ...
    def relation_connected_object_ids(self, db: object, notebook_id: str, object_ids: Sequence[str]) -> list[Any]: ...
    def neighbor_ids(self, db: object, notebook_id: str, object_id: str, *, endpoint: str, edge_type: str | None = None) -> list[Any]: ...
    def usable_object_rows_on(self, db: object, object_ids: Sequence[str], statuses: Sequence[str]) -> list[Any]: ...
    def graph_version_rows(self, db: object, notebook_id: str) -> tuple[Any, Any]: ...
    def graph_object_rows(self, db: object, notebook_id: str, statuses: Sequence[str]) -> list[Any]: ...
    def graph_relation_rows(self, db: object, notebook_id: str, *, include_id_evidence: bool = True) -> list[Any]: ...
    def object_evidence_rows(self, db: object, object_ids: Sequence[str]) -> list[Any]: ...
    def notebook_object_evidence_rows(self, db: object, notebook_id: str) -> list[Any]: ...
    def follow_start_row(self, db: object, object_id: str, active_notebook_id: str, statuses: Sequence[str]) -> Any: ...
    def follow_endpoint_rows(self, db: object, notebook_id: str, object_id: str, endpoint: str, limit: int) -> list[Any]: ...
    def follow_relation_evidence_rows(self, db: object, relation_ids: Sequence[str]) -> list[Any]: ...
    def follow_object_rows(self, db: object, notebook_id: str, object_ids: Sequence[str], statuses: Sequence[str]) -> list[Any]: ...
    def in_network_relation_rows(self, db: object, notebook_id: str, object_ids: Sequence[str]) -> list[Any]: ...


@runtime_checkable
class UnifiedKgStorePort(Protocol):
    @staticmethod
    def canonical_relation_seed_rows(db: object, notebook_id: str) -> object: ...
    @staticmethod
    def canonical_relations_count(db: object, notebook_id: str) -> int: ...
    def checkpoint_put(self, notebook_id: str, input_version: str, stage: str, rows: object, now: str) -> None: ...
    @staticmethod
    def clear_mention_bridge(db: object, notebook_id: str) -> None: ...
    @staticmethod
    def clear_scratch_run(db: object, notebook_id: str, run_id: str) -> None: ...
    @staticmethod
    def clear_canonical_scratch_run(db: object, notebook_id: str, run_id: str) -> None: ...
    @staticmethod
    def cluster_description_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def cluster_evidence_rows(db: object, notebook_id: str, run_id: str, seeds: object) -> list[Any]: ...
    @staticmethod
    def cluster_input_facts(db: object, notebook_id: str, *, exclude_emb_count: bool = False) -> object: ...
    @staticmethod
    def cluster_fold_rows(db: object, notebook_id: str, ids: list[str]) -> list[Any]: ...
    @staticmethod
    def communities_count(db: object, notebook_id: str, level: int) -> int: ...
    @staticmethod
    def community_graph_rows(db: object, notebook_id: str) -> object: ...
    @staticmethod
    def community_member_ids(db: object, notebook_id: str, level: int) -> list[Any]: ...
    @staticmethod
    def community_reports(db: object, notebook_id: str, level: int) -> list[Any]: ...
    @staticmethod
    def community_rows_for_summary(db: object, notebook_id: str, level: int) -> list[Any]: ...
    # ⚠ 与上面那条配对,而且**必须在发布事务内**调:`community_rows_for_summary` 是
    # 「只补账本」路径在**写事务之外**读回来的板块划分,而随后的预计算在生产上是分钟级
    # 的 —— 期间另一次 `force=True` 的重建可以把整套板块换掉。所以写产物之前要在**那个
    # 写事务里**问一句「我算的那套还在吗」,不在就整趟放弃(见
    # `KnowledgeLifecycleService.rebuild_communities` 的发布段)。
    # 查一行就够:`replace_communities` 对 (notebook_id, level) 是整表删再插、id 是 128
    # bit 新铸的,所以「任意一个旧 id 还在」⟺「期间没有任何一次 replace 提交过」。
    # 两侧的原子性来源刻意不同(parity 要的是语义等价):PostgreSQL 侧靠 `FOR SHARE` 行锁
    # (READ COMMITTED、无进程级锁,裸 SELECT 挡不住并发写者在查完与插完之间提交),
    # SQLite 侧靠 `SqliteDatabase.write()` 的进程级 `threading.Lock` + 文件写锁,既不需要
    # 也没有 `FOR SHARE` 这个语法。
    # 它与下面 `discard_board_dependent_kg_analysis_artifacts` 是同一条不变式的两半:
    # 那条守的是**本次自己重铸板块**时不留悬空产物(全量路径,同事务、构造上自洽),
    # 这条守的是**别人重铸了板块**时不写悬空产物(补账本路径,划分读自事务之外)。
    @staticmethod
    def board_partition_still_holds(db: object, notebook_id: str, level: int, board_id: str) -> bool: ...
    @staticmethod
    def concept_clusters_count(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def distinct_cluster_count(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def finish_rebuild_state(db: object, notebook_id: str, cluster_input_version: str, cluster_count: int, now: str) -> None: ...
    @staticmethod
    def insert_scratch_rows(db: object, rows: object) -> None: ...
    @staticmethod
    def insert_canonical_scratch_rows(db: object, rows: object) -> None: ...
    @staticmethod
    def graph_seq_row(db: object, notebook_id: str) -> tuple[int, int, int]:
        """O(1) single-row ``(kg_mutation_seq, cluster_mutation_seq,
        mention_seq)``; ``(0, 0, -1)`` when the notebook has no state row.

        Already the version key behind graph/PPR snapshot caching and the
        collection catalog's KG-count memo — declared here because it is a
        cross-backend read primitive both adapters implement identically, not
        a private helper of one of them.
        """
        ...
    @staticmethod
    def mention_edges_count(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def mention_seed_rows(db: object, notebook_id: str) -> object: ...
    @staticmethod
    def relation_endpoint_name_rows(db: object, notebook_id: str, relation_ids: list[str]) -> list[Any]: ...
    @staticmethod
    def replace_canonical_relations(db: object, notebook_id: str, rows: object, seq: int) -> None: ...
    @staticmethod
    def replace_communities(db: object, notebook_id: str, level: int, kept: object, names: object, deg: object, now: str) -> None: ...
    # ⚠ 必须与 `replace_communities` 在**同一个写事务**里调:板块 id 被重铸的那一刻,
    # `kg_community_edges` / `kg_source_profiles` 的每一行都成了悬空引用,而 T3 的
    # 记忆化签名(state 的 seq + 账本行的 seq/created_at)在 `force=True` 的同 seq
    # 重铸上一个字段都不会变。理由与「为什么只作废这两份」见
    # `app.services.kg_analysis_precompute.BOARD_DEPENDENT_ARTIFACT_KINDS`。
    # ⚠ 它只覆盖**本次自己重铸板块**那一档(全量路径:重铸与作废同事务,构造上自洽)。
    # 「板块被**别人**换掉」那一档由上面的 `board_partition_still_holds` 守 —— 补账本
    # 路径根本不调本方法(它不重铸板块),所以两条都要,少一条就漏一半。
    @staticmethod
    def discard_board_dependent_kg_analysis_artifacts(db: object, notebook_id: str) -> None: ...
    @staticmethod
    def replace_mention_bridge(db: object, notebook_id: str, edges: object, comention_rows: object, seq: int) -> None: ...
    @staticmethod
    def scratch_vector_rows(db: object, notebook_id: str, run_id: str) -> object: ...
    @staticmethod
    def seed_payload_rows(db: object, notebook_id: str, object_type: str) -> object: ...
    @staticmethod
    def set_community_seq(db: object, notebook_id: str, seq: int) -> None: ...
    @staticmethod
    def set_community_summary(db: object, community_id: str, title: str, summary: str, findings_json: str) -> None: ...
    @staticmethod
    def state_row(db: object, notebook_id: str) -> Any: ...
    @staticmethod
    def stream_seed_rows(db: object, notebook_id: str, object_type: str) -> object: ...
    @staticmethod
    def swap_cluster_map_from_scratch(
        db: object,
        notebook_id: str,
        object_type: str,
        run_id: str,
        created_at: str,
    ) -> None: ...
    @staticmethod
    def weak_support_relation_rows(
        db: object,
        notebook_id: str,
        canonical_ids: list[str],
        source_max: int,
        limit: int,
    ) -> list[Any]: ...
    def mention_alias_candidate_batches(
        self, claims: Sequence[tuple[str, str]], aliases: Sequence[str]
    ) -> ContextManager[Iterator[tuple[str, Iterator[tuple[str, str]]]]]: ...
    @staticmethod
    def cluster_version_row(db: object, notebook_id: str) -> Any: ...
    @staticmethod
    def cluster_member_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def ppr_version_rows(db: object, notebook_id: str) -> tuple[Any, Any, Any, Any, Any]: ...
    @staticmethod
    def mention_rows(db: object, notebook_id: str) -> list[Any]: ...
    def checkpoint_gc(self, notebook_id: str, input_version: str) -> None: ...
    def checkpoint_clear(self, notebook_id: str) -> None: ...
    def checkpoint_load(self, notebook_id: str, input_version: str, stage: str) -> dict[str, dict]: ...
    def checkpoint_put_current(self, notebook_id: str, input_version: str, stage: str, rows: list[tuple[str, dict]]) -> None: ...
    # ---------------------------------------- KG 质量分析的只读聚合(T1)
    # 与上面的 community-peer 原语同形:**自开只读连接**,绝不写库。
    #
    # ⚠ 调用契约(T2 起的唯一调用方是 KnowledgeLifecycleService._compute_kg_analysis,
    # 它整个跑在发布写事务之外):**调用方不得持有外层写事务**。
    # 实现两侧都自开连接——SQLite 侧是本线程复用的读连接,PostgreSQL 侧是**从池里另取
    # 一条**。从 `write()` 里调用会读到该事务提交前的旧数据,而且**不会响亮失败**:它
    # 会安静地返回一份过时的报告;SQLite 上还额外把**进程级写锁**按住一整趟全表扫。
    #
    # 这条契约**不再只是注释**:两侧实现的头一行都调
    # `_reject_inside_write_transaction()`,读 `database.in_write_transaction`
    # (SQLite = thread-local write_depth,PostgreSQL = _WRITE_ACTIVE ContextVar)并当场
    # 硬失败。必须是运行时守卫而不是形状断言:这几条查询一张产物表都不碰、在 SQLite 上
    # 还跑在另一条连接上,所以「哪个写事务碰过产物表」那类形状守卫对它们完全无效 ——
    # 评审用「把三条调用搬进 `_write()`」的移动变异实测过,25 条测试全绿。
    #
    # ⚠ **三条都是全表级的重活,一条都不能挂在在线请求路径上。** 别只盯着最后一条,
    # 也别以为 `largest_clusters` 因为有 LIMIT 就便宜 —— 三者**同一量级**:
    #   · cluster_size_histogram      本 notebook 的 concept_clusters 全扫 + 每行一次
    #                                 knowledge_objects 匹配 + 分组聚合(生产 200 万+ 簇行)
    #   · largest_clusters            同形状的扫描 + 分组,**之后还多一次排序**;LIMIT
    #                                 只截断输出,不减少扫描/排序的输入。它只扫 concept
    #                                 分片,所以在 claim 占多数的库上**绝对**耗时可以低于
    #                                 直方图,但**单位输入**的代价更高。总之不便宜。
    #   · relation_provenance_counts  knowledge_relations 全扫 + 两个端点匹配(生产 800 万+ 边)
    #
    # 代价量级**按后端分开标**,同一条查询在两侧可以差好几倍,别用一个数字盖住。
    # 本机实测(同一批合成数据:30 万对象 / 30 万簇行 / 60 万边,均为热态;
    # PostgreSQL 16 本机实例,SQLite 为同规模临时库):
    #
    #     查询                          SQLite      PostgreSQL 16
    #     cluster_size_histogram        300 ms      197 ms   (Hash Left Join + HashAggregate)
    #     largest_clusters(20)          178 ms      152 ms   (Hash Join + HashAgg + top-N heapsort)
    #     relation_provenance_counts   1039 ms      234 ms   (Parallel Hash Left Join ×2,2 worker)
    #
    #   · SQLite     nested-loop + 每行随机 PK 探查,没有并行。**热态读数不可线性外推**:
    #                同一批查询在本机 5.1GB 真实库上冷跑(全新进程、页缓存全冷)是
    #                直方图 1024 ms / 边出处 1165 ms,而暖态只要 64 ms / 217 ms —— 差
    #                6~16 倍,而那还只是 4.2 万簇行 / 5.2 万边。仓库有同形状的实测事故:
    #                relation_connected_object_ids 在 835 万边上**冷扫 39 分钟**,而那条
    #                查询还**没有**每行两次探查。437GB 的生产库上,这三条应按
    #                「与那个 39 分钟数据点同量级或更差」估,不要留任何秒级外推数字。
    #   · PostgreSQL 规划器对这三个形态都选 hash join;边出处那条还并行顺扫(实测 2 个
    #                worker),所以比 SQLite 快约 1.5×(簇查询)到 4.4×(边查询)。量级
    #                明显更好,但**仍是全表级**,冷态同样受随机 IO 支配 —— 依旧属于预计算。
    #                实测计划里两条簇查询的 HashAggregate 已经溢出到磁盘(Planned
    #                Partitions 4 / Disk Usage ~7MB @30 万行),规模再上去会放大。
    #                边查询的外层原本还落一次 external merge sort(规划器对那个巨大的
    #                CASE 表达式估不出基数),T2 已把它换成固定的 `count(*) FILTER` 单行
    #                聚合 —— CASE 保留(分桶语义是有序互斥的,拆成独立谓词必错),只砍掉
    #                外层 GROUP BY,那次排序整个消失。SQLite 侧刻意保持 GROUP BY 形态,
    #                好让「SQL 冒出契约外的桶名就炸」那条绊线至少在一个后端上原生活着;
    #                PG 侧用同一趟扫描里的一个 `min(bucket) FILTER (WHERE NOT …)` 把同一
    #                条绊线补回来。理由与守卫见两侧 `relation_provenance_counts` 的
    #                docstring(§3.35:允许形态分岔,但必须写明理由 + 两侧都有守卫)。
    #
    # ⚠ **`cluster_size_histogram` 与 `largest_clusters` 刻意不合并成一趟扫描 —— 量过的
    # 取舍,不是「T1 恰好写成两个方法」的副产品。**
    # 两者确实高度重叠:都是「扫 concept_clusters + 每行一次 knowledge_objects 主键探查 +
    # 按 canonical 分组」,榜单的输入是直方图内层分组的严格子集(只 concept),只多要一个
    # `canonical_name`。把 `MIN(NULLIF(canonical_name,''))` 加进直方图的内层分组,一趟就能
    # 导出两份产物。实测(本机真实库 nb-b37185f4ae,41713 簇行 / 37340 个 canonical;
    # 每次全新进程 = 全新 SQLite 页缓存,OS 页缓存仍暖,所以这是节省的**下界**):
    #
    #     现状(两趟)  直方图 79.5 / 70.0 / 62.8 + 榜单 12.5 / 12.3 / 11.9 = 92.0 / 82.3 / 74.7 ms
    #     合并(一趟)  物化 69.7 / 62.9 / 61.3 + 6.4 / 6.0 / 6.0 + 1.2 / 1.0 / 1.0 = 77.2 / 69.9 / 68.2 ms
    #     暖态(同连接三取最小)  62.5 vs 61.3 ms(nb-b37185f4ae)、53.3 vs **54.0** ms(nb-012fb94249)
    #     两种写法的榜单前 20 逐字相同(已比对)。
    #
    # 结论:**不合并。** 冷进程下省 8~16%、暖态是个平局(有一个库反而更慢),而代价是把
    # 内层分组物化成 O(簇数) 行的临时表:实测 37340 行 +8.8 MB RSS ≈ 236 B/行,线性外推到
    # 生产的约 200 万簇行是**约 470 MB**,而 SQLite 侧 `temp_store = MEMORY` 意味着它**不能
    # 落盘**。用 470 MB 不可落盘的常驻内存,去买五份产物里两份的 8~16% —— 而整个预计算的
    # 大头是 `relation_provenance_counts`(836 万边)与 `source_community_counts`(878 万
    # 对象)—— 在 #340/#342/#347/#351/#352/#354 那条 OOM 轨道盯着的同一个库上是明确的负收益。
    # 效率红线要求「新增前先问能不能合并」,这就是问过之后的答案:问了、量了、不合并。
    # (日后 SQLite 退役、只剩 PostgreSQL 时值得重估:PG 的 HashAggregate 本来就会溢盘,
    # 没有「不可落盘」这一条,取舍会翻过来。)
    #
    # 生产将迁移到 PostgreSQL,性能设计以 PG 为一等目标;两侧的 SQL 形态因此允许分岔
    # (`community_overview` 已经分了:PG 一条窗口函数,SQLite 逐板块有界 top-K)。
    # parity 要求的是**语义等价 + 两侧都有守卫**,不是 SQL 逐字相同。
    def cluster_size_histogram(self, notebook_id: str) -> dict[str, object]: ...
    def largest_clusters(self, notebook_id: str, limit: int = 20) -> dict[str, object]: ...
    # ⚠ `community_overview` 有**两个**入口,刻意的:
    #   · `community_overview`      自开一个多语句共享快照(手上没有读连接的调用方用);
    #   · `community_overview_on`   connection-taking,骑调用方的快照。
    # 后者是给 T3 的总览用的:那一趟要读 state 行、账本、板块列表、跨板块边**四样**,
    # 板块列表自开快照的话,并发的社区重建提交在中间就会把「上一代的新鲜度戳」与
    # 「新一代的板块 id」拼进同一份响应(codex 第 12 轮 P2)。查询本体只有一份,住在
    # `_on` 里;自开入口只负责那道 `_reject_inside_write_transaction` 与开快照。
    def community_overview(
        self, notebook_id: str, *, level: int = 0, limit: int = 50, top_k: int = 5
    ) -> dict[str, object]: ...
    @staticmethod
    def community_overview_on(
        db: object,
        notebook_id: str,
        *,
        level: int = 0,
        limit: int = 50,
        top_k: int = 5,
    ) -> dict[str, object]: ...
    def relation_provenance_counts(self, notebook_id: str) -> dict[str, object]: ...
    # ---------------------------------------- KG 质量分析的预计算产物(T2)
    # 与上面的只读聚合**相反**:这两个是 connection-taking 的,骑调用方
    # (KnowledgeLifecycleService.rebuild_communities)的事务边界。
    #
    # ⚠ `replace_kg_analysis_artifacts` 必须整个跑在**一个写事务**里(设计 §3.3);
    # 而且从 codex 第 13 轮起,那个事务同时是**板块自己的**发布事务:板块与全部产物
    # 一起可见,不再有「新板块 + 旧账本」的中间态(生产上那是分钟级的窗口)。
    # 一次预计算要么整批可见、要么完全不可见。允许「跨板块边是新的、来源画像是旧的」
    # 这种组合的话,报告里的数字会互相矛盾,而且矛盾得很隐蔽 —— 用户没有任何线索。
    # 账本行(`kg_analysis_artifacts`)带 `kg_mutation_seq`,让**每一份**产物自证建于
    # 哪个 KG 状态;它的**存在与否**才是「这份产物在不在」的判据,明细表的行数不是
    # (单一板块的图 legitimately 产出 0 条跨板块边)。
    #
    # ⚠ `source_canonical_rows` 是**重活**:本 notebook 的 knowledge_objects 全扫 +
    # 每行一次索引探查,与 `community_graph_rows` 同量级。它只能待在预计算路径上;
    # 返回游标,调用方**流式**折叠。
    # 它**刻意不 join `community_members`、也不 GROUP BY**:原子发布要求产物在板块
    # 落库**之前**就算得出来,而那一刻板块划分只在内存里。canonical → 板块 这一跳因此
    # 由 `kg_analysis_precompute.SourceBoardCounter` 做(结果与旧形态逐字相同 ——
    # 同 level 下 `community_members` 是一个划分,而内存里的 membership 就是它)。
    #
    # ⚠ 它的口径里有两条**排除**,两侧逐字一致:`source_id=''`(共享空来源,算进去就是
    # 伪造一个「空来源」的画像),以及隐藏合成来源 `source_type IN ('memory','knowhow')`
    # (产品其余各处一律当隐藏源,它们的标题是用户内容)。第二条必须写成
    # `NOT EXISTS (... AND s.source_type IN (...))` —— **孤儿引用**(source_id 指向已删
    # 来源)要留下,读侧靠 `kg_source_profile_page` 的 `source_missing` 把它报出来。
    # 写成 `JOIN sources` 或 `LEFT JOIN ... WHERE s.source_type NOT IN (...)` 都会把
    # 孤儿一起吞掉,把一个有意的诊断信号变成静默丢弃。
    #
    # ⚠ 三张产物表**都没有 level 维度**:`community_seq` 本身不分 level,一次预计算产出
    # 的永远是一套自洽的产物,它描述的 level 记在账本 payload 里。三张表统一按 notebook
    # 整表重写,删除口径因此只有一个。
    #
    # ⚠ `edges` / `profiles` 按**可迭代**声明,实现必须分批消费、不得再物化一份完整
    # 列表:`edges` 是一份最多 20 万行的有界物化,落库这一刻它整个压在栈帧上。
    # (折叠结果本身已在取完 top-N 之后当场释放,见 `_compute_kg_analysis` 的
    # `del folder` —— 那是 codex 第 9 轮 P1-2 的修法;这里说的是 `edges` 那一份。)
    @staticmethod
    def source_canonical_rows(db: object, notebook_id: str) -> object: ...
    @staticmethod
    def replace_kg_analysis_artifacts(
        db: object,
        notebook_id: str,
        kg_mutation_seq: int,
        edges: object,
        profiles: object,
        payloads: object,
        now: str,
    ) -> None: ...
    # -------------------------- KG 质量分析产物的**读**路径(T3,在线请求路径)
    # 这三个是唯一**可以**挂在在线请求上的 KG 分析读:它们只碰预计算产物表,代价按
    # 行数硬有界,与上面那三条 T1 全表重活(200 万簇行 / 836 万边)差一到两个数量级。
    #
    #   · kg_analysis_artifact_rows  账本全文,每 notebook ≤5 行,复合主键点读。
    #                                返回 `{kind: {kg_mutation_seq, payload, created_at}}`。
    #                                **读账本只有这一个入口**:预计算的新鲜度闸与 T3 的
    #                                报告共用它。早先另有一个只取 seq 的窄读,簇世代改盖
    #                                进 payload(刻意不加列)之后闸也必须拿 payload,两个
    #                                方法就只差一个 created_at —— 合成一个,账本的判据与
    #                                读取都只剩一处,不可能漂。
    #   · kg_community_edges_top     跨板块边 top-N。**没有 weight 索引**(主键是
    #                                (notebook_id, src, dst)),所以是本 notebook 分片
    #                                的一次范围扫 + 有界 top-N 排序器 —— 有界靠的是 T2
    #                                的 `MAX_PERSISTED_COMMUNITY_EDGES`(20 万行硬上限),
    #                                不是索引。真机若成为瓶颈,正解是加
    #                                (notebook_id, weight) 索引(一次迁移),不是放宽上限。
    #   · kg_source_profile_page     来源画像的一页 + 总行数。生产 48 836 个来源,一次
    #                                全返回既是几 MB payload 也没人读得完,所以分页是
    #                                硬要求。排序键 `mainstream_share` 走
    #                                idx_kg_source_profiles_nb_mainstream,并列消歧
    #                                `source_id ASC` 是**分页正确性**的一部分(混杂库里
    #                                一大片恰好 0.0,没有次级键就会跨页重复/漏行,而且
    #                                两个后端各给一种顺序)。
    #
    # 连接契约:三个都是 connection-taking,骑调用方(T3 的 KgAnalysisService)的**读**
    # 连接,故不需要上面那道 `_reject_inside_write_transaction`(它防的是「自开的另一条
    # 连接读到提交前的库」,骑调用方连接不存在这个失配)。服务层入口另有一道同语义断言。
    #
    # ⚠ 调用方骑的那条连接必须由 `read_snapshot()` 开 —— 一个**多语句共享快照**,而不是
    # 裸 `database.connect()`,而且**一趟只开一个**(每条读各开一个 = 和没开一样)。
    # 两个端点都是多条读:
    #   · `/sources`   state 行 + 账本 + COUNT + 一页(codex 第 8 轮 P2);
    #   · 总览          state 行 + 账本 + 板块列表(`community_overview_on`)+ 跨板块边
    #                  (codex 第 12 轮 P2)。
    # 两个后端的默认读都是**每条语句各取一个快照**(SQLite 自动提交 / PostgreSQL
    # READ COMMITTED)。并发的预计算整批重写产物表、社区重建整批重铸板块 id,只要提交在
    # 中间:`/sources` 会把新写入的画像行盖上**上一代**账本的世代戳、`total` 与 `rows`
    # 互相对不上;总览会把「上一代的新鲜度戳」配上「新一代的板块 id」,或者把旧板块配上
    # 新边 —— 俯瞰图照着画出来的连线是悬空的,而那份数据还盖着另一个世代的戳。
    # 两侧兑现方式不同(`BEGIN DEFERRED` 的 WAL 快照 / `BEGIN … READ ONLY` +
    # REPEATABLE READ),语义等价;**参数表刻意为空**,方言细节不外泄给后端中性的 service。
    #
    # 上限:`limit` 两侧都硬 clamp(KG_COMMUNITY_EDGES_MAX / KG_SOURCE_PAGE_MAX),
    # `offset` 收到 [0, ∞)(它天然被表的行数兜住)。截断绝不静默 —— 调用方拿返回条数
    # 与账本里的 `edges` / 这里的 `total` 一比即可,不需要多取一行来判。
    def read_snapshot(self) -> object: ...
    @staticmethod
    def kg_analysis_artifact_rows(
        db: object, notebook_id: str
    ) -> dict[str, dict[str, object]]: ...
    @staticmethod
    def kg_community_edges_top(
        db: object, notebook_id: str, limit: int = 200
    ) -> list[tuple[str, str, int]]: ...
    @staticmethod
    def kg_source_profile_page(
        db: object,
        notebook_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        ascending: bool = True,
    ) -> "tuple[int, list[dict[str, object]]]": ...


class CommunityQueryPort(Protocol):
    def mounted_base_ids(
        self, active_notebook_id: str
    ) -> list[str]: ...
    def resolve_comparison_peers(
        self,
        base_notebook_id: str,
        focal_name: str,
        question: str,
        *,
        top_k: int,
        candidates: int,
    ) -> tuple[list[str], str]: ...
    def sibling_peers(
        self, notebook_id: str, focal_name: str, *, top_k: int = 8
    ) -> list[tuple[str, int]]: ...


class EvidenceContextPort(Protocol):
    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...
    def citation_titles(
        self, source_ids: Iterable[str]
    ) -> dict[str, str]: ...
    def collection_item_citations(
        self,
        items: Sequence[object],
        *,
        active_notebook_id: str,
    ) -> dict[str, Citation]: ...
    def chunk_context(
        self,
        chunks: Sequence[RetrievedChunk],
        *,
        notebook_id: str,
        id_offset: int = 0,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]: ...
    def element_context(
        self,
        elements: Sequence[RetrievedElement],
        *,
        notebook_id: str,
        id_offset: int = 4000,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]: ...
    def knowledge_context(
        self,
        notebook_id: str,
        hits: Sequence[RetrievedKnowledge],
        *,
        id_offset: int = 0,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]: ...
    def parse_anchors(
        self,
        answer: str,
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> list[AnswerAnchor]: ...
    def citations_from(
        self,
        hits: Sequence[RetrievedKnowledge],
        valid_element_ids: set[str],
        label: str,
        *,
        notebook_id: str,
    ) -> list[Citation]: ...
    def tier_map(self, notebook_ids: Sequence[str]) -> dict[str, str]: ...


class RetrievalPort(Protocol):
    def replace_embedder(self, embedder: Any) -> None: ...
    def replace_notebook_languages(
        self, notebook_languages: dict[str, list[str]]
    ) -> None: ...
    def preload_scale_artifacts(
        self, progress: Callable[[int, int], None] | None = None
    ) -> dict[str, int]: ...
    def retrieve_scored(self, notebook_id: str, query: str, types: Iterable[str] | None = None, w_keyword: float = W_KEYWORD, w_semantic: float = W_SEMANTIC) -> list[RetrievedKnowledge]: ...
    def retrieve_relations_scored(self, notebook_id: str, query: str) -> list[RetrievedRelation]: ...
    def relations_with_names(self, notebook_id: str, relation_ids: list[str] | None = None) -> list[dict]: ...
    def federated_retrieve(self, active_notebook_id: str, query: str, types: Iterable[str] | None = None, w_keyword: float = W_KEYWORD, w_semantic: float = W_SEMANTIC) -> list[RetrievedKnowledge]: ...
    def federated_retrieve_relations(self, active_notebook_id: str, query: str) -> list[RetrievedRelation]: ...
    def retrieve_neighbors(self, notebook_id: str, object_id: str, edge_type: str | None = None, direction: str = "both") -> list[RetrievedKnowledge]: ...
    def retrieve_elements(self, notebook_id: str, query: str, limit: int = 8) -> list[RetrievedElement]: ...
    def ppr_retrieve(self, notebook_id: str, question: str) -> list[RetrievedChunk]: ...
    def exact_lookup_chunks(self, notebook_id: str, query: str) -> list[RetrievedChunk]: ...
    def follow_chain(self, active_notebook_id: str, start_object_id: str, edge_type: str | None = None, target_object_id: str = "", direction: str = "out", max_fan_out: int = 8, max_results: int = 4) -> FollowChainResult: ...
    def node_context(self, notebook_id: str, object_id: str) -> dict[str, Any]: ...
    def runtime_dim(self) -> int: ...
    def in_batches(self, ids: Iterable[str], batch_size: int = 900) -> Iterable[list[str]]: ...
    def concept_cluster_id(self, notebook_id: str, object_id: str) -> str: ...
    def weak_support_relations(self, notebook_id: str, object_ids: Iterable[str]) -> list[GapRelationRow]: ...
    def edge_support_map(self, notebook_id: str) -> dict[tuple, tuple]: ...
    def cluster_map(self, notebook_id: str) -> dict[str, str]: ...
    def community_queries(
        self, settings: Settings | None = None
    ) -> CommunityQueryPort: ...


class AskCandidatePort(Protocol):
    def notebook_languages(self, notebook_id: str) -> list[str]: ...
    def chunk_plan(self, notebook_id: str, queries: list[str]) -> "ChunkRetrievalPlan": ...
    def keyword_chunk_candidates(self, notebook_id: str, keywords: str) -> list[RetrievedChunk]: ...
    def exact_lookup_chunks(self, notebook_id: str, query: str) -> list[RetrievedChunk]: ...
    def retrieve_chunk_candidates(self, notebook_id: str, query: str) -> tuple[list[RetrievedChunk], list[str], np.ndarray | None]: ...
    def retrieve_chunk_candidates_multi(self, notebook_id: str, queries: list[str]) -> tuple[dict[str, RetrievedChunk], list[dict[str, RetrievedChunk]], list[str], np.ndarray | None]: ...
    def mixed_chunk_candidates(self, notebook_id: str, query: str, high_level: str, queries: list[str]) -> tuple[list[RetrievedChunk], str, dict[str, dict[str, object]], list[RetrievedKnowledge], int]: ...
    def merge_chunk_candidates(self, base: list[RetrievedChunk], extra: list[RetrievedChunk]) -> list[RetrievedChunk]: ...
    def select_chunk_candidates(self, scored: list[RetrievedChunk], ids: list[str], matrix: np.ndarray | None, k: int, lambda_: float) -> list[RetrievedChunk]: ...
    def has_kg(self, notebook_id: str) -> bool: ...
    def any_base_has_kg(self, notebook_id: str) -> bool: ...
    def graph_is_large(self, notebook_id: str) -> bool: ...
    def fuse_graph_seeds(self, notebook_id: str, question: str, seeds: list[str], cancel_event: CancelEvent = None) -> list[str]: ...


class AskGraphPort(Protocol):
    def federated_graph(self, notebook_id: str) -> tuple[DirectedGraphPort, dict[int, str], dict[str, int]]: ...
    def source_chunks(self, notebook_id: str, object_ids: list[str]) -> list[RetrievedChunk]: ...


@runtime_checkable
class ReasoningModelProvider(Protocol):
    def chat(self, workload_id: str) -> JsonChatClientPort: ...
    def configured(self, workload_id: str) -> bool: ...
    def parallelism(self, workload_id: str) -> int: ...


class ModelClientProvider(ReasoningModelProvider, Protocol):
    def rerank(self, workload_id: str) -> RerankClientPort: ...


class AskModelClientProvider(ModelClientProvider, Protocol):
    def primary_unconfigured(self) -> bool: ...


class ModelErrorSink(Protocol):
    def note_model_error(
        self,
        stage: str,
        error: Exception,
        *,
        workload_id: str,
    ) -> None: ...


class AssetMaintenancePort(Protocol):
    def sweep_orphan_assets(
        self,
        notebook_id: str,
        *,
        min_age_seconds: float = 0.0,
        waive_grace_if_no_tables: bool = False,
    ) -> dict[str, object]: ...


class OfflineMaintenanceBusyError(RuntimeError):
    """A second direct maintenance process could not claim the operator lock."""


class FacadePropertyContract(ModelClientProvider, Protocol):
    settings: Settings
    storage_dir: Path
    retrieval: RetrievalPort
    maintenance: AssetMaintenancePort


class AskStreamPort(Protocol):
    def current_user(self) -> UserProfile: ...
    def start_ask_stream(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: "AskMode",
        *,
        user_id: str,
    ) -> "queue.Queue[dict[str, object] | None]": ...


class NotebookRepository(IdentityRepository, NotebookAccessRepository, NotebookCatalogRepository, NotebookSharingRepository, SourceRepository, KnowledgeReadRepository, SchemaRegistryRepository, KnowledgeGovernanceRepository, KnowledgeLifecycleRepository, IndexLifecycleRepository, AskStateRepository, ReportRepository, AdminQueryRepository, AskExecutionPort, FacadePropertyContract, Protocol):
    def close(self) -> None: ...


class BatchMaintenancePort(Protocol):
    """Backend-neutral maintenance surface consumed by offline batch workflows.

    Physical datastore conversions and fixture-only helpers deliberately do not
    belong here.  Implementations may delegate to stores/services, but callers
    must not know which SQL dialect is active.
    """

    def offline_maintenance_lock(self) -> ContextManager[None]: ...
    def resolve_owner_profile(self, owner: str | None) -> UserProfile | None: ...
    def resolve_notebook_owner_profile(
        self, notebook_id: str
    ) -> UserProfile | None: ...
    def all_notebook_ids(self) -> list[str]: ...
    def source_id_by_hash(self, notebook_id: str, digest: str) -> str | None: ...
    def source_ids_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]: ...
    def user_source_ids_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]: ...
    def user_source_title_rows_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[dict[str, object]]: ...
    def source_has_kg(self, source_id: str) -> bool: ...
    def source_has_elements(self, source_id: str) -> bool: ...
    def source_is_user_visible(self, notebook_id: str, source_id: str) -> bool: ...
    def source_ids_missing_elements_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]: ...
    def kg_target_source_rows_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        retry_partial: bool = False,
    ) -> list[dict[str, object]]: ...
    def count_sources_missing_kg(self, notebook_id: str) -> int: ...
    def paper_metadata_source_ids_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        include_existing: bool = False,
    ) -> list[str]: ...
    def ensure_paper_metadata_source(self, source_id: str, *, force: bool = False) -> str: ...
    def run_extraction(
        self, source_id: str, *, preserve_existing_until_complete: bool = False
    ) -> None: ...
    def set_source_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: str | None = None,
        error_message: str = "",
    ) -> None: ...
    def missing_chunk_embedding_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[dict[str, object]]: ...
    def missing_element_embedding_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[dict[str, object]]: ...
    def embed_chunks_batch(self, notebook_id: str, items: list[dict[str, object]]) -> None: ...
    def embed_elements_batch(self, notebook_id: str, items: list[dict[str, object]]) -> int: ...
    def embed_chunks_for_source(self, source_id: str) -> None: ...
    def backfill_node_embeddings(
        self, notebook_id: str, progress: EmbeddingProgress | None = None
    ) -> int: ...
    def count_missing_chunk_vectors(self, notebook_id: str) -> int: ...
    def count_missing_element_vectors(self, notebook_id: str) -> int: ...
    def count_missing_node_vectors(self, notebook_id: str) -> int: ...
    def mark_unified_kg_dirty(self, notebook_id: str) -> None: ...
    def has_scale_index(self, notebook_id: str) -> bool: ...
    def clear_source_index(self, notebook_id: str) -> int: ...
    def backfill_source_index_batch(
        self, notebook_id: str, last_id: str, batch_size: int
    ) -> tuple[int, int, str]: ...
    def mark_source_index_backfilled(self, notebook_id: str) -> None: ...


class SQLiteVectorConversionPort(Protocol):
    """Physical legacy-vector conversion that is meaningful only on SQLite."""

    def count_text_vector_rows(
        self, table: str, id_col: str, notebook_id: str | None
    ) -> int: ...
    def convert_text_vector_batch(
        self,
        table: str,
        id_col: str,
        notebook_id: str | None,
        batch_size: int,
        encode: VectorBatchEncoder,
    ) -> tuple[int, int]: ...


class SQLiteMaintenancePort(
    BatchMaintenancePort, SQLiteVectorConversionPort, AssetMaintenancePort, Protocol
):
    def delete_notebook_kg(self, notebook_id: str) -> dict[str, object]: ...
    def eval_insert_source_for_test(self, notebook_id: str, name: str, text: str, tmpdir: str) -> str: ...
    def backfill_kg_fts(self, notebook_id: str) -> int: ...
    def backfill_chunk_fts(self, notebook_id: str) -> int: ...
    def build_scale_index(self, notebook_id: str, on_stage: IndexStageProgress | None = None) -> dict[str, object]: ...
    def fold_scale_index_delta(self, notebook_id: str, _assume_locked: bool = False) -> dict[str, object]: ...
    def resolve_owner_profile(self, owner: str | None) -> UserProfile | None: ...
    def resolve_notebook_owner_profile(self, notebook_id: str) -> UserProfile | None: ...
    def all_notebook_ids(self) -> list[str]: ...
    def notebook_rows(self) -> list[dict[str, object]]: ...
    def source_id_by_hash(self, notebook_id: str, digest: str) -> str | None: ...
    def source_ids(self, notebook_id: str) -> list[str]: ...
    def source_title_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def set_sources_doc_type(self, notebook_id: str, doc_type: str) -> None: ...
    def kg_covered_source_ids(self, notebook_id: str) -> set[str]: ...
    def partial_kg_source_ids(self, notebook_id: str) -> set[str]: ...
    def sources_with_elements(self, notebook_id: str) -> set[str]: ...
    def count_sources_missing_kg(self, notebook_id: str) -> int: ...
    def run_extraction(
        self, source_id: str, *, preserve_existing_until_complete: bool = False
    ) -> None: ...
    def set_source_status(self, source_id: str, status: str, *, summary: str | None = None, error_message: str = "") -> None: ...
    def latest_extraction_run(self, source_id: str) -> dict[str, object] | None: ...
    def seed_parsed_source(self, notebook_id: str, *, title: str, doc_type: str, file_name: str, file_path: str, elements: Sequence[Mapping[str, object]], status: str = "extracted") -> str: ...
    def seed_rule_object(self, notebook_id: str, *, payload: Mapping[str, object], evidence: Sequence[Mapping[str, object]], source_id: str) -> str: ...
    def invalidate_unified_cache(self, notebook_id: str) -> None: ...
    def sample_knowledge_objects(self, notebook_id: str, limit: int = 5) -> list[dict[str, object]]: ...
    def element_text(self, element_id: str) -> str | None: ...
    def missing_chunk_embedding_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def missing_element_embedding_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def missing_chunk_vector_source_ids(self, notebook_id: str) -> list[str]: ...
    def missing_element_vector_source_ids(self, notebook_id: str) -> list[str]: ...
    def embed_elements_batch(self, notebook_id: str, items: list[dict[str, object]]) -> int: ...
    def embed_chunks_batch(self, notebook_id: str, items: list[dict[str, object]]) -> None: ...
    def embed_chunks_for_source(self, source_id: str) -> None: ...
    def chunk_and_embed_source(self, source_id: str) -> None: ...
    def embed_objects_batch(self, notebook_id: str, items: list[dict[str, object]], progress: EmbeddingProgress | None = None, commit_every: int | None = None) -> None: ...
    def knowledge_object_payload_page(self, notebook_id: str, *, after_id: str = "", limit: int = 500, include_deprecated: bool = False) -> list[dict[str, object]]: ...
    def knowledge_object_payloads(self, notebook_id: str, *, include_deprecated: bool = False) -> list[dict[str, object]]: ...
    def backfill_node_embeddings(self, notebook_id: str, progress: EmbeddingProgress | None = None) -> int: ...
    def node_embedding_counts(self, notebook_id: str) -> tuple[int, int]: ...
    def count_missing_chunk_vectors(self, notebook_id: str) -> int: ...
    def count_missing_element_vectors(self, notebook_id: str) -> int: ...
    def count_missing_node_vectors(self, notebook_id: str) -> int: ...
    def count_chunks(self, notebook_id: str) -> int: ...
    def count_knowledge_embeddings(self, notebook_id: str) -> int: ...
    def purge_kg_embeddings(self, notebook_id: str) -> None: ...
    def backfill_relation_embeddings(self, notebook_id: str) -> None: ...
    def mark_unified_kg_dirty(self, notebook_id: str) -> None: ...
    def relations_with_names(self, notebook_id: str, relation_ids: list[str] | None = None) -> list[dict[str, object]]: ...
    def knowledge_context(self, notebook_id: str, hits: Sequence[RetrievedKnowledge]) -> tuple[str, dict[str, dict[str, object]]]: ...
    def load_scale_index(self, notebook_id: str, allow_stale: bool = False) -> "ScaleIndex | None": ...
    def has_scale_index(self, notebook_id: str) -> bool: ...
    def gold_knowledge_object_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def kg_object_counts_by_notebook(self) -> dict[str, int]: ...
    def latest_done_report(self) -> dict[str, object] | None: ...
    def sample_approved_object_payload(self, notebook_id: str) -> str | None: ...
    def chunk_notebook_map(self, chunk_ids: Sequence[str]) -> dict[str, str]: ...
    def count_text_vector_rows(self, table: str, id_col: str, notebook_id: str | None) -> int: ...
    def convert_text_vector_batch(self, table: str, id_col: str, notebook_id: str | None, batch_size: int, encode: VectorBatchEncoder) -> tuple[int, int]: ...
    def clear_source_index(self, notebook_id: str) -> int: ...
    def backfill_source_index_batch(self, notebook_id: str, last_id: str, batch_size: int) -> tuple[int, int, str]: ...
    def mark_source_index_backfilled(self, notebook_id: str) -> None: ...
    # Repeated explicitly because the frozen protocol-coverage contract
    # enumerates this concrete compatibility port's own dictionary.
    def sweep_orphan_assets(
        self,
        notebook_id: str,
        *,
        min_age_seconds: float = 0.0,
        waive_grace_if_no_tables: bool = False,
    ) -> dict[str, object]: ...


# --- Task 22: Ask/answer/conversation/job/trace persistence ----------------
@runtime_checkable
class AskStateStorePort(Protocol):
    """The raw Ask durable-state store contract (Task 22).

    Identity is explicit — ``user_id`` comes from the caller and the store
    never reads the request ContextVar. Synchronous and streaming Ask both use
    ``begin_durable_job``, which opens ONE write transaction that creates or
    touches the conversation, mutates
    ``payload.conversation_id`` at the same point as baseline and inserts the
    running job row. Their engines then use ``prepare_turn_for_job``: it locks
    that exact conversation parent and accepts only the exact running job,
    returning ``None`` instead of invoking the legacy missing-parent fallback.
    Conversation-lifecycle operations lock the parent before non-locking job
    existence/status checks; final save is the only path that holds a job lock
    while acquiring the parent, so bulk deletion/cleanup must never take a
    job-row lock while holding the parent.
    ``finish_job`` performs only the terminal job-row
    transaction and returns its conversation id — the failed/cancelled empty-
    conversation cleanup stays a LATER ``cleanup_empty_conversation``
    transaction, orchestrated by the caller.  The raw ``append_trace`` raises
    on persistence failure; the fail-open log-and-continue policy stays with
    the facade coordinator (error_policies.json: append_ask_trace).
    """

    def prepare_turn(
        self,
        notebook_id: str,
        requested_conversation_id: str | None,
        question: str,
        user_id: str,
    ) -> PreparedAskTurn: ...
    def prepare_turn_for_job(
        self,
        job_id: str,
        notebook_id: str,
        conversation_id: str | None,
        user_id: str,
    ) -> PreparedAskTurn | None: ...
    def begin_durable_job(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: str,
        user_id: str,
    ) -> tuple[str, str]: ...
    def append_trace(
        self,
        notebook_id: str,
        job_id: str,
        step: dict,
        user_id: str,
    ) -> None: ...
    def save_answer(
        self,
        notebook_id: str,
        conversation_id: str,
        question: str,
        response: AskResponse,
        user_id: str,
    ) -> str: ...
    def save_answer_for_job(
        self,
        job_id: str,
        notebook_id: str,
        conversation_id: str,
        question: str,
        response: AskResponse,
        user_id: str,
    ) -> str | None: ...
    def cancel_running_job(self, job_id: str, user_id: str) -> dict: ...
    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        answer_id: str = "",
        error: str = "",
    ) -> str | None: ...
    def cleanup_empty_conversation(self, conversation_id: str) -> None: ...
    def ask_job_status(self, job_id: str) -> dict: ...
    def answer_notebook_id(self, answer_id: str) -> str | None: ...
    def answer_memory_source(self, answer_id: str) -> dict[str, Any]: ...
    def conversation_history(self, db: object, conversation_id: str, limit: int = 5) -> str: ...
    @staticmethod
    def read_trace(db: object, job_id: str) -> list: ...
    def ask_job_detail(self, job_id: str) -> dict: ...
    def get_conversation(self, conversation_id: str) -> ConversationDetail: ...
    def rename_conversation(self, conversation_id: str, title: str) -> None: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int, user_id: str
    ) -> ConversationBulkDeleteResult: ...
    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse: ...


# Memory write/revision values are storage-neutral domain types.  The port
# remains consumer-owned and has no dependency on a concrete repository.
from app.models.memory import (  # noqa: E402
    MemoryRevision,
    MemoryWrite,
)


@runtime_checkable
class MemoryStorePort(Protocol):
    def create_agent_profile(
        self, owner_id: str, name: str, description: str
    ) -> AgentProfile: ...
    def list_agent_profiles(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentProfile]: ...
    def update_agent_profile(
        self, profile_id: str, owner_id: str, fields: Mapping[str, Any]
    ) -> AgentProfile: ...
    def create_agent_token(
        self, token_id: str, owner_id: str, agent_profile_id: str,
        token_hash: str, scopes: Sequence[str], default_notebook_id: str,
        notebook_ids: Sequence[str], expires_at: str | None,
    ) -> AgentTokenSummary: ...
    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]: ...
    def revoke_agent_token(
        self, token_id: str, owner_id: str
    ) -> AgentTokenSummary: ...
    def agent_token_auth_row(self, token_id: str) -> dict[str, Any] | None: ...
    def touch_agent_token(
        self, token_id: str, used_at: str, touch_before: str
    ) -> None: ...
    def insert_memory(self, write: MemoryWrite) -> MemoryRecord: ...
    def create_candidate_with_initial_revision(
        self, write: MemoryWrite, changed_by: str, reason: str
    ) -> MemoryRecord: ...
    def create_answer_with_initial_revision(
        self, write: MemoryWrite, changed_by: str, reason: str
    ) -> MemoryRecord: ...
    def memory_for_user(self, memory_id: str, user_id: str) -> MemoryRecord: ...
    def append_revision(
        self, memory_id: str, snapshot: dict, changed_by: str, reason: str
    ) -> None: ...
    def transition(
        self, memory_id: str, user_id: str, expected: set[str], target: str
    ) -> MemoryRecord: ...
    def list_memories(
        self, user_id: str, *, notebook_id: str | None, status: str | None,
        origin: str | None, query: str, offset: int, limit: int,
    ) -> PaginatedMemories: ...
    def answer_memory_links(
        self, notebook_id: str, user_id: str, answer_ids: Sequence[str]
    ) -> dict[str, str]: ...
    def memory_by_answer(self, user_id: str, answer_id: str) -> MemoryRecord | None: ...
    def memory_by_agent_request(
        self, user_id: str, notebook_id: str, agent_profile_id: str | None,
        client_request_id: str,
    ) -> MemoryRecord | None: ...
    def agent_profile_belongs_to(self, agent_profile_id: str, user_id: str) -> bool: ...
    def update_fields(
        self, memory_id: str, user_id: str, fields: Mapping[str, Any]
    ) -> MemoryRecord: ...
    def update_with_revision(
        self, memory_id: str, user_id: str, fields: Mapping[str, Any], *,
        expected: set[str], changed_by: str, reason: str,
    ) -> MemoryRecord: ...
    def transition_with_revision(
        self, memory_id: str, user_id: str, expected: set[str], target: str, *,
        fields: Mapping[str, Any] | None, changed_by: str, reason: str,
    ) -> MemoryRecord: ...
    def delete_memory(self, memory_id: str, user_id: str) -> None: ...
    def delete_memory_if_unchanged(
        self, memory_id: str, user_id: str, expected_revision: int | None
    ) -> bool: ...
    def bulk_delete_memories(
        self, user_id: str, memory_ids: Sequence[str]
    ) -> int: ...
    def revisions_for_user(
        self, memory_id: str, user_id: str
    ) -> list[MemoryRevision]: ...
    def embedding_revision(
        self, memory_id: str, item: MemoryRecord
    ) -> int | None: ...
    def replace_embedding(
        self, memory_id: str, expected_revision: int, model: str,
        vector: Sequence[float],
    ) -> bool: ...
    def mark_embedding_failed(
        self, memory_id: str, expected_revision: int, error: str
    ) -> bool: ...
    def memory_retrieval_rows(
        self, user_id: str, notebook_id: str, statuses: Sequence[str], query: str,
        *, lexical_limit: int, vector_limit: int,
        phrase_queries: Sequence[str] = (),
    ) -> list[dict[str, Any]]: ...
    def create_copy_with_initial_revision(
        self,
        write: MemoryWrite,
        source_memory_id: str,
        changed_by: str,
        reason: str,
        expected_source_revision: "int | None",
    ) -> MemoryRecord: ...
    def lock_promotion_memory_on(
        self,
        db: object,
        memory_id: str,
        candidate_notebook_id: str,
    ) -> MemoryRecord: ...


class ReportSourceQueryPort(Protocol):
    """Report corpus-map recon projection over sources (Task 25): title-only
    rows in creation order, capped — the deep-report engine's 0-LLM 语料侦察
    read (SQL frozen from the facade's inline query)."""

    def report_source_rows(
        self, notebook_id: str
    ) -> list[dict[str, str]]: ...


class ContentOverviewMemoryStorePort(Protocol):
    def notebook_content_overview(
        self, user_id: str, notebook_id: str, limit: int = 3
    ) -> dict[str, Any]: ...


class ContentOverviewKnowhowStorePort(Protocol):
    def knowhow_table_health_inputs(
        self, notebook_id: str
    ) -> list[dict[str, Any]]: ...


class NotebookTooLargeToCopyError(ValueError):
    """A notebook's copyable-row bound was exceeded at snapshot time. Lives here
    (neutral contract layer) so snapshot_copy_rows can raise it from the store
    without importing a service, while the service/route catch it (route → 409).
    Subclasses ValueError so callers catching ValueError keep working."""


@runtime_checkable
class SharingStorePort(Protocol):
    def bind_insert_row(self, insert_row: Callable) -> None: ...
    @staticmethod
    def insert_row_values(db: object, table: str, data: dict) -> None: ...
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def is_member(self, notebook_id: str, user_id: str) -> bool: ...
    def add_member(self, notebook_id: str, user_id: str) -> None: ...
    def remove_member(self, notebook_id: str, user_id: str) -> None: ...
    def kick_all_members(self, notebook_id: str) -> None: ...
    def list_members(self, notebook_id: str) -> list: ...


@runtime_checkable
class GovernanceStorePort(Protocol):
    @staticmethod
    def promotion_candidate_identity(
        connection: object,
        candidate_id: str,
    ) -> dict[str, Any] | None: ...
    @staticmethod
    def delete_clusters(connection: object, notebook_id: str, object_type: str) -> None: ...
    @staticmethod
    def delete_pending_merges(connection: object, notebook_id: str) -> None: ...
    def insert_clusters(self, connection: object, notebook_id: str, object_type: str, rows: object, now: str) -> None: ...
    def insert_merge_candidate(self, connection: object, notebook_id: str, a: str, b: str, score: float, now: str, *, id_prefix: str = "mc") -> None: ...
    @staticmethod
    def insert_pending_merge_rows(connection: object, rows: object) -> None: ...
    @staticmethod
    def merge_candidate_pairs(db: object, notebook_id: str, statuses: object) -> list[Any]: ...
    @staticmethod
    def valid_object_ids(db: object, object_ids: object) -> set[str]: ...
    @staticmethod
    def seed_for(object_type: str): ...
    @staticmethod
    def merge_evidence(base_evidence: list, source_evidence: list) -> list: ...
    @staticmethod
    def sweep_orphan_clusters(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def incremental_cluster_rows(
        db: object, notebook_id: str, object_type: str
    ) -> list[Any]: ...
    @staticmethod
    def update_edge_review(
        connection: object, notebook_id: str, relation_id: str, status: str
    ) -> None: ...
    @staticmethod
    def find_base_match(object_type: str, payload: dict, rows: object) -> str: ...


@runtime_checkable
class IndexProjectionStorePort(Protocol):
    def bind_runtime_callbacks(
        self,
        *,
        connect: Callable,
        in_batches: Callable,
        ent_chunk_map: Callable,
        mention_extra_edges: Callable,
        vector_matrix: Callable,
    ) -> None: ...
    def version_signal(self, notebook_id: str) -> tuple[int, int, tuple]: ...
    def version_facts(self, notebook_id: str) -> list[Any]: ...
    def version_with_settings(self, notebook_id: str, settings_tail: tuple) -> list: ...
    def graph_rows(
        self,
        notebook_id: str,
        source_ids: Sequence[str] | None,
        *,
        synonym_edges: Any = None,
        as_arrays: bool = False,
    ) -> Any: ...
    def embedding_matrix(
        self,
        notebook_id: str,
        table: str,
        id_column: str,
        object_ids: Sequence[str] | None = None,
    ) -> Any: ...


@runtime_checkable
class KnowhowStorePort(Protocol):
    def list_knowhow_tables(self, notebook_id: str) -> list[dict]: ...
    def knowhow_enumeration_catalog(
        self, notebook_id: str, *, limit: int = 8, query: str = ""
    ) -> dict:
        """Return bounded table metadata plus aggregate collection coverage.

        The table list is capped by ``limit`` and never hydrates cells, code
        attachments, projection health, or source payloads.  Aggregate counts
        and sequence sums cover the whole notebook so callers can distinguish a
        complete selected table from a partial multi-table batch.  When
        ``query`` names a table, matching titles are placed first so an explicit
        target remains reachable even beyond the bounded catalog window.
        """
        ...
    def enumerate_knowhow_rows(
        self,
        notebook_id: str,
        *,
        table_ids: Sequence[str],
        cursor: Mapping[str, object] | None = None,
        page_size: int = 25,
        column_ids: Sequence[str] | None = None,
    ) -> dict:
        """Return one complete-set retrieval page without editor hydration.

        ``table_ids`` is required, deduplicated by the adapter, and hard
        capped at 8; ``column_ids`` is optional and capped at 8.  The default
        physical-row page is 25 and requests clamp to 50.  Results advance by
        the stable ``table_id, position, id`` cursor and expose per-table
        ``mutation_seq`` plus history-backed ``enumeration_seq`` catalog values
        so a service can reject concurrent content/row-identity edits without
        changing the projector's existing mutation-sequence semantics.
        """
        ...
    def get_knowhow_table(self, table_id: str) -> dict: ...
    def create_knowhow_table(
        self, notebook_id: str, title: str, description: str, columns: list[dict],
        created_by: str = "",
        actor: str = "",
        origin: str = "user",
    ) -> str: ...
    def create_knowhow_table_with_rows(
        self,
        notebook_id: str,
        title: str,
        description: str,
        columns: list[dict],
        rows: Sequence[Sequence[str]],
        created_by: str = "",
        actor: str = "",
        origin: str = "user",
    ) -> str: ...
    def update_knowhow_cell(
        self, row_id: str, column_id: str, content_md: str,
        require_assets: Sequence[str] = (),
        actor: str = "",
        origin: str = "user",
    ) -> None: ...
    def update_knowhow_table_meta(
        self,
        table_id: str,
        title: str | None = None,
        description: str | None = None,
        actor: str = "",
        origin: str = "user",
    ) -> None: ...
    def set_knowhow_anchor_column(
        self, table_id: str, column_id: str | None,
        actor: str = "",
        origin: str = "user",
    ) -> str | None: ...
    def add_knowhow_column(
        self, table_id: str, name: str, kind: str, position: int | None = None,
        actor: str = "",
        origin: str = "user",
    ) -> str: ...
    def rename_knowhow_column(
        self, column_id: str, name: str, actor: str = "", origin: str = "user"
    ) -> None: ...
    def set_knowhow_column_kind(
        self, column_id: str, kind: str, actor: str = "", origin: str = "user"
    ) -> None: ...
    def delete_knowhow_column(
        self, column_id: str, actor: str = "", origin: str = "user"
    ) -> None: ...
    def add_knowhow_row(
        self, table_id: str, cells: dict[str, str], position: int | None = None,
        actor: str = "",
        origin: str = "user",
    ) -> str: ...
    def append_knowhow_rows(
        self, table_id: str, rows: Sequence[dict[str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> list[str]: ...
    def add_knowhow_rows(
        self, table_id: str, rows: list[dict[str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> list[str]: ...
    def delete_knowhow_row(
        self, row_id: str, actor: str = "", origin: str = "user"
    ) -> None: ...
    def delete_knowhow_table(self, table_id: str) -> dict: ...
    def set_knowhow_hidden_source(self, table_id: str, source_id: str, *, connection: object | None = None) -> None: ...
    def bump_knowhow_mutation_seq(self, table_id: str) -> int: ...
    def set_knowhow_row_projection(self, row_id: str, status: str) -> None: ...
    def update_knowhow_cells(
        self, row_ids: list[str], column_id: str, content_md: str,
        require_assets: Sequence[str] = (),
        actor: str = "",
        origin: str = "user",
    ) -> None: ...
    def update_knowhow_cells_bulk_guarded(
        self, notebook_id: str, updates: list[tuple[str, str, str, str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> dict[str, list[tuple[str, str]]]: ...
    def update_knowhow_cells_guarded_atomic(
        self,
        notebook_id: str,
        updates: list[tuple[str, str, str, str, str]],
        require_assets: Sequence[str] = (),
        anchor_column_id: str | None = None,
        expected_anchor: Sequence[str] | None = None,
        actor: str = "",
        origin: str = "user",
    ) -> dict[str, object]: ...
    def validate_cell_target(self, row_id: str, column_id: str) -> None: ...
    def upsert_knowhow_cell_code(
        self, row_id: str, column_id: str, code_text: str, language: str,
        updated_by: str, cell_content_hash: str,
        actor: str = "",
        origin: str = "user",
    ) -> str: ...
    def get_knowhow_cell_code(self, row_id: str, column_id: str) -> dict | None: ...
    def delete_knowhow_cell_code(
        self, row_id: str, column_id: str, actor: str = "", origin: str = "user"
    ) -> None: ...
    def list_knowhow_cell_code(self, table_id: str) -> list[dict]: ...
    def get_knowhow_row_location(self, row_id: str) -> dict | None: ...
    def insert_notebook_asset(self, notebook_id: str, filename: str, mime: str, size: int, created_by: str, source_id: str | None = None) -> str: ...
    def get_notebook_asset(self, asset_id: str) -> dict | None: ...
    def source_asset_ids(self, source_id: str) -> list[str]: ...
    def delete_source_asset_rows(self, source_id: str) -> list[str]: ...


@runtime_checkable
class KnowhowTransferStorePort(Protocol):
    def snapshot_table(self, table_id: str) -> dict: ...
    def insert_transfer(
        self,
        payload: dict,
        expected_counts: dict,
        *,
        new_id: Callable[[str], str] | None = None,
        now: Callable[[], str] | None = None,
        actor: str = "",
        note: str = "",
    ) -> None: ...
    def delete_table_if_unchanged(
        self, table_id: str, expected_fingerprint: str | None
    ) -> bool: ...


@runtime_checkable
class QueryStorePort(Protocol):
    @staticmethod
    def count_rows(db: object, table: str, column: str, value: str) -> int: ...
    @staticmethod
    def knowledge_type_count_rows(
        db: object, notebook_id: str, statuses: tuple[str, ...]
    ) -> list[dict]: ...
    @staticmethod
    def knowledge_type_count_rows_for_sources(
        db: object,
        notebook_id: str,
        source_ids: list[str],
        statuses: tuple[str, ...],
    ) -> list[dict]:
        """``[{object_type, c}]`` for objects owned by the GIVEN sources.

        Generic by design: the collection catalog passes
        ``SourceStorePort.memory_source_ids`` to subtract private Memory from
        the enumeration denominator, and that single definition of "which
        sources are Memory" stays in one place instead of being re-spelled as
        a join predicate here.  Bounded and index-assisted on
        ``knowledge_objects(source_id)``; the id list is batched by the
        adapter.
        """
        ...
    @staticmethod
    def knowhow_knowledge_type_rows(
        db: object, notebook_id: str, statuses: tuple[str, ...]
    ) -> list[dict]: ...
    def invalidate_knowledge_counts(self, notebook_id: str) -> None: ...
    def list_user_usage(self) -> list[dict[str, Any]]: ...
    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def pending_actions_projection_rows(self, user_id: str) -> dict: ...
    def load_notebook_scale_facts(self, notebook_id: str) -> NotebookScaleFacts: ...
    def warm_open_path_caches(
        self, progress: Callable[[int, int], None] | None = None
    ) -> int: ...


@runtime_checkable
class ReportStorePort(ReportRepository, Protocol):
    def row_to_dict(self, row: object, *, full: bool) -> dict: ...
