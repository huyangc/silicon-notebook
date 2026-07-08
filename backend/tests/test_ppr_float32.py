"""P0-B:PPR float32+tol。float32 与 float64 在小图上 top-k 排序一致;
float32 下 tol 被 clamp 到 >=1e-6(防空转满 max_iter);迭代轮数确实下降。"""
import numpy as np
import scipy.sparse as sp

from app.services.kg import scale_index as si


def _chain_graph(n=50, dtype=np.float64):
    # 链式图 0-1-2-...-(n-1),双向,列随机
    edges = []
    for i in range(n - 1):
        edges.append((str(i), str(i + 1), 1.0))
        edges.append((str(i + 1), str(i), 1.0))
    A, idx = si.build_transition([str(i) for i in range(n)], edges)
    return A.astype(dtype), idx


def test_float32_topk_matches_float64():
    A64, _ = _chain_graph(dtype=np.float64)
    A32, _ = _chain_graph(dtype=np.float32)
    reset = np.zeros(50); reset[0] = 1.0; reset[10] = 0.5
    x64 = si.personalized_ppr(A64, reset, damping=0.5, tol=1e-8)
    x32 = si.personalized_ppr(A32, reset.astype(np.float32), damping=0.5, tol=1e-6)
    top64 = list(np.argsort(-x64)[:10])
    top32 = list(np.argsort(-x32)[:10])
    assert top64 == top32


def test_float32_tol_clamped_no_spin():
    A32, _ = _chain_graph(dtype=np.float32)
    reset = np.zeros(50, dtype=np.float32); reset[0] = 1.0
    stats = {}
    si.personalized_ppr(A32, reset, damping=0.5, tol=1e-12, stats=stats)
    # clamp 生效:不会空转满 100 轮
    assert stats["iters"] < 100


def test_looser_tol_fewer_iters():
    A64, _ = _chain_graph(dtype=np.float64)
    reset = np.zeros(50); reset[0] = 1.0
    s_tight, s_loose = {}, {}
    si.personalized_ppr(A64, reset, damping=0.5, tol=1e-8, stats=s_tight)
    si.personalized_ppr(A64, reset, damping=0.5, tol=1e-6, stats=s_loose)
    assert s_loose["iters"] < s_tight["iters"]


def test_output_dtype_follows_transition():
    A32, _ = _chain_graph(dtype=np.float32)
    reset = np.zeros(50, dtype=np.float32); reset[0] = 1.0
    x = si.personalized_ppr(A32, reset, damping=0.5, tol=1e-6)
    assert x.dtype == np.float32
