"""KG 分析「问题清单」的**纯规则引擎**(设计 2026-10-19 §四 / §七 7.1)。

不碰连接、不拼 SQL、不判 dialect —— 与 `kg_analysis_precompute` 同一套做法:store 只
负责流式读与落库,规则全部在这里,两个后端因此结构性地拿到逐字相同的问题行。

**它挂在 `KnowledgeLifecycleService._compute_kg_analysis` 的哪几步**(规格 §7.1;T8 的
接线说明,签名见各类/函数的 docstring):

  ③ 折叠跨板块边**之前**:``structure = GraphStructure.from_edges(edge_table.src,
     edge_table.dst, edge_table.n_nodes)`` —— 连通分量(scipy csgraph)+ 度数,每节点
     只留两个 int32,CSR 临时结构当场释放。全量与补账本两条路径共用。
  ⑤ 与 `SourceBoardCounter` **同一趟**对象流:``tally = FindingsObjectTally(can2idx,
     structure)``,每行 ``tally.add(source_id, canonical_id, is_concept)``。零板块的库
     不建 `SourceBoardCounter`,但这一趟**照跑**(问题清单是必需产物)。
  ⑥ drain 之后、``can2idx.clear()`` 之前:``draft = compute_findings(...)``。它返回时
     已经把需要的下标全部还原成 canonical id,``can2idx`` 随即可以清空;之后按
     ``draft.pending()`` 给出的有界清单点查展示名与来源标题(写事务之外),
     ``draft.supply(...)`` 喂回,直到 ``pending()`` 为 ``None``,``draft.result()``
     取出问题行与账本 payload。
  ⑧ 发布事务里:读旧明细(有界主键范围读)+ 旧账本行,``finalize_findings`` 继承
     ``first_seen_*``、算对比摘要。

**规则顺序**(§四开头):F2 → F3 → F1 → F4 → F5。F2 先算,因为 F1 的「共享」要排除
重复伙伴;F3 先于 F1 判定,因为解析残缺的来源也会表现为「不和别人共享」,而重新解析
比删除可逆得多 —— F3 命中的来源不报 F1。

**确定性**:来源按 ``source_id`` 排序后编号,图外 concept canonical 按 id 排序后编号,
图内节点下标来自 `community_graph_rows` 的 ``ORDER BY kr.id``(两个后端同序)。所有
并列都按这些下标或 id 消歧;F4 的 squash 分桶用进程内哈希,但碰撞一律回到字符串
逐个核对,哈希的进程随机化不影响结果。

**单位**:载荷的每个数值叶子都在 `kg_findings_contracts.FINDINGS_PAYLOAD_UNITS` /
`FINDING_EVIDENCE_UNITS` 里有且只有一种单位(同一载荷里一个字段名只有一种含义)。
注意几处对象口径:F1 证据的 ``giant_share`` 与 S3 的 ``giant_object_share`` 数的都是
**对象**(该来源进了图的对象里落在主连通块的比例),不是 canonical —— 与校准用的
只读探针同口径。
"""
from __future__ import annotations

import re
from array import array
from dataclasses import dataclass
from fractions import Fraction
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import numpy as np

from app.domain.kg_analysis_contracts import (
    CLUSTER_SEQ_PAYLOAD_KEY,
    FINDING_ROW_FIELDS,
    MARK_DISMISSED,
)
from app.domain.kg_findings_contracts import (
    DECISION_APART_STATUSES,
    DECISION_CONFIRMED,
    DUP_J_MIN,
    F1_MIN_CONCEPTS,
    F1_SHARED_MAX,
    F3A_MIN_NAMES,
    F3A_MIN_SINGLE_CHAR_RUN,
    F3A_SHARE_MIN,
    F3B_ISOLATED_SHARE_FACTOR,
    F3B_ISOLATED_SHARE_FLOOR,
    F3B_MIN_ISOLATED,
    F3B_MIN_OBJECTS,
    F3B_RELATIONS_PER_OBJECT_FRACTION,
    F5_TOP_SOURCES,
    FINDING_RULES,
    MAX_MEMBER_SEEDS_PER_CANONICAL,
    MAX_PERSISTED_FINDINGS,
    MAX_RESOLVED_SAMPLES,
    MAX_SAMPLES,
    MAX_SUBJECT_REFS,
    NOTEBOOK_SUBJECT_KEY,
    RULE_DUPLICATE_IMPORT,
    RULE_ISOLATED_OBJECTS,
    RULE_OFF_TOPIC_SOURCE,
    RULE_PARSE_DAMAGE,
    RULE_SEED_DEFECT,
    RULE_SEVERITY,
    RULE_STATUS_RAN,
    RULE_STATUS_SKIPPED,
    RULE_SUBJECT_KIND,
    RULE_THRESHOLDS,
    RULE_UNDER_MERGE,
    RULES_VERSION,
    S1_EXAMPLES,
    SKIP_FEWER_THAN_TWO_SOURCES,
    SKIP_NO_NAMED_CONCEPTS,
    SKIP_NO_OBJECTS,
    UBIQUITOUS_SOURCE_COUNT,
    finding_sort_key,
    source_pair_ids,
    source_pair_key,
)
from app.domain.kg_merge_seed import (
    OVERRIDE_SEED_PREFIX,
    concept_seed_v1,
    concept_seed_v2,
    concept_squash_key,
    decision_match_key,
)

# Canonical ids of the groups a decision recompute kept in their v1 seed.
_OVERRIDE_CANONICAL_PREFIX = "K-" + OVERRIDE_SEED_PREFIX

# F3 sub-signal codes carried in the evidence (``signals``).
F3_SIGNAL_FRAGMENTED_NAMES = "fragmented_names"
F3_SIGNAL_LOW_RELATIONS = "low_relations"
F3_SIGNAL_HIGH_ISOLATED = "high_isolated"

# F3a: a run of ≥ N consecutive single-character tokens ("L l a m a 3 . 1").
# Same expression as the calibrated probe; N comes from the named threshold.
_FRAGMENTED_NAME_RE = re.compile(
    r"(?:(?<![^\s])[^\s](?:\s+|$)){%d,}" % F3A_MIN_SINGLE_CHAR_RUN
)

# F2 threshold as an exact rational (9/20): both the prefix length
# ``|A| - ceil(t·|A|) + 1`` and the verification ``inter/union >= t`` are integer
# arithmetic, so a float ``ceil`` can never shorten a prefix by one and lose a
# pair sitting exactly on the threshold. ``Fraction(repr(...))`` reads the
# decimal literal, not the binary float.
_DUP_J = Fraction(repr(DUP_J_MIN))

# Index dtypes, same reasoning as kg_analysis_precompute (§3.32): node / source
# ranks stay far below 2^31.
_INDEX_DTYPE = np.int32
_EMPTY_INDEX = np.empty(0, dtype=_INDEX_DTYPE)
# F2 verification batching — engineering bounds on transient memory, not
# result-changing limits (every candidate is verified):
#   · concepts expanded per step (≈ 25 B each across the step's temporaries,
#     so ≈ 26 MB at 1 << 20); a single pair larger than this is its own step;
#   · candidate pairs per chunk (the per-pair helper arrays are chunk-sized).
_VERIFY_ELEMENT_BUDGET = 1 << 20
_VERIFY_PAIR_CHUNK = 1 << 16
# Canonical ids per ``member_seeds`` call (the caller binds them into one IN
# list per call; same order as the store's other bounded batches).
_MEMBER_SEED_BATCH = 500
# S1 examples: candidates whose visible names are asked per lookup round, so
# that unreproducible examples cost a round per this many, not per 3.
_S1_LOOKAHEAD = 30
# F4 the same way: near the 500-row cap a round would otherwise ask only the
# few free slots, and a high drop rate (visible names not reproducing the
# group) would turn into one lookup round per handful of groups.
_F4_LOOKAHEAD = 64


# ============================================================ step ③ structure
class GraphStructure:
    """Connected components + degree of the canonical relation graph (step ③).

    Built from the **unfolded** `CanonicalEdgeTable` columns — unique undirected
    pairs, ``src <= dst``, no self loops — before ``fold_cross_community_edges``
    releases them. Keeps per node one int32 component label and one int32
    degree (distinct neighbours); the CSR matrix ``connected_components`` needs
    is a local and is gone when this returns.

    ``giant_label`` is the largest component (ties → the lowest label, i.e. the
    component holding the lowest node index — deterministic, since node indices
    follow ``ORDER BY kr.id`` on both backends); ``-1`` when the graph has no
    node. Every graph node has at least one non-self-loop edge by construction
    (`rebuild_communities` only numbers endpoints of such edges), so "not a node"
    is exactly "isolated" (F5).
    """

    __slots__ = (
        "n_nodes", "component", "degree", "giant_label", "giant_nodes",
        "components", "in_giant",
    )

    def __init__(
        self,
        n_nodes: int,
        component: "np.ndarray",
        degree: "np.ndarray",
        giant_label: int,
        giant_nodes: int,
        components: int,
        in_giant: bytes,
    ) -> None:
        self.n_nodes = int(n_nodes)
        self.component = component
        self.degree = degree
        self.giant_label = int(giant_label)
        self.giant_nodes = int(giant_nodes)
        self.components = int(components)
        # Python-indexable 0/1 per node: `FindingsObjectTally.add` reads it once
        # per object row, and a numpy scalar read there would be ~10× slower.
        self.in_giant = in_giant

    @classmethod
    def from_edges(
        cls, src: "np.ndarray", dst: "np.ndarray", n_nodes: int
    ) -> "GraphStructure":
        n = int(n_nodes)
        if n == 0:
            return cls(0, _EMPTY_INDEX, _EMPTY_INDEX, -1, 0, 0, b"")
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import connected_components

        src = np.asarray(src, dtype=_INDEX_DTYPE)
        dst = np.asarray(dst, dtype=_INDEX_DTYPE)
        matrix = csr_matrix(
            (np.ones(src.size, dtype=np.int8), (src, dst)), shape=(n, n)
        )
        count, labels = connected_components(matrix, directed=False)
        del matrix
        component = labels.astype(_INDEX_DTYPE, copy=False)
        del labels
        degree = np.bincount(src, minlength=n)
        degree += np.bincount(dst, minlength=n)
        degree = degree.astype(_INDEX_DTYPE, copy=False)
        sizes = np.bincount(component, minlength=int(count))
        giant = int(np.argmax(sizes))
        in_giant = (component == giant).astype(np.uint8).tobytes()
        return cls(n, component, degree, giant, int(sizes[giant]), int(count), in_giant)


def board_structure_counts(
    structure: GraphStructure, community_of_index: Sequence[Optional[str]],
) -> Dict[str, int]:
    """S3's two partition-dependent numbers (design §五 S3), for the
    ``community_edges`` payload — the findings payload never depends on boards:

      · ``boards_in_giant``: boards with at least one node in the giant component
        (the topics inside the main body);
      · ``boards_equal_component``: boards whose graph nodes are exactly one
        whole connected component (an island that is its own board — locally
        1,137 of 1,217).

    ``community_of_index``: board id per node index, as `_compute_kg_analysis`
    holds it (``None`` = not in a kept board). Entries past ``structure.n_nodes``
    are board members that are no longer graph nodes (backfill path): they are
    in no component and count for neither number. One pass over the nodes, then
    vectorised; nothing survives the call."""
    n = structure.n_nodes
    if n == 0:
        return {"boards_in_giant": 0, "boards_equal_component": 0}
    rank: Dict[str, int] = {}
    board = np.full(n, -1, dtype=np.int64)
    for index in range(n):
        cid = community_of_index[index]
        if cid is not None:
            board[index] = rank.setdefault(cid, len(rank))
    member = board >= 0
    boards = board[member]
    components = structure.component[member].astype(np.int64)
    del board, member
    if boards.size == 0:
        return {"boards_in_giant": 0, "boards_equal_component": 0}
    in_giant = int(np.unique(boards[components == structure.giant_label]).size)
    pairs = np.unique(boards * int(structure.components) + components)
    pair_board = pairs // int(structure.components)
    pair_component = pairs % int(structure.components)
    one_component = np.bincount(pair_board, minlength=len(rank)) == 1
    board_nodes = np.bincount(boards, minlength=len(rank))
    component_nodes = np.bincount(structure.component, minlength=int(structure.components))
    only = one_component[pair_board]
    equal = int(np.count_nonzero(
        board_nodes[pair_board[only]] == component_nodes[pair_component[only]]
    ))
    return {"boards_in_giant": in_giant, "boards_equal_component": equal}


