"""Consumer-owned repository contracts.

The concrete SQLite facade remains the compatibility implementation.  These
protocols intentionally contain no business logic and are structural seams for
the later store/service extraction.
"""
from __future__ import annotations
import json
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

from app.core.config import Settings
from app.domain.ask import AskMode
from app.domain.cancellation import CancelEvent
from app.domain.graph import FollowChainResult
from app.domain.knowledge_contracts import CONCEPT_DETAIL_PAGE_MAX
from app.domain.notebook_scale import NotebookScaleFacts
from app.domain.retrieval_experience import (
    SITUATION_ASK_MODES,
    clip_trace_text,
    closed_value,
)
from app.domain.scale import ScaleIndexView
from app.domain.retrieval import (
    ChunkRetrievalPlan,
    GapRelationRow,
    NeighborExpansion,
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
    RetrievedRelation,
    W_KEYWORD,
    W_SEMANTIC,
)
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
    PaginatedSourceElements,
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
KNOWHOW_COLUMN_KINDS = frozenset({"anchor", "procedure", "entity", "attribute"})


class ChunkLexicalSearchTimeout(RuntimeError):
    """One bounded generic chunk-lexical probe exhausted its own budget."""


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
    # 这份来源的**显示标题**，与 file_name 分开。
    #
    # 浏览器上传里两者天然同源（用户给的就是文件名），所以 upload_sources 一直拿
    # file_name 当 title 用。但合成来源的调用方（MCP 的 add_source_text：用户给的是
    # 一个**标题**，文件名是它派生出来的）两者并不同——file_name 会被 safe_filename
    # 净化、按文件系统字节预算截断、再缀上 `.md`，把它写进 title 就是拿一条派生的
    # 路径串冒充用户提交的标题。
    #
    # 空串＝没表态，落回 file_name，故全部既有调用方（浏览器/batch_ingest/eval/
    # smoke）逐位不变。只作用于**新建**行：复用既有行不改它的标题（与 doc_type 的
    # 复用语义一致——复用不是重命名）。追加在字段表末尾而不是插在 file_name 之后，
    # 以免推移任何按位置传参的构造点。
    title: str = ""


@dataclass(frozen=True)
class PreparedAskTurn:
    """The durable conversation id and history prepared for one Ask turn."""

    conversation_id: str
    history: str


class ConversationBusyError(RuntimeError):
    """Explicit deletion cannot remove a conversation used by a running Ask."""

    def __init__(self) -> None:
        super().__init__("conversation has a running Ask job")


class ConversationShareWatermarkStale(RuntimeError):
    """``share_conversation`` was handed an ``expected_through_id`` that is no
    longer a resolvable answer of this conversation (codex #522 R2 P1).

    The client passes the newest answer id it saw in the SAME turns it computed
    its disclosure from, and the store pins the watermark to exactly that answer
    so the published snapshot equals the disclosed one. If that answer has since
    been deleted, the client's disclosure describes a snapshot that can no longer
    be reproduced — publishing "current latest" instead would silently expose
    turns the user never reviewed. The safe direction is to reject (the API layer
    maps this to 409) so the user reloads and re-reviews, NOT to fall back to the
    latest answer and bypass consent."""


class ConversationHasNoShareableAnswer(RuntimeError):
    """``share_conversation`` was asked to publish a conversation with no
    committed answer to bound the snapshot (codex #522 R5).

    "会话的已完成" is "at least one written answer before the watermark" (design
    doc §七 item 5). A never-answered / in-flight conversation has nothing to
    show, so the store refuses to mint a token AT ALL — it raises this INSIDE the
    same write transaction that resolves the boundary, so token issuance and the
    "has a shareable answer" check are one atomic step. This replaces the old
    share-then-compensate path (mint a NULL-watermark token, then a second
    ``discard_unwatermarked_share`` call rolls it back): that compensation left a
    permanent token-without-watermark row if the process died between the two
    steps. The API layer maps this to the existing zero-answer 409."""


class KgBuildAlreadyRunning(RuntimeError):
    """One notebook already has a durable running KG build job."""


class KgMaintenanceAlreadyRunning(RuntimeError):
    """One notebook already has an in-flight KG maintenance pass.

    ONE slot covers both no-LLM-precondition maintenance passes — isolated-node relink and
    the unified concept rebuild — because they are not independent: a rebuild
    rewrites ``concept_clusters`` and the community partition wholesale while
    relink appends edges to the very graph that clustering reads. Letting them
    overlap does not just double the cost, it lets one pass publish over inputs
    the other is still consuming. ``holder`` says which kind currently owns the
    slot so the 409 can name the action the user actually has to wait for; the
    two status views each report only their own kind and answer ``idle``
    otherwise, so neither poll can be parked on the other's job.

    In-process rather than durable on purpose: neither pass has an LLM precondition
    deterministic work, so they do not belong in ``kg_build_jobs`` (whose
    user-facing consumers would narrate them as a source-by-source analysis) and
    must not inherit that table's LLM-configured precondition.
    """

    def __init__(self, notebook_id: str, holder: str) -> None:
        super().__init__(notebook_id)
        self.notebook_id = notebook_id
        # "relink" | "rebuild" — the kind of pass currently holding the slot.
        # Required, not defaulted: a default would let a future raise site tell
        # the user to wait on an action that is not the one running.
        self.holder = holder


class CatalogJobAlreadyRunning(RuntimeError):
    """One source already has a durable queued/running command-catalog job."""


class DocumentCapacityExceeded(RuntimeError):
    """The per-notebook visible-document ceiling refused a source INSERT —
    raised from INSIDE the creation write transaction, where the COUNT and the
    INSERT are one atomic step (SQLite: process write lock + BEGIN IMMEDIATE;
    PostgreSQL: a notebook-row lock serializes capacity-checked creators).

    This is the atomic backstop behind the API layer's read-then-check
    pre-flight: with one slot left, two concurrent creates could both snapshot
    capacity and both land (PR #584 codex R6). The pre-flight still exists for
    its cheap early 409 — this exception is what the LOSER of the race gets
    instead of silently overshooting the limit. Carries the transaction-local
    ``current`` count and the enforced ``limit`` so callers can phrase the same
    user-facing sentence (``document_capacity_message``) the pre-flight uses.

    Only raised when a caller passes ``capacity_limit`` — offline
    ``batch_ingest``, Memory/knowhow projections, and admin-owned notebooks
    keep passing None and pay zero extra queries."""

    def __init__(self, current: int, limit: int) -> None:
        super().__init__(f"notebook document capacity reached: {current}/{limit}")
        self.current = current
        self.limit = limit


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
        thinking_mode: Optional[Literal["enabled", "disabled"]] = None,
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
    def register_user_with_session(self, username: str, password: str) -> tuple[UserProfile, str]: ...
    def authenticate_user(self, username: str, password: str) -> UserProfile | None: ...
    def login_with_password(self, username: str, password: str) -> tuple[UserProfile, str] | None: ...
    def create_session(self, user_id: str) -> str: ...
    def resolve_session(self, token: str) -> UserProfile | None: ...
    def delete_session(self, token: str) -> None: ...
    def audit_labels_for_user_ids(self, user_ids: Sequence[str]) -> dict[str, str]: ...
    def set_user_role(self, actor_id: str, user_id: str, role: str) -> dict[str, str]: ...
    def set_user_ui_mode(self, user_id: str, ui_mode: str) -> UserProfile: ...
    # Agentic Memory P3(B-Profile,T6):读-改-写 ``user_profiles.
    # search_profile_json`` 里的若干字段并返回更新后的 UserProfile。
    # ``origin="user"``(自助编辑,PATCH /me/search-profile)无条件覆盖;
    # ``origin="job"``(T7 归纳)跳过已被用户写过的字段——两侧规则都在
    # ``app.services.search_profile.merge_field`` 里,这里只是端口签名。
    # 读-改-写必须在同一写事务内(SQLite 写锁天然串行/PostgreSQL 显式行锁),
    # 否则用户编辑与后台归纳并发写会互相丢字段。
    def set_user_search_profile(
        self, user_id: str, fields: "Mapping[str, object]", origin: str
    ) -> UserProfile: ...
    # Agentic Memory P3(B-Profile,T8):一次主键点读该用户的检索/回答风格偏好
    # **文档**(``app.services.search_profile.parse_search_profile`` 的返回形状,
    # 而非整个 ``UserProfile``——Ask/reasoning-plan 注入点只需要这一份 JSON 去
    # 喂 ``render_style_block``,不需要 email/display_name/role 这些字段,
    # 也不必像 ``current_user()``/``_user_profile`` 那样再 join ``users`` 表)。
    # 行不存在、列缺失(旧库未跑迁移)或畸形 JSON 一律 fail-open 到
    # ``None``——调用方(``AskService._search_profile_style_block``/
    # ``ReasoningRetriever.run()``)据此把风格提示渲染成空串,调用形状与接入前
    # 逐字相同。绝不读 ``current_user()`` ContextVar——调用方必须显式传入
    # user_id(见两处调用方各自的红线注释)。
    def get_user_search_profile(self, user_id: str) -> "dict | None": ...
    def change_user_password(
        self, user_id: str, old_password: str, new_password: str, *, keep_token: str | None = None
    ) -> None: ...
    def admin_reset_user_password(
        self, actor_id: str, user_id: str, new_password: str
    ) -> dict[str, str]: ...
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
class ExtensionToggleStorePort(Protocol):
    """部署插件运行时开关 + 审计。无行 = 启用——见两个后端 store 实现。"""

    def extension_runtime_disabled_ids(self) -> frozenset[str]: ...

    def list_extension_runtime_toggles(self) -> list[dict]: ...

    def set_extension_runtime_enabled(
        self, plugin_id: str, enabled: bool, actor_id: str
    ) -> dict: ...


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
    # 管理权(P2 能力翻转):owner ∪ role='admin' 的有效授权边。与写权**并列**而不是
    # 取代它——notebook:delete 与 Agent/MCP 面仍恒 owner,两条谓词各有引用者。
    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_source(self, source_id: str, user_id: str) -> bool: ...
    # source_owner: P0-T2 之后已无生产/测试调用者(体内自查改走
    # source_notebook_id + 能力判定),仅为冻结兼容面保留——它被
    # facade_surface.json 与 ownership_manifest 登记,删除需要 rebaseline,
    # 不值得为省一行签名去动冻结面。别因为"没人用"顺手删它。
    def source_owner(self, source_id: str) -> str | None: ...
    # P0-T2: exposed so callers that must resolve a source's owning notebook
    # BEFORE applying a capability guard (source_routes.py's body-level owner
    # self-checks — a route whose notebook_id is not a URL path segment, so it
    # cannot use a static Depends(require_notebook_capability(...))) can reuse
    # the same predicate the capability factory uses, instead of hand-rolling
    # `source_owner(source_id) == user.id`.
    def source_notebook_id(self, source_id: str) -> str | None: ...
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
    def share_state(self, notebook_id: str) -> dict: ...
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
    # ``agent_profile_id`` non-empty stamps v48 Agent provenance on rows the call
    # CREATES ("" -> NULL = a person added it). Reused dedup rows keep theirs.
    # ``capacity_limit`` is the notebook's ABSOLUTE visible-document ceiling
    # (None = exempt/offline), re-counted atomically inside each creation write
    # transaction — not a pre-computed remaining budget, which would re-open the
    # check-then-insert race this parameter closes. Over-limit URLs land in
    # ``rejected``; an over-limit upload raises ``DocumentCapacityExceeded``.
    # Its parameter POSITION differs between the two methods on purpose:
    # ``add_url_sources`` keeps it in the slot the retired ``capacity`` budget
    # occupied, ``upload_sources`` appends it last — both choices exist so the
    # positional callers each method already had keep their argument order.
    # ``trusted_proxy_origins`` is the deployment's normalized trusted-proxy
    # origin whitelist (None/empty = no exemption): the URL probe skips only
    # the SSRF public-address check for URLs whose origin matches exactly.
    # Only the plugin-port adapter passes it; browser/MCP callers never do.
    # It gates the import PROBE half only — the parse-time download half reads
    # the deployment settings itself (process_source), independent of this
    # parameter, so passing a set the deployment did not configure still
    # leaves the created source's download unexempted.
    def add_url_sources(self, notebook_id: str, urls: Iterable[str], scheduler: SourceScheduler | None = None, capacity_limit: "int | None" = None, agent_profile_id: str = "", trusted_proxy_origins: "frozenset[str] | None" = None) -> AddUrlSourcesResult: ...
    def upload_sources(self, notebook_id: str, files: Iterable[UploadedSourceFile], scheduler: SourceScheduler | None = None, agent_profile_id: str = "", capacity_limit: "int | None" = None) -> list[UploadedSourceSummary]: ...
    def get_source(self, source_id: str) -> SourceDetail: ...
    def process_source(self, source_id: str) -> SourceSummary: ...
    # Bounded probe of the per-source parse (chunk) lock: True while a parse is
    # in flight. Consumers that only SCHEDULE work (MCP's re-parse tool) use it
    # to refuse instead of queueing a duplicate pipeline run.
    def source_parse_busy(self, source_id: str, *, timeout: float = 0.0) -> bool: ...
    def parse_source(self, source_id: str) -> SourceSummary: ...
    def source_elements(self, source_id: str) -> list[SourceElement]: ...
    def source_elements_page(self, source_id: str, offset: int = 0, limit: int = 40, anchor_element_id: str = "") -> PaginatedSourceElements: ...
    def delete_source(self, source_id: str) -> None: ...
    def extract_source(self, source_id: str) -> None: ...


class KnowledgeReadRepository(Protocol):
    def knowledge_types(self, notebook_id: str) -> list[KnowledgeTypeCount]: ...
    def list_knowledge(self, notebook_id: str, object_type: str, status: str | None = None, offset: int = 0, limit: int = 50) -> PaginatedKnowledge: ...
    def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph: ...
    def kg_search(self, notebook_id: str, q: str, k: int = 30) -> list: ...
    def unified_graph(self, notebook_id: str, level: str = "concept", limit: int | None = None) -> dict: ...
    def concept_detail(self, notebook_id: str, canonical_id: str, *, source_notebook_id: str = "", limit: int | None = CONCEPT_DETAIL_PAGE_MAX, after: str = "") -> dict: ...
    def node_context(self, notebook_id: str, object_id: str, *, source_notebook_id: str = "") -> dict: ...
    def kg_neighbors(self, notebook_id: str, object_id: str, cap: int = 50, *, source_notebook_id: str = "") -> dict: ...


class SchemaRegistryRepository(Protocol):
    def list_object_schemas(self, *, can_edit: bool = False) -> list[ObjectSchemaModel]: ...
    def list_notebook_object_schemas(
        self, notebook_id: str, *, can_edit: bool = False
    ) -> list[ObjectSchemaModel]: ...
    def create_object_schema(self, payload: ObjectSchemaCreate) -> ObjectSchemaModel: ...
    def create_notebook_object_schema(
        self, notebook_id: str, payload: ObjectSchemaCreate, *, created_by: str
    ) -> ObjectSchemaModel: ...
    def update_object_schema(self, object_type: str, payload: ObjectSchemaUpdate) -> ObjectSchemaModel: ...
    def update_notebook_object_schema(
        self, notebook_id: str, object_type: str, payload: ObjectSchemaUpdate,
        *, created_by: str,
    ) -> ObjectSchemaModel: ...
    def delete_object_schema(self, object_type: str) -> None: ...
    def delete_notebook_object_schema(
        self, notebook_id: str, object_type: str
    ) -> str: ...
    def propose_schemas(self, notebook_id: str) -> list[ObjectSchemaModel]: ...


class KnowledgeGovernanceRepository(Protocol):
    def update_knowledge(self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate) -> RuleCard: ...
    def find_duplicates(self, notebook_id: str, object_type: str) -> list[DuplicateGroup]: ...
    def merge_knowledge(self, notebook_id: str, source_id: str, payload: MergeRequest) -> RuleCard: ...
    # review_queue_page is the production read (items + same-version total in
    # one call — codex #638 R2 P2's consistency contract); the facade keeps a
    # review_queue member too, but its only remaining consumers are the
    # bit-equality oracle tests, so the typed contract exposes just the page
    # form (codex #638 R3 P2).
    def review_queue_page(self, notebook_id: str, limit: int = 100) -> dict: ...
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
        allow_without_model: bool = False,
    ) -> dict: ...
    def fail_notebook_kg_job_submission(self, job_id: str) -> bool: ...
    def finish_indexing_pipeline_job(
        self,
        notebook_id: str,
        job_id: str,
        *,
        succeeded: bool,
        pipeline_identity: tuple[str, str, str] | None = None,
    ) -> None: ...
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
        preserve_existing_rebuild: bool = False,
        indexing_pipeline_identity: tuple[str, str, str] | None = None,
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
    def start_notebook_relink(self, notebook_id: str) -> dict: ...
    def notebook_relink_status(self, notebook_id: str) -> dict: ...
    def run_notebook_relink_job(self, notebook_id: str, job_id: str) -> dict: ...
    def fail_notebook_relink_submission(self, notebook_id: str, job_id: str) -> None: ...
    def rebuild_unified_kg(self, notebook_id: str, progress: Callable[[str, int, int], None] | None = None, force: bool = False) -> int: ...
    def start_unified_kg_rebuild(self, notebook_id: str) -> dict: ...
    def unified_kg_rebuild_status(self, notebook_id: str) -> dict: ...
    def run_unified_kg_rebuild_job(self, notebook_id: str, job_id: str) -> int: ...
    def fail_unified_kg_rebuild_submission(self, notebook_id: str, job_id: str) -> None: ...
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
    def ask_answer_detail(self, answer_id: str) -> dict | None: ...
    def list_conversations(self, notebook_id: str) -> list[ConversationSummary]: ...
    def get_conversation(self, conversation_id: str) -> ConversationDetail: ...
    def rename_conversation(self, conversation_id: str, title: str) -> None: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int
    ) -> ConversationBulkDeleteResult: ...
    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse: ...

    # --- Public share links -------------------------------------------------
    # Mirrors ReportRepository's block below. `public_conversation_by_token` is
    # the ONLY conversation read reachable without a session, so it takes no
    # notebook/owner argument: the token is the whole authorization, and it must
    # never consult the current-user ContextVar (which falls back to the seeded
    # admin when unset). `conversation_creator` is the notebook-scoped ownership
    # read behind the authenticated share endpoints' row-level gate.
    # `share_conversation` refuses a conversation with no committed answer
    # atomically (raises `ConversationHasNoShareableAnswer`, no token minted), so
    # there is no separate rollback method.
    def conversation_creator(self, notebook_id: str, conversation_id: str) -> str | None: ...
    def share_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
        expected_through_id: str | None = None,
    ) -> dict: ...
    def conversation_share_state(self, notebook_id: str, conversation_id: str) -> dict: ...
    def unshare_conversation(self, notebook_id: str, conversation_id: str) -> None: ...
    def public_conversation_by_token(self, token: str) -> dict | None: ...


