"""Live-PostgreSQL reject and accept paths of the pre-existing-index validation
DO blocks that carry an INCLUDE dimension: migration 0084 (hot-path batch 7,
``idx_source_paper_meta_source_nb``) and, closing the same gap, migration 0051
(batch 6's generation-aware cluster indexes).

``CREATE INDEX IF NOT EXISTS`` alone would silently skip creation over an
operator's wrong-shape index or an INVALID residue of an interrupted
``CONCURRENTLY`` build and still record the migration as applied; the DO
blocks fail the migration loudly instead (the ledger does not advance), and
an index built exactly like ``scripts/build_hotpath_indexes.py --apply``
builds it passes through as a no-op. The fake-connection anti-drift half is
``tests/test_hotpath_indexes_batch7.py``.
"""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch7"),
]

_INDEX = "idx_source_paper_meta_source_nb"
_NOW = "2026-01-01T00:00:00+00:00"


def _rejected_then_cleared(postgres_database, migrator, *, before: int, index: str,
                           match: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match=match):
        migrator.migrate()
    # RAISE rolled back the whole migration transaction, ledger row included.
    assert migrator.migrate(target_version=before) == before
    with postgres_database.write() as db:
        db.execute(f"DROP INDEX {index}")
    assert migrator.migrate() == 84


@pytest.mark.parametrize(
    "ddl",
    [
        # No INCLUDE at all.
        f"CREATE INDEX {_INDEX} ON source_paper_meta(source_id)",
        # INCLUDE missing a column.
        f"CREATE INDEX {_INDEX} ON source_paper_meta(source_id) INCLUDE (notebook_id)",
        # INCLUDE in the wrong order.
        f"CREATE INDEX {_INDEX} ON source_paper_meta(source_id) INCLUDE (is_paper, notebook_id)",
        # The include columns as keys (SQLite's spelling) is a different index.
        f"CREATE INDEX {_INDEX} ON source_paper_meta(source_id, notebook_id, is_paper)",
        # Unique where the spec is not.
        f"CREATE UNIQUE INDEX {_INDEX} ON source_paper_meta(source_id) "
        "INCLUDE (notebook_id, is_paper)",
    ],
    ids=["no-include", "include-missing-column", "include-wrong-order",
         "includes-as-keys", "unique"],
)
def test_0084_rejects_a_same_named_wrong_shape_index(postgres_database, ddl):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=83) == 83
    with postgres_database.write() as db:
        db.execute(ddl)
    _rejected_then_cleared(
        postgres_database, migrator, before=83, index=_INDEX,
        match="does not match the expected definition",
    )


def test_0084_rejects_an_invalid_same_named_index(postgres_database):
    """A real INVALID residue without catalog surgery (the CI role is
    NOSUPERUSER): a UNIQUE CONCURRENTLY build over two metadata rows that share
    a notebook fails in its validation phase and leaves ``indisvalid=false``
    behind -- the shape an interrupted builder run leaves."""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=83) == 83
    now = normalize_timestamp(_NOW)
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-b7-inv','invalid residue',%s,%s)",
            (now, now),
        )
        for source_id in ("s-b7-a", "s-b7-b"):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,"
                "updated_at) VALUES (%s,'nb-b7-inv','t','pdf','parsed','parsed','f','p',"
                "0,%s,'','',%s,%s)",
                (source_id, f"h-{source_id}", now, now),
            )
            db.execute(
                "INSERT INTO source_paper_meta(source_id,notebook_id,is_paper,created_at,"
                "updated_at) VALUES (%s,'nb-b7-inv',1,%s,%s)",
                (source_id, now, now),
            )
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY {_INDEX} "
                "ON source_paper_meta(notebook_id) INCLUDE (source_id)"
            )
        residue = conn.execute(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE n.nspname=current_schema() AND c.relname='{_INDEX}'"
        ).fetchone()
    assert residue is not None and residue[0] is False
    _rejected_then_cleared(postgres_database, migrator, before=83, index=_INDEX,
                           match="INVALID")


def test_0084_accepts_the_index_the_builder_builds(postgres_database):
    """Builder-first production order: the index built online exactly as
    HOTPATH_INDEX_SPECS renders it makes 0084 a ledger-only no-op."""
    from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=83) == 83
    (spec,) = [spec for spec in HOTPATH_INDEX_SPECS if spec.name == _INDEX]
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(spec.ddl(schema, concurrently=True))
    assert migrator.migrate() == 84
    with postgres_database.connect() as db:
        definition = db.execute(
            "SELECT pg_get_indexdef(c.oid) AS d FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relname=%s",
            (_INDEX,),
        ).fetchone()["d"]
    assert definition.endswith("(source_id) INCLUDE (notebook_id, is_paper)"), definition


@pytest.mark.parametrize(
    ("index", "ddl"),
    [
        ("idx_clusters_nb_canonical_member_gen",
         "CREATE INDEX idx_clusters_nb_canonical_member_gen "
         "ON concept_clusters(notebook_id, canonical_id, member_object_id)"),
        ("idx_clusters_nb_canonical_member_gen",
         "CREATE INDEX idx_clusters_nb_canonical_member_gen "
         "ON concept_clusters(notebook_id, canonical_id, member_object_id) "
         "INCLUDE (object_type)"),
        ("idx_clusters_nb_created_gen",
         "CREATE INDEX idx_clusters_nb_created_gen "
         "ON concept_clusters(notebook_id, created_at, generation)"),
        ("uq_clusters_nb_type_member_generation",
         "CREATE INDEX uq_clusters_nb_type_member_generation "
         "ON concept_clusters(notebook_id, object_type, member_object_id, generation)"),
    ],
    ids=["missing-include", "wrong-include", "include-as-key", "not-unique"],
)
def test_0051_rejects_a_same_named_wrong_shape_index(postgres_database, index, ddl):
    """Same gap closed for batch 6: its DO block also compares INCLUDE columns
    and uniqueness. The builder's prerequisite ADD COLUMN (generation) runs
    first, exactly as in the builder-first production order."""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=50) == 50
    with postgres_database.write() as db:
        db.execute(
            "ALTER TABLE concept_clusters "
            "ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0"
        )
        db.execute(ddl)
    with pytest.raises(
        psycopg.errors.RaiseException, match="does not match the expected definition"
    ):
        migrator.migrate()
    assert migrator.migrate(target_version=50) == 50
    with postgres_database.write() as db:
        db.execute(f"DROP INDEX {index}")
    assert migrator.migrate() == 84
