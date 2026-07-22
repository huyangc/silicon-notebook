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
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=EMBED_DIM))
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


# ---------------------------------------------------------------------------
# Fix 1 continuity test: search → canonical id → kg_neighbors non-empty
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_with_embed(tmp_path, monkeypatch):
    """Repo fixture with FakeEmbedder wired up (needed for build_scale_index)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=EMBED_DIM))
    return r


def test_search_hit_folded_to_canonical_id_and_neighbors_nonempty(repo_with_embed):
    """CONTINUITY: Fix 1 — kg_search must return canonical K- ids after rebuild
    so that kg_neighbors (viz fast-path) can expand a search hit.

    Setup:
      - Two concepts with the same name ("MOSFET" / "mosfet") → rebuild clusters them
        under one K-<canonical> id.
      - A third concept ("current mirror") related to the first.
      - After rebuild_unified_kg + build_scale_index, kg_search("mosfet") must
        return a hit whose object_id starts with "K-" (the canonical id).
      - kg_neighbors on that canonical id must return non-empty nodes (click-to-expand
        works: the canonical id exists in the viz graph and has a neighbour).
    """
    nb = repo_with_embed.create_notebook(NotebookCreate(name="continuity-nb"))

    # Ingest two same-named concepts from separate "sources" so they cluster.
    repo_with_embed.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "c", "target_local_id": "a",
         "edge_type": "uses", "evidence": []},
    ])
    repo_with_embed.store_kg(nb.id, None, [
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "mosfet", "section_path": ""}, "evidence": []},
    ], [])

    # Build the folded KG and scale index.
    repo_with_embed.rebuild_unified_kg(nb.id)
    repo_with_embed.build_scale_index(nb.id)

    # Verify cluster map exists and both MOSFET concepts are clustered.
    cmap = repo_with_embed.cluster_map(nb.id)
    assert len(cmap) >= 2, f"Expected at least 2 cluster entries; cmap={cmap}"
    mosfet_canonicals = {v for v in cmap.values()}
    assert any(c.startswith("K-") for c in mosfet_canonicals), (
        f"Expected at least one K- canonical id in cluster_map values; got {mosfet_canonicals}"
    )

    # Search and check Fix 1: returned object_id is canonical (K- prefix).
    hits = repo_with_embed.kg_search(nb.id, "mosfet", k=10)
    assert hits, "kg_search('mosfet') returned no hits"
    mosfet_hits = [h for h in hits if "mosfet" in h["name"].lower()]
    assert mosfet_hits, f"No mosfet hit in results: {hits}"
    hit = mosfet_hits[0]
    assert hit["object_id"].startswith("K-"), (
        f"Fix 1 FAILED: search hit object_id={hit['object_id']!r} is not a canonical K- id. "
        "kg_neighbors fast-path will return empty for this hit (click-to-expand broken)."
    )

    # Check click-to-expand: kg_neighbors on the canonical id must be non-empty.
    nbr = repo_with_embed.kg_neighbors(nb.id, hit["object_id"], cap=10)
    assert nbr["nodes"], (
        f"Fix 1 FAILED: kg_neighbors(canonical_id={hit['object_id']!r}) returned empty nodes. "
        "Click-to-expand is broken even after Fix 1 — check viz graph / scale index."
    )
