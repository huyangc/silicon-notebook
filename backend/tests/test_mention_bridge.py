import sqlite3

import pytest
from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, SCHEMA_VERSION


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_mention_bridge_tables(repo):
    assert {"notebook_id", "claim_object_id", "concept_canonical_id", "matched_alias"} <= _cols(repo, "mention_edges")
    assert {"notebook_id", "canonical_a", "canonical_b", "bridge_claims"} <= _cols(repo, "concept_comentions")
    assert "mention_seq" in _cols(repo, "unified_kg_state")


def test_deployed_v8_db_gets_backfilled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())
    raw = sqlite3.connect(tmp_path / "m.db")
    raw.execute("DROP TABLE mention_edges")
    raw.execute("DROP TABLE concept_comentions")
    raw.execute("ALTER TABLE unified_kg_state DROP COLUMN mention_seq")
    raw.execute("PRAGMA user_version = 8")
    raw.commit(); raw.close()
    r2 = SQLiteRepository(Settings())
    assert "claim_object_id" in _cols(r2, "mention_edges")
    assert "bridge_claims" in _cols(r2, "concept_comentions")
    assert "mention_seq" in _cols(r2, "unified_kg_state")


def test_schema_version_is_9():
    assert SCHEMA_VERSION == 9
