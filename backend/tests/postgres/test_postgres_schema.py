from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
from tests.postgres.conftest import (
    _database_catalog,
    _url_with_search_path,
    _validate_database_catalog,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_PATH = (
    REPO_ROOT / "backend" / "app" / "repositories" / "postgres" / "migrations"
)
TEST_SCHEMA_PATTERN = re.compile(r"^sn_test_[0-9a-f]{32}$")


@pytest.mark.postgres_integration
def test_schema_on_utf8_database_with_non_c_default_collation(
    postgres_non_c_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_non_c_database).migrate() == 39
    with postgres_non_c_database.connect() as conn:
        row = conn.execute(
            "SELECT current_database() AS database, "
            "current_setting('server_encoding') AS encoding, "
            "to_jsonb(d) AS catalog FROM pg_database AS d "
            "WHERE datname=current_database()"
        ).fetchone()
    catalog = _database_catalog(row)
    _validate_database_catalog(catalog, expected="non-c")
    assert catalog.encoding == "UTF8"
    assert catalog.provider == "i"
    assert catalog.provider_locale == "en-US"


@pytest.mark.postgres_integration
def test_packaged_migrations_are_idempotent_from_empty_schema(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    migrator = PostgresMigrator(postgres_database)
    assert migrator.current_version() == 0
    assert migrator.migrate() == 39
    assert migrator.migrate() == 39
    assert migrator.current_version() == 39
    assert POSTGRES_SCHEMA_MANIFEST.postgres_version == 39


@pytest.mark.postgres_integration
def test_packaged_migration_checksum_drift_is_rejected(postgres_database, tmp_path):
    from app.repositories.postgres.migrator import PostgresMigrator, load_migrations

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate() == 39

    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_PATH, copied)
    first = copied / "0001_initial.sql"
    first.write_text(first.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")
    changed = PostgresMigrator(postgres_database, migrations=load_migrations(copied))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        changed.migrate()


@pytest.mark.postgres_integration
def test_pg_trgm_is_shared_outside_disposable_schema_lifetimes(postgres_scope):
    import psycopg
    from psycopg import sql

    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    schemas = [f"sn_test_{uuid.uuid4().hex}" for _ in range(2)]
    assert all(TEST_SCHEMA_PATTERN.fullmatch(schema) for schema in schemas)
    databases = []
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        for schema in schemas:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        for schema in schemas:
            settings = Settings(
                database_url=_url_with_search_path(postgres_scope.base_url, schema),
                postgres_pool_min_size=1,
                postgres_pool_max_size=1,
            )
            databases.append(PostgresDatabase(settings, REPO_ROOT))

        barrier = threading.Barrier(2)
        versions: list[int] = []
        failures: list[BaseException] = []

        def migrate(database) -> None:
            try:
                barrier.wait(timeout=5)
                versions.append(PostgresMigrator(database).migrate())
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        workers = [
            threading.Thread(target=migrate, args=(database,)) for database in databases
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        assert not any(worker.is_alive() for worker in workers)
        assert failures == []
        assert sorted(versions) == [
            POSTGRES_SCHEMA_MANIFEST.postgres_version,
            POSTGRES_SCHEMA_MANIFEST.postgres_version,
        ]

        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            extension_schema = conn.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchone()[0]
            assert extension_schema == "public"
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schemas[0]))
            )

        with databases[1].connect() as conn:
            remaining = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() "
                "AND indexname='idx_chunks_text_trgm'"
            ).fetchone()
            extension_schema = conn.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchone()["nspname"]
        assert remaining == {"indexname": "idx_chunks_text_trgm"}
        assert extension_schema == "public"
        assert PostgresMigrator(databases[1]).migrate() == 39
    finally:
        for database in databases:
            database.close()
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            for schema in schemas:
                if TEST_SCHEMA_PATTERN.fullmatch(schema) is None:
                    raise RuntimeError("refusing to drop an unvalidated PostgreSQL schema")
                exists = conn.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
                ).fetchone()
                if exists is not None:
                    conn.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                    )


