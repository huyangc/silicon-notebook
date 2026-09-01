"""Live-PostgreSQL half of ``app/repositories/postgres/hotpath_indexes.py``'s
contract that a fake connection cannot exercise (see
``backend/tests/test_hotpath_indexes.py`` for the fake-connection half:
anti-drift against the migration file, statement shape, the diagnostics
message, and the ``psycopg.connect(autocommit=True)`` kwarg assertion).

Two things only a real server can prove:

  1. ``autocommit=True`` is load-bearing. Migrating only to schema version 38
     (one hop before migration 0039 introduces these eight indexes as an
     in-transaction, no-op-once-built ``CREATE INDEX IF NOT EXISTS`` ledger
     entry) leaves every index genuinely missing, so
     ``install_hotpath_indexes`` here issues real ``CREATE INDEX
     CONCURRENTLY`` statements against a real server. ``CONCURRENTLY``
     cannot run inside a transaction block at all -- PostgreSQL raises
     ``25001`` (``ACTIVE_SQL_TRANSACTION``) on the very first statement if
     the connection is not autocommit -- so this test would fail loudly if
     that kwarg were ever dropped or flipped.
  2. Real catalog rendering. ``_matches_shape`` compares against
     ``pg_get_indexdef``/``pg_get_expr`` text a live server actually
     produces, including PostgreSQL's own canonicalization of
     ``idx_sources_nb_hidden_type``'s ``IN ('memory','knowhow')`` predicate
     into ``= ANY (ARRAY['memory'::text,'knowhow'::text])`` -- a fake
     connection can only assert what this module *thinks* the server says,
     never what it actually does.
"""
from __future__ import annotations

import re

import pytest

from app.repositories.postgres.hotpath_indexes import (
    HOTPATH_INDEX_SPECS,
    HotpathIndexError,
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
from app.repositories.postgres.migrator import PostgresMigrator


pytestmark = pytest.mark.postgres_integration

_ALL_NAMES = frozenset(spec.name for spec in HOTPATH_INDEX_SPECS)


def _schema_of(database) -> str:
    with database.connect() as connection:
        return connection.execute(
            "SELECT current_schema() AS name"
        ).fetchone()["name"]


@pytest.mark.xdist_group(name="postgres_hotpath_indexes")
def test_install_builds_all_eight_concurrently_and_is_idempotent(postgres_database):
    # One hop before migration 39 introduces the eight indexes itself, so
    # every one of them is genuinely absent below -- proving this run's
    # CREATE INDEX CONCURRENTLY statements are for real, not skipped as
    # already-ready.
    assert PostgresMigrator(postgres_database).migrate(target_version=38) == 38
    schema = _schema_of(postgres_database)
    database_url = postgres_database.settings.database_url

    before = inspect_hotpath_indexes(database_url, schema=schema)
    assert {row["name"] for row in before["indexes"]} == _ALL_NAMES
    assert {row["state"] for row in before["indexes"]} == {"缺失"}

    state = install_hotpath_indexes(database_url, schema=schema)
    assert {row["name"] for row in state["indexes"]} == _ALL_NAMES
    assert all(row["state"] == "存在" for row in state["indexes"]), state

    # Idempotent rerun: everything is already ready, nothing rebuilt, and
    # inspect_hotpath_indexes (read-only, no advisory lock) agrees.
    repeated = install_hotpath_indexes(database_url, schema=schema)
    assert repeated == state
    inspected = inspect_hotpath_indexes(database_url, schema=schema)
    assert inspected == state

    # Migration 0039's own plain (in-transaction) CREATE INDEX IF NOT EXISTS
    # is a true no-op ledger entry once the offline CONCURRENTLY builder has
    # already built every index online -- the documented relationship
    # between the two in this module's and the migration's docstrings.
    assert PostgresMigrator(postgres_database).migrate() == 49
    after_migration = inspect_hotpath_indexes(database_url, schema=schema)
    assert after_migration == state


@pytest.mark.parametrize(
    "unexpected_ddl",
    [
        # Same name, same table, columns in the wrong order.
        "CREATE INDEX idx_clusters_nb_canonical "
        "ON concept_clusters(canonical_id, notebook_id)",
        # Same name, same table and leading columns, but a narrower partial
        # predicate than the one this module expects.
        "CREATE INDEX idx_sources_nb_hidden_type "
        "ON sources(notebook_id, source_type) WHERE source_type = 'memory'",
    ],
)
@pytest.mark.xdist_group(name="postgres_hotpath_indexes")
def test_installer_rejects_a_same_named_differently_shaped_index(
    postgres_database, unexpected_ddl,
):
    assert PostgresMigrator(postgres_database).migrate(target_version=38) == 38
    schema = _schema_of(postgres_database)
    database_url = postgres_database.settings.database_url
    install_hotpath_indexes(database_url, schema=schema)

    name = re.search(r"CREATE INDEX (\w+)", unexpected_ddl).group(1)
    assert name in _ALL_NAMES
    with postgres_database.write() as connection:
        connection.execute(f"DROP INDEX {name}")
        connection.execute(unexpected_ddl)

    state = inspect_hotpath_indexes(database_url, schema=schema)
    row = next(item for item in state["indexes"] if item["name"] == name)
    assert row["state"] == "UNEXPECTED"

    with pytest.raises(
        HotpathIndexError, match=f"unexpected_index_definition:{name}"
    ):
        install_hotpath_indexes(database_url, schema=schema)

    # A same-named hand-built definition is fail-closed, never repaired or
    # dropped as if it were this tool's own interrupted CONCURRENTLY build.
    with postgres_database.connect() as connection:
        assert (
            connection.execute(
                "SELECT to_regclass(%s) AS name", (f"{schema}.{name}",)
            ).fetchone()["name"]
            is not None
        )
