"""KG 质量分析预计算产物(T2)的回归门。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md` §3.2/§3.3/§3.4。

分两层:
  · 纯折叠(head_community_ids / fold_cross_community_edges / SourceProfileFolder)
    不起库直接钉边界与并列消歧;
  · 端到端跑真 `rebuild_communities`,钉三张产物表的内容、账本的版本戳、
    「一次预计算原子可见」、以及每一条降级路径都**缺失**而不是写半份。

双后端一致性(同一批夹具在 SQLite / PostgreSQL 上产出同一份产物)在
`tests/postgres/test_knowledge_store_conformance.py` 里,那边有真 PostgreSQL 泳道。
"""
from __future__ import annotations

import json
import re
import weakref
from contextlib import contextmanager

import igraph
import numpy as np
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg_analysis_precompute import (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_KINDS,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_SOURCE_PROFILES,
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_SEQ_PAYLOAD_KEY,
    EDGE_INDEX_DTYPE,
    EDGE_WEIGHT_DTYPE,
    MAINSTREAM_COVERAGE,
    MAX_PERSISTED_COMMUNITY_EDGES,
    CanonicalEdgeTableBuilder,
    CrossCommunityEdgeFolder,
    SourceBoardCounter,
    SourceProfileFolder,
    analysis_ledger_is_current,
    artifact_cluster_seq_is_unknown,
    artifact_is_current,
    check_artifact_payloads,
    fold_cross_community_edges,
    head_community_ids,
    required_artifact_kinds,
    reusable_artifact_payloads,
    stamp_cluster_seq,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


# --------------------------------------------------------------- 纯折叠


def test_mainstream_coverage_threshold_is_a_named_half():
    """阈值必须是具名常量:它随产物写进账本,日后改口径旧产物才不会被误读。"""
    assert MAINSTREAM_COVERAGE == 0.5


def test_head_community_ids_keeps_the_board_that_crosses_the_threshold():
    # 6+3+1 = 10,阈值 5.0。最大板块 6 >= 5.0 → 头部只有它。
    head, covered, total = head_community_ids([("b", 3), ("a", 6), ("c", 1)])
    assert (head, covered, total) == (frozenset({"a"}), 6, 10)


def test_head_community_ids_covers_at_least_half_on_an_exact_tie():
    """两个等大板块:第一个恰好覆盖 50%。

    `>` 而不是 `>=` 的写法会在这一档返回空集(严格大于才收),而空的头部集合会让
    **每一个**来源的 mainstream_share 都变成 0.0 —— 报告会宣称整库都是关联稀疏来源。
    """
    head, covered, total = head_community_ids([("a", 4), ("b", 4)])
    assert head == frozenset({"a"})
    assert covered / total >= MAINSTREAM_COVERAGE


def test_head_community_ids_breaks_size_ties_by_id_not_by_input_order():
    # 3 个等大板块(5 each,总 15,阈值 7.5):累积到第二个才越过阈值,所以头部是
    # 字典序最小的两个。输入顺序绝不能影响结果 —— kept_rows 的顺序来自 Louvain。
    forward = head_community_ids([("z", 5), ("a", 5), ("m", 5)])[0]
    backward = head_community_ids([("m", 5), ("a", 5), ("z", 5)])[0]
    assert forward == backward == frozenset({"a", "m"})


def test_head_community_ids_is_empty_without_members():
    assert head_community_ids([]) == (frozenset(), 0, 0)
    assert head_community_ids([("a", 0), ("b", 0)]) == (frozenset(), 0, 0)


def _edge_table(pairs: dict, n_nodes: int | None = None):
    """把「(下标对) -> 权重」的字面量喂成一张 `CanonicalEdgeTable`。

    刻意走真 `CanonicalEdgeTableBuilder`(每对喂 weight 次)而不是直接拼数组:
    夹具因此同时覆盖建表侧的方向归一与权重聚合。
    """
    builder = CanonicalEdgeTableBuilder()
    for (left, right), weight in pairs.items():
        for _ in range(weight):
            builder.add(left, right)
    if n_nodes is None:
        n_nodes = 1 + max(
            (max(pair) for pair in pairs), default=-1
        )
    return builder.build(n_nodes)


def test_the_edge_table_builder_refuses_to_build_twice():
    """建表是**一次性**的(块拷完就放掉),第二次必须响亮失败而不是吐一张空表。

    静默的空表在这条路径上是最坏的一种错:它的含义是「这个库一条边都没有」——
    板块划分会被整个抹平,而没有任何一处报错。
    """
    builder = CanonicalEdgeTableBuilder()
    builder.add(0, 1)
    assert len(builder.build(2)) == 1
    with pytest.raises(RuntimeError, match="只能调一次"):
        builder.build(2)


def test_fold_cross_community_edges_normalizes_direction_and_splits_intra():
    # community_of_index[i] = 节点下标 i 的板块(刻意是按下标的 list,不是 str->str dict)
    folder = fold_cross_community_edges(
        _edge_table({
            (0, 1): 2,   # cm-b <-> cm-a
            (3, 2): 5,   # cm-b <-> cm-a,反向 —— 必须折进同一个键
            (1, 2): 7,   # cm-a 内部
            (0, 3): 4,   # cm-b 内部
        }),
        ["cm-b", "cm-a", "cm-a", "cm-b"],
    )
    assert folder.top_edges(10) == [("cm-a", "cm-b", 7)]
    assert (folder.intra_weight, folder.cross_weight) == (11, 7)


def test_fold_cross_community_edges_skips_canonicals_outside_kept_boards():
    """min-size 过滤掉的板块成员在 `communities` 里根本不存在,折进去就是悬空引用。"""
    folder = fold_cross_community_edges(
        _edge_table({(0, 1): 3, (0, 2): 9}),
        ["cm-a", "cm-b", None],   # 下标 2 的节点落选
    )
    assert folder.top_edges(10) == [("cm-a", "cm-b", 3)]
    assert (folder.intra_weight, folder.cross_weight) == (0, 3)


def test_fold_normalizes_pair_direction_by_board_id_not_by_node_index():
    """无向归一必须按**板块 id 字符串**,不是按板块的整数序号(设计 §3.32)。

    旧代码是 `(src, dst) if src <= dst else (dst, src)` —— 拿字符串比大小。向量化之后
    比的是整数序号,只有序号按 id 字符串排序分配,`np.minimum`/`np.maximum` 才同解。
    这里刻意让**下标序与 id 字典序相反**:按下标归一会输出 ("cm-z", "cm-a"),
    `kg_community_edges` 的每一行都会左右颠倒。
    """
    folder = fold_cross_community_edges(
        _edge_table({(0, 1): 3}), ["cm-z", "cm-a"],
    )
    assert folder.top_edges(10) == [("cm-a", "cm-z", 3)]


def test_edge_table_rows_keep_the_first_appearance_order_of_the_dict_it_replaced():
    """**设计 §3.32 不变量 1**:数组化后的边序必须与旧 dict 的插入序逐字一致。

    Louvain 对边的输入顺序敏感,而旧模型喂给 igraph 的是 `list(ew.keys())` ——
    Python dict 保持插入顺序,也就是每对 canonical 的**首次出现序**。数组化后若按
    排序序(或任何别的顺序)喂,板块划分会漂:已部署库的 `communities`、
    `community_seq`、一切依赖板块的产物跟着全漂。这不是性能问题,是正确性问题。

    ⚠ 规模刻意不小(几千条、重复率高):首次出现序靠 `argsort(kind="stable")` 保证,
    而 numpy 的非稳定排序在**小**数组上会退化成插入排序(碰巧也稳定)—— 只用一个
    四五条边的样例,把 `kind="stable"` 删掉照样绿。
    """
    # 手写的小样例先把「排序序 != 首次出现序」这件事说清楚。
    handmade = [(5, 2), (0, 9), (5, 2), (1, 4), (9, 0), (0, 3)]
    builder = CanonicalEdgeTableBuilder()
    for left, right in handmade:
        builder.add(left, right)
    table = builder.build(10)
    assert list(zip(table.src.tolist(), table.dst.tolist())) == [
        (2, 5), (0, 9), (1, 4), (0, 3),
    ]
    assert table.weight.tolist() == [2, 2, 1, 1]

    # 再拿一条确定性的长边流与「旧模型」的 dict 逐字对账。
    rng = np.random.default_rng(20260728)
    left = rng.integers(0, 120, size=6000, dtype=np.int64)
    right = rng.integers(0, 120, size=6000, dtype=np.int64)
    reference: dict = {}
    builder = CanonicalEdgeTableBuilder()
    for a, b in zip(left.tolist(), right.tolist()):
        if a == b:
            continue
        builder.add(a, b)
        key = (a, b) if a < b else (b, a)
        reference[key] = reference.get(key, 0) + 1
    table = builder.build(120)
    assert list(zip(table.src.tolist(), table.dst.tolist())) == list(reference)
    assert table.weight.tolist() == list(reference.values())


def test_cross_edge_folder_truncates_by_weight_and_reports_the_total():
    """明细表有硬上界(生产上跨板块边数没有任何结构性约束),截断绝不静默。"""
    folder = CrossCommunityEdgeFolder()
    folder.add("cm-a", "cm-b", 1)
    folder.add("cm-a", "cm-c", 9)
    folder.add("cm-b", "cm-c", 5)
    assert folder.total_edges == 3
    assert folder.cross_weight == 15
    # 按 weight 降序取,不是按 id 序 —— 俯瞰图要的是最重的那些边。
    assert folder.top_edges(2) == [("cm-a", "cm-c", 9), ("cm-b", "cm-c", 5)]
    assert folder.top_edges(0) == []


def test_cross_edge_folder_breaks_weight_ties_deterministically():
    """并列必须按 (src, dst) 升序 —— dict 的迭代顺序不能泄漏进产物。"""
    forward = CrossCommunityEdgeFolder()
    for pair in (("cm-z", "cm-y"), ("cm-a", "cm-b"), ("cm-m", "cm-n")):
        forward.add(pair[0], pair[1], 4)
    backward = CrossCommunityEdgeFolder()
    for pair in (("cm-m", "cm-n"), ("cm-a", "cm-b"), ("cm-z", "cm-y")):
        backward.add(pair[0], pair[1], 4)
    assert forward.top_edges(2) == backward.top_edges(2) == [
        ("cm-a", "cm-b", 4), ("cm-m", "cm-n", 4),
    ]


def test_cross_edge_folder_row_stream_matches_the_preaggregated_fold():
    """两条喂入路径(整数边权 dict / 逐行 weight=1)必须给出逐字相同的产物。

    只补账本那条路径按行喂,全量重建那条路径喂 `ew`。`ew` 只是「同一对 canonical 出现
    了几次」的预聚合,所以两者对每个板块对的合计恒等 —— 这条把它钉住。

    ⚠ 这条对照在数组化之后**变强了**:此前两条路径共用同一个 `add`,比对基本恒真;
    现在一边是标量 dict 累加(`CrossCommunityEdgeFolder`)、另一边是 argsort +
    `np.add.reduceat` 的向量化实现,归一方向、并列消歧、权重合计任何一处写歪都会分叉。
    """
    index_map = ["cm-a", "cm-a", "cm-b", "cm-c"]
    edge_weights = {(0, 2): 2, (1, 2): 1, (2, 3): 4, (0, 1): 3}

    streamed = CrossCommunityEdgeFolder()
    for (left, right), weight in edge_weights.items():
        for _ in range(weight):          # 同一对 canonical 出现 weight 次
            streamed.add(index_map[left], index_map[right], 1)

    pre_aggregated = fold_cross_community_edges(_edge_table(edge_weights), index_map)

    assert streamed.top_edges(10) == pre_aggregated.top_edges(10)
    assert streamed.intra_weight == pre_aggregated.intra_weight
    assert streamed.cross_weight == pre_aggregated.cross_weight
    assert streamed.total_edges == pre_aggregated.total_edges


def test_the_fold_is_differentially_identical_to_the_scalar_reference_at_scale():
    """同一条差分对照,换一个**规模够大、并列够多**的输入再钉一遍。

    上面那条只有四条边:并列消歧、多板块归一、截断这三处都还没被真正压到。这里用一条
    确定性的长边流(几千条、几十个板块、大量权重并列),两份实现必须给出逐字相同的
    `top_edges` / `intra_weight` / `cross_weight` / `total_edges`。
    """
    rng = np.random.default_rng(4162)
    n_nodes = 300
    left = rng.integers(0, n_nodes, size=8000, dtype=np.int64).tolist()
    right = rng.integers(0, n_nodes, size=8000, dtype=np.int64).tolist()
    # 板块 id 的字典序刻意与板块序号错开,归一方向写歪当场分叉。
    index_map = [
        None if node % 37 == 0 else f"cm-{(node * 91) % 40:03d}"
        for node in range(n_nodes)
    ]

    builder = CanonicalEdgeTableBuilder()
    reference = CrossCommunityEdgeFolder()
    for a, b in zip(left, right):
        if a == b:
            continue
        builder.add(a, b)
        reference.add(index_map[a], index_map[b], 1)

    folded = fold_cross_community_edges(builder.build(n_nodes), index_map)

    assert folded.total_edges == reference.total_edges > 100
    assert folded.intra_weight == reference.intra_weight > 0
    assert folded.cross_weight == reference.cross_weight > 0
    assert folded.top_edges(1000) == reference.top_edges(1000)
    assert folded.top_edges(7) == reference.top_edges(7)     # 截断也要同解


def test_the_fold_releases_its_input_edge_table():
    """codex 第 7/9 轮 P1 在新模型下的落点:折叠**释放**输入,不是遍历它留在原地。

    折叠结果的板块对数没有任何结构性上界(补账本那条路径用的是**另一代**合并结果上
    算出来的划分,跨板块比例可以任意高),最坏与边表同量级 —— 生产 836 万条。两份
    同时驻留就是在 437 GB 库的峰值时刻再叠一个峰值。

    ⚠ 释放必须由**持有者自己**做(`CanonicalEdgeTable.release`),不能靠被调用方
    `del` 形参:边表是 `rebuild_communities` 传给 `_compute_kg_analysis` 的实参,
    调用方栈帧上的那个名字在整个调用期间都活着。这条断言看的正是**调用方**手里那个
    对象 —— 被调用方自己 `del` 一遍是过不了的。
    """
    table = _edge_table({(0, 1): 5, (1, 2): 4, (2, 3): 3, (0, 2): 2, (1, 3): 1})
    index_map = ["cm-a", "cm-b", "cm-c", "cm-a"]
    reference = CrossCommunityEdgeFolder()
    for (left, right), weight in {
        (0, 1): 5, (1, 2): 4, (2, 3): 3, (0, 2): 2, (1, 3): 1
    }.items():
        reference.add(index_map[left], index_map[right], weight)

    folded = fold_cross_community_edges(table, index_map)

    assert len(table) == 0, "输入边表必须被释放,不是折完还钉在调用方栈帧上"
    assert (table.src.size, table.dst.size, table.weight.size) == (0, 0, 0)
    assert folded.top_edges(10) == reference.top_edges(10)
    assert (folded.intra_weight, folded.cross_weight) == (
        reference.intra_weight, reference.cross_weight
    )
    assert folded.total_edges == reference.total_edges


def test_the_fold_keeps_per_edge_state_in_int32_columns_not_in_a_dict():
    """设计 §3.32 的**容器形态**本身:每条边的状态只能在列式 ndarray 里。

    第 2/7/9 轮那三个 P1 的共同根源就是 `dict[(int, int), int]` —— 只要它是 dict,
    折叠就必然要么造第二份同量级结构、要么依赖一个**不缩哈希表**的 `popitem`。所以
    形态本身要有守卫:退回 dict/list 存边,这条当场红。
    """
    folded = fold_cross_community_edges(
        _edge_table({(0, 1): 3, (1, 2): 2, (0, 2): 1}),
        ["cm-a", "cm-b", "cm-c"],
    )
    columns = (folded._pair_lo, folded._pair_hi, folded._pair_weight)
    assert all(isinstance(column, np.ndarray) for column in columns), columns
    assert folded._pair_lo.dtype == folded._pair_hi.dtype == EDGE_INDEX_DTYPE
    assert folded._pair_weight.dtype == EDGE_WEIGHT_DTYPE
    assert not any(
        isinstance(getattr(folded, slot), (dict, set))
        for slot in folded.__slots__
    ), "折叠结果里出现了按条目计数的哈希容器"


def test_source_profile_folder_computes_the_four_documented_quantities():
    folder = SourceProfileFolder(frozenset({"cm-main"}))
    folder.add("src", "cm-main", 6)
    folder.add("src", "cm-side", 2)
    folder.add("src", None, 4)          # 没进任何板块的对象
    (source_id, n_objects, n_graph_objects, top, top_share, spread,
     mainstream) = folder.rows()[0]
    assert source_id == "src"
    assert n_objects == 12              # 6 + 2 + 4,含没进板块的
    assert n_graph_objects == 8         # 只有进了板块的进分母
    assert (top, top_share) == ("cm-main", 0.75)
    assert spread == 2                  # NULL 组不算一个板块
    assert mainstream == 0.75


def test_source_profile_folder_breaks_top_board_ties_by_id_regardless_of_order():
    """GROUP BY 的行到达顺序在两个后端上都未定义 —— 并列必须按 id 消歧。

    没有这条,同一份数据在 SQLite 与 PostgreSQL 上可以给出不同的 top_community_id,
    而两边都「没错」;parity 会以最难查的方式碎掉。
    """
    forward = SourceProfileFolder(frozenset())
    for community_id in ("cm-z", "cm-a", "cm-m"):
        forward.add("src", community_id, 4)
    backward = SourceProfileFolder(frozenset())
    for community_id in ("cm-m", "cm-a", "cm-z"):
        backward.add("src", community_id, 4)
    assert forward.rows()[0][3] == backward.rows()[0][3] == "cm-a"


def test_source_profile_folder_reports_a_fully_unboarded_source():
    """一个对象都没进板块的来源是最强的「关联稀疏」信号,必须出现在结果里而不是消失。"""
    folder = SourceProfileFolder(frozenset({"cm-main"}))
    folder.add("src", None, 3)
    assert folder.rows() == [("src", 3, 0, "", 0.0, 0, 0.0)]


def test_source_profile_rows_are_source_id_ordered():
    folder = SourceProfileFolder(frozenset())
    for source_id in ("src-c", "src-a", "src-b"):
        folder.add(source_id, "cm", 1)
    assert [row[0] for row in folder.rows()] == ["src-a", "src-b", "src-c"]


# ------------------------------------------- canonical → 板块 的内存折叠(第 13 轮)


def _by_source_and_board(counter) -> list:
    """折叠结果按 (来源, 板块) 排序 —— 断言不该钉住板块序号的分配顺序。"""
    return sorted(counter.drain(), key=lambda row: (row[0], row[1] or ""))


def test_source_board_counter_groups_by_source_and_board():
    """`SourceBoardCounter` 必须给出与旧那条 ``GROUP BY source_id, community_id``
    **逐字相同**的三元组 —— 那是原子发布唯一动到口径的地方。"""
    can2idx = {"k-a": 0, "k-b": 1, "k-c": 2}
    counter = SourceBoardCounter(can2idx, ["cm-1", "cm-1", "cm-2"])
    for source_id, canonical_id in (
        ("src-x", "k-a"), ("src-x", "k-b"), ("src-x", "k-c"),
        ("src-x", "k-outside"),          # 不是图上的节点 → 没进任何板块
        ("src-y", "k-c"),
    ):
        counter.add(source_id, canonical_id)

    assert _by_source_and_board(counter) == [
        ("src-x", None, 1),
        ("src-x", "cm-1", 2),
        ("src-x", "cm-2", 1),
        ("src-y", "cm-2", 1),
    ]


def test_source_board_counter_keeps_unboarded_canonicals_apart_from_boarded_ones():
    """板块序号 -1 与 0 只差一个偏移量,揉在一起就是把「没进板块」算成第一个板块。"""
    counter = SourceBoardCounter({"k-a": 0, "k-b": 1}, [None, "cm-first"])
    counter.add("src", "k-a")
    counter.add("src", "k-b")
    assert _by_source_and_board(counter) == [
        ("src", None, 1), ("src", "cm-first", 1),
    ]


def test_source_board_counter_refuses_to_drain_twice():
    """一次性:攒下的块被消化掉之后再折一次会拿到空结果 —— 一个静默的假答案。"""
    counter = SourceBoardCounter({"k-a": 0}, ["cm-1"])
    counter.add("src", "k-a")
    assert list(counter.drain()) == [("src", "cm-1", 1)]
    with pytest.raises(RuntimeError, match="只能调一次"):
        list(counter.drain())


def test_source_board_counter_survives_more_rows_than_one_chunk():
    """分块缓冲的接缝:块满换新块那一步写歪了,只有跨块的量才看得出来。"""
    rows = SourceBoardCounter._CHUNK + 7
    counter = SourceBoardCounter({"k-a": 0, "k-b": 1}, ["cm-1", "cm-2"])
    for index in range(rows):
        counter.add("src", "k-a" if index % 2 == 0 else "k-b")
    folded = dict(((board, count) for _s, board, count in counter.drain()))
    assert folded == {
        "cm-1": (rows + 1) // 2,
        "cm-2": rows // 2,
    }


def test_source_board_counter_keeps_no_python_container_per_row():
    """有界性的**结构性**证据:每行只落两个 int32,没有任何按行计数的 Python 容器。

    与 §3.32 给边表定的判据同一条 —— dict 形态在那边连挨了三轮 P1。
    """
    counter = SourceBoardCounter({"k-a": 0}, ["cm-1"])
    for _ in range(1000):
        counter.add("src", "k-a")
    buffers = [counter._src, counter._brd, *(c for pair in counter._full for c in pair)]
    assert all(isinstance(buffer, np.ndarray) for buffer in buffers)
    assert {buffer.dtype for buffer in buffers} == {np.dtype(EDGE_INDEX_DTYPE)}
    # 只按「来源」与「板块」开条目,不按行。
    assert len(counter._source_ids) == 1 and len(counter._board_ids) == 1


def _full_payloads(
    *, cluster_seq: int = 0, partition_rebuilt: bool = True, **overrides
) -> dict:
    """一批合法的账本 payload —— 簇世代照生产路径经 `stamp_cluster_seq` 盖上。

    夹具刻意**不手抄**那个 key:分类(哪几份依赖合并结果)一旦改了,夹具跟着改,
    不会出现「守卫按新分类查、夹具还按旧分类拼」这种两边各自自洽却对不上的局面。

    ``partition_rebuilt=False`` 拼出「只补账本」那一轮的形状:依赖板块的两份显式记
    「板块划分建在哪一代合并结果上无从判断」。
    """
    payloads = {kind: {"level": 0} for kind in ARTIFACT_KINDS}
    payloads[ARTIFACT_COMMUNITY_EDGES] = {"level": 0, "communities": 2}
    payloads.update(overrides)
    return stamp_cluster_seq(
        payloads, cluster_seq, partition_rebuilt=partition_rebuilt
    )


def _ledger(seqs: dict, *, cluster_seq: int = 0,
            partition_rebuilt: bool = True) -> dict:
    """``{kind: kg_seq}`` → `kg_analysis_artifact_rows` 的形状(闸真正吃的那个)。"""
    stamped = stamp_cluster_seq(
        {kind: {"level": 0} for kind in seqs}, cluster_seq,
        partition_rebuilt=partition_rebuilt,
    )
    return {
        kind: {"kg_mutation_seq": seq, "payload": stamped[kind],
               "created_at": "2026-07-27"}
        for kind, seq in seqs.items()
    }


def test_check_artifact_payloads_rejects_unknown_kinds():
    check_artifact_payloads(_full_payloads())
    with pytest.raises(ValueError, match="契约外的 kind"):
        check_artifact_payloads(_full_payloads(board_edges={}))


def test_check_artifact_payloads_rejects_a_missing_kind():
    """账本是整批重写的:少写一行,下游读到的是「从来没算过」,而且不会有任何报错。"""
    for kind in (ARTIFACT_CLUSTER_HISTOGRAM, ARTIFACT_LARGEST_CLUSTERS,
                 ARTIFACT_RELATION_PROVENANCE, ARTIFACT_COMMUNITY_EDGES):
        payloads = _full_payloads()
        payloads.pop(kind)
        with pytest.raises(ValueError, match="缺少必需的 kind"):
            check_artifact_payloads(payloads)


def test_check_artifact_payloads_allows_absent_profiles_only_without_boards():
    empty = _full_payloads()
    empty.pop(ARTIFACT_SOURCE_PROFILES)
    empty[ARTIFACT_COMMUNITY_EDGES] = dict(
        empty[ARTIFACT_COMMUNITY_EDGES], communities=0
    )
    check_artifact_payloads(empty)                      # 零板块:合法缺席

    with_boards = _full_payloads()
    with_boards.pop(ARTIFACT_SOURCE_PROFILES)
    with pytest.raises(ValueError, match="才允许缺席"):
        check_artifact_payloads(with_boards)            # 有板块却缺席 = 漏写


def test_check_artifact_payloads_demands_the_cluster_stamp_exactly_where_it_belongs():
    """簇世代的戳:依赖合并结果的四份必须有这个**键**,不依赖的那份必须没有。

    两个方向都拦是有理由的 —— 漏盖不会报错,只会让这个库的预计算**永远**重跑
    (闸判它不新鲜)、读侧永远报「合并世代未知」;多盖则相反,给一份输入根本没变的
    产物编造一个落后量。两种都是静默的错。

    ⚠ **值分两档**(codex 第 7 轮 P2):两条统计快照当场从 `concept_clusters` 重算,
    世代永远是知道的,必须是 int;依赖板块的两份可以是 ``None``(板块划分是库里现成
    的、建在哪一代无从判断)。但「键干脆不写」在任何一档都不许 —— 允许它就把「漏盖」
    与「盖不出来」揉成同一档,上面那道守卫当场作废。
    """
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        missing = _full_payloads()
        missing[kind] = {
            key: value for key, value in missing[kind].items()
            if key != CLUSTER_SEQ_PAYLOAD_KEY
        }
        with pytest.raises(ValueError, match="payload 必须带"):
            check_artifact_payloads(missing)
        # 盖了但既不是整数也不是 None,与没盖同档(库被手工改过)。
        garbled = _full_payloads()
        garbled[kind] = dict(garbled[kind], **{CLUSTER_SEQ_PAYLOAD_KEY: "7"})
        with pytest.raises(ValueError, match="payload 必须带"):
            check_artifact_payloads(garbled)
        # 显式的「无从判断」只有依赖板块的两份可以用。
        unknown = _full_payloads()
        unknown[kind] = dict(unknown[kind], **{CLUSTER_SEQ_PAYLOAD_KEY: None})
        if kind in BOARD_DEPENDENT_ARTIFACT_KINDS:
            check_artifact_payloads(unknown)
        else:
            with pytest.raises(ValueError, match="payload 必须带"):
                check_artifact_payloads(unknown)

    extra = _full_payloads()
    extra[ARTIFACT_RELATION_PROVENANCE] = dict(
        extra[ARTIFACT_RELATION_PROVENANCE], **{CLUSTER_SEQ_PAYLOAD_KEY: 3}
    )
    with pytest.raises(ValueError, match="不依赖合并结果"):
        check_artifact_payloads(extra)


def test_relation_provenance_is_the_only_kind_outside_the_cluster_classification():
    """分类的依据是「那条查询读没读 concept_clusters」,这里把结论钉死。

    钉住它不是形式主义:分类同时决定**哪些产物会被合并写入作废**。一刀切多算一份,
    每次纯合并写入都要白付一趟 836 万边的全表扫;少算一份,那份就永远陈旧而报告
    还说「与当前一致」—— 正是本次修复要消灭的那一档。
    """
    assert set(ARTIFACT_KINDS) - CLUSTER_DEPENDENT_ARTIFACT_KINDS == {
        ARTIFACT_RELATION_PROVENANCE
    }


def test_analysis_ledger_is_current_requires_every_kind_at_the_same_seq():
    full = _ledger({kind: 7 for kind in ARTIFACT_KINDS})
    assert analysis_ledger_is_current(full, 7, 0, has_boards=True)
    assert not analysis_ledger_is_current({}, 7, 0, has_boards=True)
    assert not analysis_ledger_is_current(full, 8, 0, has_boards=True)
    partial = dict(full)
    partial.pop(ARTIFACT_RELATION_PROVENANCE)
    assert not analysis_ledger_is_current(partial, 7, 0, has_boards=True)
    # 一行落后 = 整份不新鲜(否则「补一半」会被判成齐了)
    mixed = dict(full)
    mixed[ARTIFACT_LARGEST_CLUSTERS] = {
        **full[ARTIFACT_LARGEST_CLUSTERS], "kg_mutation_seq": 6,
    }
    assert not analysis_ledger_is_current(mixed, 7, 0, has_boards=True)


def test_analysis_ledger_is_current_also_tracks_the_cluster_generation():
    """簇世代漂了就必须补跑 —— 哪怕 KG 世代一动没动(codex 第 5 轮报的那一条)。

    ⚠ 与簇无关的那份**不能**被簇世代拖下水:否则每一次纯合并写入都要白付一趟
    836 万边的全表扫,而它的输入一个字节都没变。
    """
    ledger = _ledger({kind: 7 for kind in ARTIFACT_KINDS}, cluster_seq=3)
    assert analysis_ledger_is_current(ledger, 7, 3, has_boards=True)
    assert not analysis_ledger_is_current(ledger, 7, 4, has_boards=True)

    only_provenance = {ARTIFACT_RELATION_PROVENANCE: ledger[ARTIFACT_RELATION_PROVENANCE]}
    assert analysis_ledger_is_current(
        only_provenance, 7, 999, has_boards=False
    ) is (required_artifact_kinds(has_boards=False) <= {ARTIFACT_RELATION_PROVENANCE})
    # 直接对那一份问判据:簇世代再怎么漂,它都新鲜。
    assert artifact_is_current(
        ARTIFACT_RELATION_PROVENANCE,
        7,
        ledger[ARTIFACT_RELATION_PROVENANCE]["payload"],
        seq=7,
        cluster_seq=999,
    )
    # 依赖合并结果的那几份没盖戳(修复之前写下的行)= 不新鲜,而不是默认新鲜。
    unstamped = {
        ARTIFACT_CLUSTER_HISTOGRAM: {
            "kg_mutation_seq": 7, "payload": {"level": 0}, "created_at": "x",
        }
    }
    assert not artifact_is_current(
        ARTIFACT_CLUSTER_HISTOGRAM, 7, unstamped[ARTIFACT_CLUSTER_HISTOGRAM]["payload"],
        seq=7, cluster_seq=0,
    )


def test_analysis_ledger_without_boards_does_not_require_source_profiles():
    """零板块的库合法地没有来源画像;要求它存在会让这类库每次调用都重跑全部重活。"""
    without_profiles = _ledger({
        kind: 7 for kind in ARTIFACT_KINDS if kind != ARTIFACT_SOURCE_PROFILES
    })
    assert analysis_ledger_is_current(without_profiles, 7, 0, has_boards=False)
    assert not analysis_ledger_is_current(without_profiles, 7, 0, has_boards=True)


def test_reusable_payloads_only_carry_cluster_independent_kinds_at_the_same_seq():
    """复用只覆盖与簇无关、且 KG 世代未动的那几份 —— 判据与闸赖以成立的是同一条。

    ⚠ 判据走的是共享的 `artifact_is_current`,不是内联的 `seq ==`(第 8 轮把最后一处
    内联抄写也收了回去)。所以簇世代**单独**动过时复用照旧成立:与簇无关的那份没有第
    二条世代线,那才是它值得复用的全部理由。
    """
    ledger = _ledger({kind: 7 for kind in ARTIFACT_KINDS}, cluster_seq=3)
    assert set(reusable_artifact_payloads(ledger, 7, 3)) == {
        ARTIFACT_RELATION_PROVENANCE}
    # 簇世代动了、KG 世代没动:纯合并写入的形态,与簇无关的那份仍然可复用
    # (它一个字都没提 concept_clusters)——否则每次补账本都要白扫一趟关系表。
    assert set(reusable_artifact_payloads(ledger, 7, 4)) == {
        ARTIFACT_RELATION_PROVENANCE}
    # KG 世代动了 → 输入可能变了 → 一份都不复用。
    assert reusable_artifact_payloads(ledger, 8, 3) == {}


# ------------------------------------------------------------- 端到端


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _claim(local_id: str) -> dict:
    return {"local_id": local_id, "object_type": "claim",
            "payload": {"name": local_id, "section_path": "1"}, "evidence": []}


def _rel(source: str, target: str) -> dict:
    return {"source_local_id": source, "target_local_id": target,
            "edge_type": "supports", "evidence": []}


def _add_sources(repo, notebook_id: str, *source_ids: str) -> None:
    """`knowledge_relations.source_id` 有外键指向 sources,所以夹具必须建真来源行。"""
    with repo._write() as db:
        db.executemany(
            "INSERT INTO sources "
            "(id, notebook_id, title, source_type, status, parse_status, "
            " created_at, updated_at) "
            "VALUES (?,?,?, 'markdown', 'extracted', 'parsed', "
            "        '2026-01-01', '2026-01-01')",
            [(sid, notebook_id, sid) for sid in source_ids],
        )


def _seed(repo) -> str:
    """两个不等大的板块 + 一条跨板块边 + 三种「不该进画像」的对象。

    板块大小刻意不同(4 vs 3):头部板块集合的阈值是总成员的 50%,等大时哪个板块进头部
    取决于 id 的字典序,而 id 是随机铸的 —— 断言会变成掷骰子。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a", "src-b", "src-c", "src-d")
    # 大板块(4 个节点)全部来自 src-a
    repo.store_kg(
        notebook.id, "src-a",
        [_claim(name) for name in ("A", "B", "C", "G")],
        [_rel("A", "B"), _rel("B", "C"), _rel("A", "C"),
         _rel("C", "G"), _rel("A", "G")],
    )
    # 小板块(3 个节点)全部来自 src-b
    repo.store_kg(
        notebook.id, "src-b",
        [_claim(name) for name in ("D", "E", "F")],
        [_rel("D", "E"), _rel("E", "F"), _rel("D", "F")],
    )
    # 孤立对象:有来源、但不在图里 → n_graph_objects=0 的极端关联稀疏来源
    repo.store_kg(notebook.id, "src-c", [_claim("Z")], [])
    # 不挂来源的对象:共享同一个空 source_id,绝不能被算成一个「空来源」
    repo.store_kg(notebook.id, None, [_claim("N")], [])
    # 一条跨板块边(C 属大板块、D 属小板块)。store_kg 的 local_id 只在单次调用内有效,
    # 所以这条边直接按库内 id 写。
    with repo._connect() as db:
        ids = {
            json.loads(row["payload"])["name"]: row["id"]
            for row in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                (notebook.id,),
            )
        }
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id, notebook_id, source_id, source_object_id, target_object_id, "
            " edge_type, evidence, created_at) "
            "VALUES ('rel-cross',?, 'src-a', ?, ?, 'supports', '[]', '2026-01-01')",
            (notebook.id, ids["C"], ids["D"]),
        )
        # 一个 deprecated 对象:口径要求它不进 n_objects。
        db.execute(
            "UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
            (ids["Z"],),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, payload, evidence, source_id, "
            " created_at, updated_at) "
            "VALUES ('ko-live-c', ?, 'claim', 'approved', '{\"name\":\"Z2\"}', '[]', "
            "        'src-c', '2026-01-01', '2026-01-01')",
            (notebook.id,),
        )
    return notebook.id


def _artifacts(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        return {
            row["kind"]: {
                "seq": int(row["kg_mutation_seq"]),
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in db.execute(
                "SELECT kind, kg_mutation_seq, payload, created_at "
                "FROM kg_analysis_artifacts WHERE notebook_id=?", (notebook_id,),
            )
        }


def _edges(repo, notebook_id: str) -> list:
    with repo._connect() as db:
        return [
            (row["src_community_id"], row["dst_community_id"], int(row["weight"]))
            for row in db.execute(
                "SELECT src_community_id, dst_community_id, weight "
                "FROM kg_community_edges WHERE notebook_id=? "
                "ORDER BY src_community_id, dst_community_id", (notebook_id,),
            )
        ]


def _profiles(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        return {
            row["source_id"]: (
                int(row["n_objects"]), int(row["n_graph_objects"]),
                row["top_community_id"], float(row["top_share"]),
                int(row["community_spread"]), float(row["mainstream_share"]),
            )
            for row in db.execute(
                "SELECT * FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,)
            )
        }


def _boards(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        return {
            row["id"]: int(row["size"])
            for row in db.execute(
                "SELECT id, size FROM communities WHERE notebook_id=? AND level=0 "
                "AND generation = COALESCE((SELECT community_generation "
                "FROM unified_kg_state WHERE notebook_id=?),0)",
                (notebook_id, notebook_id),
            )
        }


def test_rebuild_writes_every_artifact_kind_under_one_version_stamp(repo):
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2

    artifacts = _artifacts(repo, notebook_id)
    assert set(artifacts) == set(ARTIFACT_KINDS)
    with repo._connect() as db:
        state = db.execute(
            "SELECT kg_mutation_seq, community_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    # 设计 §3.3:每一份产物都要能自证建于哪个 kg_mutation_seq,而且五份必须同一批。
    assert {entry["seq"] for entry in artifacts.values()} == {
        int(state["kg_mutation_seq"])
    }
    assert int(state["community_seq"]) == int(state["kg_mutation_seq"])


def test_the_real_rebuild_holds_no_second_copy_of_the_edge_graph(repo, monkeypatch):
    """codex 第 7 轮 P1 的**真实路径**守卫,数组模型下的落点:边表不得有第二份。

    第 7 轮抓到的是 `list(ew.keys())` —— 那份 Louvain 输入拷贝与 `ew` 共享同一批 key
    元组,只要它还活在 `rebuild_communities` 的栈帧上,「消化一条释放一条」就一个字节
    都释放不掉。数组化把那个别名结构性地消掉了(`igraph_edges()` 是独立缓冲区的临时
    值),但**同一类**缺陷换个形态照样能回来:

      · **Louvain 那张 igraph 图**就是今天的「第二份边表」:igraph 把 836 万条边整个
        拷进了自己的 C 结构,而它的全部产出(membership / degree)在折叠开始前早已被
        抄成 Python 侧的 comms/deg。它活着跨进折叠,就是原样的第 7 轮缺陷换了个持有者;
      · 三列若最终指向建表缓冲的**切片视图**,整块 65536 槽的缓冲会跟着活着 ——
        `base is not None` 当场抓到。⚠ 今天光把 `_drain_column` 的 `.copy()` 删掉是
        **抓不到**的:`build` 末尾那次 fancy-index 无条件产出新缓冲,视图只活在
        `build` 里。实测报红的是**组合变异** ——「`_drain_column` 返回视图」+「无重复边
        时省掉末尾那次拷贝」,而后者恰恰是一个看起来很自然的优化。这条守卫钉的就是
        那个组合;
      · 三列原始数组若被调用栈、容器或对象属性中的任何额外引用留住,弱引用会在真实
        折叠返回后继续存活,直接抓住没有释放的约 100 MB 底层缓冲。

    直接检查三列大缓冲的生命周期,而不是断言 ``sys.getrefcount`` 的精确值。CPython
    3.14 会借用操作数栈上的引用,同一条调用路径的计数从 4 变成 2;官方也只保证 0/1
    对判断唯一引用有意义。``release()`` 会原位替换 wrapper 的三列,所以 wrapper 自身
    多一个别名并不妨碍释放;真正的回归是旧数组仍被某处持有。
    """
    notebook_id = _seed(repo)

    from app.services import knowledge_lifecycle as lifecycle_module

    original = lifecycle_module.fold_cross_community_edges
    observed: list = []
    graph_ref: list = []
    original_graph_type = igraph.Graph

    def recording_graph(*args, **kwargs):
        graph = original_graph_type(*args, **kwargs)
        graph_ref.append(weakref.ref(graph))
        return graph

    def _probe(edge_table, community_of_index):
        edge_count = len(edge_table)
        column_bases = (
            edge_table.src.base, edge_table.dst.base, edge_table.weight.base,
        )
        column_refs = tuple(
            weakref.ref(column)
            for column in (edge_table.src, edge_table.dst, edge_table.weight)
        )
        graph_alive = bool(graph_ref) and graph_ref[-1]() is not None
        folded = original(edge_table, community_of_index)
        observed.append((
            edge_count,
            tuple(ref() is not None for ref in column_refs),
            column_bases,
            graph_alive,
        ))
        return folded

    monkeypatch.setattr(igraph, "Graph", recording_graph)
    monkeypatch.setattr(lifecycle_module, "fold_cross_community_edges", _probe)
    assert repo.rebuild_communities(notebook_id) == 2

    assert len(observed) == 1
    assert graph_ref, "仪器没装上:Louvain 那张图一次都没被建"
    edges, columns_alive, column_bases, graph_alive = observed[0]
    assert edges > 0, "夹具必须真的有边,否则这条守卫是空的"
    assert not graph_alive, (
        "折叠时 Louvain 的 igraph 图还活着 —— 那是边表的第二份拷贝(836 万边),"
        "它的 membership/degree 早已被抄进 comms/deg"
    )
    assert column_bases == (None, None, None), (
        "边表的列是视图,底下那块更大的建表缓冲跟着活着"
    )
    assert columns_alive == (False, False, False), (
        f"折叠返回后边表三列仍被持有:{columns_alive!r};"
        "原始大缓冲没有按契约释放"
    )


def test_the_precompute_leaves_the_edge_graph_released(repo, monkeypatch):
    """走真 `rebuild_communities`:边表交给预计算之后,**调用方手里那份**必须已被释放。

    纯折叠层那条(`test_the_fold_releases_its_input_edge_table`)钉的是折叠器的行为;
    这一条钉的是**生产调用点真的把边表交出去了**,而不是先自己拷一份再喂 —— 那样
    折叠释放掉的就只是拷贝,`rebuild_communities` 的栈帧上照旧压着 GB 级的原件。
    """
    notebook_id = _seed(repo)
    captured: list = []
    original = repo._runtime.knowledge_lifecycle._compute_kg_analysis

    def _spy(*args, **kwargs):
        captured.append(args[4])          # edge_table(见 _compute_kg_analysis 签名)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_compute_kg_analysis", _spy
    )
    assert repo.rebuild_communities(notebook_id) == 2

    assert len(captured) == 1
    assert len(captured[0]) == 0
    assert (captured[0].src.size, captured[0].dst.size, captured[0].weight.size) == (
        0, 0, 0
    )


def test_cross_board_edges_fold_the_one_bridge(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)

    boards = _boards(repo, notebook_id)
    assert sorted(boards.values()) == [3, 4]
    big = next(cid for cid, size in boards.items() if size == 4)
    small = next(cid for cid, size in boards.items() if size == 3)
    expected = (min(big, small), max(big, small), 1)
    assert _edges(repo, notebook_id) == [expected]

    payload = _artifacts(repo, notebook_id)[ARTIFACT_COMMUNITY_EDGES]["payload"]
    assert payload["edges"] == 1
    assert payload["edges_total"] == 1
    assert payload["truncated"] is False
    assert payload["edge_limit"] == MAX_PERSISTED_COMMUNITY_EDGES
    assert payload["cross_weight"] == 1
    assert payload["communities"] == 2
    # 板块内边:大板块 5 条 + 小板块 3 条。
    assert payload["intra_weight"] == 8


def test_source_profiles_apply_the_documented_field_semantics(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)

    boards = _boards(repo, notebook_id)
    big = next(cid for cid, size in boards.items() if size == 4)
    small = next(cid for cid, size in boards.items() if size == 3)
    profiles = _profiles(repo, notebook_id)

    # 不挂来源的对象绝不能凝成一个「空来源」的画像。
    assert set(profiles) == {"src-a", "src-b", "src-c"}
    # src-a:4 个对象全在大板块。
    assert profiles["src-a"] == (4, 4, big, 1.0, 1, 1.0)
    # src-b:3 个对象全在小板块 —— 头部集合只有大板块(4/7 >= 50%),所以它的
    # mainstream_share 是 0.0。这正是「关联稀疏的来源」要暴露的形态。
    assert profiles["src-b"] == (3, 3, small, 1.0, 1, 0.0)
    # src-c:一个 deprecated(不计)+ 一个可用但不在图里的对象。
    assert profiles["src-c"] == (1, 0, "", 0.0, 0, 0.0)

    payload = _artifacts(repo, notebook_id)[ARTIFACT_SOURCE_PROFILES]["payload"]
    assert payload["sources"] == 3
    assert payload["mainstream_coverage"] == MAINSTREAM_COVERAGE
    assert (payload["head_communities"], payload["head_members"],
            payload["total_members"]) == (1, 4, 7)


def test_source_profiles_drop_hidden_sources_but_keep_orphan_references(repo):
    """两类**长得很像、处置必须相反**的来源,一条测试同时钉住。

      · 合成来源(`source_type IN ('memory','knowhow')`)→ **排除**。产品其余各处一律
        把它们当隐藏源;它们的 `title` 是用户内容(knowhow 的是表名、Memory 的是那条
        记忆的抬头),而且天生只连自己那一小片,会把「最不连通的来源」这张榜的头部占满。
      · 孤儿来源(`source_id` 在 `sources` 里没有对应行,历史清理会留下)→ **保留**,
        并在读侧标 `source_missing`。那是本视图**有意**的诊断能力。

    ⚠ 一条测试同时断两边是刻意的:这两件事由**同一个谓词**决定,而最容易犯的错正是
    「修好一边、顺手把另一边弄坏」—— 把 `NOT EXISTS` 写成 `JOIN sources` 或
    `LEFT JOIN … WHERE s.source_type NOT IN (…)`(NULL 让谓词为 NULL)就会把孤儿一起
    吞掉,而那两种写法对「排除合成来源」这一半是完全正确的。分成两条测试的话,
    先跑的那条会绿,读的人不一定注意到另一条在报什么。

    账本 payload 的 `sources` 一并断:排除发生在预计算,所以 `len(profiles)` 与读侧的
    分页 `total` 必须还是同一个口径 —— 只在读侧过滤的修法会让这两个数字分岔。
    """
    notebook_id = _seed(repo)
    with repo._write() as db:
        db.executemany(
            "INSERT INTO sources "
            "(id, notebook_id, title, source_type, status, parse_status, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?, 'extracted', 'parsed', '2026-01-01', '2026-01-01')",
            [
                ("src-knowhow", notebook_id, "季度奖金核算口径", "knowhow"),
                ("src-memory", notebook_id, "老板不喜欢周一开会", "memory"),
            ],
        )
        # 隐藏合成来源的对象:形态与 src-c 那条一模一样(可用、有来源、不在图里),
        # 唯一的差别就是来源类型 —— 所以排除只可能来自类型谓词。
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, payload, evidence, source_id, "
            " created_at, updated_at) "
            "VALUES (?,?, 'claim', 'approved', ?, '[]', ?, "
            "        '2026-01-01', '2026-01-01')",
            [
                ("ko-knowhow", notebook_id, '{"name":"KH"}', "src-knowhow"),
                ("ko-memory", notebook_id, '{"name":"MEM"}', "src-memory"),
                # 孤儿:src-deleted 在 sources 里根本没有行。
                ("ko-orphan", notebook_id, '{"name":"ORPH"}', "src-deleted"),
            ],
        )
    repo.rebuild_communities(notebook_id, force=True)

    profiles = _profiles(repo, notebook_id)
    assert "src-knowhow" not in profiles, (
        "knowhow 投影的来源进了来源画像 —— 它的标题是用户的表名"
    )
    assert "src-memory" not in profiles, "Memory 的合成来源进了来源画像"
    # 孤儿必须还在,而且带完整数据(1 个可用对象、不在图里)。
    assert profiles["src-deleted"] == (1, 0, "", 0.0, 0, 0.0), (
        "孤儿来源被一起排除了 —— 一个有意的诊断信号变成了静默丢弃"
    )
    assert set(profiles) == {"src-a", "src-b", "src-c", "src-deleted"}

    payload = _artifacts(repo, notebook_id)[ARTIFACT_SOURCE_PROFILES]["payload"]
    assert payload["sources"] == 4

    # 读侧:孤儿要能与「有来源但没标题」分得开。
    with repo._connect() as db:
        total, rows = repo._runtime.unified_kg.kg_source_profile_page(
            db, notebook_id, limit=50, offset=0
        )
    by_id = {row["source_id"]: row for row in rows}
    assert total == 4 and set(by_id) == set(profiles)
    assert by_id["src-deleted"]["source_missing"] is True
    assert by_id["src-deleted"]["title"] == ""
    assert by_id["src-a"]["source_missing"] is False
    # 泄漏面的直接断言:隐藏来源的标题一个字都不能出现在响应里。
    assert not {row["title"] for row in rows} & {
        "季度奖金核算口径", "老板不喜欢周一开会"
    }


def test_a_board_member_that_left_the_graph_still_places_its_objects(repo):
    """来源画像的板块映射走内存 membership 之后,**唯一**会与旧 SQL 分岔的那一档。

    旧形态 join `community_members`,所以只要 canonical 在成员表里就定得了位。新形态
    经 `can2idx`(= 本轮 canonical 边图的节点表)转一跳,于是「在板块里、但已经不是
    图上的节点」的 canonical 会掉出去 —— 那不是理论边角:补账本那条路径上板块划分读自
    库里现成的一套,而当前的边图是按**新的** cluster_map 现算的,一次合并把某个
    canonical 的边全变成自环,它就不再是节点了。掉出去的表现是那些对象被静默判成
    「没进任何板块」,`n_graph_objects` 凭空缩水、`mainstream_share` 跟着变 —— 一个
    没有任何报错的口径漂移。

    所以 `_compute_kg_analysis` 给这些成员补下标。这里用**最纯粹**的形态钉住它:一张
    空边图(一个 canonical 都不是节点)+ 一套库里现成的板块。少了那段补下标,
    `n_graph_objects` 会是 0。
    """
    notebook_id = _seed(repo)
    with repo._connect() as db:
        ids = {
            json.loads(row["payload"])["name"]: row["id"]
            for row in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                (notebook_id,),
            )
        }
    can2idx: dict = {}
    edge_table = CanonicalEdgeTableBuilder().build(0)
    kept_rows = [("cm-ghost", sorted([ids["A"], ids["B"]]))]

    _edges, profiles, payloads = (
        repo._runtime.knowledge_lifecycle._compute_kg_analysis(
            notebook_id, 0, 0, kept_rows, edge_table, can2idx,
            reusable=None, partition_rebuilt=False,
        )
    )

    by_source = {row[0]: row for row in profiles}
    # src-a 有 4 个可用对象,其中 A / B 属于那个板块。
    assert by_source["src-a"][1] == 4, "n_objects 变了 —— 本条只该影响板块映射"
    assert by_source["src-a"][2] == 2, (
        "板块成员因为不在当前边图上而被判成「没进任何板块」—— 与旧 SQL 分岔"
    )
    assert by_source["src-a"][3] == "cm-ghost"
    assert payloads[ARTIFACT_SOURCE_PROFILES]["total_members"] == 2


def test_statistic_snapshots_round_trip_the_query_layer_payloads(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    artifacts = _artifacts(repo, notebook_id)
    store = repo._runtime.unified_kg

    # 依赖合并结果的两份多带一个簇世代的戳(`stamp_cluster_seq` 盖的),其余逐字相同。
    def _unstamped(kind: str) -> dict:
        return {
            key: value for key, value in artifacts[kind]["payload"].items()
            if key != CLUSTER_SEQ_PAYLOAD_KEY
        }

    assert _unstamped(ARTIFACT_CLUSTER_HISTOGRAM) == json.loads(
        json.dumps(store.cluster_size_histogram(notebook_id))
    )
    assert _unstamped(ARTIFACT_LARGEST_CLUSTERS) == json.loads(
        json.dumps(store.largest_clusters(notebook_id, 20))
    )
    # 与合并无关的那份**不盖**戳:它逐字就是查询层的载荷。
    assert artifacts[ARTIFACT_RELATION_PROVENANCE]["payload"] == json.loads(
        json.dumps(store.relation_provenance_counts(notebook_id))
    )
    # 载荷结构必须仍能按类型分组还原(T1 的分组直方图)。
    groups = artifacts[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]["by_object_type"]
    assert [group["object_type"] for group in groups] == [
        "concept", "claim", "formula", "procedure", "other"
    ]


def test_a_single_board_still_gets_an_edge_ledger_row(repo):
    """空产物与缺失产物必须可区分:0 条跨板块边照样要有账本行。"""
    notebook = repo.create_notebook(NotebookCreate(name="one-board"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a")
    repo.store_kg(
        notebook.id, "src-a", [_claim(n) for n in ("A", "B", "C")],
        [_rel("A", "B"), _rel("B", "C"), _rel("A", "C")],
    )
    assert repo.rebuild_communities(notebook.id) == 1
    assert _edges(repo, notebook.id) == []
    ledger = _artifacts(repo, notebook.id)
    assert ledger[ARTIFACT_COMMUNITY_EDGES]["payload"]["edges"] == 0
    assert ledger[ARTIFACT_COMMUNITY_EDGES]["seq"] >= 1


PRODUCT_TABLES = ("kg_community_edges", "kg_source_profiles", "kg_analysis_artifacts")


ANALYSIS_SNAPSHOTS = (
    "cluster_size_histogram", "largest_clusters", "relation_provenance_counts",
)


def test_analysis_snapshots_refuse_to_run_inside_a_write_transaction(repo):
    """把全表级只读聚合放进写事务里必须**当场炸**,不是静默读到旧数据。

    两个危害:读的是提交前的库(过时报告,零报错),以及 `SqliteDatabase.write()` 的
    **进程级写锁**会被按住一整趟全表扫(同形状数据点:835 万边冷扫 39 分钟,期间全库
    写入排队)。
    """
    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    with repo._write():
        for name in ANALYSIS_SNAPSHOTS:
            with pytest.raises(RuntimeError, match="写事务"):
                getattr(store, name)(notebook_id)
        with pytest.raises(RuntimeError, match="写事务"):
            store.community_overview(notebook_id)


def test_precompute_takes_every_snapshot_outside_any_write_transaction(repo, monkeypatch):
    """**语义**守卫:三条全表重活被调用的那一刻,本线程的写深度必须是 0。

    为什么不能只靠下面那条形状守卫(「哪个写事务碰过产物表」):这三条查询一张产物表
    都不碰,在 SQLite 上还跑在**另一条**连接上,所以把它们从 `_write()` 外面搬到里面
    时,那条断言完全看不见 —— 评审实测过这个移动变异,25 条测试全绿。这里改成直接记录
    调用时刻的 `write_depth`,移动变异会当场把它顶成 1。
    """
    notebook_id = _seed(repo)
    database = repo._runtime.database
    store = repo._runtime.unified_kg
    depths: dict[str, list[int]] = {}

    def instrument(name):
        original = getattr(store, name)

        def wrapper(*args, **kwargs):
            depths.setdefault(name, []).append(
                getattr(database._local, "write_depth", 0)
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapper)

    for name in (*ANALYSIS_SNAPSHOTS, "source_canonical_rows"):
        instrument(name)
    repo.rebuild_communities(notebook_id)

    assert set(depths) == {*ANALYSIS_SNAPSHOTS, "source_canonical_rows"}, (
        f"仪器没装上或有查询没被调用:{sorted(depths)}"
    )
    assert all(depth == 0 for calls in depths.values() for depth in calls), (
        f"全表级重活在写事务内被调用(write_depth != 0):{depths}"
    )


def test_the_membership_is_released_before_the_three_statistic_snapshots(
    repo, monkeypatch
):
    """membership 只有来源画像用得到,用完必须当场放掉,不许跨进后面三条全表扫。

    这是原子发布带来的**新**驻留:板块划分要在落库之前完成 canonical → 板块 的映射,
    所以 `can2idx`(生产 ~171 万条 str->int,实测 109 MB)与 `community_of_index`
    (15 MB)必须活到来源画像折完。补偿的做法是把来源画像排在三条统计快照**之前**,
    折完就把两份一起放掉 —— 那三条自己要分配 O(分组数) 的临时结构(生产 200 万簇行 /
    836 万边),让 124 MB 的 membership 跨过它们就是又叠一个峰值。

    ⚠ 语义守卫,形状守卫在这里没用:membership 是纯 Python 对象,谁都不碰产物表。
    这里直接在每条统计快照被调用的那一刻看**调用方那份** `can2idx` 空没空 ——
    删掉 `can2idx.clear()`、或者把来源画像挪到三条快照**之后**,两种变异都会报红。
    """
    notebook_id = _seed(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    original_compute = lifecycle._compute_kg_analysis
    handed: list = []

    def spying_compute(*args, **kwargs):
        handed.append(args[5])              # can2idx(见 _compute_kg_analysis 签名)
        return original_compute(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_compute_kg_analysis", spying_compute)
    live_at: dict = {}
    store = repo._runtime.unified_kg
    for name in (*ANALYSIS_SNAPSHOTS, "source_canonical_rows"):
        original = getattr(store, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            live_at.setdefault(_name, []).append(bool(handed) and len(handed[-1]) > 0)
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapper)

    repo.rebuild_communities(notebook_id)

    assert handed, "仪器没装上:_compute_kg_analysis 一次都没被调用"
    assert set(live_at) == {*ANALYSIS_SNAPSHOTS, "source_canonical_rows"}, (
        f"仪器没装上或有查询没被调用:{sorted(live_at)}"
    )
    assert all(live_at["source_canonical_rows"]), (
        "来源画像那次扫描时 membership 已经空了 —— 板块映射根本没数据可用"
    )
    assert not any(any(live_at[name]) for name in ANALYSIS_SNAPSHOTS), (
        f"membership 活着跨过了三条统计快照:{live_at}"
    )


def test_the_detection_leftovers_are_released_before_the_precompute(repo, monkeypatch):
    """社区检测的中间结果不得活着跨进预计算(codex 第 14 轮 P2)。

    `kept_rows` 建好之后,`comms` 与 `idx2can` 装的是**同一批 canonical 字符串的另一份
    引用**(生产 ~171 万条),`g` 是 networkx 兜底那条路上的整张图。它们此前一直活到函数
    返回 —— 也就是活着跨过预计算里那四趟全表扫,把两个内存峰值叠在一起。

    ⚠ 与隔壁 `test_the_membership_is_released_before_the_three_statistic_snapshots`
    是同一条不变式的两半,但**观察方式必须不同**:`can2idx` 是传进 `_compute_kg_analysis`
    的实参、可以直接看,而 `idx2can` / `comms` 是 `rebuild_communities` 的局部变量,
    调用方拿不到。所以这里读调用方栈帧的 locals —— 释放发生在预计算**之前**,
    在预计算入口看一眼就够,不必逐条查询去查。

    这条挡两种形态:删掉释放,以及把释放**挪到**预计算之后 —— 后者 `grep` 找得到
    `idx2can.clear()`、看着还在,实际一点用没有。移动变异实测过:不加这条守卫时全绿。
    """
    import sys

    notebook_id = _seed(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    original_compute = lifecycle._compute_kg_analysis
    seen: list = []

    def spying_compute(*args, **kwargs):
        caller = sys._getframe(1)
        assert caller.f_code.co_name == "rebuild_communities", (
            f"仪器装错了帧:{caller.f_code.co_name}"
        )
        loc = caller.f_locals
        seen.append({
            "idx2can": len(loc.get("idx2can") or ()),
            "comms_present": "comms" in loc,
            "g_present": "g" in loc,
        })
        return original_compute(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_compute_kg_analysis", spying_compute)
    repo.rebuild_communities(notebook_id)

    assert seen, "仪器没装上:_compute_kg_analysis 一次都没被调用"
    assert seen[-1]["idx2can"] == 0, (
        f"idx2can 活着跨进了预计算(还有 {seen[-1]['idx2can']} 条):"
        "生产 ~171 万条 canonical 字符串会与预计算自己的峰值叠在一起"
    )
    assert not seen[-1]["comms_present"], "comms 活着跨进了预计算"
    assert not seen[-1]["g_present"], "networkx 的整张图活着跨进了预计算"


# 折叠结果**不得**跨过的那几条:三条全表统计快照 + 来源画像那次全表扫。
_SCANS_AFTER_THE_FOLD = (*ANALYSIS_SNAPSHOTS, "source_canonical_rows")


def test_the_fold_is_released_before_the_full_table_scans_that_follow(repo, monkeypatch):
    """codex 第 9 轮 P1-2:折叠结果取完 top-N 就必须当场整个放掉。

    它后面紧跟着四次全表扫(簇大小直方图 / 最大簇榜单 / 边出处构成 / 来源画像),
    每一条自己都要在库内分配 O(分组数) 的临时结构 —— 生产 200 万簇行、836 万边。
    让折叠结果跨过它们,就是把两个峰值叠在同一时刻,而这四条一个字节都用不到它。

    ⚠ 这是**语义**守卫,形状守卫在这里没用:折叠结果是一个纯 Python 对象,谁都不碰
    产物表,把 `del folder` 挪到方法末尾(甚至删掉)不会让任何既有断言变色。这里直接
    对折叠结果挂 weakref,在每条重活被调用的那一刻记它死没死 —— 移动变异(把 `del`
    挪到三条查询之后)与删除变异都会当场报红。
    """
    notebook_id = _seed(repo)
    alive_at: dict[str, list[bool]] = {}
    fold_ref: list = []

    from app.services import knowledge_lifecycle as lifecycle_module

    original_fold = lifecycle_module.fold_cross_community_edges

    def spying_fold(*args, **kwargs):
        folded = original_fold(*args, **kwargs)
        # 弱引用挂在**权重列**上而不是折叠器对象上:它才是那份按板块对计数的内存,
        # 而且 ndarray 天然支持 weakref(折叠器带 `__slots__`,挂不上)。
        # 本闭包自己绝不能留强引用,否则这条守卫恒绿。
        fold_ref.append(weakref.ref(folded._pair_weight))
        return folded

    monkeypatch.setattr(
        lifecycle_module, "fold_cross_community_edges", spying_fold
    )
    # (Louvain 那张 igraph 图同样不该跨过这几条,但它由
    # `test_the_real_rebuild_holds_no_second_copy_of_the_edge_graph` 在更早的时刻
    # ——折叠开始那一刻——钉住,比这里严格,不在这里重复。)
    store = repo._runtime.unified_kg
    for name in _SCANS_AFTER_THE_FOLD:
        original = getattr(store, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            alive_at.setdefault(_name, []).append(
                bool(fold_ref) and fold_ref[-1]() is not None
            )
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapper)

    repo.rebuild_communities(notebook_id)

    assert fold_ref, "仪器没装上:折叠一次都没被调用"
    assert set(alive_at) == set(_SCANS_AFTER_THE_FOLD), (
        f"仪器没装上或有查询没被调用:{sorted(alive_at)}"
    )
    assert not any(any(calls) for calls in alive_at.values()), (
        f"折叠结果活着跨过了后续全表扫:{alive_at}"
    )


BOARD_TABLE = "communities"
# 板块的版本戳。它必须与板块**同事务** —— 分开提交会留下「板块已换、seq 还是旧的」的
# 窗口,而闸正是拿 seq 判「建过没有」。⚠ 少了它,把 `set_community_seq` 拆回自己的
# `with self._write()` 这个移动变异对下面两条形态守卫完全不可见(它碰的是
# `unified_kg_state`,不在产物表里)—— 实测过,那次变异真的全绿。
STATE_TABLE = "unified_kg_state"

# ⚠ 表名**不能**用裸子串匹配:trace 回调拿到的是参数已展开的 SQL,而账本 payload 里
# 恰好有一个 `"communities"` 字段(板块个数)—— 于是落库事务会被误判成板块重铸事务,
# 两类混成一类、断言全空。只认写语句里那个被写的标识符。
#
# ⚠ **只认写,裸 `FROM` 不算**(codex 第 16 轮 P2 之后)。下面两条守卫钉的都是「哪个
# 写事务**写过**哪张表」(重铸板块 / 记版本戳 / 落产物),而发布事务里现在有一次刻意的
# **读** —— `board_partition_still_holds` 的 `SELECT 1 FROM communities`(发布前复核那套
# 划分还在,不在就整趟放弃)。把只读的 `FROM` 也记成「碰过」,补账本那条守卫会报
# 「它重铸了板块」,而它一行板块都没写。这不是放松:`DELETE FROM communities` /
# `INSERT INTO communities` / `UPDATE unified_kg_state` 照旧逐条命中,「把
# `replace_communities` 或 `set_community_seq` 挪进/拆出发布事务」这两类移动变异一个都
# 没放过 —— 改这条正则的**同一轮**里重新实测过两次:把 `set_community_seq` 拆回自己的
# `with self._write()` → 下面第一条报红(`Extra items in the right set: 'unified_kg_state'`);
# 让补账本路径顺手调一次 `replace_communities` → 第二条报红
# (`Extra items in the left set: 'communities'`)。守卫的匹配规则一改就得重新验,
# 不能靠改之前那次实测背书。
#
# ⚠ 插入的**冲突子句变体**必须一起认。旧的裸 `INTO` 顺带覆盖了 `INSERT OR REPLACE INTO`
# / `INSERT OR IGNORE INTO` / `REPLACE INTO`(SQLite 方言)与 `INSERT INTO … ON CONFLICT`;
# 收紧成 `INSERT\s+INTO` 就把前三种漏掉了。今天这几张表的写语句**全是**裸 `INSERT INTO`
# (两侧 store 逐条查过),所以漏掉它们此刻不改变任何结论 —— 但这正是「守卫悄悄留下
# 未来的盲区」的形状:哪天有人给 `communities` 换成 upsert 写法,守卫会静默放行而不是
# 报红。把变体写进正则,代价是一个分支。
_DML_TABLE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|DELETE\s+FROM|UPDATE)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.I,
)


def _trace_product_table_transactions(repo, monkeypatch) -> list:
    """把每个写事务**写过**的表名记下来,供下面两条形态守卫共用。

    返回一个 list,每个元素是**一个写事务**碰过的表名集合(按开启顺序)。
    """
    database = repo._runtime.database
    original_write = database.write
    per_transaction: list[set] = []
    watched = {*PRODUCT_TABLES, BOARD_TABLE, STATE_TABLE}

    @contextmanager
    def tracing_write(**kwargs):
        with original_write(**kwargs) as db:
            touched: set = set()
            per_transaction.append(touched)

            def trace(statement: str) -> None:
                touched.update(watched.intersection(_DML_TABLE.findall(statement)))

            db.set_trace_callback(trace)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(database, "write", tracing_write)
    return per_transaction


def test_boards_and_all_three_product_tables_publish_in_one_transaction(
    repo, monkeypatch
):
    """codex 第 13 轮 P1 的**结构性**证据:板块与三张产物表落在**同一个**写事务里。

    修复前这里是三个事务(板块+作废 → 版本戳 → 产物),中间隔着分钟级的预计算,于是
    并发的 `/kg-analysis` 会读到「新板块 + 旧/缺失账本」。下面
    `test_no_reader_ever_sees_new_boards_with_the_previous_ledger` 钉的是**结果**
    (读者看到什么),这条钉的是**形态** —— 两条缺一不可:结果守卫要靠一次注入的读来
    观察,形态守卫则对「把发布重新拆成两个事务」这种改动直接报红,不依赖任何时序。

    判据是「碰产物表的写事务恰好一个,而且它就是碰 `communities` 的那一个」。
    把 `replace_kg_analysis_artifacts` 或 `set_community_seq` 拆回自己的 `with`,
    这条立刻红。
    """
    notebook_id = _seed(repo)
    per_transaction = _trace_product_table_transactions(repo, monkeypatch)
    repo.rebuild_communities(notebook_id)

    products = set(PRODUCT_TABLES)
    touching = [touched for touched in per_transaction if touched & products]
    assert len(touching) == 1, (
        f"产物被 {len(touching)} 个写事务碰过 —— 发布必须是**一个**事务:{touching}"
    )
    # 批 3·W2:板块行先写进**不可见的新代**(独立写事务),发布 = 指针翻转。
    # 判据随之从「发布事务碰 communities」换成「发布事务碰 unified_kg_state
    # (翻转 + 版本戳同事务)且不碰 communities」——可见性语义与旧形态等价:
    # 读者要么「旧代板块 + 旧账本」要么「新代板块 + 新账本」,中间态不存在。
    # (copy-forward 在今天的 level=0 调用面上复制集恒空,发布事务因此不写
    # communities;它一旦开始复制,这条断言会红,把新的形态摆到台面上再定。)
    assert touching[0] == products | {STATE_TABLE}, (
        "发布事务没有把指针翻转、版本戳与三张产物表一起写出去 —— "
        "中间态就是「新板块 + 旧账本」或「新板块 + 旧 seq」"
    )
    boarding = [touched for touched in per_transaction if BOARD_TABLE in touched]
    assert boarding, f"没有任何写事务写过新代板块行:{per_transaction}"
    assert all(not (touched & products) for touched in boarding), (
        f"写新代/回收板块行的事务携带了产物写 —— 发布必须整体骑在指针翻转"
        f"事务上:{per_transaction}"
    )


def test_the_backfill_transaction_publishes_products_without_touching_the_boards(
    repo, monkeypatch
):
    """「只补账本」那条路径的对称面:它不重铸板块,所以发布事务**不该**碰 `communities`。

    这一条同时是上面那条的反向护栏 —— 少了它,「无条件在发布事务里重写板块」也能让
    上面那条全绿,而那会把 171 万成员行的整表重写强加到一条**存在的全部理由就是避免
    那笔钱**的路径上。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    _wipe_ledger(repo, notebook_id)          # 图新鲜、账本缺失 → 只补账本

    per_transaction = _trace_product_table_transactions(repo, monkeypatch)
    repo.rebuild_communities(notebook_id)

    products = set(PRODUCT_TABLES)
    touching = [touched for touched in per_transaction if touched & products]
    assert len(touching) == 1, f"补账本也必须是一个事务:{touching}"
    assert touching[0] == products, (
        "补账本那一趟碰了板块表或版本戳 —— 它本来就不该重铸板块"
    )
    assert all(
        BOARD_TABLE not in touched and STATE_TABLE not in touched
        for touched in per_transaction
    ), f"补账本路径上有写事务碰了板块表 / 版本戳:{per_transaction}"


def test_no_reader_ever_sees_new_boards_with_the_previous_ledger(repo, monkeypatch):
    """codex 第 13 轮 P1 的**结果**守卫:预计算进行中,并发读绝不能看到新板块。

    复现(修复前的真实输出):板块表在预计算开始前就已经换成了新一代 id,而账本里
    依赖板块的两份被作废、三条统计快照还停在上一个 `kg_mutation_seq` —— 生产
    (878 万对象 / 836 万边)上这个窗口是**分钟级**的,报告同屏混着两代库。

    仪器挂在 `cluster_size_histogram` 上(预计算里的一条重活),用**另一条连接**读库,
    所以它看到的就是「已提交的东西」。修复后那一刻还什么都没提交:板块与账本都必须
    仍是上一轮那一套,整份报告内部自洽。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    before = (_boards(repo, notebook_id), _artifacts(repo, notebook_id))
    # KG 变动 → 下一次调用真的重建图并重铸板块 id。
    repo.store_kg(notebook_id, "src-d", [_claim("Q")], [])

    observed: list = []
    store = repo._runtime.unified_kg
    original = store.cluster_size_histogram

    def spying_histogram(*args, **kwargs):
        observed.append((_boards(repo, notebook_id), _artifacts(repo, notebook_id)))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "cluster_size_histogram", spying_histogram)
    repo.rebuild_communities(notebook_id)
    monkeypatch.undo()

    assert len(observed) == 1, "仪器没装上:预计算期间一次都没读到"
    mid_boards, mid_ledger = observed[0]
    after_boards = _boards(repo, notebook_id)
    assert set(after_boards) != set(before[0]), "板块 id 没被重铸 —— 本条的前提不成立"
    assert mid_boards == before[0], (
        f"预计算进行中就看到了新板块:{sorted(mid_boards)} != {sorted(before[0])}"
    )
    assert mid_ledger == before[1], (
        "预计算进行中账本已经变了 —— 板块与产物必须一起出现,不能各自提交"
    )


def test_reminting_boards_discards_the_board_dependent_products_in_the_same_transaction(
    repo, monkeypatch
):
    """P2:板块 id 一被重铸,依赖板块的两份产物必须**同事务**作废。

    为什么必须同事务、而不是「反正预计算马上就整批重写了」:预计算是一段重活,中间
    可能失败、被取消、或者进程崩。任何一种都会把**悬空**的产物留在库里 —— 明细行指着
    已经不存在的板块 id,而 T3 的记忆化签名(state 的 seq + 账本行的 seq/created_at)
    对「同一个 `kg_mutation_seq` 上的 force 重铸」一个字段都不会变,已预热的缓存会
    **无限期**继续吐上一套板块。

    这条钉的是**形态**(作废与重铸同事务),不是结果 —— 结果由
    `test_a_failed_precompute_after_a_force_rebuild_stops_serving_the_old_boards` 钉。

    ⚠ 原子发布之后,「预计算成功」那一趟里这次作废是**冗余**的(紧接着的整批重写会把
    三张表整个删掉再写)。它守的是**失败**那一档:板块照旧发布、产物没算出来,少了这
    一句库里就会留下悬空明细行 + 一份追不住板块重铸的签名。所以这条测试刻意让预计算
    整段失败 —— 把作废删掉、或者挪到 `if analysis is not None:` 里面去,它都会报红。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    # KG 变动 → 下一次调用真的重建图(而不是走「只补账本」)。
    repo.store_kg(notebook_id, "src-d", [_claim("Q")], [])

    per_transaction = _trace_product_table_transactions(repo, monkeypatch)
    store = repo._runtime.unified_kg
    # 预计算整段失败:如果作废被挪进了落库事务,它会跟着一起回滚。
    monkeypatch.setattr(
        store, "cluster_size_histogram",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    repo.rebuild_communities(notebook_id)

    # 批 3·W2:重铸的**发布**是指针翻转事务(新代行早已写在不可见处),
    # 作废必须骑在同一个翻转事务上——判据从「碰 communities 的事务」换成
    # 「碰 unified_kg_state 且碰产物表的事务」(claim/release 只碰 state,
    # 写新代/回收只碰 communities,都不会混进来)。
    publishing = [t for t in per_transaction
                  if STATE_TABLE in t and (t & set(PRODUCT_TABLES))]
    assert len(publishing) == 1, (
        f"发布(翻转)不在恰好一个写事务里:{per_transaction}")
    assert publishing[0] == set(PRODUCT_TABLES) | {STATE_TABLE}, (
        "发布(指针翻转)事务没有同时作废依赖板块的产物"
    )
    # 落库失败了,所以库里剩下的必须是「作废已生效」的状态。
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}
    assert set(_artifacts(repo, notebook_id)) == {
        ARTIFACT_CLUSTER_HISTOGRAM, ARTIFACT_LARGEST_CLUSTERS,
        ARTIFACT_RELATION_PROVENANCE,
    }, "与板块无关的三条统计快照被连坐删掉了 —— 它们仍是可读的陈旧快照"


def test_a_failed_publish_leaves_the_library_exactly_as_it_was(repo, monkeypatch):
    """设计 §3.3 + 第 13 轮 P1:一次发布要么整批可见、要么完全不可见。

    先成功产出一批,再让第二批在**写完之后**炸 —— 发布事务必须整个回滚。原子发布之后
    「整个」比以前大了一圈:板块划分与版本戳也在同一个事务里,所以回滚之后库必须与
    修改前**逐字相同**(旧板块 + 完整的旧账本 + 旧的 `community_seq`),而不是留下
    「新板块 + 空账本」那种半截状态。

    ⚠ 发布事务失败**冒泡**,不像预计算失败那样被吞掉。两者的差别是实质的:预计算是
    派生报告的重活,它失败时板块仍然照常发布,返回值仍然诚实;而发布事务失败意味着
    **这一趟什么都没发生** —— 吞掉它再返回板块数就是谎报一次并不存在的重建。
    `replace_communities` 的失败一向冒泡,这里与它同一档。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    first_artifacts = _artifacts(repo, notebook_id)
    first_edges = _edges(repo, notebook_id)
    first_profiles = _profiles(repo, notebook_id)
    first_boards = _boards(repo, notebook_id)
    assert first_edges and first_profiles
    assert set(first_artifacts) == set(ARTIFACT_KINDS)

    # KG 变动 → seq 闸不再短路
    repo.store_kg(notebook_id, "src-d", [_claim("Q")], [])
    store = repo._runtime.unified_kg
    original = store.replace_kg_analysis_artifacts

    def explode(db, *args, **kwargs):
        original(db, *args, **kwargs)          # 先真的写进去
        raise RuntimeError("injected artifact write failure")

    monkeypatch.setattr(store, "replace_kg_analysis_artifacts", explode)
    with pytest.raises(RuntimeError, match="injected artifact write failure"):
        repo.rebuild_communities(notebook_id, force=True)
    monkeypatch.undo()

    # 发布整段回滚 —— 板块、产物、账本一个字节都没动。
    assert _boards(repo, notebook_id) == first_boards, (
        "板块被单独提交了 —— 发布不再是原子的"
    )
    assert _edges(repo, notebook_id) == first_edges
    assert _profiles(repo, notebook_id) == first_profiles
    assert "src-d" not in _profiles(repo, notebook_id)
    assert _artifacts(repo, notebook_id) == first_artifacts


def test_a_failed_precompute_after_a_force_rebuild_stops_serving_the_old_boards(
    repo, monkeypatch
):
    """P2 的**结果**守卫:force 重建成功 + 预计算失败,报告不得再吐上一套板块。

    这是评审报的那个窗口:板块 id 与成员都换了,而缓存签名(state 的全部 seq/dirty +
    账本每行的 seq/created_at)一个字段都没变 —— `force=True` 在**同一个**
    `kg_mutation_seq` 上重铸,账本又因为预计算失败而原封不动。已预热的缓存会
    **无限期**返回上一套板块,直到 LRU 淘汰或进程重启,而端点自称读的是 live 快照。

    ⚠ 必须先 `overview()` 预热一次,否则测的是「冷缓存」——那本来就不会错。
    """
    from app.services.kg_analysis import KgAnalysisService

    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    service = KgAnalysisService(
        database=repo._runtime.database,
        unified_kg=repo._runtime.unified_kg,
        now=lambda: "2026-01-03T00:00:00",
    )
    warmed = sorted(
        board["id"] for board in service.overview(notebook_id).boards.payload["communities"]
    )
    assert warmed == sorted(_boards(repo, notebook_id))

    monkeypatch.setattr(
        repo._runtime.unified_kg, "source_canonical_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    repo.rebuild_communities(notebook_id, force=True)
    monkeypatch.undo()

    live = sorted(_boards(repo, notebook_id))
    assert live != warmed, "force 重建没有重铸板块 id —— 本条的前提不成立"
    served = sorted(
        board["id"] for board in service.overview(notebook_id).boards.payload["communities"]
    )
    assert served == live, f"缓存吐出了已经不存在的板块:{served} != {live}"


def test_precompute_failure_is_reported_and_does_not_break_the_rebuild(repo, monkeypatch):
    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(
        store, "source_canonical_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    emitted = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", emitted.append)

    # 核心路径不受影响:社区照建、返回值照常。
    assert repo.rebuild_communities(notebook_id) == 2
    kinds = [event["kind"] for event in emitted]
    assert "kg_analysis_precompute_failed" in kinds
    assert "communities_rebuilt" in kinds
    failure = next(e for e in emitted if e["kind"] == "kg_analysis_precompute_failed")
    assert "RuntimeError" in failure["error"]
    # 产物缺失,不是半份。
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}


def test_precompute_does_not_swallow_a_user_abort(repo, monkeypatch):
    """取消不是失败:`KgBuildAborted` 必须冒泡,否则「中止」看起来像成功了。"""
    from app.services.kg.run_control import KgBuildAborted, KgBuildFailure

    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    aborted = KgBuildAborted(KgBuildFailure(code="cancelled", user_message="已中止"))

    def abort(*args, **kwargs):
        raise aborted

    monkeypatch.setattr(store, "source_canonical_rows", abort)
    with pytest.raises(KgBuildAborted):
        repo.rebuild_communities(notebook_id)


def test_disabled_community_layer_writes_no_artifacts(repo):
    notebook_id = _seed(repo)
    repo.settings.community_layer_enabled = False
    assert repo.rebuild_communities(notebook_id) == 0
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}


def test_refused_large_graph_build_writes_no_artifacts(repo, monkeypatch):
    """大库守卫拒建社区时,产物必须**缺失**,而不是写出一份没有板块的画像。"""
    import sys

    notebook_id = _seed(repo)
    monkeypatch.setitem(sys.modules, "igraph", None)   # networkx 回退
    monkeypatch.setattr(repo, "notebook_copy_stats", lambda _nb: {"copyable": False})
    monkeypatch.setattr(
        repo._runtime.scale_artifacts, "load", lambda _nb, allow_stale=False: None
    )
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    assert repo.rebuild_communities(notebook_id) == 0
    assert any(e.get("kind") == "community_build_refused" for e in events)
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}


def test_version_gate_does_not_recompute_when_the_kg_is_unchanged(repo, monkeypatch):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    created_at = {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    }
    calls = []
    store = repo._runtime.unified_kg
    original = store.source_canonical_rows
    monkeypatch.setattr(
        store, "source_canonical_rows",
        lambda *args, **kwargs: (calls.append(args), original(*args, **kwargs))[1],
    )
    repo.rebuild_communities(notebook_id)
    assert calls == []
    assert {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    } == created_at


def _merge_only_write(repo, notebook_id: str) -> int:
    """一次**只动合并结果**的写入 —— `kg_mutation_seq` 刻意不动(生产语义)。

    走的是真的写路径 `append_clusters`(facade 的公开入口,`incremental_fuse_source`
    也用它),不是手工 UPDATE 计数器:被测的正是「簇写路径不抬 KG 世代」这条真实行为。
    返回写入的簇成员行数。
    """
    with repo._connect() as db:
        object_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=? "
                "AND status != 'deprecated' ORDER BY id",
                (notebook_id,),
            )
        ]
    return repo.append_clusters(
        notebook_id,
        [{"canonical_id": "K-merged", "canonical_name": "merged",
          "member_object_id": object_id} for object_id in object_ids[:3]],
        object_type="concept",
    )


def _seqs(repo, notebook_id: str) -> tuple:
    with repo._connect() as db:
        row = db.execute(
            "SELECT kg_mutation_seq, cluster_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    return int(row["kg_mutation_seq"]), int(row["cluster_mutation_seq"])


def test_a_merge_only_write_does_not_move_the_kg_generation(repo):
    """本次修复的**前提事实**:合并写入只抬簇世代,KG 世代一动没动。

    钉住它是因为整条修复都建立在这上面 —— 如果哪天簇写路径开始 bump
    `kg_mutation_seq`,下面那几条守卫会全部变成恒真的空守卫而没人发现。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    kg_before, cluster_before = _seqs(repo, notebook_id)
    assert _merge_only_write(repo, notebook_id) == 3
    kg_after, cluster_after = _seqs(repo, notebook_id)
    assert kg_after == kg_before, "簇写路径不该抬 KG 世代"
    assert cluster_after == cluster_before + 1


def test_a_merge_only_write_makes_the_ledger_stale_and_a_plain_rebuild_heals_it(repo):
    """codex 第 5 轮:纯合并写入之后闸必须判「不新鲜」,而且非 force 调用就能补上。

    修复之前:闸只比 `kg_mutation_seq`,合并写入之后它直接短路 —— 簇大小直方图与最大
    簇榜单(**从 `concept_clusters` 算出来**)永远停在旧内容上,而账本的戳一动没动,
    读侧照报「与当前一致」。这里用**产物内容真的变了**来证明它确实重算过,不是只把
    戳改了一下。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    before = _artifacts(repo, notebook_id)
    _merge_only_write(repo, notebook_id)

    assert repo.rebuild_communities(notebook_id) == 2      # 刻意不带 force

    after = _artifacts(repo, notebook_id)
    kg_seq, cluster_seq = _seqs(repo, notebook_id)
    # 两条统计快照的内容真的变了(多了一个 3 成员的簇),不是只换了个戳。
    assert (before[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]["clusters"]
            < after[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]["clusters"])
    # 两条统计快照当场从 `concept_clusters` 重算,盖上当前簇世代;依赖板块的两份不能
    # ——它们描述的板块划分是库里现成的,详见下面那条专门的守卫。
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS - set(BOARD_DEPENDENT_ARTIFACT_KINDS)):
        assert after[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] == cluster_seq
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        assert after[kind]["seq"] == kg_seq
    # 与合并无关的那份仍然不带这个戳。
    assert CLUSTER_SEQ_PAYLOAD_KEY not in after[ARTIFACT_RELATION_PROVENANCE]["payload"]

    # 补完之后闸重新短路:再调一次不写任何东西(否则每次打开视图都在重算)。
    settled = {kind: entry["created_at"] for kind, entry in after.items()}
    repo.rebuild_communities(notebook_id)
    assert {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    } == settled


def test_a_backfilled_board_product_never_claims_the_current_merge_generation(repo):
    """codex 第 7 轮 P2:**混合世代的产物不可能被盖成「当前」**。

    「只补账本」那条路径沿用库里**现成的**板块划分(它跑在上一代合并结果上),而边与
    来源映射是按**当前** cluster_map 现算的 —— 拼出来的东西是混合世代的。修复之前它还
    要盖上当前簇世代的戳,于是 API 报「与当前一致」:那比「陈旧但内部自洽」更糟,陈旧
    至少是诚实的。

    现在这一档显式记 ``None`` = 无从判断:板块划分建在哪一代合并结果上,库里根本没有
    地方记(设计 §3.3 的已知缺口)。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    fresh = _artifacts(repo, notebook_id)
    _, cluster_seq_before = _seqs(repo, notebook_id)
    # 全量重建那一轮:板块刚跑出来,四份都名副其实地盖着当前簇世代。
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        assert fresh[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] == cluster_seq_before

    _merge_only_write(repo, notebook_id)
    assert repo.rebuild_communities(notebook_id) == 2       # 刻意不带 force
    after = _artifacts(repo, notebook_id)
    _, cluster_seq = _seqs(repo, notebook_id)
    assert cluster_seq == cluster_seq_before + 1

    for kind in sorted(BOARD_DEPENDENT_ARTIFACT_KINDS):
        payload = after[kind]["payload"]
        # 键必须在(不然「漏盖」与「盖不出来」就分不开了),值必须是 None。
        assert CLUSTER_SEQ_PAYLOAD_KEY in payload, kind
        assert payload[CLUSTER_SEQ_PAYLOAD_KEY] is None, kind
        assert artifact_cluster_seq_is_unknown(kind, payload), kind
        # 判据的三值:既不是「当前」也不是「落后」。
        assert artifact_is_current(
            kind, after[kind]["seq"], payload,
            seq=after[kind]["seq"], cluster_seq=cluster_seq,
        ) is None, kind

    # 而且不会因此永远重跑:闸把「无从判断」当作补不出来的那一档,再调一次不写任何东西。
    settled = {kind: entry["created_at"] for kind, entry in after.items()}
    repo.rebuild_communities(notebook_id)
    assert {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    } == settled

    # 反向:板块真的被重建的那一轮(force),戳必须回到名副其实的整数。
    repo.rebuild_communities(notebook_id, force=True)
    rebuilt = _artifacts(repo, notebook_id)
    for kind in sorted(BOARD_DEPENDENT_ARTIFACT_KINDS):
        assert rebuilt[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] == cluster_seq, kind


def test_an_empty_ledger_backfill_also_refuses_to_claim_a_merge_generation(repo):
    """B1(刚部署、账本一片空白)同样不许盖 —— 那一档**没有任何证据**判断簇世代。

    这条与上一条不是同一件事:上一条有账本可比,能证明簇世代动过;B1 连账本都没有,
    「簇世代自板块建成以来动没动过」根本无从判断。所以「簇世代变了就重建板块划分」
    那条路治不了这一档,而「不知道就说不知道」两档都治得了。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    _wipe_ledger(repo, notebook_id)

    assert repo.rebuild_communities(notebook_id) == 2       # 刻意不带 force

    artifacts = _artifacts(repo, notebook_id)
    assert set(artifacts) == set(ARTIFACT_KINDS)            # B1 照样补得出账本
    for kind in sorted(BOARD_DEPENDENT_ARTIFACT_KINDS):
        assert artifacts[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] is None, kind
    # 两条统计快照当场重算,世代是知道的 —— 不能被一起拖成「未知」。
    _, cluster_seq = _seqs(repo, notebook_id)
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS
                       - set(BOARD_DEPENDENT_ARTIFACT_KINDS)):
        assert artifacts[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] == cluster_seq, kind


def test_a_merge_only_backfill_does_not_rescan_the_relation_table(repo, monkeypatch):
    """成本主张:纯合并写入触发的补账本**不重扫关系表**。

    `relation_provenance` 只读 `knowledge_relations` + 两次端点探查,一个字都没提
    `concept_clusters`,而 KG 世代没动 ⟹ 它的输入一个字节都没变。它同时是五份里最贵的
    一份(生产 836 万边、每行两次随机 PK 探查)。一刀切作废等于每次合并都白付一趟。

    反向也钉住:`force=True` 必须重扫 —— 那是抽取口径/代码改了之后唯一的人工恢复手段,
    在它上面省这趟等于宣布旧载荷永远换不掉。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    baseline = _artifacts(repo, notebook_id)[ARTIFACT_RELATION_PROVENANCE]["payload"]

    store = repo._runtime.unified_kg
    calls: list[int] = []
    original = store.relation_provenance_counts
    monkeypatch.setattr(
        store, "relation_provenance_counts",
        lambda *args, **kwargs: (calls.append(1), original(*args, **kwargs))[1],
    )

    _merge_only_write(repo, notebook_id)
    assert repo.rebuild_communities(notebook_id) == 2
    assert calls == [], "纯合并写入的补账本不该重扫 knowledge_relations"
    # 复用的必须是**逐字相同**的载荷,不是一个空壳。
    assert (_artifacts(repo, notebook_id)[ARTIFACT_RELATION_PROVENANCE]["payload"]
            == baseline)

    repo.rebuild_communities(notebook_id, force=True)
    assert len(calls) == 1, "force 是人工恢复手段,必须真的重扫"


def test_replace_artifacts_consumes_edges_and_profiles_as_one_shot_iterators(repo):
    """落库入口按**可迭代**消费,不得要求 Sequence(len / 下标 / 二次遍历)。

    契约意义:落库这一刻栈帧上还压着折叠出来的板块对 dict(`ew` 本身已被
    `CrossCommunityEdgeFolder.drain` 边消费边释放,但换来的那份条目数同样没有结构性
    上界),store 再把行物化成一份完整列表就是在峰值上叠峰值。一次性迭代器能跑通,
    就说明它没有回头再读一遍。
    """
    notebook = repo.create_notebook(NotebookCreate(name="stream"))
    _add_sources(repo, notebook.id, "src-a")
    store = repo._runtime.unified_kg
    payloads = _full_payloads(**{ARTIFACT_COMMUNITY_EDGES: {"level": 0, "communities": 1}})
    edges = iter([("cm-a", "cm-b", 3)])
    profiles = iter([("src-a", 5, 4, "cm-a", 1.0, 1, 1.0)])
    with repo._write() as db:
        store.replace_kg_analysis_artifacts(
            db, notebook.id, 9, edges, profiles, payloads, "2026-01-01T00:00:00"
        )
    assert next(edges, None) is None and next(profiles, None) is None
    assert _edges(repo, notebook.id) == [("cm-a", "cm-b", 3)]
    assert set(_profiles(repo, notebook.id)) == {"src-a"}


def _wipe_ledger(repo, notebook_id: str) -> None:
    """把产物抹成「本特性上线前」的样子:社区图完好、账本一片空白。"""
    with repo._write() as db:
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM kg_community_edges WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,))


