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
