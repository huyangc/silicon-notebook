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

import heapq
import itertools
import logging
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import (
    Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union,
)

import numpy as np

# Reuse the canonical name-normaliser from edge_trust — avoids re-implementing.
from app.services.kg.edge_trust import _norm

# Intentional reuse of private helpers from kg_merge — coupling note: if
# _discriminative_conflict / _CONTRAST_GROUPS / _norm are renamed or moved in
# kg_merge.py, update this import.  ``_kg_merge_norm`` is kg_merge's own
# normaliser (deliberately different from edge_trust's ``_norm`` above); the
# discriminative bucket index below MUST tokenise with exactly the normaliser
# ``_discriminative_conflict`` uses internally, or the index and the predicate
# would disagree.
from app.services.kg_merge import (
    _CONTRAST_GROUPS,
    _discriminative_conflict,
    _norm as _kg_merge_norm,
)

_log = logging.getLogger(__name__)

# Defaults for the semantic strategy's size dispatch.  Production passes the
# deployment Settings values; these keep the pure function callable standalone
# (tests, offline tools) with the same behaviour as the shipped defaults.
_DEFAULT_SEMANTIC_BRUTEFORCE_MAX = 2000
_DEFAULT_SEMANTIC_ANN_K = 10

# Cap on the number of representative relations taken from each tail/head group for
# shared_head / shared_tail strategies.  Exactly 10 groups are taken; pairs are
# emitted from these representatives only (C(10,2)=45 max per group-key).
_MAX_GROUP_REPS = 10

# Sparsity cap on emitted semantic pairs, and the bound on how many distinct
# pairs the ANN branch will collect before it stops scanning neighbour rows.
# The collection bound only has to stay comfortably above what can be emitted.
_MAX_SEM_PAIRS = 500
_MAX_ANN_PAIR_COLLECTION = _MAX_SEM_PAIRS * 4

# Object types whose node-level conflict checks are meaningful.
# Stored lowercase in production (kg_ingest stores node.type.lower()); kept as
# lowercase here and matched case-folded so both legacy capitalized synthetic data
# and production lowercase data work.
_NODE_CONFLICT_TYPES = {"concept", "claim"}

# token → indices of the contrast groups containing it.  Built once so the
# bucket index below costs O(tokens in the name) instead of O(groups × tokens).
_TOKEN_GROUPS: Dict[str, Tuple[int, ...]] = {}
for _group_index, _group in enumerate(_CONTRAST_GROUPS):
    for _token in _group:
        _TOKEN_GROUPS[_token] = _TOKEN_GROUPS.get(_token, ()) + (_group_index,)
del _group_index, _group, _token


# ---------------------------------------------------------------------------
# Embeddings — one dense matrix, not a dict of Python float lists
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingMatrix:
    """``{object_id: vector}`` backed by one ``(N, dim)`` float32 array.

    A dict of Python float lists costs roughly 8 KB per 1024-d vector once the
    list object, the pointer array and the boxed floats are counted — about
    5 GB at 150k objects, which is the shape this pass used to load into memory
    before doing anything else.  The same data as one float32 matrix is
    ``N × dim × 4`` bytes: 0.6 GB at 150k × 1024, and rows are views into it.

    Exposes just enough of the mapping protocol (``get`` / ``items`` /
    ``in`` / ``len``) for callers that only want "the vector for this id",
    while the strategies that can go faster read ``matrix``/``row_by_id``
    directly.
    """

    matrix: np.ndarray
    row_by_id: Dict[str, int]

    @classmethod
    def from_mapping(
        cls, mapping: Mapping[str, Sequence[float]]
    ) -> "EmbeddingMatrix":
        """Build from a plain ``{id: [floats]}`` (tests, offline callers).

        Mixed dimensions cannot be represented by one matrix: the first vector's
        dimension wins and differently sized ones are dropped, which makes them
        behave as "no vector at all".  The previous per-pair code scored a
        mixed-dim pair 0.0 (never a candidate), so this only changes the fate of
        pairs where BOTH sides were off-dimension.  Production never reaches that
        branch — ``resolve_notebook_conflicts`` filters to one storage dimension
        before building this.
        """
        ids: List[str] = []
        dim: Optional[int] = None
        for object_id, vector in mapping.items():
            if vector is None:
                continue
            if dim is None:
                dim = len(vector)
            elif len(vector) != dim:
                continue
            ids.append(object_id)
        if dim is None or not ids:
            return cls(np.zeros((0, 0), dtype=np.float32), {})
        matrix = np.empty((len(ids), dim), dtype=np.float32)
        row_by_id: Dict[str, int] = {}
        row = 0
        for object_id, vector in mapping.items():
            if vector is None or len(vector) != dim:
                continue
            matrix[row] = vector
            row_by_id[object_id] = row
            row += 1
        return cls(matrix, row_by_id)

    def get(self, object_id: str):
        row = self.row_by_id.get(object_id)
        return None if row is None else self.matrix[row]

    def items(self):
        for object_id, row in self.row_by_id.items():
            yield object_id, self.matrix[row]

    def __contains__(self, object_id: object) -> bool:
        return object_id in self.row_by_id

    def __len__(self) -> int:
        return len(self.row_by_id)


