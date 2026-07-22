from __future__ import annotations

import math
import threading

import pytest


pytestmark = pytest.mark.postgres_integration


def _migration(version: int, sql: str, name: str | None = None):
    from app.repositories.postgres.migrator import PostgresMigration

    return PostgresMigration(version=version, name=name or f"migration_{version}", sql=sql)


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
                    "SELECT pg_sleep(1.1); "
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
    assert _migrator(
        postgres_database,
        [_migration(1, "CREATE TABLE checksum_probe (id integer)")],
    ).migrate() == 1

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


def test_packaged_manifest_records_schema_complete_sqlite_pair(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    assert POSTGRES_SCHEMA_MANIFEST.postgres_version == 7
    assert POSTGRES_SCHEMA_MANIFEST.sqlite_version == 24
    assert len(PostgresMigrator(postgres_database).migrations) == 7
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
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema()"
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
    for version in (3, 4, 5, 6, 7):
        assert migrator.migrate(target_version=version) == version
    assert migrator.migrate() == 7
    with postgres_database.connect() as conn:
        final_indexes = {
            row["indexname"]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema()"
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
    assert "uq_clusters_notebook_type_member" in final_indexes
    assert ledger_versions == [1, 2, 3, 4, 5, 6, 7]


def test_cluster_membership_migration_dedupes_before_unique_guard(postgres_database):
    import psycopg

    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=6) == 6
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
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

    assert migrator.migrate() == 7
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

    assert _migrator(
        postgres_database,
        [_migration(1, "CREATE TABLE advisory_probe (id integer)")],
    ).migrate() == 1


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
