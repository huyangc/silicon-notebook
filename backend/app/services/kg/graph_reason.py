"""rustworkx-backed in-memory KG graph for multi-hop reasoning.

Nodes carry: object_id, object_type, name.
Edges carry: edge_type, evidence (list[dict]), confidence (float), tier (str).

build_rx_graph() is a pure function — no I/O, easily unit-tested with a
synthetic fixture.  The repo wraps it via _rx_graph() with VectorCache
version-keying (same (COUNT, MAX created_at) pattern as _vector_matrix).
"""
from __future__ import annotations

import json
from collections import deque
from typing import Dict, List, Optional, Tuple

import rustworkx as rx

# Default reasoning edge types (well-populated: derived_from=4160, supports=6068,
# depends_on=791).  contrasts_with/prerequisite_of are thin; callers may extend.
DEFAULT_REASONING_EDGES = frozenset({"derived_from", "supports", "depends_on"})


def build_rx_graph(
    nodes: Dict[str, dict],
    relations: List[dict],
    tier: str = "base",
) -> Tuple[rx.PyDiGraph, Dict[int, str], Dict[str, int]]:
    """Build a PyDiGraph from dicts.

    `nodes`  — {object_id: {"type": str, "name": str, ...}}
    `relations` — list of knowledge_relations rows (dicts with keys:
        id, source_object_id, target_object_id, edge_type, evidence)

    Returns (graph, idx_to_oid, oid_to_idx).
    `evidence` in each edge payload is a list[dict] (JSON-decoded Evidence dicts).
    `confidence` defaults to 1.0 (no confidence column in knowledge_relations).
    `tier` is injected per-call (default "base" for single-tier POC).
    """
    G: rx.PyDiGraph = rx.PyDiGraph()
    idx_to_oid: Dict[int, str] = {}
    oid_to_idx: Dict[str, int] = {}

    for oid, meta in nodes.items():
        idx = G.add_node({
            "object_id": oid,
            "object_type": meta.get("type", ""),
            "name": meta.get("name", ""),
        })
        idx_to_oid[idx] = oid
        oid_to_idx[oid] = idx

    for rel in relations:
        src_oid = rel["source_object_id"]
        tgt_oid = rel["target_object_id"]
        if src_oid not in oid_to_idx or tgt_oid not in oid_to_idx:
            continue  # skip dangling edges (object deleted/deprecated)
        ev_raw = rel.get("evidence", [])
        if isinstance(ev_raw, str):
            try:
                ev_raw = json.loads(ev_raw)
            except Exception:
                ev_raw = []
        G.add_edge(
            oid_to_idx[src_oid],
            oid_to_idx[tgt_oid],
            {
                "rel_id": rel.get("id", ""),
                "edge_type": rel["edge_type"],
                "evidence": ev_raw if isinstance(ev_raw, list) else [],
                "confidence": float(rel.get("confidence", 1.0)),
                "tier": tier,
            },
        )

    return G, idx_to_oid, oid_to_idx


def multihop_subgraph(
    G: rx.PyDiGraph,
    oid_to_idx: Dict[str, int],
    idx_to_oid: Dict[int, str],
    seed_ids: List[str],
    edge_types: Optional[frozenset] = None,
    max_depth: int = 3,
    max_fan_out: int = 8,
) -> List[Tuple[dict, Optional[dict], Optional[str]]]:
    """BFS from `seed_ids` along `edge_types`, bounded by depth and fan-out.

    Returns ordered list of (node_payload, edge_payload_or_None, src_object_id)
    triples.  Seed nodes carry edge_payload=None and src_object_id=None; each
    non-seed item's src_object_id is the object_id of the node the edge was
    traversed FROM (so render_subgraph_context can emit full chain annotations).
    Each node appears at most once (visited set guards cycles).  At each hop the
    eligible out-edges are sorted by confidence desc, then capped to
    `max_fan_out`.

    edge_types: frozenset of edge_type strings to follow; None = all edges.
    """
    if edge_types is None:
        edge_types = frozenset()   # empty = treat as "all" below
    use_all = len(edge_types) == 0

    visited: set = set()
    result: List[Tuple[dict, Optional[dict], Optional[str]]] = []
    # queue entries: (node_idx, depth)
    queue: deque = deque()

    for oid in seed_ids:
        idx = oid_to_idx.get(oid)
        if idx is None or idx in visited:
            continue
        visited.add(idx)
        result.append((G[idx], None, None))
        queue.append((idx, 0))

    while queue:
        cur_idx, depth = queue.popleft()
        if depth >= max_depth:
            continue
        cur_oid = idx_to_oid.get(cur_idx)
        # Gather eligible out-edges for this node
        out_edges = []
        for tgt_idx in G.successor_indices(cur_idx):
            if tgt_idx in visited:
                continue
            edge_data = G.get_edge_data(cur_idx, tgt_idx)
            if use_all or edge_data.get("edge_type") in edge_types:
                out_edges.append((tgt_idx, edge_data))
        # Sort by confidence desc, cap fan-out
        out_edges.sort(key=lambda x: x[1].get("confidence", 1.0), reverse=True)
        out_edges = out_edges[:max_fan_out]

        for tgt_idx, edge_data in out_edges:
            if tgt_idx in visited:
                continue
            visited.add(tgt_idx)
            result.append((G[tgt_idx], edge_data, cur_oid))
            queue.append((tgt_idx, depth + 1))

    return result


