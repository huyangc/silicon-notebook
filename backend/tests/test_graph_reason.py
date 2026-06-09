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
