"""Pure follow-chain composition and rendering tests (no repository / I/O)."""
from __future__ import annotations

import pytest

from app.services.kg.follow_chain import (
    EDGE_NODE_TYPES,
    TRANSITIVE_EDGE_TYPES,
    FollowChainResult,
    chain_anchor_relevances,
    compose_two_hop_paths,
    merge_validity_scopes,
    render_follow_chain_context,
)


def _node(
    oid: str,
    object_type: str,
    *,
    scope: dict | None = None,
    status: str = "approved",
) -> dict:
    payload = {"name": oid}
    if scope is not None:
        payload["validity_scope"] = scope
    return {
        "id": oid,
        "object_type": object_type,
        "status": status,
        "payload": payload,
    }


def _rel(
    rid: str,
    source: str,
    target: str,
    edge_type: str,
    *,
    quote: str | None = None,
    confidence: float | None = None,
    review_status: str = "pending",
    tier: str = "personal",
    notebook_id: str = "nb",
) -> dict:
    evidence = []
    if quote is not None:
        item = {
            "quote": quote,
            "source_id": "src-1",
            "element_id": f"el-{rid}",
            "location_label": "p.1",
        }
        if confidence is not None:
            item["confidence"] = confidence
        evidence.append(item)
    return {
        "id": rid,
        "notebook_id": notebook_id,
        "tier": tier,
        "source_object_id": source,
        "target_object_id": target,
        "edge_type": edge_type,
        "evidence": evidence,
        "review_status": review_status,
        "source_title": "Source One",
    }


