"""Backend-neutral composition contracts for repository persistence stores."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.config import Settings
from app.repositories.ports import (
    AgentObservationStorePort,
    AgentProfileStorePort,
    AskStateStorePort,
    CatalogStorePort,
    ChunkStorePort,
    EmbeddingStorePort,
    ExtensionToggleStorePort,
    GovernanceStorePort,
    GroupStorePort,
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
    RetrievalExperienceStorePort,
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
    groups: GroupStorePort
    sources: SourceStorePort
    chunks: ChunkStorePort
    embeddings: EmbeddingStorePort
    knowledge: KnowledgeStorePort
    governance: GovernanceStorePort
    index_projection: IndexProjectionStorePort
    kg_build_jobs: KgBuildJobStorePort
    catalog: CatalogStorePort
    knowhow: KnowhowStorePort
    knowhow_history: KnowhowHistoryStorePort
    knowhow_transfer: KnowhowTransferStorePort
    memory: MemoryStorePort
    queries: QueryStorePort
    reports: ReportStorePort
    ask_state: AskStateStorePort
    unified_kg: UnifiedKgStorePort
    model_status: ModelStatusStorePort
    agent_profile: AgentProfileStorePort
    retrieval_experiences: RetrievalExperienceStorePort
    agent_observations: AgentObservationStorePort
    extension_toggles: ExtensionToggleStorePort


class PersistenceBundleFactory(Protocol):
    def create(
        self,
        *,
        settings: Settings,
        root_dir: Path,
        seams: RepositorySeams,
    ) -> PersistenceBundle: ...
