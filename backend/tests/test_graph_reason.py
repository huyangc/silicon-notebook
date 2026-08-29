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


def test_build_rx_graph_drops_historical_type_invalid_edges():
    from app.services.kg.graph_reason import build_rx_graph

    relations = RELATIONS + [{
        "id": "invalid",
        "source_object_id": "D",  # Concept cannot precede a Formula
        "target_object_id": "A",
        "edge_type": "precedes",
        "evidence": [],
    }]
    graph, _idx_to_oid, _oid_to_idx = build_rx_graph(NODES, relations)
    assert graph.num_edges() == len(RELATIONS)


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


# ── Task 3: render subgraph → [k] context + ask graph mode ───────────────────

def test_render_subgraph_context_k_ids():
    """Each node in the subgraph gets a stable k{i} key; seed has no edge annotation."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from", "supports"}, max_depth=2, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    # k1 = seed A (no edge), k2 = B (derived_from), k3 = C (supports)
    assert "k1" in id_map and id_map["k1"]["object_id"] == "A"
    assert "k2" in id_map and id_map["k2"]["object_id"] == "B"
    assert "k3" in id_map and id_map["k3"]["object_id"] == "C"


def test_render_subgraph_context_edge_annotation():
    """Edge annotation preserves the extracted source → target direction."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    # The context block must contain the chain annotation
    assert "[k1] Node A --derived_from--> [k2] Node B" in ctx


def test_render_subgraph_context_id_offset():
    """id_offset=5 starts keys at k6, not k1."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=5)
    assert "k6" in id_map
    assert "k7" in id_map
    assert "k1" not in id_map


def test_render_subgraph_context_evidence_quote():
    """Evidence quote from the edge is included in the context block."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    assert "A derives B" in ctx


# ── Task 4: adversarial chain verification (LLM mocked) ──────────────────────

class _FakeLLMClient:
    configured = True

    def __init__(self, responses):
        self._responses = iter(responses)

    def chat_json(self, messages, schema_hint, **kwargs):
        return next(self._responses)


def test_verify_chain_edges_all_pass():
    """All edges verified → chain_trust = 1.0, no flagged edges."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from", "supports"}, max_depth=2, max_fan_out=10)
    import json
    # LLM returns valid=true for every edge
    llm = _FakeLLMClient([
        json.dumps({"valid": True,  "reason": "ok"}),
        json.dumps({"valid": True,  "reason": "ok"}),
    ])
    result = verify_chain_edges(sub, llm)
    assert result["chain_trust"] == 1.0
    assert result["flagged"] == []


def test_verify_chain_edges_bad_edge_flagged():
    """One edge returns valid=false → that edge is flagged; chain_trust drops."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from", "supports"}, max_depth=2, max_fan_out=10)
    import json
    # First edge (A→B derived_from) passes; second (B→C supports) fails
    llm = _FakeLLMClient([
        json.dumps({"valid": True,  "reason": "ok"}),
        json.dumps({"valid": False, "reason": "evidence does not support this"}),
    ])
    result = verify_chain_edges(sub, llm)
    assert len(result["flagged"]) == 1
    assert result["flagged"][0]["edge_type"] == "supports"
    assert result["chain_trust"] < 1.0


def test_verify_chain_edges_majority_vote():
    """Majority vote (votes=3): 2 valid + 1 invalid → valid; 1 valid + 2 invalid → invalid."""
    from app.services.kg.graph_reason import verify_chain_edges, build_rx_graph, multihop_subgraph
    import json
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    # 3 votes for the single edge: 2 valid → edge passes
    llm_pass = _FakeLLMClient([
        json.dumps({"valid": True}),
        json.dumps({"valid": False}),
        json.dumps({"valid": True}),
    ])
    result_pass = verify_chain_edges(sub, llm_pass, votes=3)
    assert result_pass["flagged"] == []

    G2, idx_to_oid2, oid_to_idx2 = build_rx_graph(NODES, RELATIONS)
    sub2 = multihop_subgraph(G2, oid_to_idx2, idx_to_oid2, ["A"],
                             {"derived_from"}, max_depth=1, max_fan_out=10)
    # 3 votes: 1 valid + 2 invalid → edge fails
    llm_fail = _FakeLLMClient([
        json.dumps({"valid": True}),
        json.dumps({"valid": False}),
        json.dumps({"valid": False}),
    ])
    result_fail = verify_chain_edges(sub2, llm_fail, votes=3)
    assert len(result_fail["flagged"]) == 1


