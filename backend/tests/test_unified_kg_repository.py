import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)   # inject deterministic embedder
    return r

def test_store_kg_batch_embeds_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": "1"}, "evidence": []},
    ]
    repo.store_kg(nb.id, None, objs, [])
    with repo._connect() as db:
        rows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchall()
    assert len(rows) == 2                      # both nodes embedded
    assert len(json.loads(rows[0]["vector"])) == 16
