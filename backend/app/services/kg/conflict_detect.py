"""Conflict candidate detection — pure, DB-free, recall-oriented.

Given a notebook's KG objects + relations (+ optional embeddings), surfaces a
SPARSE, HIGH-RECALL list of *candidate* contradictory pairs.

This is RECALL ONLY — it does NOT decide whether a pair truly conflicts.
An LLM adjudicator in a later pipeline stage does that.

Output contract (downstream tasks depend on this — keep it stable):
    Each candidate is a plain dict:
        {"kind": "edge"|"node", "left_ref": str, "right_ref": str, "signal": str}
    where:
    - kind="edge" → left_ref/right_ref are relation ids
    - kind="node" → left_ref/right_ref are object ids
    - signal ∈ {"shared_head", "shared_tail", "shared_pair_diff_edge",
                 "discriminative", "semantic"}
    - Unordered pairs are deduplicated (A,B == B,A).

Node strategies:

    ``discriminative``
        Two Concept/Claim objects of the SAME ``object_type`` whose names differ
        by exactly one token belonging to a contrast group (e.g. nmos/pmos,
        positive/negative, input/output …).  Works with or without embeddings.

    ``semantic``  ← **independent recall strategy**
        Two Concept/Claim objects of the SAME ``object_type`` whose embeddings
        have cosine ≥ ``sim_threshold``.  Surfaced so the LLM adjudicator can
        check whether they agree or conflict — near-duplicate embeddings mean the
        objects discuss the same subject, which is a precondition for conflict.
        **Precision tradeoff**: many semantic pairs will be mere agreements; the
        LLM stage filters those out.
        Skipped entirely when ``embeddings`` is None.

    If a pair qualifies for BOTH strategies, it is emitted ONCE with
    ``signal="semantic"`` (semantic is the stronger same-subject evidence).

Reuse:
    - _norm from edge_trust.py  (name normalisation for triple-identity matching)
    - _discriminative_conflict from kg_merge.py  (contrast-group token check)
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Dict, List, Optional

# Reuse the canonical name-normaliser from edge_trust — avoids re-implementing.
from app.services.kg.edge_trust import _norm

# Intentional reuse of a private helper from kg_merge — coupling note: if
# _discriminative_conflict is renamed or moved in kg_merge.py, update this import.
from app.services.kg_merge import _discriminative_conflict

_log = logging.getLogger(__name__)

# Cap on the number of representative relations taken from each tail/head group for
# shared_head / shared_tail strategies.  Exactly 10 groups are taken; pairs are
# emitted from these representatives only (C(10,2)=45 max per group-key).
_MAX_GROUP_REPS = 10

# Object types whose node-level conflict checks are meaningful.
# Stored lowercase in production (kg_ingest stores node.type.lower()); kept as
# lowercase here and matched case-folded so both legacy capitalized synthetic data
# and production lowercase data work.
_NODE_CONFLICT_TYPES = {"concept", "claim"}


def detect_conflict_candidates(
    objects: List[dict],
    relations: List[dict],
    *,
    embeddings: Optional[Dict[str, List[float]]] = None,
    sim_threshold: float = 0.8,
) -> List[dict]:
    """Return a sparse, high-recall list of conflict-candidate pairs.

    Parameters
    ----------
    objects:
        List of dicts with at least ``id``, ``object_type``
        (Concept/Claim/Formula/Procedure), ``payload`` (dict containing ``name``).
    relations:
        List of dicts with at least ``id``, ``source_object_id``,
        ``target_object_id``, ``edge_type``.
    embeddings:
        Optional ``{object_id: list[float]}`` for the semantic strategy.
        If None, the semantic strategy is skipped entirely.
    sim_threshold:
        Cosine similarity threshold for the semantic strategy (default 0.8).

    Returns
    -------
    list[dict]
        Each dict: ``{"kind": "edge"|"node", "left_ref": str,
                       "right_ref": str, "signal": str}``.
        Unordered pairs are deduplicated.
    """
    candidates: List[dict] = []
    seen: set = set()  # frozenset({left_ref, right_ref}) for dedup

    def _add(kind: str, a: str, b: str, signal: str) -> None:
        """Emit a candidate if the unordered pair hasn't been seen yet."""
        key = frozenset([a, b])
        if key in seen or a == b:
            return
        seen.add(key)
        # Canonical ordering: smaller id goes left (deterministic, tidy)
        left, right = (a, b) if a < b else (b, a)
        candidates.append({"kind": kind, "left_ref": left,
                            "right_ref": right, "signal": signal})

    # ── Build object id → name map (for triple-key building, normalised) ──────
    obj_name: Dict[str, str] = {}
    for obj in objects:
        payload = obj.get("payload") or {}
        name = payload.get("name", "") if isinstance(payload, dict) else ""
        obj_name[obj["id"]] = name

    # ── Strategy 1 & 2: Edge candidates (structural) ─────────────────────────
    # Group relations by normalised triple for shared_pair_diff_edge, and by
    # (head_id, edge_type, norm_tail) for shared_head, and
    # (norm_head, edge_type, tail_id) for shared_tail.
    #
    # Key insight: we use normalised names for triple-identity matching (same as
    # corroboration_counts in edge_trust.py builds its triple_key), so that
    # "NMOS transistor" and "nmos transistor" are treated as the same endpoint.

    # shared_pair_diff_edge: same normalised (head, tail) different edge_type
    # Key: (norm_head_name, norm_tail_name) → list of (rel_id, edge_type)
    pair_rels: Dict[tuple, List[tuple]] = defaultdict(list)

    # shared_head: same head object + same edge_type but DIFFERENT normalised tail
    # Key: (src_oid, edge_type) → {norm_tgt: [rel_id, ...]}
    head_type_tails: Dict[tuple, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))

    # shared_tail: same edge_type + same tail object but DIFFERENT normalised head
    # Key: (edge_type, tgt_oid) → {norm_src: [rel_id, ...]}
    tail_type_heads: Dict[tuple, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))

    for rel in relations:
        rid = rel.get("id", "")
        src_oid = rel.get("source_object_id", "")
        tgt_oid = rel.get("target_object_id", "")
        edge_type = rel.get("edge_type", "")

        # Use _norm from edge_trust for name-normalised pair keys (same as
        # corroboration_counts uses triple_key).
        norm_src = _norm(obj_name.get(src_oid) or src_oid)
        norm_tgt = _norm(obj_name.get(tgt_oid) or tgt_oid)

        pair_rels[(norm_src, norm_tgt)].append((rid, edge_type))
        head_type_tails[(src_oid, edge_type)][norm_tgt].append(rid)
        tail_type_heads[(edge_type, tgt_oid)][norm_src].append(rid)

    # Strategy 1 — shared_pair_diff_edge
    for (norm_src, norm_tgt), entries in pair_rels.items():
        # Group by edge_type; if more than one distinct type → conflict candidate
        by_type: Dict[str, List[str]] = defaultdict(list)
        for rid, etype in entries:
            by_type[etype].append(rid)
        etypes = list(by_type.keys())
        if len(etypes) < 2:
            continue
        # Emit one representative pair per distinct edge_type combination
        # (take first rel from each edge_type group to keep it sparse)
        reps = [by_type[et][0] for et in etypes]
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                _add("edge", reps[i], reps[j], "shared_pair_diff_edge")

    # Strategy 2a — shared_head (same head+edge_type, DIFFERENT normalised tail)
    # Two edges share (head, edge_type) but point to different normalised tails →
    # possible one-to-one relation pointing two ways.
    # We pick one representative rel per distinct tail-group and emit pairs
    # across groups (not within, since same-tail rels are corroboration).
    for (src_oid, edge_type), tail_groups in head_type_tails.items():
        if len(tail_groups) < 2:
            # All rels under this (head, edge_type) go to the same normalised tail
            # → they are corroborating, not conflicting.
            continue
        all_groups = list(tail_groups.items())
        if len(all_groups) > _MAX_GROUP_REPS:
            _log.debug(
                "shared_head cap: (%s, %s) has %d tail groups, truncating to %d reps",
                src_oid, edge_type, len(all_groups), _MAX_GROUP_REPS,
            )
        # Take exactly _MAX_GROUP_REPS groups (no +1 overshoot)
        reps = [rids[0] for _norm_tgt, rids in all_groups[:_MAX_GROUP_REPS]]
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                _add("edge", reps[i], reps[j], "shared_head")

    # Strategy 2b — shared_tail (same edge_type+tail, DIFFERENT normalised head)
    for (edge_type, tgt_oid), head_groups in tail_type_heads.items():
        if len(head_groups) < 2:
            continue
        all_groups = list(head_groups.items())
        if len(all_groups) > _MAX_GROUP_REPS:
            _log.debug(
                "shared_tail cap: (%s, %s) has %d head groups, truncating to %d reps",
                edge_type, tgt_oid, len(all_groups), _MAX_GROUP_REPS,
            )
        reps = [rids[0] for _norm_src, rids in all_groups[:_MAX_GROUP_REPS]]
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                _add("edge", reps[i], reps[j], "shared_tail")

    # ── Strategy 3: Node candidates — discriminative ──────────────────────────
    # Filter to Concept/Claim only; check pairs via _discriminative_conflict.
    # _discriminative_conflict uses kg_merge._norm internally (slightly different
    # from edge_trust._norm — intentional, each module has its own normaliser).
    # Case-fold so both production lowercase ("concept"/"claim") and legacy
    # capitalized synthetic test data ("Concept"/"Claim") pass the filter.
    node_objects = [
        o for o in objects
        if (o.get("object_type") or "").lower() in _NODE_CONFLICT_TYPES
    ]

    # Cap overall emitted node pairs for the discriminative pass to keep it sparse.
    _MAX_DISC_PAIRS = 200  # sparsity cap for discriminative
    disc_count = 0

    for i in range(len(node_objects)):
        if disc_count >= _MAX_DISC_PAIRS:
            _log.debug(
                "discriminative cap reached (%d); skipping remaining node pairs",
                _MAX_DISC_PAIRS,
            )
            break
        oi = node_objects[i]
        pi = oi.get("payload") or {}
        name_i = pi.get("name", "") if isinstance(pi, dict) else ""
        for j in range(i + 1, len(node_objects)):
            if disc_count >= _MAX_DISC_PAIRS:
                break
            oj = node_objects[j]
            pj = oj.get("payload") or {}
            name_j = pj.get("name", "") if isinstance(pj, dict) else ""
            if not _discriminative_conflict(name_i, name_j):
                continue

            _add("node", oi["id"], oj["id"], "discriminative")
            disc_count += 1

    # ── Strategy 4: Node candidates — semantic (independent) ─────────────────
    # Emit a candidate for any same-object_type Concept/Claim pair whose
    # embeddings have cosine ≥ sim_threshold — regardless of _discriminative_conflict.
    # Near-identical embeddings mean both objects discuss the same subject, which is
    # a necessary precondition for conflict.  The LLM adjudicator will filter mere
    # agreements.
    #
    # Dedup note: _add() already tracks seen pairs.  If a pair was already emitted
    # as "discriminative", we need to upgrade it to "semantic" (stronger evidence).
    # We handle this by tracking which discriminative pairs also qualify semantically
    # and re-emitting them with the "semantic" signal — _add() deduplicates by
    # frozenset, so we must check the seen set ourselves for the upgrade path.
    #
    # Performance note (brute-force O(n^2) cosine): fine for typical notebook KG
    # sizes (hundreds of nodes).  For very large KGs, the upgrade path is hnswlib
    # ANN via `_ann_candidates` in kg_merge.py.
    if embeddings is not None:
        _MAX_SEM_PAIRS = 500  # sparsity cap for semantic pass
        sem_count = 0

        # Group nodes by object_type so we only compare same-type pairs.
        # Case-fold the key so "Claim" and "claim" land in the same group.
        by_type: Dict[str, List[dict]] = defaultdict(list)
        for o in node_objects:
            by_type[(o.get("object_type") or "").lower()].append(o)

        for _otype, type_nodes in by_type.items():
            if sem_count >= _MAX_SEM_PAIRS:
                break
            for i in range(len(type_nodes)):
                if sem_count >= _MAX_SEM_PAIRS:
                    _log.debug(
                        "semantic cap reached (%d); skipping remaining pairs",
                        _MAX_SEM_PAIRS,
                    )
                    break
                oi = type_nodes[i]
                vi = embeddings.get(oi["id"])
                if vi is None:
                    continue
                for j in range(i + 1, len(type_nodes)):
                    if sem_count >= _MAX_SEM_PAIRS:
                        break
                    oj = type_nodes[j]
                    vj = embeddings.get(oj["id"])
                    if vj is None:
                        continue
                    if _cosine_sim(vi, vj) < sim_threshold:
                        continue

                    # This pair qualifies for semantic.  If already emitted as
                    # discriminative, upgrade the signal to "semantic".
                    key = frozenset([oi["id"], oj["id"]])
                    if key in seen:
                        # Find the existing entry and upgrade its signal.
                        for c in candidates:
                            if (frozenset([c["left_ref"], c["right_ref"]]) == key
                                    and c["signal"] == "discriminative"):
                                c["signal"] = "semantic"
                                break
                    else:
                        _add("node", oi["id"], oj["id"], "semantic")
                    sem_count += 1

    return candidates


def _cosine_sim(va: List[float], vb: List[float]) -> float:
    """Compute cosine similarity between two plain-list vectors."""
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