def test_verify_chain_edges_no_edges_no_calls():
    """Seed-only subgraph (no edges) → chain_trust=1.0, zero LLM calls."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges
    import json
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    # Depth 0 → only the seed, no edges
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=0, max_fan_out=10)
    calls = []

    class _CountingLLM:
        configured = True

        def chat_json(self, *args, **kwargs):
            calls.append(1)
            return json.dumps({"valid": True})

    result = verify_chain_edges(sub, _CountingLLM())
    assert result["chain_trust"] == 1.0
    assert len(calls) == 0


# ── Cache-isolation regression (cross-ask leak) ──────────────────────────────
# Bug: multihop_subgraph used to return LIVE references into the cached
# PyDiGraph (rustworkx get_edge_data / G[idx] hand back the same object). The
# now-retired full-graph ask engine demoted a flagged edge in-place
# (edge["confidence"] = 0.05) before re-rendering, which mutated the CACHED
# graph and leaked into the NEXT ask on the same cached graph. Any caller that
# demotes edges the same way (verify_chain_edges is still exercised standalone
# below) would hit the same leak, so multihop_subgraph must hand back COPIES
# so the cache stays pristine.

def test_multihop_subgraph_returns_edge_payload_copies():
    """Edge payloads returned by multihop_subgraph must be copies, not the live
    dicts stored in the PyDiGraph (so callers can mutate them safely)."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    src_idx, tgt_idx = oid_to_idx["A"], oid_to_idx["B"]
    cached_edge = G.get_edge_data(src_idx, tgt_idx)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    returned_edges = [e for _n, e, _s in sub if e is not None]
    assert returned_edges, "expected at least one edge in the subgraph"
    # Same content...
    assert returned_edges[0]["edge_type"] == cached_edge["edge_type"]
    # ...but NOT the same object as the one held inside the cached graph.
    assert returned_edges[0] is not cached_edge


def test_flag_demotion_does_not_leak_into_next_ask():
    """Cross-ask cache isolation: build the graph ONCE (as the version-cache
    would hand it out), traverse + demote a flagged edge in the first ask, then
    traverse AGAIN on the SAME cached graph. The cached graph's edge confidence
    must still be 1.0 — the first ask's demotion must not leak into the second.
    """
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph

    # Build ONCE — this stands in for the version-keyed cached PyDiGraph that
    # _rx_graph hands out by reference across multiple asks.
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    src_idx, tgt_idx = oid_to_idx["A"], oid_to_idx["B"]
    assert G.get_edge_data(src_idx, tgt_idx)["confidence"] == 1.0

    # ── First ask: traverse, then demote the flagged edge in-place exactly as
    # the (now-retired) full-graph ask engine used to: edge["confidence"] = 0.05.
    sub1 = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                             {"derived_from"}, max_depth=1, max_fan_out=10)
    flagged = [e for _n, e, _s in sub1 if e and e.get("edge_type") == "derived_from"]
    assert flagged, "expected the derived_from edge in the first traversal"
    for edge in flagged:
        edge["confidence"] = 0.05
    # Within this ask the demotion is visible on the returned payload.
    assert flagged[0]["confidence"] == 0.05

    # ── The cached graph itself must be untouched by the first ask's demotion.
    assert G.get_edge_data(src_idx, tgt_idx)["confidence"] == 1.0

    # ── Second ask on the SAME cached graph: it must see the original 1.0,
    # not the leaked 0.05.
    sub2 = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                             {"derived_from"}, max_depth=1, max_fan_out=10)
    edges2 = [e for _n, e, _s in sub2 if e and e.get("edge_type") == "derived_from"]
    assert edges2, "expected the derived_from edge in the second traversal"
    assert edges2[0]["confidence"] == 1.0