Embeddings = Union[Mapping[str, Sequence[float]], EmbeddingMatrix]


def _as_embedding_matrix(embeddings: Embeddings) -> EmbeddingMatrix:
    if isinstance(embeddings, EmbeddingMatrix):
        return embeddings
    return EmbeddingMatrix.from_mapping(embeddings)


# ---------------------------------------------------------------------------
# Strategy 3 support — lossless inverted index over contrast-group tokens
# ---------------------------------------------------------------------------
#
# ``_discriminative_conflict(a, b)`` is true  ⟺  there exists a contrast group
# ``g`` and tokens ``ta ∈ Counter(tokens_a)``, ``tb ∈ Counter(tokens_b)`` with
# ``ta ≠ tb``, ``ta, tb ∈ g`` and ``Counter(tokens_a) - {ta} == Counter(tokens_b) - {tb}``.
#
# Proof of the rewrite (both directions):
#   (⇒) ``Ca - Cb == {ta}`` and ``Cb - Ca == {tb}``  ⟹  ``Ca = (Ca ∩ Cb) + {ta}``
#       and ``Cb = (Ca ∩ Cb) + {tb}``, hence ``Ca - {ta} == Ca ∩ Cb == Cb - {tb}``.
#   (⇐) With ``R := Ca - {ta} == Cb - {tb}`` and ``ta ≠ tb`` we get ``Ca = R + {ta}``,
#       ``Cb = R + {tb}``, so ``Ca - Cb == {ta}`` and ``Cb - Ca == {tb}`` — exactly
#       the predicate's ``len(only_a) == len(only_b) == 1`` branch.
#
# So bucketing on ``(group index, sorted remainder multiset)`` and pairing
# entries whose contrast token differs yields EXACTLY the pair set the O(N²)
# double loop accepted.  The pairs are then walked in ``(i, j)`` order — the
# double loop's own discovery order — and each is re-verified with the original
# predicate before emission, so this is a bit-for-bit equivalent rewrite that
# merely stops paying for the (overwhelmingly common) non-matching pairs.

# The key stores a HASH of the remainder multiset, not the multiset itself: at
# 200k objects the retained tuples of interned-but-still-referenced token strings
# cost hundreds of MB, while an int costs nothing.  Hashing is safe here because
# the key only decides which pairs are WORTH CHECKING — every pair is still
# re-verified with the original predicate before emission, so a collision costs
# one wasted predicate call and can never emit a pair the old loop rejected.
# Equal remainders always hash equally, so no true pair is ever missed.
_DiscKey = Tuple[int, int]
_DiscBuckets = Dict[_DiscKey, Dict[str, List[int]]]
_DiscOwnKeys = List[Tuple[_DiscKey, str]]


def _discriminative_buckets(
    names: List[str],
) -> Tuple[_DiscBuckets, List[_DiscOwnKeys]]:
    """Build the contrast-token inverted index for ``names`` (positional).

    Returns ``(buckets, keys_by_index)`` where ``buckets[key][token]`` is the
    ascending list of object indices carrying ``token`` with that remainder, and
    ``keys_by_index[i]`` lists ``(key, token)`` pairs object ``i`` belongs to.
    """
    buckets: _DiscBuckets = defaultdict(lambda: defaultdict(list))
    keys_by_index: List[_DiscOwnKeys] = []
    for index, name in enumerate(names):
        tokens = Counter(_kg_merge_norm(name).split())
        own: _DiscOwnKeys = []
        for token in tokens:
            groups = _TOKEN_GROUPS.get(token)
            if not groups:
                continue
            # The tuple is transient — only its hash is retained.
            remainder = hash(tuple(sorted((tokens - Counter([token])).elements())))
            for group_index in groups:
                key = (group_index, remainder)
                buckets[key][token].append(index)
                own.append((key, token))
        keys_by_index.append(own)
    return buckets, keys_by_index


