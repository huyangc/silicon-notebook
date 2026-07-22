import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


def _make_repo(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("RELATION_RETRIEVAL_ENABLED", "true" if flag else "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_bridge(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b",
                  "edge_type": "derived_from", "evidence": []}]
    repo.store_kg(nb.id, None, objects, relations)
    return nb


def test_seed_fusion_off_returns_base_unchanged(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch, flag=False)
    nb = _seed_bridge(repo)
    base = ["ko-x", "ko-y"]
    assert repo._graph_seed_fusion(nb.id, "regulated cascode", base) == base


def test_seed_fusion_on_adds_relation_endpoints(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch, flag=True)
    nb = _seed_bridge(repo)
    with repo._connect() as db:
        row = db.execute(
            "SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?",
            (nb.id,)).fetchone()
    base = ["ko-seed"]
    fused = repo._graph_seed_fusion(nb.id, "regulated cascode", base)
    assert "ko-seed" in fused                       # 不丢原种子(只增不减)
    assert fused[0] == "ko-seed"                    # base 种子保序在前
    assert row["source_object_id"] in fused or row["target_object_id"] in fused