# ── TD1: transit-only cluster hubs (cross-doc bridge, never cited) ───────────
# Synthetic fixture: two documents with NO direct edge between them, only
# co-membership in a concept cluster.  A synthetic hub node bridges them so
# multi-hop reasoning can cross documents, but the hub is TRANSIT-ONLY: it must
# never appear in the multihop result, the rendered context, the id_map, or be
# handed to the edge verifier (so the LLM cannot mistake "cluster:..." for a
# real answer node).
#
# doc-A node a1 ──(intra-doc)──> a2     doc-B node b1 ──(intra-doc)──> b2
#   a1 and b1 are both members of cluster "K-x"; no a*↔b* relation exists.
CD_NODES = {
    "a1": {"type": "Concept", "name": "Alpha One"},
    "a2": {"type": "Claim",   "name": "Alpha Two"},
    "b1": {"type": "Concept", "name": "Beta One"},
    "b2": {"type": "Claim",   "name": "Beta Two"},
}
CD_RELATIONS = [
    {"id": "ra", "source_object_id": "a1", "target_object_id": "a2",
     "edge_type": "supports",
     "evidence": [{"file": "fa", "char_start": 0, "char_end": 5,
                   "line_start": 1, "line_end": 1, "quote": "a1 supports a2"}]},
    {"id": "rb", "source_object_id": "b1", "target_object_id": "b2",
     "edge_type": "supports",
     "evidence": [{"file": "fb", "char_start": 0, "char_end": 5,
                   "line_start": 1, "line_end": 1, "quote": "b1 supports b2"}]},
]


def test_cluster_groups_none_identical_to_before():
    """Regression: cluster_groups=None (and omitted) → byte-identical graph
    shape AND no `kind` change to behavior. Same node/edge counts as the
    no-cluster build; multihop result unchanged."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph

    G_omit, i2o_omit, o2i_omit = build_rx_graph(CD_NODES, CD_RELATIONS)
    G_none, i2o_none, o2i_none = build_rx_graph(CD_NODES, CD_RELATIONS,
                                                cluster_groups=None)
    assert G_omit.num_nodes() == 4
    assert G_omit.num_edges() == 2
    assert G_none.num_nodes() == 4
    assert G_none.num_edges() == 2

    # multihop from a1 stays inside doc-A (no bridge) — exactly as before.
    sub = multihop_subgraph(G_none, o2i_none, i2o_none, ["a1"],
                            {"supports"}, max_depth=3, max_fan_out=10)
    oids = [n["object_id"] for n, _, _ in sub]
    assert oids == ["a1", "a2"]
    assert "b1" not in oids and "b2" not in oids


def test_real_nodes_tagged_kind_entity():
    """When cluster_groups is in play, every real node payload carries
    kind='entity' so the hub (kind='cluster') is distinguishable downstream."""
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        CD_NODES, CD_RELATIONS, cluster_groups={"K-x": ["a1", "b1"]})
    for oid, idx in oid_to_idx.items():
        assert G[idx].get("kind") == "entity", f"{oid} should be kind=entity"


def test_cluster_hub_bridges_across_documents():
    """The core cross-doc win: seed=a1 reaches b1 (and onward b2) PURELY through
    the synthetic hub for cluster 'K-x' — there is NO a*↔b* relation."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        CD_NODES, CD_RELATIONS,
        cluster_groups={"K-x": ["a1", "b1"]},
    )
    # Hub adds a node + 4 directed edges (hub↔a1, hub↔b1, both directions).
    assert G.num_nodes() == 5
    assert G.num_edges() == 2 + 4

    sub = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["a1"],
        # follow real edges + the synthetic synonym edge so mass crosses the hub
        edge_types={"supports", "synonym"},
        max_depth=4, max_fan_out=10,
    )
    oids = [n["object_id"] for n, _, _ in sub]
    # cross-doc bridge worked: b1 (and b2 one hop further) are reachable
    assert "b1" in oids, "expected to reach doc-B node b1 via the cluster hub"
    assert "b2" in oids, "expected to reach b2 one hop past the bridge"


