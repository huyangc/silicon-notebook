import numpy as np
import scipy.sparse as sp
from app.services.kg.scale_index import personalized_ppr


def _line_graph_transition():
    # 三节点有向链 0->1->2，列随机转移阵 A（A[j,i]=i->j 的归一化权重）
    A = sp.csr_matrix(np.array([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]))
    return A


def test_personalized_ppr_concentrates_mass_near_seed():
    A = _line_graph_transition()
    reset = np.array([1.0, 0.0, 0.0])
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-10, max_iter=200)
    assert x.shape == (3,)
    assert abs(x.sum() - 1.0) < 1e-6
    assert x[0] > x[1] > x[2]


def test_personalized_ppr_empty_reset_returns_zeros():
    A = _line_graph_transition()
    x = personalized_ppr(A, np.zeros(3), damping=0.5)
    assert np.allclose(x, 0.0)


from app.services.kg.scale_index import build_transition


def test_build_transition_column_stochastic():
    node_ids = ["a", "b", "c"]
    edges = [("a", "b", 1.0), ("b", "a", 1.0), ("b", "c", 1.0), ("c", "b", 1.0)]
    A, index = build_transition(node_ids, edges)
    assert index == {"a": 0, "b": 1, "c": 2}
    dense = A.toarray()
    assert abs(dense[:, index["b"]].sum() - 1.0) < 1e-9
    assert abs(dense[index["a"], index["b"]] - 0.5) < 1e-9
    assert abs(dense[index["c"], index["b"]] - 0.5) < 1e-9


def test_build_transition_drops_dangling_endpoints():
    node_ids = ["a", "b"]
    edges = [("a", "b", 1.0), ("a", "zzz", 1.0)]
    A, index = build_transition(node_ids, edges)
    assert A.shape == (2, 2)


import rustworkx as rx


def _random_graph(n=60, seed=7):
    rng = np.random.default_rng(seed)
    ids = [f"n{i}" for i in range(n)]
    edges = []
    for i in range(n):
        for _ in range(rng.integers(1, 4)):
            j = int(rng.integers(0, n))
            if j != i:
                edges.append((ids[i], ids[j], 1.0))
                edges.append((ids[j], ids[i], 1.0))
    return ids, edges


def test_scale_ppr_topk_matches_rustworkx():
    ids, edges = _random_graph()
    index = {nid: i for i, nid in enumerate(ids)}
    G = rx.PyDiGraph()
    G.add_nodes_from(ids)
    for s, t, w in edges:
        G.add_edge(index[s], index[t], {"weight": w})
    seeds = {index["n0"]: 1.0, index["n5"]: 1.0}
    rx_scores = rx.pagerank(G, alpha=0.5,
                            personalization={k: float(v) for k, v in seeds.items()},
                            weight_fn=lambda p: float(p.get("weight", 1.0)))
    rx_rank = [k for k, v in sorted(rx_scores.items(), key=lambda x: x[1], reverse=True)]

    A, idx2 = build_transition(ids, edges)
    reset = np.zeros(len(ids)); reset[index["n0"]] = 1.0; reset[index["n5"]] = 1.0
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-12, max_iter=500)
    scale_rank = list(np.argsort(-x))

    assert set(rx_rank[:10]) >= set(scale_rank[:3])
    assert len(set(rx_rank[:10]) & set(scale_rank[:10])) >= 8


from app.services.kg.scale_index import splice_active, build_transition


def test_splice_active_unifies_shared_node_and_keeps_bridge():
    base_ids = ["K-mosfet", "c1"]
    base_edges = [("K-mosfet", "c1", 1.0), ("c1", "K-mosfet", 1.0)]
    base_A, _ = build_transition(base_ids, base_edges)
    active_ids = ["K-mosfet", "c2"]
    active_edges = [("K-mosfet", "c2", 1.0), ("c2", "K-mosfet", 1.0)]
    ids, A = splice_active(base_ids, base_A, active_ids, active_edges)
    assert set(ids) == {"K-mosfet", "c1", "c2"}
    index = {n: i for i, n in enumerate(ids)}
    dense = A.toarray()
    assert dense[index["c1"], index["K-mosfet"]] > 0
    assert dense[index["c2"], index["K-mosfet"]] > 0


import time
import pytest