def _chain_fixture(
    edge_type: str = "derived_from",
    object_type: str = "formula",
    *,
    first_type: str | None = None,
    second_type: str | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    nodes = {oid: _node(oid, object_type) for oid in ("A", "B", "C")}
    relations = [
        _rel("r1", "A", "B", first_type or edge_type, quote="A to B"),
        _rel("r2", "B", "C", second_type or edge_type, quote="B to C"),
    ]
    return nodes, relations


def test_whitelist_is_exactly_the_five_reviewed_transitive_relations():
    assert TRANSITIVE_EDGE_TYPES == {
        "derived_from", "kind_of", "prerequisite_of", "precedes", "part_of",
    }
    assert set(EDGE_NODE_TYPES) == set(TRANSITIVE_EDGE_TYPES)


@pytest.mark.parametrize(
    ("edge_type", "object_type"),
    [
        ("derived_from", "formula"),
        ("kind_of", "concept"),
        ("prerequisite_of", "claim"),
        ("precedes", "procedure"),
        ("part_of", "concept"),
    ],
)
def test_each_whitelisted_relation_composes_outward(edge_type, object_type):
    nodes, relations = _chain_fixture(edge_type, object_type)
    found = compose_two_hop_paths(
        nodes, relations, "A", direction="out", edge_type=edge_type,
    )
    assert len(found) == 1
    chain = found[0]
    assert (chain.source_object_id, chain.via_object_id, chain.target_object_id) == (
        "A", "B", "C",
    )
    assert (chain.source_id, chain.via_id, chain.target_id) == ("A", "B", "C")
    assert chain.inferred_edge_type == edge_type
    assert [(h.source_object_id, h.target_object_id) for h in chain.hops] == [
        ("A", "B"), ("B", "C"),
    ]


def test_inward_search_keeps_stored_direction_and_filters_the_other_endpoint():
    nodes, relations = _chain_fixture()
    found = compose_two_hop_paths(
        nodes,
        relations,
        "C",
        direction="in",
        target_object_id="A",
    )
    assert len(found) == 1
    chain = found[0]
    assert chain.search_direction == "in"
    assert (chain.source_object_id, chain.via_object_id, chain.target_object_id) == (
        "A", "B", "C",
    )
    assert [(h.source_object_id, h.target_object_id) for h in chain.hops] == [
        ("A", "B"), ("B", "C"),
    ]
    assert compose_two_hop_paths(
        nodes, relations, "C", direction="in", target_object_id="wrong",
    ) == []


def test_both_direction_finds_a_chain_from_either_end_only():
    nodes, relations = _chain_fixture()
    assert compose_two_hop_paths(nodes, relations, "A", direction="both")
    assert compose_two_hop_paths(nodes, relations, "C", direction="both")
    assert compose_two_hop_paths(nodes, relations, "B", direction="both") == []


def test_invalid_direction_is_rejected():
    nodes, relations = _chain_fixture()
    with pytest.raises(ValueError, match="direction"):
        compose_two_hop_paths(nodes, relations, "A", direction="sideways")


@pytest.mark.parametrize("edge_type", ["supports", "depends_on", "about", "contrasts_with"])
def test_non_transitive_relation_does_not_compose(edge_type):
    nodes, relations = _chain_fixture(edge_type, "claim")
    assert compose_two_hop_paths(nodes, relations, "A") == []
    assert compose_two_hop_paths(nodes, relations, "A", edge_type=edge_type) == []


def test_mixed_relation_types_do_not_compose():
    nodes, relations = _chain_fixture(
        object_type="concept", first_type="kind_of", second_type="part_of",
    )
    assert compose_two_hop_paths(nodes, relations, "A") == []


@pytest.mark.parametrize(
    ("edge_type", "invalid_type"),
    [
        ("derived_from", "concept"),
        ("kind_of", "claim"),
        ("prerequisite_of", "formula"),
        ("precedes", "concept"),
        ("part_of", "procedure"),
    ],
)
def test_node_type_contract_is_fail_closed(edge_type, invalid_type):
    valid_type = next(iter(EDGE_NODE_TYPES[edge_type]))
    nodes, relations = _chain_fixture(edge_type, valid_type)
    nodes["B"] = _node("B", invalid_type)
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_precedes_mixed_endpoint_types_do_not_compose():
    nodes = {
        "A": _node("A", "claim"),
        "B": _node("B", "formula"),
        "C": _node("C", "claim"),
    }
    relations = [
        _rel("r1", "A", "B", "precedes", quote="A before B"),
        _rel("r2", "B", "C", "precedes", quote="B before C"),
    ]
    assert compose_two_hop_paths(nodes, relations, "A") == []


@pytest.mark.parametrize("missing_index", [0, 1])
def test_every_hop_requires_a_nonempty_quote(missing_index):
    nodes, relations = _chain_fixture()
    relations[missing_index]["evidence"] = [{"quote": "  "}]
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_legacy_quoted_span_is_still_evidence_but_invalid_json_is_not():
    nodes, relations = _chain_fixture()
    relations[0]["evidence"] = '[{"quoted_span":"legacy quote"}]'
    assert compose_two_hop_paths(nodes, relations, "A")
    relations[0]["evidence"] = "not-json"
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_rejected_edge_and_deprecated_node_are_excluded():
    nodes, relations = _chain_fixture()
    relations[1]["review_status"] = "rejected"
    assert compose_two_hop_paths(nodes, relations, "A") == []

    nodes, relations = _chain_fixture()
    nodes["B"]["status"] = "deprecated"
    assert compose_two_hop_paths(nodes, relations, "A") == []

    nodes, relations = _chain_fixture()
    relations[1]["review_status"] = "unknown"
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_cross_notebook_hops_are_not_stitched():
    nodes, relations = _chain_fixture()
    relations[1]["notebook_id"] = "other-nb"
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_scope_merge_intersects_region_and_unions_assumptions_stably():
    merged = merge_validity_scopes([
        {
            "region": ["Saturation", "Weak Inversion"],
            "assumptions": ["long channel"],
            "approximation": "Small Signal",
            "range": "Low Frequency",
        },
        {
            "region": ["saturation"],
            "assumptions": ["perfect matching", "LONG CHANNEL"],
            "approximation": "small signal",
            "range": "low frequency",
        },
        {},
    ])
    assert merged == {
        "region": ["saturation"],
        "assumptions": ["long channel", "perfect matching"],
        "approximation": "small signal",
        "range": "low frequency",
    }


@pytest.mark.parametrize(
    "scopes",
    [
        [{"region": ["saturation"]}, {"region": ["weak inversion"]}],
        [{"approximation": "small signal"}, {"approximation": "large signal"}],
        [{"range": "low frequency"}, {"range": "high frequency"}],
        [{"assumptions": ["with body effect"]}, {"assumptions": ["without body effect"]}],
        [{"assumptions": ["考虑体效应"]}, {"assumptions": ["不考虑体效应"]}],
        [{"assumptions": ["long-channel device"]},
         {"assumptions": ["short channel device"]}],
        [{"assumptions": ["小信号模型"]}, {"assumptions": ["大信号模型"]}],
        [{"assumptions": ["ideal device"]}, {"assumptions": ["nonideal device"]}],
    ],
)
def test_explicit_scope_conflicts_return_none(scopes):
    assert merge_validity_scopes(scopes) is None


@pytest.mark.parametrize("assumption", ["nonideal device", "非理想器件"])
def test_single_nonideal_assumption_does_not_self_conflict(assumption):
    assert merge_validity_scopes([
        {"assumptions": [assumption]}, {"assumptions": ["perfect matching"]},
    ]) is not None


def test_scope_conflict_rejects_the_whole_chain():
    nodes, relations = _chain_fixture()
    nodes["A"]["payload"]["validity_scope"] = {"region": ["saturation"]}
    nodes["C"]["payload"]["validity_scope"] = {"region": ["weak inversion"]}
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_chain_trust_uses_weakest_hop_review_tier_and_hop_penalty():
    nodes, relations = _chain_fixture()
    relations[0].update({
        "review_status": "verified", "tier": "base",
        "evidence": [{"quote": "A to B", "confidence": 0.8}],
    })
    relations[1].update({
        "review_status": "pending", "tier": "personal",
        "evidence": [{"quote": "B to C", "confidence": 0.8}],
    })
    chain = compose_two_hop_paths(nodes, relations, "A")[0]
    assert chain.hops[0].trust == pytest.approx(0.8)
    assert chain.hops[1].trust == pytest.approx(0.8 * 0.9 * 0.85)
    assert chain.chain_trust == pytest.approx((0.8 * 0.9 * 0.85) * 0.9)
    assert chain.tier == "personal"


def test_missing_confidence_defaults_to_one():
    nodes, relations = _chain_fixture()
    for relation in relations:
        relation.update({"review_status": "verified", "tier": "base"})
    chain = compose_two_hop_paths(nodes, relations, "A")[0]
    assert chain.chain_trust == pytest.approx(0.9)
    assert chain.tier == "base"


def test_low_trust_chain_is_not_exposed_as_an_inference():
    nodes, relations = _chain_fixture()
    relations[1]["evidence"] = [{"quote": "weak B to C", "confidence": 0.1}]
    assert compose_two_hop_paths(nodes, relations, "A") == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), "bad"])
