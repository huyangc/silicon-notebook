"""PostgreSQL migration ``0083_kg_seed_space_and_board_partition.sql`` (SQLite
twins: ``test_sqlite_migrations.py::test_v102_…``).

The one migration carries both halves of PR-4. Pinned on real PostgreSQL: the
board partition's three columns and their defaults; the ``board_key`` backfill
of every existing board, every generation, to its own id; existing state rows
reading 0 / '' (the next rebuild starts cold); the concept seed space of the
same state rows reading 1 while a row created afterwards starts in 2 and an
existing decision row reads seed space 1 -- both halves in one step; and a
re-run of the frozen SQL finishing an interrupted backfill while leaving keys
that are already set, and every row's seed space, alone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_board_partition_migration"),
]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0083_kg_seed_space_and_board_partition.sql"
)
NOW = "2026-10-21T00:00:00+00:00"
NB = "nb-v82"
NB_AFTER = "nb-v83"


def _columns(db) -> dict:
    rows = db.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default, "
        "collation_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND ("
        "(table_name = 'unified_kg_state' AND column_name IN "
        "('community_partition_version', 'community_partition_mode')) OR "
        "(table_name = 'communities' AND column_name = 'board_key'))").fetchall()
    return {(r["table_name"], r["column_name"]): (
        r["data_type"], r["is_nullable"], r["column_default"], r["collation_name"])
        for r in rows}


def _keys(db) -> dict:
    return {r["id"]: r["board_key"] for r in db.execute(
        "SELECT id, board_key FROM communities WHERE notebook_id = %s", (NB,))}


def _seed_spaces(db) -> dict:
    return {r["notebook_id"]: r["concept_seed_version"] for r in db.execute(
        "SELECT notebook_id, concept_seed_version FROM unified_kg_state "
        "WHERE notebook_id IN (%s, %s)", (NB, NB_AFTER))}


def test_0083_adds_the_columns_and_backfills_every_generation(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=82) == 82
    with postgres_database.write() as db:
        for notebook_id in (NB, NB_AFTER):
            db.execute("INSERT INTO notebooks (id, name, created_at, updated_at) "
                       "VALUES (%s, 'n', %s, %s)", (notebook_id, NOW, NOW))
        db.execute("INSERT INTO unified_kg_state (notebook_id, updated_at) VALUES (%s, %s)",
                   (NB, NOW))
        db.execute(
            "INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, "
            "canonical_b, score, status, created_at, updated_at) VALUES "
            "('mc-old', %s, 'K-a b', 'K-c', 0.9, 'rejected', %s, %s)", (NB, NOW, NOW))
        for cid, generation in (("cm-a", 0), ("cm-b", 1), ("cm-c", 2)):
            db.execute(
                "INSERT INTO communities (id, notebook_id, level, member_ids, size, "
                "created_at, generation) VALUES (%s, %s, 0, '[\"K-x\"]'::jsonb, 1, %s, %s)",
                (cid, NB, NOW, generation))
    assert migrator.migrate() == 83
    with postgres_database.write() as db:
        db.execute("INSERT INTO unified_kg_state (notebook_id, updated_at) VALUES (%s, %s)",
                   (NB_AFTER, NOW))
    with postgres_database.connect() as db:
        assert _columns(db) == {
            ("unified_kg_state", "community_partition_version"): ("bigint", "NO", "0", None),
            ("unified_kg_state", "community_partition_mode"): ("text", "NO", "''::text", "C"),
            ("communities", "board_key"): ("text", "NO", "''::text", "C"),
        }
        state = db.execute(
            "SELECT community_partition_version, community_partition_mode "
            "FROM unified_kg_state WHERE notebook_id = %s", (NB,)).fetchone()
        assert (state["community_partition_version"], state["community_partition_mode"]) == (0, "")
        assert _keys(db) == {"cm-a": "cm-a", "cm-b": "cm-b", "cm-c": "cm-c"}
        # The seed-space half of the same step: the existing state row and
        # decision were minted in space 1, a row created afterwards starts in 2.
        assert _seed_spaces(db) == {NB: 1, NB_AFTER: 2}
        assert db.execute(
            "SELECT seed_version FROM concept_merge_candidates WHERE id = 'mc-old'"
        ).fetchone()["seed_version"] == 1

    # An interrupted backfill leaves defaults behind; re-running the frozen SQL
    # finishes it, leaves a key that is already set alone, and moves no row's
    # seed space (ADD COLUMN IF NOT EXISTS is a no-op, SET DEFAULT touches no row).
    with postgres_database.write() as db:
        db.execute("UPDATE communities SET board_key = '' WHERE id = 'cm-b'")
        db.execute("UPDATE communities SET board_key = 'bk-kept' WHERE id = 'cm-c'")
        db.execute("UPDATE unified_kg_state SET concept_seed_version = 2 WHERE notebook_id = %s",
                   (NB,))
    with postgres_database.write() as db:
        db.execute(MIGRATION.read_text(encoding="utf-8"), prepare=False)
    with postgres_database.connect() as db:
        assert _keys(db) == {"cm-a": "cm-a", "cm-b": "cm-b", "cm-c": "bk-kept"}
        assert _seed_spaces(db) == {NB: 2, NB_AFTER: 2}
