from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Callable

from app.core import ask_context
from app.core.config import Settings
from app.core.event_logging import EventLogger, llm_log_dir_aligned
from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
from app.repositories.source_files import SourceFileStore
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.governance_store import GovernanceStore
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.index_projection_store import IndexProjectionStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.report_store import ReportStore
from app.repositories.sqlite.sharing_store import SharingStore
from app.repositories.sqlite.source_store import SourceStore
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
from app.services.kg_mutation import KgMutationCoordinator
from app.services.knowledge_governance import KnowledgeGovernanceService
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.knowledge_query import KnowledgeQueryService
from app.services.evidence_context import EvidenceContextService
from app.services.graph_retrieval import GraphRetrievalService
from app.services.model_provider import RuntimeModelProvider
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery
from app.services.notebook_sharing import NotebookCopyService, NotebookSharingService
from app.services.report_execution import (
    REPORT_CANCELLATIONS,
    ReportExecutionCoordinator,
)
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


class RepositoryRuntime:
    def __init__(self, settings: Settings, root_dir: Path, seams: RepositoryCompatibilitySeams) -> None:
        self.settings = settings
        self.root_dir = root_dir
        self.seams = seams
        self.database = SqliteDatabase(settings, root_dir)
        self.model_config_cache: dict[str, dict[str, Any]] = {}
        self.identity = IdentityStore(
            self.database,
            settings,
            self.model_config_cache,
        )
        self.queries = QueryStore(self.database)
        self.notebook_store = NotebookStore(
            self.database,
            new_id=seams.new_id,
            now=seams.now,
        )
        self.notebook_summaries = NotebookSummaryQuery(self.database, self.queries)
        self.catalog = NotebookCatalogService(
            store=self.notebook_store,
            summaries=self.notebook_summaries,
            queries=self.queries,
            identity=self.identity,
        )
        self.source_store = SourceStore(self.database, now=seams.now)
        self.chunk_store = ChunkStore(self.database)
        # Task 25: reports-table row persistence is seam-free (the shared
        # database boundary + the id/clock seams + identity's current_user),
        # so the runtime owns and constructs it eagerly.  The process-global
        # cancellation registry is referenced BY IDENTITY (report_engine's
        # register/cancel/unregister delegates observe the same instance);
        # the execution coordinator is finished by wire_report_execution()
        # because its engine factory rides the lazily wired retrieval and
        # evidence-context ports.
        self.report_store = ReportStore(
            self.database,
            new_id=seams.new_id,
            now=seams.now,
            current_user_id=lambda: self.identity.current_user().id,
            get_notebook=self.catalog.get_notebook,
        )
        self.report_cancellations = REPORT_CANCELLATIONS
        self.report_execution: "ReportExecutionCoordinator | None" = None
        # Task 13: the three knowledge-domain persistence stores share the ONE
        # database boundary. Their primitives are connection-taking — the
        # facade keeps every transaction/connection boundary (its `_write` /
        # `_connect` compatibility seams stay the observable commit points),
        # so construction is eager and seam-free.
        self.knowledge = KnowledgeStore(self.database, seams)
        self.governance = GovernanceStore(self.database, seams)
        self.unified_kg = UnifiedKgStore(self.database, seams.now)
        # Task 22: Ask/answer/conversation/job/trace persistence shares the ONE
        # database boundary.  Identity is explicit (user_id per call — the
        # store never reads request ContextVars) and the seams dataclass is
        # stored, never evaluated, so construction is eager and seam-free.
        # Cancel-event registry + fail-open trace logging stay facade-side.
        self.ask_state = AskStateStore(self.database, seams)
        # Source file persistence resolves storage_dir through the database
        # boundary's resolve_path — no facade seams, so construction is eager.
        self.source_files = SourceFileStore(
            self.database.resolve_path(settings.storage_dir),
            resolve_path=self.database.resolve_path,
        )
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
        self.index_projections: "IndexProjectionStore | None" = None
        self.scale_catalog: "ScaleArtifactCatalog | None" = None
        self.scale_builder: "ScaleIndexBuilder | None" = None
        self.scale_artifacts: "ScaleArtifactRuntime | None" = None
        # Vector persistence is finished by wire_persistence(): its write seat
        # is the facade's `_write` compatibility seam, which only exists once
        # the facade constructor reaches it. Construction stays lazy.
        self.embedding_store: "EmbeddingStore | None" = None
        # The source embed/chunk pipeline is finished by wire_source_pipeline():
        # its collaborators (facade embedder attribute, _flush_object_vectors
        # seat, _mark_unified_kg_dirty seat, the wired EmbeddingStore) are
        # facade-bound seams that only exist once the facade constructor
        # reaches them.  Construction stays lazy — no seam calls.
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
        # per-user model-client properties and the Gate-6 scale/viz adapters)
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
        self.sharing_store: "SharingStore | None" = None
        self.notebook_copies: "NotebookCopyService | None" = None
        self.sharing: "NotebookSharingService | None" = None
        self.event_log = EventLogger(settings, channel="events", per_user=True)
        if not llm_log_dir_aligned(settings.llm_log_path, settings.event_log_dir):
            self.event_log.logger.warning(
                "LLM_LOG_PATH 的目录(%s)与 EVENT_LOG_DIR(%s)不一致，"
                "日志查看器将读不到 per-user 的 llm 日志；请对齐两者或都设为同一目录。",
                settings.llm_log_path,
                settings.event_log_dir,
            )
        self.models = RuntimeModelProvider(
            self.identity,
            settings,
            self.event_log,
            ask_context,
        )
        self.evidence_context: "EvidenceContextService | None" = None
        self.candidate_retrieval: "CandidateRetrievalService | None" = None
        self.graph_retrieval: "GraphRetrievalService | None" = None
        self.retrieval: "RetrievalService | None" = None
        self._retrieval_wire_lock = threading.Lock()
        # Task 13: schema CRUD + LLM-backed induction. Depends on the model
        # provider (late-bound per-user llm_client property), so it composes
        # after `models`.
        self.schema_registry = SchemaRegistryService(
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
        self.maintenance: Any = None

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
        return self._embedder

    @embedder.setter
    def embedder(self, value: Any) -> None:
        self.set_embedder(value)

    def set_embedder(self, value: Any) -> None:
        self._embedder = value
        if self.retrieval is not None:
            self.retrieval.replace_embedder(value)

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

    def set_model_config_cache(self, value: dict[str, dict[str, Any]]) -> None:
        self.model_config_cache = value
        self.identity.model_config_cache = value
        self.models.model_config_cache = value

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
            candidates = CandidateRetrievalService(**common)
            graph = GraphRetrievalService(**common)
            retrieval = RetrievalService(candidates=candidates, graph=graph)
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

    def wire_persistence(self, *, write: Callable[..., Any]) -> EmbeddingStore:
        """Compose the vector persistence (Task 10) once the facade-bound
        ``write`` seat exists: it is the facade's ``_write`` compatibility
        seam (itself delegating to the shared database write lock), resolved
        at call time so per-instance monkeypatches — transaction counting,
        failure injection — keep observing every vector flush."""
        self.embedding_store = EmbeddingStore(write=write)
        return self.embedding_store

    def wire_source_pipeline(
        self,
        *,
        embedder: Callable[[], Any],
        flush_object_vectors: Callable[[str, list], None],
        mark_unified_dirty: Callable[[str], None],
    ) -> tuple[SourceEmbeddingService, SourceChunkingService]:
        """Compose the source embed/chunk pipeline (Task 11) once the
        facade-bound seams exist: ``embedder`` resolves the facade's mutable
        ``self.embedder`` at call time (tests swap in fakes post-construction),
        ``flush_object_vectors`` is the facade's ``_flush_object_vectors``
        MASTER_V10 seat (incremental object-vector commits stay observable and
        interruptible), ``mark_unified_dirty`` the facade's
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
            event_log=self.event_log,
            flush_object_vectors=flush_object_vectors,
            now=self.seams.now,
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
        llm: Callable[[], Any],
        kg_llm: Callable[[], Any],
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
    ) -> SourceIngestionService:
        """Compose the source ingestion orchestration (Task 12) once the
        facade-bound seams exist.  ``write`` is the facade's ``_write``
        compatibility seat resolved per call (transaction counting / failure
        injection keep observing every ingestion commit boundary);
        ``source_elements``/``summarize_source``/``parse_file`` and the model
        client seams stay facade/module late-bound so frozen patch targets
        (repo.source_elements, repo._summarize_source, module
        parse_source_file, per-user llm/kg_llm properties) keep working; the
        remaining callables are TEMPORARY facade-owned KG/catalog callbacks —
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
            llm=llm,
            kg_llm=kg_llm,
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
        )
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
        self.index_projections = IndexProjectionStore(
            self.settings,
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
            event_log=self.event_log,
            database=self.database,
            catalog=self.catalog,
            knowledge=self.knowledge,
            chunk_store=self.chunk_store,
            unified_kg=self.unified_kg,
            scale_runtime=self.scale_artifacts,
            retrieval=retrieval,
            schemas=self.schema_registry,
            vector_cache=self.retrieval_snapshots.vector_cache,
            notebook_languages=self.notebook_languages,
        )
        self.pending_actions_service = PendingActionsService(
            self.queries,
            scale_runtime=self.scale_artifacts,
        )

    def wire_knowledge_lifecycle(
        self,
        *,
        connect: Callable[[], Any],
        write: Callable[[], Any],
        get_notebook: Callable[[str], Any],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_kg_dirty: Callable[[str], None],
        bump_cluster_mutation_seq: Callable[..., None],
        embed_objects_batch: Callable[..., None],
        embed_relations_batch: Callable[..., None],
        source_ids_from_evidence: Callable[..., set],
        set_source_status: Callable[..., None],
        run_extraction: Callable[[str], None],
        llm: Callable[[], Any],
        kg_llm: Callable[[], Any],
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
            llm=llm,
            kg_llm=kg_llm,
            relations_for_notebook=relations_for_notebook,
            edge_centrality_map=edge_centrality_map,
            embed_knowledge=embed_knowledge,
            knowledge_objects=knowledge_objects,
            as_retrieved=as_retrieved,
            rule_card=rule_card,
            set_conflict_status=set_conflict_status,
        )
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
            governance=self.knowledge_governance,
            kg_building=self.catalog.kg_building,
            unified_cache=self.retrieval_snapshots.unified_cache,
            scale_artifacts=self.scale_artifacts,
            new_id=self.seams.new_id,
            now=self.seams.now,
            connect=connect,
            write=write,
            get_notebook=get_notebook,
            invalidate_unified_cache=invalidate_unified_cache,
            mark_unified_kg_dirty=mark_unified_kg_dirty,
            bump_cluster_mutation_seq=bump_cluster_mutation_seq,
            embed_objects_batch=embed_objects_batch,
            embed_relations_batch=embed_relations_batch,
            source_ids_from_evidence=source_ids_from_evidence,
            set_source_status=set_source_status,
            run_extraction=run_extraction,
            llm=llm,
            kg_llm=kg_llm,
            cluster_map=cluster_map,
            annotate_edge_support=annotate_edge_support,
            decided_seed_pairs=decided_seed_pairs,
            relations_for_notebook=relations_for_notebook,
            notebook_copy_stats=notebook_copy_stats,
            note_model_error=note_model_error,
        )
        self.scale_artifacts.lifecycle = self.knowledge_lifecycle
        return self.knowledge_lifecycle

    def wire_sharing(
        self,
        *,
        insert_row: Callable[..., None],
        copy_stats: Callable[[str], dict],
        storage_dir: Callable[[], Path],
    ) -> NotebookSharingService:
        """Compose the sharing domain (Task 9) once the facade-bound seams
        exist: ``insert_row`` = the facade's ``_insert_row`` compatibility
        seat, ``copy_stats`` = the facade's memoized ``notebook_copy_stats``,
        ``storage_dir`` resolves the live storage root."""
        self.sharing_store = SharingStore(
            self.database,
            self.settings,
            now=self.seams.now,
            insert_row=insert_row,
        )
        self.notebook_copies = NotebookCopyService(
            store=self.sharing_store,
            catalog=self.catalog,
            seams=self.seams,
            storage_dir=storage_dir,
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
        from app.services.communities import CommunityQueryService
        from app.services.report_engine import ReportEngine, ReportEngineDependencies

        def engine_factory(*, user_id: str, cancel_event=None) -> ReportEngine:
            retrieval_port = retrieval()
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
                communities=CommunityQueryService(
                    notebooks=self.notebook_store,
                    unified_kg=self.unified_kg,
                    event_log=self.event_log,
                    sibling_min_bridge=self.settings.sibling_min_bridge,
                ),
                settings=self.settings,
                event_log=self.event_log,
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

        Ports: the Task-22 ask-state store (prepare_turn/save_answer with
        EXPLICIT user_id), the Task-21 retrieval + evidence-context services,
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
            from app.services.communities import CommunityQueryService

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
                communities=lambda: CommunityQueryService(
                    notebooks=self.notebook_store,
                    unified_kg=self.unified_kg,
                    event_log=self.event_log,
                    sibling_min_bridge=self.settings.sibling_min_bridge,
                ),
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
                current_user_id=lambda: self.identity.current_user().id,
                cancellations=self.ask_cancellations,
            )
        return self.ask

    @property
    def ask_component(self) -> AskService:
        return self.ask_service()

    def wire_maintenance(self, *, retrieval: Callable[[], Any]) -> Any:
        if self.maintenance is None:
            from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter

            self.maintenance = SQLiteMaintenanceAdapter(self, retrieval=retrieval)
        return self.maintenance

    @property
    def maintenance_component(self) -> Any:
        if self.maintenance is None:
            return self.wire_maintenance(retrieval=lambda: self.retrieval_component)
        return self.maintenance
