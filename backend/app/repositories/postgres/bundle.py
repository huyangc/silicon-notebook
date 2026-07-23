"""The single construction root for PostgreSQL persistence components."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.repositories.bundle import PersistenceBundle
from app.repositories.ports import RepositorySeams
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp
from app.repositories.postgres.ask_state_store import AskStateStore
from app.repositories.postgres.chunk_store import ChunkStore
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.embedding_store import EmbeddingStore
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.identity_store import IdentityStore
from app.repositories.postgres.index_projection_store import IndexProjectionStore
from app.repositories.postgres.kg_build_job_store import KgBuildJobStore
from app.repositories.postgres.knowhow_store import KnowhowStore
from app.repositories.postgres.knowhow_transfer_store import KnowhowTransferStore
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.notebook_store import NotebookStore
from app.repositories.postgres.query_store import QueryStore
from app.repositories.postgres.report_store import ReportStore
from app.repositories.postgres.sharing_store import SharingStore
from app.repositories.postgres.source_store import SourceStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from app.services.extraction_profiles import (
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
    """Migrate, then atomically recover work and seed bootstrap metadata.

    This runs before any store is constructed. The only account inserted into
    a fresh database is the built-in admin; no notebook/source/demo data is
    ever created here.
    """
    PostgresMigrator(database).migrate()
    now = normalize_timestamp(seams.now())
    from app.services.auth_utils import hash_password
    from app.services.kg.filters import _norm as whitelist_norm

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

        # Preserve SQLite's restart recovery contract before traffic is ready.
        connection.execute(
            "UPDATE merge_review_jobs SET status='failed',"
            "error='中断:服务重启' WHERE status='running'"
        )
        connection.execute(
            "UPDATE ask_jobs SET status='interrupted',"
            "error='中断:服务重启' WHERE status='running'"
        )
        connection.execute(
            "UPDATE knowhow_rows SET projection_status='failed' "
            "WHERE projection_status IN ('syncing','pending')"
        )
        connection.execute(
            "UPDATE sources SET status='parsed',parse_status='parsed',"
            "error_message='',updated_at=%s WHERE parse_status='extracting'",
            (now,),
        )
        connection.execute(
            "UPDATE extraction_runs SET status='failed',"
            "error_message='worker_interrupted: 服务重启导致知识图谱分析中断',"
            "updated_at=%s WHERE run_type='kg' AND status='running'",
            (now,),
        )
        connection.execute(
            "UPDATE kg_build_jobs SET status='failed',stage='finished',"
            "error_code='worker_interrupted',"
            "error_message='服务重启导致本次分析中断；已完成内容已保留，可继续分析未完成内容。',"
            "updated_at=%s,finished_at=%s WHERE status='running'",
            (now, now),
        )


@dataclass(frozen=True)
class PostgresPersistenceBundle(PersistenceBundle):
    database: PostgresDatabase
    identity: IdentityStore
    notebooks: NotebookStore
    sharing: SharingStore
    sources: SourceStore
    chunks: ChunkStore
    embeddings: EmbeddingStore
    knowledge: KnowledgeStore
    governance: GovernanceStore
    index_projection: IndexProjectionStore
    kg_build_jobs: KgBuildJobStore
    knowhow: KnowhowStore
    knowhow_transfer: KnowhowTransferStore
    memory: MemoryStore
    queries: QueryStore
    reports: ReportStore
    ask_state: AskStateStore
    unified_kg: UnifiedKgStore


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
        model_config_cache: dict[str, object],
    ) -> PostgresPersistenceBundle:
        database = PostgresDatabase(settings, root_dir)
        self._database = database
        try:
            _initialize(database, settings, seams)
            identity = IdentityStore(database, settings, model_config_cache)
            notebooks = NotebookStore(database, new_id=seams.new_id, now=seams.now)
            sharing = SharingStore(
                database,
                settings,
                now=seams.now,
                insert_row=SharingStore.insert_row_values,
            )
            sources = SourceStore(database, now=seams.now)
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
            knowhow = KnowhowStore(database, new_id=seams.new_id, now=seams.now)
            knowhow_transfer = KnowhowTransferStore(database)
            memory = MemoryStore(database, new_id=seams.new_id, now=seams.now)
            queries = QueryStore(database)
            reports = ReportStore(
                database,
                new_id=seams.new_id,
                now=seams.now,
                current_user_id=lambda: identity.current_user().id,
            )
            ask_state = AskStateStore(database, seams)
            unified_kg = UnifiedKgStore(database, seams.now)
            return PostgresPersistenceBundle(
                database=database,
                identity=identity,
                notebooks=notebooks,
                sharing=sharing,
                sources=sources,
                chunks=chunks,
                embeddings=embeddings,
                knowledge=knowledge,
                governance=governance,
                index_projection=index_projection,
                kg_build_jobs=kg_build_jobs,
                knowhow=knowhow,
                knowhow_transfer=knowhow_transfer,
                memory=memory,
                queries=queries,
                reports=reports,
                ask_state=ask_state,
                unified_kg=unified_kg,
            )
        except BaseException:
            database.close()
            raise
