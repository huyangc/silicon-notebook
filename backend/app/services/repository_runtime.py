from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core import ask_context
from app.core.config import Settings
from app.core.event_logging import EventLogger, llm_log_dir_aligned
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.sharing_store import SharingStore
from app.repositories.sqlite.source_store import SourceStore
from app.services.model_provider import RuntimeModelProvider
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery
from app.services.notebook_sharing import NotebookCopyService, NotebookSharingService


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
        # Vector persistence is finished by wire_persistence(): its write seat
        # is the facade's `_write` compatibility seam, which only exists once
        # the facade constructor reaches it. Construction stays lazy.
        self.embedding_store: "EmbeddingStore | None" = None
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

    def wire_persistence(self, *, write: Callable[..., Any]) -> EmbeddingStore:
        """Compose the vector persistence (Task 10) once the facade-bound
        ``write`` seat exists: it is the facade's ``_write`` compatibility
        seam (itself delegating to the shared database write lock), resolved
        at call time so per-instance monkeypatches — transaction counting,
        failure injection — keep observing every vector flush."""
        self.embedding_store = EmbeddingStore(write=write)
        return self.embedding_store

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
