"""Authoritative knowledge-graph edge contract.

Every producer and consumer must use this registry for edge vocabulary and
endpoint validation.  Extraction prompts, trust scoring, graph hydration and
query-time composition deliberately remain separate policies, but they all
reference the same endpoint pairs so a prompt-legal edge cannot later become
type-invalid by drift.

Sunk to app.domain in B3 (zero app.* dependency). ``app.services.kg.edge_schema``
re-exports every name here unchanged for existing importers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Mapping


NodeType = Literal["concept", "claim", "formula", "procedure"]
EdgeCategory = Literal["structural", "reasoning"]

NODE_TYPES = frozenset({"concept", "claim", "formula", "procedure"})

# Bump whenever persisted graph-artifact semantics change.  Scale-index and
# in-memory PPR cache versions include this value, so a deployment cannot keep
# serving an artifact built before a contract correction.
EDGE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EdgeSpec:
    edge_type: str
    allowed_pairs: frozenset[tuple[str, str]]
    category: EdgeCategory
    symmetric: bool = False
    transitive: bool = False
    default_reasoning_traversal: bool = False
    prompt_description: str = ""


def _pairs(sources: Iterable[str], targets: Iterable[str]):
    return frozenset((source, target) for source in sources for target in targets)


_C = ("concept",)
_CL = ("claim",)
_F = ("formula",)
_P = ("procedure",)
_CCF = ("concept", "claim", "formula")
_CLF = ("claim", "formula")


# Stable order is protocol-adjacent: it controls prompt/schema rendering only;
# persisted edge ids remain the string keys below.
EDGE_TYPE_ORDER = (
    "about",
    "supports",
    "derived_from",
    "depends_on",
    "contrasts_with",
    "prerequisite_of",
    "defines",
    "part_of",
    "composed_of",
    "kind_of",
    "used_in",
    "precedes",
)


EDGE_SPECS: Mapping[str, EdgeSpec] = {
    "defines": EdgeSpec(
        "defines", _pairs(_CL, _C), "structural",
        prompt_description="claim -> concept: the claim defines the concept",
    ),
    "about": EdgeSpec(
        "about", _pairs(_CLF, _C), "structural",
        prompt_description="claim/formula -> concept: the statement concerns the concept",
    ),
    "supports": EdgeSpec(
        "supports", _pairs(_CCF, _CL), "reasoning",
        default_reasoning_traversal=True,
        prompt_description="concept/claim/formula -> claim: evidence or argument backs the claim",
    ),
    "derived_from": EdgeSpec(
        "derived_from", _pairs(_CLF, _CLF), "reasoning",
        transitive=True, default_reasoning_traversal=True,
        prompt_description="claim/formula -> claim/formula: the result follows from the source",
    ),
    "depends_on": EdgeSpec(
        "depends_on", _pairs(_CCF, NODE_TYPES), "reasoning",
        default_reasoning_traversal=True,
        prompt_description=(
            "concept/claim/formula -> concept/claim/formula/procedure: "
            "the source's validity or value depends on the target"
        ),
    ),
    "contrasts_with": EdgeSpec(
        "contrasts_with", _pairs(_CCF, _CCF), "reasoning", symmetric=True,
        prompt_description=(
            "concept/claim/formula <-> concept/claim/formula: explicit trade-off, "
            "disagreement or contradiction"
        ),
    ),
    "prerequisite_of": EdgeSpec(
        "prerequisite_of", _pairs(("concept", "claim"), ("concept", "claim")),
        "reasoning", transitive=True,
        prompt_description=(
            "concept/claim -> concept/claim: the source must hold or be understood first"
        ),
    ),
    "part_of": EdgeSpec(
        "part_of", _pairs(_C, _C), "structural", transitive=True,
        prompt_description="concept -> concept: the source is a part of the target",
    ),
    "composed_of": EdgeSpec(
        "composed_of", _pairs(_C, _C), "structural",
        prompt_description="concept -> concept: the source is composed of the target",
    ),
    "kind_of": EdgeSpec(
        "kind_of", _pairs(_C, _C), "structural", transitive=True,
        prompt_description="concept -> concept: the source is a kind of the target",
    ),
    "used_in": EdgeSpec(
        "used_in", _pairs(("concept", "formula"), _P), "structural",
        prompt_description="concept/formula -> procedure: the source is used in the procedure",
    ),
    "precedes": EdgeSpec(
        "precedes",
        frozenset({
            ("claim", "claim"),
            ("formula", "formula"),
            ("procedure", "procedure"),
        }),
        "reasoning", transitive=True,
        prompt_description=(
            "claim -> claim, formula -> formula, or procedure -> procedure: "
            "the source explicitly comes before the target"
        ),
    ),
}

if tuple(EDGE_SPECS) != (
    "defines", "about", "supports", "derived_from", "depends_on",
    "contrasts_with", "prerequisite_of", "part_of", "composed_of",
    "kind_of", "used_in", "precedes",
):
    raise AssertionError("EDGE_SPECS declaration order changed unexpectedly")

VALID_EDGE_TYPES = frozenset(EDGE_SPECS)
DEFAULT_REASONING_EDGE_TYPES = frozenset(
    name for name, spec in EDGE_SPECS.items()
    if spec.default_reasoning_traversal
)
TRANSITIVE_EDGE_TYPES = frozenset(
    name for name, spec in EDGE_SPECS.items() if spec.transitive
)


def normalize_node_type(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in NODE_TYPES else ""


def is_valid_edge_pair(edge_type: object, source_type: object, target_type: object) -> bool:
    spec = EDGE_SPECS.get(str(edge_type or ""))
    if spec is None:
        return False
    pair = (normalize_node_type(source_type), normalize_node_type(target_type))
    return bool(pair[0] and pair[1] and pair in spec.allowed_pairs)


def is_queryable_edge_pair(edge_type: object, source_type: object, target_type: object) -> bool:
    """Apply the core contract without hiding schema-extension node types.

    Extraction emits only the four core object types and therefore uses the
    strict :func:`is_valid_edge_pair`. Existing deployments may additionally
    project administrator-defined knowledge types into the unified graph. A
    known edge touching such an extension type remains queryable; pairs whose
    two endpoints are both core types are always checked strictly.
    """
    spec = EDGE_SPECS.get(str(edge_type or ""))
    if spec is None:
        return False
    source = normalize_node_type(source_type)
    target = normalize_node_type(target_type)
    if not source or not target:
        return True
    return (source, target) in spec.allowed_pairs


def canonical_edge_endpoints(edge_type: str, source_id: str, target_id: str):
    spec = EDGE_SPECS.get(edge_type)
    if spec is not None and spec.symmetric and target_id < source_id:
        return target_id, source_id
    return source_id, target_id


def canonical_edge_key(edge_type: str, source_id: str, target_id: str):
    source_id, target_id = canonical_edge_endpoints(edge_type, source_id, target_id)
    return edge_type, source_id, target_id


def render_edge_prompt_rules() -> str:
    lines = []
    for edge_type in EDGE_TYPE_ORDER:
        spec = EDGE_SPECS[edge_type]
        lines.append(f"- {edge_type} ({spec.prompt_description}).")
    return "\n".join(lines)


def edge_schema_hint() -> str:
    return "|".join(EDGE_TYPE_ORDER)


def relation_pair_is_valid(relation: Mapping[str, object], node_types: Mapping[str, str]) -> bool:
    return is_valid_edge_pair(
        relation.get("edge_type"),
        node_types.get(str(relation.get("source_object_id") or "")),
        node_types.get(str(relation.get("target_object_id") or "")),
    )


__all__ = [
    "DEFAULT_REASONING_EDGE_TYPES",
    "EDGE_SPECS",
    "EDGE_TYPE_ORDER",
    "EDGE_SCHEMA_VERSION",
    "EdgeSpec",
    "NODE_TYPES",
    "TRANSITIVE_EDGE_TYPES",
    "VALID_EDGE_TYPES",
    "canonical_edge_endpoints",
    "canonical_edge_key",
    "edge_schema_hint",
    "is_valid_edge_pair",
    "is_queryable_edge_pair",
    "normalize_node_type",
    "relation_pair_is_valid",
    "render_edge_prompt_rules",
]
