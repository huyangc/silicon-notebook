"""rustworkx-backed in-memory KG graph for multi-hop reasoning.

Nodes carry: object_id, object_type, name.
Edges carry: edge_type, evidence (list[dict]), confidence (float), tier (str).

build_rx_graph() is a pure function — no I/O, easily unit-tested with a
synthetic fixture.  The repo wraps it via _federated_rx_graph() with
VectorCache version-keying (same (COUNT, MAX created_at) pattern as
_vector_matrix).
"""
from __future__ import annotations

import json
from collections import deque
from typing import Dict, List, Optional, Tuple

import rustworkx as rx

from app.services.kg.edge_schema import (
    DEFAULT_REASONING_EDGE_TYPES,
    is_queryable_edge_pair,
)

# Default reasoning edge types (well-populated: derived_from=4160, supports=6068,
# depends_on=791).  contrasts_with/prerequisite_of are thin; callers may extend.
DEFAULT_REASONING_EDGES = DEFAULT_REASONING_EDGE_TYPES


def build_rx_graph(
    nodes: Dict[str, dict],
    relations: List[dict],
    tier: str = "base",
    tier_map: Optional[Dict[str, str]] = None,
    cluster_groups: Optional[Dict[str, List[str]]] = None,
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

    `cluster_groups` — optional {canonical_id: [object_id, ...]} mapping of
    concept-cluster membership.  When provided, every REAL node is tagged
    kind="entity" and, for each canonical_id with ≥2 of its members PRESENT as
    nodes, a synthetic TRANSIT-ONLY hub node is added (object_id
    f"cluster:{canonical_id}", kind="cluster") with both-direction synonym
    edges to each present member.  These hubs let multi-hop reasoning bridge
    documents that share a cluster but have no direct relation; they are
    PASS-THROUGH only — multihop_subgraph traverses them but never emits them,
    and render_subgraph_context / verify_chain_edges skip them — so the LLM can
    never cite a "cluster:..." hub as a real answer.  Clusters with <2 present
    members add no hub (a lone member needs no bridge).  When cluster_groups is
    None the graph shape and node payloads are byte-identical to before.
    """
    G: rx.PyDiGraph = rx.PyDiGraph()
    idx_to_oid: Dict[int, str] = {}
    oid_to_idx: Dict[str, int] = {}

    # Tag real nodes with kind="entity" ONLY when cluster_groups is in play, so
    # the None path stays byte-identical to the pre-hub payload shape.
    tag_kind = cluster_groups is not None
    for oid, meta in nodes.items():
        payload = {
            "object_id": oid,
            "object_type": meta.get("type", ""),
            "name": meta.get("name", ""),
        }
        if meta.get("tier"):
            payload["tier"] = meta["tier"]
        if meta.get("notebook_id"):
            payload["notebook_id"] = meta["notebook_id"]
        if tag_kind:
            payload["kind"] = "entity"
        # gate ii (T3): thread a single-row knowhow cell KO's table_id/rows so
        # render_subgraph_context can compute its row-drawer jump ref. Only
        # present when the caller's node meta was enriched (graph_retrieval's
        # _load, flag on); otherwise the keys are absent → payload shape stays
        # byte-identical to the pre-knowhow build.
        if meta.get("table_id") is not None and meta.get("rows") is not None:
            payload["table_id"] = meta["table_id"]
            payload["rows"] = meta["rows"]
        idx = G.add_node(payload)
        idx_to_oid[idx] = oid
        oid_to_idx[oid] = idx

    for rel in relations:
        src_oid = rel["source_object_id"]
        tgt_oid = rel["target_object_id"]
        if src_oid not in oid_to_idx or tgt_oid not in oid_to_idx:
            continue  # skip dangling edges (object deleted/deprecated)
        if not is_queryable_edge_pair(
            rel.get("edge_type"),
            nodes[src_oid].get("type") or nodes[src_oid].get("object_type"),
            nodes[tgt_oid].get("type") or nodes[tgt_oid].get("object_type"),
        ):
            continue
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
                "review_status": str(rel.get("review_status") or "pending"),
            },
        )

    # Synthetic transit-only cluster hubs.  Only built when cluster_groups is
    # provided; never alters the None-path graph.
    if cluster_groups:
        for canonical_id, members in cluster_groups.items():
            present = [m for m in members if m in oid_to_idx]
            if len(present) < 2:
                continue  # a lone present member needs no hub
            hub_idx = G.add_node({
                "object_id": f"cluster:{canonical_id}",
                "kind": "cluster",
                "name": canonical_id,
            })
            for m in present:
                m_idx = oid_to_idx[m]
                # Both directions so a hub reached from any member can hand mass
                # on to every sibling member (the cross-doc bridge).
                G.add_edge(hub_idx, m_idx,
                           {"edge_type": "synonym", "rel_id": "", "kind": "synonym"})
                G.add_edge(m_idx, hub_idx,
                           {"edge_type": "synonym", "rel_id": "", "kind": "synonym"})

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
    eligible out-edges are sorted by confidence desc, relation id, and target
    object id, then capped to `max_fan_out`.  The explicit ties are required
    because rustworkx successor iteration follows node-index order rather than
    relation insertion order.

    TRANSIT-ONLY cluster hubs (kind=="cluster", produced by build_rx_graph from
    cluster_groups): a hub is traversed THROUGH — its successors are still
    enqueued so reasoning mass crosses to sibling members in other documents —
    but the hub itself is NEVER appended to the result, and neither is the
    synonym edge leading into it.  Downstream the answer only ever sees real
    entity nodes, so the LLM cannot cite a "cluster:..." hub as an answer.  The
    visited set still records the hub index, so the hub↔member both-direction
    synonym edges can never ping-pong into an infinite loop.

    The returned node and edge payloads are shallow COPIES, never the live dicts
    stored inside `G`.  rustworkx's get_edge_data / G[idx] hand back the same
    object held in the graph, and `G` is typically the version-cached PyDiGraph
    (see SqliteRepository._federated_rx_graph) reused across many asks.  A consumer that
    mutates a payload in place — e.g. the now-retired graph ask engine used to
    demote a flagged edge's confidence to 0.05 before re-rendering — would
    otherwise corrupt the cached graph and leak that change into every
    subsequent ask until the next version rebuild.  Copying here keeps the
    cache pristine for all downstream callers.

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
        # Is the node we're expanding FROM a transit hub?  If so, the synthetic
        # synonym edges out of it are NOT real reasoning steps: the real member
        # we reach through the hub is recorded as a fresh transit-arrival node
        # (edge=None, src=None) so neither the synthetic edge nor the hub's id
        # ever leaks into the rendered chain or the edge verifier.
        cur_is_hub = G[cur_idx].get("kind") == "cluster"
        # Gather eligible out-edges for this node
        out_edges = []
        for tgt_idx in G.successor_indices(cur_idx):
            if tgt_idx in visited:
                continue
            edge_data = G.get_edge_data(cur_idx, tgt_idx)
            if use_all or edge_data.get("edge_type") in edge_types:
                out_edges.append((tgt_idx, edge_data))
        # Stable confidence ties must not inherit rustworkx node-index order.
        out_edges.sort(
            key=lambda item: (
                -float(item[1].get("confidence", 1.0)),
                str(item[1].get("rel_id", "")),
                str(idx_to_oid.get(item[0], "")),
            )
        )
        out_edges = out_edges[:max_fan_out]

        for tgt_idx, edge_data in out_edges:
            if tgt_idx in visited:
                continue
            visited.add(tgt_idx)
            tgt_node = G[tgt_idx]
            # Transit-only hub: still enqueue so its successors expand (mass
            # crosses the bridge to sibling members), but do NOT append the hub
            # (nor the synonym edge into it) to the result — hubs are never
            # rendered or cited.  visited already guards the hub↔member cycle.
            if tgt_node.get("kind") == "cluster":
                queue.append((tgt_idx, depth + 1))
                continue
            if cur_is_hub:
                # Reached a real member THROUGH a hub: the synthetic synonym
                # edge is not a citable reasoning step, and src would be the
                # hub's id.  Record the member as a transit-arrival node with no
                # edge so nothing references the hub downstream.
                result.append((dict(tgt_node), None, None))
            else:
                result.append((dict(tgt_node), dict(edge_data), cur_oid))
            queue.append((tgt_idx, depth + 1))

    return result


def render_subgraph_context(
    subgraph: List[Tuple[dict, Optional[dict], Optional[str]]],
    id_offset: int = 0,
    knowhow_enabled: bool = True,
) -> Tuple[str, dict]:
    """Render the (node, edge, src_oid) subgraph into (context_block_str, id_map).

    The format mirrors _answer_context (sqlite_repository.py) so that
    _parse_answer_anchors and _MARKER_RE all work unchanged:

        k1: [Formula] Node A
        k2: [Claim] Node B  — ev: "A derives B"
        chain:
          [k1] Node A --derived_from--> [k2] Node B

    The per-edge chain line carries BOTH endpoint keys (`[k_src] src
    --edge_type--> [k_tgt] tgt`), preserving the extraction/build contract and
    mirroring `_answer_context`'s existing
    `k2 -[derived_from]-> k1` relation lines so the `[k]` anchor markers remain
    resolvable by `_parse_answer_anchors` / `_MARKER_RE`.

    id_map[k{i}] = {"object_id": ..., "object_type": ..., "name": ...,
                    "definition": "", "snippet": quote, "source_title": "",
                    "location_label": "", "tier": ..., "knowhow": ref|None}

    id_offset lets the caller start numbering after an existing context block
    (e.g., so graph nodes can begin at k{n+1} to avoid key collisions).

    gate ii (T3, knowhow KG-node retrieval): when a node is a single-row knowhow
    cell KO it carries the raw `{table_id, rows}` (threaded here by
    `graph_retrieval._federated_rx_graph._load` → `build_rx_graph`, only when the
    `knowhow_kg_node_retrieval_enabled` flag is on). We reuse the SHARED anchor
    helper `evidence_context._knowhow_ref_from_payload` (len(rows)==1 rule) so
    `parse_anchors` — which already reads `context.get("knowhow")` — can put a
    `CitationKnowhowRef` on the anchor and the ask citation jumps to the row
    drawer, mirroring the chunk path. It is null-safe end to end: non-knowhow
    nodes (no `rows`) and merged multi-row KOs get None, so the `"knowhow"` key
    is added for EVERY node but is None unless a single unambiguous row exists —
    and `AnswerAnchor.knowhow`'s `exclude_if=None` keeps the wire byte-identical
    when there's no ref. `knowhow_enabled=False` forces None regardless (a
    render-level off-switch; the primary gate is the settings flag read in
    `_load`, so a caller that doesn't pass this argument still honors the flag
    via the node payload carrying — or not carrying — the raw fields).
    """
    # Lazy import (evidence_context pulls schemas/retrieval): keep graph_reason
    # import-light and cycle-free, matching verify_chain_edges' cancellation import.
    from app.services.evidence_context import _knowhow_ref_from_payload

    lines: List[str] = []
    id_map: Dict[str, dict] = {}
    oid_to_key: Dict[str, str] = {}

    # Transit-only cluster hubs are never rendered or cited.  Filter them out
    # up front so k-key numbering stays contiguous (no gaps) and the chain
    # loop below never references a hub endpoint.  (multihop_subgraph already
    # suppresses hubs; this is belt-and-suspenders for any other caller.)
    subgraph = [t for t in subgraph if t[0].get("kind") != "cluster"]

    for i, (node, edge, _src_oid) in enumerate(subgraph, start=id_offset + 1):
        key = f"k{i}"
        oid = node["object_id"]
        name = node.get("name", oid)
        otype = node.get("object_type", "")
        # Federated graph nodes carry their owning notebook tier. Fall back to
        # the incoming edge only for legacy/synthetic callers without it.
        node_tier = node.get("tier") or (edge.get("tier", "personal") if edge else "personal")
        quote = ""
        if edge:
            ev_list = edge.get("evidence", [])
            if ev_list and isinstance(ev_list[0], dict):
                quote = ev_list[0].get("quote", "")
        ev_suffix = f'  — ev: "{quote}"' if quote else ""
        # [type][tier] matches the format answer_prompt expects (prompts.py).
        lines.append(f"{key}: [{otype}][{node_tier}] {name}{ev_suffix}")
        # gate ii (T3): single-row knowhow cell KO → row-drawer jump ref; else
        # None (null-safe helper). `parse_anchors` surfaces it onto the anchor.
        knowhow = _knowhow_ref_from_payload(node) if knowhow_enabled else None
        id_map[key] = {
            "object_id": oid,
            "object_type": otype,
            "name": name,
            "definition": "",
            "snippet": quote,
            "source_title": "",
            "location_label": "",
            "tier": node_tier,
            # Active personal anchors preserve the established empty-id wire
            # convention; mounted base nodes retain their real participant id
            # so citation focus can resolve under the active notebook's auth.
            "notebook_id": (
                node.get("notebook_id", "") if node_tier == "base" else ""
            ),
            "knowhow": knowhow,
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
            f"  [{src_key}] {src_name} --{etype}--> [{tgt_key}] {tgt_name}  (tier={edge_tier})".rstrip()
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
    cancel_event=None,
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
    from app.services.cancellation import AskCancelled, raise_if_cancelled

    # Never verify an edge whose endpoint is a transit-only cluster hub — the
    # synthetic synonym edges aren't real reasoning steps and the hub id must
    # not reach the LLM prompt.  Filter once so the edge_results loop and the
    # edge_triples conflict-precedence loop below stay index-aligned.
    # (multihop_subgraph already suppresses hubs; this guards other callers.)
    subgraph = [t for t in subgraph if t[0].get("kind") != "cluster"]

    edge_results = []
    flagged = []
    flagged_pairs = []   # parallel to `flagged`: (src_oid, tgt_oid) per entry
    confidences = []

    for node, edge, src_oid in subgraph:
        raise_if_cancelled(cancel_event)
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
                raise_if_cancelled(cancel_event)
                try:
                    raw = llm_client.chat_json(
                        [{"role": "user", "content": prompt}],
                        _VERIFY_SCHEMA_HINT,
                        timeout=timeout,
                        max_retries=1,
                        cancel_event=cancel_event,
                    )
                    data = _json.loads(raw)
                    if isinstance(data, dict) and data.get("valid", True):
                        valid_votes += 1
                    last_reason = str(data.get("reason", "")) if isinstance(data, dict) else ""
                except AskCancelled:
                    raise
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


# ---------------------------------------------------------------------------
# Centrality helpers (Track E — edge trust & curation tooling)
# These are ADDITIVE functions; they do not modify any existing function.
# ---------------------------------------------------------------------------

def compute_edge_centrality(G: rx.PyDiGraph) -> Dict[str, float]:
    """Compute edge betweenness centrality for each edge in the graph.

    Returns {rel_id: float} where rel_id is taken from the edge payload key
    'rel_id'.  If a payload has no rel_id, falls back to str(edge_index).

    Empty graph returns {}.
    """
    if G.num_edges() == 0:
        return {}

    ec_raw: Dict[int, float] = rx.digraph_edge_betweenness_centrality(G, normalized=True)
    edge_idx_map = G.edge_index_map()   # {edge_idx: (src_idx, tgt_idx, payload)}

    result: Dict[str, float] = {}
    for edge_idx, score in ec_raw.items():
        # rustworkx.EdgeIndexMap supports __contains__/__getitem__ but not .get()
        if edge_idx not in edge_idx_map:
            continue
        _, _, payload = edge_idx_map[edge_idx]
        rel_id = payload.get("rel_id") if isinstance(payload, dict) else None
        key = str(rel_id) if rel_id else str(edge_idx)
        result[key] = float(score)
    return result
