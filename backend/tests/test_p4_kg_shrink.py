import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


def test_kg_auto_extract_defaults_false():
    assert Settings().kg_auto_extract is False


def test_kg_auto_extract_env_override(monkeypatch):
    monkeypatch.setenv("KG_AUTO_EXTRACT", "true")
    assert Settings().kg_auto_extract is True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_concept(repo, nb_id, local_id="K1", name="Engram"):
    repo.store_kg(nb_id, None, [
        {"local_id": local_id, "object_type": "concept",
         "payload": {"name": name, "section_path": "1"}, "evidence": []},
    ], [])


def test_notebook_has_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._notebook_has_kg(nb.id) is False
    _seed_concept(repo, nb.id)
    assert repo._notebook_has_kg(nb.id) is True


def test_should_extract_false_when_auto_off_and_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._should_extract_kg(nb.id) is False


def test_should_extract_true_when_auto_on(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_auto_extract", True)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._should_extract_kg(nb.id) is True


def test_should_extract_true_when_notebook_already_has_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    _seed_concept(repo, nb.id)
    assert repo._should_extract_kg(nb.id) is True