def _discriminative_partners(
    index: int,
    buckets: _DiscBuckets,
    own_keys: _DiscOwnKeys,
) -> Iterable[int]:
    """Yield, ascending and deduplicated, the partners ``j > index`` of ``index``.

    Lazy on purpose: a pathological bucket (thousands of ``nmos`` vs thousands of
    ``pmos`` under the same remainder) holds a genuinely quadratic candidate set,
    and the caller stops at ``_MAX_DISC_PAIRS``.  Materialising the partner list
    first would reintroduce the blow-up this rewrite exists to remove, so the
    per-token sublists (already ascending) are merged lazily instead.
    """
    streams: List[Iterable[int]] = []
    for key, token in own_keys:
        by_token = buckets.get(key)
        if by_token is None or len(by_token) < 2:
            # Single-token bucket: every member carries the same contrast token,
            # so no member of it can pair with any other.  O(1) skip keeps a big
            # same-name cohort from being rescanned once per member.
            continue
        for other_token, members in by_token.items():
            if other_token == token:
                continue
            start = bisect_right(members, index)
            if start < len(members):
                streams.append(itertools.islice(members, start, None))
    if not streams:
        return ()
    return _dedup_sorted(heapq.merge(*streams))


def _dedup_sorted(values: Iterable[int]) -> Iterable[int]:
    """Drop repeats from an ascending stream.

    Repeats only occur when two objects share more than one contrast group under
    the same remainder (e.g. a name carrying both ``high`` and ``input``), so
    this is a rare-case guard rather than the common path.
    """
    previous: Optional[int] = None
    for value in values:
        if value != previous:
            yield value
            previous = value


# ---------------------------------------------------------------------------
# Strategy 4 support — ANN pairs for oversized same-type groups
# ---------------------------------------------------------------------------


def _semantic_ann_pairs(
    type_nodes: List[dict],
    embeddings: Embeddings,
    sim_threshold: float,
    ann_k: int,
    *,
    on_failure: Optional[Callable[[int], None]] = None,
) -> Optional[List[Tuple[int, int]]]:
    """Approximate the brute-force cosine pass with an hnswlib cosine index.

    Returns ascending ``(i, j)`` index pairs (positions within ``type_nodes``)
    whose cosine similarity is ``>= sim_threshold``, or ``None`` when the ANN
    pass could not run — the caller then skips the semantic strategy for this
    group rather than falling back to the quadratic scan.

    **Registered behaviour difference**: this is approximate recall.  It only
    engages above ``kg_conflict_semantic_bruteforce_max`` (groups at or below
    that size keep the exact, bit-for-bit unchanged brute-force path), where the
    exact pass is not something production can afford to finish at all.
    """
    try:
        import hnswlib

        table = _as_embedding_matrix(embeddings)
        indexed: List[int] = []
        rows: List[int] = []
        for position, node in enumerate(type_nodes):
            row = table.row_by_id.get(node["id"])
            if row is None:
                continue
            indexed.append(position)
            rows.append(row)

        count = len(indexed)
        if count < 2:
            return []

        matrix = table.matrix[rows]
        dim = int(matrix.shape[1])
        index = hnswlib.Index(space="cosine", dim=dim)
        index.init_index(max_elements=count, ef_construction=200, M=16, random_seed=42)
        index.set_num_threads(1)
        index.add_items(matrix, np.arange(count))
        index.set_ef(max(64, ann_k + 32))
        neighbours = min(ann_k + 1, count)  # +1 absorbs the self hit
        labels, distances = index.knn_query(matrix, k=neighbours)
    except Exception:  # noqa: BLE001 — fail-open: recall strategy, never fatal
        _log.warning(
            "semantic ANN pass unavailable for a %d-node group; "
            "skipping the semantic strategy for it",
            len(type_nodes),
            exc_info=True,
        )
        if on_failure is not None:
            try:
                on_failure(len(type_nodes))
            except Exception:  # noqa: BLE001 — observability must not escalate
                _log.debug("semantic ANN failure sink raised", exc_info=True)
        return None

    return _collect_ann_pairs(labels, distances, indexed, sim_threshold)


