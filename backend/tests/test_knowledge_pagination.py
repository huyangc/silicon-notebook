import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings()); bind_embedding_client(r, FakeEmbedder(dim=16)); return r


def test_list_knowledge_paginated(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":f"c{i}","object_type":"concept","payload":{"name":f"c{i}","section_path":""},"evidence":[]} for i in range(60)], [])
    p0 = repo.list_knowledge(nb.id, "concept", status=None, offset=0, limit=50)
    assert p0.total_count == 60 and len(p0.items) == 50 and p0.offset == 0 and p0.limit == 50
    p1 = repo.list_knowledge(nb.id, "concept", status=None, offset=50, limit=50)
    assert len(p1.items) == 10
    assert len(repo.list_knowledge(nb.id, "concept", status=None, offset=999, limit=50).items) == 0
