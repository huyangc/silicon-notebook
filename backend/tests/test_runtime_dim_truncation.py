"""T1:truncate_vec / resolve_runtime_dim / build_matrix 截断接线。

核心不变量:①截断+renorm 原子语义,与 app.eval.mrl_truncation.truncated_sims
数值等价(eval 与生产同口径);②截断先于首行定维(原生维与恰为运行时维的行
混存不互丢);③runtime=0 与现行为逐位相同;④设置路径(EMBED_RUNTIME_DIM env)
生效。计划:docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md T1。
"""
import json

import numpy as np
import pytest

from app.eval.mrl_truncation import truncated_sims
from app.services.vector_index import (
    build_matrix,
    encode_vector,
    resolve_runtime_dim,
    truncate_vec,
)
from tests.model_testkit import bind_embedding_client


def _rows(vectors, blob=True):
    return [(f"v{i}", encode_vector(v) if blob else json.dumps(list(map(float, v))))
            for i, v in enumerate(vectors)]


# ── truncate_vec 纯语义 ──────────────────────────────────────────────────────

def test_truncate_vec_slices_and_renormalizes():
    arr = np.array([3.0, 4.0, 100.0, 100.0], dtype=np.float32)
    out = truncate_vec(arr, 2)
    assert out.shape == (2,)
    assert np.linalg.norm(out) == pytest.approx(1.0, abs=1e-6)
    assert out[0] == pytest.approx(0.6) and out[1] == pytest.approx(0.8)


def test_truncate_vec_noop_when_off_or_short():
    arr = np.array([1.0, 2.0], dtype=np.float32)
    assert truncate_vec(arr, 0) is arr          # 关闭 = 原样
    assert truncate_vec(arr, 4) is arr          # 短于运行时维 = 原样(下游 dim 检查处理)
    assert truncate_vec(arr, 2) is arr          # 恰等长 = 原样(无损)


def test_truncate_vec_zero_prefix_safe():
    arr = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    out = truncate_vec(arr, 2)
    assert np.isfinite(out).all() and out.shape == (2,)


# ── build_matrix 接线 ────────────────────────────────────────────────────────

def test_build_matrix_truncates_to_runtime_dim_unit_rows():
    rng = np.random.default_rng(3)
    vecs = rng.normal(size=(10, 32)).astype(np.float32)
    ids, mat = build_matrix(_rows(vecs), runtime_dim=16)
    assert len(ids) == 10 and mat.shape == (10, 16)
    assert np.allclose(np.linalg.norm(mat, axis=1), 1.0, atol=1e-5)


def test_build_matrix_mixed_native_and_runtime_dim_rows_kept():
    """原生 32 维行与「恰为运行时维」16 维行混存 → 截断先于定维,两类都保留;
    真正的异维残留(8 维)仍按既有语义跳过。"""
    rng = np.random.default_rng(5)
    rows = _rows([rng.normal(size=32).astype(np.float32) for _ in range(3)])
    rows += [("legacy16", encode_vector(rng.normal(size=16).astype(np.float32)))]
    rows += [("alien8", encode_vector(rng.normal(size=8).astype(np.float32)))]
    ids, mat = build_matrix(rows, runtime_dim=16)
    assert set(ids) == {"v0", "v1", "v2", "legacy16"}
    assert mat.shape == (4, 16)


def test_build_matrix_runtime_zero_bit_identical_to_legacy():
    rng = np.random.default_rng(7)
    vecs = rng.normal(size=(6, 24)).astype(np.float32)
    ids0, mat0 = build_matrix(_rows(vecs), runtime_dim=0)
    expect = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    assert mat0.shape == (6, 24)
    assert np.allclose(mat0, expect, atol=1e-6)


