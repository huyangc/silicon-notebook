from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core import ask_context
from app.core.config import Settings
from app.core.event_logging import EventLogger, llm_log_dir_aligned
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.query_store import QueryStore
from app.services.model_provider import RuntimeModelProvider
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery


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
