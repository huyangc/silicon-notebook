"""``validate_sqlite_schema`` guard: one cheap, no-PostgreSQL-needed check
that pins the SQLite side of the shadow manifest against a real, freshly
migrated SQLite database.

This closes a coverage gap flagged in P2-T1 code review: every production
caller of ``validate_sqlite_schema`` (control.py, snapshot.py, bulk_copy.py,
verifier.py, retention.py) lives behind the PostgreSQL-integration lane
(``postgres_integration``/``slow`` markers, requiring ``TEST_POSTGRES_URL``),
so a table/index whose ``copy_rank`` disagrees with its own foreign keys --
the C3 shape from review -- would only be caught the first time someone
actually runs a shadow migration against live PostgreSQL, not in G1. This
test calls the same function directly against a plain SQLite connection, so
it runs in G1 for every future new table without anyone having to remember
to add coverage for it.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest


def _fresh_sqlite_database(tmp_path: Path):
    from app.core.config import Settings
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SqliteMigrator

    db_path = tmp_path / "schema-validation.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=tmp_path)
    # Raw migrator only -- no SQLiteRepository seeding. validate_sqlite_schema
    # (validate_rows=True by default) only rejects NULL/duplicate replication
    # keys among *existing* rows, which an empty table trivially satisfies.
    SqliteMigrator(database, settings).migrate()
    return database


def test_validate_sqlite_schema_accepts_the_current_manifest(tmp_path):
    """A freshly migrated, empty SQLite database at the current schema
    version must validate cleanly against the real (unmutated) manifest:
    table totality, declared-PK/NULL-guard classification, FK copy-rank
    ordering, and BLOB/path column classification all agree. This is the
    baseline the mutation test below proves is actually load-bearing."""
    from app.migration.shadow.manifest import (
        MANIFEST,
        RUNNING_SCHEMA_PAIR,
        validate_sqlite_schema,
    )

    database = _fresh_sqlite_database(tmp_path)
    try:
        conn = database.connect()
        report = validate_sqlite_schema(
            conn, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )
        assert report is not None
    finally:
        database.close_local()


def test_validate_sqlite_schema_rejects_a_foreign_key_copy_rank_inversion(
    tmp_path,
):
    """Mutation guard (the C3 shape flagged in P2-T1 code review): swap
    ``copy_rank`` between an FK parent (``groups``) and one of its children
    (``notebook_share_requests``, which FKs onto ``groups.id``). After the
    swap the parent's rank is >= the child's rank, which is exactly the
    inversion ``validate_sqlite_schema`` exists to catch -- a table copied
    before a foreign key it depends on would exist during snapshot COPY.

    This is deliberately a "move" mutation (swap two real ranks), not a
    "delete" -- per the red line that a mutation test must prove the check
    fires, not merely that removing code changes behavior.
    """
    from app.migration.shadow.manifest import (
        MANIFEST,
        RUNNING_SCHEMA_PAIR,
        validate_sqlite_schema,
    )
    from app.migration.shadow.types import Manifest

    by_name = {spec.name: spec for spec in MANIFEST.tables}
    parent = by_name["groups"]
    child = by_name["notebook_share_requests"]
    # Sanity: today's real manifest has the correct FK-consistent order, so
    # the swap below is a genuine inversion and not an accidental no-op.
    assert parent.copy_rank < child.copy_rank

    swapped_parent = dataclasses.replace(parent, copy_rank=child.copy_rank)
    swapped_child = dataclasses.replace(child, copy_rank=parent.copy_rank)
    mutated_tables = tuple(
        swapped_parent
        if spec.name == "groups"
        else swapped_child
        if spec.name == "notebook_share_requests"
        else spec
        for spec in MANIFEST.tables
    )
    mutated_manifest = Manifest(
        schema_pair=MANIFEST.schema_pair, tables=mutated_tables
    )

    database = _fresh_sqlite_database(tmp_path)
    try:
        conn = database.connect()
        with pytest.raises(ValueError, match="foreign-key copy-rank inversion"):
            validate_sqlite_schema(
                conn, mutated_manifest, expected_pair=RUNNING_SCHEMA_PAIR
            )
    finally:
        database.close_local()
