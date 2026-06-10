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


class TestTask2FederatedRxGraph:
    @pytest.fixture
    def repo_two_notebooks(self, tmp_path, monkeypatch):
        """Returns (repo, base_nb_id, personal_nb_id) with relations seeded."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        from app.core.config import Settings
        from app.services.sqlite_repository import SQLiteRepository
        from app.services.embedding import FakeEmbedder
        from app.models.schemas import NotebookCreate
        r = SQLiteRepository(Settings())
        r.embedder = FakeEmbedder(dim=16)

        base_nb = r.create_notebook(NotebookCreate(name="base"))
        r.mark_notebook_base(base_nb.id)
        r.store_kg(base_nb.id, None, [
            {"local_id": "B1", "object_type": "formula",
             "payload": {"name": "Base Formula", "section_path": "1"}, "evidence": []},
            {"local_id": "B2", "object_type": "claim",
             "payload": {"name": "Base Claim", "section_path": "1"}, "evidence": []},
        ], [
            {"local_id": "r1", "source_local_id": "B1", "target_local_id": "B2",
             "edge_type": "derived_from", "evidence": [
                 {"source_id": "src1", "source_title": "s", "element_id": "e1",
                  "element_type": "paragraph", "location_label": "p1",
                  "quoted_span": "base formula derives base claim", "confidence": 1.0}
             ]},
        ])

        pers_nb = r.create_notebook(NotebookCreate(name="personal"))
        r.store_kg(pers_nb.id, None, [
            {"local_id": "P1", "object_type": "concept",
             "payload": {"name": "Personal Concept", "section_path": "1"}, "evidence": []},
            {"local_id": "P2", "object_type": "claim",
             "payload": {"name": "Personal Claim", "section_path": "1"}, "evidence": []},
        ], [
            {"local_id": "r2", "source_local_id": "P1", "target_local_id": "P2",
             "edge_type": "supports", "evidence": [
                 {"source_id": "src2", "source_title": "t", "element_id": "e2",
                  "element_type": "paragraph", "location_label": "p2",
                  "quoted_span": "personal concept supports personal claim", "confidence": 1.0}
             ]},
        ])
        return r, base_nb.id, pers_nb.id

    def test_federated_graph_contains_nodes_from_both_notebooks(self, repo_two_notebooks):
        """_federated_rx_graph merges nodes from base + personal into one graph."""
        repo, base_id, pers_id = repo_two_notebooks
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_id)
        # All 4 objects must appear as nodes (B1, B2 from base; P1, P2 from personal).
        assert G.num_nodes() >= 4

    def test_federated_graph_base_edges_stamped_base(self, repo_two_notebooks):
        """Edges from the base notebook must carry tier='base'."""
        repo, base_id, pers_id = repo_two_notebooks
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_id)
        # Find the derived_from edge (B1→B2, both from base_nb).
        edge_tiers = {
            G.get_edge_data(src, tgt)["tier"]
            for src, tgt, _ in G.edge_index_map().values()
        }
        assert "base" in edge_tiers

    def test_federated_graph_personal_edges_stamped_personal(self, repo_two_notebooks):
        """Edges from the personal notebook must carry tier='personal'."""
        repo, base_id, pers_id = repo_two_notebooks
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_id)
        edge_tiers = {
            G.get_edge_data(src, tgt)["tier"]
            for src, tgt, _ in G.edge_index_map().values()
        }
        assert "personal" in edge_tiers

    def test_federated_graph_version_key_includes_both_notebooks(self, repo_two_notebooks):
        """Calling _federated_rx_graph twice returns the same cached object
        (version key covers both notebooks; no rebuild on same data)."""
        repo, base_id, pers_id = repo_two_notebooks
        result1 = repo._federated_rx_graph(pers_id)
        result2 = repo._federated_rx_graph(pers_id)
        # Same PyDiGraph object returned from cache.
        assert result1[0] is result2[0]