# ================================================================ step ⑤ tally
class FindingsObjectTally:
    """Per-object fold of the `source_canonical_rows` stream (step ⑤).

    Fed **in the same loop** as `SourceBoardCounter` (one ``add`` each per row)
    and — unlike it — also on zero-board libraries. Per source it counts
    objects, isolated objects (canonical not a graph node), in-graph objects and
    objects in the giant component; per **concept** row it buffers two int32
    (source rank, concept index) in chunked columns, the same pattern as
    `SourceBoardCounter` (no per-row Python container survives the row).

    ⚠ It never grows ``can2idx``. A concept canonical that is not a graph node
    gets a side index ``n_nodes + k`` from its own dict, used only for F1/F2/F3a/
    F4 counting. "In the graph" is ``index < structure.n_nodes``, NOT "present in
    ``can2idx``": the backfill path extends ``can2idx`` with board members that are
    no longer graph nodes (see ``_compute_kg_analysis``).

    One-shot: `compute_findings` drains it.
    """

    _CHUNK = 1 << 16

    __slots__ = (
        "_can2idx", "_n_nodes", "_in_giant", "_source_ids", "_source_rank",
        "_objects", "_isolated", "_in_graph", "_giant", "_extra", "_full",
        "_src", "_can", "_fill", "_rows", "_drained",
    )

    def __init__(self, can2idx: Mapping[str, int], structure: GraphStructure) -> None:
        self._can2idx = can2idx
        self._n_nodes = structure.n_nodes
        self._in_giant = structure.in_giant
        self._source_ids: List[str] = []
        self._source_rank: Dict[str, int] = {}
        self._objects: List[int] = []
        self._isolated: List[int] = []
        self._in_graph: List[int] = []
        self._giant: List[int] = []
        self._extra: Dict[str, int] = {}
        self._full: List[List["np.ndarray"]] = []
        self._src = np.empty(self._CHUNK, dtype=_INDEX_DTYPE)
        self._can = np.empty(self._CHUNK, dtype=_INDEX_DTYPE)
        self._fill = 0
        self._rows = 0
        self._drained = False

    def add(self, source_id: str, canonical_id: str, is_concept: object) -> None:
        """One usable visible object: its source, its canonical, whether it is a concept."""
        rank = self._source_rank.get(source_id)
        if rank is None:
            rank = len(self._source_ids)
            self._source_rank[source_id] = rank
            self._source_ids.append(source_id)
            self._objects.append(0)
            self._isolated.append(0)
            self._in_graph.append(0)
            self._giant.append(0)
        self._objects[rank] += 1
        index = self._can2idx.get(canonical_id)
        if index is not None and index < self._n_nodes:
            self._in_graph[rank] += 1
            if self._in_giant[index]:
                self._giant[rank] += 1
        else:
            self._isolated[rank] += 1
            if not is_concept:
                return
            index = self._extra.get(canonical_id)
            if index is None:
                index = self._n_nodes + len(self._extra)
                self._extra[canonical_id] = index
        if not is_concept:
            return
        if self._fill == self._CHUNK:
            self._full.append([self._src, self._can])
            self._src = np.empty(self._CHUNK, dtype=_INDEX_DTYPE)
            self._can = np.empty(self._CHUNK, dtype=_INDEX_DTYPE)
            self._fill = 0
        self._src[self._fill] = rank
        self._can[self._fill] = index
        self._fill += 1
        self._rows += 1

    def _drain_column(self, column: int) -> "np.ndarray":
        """Concatenate one column, releasing each chunk as it is copied."""
        tail = (self._src if column == 0 else self._can)[: self._fill]
        joined = np.empty(self._rows, dtype=_INDEX_DTYPE)
        at = 0
        for pair in self._full:
            chunk = pair[column]
            joined[at:at + chunk.size] = chunk
            pair[column] = _EMPTY_INDEX
            at += chunk.size
        joined[at:at + tail.size] = tail
        if column == 0:
            self._src = _EMPTY_INDEX
        else:
            self._can = _EMPTY_INDEX
        return joined


# ============================================================ drained corpus
@dataclass
class _Corpus:
    """The drained tally in canonical order (sources by id, side indices by id)."""

    source_ids: List[str]
    objects: "np.ndarray"          # int64 [S] usable visible objects per source
    isolated: "np.ndarray"         # int64 [S] objects whose canonical is not a graph node
    in_graph: "np.ndarray"         # int64 [S]
    giant: "np.ndarray"            # int64 [S]
    n_nodes: int
    extra_ids: List[str]           # side index n_nodes + k  ->  canonical id
    extra_rank: Dict[str, int]     # canonical id -> side index (the tally's own dict, renumbered)
    stride: int                    # n_nodes + len(extra_ids)
    inc_src: "np.ndarray"          # int32 unique (source, concept) incidences,
    inc_can: "np.ndarray"          #   sorted by (source rank, concept index)
    inc_objects: "np.ndarray"      # int32 objects behind each incidence
    indptr: "np.ndarray"           # int64 [S+1] incidence range per source
    df: "np.ndarray"               # int32 [stride] sources containing the concept
    concepts: "np.ndarray"         # int64 [S] distinct concept canonicals per source
    can_objects: "np.ndarray"      # int64 [stride] visible usable objects per concept canonical
    degree: "np.ndarray"           # int32 [stride] graph degree (0 off-graph)


def _group_starts(sorted_keys: "np.ndarray") -> "np.ndarray":
    size = int(sorted_keys.size)
    if size == 0:
        return np.empty(0, dtype=np.intp)
    is_start = np.empty(size, dtype=bool)
    is_start[0] = True
    np.not_equal(sorted_keys[1:], sorted_keys[:-1], out=is_start[1:])
    return np.flatnonzero(is_start)


