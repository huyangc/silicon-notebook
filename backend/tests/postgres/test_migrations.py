from __future__ import annotations

import math
import threading

import pytest

from tests.postgres.conftest import _safe_ascii_text


pytestmark = pytest.mark.postgres_integration


def _migration(version: int, sql: str, name: str | None = None):
    from app.repositories.postgres.migrator import PostgresMigration

    return PostgresMigration(
        version=version, name=name or f"migration_{version}", sql=sql
    )


def _migrator(database, migrations):
    from app.repositories.postgres.migrator import PostgresMigrator

    return PostgresMigrator(database, migrations=migrations)


def test_migrate_creates_ledger_and_is_idempotent(postgres_database):
    migrator = _migrator(
        postgres_database,
        [
            _migration(1, "CREATE TABLE migrated_one (id integer PRIMARY KEY)"),
            _migration(2, "ALTER TABLE migrated_one ADD COLUMN value text"),
        ],
    )

    assert migrator.current_version() == 0
    assert migrator.migrate() == 2
    assert migrator.migrate() == 2
    assert migrator.current_version() == 2

    with postgres_database.connect() as conn:
        rows = conn.execute(
            "SELECT version, checksum, applied_at "
            "FROM silicon_schema_migrations ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in rows] == [1, 2]
    assert all(len(row["checksum"]) == 64 for row in rows)
    assert all(row["applied_at"].utcoffset().total_seconds() == 0 for row in rows)


def test_migrate_can_stop_at_a_valid_target_then_resume(postgres_database):
    migrator = _migrator(
        postgres_database,
        [
            _migration(1, "CREATE TABLE target_probe (id integer PRIMARY KEY)"),
            _migration(2, "ALTER TABLE target_probe ADD COLUMN value text"),
            _migration(3, "ALTER TABLE target_probe ADD COLUMN note text"),
        ],
    )

    assert migrator.migrate(target_version=2) == 2
    assert migrator.current_version() == 2
    assert migrator.migrate(target_version=2) == 2
    assert migrator.migrate() == 3
    assert migrator.current_version() == 3
    with pytest.raises(RuntimeError, match="newer than requested target"):
        migrator.migrate(target_version=2)


@pytest.mark.parametrize("target_version", [0, -1, 4, True, 1.5])
def test_migrate_rejects_invalid_target_versions_before_connect(
    postgres_database, target_version, monkeypatch
):
    migrator = _migrator(
        postgres_database,
        [_migration(1, "SELECT 1"), _migration(2, "SELECT 2")],
    )
    monkeypatch.setattr(
        postgres_database,
        "write",
        lambda: (_ for _ in ()).throw(AssertionError("database was touched")),
    )
    with pytest.raises(ValueError, match="target"):
        migrator.migrate(target_version=target_version)


@pytest.mark.parametrize(
    "timeout_seconds", [0, -1, True, "3", math.inf, -math.inf, math.nan, 86_401]
)
def test_migrate_rejects_invalid_timeout_overrides_before_connect(
    postgres_database, timeout_seconds, monkeypatch
):
    migrator = _migrator(postgres_database, [_migration(1, "SELECT 1")])
    monkeypatch.setattr(
        postgres_database,
        "write",
        lambda: (_ for _ in ()).throw(AssertionError("database was touched")),
    )
    with pytest.raises(ValueError, match="timeout"):
        migrator.migrate(statement_timeout_seconds=timeout_seconds)


def test_migration_timeout_override_is_local_and_bounded(postgres_database):
    import psycopg

    from app.repositories.postgres.database import PostgresDatabase

    settings = postgres_database.settings.model_copy(
        update={
            "postgres_pool_min_size": 1,
            "postgres_pool_max_size": 1,
            "postgres_statement_timeout_seconds": 1,
        }
    )
    database = PostgresDatabase(settings, postgres_database.root_dir)
    try:
        migrator = _migrator(
            database,
            [
                _migration(
                    1,
                    # Leave headroom for platforms whose timeout interrupt
                    # delivery can lag the configured boundary by ~150ms.
                    "SELECT pg_sleep(1.5); "
                    "CREATE TABLE migration_timeout_probe (id integer)",
                )
            ],
        )
        with pytest.raises(psycopg.errors.QueryCanceled) as cancelled:
            migrator.migrate()
        assert cancelled.value.sqlstate == "57014"
        assert migrator.current_version() == 0

        assert migrator.migrate(statement_timeout_seconds=3) == 1
        with database.connect() as conn:
            timeout = conn.execute(
                "SELECT current_setting('statement_timeout') AS value"
            ).fetchone()["value"]
        assert timeout == "1s"
    finally:
        database.close()


def test_changed_checksum_fails_closed(postgres_database):
    assert (
        _migrator(
            postgres_database,
            [_migration(1, "CREATE TABLE checksum_probe (id integer)")],
        ).migrate()
        == 1
    )

    changed = _migrator(
        postgres_database,
        [_migration(1, "CREATE TABLE checksum_probe (id bigint)")],
    )
    with pytest.raises(RuntimeError, match="checksum"):
        changed.migrate()


@pytest.mark.parametrize(
    "migration_specs",
    [
        [(0, "SELECT 1", None)],
        [(1, "SELECT 1", None), (1, "SELECT 2", "duplicate")],
        [(1, "SELECT 1", None), (3, "SELECT 3", None)],
    ],
)
def test_invalid_duplicate_and_gapped_manifest_versions_fail_before_connect(
    postgres_database, migration_specs, monkeypatch
):
    migrations = [_migration(*spec) for spec in migration_specs]
    monkeypatch.setattr(
        postgres_database,
        "write",
        lambda: (_ for _ in ()).throw(AssertionError("database was touched")),
    )
    with pytest.raises(ValueError, match="migration"):
        _migrator(postgres_database, migrations).migrate()


def test_unknown_future_ledger_version_fails_closed(postgres_database):
    migration = _migration(1, "CREATE TABLE future_probe (id integer)")
    migrator = _migrator(postgres_database, [migration])
    assert migrator.migrate() == 1

    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO silicon_schema_migrations(version, checksum) VALUES (2, %s)",
            ("0" * 64,),
        )

    with pytest.raises(RuntimeError, match="future"):
        migrator.migrate()