def render_subgraph_context(
    subgraph: List[Tuple[dict, Optional[dict], Optional[str]]],
    id_offset: int = 0,
) -> Tuple[str, dict]:
    """Render the (node, edge, src_oid) subgraph into (context_block_str, id_map).

    The format mirrors _answer_context (sqlite_repository.py:3682-3757) so that
    _answer_kg, _parse_answer_anchors, and _MARKER_RE all work unchanged:

        k1: [Formula] Node A
        k2: [Claim] Node B  — ev: "A derives B"
        chain:
          [k2] Node B --derived_from--> [k1] Node A

    The per-edge chain line carries BOTH endpoint keys (`[k_tgt] tgt
    --edge_type--> [k_src] src`), mirroring `_answer_context`'s existing
    `k2 -[derived_from]-> k1` relation lines so the `[k]` anchor markers remain
    resolvable by `_parse_answer_anchors` / `_MARKER_RE`.

    id_map[k{i}] = {"object_id": ..., "object_type": ..., "name": ...,
                    "definition": "", "snippet": quote, "source_title": "",
                    "location_label": ""}

    id_offset lets the caller start numbering after an existing context block
    (e.g., if fast-mode hits were already assigned k1..k5, graph nodes begin k6).
    """
    lines: List[str] = []
    id_map: Dict[str, dict] = {}
    oid_to_key: Dict[str, str] = {}

    for i, (node, edge, _src_oid) in enumerate(subgraph, start=id_offset + 1):
        key = f"k{i}"
        oid = node["object_id"]
        name = node.get("name", oid)
        otype = node.get("object_type", "")
        quote = ""
        if edge:
            ev_list = edge.get("evidence", [])
            if ev_list and isinstance(ev_list[0], dict):
                quote = ev_list[0].get("quote", "")
        ev_suffix = f'  — ev: "{quote}"' if quote else ""
        lines.append(f"{key}: [{otype}] {name}{ev_suffix}")
        id_map[key] = {
            "object_id": oid,
            "object_type": otype,
            "name": name,
            "definition": "",
            "snippet": quote,
            "source_title": "",
            "location_label": "",
        }
        oid_to_key[oid] = key

    # Chain annotation lines (one per edge, in traversal order). BFS visits the
    # source node before its targets, so oid_to_key[src_oid] is always populated.
    chain_lines: List[str] = []
    for node, edge, src_oid in subgraph:
        if not edge:
            continue
        tgt_oid = node["object_id"]
        tgt_key = oid_to_key.get(tgt_oid, "?")
        src_key = oid_to_key.get(src_oid, "?")
        etype = edge.get("edge_type", "?")
        src_name = ""  # source name resolved from id_map if present
        if src_key in id_map:
            src_name = id_map[src_key].get("name", "")
        tgt_name = node.get("name", tgt_oid)
        chain_lines.append(
            f"  [{tgt_key}] {tgt_name} --{etype}--> [{src_key}] {src_name}".rstrip()
        )

    if chain_lines:
        lines.append("chain:")
        lines.extend(chain_lines)

    return ("\n".join(lines) if lines else "(none)"), id_map
