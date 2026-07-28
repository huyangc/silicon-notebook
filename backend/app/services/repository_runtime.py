from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import weakref
from typing import Any, Callable

from app.core.config import Settings
from app.core.event_logging import EventLogger, llm_log_dir_aligned
from app.repositories.bundle import PersistenceBundleFactory
from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
from app.repositories.ports import (
    EmbeddingStorePort,
    IndexProjectionStorePort,
    RepositorySeams,
    SharingStorePort,
)
from app.repositories.source_files import SourceFileStore
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
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_service import RetrievalService
from app.services.scale_artifact_catalog import ScaleArtifactCatalog
from app.services.scale_artifact_runtime import ScaleArtifactRuntime
from app.services.scale_index_builder import ScaleIndexBuilder
from app.services.schema_registry import SchemaRegistryService
from app.services.source_chunking import SourceChunkingService
from app.services.source_embedding import SourceEmbeddingService
from app.services.source_ingestion import SourceIngestionService
from app.services.vector_cache import VectorCache
# Task 23: Ask detached-execution composition (appended block — parallel
# Gate-8 tracks keep the shared import list above untouched).
from app.services import background_jobs
from app.services.ask_execution import AskCancellationRegistry, AskExecutionCoordinator
# Task 24: Ask mode engines + synthesis (appended — same convention).
from app.services.ask_service import AskService
from app.services.notebook_scale import NotebookScaleProfile
from app.services.pending_actions_service import PendingActionsService

@dataclass(frozen=True)
class RepositoryCompatibilitySeams:
    new_id: Callable[[str], str]
    now: Callable[[], str]
    copy_chunk_size: Callable[[], int]
    remap_json_ids: Callable[[Any, dict], Any]
    in_chunk_size: Callable[[], int]


