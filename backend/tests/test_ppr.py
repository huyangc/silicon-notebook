from app.services.concept_merge_review import _prompt


def test_merge_review_prompt_is_domain_agnostic():
    p = _prompt([{"id": "c1", "score": 0.9, "canonical_a": "MoE",
                  "canonical_b": "Mixture-of-Experts"}])
    low = p.lower()
    assert "cmos" not in low and "rf" not in low and "circuit" not in low
    assert "acronym" in low
    assert "merge" in low and ("keep separate" in low or "keep_separate" in low)
    assert "MoE" in p and "Mixture-of-Experts" in p


import rustworkx as rx
from app.services.kg.ppr import build_ppr_graph


def test_build_ppr_graph_nodes_and_edges():
    kg_nodes = {"e1": {"type": "concept", "name": "MoE(paperA)"},
                "e2": {"type": "concept", "name": "MoE(paperB)"}}
    chunk_ids = ["cA", "cB"]
    relations = []
    memberships = [("e1", "cA"), ("e2", "cB")]
    cluster_groups = {"K-moe": ["e1", "e2"]}

    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        kg_nodes, chunk_ids, relations, memberships, cluster_groups)

    assert isinstance(G, rx.PyDiGraph)
    assert G.num_nodes() == 5            # 2 entity + 2 chunk + 1 cluster-router
    assert set(chunk_idx_to_id.values()) == {"cA", "cB"}
    assert G.num_edges() == 8            # (2 membership + 2 star) * 2 directed
    router = key_to_idx["cluster:K-moe"]
    assert set(G.successor_indices(router)) == {key_to_idx["e1"], key_to_idx["e2"]}


def test_build_ppr_graph_skips_dangling_and_empty_clusters():
    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        {"e1": {"type": "concept", "name": "x"}}, ["cA"],
        relations=[{"source_object_id": "e1", "target_object_id": "MISSING"}],
        memberships=[("e1", "cA"), ("GHOST", "cA")],
        cluster_groups={"K-solo": ["e1"]})
    assert G.num_edges() == 4            # (1 membership + 1 star) * 2 directed


from app.services.kg.ppr import run_ppr


def test_run_ppr_bridges_across_documents():
    # paperA: e1(MoE)--cA ; paperB: e2(MoE)--cB ; e1,e2 同簇(桥)。
    # paperC: e3(unrelated)--cC,不在任何簇 → 与种子无通路(对照组)。
    kg_nodes = {"e1": {"type": "concept", "name": "MoE"},
                "e2": {"type": "concept", "name": "MoE"},
                "e3": {"type": "concept", "name": "Unrelated"}}
    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        kg_nodes, ["cA", "cB", "cC"], [],
        [("e1", "cA"), ("e2", "cB"), ("e3", "cC")],
        {"K-moe": ["e1", "e2"]})

    reset = {key_to_idx["e1"]: 1.0}
    ranked = run_ppr(G, chunk_idx_to_id, reset, damping=0.5)

    assert all(0.0 <= s <= 1.0 for _, s in ranked)
    score = dict(ranked)
    assert score["cB"] > score["cC"]      # bridged beats no-path
    assert score["cB"] > 0.0
    assert score["cA"] >= score["cB"]
    assert ranked[0][0] == "cA"


def test_run_ppr_empty_reset_returns_empty():
    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        {"e1": {"type": "concept", "name": "x"}}, ["cA"], [],
        [("e1", "cA")], {})
    assert run_ppr(G, chunk_idx_to_id, {}, damping=0.5) == []


def test_build_ppr_graph_extra_edges():
    from app.services.kg.ppr import build_ppr_graph
    kg = {"a": {"type": "concept", "name": "A"}, "b": {"type": "concept", "name": "B"}}
    G, key_to_idx, _ = build_ppr_graph(kg, [], [], [], {}, extra_edges=[("a", "b", 0.5)])
    ai, bi = key_to_idx["a"], key_to_idx["b"]
    assert bi in set(G.successor_indices(ai)) and ai in set(G.successor_indices(bi))  # reciprocal
    # weight stored on the edge payload
    assert G.get_edge_data(ai, bi)["weight"] == 0.5

def test_build_ppr_graph_extra_edges_default_none():
    from app.services.kg.ppr import build_ppr_graph
    G, _, _ = build_ppr_graph({"a": {"type": "c", "name": "A"}}, [], [], [], {})
    assert G.num_edges() == 0   # no extra_edges → unchanged


