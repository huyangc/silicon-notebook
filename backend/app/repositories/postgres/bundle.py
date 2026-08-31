"""The single construction root for PostgreSQL persistence components."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.repositories.bundle import PersistenceBundle
from app.repositories.ports import RepositorySeams
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp
from app.repositories.postgres.agent_observation_store import AgentObservationStore
from app.repositories.postgres.agent_profile_store import AgentProfileStore
from app.repositories.postgres.ask_state_store import AskStateStore
from app.repositories.postgres.catalog_store import CatalogStore
from app.repositories.postgres.chunk_store import ChunkStore
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.embedding_store import EmbeddingStore
from app.repositories.postgres.extension_toggle_store import ExtensionToggleStore
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.group_store import GroupStore
from app.repositories.postgres.identity_store import IdentityStore
from app.repositories.postgres.index_projection_store import IndexProjectionStore
from app.repositories.postgres.kg_build_job_store import KgBuildJobStore
from app.repositories.postgres.knowhow_history_store import KnowhowHistoryStore
from app.repositories.postgres.knowhow_store import KnowhowStore
from app.repositories.postgres.knowhow_transfer_store import KnowhowTransferStore
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.model_status_store import ModelStatusStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.notebook_store import NotebookStore
from app.repositories.postgres.query_store import QueryStore
from app.repositories.postgres.report_store import ReportStore
from app.repositories.postgres.retrieval_experience_store import (
    RetrievalExperienceStore,
)
from app.repositories.postgres.sharing_store import SharingStore
from app.repositories.postgres.source_store import SourceStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from app.repositories.postgres.wish_store import WishStore
from app.domain.extraction_profiles import (
    LIST_FIELDS,
    OBJECT_SCHEMAS,
    OBJECT_TYPE_LABELS,
)


def _not_wired(*_args: object, **_kwargs: object) -> Any:
    raise RuntimeError("PostgreSQL persistence callback used before runtime wiring")


def _in_batches(ids: object, seams: RepositorySeams):
    values = list(ids)  # type: ignore[arg-type]
    size = seams.in_chunk_size()
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _initialize(
    database: PostgresDatabase, settings: Settings, seams: RepositorySeams
) -> None:
    """Migrate, then seed bootstrap metadata.

    This runs before any store is constructed. The only account inserted into
    a fresh database is the built-in admin; no notebook/source/demo data is
    ever created here.
    """
    PostgresMigrator(database).migrate()
    now = normalize_timestamp(seams.now())
    from app.domain.auth_utils import hash_password
    from app.domain.kg.names import normalize_name as whitelist_norm

    password_hash, password_salt, password_iterations = hash_password(
        settings.admin_password
    )
    whitelist = (
        "VCO", "PLL", "LNA", "BJT", "MOS", "MOSFET", "CMOS", "FET",
        "NMOS", "PMOS", "BiCMOS", "JFET", "op amp", "ADC", "DAC",
        "CMRR", "PSRR", "ESD", "PVT", "AC", "DC", "IC", "RF", "IF", "LO",
    )
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,'curator','active',%s,%s) ON CONFLICT DO NOTHING",
            (
                "user-local",
                settings.single_user_email,
                settings.single_user_name,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO user_profiles "
            "(id,user_id,memory_mode,domain_focus,created_at,updated_at) "
            "VALUES ('profile-local','user-local','manual',%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (jsonb(["Analog IC", "Packaging", "Reliability"]), now, now),
        )
        connection.execute(
            "UPDATE users SET role='admin',username='admin',password_hash=%s,"
            "password_salt=%s,password_iterations=%s,updated_at=%s "
            "WHERE id='user-local'",
            (password_hash, password_salt, password_iterations, now),
        )
        for term in whitelist:
            connection.execute(
                "INSERT INTO concept_whitelist(term,note,created_at) "
                "VALUES (%s,'builtin',%s) ON CONFLICT DO NOTHING",
                (whitelist_norm(term), now),
            )
        for object_type, schema in OBJECT_SCHEMAS.items():
            connection.execute(
                "INSERT INTO object_schemas "
                "(object_type,plural,fields,primary_field,description,label,"
                "list_fields,source,status,rationale,notebook_id,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'builtin','active','','',%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    object_type,
                    schema.plural,
                    jsonb(schema.fields),
                    schema.primary,
                    schema.description,
                    OBJECT_TYPE_LABELS.get(object_type, object_type),
                    jsonb([field for field in schema.fields if field in LIST_FIELDS]),
                    now,
                    now,
                ),
            )

@dataclass(frozen=True)
class PostgresPersistenceBundle(PersistenceBundle):
    database: PostgresDatabase
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
    wishes: WishStore


class PostgresPersistenceBundleFactory:
    def __init__(self) -> None:
        self._database: PostgresDatabase | None = None

    def close(self) -> None:
        if self._database is not None:
            self._database.close()

    def create(
        self,
        *,
        settings: Settings,
        root_dir: Path,
        seams: RepositorySeams,
    ) -> PostgresPersistenceBundle:
        database = PostgresDatabase(settings, root_dir)
        self._database = database
        try:
            _initialize(database, settings, seams)
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
            retrieval_experiences = RetrievalExperienceStore(
                database, now=seams.now
            )
            agent_observations = AgentObservationStore(
                database, new_id=seams.new_id, now=seams.now
            )
            extension_toggles = ExtensionToggleStore(database)
            wishes = WishStore(database, new_id=seams.new_id, now=seams.now)
            return PostgresPersistenceBundle(
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
                wishes=wishes,
            )
        except BaseException:
            database.close()
            raise
