"""Compatibility re-export shim (definitions sunk to app.domain in B3).

The authoritative KG edge contract now lives in
``app.domain.kg.edge_schema`` (zero ``app.*`` dependency, so
``app.repositories`` adapters can import it directly). This module
re-exports every name unchanged so existing importers keep resolving to the
SAME objects without any call-site changes.
"""
from __future__ import annotations

from app.domain.kg.edge_schema import (
    DEFAULT_REASONING_EDGE_TYPES,
    EDGE_SCHEMA_VERSION,
    EDGE_SPECS,
    EDGE_TYPE_ORDER,
    NODE_TYPES,
    TRANSITIVE_EDGE_TYPES,
    VALID_EDGE_TYPES,
    EdgeCategory,
    EdgeSpec,
    NodeType,
    canonical_edge_endpoints,
    canonical_edge_key,
    edge_schema_hint,
    is_queryable_edge_pair,
    is_valid_edge_pair,
    normalize_node_type,
    relation_pair_is_valid,
    render_edge_prompt_rules,
)

__all__ = [
    "DEFAULT_REASONING_EDGE_TYPES",
    "EDGE_SCHEMA_VERSION",
    "EDGE_SPECS",
    "EDGE_TYPE_ORDER",
    "NODE_TYPES",
    "TRANSITIVE_EDGE_TYPES",
    "VALID_EDGE_TYPES",
    "EdgeCategory",
    "EdgeSpec",
    "NodeType",
    "canonical_edge_endpoints",
    "canonical_edge_key",
    "edge_schema_hint",
    "is_queryable_edge_pair",
    "is_valid_edge_pair",
    "normalize_node_type",
    "relation_pair_is_valid",
    "render_edge_prompt_rules",
]
