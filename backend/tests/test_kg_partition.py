"""主题板块划分纯计算模块(`app.services.kg_partition`)的回归门。

承规格 `docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md` §五 S2(方案 C)
与计划 P4-T9。全部不起库:输入是 canonical id 列表 + 列式边表(与 `CanonicalEdgeTable`
同形态),输出是板块、board_key、度数与续用统计。

夹具都是**结构上**能说清答案的图(团、环、哑铃、孤岛),断言钉的是性质(不动点、与输入
顺序无关、局部性、续用阈值),不钉 igraph 某一版的具体随机路径。
"""
from __future__ import annotations

import itertools
import random
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import igraph
import numpy as np
import pytest

from app.services import kg_partition
from app.services.kg_partition import (
    BOARD_KEY_CARRY_JACCARD,
    PARTITION_MODE_COLD,
    PARTITION_MODE_COLD_FALLBACK,
    PARTITION_MODE_WARM,
    PreviousPartition,
    PreviousPartitionBuilder,
    mint_board_key,
    partition_boards,
)

MIN_SIZE = 3
Edge = Tuple[str, str, int]


# ------------------------------------------------------------------ 夹具


class _Graph:
    """按 canonical id 记边;节点列表按首次出现序(与 `rebuild_communities` 的 can2idx 同形)。"""

    def __init__(self) -> None:
        self.ids: List[str] = []
        self._seen: Dict[str, int] = {}
        self.edges: List[Edge] = []

    def edge(self, a: str, b: str, w: int = 1) -> None:
        for node in (a, b):
            if node not in self._seen:
                self._seen[node] = len(self.ids)
                self.ids.append(node)
        self.edges.append((a, b, w))

    def weight(self) -> int:
        return sum(w for _, _, w in self.edges)


def _clique(graph: _Graph, prefix: str, k: int, w: int = 1) -> List[str]:
    nodes = [f"{prefix}{i:02d}" for i in range(k)]
    for a, b in itertools.combinations(nodes, 2):
        graph.edge(a, b, w)
    return nodes


def _barbell(graph: _Graph, prefix: str, k: int, bridge_weight: int = 1) -> Tuple[List[str], List[str]]:
    left = _clique(graph, f"{prefix}a-", k)
    right = _clique(graph, f"{prefix}b-", k)
    graph.edge(left[0], right[0], bridge_weight)
    return left, right


def _triangle_ring(graph: _Graph, prefix: str) -> List[List[str]]:
    """4 个三角形 A-B-C-D-A 成环,相邻两团之间两条权 2 的边。

    {A∪B, C∪D} 与 {B∪C, D∪A} 由旋转对称**模块度恰好相等**(0.2143),都优于四团各自成块
    (0.1786)。
    """
    cliques = [_clique(graph, f"{prefix}{c}-", 3) for c in range(4)]
    for c in range(4):
        nxt = cliques[(c + 1) % 4]
        for j in range(2):
            graph.edge(cliques[c][j], nxt[j], 2)
    return cliques


def _k6_ring(graph: _Graph, prefix: str) -> None:
    """4 个 K6 成环、相邻团之间一条单位边。全图边权在 (512, 1024) 时两两成对最优、
    两种成对方式恰好并列——划分只能靠随机序列与节点编号来破平,正好用来测局部性。"""
    cliques = [_clique(graph, f"{prefix}{c}-", 6) for c in range(4)]
    for c in range(4):
        graph.edge(cliques[c][0], cliques[(c + 1) % 4][5])


def _planted(graph: _Graph, prefix: str, seed: int, *, groups: int, size: int,
             p_in: float, p_out: float, max_weight: int = 1) -> None:
    rng = np.random.default_rng(seed)
    nodes = [f"{prefix}{i:03d}" for i in range(groups * size)]
    for i, j in itertools.combinations(range(len(nodes)), 2):
        p = p_in if i // size == j // size else p_out
        if rng.random() < p:
            graph.edge(nodes[i], nodes[j], int(rng.integers(1, max_weight + 1)))


def _arrays(ids: Sequence[str], edges: Sequence[Edge]):
    index = {c: i for i, c in enumerate(ids)}
    src = np.array([index[a] for a, _, _ in edges], dtype=np.int32)
    dst = np.array([index[b] for _, b, _ in edges], dtype=np.int32)
    weight = np.array([w for _, _, w in edges], dtype=np.int32)
    return src, dst, weight


def _run(ids: Sequence[str], edges: Sequence[Edge], *, previous: Optional[Dict[str, str]] = None,
         warm: bool = False, min_size: int = MIN_SIZE):
    src, dst, weight = _arrays(ids, edges)
    prev = None if previous is None else PreviousPartition.from_mapping(ids, previous)
    return partition_boards(ids, src, dst, weight, min_size=min_size, previous=prev, warm=warm)


def _partition(graph: _Graph, **kw):
    return _run(graph.ids, graph.edges, **kw)


