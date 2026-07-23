"""Task 4: rebuild_unified_kg must invoke rebuild_communities after clustering
settles (fail-open — a community error must never break the KG rebuild)."""
import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients

NOW = "2026-07-07T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s", nb.id, "p", "md", "ready", NOW, NOW))
        for oid, nm in (("o1", "Alpha"), ("o2", "Beta")):
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": nm}), "[]", "s", NOW, NOW))
    return nb


def test_rebuild_unified_kg_calls_rebuild_communities(repo, monkeypatch):
    nb = _seed(repo)
    called = {}
    orig = repo._runtime.knowledge_lifecycle.rebuild_communities

    def spy(notebook_id, level=0, force=False):
        called["nb"] = notebook_id
        return orig(notebook_id, level, force=force)

    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "rebuild_communities", spy)
    repo.rebuild_unified_kg(nb.id, force=True)
    assert called.get("nb") == nb.id


def test_rebuild_unified_kg_failopen_on_community_error(repo, monkeypatch):
    """A community-build exception must NOT break rebuild_unified_kg; it should
    emit communities_rebuild_failed and the rebuild should still return."""
    nb = _seed(repo)

    def boom(notebook_id, level=0, force=False):
        raise RuntimeError("community boom")

    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "rebuild_communities", boom)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))
    # Must not raise.
    out = repo.rebuild_unified_kg(nb.id, force=True)
    assert isinstance(out, int)
    assert any(e.get("kind") == "communities_rebuild_failed" for e in events)
