"""索引与构建统一整合:聚合状态 / 取消 / built_at。"""
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_index_status_aggregates_three_systems(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    # 纯读、不得触发 viz build
    called = {"viz": 0}
    monkeypatch.setattr(repo, "_spawn_viz_build", lambda *a, **k: called.__setitem__("viz", called["viz"] + 1))
    out = repo.index_status(nb.id)
    assert set(out) == {"kg", "unified_kg", "scale_index"}
    assert set(out["kg"]) >= {"ready", "building", "pending_sources"}
    assert set(out["unified_kg"]) >= {"dirty", "building", "last_rebuild_at"}
    assert "state" in out["scale_index"]
    # 与各自旧 status 一致
    assert out["scale_index"]["state"] == repo.scale_index_status(nb.id)["state"]
    assert out["unified_kg"]["dirty"] == repo.unified_kg_status(nb.id)["dirty"]
    assert called["viz"] == 0   # 聚合是纯读
    # kg 子字典值级对照 NotebookSummary
    nb2 = repo.get_notebook(nb.id)
    assert out["kg"] == {"ready": bool(nb2.kg_ready), "building": bool(nb2.kg_building),
                         "pending_sources": int(nb2.kg_pending_sources)}


def test_index_status_kg_pending_matches_summary(repo):
    """A source with source_elements but no knowledge_objects is pending KG."""
    nb = repo.create_notebook(NotebookCreate(name="pend"))
    now = "2026-07-09T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,created_at) VALUES (?,?,?,?,?,?)",
                   ("e1", "s1", "paragraph", "loc", "hello", now))
    out = repo.index_status(nb.id)
    summary = repo.get_notebook(nb.id)
    assert out["kg"]["pending_sources"] == summary.kg_pending_sources
    assert out["kg"]["pending_sources"] >= 1


def test_cancel_dequeues_queued(repo):
    nb = repo.create_notebook(NotebookCreate(name="q"))
    # 手动放入空闲队列(镜像 trigger_scale_index_rebuild(when=idle) 的效果)
    with repo._scale_building_lock:
        repo._scale_idle_queue[nb.id] = "auto"
    assert repo.scale_index_status(nb.id)["state"] == "queued"
    out = repo.cancel_scale_index(nb.id)
    assert out["cancelled"] is True
    assert nb.id not in repo._scale_idle_queue
    assert repo.scale_index_status(nb.id)["state"] != "queued"


def test_cancel_building_refuses(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    with repo._scale_building_lock:
        repo._scale_building.add(nb.id)
    try:
        out = repo.cancel_scale_index(nb.id)
        assert out["cancelled"] is False
        assert out["reason"] == "building_not_interruptible"
    finally:
        with repo._scale_building_lock:
            repo._scale_building.discard(nb.id)


def test_cancel_noop_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="x"))
    out = repo.cancel_scale_index(nb.id)   # 无队列项、未在建
    assert out["cancelled"] is False


def test_dequeue_returns_bool(repo):
    nb = repo.create_notebook(NotebookCreate(name="d"))
    assert repo._dequeue_scale_idle(nb.id) is False   # 不存在
    with repo._scale_building_lock:
        repo._scale_idle_queue[nb.id] = "auto"
    assert repo._dequeue_scale_idle(nb.id) is True     # 移除
    assert repo._dequeue_scale_idle(nb.id) is False    # 再移除幂等
