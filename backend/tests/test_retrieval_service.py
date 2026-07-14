"""W2.1: RetrievalService —— 检索原语的显式接口。

让 reasoning/graph 等消费者依赖一个明确的检索接口，而非穿透 repo 的私有方法
（`repo._retrieve_scored` 等）。本增量零行为变化：service 委托 repo 现有实现，
为后续把实现逐步搬进 service 立好接缝。
"""
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.retrieval_service import RetrievalService
from app.services.sqlite_repository import SQLiteRepository


def _repo_with_kg(tmp_path):
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s")))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"L{i}", "object_type": "concept",
             "payload": {"name": f"运算放大器{i}", "definition": f"定义{i}"},
             "evidence": []} for i in range(5)]
    repo.store_kg(nb.id, None, objs, [])
    return repo, nb.id


def test_retrieve_scored_delegates_to_repo(tmp_path):
    repo, nb = _repo_with_kg(tmp_path)
    via_service = repo.retrieval.retrieve_scored(nb, "运算放大器")
    via_private = repo._retrieve_scored(nb, "运算放大器")
    assert [h.object_id for h in via_service] == [h.object_id for h in via_private]
    assert via_service  # 关键词命中，非空


def test_neighbors_elements_node_context_delegate(tmp_path):
    repo, nb = _repo_with_kg(tmp_path)
    oid = repo._retrieve_scored(nb, "运算放大器")[0].object_id
    assert ([h.object_id for h in repo.retrieval.retrieve_neighbors(nb, oid)]
            == [h.object_id for h in repo._retrieve_neighbors(nb, oid)])
    assert (repo.retrieval.retrieve_elements(nb, "运算放大器")
            == repo._retrieve_elements(nb, "运算放大器"))
    assert repo.retrieval.node_context(nb, oid) == repo.node_context(nb, oid)


def test_federated_and_ppr_delegate(tmp_path):
    repo, nb = _repo_with_kg(tmp_path)
    assert ([h.object_id for h in repo.retrieval.federated_retrieve(nb, "运算放大器")]
            == [h.object_id for h in repo.federated_retrieve(nb, "运算放大器")])
    assert ([c for c in repo.retrieval.ppr_retrieve(nb, "运算放大器")]
            == [c for c in repo._ppr_retrieve(nb, "运算放大器")])
