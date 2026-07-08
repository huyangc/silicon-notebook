import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
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


def _table_cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_canonical_relations_table(repo):
    assert {"notebook_id", "canonical_src", "edge_type", "canonical_tgt",
            "support_count", "source_count", "sample_relation_ids",
            "updated_at"} <= _table_cols(repo, "canonical_relations")
    assert "canonical_rel_seq" in _table_cols(repo, "unified_kg_state")


def test_deployed_v7_db_gets_backfilled(tmp_path, monkeypatch):
    # 模拟已部署 user_version=7 的库:全新建库后删掉新表/新列、回拨版本号,
    # 再次实例化必须经 _migration_8 补齐(schema-migration-convention 教训用例)。
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())  # 建全新库(version=SCHEMA_VERSION)
    raw = sqlite3.connect(tmp_path / "m.db")
    raw.execute("DROP TABLE canonical_relations")
    raw.execute("ALTER TABLE unified_kg_state DROP COLUMN canonical_rel_seq")
    raw.execute("PRAGMA user_version = 7")
    raw.commit(); raw.close()
    r2 = SQLiteRepository(Settings())  # 重新迁移:必须跑 _migration_8
    assert "canonical_src" in _table_cols(r2, "canonical_relations")
    assert "canonical_rel_seq" in _table_cols(r2, "unified_kg_state")


def test_schema_version_bumped():
    assert SCHEMA_VERSION == 8
