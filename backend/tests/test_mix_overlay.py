import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
         {"local_id": "b", "object_type": "claim", "payload": {"name": "Cascode raises output resistance"}, "evidence": []}],
        [{"source_local_id": "b", "target_local_id": "a", "edge_type": "about", "evidence": []}])
    return nb


def test_overlay_block_idmap_hits(repo):
    nb = _seed(repo)
    block, id_map, hits = repo._chunk_kg_overlay(nb.id, "cascode output resistance", "", id_offset=5)
    assert isinstance(block, str) and id_map
    assert any(int(k[1:]) >= 6 for k in id_map)          # id_offset=5 → key 从 k6
    assert all(hasattr(h, "relevance") for h in hits)


def test_overlay_empty_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="e"))
    assert repo._chunk_kg_overlay(nb.id, "x", "", 0) == ("", {}, [])