def test_gapped_ledger_versions_fail_closed(postgres_database):
    migrations = [
        _migration(1, "CREATE TABLE ledger_gap_probe (id integer)"),
        _migration(2, "ALTER TABLE ledger_gap_probe ADD COLUMN value integer"),
        _migration(3, "ALTER TABLE ledger_gap_probe ADD COLUMN note text"),
    ]
    migrator = _migrator(postgres_database, migrations)
    assert migrator.migrate() == 3

    with postgres_database.write() as conn:
        conn.execute("DELETE FROM silicon_schema_migrations WHERE version = 2")

    with pytest.raises(RuntimeError, match="gap"):
        migrator.migrate()


def test_failed_migration_rolls_back_ddl_and_ledger(postgres_database):
    migrator = _migrator(
        postgres_database,
        [
            _migration(
                1,
                "CREATE TABLE rollback_migration_probe (id integer); "
                "INSERT INTO missing_table VALUES (1)",
            )
        ],
    )

    with pytest.raises(Exception):
        migrator.migrate()

    with postgres_database.connect() as conn:
        row = conn.execute(
            "SELECT to_regclass('rollback_migration_probe') AS relation"
        ).fetchone()
        ledger = conn.execute(
            "SELECT to_regclass('silicon_schema_migrations') AS relation"
        ).fetchone()
    assert row["relation"] is None
    assert ledger["relation"] is None


