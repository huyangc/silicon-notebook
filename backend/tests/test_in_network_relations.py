import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


def _ids_by_name(repo, nb_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
            (nb_id,)).fetchall()
    return {json.loads(r["payload"])["name"]: r["id"] for r in rows}


def _hit(oid, name):
    # use claim type to avoid the concept-cluster de-dup path in _answer_context
    return RetrievedKnowledge(object_id=oid, object_type="claim",
                              payload={"name": name}, evidence=[])


def test_in_network_relation_surfaced_in_context(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim Alpha", "section_path": "1"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Claim Beta", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "supports", "evidence": []},
    ])
    ids = _ids_by_name(repo, nb.id)
    hits = [_hit(ids["Claim Alpha"], "Claim Alpha"),
            _hit(ids["Claim Beta"], "Claim Beta")]
    block, id_map = repo._answer_context(nb.id, hits)
    assert "relations:" in block
    assert "supports" in block
    # the relation references the two k-ids the hits received
    keys = list(id_map.keys())
    assert (f"{keys[0]} -[supports]-> {keys[1]}" in block
            or f"{keys[1]} -[supports]-> {keys[0]}" in block)


def test_no_relations_line_when_single_hit(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim Alpha", "section_path": "1"}, "evidence": []},
    ], [])
    ids = _ids_by_name(repo, nb.id)
    block, _ = repo._answer_context(nb.id, [_hit(ids["Claim Alpha"], "Claim Alpha")])
    assert "relations:" not in block   # need >=2 in-context objects for a relation


def test_no_relations_line_when_edge_endpoint_not_in_context(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim Alpha", "section_path": "1"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Claim Beta", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "supports", "evidence": []},
    ])
    ids = _ids_by_name(repo, nb.id)
    # only A is in context -> the A->B edge is NOT in-network
    block, _ = repo._answer_context(nb.id, [_hit(ids["Claim Alpha"], "Claim Alpha")])
    assert "relations:" not in block