def test_variant_edge_pairs_groups_version_siblings():
    from app.services.kg.ppr import variant_edge_pairs
    kg = {
        "v2": {"type": "concept", "name": "DeepSeek-V2"},
        "v3": {"type": "concept", "name": "DeepSeek-V3"},
        "q7": {"type": "concept", "name": "Qwen2-7B"},
        "q72": {"type": "concept", "name": "Qwen2-72B"},
        "att": {"type": "concept", "name": "Attention"},   # no version → excluded
    }
    pairs = variant_edge_pairs(kg, weight=0.5)
    s = {frozenset((a, b)) for a, b, _ in pairs}
    assert frozenset(("v2", "v3")) in s      # DeepSeek V2↔V3
    assert frozenset(("q7", "q72")) in s     # Qwen2 7B↔72B
    assert all(w == 0.5 for _, _, w in pairs)
    # 'Attention' has no version/size token → not connected to anything
    assert not any("att" in (a, b) for a, b, _ in pairs)
    # different base models NOT cross-connected
    assert frozenset(("v2", "q7")) not in s


def test_emb_synonym_edges_connects_similar():
    import numpy as np
    from app.services.kg.ppr import emb_synonym_edges
    M = np.array([[1.0, 0.0, 0.0], [0.99, 0.01, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    pairs = emb_synonym_edges(["e0", "e1", "e2"], M, threshold=0.8, top_k=5, max_entities=1000)
    s = {frozenset((a, b)) for a, b, _ in pairs}
    assert frozenset(("e0", "e1")) in s        # near-identical → edge
    assert frozenset(("e0", "e2")) not in s    # orthogonal → no edge
    assert all(0.8 <= w <= 1.0001 for _, _, w in pairs)


def test_variant_edge_pairs_star_not_quadratic():
    from collections import Counter
    from app.services.kg.ppr import variant_edge_pairs
    kg = {f"o{i}": {"name": f"GPT v{i}"} for i in range(5)}  # 同 base "gpt"
    edges = variant_edge_pairs(kg, 0.5)
    # 星型:4 条无向(代表↔其余),非 C(5,2)=10 条
    undirected = {frozenset((a, b)) for a, b, _ in edges}
    assert len(undirected) == 4                    # k-1,非 k(k-1)/2
    # 连通性:所有成员经代表连通(代表在每条边里)
    deg = Counter()
    [deg.update([a, b]) for a, b, _ in edges]
    assert max(deg.values()) == 4                  # 代表 degree=k-1


def test_emb_synonym_edges_ann_beyond_cutoff():
    import numpy as np
    from app.services.kg.ppr import emb_synonym_edges
    rng = np.random.RandomState(0)
    n = 60000                                       # > 旧 max_entities=50000
    # 造 3 对近似同义(其余随机),确认 ANN 版能召回这几对(旧版此规模返 [])
    M = rng.randn(n, 16).astype(np.float32)
    for a, b in [(0, 1), (2, 3), (4, 5)]:
        M[b] = M[a] + 0.001 * rng.randn(16)
    ids = [f"e{i}" for i in range(n)]
    edges = emb_synonym_edges(ids, M, threshold=0.9, top_k=10)
    pairs = {frozenset((a, b)) for a, b, _ in edges}
    assert frozenset(("e0", "e1")) in pairs
    assert edges != []                              # 关键:超 5 万不再返 []


# ── Task 1: hnsw 只建一次 + 去归一化拷贝 + 向量化去重 (build_scale_index perf) ──


def _old_emb_synonym_edges(ids, matrix, threshold=0.8, top_k=20, max_entities=50000):
    """Oracle copy of the PRE-optimization emb_synonym_edges implementation
    (explicit `M = M / norms` copy + Python set-based row-major dedup, first
    row-major occurrence's sim kept). Kept test-local so the vectorized
    rewrite can be checked for exact output-set equivalence against the
    historical behavior — see docs/superpowers/plans/2026-07-02-scale-build-perf.md."""
    import numpy as np
    import hnswlib
    n = len(ids)
    if n < 2 or matrix is None:
        return []
    M = np.asarray(matrix, dtype=np.float32)
    if M.ndim != 2 or M.shape[0] != n:
        return []
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    M = M / norms
    dim = int(M.shape[1])
    try:
        idx = hnswlib.Index(space="cosine", dim=dim)
        idx.init_index(max_elements=n, ef_construction=200, M=16, random_seed=42)
        idx.add_items(M, np.arange(n))
        idx.set_ef(max(top_k + 1, 64))
        k = min(top_k + 1, n)
        labels, distances = idx.knn_query(M, k=k)
    except Exception:
        return []
    out, seen = [], set()
    for i in range(n):
        for lab, dist in zip(labels[i], distances[i]):
            j = int(lab)
            if j == i:
                continue
            sim = 1.0 - float(dist)
            if sim >= threshold:
                a, b = (i, j) if i < j else (j, i)
                if (a, b) not in seen:
                    seen.add((a, b))
                    out.append((ids[a], ids[b], sim))
    return out


def _synonym_fixture(n=40, dim=12, seed=3):
    import numpy as np
    rng = np.random.RandomState(seed)
    M = rng.randn(n, dim).astype(np.float32)
    # Force several near-duplicate clusters so KNN produces reciprocal
    # neighbor pairs (i finds j AND j finds i) — this is what exercises the
    # dedup path (old Python `seen` set / new vectorized min*n+max encoding).
    for a, b in [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9)]:
        M[b] = M[a] + 0.001 * rng.randn(dim)
    ids = [f"e{i}" for i in range(n)]
    return ids, M


def test_emb_synonym_edges_matches_oracle_output_set():
    """New implementation's output SET (id_a,id_b,sim) must equal the old
    (pre-optimization) implementation's, for a fixed small dataset."""
    from app.services.kg.ppr import emb_synonym_edges
    ids, M = _synonym_fixture()
    old = _old_emb_synonym_edges(ids, M, threshold=0.8, top_k=10)
    new = emb_synonym_edges(ids, M, threshold=0.8, top_k=10)
    assert {(a, b) for a, b, _ in new} == {(a, b) for a, b, _ in old}
    old_by_pair = {(a, b): s for a, b, s in old}
    new_by_pair = {(a, b): s for a, b, s in new}
    for pair, sim in new_by_pair.items():
        assert abs(sim - old_by_pair[pair]) < 1e-4


def test_emb_synonym_edges_output_pairs_are_ordered_and_deduped():
    """(a,b) pairs are emitted with a<b (stable ordering) and no duplicate
    unordered pair appears twice, even though hnsw KNN is reciprocal (i finds
    j in its row AND j finds i in its row)."""
    from app.services.kg.ppr import emb_synonym_edges
    ids, M = _synonym_fixture()
    edges = emb_synonym_edges(ids, M, threshold=0.8, top_k=10)
    seen = set()
    for a, b, sim in edges:
        ia, ib = ids.index(a), ids.index(b)
        assert ia < ib, f"pair not ordered: {a},{b}"
        assert (a, b) not in seen, f"duplicate pair: {a},{b}"
        seen.add((a, b))
        assert 0.8 <= sim <= 1.0001


def test_emb_synonym_edges_does_not_mutate_input_matrix():
    """No more `M = M / norms` normalized COPY of the whole matrix — the
    function must not mutate the caller's array in place either (hnswlib
    space='cosine' normalizes internally at both index-build and query time,
    so an explicit normalization pass is redundant and is dropped)."""
    from app.services.kg.ppr import emb_synonym_edges
    ids, M = _synonym_fixture()
    before = M.copy()
    emb_synonym_edges(ids, M, threshold=0.8, top_k=10)
    assert (M == before).all()


def test_emb_synonym_edges_prebuilt_index_matches_self_built():
    """Passing a caller-built hnswlib.Index (prebuilt_index=) must produce the
    same edge set as letting the function build its own (same data/params)."""
    import hnswlib
    from app.services.kg.ppr import emb_synonym_edges
    ids, M = _synonym_fixture()
    dim = M.shape[1]
    idx = hnswlib.Index(space="cosine", dim=dim)
    idx.init_index(max_elements=len(ids), ef_construction=200, M=16, random_seed=42)
    idx.add_items(M, list(range(len(ids))))

    self_built = emb_synonym_edges(ids, M, threshold=0.8, top_k=10)
    prebuilt = emb_synonym_edges(ids, M, threshold=0.8, top_k=10, prebuilt_index=idx)
    assert {(a, b) for a, b, _ in self_built} == {(a, b) for a, b, _ in prebuilt}


def test_emb_synonym_edges_ef_construction_param_used_when_self_building(monkeypatch):
    """ef_construction is forwarded to init_index instead of the module
    hardcoding 200 — ppr.py stays a pure function (no settings import), the
    caller (sqlite_repository) passes the configured value."""
    import hnswlib
    from app.services.kg.ppr import emb_synonym_edges
    ids, M = _synonym_fixture()

    seen_kwargs = {}
    real_init = hnswlib.Index.init_index

    def spy_init(self, *a, **kw):
        seen_kwargs.update(kw)
        return real_init(self, *a, **kw)

    monkeypatch.setattr(hnswlib.Index, "init_index", spy_init)
    emb_synonym_edges(ids, M, threshold=0.8, top_k=10, ef_construction=77)
    assert seen_kwargs.get("ef_construction") == 77
