"""rustworkx-backed in-memory graph for HippoRAG-style Personalized PageRank
cross-document retrieval. Pure functions, zero I/O — unit-testable.

Graph is a PyDiGraph with RECIPROCAL edges: rx.pagerank only accepts PyDiGraph
(it rejects PyGraph); adding both directions per edge makes PPR flow
symmetrically — equivalent to HippoRAG's igraph directed=False. Three node kinds
share one index:
  - KG entity      key = object_id
  - passage(chunk) key = f"chunk:{chunk_id}"
  - cluster router key = f"cluster:{canonical_id}"  (synthetic synonym hub)
Synonym bridges are a star: every member of a concept cluster links to the
cluster's router node, so PPR mass flows between same-concept nodes living in
different documents (N edges per cluster, not N^2).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import rustworkx as rx

from app.services.kg.edge_schema import is_queryable_edge_pair


def build_ppr_graph(
    kg_nodes: Dict[str, dict],
    chunk_ids: List[str],
    relations: List[dict],
    memberships: List[Tuple[str, str]],
    cluster_groups: Dict[str, List[str]],
    extra_edges: Optional[List[Tuple[str, str, float]]] = None,
) -> Tuple[rx.PyDiGraph, Dict[str, int], Dict[int, str]]:
    """Build the reciprocal-edge PPR digraph.

    kg_nodes       — {object_id: {"type": str, "name": str}}
    chunk_ids      — list of chunk_id strings (passage nodes)
    relations      — [{"source_object_id","target_object_id", ...}, ...] (KG↔KG)
    memberships    — [(object_id, chunk_id), ...] (KG↔chunk)
    cluster_groups — {canonical_id: [object_id, ...]} (synonym bridges)

    Returns (G, key_to_idx, chunk_idx_to_id):
      key_to_idx      — {node_key: vertex_idx} (object_id / chunk:* / cluster:*)
      chunk_idx_to_id — {vertex_idx: chunk_id} for passage nodes only
    """
    G: rx.PyDiGraph = rx.PyDiGraph()
    key_to_idx: Dict[str, int] = {}
    chunk_idx_to_id: Dict[int, str] = {}

    def _add(key: str, payload: dict) -> int:
        idx = key_to_idx.get(key)
        if idx is None:
            idx = G.add_node(payload)
            key_to_idx[key] = idx
        return idx

    for oid, meta in kg_nodes.items():
        _add(oid, {"kind": "entity", "object_id": oid,
                   "object_type": meta.get("type", ""), "name": meta.get("name", "")})

    for cid in chunk_ids:
        idx = _add(f"chunk:{cid}", {"kind": "chunk", "chunk_id": cid})
        chunk_idx_to_id[idx] = cid

    seen_pairs: set = set()

    def _edge(a: int, b: int, weight: float) -> None:
        # Add BOTH directions (rx.pagerank needs a PyDiGraph; reciprocal edges
        # make traversal symmetric). Dedup on the unordered pair so one logical
        # undirected edge yields exactly two directed edges.
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        if k in seen_pairs:
            return
        seen_pairs.add(k)
        G.add_edge(a, b, {"weight": weight})
        G.add_edge(b, a, {"weight": weight})

    for rel in relations:
        if (rel["source_object_id"] not in kg_nodes
                or rel["target_object_id"] not in kg_nodes):
            continue
        a = key_to_idx.get(rel["source_object_id"])
        b = key_to_idx.get(rel["target_object_id"])
        if a is None or b is None:
            continue  # dangling
        if not is_queryable_edge_pair(
            rel.get("edge_type"),
            kg_nodes[rel["source_object_id"]].get("type"),
            kg_nodes[rel["target_object_id"]].get("type"),
        ):
            continue
        _edge(a, b, 1.0)

    for oid, cid in memberships:
        a = key_to_idx.get(oid)
        b = key_to_idx.get(f"chunk:{cid}")
        if a is None or b is None:
            continue
        _edge(a, b, 1.0)

    for canonical_id, members in cluster_groups.items():
        present = [key_to_idx[o] for o in members if o in key_to_idx]
        if not present:
            continue
        router = _add(f"cluster:{canonical_id}", {"kind": "cluster",
                                                  "canonical_id": canonical_id})
        for m in present:
            _edge(router, m, 1.0)

    for a_oid, b_oid, weight in (extra_edges or []):
        a = key_to_idx.get(a_oid)
        b = key_to_idx.get(b_oid)
        if a is not None and b is not None:
            _edge(a, b, float(weight))

    return G, key_to_idx, chunk_idx_to_id


# variant_edge_pairs / emb_synonym_edges sunk to app.domain.kg.ppr_pairs in B3
# (pure functions, zero app.services/app.repositories dependency, so
# app.repositories adapters can import them directly). Re-exported here
# unchanged so this module's own callers (build_scale_index and friends)
# keep resolving to the SAME objects without any call-site changes.
from app.domain.kg.ppr_pairs import emb_synonym_edges, variant_edge_pairs  # noqa: F401


def run_ppr(
    G: rx.PyDiGraph,
    chunk_idx_to_id: Dict[int, str],
    reset: Dict[int, float],
    damping: float = 0.5,
) -> List[Tuple[str, float]]:
    """Run Personalized PageRank and return chunk rankings.

    reset   — {vertex_idx: weight} personalization vector (>=1 non-zero entry).
    Returns [(chunk_id, normalized_score), ...] sorted desc; scores min-max
    normalized into [0,1] so they satisfy the relevance/tau invariant. Empty
    reset (or no non-zero weight) → [] (caller falls back to dense retrieval).
    """
    if not reset or not any(w > 0 for w in reset.values()) or G.num_nodes() == 0:
        return []
    scores = rx.pagerank(
        G,
        alpha=damping,
        personalization={int(k): float(v) for k, v in reset.items() if v > 0},
        weight_fn=lambda payload: float(payload.get("weight", 1.0)),
    )
    raw = [(cid, float(scores[idx])) for idx, cid in chunk_idx_to_id.items()]
    if not raw:
        return []
    vals = [s for _, s in raw]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    norm = [(cid, (s - lo) / span if span > 0 else 0.0) for cid, s in raw]
    norm.sort(key=lambda x: x[1], reverse=True)
    return norm
