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


class TestTask3TierAnnotatedRender:
    def _make_federated_subgraph(self):
        """Build a 3-node subgraph spanning base→personal."""
        from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        # Seed from B1 (base), traverse derived_from (base) and depends_on (personal)
        return multihop_subgraph(
            G, oid_to_idx, idx_to_oid,
            seed_ids=["B1"],
            edge_types={"derived_from", "supports", "depends_on"},
            max_depth=3, max_fan_out=10,
        )

    def test_id_map_carries_tier(self):
        """id_map entries must include a 'tier' key."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        _, id_map = render_subgraph_context(sub)
        for key, entry in id_map.items():
            assert "tier" in entry, f"id_map[{key}] missing 'tier'"

    def test_base_edge_node_gets_base_tier_in_id_map(self):
        """A node reached via a base edge carries tier='base' in id_map."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        _, id_map = render_subgraph_context(sub)
        # B2 is reached via r1 (base edge derived_from)
        b2_entry = next(v for v in id_map.values() if v["object_id"] == "B2")
        assert b2_entry["tier"] == "base"

    def test_personal_edge_node_gets_personal_tier_in_id_map(self):
        """A node reached via a personal edge carries tier='personal' in id_map."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        _, id_map = render_subgraph_context(sub)
        # P2 is reached via r3 (personal edge depends_on from P1);
        # or P2 itself reached via personal edge.
        personal_entries = [v for v in id_map.values() if v["tier"] == "personal"]
        assert len(personal_entries) >= 1, "expected at least one personal-tier node in id_map"

    def test_context_block_contains_tier_tag_on_node_lines(self):
        """Context block lines for non-seed nodes carry [tier] tag: '[Claim][base] …'."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        ctx, _ = render_subgraph_context(sub)
        # At least one line must carry a tier tag
        assert "[base]" in ctx or "[personal]" in ctx, (
            f"No tier tag in context block:\n{ctx}")

    def test_chain_annotation_carries_tier(self):
        """Chain annotation lines must include tier information per hop."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        ctx, _ = render_subgraph_context(sub)
        lines = ctx.splitlines()
        chain_lines = [l for l in lines if "--" in l and "-->" in l]
        assert chain_lines, "expected at least one chain annotation line"
        for line in chain_lines:
            assert "tier=" in line, (
                f"Chain annotation line missing tier= tag:\n  {line}")


_AUTHORITY_FACTOR = {"base": 1.0, "personal": 0.85}


class TestTask4AuthorityWeightedChainTrust:
    def _build_mixed_subgraph(self):
        """Subgraph with one base edge (confidence=1.0) and one personal edge (confidence=1.0).

        DEVIATION from plan fixture: the plan seeded only ["B1"] with
        {derived_from, supports}, but the fixture's personal edges point INTO B2
        (P2->B2 supports) / from P1 (P1->P2 depends_on), so BFS from B1 reaches
        only B2 via the base derived_from edge — no personal edge is ever
        traversed, leaving chain_trust=1.0.  To exercise the plan's stated intent
        (a mixed base+personal chain with chain_trust capped at 0.85), we seed
        BOTH B1 (yields base derived_from B1->B2) and P1 (yields personal
        depends_on P1->P2) and include depends_on in the edge set.
        """
        from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        return multihop_subgraph(
            G, oid_to_idx, idx_to_oid,
            seed_ids=["B1", "P1"],
            edge_types={"derived_from", "supports", "depends_on"},
            max_depth=2, max_fan_out=10,
        )

    class _NoLLM:
        configured = False

    def test_chain_trust_is_weakest_effective_confidence(self):
        """With a personal edge (confidence=1.0), chain_trust = 0.85 (authority factor)."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._build_mixed_subgraph()
        result = verify_chain_edges(sub, self._NoLLM())
        # Base edge: effective_conf = 1.0 * 1.0 = 1.0
        # Personal edge (P2→B2, confidence=1.0): effective_conf = 1.0 * 0.85 = 0.85
        # chain_trust = min(1.0, 0.85) = 0.85
        assert abs(result["chain_trust"] - 0.85) < 1e-9, (
            f"Expected chain_trust≈0.85, got {result['chain_trust']}")

    def test_all_base_edges_chain_trust_is_1(self):
        """A chain with only base edges and confidence=1.0 → chain_trust = 1.0."""
        import rustworkx as rx
        from app.services.kg.graph_reason import multihop_subgraph, verify_chain_edges
        G = rx.PyDiGraph()
        n1 = G.add_node({"object_id": "X", "object_type": "Claim", "name": "X"})
        n2 = G.add_node({"object_id": "Y", "object_type": "Claim", "name": "Y"})
        G.add_edge(n1, n2, {"edge_type": "supports", "confidence": 1.0,
                             "evidence": [], "tier": "base"})
        idx_to_oid = {n1: "X", n2: "Y"}
        oid_to_idx = {"X": n1, "Y": n2}
        sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["X"],
                                {"supports"}, max_depth=1, max_fan_out=10)
        result = verify_chain_edges(sub, self._NoLLM())
        assert result["chain_trust"] == 1.0

    def test_edge_results_carry_tier(self):
        """edge_results entries must carry a 'tier' field."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._build_mixed_subgraph()
        result = verify_chain_edges(sub, self._NoLLM())
        for er in result["edge_results"]:
            assert "tier" in er, f"edge_result missing 'tier': {er}"

    def test_authority_notes_surfaces_personal_edge(self):
        """authority_notes must list a warning for each personal-tier edge."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._build_mixed_subgraph()
        result = verify_chain_edges(sub, self._NoLLM())
        assert "authority_notes" in result
        # At least one note for the personal edge
        assert any("personal" in note.lower() for note in result["authority_notes"]), (
            f"No personal-tier note in authority_notes: {result['authority_notes']}")

    def test_flagged_entry_carries_tier(self):
        """When an edge is flagged (LLM invalid), the flagged entry carries 'tier'."""
        import json
        from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges

        class _FailLLM:
            configured = True
            def chat_json(self, messages, schema, **kw):
                return json.dumps({"valid": False, "reason": "bad"})

        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["B1"],
                                {"derived_from"}, max_depth=1, max_fan_out=10)
        result = verify_chain_edges(sub, _FailLLM())
        assert result["flagged"], "expected at least one flagged edge"
        for f in result["flagged"]:
            assert "tier" in f, f"flagged entry missing 'tier': {f}"