def _published(result) -> Dict[str, str]:
    """一代发布结果在下一代眼里的样子:canonical → board_key,只有保留下来的板块。"""
    return {m: b.key for b in result.boards for m in b.members}


def _boards(result) -> set:
    return {(b.key, b.members) for b in result.boards}


def _snapshot(ids: Sequence[str], result):
    """与节点下标无关的完整输出:板块、每个 canonical 的板块 key 与度数、统计。"""
    key_of = {}
    degree_of = {}
    for i, cid in enumerate(ids):
        b = int(result.board_of_node[i])
        key_of[cid] = None if b < 0 else result.boards[b].key
        degree_of[cid] = int(result.degree[i])
    return (result.mode, tuple((b.key, b.members, b.carried) for b in result.boards),
            key_of, degree_of, result.carried, result.minted, result.rehashed,
            result.components, result.components_skipped, result.unstable_components)


def _groups(result) -> set:
    return {frozenset(b.members) for b in result.boards}


# ------------------------------------------------------------------ ① 不动点


def test_warm_start_from_its_own_output_is_a_fixed_point():
    """以本次发布的板块(只有成员数 >= min_size 的那些)热启动,输出逐字不变——冷启动的
    输出也一样。下一代的「什么都没变」因此真的什么都不变。

    这条不是 Leiden 的天然性质:igraph 的 ``n_iterations=-1`` 停在续下去的随机序列上,重置
    随机数后热启动会挪动节点(本机真实库挪了 2 个);模块靠逐轮重置、跑到不动点来保证。
    """
    graph = _Graph()
    _planted(graph, "p-", 7, groups=6, size=20, p_in=0.3, p_out=0.02, max_weight=3)
    _barbell(graph, "m-", 6)
    for t in range(4):
        _clique(graph, f"z{t}-", 3 + t)
    cold = _partition(graph)
    assert cold.mode == PARTITION_MODE_COLD and cold.unstable_components == 0
    warm = _partition(graph, previous=_published(cold), warm=True)
    assert warm.mode == PARTITION_MODE_WARM
    assert _boards(warm) == _boards(cold)
    assert np.array_equal(warm.board_of_node, cold.board_of_node)
    assert np.array_equal(warm.degree, cold.degree)
    assert (warm.carried, warm.minted, warm.rehashed) == (len(cold.boards), 0, 0)
    again = _partition(graph, previous=_published(warm), warm=True)
    assert _snapshot(graph.ids, again) == _snapshot(graph.ids, warm)


def _assert_same_partition(first, second) -> None:
    assert _boards(second) == _boards(first)
    assert np.array_equal(second.board_of_node, first.board_of_node)


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_a_discarded_round_that_differs_from_its_start_leaves_a_fixed_point(monkeypatch, seed):
    """稀疏的植入图上,作废的那一轮与它的起点**不同**——这正是「作废时输出起点、而不是输出
    这一轮的结果」要守的那条路。把增益下限调到够不着,让作废尽早发生:以冷启动输出热启动,
    第一轮做出同一个作废判断,原样输出。"""
    graph = _Graph()
    _planted(graph, "s-", seed, groups=12, size=25, p_in=0.15, p_out=0.01, max_weight=2)
    monkeypatch.setattr(kg_partition, "LEIDEN_MIN_ROUND_GAIN", 10.0)
    cold = _partition(graph)
    _assert_same_partition(cold, _partition(graph, previous=_published(cold), warm=True))


def _cliques_with_dangling_pairs(graph: _Graph, seed: int) -> None:
    """6 个近 K8 团连成链,每团挂 1–3 个悬挂对(对内权 2–8,以权 1 挂到团上):大分量里
    混着成员数不足 min_size 的社区。"""
    rng = np.random.default_rng(seed)
    cliques = []
    for c in range(6):
        nodes = [f"c{c}-{i}" for i in range(8)]
        for a, b in itertools.combinations(nodes, 2):
            if rng.random() < 0.85:
                graph.edge(a, b)
        cliques.append(nodes)
        for p in range(int(rng.integers(1, 4))):
            x, y = f"c{c}-p{p}x", f"c{c}-p{p}y"
            graph.edge(x, y, int(rng.integers(2, 9)))
            graph.edge(x, nodes[int(rng.integers(0, 8))])
            if rng.random() < 0.5:
                graph.edge(y, nodes[int(rng.integers(0, 8))])
    for c in range(5):
        graph.edge(cliques[c][0], cliques[c + 1][7])


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_fixed_point_holds_with_sub_min_size_communities_inside_a_large_component(seed):
    """大分量里有成员数不足 min_size 的社区时,它们不发布、下一代从单点起步——起点形式必须
    把它们拆成单点,停下时的起点才与下一代的起点逐字相同。"""
    graph = _Graph()
    _cliques_with_dangling_pairs(graph, seed)
    cold = _partition(graph)
    warm = _partition(graph, previous=_published(cold), warm=True)
    _assert_same_partition(cold, warm)
    _assert_same_partition(warm, _partition(graph, previous=_published(warm), warm=True))