class ReportRepository(Protocol):
    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str: ...
    def update_report(self, notebook_id: str, report_id: str, *, status=None, progress=None, error=None, outline=None, sections=None, gaps=None, references=None, content_md=None, section_status=None, understanding=None) -> None: ...
    def claim_report_intent(self, notebook_id: str, report_id: str, understanding: dict) -> bool: ...
    def claim_report_generation(
        self,
        notebook_id: str,
        report_id: str,
        understanding: dict | None = None,
    ) -> bool: ...
    def complete_report_generation(self, notebook_id: str, report_id: str, *, sections: list, content_md: str, gaps: list, references: list) -> bool: ...
    def get_report(self, notebook_id: str, report_id: str) -> dict: ...
    # `created_by` is keyword-only and required on both listing reads (P1 group
    # sharing): reports inside a shared notebook are private to whoever created
    # them, so every call site must state which creator it means. `None` is the
    # explicit "whole notebook" escape hatch for ops/verification reads only.
    def list_reports(self, notebook_id: str, *, created_by: str | None) -> list: ...
    def delete_report(self, notebook_id: str, report_id: str) -> None: ...
    def export_reports(self, notebook_id: str, report_ids: list, *, created_by: str | None) -> list: ...

    # --- Public share links -------------------------------------------------
    # `public_report_by_token` is the ONLY report read reachable without a
    # session, so it takes no notebook/owner argument: the token is the whole
    # authorization. It must never consult the current-user ContextVar, which
    # falls back to the seeded admin when unset.
    def share_report(self, notebook_id: str, report_id: str) -> str: ...
    def unshare_report(self, notebook_id: str, report_id: str) -> None: ...
    def report_share_token(self, notebook_id: str, report_id: str) -> str: ...
    def public_report_by_token(self, token: str) -> dict | None: ...


class AdminQueryRepository(Protocol):
    def list_user_usage(self) -> list[dict[str, Any]]: ...
    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]: ...
    def notebook_exists_for_owner(self, notebook_id: str, user_id: str) -> bool: ...
    def list_user_activity(
        self,
        user_id: str,
        *,
        activity_type: str | None = None,
        include_inaccessible_questions: bool = False,
        notebook_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        before_ts: str | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def pending_actions_projection_rows(self, user_id: str) -> dict: ...
    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse: ...
    def load_notebook_scale_facts(self, notebook_id: str) -> NotebookScaleFacts: ...


class AskExecutionPort(Protocol):
    def preview_reasoning_intent(
        self, notebook_id: str, question: str, history: str = "",
        cancel_event: CancelEvent = None
    ) -> QueryIntentContract: ...
    def validate_reasoning_submission(
        self, notebook_id: str, payload: AskRequest
    ) -> None: ...
    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse: ...
    def ask_chunk(self, notebook_id: str, payload: AskRequest, cancel_event: CancelEvent = None) -> AskResponse: ...
    def ask_reasoning(self, notebook_id: str, payload: AskRequest, on_trace: Callable[[Any], None] | None = None, cancel_event: CancelEvent = None) -> AskResponse: ...


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
    def indexing_pipeline_state(self, notebook_id: str) -> dict[str, str]: ...
    def set_indexing_pipeline_desired(
        self, notebook_id: str, pipeline_id: str, pipeline_version: str
    ) -> str: ...
    def attach_indexing_pipeline_job(
        self, notebook_id: str, generation: str, job_id: str
    ) -> bool: ...

# Stable database-write page shared by both indexing-stage publishers.  This
# bounds transient id lists; it never truncates a product because publishers
# continue until the matching set is empty.
INDEXING_PIPELINE_PUBLISH_DELETE_BATCH = 500


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
    def begin_indexing_pipeline_stage(
        self,
        job_id: str,
        notebook_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
        source_ids: Sequence[str],
    ) -> None: ...
    def stage_indexing_pipeline_chunks(
        self, job_id: str, source_id: str, payload: dict
    ) -> bool: ...
    def stage_indexing_pipeline_kg(
        self, job_id: str, source_id: str, payload: dict
    ) -> bool: ...
    def complete_indexing_pipeline_stage_without_kg(self, job_id: str) -> bool: ...
    def discard_indexing_pipeline_stage(self, job_id: str) -> None: ...
    def publish_indexing_pipeline_success(
        self,
        job_id: str,
        notebook_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
    ) -> bool:
        """Atomically publish staged products, identity and job terminal."""
        ...
    def fail_submission(self, job_id: str) -> bool: ...


# Both catalog stores share these bounds. They live here, on the neutral port,
# rather than in either adapter: a backend store must never import the other
# backend's module, and duplicating a bound in two adapters is exactly how the
# two silently drift into paging differently.
#
# MAX_CANDIDATE_BATCH bounds one ``add_candidates`` write (one section's rows);
# the pure layer already caps a section's rejections and its candidate list, so
# this is the write-side belt to those braces. MAX_CANDIDATE_PAGE is the hard
# ceiling on any single candidate page regardless of what a caller asks for.
CATALOG_MAX_CANDIDATE_BATCH = 128  # rows per INSERT batch; the input is
# CHUNKED to this size, never truncated to it — a store that drops rows the
# caller already counted produces exactly the under-report this feature
# exists to eliminate.
CATALOG_MAX_CANDIDATE_PAGE = 100
CATALOG_CANDIDATE_STATES = frozenset(
    {"candidate", "rejected", "applied", "dismissed"}
)
CATALOG_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@runtime_checkable
class CatalogStorePort(Protocol):
    """Durable state for command-catalog extraction: one job row per run plus
    its reviewable candidate rows.

    Every read here is bounded on purpose. ``list_candidates`` is a keyset page
    over ``(job_id, state, position)``; ``candidates_by_ids`` and
    ``pending_candidates`` take explicit caps; ``preview_elements`` clips both
    the row count and each row's text so a cost preview can never turn into a
    full-source scan; ``source_text_stats`` is bounded the other way — it
    scans every row of one source, but returns only two integers, never any
    element text."""

    def create_job(
        self,
        notebook_id: str,
        source_id: str,
        created_by: str,
        *,
        source_generation: str = "",
    ) -> dict: ...
    def get_job(self, job_id: str) -> dict: ...
    def latest_job(self, source_id: str) -> dict | None: ...
    def active_job(self, source_id: str) -> dict | None: ...
    def latest_applied_table_id(self, source_id: str) -> str:
        """The most recently landed target across EVERY job this source has
        ever had — a bounded point query on the same
        ``(source_id, created_at DESC, id DESC)`` index ``latest_job`` already
        uses, narrowed to rows whose ``applied_table_id`` is non-empty. ``""``
        when no job for this source has ever landed a row anywhere.

        R18 (codex PR #412 review round 18): read by
        ``CommandCatalogService._resolve_target_table`` as the fallback a
        rerun's brand NEW job (its own ``applied_table_id`` still empty) uses
        so its confirm converges on the table an EARLIER job for the same
        source already wrote to, instead of re-deriving a title that may no
        longer resolve it (a knowhow rename, or a paper-metadata backfill
        changing the source's canonical title) and forking a second table.

        The id this returns is a HINT, exactly like ``job["applied_table_id"]``
        itself — the caller re-proves both existence and notebook membership
        before trusting it; this method does not touch ``knowhow_tables`` at
        all.
        """
        ...
    def start_job(self, job_id: str, sections_total: int) -> bool: ...
    def set_section_total(self, job_id: str, sections_total: int) -> bool: ...
    def record_section(
        self,
        job_id: str,
        *,
        entries: int,
        rejected: int,
        uncovered: int,
        truncated: int = 0,
    ) -> bool: ...
    def set_applied_table_id(self, job_id: str, table_id: str) -> bool:
        """Remember which knowhow table THIS job's confirms have landed in.

        Written once the first successful ``apply`` resolves a target (create
        or find-by-title) and read back on every later ``apply`` for the SAME
        job so a second confirmation page targets the SAME table even if a
        person renamed it in between — resolving by title again would either
        miss it (rename) or, worse, resolve a DIFFERENT table that happens to
        share the derived title with another source. Unconditional: apply is
        legal regardless of the job's own run status.
        """
        ...
    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        failure_reason: str = "",
        diagnostic: str = "",
    ) -> bool: ...
    def add_candidates(self, rows: Sequence[Mapping[str, Any]]) -> None: ...
    def update_candidate_payload(
        self,
        candidate_id: str,
        payload: Mapping[str, Any],
        reject_info: Mapping[str, Any],
    ) -> bool:
        """Overwrite ONE still-unreviewed candidate's payload and reject_info.

        v2's cross-window merge is the only writer. A command's documentation
        routinely spans several extraction windows (a long options table, or a
        second mention in an EXAMPLES chapter), and the catalog holds one row
        per command — so the row written when the command was first seen is
        revised in place as later windows add parameters to it, rather than a
        second row being appended for the same name.

        Deliberately narrow. It touches ``payload``/``reject_info`` and nothing
        else: ``position`` is the keyset cursor the review page pages on,
        ``state`` is the review lifecycle, and ``command_name``/``section_path``
        identify the row — a merge revises what is KNOWN about a command, never
        which command it is or where the reviewer finds it. It is also scoped to
        ``state='candidate'``, so a row a person already confirmed or skipped
        mid-run is left exactly as they saw it (the knowhow row it produced was
        written from the old payload and would not be revised by a later write
        here anyway); ``False`` says the row was not there to revise.
        """
        ...
    def list_candidates(
        self, job_id: str, *, state: str, cursor: int, limit: int
    ) -> list[dict]: ...
    def candidate_counts(self, job_id: str) -> dict[str, int]: ...
    def candidates_by_ids(
        self, job_id: str, candidate_ids: Sequence[str], *, limit: int
    ) -> list[dict]: ...
    def pending_candidates(self, job_id: str, *, limit: int) -> list[dict]: ...
    def mark_candidates_applied(
        self, job_id: str, candidate_ids: Sequence[str]
    ) -> int: ...
    def mark_candidates_dismissed(
        self,
        job_id: str,
        candidate_ids: Sequence[str],
        *,
        reject_info: Mapping[str, Any],
    ) -> int:
        """Move candidates OUT of ``candidate`` state without landing a row.

        Used for apply-time conflicts: a candidate whose command already has a
        row is left un-applied (v1's conservative merge, see
        ``CommandCatalogService.apply``) but must still leave ``candidate``
        state, or ``pending_candidates``'s cursor=0 keyset read would return
        the exact same page forever and a source whose first page is entirely
        conflicts could never be confirmed past it. ``dismissed`` is a
        terminal, non-`candidate` state distinct from ``rejected`` (which
        means "the model/grounding pass itself produced nothing usable") —
        this row WAS a legitimate command, it just already exists.
        """
        ...
    def expire_pending_candidates(
        self, job_id: str, *, reject_info: Mapping[str, Any]
    ) -> int:
        """Dismiss every still-``candidate`` row of one job in one statement.

        The complete-set counterpart of ``mark_candidates_dismissed``: that one
        takes an explicit, page-capped selection (apply's conflict set), this
        one takes the whole job. Used when a reparse invalidates a run — the
        restart guard reads "are any candidates still unreviewed", so a
        page-bounded expiry would leave a large job blocked forever.
        """
        ...
    def source_element_generation(self, source_id: str) -> str:
        """Opaque token that changes IFF this source's ``source_elements`` were
        swapped by ``replace_elements`` (a reparse).

        Snapshotted into ``catalog_jobs.source_generation`` when a run starts,
        and re-read before ``apply``/``dismiss``: a candidate row's command
        name, excerpt and section path all point at the element generation the
        run actually read, so confirming them after a reparse would write
        content the document no longer contains.

        Deliberately narrower than ``source_change_signal_rows``' token, which
        is built from ``sources.updated_at`` and is intentionally COARSE (it
        also moves on lifecycle transitions that never touch the elements).
        That signal feeds a count cache, where a false invalidation costs a
        recount; this one refuses a confirm and tells the user their source was
        reparsed, so it must not fire when it was not. Bounded: one indexed
        aggregate over one source, on human-paced paths only.
        """
        ...
    def preview_elements(
        self, source_id: str, *, limit: int, text_chars: int
    ) -> tuple[list[dict], bool]:
        """A row-capped, per-row-clipped prefix of one source's elements.

        Each row carries ``id`` / ``element_type`` / ``text`` /
        ``section_path`` plus ``full_chars``: the element's WHOLE stripped
        length, normalised exactly as ``source_text_stats`` normalises its
        sum. It cannot be derived from ``text`` (that string has already lost
        everything past ``text_chars``), and the caller needs it to subtract
        this prefix from that total exactly.
        """
        ...
    def source_text_stats(self, source_id: str) -> tuple[int, int]:
        """``(element_count, total_chars)`` over ALL of one source's elements —
        the same row universe ``preview_elements`` reads (``WHERE
        source_id=?``, no other predicate), just aggregated in SQL instead of
        clipped and returned.

        Feeds the v2 cost preview's ``estimated_windows`` (windows are packed
        by character count, so the window count is a function of the source's
        TOTAL text, not the bounded prefix ``preview_elements`` is willing to
        hydrate). One indexed, bounded scan per call — ``COUNT(*)`` and a sum
        over a single source's rows, computed entirely in SQL — and it
        transmits zero element text back to Python, unlike
        ``preview_elements`` which exists precisely to avoid that for its own
        (smaller, row-capped) read.

        ``total_chars`` sums each element's text AFTER stripping leading and
        trailing whitespace, mirroring the packer
        (``command_catalog._window_elements``). Raw ``LENGTH`` would make the
        caller's window arithmetic OVER-count — the one direction it may not
        err in, since the number is published as a lower bound. The join
        separators the packer inserts BETWEEN elements are deliberately not
        counted: that omission can only shrink the total, which keeps the
        bound on the safe side.
        """
        ...