def test_malformed_or_nonfinite_confidence_fails_closed(bad):
    nodes, relations = _chain_fixture()
    relations[1]["evidence"] = [{"quote": "bad confidence", "confidence": bad}]
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_chain_trust_threshold_is_inclusive_at_point_five():
    nodes, relations = _chain_fixture()
    for relation in relations:
        relation.update({"review_status": "verified", "tier": "base"})
    relations[1]["evidence"] = [{"quote": "boundary", "confidence": 5 / 9}]
    chain = compose_two_hop_paths(nodes, relations, "A")[0]
    assert chain.chain_trust == pytest.approx(0.5)
    relations[1]["evidence"] = [{"quote": "below", "confidence": 0.55}]
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_trust_uses_the_same_primary_quote_that_is_rendered():
    nodes, relations = _chain_fixture()
    relations[1]["evidence"] = [
        {"quote": "low-confidence primary", "confidence": 0.1},
        {"quote": "hidden high-confidence alternative", "confidence": 1.0},
    ]
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_chain_anchor_relevance_uses_authorizing_candidate_not_global_hits():
    nodes, relations = _chain_fixture()
    chain = compose_two_hop_paths(nodes, relations, "A")[0]
    chain.query_relevance = 0.2
    relevances = chain_anchor_relevances([chain])
    assert relevances == {"r1": pytest.approx(0.2), "r2": pytest.approx(0.2)}

    # The helper has no global/top-hit argument: an unrelated 0.99 hit elsewhere
    # cannot elevate these relation anchors.  Trust still caps a relevant action.
    chain.query_relevance = 1.0
    relevances = chain_anchor_relevances([chain])
    assert relevances["r1"] == pytest.approx(chain.hops[0].trust)
    assert relevances["r2"] == pytest.approx(chain.hops[1].trust)

    chain.query_relevance = float("nan")
    assert chain_anchor_relevances([chain]) == {"r1": 0.0, "r2": 0.0}


