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
    monkeypatch.setenv("EMBED_PROVIDER", "local")   # embedder_configured=True
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)               # inject; no real model loads (lazy)
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

def test_cluster_and_candidate_crud(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [
        {"canonical_id": "K1", "member_object_id": "o1", "canonical_name": "MOSFET"},
        {"canonical_id": "K1", "member_object_id": "o2", "canonical_name": "MOSFET"},
    ])
    assert repo.cluster_map(nb.id) == {"o1": "K1", "o2": "K1"}
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    pend = repo.pending_merges(nb.id)
    assert len(pend) == 1 and pend[0]["status"] == "pending"
    repo.set_merge_decision(nb.id, pend[0]["id"], "rejected")
    assert repo.pending_merges(nb.id) == []
    assert repo.decided_pairs(nb.id) == {("K1", "K2"): "rejected"}

def test_set_merge_decision_rejects_bad_status(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    cid = repo.pending_merges(nb.id)[0]["id"]
    with pytest.raises(ValueError):
        repo.set_merge_decision(nb.id, cid, "maybe")
