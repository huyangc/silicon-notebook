"""索引与构建统一整合:聚合状态 / 取消 / built_at。"""
import json
import os

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_scale_index_status_fail_soft_on_corrupt_manifest(repo):
    """损坏 manifest(read_manifest 刻意 raise)时 status() 必须 fail-soft:不把异常抛出、
    返回 state='stale'+stale_reason='corrupt'。否则前端 /index-status 拿不到 → 「索引与构建」
    块卡「加载中」→ 嵌在块里的 H8「重建索引」CTA 够不着,恰好在损坏(最需要重建)时修不了
    (codex P1)。H8 由 /checkup 独立报「已损坏」;这里保证状态可达 + 重建动作可发起。"""
    nb = repo.create_notebook(NotebookCreate(name="n"))
    scale_dir = repo._runtime.scale_artifact_store.scale_dir(nb.id)
    os.makedirs(str(scale_dir), exist_ok=True)
    with open(os.path.join(str(scale_dir), "manifest.json"), "w") as fh:
        fh.write("{ this is not valid json")  # 截断/损坏 → read_manifest raise

    st = repo.scale_index_status(nb.id)  # 不得 raise
    assert st["state"] == "stale"
    assert st.get("stale_reason") == "corrupt"
    assert st["exists"] is True and st["stale"] is True
    # 看板聚合端点同样 fail-soft、能返回(供前端渲染出块 + H8 重建入口)。
    assert repo.index_status(nb.id)["scale_index"]["state"] == "stale"


def test_scale_index_status_fail_soft_on_non_object_manifest(repo):
    """**合法但非对象**的 JSON manifest(如 ``[]``)也是结构性损坏(codex 第4轮 P1):read_manifest /
    read_manifest_version 须返 None(而非把 list 传给下游 ``.get``→AttributeError→/index-status 500、
    H8 身份路径被 checkup 当「探测不确定」漏报、重建 CTA 够不着)。

    **变异锚点**:去掉 read_manifest 的 ``isinstance(data, dict)`` 守卫(直接 return data)→ status()
    的 ``manifest.get`` 对 list 抛 AttributeError → scale_index_status raise、本用例红。"""
    nb = repo.create_notebook(NotebookCreate(name="n"))
    store = repo._runtime.scale_artifact_store
    scale_dir = store.scale_dir(nb.id)
    os.makedirs(str(scale_dir), exist_ok=True)
    with open(os.path.join(str(scale_dir), "manifest.json"), "w") as fh:
        fh.write("[]")  # 合法 JSON、但不是对象 → 结构性损坏

    # store 层:非对象 → None(与「读不出」同款),不抛。
    assert store.read_manifest(scale_dir) is None
    assert store.read_manifest_version(scale_dir) is None
    # H8 身份路径不抛(存在=True、version=None):否则 checkup 把 AttributeError 当探测不确定→漏报。
    assert store.scale_manifest_identity(nb.id) == (True, None)

    # status() fail-soft:stale/corrupt、不 raise、重建 CTA 可达。
    st = repo.scale_index_status(nb.id)
    assert st["state"] == "stale" and st.get("stale_reason") == "corrupt"
    assert st["exists"] is True and st["stale"] is True


def test_index_status_aggregates_three_systems(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    # 纯读、不得触发 viz build
    called = {"viz": 0}
    monkeypatch.setattr(repo._runtime.scale_artifacts, "_spawn_viz_build", lambda *a, **k: called.__setitem__("viz", called["viz"] + 1))
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
                         "pending_sources": int(nb2.kg_pending_sources), "job": None}


def test_index_status_kg_pending_matches_summary(repo):
    """A source with source_elements but no knowledge_objects is pending KG."""
    nb = repo.create_notebook(NotebookCreate(name="pend"))
    now = "2026-07-09T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,created_at) VALUES (?,?,?,?,?,?)",
                   ("e1", "s1", "paragraph", "loc", "hello", now))
    # Raw insert bypasses the pipeline's kg_mutation_seq bump; drop the seq-gated
    # pending memo (create_notebook warmed it at seq 0) so the read recomputes.
    from app.repositories.sqlite import knowledge_counts_cache
    knowledge_counts_cache.invalidate(nb.id)
    out = repo.index_status(nb.id)
    summary = repo.get_notebook(nb.id)
    assert out["kg"]["pending_sources"] == summary.kg_pending_sources
    assert out["kg"]["pending_sources"] >= 1


def test_index_status_mixed_pending_is_visible_but_job_total_is_physical(repo):
    nb = repo.create_notebook(NotebookCreate(name="mixed pending"))
    now = "2026-07-20T00:00:00"
    with repo._write() as db:
        for source_id, source_type in (
            ("s-uploaded", "document"),
            ("s-memory", "memory"),
            ("s-knowhow", "knowhow"),
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    source_id,
                    nb.id,
                    source_id,
                    source_type,
                    "ready",
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"e-{source_id}", source_id, "paragraph", "p1", "text", now),
            )
    from app.repositories.sqlite import knowledge_counts_cache

    knowledge_counts_cache.invalidate(nb.id)
    repo._runtime.kg_build_jobs.create_job(
        nb.id,
        "user-local",
        "incremental",
        3,
    )
    out = repo.index_status(nb.id)
    assert out["kg"]["pending_sources"] == 1
    assert out["kg"]["job"]["total_sources"] == 3


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


def _tiny_indexed_nb(repo):
    nb = repo.create_notebook(NotebookCreate(name="idx"))
    now = "2026-07-09T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   ("c1", nb.id, "s1", "alpha", "", "[]", now))
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_query("alpha")
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   ("c1", nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    return nb


def test_build_writes_built_at(repo):
    nb = _tiny_indexed_nb(repo)
    out_dir = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    with open(os.path.join(out_dir, "manifest.json")) as fh:
        manifest = json.load(fh)
    assert manifest.get("built_at")   # 非空
    st = repo.scale_index_status(nb.id)
    assert st.get("last_built_at") == manifest["built_at"]


def test_status_last_built_at_absent_manifest_safe(repo):
    nb = _tiny_indexed_nb(repo)
    out_dir = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    mpath = os.path.join(out_dir, "manifest.json")
    with open(mpath) as fh:
        manifest = json.load(fh)
    manifest.pop("built_at", None)        # 模拟旧索引
    with open(mpath, "w") as fh:
        json.dump(manifest, fh)
    repo._scale_idx_cache.pop(nb.id, None)  # 清进程缓存强制重读
    st = repo.scale_index_status(nb.id)
    assert st.get("last_built_at", "") == ""   # 缺键→空,不报错


def test_index_status_reuses_notebook_job_projection(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    job = repo._runtime.kg_build_jobs.create_job(
        nb.id,
        "user-local",
        "incremental",
        5,
    )
    status = repo.index_status(nb.id)
    assert status["kg"]["job"]["job_id"] == job["id"]
    assert status["kg"]["building"] is True
