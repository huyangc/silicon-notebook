# backend/tests/test_edge_trust.py
"""Unit tests for edge trust signals. All pure-function, no DB/IO."""
import pytest


# ── fixtures ─────────────────────────────────────────────────────────────────

def _rel(edge_type, src_type, tgt_type, evidence=None):
    """Build a minimal relation dict as used by build_rx_graph / relations_for_notebook."""
    return {
        "id": "r1",
        "edge_type": edge_type,
        "evidence": evidence if evidence is not None else [],
        "source_object_id": "src",
        "target_object_id": "tgt",
        "_src_type": src_type,  # extra key used by tests only
        "_tgt_type": tgt_type,
    }


def _ev(quote="some quote"):
    return [{"file": "f1", "char_start": 0, "char_end": 10,
             "line_start": 1, "line_end": 1, "quote": quote}]


# ── evidence anchoring ────────────────────────────────────────────────────────

def test_evidence_anchor_present():
    from app.services.kg.edge_trust import evidence_anchor_score
    assert evidence_anchor_score(_rel("supports", "Claim", "Claim", _ev())) == 1.0


def test_evidence_anchor_empty_list():
    from app.services.kg.edge_trust import evidence_anchor_score
    assert evidence_anchor_score(_rel("supports", "Claim", "Claim", [])) == 0.0


def test_evidence_anchor_empty_quote():
    from app.services.kg.edge_trust import evidence_anchor_score
    ev = [{"file": "f1", "char_start": 0, "char_end": 0, "line_start": 1, "line_end": 1, "quote": ""}]
    assert evidence_anchor_score(_rel("supports", "Claim", "Claim", ev)) == 0.0


def test_evidence_anchor_string_blob_decoded():
    """evidence stored as JSON string (as loaded from DB) is decoded."""
    import json
    from app.services.kg.edge_trust import evidence_anchor_score
    rel = _rel("supports", "Claim", "Claim")
    rel["evidence"] = json.dumps(_ev())
    assert evidence_anchor_score(rel) == 1.0


# ── type-constraint validity ───────────────────────────────────────────────────

def test_type_validity_defines_claim_concept():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Claim", "tgt": "Concept"}
    assert type_validity_score(_rel("defines", "Claim", "Concept"), node_types) == 1.0


def test_type_validity_defines_wrong_src():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Concept", "tgt": "Concept"}
    assert type_validity_score(_rel("defines", "Concept", "Concept"), node_types) == 0.0


def test_type_validity_used_in_formula_procedure():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Formula", "tgt": "Procedure"}
    assert type_validity_score(_rel("used_in", "Formula", "Procedure"), node_types) == 1.0


def test_type_validity_used_in_wrong():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Claim", "tgt": "Procedure"}
    assert type_validity_score(_rel("used_in", "Claim", "Procedure"), node_types) == 0.0


def test_type_validity_reasoning_edges_follow_exact_matrix():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Concept", "tgt": "Claim"}
    assert type_validity_score(_rel("depends_on", "Concept", "Claim"), node_types) == 1.0
    assert type_validity_score(_rel("precedes", "Formula", "Claim"), node_types) == 0.0


@pytest.mark.parametrize(
    ("edge_type", "source_type", "target_type"),
    [
        ("supports", "Concept", "Claim"),
        ("derived_from", "Claim", "Formula"),
        ("contrasts_with", "Claim", "Formula"),
        ("used_in", "Concept", "Procedure"),
        ("precedes", "Procedure", "Procedure"),
    ],
)
def test_prompt_legal_pairs_are_trust_legal(edge_type, source_type, target_type):
    from app.services.kg.edge_trust import type_validity_score

    node_types = {"src": source_type, "tgt": target_type}
    assert type_validity_score(
        _rel(edge_type, source_type, target_type), node_types
    ) == 1.0


def test_type_validity_unknown_edge_type_fails():
    """An edge_type not in EDGE_TYPES is invalid."""
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Claim", "tgt": "Concept"}
    assert type_validity_score(_rel("made_up", "Claim", "Concept"), node_types) == 0.0


def test_type_validity_missing_node_type_fails():
    """If a node's type is not in node_types dict, conservative → 0.0."""
    from app.services.kg.edge_trust import type_validity_score
    assert type_validity_score(_rel("defines", "Claim", "Concept"), {}) == 0.0


# ── combined trust score ───────────────────────────────────────────────────────

def test_trust_score_full():
    from app.services.kg.edge_trust import compute_trust_score
    rel = _rel("defines", "Claim", "Concept", _ev())
    node_types = {"src": "Claim", "tgt": "Concept"}
    # ev=1, corr=1 (corr_score injected as 1.0), type=1 → 0.4+0.3+0.3 = 1.0
    score = compute_trust_score(rel, node_types, corroboration_score=1.0)
    assert abs(score - 1.0) < 1e-9


def test_trust_score_no_evidence_no_corroboration():
    from app.services.kg.edge_trust import compute_trust_score
    rel = _rel("defines", "Claim", "Concept", [])
    node_types = {"src": "Claim", "tgt": "Concept"}
    # ev=0, corr=0, type=1 → 0 + 0 + 0.3 = 0.3
    score = compute_trust_score(rel, node_types, corroboration_score=0.0)
    assert abs(score - 0.3) < 1e-9


def test_trust_score_bounds():
    from app.services.kg.edge_trust import compute_trust_score
    rel = _rel("supports", "Claim", "Claim", _ev())
    node_types = {"src": "Claim", "tgt": "Claim"}
    s = compute_trust_score(rel, node_types, corroboration_score=0.5)
    assert 0.0 <= s <= 1.0


# ── cross-doc corroboration grouping ─────────────────────────────────────────

def test_corroboration_count_distinct_sources():
    """Two edges with same (norm_src_name, edge_type, norm_tgt_name) but different
    source_id → corroboration_count = 2."""
    from app.services.kg.edge_trust import corroboration_counts
    rels = [
        {"id": "r1", "source_object_id": "A", "target_object_id": "B",
         "edge_type": "supports", "source_id": "src1",
         "_src_name": "Claim Alpha", "_tgt_name": "Concept Beta"},
        {"id": "r2", "source_object_id": "A2", "target_object_id": "B2",
         "edge_type": "supports", "source_id": "src2",
         "_src_name": "Claim Alpha", "_tgt_name": "Concept Beta"},
    ]
    counts = corroboration_counts(rels)
    assert counts["r1"] == 2
    assert counts["r2"] == 2


def test_corroboration_same_source_not_counted_twice():
    """Same source_id asserting same triple → still counts as 1."""
    from app.services.kg.edge_trust import corroboration_counts
    rels = [
        {"id": "r1", "source_object_id": "A", "target_object_id": "B",
         "edge_type": "supports", "source_id": "src1",
         "_src_name": "Alpha", "_tgt_name": "Beta"},
        {"id": "r2", "source_object_id": "A2", "target_object_id": "B2",
         "edge_type": "supports", "source_id": "src1",
         "_src_name": "Alpha", "_tgt_name": "Beta"},
    ]
    counts = corroboration_counts(rels)
    assert counts["r1"] == 1
    assert counts["r2"] == 1


def test_corroboration_cap_at_corr_cap():
    from app.services.kg.edge_trust import corroboration_score_from_count, CORR_CAP
    assert corroboration_score_from_count(CORR_CAP) == 1.0
    assert corroboration_score_from_count(CORR_CAP + 5) == 1.0
    assert corroboration_score_from_count(0) == 0.0
    assert abs(corroboration_score_from_count(1) - 1.0 / CORR_CAP) < 1e-9