def test_build_matrix_prealloc_hint_with_truncation():
    """n_hint 预分配按截断维开数组(峰值 ÷4 的前提),溢出路径同样截断。"""
    rng = np.random.default_rng(9)
    vecs = rng.normal(size=(7, 32)).astype(np.float32)
    ids, mat = build_matrix(_rows(vecs), n_hint=4, runtime_dim=8)   # hint 故意偏小
    assert mat.shape == (7, 8)
    assert np.allclose(np.linalg.norm(mat, axis=1), 1.0, atol=1e-5)


def test_build_matrix_json_and_blob_mixed_formats_truncate_same():
    rng = np.random.default_rng(11)
    v = rng.normal(size=32).astype(np.float32)
    ids, mat = build_matrix([("b", encode_vector(v)),
                             ("j", json.dumps([float(x) for x in v]))],
                            runtime_dim=16)
    assert set(ids) == {"b", "j"}
    assert np.allclose(mat[0], mat[1], atol=1e-5)


def test_build_matrix_settings_path(monkeypatch):
    """runtime_dim=None → 读 EMBED_RUNTIME_DIM(经 get_settings)。"""
    from app.core.config import get_settings
    monkeypatch.setenv("EMBED_RUNTIME_DIM", "16")
    monkeypatch.setenv("EMBED_DIM", "32")
    get_settings.cache_clear()
    try:
        rng = np.random.default_rng(13)
        vecs = rng.normal(size=(4, 32)).astype(np.float32)
        assert resolve_runtime_dim() == 16
        ids, mat = build_matrix(_rows(vecs))
        assert mat.shape == (4, 16)
    finally:
        get_settings.cache_clear()


# ── 与 eval 口径锁死:生产 helper == mrl_truncation ──────────────────────────

def test_helper_equivalent_to_mrl_truncated_sims():
    """线上截断(truncate_vec 进矩阵 + matmul)与 spike 工具的 truncated_sims
    对同一批原始向量给出相同余弦 —— eval 结论可直接外推到生产行为。"""
    rng = np.random.default_rng(17)
    corpus = rng.normal(size=(20, 32)).astype(np.float32)
    q = rng.normal(size=32).astype(np.float32)
    d = 16

    ids, mat = build_matrix(_rows(corpus), runtime_dim=d)   # 行已截断+归一
    q_t = truncate_vec(q, d)                                # 查询侧同 helper
    prod_sims = mat @ q_t

    eval_sims = truncated_sims(corpus, q, d)                # spike 工具口径
    assert np.allclose(prod_sims, eval_sims, atol=1e-5)


# ── T2:查询侧截断 + 同空间端到端不变量 ──────────────────────────────────────

@pytest.fixture
def dual_dim_repo(tmp_path, monkeypatch):
    """存储维 32 / 运行时维 16 的双维 repo(FakeEmbedder 按存储维出向量,
    模拟端点原生输出)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_RUNTIME_DIM", "16")
    for k, v in {"EMBED_DIM": "32"}.items():
        monkeypatch.setenv(k, v)
    from app.core.config import Settings, get_settings
    get_settings.cache_clear()
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=32))
    return r


def test_embed_query_truncated_to_runtime_dim(dual_dim_repo):
    vec = dual_dim_repo._embed_query("bandgap reference")
    assert vec is not None and len(vec) == 16
    assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-5)


def test_query_and_corpus_same_space_end_to_end(dual_dim_repo):
    """全计划核心回归:_embed_query 输出维 == build_matrix 输出列数
    (查询/语料同空间;不满足 = query_sims 静默返 {} = 零召回)。"""
    from app.services.vector_index import build_matrix, encode_vector, query_sims
    emb = dual_dim_repo.embedder
    texts = ["bandgap reference", "cascode amplifier", "PLL jitter"]
    rows = [(f"c{i}", encode_vector(emb.embed_query(t))) for i, t in enumerate(texts)]
    ids, mat = build_matrix(rows)                      # settings 路径 → 截断到 16
    q = dual_dim_repo._embed_query(texts[0])
    assert len(q) == mat.shape[1] == 16
    sims = query_sims(q, ids, mat)
    assert sims and sims["c0"] == pytest.approx(1.0, abs=1e-4)   # 自身命中,非零召回
