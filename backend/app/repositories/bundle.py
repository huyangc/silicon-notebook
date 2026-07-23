"""Backend-neutral composition contracts for repository persistence stores."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.config import Settings
from app.repositories.ports import (
    AskStateStorePort,
    ChunkStorePort,
    EmbeddingStorePort,
    GovernanceStorePort,
    IdentityStorePort,
    IndexProjectionStorePort,
    KgBuildJobStorePort,
    KnowhowHistoryStorePort,
    KnowhowStorePort,
    KnowhowTransferStorePort,
    KnowledgeStorePort,
    MemoryStorePort,
    ModelStatusStorePort,
    NotebookStorePort,
    QueryStorePort,
    RepositoryDatabasePort,
    RepositorySeams,
    ReportStorePort,
    SharingStorePort,
    SourceStorePort,
    UnifiedKgStorePort,
)


@runtime_checkable
class PersistenceBundle(Protocol):
    database: RepositoryDatabasePort
    identity: IdentityStorePort
    notebooks: NotebookStorePort
    sharing: SharingStorePort
    sources: SourceStorePort
    chunks: ChunkStorePort
    embeddings: EmbeddingStorePort
    knowledge: KnowledgeStorePort
    governance: GovernanceStorePort
    index_projection: IndexProjectionStorePort
    kg_build_jobs: KgBuildJobStorePort
    knowhow: KnowhowStorePort
    knowhow_history: KnowhowHistoryStorePort
    knowhow_transfer: KnowhowTransferStorePort
    memory: MemoryStorePort
    queries: QueryStorePort
    reports: ReportStorePort
    ask_state: AskStateStorePort
    unified_kg: UnifiedKgStorePort
    model_status: ModelStatusStorePort


class PersistenceBundleFactory(Protocol):
    def create(
        self,
        *,
        settings: Settings,
        root_dir: Path,
        seams: RepositorySeams,
    ) -> PersistenceBundle: ...