def _collect_ann_pairs(
    labels, distances, indexed: List[int], sim_threshold: float
) -> List[Tuple[int, int]]:
    """Collect ``(i, j)`` ascending from an already-computed neighbour query.

    **Both directions are read.** kNN membership is not symmetric: B's neighbour
    list can contain A while A's does not contain B (A simply has k closer
    neighbours). An earlier version deduplicated by emitting a pair only from the
    lower-positioned side, which silently dropped every qualifying pair that only
    the higher-positioned node reported — a recall hole wider than the
    approximation this strategy actually declares. So every row contributes its
    qualifying partners as a canonical ``(min, max)`` pair and a set does the
    deduplication.

    The set is what needs bounding, so collection stops at
    ``_MAX_ANN_PAIR_COLLECTION``: the caller emits at most ``_MAX_SEM_PAIRS``
    anyway, and four times that keeps the retained set — and the sort over it —
    trivial (a few thousand tuples) while still refusing to materialise the
    ``count × k`` result set that made this pass a hundreds-of-MB allocation.
    Stopping early is inside the ANN approximation already disclosed for this
    branch.
    """
    pairs: set = set()
    truncated = False
    for row, position in enumerate(indexed):
        for label, distance in zip(labels[row], distances[row]):
            other = int(label)
            if other == row:
                continue
            if 1.0 - float(distance) < sim_threshold:
                continue
            partner = indexed[other]
            pair = (position, partner) if position < partner else (partner, position)
            if pair in pairs:
                continue
            if len(pairs) >= _MAX_ANN_PAIR_COLLECTION:
                truncated = True
                break
            pairs.add(pair)
        if truncated:
            break
    if truncated:
        _log.debug(
            "semantic ANN collection cap reached (%d); remaining neighbour "
            "rows were not scanned",
            _MAX_ANN_PAIR_COLLECTION,
        )
    return sorted(pairs)


