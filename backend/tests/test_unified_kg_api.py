import datetime

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


def test_object_context_endpoint_preserves_formula_element_type(client):
    from app.api.deps import repository

    nb = client.post("/api/notebooks", json={"name": "DeepSeek-V4"}).json()["id"]
    repo = repository()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    formula = r"C _ {l} = 2 \sigma (\tilde {C} _ {l}).\tag{7}"
    with repo._connect() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,'file','extracted','parsed',?,'',0,'','','academic_paper',?,?)",
            ("source-formula", nb, "2606.19348v1.pdf", "2606.19348v1.pdf", now, now),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,'{}',?)",
            ("element-formula", "source-formula", "formula", "eq. 7", formula, now),
        )
    repo.store_kg(
        nb,
        "source-formula",
        [{
            "local_id": "formula",
            "object_type": "formula",
            "payload": {"name": formula, "section_path": "2"},
            "evidence": [{
                "source_id": "source-formula",
                "source_title": "2606.19348v1.pdf",
                "element_id": "element-formula",
                "element_type": "paragraph",
                "location_label": "old location",
                "quoted_span": formula,
                "confidence": 0.98,
            }],
        }],
        [],
    )
    with repo._connect() as db:
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?",
            (nb,),
        ).fetchone()["id"]

    response = client.get(f"/api/notebooks/{nb}/objects/{object_id}/context")
    assert response.status_code == 200
    occurrence = response.json()["occurrences"][0]
    assert occurrence["element_type"] == "formula"
    assert occurrence["location_label"] == "eq. 7"
    assert occurrence["element_text"] == formula
    assert occurrence["confidence"] == 0.98
