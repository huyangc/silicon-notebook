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


def test_emb_synonym_edges_guard_skips_large():
    import numpy as np
    from app.services.kg.ppr import emb_synonym_edges
    M = np.ones((5, 3), dtype=np.float32)
    assert emb_synonym_edges(["a", "b", "c", "d", "e"], M, 0.8, 5, max_entities=3) == []