def test_packaged_index_migration_phases_are_exact():
    from app.repositories.postgres.migrator import load_migrations

    migrations = {migration.version: migration for migration in load_migrations(MIGRATIONS_PATH)}
    assert [(version, migrations[version].name) for version in migrations] == [
        (1, "initial"),
        (2, "integrity_indexes"),
        (3, "core_indexes"),
        (4, "knowledge_indexes"),
        (5, "memory_knowhow_governance_indexes"),
        (6, "search_gin"),
        (7, "cluster_membership_unique"),
        (8, "master_v28_features"),
        (9, "sources_file_hash_index"),
        (10, "report_understanding"),
        (11, "relation_endpoint_keyset_indexes"),
        (12, "relation_completion_state"),
        (13, "ask_job_asked_at"),
        (14, "kg_analysis_precompute"),
        (15, "source_element_type_index"),
        (16, "visible_source_identity_index"),
        (17, "command_catalog"),
        (18, "source_local_facts"),
        (19, "source_fact_backfills"),
        (20, "source_index_backfills"),
        (21, "report_share_tokens"),
        (22, "chunk_questions"),
        (23, "user_profiles_ui_mode"),
        (24, "chunk_elements"),
        (25, "notebook_object_schemas"),
        (26, "source_agent_profile_id"),
        (27, "group_sharing"),
        (28, "share_requests"),
        (29, "agent_profile"),
        (30, "conversation_share"),
        (31, "agent_profile_claim_token"),
        (32, "retrieval_experiences"),
        (33, "agent_observations"),
        (34, "group_owner"),
        (35, "group_invite"),
        (36, "pluggable_indexing_pipeline"),
        (37, "indexing_pipeline_staging"),
        (38, "agent_observation_kind"),
        (39, "hotpath_batch1_indexes"),
    ]

    def index_declarations(version: int) -> list[tuple[bool, str]]:
        # ``IF NOT EXISTS`` is optional here (only migration 39 uses it, for
        # its no-op-once-the-offline-builder-already-ran semantics) — without
        # skipping it the capture group would grab the literal word "IF" as
        # the index name instead of the real one.
        return [
            (bool(unique), name)
            for unique, name in re.findall(
                r"(?mi)^CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)",
                migrations[version].sql,
            )
        ]

    assert index_declarations(1) == []
    assert index_declarations(2) == [
        (True, name)
        for name in (
            "idx_kg_build_jobs_one_running",
            "idx_memory_answer_once",
            "idx_notebooks_share_token",
            "idx_promotion_object",
            "idx_sources_memory_id",
            "idx_users_username",
        )
    ]
    operational = [
        declaration
        for version in (3, 4, 5, 8)
        for declaration in index_declarations(version)
    ]
    assert len(operational) == 76
    assert not any(unique for unique, _name in operational)
    gin_names = {
        "idx_chunks_text_trgm",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_title_trgm",
        "idx_memory_items_content_md_trgm",
        "idx_memory_items_tags_trgm",
    }
    gin_declarations = index_declarations(6)
    assert len(gin_declarations) == 5
    assert not any(unique for unique, _name in gin_declarations)
    assert {name for _unique, name in gin_declarations} == gin_names
    assert all(
        "USING gin" in line
        for line in re.findall(r"(?mis)^CREATE INDEX idx_.*?;", migrations[6].sql)
    )
    cluster_unique = index_declarations(7)
    assert cluster_unique == [(True, "uq_clusters_notebook_type_member")]

    # Migration 8 carries canonical scratch plus knowhow history indexes.
    v28_feature_indexes = index_declarations(8)
    assert v28_feature_indexes == [
        (False, "idx_kg_canonical_scratch_nb_run_seed"),
        (False, "idx_knowhow_changes_table"),
        (False, "idx_knowhow_milestones_table"),
    ]

    # Migration 9 installs the notebook/file-hash dedup lookup index.
    v30_index = index_declarations(9)
    assert v30_index == [(False, "idx_sources_notebook_file_hash")]

    # Migration 11 installs stable relation-endpoint keyset indexes.
    v33_indexes = index_declarations(11)
    assert v33_indexes == [
        (False, "idx_knowledge_relations_nb_source_id"),
        (False, "idx_knowledge_relations_nb_target_id"),
    ]

    # Migration 12 installs the source-local completion keyset and pending-state lookup.
    v12_indexes = index_declarations(12)
    assert v12_indexes == [
        (False, "idx_knowledge_objects_source_id"),
        (False, "idx_kg_relation_completion_state_nb_status"),
    ]

    # Migration 14 mirrors SQLite v36's three KG-quality-analysis precompute
    # product tables. Only one of them needs an explicit index: the other two are
    # read by their primary-key prefix, and PostgreSQL (like SQLite) already
    # backs a declared primary key with its own index.
    v14_indexes = index_declarations(14)
    assert v14_indexes == [(False, "idx_kg_source_profiles_nb_mainstream")]

    # Migration 15 installs the per-source, per-element-type keyset ordering
    # backing bounded collection enumeration (formula/table/image/code_block).
    v37_index = index_declarations(15)
    assert v37_index == [(False, "idx_source_elements_source_type")]

    assert index_declarations(25) == [
        (False, "idx_notebook_object_schemas_status")
    ]

    # Migration 26 pairs SQLite v48's sources.agent_profile_id provenance
    # column. It declares NO index on purpose: the Agent-facing delete check is
    # a single-row primary-key read, and nothing enumerates an agent's sources.
    assert index_declarations(26) == []

    # Migration 16 pairs SQLite v38's bounded visible-source identity roster.
    v38_index = index_declarations(16)
    assert v38_index == [(False, "idx_sources_visible_identity")]

    # Migration 17 mirrors SQLite v39's command-catalog tables. The partial
    # unique index is the cross-process single-flight guard and its predicate
    # must cover queued AND running: the job row is written before the worker
    # thread starts, so a running-only guard would admit a second writer for the
    # same candidate set. The last entry is the R14 P2 addition and the only one
    # on a pre-existing table: it backs the bounded by-title target resolution
    # (`knowhow_table_id_by_title`) that runs inside the locked apply window.
    v39_indexes = index_declarations(17)
    assert v39_indexes == [
        (False, "idx_catalog_candidates_nb"),
        (False, "idx_catalog_candidates_source"),
        (True, "idx_catalog_jobs_one_active"),
        (False, "idx_catalog_jobs_source_created"),
        (False, "idx_catalog_jobs_nb_created"),
        (False, "idx_catalog_candidates_job_state"),
        (False, "idx_knowhow_tables_nb_title"),
    ]
    assert "WHERE status IN ('queued', 'running')" in migrations[17].sql
    # Column order is the contract, not just the name: (notebook_id, title) are
    # the equality seek and (created_at, id) ARE the tie-break the point lookup
    # orders by, so `LIMIT 1` resolves without a sort node.
    assert (
        "ON knowhow_tables(notebook_id, title, created_at, id)"
        in migrations[17].sql
    )

    v40_indexes = index_declarations(18)
    assert v40_indexes == [
        (False, "idx_knowledge_source_facts_source_generation"),
        (True, "uq_knowledge_source_facts_generation_local"),
        (False, "idx_knowledge_source_facts_notebook_object"),
        (False, "idx_knowledge_source_fact_elements_source"),
    ]
    assert index_declarations(19) == [
        (False, "idx_knowledge_source_fact_backfills_notebook"),
        (False, "idx_kos_source_object"),
        (False, "idx_knowledge_source_facts_source_generation_global"),
    ]
    assert "projection_origin text COLLATE \"C\" NOT NULL DEFAULT 'live'" in migrations[19].sql
    assert "incomplete_reason text COLLATE \"C\" NOT NULL DEFAULT ''" in migrations[19].sql
    assert (
        "source_id, source_generation, projection_origin, global_object_id"
        in migrations[19].sql
    )
    assert index_declarations(20) == [
        (False, "idx_source_index_backfills_status"),
    ]
    assert "failure_code text COLLATE \"C\" NOT NULL DEFAULT ''" in migrations[20].sql
    assert "status IN ('running','complete','failed')" in migrations[20].sql

    # Migration 22 mirrors SQLite v44's optional generated-question index.
    assert index_declarations(22) == [
        (False, "idx_chunk_questions_nb"),
        (False, "idx_chunk_questions_source"),
    ]

    # Migration 23 is the nullable user_profiles.ui_mode column: no index.
    assert index_declarations(23) == []

    # Migration 24 mirrors SQLite v46's element -> chunk reverse index. The
    # composite primary key IS the (notebook_id, element_id) seek index, so the
    # only declared index is the one serving the cascade from chunks — plus the
    # backfill ledger's status lookup.
    assert index_declarations(24) == [
        (False, "idx_chunk_elements_chunk"),
        (False, "idx_chunk_element_backfills_status"),
    ]
    assert (
        "PRIMARY KEY (notebook_id, element_id, chunk_id)" in migrations[24].sql
    )
    assert "REFERENCES chunks(id) ON UPDATE NO ACTION ON DELETE CASCADE" in (
        migrations[24].sql
    )
    assert "status IN ('running','complete','failed')" in migrations[24].sql
    assert (
        "ADD COLUMN chunk_elements_indexed bigint NOT NULL DEFAULT 0"
        in migrations[24].sql
    )

    # Migration 36 mirrors SQLite v58. Desired selection and its generation
    # live on the notebook, while readers keep using the independently
    # published product identity until the whole-notebook swap succeeds.
    assert index_declarations(36) == []
    assert "ADD COLUMN indexing_pipeline text COLLATE \"C\"" in migrations[36].sql
    assert "ADD COLUMN indexing_pipeline_generation text COLLATE \"C\"" in (
        migrations[36].sql
    )
    assert "ADD COLUMN indexing_pipeline_id text COLLATE \"C\"" in (
        migrations[36].sql
    )

    # Migration 39 (hot-path fix batch 1) — six query-family groups (eight
    # indexes) a production audit found scanning without one; see
    # migrations/0039_hotpath_batch1_indexes.sql's header comment for the full
    # per-group evidence. Pure additions: no table, column, or FK changes.
    v39_hotpath_indexes = index_declarations(39)
    assert v39_hotpath_indexes == [
        (False, "idx_clusters_nb_canonical"),
        (False, "idx_clusters_nb_canonical_name_lower"),
        (False, "idx_extraction_runs_notebook"),
        (False, "idx_knowledge_source_fact_elements_notebook"),
        (False, "idx_memory_items_notebook"),
        (False, "idx_knowledge_relations_nb_source_target_edge"),
        (False, "idx_chunks_source_ordinal"),
        (False, "idx_sources_nb_hidden_type"),
    ]
    # The migration's own header comment walks through each index's column
    # list in prose (e.g. "ON concept_clusters(notebook_id, canonical_id)")
    # as part of explaining which query family it serves, so a bare
    # substring assertion against the raw file text could pass on the
    # comment alone even if the real DDL below it were mangled. Strip
    # ``--``-prefixed lines first so these assertions can only be satisfied
    # by the actual CREATE INDEX statements.
    v39_ddl_only = "\n".join(
        line for line in migrations[39].sql.splitlines()
        if not line.strip().startswith("--")
    )
    assert (
        "ON concept_clusters(notebook_id, canonical_id)" in v39_ddl_only
    )
    assert (
        "ON concept_clusters(notebook_id, lower(canonical_name))"
        in v39_ddl_only
    )
    assert (
        "ON knowledge_relations(notebook_id, source_object_id, target_object_id, edge_type)"
        in v39_ddl_only
    )
    assert "ON chunks(source_id, ordinal)" in v39_ddl_only
    # Partial, not full: the complementary NOT IN majority-case predicate is
    # deliberately left unindexed (see the migration's own header comment).
    assert "WHERE source_type IN ('memory', 'knowhow')" in v39_ddl_only


