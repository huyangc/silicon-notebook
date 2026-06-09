# backend/tests/test_graph_reason.py
import pytest

# ── synthetic fixture ────────────────────────────────────────────────────────
# 4 nodes: A→B derived_from, B→C supports, C→D depends_on; one bad edge D→A
NODES = {
    "A": {"type": "Formula",  "name": "Node A"},
    "B": {"type": "Claim",    "name": "Node B"},
    "C": {"type": "Claim",    "name": "Node C"},
    "D": {"type": "Concept",  "name": "Node D"},
}
RELATIONS = [
    {"id": "r1", "source_object_id": "A", "target_object_id": "B",
     "edge_type": "derived_from",
     "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "A derives B"}]},
    {"id": "r2", "source_object_id": "B", "target_object_id": "C",
     "edge_type": "supports",
     "evidence": [{"file": "f1", "char_start": 20, "char_end": 30,
                   "line_start": 2, "line_end": 2, "quote": "B supports C"}]},
    {"id": "r3", "source_object_id": "C", "target_object_id": "D",
     "edge_type": "depends_on",
     "evidence": [{"file": "f1", "char_start": 40, "char_end": 50,
                   "line_start": 3, "line_end": 3, "quote": "C depends on D"}]},
    {"id": "r4", "source_object_id": "D", "target_object_id": "A",
     "edge_type": "contrasts_with",
     "evidence": []},
]


def test_build_rx_graph_node_count():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    assert G.num_nodes() == 4


def test_build_rx_graph_edge_count():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    assert G.num_edges() == 4


def test_build_rx_graph_edge_payload():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    src_idx = oid_to_idx["A"]
    tgt_idx = oid_to_idx["B"]
    # rustworkx: G.get_edge_data(src, tgt) returns the payload
    payload = G.get_edge_data(src_idx, tgt_idx)
    assert payload["edge_type"] == "derived_from"
    assert payload["tier"] == "base"
    assert payload["confidence"] == 1.0
    assert len(payload["evidence"]) == 1
    assert payload["evidence"][0]["quote"] == "A derives B"


def test_build_rx_graph_node_payload():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    idx = oid_to_idx["C"]
    node_data = G[idx]
    assert node_data["object_id"] == "C"
    assert node_data["object_type"] == "Claim"
    assert node_data["name"] == "Node C"


# ── Task 2: multihop subgraph ────────────────────────────────────────────────
# multihop_subgraph returns 3-tuples (node_payload, edge_payload_or_None,
# src_object_id_or_None); the src oid lets render_subgraph_context emit full
# chain annotations in Task 3.

def test_multihop_subgraph_depth2_derived_supports():
    """Seed=A, edge_types={derived_from, supports}, depth=2 → A→B→C."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["A"],
        edge_types={"derived_from", "supports"},
        max_depth=2,
        max_fan_out=10,
    )
    visited_oids = [n["object_id"] for n, _, _ in result]
    assert "A" in visited_oids
    assert "B" in visited_oids
    assert "C" in visited_oids
    assert "D" not in visited_oids   # depends_on not in edge_types


def test_multihop_subgraph_all_three_hops():
    """Seed=A, all three default edges, depth=3 → A→B→C→D."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, DEFAULT_REASONING_EDGES
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["A"],
        edge_types=DEFAULT_REASONING_EDGES,
        max_depth=3,
        max_fan_out=10,
    )
    visited_oids = [n["object_id"] for n, _, _ in result]
    assert visited_oids == ["A", "B", "C", "D"]


def test_multihop_subgraph_fan_out_capped():
    """Fan-out cap: if a node has 3 out-edges of the right type and cap=1,
    only the highest-confidence one is followed."""
    import rustworkx as rx
    from app.services.kg.graph_reason import multihop_subgraph
    G = rx.PyDiGraph()
    n_seed = G.add_node({"object_id": "S", "object_type": "Concept", "name": "S"})
    n_hi   = G.add_node({"object_id": "HI", "object_type": "Claim",   "name": "HI"})
    n_lo   = G.add_node({"object_id": "LO", "object_type": "Claim",   "name": "LO"})
    G.add_edge(n_seed, n_hi, {"edge_type": "supports", "confidence": 0.9,
                               "evidence": [], "tier": "base"})
    G.add_edge(n_seed, n_lo, {"edge_type": "supports", "confidence": 0.2,
                               "evidence": [], "tier": "base"})
    idx_to_oid = {n_seed: "S", n_hi: "HI", n_lo: "LO"}
    oid_to_idx = {"S": n_seed, "HI": n_hi, "LO": n_lo}
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["S"],
        edge_types={"supports"},
        max_depth=1,
        max_fan_out=1,
    )
    visited_oids = [n["object_id"] for n, _, _ in result]
    assert "HI" in visited_oids
    assert "LO" not in visited_oids


def test_multihop_subgraph_cycle_guard():
    """contrasts_with D→A: traversal with all edges + cycle guard must not loop."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    # include contrasts_with so D→A edge is eligible
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["A"],
        edge_types={"derived_from", "supports", "depends_on", "contrasts_with"},
        max_depth=5,
        max_fan_out=10,
    )
    visited_oids = [n["object_id"] for n, _, _ in result]
    # each node appears at most once despite the cycle
    assert len(visited_oids) == len(set(visited_oids))
