import pytest
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from app.core.config import Settings


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def test_add_and_read_relations(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    b = repo._test_insert_object(nb.id, "claim", {"name": "MOSFET has threshold voltage"})
    repo.add_relations(nb.id, None, [
        {"source_object_id": b, "target_object_id": a, "edge_type": "about",
         "evidence": [{"quote": "threshold voltage of the MOSFET"}]},
    ])
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1
    assert rels[0]["source_object_id"] == b
    assert rels[0]["target_object_id"] == a
    assert rels[0]["edge_type"] == "about"
    assert rels[0]["evidence"] == [{"quote": "threshold voltage of the MOSFET"}]