class RepositoryRuntime:
    def __init__(
        self,
        settings: Settings,
        root_dir: Path,
        seams: RepositoryCompatibilitySeams,
        persistence_factory: PersistenceBundleFactory,
        *,
        model_provider: Any | None = None,
    ) -> None:
        validate_process_local_scheduler_deployment()
        self.settings = settings
        self.root_dir = root_dir
        self.seams = seams
        self._closed = False
        self.event_log = EventLogger(settings, channel="events", per_user=True)
        if not llm_log_dir_aligned(settings.llm_log_path, settings.event_log_dir):
            self.event_log.logger.warning(
                "LLM_LOG_PATH 的目录(%s)与 EVENT_LOG_DIR(%s)不一致，"
                "日志查看器将读不到 per-user 的 llm 日志；请对齐两者或都设为同一目录。",
                settings.llm_log_path,
                settings.event_log_dir,
            )
        self.models = model_provider or RuntimeModelProvider(settings, self.event_log)
        kg_scheduler.initialize(
            window_workers=self.models.parallelism("kg_extract"),
            job_workers=settings.kg_job_concurrency,
        )
        bundle = persistence_factory.create(
            settings=settings,
            root_dir=root_dir,
            seams=seams,
        )
        self.database = bundle.database
        stats = getattr(self.database, "stats", None)
        if stats is not None:
            stats.sink = self.event_log.emit
        self.identity = bundle.identity
        self.model_status_store = bundle.model_status
        self.model_status = ModelStatusService(
            getattr(self.models, "registry", SystemModelServiceRegistry({}, {})),
            self.models,
            self.model_status_store,
        )
        set_observation_sink = getattr(self.models, "set_observation_sink", None)
        if callable(set_observation_sink):
            set_observation_sink(self.model_status.record_provider_observation)
        self.queries = bundle.queries
        self.notebook_store = bundle.notebooks
        self.kg_build_jobs = bundle.kg_build_jobs
        self.notebook_summaries = NotebookSummaryQuery(self.database, self.queries, self.kg_build_jobs)
        # Source files resolve storage_dir through the database. Construct eagerly
        # BEFORE the catalog so its storage_dir callable can bind THIS store
        # directly.
        self.source_files = SourceFileStore(
            self.database.resolve_path(settings.storage_dir),
            resolve_path=self.database.resolve_path,
        )
        self.catalog = NotebookCatalogService(
            store=self.notebook_store,
            summaries=self.notebook_summaries,
            queries=self.queries,
            identity=self.identity,
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
            storage_dir=lambda source_files=self.source_files: source_files.storage_dir,
        )
        self.source_store = bundle.sources
        self.chunk_store = bundle.chunks
        # Task 25: reports-table row persistence is seam-free (the shared
        # database boundary + the id/clock seams + identity's current_user),
        # so the runtime owns and constructs it eagerly.  The process-global
        # cancellation registry is referenced BY IDENTITY (report_engine's
        # register/cancel/unregister delegates observe the same instance);
        # the execution coordinator is finished by wire_report_execution()
        # because its engine factory rides the lazily wired retrieval and
        # evidence-context ports.
        self.report_store = bundle.reports
        self.report_application = ReportApplicationService(
            self.catalog, self.report_store
        )
        self.report_cancellations = REPORT_CANCELLATIONS
        self.report_execution: "ReportExecutionCoordinator | None" = None
        # Task 13: the three knowledge-domain persistence stores share the ONE
        # database boundary. Their primitives are connection-taking — the
        # facade keeps every transaction/connection boundary (its `_write` /
        # `_connect` compatibility seams stay the observable commit points),
        # so construction is eager and seam-free.
        self.knowledge = bundle.knowledge
        self.governance = bundle.governance
        self.unified_kg = bundle.unified_kg
        # KG 质量分析报告的只读装配(T3)。**后端中性**:它只吃 database + unified_kg
        # 两个 seam(两者都是后端相关的实例,由上面的 bundle 给),自己不 import 任何
        # 后端 —— 所以它落在这个中性 runtime 里,而不是像 checkup 那样落在两个 facade
        # 各一份(checkup 依赖 sqlite/postgres 各自的 maintenance adapter,neutrality
        # 守卫禁止 runtime import 它们)。
        # 构造是 eager 且 seam-free 的(只存引用);进程内的记忆化因此跟着 runtime 单例
        # 跨请求存活 —— 那正是它存在的意义(大库上板块列表要排 8.8 万行)。
        self.kg_analysis = KgAnalysisService(
            database=self.database,
            unified_kg=self.unified_kg,
            now=seams.now,
        )
        # Task 22: Ask/answer/conversation/job/trace persistence shares the ONE
        # database boundary.  Identity is explicit (user_id per call — the
        # store never reads request ContextVars) and the seams dataclass is
        # stored, never evaluated, so construction is eager and seam-free.
        # Cancel-event registry + fail-open trace logging stay facade-side.
        self.ask_state = bundle.ask_state
        self.memory_store = bundle.memory
        self.memory_service: "MemoryService | None" = None
        self.memory_retriever: "MemoryRetriever | None" = None
        self._embedder: Any = None
        self._notebook_languages: dict[str, list[str]] = {}
        # Task 18: scale/viz artifact FILE persistence is seam-free (raw
        # settings.storage_dir paths + the pure kg.scale_index / kg.viz_index
        # load/save modules), so the runtime owns and constructs it eagerly.
        # The DB projections + read catalog over it are finished by
        # wire_scale_artifacts() — their collaborators (the facade `_connect`
        # read seam, the `_in_batches` IN-chunking helper, the retrieval-owned
        # ent-chunk/mention/vector-matrix caches, the memoized version key,
        # the LRU cache + cold-load lock table and the model-error note) are
        # facade-bound seams that only exist once the facade constructor
        # reaches them.  Construction stays lazy — no seam calls.
        self.scale_artifact_store = ScaleArtifactStore(settings)
        self.index_projections: IndexProjectionStorePort = bundle.index_projection
        self.scale_catalog: "ScaleArtifactCatalog | None" = None
        self.scale_builder: "ScaleIndexBuilder | None" = None
        self.scale_artifacts: "ScaleArtifactRuntime | None" = None
        # Vector persistence is finished by wire_persistence(): its write seat
        # is the facade's `_write` compatibility seam, which only exists once
        # the facade constructor reaches it. Construction stays lazy.
        self.embedding_store: EmbeddingStorePort = bundle.embeddings
        # The source embed/chunk pipeline is finished by wire_source_pipeline():
        # its mutable embedder and _mark_unified_kg_dirty collaborators plus
        # the wired EmbeddingStore only exist once the facade constructor
        # reaches them. Construction stays lazy — no seam calls.
        self.source_embedding: "SourceEmbeddingService | None" = None
        self.source_chunking: "SourceChunkingService | None" = None
        # Source ingestion orchestration is finished by wire_source_ingestion():
        # its collaborators (facade _write seat, facade-owned parse/summarize/
        # model seams and the TEMPORARY KG callbacks that Gate 5 replaces with
        # real services) are facade-bound seams that only exist once the facade
        # constructor reaches them.  Construction stays lazy — no seam calls.
        self.source_ingestion: "SourceIngestionService | None" = None
        # Task 17: the retrieval snapshot caches are seam-free (a version-keyed
        # VectorCache sized from settings plus the plain unified-graph dict),
        # so the runtime owns and constructs them eagerly. The facade's
        # `_vector_cache` / `_unified_cache` handles are write-through
        # descriptors over THESE objects and the KG mutation coordinator reads
        # them through this cache — one owner, no facade-only copies.
        self.retrieval_snapshots = RetrievalSnapshotCache(
            vector_cache=VectorCache(max_entries=settings.vector_cache_max_entries),
            unified_cache={},
        )
        # The KG mutation coordinator is finished by wire_kg_mutations(): its
        # remaining collaborators (the scale-runtime auto-index once-set, the
        # corpus-language memo and the facade `_write` transaction seat) only
        # exist once the facade constructor reaches them.  Construction stays
        # lazy — no seam calls.  The caches come from retrieval_snapshots.
        self.kg_mutations: "KgMutationCoordinator | None" = None
        # The knowledge lifecycle/governance services are finished by
        # wire_knowledge_lifecycle(): their collaborators (the facade `_write`/
        # `_connect` transaction seats, the facade-owned unified/viz cache
        # objects, the coordinator-backed dirty/invalidate wrappers, the
        # process-owned model provider and the Gate-6 scale/viz adapters)
        # only exist once the facade constructor reaches them.  Construction
        # stays lazy — no seam calls.
        self.knowledge_governance: "KnowledgeGovernanceService | None" = None
        self.knowledge_lifecycle: "KnowledgeLifecycleService | None" = None
        self.knowledge_query: "KnowledgeQueryService | None" = None
        self.pending_actions_service: "PendingActionsService | None" = None
        # Sharing/deep-copy composition is finished by wire_sharing(): its
        # collaborators (facade _insert_row seat, notebook_copy_stats memo,
        # storage_dir) are facade-bound seams that only exist once the facade
        # constructor reaches them.  Construction stays lazy — no seam calls.
        self.sharing_store: SharingStorePort = bundle.sharing
        self.notebook_copies: "NotebookCopyService | None" = None
        self.sharing: "NotebookSharingService | None" = None
        self.evidence_context: "EvidenceContextService | None" = None
        self.candidate_retrieval: "CandidateRetrievalService | None" = None
        self.graph_retrieval: "GraphRetrievalService | None" = None
        self.retrieval: "RetrievalService | None" = None
        self._retrieval_wire_lock = threading.Lock()
        # Task 13: schema CRUD + LLM-backed induction. It requests the
        # ``schema_induction`` workload from the process-owned provider.
        self.schema_registry = SchemaRegistryService(
            self.database,
            self.notebook_store,
            self.knowledge,
            self.source_store,
            self.models,
            settings,
        )
        # Task 23: Ask cancellation + detached streaming execution.  Both are
        # seam-free (the Task-22 ask_state store, the module-level
        # copied-context job helper and the runtime event log), so the runtime
        # owns and constructs them eagerly.  The facade's frozen
        # _ask_cancel_events/_ask_cancel_lock attributes become compatibility
        # handles over THIS registry (one owner — the explicit cancel endpoint
        # and the coordinator's register/unregister meet in the same map).
        # Task 24: the coordinator's runner is no longer a facade callback —
        # it resolves the ONE runtime-owned AskService through the bound
        # ``ask_service`` accessor (lazy: composition rides the wired
        # retrieval port, which is embedder-bound and only exists after the
        # facade constructor finishes and the first ask touches it).
        self.ask_cancellations = AskCancellationRegistry()
        self.ask: "AskService | None" = None
        self._ask_wire_lock = threading.Lock()
        self._ask_retrieval: "Callable[[], Any] | None" = None
        self.ask_execution = AskExecutionCoordinator(
            ask_state=self.ask_state,
            cancellations=self.ask_cancellations,
            job_submitter=background_jobs,
            event_log=self.event_log,
            ask=self.ask_service,
        )
        # Task 27: SQLite maintenance face for CLI/batch composition roots —
        # lazily wired by the facade `maintenance` property (it needs the
        # embedder-bound retrieval provider, exactly like `ask` above).
        # knowhow-tables PR-1 Task 2: row persistence for the Task-1 schema
        # (knowhow_tables/columns/rows/cells + notebook_assets). Seam-free
        # (only new_id/now, like notebook_store above), so construction is
        # eager. Task 5's projector and Task 6's import/table API depend on
        # the facade's one-hop delegates over this exact instance.
        self.knowhow_store = bundle.knowhow
        # knowhow 单表跨 notebook 传输的 SQL（快照+单事务插入+校验）。id/时钟
        # 由 transfer.py 的 _remap 直接从 repo._runtime.seams 取（见该文件头
        # 注释），store 自己不需要——不带 new_id/now 构造参数。
        self.knowhow_transfer_store = bundle.knowhow_transfer
        # knowhow 表版本管理 Task 3：变更流水/里程碑的读侧 store。new_id/now
        # 与上面 knowhow_store 用同一对 seams 可调用对象，保持三个 store 的
        # id/时钟来源单一（record_change 本身是模块级函数，不在这里持有——
        # 由 Task 4-6 的写方法在各自事务内直接调用）。
        self.knowhow_history_store = bundle.knowhow_history
        # 逐步推理集合枚举 PR-2 T2:类型化集合的「地图层」计数。只吃窄端口
        # (database/sources/notebooks/queries/unified_kg),零模型调用,构造即
        # 建三个有界进程内缓存,故与上面几个 store 一样是 eager 且无 seam。
        self.collection_catalog = CollectionCatalogService(
            database=self.database,
            sources=self.source_store,
            notebooks=self.notebook_store,
            queries=self.queries,
            unified_kg=self.unified_kg,
        )
        # 同批 T3:类型化集合的「清单层」。刻意吃**上面那一个** catalog 实例而不是
        # 自己新建——地图计数与枚举必须来自同一份 per-source 缓存,否则会出现
        # 「地图报 12、清单只列 8」这种无法自证的假部分。同样零模型调用、无 seam。
        self.collection_enumeration = CollectionEnumerationService(
            database=self.database,
            catalog=self.collection_catalog,
            sources=self.source_store,
            notebooks=self.notebook_store,
            knowledge=self.knowledge,
            unified_kg=self.unified_kg,
        )
        # P2·T2 体检聚合(CheckupService)刻意**不**在这里构造:它依赖 maintenance 的 COUNT +
        # sqlite QueryStore,而 repository_runtime 是**后端中性**模块(neutrality 守卫禁止它 import
        # 任何 app.repositories.sqlite/postgres)。故 checkup 由**后端相关的** SQLiteRepository facade
        # 懒构造(见 sqlite_repository.py 的 ``checkup`` 属性),复用 facade 的 ``maintenance`` adapter。
        # 本 runtime 只提供 ``_active_source_ids_snapshot`` 这个窄 seam 给它。

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
        )
        self.source_chunking = SourceChunkingService(
            settings=self.settings,
            sources=self.source_store,
            chunks=self.chunk_store,
            embedding=self.source_embedding,
            new_id=self.seams.new_id,
            now=self.seams.now,
            mark_unified_dirty=mark_unified_dirty,
        )
        return self.source_embedding, self.source_chunking

    def wire_source_ingestion(
        self,
        *,
        write: Callable[[], Any],
        source_elements: Callable[[str], list],
        summarize_source: Callable[..., str],
        source_type_from_name: Callable[[str], str],
        parse_file: Callable[..., list],
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
        ``source_elements``/``summarize_source``/``parse_file`` stay
        facade/module late-bound so frozen patch targets (repo.source_elements,
        repo._summarize_source, module parse_source_file) keep working; model
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
            parse_file=parse_file,
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
            make_persist_image=make_persist_image,
            delete_source_images=delete_source_images,
            invalidate_knowledge_counts=self.queries.invalidate_knowledge_counts,
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
            snapshots=self.retrieval_snapshots,
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
            current_user_id=lambda: self.identity.current_user().id,
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
        bump_cluster_mutation_seq: Callable[..., None],
        embed_objects_batch: Callable[..., None],
        embed_relations_batch: Callable[..., None],
        source_ids_from_evidence: Callable[..., set],
        set_source_status: Callable[..., None],
        run_extraction: Callable[..., None],
        reconcile_extracted_terminal: Callable[..., None],
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
        ``mark_unified_kg_dirty`` / ``bump_cluster_mutation_seq`` are the
        facade's Task-14 wrapper seats (the coordinator stays the single dirty
        entry and repo-level patch seats stay effective); ``llm``/``kg_llm``
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
            relations_for_notebook=relations_for_notebook,
            edge_centrality_map=edge_centrality_map,
            embed_knowledge=embed_knowledge,
            knowledge_objects=knowledge_objects,
            as_retrieved=as_retrieved,
            rule_card=rule_card,
            set_conflict_status=set_conflict_status,
            memory_store=self.memory_store,
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
            bump_cluster_mutation_seq=bump_cluster_mutation_seq,
            embed_objects_batch=embed_objects_batch,
            embed_relations_batch=embed_relations_batch,
            source_ids_from_evidence=source_ids_from_evidence,
            set_source_status=set_source_status,
            run_extraction=run_extraction,
            model_clients=self.models,
            reconcile_extracted_terminal=reconcile_extracted_terminal,
            cluster_map=cluster_map,
            annotate_edge_support=annotate_edge_support,
            decided_seed_pairs=decided_seed_pairs,
            relations_for_notebook=relations_for_notebook,
            notebook_copy_stats=notebook_copy_stats,
            note_model_error=note_model_error,
            participant_notebook_ids=self.notebook_store.participant_notebook_ids,
            invalidate_knowledge_counts=self.queries.invalidate_knowledge_counts,
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
        )
        return self.sharing

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
            )
            return ReportEngine(
                dependencies, user_id=user_id, cancel_event=cancel_event
            )

        self.report_execution = ReportExecutionCoordinator(
            reports=self.report_store,
            engine_factory=engine_factory,
            cancellations=self.report_cancellations,
            job_submitter=job_submitter,
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
        NotebookScaleProfile PER ``_needs_index`` call (reads the CURRENT
        ``retrieval_snapshots.vector_cache`` so facade-level cache swaps stay
        observed), the catalog's notebook guard, the schema registry, the
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
                graph=retrieval,
                evidence_context=self.evidence_context,
                model_clients=self.models,
                model_errors=self.models,
                communities=retrieval.community_queries,
                scale_profiles=lambda: NotebookScaleProfile(
                    self.settings,
                    self.queries,
                    lambda nb: tuple(self.scale_artifacts.version(nb)),
                    self.retrieval_snapshots.vector_cache,
                ),
                scale_index_probe=lambda nb: (
                    self.scale_artifacts.load(nb, allow_stale=True) is not None
                ),
                settings=self.settings,
                event_log=self.event_log,
                notebooks=self.catalog,
                schemas=self.schema_registry,
                community_reports=(
                    lambda nb: self.knowledge_lifecycle.get_community_reports(nb)
                ),
                source_titles=self.source_store.source_titles,
                knowhow_store=self.knowhow_store,
                memory_retriever=self.memory_retriever,
                current_user_id=lambda: self.identity.current_user().id,
                cancellations=self.ask_cancellations,
                # 逐步推理的集合地图/清单:交**这两个** eager 实例,和离线/其他
                # 调用方共用同一份 per-source 计数缓存(地图与清单必须同源)。
                collection_catalog=self.collection_catalog,
                collection_enumeration=self.collection_enumeration,
            )
        return self.ask

    @property
    def ask_component(self) -> AskService:
        return self.ask_service()
