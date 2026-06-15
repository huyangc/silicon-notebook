import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repo with FakeEmbedder but embedder_configured=True.
    Mirrors test_reasoning_retrieval.py::rrepo — EMBED_* MUST be set (else
    embedder_configured is False and every embed path early-returns, so chunk
    vectors never get written), and the network client is replaced by
    FakeEmbedder; LLM keys cleared so answer paths stay offline (the .env
    env_file would otherwise leak real keys)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_chunks_tables_exist(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(chunks)").fetchall()}
        assert {"id","notebook_id","source_id","text","section_path","element_ids"} <= cols
        ecols = {r["name"] for r in db.execute("PRAGMA table_info(chunk_embeddings)").fetchall()}
        assert {"chunk_id","notebook_id","vector"} <= ecols
