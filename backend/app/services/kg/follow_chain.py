"""Pure, query-time two-hop relation composition for ``follow_chain``.

The repository is responsible for fetching a *bounded* set of hydrated nodes
and relations.  This module deliberately knows nothing about SQLite or
rustworkx: it validates and composes those rows, computes a separate chain
trust signal, and renders the two original relations as citable answer
context.  Inferred relations are transient and are never persisted here.

All paths are normalised to the database's stored ``source -> target``
direction.  ``direction='in'`` changes which end must match
``start_object_id``; it never reverses either hop in the result or renderer.
"""
from __future__ import annotations

import json
import math
from typing import Iterable, Mapping, Optional, Sequence

from app.domain.graph import (
    ChainHop,
    FollowChainResult,
    InferredChain,
    evidence_quote as _evidence_quote,
)
from app.services.kg.edge_schema import (
    EDGE_SPECS,
    TRANSITIVE_EDGE_TYPES as REGISTERED_TRANSITIVE_EDGE_TYPES,
    is_valid_edge_pair,
)

# Only relations with a well-understood transitive interpretation are enabled
# in v1.  The tuple shape leaves room for safe mixed-type composition rules in
# a future version without changing the composition engine.
TRANSITIVE_COMPOSITIONS = {
    ("derived_from", "derived_from"): "derived_from",
    ("kind_of", "kind_of"): "kind_of",
    ("prerequisite_of", "prerequisite_of"): "prerequisite_of",
    ("precedes", "precedes"): "precedes",
    ("part_of", "part_of"): "part_of",
}
TRANSITIVE_EDGE_TYPES = frozenset(result for result in TRANSITIVE_COMPOSITIONS.values())
if TRANSITIVE_EDGE_TYPES != REGISTERED_TRANSITIVE_EDGE_TYPES:
    raise AssertionError("follow_chain compositions drifted from edge_schema")

# Compatibility view used by callers/tests. Pair legality is authoritative and
# checked per hop below; this set alone must never validate a mixed-type edge.
EDGE_NODE_TYPES = {
    edge_type: frozenset(
        node_type
        for pair in EDGE_SPECS[edge_type].allowed_pairs
        for node_type in pair
    )
    for edge_type in TRANSITIVE_EDGE_TYPES
}

USABLE_NODE_STATUSES = frozenset({
    "approved", "reviewed", "project_specific", "conflict",
})

_REVIEW_FACTOR = {"verified": 1.0, "pending": 0.9}
_TIER_FACTOR = {"base": 1.0, "personal": 0.85}
_CHAIN_HOP_PENALTY = 0.9
_MIN_CHAIN_TRUST = 0.5


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _node_type(node: Mapping[str, object]) -> str:
    return _normalise_text(node.get("object_type") or node.get("type"))


def _node_name(node: Mapping[str, object], fallback: str) -> str:
    payload = node.get("payload")
    if isinstance(payload, Mapping):
        name = payload.get("name")
    else:
        name = None
    return str(name or node.get("name") or fallback).strip()


def _node_scope(node: Mapping[str, object]) -> dict:
    payload = node.get("payload")
    scope = payload.get("validity_scope") if isinstance(payload, Mapping) else None
    if not isinstance(scope, Mapping):
        scope = node.get("validity_scope")
    return dict(scope) if isinstance(scope, Mapping) else {}


def _node_is_usable(node: Mapping[str, object]) -> bool:
    # Hydrated rows normally carry status.  Keeping absent status usable makes
    # the pure function convenient for extraction fixtures while still failing
    # closed for an explicit deprecated/unknown state.
    status = _normalise_text(node.get("status") or "approved")
    return status in USABLE_NODE_STATUSES