def test_existing_direct_triple_is_not_repackaged_as_an_inference():
    nodes, relations = _chain_fixture()
    assert compose_two_hop_paths(
        nodes,
        relations,
        "A",
        direct_triples=frozenset({("A", "C", "derived_from")}),
    ) == []

    # Direct rows present in the bounded input are detected without a separate set.
    relations.append(_rel("direct", "A", "C", "derived_from", quote="A to C"))
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_rejected_direct_triple_does_not_hide_a_valid_inference():
    nodes, relations = _chain_fixture()
    relations.append(_rel(
        "direct", "A", "C", "derived_from", quote="bad direct",
        review_status="rejected",
    ))
    assert len(compose_two_hop_paths(nodes, relations, "A")) == 1


def test_cycle_or_repeated_node_is_not_inferred():
    nodes = {oid: _node(oid, "formula") for oid in ("A", "B")}
    relations = [
        _rel("r1", "A", "B", "derived_from", quote="A to B"),
        _rel("r2", "B", "A", "derived_from", quote="B to A"),
    ]
    assert compose_two_hop_paths(nodes, relations, "A") == []


def test_max_results_is_hard_capped_and_stronger_paths_rank_first():
    nodes = {"A": _node("A", "formula")}
    relations = []
    for index, confidence in enumerate((0.4, 0.9, 0.6), start=1):
        via, target = f"B{index}", f"C{index}"
        nodes[via] = _node(via, "formula")
        nodes[target] = _node(target, "formula")
        relations.extend([
            _rel(f"r{index}a", "A", via, "derived_from",
                 quote=f"A to {via}", confidence=confidence,
                 review_status="verified", tier="base"),
            _rel(f"r{index}b", via, target, "derived_from",
                 quote=f"{via} to {target}", confidence=confidence,
                 review_status="verified", tier="base"),
        ])
    found = compose_two_hop_paths(nodes, relations, "A", max_results=2)
    assert len(found) == 2
    assert [item.via_object_id for item in found] == ["B2", "B3"]


def test_parallel_relation_rows_collapse_to_the_strongest_semantic_path():
    nodes, relations = _chain_fixture()
    relations += [
        _rel("r1-strong", "A", "B", "derived_from", quote="strong AB",
             confidence=1.0, review_status="verified", tier="base"),
        _rel("r2-strong", "B", "C", "derived_from", quote="strong BC",
             confidence=1.0, review_status="verified", tier="base"),
    ]
    found = compose_two_hop_paths(nodes, relations, "A")
    assert len(found) == 1
    assert tuple(h.relation_id for h in found[0].hops) == ("r1-strong", "r2-strong")


def test_follow_chain_result_has_independent_default_lists():
    first = FollowChainResult()
    second = FollowChainResult()
    first.nodes.append({"id": "A"})
    assert second.nodes == []
    assert first.inferences == []