def test_packaged_migration_refuses_non_utf_database_before_any_ddl(
    postgres_non_utf_database,
):
    import psycopg

    from app.repositories.postgres.migrator import PostgresMigrator

    with pytest.raises(
        psycopg.errors.RaiseException, match="server_encoding must be UTF8"
    ):
        PostgresMigrator(postgres_non_utf_database).migrate()

    with postgres_non_utf_database.connect() as conn:
        encoding = conn.execute(
            "SELECT current_setting('server_encoding') AS value"
        ).fetchone()["value"]
        relations = conn.execute(
            "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','S','f')"
        ).fetchall()
    assert _safe_ascii_text(encoding, "server encoding") != "UTF8"
    assert relations == []


def test_packaged_migrations_apply_in_order(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert len(PostgresMigrator(postgres_database).migrations) == 45
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=2) == 2
    with postgres_database.connect() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name"
        ).fetchall()
        indexes = {
            row["indexname"]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"
            ).fetchall()
        }
        non_constraint_indexes = {
            row["index_name"]
            for row in conn.execute(
                "SELECT idx.relname AS index_name "
                "FROM pg_index i "
                "JOIN pg_class idx ON idx.oid=i.indexrelid "
                "JOIN pg_class tbl ON tbl.oid=i.indrelid "
                "JOIN pg_namespace n ON n.oid=tbl.relnamespace "
                "LEFT JOIN pg_constraint c ON c.conindid=i.indexrelid "
                "WHERE n.nspname=current_schema() AND c.oid IS NULL"
            ).fetchall()
        }
    names = {row["table_name"] for row in tables}
    assert "silicon_schema_migrations" in names
    assert "users" in names
    assert "notebooks" in names
    integrity_indexes = {
        "idx_kg_build_jobs_one_running",
        "idx_memory_answer_once",
        "idx_notebooks_share_token",
        "idx_promotion_object",
        "idx_sources_memory_id",
        "idx_users_username",
    }
    assert non_constraint_indexes == integrity_indexes
    assert integrity_indexes <= indexes
    assert "idx_chunks_nb" not in indexes
    assert "idx_chunks_text_trgm" not in indexes
    for version in (3, 4, 5, 6, 7, 8, 9, 10, 11):
        assert migrator.migrate(target_version=version) == version
    assert migrator.migrate() == 45
    with postgres_database.connect() as conn:
        final_indexes = {
            row["indexname"]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"
            ).fetchall()
        }
        ledger_versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM silicon_schema_migrations ORDER BY version"
            ).fetchall()
        ]
    assert "idx_chunks_nb" in final_indexes
    assert "idx_chunks_text_trgm" in final_indexes
    assert "idx_knowledge_relations_nb_source_id" in final_indexes
    assert "idx_knowledge_relations_nb_target_id" in final_indexes
    assert "uq_clusters_notebook_type_member" in final_indexes
    assert "idx_source_elements_source_type" in final_indexes
    assert "idx_notebook_object_schemas_status" in final_indexes
    assert "idx_groups_invite_token" in final_indexes
    # v39 (hot-path fix batch 1) — see migrations/0039_hotpath_batch1_indexes.sql.
    assert "idx_clusters_nb_canonical" in final_indexes
    assert "idx_clusters_nb_canonical_name_lower" in final_indexes
    assert "idx_extraction_runs_notebook" in final_indexes
    assert "idx_knowledge_source_fact_elements_notebook" in final_indexes
    assert "idx_memory_items_notebook" in final_indexes
    assert "idx_knowledge_relations_nb_source_target_edge" in final_indexes
    assert "idx_chunks_source_ordinal" in final_indexes
    assert "idx_sources_nb_hidden_type" in final_indexes
    # v40: creator-wide question overview keyset index.
    assert "idx_ask_jobs_creator_activity" in final_indexes
    # v42 (hot-path fix batch 2 / R6) — see
    # migrations/0042_hotpath_batch2_search_indexes.sql.
    assert "idx_knowledge_objects_nb_payload_trgm" in final_indexes
    assert "idx_source_elements_nonblank" in final_indexes
    # v43 (hot-path fix batch 3) — see
    # migrations/0043_concept_cluster_keyset_index.sql.
    assert "idx_clusters_nb_canonical_member" in final_indexes
    assert "idx_retained_activity_actor_type_created" in final_indexes
    assert "idx_retained_activity_owner_created" in final_indexes
    assert "idx_retained_activity_expires" in final_indexes
    assert "idx_retained_activity_notebook" in final_indexes
    assert "idx_sources_uploaded_by_created" in final_indexes
    assert "idx_wishes_kind_created" in final_indexes
    assert "idx_wish_votes_user" in final_indexes
    assert ledger_versions == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
        22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
        41, 42, 43, 44, 45, 46,
    ]