def _drain_tally(tally: FindingsObjectTally, structure: GraphStructure) -> _Corpus:
    if tally._drained:
        raise RuntimeError(
            "FindingsObjectTally 只能消化一次:攒下的块已经被消化掉,再算一次只会拿到"
            "空结果 —— 而空结果意味着「一个对象都没有」"
        )
    tally._drained = True
    count = len(tally._source_ids)
    order = sorted(range(count), key=tally._source_ids.__getitem__)
    take = np.asarray(order, dtype=np.int64)
    new_rank = np.empty(count, dtype=_INDEX_DTYPE)
    new_rank[take] = np.arange(count, dtype=_INDEX_DTYPE)
    source_ids = [tally._source_ids[i] for i in order]

    def per_source(values: List[int]) -> "np.ndarray":
        return np.asarray(values, dtype=np.int64)[take] if count else np.zeros(0, np.int64)

    objects = per_source(tally._objects)
    isolated = per_source(tally._isolated)
    in_graph = per_source(tally._in_graph)
    giant = per_source(tally._giant)
    n_nodes = tally._n_nodes
    extra_rank = tally._extra
    tally._extra = {}
    extra_ids = sorted(extra_rank)
    stride = n_nodes + len(extra_ids)
    remap = np.empty(len(extra_ids), dtype=_INDEX_DTYPE)
    for position, canonical_id in enumerate(extra_ids):
        remap[extra_rank[canonical_id] - n_nodes] = n_nodes + position
        # Renumber in place: the names pass reuses this dict (no second map).
        extra_rank[canonical_id] = n_nodes + position
    rows = int(tally._rows)
    src = tally._drain_column(0)
    can = tally._drain_column(1)
    tally._source_ids, tally._source_rank = [], {}
    tally._objects = tally._isolated = tally._in_graph = tally._giant = []
    if rows:
        src = new_rank[src]
        off_graph = can >= n_nodes
        can[off_graph] = remap[can[off_graph] - n_nodes]
        del off_graph
    can_objects = np.bincount(can, minlength=stride).astype(np.int64)
    width = max(stride, 1)
    key = src.astype(np.int64)
    del src
    key *= width
    key += can
    del can
    key.sort()
    starts = _group_starts(key)
    unique_keys = key[starts]
    inc_objects = np.diff(starts, append=rows).astype(_INDEX_DTYPE)
    del key, starts
    inc_src = (unique_keys // width).astype(_INDEX_DTYPE)
    inc_can = (unique_keys % width).astype(_INDEX_DTYPE)
    del unique_keys
    indptr = np.searchsorted(inc_src, np.arange(count + 1)).astype(np.int64)
    df = np.bincount(inc_can, minlength=stride).astype(_INDEX_DTYPE)
    concepts = np.diff(indptr)
    degree = np.zeros(stride, dtype=_INDEX_DTYPE)
    if n_nodes:
        degree[:n_nodes] = structure.degree
    return _Corpus(
        source_ids, objects, isolated, in_graph, giant, n_nodes, extra_ids, extra_rank,
        stride, inc_src, inc_can, inc_objects, indptr, df, concepts, can_objects, degree,
    )


def _source_tokens(corpus: _Corpus, rank: int) -> "np.ndarray":
    return corpus.inc_can[corpus.indptr[rank]:corpus.indptr[rank + 1]]


def _top_by_degree(corpus: _Corpus, indices: "np.ndarray", limit: int) -> List[int]:
    """``limit`` concept indices by degree desc, index asc (deterministic)."""
    indices = np.asarray(indices, dtype=np.int64)
    if indices.size == 0:
        return []
    order = np.lexsort((indices, -corpus.degree[indices].astype(np.int64)))
    return indices[order[:limit]].tolist()


# ================================================================== F2
@dataclass
class _DuplicatePairs:
    pairs: List[Tuple[int, int, int, int]]   # (rank a, rank b, |A∩B|, |A∪B|), a < b
    candidate_pairs: int                     # distinct pairs verified
    candidate_generations: int               # pairs generated, repeats included (the capped count)
    candidates_truncated: bool
    universe_can: "np.ndarray"               # universe incidences, sorted (source, concept)
    universe_indptr: "np.ndarray"


def _prefix_lengths(sizes: "np.ndarray") -> "np.ndarray":
    """``|A| - ceil(t·|A|) + 1`` per source, exactly (t = 9/20), 0 for empty sets."""
    num, den = _DUP_J.numerator, _DUP_J.denominator
    alpha = (num * sizes + den - 1) // den
    return np.where(sizes > 0, sizes - alpha + 1, 0)


def _candidate_pairs(
    p_src: "np.ndarray", p_can: "np.ndarray", df: "np.ndarray",
    n_sources: int, cap: int,
) -> Tuple["np.ndarray", "np.ndarray", int, bool]:
    """All pairs of sources sharing a prefix concept, generation capped at ``cap``.

    Returns ``(a, b, generated, truncated)``: the distinct pairs (``a < b``), and
    how many pairs were GENERATED — repeats included, one per shared prefix
    concept. ``cap`` bounds the generated count (that is what costs memory), so
    the ledger reports it against the limit, never the distinct count.

    Inverted lists are visited in the global order (document frequency asc,
    concept index asc), so the cap cuts the same lists on every run and backend.
    Like the calibrated probe, the list that would push the running count over
    the cap stops generation (``truncated``); every list before it is complete.
    Generation is vectorised; transient memory measured at ≈ 25 B per generated
    pair (``test_candidate_generation_memory_per_pair`` bounds it at 32 B).
    """
    if p_src.size == 0:
        return _EMPTY_INDEX, _EMPTY_INDEX, 0, False
    order = np.lexsort((p_src, p_can, df[p_can]))
    p_src = p_src[order]
    p_can = p_can[order]
    del order
    starts = _group_starts(p_can)
    sizes = np.diff(starts, append=p_can.size).astype(np.int64)
    running = np.cumsum(sizes * (sizes - 1) // 2)
    within = running <= int(cap)
    truncated = not bool(within.all())
    kept_lists = int(np.argmin(within)) if truncated else int(within.size)
    if kept_lists == 0:
        return _EMPTY_INDEX, _EMPTY_INDEX, 0, truncated
    generated = int(running[kept_lists - 1])
    del running, within
    end = int(starts[kept_lists]) if kept_lists < starts.size else int(p_src.size)
    members = p_src[:end].astype(np.int64)
    list_end = np.repeat(starts[:kept_lists] + sizes[:kept_lists], sizes[:kept_lists])
    position = np.arange(end, dtype=np.int64)
    after = list_end - position - 1
    del list_end
    total = int(after.sum())
    if total == 0:
        return _EMPTY_INDEX, _EMPTY_INDEX, 0, truncated
    left = np.repeat(members, after)
    first = np.cumsum(after) - after
    right_position = np.repeat(position + 1 - first, after)
    del first, position
    right_position += np.arange(total, dtype=np.int64)
    right = members[right_position]
    del right_position, after, members
    width = max(n_sources, 1)
    key = left * width
    key += right
    del left, right
    key.sort()
    # A boolean mask, not `_group_starts`' index array: 1 byte per generated pair
    # instead of 8 at the peak.
    first_of_run = np.empty(key.size, dtype=bool)
    first_of_run[0] = True
    np.not_equal(key[1:], key[:-1], out=first_of_run[1:])
    unique = key[first_of_run]
    del key, first_of_run
    return (
        (unique // width).astype(_INDEX_DTYPE),
        (unique % width).astype(_INDEX_DTYPE),
        generated,
        truncated,
    )


def _verify_pairs(
    cand_a: "np.ndarray", cand_b: "np.ndarray", universe_can: "np.ndarray",
    indptr: "np.ndarray", sizes: "np.ndarray", width: int,
) -> List[Tuple[int, int, int, int]]:
    """Exact ``|A∩B|`` for every candidate, in batches bounded by ELEMENTS.

    For each pair only the SMALLER set is expanded and each of its concepts is
    looked up (one vectorised ``searchsorted``) in the sorted universe keys
    ``source * width + concept`` of the other source — the larger source's row is
    never copied, so one big source with many partners costs its partners'
    sizes, not its own size per partner. A batch takes pairs until the expanded
    elements reach ``_VERIFY_ELEMENT_BUDGET`` (a single pair larger than the budget
    is its own batch); ``test_verification_memory_is_bounded_by_the_element_budget``
    pins the peak.
    """
    if cand_a.size == 0:
        return []
    src = np.repeat(np.arange(sizes.size, dtype=np.int64), sizes)
    keys = src * int(width)          # sorted: universe incidences are (source, concept) sorted
    del src
    keys += universe_can
    pairs: List[Tuple[int, int, int, int]] = []
    for chunk in range(0, int(cand_a.size), _VERIFY_PAIR_CHUNK):
        a = cand_a[chunk:chunk + _VERIFY_PAIR_CHUNK].astype(np.int64)
        b = cand_b[chunk:chunk + _VERIFY_PAIR_CHUNK].astype(np.int64)
        inter = _intersections(a, b, keys, universe_can, indptr, sizes, int(width))
        union = sizes[a] + sizes[b] - inter
        hit = inter * _DUP_J.denominator >= _DUP_J.numerator * union
        pairs.extend(zip(a[hit].tolist(), b[hit].tolist(),
                         inter[hit].tolist(), union[hit].tolist()))
    return pairs


def _intersections(
    a: "np.ndarray", b: "np.ndarray", keys: "np.ndarray", universe_can: "np.ndarray",
    indptr: "np.ndarray", sizes: "np.ndarray", width: int,
) -> "np.ndarray":
    """``|A∩B|`` per pair of one chunk; expanded elements per step ≤ the budget."""
    small_is_a = sizes[a] <= sizes[b]
    small = np.where(small_is_a, a, b)
    large = np.where(small_is_a, b, a)
    del small_is_a
    cost = sizes[small]                # ≥ 1: every candidate shares a universe concept
    running = np.cumsum(cost)
    out = np.empty(a.size, dtype=np.int64)
    at = 0
    while at < a.size:
        base = int(running[at - 1]) if at else 0
        end = max(int(np.searchsorted(running, base + _VERIFY_ELEMENT_BUDGET, side="right")),
                  at + 1)
        lengths = cost[at:end]
        expanded = int(lengths.sum())
        first = np.cumsum(lengths) - lengths
        position = np.repeat(indptr[small[at:end]] - first, lengths)
        position += np.arange(expanded, dtype=np.int64)
        query = np.repeat(large[at:end] * width, lengths)
        query += universe_can[position]
        del position
        found = np.searchsorted(keys, query)
        np.minimum(found, keys.size - 1, out=found)
        matched = (keys[found] == query).astype(np.int64)
        del found, query
        out[at:end] = np.add.reduceat(matched, first)
        del matched, first
        at = end
    return out


def _duplicate_pairs(corpus: _Corpus, cap: int) -> _DuplicatePairs:
    """F2: exact all-pairs prefix-filtering similarity join (design §4.2).

    Universe = each source's concept canonicals minus the ubiquitous ones (in
    more than ``UBIQUITOUS_SOURCE_COUNT`` sources); concepts in only one of the
    two count in the denominator. Exact, not a sketch: the same input yields the
    same pairs, and ``test_prefix_join_equals_brute_force`` pins it equal to the
    brute-force all-pairs Jaccard.
    """
    n_sources = len(corpus.source_ids)
    in_universe = corpus.df[corpus.inc_can] <= UBIQUITOUS_SOURCE_COUNT
    u_src = corpus.inc_src[in_universe]
    u_can = corpus.inc_can[in_universe]
    del in_universe
    sizes = np.bincount(u_src, minlength=n_sources).astype(np.int64)
    indptr = np.zeros(n_sources + 1, dtype=np.int64)
    np.cumsum(sizes, out=indptr[1:])
    # Rare-first inside each source (global order: df asc, concept index asc).
    order = np.lexsort((u_can, corpus.df[u_can], u_src))
    o_src = u_src[order]
    o_can = u_can[order]
    del order
    position = np.arange(o_src.size, dtype=np.int64) - indptr[o_src]
    in_prefix = position < _prefix_lengths(sizes)[o_src]
    # A concept in one source only can never be shared: it keeps its place in
    # the prefix (positions are global), it is just not indexed.
    in_prefix &= corpus.df[o_can] >= 2
    cand_a, cand_b, generated, truncated = _candidate_pairs(
        o_src[in_prefix], o_can[in_prefix], corpus.df, n_sources, cap
    )
    del o_src, o_can, position, in_prefix, u_src
    pairs = _verify_pairs(cand_a, cand_b, u_can, indptr, sizes, max(corpus.stride, 1))
    pairs.sort()
    return _DuplicatePairs(pairs, int(cand_a.size), generated, truncated, u_can, indptr)


# ================================================================== F1 inputs
def _shared_shares(
    corpus: _Corpus, pairs: Sequence[Tuple[int, int, int, int]],
) -> Tuple["np.ndarray", "np.ndarray"]:
    """F1: per incidence, in how many OTHER non-partner sources the concept is
    (``others``); per source, the share of its concepts with ``others >= 1``.

    Duplicate partners (F2 pairs) are subtracted: without that a pair of
    duplicated wrong files would "share" 100% with each other and F1 would miss
    both (§4.1). All concepts count here — ubiquitous ones included."""
    others = corpus.df[corpus.inc_can].astype(np.int64) - 1
    for a, b, _inter, _union in pairs:
        _common, at_a, at_b = np.intersect1d(
            _source_tokens(corpus, a), _source_tokens(corpus, b),
            assume_unique=True, return_indices=True,
        )
        others[corpus.indptr[a] + at_a] -= 1
        others[corpus.indptr[b] + at_b] -= 1
    shared = np.bincount(
        corpus.inc_src, weights=(others >= 1), minlength=len(corpus.source_ids)
    )
    share = np.where(corpus.concepts > 0, shared / np.maximum(1, corpus.concepts), 0.0)
    return share, others


def _off_topic_components(
    ranks: Sequence[int], pairs: Sequence[Tuple[int, int, int, int]],
) -> Dict[int, int]:
    """F1 sources joined by F2 pairs → one row per component, keyed by its
    smallest rank (= smallest source id: ranks follow the sorted ids)."""
    root: Dict[int, int] = {rank: rank for rank in ranks}

    def find(x: int) -> int:
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    for a, b, _inter, _union in pairs:
        if a in root and b in root:
            ra, rb = find(a), find(b)
            if ra != rb:
                root[max(ra, rb)] = min(ra, rb)
    return {rank: find(rank) for rank in ranks}


# ============================================================== lookups
NameKey = Tuple[Optional[str], str]
"""A display-name request: ``(source_id, canonical_id)`` = the name of one of
THAT source's own objects in that canonical; ``(None, canonical_id)`` = the name
of any member object from a **visible** source (F4 / S1)."""


@dataclass(frozen=True)
class SourceFacts:
    """What the engine needs to know about one source: its title (the label
    snapshot, §3.1) and its creation time (F2's tie-break: equal object counts
    keep the earlier import). ``created_at`` is an ISO-8601 string or ``None``."""

    title: str
    created_at: Optional[str] = None


@dataclass(frozen=True)
class LookupRequest:
    """One bounded round of point lookups the engine still needs.

    ``names``: display-name keys (see ``NameKey``) — answer with rows
    ``(source_id_or_None, canonical_id, name)``, several names per key allowed,
    in a deterministic store order; the engine uses the first acceptable one.
    ``sources``: source ids whose ``SourceFacts`` are needed.
    A key answered with nothing still counts as answered (the object or source
    vanished between the scan and the lookup): that sample or label is omitted.
    """

    names: Tuple[NameKey, ...]
    sources: Tuple[str, ...]


class _Lookups:
    __slots__ = ("names", "sources", "seeds")

    def __init__(self) -> None:
        self.names: Dict[NameKey, List[str]] = {}
        self.sources: Dict[str, SourceFacts] = {}
        # canonical id → decision seeds (v1 and v2) of ALL its members (hidden
        # ones included), answered with the (None, canonical) name keys; stored
        # in F4 evidence.
        self.seeds: Dict[str, Set[str]] = {}

    def candidates(self, key: NameKey) -> List[str]:
        return self.names.get(key) or []

    def name(self, key: NameKey) -> Optional[str]:
        found = self.candidates(key)
        return found[0] if found else None

    def title(self, source_id: str) -> str:
        facts = self.sources.get(source_id)
        return facts.title if facts is not None else ""

    def created_at(self, source_id: str) -> Optional[str]:
        facts = self.sources.get(source_id)
        return facts.created_at if facts is not None else None


class _Pending:
    """One finding waiting for its lookups. ``build`` returns the row, or
    ``None`` when the visible names do not reproduce the finding (F4 / S1)."""

    __slots__ = ("rule_id", "subject_key", "name_keys", "source_keys", "_build")

    def __init__(
        self,
        rule_id: str,
        subject_key: str,
        name_keys: Sequence[NameKey],
        source_keys: Sequence[str],
        build: Callable[[_Lookups], Optional[dict]],
    ) -> None:
        self.rule_id = rule_id
        self.subject_key = subject_key
        self.name_keys = tuple(name_keys)
        self.source_keys = tuple(source_keys)
        self._build = build

    def build(self, lookups: _Lookups) -> Optional[dict]:
        return self._build(lookups)


@dataclass
class _Spec:
    """A finding before canonical ids are known: its sort key, the concept
    indices it needs ids for, and how to turn into a `_Pending` once they are."""

    impact: int
    key: str
    indices: List[int]
    make: Callable[[Dict[int, str]], _Pending]


def _cap(specs: List[_Spec]) -> List[_Spec]:
    """The persisted window of one rule: impact desc, subject key asc, ≤ cap."""
    specs.sort(key=lambda spec: (-spec.impact, spec.key))
    return specs[:MAX_PERSISTED_FINDINGS]


def _row(
    rule_id: str, subject_key: str, label: str, refs: Sequence[str],
    impact: int, evidence: dict,
) -> dict:
    return {
        "rule_id": rule_id,
        "subject_key": subject_key,
        "subject_kind": RULE_SUBJECT_KIND[rule_id],
        "subject_label": label,
        "subject_refs": list(refs),
        "severity": RULE_SEVERITY[rule_id],
        "impact_objects": int(impact),
        "evidence": evidence,
    }


def _sample_names(lookups: _Lookups, keys: Sequence[NameKey]) -> List[str]:
    return [name for name in (lookups.name(key) for key in keys) if name]


# ================================================================== F1 rows
def _off_topic_specs(
    corpus: _Corpus,
    components: Mapping[int, int],
    others: "np.ndarray",
    shared_share: "np.ndarray",
    giant_share: Optional["np.ndarray"],
) -> List[_Spec]:
    by_root: Dict[int, List[int]] = {}
    for rank, root in components.items():
        by_root.setdefault(root, []).append(rank)
    specs: List[_Spec] = []
    for root, members in by_root.items():
        members.sort()
        primary = members[0]
        source_id = corpus.source_ids[primary]
        lo, hi = int(corpus.indptr[primary]), int(corpus.indptr[primary + 1])
        # "独有": in no other non-partner source — what the source is about.
        unique = corpus.inc_can[lo:hi][others[lo:hi] == 0]
        samples = _top_by_degree(corpus, unique, MAX_SAMPLES)
        refs = [corpus.source_ids[rank] for rank in members]
        impact = int(corpus.objects[members].sum())
        # Only the numbers the criterion used (§3.1): the object count is the
        # impact, not evidence. Without any relation there is no giant component,
        # so the key is absent rather than null (F1 still judges sharing, §4.1).
        evidence: dict = {
            "shared_share": float(shared_share[primary]),
            "concepts": int(corpus.concepts[primary]),
        }
        if giant_share is not None:
            evidence["giant_share"] = float(giant_share[primary])
        partners = [
            {"source_id": corpus.source_ids[rank],
             "concepts": int(corpus.concepts[rank]),
             "shared_share": float(shared_share[rank])}
            for rank in members[1:]
        ]

        def make(ids: Dict[int, str], *, source_id=source_id, samples=samples,
                 refs=refs, impact=impact, evidence=evidence,
                 partners=partners) -> _Pending:
            keys = [(source_id, ids[index]) for index in samples]

            def build(lookups: _Lookups) -> dict:
                body = dict(evidence)
                body["samples"] = _sample_names(lookups, keys)
                if partners:
                    # Two duplicated off-topic copies: one row, both deletions (§4.1).
                    body["partners"] = [
                        {**partner, "label": lookups.title(partner["source_id"])}
                        for partner in partners
                    ]
                return _row(RULE_OFF_TOPIC_SOURCE, source_id, lookups.title(source_id),
                            refs, impact, body)

            return _Pending(RULE_OFF_TOPIC_SOURCE, source_id, keys, refs, build)

        specs.append(_Spec(impact, source_id, samples, make))
    return specs


# ================================================================== F2 rows
def _keep_first(
    lookups: _Lookups, a: str, b: str, objects_a: int, objects_b: int,
) -> bool:
    """F2 suggestion: keep the copy with more objects; on a tie the earlier
    import; on a tie of that (or unknown dates) the smaller source id."""
    if objects_a != objects_b:
        return objects_a > objects_b
    created_a, created_b = lookups.created_at(a), lookups.created_at(b)
    if created_a and created_b and created_a != created_b:
        return created_a < created_b
    if bool(created_a) != bool(created_b):
        return bool(created_a)
    return True


def _duplicate_specs(
    corpus: _Corpus, duplicates: _DuplicatePairs, absorbed: Set[Tuple[int, int]],
) -> List[_Spec]:
    specs: List[_Spec] = []
    u_can, u_ptr = duplicates.universe_can, duplicates.universe_indptr
    for a, b, inter, union in duplicates.pairs:
        if (a, b) in absorbed:
            continue
        sid_a, sid_b = corpus.source_ids[a], corpus.source_ids[b]
        shared = np.intersect1d(u_can[u_ptr[a]:u_ptr[a + 1]], u_can[u_ptr[b]:u_ptr[b + 1]],
                                assume_unique=True)
        samples = _top_by_degree(corpus, shared, MAX_SAMPLES)
        objects_a, objects_b = int(corpus.objects[a]), int(corpus.objects[b])
        impact = min(objects_a, objects_b)        # the copy suggested for deletion
        key = source_pair_key(sid_a, sid_b)
        evidence = {"jaccard": inter / union, "shared_concepts": int(inter)}

        def make(ids: Dict[int, str], *, sid_a=sid_a, sid_b=sid_b, samples=samples,
                 impact=impact, key=key, evidence=evidence, objects_a=objects_a,
                 objects_b=objects_b) -> _Pending:
            keys = [(sid_a, ids[index]) for index in samples]

            def build(lookups: _Lookups) -> dict:
                keep_a = _keep_first(lookups, sid_a, sid_b, objects_a, objects_b)
                title_a, title_b = lookups.title(sid_a), lookups.title(sid_b)
                body = dict(evidence)
                body["sources"] = [
                    {"source_id": sid_a, "label": title_a, "objects": objects_a,
                     "keep": keep_a},
                    {"source_id": sid_b, "label": title_b, "objects": objects_b,
                     "keep": not keep_a},
                ]
                body["samples"] = _sample_names(lookups, keys)
                return _row(RULE_DUPLICATE_IMPORT, key, f"{title_a} ⇄ {title_b}",
                            [sid_a, sid_b], impact, body)

            return _Pending(RULE_DUPLICATE_IMPORT, key, keys, [sid_a, sid_b], build)

        specs.append(_Spec(impact, key, samples, make))
    return specs


# ================================================================== F3
@dataclass
class _ParseDamage:
    hit: "np.ndarray"
    fragmented: "np.ndarray"          # [stride] broken concept-canonical name
    fragmented_hit: "np.ndarray"
    low_relations: "np.ndarray"
    high_isolated: "np.ndarray"
    fragmented_names: "np.ndarray"
    fragmented_objects: "np.ndarray"
    fragmented_share: "np.ndarray"
    relations_per_object: "np.ndarray"
    isolated_share: "np.ndarray"
    median_relations: Optional[float]     # None: no source reaches F3B_MIN_OBJECTS
    median_isolated: float
    isolated_threshold: float             # max(factor × median, floor)


def _exact_median(numerators: Sequence[int], denominators: Sequence[int]) -> Fraction:
    """The median of ``numerators[i] / denominators[i]`` as an exact fraction.

    The order comes from the float ratios (monotone in the exact values); the
    middle element(s) are then taken exactly, so a threshold "relative to the
    median" can be compared without a float product shifting an exact tie."""
    ratios = np.asarray(numerators, dtype=np.float64) / np.asarray(denominators, dtype=np.float64)
    order = np.argsort(ratios, kind="stable")
    middle = len(order) // 2

    def exact(position: int) -> Fraction:
        index = int(order[position])
        return Fraction(int(numerators[index]), int(denominators[index]))

    if len(order) % 2:
        return exact(middle)
    return (exact(middle - 1) + exact(middle)) / 2


def _parse_damage(
    corpus: _Corpus, relations_by_source: Mapping[str, int], fragmented: "np.ndarray",
) -> _ParseDamage:
    """F3 (design §4.3): every sub-signal relative to this library's median.

    F3a's share is over ALL the source's concept canonicals (named or not), the
    calibrated probe's denominator; canonicals without a name are never broken.

    F3b, both sub-signals floored (a tiny source's ratios say nothing about
    parsing):
      · relations / objects strictly below ``F3B_RELATIONS_PER_OBJECT_FRACTION`` of
        the median — judged, and the median taken, only over sources with at least
        ``F3B_MIN_OBJECTS`` objects;
      · isolated share strictly above max(``F3B_ISOLATED_SHARE_FACTOR`` × median,
        ``F3B_ISOLATED_SHARE_FLOOR``) with at least ``F3B_MIN_ISOLATED`` isolated
        objects — the floor keeps a 0 median from turning "20 isolated objects at
        1%" into a hit.
    Both comparisons are exact rationals: strict, and an exact tie stays a non-hit.
    """
    n_sources = len(corpus.source_ids)
    objects = [max(1, int(n)) for n in corpus.objects.tolist()]
    relations = [int(relations_by_source.get(source_id, 0) or 0)
                 for source_id in corpus.source_ids]
    isolated = [int(n) for n in corpus.isolated.tolist()]
    judged = [i for i, o in enumerate(objects) if o >= F3B_MIN_OBJECTS]
    median_relations: Optional[Fraction] = (
        _exact_median([relations[i] for i in judged], [objects[i] for i in judged])
        if judged else None
    )
    median_isolated = _exact_median(isolated, objects)
    high_cut = max(median_isolated * Fraction(repr(F3B_ISOLATED_SHARE_FACTOR)),
                   Fraction(repr(F3B_ISOLATED_SHARE_FLOOR)))
    low_relations = np.zeros(n_sources, dtype=bool)
    if median_relations is not None:
        low_cut = median_relations * Fraction(repr(F3B_RELATIONS_PER_OBJECT_FRACTION))
        for i in judged:
            low_relations[i] = relations[i] * low_cut.denominator < low_cut.numerator * objects[i]
    high_isolated = np.asarray([
        i >= F3B_MIN_ISOLATED and i * high_cut.denominator > high_cut.numerator * o
        for i, o in zip(isolated, objects)
    ], dtype=bool)
    relations_per_object = np.asarray(relations, dtype=np.float64) / np.asarray(objects)
    isolated_share = np.asarray(isolated, dtype=np.float64) / np.asarray(objects)
    mask = fragmented[corpus.inc_can]
    fragmented_names = np.bincount(corpus.inc_src[mask], minlength=n_sources).astype(np.int64)
    fragmented_objects = np.bincount(
        corpus.inc_src[mask], weights=corpus.inc_objects[mask], minlength=n_sources
    ).astype(np.int64)
    fragmented_share = np.where(
        corpus.concepts > 0, fragmented_names / np.maximum(1, corpus.concepts), 0.0
    )
    fragmented_hit = ((corpus.concepts > 0) & (fragmented_share >= F3A_SHARE_MIN)
                      & (fragmented_names >= F3A_MIN_NAMES))
    return _ParseDamage(
        fragmented_hit | low_relations | high_isolated, fragmented, fragmented_hit,
        low_relations, high_isolated, fragmented_names, fragmented_objects,
        fragmented_share, relations_per_object, isolated_share,
        None if median_relations is None else float(median_relations),
        float(median_isolated), float(high_cut),
    )


def _parse_damage_specs(
    corpus: _Corpus, f3: _ParseDamage, off_topic: "np.ndarray",
) -> List[_Spec]:
    specs: List[_Spec] = []
    for rank in np.flatnonzero(f3.hit).tolist():
        source_id = corpus.source_ids[rank]
        signals: List[str] = []
        evidence: dict = {}
        samples: List[int] = []
        if f3.fragmented_hit[rank]:
            signals.append(F3_SIGNAL_FRAGMENTED_NAMES)
            evidence["fragmented_names"] = int(f3.fragmented_names[rank])
            evidence["fragmented_share"] = float(f3.fragmented_share[rank])
            evidence["fragmented_objects"] = int(f3.fragmented_objects[rank])
            tokens = _source_tokens(corpus, rank)
            samples = _top_by_degree(corpus, tokens[f3.fragmented[tokens]], MAX_SAMPLES)
        if f3.low_relations[rank]:
            signals.append(F3_SIGNAL_LOW_RELATIONS)
            evidence["relations_per_object"] = float(f3.relations_per_object[rank])
            evidence["median_relations_per_object"] = f3.median_relations
        if f3.high_isolated[rank]:
            signals.append(F3_SIGNAL_HIGH_ISOLATED)
            evidence["isolated_objects"] = int(corpus.isolated[rank])
            evidence["isolated_share"] = float(f3.isolated_share[rank])
            evidence["median_isolated_share"] = f3.median_isolated
            evidence["isolated_share_threshold"] = f3.isolated_threshold
        evidence["signals"] = signals
        # It would also have been F1: if it is still unrelated to the library
        # after reparsing, it turns into an off-topic finding (§4.3).
        evidence["also_off_topic"] = bool(off_topic[rank])
        # F3a alone: the broken names' objects; any F3b signal: the whole source
        # (reparsing replaces every knowledge item of the source).
        impact = (int(corpus.objects[rank])
                  if (f3.low_relations[rank] or f3.high_isolated[rank])
                  else int(f3.fragmented_objects[rank]))

        def make(ids: Dict[int, str], *, source_id=source_id, evidence=evidence,
                 samples=samples, impact=impact) -> _Pending:
            keys = [(source_id, ids[index]) for index in samples]

            def build(lookups: _Lookups) -> dict:
                body = dict(evidence)
                if keys:
                    body["samples"] = _sample_names(lookups, keys)
                return _row(RULE_PARSE_DAMAGE, source_id, lookups.title(source_id),
                            [source_id], impact, body)

            return _Pending(RULE_PARSE_DAMAGE, source_id, keys, [source_id], build)

        specs.append(_Spec(impact, source_id, samples, make))
    return specs


# ================================================================== F5
def _isolated_specs(corpus: _Corpus, usable: int) -> List[_Spec]:
    total = int(corpus.isolated.sum())
    if total == 0:
        return []
    ranks = [rank for rank in np.lexsort((np.arange(corpus.isolated.size),
                                          -corpus.isolated)).tolist()
             if corpus.isolated[rank] > 0][:F5_TOP_SOURCES]
    top = [(corpus.source_ids[rank], int(corpus.isolated[rank])) for rank in ranks]
    refs = [source_id for source_id, _n in top]
    evidence = {
        "isolated_objects": total,
        "isolated_share": total / usable if usable else 0.0,
        "usable_objects": usable,
    }

    def make(_ids: Dict[int, str]) -> _Pending:
        def build(lookups: _Lookups) -> dict:
            body = dict(evidence)
            body["top_sources"] = [
                {"source_id": source_id, "label": lookups.title(source_id),
                 "isolated_objects": count}
                for source_id, count in top
            ]
            return _row(RULE_ISOLATED_OBJECTS, NOTEBOOK_SUBJECT_KEY, "", refs, total, body)

        return _Pending(RULE_ISOLATED_OBJECTS, NOTEBOOK_SUBJECT_KEY, [], refs, build)

    return [_Spec(total, NOTEBOOK_SUBJECT_KEY, [], make)]


# ================================================================== names pass
def _names_pass(
    names: Mapping[str, str], can2idx: Mapping[str, int], corpus: _Corpus,
) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray", "np.ndarray"]:
    """One pass over ``names``: F3a's broken-name flag and F4's squash-key hash,
    for every concept canonical that has a visible member (``df > 0``).

    Returns ``(named, fragmented, hashes, hashed_indices)``; the two flags are
    boolean arrays over the concept index space."""
    stride = corpus.stride
    present = (corpus.df > 0).astype(np.uint8).tobytes()
    extra_rank = corpus.extra_rank
    n_nodes = corpus.n_nodes
    named_idx = array("q")
    fragmented_idx = array("q")
    hashes = array("q")
    hashed_idx = array("q")
    search = _FRAGMENTED_NAME_RE.search
    for canonical_id, name in names.items():
        index = can2idx.get(canonical_id)
        if index is None or index >= n_nodes:
            index = extra_rank.get(canonical_id)
            if index is None:
                continue
        if not present[index] or not name:
            continue
        named_idx.append(index)
        if search(name):
            fragmented_idx.append(index)
        if canonical_id.startswith(_OVERRIDE_CANONICAL_PREFIX):
            # A canonical a stored rejection keeps in its v1 seed
            # (``~v1:<seed>``, the decision recompute's collision exception)
            # is apart by the user's own decision — never an F4 / S1 group.
            continue
        squash = concept_squash_key(name)
        if squash:
            hashes.append(hash(squash))
            hashed_idx.append(index)
    # ``np.array`` reads the ``array('q')`` buffers directly (buffer protocol, one
    # copy); these are index / hash columns, never similarity vectors.
    named = np.zeros(stride, dtype=bool)
    named[np.array(named_idx, dtype=np.int64)] = True
    fragmented = np.zeros(stride, dtype=bool)
    fragmented[np.array(fragmented_idx, dtype=np.int64)] = True
    return (
        named, fragmented,
        np.array(hashes, dtype=np.int64),
        np.array(hashed_idx, dtype=np.int64),
    )


def _squash_collisions(hashes: "np.ndarray", indices: "np.ndarray") -> Set[int]:
    """Indices whose squash-key hash is shared — a superset of the real
    squash-key groups (true equality is re-checked on the strings)."""
    if hashes.size < 2:
        return set()
    order = np.argsort(hashes, kind="stable")
    ordered = hashes[order]
    equal = ordered[1:] == ordered[:-1]
    flagged = np.zeros(ordered.size, dtype=bool)
    flagged[1:] |= equal
    flagged[:-1] |= equal
    return set(indices[order][flagged].tolist())


def _resolve_canonical_ids(
    can2idx: Mapping[str, int], corpus: _Corpus, needed: Set[int],
) -> Dict[int, str]:
    """Map the needed concept indices back to canonical ids: graph nodes by ONE
    bounded scan of ``can2idx`` (``idx2can`` was cleared before step ⑥), side
    indices from the sorted id list."""
    ids: Dict[int, str] = {}
    graph = {index for index in needed if index < corpus.n_nodes}
    if graph:
        for canonical_id, index in can2idx.items():
            if index in graph:
                ids[index] = canonical_id
                if len(ids) == len(graph):
                    break
    for index in needed:
        if index >= corpus.n_nodes:
            ids[index] = corpus.extra_ids[index - corpus.n_nodes]
    return ids


# ================================================================== F4 / S1
def _sources_of(corpus: _Corpus, indices: Set[int]) -> Dict[int, List[int]]:
    """Source ranks holding a visible member, per concept index.

    One argsort, then ONE vectorised ``searchsorted`` per side for all the
    indices, with the keys already in the array's dtype — a scalar Python-int
    key per call would make numpy convert the whole sorted array every time
    (≈ 230 µs per call at 2.1 M incidences: quadratic in a wide group's size).
    ``test_sources_of_is_one_vectorised_lookup`` pins the call shape."""
    if not indices:
        return {}
    order = np.argsort(corpus.inc_can, kind="stable")
    ordered = corpus.inc_can[order]
    wanted = np.fromiter(sorted(indices), dtype=ordered.dtype, count=len(indices))
    low = np.searchsorted(ordered, wanted, side="left")
    high = np.searchsorted(ordered, wanted, side="right")
    del ordered
    sources = corpus.inc_src[order]       # source rank per incidence, grouped by concept
    del order
    return {
        index: sorted(sources[lo:hi].tolist())
        for index, lo, hi in zip(wanted.tolist(), low.tolist(), high.tolist())
    }


def _member_seed_sets(
    canonical_ids: Sequence[str],
    member_seeds: Callable[[Sequence[str]], Mapping[str, Iterable[str]]],
) -> Dict[str, Set[str]]:
    """The decision seeds (``kg_merge_seed.concept_decision_seeds``: v1 and v2)
    of every member of each canonical, asked in bounded batches, as
    ``decision_match_key`` reads them.

    The canonical id itself (``"K-" + min seed``) is a member seed too, and it
    is the key legacy decision rows fell back to when ``seed_a/seed_b`` were
    empty — so it is always included."""
    seeds: Dict[str, Set[str]] = {
        cid: ({decision_match_key(cid[2:])} if cid.startswith("K-") else set())
        for cid in canonical_ids
    }
    for at in range(0, len(canonical_ids), _MEMBER_SEED_BATCH):
        batch = list(canonical_ids[at:at + _MEMBER_SEED_BATCH])
        for cid, found in member_seeds(batch).items():
            if cid in seeds:
                seeds[cid].update(decision_match_key(str(seed)) for seed in found if seed)
    return seeds


def _apart_adjacency(
    seeds_of: Mapping[str, Set[str]], decisions: Mapping[frozenset, str],
) -> Dict[str, Set[str]]:
    """canonical id → the canonicals the user judged NOT the same thing.

    Decisions are stored per v1 SEED pair (`decided_seed_pairs_from`), because
    a canonical id moves whenever its cluster's minimum member changes (locally
    183 of 1,344 rejected/deferred rows name a canonical id that no longer
    exists). X and Y are apart when some rejected or deferred decision {a, b}
    has a ∈ seeds(X) and b ∈ seeds(Y). Cost: one pass over the decisions plus
    the owners of their two seeds."""
    owners: Dict[str, Set[str]] = {}
    for cid, seeds in seeds_of.items():
        for seed in seeds:
            owners.setdefault(seed, set()).add(cid)
    adjacency: Dict[str, Set[str]] = {}
    for key, status in decisions.items():
        if status not in DECISION_APART_STATUSES:
            continue
        pair = tuple(decision_match_key(str(seed)) for seed in key)
        if len(pair) != 2:
            continue
        left, right = owners.get(pair[0], ()), owners.get(pair[1], ())
        for x in left:
            for y in right:
                if x != y:
                    adjacency.setdefault(x, set()).add(y)
                    adjacency.setdefault(y, set()).add(x)
    return adjacency


def _open_classes(
    classes: Mapping[str, Sequence[str]], adjacency: Mapping[str, Set[str]],
) -> Set[str]:
    """The v2-seed classes in at least one class pair nobody decided apart.

    A class pair is apart as soon as ANY member pair across it is apart (after
    S1 each class is one canonical, and the decision carries over to it). Linear
    in the members' apart edges: a class is open iff it is apart from fewer than
    all the other classes."""
    class_of = {cid: seed for seed, members in classes.items() for cid in members}
    apart_from: Dict[str, Set[str]] = {seed: set() for seed in classes}
    for cid, seed in class_of.items():
        for other in adjacency.get(cid, ()):
            other_seed = class_of.get(other)
            if other_seed is not None and other_seed != seed:
                apart_from[seed].add(other_seed)
    others = len(classes) - 1
    return {seed for seed, apart in apart_from.items() if others and len(apart) < others}


UNDER_MERGE_OPEN = "open"
UNDER_MERGE_CONFIRMED = "confirmed"
UNDER_MERGE_DECIDED_APART = "decided_apart"


def under_merge_decision(
    classes: Sequence[Sequence[str]],
    member_seeds: Mapping[str, Iterable[str]],
    decisions: Mapping[frozenset, str],
) -> str:
    """The read side's verdict on one stored F4 group (§3.5), with the decisions
    as they are NOW — the same matching the engine used when it built the row:

      · ``decided_apart``: no class pair is left that nobody decided apart
        (`_open_classes` is empty) — the user rejected or deferred it; it moves
        to "passed" at once (rejecting does not set ``dirty``, so waiting for a
        rebuild to make it disappear would leave it open forever);
      · ``confirmed``: some confirmed decision joins two of its classes — the
        user sent it to be merged; "handled, verified on the next update";
      · ``open`` otherwise.

    ``classes``: the evidence's ``classes`` (canonical ids per v2 spelling).
    ``member_seeds``: canonical id → the decision seeds (v1 and v2,
    ``concept_decision_seeds``) of its member names, as
    `kg_finding_member_name_rows` reads them (hidden members included); the
    canonical id's own seed is added here, as in the engine. Decision keys are
    read through ``decision_match_key`` (an override ``~v1:<seed>`` names the
    members of that v1 seed)."""
    canonical_ids = sorted({cid for cls in classes for cid in cls})
    seeds_of = _member_seed_sets(
        canonical_ids, lambda batch: {cid: member_seeds.get(cid, ()) for cid in batch}
    )
    by_class = {str(index): list(cls) for index, cls in enumerate(classes)}
    if not _open_classes(by_class, _apart_adjacency(seeds_of, decisions)):
        return UNDER_MERGE_DECIDED_APART
    class_of = {cid: index for index, cls in enumerate(classes) for cid in cls}
    owners: Dict[str, Set[int]] = {}
    for cid, seeds in seeds_of.items():
        for seed in seeds:
            owners.setdefault(seed, set()).add(class_of[cid])
    for key, status in decisions.items():
        pair = tuple(decision_match_key(str(seed)) for seed in key)
        if status != DECISION_CONFIRMED or len(pair) != 2:
            continue
        left, right = owners.get(pair[0], set()), owners.get(pair[1], set())
        if any(a != b for a in left for b in right):
            return UNDER_MERGE_CONFIRMED
    return UNDER_MERGE_OPEN


def _undecided_inside(members: Sequence[str], adjacency: Mapping[str, Set[str]]) -> bool:
    """Whether some pair inside a narrow class is NOT decided apart."""
    inside = set(members)
    apart_pairs = sum(len(adjacency.get(cid, set()) & inside) for cid in members) // 2
    return apart_pairs < len(inside) * (len(inside) - 1) // 2


def _merge_specs(
    corpus: _Corpus,
    groups: Mapping[str, List[int]],
    ids: Mapping[int, str],
    names: Mapping[str, str],
    adjacency: Mapping[str, Set[str]],
) -> Tuple[List[_Pending], List[_Pending], Dict[str, int]]:
    """F4 groups (the wide half) and S1 classes (the narrow half), design §4.4.

    For a squash-key group G (≥ 2 canonicals whose names share a squash key):

      · partition G by the v2 seed of the canonical names. A class of ≥ 2
        canonicals whose v1 seeds differ is a **narrow** group: S1 (drop the
        ORIGINAL whitespace / hyphen / underscore before punctuation) will
        merge it by itself — it is counted for "needs development", not listed;
      · ≥ 2 classes ⇒ **wide**: the names collide only once punctuation-made
        spaces go too ("Llama 3.1" / "Llama 31") — F4 proper, for a human.

    Groups the user rejected or deferred are excluded (§4.4): a wide group needs
    a class pair nobody decided apart; a narrow class needs an undecided pair.
    Everything is judged on canonical names here (internal), then re-checked on
    the visible members' own names in the lookup rounds.
    """
    counts = {"narrow_groups": 0, "narrow_group_objects": 0,
              "narrow_decided_apart": 0, "wide_decided_apart": 0}
    wide: List[Tuple[int, str, List[int]]] = []
    narrow: List[Tuple[int, str, List[int]]] = []
    for squash in sorted(groups):
        members = groups[squash]
        classes: Dict[str, List[int]] = {}
        for index in members:
            classes.setdefault(concept_seed_v2(names[ids[index]]), []).append(index)
        for seed in sorted(classes):
            cls = classes[seed]
            if len(cls) < 2 or len({concept_seed_v1(names[ids[i]]) for i in cls}) < 2:
                continue
            if not _undecided_inside([ids[i] for i in cls], adjacency):
                counts["narrow_decided_apart"] += 1
                continue
            objects = int(corpus.can_objects[cls].sum())
            counts["narrow_groups"] += 1
            counts["narrow_group_objects"] += objects
            narrow.append((objects, seed, sorted(cls)))
        if len(classes) < 2:
            continue
        open_classes = _open_classes(
            {seed: [ids[i] for i in cls] for seed, cls in classes.items()}, adjacency
        )
        if not open_classes:
            counts["wide_decided_apart"] += 1
            continue
        kept = sorted(i for seed in open_classes for i in classes[seed])
        wide.append((int(corpus.can_objects[kept].sum()), squash, kept))
    wide.sort(key=lambda item: (-item[0], item[1]))
    narrow.sort(key=lambda item: (-item[0], item[1]))
    sources = _sources_of(corpus, {i for _o, _k, kept in wide for i in kept})
    f4 = [
        _under_merge_pending(
            squash,
            {ids[i]: int(corpus.can_objects[i]) for i in kept},
            {ids[i]: [corpus.source_ids[r] for r in sources.get(i, [])] for i in kept},
            {ids[i]: set(adjacency.get(ids[i], ())) for i in kept},
        )
        for _objects, squash, kept in wide
    ]
    s1 = [_seed_defect_pending(seed, cls, ids) for _objects, seed, cls in narrow]
    return f4, s1, counts


def _visible_name(lookups: _Lookups, canonical_id: str, squash: str) -> Optional[str]:
    """The first visible member name of ``canonical_id`` that still has the
    group's squash key — the name F4 shows AND re-judges on (§7.1 ⑥.7)."""
    for name in lookups.candidates((None, canonical_id)):
        if concept_squash_key(name) == squash:
            return name
    return None


def _under_merge_pending(
    squash: str,
    objects: Dict[str, int],
    member_sources: Dict[str, List[str]],
    adjacency: Dict[str, Set[str]],
) -> _Pending:
    """One F4 candidate. Its closure keeps only these small per-group maps —
    never the corpus: the draft lives through the lookup rounds, and the
    corpus' arrays must be free by then (``test_corpus_is_freed_once_compute_returns``)."""
    canonical_ids = sorted(objects)
    keys: List[NameKey] = [(None, canonical_id) for canonical_id in canonical_ids]

    def build(lookups: _Lookups) -> Optional[dict]:
        visible = {}
        for canonical_id in canonical_ids:
            name = _visible_name(lookups, canonical_id, squash)
            if name is not None:
                visible[canonical_id] = name
        classes: Dict[str, List[str]] = {}
        for canonical_id, name in visible.items():
            classes.setdefault(concept_seed_v2(name), []).append(canonical_id)
        if len(classes) < 2:
            return None            # the visible names do not reproduce the group
        open_classes = _open_classes(classes, adjacency)
        if not open_classes:
            return None
        kept = sorted(c for seed in open_classes for c in classes[seed])
        shown = sorted(kept, key=lambda c: (-objects[c], c))
        refs = sorted({source_id for c in kept for source_id in member_sources[c]})
        evidence = {
            "canonicals": len(kept),
            "spellings": len(open_classes),
            "member_sources": len(refs),
            "members": [
                {"canonical_id": c, "name": visible[c], "objects": objects[c]}
                for c in shown[:MAX_SAMPLES]
            ],
        }
        # The whole group, not just the shown sample: the read side re-judges
        # "the user rejected / deferred / confirmed it" on these classes with
        # the decisions stored at read time (`under_merge_decision`), matched
        # by the members' decision seeds stored here — so it never reads members.
        evidence["canonical_ids"] = kept
        evidence["classes"] = sorted(sorted(classes[seed]) for seed in open_classes)
        member_seeds: Dict[str, List[str]] = {}
        truncated = False
        for canonical_id in kept:
            seeds = sorted(lookups.seeds.get(canonical_id, ()))
            truncated = truncated or len(seeds) > MAX_MEMBER_SEEDS_PER_CANONICAL
            member_seeds[canonical_id] = seeds[:MAX_MEMBER_SEEDS_PER_CANONICAL]
        evidence["member_seeds"] = member_seeds
        evidence["seeds_truncated"] = truncated
        impact = sum(objects[c] for c in kept)
        return _row(RULE_UNDER_MERGE, squash, visible[shown[0]], refs[:MAX_SUBJECT_REFS],
                    impact, evidence)

    return _Pending(RULE_UNDER_MERGE, squash, keys, [], build)


def _seed_defect_pending(seed: str, members: List[int], ids: Mapping[int, str]) -> _Pending:
    canonical_ids = sorted(ids[index] for index in members)
    keys: List[NameKey] = [(None, canonical_id) for canonical_id in canonical_ids]

    def build(lookups: _Lookups) -> Optional[dict]:
        shown: List[str] = []
        spellings: Set[str] = set()
        for canonical_id in canonical_ids:
            for name in lookups.candidates((None, canonical_id)):
                if concept_seed_v2(name) == seed:
                    shown.append(name)
                    spellings.add(concept_seed_v1(name))
                    break
        if len(spellings) < 2:
            return None            # visible names no longer show two spellings
        return {"rule_id": RULE_SEED_DEFECT, "subject_key": seed,
                "evidence": {"names": shown[:MAX_SAMPLES]}}

    return _Pending(RULE_SEED_DEFECT, seed, keys, [], build)


# ============================================================ engine result
@dataclass
class FindingsResult:
    """``rows``: detail rows (no ``first_seen_*`` yet) in display order, ≤ the cap
    per rule. ``payload``: the findings ledger payload without the comparison
    summary — both completed by `finalize_findings` in the publish transaction."""

    rows: List[dict]
    payload: dict


class _Window:
    """Fill-to-cap window over ordered candidates the lookups may still reject.

    ``lookahead`` asks for more candidates than the free slots per round (S1
    needs 3 examples; asking 3 at a time would cost one round per 3 failures).
    Candidates still awaiting when the cap fills mid-round are not judged —
    they stay counted in `unverified` (found, never re-checked), so a cap that
    cut the window is never reported as "everything persisted"."""

    __slots__ = ("_candidates", "_cap", "_lookahead", "_next", "_awaiting", "kept",
                 "dropped", "_unjudged")

    def __init__(self, candidates: List[_Pending], cap: int, lookahead: int = 0) -> None:
        self._candidates = candidates
        self._cap = int(cap)
        self._lookahead = int(lookahead)
        self._next = 0
        self._awaiting: List[_Pending] = []
        self.kept: List[dict] = []
        self.dropped = 0
        # pulled into the window (``_next`` moved past them) but never built
        # because the cap filled first in the same round
        self._unjudged = 0

    def refill(self) -> None:
        free = self._cap - len(self.kept)
        if free <= 0:
            return
        wanted = max(free, self._lookahead)
        while len(self._awaiting) < wanted and self._next < len(self._candidates):
            self._awaiting.append(self._candidates[self._next])
            self._next += 1

    def awaiting(self) -> List[_Pending]:
        return list(self._awaiting)

    def settle(self, lookups: _Lookups) -> bool:
        """Build every awaiting candidate (their lookups have all been asked)."""
        if not self._awaiting:
            return False
        for position, item in enumerate(self._awaiting):
            if len(self.kept) >= self._cap:
                # the rest of this round's lookahead: asked, never judged
                self._unjudged += len(self._awaiting) - position
                break
            row = item.build(lookups)
            if row is None:
                self.dropped += 1
            else:
                self.kept.append(row)
        self._awaiting = []
        return True

    def unverified(self) -> int:
        """Candidates found on canonical names but never re-checked on visible
        names: never pulled into the window, or pulled in a round whose cap
        filled before their turn."""
        return len(self._candidates) - self._next + self._unjudged


class FindingsDraft:
    """Findings computed down to their display names (step ⑥, then lookups).

    Protocol (T8)::

        draft = compute_findings(...)
        can2idx.clear()                       # no index is needed past here
        while (request := draft.pending()) is not None:
            for page in pages(request):        # any number of supply calls per round
                draft.supply(names=…rows…, sources=…SourceFacts…)
        result = draft.result()

    ``supply`` only accumulates; the round is judged when ``pending()`` (or
    ``result()``) is called next — calling it declares the previous request
    fully answered, and keys still without rows count as answered with nothing.

    Every round is bounded by the per-rule cap. F1/F2/F3/F5 need one round. F4
    groups (and S1 examples) are re-verified on the **visible** members' own names
    (a canonical name can come from a hidden knowhow object): a group whose
    visible names no longer collide is dropped and the next candidate is
    promoted into the window, so a later round may ask for more names. Each
    candidate is asked about at most once, so the loop always ends.
    """

    def __init__(
        self,
        fixed: List[_Pending],
        f4_candidates: List[_Pending],
        s1_candidates: List[_Pending],
        statuses: Dict[str, dict],
        payload_base: dict,
    ) -> None:
        self._fixed = fixed
        self._windows = {
            RULE_UNDER_MERGE: _Window(f4_candidates, MAX_PERSISTED_FINDINGS, _F4_LOOKAHEAD),
            RULE_SEED_DEFECT: _Window(s1_candidates, S1_EXAMPLES, _S1_LOOKAHEAD),
        }
        self._statuses = statuses
        self._payload_base = payload_base
        self._lookups = _Lookups()
        self._asked_names: Set[NameKey] = set()
        self._asked_sources: Set[str] = set()

    def _open(self) -> Iterator[_Pending]:
        yield from self._fixed
        for window in self._windows.values():
            yield from window.awaiting()

    def pending(self) -> Optional[LookupRequest]:
        """The next round of lookups, or ``None`` when everything is answered.

        Calling it closes the previous round: every candidate still awaiting
        was part of the last request, so it is judged now on what was supplied.
        """
        while True:
            for window in self._windows.values():
                window.settle(self._lookups)
                window.refill()
            names: List[NameKey] = []
            sources: List[str] = []
            for item in self._open():
                for key in item.name_keys:
                    if key not in self._asked_names:
                        self._asked_names.add(key)
                        names.append(key)
                for source_id in item.source_keys:
                    if source_id not in self._asked_sources:
                        self._asked_sources.add(source_id)
                        sources.append(source_id)
            if names or sources:
                return LookupRequest(tuple(names), tuple(sources))
            if not any(window.awaiting() for window in self._windows.values()):
                return None
            # Awaiting candidates whose keys were all asked already (shared keys):
            # loop once more to judge them and promote the next ones.

    def supply(
        self,
        *,
        names: Iterable[Tuple[Optional[str], str, str]] = (),
        sources: Optional[Mapping[str, SourceFacts]] = None,
        member_seeds: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> None:
        """Add answers to the current round: name rows, facts per source id, and
        for the ``(None, canonical)`` keys the decision seeds of ALL that canonical's
        members (hidden ones included — they decide, never display; F4 stores
        them so the read side matches decisions without reading members).

        Accumulates — a paged lookup may call it any number of times; nothing is
        judged until the next ``pending()`` / ``result()``."""
        for source_id, canonical_id, name in names:
            key = (source_id, canonical_id)
            if key in self._asked_names and name:
                self._lookups.names.setdefault(key, []).append(str(name))
        for canonical_id, seeds in (member_seeds or {}).items():
            if (None, canonical_id) in self._asked_names:
                self._lookups.seeds.setdefault(canonical_id, set()).update(
                    str(seed) for seed in seeds if seed)
        for source_id, facts in (sources or {}).items():
            if source_id in self._asked_sources:
                self._lookups.sources[source_id] = facts

    def result(self) -> FindingsResult:
        if self.pending() is not None:
            raise RuntimeError(
                "FindingsDraft.result:还有未回答的点查(先循环 pending()/supply() 到 None)"
            )
        rows = [row for row in (item.build(self._lookups) for item in self._fixed)
                if row is not None]
        f4 = self._windows[RULE_UNDER_MERGE]
        rows.extend(f4.kept)
        rows.sort(key=finding_sort_key)
        statuses = {rule: dict(status) for rule, status in self._statuses.items()}
        f4_status = statuses[RULE_UNDER_MERGE]
        if f4_status["status"] == RULE_STATUS_RAN:
            # Persisted groups were re-verified on visible names; the ones past
            # the window could not be, and still count as found on canonical names.
            f4_status["total"] = len(f4.kept) + f4.unverified()
            f4_status["groups_unreproducible"] = f4.dropped
        s1_status = statuses[RULE_SEED_DEFECT]
        if s1_status["status"] == RULE_STATUS_RAN:
            s1_status["examples"] = [
                example["evidence"] for example in self._windows[RULE_SEED_DEFECT].kept
            ]
        persisted = {rule: 0 for rule in FINDING_RULES}
        for row in rows:
            persisted[row["rule_id"]] += 1
        for rule in FINDING_RULES:
            status = statuses[rule]
            status["persisted"] = persisted[rule]
            status["truncated"] = bool(status.get("truncated")) or (
                persisted[rule] < int(status["total"])
            )
        return FindingsResult(rows, {**self._payload_base, "rules": statuses})


# ============================================================== compute
def compute_findings(
    *,
    tally: FindingsObjectTally,
    structure: GraphStructure,
    can2idx: Mapping[str, int],
    names: Mapping[str, str],
    relations_by_source: Mapping[str, int],
    decisions: Mapping[frozenset, str],
    member_seeds: Callable[[Sequence[str]], Mapping[str, Iterable[str]]],
    max_candidate_pairs: int,
) -> FindingsDraft:
    """Run F2 → F3 → F1 → F4 → F5 on the drained step-⑤ tally (step ⑥).

    Inputs, by where `_compute_kg_analysis` has them:

      ``tally`` / ``structure``  steps ⑤ / ③ above;
      ``can2idx``          the canonical → node index map, still intact. Only
                           read: one bounded scan maps the few indices that need
                           an id back (``idx2can`` is long gone). It may be
                           cleared as soon as this returns;
      ``names``            canonical_id → canonical_name from
                           ``community_graph_rows`` (``rebuild_communities``
                           already holds it; becomes a new parameter of
                           ``_compute_kg_analysis``). **Internal only**: it judges
                           F3a / F4 and is never displayed — display names come
                           from the lookups. Canonicals without a
                           ``concept_clusters`` row have no name: counted
                           (``unnamed_concept_canonicals``), not judged;
      ``relations_by_source`` relation rows per ``kr.source_id``, counted over every
                           ``community_graph_rows`` row (step ①, before the
                           self-loop skip) — F3b's relations / objects;
      ``decisions``        the stored merge decisions keyed by v1 SEED pair —
                           exactly ``governance_store.decided_seed_pairs_from``'s
                           return (``{frozenset({seed_a, seed_b}): status}``, the
                           empty-seed legacy rows already keyed by the canonical
                           id minus ``K-``). Rejected / deferred pairs are never
                           reported by F4 / S1 (§4.4);
      ``member_seeds``     ``(canonical_ids) -> {canonical_id: seeds}``: the
                           ``concept_decision_seeds`` (v1 and v2 — a stored
                           decision of either seed space names the member) of
                           the name of EVERY member object
                           of each canonical — hidden (memory / knowhow) members
                           included: the seeds only decide, they are never shown.
                           The engine calls it only for canonicals in a squash-key
                           group, in batches of at most ``_MEMBER_SEED_BATCH`` ids
                           (one bounded ``IN`` read per call). Decisions are
                           matched by seed, not canonical id: a canonical id moves
                           whenever its cluster's minimum member does;
      ``max_candidate_pairs`` ``settings.kg_findings_max_candidate_pairs`` — the
                           cap on F2 pair GENERATIONS (repeats included).
    Both decision inputs are required: a silent default would ignore what the
    user already judged.
    """
    corpus = _drain_tally(tally, structure)
    n_sources = len(corpus.source_ids)
    usable = int(corpus.objects.sum())
    payload_base = {
        "rules_version": RULES_VERSION,
        "thresholds": dict(RULE_THRESHOLDS),
        "structure": _structure_payload(structure, corpus, usable),
    }
    if n_sources == 0:
        statuses = {
            rule: {"status": RULE_STATUS_SKIPPED, "reason": SKIP_NO_OBJECTS,
                   "total": 0, "persisted": 0, "truncated": False}
            for rule in FINDING_RULES
        }
        statuses[RULE_SEED_DEFECT] = {"status": RULE_STATUS_SKIPPED,
                                      "reason": SKIP_NO_OBJECTS}
        return FindingsDraft([], [], [], statuses, payload_base)

    named, fragmented, squash_hashes, squash_indices = _names_pass(names, can2idx, corpus)
    comparable = int((corpus.concepts > 0).sum()) >= 2

    # F2 first: F1's "shared" excludes duplicate partners.
    if comparable:
        duplicates = _duplicate_pairs(corpus, max_candidate_pairs)
    else:
        duplicates = _DuplicatePairs([], 0, 0, False, _EMPTY_INDEX,
                                     np.zeros(n_sources + 1, dtype=np.int64))
    # F3 before F1: a parse-damaged source also "shares nothing".
    f3 = _parse_damage(corpus, relations_by_source, fragmented)
    shared_share, others = _shared_shares(corpus, duplicates.pairs)
    giant_share = (
        np.where(corpus.in_graph > 0, corpus.giant / np.maximum(1, corpus.in_graph), 0.0)
        if corpus.n_nodes else None
    )
    eligible = corpus.concepts >= F1_MIN_CONCEPTS
    # Gate = concept sharing only; the giant-component share is evidence: one to
    # five bridging concepts pull an unrelated document into the giant component.
    off_topic = eligible & (shared_share < F1_SHARED_MAX) & comparable
    f1_sources = off_topic & ~f3.hit
    components = _off_topic_components(np.flatnonzero(f1_sources).tolist(), duplicates.pairs)
    absorbed = {
        (a, b) for a, b, _inter, _union in duplicates.pairs
        if a in components and b in components and components[a] == components[b]
    }
    f1_specs = _off_topic_specs(corpus, components, others, shared_share, giant_share)
    f2_specs = _duplicate_specs(corpus, duplicates, absorbed)
    f3_specs = _parse_damage_specs(corpus, f3, off_topic)
    f5_specs = _isolated_specs(corpus, usable)
    windows = {rule: _cap(list(specs)) for rule, specs in (
        (RULE_OFF_TOPIC_SOURCE, f1_specs), (RULE_DUPLICATE_IMPORT, f2_specs),
        (RULE_PARSE_DAMAGE, f3_specs), (RULE_ISOLATED_OBJECTS, f5_specs),
    )}
    collisions = _squash_collisions(squash_hashes, squash_indices)
    del squash_hashes, squash_indices
    needed = set(collisions)
    for specs in windows.values():
        for spec in specs:
            needed.update(spec.indices)
    ids = _resolve_canonical_ids(can2idx, corpus, needed)

    groups: Dict[str, List[int]] = {}
    for index in sorted(collisions):
        groups.setdefault(concept_squash_key(names[ids[index]]), []).append(index)
    groups = {squash: members for squash, members in groups.items() if len(members) >= 2}
    adjacency: Dict[str, Set[str]] = {}
    # Member seeds only feed the "decided apart" adjacency: with no rejected or
    # deferred decision in the library there is nothing to match, and the gate
    # is exact — every member-seed read is skipped.
    if groups and any(status in DECISION_APART_STATUSES for status in decisions.values()):
        group_ids = sorted(ids[index] for members in groups.values() for index in members)
        adjacency = _apart_adjacency(_member_seed_sets(group_ids, member_seeds), decisions)
    f4_pending, s1_pending, merge_counts = _merge_specs(corpus, groups, ids, names, adjacency)
    del adjacency
    fixed = [spec.make(ids) for rule in (RULE_OFF_TOPIC_SOURCE, RULE_DUPLICATE_IMPORT,
                                         RULE_PARSE_DAMAGE, RULE_ISOLATED_OBJECTS)
             for spec in windows[rule]]

    statuses: Dict[str, dict] = {}
    if comparable:
        statuses[RULE_OFF_TOPIC_SOURCE] = {
            "status": RULE_STATUS_RAN,
            "sources_checked": int(eligible.sum()),
            "sources_too_few_concepts": int(n_sources - eligible.sum()),
            "sources_hit": int(f1_sources.sum()),
            "sources_deferred_to_f3": int((off_topic & f3.hit).sum()),
            # F2 stopped at its candidate cap: some partners may be missing, and a
            # duplicated pair of wrong files then "shares" everything with its
            # unseen partner and is not reported at all — F1's list is incomplete
            # too, so it counts as truncated (never compared across builds).
            "partner_exclusion_incomplete": duplicates.candidates_truncated,
            "total": len(f1_specs),
            "truncated": duplicates.candidates_truncated,
        }
        statuses[RULE_DUPLICATE_IMPORT] = {
            "status": RULE_STATUS_RAN,
            "sources_checked": int((corpus.concepts > 0).sum()),
            "candidate_pairs": duplicates.candidate_pairs,
            "candidate_pair_generations": duplicates.candidate_generations,
            "candidate_pair_generation_limit": int(max_candidate_pairs),
            "candidates_truncated": duplicates.candidates_truncated,
            "pairs_hit": len(duplicates.pairs),
            "pairs_absorbed_into_f1": len(absorbed),
            "total": len(f2_specs),
            "truncated": duplicates.candidates_truncated,
        }
    else:
        for rule in (RULE_OFF_TOPIC_SOURCE, RULE_DUPLICATE_IMPORT):
            statuses[rule] = {"status": RULE_STATUS_SKIPPED,
                              "reason": SKIP_FEWER_THAN_TWO_SOURCES, "total": 0}
    unnamed = int((corpus.df > 0).sum()) - int(named.sum())
    statuses[RULE_PARSE_DAMAGE] = {
        "status": RULE_STATUS_RAN,
        "sources_checked": n_sources,
        "median_relations_per_object": f3.median_relations,
        "median_isolated_share": f3.median_isolated,
        "sources_hit_fragmented_names": int(f3.fragmented_hit.sum()),
        "sources_hit_low_relations": int(f3.low_relations.sum()),
        "sources_hit_isolated": int(f3.high_isolated.sum()),
        "unnamed_concept_canonicals": unnamed,
        "total": len(f3_specs),
    }
    if int(named.sum()):
        statuses[RULE_UNDER_MERGE] = {
            "status": RULE_STATUS_RAN,
            "canonicals_checked": int(named.sum()),
            "unnamed_concept_canonicals": unnamed,
            "groups_decided_apart": merge_counts["wide_decided_apart"],
            "total": len(f4_pending),
        }
        statuses[RULE_SEED_DEFECT] = {
            "status": RULE_STATUS_RAN,
            "narrow_groups": merge_counts["narrow_groups"],
            "narrow_group_objects": merge_counts["narrow_group_objects"],
            "groups_decided_apart": merge_counts["narrow_decided_apart"],
        }
    else:
        statuses[RULE_UNDER_MERGE] = {"status": RULE_STATUS_SKIPPED,
                                      "reason": SKIP_NO_NAMED_CONCEPTS, "total": 0}
        statuses[RULE_SEED_DEFECT] = {"status": RULE_STATUS_SKIPPED,
                                      "reason": SKIP_NO_NAMED_CONCEPTS}
    statuses[RULE_ISOLATED_OBJECTS] = {
        "status": RULE_STATUS_RAN,
        "objects_checked": usable,
        "total": len(f5_specs),
    }
    return FindingsDraft(fixed, f4_pending, s1_pending, statuses, payload_base)


def _structure_payload(structure: GraphStructure, corpus: _Corpus, usable: int) -> dict:
    """S3: numbers that depend on connected components only (never the boards)."""
    giant_objects = int(corpus.giant.sum())
    return {
        "graph_nodes": structure.n_nodes,
        "components": structure.components,
        "giant_nodes": structure.giant_nodes,
        "usable_objects": usable,
        "giant_objects": giant_objects,
        "giant_object_share": (giant_objects / usable) if usable else 0.0,
        "island_components": max(0, structure.components - 1),
        "island_sources": int(((corpus.in_graph - corpus.giant) > 0).sum()),
    }


# ============================================================== publish
def finding_row_values(row: Mapping[str, object]) -> tuple:
    """One finalized row as the store's ``findings_rows`` tuple
    (``FINDING_ROW_FIELDS`` order)."""
    return tuple(row[name] for name in FINDING_ROW_FIELDS)


def _dismissed_subject_sources(rule_id: str, subject_key: str) -> Tuple[str, ...]:
    """The sources whose deletion ends a dismissed subject: F1 / F3 their source,
    F2 its two sources; ``()`` for F4 / F5 (never pruned at publication)."""
    if rule_id in (RULE_OFF_TOPIC_SOURCE, RULE_PARSE_DAMAGE):
        return (subject_key,) if subject_key else ()
    if rule_id == RULE_DUPLICATE_IMPORT:
        return source_pair_ids(subject_key)
    return ()


def dismissal_source_ids(marks: Iterable[Mapping[str, object]]) -> List[str]:
    """Source ids to point-look-up before computing `stale_dismissals`."""
    ids: Set[str] = set()
    for mark in marks:
        if mark["kind"] == MARK_DISMISSED:
            ids.update(_dismissed_subject_sources(str(mark["rule_id"]),
                                                  str(mark["subject_key"])))
    return sorted(ids)


def stale_dismissals(
    marks: Iterable[Mapping[str, object]],
    rule_statuses: Mapping[str, Mapping[str, object]],
    source_states: Mapping[str, Mapping[str, object]],
) -> List[Tuple[str, str, str]]:
    """The ``dismissed`` marks whose subject no longer EXISTS — the store's
    ``prune_marks`` for this publication (design §8.3: "只在发布时清掉主体已不存在
    的标记(来源被删)"; §8.5⑤: a dismissal survives a re-organisation and the
    same finding is dismissed again).

    "Not in the new detail" is NOT "gone": a library re-extracted after a KG
    delete, a dismissed source caught mid-reparse, a rule cut at its 500-row cap
    or temporarily skipped (fewer than two comparable sources) all leave the
    subject out of the detail while it still exists — pruning there would drop
    the user's judgement silently. So:

      · F1 / F3: pruned only when the subject source is no longer in ``sources``;
        F2: when either of its two sources is gone (``source_states`` from
        `kg_finding_source_states`, read outside the write transaction);
      · F4 / F5: never pruned here — only an undo, a graph / notebook delete
        removes them;
      · a rule that did not run, or was truncated, in the new ledger prunes
        nothing (its view of the library is incomplete).
    ``relink_attempted`` is a system mark, never touched. Dismissals therefore
    outlive the detail; ``KG_ANALYSIS_MARKS_MAX`` is the table's own bound."""
    stale: List[Tuple[str, str, str]] = []
    for mark in marks:
        if mark["kind"] != MARK_DISMISSED:
            continue
        rule_id, subject_key = str(mark["rule_id"]), str(mark["subject_key"])
        status = rule_statuses.get(rule_id) or {}
        if status.get("status") != RULE_STATUS_RAN or status.get("truncated"):
            continue
        sources = _dismissed_subject_sources(rule_id, subject_key)
        if sources and any(not (source_states.get(source_id) or {}).get("exists")
                           for source_id in sources):
            stale.append((rule_id, subject_key, MARK_DISMISSED))
    return stale


# ============================================================== finalize
def _rule_comparable(old: Optional[Mapping[str, object]], new: Mapping[str, object]) -> bool:
    """Both sides ran and neither truncated — or both sides skipped.

    "Skipped on both sides" is comparable (0 / 0 / 0): a single-source or
    concept-less library skips F1/F2 on every build, and treating that as
    incomparable would leave the headline comparison null forever. Ran on one
    side and skipped on the other is not: the skipped side says nothing."""
    if old is None:
        return False
    if old.get("status") == RULE_STATUS_SKIPPED and new.get("status") == RULE_STATUS_SKIPPED:
        return True
    return bool(
        old.get("status") == RULE_STATUS_RAN and new.get("status") == RULE_STATUS_RAN
        and not old.get("truncated") and not new.get("truncated")
    )


def finalize_findings(
    new: FindingsResult,
    old_rows: Iterable[Mapping[str, object]],
    old_ledger: Optional[Mapping[str, object]],
    *,
    kg_seq: int,
    cluster_seq: int,
) -> Tuple[List[dict], dict]:
    """Inherit ``first_seen_*`` and add the comparison summary (step ⑧, pure).

    ``old_rows``: the previous detail rows (≤ cap per rule; ``rule_id``,
    ``subject_key``, ``subject_kind``, ``subject_label``, ``impact_objects``,
    ``severity``, ``first_seen_kg_seq``, ``first_seen_cluster_seq``).
    ``old_ledger``: the previous ``findings`` ledger entry
    (``{kg_mutation_seq, payload, created_at}``, the `kg_analysis_artifact_rows`
    shape) or ``None``. ``kg_seq`` / ``cluster_seq``: the generation this build is
    stamped with — the first-seen of findings that are new.

    Comparability (§3.2 / §六): nothing is compared across a ``rules_version``
    change (keys may mean something else — and first-seen is not inherited
    either); per rule, nothing is compared when either side truncated it (a
    finding beyond the old cap would read as "new", one beyond the new cap as
    "resolved") or either side could not run it. The top-level ``comparable`` is
    true only when every finding rule is; the counts then cover all of them.
    """
    old_payload = (old_ledger or {}).get("payload") if old_ledger else None
    old_payload = old_payload if isinstance(old_payload, Mapping) else None
    old_version = old_payload.get("rules_version") if old_payload else None
    same_rules = (old_payload is not None and not isinstance(old_version, bool)
                  and old_version == new.payload["rules_version"])
    old_list = list(old_rows) if same_rules else []
    old_index = {(str(r["rule_id"]), str(r["subject_key"])): r for r in old_list}
    rows: List[dict] = []
    for row in new.rows:
        previous = old_index.get((row["rule_id"], row["subject_key"]))
        first_kg = previous.get("first_seen_kg_seq") if previous else None
        first_cluster = previous.get("first_seen_cluster_seq") if previous else None
        rows.append({
            **row,
            "first_seen_kg_seq": int(kg_seq) if first_kg is None else int(first_kg),
            "first_seen_cluster_seq": (int(cluster_seq) if first_cluster is None
                                       else int(first_cluster)),
        })
    comparison: dict = {
        "previous_built_at_seq": (int(old_ledger["kg_mutation_seq"])   # type: ignore[index]
                                  if old_ledger else None),
        "previous_built_at_cluster_seq": (
            old_payload.get(CLUSTER_SEQ_PAYLOAD_KEY) if old_payload else None
        ),
    }
    if old_payload is None:
        comparison.update({"comparable": False, "reason": "no_previous",
                           "resolved": None, "persisting": None, "new": None,
                           "resolved_samples": [], "by_rule": {}})
        return rows, {**new.payload, "comparison": comparison}
    if not same_rules:
        comparison.update({"comparable": False, "reason": "rules_version_changed",
                           "resolved": None, "persisting": None, "new": None,
                           "resolved_samples": [], "by_rule": {}})
        return rows, {**new.payload, "comparison": comparison}
    old_statuses = old_payload.get("rules") or {}
    new_keys = {(row["rule_id"], row["subject_key"]) for row in new.rows}
    by_rule: Dict[str, dict] = {}
    resolved_rows: List[Mapping[str, object]] = []
    totals = {"resolved": 0, "persisting": 0, "new": 0}
    for rule in FINDING_RULES:
        old_status = old_statuses.get(rule) if isinstance(old_statuses, Mapping) else None
        if not _rule_comparable(old_status, new.payload["rules"][rule]):
            by_rule[rule] = {"comparable": False}
            continue
        old_rule = [r for r in old_list if str(r["rule_id"]) == rule]
        old_rule_keys = {str(r["subject_key"]) for r in old_rule}
        new_rule_keys = {key for r_id, key in new_keys if r_id == rule}
        gone = [r for r in old_rule if str(r["subject_key"]) not in new_rule_keys]
        counts = {"resolved": len(gone),
                  "persisting": len(old_rule_keys & new_rule_keys),
                  "new": len(new_rule_keys - old_rule_keys)}
        by_rule[rule] = {"comparable": True, **counts}
        for name, value in counts.items():
            totals[name] += value
        resolved_rows.extend(gone)
    comparable = all(entry["comparable"] for entry in by_rule.values())
    resolved_rows.sort(key=finding_sort_key)
    comparison.update({
        "comparable": comparable,
        "reason": None if comparable else "truncated_or_not_run",
        # A headline over only some rules would read as the whole list; the
        # comparable rules keep their own counts under ``by_rule``.
        **(totals if comparable else {"resolved": None, "persisting": None, "new": None}),
        "resolved_samples": [
            {"rule_id": str(r["rule_id"]), "subject_kind": str(r["subject_kind"]),
             "subject_key": str(r["subject_key"]), "subject_label": str(r["subject_label"]),
             "impact_objects": int(r["impact_objects"])}       # type: ignore[arg-type]
            for r in resolved_rows[:MAX_RESOLVED_SAMPLES]
        ],
        "by_rule": by_rule,
    })
    return rows, {**new.payload, "comparison": comparison}