def test_renderer_starts_at_k2001_and_keeps_source_to_target_direction():
    nodes, relations = _chain_fixture()
    chain = compose_two_hop_paths(
        nodes, relations, "C", direction="in",
    )[0]
    # _chain_fixture's relations all default to notebook_id="nb" (_rel); this
    # ask's own active_notebook_id matches, i.e. an ordinary same-notebook
    # chain (the common case) — see the dedicated own/foreign pair below for
    # the notebook_id-in-id_map contract itself (Task 14 审查修复 #1).
    context, id_map = render_follow_chain_context([chain], active_notebook_id="nb")

    assert set(id_map) == {"k2001", "k2002"}
    assert "k2001: [relation][personal] A --derived_from--> B" in context
    assert "k2002: [relation][personal] B --derived_from--> C" in context
    assert "inference 1: A --derived_from--> C via B" in context
    assert "B --derived_from--> A" not in context
    assert "C --derived_from--> B" not in context
    assert "path 1: [k2001] + [k2002]" in context

    first = id_map["k2001"]
    assert first["object_id"] == "r1"
    assert first["object_type"] == "relation"
    assert first["snippet"] == "A to B"
    assert first["source_title"] == "Source One"
    assert first["location_label"] == "p.1"
    assert first["tier"] == "personal"
    assert first["source_object_id"] == "A"
    assert first["target_object_id"] == "B"


def test_renderer_deduplicates_shared_relation_anchors_and_honours_offset():
    nodes = {oid: _node(oid, "formula") for oid in ("A", "B", "C", "D")}
    relations = [
        _rel("shared", "A", "B", "derived_from", quote="A to B"),
        _rel("to-c", "B", "C", "derived_from", quote="B to C"),
        _rel("to-d", "B", "D", "derived_from", quote="B to D"),
    ]
    chains = compose_two_hop_paths(nodes, relations, "A", max_results=4)
    context, id_map = render_follow_chain_context(
        chains, id_offset=20, active_notebook_id="nb")
    assert set(id_map) == {"k21", "k22", "k23"}
    assert context.count("A --derived_from--> B — evidence") == 1


def test_renderer_empty_input_matches_other_context_renderers():
    assert render_follow_chain_context([], active_notebook_id="") == ("(none)", {})


def test_renderer_blanks_notebook_id_for_the_active_notebooks_own_hops():
    """Task 14 审查修复 #1: follow_chain 是引用徽章库名贯通漏掉的第五个锚点
    生产点。同库(active_notebook_id 本身)的跳步不能在 id_map 里带自己的
    notebook_id——否则前端徽章会显示一个多余的"来自「自己」"标签，与
    evidence_context.py 的 chunk_context/knowledge_context 用 raw_origin
    (而非回退过的 origin)的既有惯例不一致。"""
    nodes, relations = _chain_fixture()  # 两条 relation 都 notebook_id="nb"(_rel 默认)
    chain = compose_two_hop_paths(nodes, relations, "A")[0]
    _context, id_map = render_follow_chain_context([chain], active_notebook_id="nb")
    assert id_map["k2001"]["notebook_id"] == ""
    assert id_map["k2002"]["notebook_id"] == ""


def test_renderer_keeps_a_mounted_bases_real_notebook_id_for_cross_notebook_hops():
    """跨库(挂载的参考库)跳步的 notebook_id 原样带出，供前端引用徽章解出真实
    库名（而不是退化成泛化的"来自公共知识库"文案）。"""
    nodes = {oid: _node(oid, "formula") for oid in ("A", "B", "C")}
    relations = [
        _rel("r1", "A", "B", "derived_from", quote="A to B",
             tier="base", notebook_id="base-nb"),
        _rel("r2", "B", "C", "derived_from", quote="B to C",
             tier="base", notebook_id="base-nb"),
    ]
    chain = compose_two_hop_paths(nodes, relations, "A")[0]
    _context, id_map = render_follow_chain_context(
        [chain], active_notebook_id="active-nb")
    assert id_map["k2001"]["notebook_id"] == "base-nb"
    assert id_map["k2002"]["notebook_id"] == "base-nb"
