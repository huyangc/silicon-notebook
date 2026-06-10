"""Edge trust signals: evidence anchoring, cross-doc corroboration, type-constraint validity.

compute_trust_score is a pure function — no I/O, easily unit-tested.
It produces a SEPARATE computed signal (not the [0,1]/tau relevance score).
Do NOT use trust_score in classify_evidence / _fuse / W_KEYWORD / W_SEMANTIC paths.

Formula (weights documented in plan 2026-06-10-trackE-edge-trust-curation.md):
    trust_score = 0.4 * evidence_anchor + 0.3 * corroboration + 0.3 * type_validity
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Dict, List

# ── Typed edge constraints (mirrored from extract.py:32-35) ──────────────────
# Maps edge_type → frozenset of valid (src_type, tgt_type) pairs.
# None in the frozenset means "unconstrained" (any type allowed).
# Extract.py prompt text: defines(Claim->Concept), about(Claim|Formula->Concept),
# supports(Claim|Formula->Claim), derived_from(Formula->Formula),
# used_in(Formula->Procedure), part_of/composed_of/contrasts_with/kind_of(Concept->Concept),
# depends_on/prerequisite_of/precedes: unconstrained.
TYPE_CONSTRAINTS: Dict[str, frozenset] = {
    "defines":       frozenset({("Claim", "Concept")}),
    "about":         frozenset({("Claim", "Concept"), ("Formula", "Concept")}),
    "supports":      frozenset({("Claim", "Claim"), ("Formula", "Claim")}),
    "derived_from":  frozenset({("Formula", "Formula")}),
    "used_in":       frozenset({("Formula", "Procedure")}),
    "part_of":       frozenset({("Concept", "Concept")}),
    "composed_of":   frozenset({("Concept", "Concept")}),
    "contrasts_with":frozenset({("Concept", "Concept")}),
    "kind_of":       frozenset({("Concept", "Concept")}),
    # Unconstrained: depends_on, prerequisite_of, precedes
}

VALID_EDGE_TYPES = frozenset({
    "defines", "part_of", "composed_of", "contrasts_with", "kind_of",
    "about", "supports", "derived_from", "depends_on", "prerequisite_of",
    "used_in", "precedes",
})

CORR_CAP = 3  # 3 independent sources → full corroboration score (= 1.0)

# Weight constants
W_EVIDENCE  = 0.4
W_CORR      = 0.3
W_TYPE      = 0.3


def _decode_evidence(rel: dict) -> list:
    ev = rel.get("evidence", [])
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = []
    return ev if isinstance(ev, list) else []


def evidence_anchor_score(rel: dict) -> float:
    """1.0 if the edge has at least one evidence entry with a non-empty quote; 0.0 otherwise."""
    for entry in _decode_evidence(rel):
        if isinstance(entry, dict) and entry.get("quote", "").strip():
            return 1.0
    return 0.0


def type_validity_score(rel: dict, node_types: Dict[str, str]) -> float:
    """1.0 if the edge_type is known AND the (src, tgt) node-type pair respects the constraint.

    node_types: {object_id: object_type} — caller provides the type of each endpoint.
    Unconstrained edge types (depends_on, prerequisite_of, precedes) always return 1.0
    provided the edge_type is in VALID_EDGE_TYPES.
    Returns 0.0 for unknown edge types, missing node types, or type-constraint violations.
    """
    edge_type = rel.get("edge_type", "")
    if edge_type not in VALID_EDGE_TYPES:
        return 0.0

    src_oid = rel.get("source_object_id", "")
    tgt_oid = rel.get("target_object_id", "")
    src_type = node_types.get(src_oid)
    tgt_type = node_types.get(tgt_oid)
    if not src_type or not tgt_type:
        return 0.0

    constraints = TYPE_CONSTRAINTS.get(edge_type)
    if constraints is None:
        # Unconstrained edge type
        return 1.0
    return 1.0 if (src_type, tgt_type) in constraints else 0.0


def corroboration_score_from_count(count: int) -> float:
    """Normalise a raw distinct-source count to [0, 1] with cap CORR_CAP."""
    if count <= 0:
        return 0.0
    return min(count / CORR_CAP, 1.0)


def _norm(name: str) -> str:
    """Lightweight name normaliser for triple-identity matching (case + punctuation)."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def corroboration_counts(
    relations: List[dict],
    node_names: Dict[str, str] | None = None,
) -> Dict[str, int]:
    """Return {rel_id: distinct_source_count} for each relation.

    Triple identity = (norm(src_name), edge_type, norm(tgt_name)).
    Corroboration counts DISTINCT source_ids asserting the same triple.
    If a rel's source_id is None/empty it is treated as its own unique source.

    node_names: {object_id: name} — when provided, triple keys use name-normalised
    values (more robust cross-doc matching).  When absent, raw object IDs are used.
    """
    if node_names is None:
        node_names = {}
    # Two-pass: first collect (triple_key → set of source_ids); then map rel_id → count.
    triple_sources: Dict[str, set] = defaultdict(set)
    rel_triple: Dict[str, str] = {}

    for rel in relations:
        rid = rel.get("id", "")
        src_oid = rel.get("source_object_id", "")
        tgt_oid = rel.get("target_object_id", "")
        src_name = _norm(node_names.get(src_oid) or rel.get("_src_name") or src_oid)
        tgt_name = _norm(node_names.get(tgt_oid) or rel.get("_tgt_name") or tgt_oid)
        edge_type = rel.get("edge_type", "")
        triple_key = f"{src_name}|{edge_type}|{tgt_name}"
        source_id = rel.get("source_id") or f"__unique_{rid}"
        triple_sources[triple_key].add(source_id)
        rel_triple[rid] = triple_key

    return {rid: len(triple_sources[triple_key]) for rid, triple_key in rel_triple.items()}


def compute_trust_score(
    rel: dict,
    node_types: Dict[str, str],
    corroboration_score: float,
) -> float:
    """Combine the three signals into a single edge trust score in [0, 1].

    corroboration_score is pre-computed by the caller (use corroboration_counts +
    corroboration_score_from_count so the caller can batch-compute it once over all edges).
    """
    ev_score = evidence_anchor_score(rel)
    tv_score = type_validity_score(rel, node_types)
    score = W_EVIDENCE * ev_score + W_CORR * corroboration_score + W_TYPE * tv_score
    return max(0.0, min(1.0, score))
