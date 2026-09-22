"""T3:缓存/版本键纳入 runtime_dim(风险 R4)。

EMBED_RUNTIME_DIM 改变(如 0→16)而不重启,所有涉及向量空间的缓存/版本键
必须失效——否则命中旧维矩阵/旧索引恒空(与聚类缓存时间戳版本键漏同秒编辑
PR#132 同族旧伤)。覆盖四个键:

1. `_vector_matrix` 缓存(经 `_vector_matrix_version`,`_vector_matrix_warm`
   的 peek 同源自动同步);
2. `_ppr_graph` 图缓存 version(emb_synonym 边由向量矩阵派生);
3. `_probe_scale_version_signal` 的 settings_tail(→ `_scale_index_version`
   → scale 索引 manifest stale 判定);
4. `_cluster_input_version`(rebuild 版本闸对维度变化不能跳过重聚)。

计划:docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md T3。
"""
import json

import pytest

from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """存储维 32、运行时截断关闭(EMBED_RUNTIME_DIM=0)起步的 repo。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_RUNTIME_DIM", "0")
    for k, v in {"EMBED_DIM": "32"}.items():
        monkeypatch.setenv(k, v)
    from app.core.config import Settings, get_settings
    get_settings.cache_clear()
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=32))
    return r


def _switch_runtime_dim(repo, monkeypatch, dim: int):
    """模拟「改 EMBED_RUNTIME_DIM 后进程重新读配置」:同步更新全局 get_settings
    (build_matrix 的 settings 默认路径)与 repo.settings(版本键路径)。"""
    monkeypatch.setenv("EMBED_RUNTIME_DIM", str(dim))
    from app.core.config import get_settings
    get_settings.cache_clear()
    repo.settings.embed_runtime_dim = dim


def _seed_embeddings(repo, nb_id: str, n: int = 3, dim: int = 32):
    from app.services.sqlite_repository import _now
    now = _now()
    with repo._write() as db:
        for i in range(n):
            vec = [0.0] * dim
            vec[i % dim] = 1.0
            db.execute(
                "INSERT INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) "
                "VALUES (?,?,?,?)",
                (f"ko-{i}", nb_id, json.dumps(vec), now),
            )


# ── 1. _vector_matrix ────────────────────────────────────────────────────────

def test_vector_matrix_version_includes_runtime_dim(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    with repo._connect() as db:
        v0 = repo._vector_matrix_version(db, nb.id, "knowledge_embeddings")
        v0_again = repo._vector_matrix_version(db, nb.id, "knowledge_embeddings")
    assert v0 == v0_again, "同配置同数据下版本必须稳定(不虚失效)"
    _switch_runtime_dim(repo, monkeypatch, 16)
    with repo._connect() as db:
        v1 = repo._vector_matrix_version(db, nb.id, "knowledge_embeddings")
    assert v1 != v0, "runtime_dim 变化必须改变矩阵缓存版本"


def test_vector_matrix_cache_misses_and_reloads_on_runtime_dim_change(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _seed_embeddings(repo, nb.id, n=3, dim=32)
    with repo._connect() as db:
        ids0, mat0 = repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
        assert mat0.shape == (3, 32)
        # 同配置重取 = 命中(shape 不变即同一空间;控制组)
        _, mat0b = repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
        assert mat0b.shape == (3, 32)
    _switch_runtime_dim(repo, monkeypatch, 16)
    with repo._connect() as db:
        ids1, mat1 = repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
    assert mat1.shape == (3, 16), (
        "切 runtime_dim 后必须缓存 miss 重载到新空间——命中旧 32 维矩阵 = R4")
    assert ids1 == ids0


def test_vector_matrix_warm_peek_stays_in_sync(repo, monkeypatch):
    """守卫的暖判定(_vector_matrix_warm)与真实缓存共用同一 version——切维后
    旧矩阵不得再被判定为「暖」,否则大库守卫放行后命中旧空间。"""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _seed_embeddings(repo, nb.id, n=2, dim=32)
    with repo._connect() as db:
        repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
        assert repo._vector_matrix_warm(db, nb.id, "knowledge_embeddings") is True
    _switch_runtime_dim(repo, monkeypatch, 16)
    with repo._connect() as db:
        assert repo._vector_matrix_warm(db, nb.id, "knowledge_embeddings") is False, (
            "切维后旧维矩阵不得判暖")
        repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
        assert repo._vector_matrix_warm(db, nb.id, "knowledge_embeddings") is True


# ── 2. _ppr_graph ────────────────────────────────────────────────────────────

def test_ppr_graph_version_includes_runtime_dim(repo, monkeypatch):
    import app.services.kg.ppr as ppr_mod
    nb = repo.create_notebook(NotebookCreate(name="b"))
    calls = {"n": 0}
    real = ppr_mod.build_ppr_graph

    def _spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(ppr_mod, "build_ppr_graph", _spy)
    repo._ppr_graph(nb.id)
    repo._ppr_graph(nb.id)
    assert calls["n"] == 1, "同配置第二次调用必须命中缓存(控制组)"
    _switch_runtime_dim(repo, monkeypatch, 16)
    repo._ppr_graph(nb.id)
    assert calls["n"] == 2, "runtime_dim 变化必须使 PPR 图缓存失效重建"


# ── 3. scale 版本探针 / _scale_index_version ─────────────────────────────────

def test_probe_scale_settings_tail_includes_runtime_dim(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    tail0 = repo._probe_scale_version_signal(nb.id)[2]
    assert tail0 == repo._probe_scale_version_signal(nb.id)[2]
    _switch_runtime_dim(repo, monkeypatch, 16)
    tail1 = repo._probe_scale_version_signal(nb.id)[2]
    assert tail1 != tail0, "settings_tail 必须包含 runtime_dim(scale 索引 stale 判定)"


def test_scale_index_version_changes_with_runtime_dim(repo, monkeypatch):
    """端到端:manifest.version 对照的 _scale_index_version 在切维后必须漂移
    (旧 4096 索引 + runtime 1024 → 判 stale,而非恒空命中)。"""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    v0 = repo._scale_index_version(nb.id)
    assert v0 == repo._scale_index_version(nb.id), "memo 快路径同配置必须稳定"
    _switch_runtime_dim(repo, monkeypatch, 16)
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0, "runtime_dim 变化必须改变 scale 索引版本(含 memo 快路径)"


# ── 4. _cluster_input_version ────────────────────────────────────────────────

def test_cluster_input_version_changes_with_runtime_dim(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    v0 = repo._cluster_input_version(nb.id)
    assert v0 == repo._cluster_input_version(nb.id)
    _switch_runtime_dim(repo, monkeypatch, 16)
    v1 = repo._cluster_input_version(nb.id)
    assert v1 != v0, "rebuild 版本闸对 runtime_dim 变化不能跳过重聚"


def test_runtime_dim_zero_roundtrip_restores_versions(repo, monkeypatch):
    """切回原值 = 版本键回到原值(键是纯函数,不掺时间戳——PR#132 教训)。"""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    v0 = repo._cluster_input_version(nb.id)
    with repo._connect() as db:
        m0 = repo._vector_matrix_version(db, nb.id, "knowledge_embeddings")
    _switch_runtime_dim(repo, monkeypatch, 16)
    _switch_runtime_dim(repo, monkeypatch, 0)
    assert repo._cluster_input_version(nb.id) == v0
    with repo._connect() as db:
        assert repo._vector_matrix_version(db, nb.id, "knowledge_embeddings") == m0


# ── 5. cross-process kg_mutation_seq advance (codex #772 r18 P2) ────────────

def test_vector_matrix_version_changes_when_kg_mutation_seq_advances_underneath(repo):
    """(COUNT, MAX(created_at)) alone misses a cross-process sync importer that
    REPLACES a notebook's mirrored embedding rows in place: the row count is
    unchanged and the importer can carry a ``created_at``/``now()`` that ties
    or falls behind the pre-import max, so neither aggregate need move. Such
    an importer writes through its own transactions, never through this
    process's ``_vector_cache`` invalidation call sites, so a bare
    (count, max-ts) version would keep an already-warm matrix cache serving
    the pre-import vectors forever.

    ``_vector_matrix_version`` now folds in ``unified_kg.graph_seq_row``'s
    (kg_mutation_seq, cluster_mutation_seq, mention_seq, kg_reset_epoch)
    quadruple, which every import advances (``migration/sync/import_.py``).
    This guard warms the matrix cache, then — via a SEPARATE connection, like
    a cross-process writer — bumps only ``kg_mutation_seq`` (no embedding rows
    touched at all), and asserts the version and warm-peek both flip.

    变异验证: 把 ``_vector_matrix_version`` 里 ``*self.unified_kg.graph_seq_row(...)``
    那段去掉,本条必须报红(版本不变,``_vector_matrix_warm`` 仍判暖)。
    """
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _seed_embeddings(repo, nb.id, n=2, dim=32)

    with repo._connect() as db:
        v0 = repo._vector_matrix_version(db, nb.id, "knowledge_embeddings")
        repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
        assert repo._vector_matrix_warm(db, nb.id, "knowledge_embeddings") is True

    # A cross-process importer's own transaction, on its own connection —
    # bumps unified_kg_state.kg_mutation_seq WITHOUT touching
    # knowledge_embeddings at all, so COUNT/MAX(created_at) stay identical.
    with repo._write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at) "
            "VALUES (?, 0, 1, '2020-01-01T00:00:00') "
            "ON CONFLICT(notebook_id) DO UPDATE SET "
            "kg_mutation_seq=unified_kg_state.kg_mutation_seq+1",
            (nb.id,),
        )

    with repo._connect() as db:
        v1 = repo._vector_matrix_version(db, nb.id, "knowledge_embeddings")
        assert v1 != v0, (
            "cross-process 导入器推进 kg_mutation_seq 后,向量矩阵版本必须变化"
        )
        assert repo._vector_matrix_warm(db, nb.id, "knowledge_embeddings") is False, (
            "导入后旧版本矩阵不得再判暖 —— 否则大库守卫会放行、命中导入前的旧矩阵"
        )
