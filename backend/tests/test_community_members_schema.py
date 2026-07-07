"""Schema test for the community_members reverse-index table (Task 2).

SQLiteRepository's real ctor is SQLiteRepository(settings) (db_path resolved from
settings.sqlite_path), so we point DATABASE_URL at tmp_path like the existing repo
fixtures (see test_ppr_retrieve.py) rather than the (path, settings) signature the
plan sketched.
"""
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_community_members_table(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(community_members)")}
    assert {
        "canonical_id",
        "notebook_id",
        "level",
        "community_id",
        "canonical_name",
        "centrality",
    } <= cols


def test_community_members_indexes(repo):
    with repo._connect() as db:
        idx = {r["name"] for r in db.execute("PRAGMA index_list(community_members)")}
    assert "idx_commmem_nb_can" in idx
    assert "idx_commmem_nb_comm" in idx