def test_a_board_left_disconnected_is_split_in_the_start_not_after_the_fact(monkeypatch):
    """上一代把只挂在 Y 上的 f1–f2 留在了 X 的板块里。拆不连通的社区发生在**起点**里:
    f1–f2 这一代就从起点里的单点并进 Y,下一代原样不变。若改成输出前的后处理,这一代只会
    把它们踢出板块(增益够不着下限,并进 Y 的那一轮被作废),下一代才并进 Y——不动点破。"""
    graph = _Graph()
    x_nodes = _clique(graph, "x-", 8)
    y_nodes = _clique(graph, "y-", 8)
    graph.edge(x_nodes[0], y_nodes[0])
    graph.edge("f1", "f2")
    for f in ("f1", "f2"):
        graph.edge(f, y_nodes[1])
        graph.edge(f, y_nodes[2])
    previous = {m: "cm-x" for m in x_nodes + ["f1", "f2"]}
    previous.update({m: "cm-y" for m in y_nodes})
    monkeypatch.setattr(kg_partition, "LEIDEN_MIN_ROUND_GAIN", 10.0)
    first = _partition(graph, previous=previous, warm=True)
    by_key = {b.key: set(b.members) for b in first.boards}
    assert by_key["cm-y"] == set(y_nodes) | {"f1", "f2"}
    assert by_key["cm-x"] == set(x_nodes)
    _assert_same_partition(first, _partition(graph, previous=_published(first), warm=True))


def test_every_published_board_is_internally_connected():
    graph = _Graph()
    _planted(graph, "g-", 13, groups=8, size=20, p_in=0.2, p_out=0.02, max_weight=2)
    _cliques_with_dangling_pairs(graph, 5)
    result = _partition(graph)
    adjacency: Dict[str, set] = {}
    for a, b, _ in graph.edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    for board in result.boards:
        members = set(board.members)
        seen = {board.members[0]}
        frontier = [board.members[0]]
        while frontier:
            node = frontier.pop()
            for nxt in adjacency[node] & members:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert seen == members, board.key


def test_a_round_that_boards_more_nodes_but_lowers_modularity_is_discarded(monkeypatch):
    """「让更多节点进入板块」那半条要求模块度不降。用一个假的 Leiden 把哑铃两团连同挂在 A 上
    的 z 并成一个社区(连通,归一之后仍是一块):进板块的节点多了一个,模块度却明显下降——
    这一轮必须作废。没有「不降」这个条件时它会被接受(也就能与「模块度回升、少进板块」的
    轮次来回振荡)。"""
    graph = _Graph()
    a_nodes, b_nodes = _barbell(graph, "", 6)            # a-00..a-05 / b-00..b-05
    graph.edge(a_nodes[1], "z")
    previous = {m: "cm-a" for m in a_nodes}
    previous.update({m: "cm-b" for m in b_nodes})
    ids = sorted(graph.ids)

    class _Round:
        def __init__(self, membership):
            self.membership = membership

    def fake_leiden(self, **kwargs):
        return _Round([0] * len(kwargs["initial_membership"]))

    monkeypatch.setattr(igraph.Graph, "community_leiden", fake_leiden)
    result = _run(ids, graph.edges, previous=previous, warm=True)
    assert {b.key: set(b.members) for b in result.boards} == {
        "cm-a": set(a_nodes), "cm-b": set(b_nodes)}
    assert result.board_of_node[ids.index("z")] == -1


# ------------------------------------------------------------------ ② 热启动保留给定的那一种


def test_warm_start_keeps_whichever_of_two_equal_partitions_it_was_given():
    """四团环的两种成对方式模块度恰好相等;热启动必须原样保留上一代给的那一种,并沿用它的
    board_key。冷启动只能给出其中一种,所以把热启动换成冷启动,两条断言必有一条失败。"""
    graph = _Graph()
    a, b, c, d = _triangle_ring(graph, "q")
    pairings = {
        "AB|CD": [a + b, c + d],
        "BC|DA": [b + c, d + a],
    }
    g = igraph.Graph(n=len(graph.ids))
    index = {cid: i for i, cid in enumerate(graph.ids)}
    g.add_edges([(index[x], index[y]) for x, y, _ in graph.edges])
    weights = [w for _, _, w in graph.edges]
    quality = {}
    for name, groups in pairings.items():
        membership = [0] * len(graph.ids)
        for label, members in enumerate(groups):
            for m in members:
                membership[index[m]] = label
        quality[name] = g.modularity(membership, weights=weights)
    separate = [0] * len(graph.ids)
    for label, members in enumerate((a, b, c, d)):
        for m in members:
            separate[index[m]] = label
    assert quality["AB|CD"] == pytest.approx(quality["BC|DA"], abs=1e-12)
    assert quality["AB|CD"] > g.modularity(separate, weights=weights)

    for name, groups in pairings.items():
        previous = {m: f"cm-{name}-{i}" for i, members in enumerate(groups) for m in members}
        result = _partition(graph, previous=previous, warm=True)
        assert _groups(result) == {frozenset(members) for members in groups}, name
        assert {b.key for b in result.boards} == {f"cm-{name}-0", f"cm-{name}-1"}
        assert result.carried == 2 and result.minted == 0
    cold = _partition(graph)
    assert _groups(cold) in [{frozenset(m) for m in groups} for groups in pairings.values()]