@runtime_checkable
class SourceStorePort(Protocol):
    def all_visible_source_ids(self, notebook_id: str) -> list[str]: ...
    def hidden_source_ids(
        self, notebook_id: str, owner_id: str
    ) -> list[str]: ...
    def visible_source_scope_snapshot(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple[list[str], int]: ...
    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...
    def image_asset_rows(
        self, element_ids: Sequence[str]
    ) -> list[tuple[str, Any]]:
        """``(element_id, metadata)`` for the requested ids that are IMAGE
        elements — filtered in SQL, and WITHOUT the ``text`` column.

        Deliberately not ``evidence_elements`` plus a Python-side type test.
        The citation-image pass asks about every candidate element of every
        cited chunk, so the wide reader drags each of those element BODIES
        across the wire to answer a question about a handful of figures
        (measured on a 40-section MinerU PDF: 2 750 KiB of element text for one
        answer that attached zero images; a workbook-shaped source asks about
        908 ids at once).  Both selective predicates — the id set and
        ``element_type='image'`` — belong in the database, and ``text`` is
        never read on this path.  ``metadata`` arrives as the stored JSON TEXT
        on both backends, the same carrier ``evidence_elements`` hands over.
        """
        ...
    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...
    @staticmethod
    def retrieval_element_rows(
        db: object,
        notebook_id: str,
        allowed_source_ids: Sequence[str] | None = None,
    ) -> list[Any]: ...
    def source_titles(self, source_ids: list[str]) -> dict[str, str]: ...
    def report_source_rows(
        self, notebook_id: str, *, representative_limit: int = 20,
        distribution_limit: int = 32,
    ) -> dict[str, Any]: ...
    def report_source_identity_rows(
        self, source_ids: Sequence[str]
    ) -> list[dict[str, Any]]: ...
    def get_source(self, source_id: str) -> SourceDetail: ...
    def mark_indexing_chunk_fallback(self, source_id: str, warning_code: str) -> None:
        """Persist/clear the visible chunk-fallback marker on the source row.

        Non-empty ``warning_code`` writes the stable
        ``INDEXING_CHUNK_FALLBACK_WARNING_PREFIX`` diagnostic only when
        ``error_message`` is empty or already carries this prefix (a MinerU
        degradation diagnostic must not be clobbered); empty ``warning_code``
        clears only this prefix's own diagnostic."""
        ...
    @staticmethod
    def source_exists_tx(connection: object, source_id: str) -> bool: ...
    @staticmethod
    def source_exists_for_update_tx(
        connection: object, source_id: str, notebook_id: str | None = None
    ) -> bool: ...
    def source_elements(self, source_id: str) -> list[SourceElement]: ...
    def source_elements_page(self, source_id: str, offset: int = 0, limit: int = 40, anchor_element_id: str = "") -> PaginatedSourceElements: ...
    def source_elements_after(
        self, source_id: str, after: "tuple[Any, str] | None", limit: int
    ) -> "tuple[list[SourceElement], tuple[Any, str] | None]":
        """One keyset page of ``source_id``'s elements in the SAME global order
        ``source_elements`` returns (``ORDER BY created_at, id``), plus the
        cursor for the next page (``None`` once the walk is exhausted).

        This is the bounded reader for whole-source PIPELINES (re-embedding),
        distinct from ``source_elements_page``: that one is the source-detail
        window and pages by ``OFFSET``, which is O(n²) over a whole source.
        The row-value comparison ``(created_at, id) > (?, ?)`` keeps every page
        an index range seek on ``idx_source_elements_source_created``.

        ``after`` carries values THIS adapter returned earlier and is never
        reparsed or reformatted — the same contract (and the same reason) as
        ``source_element_type_page``: PostgreSQL hands back ``timestamptz``
        ``datetime`` values, and a text round trip could skip or repeat a row
        at a page boundary through a microsecond/offset difference.

        Unlike ``source_elements``/``source_elements_page`` this does NOT probe
        for the source's existence: a missing source is indistinguishable from
        an exhausted one (``([], None)``), never a ``KeyError``. Callers that
        need the distinction take it from ``get_source`` before walking — a
        per-page existence probe would be a query per page for a fact the walk's
        one caller has already established.
        """
        ...
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
    def visible_parse_status_counts(
        self, db: object, notebook_id: str
    ) -> list[tuple[str, int]]:
        """``[(parse_status, count)]`` over the notebook's USER-VISIBLE sources.

        A separate query rather than a fourth field on
        ``source_change_signal_rows``: that row's ``change_signal`` is
        contractually OPAQUE ("never parse it"), so the ``parse_status`` baked
        into it is not readable by a consumer even though the characters are
        physically there.  Widening that tuple instead would push a column into
        the collection catalog's hottest read for a caller it does not have.

        Bounded by construction — the notebook's source rows are visited once
        (the same ``notebook_id``-prefixed seek the signal query uses; the
        visible-source predicate is a residual filter on rows already in hand,
        never a second access path) and the RESULT is one row per distinct
        status, i.e. a handful.  The visible predicate is each adapter's own
        ``VISIBLE_SOURCE_TYPES_PREDICATE``, the same one ``list_sources`` and
        ``source_change_signal_rows``' ``user_visible`` projection evaluate, so
        the denominator here means what the source tab shows.
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
    def visible_source_identity_rows_bounded(
        self, db: object, notebook_id: str, limit: int
    ) -> list[Any]:
        """At most ``limit`` user-visible source identity rows.

        This is the bounded identity-catalog primitive used before a reasoning
        run accepts a model-proposed source restriction.  It carries the same
        display columns as ``source_display_rows``, applies the source tab's
        hidden synthetic-source predicate in SQL, and orders by ``created_at,
        id``.  Callers pass at most their hard catalog ceiling plus one, so a
        large notebook never has to materialize its full source roster merely
        to prove that the ceiling was exceeded.
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
    def question_index_chunk_page(
        self,
        notebook_id: str,
        *,
        after_id: str,
        limit: int,
        include_existing: bool,
    ) -> list[dict[str, Any]]: ...
    def replace_chunk_questions(
        self,
        chunk_id: str,
        notebook_id: str,
        source_id: str,
        rows: Sequence[tuple[str, str, Any]],
        *,
        created_at: str,
    ) -> None: ...
    def question_index_rows(
        self,
        notebook_id: str,
        *,
        actor_id: str,
        allowed_source_ids: Sequence[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...
    def question_index_stats(self, notebook_id: str) -> dict[str, int]: ...
    @staticmethod
    def language_probe_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def retrieval_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def ids_for_sources(
        db: object, notebook_id: str, source_ids: Sequence[str]
    ) -> list[Any]: ...
    @staticmethod
    def count_row(db: object, notebook_id: str) -> Any: ...
    @staticmethod
    def hydrate_rows(db: object, chunk_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def graph_hydrate_rows(db: object, chunk_ids: Sequence[str]) -> list[Any]: ...
    @staticmethod
    def retrieval_contribution_rows(
        db: object,
        notebook_id: str,
        chunk_ids: Sequence[str],
        *,
        actor_id: str,
        source_mode: str | None,
        source_ids: Sequence[str],
    ) -> list[Any]: ...
    @staticmethod
    def id_element_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def chunks_for_element_ids(
        db: object, notebook_id: str, element_ids: Sequence[str]
    ) -> list[Any]: ...
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
    def lock_schema_registry(db: object) -> None: ...
    @staticmethod
    def schema_rows(db: object) -> list[Any]: ...
    @staticmethod
    def schema_row(db: object, object_type: str) -> Any | None: ...
    @staticmethod
    def notebook_schema_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def notebook_schema_row(
        db: object, notebook_id: str, object_type: str
    ) -> Any | None: ...
    @staticmethod
    def insert_notebook_schema(db: object, **values: Any) -> None: ...
    @staticmethod
    def update_notebook_schema_columns(
        db: object, notebook_id: str, object_type: str,
        updates: List[str], values: List[object],
    ) -> None: ...
    @staticmethod
    def delete_notebook_schema_row(
        db: object, notebook_id: str, object_type: str
    ) -> None: ...
    @staticmethod
    def notebook_schema_has_objects(
        db: object, notebook_id: str, object_type: str
    ) -> bool: ...
    @staticmethod
    def legacy_typed_table_ids(
        connection: object, object_types: Sequence[str], id_prefix: str
    ) -> list[str]: ...
    @staticmethod
    def active_object_count(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def count_active_objects(db: object, notebook_id: str) -> int: ...
    @staticmethod
    def community_context_rows(db: object, notebook_id: str, members: object) -> list[Any]: ...
    @staticmethod
    def concept_embedding_rows(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def delete_notebook_graph_rows(db: object, notebook_id: str) -> None: ...
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
    def prune_cluster_rows_for_source(
        connection: object, notebook_id: str, source_id: str,
        keep_object_ids: object = (),
    ) -> int: ...
    @staticmethod
    def validate_source_fact_publish(
        connection: object, notebook_id: str, source_id: str,
        source_generation: str, element_ids: Sequence[str]
    ) -> None: ...
    @staticmethod
    def validate_stage_source_elements(
        connection: object, notebook_id: str, source_id: str,
        element_ids: Sequence[str]
    ) -> None: ...
    @staticmethod
    def insert_source_fact_rows(
        connection: object,
        rows: object,
        element_rows: object,
        *,
        projection_origin: str = "live",
    ) -> None: ...
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
    def relink_source_page(
        db: object,
        notebook_id: str,
        after_created_at: object,
        after_id: str,
        limit: int,
    ) -> list[Any]: ...
    @staticmethod
    def relink_orphan_source_ids(db: object, notebook_id: str) -> list[Any]: ...
    @staticmethod
    def relink_object_rows_for_source(
        db: object, notebook_id: str, source_id: str
    ) -> list[Any]: ...
    @staticmethod
    def relink_relation_rows_for_objects(
        db: object, notebook_id: str, object_ids: object
    ) -> list[Any]: ...
    @staticmethod
    def relink_source_is_live(
        db: object, notebook_id: str, source_id: str
    ) -> bool: ...
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
    def source_ids_from_evidence(evidence_json: str | list | None) -> set: ...
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
        indexing_pipeline_id: str = "",
        indexing_pipeline_version: str = "builtin.chunk.v1",
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
    @staticmethod
    def chunk_elements_indexed(db: object, notebook_id: str) -> bool: ...
    def mark_chunk_elements_indexed(self, db: object, notebook_id: str) -> None: ...
    def stale_object_ids_for_source(self, db: object, source_id: str, notebook_id: str) -> list[str]: ...
    def clear_source_graph_state(self, db: object, source_id: str, notebook_id: str) -> None: ...
    def clear_source_extraction_state(self, db: object, source_id: str, notebook_id: str, *, clear_embeddings: bool) -> None: ...
    def retrieval_objects_compat(self, db: object, notebook_id: str, object_type: str, statuses: object, id_filter: object) -> list[dict]: ...
    @staticmethod
    def delete_relations_for_source(db: object, source_id: str) -> None: ...
    @staticmethod
    def duplicate_seed_rows(
        db: object, notebook_id: str, object_type: str
    ) -> list[dict[str, Any]]:
        """R3 T-B1 (KG-3) dedup pass 1 -- the thin BLOCKING projection
        ``find_duplicates`` needs to build ``by_seed``, in place of shipping
        every object's full ``payload``+``evidence`` for the type
        (``retrieval_objects``) when most objects land in a singleton block
        and are never looked at again.

        ``status != 'deprecated'`` is pushed down (the old code fetched every
        status and filtered in Python): no LIMIT sits above this predicate,
        so the total row count visited is unchanged -- only the per-row
        transfer/construction cost for excluded rows is removed.

        Rows for ``object_type in {'concept','claim','formula'}`` carry a raw
        ``name`` column (``payload->>'name'`` / ``json_extract(payload,
        '$.name')`` -- dialect-native type, NOT yet coerced to text) because
        the seed function and the acronym alias map only ever read
        ``payload["name"]``. ``object_type == 'procedure'`` carries the FULL
        ``payload`` instead, under the ``payload`` key -- ``seed_procedure``
        also needs ``payload["steps"]``'s signature, which has no scalar SQL
        projection. Ordered ``created_at ASC, id ASC``, identical to
        ``retrieval_objects``'s no-id-filter branch: ``find_duplicates``'s
        ``by_seed`` insertion order, each block's member order, and the
        final stable sort's tie-break all derive from this order.
        """
        ...
    @staticmethod
    def duplicate_member_rows(
        db: object, notebook_id: str, object_ids: Sequence[str], *, batch_size: int = 900
    ) -> list[dict[str, Any]]:
        """R3 T-B1 (KG-3) dedup pass 2 -- the FULL ``payload`` backfill for
        the members of blocks ``duplicate_seed_rows`` grouped to >=2 members
        (singleton blocks -- the majority of a notebook's objects -- never
        reach this read).

        ``evidence`` is deliberately NOT selected: the only reader,
        ``KnowledgeGovernanceService._knowledge_similarity``, is called with
        ``element_vectors={}`` from ``find_duplicates``, which makes its
        evidence-vector branch unreachable dead code (design review B5a) --
        the caller fills ``"evidence": []`` in Python instead of shipping the
        column.

        Return order is UNSPECIFIED -- callers needing pass-1 order (this
        method's only consumer does) must re-key the result by ``id`` and
        re-apply pass 1's order themselves; batching this by ``object_ids``
        chunks would scramble a single global order anyway.
        """
        ...


@runtime_checkable
class EvidenceKnowledgeContextPort(Protocol):
    def cluster_map(self, notebook_id: str) -> dict[str, str]: ...
    # 有界化(B2 热点整改批 1):knowledge_context 的 canonical 折叠只需要
    # 本次装配命中的这一小撮 id,不需要整表 cluster_map()。member→canonical,
    # 缺席的 member(非簇成员)不进返回 dict——调用方按
    # ``fold.get(object_id, object_id)`` 回退原 id,与 cluster_map() 整表
    # dict 的同一回退语义逐字等价(两者查的是同一张表,只是谓词范围不同)。
    def cluster_fold(
        self, notebook_id: str, object_ids: Sequence[str]
    ) -> dict[str, str]: ...
    def node_context(self, notebook_id: str, object_id: str) -> dict[str, Any]: ...
    def in_network_relations(
        self, participant_ids: Sequence[str], object_ids: Sequence[str]
    ) -> list[dict[str, Any]]: ...
    # 保留:唯一生产调用方已改用下面的批量 relation_support_counts(有界化
    # B1 热点整改批 1)。逐条调用每次都要整表冷缓存 edge_support_map(8.35M
    # 行~3.6GB)+ cluster_map(整表);批量版按 triples 定点查询。不删是因为
    # 语义仍然是唯一真源(批量实现按它的 docstring 差分钉住),且仍有测试
    # 双态实现着它。
    def relation_support_count(
        self, notebook_id: str, source_id: str, edge_type: str, target_id: str
    ) -> int: ...
    def relation_support_counts(
        self, notebook_id: str, triples: Sequence[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], int]: ...


class RetrievalKnowledgeStorePort(KnowledgeStorePort, Protocol):
    # Capability declaration, not dialect branching: the service asks the bound
    # adapter whether the KNN access-path hint can ever do anything, and skips
    # the sizing verdict (a version query per probe, five scale aggregates on a
    # cold cache) when it cannot.  SQLite declares False; PostgreSQL True.
    lexical_knn_capable: bool
    def fts_search(self, db: object, notebook_id: str, q: str, k: int = 30, *, allowed_source_ids: Sequence[str] | None = None, corpus_langs: Sequence[str] | None = None, allow_knn: bool = False, authoritative_source_filter: bool = False) -> list[dict[str, Any]]: ...
    def chunk_fts_search(self, db: object, notebook_id: str, q: str, k: int = 30, *, allowed_source_ids: Sequence[str] | None = None, corpus_langs: Sequence[str] | None = None) -> list[dict[str, Any]]: ...
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
    def neighbor_ids(self, db: object, notebook_id: str, object_id: str, *, endpoint: str, edge_type: str | None = None, limit: int | None = None, usable_statuses: Sequence[str] | None = None) -> list[Any]: ...
    def usable_object_rows_on(self, db: object, object_ids: Sequence[str], statuses: Sequence[str], *, batch_size: int = 500) -> list[Any]: ...
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
    def cluster_map_rows(db: object, notebook_id: str) -> dict[str, str]: ...
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
    def relation_support_rows(
        db: object, notebook_id: str, triples: list[tuple[str, str, str]]
    ) -> list[Any]:
        """Bounded point lookup by ``canonical_relations``'s primary key
        ``(notebook_id, canonical_src, edge_type, canonical_tgt)`` — the
        batched replacement for calling ``edge_support_rows`` (a per-notebook
        full-table scan, up to 8.35M rows in production) once per relation
        via ``relation_support_count``. ``triples`` holds already-canonical
        ``(canonical_src, edge_type, canonical_tgt)`` tuples, at most the
        handful of distinct relation identities one answer assembly admits.
        Returns rows with ``canonical_src, edge_type, canonical_tgt,
        support_count, source_count``; empty ``triples`` returns ``[]``
        without querying.

        两个消费者(热路径修复批 2 · R2-1 起):``relation_support_counts``
        只读 ``source_count``;``KnowledgeQueryService.annotate_edge_support``
        读 ``(support_count, source_count)`` 二元组——那是它替换掉的整表
        ``edge_support_map`` 的值形状。两列同在 PK 行内,多投影一列不改访问
        路径,所以这条仍是 canonical_relations 支撑数的唯一有界定点原语。
        """
        ...
    @staticmethod
    def replace_canonical_relations(db: object, notebook_id: str, rows: object, seq: int) -> None: ...
    @staticmethod
    def replace_communities(db: object, notebook_id: str, level: int, kept: object, names: object, deg: object, now: str) -> None: ...
    # ⚠ 必须与 `replace_communities` 在**同一个写事务**里调:板块 id 被重铸的那一刻,
    # `kg_community_edges` / `kg_source_profiles` 的每一行都成了悬空引用,而 T3 的
    # 记忆化签名(state 的 seq + 账本行的 seq/created_at)在 `force=True` 的同 seq
    # 重铸上一个字段都不会变。理由与「为什么只作废这两份」见
    # `app.domain.kg_analysis_contracts.BOARD_DEPENDENT_ARTIFACT_KINDS`。
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
    def citation_source_info(
        self, source_ids: Iterable[str]
    ) -> dict[str, dict[str, str]]: ...
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
        # ``priority_object_ids`` are assembled before the rest and may carry a
        # tighter sub-budget.  It is one call, not two, because the ``relations:``
        # line is computed over this call's own evidence map: splitting the hits
        # across two calls silently drops every edge whose endpoints land on
        # opposite sides.
        priority_object_ids: Sequence[str] = (),
        priority_budget_chars: int | None = None,
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
    def retrieve_scored(self, notebook_id: str, query: str, types: Iterable[str] | None = None, w_keyword: float = W_KEYWORD, w_semantic: float = W_SEMANTIC, *, allowed_source_ids: Iterable[str] | None = None) -> list[RetrievedKnowledge]: ...
    def retrieve_relations_scored(self, notebook_id: str, query: str) -> list[RetrievedRelation]: ...
    def relations_with_names(self, notebook_id: str, relation_ids: list[str] | None = None) -> list[dict]: ...
    def federated_retrieve(self, active_notebook_id: str, query: str, types: Iterable[str] | None = None, w_keyword: float = W_KEYWORD, w_semantic: float = W_SEMANTIC, *, allowed_source_keys: Iterable[tuple[str, str]] | None = None) -> list[RetrievedKnowledge]: ...
    def federated_retrieve_relations(self, active_notebook_id: str, query: str) -> list[RetrievedRelation]: ...
    def retrieve_neighbors(self, notebook_id: str, object_id: str, edge_type: str | None = None, direction: str = "both") -> NeighborExpansion: ...
    def retrieve_elements(self, notebook_id: str, query: str, limit: int = 8, *, allowed_source_ids: Iterable[str] | None = None) -> list[RetrievedElement]: ...
    def federated_retrieve_elements(self, active_notebook_id: str, query: str, *, allowed_source_keys: Iterable[tuple[str, str]], limit: int = 8) -> list[RetrievedElement]: ...
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
    def hydrate_chunk_candidates(
        self, candidate_ids: Iterable[str]
    ) -> tuple[list[dict[str, Any]], list[str], Any]: ...
    def hydrate_retrieval_contribution_chunks(
        self, notebook_id: str, actor_id: str, candidate_ids: Iterable[str]
    ) -> list[RetrievedChunk]: ...
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
    # merge_chunk_candidates was dropped from this port when #489's ask_service
    # rewrite removed its last port-mediated call site; the protocol-coverage
    # guard requires every declared member to have a live service/route call.
    def select_chunk_candidates(self, scored: list[RetrievedChunk], ids: list[str], matrix: np.ndarray | None, k: int, lambda_: float) -> list[RetrievedChunk]: ...
    def has_kg(self, notebook_id: str) -> bool: ...
    def any_base_has_kg(self, notebook_id: str) -> bool: ...


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
    def build_chunk_question_index(
        self,
        notebook_id: str,
        *,
        workers: int,
        force: bool = False,
        progress: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int]: ...
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
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        only_source_id: str | None = None,
    ) -> list[dict[str, object]]: ...
    def missing_element_embedding_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        only_source_id: str | None = None,
    ) -> list[dict[str, object]]: ...
    # 交互式 backfill 的「单次发现 → 按页主键 hydrate」对(审计批4修订):`_page` 的
    # keyset 在无 ANALYZE 的 SQLite 上每页都退化成整表主键区间扫,逐源分页因此比一次
    # 发现贵一个数量级;id 只有几十字节,发现一次全取回来、正文才按页取。
    def missing_chunk_embedding_ids(self, notebook_id: str, *, only_source_id: str | None = None) -> list[str]: ...
    def missing_element_embedding_ids(self, notebook_id: str, *, only_source_id: str | None = None) -> list[str]: ...
    def chunk_texts_by_ids(self, ids: Sequence[str]) -> list[dict[str, object]]: ...
    def element_texts_by_ids(self, ids: Sequence[str]) -> list[dict[str, object]]: ...
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
    def selected_source_graph_artifact_status(
        self, notebook_id: str
    ) -> dict[str, object]: ...
    def begin_source_index_backfill(
        self, notebook_id: str, *, force: bool = False
    ) -> dict[str, object]: ...
    def resume_source_index_backfill_batch(
        self, notebook_id: str, *, batch_size: int = 2000
    ) -> dict[str, object]: ...
    def mark_source_index_backfill_failed(
        self, notebook_id: str, failure_code: str
    ) -> None: ...
    def begin_chunk_element_backfill(
        self, notebook_id: str, *, force: bool = False
    ) -> dict[str, object]: ...
    def resume_chunk_element_backfill_batch(
        self, notebook_id: str, *, batch_size: int = 2000
    ) -> dict[str, object]: ...
    def mark_chunk_element_backfill_failed(
        self, notebook_id: str, failure_code: str
    ) -> None: ...
    def clear_chunk_element_index(self, notebook_id: str) -> int: ...
    def chunk_elements_indexed(self, notebook_id: str) -> bool: ...
    def clear_source_index(self, notebook_id: str) -> int: ...
    def backfill_source_index_batch(
        self, notebook_id: str, last_id: str, batch_size: int
    ) -> tuple[int, int, str]: ...
    def mark_source_index_backfilled(self, notebook_id: str) -> None: ...
    def source_fact_backfill_target_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]: ...
    def source_index_backfilled(self, notebook_id: str) -> bool: ...
    def backfill_source_fact_batch(
        self,
        notebook_id: str,
        source_id: str,
        *,
        batch_size: int = 500,
        projection_version: int = 1,
        force: bool = False,
    ) -> dict[str, object]: ...
    def mark_source_fact_backfill_failed(self, source_id: str, code: str) -> None: ...


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
    # ⚠ reference-only(审计批4):这两条无界 rows 声明**没有生产调用方**——它们把该
    # notebook/该源的每一行全文一次性物化进内存,大库上就是 GB 级。留着只因为它们是判据的
    # 参考实现(等价差分测试拿它们当基准)。生产要「缺哪些向量」走
    # ``missing_*_embedding_ids`` + ``*_texts_by_ids``(单次发现 + 按页主键 hydrate),
    # 离线 CLI 走 ``missing_*_embedding_page``。勿在生产接回。
    def missing_chunk_embedding_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def missing_element_embedding_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def missing_chunk_vector_source_ids(self, notebook_id: str) -> list[str]: ...
    def missing_element_vector_source_ids(self, notebook_id: str) -> list[str]: ...
    def embed_elements_batch(self, notebook_id: str, items: list[dict[str, object]]) -> int: ...
    def embed_chunks_batch(self, notebook_id: str, items: list[dict[str, object]]) -> None: ...
    def embed_chunks_for_source(self, source_id: str) -> None: ...
    def chunk_and_embed_source(self, source_id: str) -> None: ...
    def embed_objects_batch(self, notebook_id: str, items: list[dict[str, object]], progress: EmbeddingProgress | None = None, commit_every: int | None = None) -> int: ...
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
    def load_scale_index(self, notebook_id: str, allow_stale: bool = False) -> ScaleIndexView | None: ...
    def has_scale_index(self, notebook_id: str) -> bool: ...
    def gold_knowledge_object_rows(self, notebook_id: str) -> list[dict[str, object]]: ...
    def kg_object_counts_by_notebook(self) -> dict[str, int]: ...
    def latest_done_report(self) -> dict[str, object] | None: ...
    def sample_approved_object_payload(self, notebook_id: str) -> str | None: ...
    def chunk_notebook_map(self, chunk_ids: Sequence[str]) -> dict[str, str]: ...
    def count_text_vector_rows(self, table: str, id_col: str, notebook_id: str | None) -> int: ...
    def convert_text_vector_batch(self, table: str, id_col: str, notebook_id: str | None, batch_size: int, encode: VectorBatchEncoder) -> tuple[int, int]: ...
    def begin_source_index_backfill(self, notebook_id: str, *, force: bool = False) -> dict[str, object]: ...
    def resume_source_index_backfill_batch(self, notebook_id: str, *, batch_size: int = 2000) -> dict[str, object]: ...
    def mark_source_index_backfill_failed(self, notebook_id: str, failure_code: str) -> None: ...
    def begin_chunk_element_backfill(self, notebook_id: str, *, force: bool = False) -> dict[str, object]: ...
    def resume_chunk_element_backfill_batch(self, notebook_id: str, *, batch_size: int = 2000) -> dict[str, object]: ...
    def mark_chunk_element_backfill_failed(self, notebook_id: str, failure_code: str) -> None: ...
    def clear_chunk_element_index(self, notebook_id: str) -> int: ...
    def chunk_elements_indexed(self, notebook_id: str) -> bool: ...
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


#: Agentic Memory P3 (B-Profile, T7) — bounded read size and the two
#: thresholds the deterministic inference job applies to what
#: ``AskStateStorePort.recent_user_ask_languages`` returns. Registered here,
#: module-level and beside the port they describe, the same placement as
#: ``RETRIEVAL_EXPERIENCE_BATCH_RUNS``/``AGENT_OBSERVATION_RING_MAX`` beside
#: their own ports rather than inside ``search_profile_job.py``: these
#: numbers describe the SHAPE of the read (how many rows, how sure a
#: majority has to be before the job trusts it), which is a property of the
#: read itself, not of whichever caller happens to invoke it.
#:
#: ``SAMPLE_LIMIT`` bounds the read; ``MIN_SAMPLES`` is the floor below which
#: the job writes nothing at all (a person's first few questions are not
#: enough evidence either way); ``MAJORITY_RATIO`` is how dominant one
#: language's share of the FULL bounded sample has to be before the job
#: calls it a "明确多数" rather than a toss-up it should stay silent about —
#: "other" counts in the denominator (it is real, sampled evidence that this
#: person's recent questions were not all clearly one language) but can never
#: itself be the winning candidate (``search_profile_job._WRITABLE_LANGUAGES``
#: is the closed ``("zh", "en")`` tuple the job may ever write). T9 fix round:
#: this docstring previously said "non-'other' sample", which described a
#: denominator the code has never actually computed — deliberately keeping
#: the full sample here means "other" rows dilute a real language's apparent
#: majority instead of being silently excluded from the count, the more
#: conservative of the two readings (see :func:`app.services.search_profile.
#: classify_ask_language`'s own docstring for the flip side of this same
#: choice, and ``test_search_profile_job.py`` for the boundary case this
#: keeps distinguishable from the excluded-denominator alternative).
#:
#: Exact values are registered in ``docs/product-and-api*.md`` only; these
#: names are the protocol boundary, not a deployment knob a config file
#: could raise (same registration rule as ``AGENT_OBSERVATION_RING_MAX``
#: above).
SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT = 30
SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES = 10
SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO = 0.7


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
    def ask_answer_detail(self, answer_id: str) -> dict | None: ...
    def get_conversation(self, conversation_id: str) -> ConversationDetail: ...
    def rename_conversation(self, conversation_id: str, title: str) -> None: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int, user_id: str
    ) -> ConversationBulkDeleteResult: ...
    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse: ...
    def recent_user_ask_traces(
        self,
        notebook_id: str,
        user_id: str,
        *,
        job_limit: int,
        step_limit: int,
    ) -> list[dict]: ...
    # ⚠ Agentic Memory P1 (T5) — the ONE read that crosses from "this notebook"
    # into "this member's own use of it", and therefore the one place in this
    # port where the ``user_id`` argument is a PRIVACY BOUNDARY rather than an
    # audit attribution.
    #
    # Both statements behind it carry ``created_by = ?`` IN THE SQL TEXT, the
    # trace statement included even though its job ids were already narrowed by
    # the first one: a redundant predicate costs nothing on an indexed column,
    # while a Python-side filter is one refactor away from being dropped by
    # someone who cannot see why it was there. ``memory_items.created_by`` is
    # the same rule for the same reason.
    # ``backend/tests/test_agent_profile_isolation_guard.py`` pins that
    # statically, in both backends.
    #
    # Bounded by BOTH arguments (most recent ``job_limit`` asks, at most
    # ``step_limit`` trace rows across them) and PROJECTED through
    # ``project_trace_step``: what comes back is the member's own question text
    # plus, per step, the action type / human summary / duration / one count.
    # Never an answer body, never Memory content, never evidence text.
    def recent_completed_ask_runs(
        self, *, job_limit: int, step_limit: int
    ) -> list[dict]: ...
    # ⚠ Agentic Memory P2 (T5) — the DELIBERATELY UNSCOPED read on this port,
    # and the only one. It takes no ``notebook_id`` and no ``user_id`` because
    # it feeds the deployment-GLOBAL retrieval-experience library: what that
    # library learns is "in this shape of question, this retrieval action pays
    # off", a statement about tactics that would be worthless if it could only
    # be drawn from one person's runs.
    #
    # It therefore CANNOT borrow the safety argument of the two reads above,
    # and must not be mistaken for a relative of them. Theirs is a predicate in
    # the SQL text. This one's is the PROJECTION: the rows come back through
    # ``project_run_row``/``project_run_step``, which keep an opaque run id, a
    # closed-vocabulary engine mode, per step an action type / one count / one
    # duration, and — from the ``intent`` step alone — a situation made of
    # bools, small ints and closed enum values. No question, no summary, no
    # ``created_by``, no ``notebook_id``, no timestamp. Nothing that comes out
    # of here can identify a person, a library or a topic, which is why it is
    # safe to aggregate across all of them.
    #
    # ⚠ It must never appear in ANY of the agent-profile chains' port
    # allowlists. Both of those chains are defined by what they may NOT reach —
    # the base chain by "no member's usage at all", the overlay chain by "no
    # member's usage but its own owner's" — and a read with no user predicate
    # violates both by construction. ``test_agent_profile_isolation_guard.py``
    # pins that as a reverse guard (``GLOBAL_TRACE_READ_METHODS``).
    #
    # Bounded by BOTH arguments (``job_limit`` most-recent completed asks,
    # ``step_limit`` trace rows across them), exactly like
    # ``recent_user_ask_traces``, and for the same reason: one exhaustive
    # reasoning ask can carry a hundred steps.
    def recent_user_report_traces(
        self,
        notebook_id: str,
        user_id: str,
        *,
        report_limit: int,
        attempt_limit: int,
    ) -> list[dict]: ...
    # ⚠ Agentic Memory P2 (T4) — the SECOND read in this port whose
    # ``user_id`` argument is a PRIVACY BOUNDARY. It lives here, on
    # ``AskStateStorePort``, even though its SQL selects from ``reports`` — a
    # table this port otherwise has no business with — for exactly one
    # reason: this port already carries the ``created_by = ?``/``= %s``
    # static-guard wiring (``test_agent_profile_isolation_guard.py``'s
    # ``TRACE_READ_METHODS`` × the two ``ask_state_store.py`` files), and
    # ``test_the_base_allowlist_never_collides_with_ask_state_port_methods``
    # (Agentic Memory P1) already asserts this port's method-name set never
    # intersects the BASE chain's allowlist. Adding a ``report_store`` seat
    # instead would need a new port attribute, a new base-collision guard and
    # a new ``_STORE_PATHS`` entry — three more places this isolation could
    # be left unfinished on.
    #
    # ⚠ Attribution asymmetry, REGISTERED (Agentic Memory P2 plan, T4 拍板
    # 2/主 agent 裁决 Q5): a report's ``created_by`` is its ORIGINAL creator,
    # not necessarily the member whose retrieval this run is summarising — in
    # a shared notebook any writable member can (re)trigger generation of a
    # report someone else created (see
    # the report completed-observer's own actor projection). The
    # direction is deliberately the SAFE one: a member who reruns someone
    # else's report triggers their OWN overlay refresh (via
    # ``note_report_completed`` → ``bump_signal``) but that report never
    # enters the trigger's own sample here (its ``created_by`` is the
    # original author's id), so the refresh can legitimately come back empty
    # (``no_usage_sample`` — a normal terminal outcome, never an error)
    # rather than summarise a report the caller did not create. The
    # alternative (a ``reports.last_run_by`` column) is a new migration and a
    # new usage-data surface; P2 explicitly declined it.
    #
    # ⚠ Only ``status = 'done'`` reports are eligible — a failed or cancelled
    # report cannot say how this member searched, mirroring
    # ``note_report_completed``'s own gate (only fires on ``report_done``).
    #
    # ⚠ THIS SAMPLE CARRIES NO ZERO-HIT SIGNAL, ON PURPOSE (P2-T4 fix round,
    # spec review P1). The original design read ``attempted[j].new == 0`` as
    # "this direction came back empty" and folded it into ``usage_gaps``'
    # evidence. That reading does not hold, for four independent reasons —
    # all four have to be false for it to work, and each one alone is fatal:
    #
    #   * ``new`` counts KNOWLEDGE OBJECTS newly added to the run's SHARED
    #     candidate pool, not results for that direction. A direction that
    #     returned plenty of material already collected by an earlier
    #     direction scores 0.
    #   * a report seeds the pool with the SECTION QUESTION before running
    #     the confirmed directions, so overlapping directions score 0 by
    #     construction — which is most of them, since the directions of one
    #     section are by design about one topic.
    #   * a notebook with no knowledge graph scores 0 for EVERY direction,
    #     always: there are no KG objects to add.
    #   * chunk and element hits — the bulk of what most retrieval returns —
    #     are not counted by ``new`` at all.
    #
    # Reading it as "empty" would therefore write "this library has nothing
    # on X" into a member's PRIVATE, model-authored notes on the strength of
    # a counter that measures something else. Zero-hit evidence
    # (``zero_hit_steps`` / ``usage_gaps`` / ``empty_search_summaries``) is
    # consequently ASK-ONLY, exactly as it was before P2-T4; what this
    # sample feeds is ``retrieval_notes`` — HOW this member phrases their
    # research directions — which needs the wording, not an outcome.
    #
    # ⚠ ``failed: true`` is kept, but ONLY as a count of directions whose
    # execution errored. It is not a retrieval outcome either (a retrieval
    # that threw is not one that ran and found nothing); it exists so the
    # rendered sample can say how much of a report's direction list is
    # missing wording rather than silently listing fewer directions.
    #
    # Bounded by BOTH arguments (``report_limit`` most-recent ``done``
    # reports, ``attempt_limit`` attempt rows across them) and PROJECTED
    # through ``project_report_row``/``project_report_attempt``: what comes
    # back is the member's own report question plus, per confirmed
    # direction, that direction's own wording and whether it errored. Never
    # section markdown, never citations, never evidence text —
    # ``sections_json`` also carries full section prose per entry, so the SQL
    # does the ``$[*].attempted`` projection itself rather than pulling the
    # whole blob into Python.
    #
    # ⚠ ``attempt_limit`` truncation is NOT distinguishable from "this
    # report had no directions" in what comes back — the rows simply are not
    # there. The renderer must therefore never turn an empty ``attempts``
    # list into an assertion about the report (see
    # ``agent_profile_job.render_usage_block``); with the shipped defaults a
    # full sample overruns the cap routinely, so this is the common case,
    # not an edge one.

    def recent_user_ask_languages(
        self, user_id: str, *, limit: int
    ) -> list[dict]: ...
    # ⚠ Agentic Memory P3 (B-Profile, T7) — the THIRD read on this port whose
    # ``user_id`` argument is a PRIVACY BOUNDARY, and the narrowest
    # projection of the three. Every row it returns is EXACTLY
    # ``{"language": "zh" | "en" | "other"}`` — a closed three-value bucket
    # computed FROM the question text INSIDE the store (both backends call
    # ``app.services.search_profile.classify_ask_language`` on the row
    # before it leaves the SQL boundary), never the question text itself.
    # ``recent_user_ask_traces``/``recent_user_report_traces`` above both
    # cross this port's boundary carrying real question/direction wording —
    # safe there because their consumer (the private overlay block) renders
    # it back to the very same person who wrote it. This method feeds a
    # deterministic, ZERO-LLM inference job whose entire reason to exist is
    # to never see that text at all, anywhere outside the store, so the
    # classification happens on the near side of the read rather than the
    # far side.
    #
    # ⚠ Deliberately UNSCOPED BY NOTEBOOK — the SQL carries
    # ``created_by = ?``/``= %s`` and nothing else, unlike its two siblings
    # above which also take a ``notebook_id``. Their per-(notebook, user)
    # narrowing exists because what they write is a per-notebook overlay
    # block; a person's own spoken/written language does not change by
    # notebook, and narrowing this read to one notebook would only shrink
    # the sample for no privacy benefit — the isolation this method needs is
    # "this person, nobody else", which the ``created_by`` predicate alone
    # already gives it.
    #
    # ⚠ Reads ONLY ``status = 'done'`` jobs, mirroring
    # ``recent_completed_ask_runs``/``recent_user_report_traces`` above —
    # the trigger that drives this job (``note_ask_completed``) only fires
    # on an ask's successful-delivery path (see
    # ``AskExecutionCoordinator.start``'s ``worker()``), so a statement that
    # also matched cancelled/failed rows would disagree with what actually
    # produces a sample.
    #
    # Bounded by ``limit`` (``SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT`` at the
    # call site): the ``limit`` most recent asks by
    # ``created_at DESC, id DESC`` — newest evidence of how a person
    # currently writes, not their oldest.
    #
    # ⚠ Both backends' ``ask_state_store.py`` pin this method into
    # ``TRACE_READ_METHODS`` (``test_agent_profile_isolation_guard.py``),
    # which statically scans its SQL text for the same ``created_by``
    # predicate token the two siblings above use — even though this method
    # belongs to NEITHER chain that guard file's layer one/two classify (it
    # feeds ``search_profile_job.py``, a module those two layers never scan
    # at all). Only layer three's "does the SQL literally carry the
    # predicate" check applies here, and it is exactly the check this method
    # needs: a Python-side filter reading ``row["created_by"] == user_id``
    # after an unscoped SELECT would look identical in every test that does
    # not specifically assert the predicate is IN THE SQL TEXT.


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
    """Bounded report corpus statistics and source-identity projection."""

    def report_source_rows(
        self, notebook_id: str, *, representative_limit: int = 20,
        distribution_limit: int = 32,
    ) -> dict[str, Any]: ...

    def report_source_identity_rows(
        self, source_ids: Sequence[str]
    ) -> list[dict[str, Any]]: ...


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
    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def is_member(self, notebook_id: str, user_id: str) -> bool: ...
    def add_member(self, notebook_id: str, user_id: str) -> None: ...
    def remove_member(self, notebook_id: str, user_id: str) -> None: ...
    def kick_all_members(self, notebook_id: str) -> None: ...
    def list_members(self, notebook_id: str) -> list: ...


class LastGroupAdminError(RuntimeError):
    """把一个群组的最后一名组管理员降级/移除了。

    住在这一层(后端中性契约层)的理由与 `NotebookTooLargeToCopyError` 同款:两个
    `GroupStore` 都要在自己的写事务里抛它,而路由要 catch 它并映射成 409 —— store
    不能 import service/api,路由不能 import 某一个后端。
    """


class GroupOwnerProtectedError(RuntimeError):
    """An operation tried to demote, remove, or self-remove the current owner."""


class GroupOwnerRequiredError(RuntimeError):
    """A transaction-time recheck found that the actor is no longer owner."""


class GroupOwnerTransferTargetError(RuntimeError):
    """The requested new owner is not a current member of this group."""


class GroupGrantAlreadyExists(RuntimeError):
    """同一本笔记本上,同一个主体已经有一条授权边(UNIQUE 冲突)。

    刻意做成明确失败而不是幂等复用:两条边的 `role` 可以不同,静默返回既有行会让
    「我改成了管理」与「库里其实还是只读」这两件事在响应上长得一模一样。
    """


class GroupNotFoundError(RuntimeError):
    """写事务在**真正落库之前**复核时发现群组已不存在。

    路由层的前置检查(「这个组在不在」)与写入之间永远有一个窗口:并发的删组请求可
    以恰好落在中间。少了这条,SQLite 侧会撞 `group_members.group_id` 的外键约束抛
    `IntegrityError`(用户看到 500),PG 侧则在 `FOR UPDATE` 拿不到行之后继续往下走。
    store 在同一事务里复核并抛它,路由统一映射成 404。
    """


class NotebookNotFoundError(RuntimeError):
    """写事务在**真正落库之前**复核时发现笔记本已不存在(codex #519 R7 P2)。

    `GroupNotFoundError` 的**同类兄弟**,只是换了另一个外键父行:能力守卫
    (`require_notebook_capability("notebook:manage")`)与写事务之间同样有一个窗口,
    并发的删库请求可以恰好落在中间。少了这条复核,`INSERT INTO notebook_share_requests`
    会撞 `notebook_id` 的外键——SQLite 抛 `IntegrityError`、PG 抛 `ForeignKeyViolation`,
    两边都是未处理异常 → 用户拿到 **500**,而正确答案是 404。

    ⚠ 它复核的是**外键父行的存在性**,不是权限。权限那条轴由
    `NotebookManageRequiredError` 承担(判据见 `api/deps.py` 的裁决 P2-8:只有写
    `notebook_grants` 这类「授予他人访问权」的路径才做事务内权限复检)。两者不可互相
    替代:一个已经被删掉的库上,发起人的管理权判定本来就没有意义。

    路由映射成 **404**,与能力守卫拒绝时同一个状态码口径(不泄露存在性)。
    """


class GroupAdminRequiredError(RuntimeError):
    """写事务在落库前复核时发现请求者已不是目标群组的组管理员。

    与 `GroupNotFoundError` 同源:授权边的双重条件在路由层查过一次,但「被移出组」
    与「被降级」都可以发生在那次查询与写入之间。真正承重的是写事务里的这一次复核。
    """


class GroupMembershipRequiredError(RuntimeError):
    """写事务在落库前复核时发现请求者已不是目标群组的**成员**(群组知识共享 P2-T3)。

    与 `GroupAdminRequiredError` 同源、只是轴更低一档:提交共享申请只要求是组成员(任意
    role),不要求组管理员。路由层查过一次「你在不在这个组里」,但「被移出组」可以发生在
    那次查询与写入之间——少了事务内这一次复核,一个刚被移出组的人仍能落一条**非成员**的
    待审批申请,而组管理员的审核队列里会出现一个已经不属于本组的人。

    路由把它映射成 **404**(与路由自己那次前置检查逐字同一个响应):群组维度的「看不见」
    口径是 404,非成员与「组不存在」不可区分。
    """


class GroupAdminShouldShareDirectlyError(RuntimeError):
    """提交共享申请的人**是目标组的组管理员**——他不该走审批流(codex #519 R8 P2)。

    审批流覆盖的是**另一半**入口:对库有管理权、但对目标组只是**普通成员**的人没有直接
    发边的权限,只能申请。组管理员分享进自己管理的组**永远走 `POST /notebooks/{id}/grants`、
    不经这张表**(设计 §4 决策 9,v49/v50 迁移的 docstring 也逐字写着这一条)。此前
    store 与路由的判据都只是「有没有成员行」,于是组管理员也能建出一条 pending 申请、
    再自己批准自己——**契约早就写明了,只是实现没兑现**。

    放行判据是**正向精确匹配** `role == 'member'`:组管理员与任何未知取值(含正向 shadow
    停车写进去的哨兵串)一律落进本异常,方向是 fail closed。

    路由映射成 **403** 并给一句**可操作**的说明(「直接共享给它即可」)。⚠ 这里刻意
    **不**套群组维度那套 404 遮蔽:他是这个组的管理员,组的存在性对他根本不是秘密,回一句
    「群组不存在」只会让他去查一个没有问题的组。
    """


class NotebookManageRequiredError(RuntimeError):
    """写事务在落库前复核时发现**发起人**已不再对这本笔记本拥有管理权。

    能力守卫(`require_notebook_capability("notebook:manage")`)与写事务之间永远有一个
    窗口,库主可以在中间撤掉发起人的管理边。**这个窗口不是每个写端点都要堵**——判据见
    `api/deps.py` 里「授予他人访问权的写入必须事务内复检」那条裁决:内容写入在窗口内落库
    只是普通竞态,而**创建持久授权状态**的写入会把访问权授予他人、效力超出发起人自身权限
    的存续,必须在同一写事务内复检并锁住发起人的笔记本侧权限。

    路由映射成 **403**:能走到这一步说明他刚刚还有管理权,库的存在性对他不是秘密。
    """


class ShareRequesterUnauthorizedError(RuntimeError):
    """批准时发现**申请人**已不再对那本笔记本拥有管理权(群组知识共享 P2-T3)。

    场景:Bob 经 `group_admins` 边对库 N 有管理权 → 提交「把 N 共享给 G1」的申请 →
    库主 Alice 撤掉 Bob 的管理边 → G1 的组管理员批准 → N 的读权发给整个 G1。Bob 早已
    失权、库主从未同意,而一条**活的**授权边就这样落库了。

    ⚠ 这条**推翻**了 codex #519 R2 那轮登记的「approve 不复检申请人当前 manage 权是刻意
    设计(异步审批语义)」(R4 裁决变更)。理由是本仓库最反复钉的那条原则:**授权在生效
    时刻实时判定、绝不缓存**——挂载边不是授权凭证、撤销即时生效;P1-T3b 也正是按它裁的
    公开报告页(创建时合法 ≠ 持续有效,创建者失去读权链接即 404)。审批把一次陈旧的检查
    兑现成一条活授权边,与这条原则正相反。而且批准这一刻**没有任何一方**在验「申请人现在
    还有没有权把它交出去」:组管理员验的是「我的组要不要这个库」,库主根本不在回路里。

    路由映射成 **409** 并给出可读原因,而不是静默失败——组管理员要看得懂为什么批不动。
    申请行**刻意保留**(不自动删):审计价值大于清理,组管理员可以自行驳回。

    `reject_share_request` **不做**这条复检:驳回是终止、不产生任何授权,失权申请人的
    申请当然可以被驳回。
    """


class ShareRequestAlreadyPendingError(RuntimeError):
    """同一 (笔记本, 群组) 上**别人**已经有一条待审批申请(群组知识共享 P2-T3)。

    幂等只对**同一个申请者**成立(codex #519 R3):一本库可以有多个管理权持有者(owner +
    组管理员),两个人先后对同一个组提交时,第二个人撞上
    `uq_share_requests_one_pending`。把第一个人的行原样返回给他,会让他的界面报成功、
    而 `list_my_share_requests` 里查不到、也撤不掉,那个组对他还永远显示「可申请」——
    一个说不通的三方矛盾。所以按申请者收窄:不是本人的就明确冲突。

    路由映射成 **409**,文案只说「已有一条待审批的申请」,**不点名申请者**——谁提的是
    别人的事,冲突本身已经足够解释为什么这次没提交成功。
    """


class ShareRequestNotPendingError(RuntimeError):
    """撤回一条**已被决定**(approved/rejected)的共享申请(群组知识共享 P2-T3)。

    撤回(`DELETE /notebooks/{id}/share-requests/{rid}`)只在 `status='pending'` 时允许:
    已审批/已驳回的申请是一个既成的决定,撤回它没有意义。store 在写事务内按**精确**
    状态匹配判定(`status == 'pending'` 才删),否则抛它,路由映射成 409。与
    `GroupNotFoundError` 分开:「这条申请不存在」是 404,「它在但已决定」是 409,两者
    对用户是完全不同的两件事。
    """


@runtime_checkable
class GroupStorePort(Protocol):
    """群组 / 组成员 / 笔记本授权边的行持久化面。

    **不含任何授权判定谓词**:「谁能读这个 notebook」的唯一定义点仍是
    `repositories/*/access_sql.py`,本 store 只做 CRUD 与按主体 id 的直查。
    策略(谁能建哪一类组、双重条件的授权边创建)全在 `app/api/group_routes.py`。
    """

    def create_group(
        self, *, name: str, kind: str, description: str, created_by: str
    ) -> dict: ...
    def get_group(self, group_id: str, *, user_id: str = "") -> "dict | None": ...
    def user_group_role(self, group_id: str, user_id: str) -> "str | None": ...
    def list_groups_for_user(self, user_id: str) -> list[dict]: ...
    def list_all_groups(self, *, user_id: str = "") -> list[dict]: ...
    def list_members(self, group_id: str) -> list[dict]: ...
    def update_group(
        self,
        group_id: str,
        *,
        name: "str | None" = None,
        description: "str | None" = None,
    ) -> bool: ...
    def transfer_group_owner(
        self,
        group_id: str,
        *,
        new_owner_id: str,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> dict:
        """Atomically transfer owner to an existing member and promote them.

        ``created_by`` is never touched. The old owner remains an admin. The
        transaction locks/rechecks the group root so concurrent transfer,
        member removal, and role changes cannot leave an owner outside the
        membership set.
        """
        ...
    def delete_group(
        self,
        group_id: str,
        *,
        actor_id: "str | None" = None,
        actor_is_system_admin: bool = False,
    ) -> bool:
        """Delete the aggregate, transactionally rechecking owner when supplied."""
        ...
    def upsert_member(
        self, group_id: str, user_id: str, *, role: str, added_by: str
    ) -> str: ...
    def remove_member(self, group_id: str, user_id: str) -> bool: ...
    def get_invite_state(
        self,
        group_id: str,
        *,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> dict: ...
    def issue_invite(
        self,
        group_id: str,
        *,
        token: str,
        actor_id: str,
        actor_is_system_admin: bool = False,
        rotate: bool = False,
    ) -> dict:
        """Issue/reuse a link under the group root lock.

        The store rechecks that the actor is still a group admin in the same
        transaction that publishes the bearer capability. ``rotate`` replaces
        an existing token atomically; otherwise an existing token is reused.
        """
        ...
    def revoke_invite(
        self,
        group_id: str,
        *,
        actor_id: str,
        actor_is_system_admin: bool = False,
    ) -> bool: ...
    def join_by_invite(self, token: str, *, user_id: str) -> "dict | None":
        """Atomically resolve a live token and add ``user_id`` as a member.

        Existing membership is preserved byte-for-byte, making repeated link
        visits idempotent and preventing a link from demoting an administrator.
        ``None`` deliberately conflates unknown, revoked, and deleted links.
        """
        ...
    def find_user_by_username(self, username: str) -> "dict | None": ...
    def find_user_by_id(self, user_id: str) -> "dict | None": ...
    def list_grants(self, notebook_id: str) -> list[dict]: ...
    def create_grant(
        self,
        notebook_id: str,
        *,
        principal_type: str,
        principal_id: str,
        role: str,
        created_by: str,
        admin_user_id: str,
    ) -> dict:
        """插入一条群组授权边,并在**同一个写事务**里复核双重条件的群组那一半。

        ``admin_user_id`` 是发起者。store 在事务内复核「``principal_id`` 这个组还
        在」且「发起者仍是它的组管理员」,不成立分别抛 `GroupNotFoundError` /
        `GroupAdminRequiredError`。路由层那次前置查询只用来给出友好文案,**授权
        判定以这一次为准**——两次查询之间,组可以被删、发起者可以被移出或降级。

        **两个外键父行都要在同一写事务内复核**(与 `create_share_request` 同一条契约):
        笔记本已被并发删掉抛 `NotebookNotFoundError`(路由 → 404)。PG 侧不复核就是
        `notebook_grants.notebook_id` 的 `ForeignKeyViolation` → 500;SQLite 侧本来就
        不会 500(权限复核的两半都查不到 → `NotebookManageRequiredError`),补它是为了
        两个后端答同一句话(codex #519 R7 存疑项收口)。
        """
        ...
    def delete_grant(self, notebook_id: str, grant_id: str) -> bool: ...
    def list_group_shared_notebooks(
        self, group_id: str, *, include_admin_only: bool = True
    ) -> list[dict]: ...
    def delete_group_grants_for_notebook(
        self, group_id: str, notebook_id: str
    ) -> int: ...

    # --- 成员贡献审批流(群组知识共享 P2-T3) -----------------------------
    def create_share_request(
        self, notebook_id: str, *, group_id: str, requested_by: str
    ) -> dict:
        """新建一条共享申请;撞 `uq_share_requests_one_pending` 时**按申请者收窄**地幂等:
        既有 pending 是**本人**提的就原样返回(刷新页面重复提交是常见操作),是别人提的
        则抛 `ShareRequestAlreadyPendingError`(路由 → 409)——把别人的行返回给他会让
        「界面报成功、自查列表里没有、也撤不掉」三件事同时成立(codex #519 R3)。

        **两个外键父行都要在同一写事务内复核**:组已被并发删掉抛 `GroupNotFoundError`,
        笔记本已被并发删掉抛 `NotebookNotFoundError`(codex #519 R7 P2)——只复核其中一个
        等于把另一个的 FK 违例留成 500。`requested_by` 已不是该组成员抛
        `GroupMembershipRequiredError`——三条复核都在**同一写事务内**,路由层那次前置
        查询与写入之间的窗口足够让组被删、让库被删、让人被移出组(codex #519 R2 P2-1)。

        ``requested_by`` 必须是目标组的**普通成员**(`role == 'member'` 正向精确匹配):
        组管理员分享进自己管理的组永远走 `create_grant`、不经这张表,所以他落进
        `GroupAdminShouldShareDirectlyError`(路由 → 403 + 可操作说明)。这条同样是
        **事务内**判据而不只是路由前置检查——中间那个窗口足够让一个普通成员被提升成组
        管理员(codex #519 R8 P2)。

        冲突恢复期间那条 pending 又被决定/撤回时**重试一次插入**(此时部分唯一索引的
        谓词已不再覆盖它),绝不让原始的 DB 唯一违例冒成 500(codex #519 R3)。"""
        ...
    def list_pending_share_requests(self, group_id: str) -> list[dict]:
        """某个组的**待审批**申请清单(组管理员的审核队列)。`status='pending'` 精确匹配。"""
        ...
    def list_my_share_requests(
        self, notebook_id: str, *, requested_by: str
    ) -> list[dict]:
        """请求者本人对某本库发起过的全部申请(弹窗里回显「待审批 / 已驳回」)。"""
        ...
    def list_pending_share_requests_by_requester(
        self, requested_by: str
    ) -> list[dict]:
        """**我发起的、仍待审批的**申请,跨笔记本、不带 notebook 维度收窄(codex #519 R11)。

        与上面那条的区别是**授权轴**:那条按笔记本列(消费它的端点挂 `notebook:manage`),
        这条唯一的谓词是 `requested_by` —— 与撤回端点逐字相同的判据。裁决 P2-7 让撤回不要求
        笔记本管理权(否则失权申请人的申请既批不了也撤不掉),但申请人失权后就打不开那本
        笔记本、也就拿不到申请 id,那个口子形同虚设。只回 `status='pending'`(正向精确匹配)。
        """
        ...
    def approve_share_request(
        self,
        group_id: str,
        request_id: str,
        *,
        decided_by: str,
        decided_by_is_system_admin: bool = False,
    ) -> "dict | None":
        """批准:**同一写事务**内复核申请仍 pending + 组在 + `decided_by` 仍有审批资格
        + **`requested_by` 仍对该库有管理权** + 写 `(group, viewer)` 授权边(已共享则幂等
        复用)+ 状态置 approved、写 `decided_by`/`decided_at`。申请不存在或已被决定返回
        `None`(路由 → 404);`decided_by` 已不是该组组管理员抛 `GroupAdminRequiredError`
        (路由 → 403);`requested_by` 已失去管理权抛 `ShareRequesterUnauthorizedError`
        (路由 → 409,申请行保留)。

        ``decided_by_is_system_admin`` 由**路由**传入,不在 store 里读 `users.role` 判定
        (store 不做身份解析)。它承载的是 `_require_group_admin` 的系统管理员运维旁路:
        系统管理员不必是组成员也能审批,所以事务内的复核判据是「本人是该组组管理员 **或**
        路由已证明他是系统管理员」。
        """
        ...
    def reject_share_request(
        self,
        group_id: str,
        request_id: str,
        *,
        decided_by: str,
        decided_by_is_system_admin: bool = False,
    ) -> "dict | None":
        """驳回:状态置 rejected + `decided_by`/`decided_at`。申请不存在或已被决定返回
        `None`。不写任何授权边。审批资格复核与旁路参数同 `approve_share_request`——驳回
        同样是组管理员对本组的决定,同一个 TOCTOU 窗口。"""
        ...
    def delete_share_request(
        self, notebook_id: str, request_id: str, requester_id: str
    ) -> str:
        """撤回:仅**申请者本人**、仅 `status='pending'` 可删。返回 `"deleted"`;不存在、
        不属于这本库、或不是本人提交的都返回 `"not_found"`(不泄露存在性);已决定抛
        `ShareRequestNotPendingError`。`requester_id` 不可省——能力守卫证明不了「这条申请
        是他提的」(codex #519 R1 P1)。"""
        ...


@runtime_checkable
class GovernanceStorePort(Protocol):
    @staticmethod
    def conflict_relation_count(
        connection: object, notebook_id: str, *, max_rows: int | None = None
    ) -> int: ...
    @staticmethod
    def conflict_relation_rows(
        connection: object, notebook_id: str, *, max_rows: int | None = None
    ) -> list[dict]: ...
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
    def decided_seed_pairs_from(connection: object, notebook_id: str) -> dict[frozenset, str]: ...
    @staticmethod
    def merge_candidate_pairs(db: object, notebook_id: str, statuses: object) -> list[Any]: ...
    @staticmethod
    def merge_candidate_pairs_for_canonicals(
        db: object, notebook_id: str, statuses: object, canonical_ids: object
    ) -> list[Any]: ...
    @staticmethod
    def valid_object_ids(db: object, object_ids: object) -> set[str]: ...
    @staticmethod
    def seed_for(object_type: str): ...
    @staticmethod
    def merge_evidence(base_evidence: list, source_evidence: list) -> list: ...
    @staticmethod
    def sweep_orphan_clusters_page(
        db: object,
        notebook_id: str,
        after_object_type: str,
        after_member_object_id: str,
        limit: int,
    ) -> tuple[list[Any], int]:
        """One keyset batch of the orphan-cluster sweep: scan ``≤ limit``
        cluster rows after the ``(after_object_type, after_member_object_id)``
        cursor, delete whichever of them are orphans.

        Returns ``(page_rows, deleted_count)``. ``page_rows`` are the SCANNED
        keys in ``(object_type, member_object_id)`` order — the batch bound is
        on scanned rows, so the cursor advances over rows the batch did not
        delete, and ``deleted_count == 0`` must not be read as "sweep done"
        (a short page is what ends it)."""
        ...
    @staticmethod
    def incremental_cluster_rows(
        db: object, notebook_id: str, object_type: str
    ) -> list[Any]: ...
    @staticmethod
    def update_edge_review(
        connection: object, notebook_id: str, relation_id: str, status: str
    ) -> str:
        """Set ``review_status``, return the PREVIOUS value (R3 T-A3 P1-2) —
        callers use it to decide whether the review-queue count memos can
        carry-forward (verified<->pending) or must invalidate (either side
        'rejected'). Raises ``KeyError`` if the relation is not found in the
        notebook; allowed-status validation/error behavior is backend-specific
        and unchanged by this contract."""
        ...
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
    def pipeline_identity(self, notebook_id: str) -> tuple[str, str]: ...
    def version_with_settings(self, notebook_id: str, settings_tail: tuple) -> list: ...
    def graph_rows(
        self,
        notebook_id: str,
        source_ids: Sequence[str] | None,
        *,
        synonym_edges: Any = None,
        as_arrays: bool = False,
    ) -> Any: ...
    def source_subgraph_signature(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple: ...
    def source_subgraph_rows(
        self,
        notebook_id: str,
        source_ids: Sequence[str],
        limits: Mapping[str, int],
    ) -> Mapping[str, Any]: ...
    def source_graph_partition_rows(
        self, notebook_id: str, source_id: str
    ) -> Mapping[str, Any]: ...
    def embedding_matrix(
        self,
        notebook_id: str,
        table: str,
        id_column: str,
        object_ids: Sequence[str] | None = None,
    ) -> Any: ...
    def chunk_sources_for_ids(
        self, notebook_id: str, chunk_ids: Sequence[str]
    ) -> dict[str, str]: ...
    def source_ids(self, notebook_id: str) -> list[str]: ...
    def visible_source_ids(
        self, notebook_id: str, source_ids: list[str]
    ) -> list[str]: ...


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
    def knowhow_table_id_by_title(self, notebook_id: str, title: str) -> str:
        """The id of the table named exactly ``title`` in ``notebook_id``, or
        ``""`` when none matches — a bounded point lookup on
        ``(notebook_id, title)``, never the health-aggregated
        ``list_knowhow_tables`` scan (row counts, projection status, cell
        activity, code inputs for every table in the notebook) that a title
        lookup has no use for.

        When more than one table shares the derived title (a user is free to
        rename tables to collide), the tie-break is the same one
        ``list_knowhow_tables`` already produces by construction — creation
        order (``created_at``, then ``id``) — so callers that used to take
        the first match out of the health-aggregated list see byte-identical
        results from this cheaper path.
        """
        ...
    def knowhow_table_columns(self, table_id: str) -> list[dict]:
        """A table's columns alone — ``id``/``name``/``role``/``position`` —
        never its rows or cells. Raises ``KeyError`` if the table is gone (the
        same contract ``get_knowhow_table`` uses), so a caller that only needs
        "does this table still exist, and what are its columns" (command
        catalog's apply, resolving a remembered target) never has to pay for a
        full row/cell hydrate just to find out.
        """
        ...
    def knowhow_table_title(self, table_id: str) -> str:
        """A table's current ``title`` alone. Raises ``KeyError`` if the
        table is gone, same contract as ``knowhow_table_columns``.

        Exists for the same reason ``knowhow_table_columns`` does: a caller
        that resolved a target table through a REMEMBERED id (command
        catalog's ``applied_table_id`` fast path) cannot assume the title it
        would DERIVE today still matches — the source's canonical title can
        drift mid-job (async paper-metadata backfill) or the table can have
        been renamed by hand between two calls — and must not pay for a full
        ``get_knowhow_table`` row/cell hydrate just to read one column.
        """
        ...
    def knowhow_table_notebook_id(self, table_id: str) -> str:
        """A table's owning ``notebook_id`` alone. Raises ``KeyError`` if the
        table is gone, same contract as ``knowhow_table_title``.

        Exists for command catalog's R20 (codex PR #412 review round 20)
        cross-notebook membership check on an INHERITED apply target
        (``_inherit_applied_table``): that check used to be a title
        round-trip (``knowhow_table_id_by_title(notebook_id, title) ==
        candidate_table_id``), but a table's title is not unique — when the
        candidate had been renamed to collide with an EARLIER, unrelated
        table in the same notebook, the by-title lookup's documented
        creation-order tie-break resolves to that earlier table instead, and
        a target that in fact still belongs to this notebook gets rejected.
        Reading the row's own ``notebook_id`` column directly sidesteps title
        collisions entirely.
        """
        ...
    def knowhow_anchor_existing_values(
        self, column_id: str, values: Sequence[str]
    ) -> set[str]:
        """Which of `values` already have a row in `column_id`'s anchor
        column — bounded to `values` (callers pass at most a page's worth),
        and indexed on `column_id` alone via the same normalized-anchor
        expression index the guarded-write anchor-membership check already
        relies on (migration 21 / PostgreSQL 0005). Never touches
        `knowhow_rows`: `column_id` already scopes to exactly one table, so
        this costs one indexed IN-lookup regardless of how many OTHER rows
        that table holds.
        """
        ...
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
    def append_knowhow_rows_skipping_existing_anchors(
        self,
        table_id: str,
        anchor_column_id: str,
        rows: Sequence[dict[str, str]],
        actor: str = "",
        origin: str = "user",
    ) -> dict:
        """``append_knowhow_rows``, but anchor-membership de-duplication and
        the insert share ONE write transaction — the membership check
        (js-trim normalized, same expression ``knowhow_anchor_existing_values``
        uses) is re-run under this method's own lock instead of trusting a
        caller's earlier, separately-locked read. Closes the window where an
        ordinary knowhow row/cell edit (not covered by the command catalog's
        own apply lock) lands between a caller's pre-read and a would-be
        separate append, double-inserting a command.

        Returns ``{"row_ids": {anchor_value: row_id}, "skipped_anchor_values":
        set[str]}`` keyed by NORMALIZED anchor value: for each input row, if
        its (js-trim normalized) value at ``anchor_column_id`` already has a
        row in this table OR was already claimed earlier in THIS batch, it is
        skipped (recorded in ``skipped_anchor_values``); a blank/missing
        anchor value has nothing to collide with and is always inserted.
        ``anchor_column_id`` must currently be ``table_id``'s anchor column
        (``role == 'anchor'``), re-verified under this same lock; raises
        ``ValueError`` if it is not. Empty ``rows`` and skip-everything
        batches are silent no-ops — no transaction opens (former) or nothing
        is recorded (latter), mirroring ``append_knowhow_rows``'s own
        empty-batch convention.
        """
        ...
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
    def delete_source_asset_rows(self, source_id: str) -> list[dict]: ...


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
    def knowledge_type_count_rows_excluding_memory(
        db: object, notebook_id: str, statuses: tuple[str, ...]
    ) -> list[dict]:
        """``[{object_type, c}]`` for the notebook, MINUS the objects owned by
        its private Memory synthetic sources — the exclusion evaluated INSIDE
        the statement.

        Deliberately not expressible as ``knowledge_type_count_rows`` minus
        ``knowledge_type_count_rows_for_sources(memory_source_ids)``: that
        arithmetic reads three times with no shared snapshot (PostgreSQL runs
        each at READ COMMITTED), so a Memory created or deleted between them
        makes the subtrahend describe a different library than the minuend, and
        the shared-base understanding then carries a number derived from one
        member's private Memory.  The generic ``…_for_sources`` variant stays
        for the enumeration denominator, whose caller genuinely holds an
        arbitrary id list.

        Same cost class as ``knowledge_type_count_rows`` plus one primary-key
        probe into ``sources`` per counted row; not served from the counts
        memo, which knows nothing about sources.
        """
        ...
    @staticmethod
    def top_concept_names(
        db: object,
        notebook_id: str,
        statuses: tuple[str, ...],
        limit: int,
    ) -> list[tuple[str, int]]:
        """``[(canonical concept name, member count)]``, most-supported first.

        The one place a caller can learn WHAT this library's recurring concepts
        are called without hydrating objects: names are not a column on
        ``knowledge_objects`` (they live inside ``payload`` JSON), so the only
        materialized spelling is ``concept_clusters.canonical_name``.  Restricted
        to ``object_type='concept'`` for the reason ``largest_clusters`` is — a
        whole claim sentence is not a name — and the name is taken as
        ``MIN(NULLIF(canonical_name,''))`` because one ``canonical_id`` is not
        guaranteed to carry one spelling and a bare ``MIN()`` lets an empty
        string hijack the row.

        Objects owned by the notebook's private Memory synthetic sources are
        excluded BY THE STATEMENT ITSELF — a confirmed Memory is owner-private,
        and this result feeds a NOTEBOOK-SHARED prompt block, so a concept that
        exists only inside one member's Memory must not become part of what
        every member's agent "knows" about the library.  ⚠ The exclusion used
        to be an ``exclude_source_ids`` list the caller read separately and
        passed in; that read happens at a different instant than this query
        runs, and the window between them leaks CONCEPT NAMES — the one input
        here that is not a count — into a block every member reads.  There is
        no parameter for it any more, so there is no way to call this without
        the exclusion.

        Output is bounded by ``limit`` and ordered ``members DESC, name ASC`` so
        two runs over an unchanged library produce the same list.  ⚠ Bounded
        OUTPUT is not a cheap query: like ``UnifiedKgStorePort.largest_clusters``
        (same shape) the LIMIT truncates the result, not the scan — the
        notebook's ``concept_clusters`` rows are grouped in full.  This is
        deliberately NOT a request-path read; its only caller is the background
        understanding-consolidation chain, which runs at most once per threshold
        batch of source changes, i.e. strictly less often than the community
        precompute that already runs ``largest_clusters`` on every KG rebuild of
        the same notebook.
        """
        ...
    @staticmethod
    def knowhow_knowledge_type_rows(
        db: object, notebook_id: str, statuses: tuple[str, ...]
    ) -> list[dict]: ...
    def invalidate_knowledge_counts(self, notebook_id: str) -> None: ...
    def list_user_usage(self) -> list[dict[str, Any]]: ...
    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]: ...
    def notebook_exists_for_owner(self, notebook_id: str, user_id: str) -> bool: ...
    def list_user_activity(
        self,
        user_id: str,
        *,
        activity_type: str | None = None,
        include_inaccessible_questions: bool = False,
        notebook_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        before_ts: str | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def pending_actions_projection_rows(self, user_id: str) -> dict: ...
    def load_notebook_scale_facts(self, notebook_id: str) -> NotebookScaleFacts: ...
    def warm_open_path_caches(
        self, progress: Callable[[int, int], None] | None = None
    ) -> int: ...


@runtime_checkable
class ReportStorePort(ReportRepository, Protocol):
    def row_to_dict(self, row: object, *, full: bool) -> dict: ...


# Agentic Memory P1 (T2): the agent's per-notebook "understanding" — a
# handful of named text blocks plus one consolidation-chain status row per
# (notebook, owner). See ``docs/superpowers/specs/2026-08-18-agentic-memory-
# design.md`` §5 for the product shape; this store is deliberately consumer-
# free as of T2 (no route, no injection, no job wires to it yet).

AGENT_PROFILE_HISTORY_MAX = 20  # ring cap for agent_notebook_profile.history_json;
# the oldest before/after entry drops once a block accumulates more edits
# than this. Written by write_block/clear_block inside the SAME write
# transaction as the value change, as the transaction's LAST step.

AGENT_PROFILE_JOB_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

#: ``sweep_stale_on_start``'s fixed ``failure_reason``. ⚠ USER-FACING TEXT: it
#: reaches the screen through the understanding panel's job status, so it lives
#: here as one named constant rather than as a literal inside each backend's
#: SQL. Two copies of a Chinese sentence embedded in two SQL strings drift
#: silently — the panel would then show a different wording depending on which
#: backend the deployment runs, and neither copy is greppable from the UI side.
AGENT_PROFILE_RESTART_FAILURE_MESSAGE = "服务重启，整理未完成"

#: The consolidation job's own terminal ``failure_reason`` wordings (T4). ⚠ ALL
#: USER-FACING: they reach the screen through the same job-status field as the
#: sweep message above, so they live here as named constants for the same
#: reason — one greppable home per sentence, never a literal buried in a
#: service branch. Wording is deliberately about the ACTION the user asked for
#: ("整理"), never about the internal chain, workload or model plumbing.
AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE = "模型未配置，无法整理"
AGENT_PROFILE_MALFORMED_MESSAGE = "整理结果不可用，已保留原有理解"
AGENT_PROFILE_INTERNAL_FAILURE_MESSAGE = "整理时出错，请稍后重试"
AGENT_PROFILE_INTERRUPTED_MESSAGE = "服务中断，整理未完成"
AGENT_PROFILE_SUBMISSION_FAILED_MESSAGE = "整理任务未能启动，请稍后重试"


def append_profile_history(
    history: list,
    before: object,
    after: str,
    origin: str,
    actor: str,
    at: str,
    revision: int,
) -> list:
    """Append one before/after entry to a block's history ring and keep it
    bounded at ``AGENT_PROFILE_HISTORY_MAX`` — the OLDEST entry drops first.

    ⚠ SINGLE SOURCE OF TRUTH for both backends. This is pure list arithmetic
    with no SQL in it, so the two stores share it instead of each carrying an
    identical private copy: the ring's DIRECTION (keep the newest, drop the
    oldest) is exactly the kind of detail a copy can invert while every
    existing test stays green — a store that kept the oldest 20 entries still
    returns 20 entries of the right shape. One implementation makes that
    class of divergence unrepresentable rather than merely tested-for.
    """
    entry = {
        "before": before,
        "after": after,
        "origin": origin,
        "actor": actor,
        "at": at,
        "revision": revision,
    }
    updated = [*history, entry]
    if len(updated) > AGENT_PROFILE_HISTORY_MAX:
        updated = updated[-AGENT_PROFILE_HISTORY_MAX:]
    return updated


#: How many of the member's most recent asks in ONE notebook the overlay chain
#: samples, and the hard ceiling on trace rows read across all of them. Two
#: numbers rather than one because they bound different things: a member can
#: have thousands of asks (so the ask count must be capped), and ONE exhaustive
#: reasoning ask can carry a hundred trace steps (so the step total must be
#: capped independently — 40 asks × "however many steps each happens to have"
#: is not a bound).
AGENT_PROFILE_TRACE_SAMPLE = 40
AGENT_PROFILE_TRACE_STEP_LIMIT = 600

#: Agentic Memory P2 (T4) mirror of the pair above, for the OTHER usage
#: sample the overlay chain reads: a member's own recently COMPLETED deep
#: reports in this notebook. Both numbers are smaller than the ask pair —
#: a report persists at most one ``attempted`` row per CONFIRMED retrieval
#: direction (never per model action, never per section-writing step), so
#: even an ``exhaustive``-depth report carries far fewer rows than one
#: exhaustive reasoning ask's trace.
AGENT_PROFILE_REPORT_SAMPLE = 10
AGENT_PROFILE_REPORT_ATTEMPT_LIMIT = 200

#: ``project_trace_step``, ``project_run_step`` and their shared narrowing
#: helpers/vocabularies (``clip_trace_text``, ``_step_detail_mapping``,
#: ``_bounded_id_list``, ``closed_value``, ``_list_len``, the ``SITUATION_*``
#: tuples) moved to ``app.domain.retrieval_experience`` (2026-08-23):
#: ``project_run_step`` is consumed both by repository adapters
#: (``ask_state_store.py``, both backends) and by
#: ``app.services.retrieval_experience_projection``, and repositories may not
#: import services. ``clip_trace_text``/``closed_value`` are imported back
#: here by name because the four ``project_*_row``/``project_report_attempt``
#: functions below stay in this module and reuse the same two budgets — see
#: that module's own docstring for the full argument.


def project_run_row(job_id: object, mode: object) -> dict:
    """One sampled completed ask, for the GLOBAL experience library.

    Agentic Memory P2 (T5). Compare ``project_ask_row`` directly: that one
    keeps the member's own question, their job status and a timestamp, because
    its product is readable only by that member. This one keeps an OPAQUE run
    id and a closed-vocabulary engine mode, and nothing else — no question, no
    ``created_by``, no ``notebook_id``, no timestamp.

    The run id is bookkeeping, not content: it is what lets one entry's
    provenance list de-duplicate runs it has already counted, which is why the
    distillation needs no cursor table. It never reaches a prompt (see
    ``ObservedRun`` in ``retrieval_experience_projection.py``) and it is never
    stored beside anything that would say whose run it was.
    """
    return {
        "run_id": str(job_id or ""),
        "mode": closed_value(mode, SITUATION_ASK_MODES),
        "steps": [],
    }


def project_ask_row(
    job_id: object, question: object, status: object, created_at: object
) -> dict:
    """One sampled ask, narrowed the same way and in the same one place.

    The question is the member's OWN question — the single most useful input
    the overlay has (``retrieval_notes`` is about how this person searches this
    library) — and it is also the only free text in the sample, so it is
    clipped here rather than at each call site.
    """
    return {
        "job_id": str(job_id or ""),
        "question": clip_trace_text(question),
        "status": str(status or ""),
        "created_at": str(created_at or ""),
        "steps": [],
    }


def project_report_row(report_id: object, question: object, created_at: object) -> dict:
    """One sampled deep report, narrowed the same way as ``project_ask_row``.

    Agentic Memory P2 (T4). The report's own question is the same kind of
    input as an ask's own question — the member's own words about their own
    run, the single most useful free text the overlay can see — so it is kept
    and clipped through the same ``clip_trace_text`` budget. ``attempts``
    starts empty; the caller fills it from the report's persisted
    ``sections_json[i].attempted`` rows via ``project_report_attempt``.
    """
    return {
        "report_id": str(report_id or ""),
        "question": clip_trace_text(question),
        "created_at": str(created_at or ""),
        "attempts": [],
    }


def project_report_attempt(query: object, failed: object) -> dict:
    """One projected ``attempted`` row from a report's ``sections_json``.

    Agentic Memory P2 (T4). ``sections_json[i].attempted`` entries are
    ``{"query": str, "new": int, "tries": int, "failed"?: bool}``. Two of
    those four are kept, and WHICH two changed in the T4 fix round:

    * ``query`` — the direction's own wording, clipped through the same
      ``clip_trace_text`` budget as an ask's question. This is what the
      sample is FOR: ``retrieval_notes`` is a note about how this member
      phrases research, and a direction they confirmed is their phrasing.
      It is their own text about their own run, and the block it feeds is
      readable only by them — the same disclosure ``project_ask_row`` makes
      for the ask question.
    * ``failed`` — whether executing that direction errored. A count, not
      an outcome (see the port docstring).

    ``new`` is deliberately DROPPED. It counts knowledge objects newly added
    to the run's shared candidate pool, which is not the same thing as "this
    direction found something" — the port docstring enumerates the four
    independent ways that reading breaks. Keeping the field "just in case"
    would leave the wrong number one refactor away from being read as an
    outcome again.

    ``query``/``failed`` arrive in different native shapes depending on
    which backend's SQL produced them — SQLite's ``json_extract`` hands back
    ``str``/``None`` and degrades a JSON boolean to ``0``/``1``, PostgreSQL's
    ``->>`` text extraction hands back ``str``/``None`` — and this is the ONE
    place both are reconciled into the same ``{str, bool}`` pair, so a caller
    downstream (``summarize_usage``) never has to know which backend
    answered. Anything unrecognisable degrades to ``""``/``False`` rather
    than raising — the same tolerance ``project_trace_step`` has for a
    corrupt row shape.
    """
    if isinstance(failed, bool):
        f = failed
    elif isinstance(failed, int):
        f = bool(failed)
    elif isinstance(failed, str):
        f = failed.strip().lower() == "true"
    else:
        f = False
    return {"query": clip_trace_text(query), "failed": f}


@dataclass(frozen=True)
class AgentProfileClaim:
    """What a winning ``claim`` hands its run: the threshold snapshot AND the
    GENERATION token that proves which incarnation of the row it holds.

    Two values, one object, on purpose. They are an ``int`` and a ``str``, so a
    tuple would be positional and a caller passing them on in the wrong order
    would type-check, run, and silently settle the wrong generation with the
    wrong count — which is the exact failure class this token exists to close
    (Agentic Memory P2, the registered R4 ABA).
    """

    pending_signal: int
    token: str


#: ``settle``'s three outcomes. Two of them mean "the CAS did not land", and
#: telling them apart is the whole point of the tri-state: the caller's
#: revoked-overlay wipe must fire for one and MUST NOT fire for the other.
AGENT_PROFILE_SETTLED = "settled"
#: The job row is gone. Only member removal deletes it (``clear_job_row``), so
#: any block this run wrote is revoked private data and gets wiped.
AGENT_PROFILE_SETTLE_GONE = "gone"
#: The row is there but belongs to a LATER claim (a different ``claim_token``,
#: or it already reached a terminal status). This run is void — and wiping here
#: would delete the blocks the NEW generation may have just written, which is
#: strictly worse than the ABA it would be trying to fix.
AGENT_PROFILE_SETTLE_SUPERSEDED = "superseded"
AgentProfileSettleOutcome = Literal["settled", "gone", "superseded"]


class AgentProfileClaimSuperseded(RuntimeError):
    """``write_block``'s ``claim_token`` no longer matches the chain's job row:
    this worker's claim was superseded (the row was deleted and recreated, or
    a later claim took the slot) while the run was in flight — typically
    inside its minutes-long model call.

    Deliberately NOT an ``AgentProfileRevisionConflict``. That one means "a
    concurrent writer changed this block", and the caller answers it by
    skipping that one label and keeping the rest of the run. This one means
    "the entire run is void", and a caller that lumped the two together would
    keep writing the remaining pre-removal blocks after the first one was
    refused."""

    def __init__(self, notebook_id: str, owner_id: str) -> None:
        super().__init__(
            "agent profile claim superseded: "
            f"{notebook_id}/{owner_id or '(base)'}"
        )
        self.notebook_id = notebook_id
        self.owner_id = owner_id


class AgentProfileRevisionConflict(RuntimeError):
    """``write_block``/``clear_block``'s ``expected_revision`` no longer
    matches the row's stored ``revision`` — a concurrent edit (another user,
    or the consolidation job) landed first, or (``expected_revision=0``) a
    concurrent writer already created the row. Mirrors the
    ``memory_revisions`` lesson: optimistic concurrency is a revision
    counter, never a timestamp — SQLite's second-granularity clock makes a
    timestamp CAS falsely agree on rapid successive edits. The caller
    re-reads the current block and either re-applies or surfaces a 409; this
    store never blind-overwrites."""

    def __init__(self, notebook_id: str, owner_id: str, label: str) -> None:
        super().__init__(
            "agent profile block revision conflict: "
            f"{notebook_id}/{owner_id or '(base)'}/{label}"
        )
        self.notebook_id = notebook_id
        self.owner_id = owner_id
        self.label = label


@runtime_checkable
class AgentProfileStorePort(Protocol):
    """Durable state for the agent's per-notebook "understanding":
    ``agent_notebook_profile`` (the blocks themselves) and
    ``agent_profile_jobs`` (one consolidation-chain status row per
    (notebook, owner), doubling as the threshold counter).

    Every read here is bounded on purpose. A block read is a primary-key-
    prefix query over at most TEN rows — the five app-layer labels for the
    shared base layer plus the same five for the one caller's overlay, which
    is exactly what ``read_blocks``' ``owner_id IN ('', ?)`` predicate spans —
    never a scan. A job read is a single primary-key
    point query. ``owner_id=''`` is the notebook's shared base layer; a
    non-empty ``owner_id`` is that one member's private overlay, and the
    ``owner_id`` predicate in ``read_blocks`` is baked into the SQL text
    itself — it is a privacy boundary, not a filter a caller could
    accidentally omit or apply after the fact in Python. Nothing in this
    store ever hands back another user's overlay row.
    """

    # ------------------------------------------------------------- blocks
    def read_blocks(self, notebook_id: str, owner_id: str) -> list[dict]: ...
    def read_block(
        self, notebook_id: str, owner_id: str, label: str
    ) -> dict | None: ...
    def write_block(
        self,
        notebook_id: str,
        owner_id: str,
        label: str,
        *,
        value: str,
        evidence: Sequence[Mapping[str, Any]],
        expected_revision: int,
        origin: str,
        actor: str,
        claim_token: str = "",
    ) -> dict:
        """Upsert one block inside a single write transaction: the
        value/evidence change and the ``revision`` optimistic-concurrency
        bump happen together, and the LAST step of that same transaction
        appends a bounded before/after entry to ``history_json`` (ring
        capped at ``AGENT_PROFILE_HISTORY_MAX``, oldest dropped first).

        ``expected_revision=0`` means "no row yet, this write creates it";
        any other value is compared against the stored ``revision`` and
        raises ``AgentProfileRevisionConflict`` on mismatch (including
        "someone else's write created the row first").

        A non-empty ``claim_token`` additionally requires — inside that SAME
        write transaction — that the chain's ``agent_profile_jobs`` row still
        carries this token, raising ``AgentProfileClaimSuperseded`` otherwise.
        That is what turns the consolidation job's pre-write "is the row still
        there?" probe from best-effort into atomic: the probe closes the
        minutes-long model call, and this closes the microseconds between the
        probe and the write, including the ABA case where the row was deleted
        and recreated in between (a bare existence check passes there; a
        generation check cannot).

        The default ``""`` means "no generation asserted" and is the user-facing
        edit path: a person editing their own block through the API holds no
        claim, and requiring one would make the token a second, wrong kind of
        lock."""
        ...
    def clear_block(
        self,
        notebook_id: str,
        owner_id: str,
        label: str,
        *,
        expected_revision: int,
        actor: str,
    ) -> dict:
        """Blank a block's value (and evidence) while KEEPING the row and its
        history — the opposite of ``clear_all``. Same CAS and same
        same-transaction history append as ``write_block``; raises
        ``KeyError`` if the block was never written (nothing to clear)."""
        ...
    def clear_all(self, notebook_id: str, owner_id: str) -> int:
        """Delete every block row for one (notebook, owner) scope outright —
        the "start this chain's understanding over" reset, not a per-block
        clear. Returns the row count deleted. No CAS: this is a full-scope
        wipe, not a single block's optimistic-concurrency edit."""
        ...

    # --------------------------------------------------------------- jobs
    def job_row(self, notebook_id: str, owner_id: str) -> dict | None: ...
    def bump_signal(self, notebook_id: str, owner_id: str, delta: int = 1) -> int:
        """Zero-scan primary-key upsert: increments (or creates at) this
        chain's ``pending_signal`` threshold counter and returns the new
        count. The row is created with ``status='idle'`` on first touch.

        ``delta`` must be non-negative (``ValueError`` otherwise). This
        counter is a monotone accumulator whose only legitimate decrement is
        ``settle(consumed=...)``, which is CAS'd on the chain being claimed;
        a negative bump would be an uncontrolled decrement racing whatever
        run is in flight."""
        ...
    def claim(self, notebook_id: str, owner_id: str) -> "AgentProfileClaim | None":
        """Take this chain's single-flight slot, returning the
        ``pending_signal`` SNAPSHOT observed at the moment of the claim plus a
        freshly minted generation token, or ``None`` when the slot was already
        taken.

        The row is created first if it does not exist (``INSERT OR IGNORE`` /
        ``ON CONFLICT DO NOTHING``), so a chain that was never signalled is
        claimable — that is what a manual "rebuild now" is, and it must not
        have to fake a threshold bump to get a slot. The CAS then moves
        ``status`` to ``'running'`` only when it was not already
        ``queued``/``running``, decided on the UPDATE's own rowcount and never
        on a prior read: two callers racing this method can never both get an
        int back.

        The same UPDATE resets the PREVIOUS run's leftovers
        (``failure_reason``, ``diagnostic``, ``blocks_written``,
        ``finished_at``) and stamps ``started_at``. Without that reset a
        successful run would keep displaying the last failure's reason.

        The snapshot is half the point of the return value: the run that holds
        the slot passes it back as ``settle(consumed=...)``, so signals that
        arrive WHILE it runs survive into the next round instead of being
        zeroed by a run that never saw them.

        The token is the other half. Every claim mints a NEW one and stamps it
        on the row, so "the row I claimed" and "the row that is here now" become
        distinguishable even when the second one has the same primary key: a
        member removed and re-added gets a row that is byte-identical in
        (notebook_id, owner_id) and starts over at ``runs=0``, which is why
        neither the key nor the run counter could serve as a generation. The
        holder passes the token to ``settle`` and ``write_block``, and both
        refuse to act on a row that no longer carries it.
        """
        ...
    def settle(
        self,
        notebook_id: str,
        owner_id: str,
        status: str,
        *,
        failure_reason: str = "",
        diagnostic: str = "",
        blocks_written: int = 0,
        consumed: int,
        claim_token: str,
    ) -> "AgentProfileSettleOutcome":
        """Move a claimed chain to a terminal status (``done``/``failed``/
        ``cancelled``), stamping ``finished_at`` and incrementing ``runs``.

        ``consumed`` is subtracted from ``pending_signal``, floored at zero
        (``max(0, pending_signal - consumed)``). EVERY terminal path passes the
        snapshot ``claim`` handed it — success, failure and interruption alike.
        That consumes exactly the signal the run was handed and LEAVES anything
        that arrived mid-run, so the next threshold check still sees those
        changes.

        Failure deliberately consumes too, and the reason is cost, not
        bookkeeping: a chain that keeps its signal on failure re-fires on the
        very next source change, so a provider returning malformed output would
        bill one model call per upload for as long as it stays broken. Charging
        the batch caps the chain at one call per threshold batch no matter what
        the provider does; a transient failure is picked up by the next batch of
        changes, or immediately by T6's manual rebuild. The one exception is a
        claim that never produced a run at all (a submit that raised before any
        worker existed): nothing looked at those signals, so that path passes
        ``0``.

        CAS'd on ``status IN ('queued','running')`` AND on ``claim_token``
        matching the caller's, so a settle racing a second settle for the same
        chain can only land once, and a settle from a SUPERSEDED claim cannot
        land at all.

        The three outcomes are the reason this is not a bool. ``"settled"`` is
        the CAS landing. When it does not land, the store re-reads the row
        INSIDE THE SAME TRANSACTION and reports ``"gone"`` (no row — only
        member removal deletes it) or ``"superseded"`` (a row is there, but it
        belongs to a later claim or already reached a terminal status). The
        caller acts on those two in opposite directions: ``"gone"`` triggers
        the revoked-overlay wipe, ``"superseded"`` must never — the newer
        generation may already have written blocks, and wiping them would be a
        worse bug than the ABA the token closes. A single ``False`` cannot
        carry that distinction, which is exactly how P1 ended up wiping on the
        wrong branch."""
        ...
    def sweep_stale_on_start(self) -> int:
        """Startup crash recovery: every ``queued``/``running`` row (there is
        no cross-process liveness for this in-process job) is force-settled
        to ``failed`` with ``AGENT_PROFILE_RESTART_FAILURE_MESSAGE``. Returns
        the row count swept.

        ``queued`` is defensive-only in P1: nothing writes that status today
        (``claim`` goes straight to ``running``). It stays in every CAS
        predicate and in this sweep so that a future queue-then-run split
        cannot leave rows that no guard recognises; T6/T7 deliberately do not
        render a queued state."""
        ...
    def clear_job_row(self, notebook_id: str, owner_id: str) -> int:
        """Delete this chain's status/threshold row outright — the job-table
        counterpart of ``clear_all`` for the block rows.

        Agentic Memory P1 (T5 repair round). A removed member's overlay
        JOB row is exactly as much "unreadable data about a notebook they can
        no longer open" as the block rows ``clear_all`` already discards —
        it carries a ``pending_signal`` counter derived from that person's
        own activity, and a stale ``running``/``failed`` status would
        otherwise survive their removal and greet them with someone else's
        (or their own, stale) job state if they are ever re-added. Returns
        the row count deleted (0 or 1 — the primary key is
        ``(notebook_id, owner_id)``)."""
        ...


# ---------------------------------------------------------------------------
# Agentic Memory P2 (A / T5): the deployment-GLOBAL retrieval-strategy
# experience library.
# ---------------------------------------------------------------------------

#: Hard ceiling on how many experience entries the deployment keeps. When the
#: table grows past it, the distillation run evicts ascending by
#: ``(adopted, support, updated_at)`` — unused entries first, then thinly
#: supported ones, and only then by age.
#:
#: The number is small on purpose and is a QUALITY bound, not a storage one.
#: Every read of this table is a bounded full scan (there is no index, and no
#: query narrows it), and the injection side scores every row against the
#: current situation in memory; a library of tens of thousands of entries would
#: not make the advice better, it would make the top-k a lottery among near
#: duplicates while costing a scan on a hot path.
RETRIEVAL_EXPERIENCE_MAX_ENTRIES = 300

#: How many opaque run ids ONE entry retains as provenance. This set is what
#: makes distillation idempotent: a run id already listed does not increment
#: ``support`` again, so re-reading an overlapping batch cannot inflate the
#: evidence behind an entry.
#:
#: ⚠ Its relationship with ``RETRIEVAL_EXPERIENCE_BATCH_RUNS`` is an
#: INVARIANT, not a coincidence: one batch must never carry more runs than one
#: entry can remember, or the ids evicted from the tail come back in the next
#: overlapping batch and get counted a second time. Pinned by
#: ``test_retrieval_experience_job.py``.
RETRIEVAL_EXPERIENCE_PROVENANCE_MAX = 60

#: How many completed asks ONE distillation batch reads. Bounded twice, like
#: the agent-profile trace sample: this count, and the step ceiling below (one
#: exhaustive reasoning ask can carry a hundred trace steps, so "N asks" alone
#: is not a bound).
RETRIEVAL_EXPERIENCE_BATCH_RUNS = 40
RETRIEVAL_EXPERIENCE_BATCH_STEPS = 600

#: Longest rationale accepted from the distillation model. Entries ride into a
#: bounded prompt block on the injection side, and a rationale is one sentence
#: of advice about how to search — not an explanation. Over-length is a
#: REJECTION of that one entry rather than a silent clip: a truncated sentence
#: of advice reads as confident and complete while having lost its qualifier.
RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS = 160


class RetrievalExperienceStorePort(Protocol):
    """Durable rows for ``retrieval_experiences`` — Agentic Memory P2's
    deployment-GLOBAL retrieval-strategy experience library.

    ⚠ This is the only store in the repository with NO tenancy column at all:
    no ``notebook_id``, no ``created_by``, no ``owner_id``. That is the point
    of the table and also the reason its safety argument has to be a different
    one from every other store here. Everywhere else, isolation is a predicate
    in the SQL text (``memory_items.created_by``, ``agent_notebook_profile``'s
    ``owner_id IN ('', ?)``). There is no predicate to write here, so the
    isolation is instead STRUCTURAL and lives one layer up, in what may become
    a row at all: the distillation input is projected to
    ``RunObservation`` — a frozen dataclass whose every field is an ``int``, a
    ``bool`` or a closed ``Literal``, with no free-text field anywhere in its
    reachable shape — so the model that writes ``rationale`` has never seen a
    question, an answer, a document title or an id. See
    ``app/services/retrieval_experience_projection.py``.

    Every read here is bounded by construction: a primary-key point lookup, or
    a full scan over a table whose row count is hard-capped at
    ``RETRIEVAL_EXPERIENCE_MAX_ENTRIES`` (the injection side scores rows in
    memory rather than asking the database to rank them — the situation
    similarity is a set overlap over closed enum values, which no index can
    answer).
    """

    def read_all(self, limit: int) -> list[dict]: ...
    def read_experience(self, experience_id: str) -> dict | None: ...
    def upsert_experience(
        self,
        experience_id: str,
        *,
        situation: Mapping[str, Any],
        action: str,
        polarity: str,
        rationale: str,
        provenance: Sequence[str],
        provenance_max: int,
        replace_conclusion: bool,
    ) -> dict:
        """Create or merge ONE entry, keyed by its content-addressed id.

        ⚠ ``experience_id`` is computed by the CALLER (from ``situation`` and
        ``action``, via ``retrieval_experience_projection.experience_id``) and
        is never re-derived here. It is passed in rather than computed inside
        so the hash function has exactly one definition — the same one the
        injection side and the merge tool reason about.

        ``situation`` is serialised here rather than by the caller, with sorted
        keys, so both backends store the same canonical text and a row read
        back re-canonicalises to the identical hash input. PostgreSQL stores it
        as ``jsonb``, which does not preserve key order or whitespace; sorting
        on the way in is what keeps "the id can be re-verified from the row"
        true on both backends.

        ⚠ ``provenance`` is NEWEST-FIRST: the only caller
        (``retrieval_experience_job.py``) builds it by absorbing rows from a
        query ordered ``created_at DESC``. Both backends reverse it, ONCE, at
        the top of this method, before it touches any trailing-slice logic —
        every truncation below assumes the tail of a list is the newest entry,
        and a caller-order mismatch there is exactly the bug this reversal
        closes (see either backend's docstring for the failure shape).

        Merge semantics, all inside ONE write transaction:

        * ``support`` grows by the number of ``provenance`` run ids that the
          stored provenance list does not ALREADY contain. That de-duplication
          is the whole reason distillation needs no cursor table: re-reading an
          overlapping batch of runs cannot inflate an entry's evidence.
        * the retained provenance list keeps the newest ``provenance_max``
          ids — genuinely, now: this is the sentence the reversal above exists
          to make true. Without it, an overlapping sequence of batches could
          evict runs from the MIDDLE of an entry's history instead of its
          oldest end, while still reporting a correct ``support`` count.
        * ``replace_conclusion`` decides whether the model's new
          ``polarity``/``rationale`` overwrite the stored ones (a Mem0-style
          UPDATE) or only the counters move (an ADD that landed on an entry
          that already existed).
        * ``updated_at`` moves ONLY when something actually changed. An
          otherwise-empty merge must not refresh the timestamp, because
          ``updated_at`` is the last tie-break of the eviction ordering — an
          entry that keeps being re-observed with no new runs and no new
          conclusion would otherwise be immortal.
        """
        ...

    def note_adopted(self, experience_ids: Sequence[str], delta: int = 1) -> int:
        """Increment ``adopted`` for the entries a run actually acted on.

        No consumer in T5 — the injection side (T6) is the only caller there
        will ever be. It ships with the store rather than later because
        ``adopted`` is the FIRST key of the eviction ordering: without a writer
        the column is constantly zero, and eviction silently degrades to
        ``(support, updated_at)``, which is a different policy from the one the
        migration documents. Returns the row count updated.
        """
        ...

    def evict_to_limit(self, max_entries: int) -> int:
        """Trim the table to ``max_entries`` rows, ascending by
        ``(adopted, support, updated_at, id)``; returns the row count deleted.

        ``id`` is the final tie-break so the deletion is deterministic even
        when three entries were written in the same second by the same batch
        (SQLite's clock is second-granular — the ``memory_revisions`` lesson).
        Without it, "which of the tied entries survived" would differ between
        two runs over identical data, and between the two backends.
        """
        ...

    def count(self) -> int: ...

    def version_signal(self) -> tuple[int, int, str]:
        """``(mutation revision, row count, newest updated_at)``, for the injection side's memo.

        Agentic Memory P2 (T6). One cheap aggregate lets a run decide whether
        the library it already scored is still the library on disk, instead of
        re-hydrating a few hundred rows and re-parsing their JSON on every
        single run. Both halves are load-bearing — see either backend's
        implementation for which change each one catches, and for why an
        ``adopted`` bump is deliberately invisible to it.
        """
        ...


# ---------------------------------------------------------------------------
# Agentic Memory P3 (T2): the per-(notebook, owner) log of short lines an
# external Agent writes via the ``add_observation`` MCP tool (T3). Feeds T4's
# per-member overlay consolidation as UNTRUSTED input only — never the
# answer/report path itself. See ``agent_observations`` in ``_migration_55``
# (SQLite) / ``0033_agent_observations.sql`` (PostgreSQL) for the schema
# rationale this port's contract continues.
# ---------------------------------------------------------------------------

#: Hard ceiling on how many observation rows ONE ``(notebook_id, owner_id)``
#: pair keeps. ``append_observation`` evicts down to this bound, oldest-first
#: by absolute instant then ``id`` (see its docstring's point 2b for the
#: exact per-backend ``ORDER BY``), in the SAME write transaction as the
#: insert that may have just pushed the group over it. The TABLE total is not
#: capped by this constant — it grows with notebooks × members — only each
#: group's own ring is. Exact value is registered in
#: ``docs/product-and-api*.md`` only; this name is the protocol boundary, not
#: a deployment knob a config file could raise.
AGENT_OBSERVATION_RING_MAX = 200

#: How many of the newest rows a single ``recent_observations``/
#: ``list_observations`` read pulls back by default. Independent of the ring
#: bound above — a caller is free to ask for fewer than the ring holds, and
#: nothing here requires the two constants to move together.
AGENT_OBSERVATION_SAMPLE_MAX = 20

#: ``agent_observations.kind`` — the two kinds of row this ONE table carries.
#: ``NOTE`` is what an Agent wrote about this notebook through
#: ``add_observation``; ``CALL`` is the system's own ledger of one tool call
#: that reached this notebook. They are separated at BOTH ends and neither
#: separation is optional:
#:
#: * every write evicts within its OWN kind (see ``append_observation`` /
#:   ``append_call``) — call accounting is written once per tool call and
#:   would otherwise evict notes a member accumulated over weeks;
#: * ``recent_observations`` — the consolidation job's read — pins
#:   ``kind='note'`` in SQL, so call accounting cannot reach the model even
#:   if a future caller forgets it exists.
AGENT_OBSERVATION_KIND_NOTE = "note"
AGENT_OBSERVATION_KIND_CALL = "call"

#: Ring bound for ``kind='call'`` rows, held SEPARATELY from
#: ``AGENT_OBSERVATION_RING_MAX`` above. Same shape, different number's right
#: to move: a member's own notes and the call ledger answer different
#: questions and are written at wildly different rates, so tying them to one
#: constant would mean tuning either one against the other's cost.
AGENT_CALL_RING_MAX = 200

#: Default read width for ``list_calls``. Mirrors
#: ``AGENT_OBSERVATION_SAMPLE_MAX``'s role for notes, and is likewise
#: independent of the ring above.
AGENT_CALL_SAMPLE_MAX = 20


def project_call_row(
    call_id: object,
    agent_profile_id: object,
    capability: object,
    created_at: object,
) -> dict:
    """The ONLY shape a caller of ``list_calls`` ever sees:
    ``{"id", "agent_profile_id", "capability", "created_at"}``.

    ``capability`` is the stored ``text`` column read back under the name it
    actually carries for a ``kind='call'`` row: the CAPABILITY SCOPE the tool
    was admitted under (``"ask:execute"``, ``"knowledge:read"``, …), not the
    tool's own name. That is a deliberate, registered coarsening — see
    ``append_call``'s docstring for why the scope (which the single choke
    point already receives) is recorded instead of a tool name (which it
    would have to be told, at every one of ~17 call sites, and which a
    newly added tool could silently forget to pass).

    ``owner_id`` is absent for the same reason it is absent from
    ``project_observation_row``: the caller supplied it as the query
    parameter, so projecting it back is a redundant copy of a value no
    reader needs.
    """
    return {
        "id": str(call_id or ""),
        "agent_profile_id": str(agent_profile_id or ""),
        "capability": str(capability or ""),
        "created_at": str(created_at or ""),
    }


def project_observation_row(
    observation_id: object,
    agent_profile_id: object,
    text: object,
    created_at: object,
) -> dict:
    """The ONLY shape a caller of ``recent_observations``/``list_observations``
    ever sees: ``{"id", "agent_profile_id", "text", "created_at"}``.

    ⚠ ``owner_id`` is deliberately NOT one of the four fields, even though it
    is the column both read methods filter by. Every consumer of these rows —
    T4's untrusted overlay-consolidation prompt, T5's "my observations" API —
    already knows whose scope it asked for, because it is the caller who
    supplied ``owner_id`` as the query parameter in the first place.
    Projecting it back into the row would be a second, redundant copy of a
    value no reader needs, and one more field an accidental ``**row`` forward
    could leak into a response shape that was never audited for it.

    Both backends call this with already-normalised field values — SQLite's
    ``created_at`` comes back as ISO text as stored; PostgreSQL's is coerced
    through ``iso_timestamp`` first — so this function itself stays free of
    any per-backend timestamp handling.
    """
    return {
        "id": str(observation_id or ""),
        "agent_profile_id": str(agent_profile_id or ""),
        "text": str(text or ""),
        "created_at": str(created_at or ""),
    }


class AgentObservationStorePort(Protocol):
    """Durable rows for ``agent_observations`` — Agentic Memory P3's
    append-only per-(notebook, owner) log of short lines an external Agent
    writes about how it used this notebook.

    Every read here is bounded by construction: both read methods take an
    explicit ``limit`` and are always further bounded by
    ``AGENT_OBSERVATION_RING_MAX`` — the group they scan can never hold more
    rows than that, because ``append_observation`` evicts down to it on every
    write. That bound is what makes the reads and the eviction DELETE cheap
    to POINT AT — ``idx_agent_observations_scope`` (``notebook_id, owner_id,
    created_at, id``) lets both land on this ONE group's rows directly
    instead of scanning past every OTHER ``(notebook_id, owner_id)`` group in
    the table, which is not bounded by this ring (it grows with notebooks ×
    members). T2's quality review measured the difference on a 100k-row
    table: the eviction DELETE went from ~9.5ms to ~1.1ms, and
    ``recent_observations``/``list_observations`` from ~3.2ms to ~0.07ms,
    with the index in place — see the migration's own comment for the exact
    numbers this cost call is registered against.
    """

    def append_observation(
        self,
        notebook_id: str,
        owner_id: str,
        agent_profile_id: str,
        *,
        text: str,
        client_request_id: str,
    ) -> tuple[str, bool]:
        """Append one observation; return ``(observation_id, deduplicated)``.

        1. Idempotent on ``(notebook_id, owner_id, agent_profile_id,
           client_request_id)`` — the same four columns
           ``idx_agent_observations_request`` covers — decided inside the
           SAME write transaction as the insert it may replace. ⚠
           ``agent_profile_id`` MUST be part of that key: two different
           Agents that each happen to mint the client-side request id
           ``"obs-1"`` are two different observations, not one. A caller that
           lands on an existing row gets that row's id back with
           ``deduplicated=True`` and writes nothing new.

        2. ``created_at`` is written as an ISO timestamp and ONLY an ISO
           timestamp — never the empty string. The column is deliberately
           excluded from ``POSTGRES_EMPTY_TIME_SENTINELS``: SQLite would
           accept ``''`` with no local symptom, and forward shadow would then
           hand that empty string to a PostgreSQL ``timestamptz`` and poison
           the whole replication direction (the
           ``notebook_share_requests.decided_at`` lesson). There is no
           parameter on this method that could produce an empty value — the
           timestamp comes from the store's own clock seam, never from the
           caller — and that is deliberate, not incidental.

        2b. The SAME write transaction's LAST step evicts this
            ``(notebook_id, owner_id)`` group down to
            ``AGENT_OBSERVATION_RING_MAX`` rows, oldest-first by absolute
            instant descending then ``id`` descending — i.e. it deletes every
            row NOT among the newest ``AGENT_OBSERVATION_RING_MAX`` under
            that ordering. ``created_at`` comes from the shared ``now`` seam
            (``repository_facade``'s ``datetime.now().astimezone().
            isoformat(timespec="microseconds")``) on both backends, so it is
            MICROSECOND-granular, not second-granular — same-instant ties are
            possible but rare, not a routine occurrence this ordering has to
            paper over. ``id`` (a random uuid, not an insertion-order column)
            is still the tie-break, for a different reason than clock
            resolution: it is the one thing BOTH backends can compare
            identically without either adding an auto-incrementing sequence
            column PostgreSQL has no ``rowid`` equivalent for (this table is
            not in ``POSTGRES_ROWID_ORDINAL_TABLES``) or leaving the outcome
            of a genuine tie backend-dependent. SQLite additionally compares
            by ``julianday(created_at)`` before the raw text — mirroring
            ``conversations``' own ``CONVERSATION_ANSWERS_ORDER_DESC``
            precedent — so a legacy naive-UTC row and a newer offset-aware
            row are compared by the instant they represent, not by which one
            happens to sort first as text; PostgreSQL's ``timestamptz``
            already normalises that away at storage time. Both backends use
            this SAME effective ordering and each carries its own regression
            pinning it — the two must never independently decide which rows
            survive a tie.
        """
        ...

    def recent_observations(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        """The newest ``limit`` rows for exactly one ``(notebook_id,
        owner_id)`` scope, ordered newest-instant-first then ``id``
        descending — the SAME ordering ``append_observation``'s eviction
        step uses (see its docstring's point 2b for the exact per-backend
        ``ORDER BY``), so "recent" means the same thing on both sides of
        this port: nothing this method can return is a row the eviction
        would have already dropped.

        3. ``owner_id`` is baked into the SQL text as a literal ``owner_id =
        ?`` / ``owner_id = %s`` equality predicate — a privacy boundary, not
        a filter a caller could omit or apply after the fact in Python. Every
        row projects through ``project_observation_row``, which deliberately
        does NOT include ``owner_id`` in its output — see that function's
        docstring for why.

        This is T4's read: the untrusted-overlay-consolidation sample calls
        it with ``limit=AGENT_OBSERVATION_SAMPLE_MAX``.

        6. ``kind='note'`` is baked into the SQL text exactly like
        ``owner_id`` is, and for the same class of reason: this read feeds a
        model prompt, and the call ledger (``kind='call'``) is the system's
        own bookkeeping, not something an Agent said. Making it a predicate
        the query cannot omit means no future caller can accidentally widen
        the prompt's input by passing an argument.
        """
        ...

    def list_observations(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        """Byte-identical to ``recent_observations`` in ordering, filtering
        and projection — the SAME query under a second name.

        Kept as a genuinely separate port method, rather than having T5's
        HTTP route call ``recent_observations`` directly, because the two
        call sites answer different questions that only happen to share an
        implementation today: T4 asks "what should the consolidation job
        read", T5's "my observations" panel asks "what should this member
        see". Naming the panel's read after the consolidation job's use case
        would make a future divergence between them (the panel growing
        pagination, say) look like it required auditing T4's call sites too,
        when it would not.

        That foreseen divergence has since happened on the OTHER axis and is
        worth naming: both reads still pin ``kind='note'``, but the panel
        additionally has ``list_calls`` beside it, while the consolidation
        job has — and must have — no such second read.
        """
        ...

    def append_call(
        self,
        notebook_id: str,
        owner_id: str,
        agent_profile_id: str,
        *,
        capability: str,
    ) -> str:
        """Record ONE tool call that reached this notebook; return its id.

        The ``kind='call'`` twin of ``append_observation``, and deliberately
        NOT the same method with a flag — four properties differ, and each
        of them is load-bearing:

        1. **No idempotency key.** ``client_request_id`` is written NULL, so
           a call row never participates in
           ``idx_agent_observations_request``'s partial unique surface. Two
           identical calls a second apart ARE two calls; folding them onto
           one row would be a lie about what happened. (This is also why the
           partial index has to stay partial: the call ledger is the one
           writer that legitimately carries no request id — see
           ``append_observation``'s guard, which fails loud on an EMPTY
           string precisely because an empty string is not NULL and WOULD
           collide.)

        2. **Eviction is per kind.** The same write transaction evicts this
           ``(notebook_id, owner_id, kind='call')`` group down to
           ``AGENT_CALL_RING_MAX`` — never touching ``kind='note'`` rows. A
           shared ring would let one burst of retrieval evict every note a
           member accumulated, which is the whole reason the kinds are
           separated rather than merged into one log.

        3. **``capability`` is a CAPABILITY SCOPE, not a tool name.** The
           value stored is the scope string the call was admitted under
           (``"ask:execute"``, ``"knowledge:read"``, ``"sources:write"``,
           …). The single choke point every notebook-scoped MCP tool already
           passes through receives that scope as an argument it must supply
           to be admitted at all, so recording it CANNOT be forgotten by a
           newly added tool — whereas a tool name would have to be threaded
           through ~17 call sites, each of which is one edit away from
           silently logging nothing. The registered cost of that choice:
           two tools admitted under the same scope are indistinguishable in
           the ledger (``search_notebook_context`` and
           ``list_knowhow_tables`` both read back as ``knowledge:read``).

        4. **Failure is never the caller's problem.** This ledger is
           bookkeeping about a call, not part of it. The caller records
           on a best-effort basis: a write that fails must never turn a tool
           call that already passed authorization into an error the Agent
           sees.

        ``created_at`` comes from the store's own clock seam and is an ISO
        timestamp, never the empty string — same contract, and same reason,
        as ``append_observation``'s point 2.
        """
        ...

    def list_calls(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        """The newest ``limit`` ``kind='call'`` rows for one ``(notebook_id,
        owner_id)`` scope, newest-instant-first then ``id`` descending — the
        same ordering, the same ``owner_id`` equality predicate as a privacy
        boundary, and the same "nothing returned here is a row eviction
        would already have dropped" property as ``recent_observations``.

        Rows project through ``project_call_row``, NOT
        ``project_observation_row``: a call row's ``text`` column is a
        capability scope, and handing it to a reader under the key ``text``
        would invite it to be rendered as if an Agent had written it.
        """
        ...

    def clear_observations(
        self,
        notebook_id: str,
        owner_id: str,
        *,
        agent_profile_id: str = "",
        kind: str = "",
    ) -> int:
        """Delete observations for one ``(notebook_id, owner_id)`` scope.
        Returns the row count deleted.

        4. ``agent_profile_id=""`` (the default) clears EVERY observation in
        scope, regardless of which Agent wrote it. A non-empty value narrows
        the delete to that one Agent's rows only, leaving every other Agent's
        observations in the same scope untouched. Both forms already carry a
        confirmed scope by construction: ``notebook_id``/``owner_id`` come
        from the caller's own authenticated server-side identity, never from
        request input a caller could spoof.

        5. ``kind=""`` (the default) clears BOTH kinds. That default is what
        every pre-existing caller means and must keep meaning: the member
        removal cleanup (``notebook_sharing``) is removing this member's
        entire footprint in this notebook, and a default that silently
        spared the call ledger would leave rows behind at exactly the moment
        the member lost access. A non-empty value narrows to that one kind —
        the panel's "clear the call log but keep what my Agents wrote".
        """
        ...
