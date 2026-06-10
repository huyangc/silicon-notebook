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
    tier_map: Optional[Dict[str, str]] = None,
) -> Tuple[rx.PyDiGraph, Dict[int, str], Dict[str, int]]:
    """Build a PyDiGraph from dicts.

    `nodes`  — {object_id: {"type": str, "name": str, ...}}
    `relations` — list of knowledge_relations rows (dicts with keys:
        id, source_object_id, target_object_id, edge_type, evidence)

    Returns (graph, idx_to_oid, oid_to_idx).
    `evidence` in each edge payload is a list[dict] (JSON-decoded Evidence dicts).
    `confidence` defaults to 1.0 (no confidence column in knowledge_relations).
    `tier` is injected per-call (default "base" for single-tier POC).

    `tier_map` — optional {notebook_id: tier_str} mapping.  When provided,
    each relation's tier is looked up via rel["notebook_id"]; falls back to
    `tier` when the key is absent or tier_map is None.  Federated callers pass
    tier_map AND tier="personal" so any unmapped (orphan) relation is treated
    conservatively as personal rather than authoritative base.
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
        rel_nb = rel.get("notebook_id", "")
        edge_tier = (
            tier_map[rel_nb]
            if (tier_map and rel_nb in tier_map)
            else tier
        )
        G.add_edge(
            oid_to_idx[src_oid],
            oid_to_idx[tgt_oid],
            {
                "rel_id": rel.get("id", ""),
                "edge_type": rel["edge_type"],
                "evidence": ev_raw if isinstance(ev_raw, list) else [],
                "confidence": float(rel.get("confidence", 1.0)),
                "tier": edge_tier,
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

    The returned node and edge payloads are shallow COPIES, never the live dicts
    stored inside `G`.  rustworkx's get_edge_data / G[idx] hand back the same
    object held in the graph, and `G` is typically the version-cached PyDiGraph
    (see SqliteRepository._rx_graph) reused across many asks.  A consumer that
    mutates a payload in place — e.g. ask_graph demoting a flagged edge's
    confidence to 0.05 before re-rendering — would otherwise corrupt the cached
    graph and leak that change into every subsequent ask until the next version
    rebuild.  Copying here keeps the cache pristine for all downstream callers.

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
        result.append((dict(G[idx]), None, None))
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
            result.append((dict(G[tgt_idx]), dict(edge_data), cur_oid))
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
        # Tier comes from the incoming edge; seed nodes (no edge) default
        # "personal" since we cannot know their tier without an edge to read.
        node_tier = edge.get("tier", "personal") if edge else "personal"
        quote = ""
        if edge:
            ev_list = edge.get("evidence", [])
            if ev_list and isinstance(ev_list[0], dict):
                quote = ev_list[0].get("quote", "")
        ev_suffix = f'  — ev: "{quote}"' if quote else ""
        # [type][tier] matches the format answer_prompt expects (prompts.py).
        lines.append(f"{key}: [{otype}][{node_tier}] {name}{ev_suffix}")
        id_map[key] = {
            "object_id": oid,
            "object_type": otype,
            "name": name,
            "definition": "",
            "snippet": quote,
            "source_title": "",
            "location_label": "",
            "tier": node_tier,
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
        edge_tier = edge.get("tier", "personal")
        src_name = ""  # source name resolved from id_map if present
        if src_key in id_map:
            src_name = id_map[src_key].get("name", "")
        tgt_name = node.get("name", tgt_oid)
        chain_lines.append(
            f"  [{tgt_key}] {tgt_name} --{etype}--> [{src_key}] {src_name}  (tier={edge_tier})".rstrip()
        )

    if chain_lines:
        lines.append("chain:")
        lines.extend(chain_lines)

    return ("\n".join(lines) if lines else "(none)"), id_map


# Prompt + schema for adversarial edge verification
_VERIFY_SCHEMA_HINT = '{"valid": true, "reason": ""}'

_VERIFY_PROMPT = (
    "Does the cited evidence below actually support the claimed knowledge-graph edge? "
    "Answer valid=true only if the quote directly substantiates the edge; "
    "valid=false if the quote is absent, unrelated, or only tangentially relevant.\n\n"
    "Edge: {src_name} --{edge_type}--> {tgt_name}\n"
    "Evidence quote: \"{quote}\"\n\n"
    "Respond ONLY with JSON matching: {schema}"
)


# Authority factor per tier: personal notes are plausible but unverified.
# Applied as a multiplier on confidence before the chain_trust min so a
# fully-confident personal hop never out-trusts a curated base hop.
_AUTHORITY_FACTOR: Dict[str, float] = {
    "base":     1.0,
    "personal": 0.85,
}


def verify_chain_edges(
    subgraph: List[Tuple[dict, Optional[dict], Optional[str]]],
    llm_client,
    votes: int = 1,
    timeout: int = 30,
) -> dict:
    """Adversarial LLM check for each edge in the chain.

    For each (node, edge, src_oid) triple where edge is not None, ask the LLM
    `votes` times whether the evidence supports the edge.  A majority of
    valid=True votes → edge passes; otherwise it is flagged and its confidence
    is demoted to 0.05 in the returned flagged list.

    chain_trust = min(effective_confidence) over all edges (1.0 if no edges),
    where effective_confidence = (original_conf if passed else 0.05) *
    authority_factor(tier).  Base edges have factor 1.0; personal edges 0.85,
    so a fully-confident personal hop caps chain_trust at 0.85.

    Returns:
        {
          "chain_trust": float,       # weakest-link, authority-weighted
          "flagged": [                # edges that failed verification
            {"edge_type": str, "src_name": str, "tgt_name": str,
             "reason": str, "demoted_confidence": 0.05, "tier": str,
             "base_override": bool}   # base_override only when base wins a conflict
          ],
          "edge_results": [           # per-edge detail
            {"edge_type": str, "valid": bool, "original_confidence": float,
             "tier": str}
          ],
          "authority_notes": [str]    # one note per personal hop + override notes
        }
    """
    import json as _json

    edge_results = []
    flagged = []
    flagged_pairs = []   # parallel to `flagged`: (src_oid, tgt_oid) per entry
    confidences = []

    for node, edge, src_oid in subgraph:
        if not edge:
            continue
        tgt_name = node.get("name", node.get("object_id", "?"))
        src_name = src_oid or "?"  # source object_id (no name lookup here)

        edge_type = edge.get("edge_type", "?")
        ev_list = edge.get("evidence", [])
        quote = ev_list[0].get("quote", "") if ev_list and isinstance(ev_list[0], dict) else ""
        original_conf = float(edge.get("confidence", 1.0))

        # Cast majority vote
        valid_votes = 0
        last_reason = ""
        if not getattr(llm_client, "configured", False) or not quote:
            # No LLM or no evidence → pass-through (cannot verify; fail-open)
            valid_votes = votes
        else:
            prompt = _VERIFY_PROMPT.format(
                src_name=src_name, edge_type=edge_type, tgt_name=tgt_name,
                quote=quote, schema=_VERIFY_SCHEMA_HINT,
            )
            for _ in range(votes):
                try:
                    raw = llm_client.chat_json(
                        [{"role": "user", "content": prompt}],
                        _VERIFY_SCHEMA_HINT,
                        timeout=timeout,
                        max_retries=1,
                    )
                    data = _json.loads(raw)
                    if isinstance(data, dict) and data.get("valid", True):
                        valid_votes += 1
                    last_reason = str(data.get("reason", "")) if isinstance(data, dict) else ""
                except Exception:
                    valid_votes += 1   # on error, assume valid (fail-open)

        passed = valid_votes > (votes / 2)
        effective_conf = original_conf if passed else 0.05
        # Authority discount: a personal edge counts less than a base edge even
        # when the LLM verifier passed it (curation, not just plausibility).
        edge_tier = edge.get("tier", "personal")
        auth_factor = _AUTHORITY_FACTOR.get(edge_tier, 0.85)
        effective_conf_with_auth = effective_conf * auth_factor
        confidences.append(effective_conf_with_auth)
        edge_results.append({
            "edge_type": edge_type,
            "valid": passed,
            "original_confidence": original_conf,
            "tier": edge_tier,
        })
        if not passed:
            flagged.append({
                "edge_type": edge_type,
                "src_name": src_name,
                "tgt_name": tgt_name,
                "reason": last_reason,
                "demoted_confidence": 0.05,
                "tier": edge_tier,
            })
            flagged_pairs.append((src_oid or "", node.get("object_id", "")))

    # Conflict precedence: group edges by (src_oid, tgt_oid). If a personal edge
    # is flagged AND a base edge on the same pair passed, mark the personal flag
    # with base_override=True and record in authority_notes. edge_results and the
    # edge_triples below share the same loop order, so index i lines up.
    edge_triples = [(node, edge, src_oid)
                    for node, edge, src_oid in subgraph if edge]
    pair_to_results: Dict[tuple, list] = {}
    for i, (node, edge, src_oid) in enumerate(edge_triples):
        tgt_oid = node.get("object_id", "")
        pair = (src_oid or "", tgt_oid)
        pair_to_results.setdefault(pair, []).append(i)

    override_pairs = set()
    for pair, indices in pair_to_results.items():
        if len(indices) < 2:
            continue
        base_valid = any(
            edge_results[i]["tier"] == "base" and edge_results[i]["valid"]
            for i in indices
        )
        pers_invalid = any(
            edge_results[i]["tier"] == "personal" and not edge_results[i]["valid"]
            for i in indices
        )
        if base_valid and pers_invalid:
            override_pairs.add(pair)

    # Scope base_override per (src,tgt) pair: only flagged personal entries on a
    # pair where a base edge verified OK get marked — never unrelated flags.
    for fi, f in enumerate(flagged):
        if f.get("tier") == "personal" and flagged_pairs[fi] in override_pairs:
            flagged[fi]["base_override"] = True

    authority_notes = []
    for er in edge_results:
        if er["tier"] == "personal":
            authority_notes.append(
                f"{er['edge_type']} (personal): this step rests on a personal note"
            )
    for f in flagged:
        if f.get("base_override"):
            authority_notes.append(
                f"{f['edge_type']} (personal overridden by base): base_override=True; "
                "base reference supersedes personal note on this hop"
            )

    chain_trust = min(confidences) if confidences else 1.0
    return {
        "chain_trust": chain_trust,
        "flagged": flagged,
        "edge_results": edge_results,
        "authority_notes": authority_notes,
    }
