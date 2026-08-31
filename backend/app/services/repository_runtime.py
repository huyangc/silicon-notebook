from __future__ import annotations

from pathlib import Path
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import Settings
from app.domain.repository import RepositoryCompatibilitySeams
from app.domain.extensions import (
    AskCompletedObserverCallContext,
    AskCompletedObserverHostPort,
    CompletedAskNotification,
    CompletedReportNotification,
    ReportCompletedObserverCallContext,
    ReportCompletedObserverHostPort,
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.domain.gap_consult import GapConsultHostPort
from app.domain.ask_engine import AskEngineHostPort
from app.domain.indexing_pipeline import IndexingPipelineHostPort
from app.core.event_logging import EventLogger, llm_log_dir_aligned
from app.repositories.bundle import PersistenceBundleFactory
from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
from app.repositories.ports import (
    EmbeddingStorePort,
    GroupStorePort,
    IndexProjectionStorePort,
    SharingStorePort,
)
from app.repositories.source_files import SourceFileStore
from app.services.agent_profile_job import AgentProfileConsolidationService
from app.services.retrieval_experience_job import (
    RetrievalExperienceDistillationService,
)
from app.services.search_profile_job import SearchProfileInferenceService
from app.services.catalog_job import CommandCatalogService
from app.services.kg_analysis import KgAnalysisService
from app.services.kg_mutation import KgMutationCoordinator
from app.services.knowledge_governance import KnowledgeGovernanceService
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.knowledge_query import KnowledgeQueryService
from app.services.evidence_context import EvidenceContextService
from app.services.graph_retrieval import GraphRetrievalService
from app.services.model_provider import (
    RuntimeModelProvider,
    validate_process_local_scheduler_deployment,
)
from app.services.model_registry import SystemModelServiceRegistry
from app.services.model_status import ModelStatusService
from app.services.memory_service import MemoryService
from app.services.memory_retrieval import MemoryRetriever
from app.services.kg import scheduler as kg_scheduler
from app.services.collection_catalog import CollectionCatalogService
from app.services.collection_enumeration import CollectionEnumerationService
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery
from app.services.notebook_sharing import NotebookCopyService, NotebookSharingService
from app.services.report_execution import (
    REPORT_CANCELLATIONS,
    ReportExecutionCoordinator,
)
from app.services.report_application import ReportApplicationService
from app.services.retrieval_snapshot_cache import RetrievalSnapshotCache
from app.services.review_queue_memo import ReviewQueueMemo
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_service import RetrievalService
from app.services.scale_artifact_catalog import ScaleArtifactCatalog
from app.services.scale_artifact_runtime import ScaleArtifactRuntime
from app.services.scale_index_builder import ScaleIndexBuilder
from app.services.schema_registry import SchemaRegistryService
from app.services.source_chunking import SourceChunkingService
from app.services.indexing_pipeline import IndexingPipelineService
from app.services.source_embedding import SourceEmbeddingService
from app.services.source_ingestion import SourceIngestionService
from app.services.chunk_question_index import ChunkQuestionIndexService
from app.services.source_graph_primitives import SourceGraphPrimitives
from app.services.source_subgraph import SourceSubgraphService
from app.services.source_subgraph_ppr import SourceSubgraphPprService
from app.services.source_partitioned_ppr import SourcePartitionedPprService
from app.services.source_graph_activation import (
    SelectedSourceGraphActivationService,
    hydrate_selected_graph_chunk_rows,
)
from app.services.retrieval_enrichment import BaselineProtectedEnrichmentService
from app.services.vector_cache import VectorCache
# Task 23: Ask detached-execution composition (appended block — parallel
# Gate-8 tracks keep the shared import list above untouched).
from app.services import background_jobs
from app.services.ask_execution import AskCancellationRegistry, AskExecutionCoordinator
# Task 24: Ask mode engines + synthesis (appended — same convention).
from app.services.ask_service import AskService
from app.services.notebook_scale import CopyStatsMemo, NotebookScaleProfile
from app.services.pending_actions_service import PendingActionsService
from app.services.parser_chain_execution import BuiltinParserChainHost

_log = logging.getLogger("silicon_notebook.repository_runtime")


class _AskCompletedAccess:
    """Core-only, request-local adapter behind one observer capability."""

    __slots__ = ("__notify",)

    def __init__(self, notify: Callable[[], None]) -> None:
        self.__notify = notify

    def notify(self) -> None:
        self.__notify()


class _ReportCompletedAccess:
    """Opaque adapter for the existing report-completed consolidation call."""

    __slots__ = ("__notify",)

    def __init__(self, notify: Callable[[], None]) -> None:
        self.__notify = notify

    def notify(self) -> None:
        self.__notify()


# ──────────────────────────────────────────────── domain composition ──
# ``RepositoryRuntime.__init__`` used to inline the wiring of the whole
# backend in one 428-line constructor (architecture review 2026-08-21, A1/B4:
# "按领域提取纯构造函数……`RepositoryRuntime` 只组合这些结果").  The composition
# now lives in the frozen domain bundles below.  Two properties make the split
# load-bearing rather than cosmetic, and both are pinned by
# ``backend/tests/test_repository_runtime_composition.py``:
#
#   * every ``_build_*`` is a module-level PURE function whose parameters are
#     earlier bundles — never ``self``/the runtime.  A builder that could
#     reach the runtime would be able to read a *later* domain and re-create
#     the cycle this split removes, so the call order in ``__init__`` IS the
#     dependency topology and a cycle cannot be spelled;
#   * ``__init__`` keeps exactly one job: call the builders in order and mount
#     each bundle field with an explicit ``self.<name> = <bundle>.<name>``
#     line, so a dropped seat is visible in the diff and caught by the frozen
#     attribute-set test.
#
# Construction order still matters for the three process-level side effects in
# domain 1 and the persistence bundle in domain 2; every later domain only
# constructs objects (no seam calls, no I/O), which is why they may be grouped
# by domain rather than by their historical interleaved order.
#
# The one deliberate exception to "no runtime in a builder": domains 6, 9 and
# 10 take NARROW runtime-bound callables (``_current_user_id``, the
# ``ask_service`` accessor and ``_note_ask_completed``).  Those three seats are
# late-bound by design — they resolve state that only exists after the facade
# constructor finishes — and passing the two-or-three callables keeps the edge
# explicit and one-directional where passing ``self`` would not.


@dataclass(frozen=True)
class _ProcessFoundation:
    """Process-level collaborators every other domain reads."""

    settings: Settings
    root_dir: Path
    seams: RepositoryCompatibilitySeams
    event_log: EventLogger
    # Production factory injection supplies the process-shared host used
    # by app.state, Ask and Report. Direct constructor tests may leave the
    # optional seat empty and retain their exact historical behavior.
    retrieval_contributors: RetrievalContributorHostPort | None
    ask_completed_observers: AskCompletedObserverHostPort | None
    report_completed_observers: ReportCompletedObserverHostPort | None
    ask_engines: AskEngineHostPort | None
    indexing_pipelines: IndexingPipelineHostPort | None
    # Gap consultation is the one host with no built-in contribution at all:
    # an empty seat here is the shipped shape, not an unwired deployment.
    gap_consult: GapConsultHostPort | None
    parser_provider_chain: ParserProviderChainHostPort
    models: Any


def _build_process_foundation(
    settings: Settings,
    root_dir: Path,
    seams: RepositoryCompatibilitySeams,
    model_provider: Any | None,
    # Keyword-only: four same-shaped optional host seats, where a positional
    # call site would swap two of them without any type or test noticing.
    *,
    retrieval_contributor_host: RetrievalContributorHostPort | None,
    ask_completed_observer_host: AskCompletedObserverHostPort | None,
    report_completed_observer_host: ReportCompletedObserverHostPort | None,
    ask_engine_host: AskEngineHostPort | None,
    indexing_pipeline_host: IndexingPipelineHostPort | None,
    gap_consult_host: GapConsultHostPort | None,
    parser_provider_chain_host: ParserProviderChainHostPort | None,
) -> _ProcessFoundation:
    """Domain 1 — depends on nothing but the constructor arguments.

    It owns the three process-level side effects that must keep happening in
    this exact order, ahead of every other domain: the scheduler deployment
    check, the event logger (whose ``logger`` carries the LLM-log-directory
    warning) and ``kg_scheduler.initialize`` sized from the model provider.
    ``BuiltinParserChainHost`` is a stateless fallback, so composing it with
    the returned bundle rather than before the warning is order-neutral.
    """

    validate_process_local_scheduler_deployment()
    event_log = EventLogger(settings, channel="events", per_user=True)
    if not llm_log_dir_aligned(settings.llm_log_path, settings.event_log_dir):
        event_log.logger.warning(
            "LLM_LOG_PATH 的目录(%s)与 EVENT_LOG_DIR(%s)不一致，"
            "日志查看器将读不到 per-user 的 llm 日志；请对齐两者或都设为同一目录。",
            settings.llm_log_path,
            settings.event_log_dir,
        )
    models = model_provider or RuntimeModelProvider(settings, event_log)
    kg_scheduler.initialize(
        window_workers=models.parallelism("kg_extract"),
        job_workers=settings.kg_job_concurrency,
    )
    return _ProcessFoundation(
        settings=settings,
        root_dir=root_dir,
        seams=seams,
        event_log=event_log,
        retrieval_contributors=retrieval_contributor_host,
        ask_completed_observers=ask_completed_observer_host,
        report_completed_observers=report_completed_observer_host,
        ask_engines=ask_engine_host,
        indexing_pipelines=indexing_pipeline_host,
        gap_consult=gap_consult_host,
        parser_provider_chain=(
            parser_provider_chain_host or BuiltinParserChainHost()
        ),
        models=models,
    )


@dataclass(frozen=True)
class _PersistenceSeats:
    """Every port seat produced by the ONE persistence bundle.

    Construction of all of these is seam-free (they only capture the shared
    database boundary plus the id/clock seams), so the whole domain is eager.
    ``model_status`` is the one service in here: it is the read/write pair over
    ``model_status_store`` and the process model provider, and it has to be
    built where the store is because it also installs the provider's
    observation sink.
    """

    database: Any
    identity: Any
    model_status_store: Any
    model_status: ModelStatusService
    queries: Any
    notebook_store: Any
    kg_build_jobs: Any
    # Agentic Memory P1 (T2): bare store seat, no consumer yet — T3-T6
    # wire injection/routes on top of this. See AgentProfileStorePort in
    # repositories/ports.py for the read/write contract.
    agent_profile: Any
    # Agentic Memory P2 (T5): bare store seat for the deployment-GLOBAL
    # retrieval-experience library. Its distillation service is built by
    # ``_build_agent_jobs`` (it needs ``models``/``event_log``); the injection
    # side is T6. See RetrievalExperienceStorePort in repositories/ports.py —
    # note in particular why a store with no tenancy column is the right
    # shape here, and what carries its isolation argument instead.
    retrieval_experiences: Any
    # Agentic Memory P3 (T2): bare store seat, no consumer yet — T3 (MCP
    # ``add_observation``), T4 (untrusted overlay-consolidation sample)
    # and T5 (the member-facing "my observations" API) wire on top of
    # this. See AgentObservationStorePort in repositories/ports.py.
    agent_observations: Any
    # Deployment-plugin runtime enable/disable switch + audit (who, when):
    # bare store seat, no consumer yet — T3 (the admission-gate refresh
    # service) and T4 (the admin route + PATCH endpoint) wire on top of
    # this. See ExtensionToggleStorePort in repositories/ports.py.
    extension_toggles: Any
    source_store: Any
    chunk_store: Any
    # Task 25: reports-table row persistence is seam-free (the shared
    # database boundary + the id/clock seams + identity's current_user).
    # The execution coordinator over it is finished by
    # wire_report_execution() because its engine factory rides the lazily
    # wired retrieval and evidence-context ports.
    report_store: Any
    # Task 13: the three knowledge-domain persistence stores share the ONE
    # database boundary. Their primitives are connection-taking — the
    # facade keeps every transaction/connection boundary (its `_write` /
    # `_connect` compatibility seams stay the observable commit points).
    knowledge: Any
    governance: Any
    unified_kg: Any
    # Task 22: Ask/answer/conversation/job/trace persistence shares the ONE
    # database boundary.  Identity is explicit (user_id per call — the
    # store never reads request ContextVars) and the seams dataclass is
    # stored, never evaluated.  Cancel-event registry + fail-open trace
    # logging stay facade-side.
    ask_state: Any
    memory_store: Any
    index_projections: IndexProjectionStorePort
    # Vector persistence is finished by wire_persistence(): its write seat
    # is the facade's `_write` compatibility seam, which only exists once
    # the facade constructor reaches it.
    embedding_store: EmbeddingStorePort
    # Sharing/deep-copy composition is finished by wire_sharing(): its
    # collaborators (facade _insert_row seat, notebook_copy_stats memo,
    # storage_dir) are facade-bound seams.
    sharing_store: SharingStorePort
    # 群组 / 组成员 / 授权边的行持久化。构造 seam-free(只存 bundle 给的实例),
    # 故 eager;它没有 service 层——策略全在 `app/api/group_routes.py`,store 只
    # 管行,所以路由经 `deps.group_repository()` 直取这个端口(与
    # `notebook_store_port()` 同一惯例),中间不架一层只做转发的 service。
    groups: GroupStorePort
    # knowhow-tables PR-1 Task 2: row persistence for the Task-1 schema
    # (knowhow_tables/columns/rows/cells + notebook_assets). Seam-free
    # (only new_id/now, like notebook_store above). Task 5's projector and
    # Task 6's import/table API depend on the facade's one-hop delegates
    # over this exact instance.
    knowhow_store: Any
    # knowhow 单表跨 notebook 传输的 SQL（快照+单事务插入+校验）。id/时钟
    # 由 transfer.py 的 _remap 直接从 repo._runtime.seams 取（见该文件头
    # 注释），store 自己不需要——不带 new_id/now 构造参数。
    knowhow_transfer_store: Any
    # knowhow 表版本管理 Task 3：变更流水/里程碑的读侧 store。new_id/now
    # 与上面 knowhow_store 用同一对 seams 可调用对象，保持三个 store 的
    # id/时钟来源单一（record_change 本身是模块级函数，不在这里持有——
    # 由 Task 4-6 的写方法在各自事务内直接调用）。
    knowhow_history_store: Any
    # Consumed by ``_build_content_tools`` only; deliberately NOT mounted on
    # the runtime — the command-catalog service is the single owner of this
    # store, and an extra runtime attribute would be a second door to it.
    catalog_store: Any


def _build_persistence_seats(
    persistence_factory: PersistenceBundleFactory,
    foundation: _ProcessFoundation,
) -> _PersistenceSeats:
    """Domain 2 — the ONE persistence bundle, from domain 1's config/seams.

    Inputs: ``foundation.settings`` / ``root_dir`` / ``seams`` for the bundle
    factory, ``foundation.event_log`` for the database stats sink and
    ``foundation.models`` for the model-status pair.  This is the only place
    that touches the bundle: every later domain reads the seats off the
    returned object, so "which store did this service get" is answerable from
    one function.
    """

    bundle = persistence_factory.create(
        settings=foundation.settings,
        root_dir=foundation.root_dir,
        seams=foundation.seams,
    )
    stats = getattr(bundle.database, "stats", None)
    if stats is not None:
        stats.sink = foundation.event_log.emit
    model_status = ModelStatusService(
        getattr(foundation.models, "registry", SystemModelServiceRegistry({}, {})),
        foundation.models,
        bundle.model_status,
    )
    set_observation_sink = getattr(foundation.models, "set_observation_sink", None)
    if callable(set_observation_sink):
        set_observation_sink(model_status.record_provider_observation)
    return _PersistenceSeats(
        database=bundle.database,
        identity=bundle.identity,
        model_status_store=bundle.model_status,
        model_status=model_status,
        queries=bundle.queries,
        notebook_store=bundle.notebooks,
        kg_build_jobs=bundle.kg_build_jobs,
        agent_profile=bundle.agent_profile,
        retrieval_experiences=bundle.retrieval_experiences,
        agent_observations=bundle.agent_observations,
        extension_toggles=bundle.extension_toggles,
        source_store=bundle.sources,
        chunk_store=bundle.chunks,
        report_store=bundle.reports,
        knowledge=bundle.knowledge,
        governance=bundle.governance,
        unified_kg=bundle.unified_kg,
        ask_state=bundle.ask_state,
        memory_store=bundle.memory,
        index_projections=bundle.index_projection,
        embedding_store=bundle.embeddings,
        sharing_store=bundle.sharing,
        groups=bundle.groups,
        knowhow_store=bundle.knowhow,
        knowhow_transfer_store=bundle.knowhow_transfer,
        knowhow_history_store=bundle.knowhow_history,
        catalog_store=bundle.catalog,
    )


@dataclass(frozen=True)
class _NotebookDomain:
    """Notebook catalog, source-file storage and the deep-copy/sharing seats."""

    notebook_summaries: NotebookSummaryQuery
    source_files: SourceFileStore
    catalog: NotebookCatalogService
    # Finished by wire_sharing(): its collaborators (facade _insert_row seat,
    # notebook_copy_stats memo, storage_dir) are facade-bound seams that only
    # exist once the facade constructor reaches them.
    notebook_copies: "NotebookCopyService | None"
    sharing: "NotebookSharingService | None"


def _build_notebook_domain(
    foundation: _ProcessFoundation, seats: _PersistenceSeats
) -> _NotebookDomain:
    """Domain 3 — notebook identity/catalog over domain 2's seats.

    Inputs: ``seats.database`` / ``queries`` / ``identity`` / ``notebook_store``
    / ``kg_build_jobs`` from domain 2 and ``foundation.settings`` from domain 1.
    """

    summaries = NotebookSummaryQuery(
        seats.database,
        seats.queries,
        seats.kg_build_jobs,
        foundation.indexing_pipelines,
    )
    # Source files resolve storage_dir through the database. Construct
    # BEFORE the catalog so its storage_dir callable can bind THIS store
    # directly.
    source_files = SourceFileStore(
        seats.database.resolve_path(foundation.settings.storage_dir),
        resolve_path=seats.database.resolve_path,
    )
    return _NotebookDomain(
        notebook_summaries=summaries,
        source_files=source_files,
        catalog=NotebookCatalogService(
            store=seats.notebook_store,
            summaries=summaries,
            queries=seats.queries,
            identity=seats.identity,
            # Lazy: resolves the LIVE storage root only at delete_notebook
            # time.  Deliberately default-bound to the eager SourceFileStore
            # above — NOT ``lambda: self.storage_dir`` — because closing over
            # the runtime would chain catalog -> runtime -> facade-bound seam
            # closures -> SQLiteRepository, keeping the whole facade alive for
            # as long as anything retains the catalog (ScaleArtifactRuntime
            # holds it as ``notebooks``; see
            # test_retained_scale_runtime_does_not_transitively_retain_repository).
            # Liveness is preserved: the facade's mutable storage_dir setter
            # chain (sqlite_repository.storage_dir -> runtime.set_storage_dir)
            # mutates source_files.storage_dir IN PLACE, so this callable
            # observes every post-construction swap (same Callable[[], Path]
            # convention as wire_sharing's NotebookCopyService).
            storage_dir=lambda source_files=source_files: source_files.storage_dir,
        ),
        notebook_copies=None,
        sharing=None,
    )


@dataclass(frozen=True)
class _SourcePipelineDomain:
    """Parse → chunk → embed pipeline seats for one source."""

    chunk_question_index: ChunkQuestionIndexService
    # Finished by wire_source_pipeline(): the mutable embedder and
    # _mark_unified_kg_dirty collaborators plus the wired EmbeddingStore only
    # exist once the facade constructor reaches them.
    source_embedding: "SourceEmbeddingService | None"
    source_chunking: "SourceChunkingService | None"
    # Finished by wire_source_ingestion(): its collaborators (facade _write
    # seat, facade-owned parse/summarize/model seams and the TEMPORARY KG
    # callbacks that Gate 5 replaces with real services) are facade-bound
    # seams that only exist once the facade constructor reaches them.
    source_ingestion: "SourceIngestionService | None"


def _build_source_pipeline(
    foundation: _ProcessFoundation, seats: _PersistenceSeats
) -> _SourcePipelineDomain:
    """Domain 4 — the per-source ingestion pipeline.

    Inputs: ``foundation.settings`` / ``models`` / ``event_log`` / ``seams``
    from domain 1 and ``seats.chunk_store`` from domain 2.  Everything except
    the generated-question index is a lazy seat; none of them calls a seam
    here.
    """

    return _SourcePipelineDomain(
        chunk_question_index=ChunkQuestionIndexService(
            settings=foundation.settings,
            chunks=seats.chunk_store,
            models=foundation.models,
            event_log=foundation.event_log,
            now=foundation.seams.now,
        ),
        source_embedding=None,
        source_chunking=None,
        source_ingestion=None,
    )


@dataclass(frozen=True)
class _ReportDomain:
    """Deep-report application service plus the process-global cancel registry."""

    report_application: ReportApplicationService
    # ``REPORT_CANCELLATIONS`` is the intentionally process-global canonical
    # owner: the runtime, the report coordinator and the module compatibility
    # functions must share this same identity reference.
    report_cancellations: Any
    # Finished by wire_report_execution() — its engine factory rides the
    # lazily wired retrieval and evidence-context ports.
    report_execution: "ReportExecutionCoordinator | None"


def _build_report_domain(
    seats: _PersistenceSeats, notebook: _NotebookDomain
) -> _ReportDomain:
    """Domain 5 — reports over domain 2's row store and domain 3's catalog.

    Inputs: ``seats.report_store`` from domain 2 and ``notebook.catalog`` from
    domain 3.
    """

    return _ReportDomain(
        report_application=ReportApplicationService(
            notebook.catalog, seats.report_store
        ),
        report_cancellations=REPORT_CANCELLATIONS,
        report_execution=None,
    )


@dataclass(frozen=True)
class _KnowledgeDomain:
    """Knowledge-graph analysis, schema registry and the lifecycle seats."""

    # KG 质量分析报告的只读装配(T3)。**后端中性**:它只吃 database + unified_kg
    # 两个 seam(两者都是后端相关的实例,由 bundle 给),自己不 import 任何后端 ——
    # 所以它落在这个中性 runtime 里,而不是像 checkup 那样落在两个 facade 各一份
    # (checkup 依赖 sqlite/postgres 各自的 maintenance adapter,neutrality 守卫
    # 禁止 runtime import 它们)。构造是 eager 且 seam-free 的(只存引用);进程内
    # 的记忆化因此跟着 runtime 单例跨请求存活 —— 那正是它存在的意义(大库上板块
    # 列表要排 8.8 万行)。
    kg_analysis: KgAnalysisService
    # Task 13: schema CRUD + LLM-backed induction. It requests the
    # ``schema_induction`` workload from the process-owned provider.
    schema_registry: SchemaRegistryService
    # Finished by wire_kg_mutations(): its remaining collaborators (the
    # scale-runtime auto-index once-set, the corpus-language memo and the
    # facade `_write` transaction seat) only exist once the facade constructor
    # reaches them.  The caches come from the retrieval domain's snapshots.
    kg_mutations: "KgMutationCoordinator | None"
    # R3 T-A2: the review-queue ranking memo. Seam-free (a lock, an OrderedDict
    # and a seq the SERVICE reads through its injected closure), so the runtime
    # owns it eagerly here — one instance per runtime, exactly like the
    # retrieval domain's CopyStatsMemo. Deliberately NOT inside
    # RetrievalSnapshotCache: that class's ``invalidate_kg`` fires on every
    # online KG mutation, INCLUDING every ``set_edge_review`` click, which would
    # wipe the ranking the carry-forward exists to keep warm.
    review_queue_memo: ReviewQueueMemo
    # Finished by wire_knowledge_lifecycle(): their collaborators (the facade
    # `_write`/`_connect` transaction seats, the facade-owned unified/viz cache
    # objects, the coordinator-backed dirty/invalidate wrappers, the
    # process-owned model provider and the Gate-6 scale/viz adapters) only
    # exist once the facade constructor reaches them.
    knowledge_governance: "KnowledgeGovernanceService | None"
    knowledge_lifecycle: "KnowledgeLifecycleService | None"
    knowledge_query: "KnowledgeQueryService | None"
    pending_actions_service: "PendingActionsService | None"


def _build_knowledge_domain(
    foundation: _ProcessFoundation,
    seats: _PersistenceSeats,
    current_user_id: Callable[[], str],
) -> _KnowledgeDomain:
    """Domain 6 — knowledge graph analysis/governance over domain 2's seats.

    Inputs: ``seats.database`` / ``unified_kg`` / ``notebook_store`` /
    ``knowledge`` / ``source_store`` from domain 2, ``foundation.models`` /
    ``settings`` / ``seams`` from domain 1, plus the runtime's narrow
    ``current_user_id`` accessor (resolved per call, never at build time).
    """

    return _KnowledgeDomain(
        kg_analysis=KgAnalysisService(
            database=seats.database,
            unified_kg=seats.unified_kg,
            now=foundation.seams.now,
        ),
        schema_registry=SchemaRegistryService(
            seats.database,
            seats.notebook_store,
            seats.knowledge,
            seats.source_store,
            foundation.models,
            foundation.settings,
            current_user_id=current_user_id,
        ),
        kg_mutations=None,
        review_queue_memo=ReviewQueueMemo(),
        knowledge_governance=None,
        knowledge_lifecycle=None,
        knowledge_query=None,
        pending_actions_service=None,
    )


@dataclass(frozen=True)
class _RetrievalDomain:
    """Retrieval + memory-recall seats, all finished by wire_retrieval/memory."""

    # Task 17: the retrieval snapshot caches are seam-free (a version-keyed
    # VectorCache sized from settings plus the plain unified-graph dict), so
    # the runtime owns them eagerly. The facade's `_vector_cache` /
    # `_unified_cache` handles are write-through descriptors over THESE
    # objects and the KG mutation coordinator reads them through this cache —
    # one owner, no facade-only copies.
    retrieval_snapshots: RetrievalSnapshotCache
    memory_service: "MemoryService | None"
    memory_retriever: "MemoryRetriever | None"
    evidence_context: "EvidenceContextService | None"
    candidate_retrieval: "CandidateRetrievalService | None"
    graph_retrieval: "GraphRetrievalService | None"
    retrieval: "RetrievalService | None"


def _build_retrieval_domain(foundation: _ProcessFoundation) -> _RetrievalDomain:
    """Domain 7 — retrieval/memory seats.

    Inputs: only ``foundation.settings`` from domain 1 (the vector cache size).
    Every other seat is embedder-bound and therefore filled by
    ``wire_retrieval()`` / ``wire_memory()`` after the facade constructor
    finishes.
    """

    return _RetrievalDomain(
        retrieval_snapshots=RetrievalSnapshotCache(
            vector_cache=VectorCache(
                max_entries=foundation.settings.vector_cache_max_entries,
                per_family_entries=(
                    foundation.settings.vector_cache_per_family_entries
                ),
                max_bytes=foundation.settings.vector_cache_max_bytes,
            ),
            unified_cache={},
            # 每个 runtime 一份 copy-stats memo(codex PR#634 R2 P2-2:模块级
            # 全局违反 development.md 的 runtime-owned 契约)。
            copy_stats_memo=CopyStatsMemo(),
        ),
        memory_service=None,
        memory_retriever=None,
        evidence_context=None,
        candidate_retrieval=None,
        graph_retrieval=None,
        retrieval=None,
    )


@dataclass(frozen=True)
class _SourceGraphDomain:
    """Selected-source graph projection, its PPR lanes and the scale artifacts."""

    # Task 18: scale/viz artifact FILE persistence is seam-free (raw
    # settings.storage_dir paths + the pure kg.scale_index / kg.viz_index
    # load/save modules), so the runtime owns it eagerly.
    scale_artifact_store: ScaleArtifactStore
    # Read-only selected-source graph projection. It is intentionally not
    # consumed by Ask/Report in this PR; wiring it here gives later graph
    # primitives one backend-neutral, generation-keyed snapshot owner.
    source_subgraphs: SourceSubgraphService
    # Shadow-only selected-source graph tools. Public Ask/Report wiring is
    # intentionally deferred to the baseline-preserving rollout PRs.
    source_graph_primitives: SourceGraphPrimitives
    # Shadow lane keeps the historical result immutable. Ask/Report wiring
    # remains deferred; later consumers must finish B before invoking G.
    source_graph_enrichment: BaselineProtectedEnrichmentService
    source_subgraph_ppr: SourceSubgraphPprService
    source_partitioned_ppr: SourcePartitionedPprService
    selected_source_graph: SelectedSourceGraphActivationService
    # The DB projections + read catalog over the artifact store are finished
    # by wire_scale_artifacts() — their collaborators (the facade `_connect`
    # read seam, the `_in_batches` IN-chunking helper, the retrieval-owned
    # ent-chunk/mention/vector-matrix caches, the memoized version key, the
    # LRU cache + cold-load lock table and the model-error note) are
    # facade-bound seams that only exist once the facade constructor
    # reaches them.
    scale_catalog: "ScaleArtifactCatalog | None"
    scale_builder: "ScaleIndexBuilder | None"
    scale_artifacts: "ScaleArtifactRuntime | None"


def _build_source_graph_domain(
    foundation: _ProcessFoundation, seats: _PersistenceSeats
) -> _SourceGraphDomain:
    """Domain 8 — selected-source graph lanes over domain 2's projections.

    Inputs: ``seats.index_projections`` from domain 2 and
    ``foundation.settings`` / ``event_log`` from domain 1.
    """

    settings = foundation.settings
    subgraphs = SourceSubgraphService(
        projections=seats.index_projections, settings=settings
    )
    primitives = SourceGraphPrimitives(snapshots=subgraphs, settings=settings)
    enrichment = BaselineProtectedEnrichmentService()
    online_ppr = SourceSubgraphPprService(settings=settings)
    artifact_store = ScaleArtifactStore(settings)
    partitioned_ppr = SourcePartitionedPprService(
        settings=settings,
        artifacts=artifact_store,
        projections=seats.index_projections,
    )
    return _SourceGraphDomain(
        scale_artifact_store=artifact_store,
        source_subgraphs=subgraphs,
        source_graph_primitives=primitives,
        source_graph_enrichment=enrichment,
        source_subgraph_ppr=online_ppr,
        source_partitioned_ppr=partitioned_ppr,
        selected_source_graph=SelectedSourceGraphActivationService(
            settings=settings,
            snapshots=subgraphs,
            primitives=primitives,
            online_ppr=online_ppr,
            partitioned_ppr=partitioned_ppr,
            enrichment=enrichment,
            event_log=foundation.event_log,
        ),
        scale_catalog=None,
        scale_builder=None,
        scale_artifacts=None,
    )


@dataclass(frozen=True)
class _AskDomain:
    """Ask cancellation + detached streaming execution."""

    # Task 23: seam-free (the Task-22 ask_state store, the module-level
    # copied-context job helper and the runtime event log).  The facade's
    # frozen _ask_cancel_events/_ask_cancel_lock attributes become
    # compatibility handles over THIS registry (one owner — the explicit
    # cancel endpoint and the coordinator's register/unregister meet in the
    # same map).
    ask_cancellations: AskCancellationRegistry
    # Task 24: composition rides the wired retrieval port, which is
    # embedder-bound and only exists after the facade constructor finishes
    # and the first ask touches it.
    ask: "AskService | None"
    ask_execution: AskExecutionCoordinator


def _build_ask_domain(
    foundation: _ProcessFoundation,
    seats: _PersistenceSeats,
    ask_service: Callable[[], AskService],
    note_ask_completed: Callable[[str, str, str], None],
) -> _AskDomain:
    """Domain 9 — Ask execution over domain 2's ask-state store.

    Inputs: ``seats.ask_state`` from domain 2, ``foundation.event_log`` from
    domain 1, plus the two NARROW runtime-bound callables this domain cannot
    resolve on its own: the ``ask_service`` accessor (Task 24 — the ONE
    runtime-owned AskService, composed lazily) and ``_note_ask_completed``.
    Both are late-bound by contract; the builder never sees the runtime.
    """

    cancellations = AskCancellationRegistry()
    return _AskDomain(
        ask_cancellations=cancellations,
        ask=None,
        ask_execution=AskExecutionCoordinator(
            ask_state=seats.ask_state,
            cancellations=cancellations,
            job_submitter=background_jobs,
            event_log=foundation.event_log,
            ask=ask_service,
            # Agentic Memory P1 (T5):完成一次提问 ⇒ 推进**该成员**在该库的覆盖层
            # 计数。
            # ⚠ 三参:coordinator 按 (nb, uid, mode_id) 调用(codex #524 R5 P1:
            # 这里少一个参数,TypeError 会被协调器的 fail-open 吞掉,两条后台
            # 链一起静默死亡而答案照常交付——由行为用例按真实 arity 钉住)。
            # 这层 lambda 保留只是为了把上面那条 ⚠ 注释钉在正确的 arity 上,不是
            # 为了迟绑定——``note_ask_completed`` 参数本身已经是 ``self._note_ask_completed``
            # 这个 bound method,迟绑定早已成立。
            note_ask_completed=lambda nb, uid, mode_id: note_ask_completed(
                nb, uid, mode_id
            ),
        ),
    )


@dataclass(frozen=True)
class _ContentToolsDomain:
    """Content-facing tool services composed over the persistence seats."""

    # 方案 C·C1b:命令目录抽取。**后端中性**——它吃的全是端口/可调用
    # (catalog/source/chunk/knowhow store + models + event_log + 两个 seam),
    # 自己不 import 任何后端,所以和 kg_analysis 一样落在这个中性 runtime,而不是
    # 每个 facade 各构一份。单例是必要的而非顺手:取消事件的注册表挂在实例上,
    # 「发起 job 的请求」与「跑 job 的后台线程」必须看到同一份。
    command_catalog: CommandCatalogService
    # 逐步推理集合枚举 PR-2 T2:类型化集合的「地图层」计数。只吃窄端口
    # (database/sources/notebooks/queries/unified_kg),零模型调用,构造即
    # 建三个有界进程内缓存,故与几个 store 一样是 eager 且无 seam。
    collection_catalog: CollectionCatalogService
    # 同批 T3:类型化集合的「清单层」。刻意吃**上面那一个** catalog 实例而不是
    # 自己新建——地图计数与枚举必须来自同一份 per-source 缓存,否则会出现
    # 「地图报 12、清单只列 8」这种无法自证的假部分。同样零模型调用、无 seam。
    collection_enumeration: CollectionEnumerationService


def _build_content_tools(
    foundation: _ProcessFoundation,
    seats: _PersistenceSeats,
    current_user_id: Callable[[], str],
) -> _ContentToolsDomain:
    """Domain 10 — command-catalog extraction and the typed-collection layers.

    Inputs: ``seats.catalog_store`` / ``source_store`` / ``chunk_store`` /
    ``knowhow_store`` / ``database`` / ``notebook_store`` / ``queries`` /
    ``knowledge`` / ``unified_kg`` from domain 2, ``foundation.models`` /
    ``event_log`` / ``seams`` from domain 1, plus the runtime's narrow
    ``current_user_id`` accessor.
    """

    collection_catalog = CollectionCatalogService(
        database=seats.database,
        sources=seats.source_store,
        notebooks=seats.notebook_store,
        queries=seats.queries,
        unified_kg=seats.unified_kg,
    )
    return _ContentToolsDomain(
        command_catalog=CommandCatalogService(
            catalog=seats.catalog_store,
            sources=seats.source_store,
            chunks=seats.chunk_store,
            knowhow=seats.knowhow_store,
            models=foundation.models,
            event_log=foundation.event_log,
            now=foundation.seams.now,
            current_user_id=current_user_id,
        ),
        collection_catalog=collection_catalog,
        collection_enumeration=CollectionEnumerationService(
            database=seats.database,
            catalog=collection_catalog,
            sources=seats.source_store,
            notebooks=seats.notebook_store,
            knowledge=seats.knowledge,
            unified_kg=seats.unified_kg,
        ),
    )


@dataclass(frozen=True)
class _AgentJobsDomain:
    """The three post-ask background chains (Agentic Memory P1/P2/P3)."""

    # Agentic Memory P1 (T4):「AI 对这个库的理解」的巡固任务。后端中性——只吃
    # 端口(agent_profile/database/sources/queries)与 models/event_log 两个进程级
    # 对象,故与 command_catalog 同处这个中性 runtime。**单例是必要的而非顺手**:
    # 阈值闸与单飞都落在持久行上,但「谁来提交后台线程」必须只有一份实现,来源
    # 管线的 hook 与(T6 的)手动重建按钮才会走同一条 claim→submit 路径。
    agent_profile_jobs: AgentProfileConsolidationService
    # Agentic Memory P2 (T5):检索打法的蒸馏任务。与它的 P1 兄弟同处这个
    # **后端中性** runtime,同样只吃端口与两个进程级对象。单例同样是必要的
    # 而非顺手:阈值计数与单飞都是**进程内**状态,构造两份就等于两个各自计
    # 到一半的计数器和两条可以同时开跑的链。
    retrieval_experience_jobs: RetrievalExperienceDistillationService
    # Agentic Memory P3(B-Profile,T7):每用户「检索/回答风格偏好」文档里
    # 唯一一个 v1 会归纳的字段(``answer_language``)的确定性、零模型触发
    # service。单例同样是必要的而非顺手:每用户阈值计数是**进程内**状态,
    # 分身出第二个实例就是两份各自计到一半的计数器。它比它的两个 P1/P2
    # 兄弟更薄——没有模型调用,读写都在同一个进程内锁的临界区内同步完成,
    # 不需要后台线程池句柄(见 ``search_profile_job`` 模块 docstring)。
    search_profile_jobs: SearchProfileInferenceService


def _build_agent_jobs(
    foundation: _ProcessFoundation, seats: _PersistenceSeats
) -> _AgentJobsDomain:
    """Domain 11 — the post-ask consolidation/distillation/inference chains.

    Inputs: ``seats.agent_profile`` / ``retrieval_experiences`` /
    ``agent_observations`` / ``database`` / ``source_store`` / ``queries`` /
    ``ask_state`` / ``sharing_store`` / ``identity`` from domain 2 and
    ``foundation.settings`` / ``models`` / ``event_log`` from domain 1.
    """

    return _AgentJobsDomain(
        agent_profile_jobs=AgentProfileConsolidationService(
            settings=foundation.settings,
            profiles=seats.agent_profile,
            database=seats.database,
            sources=seats.source_store,
            queries=seats.queries,
            models=foundation.models,
            event_log=foundation.event_log,
            # T5:覆盖层链路唯一的取数座位。它读的是**该成员自己**的提问轨迹
            # (谓词写在 SQL 里,见 ``recent_user_ask_traces``),底座链路一个字
            # 都不许碰它——那条边界由隔离守卫按函数分组静态钉住。
            ask_state=seats.ask_state,
            # P2-T3:覆盖层的成员资格座位。用的是**读侧同一份谓词**
            # (`access_sql.NOTEBOOK_READ_SQL`),所以「这条链还该不该存在」与
            # 「这个人还读不读得到这个库」不会分叉。座位单开一个而不是往
            # ask_state 上加方法:隔离守卫的端口白名单是按链路分的,而按人判定
            # 的方法绝不该出现在底座那张里。
            access=seats.sharing_store,
            # Agentic Memory P3 (T4):覆盖层的第三个取数座位,唯一够得着
            # ``agent_observations``——外部 Agent 经 MCP `add_observation`(T3)
            # 写下的、关于它自己怎么用这个库的短句。座位单开一个而不是往
            # ask_state 上加方法,理由与 ask_state/access 同形:隔离守卫的端口
            # 白名单按链路分,这个座位必须永远只出现在覆盖层那张里。
            observations=seats.agent_observations,
        ),
        retrieval_experience_jobs=RetrievalExperienceDistillationService(
            settings=foundation.settings,
            experiences=seats.retrieval_experiences,
            # ⚠ 同一个 ask_state 座位,读的却是**没有任何用户/笔记本谓词**的那条
            # 方法(``recent_completed_ask_runs``)。它的安全性不来自谓词,而来自
            # 投影——见 ports.py 上那段说明与 retrieval_experience_projection.py。
            ask_state=seats.ask_state,
            models=foundation.models,
            event_log=foundation.event_log,
        ),
        search_profile_jobs=SearchProfileInferenceService(
            settings=foundation.settings,
            # ⚠ 读的是``recent_user_ask_languages``——同一个 ask_state 座位上
            # 第三条按``created_by``收窄的读,层三隔离守卫钉在
            # ``TRACE_READ_METHODS``里(与另外两条同一份 ``ask_state_store.py``,
            # 认同一个列名),但它**不属于** P1 的底座/覆盖层链路,登记只为覆盖
            # 层三判据。
            ask_state=seats.ask_state,
            # 唯一的写入座位:读-改-写 ``user_profiles.search_profile_json`` 必须
            # 走 ``IdentityRepository.set_user_search_profile``(T6),而不是另开
            # 一个座位——该方法本就在同一个写事务内做「job 不覆盖 user 字段」的
            # 合并,详见 ``search_profile.merge_field``。
            identity=seats.identity,
            event_log=foundation.event_log,
        ),
    )


class RepositoryRuntime:
    def __init__(
        self,
        settings: Settings,
        root_dir: Path,
        seams: RepositoryCompatibilitySeams,
        persistence_factory: PersistenceBundleFactory,
        *,
        model_provider: Any | None = None,
        retrieval_contributor_host: RetrievalContributorHostPort | None = None,
        parser_provider_chain_host: ParserProviderChainHostPort | None = None,
        ask_completed_observer_host: AskCompletedObserverHostPort | None = None,
        report_completed_observer_host: ReportCompletedObserverHostPort | None = None,
        ask_engine_host: AskEngineHostPort | None = None,
        indexing_pipeline_host: IndexingPipelineHostPort | None = None,
        gap_consult_host: GapConsultHostPort | None = None,
    ) -> None:
        """Call the domain builders in order, then mount their fields: the
        call order below IS the dependency topology."""
        foundation = _build_process_foundation(
            settings, root_dir, seams, model_provider,
            retrieval_contributor_host=retrieval_contributor_host,
            ask_completed_observer_host=ask_completed_observer_host,
            report_completed_observer_host=report_completed_observer_host,
            ask_engine_host=ask_engine_host,
            indexing_pipeline_host=indexing_pipeline_host,
            gap_consult_host=gap_consult_host,
            parser_provider_chain_host=parser_provider_chain_host,
        )
        self.settings = foundation.settings
        self.root_dir = foundation.root_dir
        self.seams = foundation.seams
        self.event_log = foundation.event_log
        self.retrieval_contributors = foundation.retrieval_contributors
        self.ask_completed_observers = foundation.ask_completed_observers
        self.report_completed_observers = foundation.report_completed_observers
        self.ask_engines = foundation.ask_engines
        self.indexing_pipelines = foundation.indexing_pipelines
        self.gap_consult = foundation.gap_consult
        self.parser_provider_chain = foundation.parser_provider_chain
        self.models = foundation.models
        # Runtime-private state: no domain owns it; the locks guard wire_*.
        self._closed = False
        self._embedder: Any = None
        self._notebook_languages: dict[str, list[str]] = {}
        self._retrieval_wire_lock = threading.Lock()
        self._ask_wire_lock = threading.Lock()
        self._ask_retrieval: "Callable[[], Any] | None" = None
        seats = _build_persistence_seats(persistence_factory, foundation)
        self.database = seats.database
        self.identity = seats.identity
        self.model_status_store = seats.model_status_store
        self.model_status = seats.model_status
        self.queries = seats.queries
        self.notebook_store = seats.notebook_store
        self.kg_build_jobs = seats.kg_build_jobs
        self.agent_profile = seats.agent_profile
        self.retrieval_experiences = seats.retrieval_experiences
        self.agent_observations = seats.agent_observations
        self.extension_toggles = seats.extension_toggles
        self.source_store = seats.source_store
        self.chunk_store = seats.chunk_store
        self.report_store = seats.report_store
        self.knowledge = seats.knowledge
        self.governance = seats.governance
        self.unified_kg = seats.unified_kg
        self.ask_state = seats.ask_state
        self.memory_store = seats.memory_store
        self.index_projections: IndexProjectionStorePort = seats.index_projections
        self.embedding_store: EmbeddingStorePort = seats.embedding_store
        self.sharing_store: SharingStorePort = seats.sharing_store
        self.groups: GroupStorePort = seats.groups
        self.knowhow_store = seats.knowhow_store
        self.knowhow_transfer_store = seats.knowhow_transfer_store
        self.knowhow_history_store = seats.knowhow_history_store
        notebook = _build_notebook_domain(foundation, seats)
        self.notebook_summaries = notebook.notebook_summaries
        self.source_files = notebook.source_files
        self.catalog = notebook.catalog
        self.notebook_copies = notebook.notebook_copies
        self.sharing = notebook.sharing
        pipeline = _build_source_pipeline(foundation, seats)
        self.chunk_question_index = pipeline.chunk_question_index
        self.source_embedding = pipeline.source_embedding
        self.source_chunking = pipeline.source_chunking
        self.indexing_pipeline = None
        self.source_ingestion = pipeline.source_ingestion
        report = _build_report_domain(seats, notebook)
        self.report_application = report.report_application
        self.report_cancellations = report.report_cancellations
        self.report_execution = report.report_execution
        knowledge = _build_knowledge_domain(foundation, seats, self._current_user_id)
        self.kg_analysis = knowledge.kg_analysis
        self.schema_registry = knowledge.schema_registry
        self.kg_mutations = knowledge.kg_mutations
        self.review_queue_memo = knowledge.review_queue_memo
        self.knowledge_governance = knowledge.knowledge_governance
        self.knowledge_lifecycle = knowledge.knowledge_lifecycle
        self.knowledge_query = knowledge.knowledge_query
        self.pending_actions_service = knowledge.pending_actions_service
        retrieval = _build_retrieval_domain(foundation)
        self.retrieval_snapshots = retrieval.retrieval_snapshots
        self.memory_service = retrieval.memory_service
        self.memory_retriever = retrieval.memory_retriever
        self.evidence_context = retrieval.evidence_context
        self.candidate_retrieval = retrieval.candidate_retrieval
        self.graph_retrieval = retrieval.graph_retrieval
        self.retrieval = retrieval.retrieval
        graph = _build_source_graph_domain(foundation, seats)
        self.scale_artifact_store = graph.scale_artifact_store
        self.source_subgraphs = graph.source_subgraphs
        self.source_graph_primitives = graph.source_graph_primitives
        self.source_graph_enrichment = graph.source_graph_enrichment
        self.source_subgraph_ppr = graph.source_subgraph_ppr
        self.source_partitioned_ppr = graph.source_partitioned_ppr
        self.selected_source_graph = graph.selected_source_graph
        self.scale_catalog = graph.scale_catalog
        self.scale_builder = graph.scale_builder
        self.scale_artifacts = graph.scale_artifacts
        ask = _build_ask_domain(foundation, seats, self.ask_service, self._note_ask_completed)
        self.ask_cancellations = ask.ask_cancellations
        self.ask = ask.ask
        self.ask_execution = ask.ask_execution
        tools = _build_content_tools(foundation, seats, self._current_user_id)
        self.command_catalog = tools.command_catalog
        self.collection_catalog = tools.collection_catalog
        self.collection_enumeration = tools.collection_enumeration
        agents = _build_agent_jobs(foundation, seats)
        self.agent_profile_jobs = agents.agent_profile_jobs
        self.retrieval_experience_jobs = agents.retrieval_experience_jobs
        self.search_profile_jobs = agents.search_profile_jobs
        # 体检 H4/H5 事件失效插槽(facade 构造期挂转发器;详见 _notify_source_vectors_written)。
        self.on_source_vectors_written: "Callable[[str], None] | None" = None
        # P2·T2 体检聚合(CheckupService)刻意**不**在这里构造:它依赖 maintenance 的 COUNT +
        # sqlite QueryStore,而 repository_runtime 是**后端中性**模块(neutrality 守卫禁止它 import
        # 任何 app.repositories.sqlite/postgres)。故 checkup 由**后端相关的** SQLiteRepository facade
        # 懒构造(见 sqlite_repository.py 的 ``checkup`` 属性),复用 facade 的 ``maintenance`` adapter。
        # 本 runtime 只提供两个窄 seam 给它:``_active_source_ids_snapshot``,加上上面的插槽。
        # SQLite maintenance face(CLI/batch 组合根)同理不在 runtime 里——它由 facade 的
        # ``maintenance`` property 懒接线,因为它需要 embedder-bound retrieval provider,与
        # 上面的 ``ask`` 同一个理由。

    def _current_user_id(self) -> str:
        """The request's current user id, resolved at CALL time.

        Handed as a bound method to the two domains that need it (schema
        registry, command catalog) so neither builder has to close over the
        runtime: a zero-argument read-only accessor is a narrow, one-way edge
        where passing ``self`` would be a cycle back into the composition root.
        Mirrors what both persistence bundles already do with ``identity``."""
        return self.identity.current_user().id

    # codex #535 R4 P2(驳回,登记决定):本通知只由流式 AskExecutionCoordinator
    # 触发,同步 `POST /notebooks/{id}/ask` 与 MCP `ask_notebook`(经
    # ask_current)刻意**不**接入——这是 P1 就登记的口径（`docs/product-and-api.md`「Agent 库
    # 理解」条:「同步 POST /ask 不计入覆盖层触发计数」),P2 经验库与 P3 语言
    # 归纳共用同一个钩子、同一条边界。接同步路径要么在 ask_current 里再挂一次
    # (流式路径包着它,会双计),要么把钩子下沉进 ask_current 并让流式去重——
    # 两者都在改三条链共同的计数语义,不是 P3 单方面能拍的;如放开,单独一件
    # 事过评审。反向护栏:test_search_profile_job.py 的
    # test_sync_ask_paths_deliberately_do_not_notify_inference。
    def _note_ask_completed(
        self, notebook_id: str, user_id: str, mode_id: str = "reasoning"
    ) -> None:
        """一次提问完成之后要推进的**三条**后台链路。

        它们是三个不同的特性,拿到的数据也刻意不同:P1 的巡固被告知是**哪位成员
        在哪个库**(它写的正是那个人在那个库的覆盖层块),P2 的经验库蒸馏则连一个
        参数都不收——那张表是部署级全局的,一个知道「这是谁的提问」的触发器,离
        「记下这是谁的提问」只有一次重构之远。P3(T7)的检索偏好归纳只收**这个人
        是谁**,不收 notebook_id——一个人的语言不因笔记本而变,归纳按人、跨库累计
        (见 ``search_profile_job`` 模块 docstring)。

        三次调用**各自**用 try 包住,不是写成一个元组表达式:三边虽然都自己
        fail-open,但那是它们各自的实现细节,而这里要保证的是「一条链坏掉不会顺带
        吃掉另一条的计数」——这条性质必须由调用点自己成立,不能靠被调方的内部约定。

        整个方法同样 fail-open:它挂在一个**答案已经交付之后**的钩子上。
        ``KeyboardInterrupt``/``SystemExit`` 不是「错误」,继续上抛。
        """
        host = self.ask_completed_observers
        if host is None:
            self._note_ask_completed_compat(notebook_id, user_id, mode_id)
            return
        context = AskCompletedObserverCallContext(
            notification=CompletedAskNotification(
                actor_id=user_id,
                notebook_id=notebook_id,
                mode_id=mode_id,
            ),
            agent_profile=_AskCompletedAccess(
                lambda: self.agent_profile_jobs.note_ask_completed(
                    notebook_id, user_id
                )
            ),
            retrieval_experience=(
                _AskCompletedAccess(
                    self.retrieval_experience_jobs.note_ask_completed
                )
                if mode_id == "reasoning"
                else None
            ),
            search_profile=_AskCompletedAccess(
                lambda: self.search_profile_jobs.note_ask_completed(user_id)
            ),
            connection_probe=self.database,
            deadline_monotonic=(
                time.monotonic()
                + self.settings.ask_post_completion_extension_timeout_seconds
            ),
        )
        host.observe_application(context, event_sink=self.event_log.emit)

    def _note_ask_completed_compat(
        self, notebook_id: str, user_id: str, mode_id: str
    ) -> None:
        """Direct-constructor compatibility until every non-app root injects a host."""
        try:
            self.agent_profile_jobs.note_ask_completed(notebook_id, user_id)
        except Exception:  # noqa: BLE001 — 已交付的答案不因后台记账而改判
            _log.exception("agent profile ask-completed notification failed")
        try:
            if mode_id == "reasoning":
                self.retrieval_experience_jobs.note_ask_completed()
        except Exception:  # noqa: BLE001 — 同上
            _log.exception("retrieval experience ask-completed notification failed")
        try:
            self.search_profile_jobs.note_ask_completed(user_id)
        except Exception:  # noqa: BLE001 — 同上
            _log.exception("search profile ask-completed notification failed")

    def _active_source_ids_snapshot(self) -> "set[str]":
        """内存活跃源快照(H2/H3/H4/H5 的 Python 后置减法用)= 活跃租约(process_source 在途)
        **并上**后台嵌入进行中的源(codex 第5轮 P2:process_source 可能先返回、撤自己的租约,而
        嵌入 worker 还在写向量——不并入 H4/H5 会把在途向量误报缺失+诱发重复 backfill)。**必须
        在锁下取**:并发 stamp/pop 改 dict 大小,不加锁的 ``set(...)`` 迭代会触发 ``dict changed
        size during iteration``。source_ingestion 未 wire 时(纯读最小 runtime)天然返回空集。两个
        私有 dict 的读取收拢在这个组合根方法里,CheckupService 只见到窄接口 ``Callable[[], set]``。"""
        ingestion = self.source_ingestion
        if ingestion is None:
            return set()
        with ingestion._active_sources_lock:
            return set(ingestion._active_sources) | set(ingestion._embedding_sources)

    def _notify_source_vectors_written(self, notebook_id: str) -> None:
        """element/chunk 向量落库后的事件转发:SourceEmbeddingService → 这里 → checkup 的
        H4/H5 memo 失效。晚绑定读 ``on_source_vectors_written`` 插槽(__init__ 置 None,
        facade 构造期挂上它的 __dict__ 晚解析转发器):插槽为 None 时天然 no-op——纯读
        runtime(未挂 facade)本就没有 checkup 缓存可失效。单消费者、单槽,列表化留给
        真出现第二个消费者那天。局部变量快照防「读判空与调用之间被换掉」的竞态窄缝。"""
        callback = self.on_source_vectors_written
        if callback is not None:
            callback(notebook_id)

    @property
    def storage_dir(self) -> Path:
        return self.source_files.storage_dir

    @storage_dir.setter
    def storage_dir(self, value: Path) -> None:
        self.set_storage_dir(value)

    def set_storage_dir(self, value: Path) -> None:
        self.source_files.storage_dir = value if isinstance(value, Path) else Path(value)

    @property
    def embedder(self) -> Any:
        # 只读:查询侧 embedder 的替换必须显式走 set_embedder()(它会连带重连
        # retrieval / memory_retriever)。刻意不提供 setter——`runtime.embedder = x`
        # 会绕过那次重连,留下不一致的组合。见 test_runtime_query_embedder_is_read_only。
        return self._embedder

    def set_embedder(self, value: Any) -> None:
        self._embedder = value
        if self.retrieval is not None:
            self.retrieval.replace_embedder(value)
        if self.memory_retriever is not None:
            self.memory_retriever.replace_embedder(value)

    def wire_memory(
        self, *, persistence_embedder: Any, query_embedder: Any
    ) -> MemoryService:
        """Compose owner-private Memory after sharing/access and embedding exist."""
        if self.sharing is None:
            raise RuntimeError("wire_memory requires wire_sharing first")
        self.memory_service = MemoryService(
            self.memory_store,
            self.ask_state,
            self.sharing,
            persistence_embedder,
            self.event_log,
            self.seams.new_id,
            self.seams.now,
            embedding_scheduler=lambda fn, item: kg_scheduler.submit_job(fn, item),
            kg_ingest_scheduler=lambda fn, item: kg_scheduler.submit_job(fn, item),
        )
        self.memory_retriever = MemoryRetriever(self.memory_store, query_embedder)
        self.catalog.memory_retriever = self.memory_retriever
        return self.memory_service

    @property
    def notebook_languages(self) -> dict[str, list[str]]:
        return self._notebook_languages

    @notebook_languages.setter
    def notebook_languages(self, value: dict[str, list[str]]) -> None:
        self.set_notebook_languages(value)

    def set_notebook_languages(self, value: dict[str, list[str]]) -> None:
        self._notebook_languages = value
        if self.kg_mutations is not None:
            self.kg_mutations.notebook_languages = value
        if self.retrieval is not None:
            self.retrieval.replace_notebook_languages(value)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.models.close()
        self.database.close()

    def set_unified_cache(self, value: dict) -> None:
        self.retrieval_snapshots.unified_cache = value
        if self.knowledge_lifecycle is not None:
            self.knowledge_lifecycle.unified_cache = value

    def set_auto_index_checked(self, value: set) -> None:
        if self.scale_artifacts is None:
            raise RuntimeError("scale runtime is not wired")
        self.scale_artifacts.auto_index_checked = value
        if self.kg_mutations is not None:
            self.kg_mutations.auto_index_checked = value

    @property
    def retrieval_component(self) -> RetrievalService:
        return self.wire_retrieval(embedder=self.embedder)

    @property
    def evidence_context_component(self) -> EvidenceContextService:
        # Resolving retrieval is the one lazy composition trigger that also
        # constructs the evidence-context service.
        _ = self.retrieval_component
        if self.evidence_context is None:  # pragma: no cover - defensive
            raise RuntimeError("retrieval did not compose evidence context")
        return self.evidence_context

    def wire_retrieval(self, *, embedder) -> RetrievalService:
        if self.retrieval is not None:
            return self.retrieval
        with self._retrieval_wire_lock:
            if self.retrieval is not None:
                return self.retrieval
            if self.embedding_store is None or self.scale_artifacts is None:
                raise RuntimeError("wire_retrieval requires persistence and scale runtime")
            self.set_embedder(embedder)
            common = dict(
                notebooks=self.notebook_store,
                sources=self.source_store,
                chunks=self.chunk_store,
                embeddings=self.embedding_store,
                knowledge=self.knowledge,
                governance=self.governance,
                unified_kg=self.unified_kg,
                queries=self.queries,
                snapshots=self.retrieval_snapshots,
                scale_runtime=self.scale_artifacts,
                model_clients=self.models,
                model_error_sink=self.models,
                settings=self.settings,
                event_log=self.event_log,
                database=self.database,
                embedder=self.embedder,
                notebook_languages=self.notebook_languages,
            )
            from app.services.communities import CommunityQueryService

            candidates = CandidateRetrievalService(
                **common,
                memory_retriever=self.memory_retriever,
                retrieval_contributors=self.retrieval_contributors,
            )
            graph = GraphRetrievalService(**common)
            retrieval = RetrievalService(
                candidates=candidates,
                graph=graph,
                community_queries=lambda settings=None: CommunityQueryService(
                    notebooks=self.notebook_store,
                    unified_kg=self.unified_kg,
                    event_log=self.event_log,
                    sibling_min_bridge=(
                        settings or self.settings
                    ).sibling_min_bridge,
                ),
            )
            candidates.bind(peer=graph, retrieval=retrieval)
            graph.bind(peer=candidates, retrieval=retrieval)
            evidence = EvidenceContextService(
                notebooks=self.notebook_store,
                sources=self.source_store,
                knowledge=graph,
                settings=self.settings,
            )
            self.candidate_retrieval = candidates
            self.graph_retrieval = graph
            self.evidence_context = evidence
            self.retrieval = retrieval
            return retrieval

    def wire_persistence(self, *, write: Callable[..., Any]) -> EmbeddingStorePort:
        """Compose the vector persistence (Task 10) once the facade-bound
        ``write`` seat exists: it is the facade's ``_write`` compatibility
        seam (itself delegating to the shared database write lock), resolved
        at call time so per-instance monkeypatches — transaction counting,
        failure injection — keep observing every vector flush."""
        self.embedding_store.bind_write(write)
        return self.embedding_store

    def wire_source_pipeline(
        self,
        *,
        embedder: Callable[[str], Any],
        mark_unified_dirty: Callable[[str], None],
    ) -> tuple[SourceEmbeddingService, SourceChunkingService]:
        """Compose the source embed/chunk pipeline (Task 11) once the
        facade-bound seams exist: ``embedder`` resolves the facade's mutable
        ``self.embedder`` at call time (tests swap in fakes post-construction),
        while ``mark_unified_dirty`` is the facade's
        ``_mark_unified_kg_dirty`` KG seat. Requires wire_persistence() first —
        vector flushes land on the already-wired EmbeddingStore."""
        if self.embedding_store is None:
            raise RuntimeError("wire_source_pipeline requires wire_persistence() first")
        self.source_embedding = SourceEmbeddingService(
            settings=self.settings,
            sources=self.source_store,
            chunks=self.chunk_store,
            vectors=self.embedding_store,
            embedder=embedder,
            parallelism=self.models.parallelism,
            event_log=self.event_log,
            now=self.seams.now,
            # Drop retrieval's brute-force _vector_matrix cache for a table after
            # a paged (re-)embed — the version key (COUNT(*), MAX(created_at))
            # can't see a same-second re-embed of existing rows (see the batch
            # embedders). Same VectorCache the retrieval snapshots read.
            invalidate_vector_matrix=(
                lambda notebook_id, table: self.retrieval_snapshots.invalidate(
                    f"{notebook_id}:matrix:{table}"
                )
            ),
            # 体检 H4/H5 计数 memo 的事件失效——经 runtime 的晚绑定插槽转发(checkup 由
            # facade 懒构造,构造时才挂上;见 __init__ 的插槽说明)。
            on_source_vectors_written=self._notify_source_vectors_written,
        )
        self.source_chunking = SourceChunkingService(
            settings=self.settings,
            sources=self.source_store,
            chunks=self.chunk_store,
            embedding=self.source_embedding,
            new_id=self.seams.new_id,
            now=self.seams.now,
            mark_unified_dirty=mark_unified_dirty,
            notebooks=self.notebook_store,
            indexing_stage_store=self.kg_build_jobs,
            indexing_pipelines=self.indexing_pipelines,
            event_log=self.event_log,
        )
        self.indexing_pipeline = IndexingPipelineService(
            self.notebook_store,
            self.source_chunking,
            self.indexing_pipelines,
        )
        return self.source_embedding, self.source_chunking

    def wire_source_ingestion(
        self,
        *,
        write: Callable[[], Any],
        source_elements: Callable[[str], list],
        summarize_source: Callable[..., str],
        source_type_from_name: Callable[[str], str],
        mineru_client: Callable[[], Any],
        mineru_cloud_client: Callable[[], Any],
        normalize_doc_type: Callable[[str], str],
        default_notebook_names: Any,
        clear_source_extraction_state: Callable[..., None],
        begin_extraction_run: Callable[..., None],
        finish_extraction_run: Callable[..., None],
        notebook_tier: Callable[[str], str],
        concept_whitelist_terms: Callable[[], set],
        notebook_has_kg: Callable[[str], bool],
        maybe_auto_index: Callable[[str], None],
        notebook_meta_row: Callable[[str], Any],
        notebook_meta_sources: Callable[..., list],
        apply_notebook_meta: Callable[..., None],
        make_persist_image: Callable[..., Any],
        delete_source_images: Callable[..., Any],
    ) -> SourceIngestionService:
        """Compose the source ingestion orchestration (Task 12) once the
        facade-bound seams exist.  ``write`` is the facade's ``_write``
        compatibility seat resolved per call (transaction counting / failure
        injection keep observing every ingestion commit boundary);
        ``source_elements``/``summarize_source`` stay
        facade late-bound so frozen patch targets (repo.source_elements and
        repo._summarize_source) keep working; model
        calls use explicit workloads on the process-owned provider;
        ``make_persist_image``/``delete_source_images`` are the per-source
        image-persistence factory and the per-source image cascade-delete
        seam (embedded-image retention); the remaining callables are
        TEMPORARY facade-owned KG/catalog callbacks —
        Task 16+ move them with their domains.  The Gate-4 KG hooks are gone
        (Task 15): extraction persists through the runtime-owned
        KnowledgeLifecycleService and delete-side cache eviction goes through
        the KgMutationCoordinator directly, so this requires BOTH
        wire_source_pipeline() and wire_knowledge_lifecycle() first."""
        if self.source_embedding is None or self.source_chunking is None:
            raise RuntimeError(
                "wire_source_ingestion requires wire_source_pipeline() first"
            )
        if self.knowledge_lifecycle is None or self.kg_mutations is None:
            raise RuntimeError(
                "wire_source_ingestion requires wire_knowledge_lifecycle() first"
            )
        self.source_ingestion = SourceIngestionService(
            settings=self.settings,
            notebooks=self.notebook_store,
            sources=self.source_store,
            source_files=self.source_files,
            chunking=self.source_chunking,
            embedding=self.source_embedding,
            event_log=self.event_log,
            new_id=self.seams.new_id,
            now=self.seams.now,
            write=write,
            source_elements=source_elements,
            summarize_source=summarize_source,
            source_type_from_name=source_type_from_name,
            parser_provider_chain=self.parser_provider_chain,
            parser_connection_probe=self.database,
            mineru_client=mineru_client,
            mineru_cloud_client=mineru_cloud_client,
            model_clients=self.models,
            normalize_doc_type=normalize_doc_type,
            default_notebook_names=default_notebook_names,
            clear_source_extraction_state=clear_source_extraction_state,
            begin_extraction_run=begin_extraction_run,
            finish_extraction_run=finish_extraction_run,
            notebook_tier=notebook_tier,
            concept_whitelist_terms=concept_whitelist_terms,
            notebook_has_kg=notebook_has_kg,
            knowledge_lifecycle=self.knowledge_lifecycle,
            kg_mutations=self.kg_mutations,
            maybe_auto_index=maybe_auto_index,
            notebook_meta_row=notebook_meta_row,
            notebook_meta_sources=notebook_meta_sources,
            apply_notebook_meta=apply_notebook_meta,
            maybe_enqueue_scale_fold=self.scale_artifacts.maybe_enqueue_fold,
            note_corpus_change=self.agent_profile_jobs.note_corpus_change,
            require_indexing_write=self.indexing_pipeline.require_write_admission,
            indexing_pipelines=self.indexing_pipelines,
            effective_object_types=lambda notebook_id: (
                self.schema_registry.effective_schemas(notebook_id).keys()
            ),
            make_persist_image=make_persist_image,
            delete_source_images=delete_source_images,
            invalidate_knowledge_counts=self.queries.invalidate_knowledge_counts,
            # copy-stats memo 是 runtime-owned 的(codex PR#634 R2 P2-2),所以
            # 摄取路径的失效走注入回调,与上面那条同一形态。
            invalidate_copy_stats=self.retrieval_snapshots.copy_stats_memo.invalidate,
        )
        # Memory-KG bridge (memory-kg-extract Task 3): MemoryService is wired
        # earlier (wire_memory, before wire_knowledge_lifecycle), but
        # source_ingestion only exists from this point on — this is the first
        # seam where both components are ready, mirroring the defensive
        # set_promotion_service call in wire_knowledge_lifecycle.
        if self.memory_service is not None:
            self.memory_service.set_memory_kg_service(self.source_ingestion)
        # paper-meta backfill status (Task 2): get_notebook reflects the
        # in-process _paper_meta_backfilling dict into NotebookSummary the
        # same way it reflects kg_building — catalog needs a live reference,
        # set here (mirrors the catalog.memory_retriever wiring in wire_memory
        # above; self.catalog is constructed eagerly, before source_ingestion
        # exists, so this can't be a constructor arg). WEAKREF, not a strong
        # ref: SourceIngestionService closes over the facade for its own
        # wiring, so a strong ref here would let anything that transitively
        # holds `catalog` (e.g. ScaleArtifactRuntime) keep the whole facade
        # alive — see test_scale_artifact_runtime's retention tests.
        self.catalog.source_ingestion = weakref.ref(self.source_ingestion)
        # 方案 C·C1b R10:命令目录 apply/dismiss 的「解析栅栏」。它要持有的正是
        # SourceIngestionService 那把 per-source 分块锁——`replace_elements` 唯一
        # 的持锁方——否则代次校验与 knowhow 落库之间存在 TOCTOU 窗口(见
        # CommandCatalogService._source_write_barrier)。同上面 catalog 一样是**延迟
        # seam**(command_catalog 在本方法之前就 eager 构造好了)且同样用 **weakref**:
        # SourceIngestionService 闭包持有 facade,强引用会让任何长期持有
        # command_catalog 的对象把整个 facade 钉住(见 scale/catalog 的滞留测试)。
        self.command_catalog.source_locks = weakref.ref(self.source_ingestion)
        # paper-meta backfill in the pending-actions bell (Task 5): same
        # deferred-seam problem as catalog above (pending_actions_service is
        # constructed in wire_query_services(), before this method runs), but
        # a PLAIN STRONG ref here, not weakref — pending_actions_service has
        # no back-edge equivalent to ScaleArtifactRuntime.notebooks=catalog:
        # nothing holds a PendingActionsService instance beyond its owning
        # facade (it's only ever reached via repository()._runtime, and
        # pending_bus's recompute closure re-resolves repository() fresh on
        # every call instead of retaining one), so there is no path for a
        # long-lived external holder to transitively pin the facade alive
        # through it the way test_retained_scale_runtime_does_not_transitively_retain_repository
        # guards against for scale_artifacts/catalog.
        if self.pending_actions_service is not None:
            self.pending_actions_service.source_ingestion = self.source_ingestion
        return self.source_ingestion

    def wire_kg_mutations(
        self,
        *,
        auto_index_checked: Any,
        notebook_languages: Any,
        write: Callable[[], Any],
    ) -> KgMutationCoordinator:
        """Compose the KG mutation coordinator (Task 14) once the facade-bound
        collaborators exist.  The unified/vector caches come from the
        runtime-owned ``retrieval_snapshots`` (Task 17 — the coordinator reads
        them through it, so identities track a facade-level cache swap); the
        once-set/memo are the facade's EXISTING objects passed BY IDENTITY
        (never replacement copies); ``write`` is the facade's ``_write``
        compatibility seat resolved per call, so the frozen transaction-phase
        traces and failure injections keep observing the dirty bump's commit
        boundary.  The unified store and the `_now` clock seam come from this
        runtime."""
        self.set_notebook_languages(notebook_languages)
        self.kg_mutations = KgMutationCoordinator(
            self.unified_kg,
            self.retrieval_snapshots,
            auto_index_checked,
            self.notebook_languages,
            write=write,
            now=self.seams.now,
        )
        return self.kg_mutations

    def wire_scale_artifacts(
        self,
        *,
        connect: Callable[[], Any],
        in_batches: Callable[..., Any],
        ent_chunk_map: Callable[[str], dict],
        mention_extra_edges: Callable[[str], list],
        vector_matrix: Callable[..., Any],
        version: Callable[[str], list],
        scale_cache: Callable[[], Any],
        load_lock: Callable[[], Any],
        load_locks: Callable[[], dict],
        note_model_error: Callable[..., None],
    ) -> ScaleArtifactCatalog:
        """Compose the scale/viz artifact READ adapters (Task 18) once the
        facade-bound seams exist.  ``connect`` resolves the facade's
        ``_connect`` compatibility seat per call (the frozen version-probe
        spies / gated-slow-connection single-flight tests keep observing
        every projection query); ``in_batches`` resolves the facade helper so
        the frozen ``_IN_CHUNK`` class patch keeps flowing; the ent-chunk /
        mention-edge / vector-matrix providers are retrieval-owned caches
        that stay facade-late until their domain moves (Gate 7).  The catalog
        applies the exact/allow_stale semantics and the lazy ANN open over
        the eager ScaleArtifactStore; it holds NO builder — reading can never
        schedule a rebuild.  ``version`` resolves the facade's memoized
        ``_scale_index_version`` and ``scale_cache``/``load_lock``/
        ``load_locks`` initially resolve the exact LRU + single-flight state
        objects that ``wire_scale_runtime`` transfers by identity.  The final
        runtime then retargets the catalog to its canonical state."""
        self.index_projections.bind_runtime_callbacks(
            connect=connect,
            in_batches=in_batches,
            ent_chunk_map=ent_chunk_map,
            mention_extra_edges=mention_extra_edges,
            vector_matrix=vector_matrix,
        )
        self.scale_catalog = ScaleArtifactCatalog(
            artifacts=self.scale_artifact_store,
            settings=self.settings,
            version=version,
            scale_cache=scale_cache,
            load_lock=load_lock,
            load_locks=load_locks,
            note_model_error=note_model_error,
            pipeline_identity=self.index_projections.pipeline_identity,
        )
        return self.scale_catalog

    def wire_scale_builder(
        self,
        *,
        get_notebook: Callable[[str], Any],
        version: Callable[[str], list],
        load_scale: Callable[[str], Any],
        full_viz_graph: Callable[[str], dict],
        relations_for_notebook: Callable[[str], list],
        cluster_map: Callable[[str], dict],
        incremental_fuse_source: Callable[[str, str], None],
        invalidate_scale_cache: Callable[[str], None],
        cache_viz: Callable[[str, Any], None],
        building: set,
        building_lock: Any,
        notify_index_done: Callable[[str], None],
    ) -> ScaleIndexBuilder:
        """Compose Task 19's builder over the exact Task 18 runtime objects.

        Facade compatibility seams stay late-bound callables. The builder owns
        orchestration only and holds no facade reference; ``wire_scale_runtime``
        transfers the supplied cache/build-state objects by identity.
        """
        if self.index_projections is None or self.scale_catalog is None:
            raise RuntimeError(
                "wire_scale_builder requires wire_scale_artifacts() first"
            )
        # Capture the plain snapshot cache (NOT self) so the builder's
        # unified-cache eviction closure holds no path back to the repository —
        # test_retained_scale_runtime_does_not_transitively_retain_repository.
        snapshots = self.retrieval_snapshots
        self.scale_builder = ScaleIndexBuilder(
            settings=self.settings,
            projections=self.index_projections,
            artifacts=self.scale_artifact_store,
            event_log=self.event_log,
            get_notebook=get_notebook,
            version=version,
            load_scale=load_scale,
            full_viz_graph=full_viz_graph,
            relations_for_notebook=relations_for_notebook,
            cluster_map=cluster_map,
            incremental_fuse_source=incremental_fuse_source,
            invalidate_scale_cache=invalidate_scale_cache,
            invalidate_source_partition_cache=(
                self.source_partitioned_ppr.invalidate
            ),
            cache_viz=cache_viz,
            building=building,
            building_lock=building_lock,
            notify_index_done=notify_index_done,
            now=self.seams.now,
            # Release full_viz_graph('object')'s cached whole-graph dict after
            # viz_arrays so it doesn't ride resident through persist. Targeted
            # (unified_cache only), not the invalidate_kg family sweep.
            invalidate_unified_cache=(
                lambda nb: snapshots.invalidate_unified(nb)
            ),
        )
        return self.scale_builder

    def wire_scale_runtime(
        self,
        *,
        scale_cache: Any,
        viz_cache: Any,
        version_memo: dict,
        version_lock: Any,
        version_locks: dict,
        load_lock: Any,
        load_locks: dict,
        building: set,
        building_lock: Any,
        idle_queue: dict,
        scheduler_started: bool,
        auto_index_checked: set,
        viz_building: set,
        viz_building_lock: Any,
    ) -> ScaleArtifactRuntime:
        """Compose Task 20 over the exact Task 18/19 objects and state.

        All mutable state arguments are transferred by identity.  The runtime
        retargets the catalog and builder to these canonical objects; neither
        is recreated and no repository facade reference is retained.
        """
        if (
            self.index_projections is None
            or self.scale_catalog is None
            or self.scale_builder is None
        ):
            raise RuntimeError(
                "wire_scale_runtime requires scale artifacts and builder first"
            )
        self.scale_artifacts = ScaleArtifactRuntime(
            settings=self.settings,
            event_log=self.event_log,
            projections=self.index_projections,
            artifacts=self.scale_artifact_store,
            catalog=self.scale_catalog,
            builder=self.scale_builder,
            scale_cache=scale_cache,
            viz_cache=viz_cache,
            version_memo=version_memo,
            version_lock=version_lock,
            version_locks=version_locks,
            load_lock=load_lock,
            load_locks=load_locks,
            building=building,
            building_lock=building_lock,
            idle_queue=idle_queue,
            scheduler_started=scheduler_started,
            auto_index_checked=auto_index_checked,
            viz_building=viz_building,
            viz_building_lock=viz_building_lock,
            notebooks=self.catalog,
            facts_repo=self.queries,
            copy_stats_memo=self.retrieval_snapshots.copy_stats_memo,
            require_indexing_write=self.indexing_pipeline.require_write_admission,
            # W-CLI: per-notebook build claim across processes. Taken from the
            # database this coordinator already owns rather than added to the
            # facade surface — the adapter decides whether the backend can
            # provide one, and the service never inspects the URL scheme.
            scale_build_lock=self.database.try_scale_build_lock,
        )
        return self.scale_artifacts

    def wire_query_services(self, *, retrieval: Callable[[], Any]) -> None:
        if self.scale_artifacts is None:
            raise RuntimeError("query services require scale runtime")
        self.knowledge_query = KnowledgeQueryService(
            settings=self.settings,
            model_provider=self.models,
            event_log=self.event_log,
            database=self.database,
            catalog=self.catalog,
            knowledge=self.knowledge,
            chunk_store=self.chunk_store,
            unified_kg=self.unified_kg,
            scale_runtime=self.scale_artifacts,
            retrieval=retrieval,
            schemas=self.schema_registry,
            snapshots=self.retrieval_snapshots,
            notebook_languages=lambda: self.notebook_languages,
            participant_notebook_ids=self.notebook_store.participant_notebook_ids,
            node_context_reader=lambda notebook_id, object_id: self.knowledge.node_context(
                notebook_id, object_id, check_access=False
            ),
            memory_retriever=self.memory_retriever,
            current_user_id=self._current_user_id,
            queries=self.queries,
        )
        self.pending_actions_service = PendingActionsService(
            self.queries,
            scale_runtime=self.scale_artifacts,
            # source_ingestion doesn't exist yet at this point in the wiring
            # sequence (wire_source_ingestion runs after this) — backfilled
            # below once it's built, same seam as catalog.source_ingestion.
            source_ingestion=self.source_ingestion,
        )

    def wire_knowledge_lifecycle(
        self,
        *,
        connect: Callable[[], Any],
        write: Callable[[], Any],
        get_notebook: Callable[[str], Any], current_user_id: Callable[[], str],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_kg_dirty: Callable[[str], None],
        mark_unified_kg_dirty_in_tx: Callable[[Any, str], int],
        bump_cluster_mutation_seq: Callable[..., None],
        embed_objects_batch: Callable[..., None],
        embed_relations_batch: Callable[..., None],
        source_ids_from_evidence: Callable[..., set],
        set_source_status: Callable[..., None],
        run_extraction: Callable[..., None],
        reconcile_extracted_terminal: Callable[..., None],
        wait_ingestion_drain: Callable[[str, float], bool],
        cluster_map: Callable[[str], dict],
        annotate_edge_support: Callable[..., list],
        decided_seed_pairs: Callable[[str], dict],
        relations_for_notebook: Callable[[str], list],
        notebook_copy_stats: Callable[[str], dict],
        note_model_error: Callable[..., None],
        edge_centrality_map: Callable[[str], dict],
        embed_knowledge: Callable[..., None],
        knowledge_objects: Callable[..., list],
        as_retrieved: Callable[..., Any],
        rule_card: Callable[..., Any],
        set_conflict_status: Callable[..., None],
    ) -> KnowledgeLifecycleService:
        """Compose the knowledge governance + lifecycle services (Task 15/16)
        once the facade-bound collaborators exist.  ``connect``/``write`` are
        the facade's ``_connect``/``_write`` compatibility seats resolved per
        call (frozen transaction traces / failure injections keep observing
        every lifecycle commit boundary); ``invalidate_unified_cache`` /
        ``mark_unified_kg_dirty`` / ``mark_unified_kg_dirty_in_tx`` /
        ``bump_cluster_mutation_seq`` are the facade's Task-14 wrapper seats
        (the coordinator stays the single dirty entry and repo-level patch
        seats stay effective — ``mark_unified_kg_dirty_in_tx`` is the
        in-transaction twin ``relink_notebook_kg`` rides so the dirty bump
        commits atomically with the per-source edge insert instead of a
        `finally` a kill -9 could skip, and — R2 P2 fix, codex #638 R2 — the
        same twin ``KnowledgeGovernanceService.set_edge_review`` rides so its
        seq bump and same-tx seq readback commit atomically with its own
        review_status UPDATE); ``llm``/``kg_llm``
        resolve the facade's frozen model-client properties per call (class-
        property monkeypatches keep working); the scale/viz callables are
        TEMPORARY Gate-6 adapters (scale-index load / ANN open / viz index /
        probe / build-viz / auto-index / copy-stats), all facade-late; the
        unified-graph memo comes from the runtime-owned retrieval_snapshots
        (Task 17) while ``viz_building`` stays the facade's EXISTING set
        passed BY IDENTITY; ``kg_building`` set identity comes from the
        catalog (get_notebook reads membership there) while the lifecycle
        owns the guard lock.  The
        governance service is constructed FIRST so the lifecycle's
        full-notebook build calls it as a real service, not a facade callback.
        Task 16 gives it the full governance surface: the Task-13 stores, the
        retrieval-owned ports (edge-centrality cache / payload embed /
        RetrievedKnowledge+RuleCard formatting / knowledge-objects reader,
        facade-late until their domain moves) and ONE surviving compound port
        — ``set_conflict_status`` resolves the FACADE wrapper per call because
        the frozen confirm_conflict phase contract patches that method."""

        def read_kg_mutation_seq(notebook_id: str) -> int:
            # R3 T-A3 P1-2 / v4: point-in-time ``kg_mutation_seq`` read used by
            # the review-queue memo's own read-order contract (``review_queue``
            # / ``review_queue_page``'s ``read_seq`` callback, invoked BEFORE
            # a cold ``compute()`` — see review_queue_memo's module
            # docstring). A NEW ``connect()`` is correct here: these are
            # ordinary reads with no write to pair the seq with, through the
            # existing ``unified_kg.graph_seq_row`` read-only channel other
            # services (checkup/collection_catalog/graph_retrieval) already
            # use for the same triple.
            #
            # ``set_edge_review`` no longer uses this callable for its OWN
            # bump (R2 P2 fix, codex #638 R2): reading the just-bumped seq
            # through a brand new post-commit connection let a concurrent
            # writer's own bump land in between, decoupling "the seq this
            # call's carry uses" from "the write this call actually made" —
            # see review_queue_memo's module docstring for the two races that
            # opened. It now gets its seq from ``mark_unified_kg_dirty_in_tx``'s
            # return value instead, read back on the SAME connection inside
            # the SAME transaction as its own review_status UPDATE.
            with connect() as db:
                return int(self.unified_kg.graph_seq_row(db, notebook_id)[0])

        self.knowledge_governance = KnowledgeGovernanceService(
            settings=self.settings,
            event_log=self.event_log,
            governance_store=self.governance,
            knowledge=self.knowledge,
            new_id=self.seams.new_id,
            now=self.seams.now,
            connect=connect,
            write=write,
            get_notebook=get_notebook,
            invalidate_unified_cache=invalidate_unified_cache,
            mark_unified_kg_dirty=mark_unified_kg_dirty,
            model_clients=self.models,
            edge_centrality_map=edge_centrality_map,
            embed_knowledge=embed_knowledge,
            knowledge_objects=knowledge_objects,
            as_retrieved=as_retrieved,
            rule_card=rule_card,
            set_conflict_status=set_conflict_status,
            memory_store=self.memory_store,
            kg_mutation_seq=read_kg_mutation_seq,
            mark_unified_kg_dirty_in_tx=mark_unified_kg_dirty_in_tx,
            review_queue_memo=self.review_queue_memo,
        )
        if self.memory_service is not None:
            self.memory_service.set_promotion_service(self.knowledge_governance)
        if self.scale_artifacts is None:
            raise RuntimeError(
                "wire_knowledge_lifecycle requires wire_scale_runtime() first"
            )
        self.knowledge_lifecycle = KnowledgeLifecycleService(
            settings=self.settings,
            event_log=self.event_log,
            knowledge=self.knowledge,
            governance_store=self.governance,
            unified_kg=self.unified_kg,
            governance=self.knowledge_governance, kg_build_jobs=self.kg_build_jobs,
            kg_building=self.catalog.kg_building,
            unified_cache=self.retrieval_snapshots.unified_cache,
            scale_artifacts=self.scale_artifacts,
            new_id=self.seams.new_id,
            now=self.seams.now,
            connect=connect,
            write=write,
            bulk_write=self.database.bulk_write,
            get_notebook=get_notebook, current_user_id=current_user_id,
            invalidate_unified_cache=invalidate_unified_cache,
            mark_unified_kg_dirty=mark_unified_kg_dirty,
            mark_unified_kg_dirty_in_tx=mark_unified_kg_dirty_in_tx,
            bump_cluster_mutation_seq=bump_cluster_mutation_seq,
            embed_objects_batch=embed_objects_batch,
            embed_relations_batch=embed_relations_batch,
            source_ids_from_evidence=source_ids_from_evidence,
            set_source_status=set_source_status,
            run_extraction=run_extraction,
            model_clients=self.models,
            reconcile_extracted_terminal=reconcile_extracted_terminal,
            wait_ingestion_drain=wait_ingestion_drain,
            cluster_map=cluster_map,
            annotate_edge_support=annotate_edge_support,
            decided_seed_pairs=decided_seed_pairs,
            relations_for_notebook=relations_for_notebook,
            notebook_copy_stats=notebook_copy_stats,
            note_model_error=note_model_error,
            participant_notebook_ids=self.notebook_store.participant_notebook_ids,
            invalidate_knowledge_counts=self.queries.invalidate_knowledge_counts,
            invalidate_review_queue_memo=self.review_queue_memo.invalidate,
        )
        self.scale_artifacts.lifecycle = self.knowledge_lifecycle
        return self.knowledge_lifecycle

    def wire_sharing(
        self,
        *,
        insert_row: Callable[..., None],
        copy_stats: Callable[[str], dict],
        storage_dir: Callable[[], Path],
        schedule_projection: Callable[[str], None],
    ) -> NotebookSharingService:
        """Compose the sharing domain (Task 9) once the facade-bound seams
        exist: ``insert_row`` = the facade's ``_insert_row`` compatibility
        seat, ``copy_stats`` = the facade's memoized ``notebook_copy_stats``,
        ``storage_dir`` resolves the live storage root, ``schedule_projection``
        (PR-2+3 Task 13) is the facade's
        ``knowhow_api.get_scheduler(repo).schedule`` — late-bound the same way
        as the other three seams, so it always resolves against the fully
        constructed facade even though wire_sharing runs mid-``__init__``."""
        self.sharing_store.bind_insert_row(insert_row)
        self.notebook_copies = NotebookCopyService(
            store=self.sharing_store,
            catalog=self.catalog,
            seams=self.seams,
            storage_dir=storage_dir,
            schedule_projection=schedule_projection,
        )
        self.sharing = NotebookSharingService(
            store=self.sharing_store,
            copies=self.notebook_copies,
            catalog=self.catalog,
            summaries=self.notebook_summaries,
            database=self.database,
            copy_stats=copy_stats,
            # T5:成员被移出后,他在这本库里的私有「理解」覆盖层随之清空。
            profiles=self.agent_profile,
            # P3(codex #535 R6):观察行同批清空,同一条空白起点契约。
            observations=self.agent_observations,
        )
        return self.sharing

    def _after_report_completed(self, committed) -> None:
        """Run report hooks after durable done and every execution scope exits."""
        observer = self.report_completed_observers
        if observer is None:
            return
        try:
            observer.observe_application(
                ReportCompletedObserverCallContext(
                    notification=CompletedReportNotification(
                        report_id=committed.report_id,
                        actor_id=committed.actor_id,
                        notebook_id=committed.notebook_id,
                        terminal_status="done",
                    ),
                    agent_profile=_ReportCompletedAccess(
                        lambda: self.agent_profile_jobs.note_report_completed(
                            committed.notebook_id, committed.actor_id
                        )
                    ),
                    connection_probe=self.database,
                    deadline_monotonic=(
                        time.monotonic()
                        + self.settings.report_post_completion_extension_timeout_seconds
                    ),
                ),
                event_sink=self.event_log.emit,
            )
        except Exception:
            pass

    def wire_report_execution(
        self,
        *,
        retrieval: Callable[[], Any],
        job_submitter: Any,
    ) -> ReportExecutionCoordinator:
        """Compose the deep-report execution domain (Task 25).

        ``retrieval`` resolves the facade's lazy ``retrieval`` property per
        engine construction (wire_retrieval is embedder-bound and lazy;
        resolving it also finishes the evidence-context service), so a report
        engine can only be built once retrieval exists — reports launch from
        request handlers, well after construction.  ``job_submitter`` is
        ``background_jobs.submit`` — the ONE detached-execution entry
        (copy_context propagation of the per-user model context, top-level
        exception guard, daemon naming).  The process-global
        ``REPORT_CANCELLATIONS`` registry is referenced BY IDENTITY: the
        ``report_engine`` module delegates (register_cancel/cancel_report/
        unregister_cancel), the cancel endpoint and this coordinator all
        observe the same instance.  A fresh CommunityQueryService is built per
        engine (per report job) so ``settings.sibling_min_bridge`` is read at
        launch time — mirroring the frozen per-deep-dive construction.
        Deliberately NO restart recovery: a dead process leaves the report row
        at its last persisted status."""
        from app.services.report_engine import ReportEngine, ReportEngineDependencies
        from app.services.report_execution import ReportGenerationGate
        from app.services.report_corpus_profile import ReportCorpusProfileService

        generation_gate = ReportGenerationGate(
            self.settings.report_generation_concurrency
        )

        def engine_factory(
            *, user_id: str, cancel_event=None, settings=None
        ) -> ReportEngine:
            retrieval_port = retrieval()
            engine_settings = settings or self.settings
            if self.evidence_context is None:
                raise RuntimeError(
                    "wire_report_execution engine factory requires wired retrieval"
                )
            dependencies = ReportEngineDependencies(
                reports=self.report_store,
                retrieval=retrieval_port,
                evidence_context=self.evidence_context,
                model_clients=self.models,
                model_errors=self.models,
                source_query=self.source_store,
                communities=retrieval_port.community_queries(engine_settings),
                settings=engine_settings,
                event_log=self.event_log,
                memory_retriever=self.memory_retriever,
                corpus_profile=ReportCorpusProfileService(self.source_store),
                generation_gate=generation_gate,
                # Agentic Memory P1:逐节深挖的理解注入(§5.2)。报告侧没有任何
                # 自动贯通的路,必须在这里显式填座位。
                agent_profile=self.agent_profile,
                # P2-T6:检索打法库的注入座位(注入本身另有默认关闭的开关)。
                retrieval_experiences=self.retrieval_experiences,
                selected_source_graph=self.selected_source_graph,
                retrieval_contributors=self.retrieval_contributors,
                retrieval_connection_probe=self.database,
                retrieval_contributor_hydrate=(
                    retrieval_port.hydrate_retrieval_contribution_chunks
                ),
                scale_version=lambda nb: tuple(self.scale_artifacts.version(nb)),
                selected_graph_hydrate=lambda ids: (
                    hydrate_selected_graph_chunk_rows(
                        retrieval_port.hydrate_chunk_candidates(ids)[0]
                    )
                ),
            )
            return ReportEngine(
                dependencies, user_id=user_id, cancel_event=cancel_event
            )

        self.report_execution = ReportExecutionCoordinator(
            reports=self.report_store,
            engine_factory=engine_factory,
            cancellations=self.report_cancellations,
            job_submitter=job_submitter,
            after_completed=self._after_report_completed,
        )
        return self.report_execution

    def wire_ask(self, *, retrieval: Callable[[], Any]) -> None:
        """Store the Ask composition's retrieval resolver (Task 24).

        ``retrieval`` resolves the facade's lazy ``retrieval`` property per
        first use (wire_retrieval is embedder-bound and lazy; resolving it
        also finishes the evidence-context service) — mirroring
        wire_report_execution's engine factory.  The AskService itself is
        composed by :meth:`ask_service` on first ask."""
        self._ask_retrieval = retrieval

    def ask_service(self) -> AskService:
        """The ONE runtime-owned AskService (Task 24), composed lazily.

        Ports: the Task-22 ask-state store (shared synchronous/streaming
        durable-job lifecycle and atomic final save with EXPLICIT user_id),
        the Task-21 retrieval + evidence-context services,
        the model provider doubling as the model-error sink (per-user client
        resolution stays a per-access ContextVar chain), a fresh
        CommunityQueryService PER USE (``sibling_min_bridge`` read at call
        time — the frozen per-ask/per-engine construction), a fresh
        NotebookScaleProfile PER ``_needs_index`` call (its copy-stats memo is
        process-owned by ``notebook_scale`` since R2-2, so the profile itself
        holds no cache state and re-reads the CURRENT settings/version each
        call), the catalog's notebook guard, the schema registry, the
        lifecycle-owned community reports and the source-title projection."""
        if self.ask is not None:
            return self.ask
        with self._ask_wire_lock:
            if self.ask is not None:
                return self.ask
            if self._ask_retrieval is None:
                raise RuntimeError("ask_service requires wire_ask() first")
            retrieval = self._ask_retrieval()
            if self.evidence_context is None or self.scale_artifacts is None:
                raise RuntimeError(
                    "ask_service requires wired retrieval and scale runtime"
                )
            self.ask = AskService(
                ask_state=self.ask_state,
                retrieval=retrieval,
                candidates=retrieval,
                evidence_context=self.evidence_context,
                model_clients=self.models,
                model_errors=self.models,
                communities=retrieval.community_queries,
                scale_profiles=lambda: NotebookScaleProfile(
                    self.settings,
                    self.queries,
                    lambda nb: tuple(self.scale_artifacts.version(nb)),
                    self.retrieval_snapshots.copy_stats_memo,
                ),
                scale_index_probe=lambda nb: (
                    self.scale_artifacts.load(nb, allow_stale=True) is not None
                ),
                settings=self.settings,
                event_log=self.event_log,
                notebooks=self.catalog,
                schemas=self.schema_registry,
                source_titles=self.source_store.source_titles,
                knowhow_store=self.knowhow_store,
                memory_retriever=self.memory_retriever,
                current_user_id=self._current_user_id,
                cancellations=self.ask_cancellations,
                # 逐步推理的集合地图/清单:交**这两个** eager 实例,和离线/其他
                # 调用方共用同一份 per-source 计数缓存(地图与清单必须同源)。
                collection_catalog=self.collection_catalog,
                collection_enumeration=self.collection_enumeration,
                # Agentic Memory P1:Agent 对该库的已有理解 store。交座位本身
                # (形态同上面两个集合服务),提问者身份由 ask 在构造 retriever
                # 时显式传入 —— 绝不让下游回退 ContextVar。
                agent_profile=self.agent_profile,
                # P2-T6:检索打法库的注入座位。没有配套的身份参数——那张表没有
                # 租户维度(见 RetrievalExperienceStorePort)。注入本身还要过一把
                # 默认**关闭**的开关。
                retrieval_experiences=self.retrieval_experiences,
                # Agentic Memory P3(B-Profile,T8):用户检索/回答风格偏好的读
                # 座位——``self.identity`` 本来就是这个运行时唯一的
                # ``IdentityStorePort`` 实例(上面 ``current_user_id`` 已经在用
                # 它)。提问者身份同样由 ask 侧显式传入,不经这里。
                identity_store=self.identity,
                selected_source_graph=self.selected_source_graph,
                retrieval_contributors=self.retrieval_contributors,
                retrieval_connection_probe=self.database,
                retrieval_contributor_hydrate=(
                    retrieval.hydrate_retrieval_contribution_chunks
                ),
                scale_version=lambda nb: tuple(self.scale_artifacts.version(nb)),
                selected_graph_hydrate=lambda ids: (
                    hydrate_selected_graph_chunk_rows(
                        retrieval.hydrate_chunk_candidates(ids)[0]
                    )
                ),
                # X9 PR-A: the frozen ``ask.gap_consult`` host.  No identity
                # travels with it — the whole point of that contract is that a
                # plugin sees a bounded question and nothing about who asked
                # it or which notebook it came from.
                gap_consult_host=self.gap_consult,
                ask_engine_host=self.ask_engines,
                ask_engine_participant_notebooks=(
                    self.notebook_store.participant_notebook_ids
                ),
                ask_engine_visible_sources=self.source_store.all_visible_source_ids,
                ask_engine_hidden_sources=self.source_store.hidden_source_ids,
            )
        return self.ask

    @property
    def ask_component(self) -> AskService:
        return self.ask_service()