def test_aligned_communities_with_an_empty_ledger_still_get_backfilled(repo):
    """B1:社区已按当前 seq 建好、账本为空时,**非 force** 的调用必须补出账本。

    这正是生产 base 库的形态:它在本特性上线**之前**就已经 rebuild 过,
    `community_seq == kg_mutation_seq`。若预计算跟着社区图的闸走,部署之后任何非 force
    调用都会在闸上直接返回,账本永远是空的 —— 用户只看得到「产物缺失」,唯一恢复手段是
    force 全量重建(836 万边 join + Louvain + 重写 171 万成员行)。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    expected_edges = _edges(repo, notebook_id)
    expected_profiles = _profiles(repo, notebook_id)
    boards_before = _boards(repo, notebook_id)
    _wipe_ledger(repo, notebook_id)
    assert _artifacts(repo, notebook_id) == {}

    assert repo.rebuild_communities(notebook_id) == 2       # 刻意不带 force

    artifacts = _artifacts(repo, notebook_id)
    assert set(artifacts) == set(ARTIFACT_KINDS)
    with repo._connect() as db:
        state = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,)).fetchone()
    assert {entry["seq"] for entry in artifacts.values()} == {
        int(state["kg_mutation_seq"])
    }
    # 补账本**不得**动社区图:板块 id 与规模必须原样(重铸 id 会让既有引用全部失效)。
    assert _boards(repo, notebook_id) == boards_before
    # 两条喂入路径产出逐字相同的产物。
    assert _edges(repo, notebook_id) == expected_edges
    assert _profiles(repo, notebook_id) == expected_profiles


def test_backfill_does_not_rerun_louvain_or_rewrite_the_membership_tables(repo, monkeypatch):
    """补账本必须跳过整个重建:不重跑 Louvain、不整表重写 communities/community_members。

    这一条是 B1 的**成本**主张,不是它的正确性主张 —— 没有它,「给预计算一个自己的闸」
    可以被实现成「闸没过就整个重来」,那和 force=True 一样贵。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    _wipe_ledger(repo, notebook_id)

    store = repo._runtime.unified_kg
    calls: list[str] = []
    for name in ("write_communities_generation", "set_community_seq"):
        monkeypatch.setattr(
            store, name,
            (lambda n: lambda *a, **k: calls.append(n))(name),
        )
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", events.append)

    assert repo.rebuild_communities(notebook_id) == 2
    assert calls == [], f"补账本却重写了社区表:{calls}"
    kinds = [event["kind"] for event in events]
    # 事件必须如实说「只补了账本」,不能伪装成一次图重建。
    assert "kg_analysis_backfilled" in kinds
    assert "communities_rebuilt" not in kinds
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_a_failed_backfill_does_not_emit_the_success_event(repo, monkeypatch):
    """codex 第 8 轮 P2:预计算失败的那一趟**不许**再发「补齐了」。

    修复之前,补账本路径上的失败会在**同一次执行**里留下两条互相矛盾的事件:
    `kg_analysis_precompute_failed` 与 `kg_analysis_backfilled` —— 而账本其实仍然不
    完整。运维监控与任何事件消费方拿到的是相反的两个结论,对应的运维动作也相反。
    这条路径除了补账本什么都没做:预计算一失败,这一趟就等于什么都没发生。

    ⚠ **三件事必须一起钉住**,少一件这条守卫就能被错误的实现满足:
      1. 成功事件没了 —— 本条要修的那句谎话;
      2. 失败事件还在 —— 不是把两条都吞掉换个清静(那是静默失败,更糟);
      3. 失败**不冒泡**、返回值仍是库里真实的板块数 —— T2 的爆炸半径决定不变
         (一份辅助报告的失败不该掀翻 437 GB 生产库的图重建),而且社区还在库里,
         报「一个板块都没有」同样是谎话。
    成功那一侧由 `test_backfill_does_not_rerun_louvain_or_rewrite_the_membership_tables`
    钉着(它断言正常补账本**必须**发这条事件),所以「干脆永远不发」也过不去。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    boards_before = _boards(repo, notebook_id)
    _wipe_ledger(repo, notebook_id)        # 图新鲜、账本空 → 下一次走「只补账本」

    store = repo._runtime.unified_kg
    monkeypatch.setattr(
        store, "source_canonical_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", events.append)

    assert repo.rebuild_communities(notebook_id) == 2   # 不冒泡,返回真实板块数

    kinds = [event["kind"] for event in events]
    assert "kg_analysis_backfilled" not in kinds, (
        f"预计算失败了却仍然发了成功事件:{kinds}"
    )
    assert "kg_analysis_precompute_failed" in kinds, (
        f"失败被吞成了静默,一条事件都没留下:{kinds}"
    )
    # 账本确实没补上 —— 事件说的是实话,不是「补上了但事件漏发」。
    assert _artifacts(repo, notebook_id) == {}
    # 社区图一动没动:失败的是那份辅助报告,不是图重建。
    assert _boards(repo, notebook_id) == boards_before


def _usurp_the_board_partition(repo, notebook_id: str) -> list[str]:
    """模拟**另一个重建**(`force=True`)当场提交:整套板块换成新铸的 id。

    刻意**不**递归调 `rebuild_communities` —— 那会把闸、事件、返回值全搅进来,断言就分不
    清哪条事件是谁发的。这里直接按发布协议走一遍 store 原语(取号 → 写新代 →
    指针翻转 → 释放):抢占者在库里留下的痕迹与真正的 force 重建**逐字相同**
    (新代行 + community_generation 前进),而窗口精确落在「划分已经读回来、
    产物还没落库」那一刻。

    ⚠ 成员列表原样保留,所以板块**个数**不变。这是刻意的:把复核写成
    `COUNT(*) > 0` 这类计数判据的实现在这条夹具上完全看不出区别 —— 并发发布在
    没变的图上产出**同样多**的板块。守卫必须靠**代次**判(批 3·W2 起:每次
    发布都翻指针,代次没变 ⟺ 期间没有任何发布提交过),不是靠数量。
    """
    store = repo._runtime.unified_kg
    with repo._connect() as db:
        members = [
            json.loads(row["member_ids"] or "[]")
            for row in store.community_rows_for_summary(db, notebook_id, 0)
        ]
    fresh_ids = [f"cm-usurper-{index}" for index in range(len(members))]
    with repo._write() as db:
        claim = store.claim_derived_generation(db, notebook_id, ttl_seconds=3600)
        assert claim is not None, "夹具前提:没有别的在飞认领"
        store.write_communities_generation(
            db, notebook_id, 0, list(zip(fresh_ids, members)), {}, {},
            "2026-01-01T00:00:00", claim["generation"],
        )
        assert store.flip_community_generation(
            db, notebook_id, published_from=claim["community_generation"],
            generation=claim["generation"], now="2026-01-01T00:00:00",
        )
        store.release_derived_claim(db, notebook_id, claim["generation"])
    return fresh_ids


def _backfill_with_a_writer_racing_the_precompute(
    repo, monkeypatch, notebook_id: str, writer
) -> tuple[int, list[dict]]:
    """走真 `rebuild_communities` 的补账本路径,在预计算**返回之前**让 `writer` 提交。

    `_compute_kg_analysis` 跑在写事务**之外**(那是硬约束,见它的 docstring),所以这里
    就是生产上那个分钟级窗口的最小复现:划分已经从库里读回来了、产物还一行没写。
    """
    lifecycle = repo._runtime.knowledge_lifecycle
    original_compute = lifecycle._compute_kg_analysis
    fired: list[bool] = []

    def compute_then_let_the_writer_commit(*args, **kwargs):
        analysis = original_compute(*args, **kwargs)
        writer()
        fired.append(True)
        return analysis

    monkeypatch.setattr(
        lifecycle, "_compute_kg_analysis", compute_then_let_the_writer_commit
    )
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", events.append)
    kept = repo.rebuild_communities(notebook_id)      # 刻意不带 force
    assert fired, "仪器没装上:补账本路径上 _compute_kg_analysis 一次都没被调用"
    return kept, events


def test_a_backfill_discards_its_products_when_the_boards_were_replaced_under_it(
    repo, monkeypatch
):
    """codex 第 16 轮 P2:补账本期间板块被换掉,产物**一行都不许**发布。

    补账本那条路径的板块划分读自**写事务之外**(`community_rows_for_summary`),而随后的
    `_compute_kg_analysis` 在生产(878 万对象 / 836 万边)上是分钟级的。这条路径没有任何
    单飞守卫 —— `POST /notebooks/{id}/unified-kg/rebuild` 直接调它,`scripts/recluster_kg.py`
    与 `batch_ingest` 还从**别的进程**调 —— 所以那段窗口里一次 `force=True` 的重建完全
    可以把整套板块换掉。修复前那一趟照旧把按**旧** `kept_rows` 算出来的产物写进库:
    `kg_community_edges` / `kg_source_profiles` 的每一行都指向已不存在的板块 id,而它们的
    账本盖着「与当前一致」的戳。悬空 + 自称新鲜,比缺失更糟 —— 缺失至少是诚实的。

    ⚠ **四件事必须一起钉住**,少一件这条守卫就能被错误的实现满足:
      1. 成功事件没了(否则又是第 8 轮那句谎话的翻版);
      2. `kg_analysis_backfill_discarded` 在,且 reason 说得出是哪一档 —— 不是把两条
         都吞掉换个清静(那是静默失败);
      3. 账本一个字节没被这一趟改写;
      4. 两张明细表里没有任何一行指向已被删除的旧板块 id。
    正向那一侧由下面 `test_a_backfill_publishes_normally_when_nobody_touches_the_boards`
    钉着(同一套仪器、只换 writer),所以「干脆永远不发布」也过不去。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    stale_board_ids = set(_boards(repo, notebook_id))
    _wipe_ledger(repo, notebook_id)        # 图新鲜、账本空 → 下一次走「只补账本」

    fresh_ids: list[str] = []
    kept, events = _backfill_with_a_writer_racing_the_precompute(
        repo, monkeypatch, notebook_id,
        lambda: fresh_ids.extend(_usurp_the_board_partition(repo, notebook_id)),
    )

    # 抢占者确实提交了(仪器真的复现了那个窗口,不是靠什么都没发生换绿)。
    assert set(_boards(repo, notebook_id)) == set(fresh_ids)
    assert stale_board_ids and not (stale_board_ids & set(fresh_ids))

    kinds = [event["kind"] for event in events]
    assert "kg_analysis_backfilled" not in kinds, (
        f"板块已经被换掉,却仍然宣布「补齐了」:{kinds}"
    )
    discarded = [
        event for event in events
        if event["kind"] == "kg_analysis_backfill_discarded"
    ]
    assert len(discarded) == 1, f"放弃发布被吞成了静默,一条事件都没留下:{kinds}"
    assert discarded[0]["reason"] == "board_partition_replaced"
    assert discarded[0]["notebook_id"] == notebook_id
    # 账本一个字节没动 —— 事件说的是实话。
    assert _artifacts(repo, notebook_id) == {}
    # 两张明细表里一行悬空引用都没有(这才是这条修复要挡的实际后果)。
    referenced = {
        board_id
        for src, dst, _weight in _edges(repo, notebook_id)
        for board_id in (src, dst)
    } | {
        row[2] for row in _profiles(repo, notebook_id).values() if row[2]
    }
    assert not (referenced & stale_board_ids), (
        f"产物里留下了指向已删除板块的悬空行:{sorted(referenced & stale_board_ids)}"
    )
    assert (_edges(repo, notebook_id), _profiles(repo, notebook_id)) == ([], {})
    # 返回值仍是读到那一刻的板块数:那些板块确实存在过,而放弃的只是一份辅助报告。
    assert kept == 2


