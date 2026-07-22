from __future__ import annotations

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


def test_empty_task4_manifest_records_sqlite_pair_without_business_ddl(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    assert POSTGRES_SCHEMA_MANIFEST.postgres_version == 0
    assert POSTGRES_SCHEMA_MANIFEST.sqlite_version == 23
    assert PostgresMigrator(postgres_database).migrations == ()
    assert _migrator(postgres_database, []).migrate() == 0
    with postgres_database.connect() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name"
        ).fetchall()
    assert tables == [{"table_name": "silicon_schema_migrations"}]


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