# ------------------------------------------------------------------ ③ 与输入顺序无关


def _shuffled(graph: _Graph, seed: int):
    rng = random.Random(seed)
    ids = list(graph.ids)
    rng.shuffle(ids)
    edges = [(b, a, w) if rng.random() < 0.5 else (a, b, w) for a, b, w in graph.edges]
    rng.shuffle(edges)
    return ids, edges


def test_edge_arrival_order_direction_and_node_order_do_not_change_the_result():
    """今天的划分按关系的首次出现序给节点编号,输入顺序一变板块就变;本模块按 canonical id
    排名编号、边按(lo, hi)排序,到达顺序、方向与节点列表的顺序都不进入结果。"""
    graph = _Graph()
    _planted(graph, "e-", 11, groups=4, size=30, p_in=0.12, p_out=0.03, max_weight=2)
    for t in range(3):
        _clique(graph, f"i{t}-", 4)
    reference = _partition(graph)
    previous = _published(reference)
    reference_warm = _partition(graph, previous=previous, warm=True)
    for seed in range(6):
        ids, edges = _shuffled(graph, seed)
        assert _snapshot(ids, _run(ids, edges)) == _snapshot(graph.ids, reference), seed
        assert (_snapshot(ids, _run(ids, edges, previous=previous, warm=True))
                == _snapshot(graph.ids, reference_warm)), seed


def test_duplicate_edges_are_summed_like_one_weighted_edge():
    graph = _Graph()
    _planted(graph, "d-", 3, groups=3, size=12, p_in=0.4, p_out=0.05, max_weight=3)
    # 拆开的两份不相邻:先喂所有 (a, b, 1),再按另一种顺序喂所有反向的 (b, a, w-1)。
    first_halves = [(a, b, 1) for a, b, w in graph.edges]
    second_halves = [(b, a, w - 1) for a, b, w in graph.edges if w > 1]
    random.Random(4).shuffle(second_halves)
    split = first_halves + second_halves
    assert len(second_halves) > 10
    assert _snapshot(graph.ids, _run(graph.ids, split)) == _snapshot(graph.ids, _partition(graph))


# ------------------------------------------------------------------ ④ 孤岛的 resolution


def test_an_island_is_partitioned_against_the_whole_graph_not_against_itself():
    """resolution = 分量边权 / 全图边权:孤岛优化的是全图模块度在它身上的那一项。

    · 两个三角形连一条边的小孤岛(总边权 7):整块是可证的唯一最优,不调 igraph;
    · 两个 K6 连一条权 20 的边(总边权 50,走 Leiden):按全图口径合并更好;若 resolution
      取 1(把孤岛当成全世界),两团各自成块——这正是规格 B' 那一行的切碎。
    """
    graph = _Graph()
    _planted(graph, "g-", 5, groups=5, size=20, p_in=0.3, p_out=0.02)
    left, right = _clique(graph, "t-a", 3), _clique(graph, "t-b", 3)
    graph.edge(left[0], right[0])
    k_left, k_right = _barbell(graph, "k-", 6, bridge_weight=20)
    total = graph.weight()
    island_weight = 2 * 15 + 20
    # 夹具自检:K6 哑铃不在「整块即最优」那一档,必须真的经过 Leiden;按全图口径合并两团的
    # 增益 20/m − 50·50/(2m²) 为正 ⟺ 40m > 2500。
    assert island_weight ** 2 >= 2 * total and 40 * total > 2500
    result = _partition(graph)
    groups = _groups(result)
    assert frozenset(left + right) in groups
    assert frozenset(k_left + k_right) in groups

    # 同一个哑铃单独按 resolution=1 跑会被切开:断言的「合并」确实来自全图口径。
    alone = _Graph()
    _barbell(alone, "k-", 6, bridge_weight=20)
    index = {cid: i for i, cid in enumerate(alone.ids)}
    g = igraph.Graph(n=len(alone.ids))
    g.add_edges([(index[x], index[y]) for x, y, _ in alone.edges])
    membership = g.community_leiden(
        objective_function="modularity", weights=[w for *_, w in alone.edges],
        resolution=1.0, n_iterations=2).membership
    assert len(set(membership)) > 1