def test_a_backfill_publishes_normally_when_nobody_touches_the_boards(
    repo, monkeypatch
):
    """上面那条的**正向对照**:同一套仪器、唯一的差别是抢占者什么都不做。

    没有它,「复核永远判失效、什么都不发布」也能让上面那条全绿 —— 而那会让本特性在
    生产上永远补不出账本(B1 原地复发)。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    board_ids = set(_boards(repo, notebook_id))
    _wipe_ledger(repo, notebook_id)

    kept, events = _backfill_with_a_writer_racing_the_precompute(
        repo, monkeypatch, notebook_id, lambda: None
    )

    kinds = [event["kind"] for event in events]
    assert "kg_analysis_backfilled" in kinds, f"正常补账本却没发成功事件:{kinds}"
    assert "kg_analysis_backfill_discarded" not in kinds, (
        f"没人碰板块,复核却判了失效:{kinds}"
    )
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)
    # 板块原样(补账本不重铸 id),产物指向的就是它们。
    assert set(_boards(repo, notebook_id)) == board_ids
    assert {
        board_id
        for src, dst, _weight in _edges(repo, notebook_id)
        for board_id in (src, dst)
    } <= board_ids
    assert kept == 2


def test_a_failed_precompute_heals_on_the_next_plain_rebuild(repo, monkeypatch):
    """预计算失败后**不需要** force 才能自愈:下一次普通调用就会补上。"""
    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(
        store, "source_canonical_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert repo.rebuild_communities(notebook_id) == 2
    assert _artifacts(repo, notebook_id) == {}

    monkeypatch.undo()
    assert repo.rebuild_communities(notebook_id) == 2       # 刻意不带 force
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_a_library_without_boards_writes_no_source_profile_product(repo):
    """S4:一个板块都没有时,来源画像必须**缺失**,而不是写一张全 0 的表。

    对象有、关系没有的库算不出任何「主体板块」,每个来源的 mainstream_share 都会是 0.0。
    明细表单独看是在说「所有来源都关联稀疏」—— 一句谎话。产物缺失是诚实的。
    """
    notebook = repo.create_notebook(NotebookCreate(name="no-boards"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a")
    repo.store_kg(notebook.id, "src-a", [_claim(n) for n in ("A", "B", "C")], [])

    assert repo.rebuild_communities(notebook.id) == 0
    artifacts = _artifacts(repo, notebook.id)
    assert set(artifacts) == set(ARTIFACT_KINDS) - {ARTIFACT_SOURCE_PROFILES}
    assert _profiles(repo, notebook.id) == {}
    assert artifacts[ARTIFACT_COMMUNITY_EDGES]["payload"]["communities"] == 0
    # 三条统计快照与板块无关,照常产出(形状完整,不是缺一块)。
    assert "by_object_type" in artifacts[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]
    assert "buckets" in artifacts[ARTIFACT_RELATION_PROVENANCE]["payload"]


# ------------------------------------------------- 两个闸的对称性(五个方向)
# 这道闸是**对称的**:放松一边就会紧另一边。它当初是为了修 B1(「已部署库永不回填」)
# 才加的;后来又发现它把「零板块」误判成「没建成」,让那类库每次刷新都重跑全部重活;
# 再后来又发现那次修复顺手吞掉了「请求的 level 从没建过」(方向四);第 15 轮又发现
# 方向四补的是**读侧**,写侧照旧对所有层推进那份不分 level 的账目,反向的洞还在
# (方向五)。五个方向必须**同时**钉住,只测一头的守卫会在下一次调参时静默失效。

_HEAVY_WORK = (
    "community_graph_rows",        # canonical 边图:生产 836 万边的一次 join
    "cluster_size_histogram",      # 三条全表统计快照
    "largest_clusters",
    "relation_provenance_counts",
)


def _spy_heavy_work(repo, monkeypatch) -> list:
    """记录整趟 rebuild 里真正跑过的重活(闸有没有短路,看这个列表空不空)。"""
    store = repo._runtime.unified_kg
    calls: list[str] = []
    for name in _HEAVY_WORK:
        monkeypatch.setattr(
            store, name,
            (lambda n, original: lambda *a, **k: (
                calls.append(n), original(*a, **k)
            )[1])(name, getattr(store, name)),
        )
    return calls


def _no_board_notebook(repo) -> str:
    """对象有、关系没有 —— 所有连通块都小于 `community_min_size`,合法的零板块库。"""
    notebook = repo.create_notebook(NotebookCreate(name="no-boards"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a")
    repo.store_kg(notebook.id, "src-a", [_claim(n) for n in ("A", "B", "C")], [])
    return notebook.id


def test_a_zero_board_library_short_circuits_instead_of_recomputing_forever(
    repo, monkeypatch
):
    """方向一(零板块短路):板块数 0 是**合法终态**,不是「没建成」。

    拿板块数当「建过」的标记,这类库的两个闸就永远过不去:每一次「刷新图谱」都要重跑
    canonical 边图 + 三条全表统计快照,而结果永远是同一个零。生产量级上那是一次
    「分钟到小时」的空转(437 GB / 836 万边)。

    `has_boards` 同样必须传实际值:零板块的库合法地不写来源画像(S4),硬写 True 等于
    宣布这类库的账本永远不完整 —— 同一个空转,换一个入口。
    """
    notebook_id = _no_board_notebook(repo)
    assert repo.rebuild_communities(notebook_id) == 0
    before = _artifacts(repo, notebook_id)
    assert set(before) == set(ARTIFACT_KINDS) - {ARTIFACT_SOURCE_PROFILES}

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 0        # 刻意不带 force
    assert calls == [], f"零板块库又跑了一遍重活:{calls}"
    # 账本一行都没被重写(created_at 会变)。
    assert _artifacts(repo, notebook_id) == before


def test_a_stale_ledger_with_boards_still_backfills(repo, monkeypatch):
    """方向二(B1 回填):有板块 + 账本陈旧 → 必须真的补,不得被闸吞掉。

    这是这道闸最初要修的那件事(生产 base 库上线前就已 `community_seq ==
    kg_mutation_seq`)。方向一的修法**不得**让它复发,所以两条并排放。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    _wipe_ledger(repo, notebook_id)

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2        # 刻意不带 force
    assert sorted(set(calls)) == sorted(_HEAVY_WORK), f"账本陈旧却没补:{calls}"
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_boards_with_a_missing_source_profile_row_still_backfill(repo, monkeypatch):
    """`has_boards` 必须来自**实际板块数**,不得从账本自己推导。

    从账本推(「来源画像行在不在」)是**循环判据**:这一行一丢,反而会被读成「这个库
    一个板块都没有,所以它合法缺席」,于是永远不补 —— 一个看起来完全合理、却让缺席
    自我合理化的实现。这条把判据的来源钉死在库里的板块数上。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    with repo._write() as db:
        db.execute(
            "DELETE FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
            (notebook_id, ARTIFACT_SOURCE_PROFILES),
        )
        db.execute(
            "DELETE FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,)
        )

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2        # 刻意不带 force
    assert calls, "有板块却把来源画像的缺席判成合法 —— 它永远补不回来了"
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)
    assert _profiles(repo, notebook_id)


def test_a_never_built_library_really_builds(repo, monkeypatch):
    """方向三(从未建过):`community_seq == -1` 必须真正构建,不许被当成「零板块」短路。

    这是把「建过」的标记从板块数换成 seq 之后唯一的风险点。列默认 -1、
    `kg_mutation_seq` 恒 >= 0,两者永远不等 —— 这条把那个前提钉住。
    """
    notebook_id = _seed(repo)
    with repo._connect() as db:
        state = db.execute(
            "SELECT community_seq, kg_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    assert int(state["community_seq"]) == -1
    assert int(state["kg_mutation_seq"]) >= 0

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2
    assert sorted(set(calls)) == sorted(_HEAVY_WORK), f"从没建过却短路了:{calls}"
    assert _boards(repo, notebook_id)
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def _boards_at(repo, notebook_id: str, level: int) -> dict:
    """按 level 查板块行 —— `_boards` 写死 level=0,方向四要比对的正是另一层。"""
    with repo._connect() as db:
        return {
            row["id"]: int(row["size"])
            for row in db.execute(
                "SELECT id, size FROM communities WHERE notebook_id=? AND level=? "
                "AND generation = COALESCE((SELECT community_generation "
                "FROM unified_kg_state WHERE notebook_id=?),0)",
                (notebook_id, level, notebook_id),
            )
        }


def test_a_never_built_level_really_builds_even_when_the_default_level_is_aligned(
    repo, monkeypatch
):
    """方向四(请求的 level 没建过):不分 level 的账目替它说不了话。

    `community_seq` 与 `kg_analysis_artifacts` 都没有 level 维度(设计 §3.4 订正 2
    刻意去掉的)。方向一把「建过」的标记从**按 level 查的**板块数换成 seq 对齐 —— 于是
    默认层建完之后,一次非 force 的 `rebuild_communities(..., level=1)`(库里一行
    level-1 都没有)会满足闸、直接返回 0 而**根本不建那一层**;被它取代的旧判据反倒
    建得出来。这是方向一那次修复的回归,codex 第 11 轮 P2-1。

    补法只落在闸上:默认层照旧信 seq(零板块是合法终态,方向一),其它层退回唯一存在
    的按层证据 `communities_count(level) > 0`。产物表**不**加回 level 维度。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2            # 默认层建好
    with repo._connect() as db:
        state = db.execute(
            "SELECT community_seq, kg_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    # 前提:闸的另外两项证据都已经「齐全且对齐」—— 短路的条件全部成立,只差 level。
    assert int(state["community_seq"]) == int(state["kg_mutation_seq"])
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)
    assert _boards_at(repo, notebook_id, 1) == {}, "前提不成立:level 1 已经有行了"

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id, level=1) == 2   # 刻意不带 force
    # 短路的表现是「一件重活都没跑、返回 0」。这里不要求跑满 `_HEAVY_WORK`:KG 世代
    # 一动没动,`relation_provenance` 的载荷按设计被复用而不重扫关系表(那条成本主张
    # 由 `test_a_merge_only_backfill_does_not_rescan_the_relation_table` 单独钉)。
    assert "community_graph_rows" in calls, (
        f"请求的 level 一行都没有,却被不分 level 的账目判成「建过」:{calls}"
    )
    assert _boards_at(repo, notebook_id, 1), "level 1 一行都没写出来"
    # 默认层不受连坐:`replace_communities` 按 level 删,它的行必须还在。
    assert _boards_at(repo, notebook_id, 0)