def _decode_evidence(raw: object) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _evidence_confidence(evidence: Sequence[Mapping[str, object]]) -> float:
    # Keep scoring and citation aligned: ChainHop.primary_evidence renders the
    # first quoted item, so trust must be based on that same item rather than a
    # later, hidden max-confidence quote.
    for item in evidence:
        if not _evidence_quote(item):
            continue
        if "confidence" not in item:
            return 1.0
        raw = item.get("confidence")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(value):
            return 0.0
        return max(0.0, min(1.0, value))
    # Called only after the fail-closed quote check.  The defensive zero keeps
    # this helper safe if used independently.
    return 0.0


def _hop_trust(relation: Mapping[str, object], evidence: Sequence[Mapping[str, object]]) -> float:
    review = _normalise_text(relation.get("review_status") or "pending")
    tier = _normalise_text(relation.get("tier") or "personal")
    review_factor = _REVIEW_FACTOR.get(review, _REVIEW_FACTOR["pending"])
    tier_factor = _TIER_FACTOR.get(tier, _TIER_FACTOR["personal"])
    return _evidence_confidence(evidence) * review_factor * tier_factor


def _scope_values(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalised = _normalise_text(value)
        if normalised and normalised not in seen:
            seen.add(normalised)
            out.append(normalised)
    return out


# Prefixes are deliberately narrow: only explicit positive/negative forms are
# treated as contradictory.  Different ordinary assumptions are cumulative.
_NEGATIVE_ASSUMPTION_PREFIXES = (
    "not considering ", "not including ", "not using ", "without ",
    "neglecting ", "ignoring ", "no ", "not ",
    "不考虑", "不包含", "不使用", "忽略", "不计", "无",
)
_POSITIVE_ASSUMPTION_PREFIXES = (
    "considering ", "including ", "using ", "with ",
    "consider ", "include ", "use ",
    "考虑", "包含", "使用", "计入", "有",
)

# Narrow, domain-relevant qualifier families that are mutually exclusive even
# without an explicit "not/without" prefix.  This is intentionally not a broad
# antonym engine: unknown assumptions are still accumulated, while common IC
# operating-model contradictions fail closed instead of silently producing an
# over-generalised inference.
_EXCLUSIVE_ASSUMPTION_FAMILIES = (
    (("long channel", "long-channel", "长沟道"),
     ("short channel", "short-channel", "短沟道")),
    (("small signal", "small-signal", "小信号"),
     ("large signal", "large-signal", "大信号")),
    (("strong inversion", "强反型"), ("weak inversion", "弱反型")),
    (("continuous time", "continuous-time", "连续时间"),
     ("discrete time", "discrete-time", "离散时间")),
    (("ideal device", "ideal devices", "理想器件"),
     ("nonideal device", "non-ideal device", "非理想器件")),
)


def _exclusive_assumption_tags(value: str) -> set[tuple[int, int]]:
    normalised = _normalise_text(value).replace("_", " ")
    tags: set[tuple[int, int]] = set()
    for family_index, alternatives in enumerate(_EXCLUSIVE_ASSUMPTION_FAMILIES):
        matches: list[tuple[int, int]] = []
        for alternative_index, phrases in enumerate(alternatives):
            for phrase in phrases:
                normalised_phrase = _normalise_text(phrase)
                if normalised_phrase in normalised:
                    matches.append((len(normalised_phrase), alternative_index))
        if matches:
            # Prefer the most specific phrase: "nonideal device" contains
            # "ideal device", and 非理想器件 contains 理想器件.  Longest-match
            # prevents one assumption from contradicting itself.
            longest = max(length for length, _ in matches)
            tags.update((family_index, alternative_index)
                        for length, alternative_index in matches if length == longest)
    return tags


def _assumption_signature(value: str) -> tuple[str, bool]:
    normalised = _normalise_text(value)
    for prefix in _NEGATIVE_ASSUMPTION_PREFIXES:
        if normalised.startswith(prefix) and normalised[len(prefix):].strip():
            return normalised[len(prefix):].strip(), False
    for prefix in _POSITIVE_ASSUMPTION_PREFIXES:
        if normalised.startswith(prefix) and normalised[len(prefix):].strip():
            return normalised[len(prefix):].strip(), True
    return normalised, True


def merge_validity_scopes(scopes: Iterable[Mapping[str, object]]) -> Optional[dict]:
    """Merge node validity scopes, returning ``None`` on a definite conflict.

    Rules are intentionally conservative and deterministic:

    * non-empty ``region`` sets are intersected;
    * distinct non-empty ``approximation`` or ``range`` values conflict;
    * assumptions are a stable union, except explicit positive/negative forms
      for the same normalised subject conflict;
    * an empty scope adds no constraint and never widens a stated one.
    """
    materialised = [dict(scope) for scope in scopes if isinstance(scope, Mapping)]
    merged: dict = {}

    region_groups = [_scope_values(scope.get("region")) for scope in materialised]
    region_groups = [group for group in region_groups if group]
    if region_groups:
        allowed = set(region_groups[0])
        for group in region_groups[1:]:
            allowed.intersection_update(group)
        if not allowed:
            return None
        # Preserve first-scope ordering while emitting normalised values.
        merged["region"] = [value for value in region_groups[0] if value in allowed]

    assumptions: list[str] = []
    seen_assumptions: set[str] = set()
    polarities: dict[str, set[bool]] = {}
    exclusive_states: dict[int, set[int]] = {}
    for scope in materialised:
        for assumption in _scope_values(scope.get("assumptions")):
            base, polarity = _assumption_signature(assumption)
            if not base:
                continue
            values = polarities.setdefault(base, set())
            values.add(polarity)
            if len(values) > 1:
                return None
            for family_index, alternative_index in _exclusive_assumption_tags(assumption):
                states = exclusive_states.setdefault(family_index, set())
                states.add(alternative_index)
                if len(states) > 1:
                    return None
            if assumption not in seen_assumptions:
                seen_assumptions.add(assumption)
                assumptions.append(assumption)
    if assumptions:
        merged["assumptions"] = assumptions

    for field_name in ("approximation", "range"):
        values: list[str] = []
        for scope in materialised:
            value = _normalise_text(scope.get(field_name))
            if value and value not in values:
                values.append(value)
        if len(values) > 1:
            return None
        if values:
            merged[field_name] = values[0]

    return merged


def _relation_triple(relation: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(relation.get("source_object_id") or ""),
        str(relation.get("target_object_id") or ""),
        _normalise_text(relation.get("edge_type")),
    )


def _is_direct(
    triples: set[tuple], source_id: str, target_id: str, edge_type: str,
) -> bool:
    # Canonical shape is (source, target, edge_type), matching repository CRUD.
    # Accept (source, edge_type, target) as a harmless compatibility courtesy.
    return ((source_id, target_id, edge_type) in triples
            or (source_id, edge_type, target_id) in triples)


def _make_hop(
    relation: Mapping[str, object],
    source_node: Mapping[str, object],
    target_node: Mapping[str, object],
) -> Optional[ChainHop]:
    review_status = _normalise_text(relation.get("review_status") or "pending")
    if review_status not in _REVIEW_FACTOR:
        return None
    evidence = _decode_evidence(relation.get("evidence", []))
    if not any(_evidence_quote(item) for item in evidence):
        return None
    source_id = str(relation.get("source_object_id") or "")
    target_id = str(relation.get("target_object_id") or "")
    tier = _normalise_text(relation.get("tier") or "personal") or "personal"
    return ChainHop(
        relation_id=str(relation.get("id") or relation.get("relation_id") or ""),
        notebook_id=str(relation.get("notebook_id") or ""),
        tier=tier,
        source_object_id=source_id,
        target_object_id=target_id,
        edge_type=_normalise_text(relation.get("edge_type")),
        source_name=_node_name(source_node, source_id),
        target_name=_node_name(target_node, target_id),
        evidence=evidence,
        review_status=review_status,
        source_title=str(relation.get("source_title") or "").strip(),
        trust=_hop_trust(relation, evidence),
    )


def compose_two_hop_paths(
    nodes: dict[str, dict],
    relations: list[dict],
    start_object_id: str,
    direction: str = "out",
    edge_type: Optional[str] = None,
    target_object_id: str = "",
    direct_triples: frozenset = frozenset(),
    max_results: int = 4,
) -> list[InferredChain]:
    """Validate and compose bounded relation rows into transient two-hop paths.

    ``relations`` may contain either the two frontier layers only or a slightly
    wider bounded adjacency set.  Existing direct relations are detected both
    from that list and from ``direct_triples``.  The canonical direct-triple
    shape is ``(source_object_id, target_object_id, edge_type)``.

    For ``out``, the normalised source endpoint must equal ``start_object_id``.
    For ``in``, the normalised target endpoint must equal it.  ``both`` accepts
    either endpoint.  ``target_object_id``, when supplied, means the *other*
    endpoint in every direction.
    """
    direction = _normalise_text(direction or "out")
    if direction not in {"out", "in", "both"}:
        raise ValueError("direction must be 'out', 'in', or 'both'")
    requested_type = _normalise_text(edge_type) if edge_type else ""
    if requested_type and requested_type not in TRANSITIVE_EDGE_TYPES:
        return []
    if not start_object_id or max_results <= 0:
        return []

    usable_relations = [
        relation for relation in relations
        if isinstance(relation, Mapping)
        and _normalise_text(relation.get("review_status") or "pending") in _REVIEW_FACTOR
    ]
    all_direct: set[tuple] = set(direct_triples or ())
    all_direct.update(_relation_triple(relation) for relation in usable_relations)

    candidates: dict[tuple[str, str, str, str], InferredChain] = {}
    for first in usable_relations:
        first_type = _normalise_text(first.get("edge_type"))
        if requested_type and first_type != requested_type:
            continue
        if first_type not in TRANSITIVE_EDGE_TYPES:
            continue
        first_source, middle, _ = _relation_triple(first)
        if not first_source or not middle:
            continue

        for second in usable_relations:
            second_source, final_target, second_type = _relation_triple(second)
            if second_source != middle:
                continue
            inferred_type = TRANSITIVE_COMPOSITIONS.get((first_type, second_type))
            if inferred_type is None:
                continue

            # Paths must stay inside one notebook when both rows are tagged.
            first_nb = str(first.get("notebook_id") or "")
            second_nb = str(second.get("notebook_id") or "")
            if first_nb and second_nb and first_nb != second_nb:
                continue

            # A, B and C must be distinct.  This also rejects self-loop hops.
            if len({first_source, middle, final_target}) != 3:
                continue
            out_match = first_source == start_object_id
            in_match = final_target == start_object_id
            if direction == "out" and not out_match:
                continue
            if direction == "in" and not in_match:
                continue
            if direction == "both" and not (out_match or in_match):
                continue
            other_endpoint = final_target if out_match else first_source
            if target_object_id and other_endpoint != target_object_id:
                continue
            if _is_direct(all_direct, first_source, final_target, inferred_type):
                continue

            source_node = nodes.get(first_source)
            via_node = nodes.get(middle)
            target_node = nodes.get(final_target)
            if not all(isinstance(node, Mapping)
                       for node in (source_node, via_node, target_node)):
                continue
            assert source_node is not None and via_node is not None and target_node is not None
            if not all(_node_is_usable(node)
                       for node in (source_node, via_node, target_node)):
                continue

            source_type = _node_type(source_node)
            via_type = _node_type(via_node)
            target_type = _node_type(target_node)
            if not is_valid_edge_pair(first_type, source_type, via_type):
                continue
            if not is_valid_edge_pair(second_type, via_type, target_type):
                continue
            if not is_valid_edge_pair(inferred_type, source_type, target_type):
                continue

            first_hop = _make_hop(first, source_node, via_node)
            second_hop = _make_hop(second, via_node, target_node)
            if first_hop is None or second_hop is None:
                continue

            merged_scope = merge_validity_scopes(
                [_node_scope(source_node), _node_scope(via_node), _node_scope(target_node)]
            )
            if merged_scope is None:
                continue

            chain_trust = min(first_hop.trust, second_hop.trust) * _CHAIN_HOP_PENALTY
            if chain_trust < _MIN_CHAIN_TRUST:
                continue
            tiers = {first_hop.tier, second_hop.tier}
            chain_tier = "base" if tiers == {"base"} else "personal"
            search_direction = "out" if out_match else "in"
            inference = InferredChain(
                source_object_id=first_source,
                via_object_id=middle,
                target_object_id=final_target,
                source_name=first_hop.source_name,
                via_name=first_hop.target_name,
                target_name=second_hop.target_name,
                inferred_edge_type=inferred_type,
                hops=(first_hop, second_hop),
                validity_scope=merged_scope,
                chain_trust=chain_trust,
                notebook_id=first_nb or second_nb,
                tier=chain_tier,
                search_direction=search_direction,
            )

            # Parallel rows for the same semantic A->B->C path are collapsed;
            # retain the strongest evidence path, with relation ids as a stable
            # tie-break independent of SQL row order.
            key = (first_source, middle, final_target, inferred_type)
            existing = candidates.get(key)
            if existing is None or (
                inference.chain_trust,
                tuple(h.relation_id for h in inference.hops),
            ) > (
                existing.chain_trust,
                tuple(h.relation_id for h in existing.hops),
            ):
                candidates[key] = inference

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            -item.chain_trust,
            item.source_object_id,
            item.via_object_id,
            item.target_object_id,
            item.inferred_edge_type,
            tuple(h.relation_id for h in item.hops),
        ),
    )
    return ordered[:max_results]


