"""ask_stage 埋点:_retrieve_scored 每次调用 emit 一条阶段耗时事件;
personalized_ppr 的 stats 出参回报迭代轮数。纯观测,不改检索结果。"""
import numpy as np
import pytest
import scipy.sparse as sp

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg import scale_index as si
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


def test_personalized_ppr_stats_reports_iters():
    # 3 节点环图,列随机;stats 出参收集迭代轮数
    A = sp.csr_matrix(np.array([[0, 0, 1.0], [1.0, 0, 0], [0, 1.0, 0]]))
    reset = np.array([1.0, 0.0, 0.0])
    stats = {}
    x = si.personalized_ppr(A, reset, damping=0.5, stats=stats)
    assert x.shape == (3,)
    assert stats.get("iters", 0) >= 1


def test_personalized_ppr_stats_none_unchanged():
    # 默认 stats=None 路径:结果与传 dict 完全一致(纯观测不改数值)
    A = sp.csr_matrix(np.array([[0, 0, 1.0], [1.0, 0, 0], [0, 1.0, 0]]))
    reset = np.array([1.0, 0.0, 0.0])
    x1 = si.personalized_ppr(A, reset, damping=0.5)
    x2 = si.personalized_ppr(A, reset, damping=0.5, stats={})
    assert np.array_equal(x1, x2)


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    def _make():
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        r = SQLiteRepository(Settings())
        bind_all_embedding_clients(r, FakeEmbedder(dim=16))
        nb = r.create_notebook(NotebookCreate(name="nb"))
        r.store_kg(nb.id, None, [
            {"local_id": "A", "object_type": "concept",
             "payload": {"name": "带隙基准电压源"}, "evidence": []},
        ], [])
        return r, nb.id
    return _make


def _capture_events(repo):
    captured = []
    orig = repo.event_log.emit
    repo.event_log.emit = lambda e: (captured.append(e), orig(e))[1]
    return captured


def test_retrieve_scored_emits_ask_stage(repo_factory):
    repo, nb = repo_factory()
    events = _capture_events(repo)
    repo._retrieve_scored(nb, "什么是带隙基准")
    kinds = [e.get("kind") for e in events]
    assert "ask_stage" in kinds
    ev = next(e for e in events if e.get("kind") == "ask_stage")
    assert ev.get("site") == "_retrieve_scored"
    assert ev.get("stage") == "kg_candidates"
    assert ev.get("latency_ms") == ev.get("total_ms")
    assert "total_ms" in ev and "embed_ms" in ev and "score_ms" in ev
    assert "ann_ms" not in ev
    assert {
        "candidate_ms",
        "scale_index_ms",
        "kg_ann_open_ms",
        "kg_ann_knn_ms",
        "kg_delta_ms",
        "kg_lexical_ms",
    } <= ev.keys()