def test_the_gate_reads_the_same_default_level_the_callers_do():
    """方向四的判据引用 `DEFAULT_COMMUNITY_LEVEL`,它必须**就是**签名的默认值。

    两者一漂,「默认层」在闸里和在调用方那里就是两个不同的数:默认调用会掉进「非默认
    层」那一支,零板块库重新变成每次刷新都重跑全部重活 —— 方向一原地复发,而方向一
    自己的守卫(它不传 level)照样全绿。
    """
    import inspect

    from app.services.knowledge_lifecycle import (
        DEFAULT_COMMUNITY_LEVEL,
        KnowledgeLifecycleService,
    )
    from app.services.repository_facade import RepositoryFacade

    for target in (KnowledgeLifecycleService.rebuild_communities,
                   RepositoryFacade.rebuild_communities):
        default = inspect.signature(target).parameters["level"].default
        assert default == DEFAULT_COMMUNITY_LEVEL, target.__qualname__


def _community_seq(repo, notebook_id: str) -> int:
    with repo._connect() as db:
        return int(db.execute(
            "SELECT community_seq FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,)).fetchone()["community_seq"])


def _grow_kg(repo, notebook_id: str) -> None:
    """再加一个自成一体的三角 —— 板块数 2 → 3,而且 `kg_mutation_seq` 跟着往前走。"""
    _add_sources(repo, notebook_id, "src-e")
    repo.store_kg(
        notebook_id, "src-e",
        [_claim(name) for name in ("P", "Q", "R")],
        [_rel("P", "Q"), _rel("Q", "R"), _rel("P", "R")],
    )


def test_building_another_level_does_not_mark_the_default_level_built(repo, monkeypatch):
    """方向五(共享账目只替默认层说话):非默认层的构建不得推进 `community_seq`。

    方向四把「非默认层没建过」补上了,但补的是**读侧**;写侧照旧对所有层推进那份
    **不分 level** 的 `community_seq`(codex 第 15 轮 P2-1)。于是先建 level 1 → seq
    被推到当前 → 随后一次非 force 的 `rebuild_communities(level=0)` 在 level 0
    **一行都没有**时仍然满足「seq 对齐 ⟹ 默认层建过」,拿 level-1 的账目短路返回 0。
    方向四的判据管不到这一档:默认层那一支是**无条件**信 seq 的(零板块是合法终态,
    见方向一),它信的正是这个被别的层顶掉的数。

    补法不加列:那份共享账目(`community_seq` + `kg_analysis_artifacts` + 两张依赖板块
    的产物表)整体收归 `DEFAULT_COMMUNITY_LEVEL` 独有 —— 只有默认层的构建写它、只有
    默认层的构建读它。「seq 对齐 ⟹ 默认层建过」的蕴含关系因此恢复成写侧的不变式,
    而不是一句但愿如此的假设。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id, level=1) == 2
    assert _boards_at(repo, notebook_id, 1), "前提不成立:level 1 没建出来"
    assert _boards_at(repo, notebook_id, 0) == {}, "前提不成立:level 0 不该有行"
    assert _community_seq(repo, notebook_id) == -1, (
        "非默认层的构建推进了那份不分 level 的共享 seq —— 默认层的「建过」判据"
        "(seq 对齐)因此被别的层顶掉:下面那次 level 0 重建会直接短路,而 level 0 "
        "一行板块都没有"
    )

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2         # 默认层,刻意不带 force
    assert sorted(set(calls)) == sorted(_HEAVY_WORK), (
        f"默认层一行板块都没有,却被别的层写下的账目判成「建过」:{calls}"
    )
    assert _boards_at(repo, notebook_id, 0), "level 0 一行都没写出来"
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_building_another_level_leaves_the_default_levels_ledger_alone(repo):
    """同一族的另一半:非默认层的构建也不得改写/作废那份不分 level 的账本与产物表。

    `kg_analysis_artifacts` / `kg_community_edges` / `kg_source_profiles` 与
    `community_seq` 是**同一份**共享账目,只是分了四张表。让非默认层去写它,后果比
    seq 那一半更重:
      · `discard_board_dependent_kg_analysis_artifacts` 会连坐作废默认层**有效**的
        两份产物(level-1 的 `replace_communities` 只删 level-1 的板块行,默认层的
        板块 id 一个都没变,那两份根本没悬空);
      · 随后写回的两份产物指向的是 **level-1** 的板块 id,而默认层的读侧照旧按
        `community_seq` 对齐判「与当前一致」—— 报告会把 level-1 的跨板块连线画在
        level-0 的板块上。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    before_ledger = _artifacts(repo, notebook_id)
    before_edges = _edges(repo, notebook_id)
    before_profiles = _profiles(repo, notebook_id)
    before_boards = _boards_at(repo, notebook_id, 0)
    assert set(before_ledger) == set(ARTIFACT_KINDS)
    assert before_edges and before_profiles and before_boards

    assert repo.rebuild_communities(notebook_id, level=1) == 2

    assert _edges(repo, notebook_id) == before_edges, (
        "跨板块边被换成了另一层的板块 id"
    )
    assert _artifacts(repo, notebook_id) == before_ledger, (
        "非默认层的构建改写了那份不分 level 的账本 —— 默认层的读侧照旧信它"
    )
    assert _profiles(repo, notebook_id) == before_profiles
    # 批 3·W2:非默认层的发布把默认层的行 copy-forward 进新代,板块 **id 重铸**
    # (communities.id 单列 PK,同 id 双代必撞)——所以这里比对的是「行还在、
    # 划分没变」(个数 + 大小多重集),不再是 id 逐字相同。id 重铸带来的
    # 已知残留(两份板块依赖产物此时指向旧 id)登记在发布段注释里:今天
    # 没有任何调用点传非默认层,复制集恒空,残留不落地。
    after_boards = _boards_at(repo, notebook_id, 0)
    assert sorted(after_boards.values()) == sorted(before_boards.values()), (
        "默认层的板块划分被连坐改掉了 —— copy-forward 必须原样带行"
    )
    assert len(after_boards) == len(before_boards)


def test_another_level_never_short_circuits_on_the_default_levels_seq(repo, monkeypatch):
    """共享账目收归默认层之后,非默认层就**没有**账本了 —— 所以它永不短路。

    保留「其它层退回 `communities_count(level) > 0`」那一支会把陈旧的划分当成新鲜的:
    共享的 seq 只证明**默认层**建于当前 KG 世代,而这一层的行可能建在好几代以前。
    这条把那个组合钉死 —— level 1 建于旧世代、KG 变过、默认层重建过(seq 因此对齐),
    此时再要 level 1 必须真的重建,不能拿默认层的 seq 加上「这层有行」冒充新鲜。

    代价是显式的、也是刻意选的:非默认层每次都全量重建(只会**多**建、绝不会漏建)。
    今天没有任何调用点传非默认层,所以这笔代价的真实值是 0;而它换回来的是一句能写死
    的不变式 —— 那份不分 level 的账目只描述默认层,别的层一个字都不占。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id, level=1) == 2   # 建于旧世代
    _grow_kg(repo, notebook_id)                                  # KG 变了
    assert repo.rebuild_communities(notebook_id) == 3            # 默认层跟上 → seq 对齐
    with repo._connect() as db:
        state = db.execute(
            "SELECT community_seq, kg_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    assert int(state["community_seq"]) == int(state["kg_mutation_seq"])
    assert len(_boards_at(repo, notebook_id, 1)) == 2, "前提不成立:level 1 还是旧划分"

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id, level=1) == 3   # 刻意不带 force
    assert "community_graph_rows" in calls, (
        f"非默认层拿默认层的 seq + 「这层有行」冒充了新鲜:{calls}"
    )
    assert len(_boards_at(repo, notebook_id, 1)) == 3, "level 1 还停在旧划分上"


def test_deleting_the_notebook_takes_the_artifacts_with_it(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    assert _artifacts(repo, notebook_id)
    repo.delete_notebook(notebook_id)
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}