def detect_conflict_candidates(
    objects: List[dict],
    relations: List[dict],
    *,
    embeddings: Optional[Embeddings] = None,
    sim_threshold: float = 0.8,
    semantic_bruteforce_max: int = _DEFAULT_SEMANTIC_BRUTEFORCE_MAX,
    semantic_ann_k: int = _DEFAULT_SEMANTIC_ANN_K,
    on_semantic_ann_failure: Optional[Callable[[int], None]] = None,
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
        Optional vectors for the semantic strategy — either an
        ``EmbeddingMatrix`` (what production passes) or a plain
        ``{object_id: list[float]}`` mapping, which is converted to one.
        If None, the semantic strategy is skipped entirely.
    sim_threshold:
        Cosine similarity threshold for the semantic strategy (default 0.8).
    semantic_bruteforce_max:
        Same-``object_type`` group size at or below which the semantic strategy
        keeps its exact pairwise cosine scan.  Above it the group is served by an
        approximate hnswlib cosine ANN pass (see ``_semantic_ann_pairs``).
    semantic_ann_k:
        Neighbours per node requested from that ANN pass.
    on_semantic_ann_failure:
        Optional sink called with the group size when that ANN pass fails, so a
        caller that knows the notebook can record it.  This module stays free of
        notebook identity.

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

    # Bounded rewrite of the original O(N²) double loop: the inverted index over
    # (contrast group, remainder multiset) yields exactly the pairs the predicate
    # accepts (equivalence proof at ``_discriminative_buckets``), walked in the
    # same (i, j) order and re-verified with the original predicate before every
    # emission.  The cap and its bookkeeping stay byte-identical to the old loop.
    node_names: List[str] = []
    for _node in node_objects:
        _payload = _node.get("payload") or {}
        node_names.append(
            _payload.get("name", "") if isinstance(_payload, dict) else ""
        )
    disc_buckets, disc_keys_by_index = _discriminative_buckets(node_names)

    for i in range(len(node_objects)):
        if disc_count >= _MAX_DISC_PAIRS:
            _log.debug(
                "discriminative cap reached (%d); skipping remaining node pairs",
                _MAX_DISC_PAIRS,
            )
            break
        oi = node_objects[i]
        name_i = node_names[i]
        for j in _discriminative_partners(i, disc_buckets, disc_keys_by_index[i]):
            if disc_count >= _MAX_DISC_PAIRS:
                break
            oj = node_objects[j]
            name_j = node_names[j]
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
    # Performance note: the brute-force O(n^2) cosine is kept verbatim for groups
    # of at most ``semantic_bruteforce_max`` nodes (typical notebook KGs), which is
    # where the exact behaviour must not move.  Larger groups are served by the
    # hnswlib cosine ANN pass — approximate recall, registered as an accepted
    # difference, and the only way those graphs finish at all.
    if embeddings is not None:
        # _MAX_SEM_PAIRS (sparsity cap) lives at module level: the ANN branch's
        # collection bound is derived from it.
        sem_count = 0
        vectors = _as_embedding_matrix(embeddings)

        def _emit_semantic(left_obj: dict, right_obj: dict) -> None:
            """Emit (or upgrade) one semantic pair — shared by both branches."""
            key = frozenset([left_obj["id"], right_obj["id"]])
            if key in seen:
                # Find the existing entry and upgrade its signal.
                for c in candidates:
                    if (frozenset([c["left_ref"], c["right_ref"]]) == key
                            and c["signal"] == "discriminative"):
                        c["signal"] = "semantic"
                        break
            else:
                _add("node", left_obj["id"], right_obj["id"], "semantic")

        # Group nodes by object_type so we only compare same-type pairs.
        # Case-fold the key so "Claim" and "claim" land in the same group.
        by_type: Dict[str, List[dict]] = defaultdict(list)
        for o in node_objects:
            by_type[(o.get("object_type") or "").lower()].append(o)

        for _otype, type_nodes in by_type.items():
            if sem_count >= _MAX_SEM_PAIRS:
                break
            if len(type_nodes) > semantic_bruteforce_max:
                ann_pairs = _semantic_ann_pairs(
                    type_nodes, vectors, sim_threshold, semantic_ann_k,
                    on_failure=on_semantic_ann_failure,
                )
                if ann_pairs is None:
                    # ANN unavailable — skip this group rather than falling back
                    # to the quadratic scan this dispatch exists to avoid.
                    continue
                for i, j in ann_pairs:
                    if sem_count >= _MAX_SEM_PAIRS:
                        _log.debug(
                            "semantic cap reached (%d); skipping remaining pairs",
                            _MAX_SEM_PAIRS,
                        )
                        break
                    # This pair qualifies for semantic.  If already emitted as
                    # discriminative, upgrade the signal to "semantic".
                    _emit_semantic(type_nodes[i], type_nodes[j])
                    sem_count += 1
                continue
            for i in range(len(type_nodes)):
                if sem_count >= _MAX_SEM_PAIRS:
                    _log.debug(
                        "semantic cap reached (%d); skipping remaining pairs",
                        _MAX_SEM_PAIRS,
                    )
                    break
                oi = type_nodes[i]
                vi = vectors.get(oi["id"])
                if vi is None:
                    continue
                for j in range(i + 1, len(type_nodes)):
                    if sem_count >= _MAX_SEM_PAIRS:
                        break
                    oj = type_nodes[j]
                    vj = vectors.get(oj["id"])
                    if vj is None:
                        continue
                    if _row_cosine(vi, vj) < sim_threshold:
                        continue

                    # This pair qualifies for semantic.  If already emitted as
                    # discriminative, upgrade the signal to "semantic".
                    _emit_semantic(oi, oj)
                    sem_count += 1

    return candidates


def _row_cosine(va: np.ndarray, vb: np.ndarray) -> float:
    """Cosine similarity between two ``EmbeddingMatrix`` rows.

    Accumulates in float64 (``np.dot`` on a float64 view) rather than summing
    boxed Python floats.  Against the previous ``_cosine_sim`` over lists the
    values differ only in summation order, i.e. around 1e-12 relative — far
    below any usable ``sim_threshold``, but it IS a numerical representation
    change and is registered as such: a pair sitting exactly on the threshold
    could in principle land on the other side of it.

    Mixed dimensions keep ``_cosine_sim``'s zero-tolerance contract; one matrix
    cannot hold mixed dimensions, so this only guards hand-built callers.
    """
    if len(va) != len(vb):
        _log.warning(
            "_row_cosine: dim mismatch %d vs %d — treating as 0.0 "
            "(mixed-dim residue should have been filtered upstream)",
            len(va), len(vb))
        return 0.0
    a = np.asarray(va, dtype=np.float64)
    b = np.asarray(vb, dtype=np.float64)
    denominator = math.sqrt(float(a @ a)) * math.sqrt(float(b @ b))
    if denominator == 0.0:
        return 0.0
    return float(a @ b) / denominator


def _cosine_sim(va: List[float], vb: List[float]) -> float:
    """Compute cosine similarity between two plain-list vectors.

    混维零容忍(runtime-dim 截断项目 T4):len 不等时 zip 会静默按短边截断算出
    错误相似度(≠0 的假信号)——显式返 0.0 并 warning。正常路径不该走到这里:
    resolve_notebook_conflicts 装载 embeddings 时已按存储维过滤+统一截断。"""
    if len(va) != len(vb):
        _log.warning(
            "_cosine_sim: dim mismatch %d vs %d — treating as 0.0 "
            "(mixed-dim residue should have been filtered upstream)",
            len(va), len(vb))
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
