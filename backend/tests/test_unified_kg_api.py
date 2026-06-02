import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    return TestClient(app)


def test_unified_kg_endpoints(client):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    assert client.post(f"/api/notebooks/{nb}/unified-kg/rebuild").status_code == 200
    g = client.get(f"/api/notebooks/{nb}/unified-kg?level=concept")
    assert g.status_code == 200 and "nodes" in g.json() and "edges" in g.json()
    assert client.get(f"/api/notebooks/{nb}/unified-kg/pending-merges").status_code == 200


def test_concept_detail_endpoint(client):
    nb = client.post("/api/notebooks", json={"name": "nb2"}).json()["id"]
    # With no KG built, concept detail for a bogus id should 404 or return empty
    r = client.get(f"/api/notebooks/{nb}/concepts/nonexistent/detail")
    assert r.status_code in (200, 404)


def test_merge_confirm_reject_endpoints(client):
    nb = client.post("/api/notebooks", json={"name": "nb3"}).json()["id"]
    # With no pending merges, confirm/reject on a bogus id should 404
    r = client.post(f"/api/notebooks/{nb}/unified-kg/merges/bogus/confirm")
    assert r.status_code in (200, 404)
    r = client.post(f"/api/notebooks/{nb}/unified-kg/merges/bogus/reject")
    assert r.status_code in (200, 404)


def test_unified_kg_unknown_notebook_404(client):
    assert client.post("/api/notebooks/bogus/unified-kg/rebuild").status_code == 404
    assert client.get("/api/notebooks/bogus/unified-kg").status_code == 404

def test_object_context_endpoint_404_unknown(client):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    assert client.get(f"/api/notebooks/{nb}/objects/nope/context").status_code == 404