def _random_graph_with_sink(n=30, seed=42):
    """Like _random_graph but forces node n-1 to be a dangling node (no outgoing
    edges).  This exercises the dangling-mass redistribution line in
    personalized_ppr:  ``x_new += (1 - x_new.sum()) * p``."""
    rng = np.random.default_rng(seed)
    ids = [f"n{i}" for i in range(n)]
    edges = []
    sink = ids[-1]
    for i in range(n - 1):  # last node (sink) gets no outgoing edges
        for _ in range(rng.integers(1, 3)):
            j = int(rng.integers(0, n))
            if j != i:
                edges.append((ids[i], ids[j], 1.0))
                edges.append((ids[j], ids[i], 1.0))
    # Remove any edges FROM sink to ensure it's truly dangling.
    edges = [(s, t, w) for s, t, w in edges if s != sink]
    return ids, edges, sink


def test_personalized_ppr_dangling_sums_to_one():
    """A graph with a dangling (no-outgoing-edge) node must still produce a PPR
    vector that sums to 1.0.  The dangling-mass line ``x_new += (1-x_new.sum())*p``
    is what keeps the mass; if removed x_new.sum() < 1 and this test would fail."""
    ids, edges, sink = _random_graph_with_sink()
    A, index = build_transition(ids, edges)
    # Verify that 'sink' truly has no outgoing edges → its column in A should be
    # all zeros (dangling column).
    col_sum = float(A.getcol(index[sink]).sum())
    assert col_sum == 0.0, (
        f"Sink node column should be zero-sum (dangling), got {col_sum}")
    reset = np.zeros(len(ids))
    reset[index["n0"]] = 1.0
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-10, max_iter=500)
    assert abs(x.sum() - 1.0) < 1e-6, f"PPR sum={x.sum():.8f} (expected 1.0)"
    assert x[index[sink]] > 0.0, "Dangling node should still receive PPR mass"


def test_personalized_ppr_dangling_topk_matches_rustworkx():
    """With a dangling node, top-k from personalized_ppr must agree with
    rx.pagerank.  Verifies that the dangling-mass fix does not distort rankings."""
    ids, edges, sink = _random_graph_with_sink()
    index = {nid: i for i, nid in enumerate(ids)}

    G = rx.PyDiGraph()
    G.add_nodes_from(ids)
    for s, t, w in edges:
        G.add_edge(index[s], index[t], {"weight": w})

    seeds = {index["n0"]: 1.0, index["n3"]: 1.0}
    rx_scores = rx.pagerank(G, alpha=0.5,
                            personalization={k: float(v) for k, v in seeds.items()},
                            weight_fn=lambda p: float(p.get("weight", 1.0)))
    rx_rank = [k for k, v in sorted(rx_scores.items(), key=lambda kv: kv[1], reverse=True)]

    A, _ = build_transition(ids, edges)
    reset = np.zeros(len(ids))
    reset[index["n0"]] = 1.0
    reset[index["n3"]] = 1.0
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-12, max_iter=500)
    assert abs(x.sum() - 1.0) < 1e-6
    scale_rank = list(np.argsort(-x))

    # At least 6 of the top-10 nodes should agree (dangling redistribution is
    # proportional to p, so global mass is conserved but distribution differs
    # slightly from rx because rx uses a uniform dangling fix by default).
    top_overlap = len(set(rx_rank[:10]) & set(scale_rank[:10]))
    assert top_overlap >= 6, (
        f"Top-10 overlap with dangling node: {top_overlap}/10 "
        f"(rx_rank[:10]={rx_rank[:10]}, scale_rank[:10]={scale_rank[:10]})")


@pytest.mark.slow
def test_personalized_ppr_scales_to_1e5():
    n, m = 100_000, 1_000_000
    rng = np.random.default_rng(1)
    rows = rng.integers(0, n, m); cols = rng.integers(0, n, m)
    A = sp.csr_matrix((np.ones(m), (rows, cols)), shape=(n, n))
    colsum = np.asarray(A.sum(0)).ravel(); colsum[colsum == 0] = 1
    A = (A @ sp.diags(1.0 / colsum)).tocsr()
    reset = np.zeros(n); reset[rng.integers(0, n, 50)] = 1.0
    t = time.perf_counter()
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-6, max_iter=100)
    dt = time.perf_counter() - t
    assert abs(x.sum() - 1.0) < 1e-6
    assert dt < 10.0
    print(f"\n[scale] personalized_ppr 1e5 nodes / 1e6 edges: {dt:.3f}s")
