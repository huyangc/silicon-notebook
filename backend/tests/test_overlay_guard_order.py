"""生产事故修复:默认 chunk-mode ask 在 1.13M 节点库上挂起。

根因(代码走查):_chunk_kg_overlay 的大库守卫(PR#164,
not notebook_copy_stats(nb)["copyable"] → 空 overlay + graph_walk_refused 事件)
排在种子收集之后 —— 但种子收集本身会调用 federated_retrieve 与
federated_retrieve_relations,后者 branch-3(向量覆盖非空)会加载
_vector_matrix(nb, "relation_embeddings") 全量关系向量矩阵(生产环境百万级
行 × 1024 维 = 数 GB;若行仍是回填中的 JSON 文本更是灾难级解析耗时)。
大库场景这些种子随后被守卫直接丢弃(返回 ("", {}, []))—— 白算 + 可能挂起。

Fix:把大库守卫挪到函数最顶部,任何检索之前。大库行为字节不变(同样的空
overlay + 同样的事件),只是不再白跑/挂起;小库字节不变。
"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"source_id": "src-x", "source_title": "test", "element_id": "el-x-0001",
                        "element_type": "paragraph", "location_label": "1", "quoted_span": "cascode",
                        "confidence": 1.0}]},
         {"local_id": "b", "object_type": "claim", "payload": {"name": "Cascode raises output resistance"}, "evidence": []}],
        [{"source_local_id": "b", "target_local_id": "a", "edge_type": "about", "evidence": []}])
    return nb


def _capture_events(repo, monkeypatch):
    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)
    return events


def _spy(repo, monkeypatch, name):
    called = {"n": 0}
    owner = repo.retrieval.candidates
    orig = getattr(owner, name)

    def wrapper(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(owner, name, wrapper)
    return called


def test_large_notebook_skips_seed_retrieval_entirely(repo, monkeypatch):
    """大库(copyable=False):federated_retrieve / federated_retrieve_relations
    绝不被调用(spy 计数为 0),直接发 graph_walk_refused 事件并返回 ("", {}, [])。"""
    nb = _seed(repo)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})
    node_calls = _spy(repo, monkeypatch, "federated_retrieve")
    rel_calls = _spy(repo, monkeypatch, "federated_retrieve_relations")
    events = _capture_events(repo, monkeypatch)

    result = repo._chunk_kg_overlay(nb.id, "cascode output resistance", "", id_offset=5)

    assert result == ("", {}, [])
    assert node_calls["n"] == 0, "large notebook must NOT call federated_retrieve"
    assert rel_calls["n"] == 0, "large notebook must NOT call federated_retrieve_relations"
    refusal = [e for e in events if e.get("kind") == "graph_walk_refused"]
    assert len(refusal) == 1
    assert refusal[0]["notebook_id"] == nb.id
    assert refusal[0]["reason"] == "large_notebook"
    assert refusal[0]["site"] == "chunk_kg_overlay"


def test_small_notebook_keeps_legacy_seed_retrieval(repo, monkeypatch):
    """小库(copyable=True):字节不变——两路种子检索仍被调用,无 refusal 事件,
    行为与守卫上移前完全一致。"""
    nb = _seed(repo)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": True, "size": {}})
    node_calls = _spy(repo, monkeypatch, "federated_retrieve")
    rel_calls = _spy(repo, monkeypatch, "federated_retrieve_relations")
    events = _capture_events(repo, monkeypatch)

    block, id_map, hits = repo._chunk_kg_overlay(nb.id, "cascode output resistance", "", id_offset=5)

    assert node_calls["n"] == 1
    assert rel_calls["n"] == 1
    assert isinstance(block, str) and id_map
    assert all(hasattr(h, "relevance") for h in hits)
    refusal = [e for e in events if e.get("kind") == "graph_walk_refused"]
    assert len(refusal) == 0


