"""T5-T6:ANN 构建 manifest 真相化 + fold/mode 闸 + build 缓存失效。

切换运行时维后旧索引(manifest.dim=存储维)必须整体重建:
- manifest.dim 记工件实际维(截断后),不信配置(T5);
- scale_index_status → state=stale + stale_reason=dim_mismatch(T6);
- _resolve_scale_mode 对 dim 失配强制 full(T6);
- fold 遇 dim 失配拒绝 + scale_fold_refused 事件 + 转 build(T6);
- build_scale_index 收尾 pop _scale_idx_cache(T6,修「重建后同进程看不见」)。
计划:docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md T5-T6。
"""
import json

import pytest

from app.core.config import Settings, get_settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """存储维 32(FakeEmbedder 原生),运行时维默认关(0),用例内按需开。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "32"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=32)
    yield r
    get_settings.cache_clear()


def _seed(repo, n=6):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-07-01T00:00:00"
    objs = []
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for i in range(n):
            txt = f"concept bandgap topic {i}"
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"c{i}", nb.id, "s1", txt, "", "[]", now))
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (f"c{i}", nb.id, json.dumps(repo.embedder.embed_texts([txt])[0]), now))
        objs = [{"local_id": f"o{i}", "object_type": "concept",
                 "payload": {"name": f"Concept {i}"}, "evidence": []} for i in range(n)]
    repo.store_kg(nb.id, None, objs, [])
    return nb


def _set_runtime(repo, monkeypatch, dim):
    monkeypatch.setattr(repo.settings, "embed_runtime_dim", dim)


# ── T5:manifest dim = 工件实际维 ────────────────────────────────────────────

def test_manifest_dim_reflects_runtime_truncation(repo, monkeypatch):
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 16)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    out_dir = f"{repo.settings.storage_dir}/kg_index/{nb.id}"
    with open(f"{out_dir}/manifest.json") as fh:
        manifest = json.load(fh)
    assert manifest["dim"] == 16                     # 不是 EMBED_DIM=32
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx.chunk_ann_labels, "chunk ANN 应建成"
    # 三方一致:manifest dim == 运行时维 == 打开的 ANN 维度可 knn
    ann = repo._open_scale_ann(idx, "chunk")
    if ann is not None:
        assert ann.dim == 16


def test_manifest_dim_native_when_runtime_off(repo, monkeypatch):
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 0)               # 关闭 → 原生维
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with open(f"{repo.settings.storage_dir}/kg_index/{nb.id}/manifest.json") as fh:
        assert json.load(fh)["dim"] == 32


# ── T6:dim 失配 → stale / full / fold 拒绝 ─────────────────────────────────

def test_status_dim_mismatch_is_stale(repo, monkeypatch):
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 0)               # 先按 32 建
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    assert repo.scale_index_status(nb.id)["state"] == "indexed"

    _set_runtime(repo, monkeypatch, 16)              # 切到 16 → 旧索引失配
    st = repo.scale_index_status(nb.id)
    assert st["state"] == "stale"
    assert st["stale_reason"] == "dim_mismatch"
    assert st["manifest_dim"] == 32 and st["runtime_dim"] == 16


def test_resolve_scale_mode_forces_full_on_dim_mismatch(repo, monkeypatch):
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 0)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    _set_runtime(repo, monkeypatch, 16)
    # 即便 auto/fold,dim 失配也必须解析为 full
    assert repo._resolve_scale_mode(nb.id, "auto") == "full"
    assert repo._resolve_scale_mode(nb.id, "fold") == "full"


def test_fold_refuses_dim_mismatch_and_emits_event(repo, monkeypatch):
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 0)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)

    events = []
    orig = repo.event_log.emit
    monkeypatch.setattr(repo.event_log, "emit",
                        lambda e, **k: (events.append(e), orig(e, **k))[1])
    _set_runtime(repo, monkeypatch, 16)
    builder = repo._runtime.scale_builder
    builds = []
    real_build = builder.build
    monkeypatch.setattr(
        builder,
        "build",
        lambda notebook_id, on_stage=None: (
            builds.append(notebook_id), real_build(notebook_id, on_stage=on_stage)
        )[1],
    )
    builder.fold(nb.id, assume_locked=True)   # 应转 build,不 hnswlib 硬错
    assert builds == [nb.id]
    refused = [e for e in events if e.get("kind") == "scale_fold_refused"]
    assert len(refused) == 1 and refused[0]["reason"] == "dim_mismatch"
    # 转 build 后 manifest 已是新维
    with open(f"{repo.settings.storage_dir}/kg_index/{nb.id}/manifest.json") as fh:
        assert json.load(fh)["dim"] == 16


# ── T6:build 收尾失效进程缓存 ──────────────────────────────────────────────

def test_build_pops_stale_cache_same_process(repo, monkeypatch):
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 0)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx_old = repo._scale_index(nb.id, allow_stale=True)     # 暖进缓存(dim=32)
    assert idx_old.manifest["dim"] == 32

    _set_runtime(repo, monkeypatch, 16)
    repo.build_scale_index(nb.id)                            # full 重建
    idx_new = repo._scale_index(nb.id, allow_stale=True)     # 必须是新实例
    assert idx_new.manifest["dim"] == 16                    # 不是缓存里的旧 32


# ── T7:dim_mismatch 事件 ─────────────────────────────────────────────────────

def test_chunk_ann_dim_mismatch_emits_event(repo, monkeypatch):
    """chunk ANN 遇 manifest dim ≠ 查询维 → 发 dim_mismatch 事件(不静默跳过)。"""
    nb = _seed(repo)
    _set_runtime(repo, monkeypatch, 0)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    events = []
    orig = repo.event_log.emit
    monkeypatch.setattr(repo.event_log, "emit",
                        lambda e, **k: (events.append(e), orig(e, **k))[1])
    import numpy as np
    # 手造 16 维查询向量(与 manifest dim=32 失配)直调 _retrieve_chunks_ann
    q16 = np.random.default_rng(1).normal(size=16).astype(np.float32).tolist()
    out = repo._retrieve_chunks_ann(nb.id, "q", q16, idx, recall=5)
    assert out is None
    dm = [e for e in events if e.get("kind") == "dim_mismatch" and e.get("site") == "chunk_ann"]
    assert len(dm) == 1 and dm[0]["manifest_dim"] == 32 and dm[0]["query_dim"] == 16


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