def test_cluster_hub_never_emitted_rendered_or_verified():
    """The hub is TRANSIT-ONLY: never in the multihop result, never in the
    rendered context text or id_map, and never handed to the edge verifier."""
    from app.services.kg.graph_reason import (
        build_rx_graph, multihop_subgraph, render_subgraph_context,
        verify_chain_edges,
    )
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        CD_NODES, CD_RELATIONS,
        cluster_groups={"K-x": ["a1", "b1"]},
    )
    sub = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["a1"],
        edge_types={"supports", "synonym"},
        max_depth=4, max_fan_out=10,
    )

    # (1) hub node never emitted into the result
    result_oids = [n["object_id"] for n, _, _ in sub]
    assert "cluster:K-x" not in result_oids
    assert all(n.get("kind") != "cluster" for n, _, _ in sub)
    # No synthetic synonym edge leaks into the result either (its endpoint is a
    # hub, so the hop that produced it must have been suppressed).
    assert all((e is None or e.get("edge_type") != "synonym") for _, e, _ in sub)

    # (2) render: hub absent from text and id_map
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    assert "cluster:K-x" not in ctx
    assert "K-x" not in ctx
    assert all(v["object_id"] != "cluster:K-x" for v in id_map.values())
    assert all(not str(v["object_id"]).startswith("cluster:")
               for v in id_map.values())

    # (3) verify: the verifier must never see a hub as src or tgt endpoint
    seen_endpoints = []

    class _SpyLLM:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            seen_endpoints.append(messages[0]["content"])
            import json as _j
            return _j.dumps({"valid": True, "reason": "ok"})

    verify_chain_edges(sub, _SpyLLM())
    for content in seen_endpoints:
        assert "cluster:K-x" not in content
        assert "K-x" not in content


def test_cluster_single_member_adds_no_hub():
    """A cluster with only 1 present member needs no hub — graph shape is the
    no-cluster baseline (4 nodes, 2 edges)."""
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        CD_NODES, CD_RELATIONS,
        cluster_groups={"K-solo": ["a1"]},
    )
    assert G.num_nodes() == 4
    assert G.num_edges() == 2
    assert "cluster:K-solo" not in oid_to_idx


def test_cluster_member_absent_counts_as_not_present():
    """Members named in cluster_groups but absent from `nodes` don't count
    toward the ≥2 threshold; a1 present + ghost absent → lone member → no hub."""
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        CD_NODES, CD_RELATIONS,
        cluster_groups={"K-x": ["a1", "ghost-not-in-nodes"]},
    )
    assert G.num_nodes() == 4
    assert "cluster:K-x" not in oid_to_idx


def test_cluster_hub_cycle_safe_low_fanout():
    """Hub↔member both-direction edges form 2-cycles. With a tiny fan-out and a
    deep budget, traversal must still terminate and visit each node at most
    once (visited set guards the hub↔member ping-pong)."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        CD_NODES, CD_RELATIONS,
        cluster_groups={"K-x": ["a1", "b1"]},
    )
    sub = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["a1"],
        edge_types={"supports", "synonym"},
        max_depth=10, max_fan_out=1,
    )
    oids = [n["object_id"] for n, _, _ in sub]
    # no node appears twice despite hub↔member 2-cycles + deep budget
    assert len(oids) == len(set(oids))
