from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    return SQLiteRepository(Settings())


def test_kg_ready_false_before_kg_true_after(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.get_notebook(nb.id).kg_ready is False
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"}, "evidence": []},
    ], [])
    assert repo.get_notebook(nb.id).kg_ready is True
