import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.eval.retrieval_metrics import run_recall


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_run_recall_reports_relation_metrics(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b", "edge_type": "derived_from", "evidence": []}]
    repo.store_kg(nb.id, None, objects, relations)
    with repo._connect() as db:
        rid = db.execute("SELECT id FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchone()["id"]
    questions = [{"id": "g1", "question": "regulated cascode derived from cascode",
                  "gold_relation_ids": [rid]}]
    rows = run_recall(repo, nb.id, questions)
    assert rows and rows[0]["id"] == "g1"
    assert rows[0]["relation_recall_at_k"] == 1.0   # 关系被检索到


class _FakeRetrieval:
    """run_recall 走公开 retrieval 端口(Task 27)——fake 镜像 RetrievalService 形状。"""
    def __init__(self, hits):
        self._hits = hits
    def retrieve_scored(self, nb, q):
        class H:
            def __init__(s, oid): s.object_id = oid
        return [H(o) for o in self._hits]
    def retrieve_relations_scored(self, nb, q): return []


class _FakeRepo:
    def __init__(self, hits, cmap):
        self.retrieval, self._cmap = _FakeRetrieval(hits), cmap
    def cluster_map(self, nb): return self._cmap


def test_run_recall_maps_object_ids_to_canonical():
    from app.eval.retrieval_metrics import run_recall
    # 检索到代表 oA;gold 是被折掉的同簇成员 oB。canonical 映射后应判命中。
    repo = _FakeRepo(hits=["oA", "x", "y"], cmap={"oA": "K", "oB": "K"})
    q = [{"id": "g1", "question": "?", "gold_object_ids": ["oB"]}]
    rows = run_recall(repo, "nb", q, k=12)
    assert rows[0]["recall_at_k"] == 1.0   # oB→K,oA→K,canonical 层命中
