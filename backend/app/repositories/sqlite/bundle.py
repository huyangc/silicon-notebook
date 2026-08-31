"""The single construction root for SQLite persistence components."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.repositories.bundle import PersistenceBundle
from app.repositories.ports import RepositorySeams
from app.repositories.sqlite.agent_observation_store import AgentObservationStore
from app.repositories.sqlite.agent_profile_store import AgentProfileStore
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.catalog_store import CatalogStore
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.extension_toggle_store import ExtensionToggleStore
from app.repositories.sqlite.governance_store import GovernanceStore
from app.repositories.sqlite.group_store import GroupStore
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.index_projection_store import IndexProjectionStore
from app.repositories.sqlite.kg_build_job_store import KgBuildJobStore
from app.repositories.sqlite.knowhow_history_store import KnowhowHistoryStore
from app.repositories.sqlite.knowhow_store import KnowhowStore
from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.memory_store import MemoryStore
from app.repositories.sqlite.model_status_store import ModelStatusStore
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.report_store import ReportStore
from app.repositories.sqlite.retrieval_experience_store import (
    RetrievalExperienceStore,
)
from app.repositories.sqlite.sharing_store import SharingStore
from app.repositories.sqlite.source_store import SourceStore
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore


def _not_wired(*_args: object, **_kwargs: object) -> Any:
    raise RuntimeError("SQLite persistence callback used before runtime wiring")


def _in_batches(ids: object, seams: RepositorySeams):
    values = list(ids)  # type: ignore[arg-type]
    size = seams.in_chunk_size()
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


@dataclass(frozen=True)
class SqlitePersistenceBundle(PersistenceBundle):
    database: SqliteDatabase
    identity: IdentityStore
    notebooks: NotebookStore
    sharing: SharingStore
    groups: GroupStore
    sources: SourceStore
    chunks: ChunkStore
    embeddings: EmbeddingStore
    knowledge: KnowledgeStore
    governance: GovernanceStore
    index_projection: IndexProjectionStore
    kg_build_jobs: KgBuildJobStore
    catalog: CatalogStore
    knowhow: KnowhowStore
    knowhow_history: KnowhowHistoryStore
    knowhow_transfer: KnowhowTransferStore
    memory: MemoryStore
    queries: QueryStore
    reports: ReportStore
    ask_state: AskStateStore
    unified_kg: UnifiedKgStore
    model_status: ModelStatusStore
    agent_profile: AgentProfileStore
    retrieval_experiences: RetrievalExperienceStore
    agent_observations: AgentObservationStore
    extension_toggles: ExtensionToggleStore


class SqlitePersistenceBundleFactory:
    def create(
        self,
        *,
        settings: Settings,
        root_dir: Path,
        seams: RepositorySeams,
    ) -> SqlitePersistenceBundle:
        database = SqliteDatabase(settings, root_dir)
        identity = IdentityStore(database, settings)
        notebooks = NotebookStore(
            database,
            new_id=seams.new_id,
            now=seams.now,
            activity_retention_days=settings.user_activity_retention_days,
        )
        sharing = SharingStore(
            database,
            settings,
            now=seams.now,
            insert_row=SharingStore.insert_row_values,
        )
        sources = SourceStore(
            database,
            now=seams.now,
            current_user_id=lambda: identity.current_user().id,
        )
        chunks = ChunkStore(database)
        embeddings = EmbeddingStore(write=database.write)
        knowledge = KnowledgeStore(database, seams)
        governance = GovernanceStore(database, seams)
        index_projection = IndexProjectionStore(
            settings,
            connect=database.connect,
            in_batches=lambda ids: _in_batches(ids, seams),
            ent_chunk_map=_not_wired,
            mention_extra_edges=_not_wired,
            vector_matrix=_not_wired,
        )
        kg_build_jobs = KgBuildJobStore(
            database, new_id=seams.new_id, now=seams.now
        )
        catalog = CatalogStore(database, new_id=seams.new_id, now=seams.now)
        groups = GroupStore(database, new_id=seams.new_id, now=seams.now)
        knowhow = KnowhowStore(database, new_id=seams.new_id, now=seams.now)
        knowhow_history = KnowhowHistoryStore(
            database, new_id=seams.new_id, now=seams.now
        )
        knowhow_transfer = KnowhowTransferStore(database)
        memory = MemoryStore(database, new_id=seams.new_id, now=seams.now)
        queries = QueryStore(database, settings)
        reports = ReportStore(
            database,
            new_id=seams.new_id,
            now=seams.now,
            current_user_id=lambda: identity.current_user().id,
        )
        ask_state = AskStateStore(database, seams)
        unified_kg = UnifiedKgStore(database, seams.now)
        model_status = ModelStatusStore(database)
        agent_profile = AgentProfileStore(
            database, new_id=seams.new_id, now=seams.now
        )
        # No ``new_id`` seam: every id in retrieval_experiences is
        # content-addressed and computed by the service layer.
        retrieval_experiences = RetrievalExperienceStore(database, now=seams.now)
        agent_observations = AgentObservationStore(
            database, new_id=seams.new_id, now=seams.now
        )
        extension_toggles = ExtensionToggleStore(database)
        return SqlitePersistenceBundle(
            database=database,
            identity=identity,
            notebooks=notebooks,
            sharing=sharing,
            groups=groups,
            sources=sources,
            chunks=chunks,
            embeddings=embeddings,
            knowledge=knowledge,
            governance=governance,
            index_projection=index_projection,
            kg_build_jobs=kg_build_jobs,
            catalog=catalog,
            knowhow=knowhow,
            knowhow_history=knowhow_history,
            knowhow_transfer=knowhow_transfer,
            memory=memory,
            queries=queries,
            reports=reports,
            ask_state=ask_state,
            unified_kg=unified_kg,
            model_status=model_status,
            agent_profile=agent_profile,
            retrieval_experiences=retrieval_experiences,
            agent_observations=agent_observations,
            extension_toggles=extension_toggles,
        )
