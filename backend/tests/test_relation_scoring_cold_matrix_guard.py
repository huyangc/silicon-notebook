"""生产事故修复(第二部分):_retrieve_relations_scored branch-3(向量覆盖非空,
调用 top_k_sims 前先加载 _vector_matrix(nb, "relation_embeddings"))本身也不是
免费的 —— 加载是 O(N_relations × dim) 内存,生产环境百万级关系即数 GB,若矩阵
未 BLOB 化(仍是逐行 JSON 文本)更是灾难级解析耗时。这条加载不应该在 ask 路径
上对大库懒触发。

Fix:大库(not notebook_copy_stats(nb)["copyable"]) 且矩阵未暖在 _vector_cache
(通过 _vector_matrix_warm 的 peek,不触发 loader)→ 跳过语义打分,发
relation_scoring_skipped 事件,返回 []（不走 branch-2 的全量 JOIN 回退 ——
_relations_with_names(relation_ids=None) 本身对大库也是无界扫描，会重新引入
同一类灾难，只是换了张表；[] + 事件对调用方是 fail-open 的既有语义，与
federated_retrieve_relations/reasoning 的空结果处理一致）。

已暖(已在 _vector_cache 里且版本匹配)或小库：字节不变，走原路径。
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """embedder_configured=True(四个 EMBED_* env 都设)供关系向量真实产出。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Regulated Cascode"}, "evidence": []},
         {"local_id": "b", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []}],
        [{"source_local_id": "a", "target_local_id": "b", "edge_type": "derived_from", "evidence": []}])
    return nb


def _capture_events(repo, monkeypatch):
    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)
    return events


def _spy_vector_matrix(repo, monkeypatch):
    """Spy on _vector_matrix to record every (notebook_id, table) it is called
    with, without altering behavior."""
    calls = []
    orig = repo.retrieval.candidates._vector_matrix

    def wrapper(db, notebook_id, table, id_col):
        calls.append((notebook_id, table))
        return orig(db, notebook_id, table, id_col)

    monkeypatch.setattr(repo.retrieval.candidates, "_vector_matrix", wrapper)
    return calls


def test_large_notebook_cold_matrix_skips_semantic_scoring(repo, monkeypatch):
    """大库(copyable=False) + relation_embeddings 矩阵未暖(从未加载过)→
    _vector_matrix(relation_embeddings) 绝不被调用,发 relation_scoring_skipped
    事件,返回 []。"""
    nb = _seed(repo)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})
    calls = _spy_vector_matrix(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "regulated cascode derived from cascode")

    assert result == []
    rel_matrix_calls = [c for c in calls if c[1] == "relation_embeddings"]
    assert rel_matrix_calls == [], "cold large-notebook matrix must NOT be loaded"
    skipped = [e for e in events if e.get("kind") == "relation_scoring_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["notebook_id"] == nb.id
    assert skipped[0]["reason"] == "large_matrix_cold"


def test_large_notebook_warm_matrix_uses_normal_bounded_path(repo, monkeypatch):
    """大库 + 矩阵已经暖在 _vector_cache 里(版本匹配)→ 正常有界路径(top_k_sims),
    不发 relation_scoring_skipped 事件,不返回 []（除非本来就该有结果）。"""
    nb = _seed(repo)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})

    # Pre-warm the cache the same way _vector_matrix would, so peek() sees it.
    with repo._connect() as db:
        repo.retrieval.candidates._vector_matrix(db, nb.id, "relation_embeddings", "relation_id")

    calls = _spy_vector_matrix(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "regulated cascode derived from cascode")

    rel_matrix_calls = [c for c in calls if c[1] == "relation_embeddings"]
    assert rel_matrix_calls, "warm matrix path should still call _vector_matrix (cache hit, cheap)"
    skipped = [e for e in events if e.get("kind") == "relation_scoring_skipped"]
    assert len(skipped) == 0
    assert any(r.source_object_id for r in result) or result == []  # sanity: no crash
    assert len(result) >= 1


def test_small_notebook_cold_matrix_uses_normal_path(repo, monkeypatch):
    """小库(copyable=True):字节不变——即便矩阵冷也走原有 top_k_sims 路径,
    不发 relation_scoring_skipped 事件。"""
    nb = _seed(repo)
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": True, "size": {}})
    calls = _spy_vector_matrix(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "regulated cascode derived from cascode")

    rel_matrix_calls = [c for c in calls if c[1] == "relation_embeddings"]
    assert rel_matrix_calls, "small notebook must still load the matrix"
    skipped = [e for e in events if e.get("kind") == "relation_scoring_skipped"]
    assert len(skipped) == 0
    assert len(result) >= 1


def test_no_relations_early_return_unaffected(repo, monkeypatch):
    """既有分支(关系表为空 → 早退 [])与新守卫无关,保持不变,即便大库。"""
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})
    events = _capture_events(repo, monkeypatch)

    result = repo.retrieval.candidates._retrieve_relations_scored(nb.id, "anything")

    assert result == []
    skipped = [e for e in events if e.get("kind") == "relation_scoring_skipped"]
    assert len(skipped) == 0