def test_indexing_pipeline_migration_upgrades_v35_without_rewriting_products(
    postgres_database,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=35) == 35
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            ("nb-pipeline-v35", "legacy", "2026-01-01", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO unified_kg_state(notebook_id,dirty,updated_at) "
            "VALUES (%s,%s,%s)",
            ("nb-pipeline-v35", 0, "2026-01-01"),
        )

    assert migrator.migrate(target_version=36) == 36
    with postgres_database.connect() as connection:
        notebook = connection.execute(
            "SELECT name,indexing_pipeline,indexing_pipeline_version,"
            "indexing_pipeline_generation,indexing_pipeline_job_id "
            "FROM notebooks WHERE id=%s",
            ("nb-pipeline-v35",),
        ).fetchone()
        product = connection.execute(
            "SELECT indexing_pipeline_id,indexing_pipeline_version "
            "FROM unified_kg_state WHERE notebook_id=%s",
            ("nb-pipeline-v35",),
        ).fetchone()
    assert notebook == {
        "name": "legacy",
        "indexing_pipeline": None,
        "indexing_pipeline_version": "builtin.chunk.v1",
        "indexing_pipeline_generation": "",
        "indexing_pipeline_job_id": "",
    }
    assert product == {
        "indexing_pipeline_id": "",
        "indexing_pipeline_version": "builtin.chunk.v1",
    }


def test_notebook_object_schema_migration_relocates_legacy_rows(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=24) == 24
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            ("nb-schema-v25", "legacy", "2026-01-01", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO object_schemas "
            "(object_type,notebook_id,label,status,source,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                "legacy_recipe", "nb-schema-v25", "Legacy recipe", "proposed",
                "induced", "2026-01-01", "2026-01-01",
            ),
        )

    assert migrator.migrate() == 45
    with postgres_database.connect() as connection:
        relocated = connection.execute(
            "SELECT notebook_id,object_type,status,created_by "
            "FROM notebook_object_schemas WHERE notebook_id=%s",
            ("nb-schema-v25",),
        ).fetchone()
        legacy = connection.execute(
            "SELECT 1 FROM object_schemas WHERE object_type=%s",
            ("legacy_recipe",),
        ).fetchone()
    assert relocated == {
        "notebook_id": "nb-schema-v25",
        "object_type": "legacy_recipe",
        "status": "proposed",
        "created_by": "",
    }
    assert legacy is None


def test_notebook_object_schema_migration_refuses_orphaned_rows(postgres_database):
    import psycopg

    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=24) == 24
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO object_schemas "
            "(object_type,notebook_id,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            ("orphan_recipe", "nb-missing", "2026-01-01", "2026-01-01"),
        )

    with pytest.raises(psycopg.errors.RaiseException, match="missing notebook"):
        migrator.migrate()
    assert migrator.current_version() == 24