def test_source_index_running_timestamp_maps_to_postgres_null():
    from app.migration.shadow.manifest import MANIFEST
    from app.migration.shadow.transform import PostgresColumn, transform_sqlite_value

    spec = next(
        table for table in MANIFEST.tables
        if table.name == "source_index_backfills"
    )
    column = PostgresColumn(
        name="completed_at", data_type="timestamp with time zone", nullable=True
    )

    assert transform_sqlite_value(spec, column, "") is None


def test_agent_profile_jobs_empty_timestamps_map_to_postgres_null():
    from app.migration.shadow.manifest import MANIFEST
    from app.migration.shadow.transform import PostgresColumn, transform_sqlite_value

    spec = next(
        table for table in MANIFEST.tables
        if table.name == "agent_profile_jobs"
    )
    for column_name in ("started_at", "finished_at"):
        column = PostgresColumn(
            name=column_name, data_type="timestamp with time zone", nullable=True
        )
        assert transform_sqlite_value(spec, column, "") is None


def test_catalog_jobs_empty_finished_at_maps_to_postgres_null():
    from app.migration.shadow.manifest import MANIFEST
    from app.migration.shadow.transform import PostgresColumn, transform_sqlite_value

    spec = next(
        table for table in MANIFEST.tables
        if table.name == "catalog_jobs"
    )
    column = PostgresColumn(
        name="finished_at", data_type="timestamp with time zone", nullable=True
    )
    assert transform_sqlite_value(spec, column, "") is None


def test_initial_migration_guards_utf8_before_business_ddl():
    from app.repositories.postgres.migrator import load_migrations

    initial = load_migrations(MIGRATIONS_PATH)[0].sql
    guard_position = initial.index("current_setting('server_encoding')")
    first_business_ddl = initial.index("CREATE TABLE agent_access_tokens")
    assert guard_position < first_business_ddl
    assert "server_encoding must be UTF8" in initial
