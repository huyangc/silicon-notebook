"""Board partition store primitives on SQLite (v102; design docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md
§五 S2).

The world and every expected value live in ``tests/board_partition_store_cases.py``;
``tests/postgres/test_board_partition_store_pg.py`` runs the same checks on
PostgreSQL against the same literals. The plan pin for the previous-partition
read is here (run on SQLite 3.45 too: CI's ubuntu-24.04 Python ships 3.45).
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import board_partition_store_cases as cases


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close_local()


def test_board_key_rows_read_one_generation_and_level(repo):
    cases.check_board_key_rows_read_one_generation_and_level(repo)


def test_board_keys_are_written_and_copied_forward(repo):
    cases.check_board_keys_are_written_and_copied_forward(repo)


def test_partition_columns_round_trip(repo):
    cases.check_partition_columns_round_trip(repo)


def test_partition_version_cas(repo):
    cases.check_partition_version_cas(repo)


def test_kg_delete_resets_the_partition_columns(repo):
    cases.check_kg_delete_resets_the_partition_columns(repo)


def test_index_publication_resets_the_partition_columns(repo):
    cases.check_index_publication_resets_the_partition_columns(repo, postgres=False)


def test_board_overview_returns_board_keys(repo):
    cases.check_board_overview_returns_board_keys(repo)


# sqlite_stat1 of a production-shaped deployment: ~45 000 boards per notebook
# across a handful of generations, ~171 万 member rows per published generation,
# many notebooks.
_PRODUCTION_STATS = (
    ("communities", "idx_communities_nb_level", "600000 60000 60000"),
    ("communities", "sqlite_autoindex_communities_1", "600000 1"),
    ("community_members", "idx_commmem_nb_can", "24000000 2400000 3"),
    ("community_members", "idx_commmem_nb_comm", "24000000 2400000 40"),
    ("community_members", "uq_community_members_sync_key", "24000000 40 1"),
)


@pytest.mark.parametrize("statistics", ["fresh", "production"])
def test_previous_partition_read_is_driven_by_the_board_index(repo, monkeypatch, statistics):
    """The previous-partition read reaches this notebook's boards of one
    generation through ``idx_communities_nb_level`` and each board's members
    through an index whose leading key is ``community_id`` (or the notebook and
    ``community_id``) -- never a scan of either table: a production library's
    published generation is ~171 万 member rows among every notebook's
    generations. The statement is the one the store runs, captured on its way
    through the connection's ``execute``; pinned with no statistics and with
    production-shaped ones, on SQLite 3.53 and 3.45 alike."""
    from app.repositories.sqlite import database as sqlite_database

    rt = repo._runtime
    world = cases.seed(repo)
    if statistics == "production":
        with rt.database.write() as db:
            db.execute("ANALYZE")
            db.execute("DELETE FROM sqlite_stat1 WHERE tbl IN ('communities', 'community_members')")
            db.executemany("INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?,?,?)",
                           _PRODUCTION_STATS)
        rt.database.close_local()
    captured: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str) and "c.board_key AS board_key" in sql:
            captured.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    assert cases.board_key_rows(rt, world.nb, 0, 1) == [
        ("K1", "bk-a"), ("K2", "bk-a"), ("K3", "bk-b")]
    monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    assert len(captured) == 1
    sql, params = captured[0]
    steps = [row[3] for row in sqlite3.Connection.execute(
        rt.database.connect(), "EXPLAIN QUERY PLAN " + sql, params)]
    assert len(steps) == 2, steps
    assert steps[0].startswith("SEARCH c USING INDEX idx_communities_nb_level "
                               "(notebook_id=? AND level=?)"), steps
    assert steps[1].startswith("SEARCH m USING INDEX "), steps
    assert ("idx_commmem_nb_comm (notebook_id=? AND community_id=?)" in steps[1]
            or "uq_community_members_sync_key (community_id=?" in steps[1]), steps
