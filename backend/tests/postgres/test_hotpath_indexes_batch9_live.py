"""Live-PostgreSQL paths of migration 0086 (hot-path batch 9, the two
reading-order successors of the ``(created_at, id)`` element indexes):

* builder-first production order -- both successors built online exactly as
  ``HOTPATH_INDEX_SPECS`` renders them, the builder's read-only inspector
  calls them ready (so the spec's catalog-echo shape of the digits expression
  is the one PostgreSQL really reports), and 0086 is then a ledger no-op that
  only retires the two old indexes;
* the DO block rejects a same-named wrong-shape index and an INVALID residue
  instead of letting ``IF NOT EXISTS`` skip over it.

The fake-connection anti-drift half is ``tests/test_hotpath_indexes_batch9.py``.
"""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.hotpath_indexes import (
    HOTPATH_INDEX_SPECS,
    inspect_hotpath_indexes,
)
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch9"),
]

_NAMES = ("idx_source_elements_reading_order", "idx_source_elements_type_reading_order")
_RETIRED = ("idx_source_elements_source_created", "idx_source_elements_source_type")
_NOW = "2026-01-01T00:00:00+00:00"


def _index_names(database) -> set[str]:
    with database.connect() as db:
        return {
            row["indexname"] for row in db.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='source_elements' "
                "AND schemaname=current_schema()"
            ).fetchall()
        }


def test_0085_accepts_the_indexes_the_builder_builds(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=85) == 85
    url = postgres_database.settings.database_url
    with psycopg.connect(url, autocommit=True) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        for spec in HOTPATH_INDEX_SPECS:
            if spec.name in _NAMES:
                conn.execute(spec.ddl(schema, concurrently=True))
    states = {row["name"]: row["state"]
              for row in inspect_hotpath_indexes(url, schema=schema)["indexes"]}
    assert {name: states[name] for name in _NAMES} == dict.fromkeys(_NAMES, "存在")
    assert set(_RETIRED) <= _index_names(postgres_database)
    assert migrator.migrate() == 86
    names = _index_names(postgres_database)
    assert set(_NAMES) <= names and not set(_RETIRED) & names
    after = {row["name"]: row["state"]
             for row in inspect_hotpath_indexes(url, schema=schema)["indexes"]}
    assert {name: after[name] for name in _NAMES} == dict.fromkeys(_NAMES, "存在")


def _rejected_then_cleared(postgres_database, migrator, *, index: str, match: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match=match):
        migrator.migrate()
    # RAISE rolled back the whole migration transaction, ledger row included,
    # and the retired indexes are still there.
    assert migrator.migrate(target_version=85) == 85
    assert set(_RETIRED) <= _index_names(postgres_database)
    with postgres_database.write() as db:
        db.execute(f"DROP INDEX {index}")
    assert migrator.migrate() == 86


@pytest.mark.parametrize(
    "ddl",
    [
        # The retired shape under the successor's name (no digits term).
        "CREATE INDEX idx_source_elements_reading_order "
        "ON source_elements(source_id, created_at, id)",
        # A digits term that is not the readers' expression.
        "CREATE INDEX idx_source_elements_reading_order "
        "ON source_elements(source_id, created_at, length(id), id)",
        # The typed successor without element_type.
        "CREATE INDEX idx_source_elements_type_reading_order "
        "ON source_elements(source_id, created_at, id)",
    ],
    ids=["no-digits", "other-expression", "typed-without-type"],
)
def test_0085_rejects_a_same_named_wrong_shape_index(postgres_database, ddl):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=85) == 85
    with postgres_database.write() as db:
        db.execute(ddl)
    index = ddl.split()[2]
    _rejected_then_cleared(postgres_database, migrator, index=index,
                           match="does not match the expected definition")


def test_0085_rejects_an_invalid_same_named_index(postgres_database):
    """A real INVALID residue (the CI role is NOSUPERUSER): a UNIQUE
    CONCURRENTLY build over two elements of one source fails in validation and
    leaves ``indisvalid=false`` -- the shape an interrupted builder run leaves."""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=85) == 85
    now = normalize_timestamp(_NOW)
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-b8-inv','invalid residue',%s,%s)", (now, now))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,"
            "updated_at) VALUES ('s-b8','nb-b8-inv','t','pdf','parsed','parsed','f','p',"
            "0,'h','','',%s,%s)", (now, now))
        for index in (1, 2):
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,"
                "text,created_at) VALUES (%s,'s-b8','paragraph','p','x',%s)",
                (f"el-s-b8-{index:04d}", now))
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY idx_source_elements_reading_order "
                "ON source_elements(source_id)"
            )
    _rejected_then_cleared(postgres_database, migrator,
                           index="idx_source_elements_reading_order", match="INVALID")