def chain_anchor_relevances(inferences: Sequence[InferredChain]) -> dict[str, float]:
    """Return per-relation relevance without conflating trust and retrieval.

    Each relation anchor is bounded by both the relevance of the candidate that
    authorized follow_chain and that hop's evidence trust.  Shared relations keep
    the strongest independently-authorized value.  Unset query relevance is zero,
    so an unrelated high-scoring hit elsewhere in the answer cannot ground a chain.
    """
    out: dict[str, float] = {}
    for inference in inferences:
        try:
            query_relevance = float(inference.query_relevance or 0.0)
        except (TypeError, ValueError):
            query_relevance = 0.0
        if not math.isfinite(query_relevance):
            query_relevance = 0.0
        query_relevance = max(0.0, min(1.0, query_relevance))
        for hop in inference.hops:
            try:
                hop_trust = float(hop.trust)
            except (TypeError, ValueError):
                hop_trust = 0.0
            if not math.isfinite(hop_trust):
                hop_trust = 0.0
            relevance = min(query_relevance, max(0.0, min(1.0, hop_trust)))
            out[hop.relation_id] = max(out.get(hop.relation_id, 0.0), relevance)
    return out


def render_follow_chain_context(
    inferences: Sequence[InferredChain],
    id_offset: int = 2000,
    *,
    active_notebook_id: str,
) -> tuple[str, dict]:
    """Render direct-hop relation anchors plus explicitly transient inference lines.

    ``id_offset`` follows the repository renderers' convention: the first key
    is ``k{id_offset + 1}``, hence the default relation-anchor namespace begins
    at ``k2001``.  Both out- and in-discovered chains are always rendered in
    stored ``source -> target`` order.

    ``active_notebook_id`` is the notebook this ask/report is running against.
    Unlike ``RetrievedChunk``/``RetrievedKnowledge`` (which default
    ``notebook_id`` to ``""`` and only a federated/cross-tier hit ever sets
    it), every ``ChainHop`` here carries its *real* owning notebook id — every
    hop in a chain composed by ``compose_two_hop_paths`` lives in one single
    notebook (own or one mounted base; see the "must stay inside one notebook"
    guard there), and that id is read straight off the stored relation row, so
    it is never blank on its own.  Required (no default) so a caller can't
    silently forget it and leak the active notebook's own id into every
    same-notebook hop — echoing it verbatim would show a redundant "来自「本
    笔记本自己」" badge on ordinary, non-federated follow-chain evidence (the
    same failure mode ``evidence_context.py``'s ``chunk_context``/
    ``knowledge_context`` avoid via their ``raw_origin``/``origin`` split).
    """
    if not inferences:
        return "(none)", {}

    lines: list[str] = []
    id_map: dict[str, dict] = {}
    hop_keys: dict[tuple, str] = {}

    def hop_identity(hop: ChainHop) -> tuple:
        # Relation ids are globally unique in normal data.  The endpoint/type
        # suffix keeps malformed id-less fixtures deterministic and distinct.
        return (
            hop.relation_id,
            hop.source_object_id,
            hop.target_object_id,
            hop.edge_type,
        )

    for inference in inferences:
        for hop in inference.hops:
            identity = hop_identity(hop)
            if identity in hop_keys:
                continue
            key = f"k{id_offset + len(hop_keys) + 1}"
            hop_keys[identity] = key
            quote = hop.quote
            relation_name = (
                f"{hop.source_name} --{hop.edge_type}--> {hop.target_name}"
            )
            lines.append(
                f"{key}: [relation][{hop.tier}] {relation_name} "
                f"— evidence: {json.dumps(quote, ensure_ascii=False)}"
            )
            primary = hop.primary_evidence
            id_map[key] = {
                "object_id": hop.relation_id,
                "object_type": "relation",
                "name": relation_name,
                "definition": "",
                "snippet": quote,
                "source_title": hop.source_title
                or str(primary.get("source_title") or "").strip(),
                "location_label": hop.location_label,
                "tier": hop.tier,
                # Own-notebook hop -> "" (matches the raw_origin convention);
                # a genuinely foreign (mounted-base) hop passes its real id
                # through, letting the frontend badge resolve a library name.
                "notebook_id": hop.notebook_id if hop.notebook_id != active_notebook_id else "",
                # Extra metadata is retained for relation-aware clients.  The
                # existing AnswerAnchor parser simply ignores unknown fields.
                "source_id": str(primary.get("source_id") or ""),
                "element_id": str(primary.get("element_id") or ""),
                "edge_type": hop.edge_type,
                "source_object_id": hop.source_object_id,
                "target_object_id": hop.target_object_id,
            }

    lines.append("")
    lines.append("[Query-time typed inference; NOT directly stated]")
    lines.append(
        "Citation rule: cite each direct hop statement with its own key; mark the "
        "inferred conclusion as （推断）/Likely and attach NO [k] marker to that conclusion."
    )
    for index, inference in enumerate(inferences, start=1):
        first_key = hop_keys[hop_identity(inference.hops[0])]
        second_key = hop_keys[hop_identity(inference.hops[1])]
        lines.append(f"path {index}: [{first_key}] + [{second_key}]")
        lines.append(
            f"inference {index}: {inference.source_name} "
            f"--{inference.inferred_edge_type}--> {inference.target_name} "
            f"via {inference.via_name}; chain_trust={inference.chain_trust:.2f}"
        )
        if inference.validity_scope:
            lines.append(
                "validity_scope " + str(index) + ": "
                + json.dumps(inference.validity_scope, ensure_ascii=False, sort_keys=True)
            )

    return "\n".join(lines), id_map


__all__ = [
    "ChainHop",
    "EDGE_NODE_TYPES",
    "FollowChainResult",
    "InferredChain",
    "TRANSITIVE_COMPOSITIONS",
    "TRANSITIVE_EDGE_TYPES",
    "chain_anchor_relevances",
    "compose_two_hop_paths",
    "merge_validity_scopes",
    "render_follow_chain_context",
]
