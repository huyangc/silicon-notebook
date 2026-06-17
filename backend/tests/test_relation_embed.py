import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.services.retrieval import relation_embed_text


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_relation_embed_text_combines_fields():
    t = relation_embed_text("Regulated Cascode", "derived_from", "Cascode",
                            ["adds a gain stage to boost output resistance"])
    assert "Regulated Cascode" in t and "Cascode" in t
    assert "derived_from" in t
    assert "gain stage" in t


def test_relation_embed_text_truncates_evidence():
    t = relation_embed_text("A", "supports", "B", ["x" * 1000], max_evidence_chars=50)
    # evidence 截断到 50;头部 "A —supports→ B." 不计入截断额度
    assert t.count("x") <= 50


def test_relation_embed_text_handles_empty_evidence():
    t = relation_embed_text("A", "about", "B", [])
    assert t == "A —about→ B."


def test_relation_embeddings_table_schema(repo):
    with repo._connect() as db:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(relation_embeddings)")]
    assert cols == ["relation_id", "notebook_id", "vector", "created_at"]


def test_relation_embeddings_idempotent_reinit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())
    SQLiteRepository(Settings())  # 第二次 init 同库不应抛错(CREATE TABLE IF NOT EXISTS)