class TestTask5ConflictPrecedence:
    def _conflicting_subgraph(self):
        """Manually built subgraph with a base AND personal edge on the same hop.

        Triple format: (node_payload, edge_payload_or_None, src_oid).
        We create:
          - seed node X (no edge)
          - node Y via base edge (derived_from, valid evidence)
          - node Y′ (same logical concept as Y) via personal edge (derived_from,
            but LLM will say invalid)
        In practice multihop only emits one hop per target, so we construct
        the subgraph directly to exercise the conflict logic.
        """
        # Two hops to the SAME src_oid/tgt_oid pair: one base, one personal.
        seed   = ({"object_id": "X", "object_type": "Concept", "name": "X"}, None, None)
        base_hop = (
            {"object_id": "Y", "object_type": "Claim", "name": "Y"},
            {"edge_type": "derived_from", "confidence": 1.0, "tier": "base",
             "evidence": [{"quote": "base evidence for Y"}]},
            "X",
        )
        pers_hop = (
            {"object_id": "Y", "object_type": "Claim", "name": "Y"},
            # DEVIATION from plan: the plan used an empty quote ("") to make the
            # LLM reject this edge, but verify_chain_edges treats an empty quote
            # as "cannot verify → fail-open (valid)", so the edge never reaches
            # the LLM and is never flagged.  We give it a NON-EMPTY but
            # unsubstantiated quote that _SelectiveLLM rejects, which is the
            # operational definition of "contradicting" the plan intends.
            {"edge_type": "derived_from", "confidence": 1.0, "tier": "personal",
             "evidence": [{"quote": "personal note claims Y but cites nothing"}]},
            "X",
        )
        return [seed, base_hop, pers_hop]

    class _SelectiveLLM:
        """Returns valid=True for base evidence, valid=False otherwise."""
        configured = True
        def chat_json(self, messages, schema, **kw):
            import json as _j
            content = messages[0]["content"] if messages else ""
            if "base evidence for Y" in content:
                return _j.dumps({"valid": True, "reason": "base evidence ok"})
            return _j.dumps({"valid": False, "reason": "no evidence"})

    def test_personal_edge_flagged_with_base_override(self):
        """When base and personal edges contradict, personal is flagged base_override=True."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._conflicting_subgraph()
        result = verify_chain_edges(sub, self._SelectiveLLM())
        pers_flags = [f for f in result["flagged"] if f.get("tier") == "personal"]
        assert pers_flags, "personal edge should be in flagged"
        assert pers_flags[0].get("base_override") is True, (
            "Expected base_override=True on personal flagged entry")

    def test_base_edge_not_flagged_despite_conflict(self):
        """The base edge itself must not appear in flagged."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._conflicting_subgraph()
        result = verify_chain_edges(sub, self._SelectiveLLM())
        base_flags = [f for f in result["flagged"] if f.get("tier") == "base"]
        assert not base_flags, f"base edge must not be flagged; got {base_flags}"

    def test_authority_notes_records_override(self):
        """authority_notes must record the base-wins override."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._conflicting_subgraph()
        result = verify_chain_edges(sub, self._SelectiveLLM())
        assert any("base_override" in note or "overridden" in note.lower()
                   for note in result["authority_notes"]), (
            f"No override note in authority_notes: {result['authority_notes']}")