def test_whole_component_shortcut_agrees_with_leiden_component_by_component():
    """总边权 m_c² < 2m 的分量直接整块成板块。逐个分量与 igraph Leiden(同一 resolution、
    seed 42)比对:Leiden 给的也恰好是一个社区。"""
    graph = _Graph()
    _planted(graph, "g-", 9, groups=6, size=20, p_in=0.3, p_out=0.02, max_weight=3)
    rng = np.random.default_rng(1)
    islands = []
    for t in range(30):
        size = int(rng.integers(3, 13))
        nodes = [f"s{t:02d}-{i:02d}" for i in range(size)]
        for i in range(1, size):                       # 随机生成树 → 连通
            graph.edge(nodes[int(rng.integers(0, i))], nodes[i], int(rng.integers(1, 4)))
        for _ in range(size // 2):                     # 再加几条弦
            x, y = rng.choice(size, size=2, replace=False)
            graph.edge(nodes[int(x)], nodes[int(y)], int(rng.integers(1, 4)))
        islands.append(nodes)
    total = graph.weight()
    result = _partition(graph)
    groups = _groups(result)
    checker = random.Random()
    for nodes in islands:
        members = set(nodes)
        edges = [(a, b, w) for a, b, w in graph.edges if a in members]
        m_c = sum(w for *_, w in edges)
        assert m_c * m_c < 2 * total                   # 夹具自检:都在这一档
        assert frozenset(nodes) in groups
        index = {cid: i for i, cid in enumerate(nodes)}
        g = igraph.Graph(n=len(nodes))
        g.add_edges([(index[a], index[b]) for a, b, _ in edges])
        igraph.set_random_number_generator(checker)
        try:
            checker.seed(42)
            membership = g.community_leiden(
                objective_function="modularity", weights=[w for *_, w in edges],
                resolution=m_c / total, n_iterations=2).membership
        finally:
            igraph.set_random_number_generator(random)
        assert len(set(membership)) == 1


# ------------------------------------------------------------------ ⑤ 局部性


def _locality_graph() -> _Graph:
    graph = _Graph()
    for r in range(8):
        _k6_ring(graph, f"r{r}-")
    _barbell(graph, "m-", 10)
    for t in range(3):
        _clique(graph, f"z{t}-", 3)
    return graph


@pytest.mark.parametrize("warm", [False, True])
def test_adding_or_removing_an_unrelated_island_leaves_other_components_alone(warm):
    """八个 K6 环(两种成对方式恰好并列,只能靠随机序列破平)+ 一个 K10 哑铃 + 几个小孤岛。
    增一个排名在最前面、要跑 Leiden 的孤岛,或删一个小孤岛,其余分量的板块成员与
    board_key 逐字不变。

    分量之间不共享随机序列(每轮重置 seed)、节点按 canonical id 排名编号,所以别的分量的
    结果只取决于它自己。全图边权的变化会改 resolution,但环的两种成对方式对称、并列不破。
    """
    graph = _locality_graph()
    base = _partition(graph)
    assert {len(b.members) for b in base.boards if b.members[0].startswith("r")} == {12}
    previous = _published(base)
    island = _Graph()
    _barbell(island, "a-", 7)                         # 排名最前;总边权 43,走 Leiden
    assert 43 * 43 >= 2 * (graph.weight() + island.weight())
    variants = {
        "added": graph.edges + island.edges,
        "removed": [e for e in graph.edges if not e[0].startswith("z0-")],
    }
    untouched = {(b.key, b.members) for b in base.boards if not b.members[0].startswith("z0-")}
    for name, edges in variants.items():
        ids = sorted({x for a, b, _ in edges for x in (a, b)})
        result = _run(ids, edges, previous=previous, warm=warm)
        others = {(b.key, b.members) for b in result.boards
                  if not b.members[0].startswith(("a-", "z0-"))}
        assert others == untouched, name
    added = _run(sorted({x for a, b, _ in variants["added"] for x in (a, b)}),
                 variants["added"], previous=previous, warm=warm)
    assert {frozenset(b.members) for b in added.boards if b.members[0].startswith("a-")} == {
        frozenset(f"a-a-{i:02d}" for i in range(7)), frozenset(f"a-b-{i:02d}" for i in range(7))}


# ------------------------------------------------------------------ ⑥ 续用阈值


def test_board_key_carries_at_half_jaccard_and_is_minted_just_below():
    """J ≥ 1/2 且互为最佳时沿用旧 key;旧板块的大小按它的全部成员算(含已不在图里的)。"""
    graph = _Graph()
    _planted(graph, "g-", 2, groups=4, size=15, p_in=0.4, p_out=0.02)
    half = _clique(graph, "h-", 4)                               # 4 个在图里
    star_hub = "s-hub"
    star = [star_hub] + [f"s-{i:02d}" for i in range(48)]
    for leaf in star[1:]:
        graph.edge(star_hub, leaf)                               # 49 个在图里
    previous = {m: "cm-half" for m in half}
    previous.update({f"h-gone-{i}": "cm-half" for i in range(4)})    # J = 4/8
    previous.update({m: "cm-star" for m in star})
    previous.update({f"s-gone-{i:02d}": "cm-star" for i in range(51)})  # J = 49/100
    assert BOARD_KEY_CARRY_JACCARD == (1, 2)
    for warm in (False, True):
        result = _partition(graph, previous=previous, warm=warm)
        by_members = {frozenset(b.members): b for b in result.boards}
        carried = by_members[frozenset(half)]
        assert carried.key == "cm-half" and carried.carried
        minted = by_members[frozenset(star)]
        assert not minted.carried
        assert minted.key == mint_board_key(sorted(star))
        assert result.previous_boards == 2
        assert result.carried == 1 and result.minted == len(result.boards) - 1


def test_an_old_board_split_in_two_equal_halves_goes_to_the_half_with_the_smaller_member():
    """J 恰好并列(各 1/2)时按最小成员消歧,结果与输入顺序无关。"""
    graph = _Graph()
    _planted(graph, "g-", 4, groups=3, size=15, p_in=0.4, p_out=0.02)
    first = _clique(graph, "x-1", 3)
    second = _clique(graph, "x-2", 3)
    previous = {m: "cm-old" for m in first + second}
    result = _partition(graph, previous=previous)
    by_members = {frozenset(b.members): b for b in result.boards}
    assert by_members[frozenset(first)].key == "cm-old"
    assert by_members[frozenset(second)].key == mint_board_key(sorted(second))
    ids, edges = _shuffled(graph, 3)
    assert _snapshot(ids, _run(ids, edges, previous=previous)) == _snapshot(graph.ids, result)


def test_a_minted_key_that_collides_inside_the_generation_is_rehashed_deterministically():
    graph = _Graph()
    _planted(graph, "g-", 6, groups=3, size=15, p_in=0.4, p_out=0.02)
    carrier = _clique(graph, "c-", 4)
    newcomer = _clique(graph, "n-", 3)
    collision = mint_board_key(sorted(newcomer))
    result = _partition(graph, previous={m: collision for m in carrier})
    by_members = {frozenset(b.members): b for b in result.boards}
    assert by_members[frozenset(carrier)].key == collision
    assert by_members[frozenset(newcomer)].key == mint_board_key(sorted(newcomer), 1)
    assert result.rehashed == 1
    keys = [b.key for b in result.boards]
    assert len(keys) == len(set(keys))
    assert _snapshot(graph.ids, _partition(graph, previous={m: collision for m in carrier})) == \
        _snapshot(graph.ids, result)


def test_unchanged_boards_mint_the_same_key_on_a_cold_start():
    """新铸的 key 是成员内容哈希:冷启动(不给上一代)时,成员没变的板块 key 也不变。"""
    graph = _Graph()
    _barbell(graph, "m-", 8)
    for t in range(3):
        _clique(graph, f"z{t}-", 4)
    first = _partition(graph)
    assert first.carried == 0 and first.minted == len(first.boards)
    assert all(b.key == mint_board_key(b.members) for b in first.boards)
    assert _boards(_partition(graph)) == _boards(first)


# ------------------------------------------------------------------ 上一代划分的构造


def test_previous_partition_builder_is_independent_of_row_order_and_counts_vanished_members():
    graph = _locality_graph()
    base = _partition(graph)
    index = {cid: i for i, cid in enumerate(graph.ids)}
    rows = [(m, b.key) for b in base.boards for m in b.members] + [("gone-1", base.boards[0].key)]
    results = []
    for seed in (0, 1):
        random.Random(seed).shuffle(rows)
        builder = PreviousPartitionBuilder(len(graph.ids))
        for cid, key in rows:
            builder.add(index.get(cid), key)
        previous = builder.build()
        assert int(previous.sizes.sum()) == len(rows)
        src, dst, weight = _arrays(graph.ids, graph.edges)
        results.append(partition_boards(graph.ids, src, dst, weight, min_size=MIN_SIZE,
                                        previous=previous, warm=True))
    assert _snapshot(graph.ids, results[0]) == _snapshot(graph.ids, results[1])
    builder = PreviousPartitionBuilder(2)
    builder.add(0, "k1")
    with pytest.raises(ValueError):
        builder.add(0, "k2")
    # 负下标不能被 numpy 静默当成「从末尾数」(`can2idx.get(c, -1)` 就会给出 -1)。
    for bad in (-1, 2):
        with pytest.raises(ValueError):
            builder.add(bad, "k3")
    builder.add(None, "k4")
    assert builder.build().label_of_node.tolist() == [0, -1]


# ------------------------------------------------------------------ min_size / 度数 / 统计


def test_components_below_min_size_are_not_partitioned_and_isolated_nodes_need_min_size_one():
    graph = _Graph()
    _barbell(graph, "m-", 5)
    graph.edge("p-1", "p-2")                                     # 两个节点的分量
    ids = graph.ids + ["solo"]                                    # 一个孤立节点
    result = _run(ids, graph.edges)
    assert result.components == 3 and result.components_skipped == 2
    assert result.board_of_node[ids.index("p-1")] == -1
    assert result.board_of_node[ids.index("solo")] == -1
    assert all(len(b.members) >= MIN_SIZE for b in result.boards)
    loose = _run(ids, graph.edges, min_size=1)
    assert loose.components_skipped == 0
    assert ("solo",) in {b.members for b in loose.boards}
    assert ("p-1", "p-2") in {b.members for b in loose.boards}


def test_degree_is_the_unique_neighbour_count_like_todays_igraph_degree():
    graph = _Graph()
    _planted(graph, "g-", 8, groups=4, size=10, p_in=0.5, p_out=0.05, max_weight=3)
    graph.edge("p-1", "p-2", 5)
    result = _partition(graph)
    index = {cid: i for i, cid in enumerate(graph.ids)}
    g = igraph.Graph(n=len(graph.ids))
    g.add_edges([(index[a], index[b]) for a, b, _ in graph.edges])
    assert result.degree.tolist() == g.degree()


def test_a_round_below_the_gain_floor_is_discarded_unless_it_boards_more_nodes(monkeypatch):
    """把增益下限调到不可能达到:每一轮都只能靠「让更多节点进入板块」被接受。

    · 冷启动第一轮把单点并成板块(进板块的节点变多),被接受;之后的轮次作废——输出仍是
      热启动的不动点(作废规则是确定性的,下一代第一轮做出同一个判断);
    · 热启动原样交回上一代的板块;
    · 新挂上来的一个概念,摆放它的增益远小于下限,照样进入板块,不会停在板块之外。
    """
    graph = _Graph()
    _planted(graph, "g-", 7, groups=6, size=20, p_in=0.3, p_out=0.02)
    _barbell(graph, "m-", 4)
    monkeypatch.setattr(kg_partition, "LEIDEN_MIN_ROUND_GAIN", 10.0)
    cold = _partition(graph)
    assert cold.unstable_components == 0
    published = _published(cold)
    assert set(published) == set(graph.ids)                      # 全部节点都在板块里
    # 作废的是第二轮,输出的是它的起点 = 第一轮的结果:与「只许跑一轮」逐字相同。
    with monkeypatch.context() as one_round_only:
        one_round_only.setattr(kg_partition, "LEIDEN_MAX_ITERATIONS", 1)
        first_round = _partition(graph)
    assert first_round.unstable_components >= 1
    assert _boards(first_round) == _boards(cold)
    previous = {m: f"cm-{key}" for m, key in published.items()}
    warm = _partition(graph, previous=previous, warm=True)
    assert _published(warm) == previous
    graph.edge("g-000", "g-new")
    grown = _partition(graph, previous=previous, warm=True)
    assert "g-new" in _published(grown)


def test_the_gain_floor_cuts_the_tail_of_rounds(monkeypatch):
    """增益下限是性能护栏:去掉它,结果仍是不动点,只是每个分量多跑几轮(大分量上每轮秒级)。
    在 20 张稀疏植入图上数 Leiden 调用次数:有下限 143 次,去掉下限 161 次(本机 igraph
    1.0.0);上界取两者之间。"""
    original = igraph.Graph.community_leiden
    calls = [0]

    def counting(self, *args, **kwargs):
        calls[0] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(igraph.Graph, "community_leiden", counting)
    for seed in range(20):
        graph = _Graph()
        _planted(graph, "t-", seed, groups=20, size=25, p_in=0.15, p_out=0.01, max_weight=2)
        _run(sorted(graph.ids), graph.edges)
    assert calls[0] <= 152


def test_unstable_components_are_counted_when_the_iteration_cap_is_hit(monkeypatch):
    graph = _Graph()
    _planted(graph, "g-", 7, groups=6, size=20, p_in=0.3, p_out=0.02)
    assert _partition(graph).unstable_components == 0
    monkeypatch.setattr(kg_partition, "LEIDEN_MAX_ITERATIONS", 1)
    assert _partition(graph).unstable_components >= 1


# ------------------------------------------------------------------ 随机数


def test_partitioning_leaves_the_global_random_state_and_igraphs_generator_alone():
    """今天的 `rebuild_communities` 会 ``random.seed(42)`` 改全局状态;本模块用专属实例,并把
    igraph 的随机数源还原成默认的 ``random`` 模块。"""
    graph = _Graph()
    _planted(graph, "g-", 7, groups=4, size=15, p_in=0.3, p_out=0.02)

    def igraph_draw():
        random.seed(1234)
        return igraph.Graph.Erdos_Renyi(n=30, p=0.2).get_edgelist()

    igraph.set_random_number_generator(random)
    expected = igraph_draw()
    random.seed(99)
    state = random.getstate()
    _partition(graph)
    assert random.getstate() == state
    assert igraph_draw() == expected


def test_a_failing_round_still_restores_igraphs_generator_and_releases_the_lock(monkeypatch):
    graph = _Graph()
    _planted(graph, "g-", 7, groups=4, size=15, p_in=0.3, p_out=0.02)

    def igraph_draw():
        random.seed(1234)
        return igraph.Graph.Erdos_Renyi(n=30, p=0.2).get_edgelist()

    igraph.set_random_number_generator(random)
    expected = igraph_draw()

    def broken_leiden(self, **kwargs):
        raise RuntimeError("leiden failed")

    with monkeypatch.context() as patched:
        patched.setattr(igraph.Graph, "community_leiden", broken_leiden)
        with pytest.raises(RuntimeError):
            _partition(graph)
    assert igraph_draw() == expected
    assert kg_partition._IGRAPH_RNG_LOCK.acquire(blocking=False)
    kg_partition._IGRAPH_RNG_LOCK.release()


def test_partition_algo_version_is_pinned_to_the_constants_it_depends_on():
    """改了任何一个影响划分或 board_key 的常量,都必须 bump `PARTITION_ALGO_VERSION`
    (调用方据此判「已发布划分是另一套算法的产物、要冷启动」)。改常量时连同这里一起改。"""
    assert (
        kg_partition.PARTITION_ALGO_VERSION,
        kg_partition.LEIDEN_SEED,
        kg_partition.LOUVAIN_FALLBACK_SEED,
        kg_partition.LEIDEN_MIN_ROUND_GAIN,
        kg_partition.LEIDEN_MAX_ITERATIONS,
        kg_partition.BOARD_KEY_CARRY_JACCARD,
        kg_partition.BOARD_KEY_PREFIX,
        kg_partition.BOARD_KEY_HEX_DIGITS,
    ) == (1, 42, 42, 1e-4, 64, (1, 2), "bk-", 24)


# ------------------------------------------------------------------ networkx 回退


def test_without_igraph_it_runs_todays_cold_louvain_and_says_so(monkeypatch):
    graph = _Graph()
    a, b = _barbell(graph, "m-", 5)
    t1, t2 = _clique(graph, "t1-", 3), _clique(graph, "t2-", 3)
    monkeypatch.setitem(sys.modules, "igraph", None)
    cold = _partition(graph)
    assert cold.mode == PARTITION_MODE_COLD_FALLBACK
    assert cold.unstable_components == 0 and cold.components_skipped == 0
    assert _groups(cold) == {frozenset(a), frozenset(b), frozenset(t1), frozenset(t2)}
    # 不热启动:给了上一代也是同一份冷启动结果,只拿它续用 board_key。
    previous = {m: f"cm-{i}" for i, board in enumerate(cold.boards) for m in board.members}
    warm = _partition(graph, previous=previous, warm=True)
    assert warm.mode == PARTITION_MODE_COLD_FALLBACK
    assert _groups(warm) == _groups(cold)
    assert warm.carried == len(warm.boards)
    ids, edges = _shuffled(graph, 5)
    assert _snapshot(ids, _run(ids, edges)) == _snapshot(graph.ids, cold)


# ------------------------------------------------------------------ 入参校验


@pytest.mark.parametrize("case", [
    "self_loop", "duplicate_id", "out_of_range", "zero_weight", "warm_without_previous",
    "misaligned_previous", "min_size_zero",
])
def test_invalid_input_is_rejected(case):
    ids = ["a", "b", "c"]
    src = np.array([0, 1], dtype=np.int32)
    dst = np.array([1, 2], dtype=np.int32)
    weight = np.array([1, 1], dtype=np.int32)
    kwargs = {"min_size": MIN_SIZE}
    if case == "self_loop":
        dst = np.array([0, 2], dtype=np.int32)
    elif case == "duplicate_id":
        ids = ["a", "b", "a"]
    elif case == "out_of_range":
        dst = np.array([1, 3], dtype=np.int32)
    elif case == "zero_weight":
        weight = np.array([1, 0], dtype=np.int32)
    elif case == "warm_without_previous":
        kwargs["warm"] = True
    elif case == "misaligned_previous":
        kwargs["previous"] = PreviousPartition.from_mapping(["a", "b"], {"a": "k"})
        kwargs["warm"] = True
    elif case == "min_size_zero":
        kwargs["min_size"] = 0
    with pytest.raises(ValueError):
        partition_boards(ids, src, dst, weight, **kwargs)


def test_previous_partition_rejects_duplicate_keys_and_out_of_range_labels():
    with pytest.raises(ValueError):
        PreviousPartition(keys=("k", "k"), sizes=np.array([1, 1]),
                          label_of_node=np.array([0, 1], dtype=np.int32))
    with pytest.raises(ValueError):
        PreviousPartition(keys=("k",), sizes=np.array([1]),
                          label_of_node=np.array([1], dtype=np.int32))
    with pytest.raises(ValueError):
        PreviousPartition(keys=("k",), sizes=np.array([1]),
                          label_of_node=np.array([0, 0], dtype=np.int32))
