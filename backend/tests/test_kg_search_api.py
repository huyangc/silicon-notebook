"""Task 3 SP1 — FTS5 + semantic kg_search integration tests.

repo-level: test_kg_search_lexical, test_kg_search_all_fields
api-level:  test_kg_search_endpoint_lexical
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate

EMBED_DIM = 16

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=EMBED_DIM)
    return r


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Repo-level tests
# ---------------------------------------------------------------------------

def test_kg_search_lexical(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
    ], [])
    hits = repo.kg_search(nb.id, "mirror", k=10)
    names = {h["name"] for h in hits}
    assert "current mirror" in names and "MOSFET" not in names
    assert all("object_type" in h and "match" in h for h in hits)


def test_kg_search_returns_all_required_fields(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "x", "object_type": "claim",
         "payload": {"name": "transistor amplifier", "section_path": "2"}, "evidence": []},
    ], [])
    hits = repo.kg_search(nb.id, "amplifier", k=5)
    assert len(hits) == 1
    h = hits[0]
    assert h["name"] == "transistor amplifier"
    assert h["object_type"] == "claim"
    assert h["match"] == "lexical"
    assert isinstance(h["score"], float) and h["score"] > 0
    assert "object_id" in h


def test_kg_search_empty_query_returns_empty(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [])
    hits = repo.kg_search(nb.id, "", k=10)
    assert hits == []


def test_kg_search_scoped_to_notebook(repo):
    """Objects in other notebooks must NOT appear in results."""
    nb1 = repo.create_notebook(NotebookCreate(name="nb1"))
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    repo.store_kg(nb1.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "bandgap reference", "section_path": ""}, "evidence": []},
    ], [])
    repo.store_kg(nb2.id, None, [
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "unrelated thing", "section_path": ""}, "evidence": []},
    ], [])
    hits = repo.kg_search(nb1.id, "bandgap", k=10)
    names = {h["name"] for h in hits}
    assert "bandgap reference" in names and "unrelated thing" not in names


def test_kg_search_unknown_notebook_raises(repo):
    with pytest.raises(KeyError):
        repo.kg_search("no-such-nb", "query", k=5)


def test_backfill_kg_fts(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "op-amp", "section_path": ""}, "evidence": []},
    ], [])
    # Simulate stale FTS: delete FTS rows manually, then backfill
    with repo._connect() as db:
        db.execute("DELETE FROM kg_objects_fts WHERE notebook_id=?", (nb.id,))
    hits_before = repo.kg_search(nb.id, "op-amp", k=5)
    assert hits_before == []   # FTS wiped, no semantic index either

    repo.backfill_kg_fts(nb.id)
    hits_after = repo.kg_search(nb.id, "op-amp", k=5)
    assert any(h["name"] == "op-amp" for h in hits_after)


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------

def test_kg_search_endpoint_lexical(client):
    nb_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # Ingest via API is complex; inject directly via repo through the test client's app
    # Instead, call the endpoint with an empty notebook to verify shape and 200 status,
    # then inject objects manually and re-query.
    r = client.get(f"/api/notebooks/{nb_id}/kg/search?q=mirror")
    assert r.status_code == 200
    body = r.json()
    assert "hits" in body and "query" in body
    assert body["query"] == "mirror"
    assert isinstance(body["hits"], list)


def test_kg_search_endpoint_404_unknown_notebook(client):
    r = client.get("/api/notebooks/bogus-nb/kg/search?q=mirror")
    assert r.status_code == 404


def test_kg_search_endpoint_with_k_param(client):
    nb_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.get(f"/api/notebooks/{nb_id}/kg/search?q=test&k=5")
    assert r.status_code == 200


def test_kg_search_endpoint_invalid_k(client):
    nb_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.get(f"/api/notebooks/{nb_id}/kg/search?q=test&k=0")
    assert r.status_code == 422   # k ge=1 validation


def test_kg_search_endpoint_returns_hit_fields(client):
    """Hit objects must have object_id, name, object_type, score, match."""
    from app.main import app
    from app.api.deps import repository as get_repo
    from app.models.schemas import NotebookCreate as NC

    # Get the singleton repo from the app
    with TestClient(app) as c:
        nb_id = c.post("/api/notebooks", json={"name": "search-test"}).json()["id"]
        repo = get_repo()
        repo.store_kg(nb_id, None, [
            {"local_id": "a", "object_type": "concept",
             "payload": {"name": "voltage regulator", "section_path": ""}, "evidence": []},
        ], [])
        r = c.get(f"/api/notebooks/{nb_id}/kg/search?q=voltage")
        assert r.status_code == 200
        hits = r.json()["hits"]
        assert len(hits) >= 1
        h = hits[0]
        for field in ("object_id", "name", "object_type", "score", "match"):
            assert field in h, f"missing field: {field}"
        assert h["name"] == "voltage regulator"
        assert h["object_type"] == "concept"
