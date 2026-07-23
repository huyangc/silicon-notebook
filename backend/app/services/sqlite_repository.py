"""SQLite compatibility wrapper around the backend-neutral repository facade."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Iterable, List, Optional

from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS
from app.core.config import Settings
from app.core.request_context import (
    _REQUEST_USER,
    reset_request_user,
    set_request_user,
)
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.bundle import SqlitePersistenceBundleFactory
from app.repositories.sqlite.governance_store import GovernanceStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator
from app.repositories.sqlite.sharing_store import SharingStore
from app.services.knowledge_contracts import (
    KNOWLEDGE_STATUSES,
    KnowledgeGraphTooLargeError,
    USABLE_STATUSES,
)
from app.services.knowledge_lifecycle import _concept_desc_sig, _fast_loads
from app.services.mineru_client import MinerUClient
from app.services.mineru_cloud_client import MinerUCloudClient, MinerUCloudNotConfigured
from app.services.parsers import parse_source_file
from app.services.repository import UploadedSourceFile
from app.services.repository_facade import (
    ChunkRetrievalPlan,
    RepositoryFacade,
    _COPY_CHUNK,
    _new_id,
    _now,
)
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_notebook_sharing import _remap_json_ids


class SQLiteRepository(RepositoryFacade):
    """Preserve the historical SQLite API while isolating backend concerns."""

    def __init__(
        self, settings: Settings, *, model_provider: Any | None = None
    ) -> None:
        super().__init__(
            settings,
            SqlitePersistenceBundleFactory(),
            model_provider=model_provider,
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrator = SqliteMigrator(self._runtime.database, self.settings)
        self._migrator.initialize()

    @property
    def db_path(self) -> Path:
        return self._runtime.database.db_path

    @db_path.setter
    def db_path(self, value: Path) -> None:
        self._runtime.database.db_path = value

    @property
    def _write_lock(self):
        return self._runtime.database.write_lock

    @_write_lock.setter
    def _write_lock(self, value) -> None:
        self._runtime.database.write_lock = value

    def _connect(self) -> sqlite3.Connection:
        return self._runtime.database.connect()

    def close_local(self) -> None:
        """关闭并清除当前线程的复用 SQLite 连接。"""
        self._runtime.database.close_local()

    def _clear_source_extraction_state(
        self,
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        return super()._clear_source_extraction_state(
            db,
            source_id,
            notebook_id,
            clear_embeddings=clear_embeddings,
        )

    def _knowledge_objects(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]] = USABLE_STATUSES,
        id_filter: Optional[Iterable[str]] = None,
    ) -> List[dict]:
        return super()._knowledge_objects(
            db,
            notebook_id,
            object_type,
            statuses,
            id_filter,
        )

    def _migrate(self) -> list[int]:
        return self._migrator.migrate()

    def _migrate_legacy(self) -> list[int]:
        return self._migrator.migrate()

    @staticmethod
    def _add_column_if_missing(db, table: str, column: str, coldef: str) -> None:
        return SqliteMigrator.add_column_if_missing(db, table, column, coldef)

    def _run_migration(self, version: int) -> None:
        return getattr(self._migrator, f"_migration_{version}")()

    def _migration_1(self) -> None:
        return self._run_migration(1)

    def _migration_2(self) -> None:
        return self._run_migration(2)

    def _migration_3(self) -> None:
        return self._run_migration(3)

    def _migration_4(self) -> None:
        return self._run_migration(4)

    def _migration_5(self) -> None:
        return self._run_migration(5)

    def _migration_6(self) -> None:
        return self._run_migration(6)

    def _migration_7(self) -> None:
        return self._run_migration(7)

    def _migration_8(self) -> None:
        return self._run_migration(8)

    def _migration_9(self) -> None:
        return self._run_migration(9)

    def _migration_10(self) -> None:
        return self._run_migration(10)

    def _migration_19(self) -> None:
        return self._run_migration(19)

    def _migration_27(self) -> None:
        return self._run_migration(27)

    def _recover_interrupted_jobs(self) -> None:
        return self._migrator.recover_interrupted_jobs()

    def _recover_interrupted_jobs_legacy(self) -> None:
        return self._migrator.recover_interrupted_jobs()

    def _seed(self) -> None:
        return self._migrator.seed()

    def _seed_legacy(self) -> None:
        return self._migrator.seed()

    @property
    def maintenance(self):
        adapter = self.__dict__.get("_maintenance")
        if adapter is None:
            adapter = SQLiteMaintenanceAdapter(
                self._runtime,
                retrieval=lambda: self.retrieval,
            )
            self.__dict__["_maintenance"] = adapter
        return adapter

    def eval_insert_source_for_test(
        self, nb_id: str, name: str, text: str, tmpdir: str
    ) -> str:
        return self.maintenance.eval_insert_source_for_test(
            nb_id, name, text, tmpdir
        )

    def _backfill_relation_embeddings(self, notebook_id: str) -> None:
        return self.maintenance.backfill_relation_embeddings(notebook_id)

    @staticmethod
    def _source_ids_from_evidence(evidence_json: Optional[str]) -> set:
        return KnowledgeStore.source_ids_from_evidence(evidence_json)

    @staticmethod
    def _delete_knowledge_object_sources(
        db: object, object_ids: List[str]
    ) -> None:
        return KnowledgeStore.delete_object_sources(db, object_ids)

    @staticmethod
    def _insert_row(db, table: str, data: dict) -> None:
        return SharingStore.insert_row_values(db, table, data)

    @staticmethod
    def _seed_fn_for(object_type: str):
        return GovernanceStore.seed_for(object_type)

    @staticmethod
    def _merge_evidence_lists(base_ev: list, src_ev: list) -> list:
        return GovernanceStore.merge_evidence(base_ev, src_ev)

    @staticmethod
    def _read_ask_trace(db, job_id: str) -> list:
        return AskStateStore.read_trace(db, job_id)
