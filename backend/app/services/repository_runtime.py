from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core import ask_context
from app.core.config import Settings
from app.core.event_logging import EventLogger, llm_log_dir_aligned
from app.repositories.source_files import SourceFileStore
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.governance_store import GovernanceStore
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.sharing_store import SharingStore
from app.repositories.sqlite.source_store import SourceStore
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
from app.services.kg_mutation import KgMutationCoordinator
from app.services.model_provider import RuntimeModelProvider
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery
from app.services.notebook_sharing import NotebookCopyService, NotebookSharingService
from app.services.schema_registry import SchemaRegistryService
from app.services.source_chunking import SourceChunkingService
from app.services.source_embedding import SourceEmbeddingService
from app.services.source_ingestion import SourceIngestionService


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
        self.notebook_summaries = NotebookSummaryQuery(self.database)
        self.catalog = NotebookCatalogService(
            store=self.notebook_store,
            summaries=self.notebook_summaries,
            queries=self.queries,
            identity=self.identity,
        )
        self.source_store = SourceStore(self.database, now=seams.now)
        self.chunk_store = ChunkStore(self.database)
        # Task 13: the three knowledge-domain persistence stores share the ONE
        # database boundary. Their primitives are connection-taking — the
        # facade keeps every transaction/connection boundary (its `_write` /
        # `_connect` compatibility seams stay the observable commit points),
        # so construction is eager and seam-free.
        self.knowledge = KnowledgeStore(self.database, seams)
        self.governance = GovernanceStore(self.database, seams)
        self.unified_kg = UnifiedKgStore(self.database)
        # Source file persistence resolves storage_dir through the database
        # boundary's resolve_path — no facade seams, so construction is eager.
        self.source_files = SourceFileStore(
            self.database.resolve_path(settings.storage_dir),
            resolve_path=self.database.resolve_path,
        )
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
        # The KG mutation coordinator is finished by wire_kg_mutations(): its
        # collaborators (the facade-owned unified/vector caches, the
        # auto-index once-set, the corpus-language memo and the facade `_write`
        # transaction seat) only exist once the facade constructor reaches
        # them.  Construction stays lazy — no seam calls.
        self.kg_mutations: "KgMutationCoordinator | None" = None
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
        store_kg: Callable[..., tuple],
        incremental_fuse_source: Callable[..., None],
        maybe_auto_index: Callable[[str], None],
        invalidate_unified_cache: Callable[[str], None],
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
        Task 13/15 replace them with KnowledgeStore /
        KnowledgeLifecycleService dependencies.  Requires
        wire_source_pipeline() first — the chunk/embed collaborators must
        already be composed."""
        if self.source_embedding is None or self.source_chunking is None:
            raise RuntimeError(
                "wire_source_ingestion requires wire_source_pipeline() first"
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
            store_kg=store_kg,
            incremental_fuse_source=incremental_fuse_source,
            maybe_auto_index=maybe_auto_index,
            invalidate_unified_cache=invalidate_unified_cache,
            notebook_meta_row=notebook_meta_row,
            notebook_meta_sources=notebook_meta_sources,
            apply_notebook_meta=apply_notebook_meta,
        )
        return self.source_ingestion

    def wire_kg_mutations(
        self,
        *,
        unified_cache: Any,
        vector_cache: Any,
        auto_index_checked: Any,
        notebook_languages: Any,
        write: Callable[[], Any],
    ) -> KgMutationCoordinator:
        """Compose the KG mutation coordinator (Task 14) once the facade-bound
        collaborators exist.  The caches/sets are the facade's EXISTING objects
        passed BY IDENTITY (never replacement copies — Task 17 transfers their
        ownership later); ``write`` is the facade's ``_write`` compatibility
        seat resolved per call, so the frozen transaction-phase traces and
        failure injections keep observing the dirty bump's commit boundary.
        The unified store and the `_now` clock seam come from this runtime."""
        self.kg_mutations = KgMutationCoordinator(
            self.unified_kg,
            unified_cache,
            vector_cache,
            auto_index_checked,
            notebook_languages,
            write=write,
            now=self.seams.now,
        )
        return self.kg_mutations

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