def test_source_agent_provenance_column_is_nullable_and_unconstrained(
    postgres_database,
):
    """v26 pairs SQLite v48: one nullable, un-indexed, FK-free text column.

    Every part is load-bearing. NULL means "a person added this source", so the
    migration must not backfill or default it; and the delete permission check
    is a single-row primary-key read, so an index or a unique constraint would
    buy nothing while a foreign key to ``agent_profiles`` would both delete the
    provenance with the profile and add an edge to the forward-shadow parent
    closure.
    """
    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=25) == 25
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='sources' "
            "AND column_name='agent_profile_id'"
        ).fetchone() is None

    assert migrator.migrate() == 45
    with postgres_database.connect() as connection:
        column = connection.execute(
            "SELECT data_type,is_nullable,column_default,collation_name "
            "FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='sources' "
            "AND column_name='agent_profile_id'"
        ).fetchone()
        indexed = connection.execute(
            "SELECT i.indexrelid::regclass::text AS name FROM pg_index i "
            "JOIN pg_class t ON t.oid=i.indrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=t.oid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname=current_schema() AND t.relname='sources' "
            "AND a.attname='agent_profile_id'"
        ).fetchall()
        constrained = connection.execute(
            "SELECT c.conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=t.oid "
            "AND a.attnum = ANY(c.conkey) "
            "WHERE n.nspname=current_schema() AND t.relname='sources' "
            "AND a.attname='agent_profile_id'"
        ).fetchall()
    assert column == {
        "data_type": "text",
        "is_nullable": "YES",
        "column_default": None,
        "collation_name": "C",
    }
    assert indexed == []
    assert constrained == []


def test_cluster_membership_migration_dedupes_before_unique_guard(postgres_database):
    import psycopg

    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=6) == 6
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,%s,%s,%s)",
            ("nb-cluster-migration", "clusters", "2026-01-01", "2026-01-01"),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        "cc-legacy-old",
                        "nb-cluster-migration",
                        "canonical-old",
                        "ko-legacy-duplicate",
                        "old",
                        "concept",
                        "2026-01-01",
                    ),
                    (
                        "cc-legacy-new",
                        "nb-cluster-migration",
                        "canonical-new",
                        "ko-legacy-duplicate",
                        "new",
                        "concept",
                        "2026-01-02",
                    ),
                ],
            )

    assert migrator.migrate() == 45
    with postgres_database.connect() as connection:
        rows = connection.execute(
            "SELECT id,canonical_id FROM concept_clusters "
            "WHERE notebook_id=%s ORDER BY id",
            ("nb-cluster-migration",),
        ).fetchall()
    assert rows == [{"id": "cc-legacy-new", "canonical_id": "canonical-new"}]

    with pytest.raises(psycopg.errors.UniqueViolation):
        with postgres_database.write() as connection:
            connection.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    "cc-legacy-conflict",
                    "nb-cluster-migration",
                    "canonical-conflict",
                    "ko-legacy-duplicate",
                    "conflict",
                    "concept",
                    "2026-01-03",
                ),
            )


def test_migrations_take_the_fixed_transaction_advisory_lock(postgres_database):
    from app.repositories.postgres.migrator import (
        MIGRATION_ADVISORY_LOCK_NAME,
        migration_advisory_lock_key,
    )

    assert MIGRATION_ADVISORY_LOCK_NAME == "silicon-notebook:postgres-migrations"
    key = migration_advisory_lock_key()
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63

    assert (
        _migrator(
            postgres_database,
            [_migration(1, "CREATE TABLE advisory_probe (id integer)")],
        ).migrate()
        == 1
    )


def test_concurrent_migrators_serialize_before_check_and_ddl(postgres_database):
    migration = _migration(
        1,
        "SELECT pg_sleep(0.25); "
        "CREATE TABLE concurrent_migration_probe (id integer PRIMARY KEY)",
    )
    migrator = _migrator(postgres_database, [migration])
    barrier = threading.Barrier(2)
    versions: list[int] = []
    failures: list[BaseException] = []

    def run_migration() -> None:
        try:
            barrier.wait(timeout=2)
            versions.append(migrator.migrate())
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    workers = [threading.Thread(target=run_migration) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not any(worker.is_alive() for worker in workers)
    assert failures == []
    assert sorted(versions) == [1, 1]
    with postgres_database.connect() as conn:
        ledger = conn.execute(
            "SELECT version, count(*) AS count FROM silicon_schema_migrations "
            "GROUP BY version"
        ).fetchall()
        table_count = conn.execute(
            "SELECT count(*) AS count FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'concurrent_migration_probe'"
        ).fetchone()["count"]
    assert ledger == [{"version": 1, "count": 1}]
    assert table_count == 1
