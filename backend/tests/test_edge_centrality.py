# backend/tests/test_edge_centrality.py
"""Tests for centrality computation in graph_reason.py.
All tests use synthetic PyDiGraph fixtures — no DB/IO.
"""
import pytest
import rustworkx as rx


# ── Synthetic graph fixture ──────────────────────────────────────────────────
# Linear chain A→B→C→D; B is on every A→D path (max betweenness).
# D has no out-edges (low pagerank contribution as source).

NODES = {
    "A": {"type": "Formula",  "name": "Node A"},
    "B": {"type": "Claim",    "name": "Node B"},
    "C": {"type": "Claim",    "name": "Node C"},
    "D": {"type": "Concept",  "name": "Node D"},
}
RELATIONS = [
    {"id": "r1", "source_object_id": "A", "target_object_id": "B",
     "edge_type": "derived_from", "evidence": [{"quote": "A→B"}]},
    {"id": "r2", "source_object_id": "B", "target_object_id": "C",
     "edge_type": "supports",     "evidence": [{"quote": "B→C"}]},
    {"id": "r3", "source_object_id": "C", "target_object_id": "D",
     "edge_type": "depends_on",   "evidence": [{"quote": "C→D"}]},
]


@pytest.fixture
def chain_graph():
    from app.services.kg.graph_reason import build_rx_graph
    return build_rx_graph(NODES, RELATIONS)


# ── compute_centrality ────────────────────────────────────────────────────────

# ── compute_edge_centrality ───────────────────────────────────────────────────

def test_compute_edge_centrality_returns_rel_id_dict(chain_graph):
    from app.services.kg.graph_reason import compute_edge_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_edge_centrality(G)
    # One entry per edge (3 edges in fixture)
    assert len(result) == 3
    # Keys are rel_id strings from the edge payload
    assert all(isinstance(k, str) for k in result)
    assert all(isinstance(v, float) for v in result.values())


def test_compute_edge_centrality_middle_edge_highest(chain_graph):
    """In a linear 4-node chain, the middle edges r2 (B→C) carries all paths
    between A and C, A and D; r1 (A→B) only carries paths starting from A.
    r2 should have >= betweenness of r1."""
    from app.services.kg.graph_reason import compute_edge_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_edge_centrality(G)
    # r2 (B→C) connects the chain at the middle — it's on more shortest paths
    assert result.get("r2", 0) >= result.get("r1", 0)


def test_compute_edge_centrality_empty_graph():
    from app.services.kg.graph_reason import compute_edge_centrality
    G = rx.PyDiGraph()
    assert compute_edge_centrality(G) == {}


def test_compute_edge_centrality_no_rel_id_falls_back(chain_graph):
    """Edges without a rel_id key in the payload are keyed by their edge index (int→str)."""
    from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality
    rels_no_id = [
        {"source_object_id": "A", "target_object_id": "B",
         "edge_type": "supports", "evidence": []},
    ]
    G2, _, _ = build_rx_graph(NODES, rels_no_id)
    result = compute_edge_centrality(G2)
    assert len(result) == 1
    # Key is a string (fallback to str(edge_index))
    assert all(isinstance(k, str) for k in result)
