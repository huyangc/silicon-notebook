"""Track D — Cross-tier multi-hop reasoning tests.

Two synthetic notebooks:
  base_nb:     nodes B1 (Formula), B2 (Claim); edge B1→B2 derived_from
  personal_nb: nodes P1 (Concept), P2 (Claim); edge P2→B2 supports (cross-tier)

Each relation row carries a "notebook_id" key so build_rx_graph can look up tier.
"""
import json
import pytest

# ── Synthetic fixture ─────────────────────────────────────────────────────────

BASE_NB_ID = "nb-base-001"
PERS_NB_ID = "nb-pers-001"

NODES_FEDERATED = {
    "B1": {"type": "Formula", "name": "Base Formula"},
    "B2": {"type": "Claim",   "name": "Base Claim"},
    "P1": {"type": "Concept", "name": "Personal Concept"},
    "P2": {"type": "Claim",   "name": "Personal Claim"},
}

# Relations carry notebook_id so tier_map lookup works.
RELATIONS_FEDERATED = [
    {"id": "r1", "notebook_id": BASE_NB_ID,
     "source_object_id": "B1", "target_object_id": "B2",
     "edge_type": "derived_from",
     "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "base formula derives base claim"}]},
    {"id": "r2", "notebook_id": PERS_NB_ID,
     "source_object_id": "P2", "target_object_id": "B2",
     "edge_type": "supports",
     "evidence": [{"file": "f2", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "personal claim supports base claim"}]},
    {"id": "r3", "notebook_id": PERS_NB_ID,
     "source_object_id": "P1", "target_object_id": "P2",
     "edge_type": "depends_on",
     "evidence": [{"file": "f2", "char_start": 20, "char_end": 30,
                   "line_start": 2, "line_end": 2, "quote": "personal concept depends on personal claim"}]},
]

TIER_MAP = {BASE_NB_ID: "base", PERS_NB_ID: "personal"}


class TestTask1FederatedGraphBuild:
    def test_base_edge_stamped_base(self):
        """Relations from a base notebook get tier='base' in the edge payload."""
        from app.services.kg.graph_reason import build_rx_graph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        b1_idx = oid_to_idx["B1"]
        b2_idx = oid_to_idx["B2"]
        payload = G.get_edge_data(b1_idx, b2_idx)
        assert payload["tier"] == "base"

    def test_personal_edge_stamped_personal(self):
        """Relations from a personal notebook get tier='personal'."""
        from app.services.kg.graph_reason import build_rx_graph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        p2_idx = oid_to_idx["P2"]
        b2_idx = oid_to_idx["B2"]
        payload = G.get_edge_data(p2_idx, b2_idx)
        assert payload["tier"] == "personal"

    def test_backward_compat_no_tier_map(self):
        """Calling build_rx_graph without tier_map still stamps 'base' (default)."""
        from app.services.kg.graph_reason import build_rx_graph
        # Use original single-notebook fixture (no notebook_id in relations).
        NODES = {
            "A": {"type": "Formula", "name": "Node A"},
            "B": {"type": "Claim",   "name": "Node B"},
        }
        RELS = [{"id": "r1", "source_object_id": "A", "target_object_id": "B",
                 "edge_type": "derived_from", "evidence": []}]
        G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELS)
        payload = G.get_edge_data(oid_to_idx["A"], oid_to_idx["B"])
        assert payload["tier"] == "base"
