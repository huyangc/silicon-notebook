"""紧凑 viz 边集 old-vs-new 等价:响应 JSON 逐位不变。

批 2 把 viz 边从 ``list[[src, dst, edge_type]]`` 换成列式数组 + 持久化派生索引,
并让 ``_unified_graph_bounded`` / 邻居定向按 kept pair 间接二分，不扫描高出度
source 的完整邻接段。**纯性能改造**,因此本文件把改造前的
实现原样抄成 oracle,断言两者的响应 dict 逐位相等——含 degree 并列的 tie 顺序、
边序、totals/truncated、以及同一 (s,t) 多重边「最后一条胜出」的字典覆盖语义。
"""
import numpy as np
import pytest
import scipy.sparse as sp

from app.services.kg.scale_index import VizEdgeSet
from app.services.kg.viz_index import VizIndex, arrays_from_graph
from app.services.knowledge_lifecycle import KnowledgeLifecycleService


# ───────────────────────────────────────────────────── legacy oracles ──
def legacy_unified_graph_bounded(idx, edge_rows, limit):
    """改造前 `_unified_graph_bounded` 的正文(去掉 _annotate_edge_support,
    那一步本轮未动且对两侧完全相同)。"""
    ids, deg = idx.viz_ids, idx.viz_deg
    names = idx.viz_names or []
    types = idx.viz_types or []
    name_by_id = {n: nm for n, nm in zip(ids, names)}
    type_by_id = {n: t for n, t in zip(ids, types)}
    total_nodes = len(ids)
    edges_all = edge_rows or []

    if limit is None or limit >= total_nodes:
        keep_ids = list(ids)
    else:
        order = sorted(range(total_nodes), key=lambda i: -int(deg[i]))
        keep_ids = [ids[i] for i in order[:limit]]
    keep = set(keep_ids)

    nodes = [{"id": fid, "object_type": type_by_id.get(fid, ""),
              "payload": {"name": name_by_id.get(fid, "")}}
             for fid in ids if fid in keep]
    kept_edges = [{"source_object_id": s, "target_object_id": t, "edge_type": et}
                  for s, t, et in edges_all if s in keep and t in keep]
    return {
        "nodes": nodes,
        "edges": kept_edges,
        "total_nodes": total_nodes,
        "total_edges": len(edges_all),
        "truncated": len(nodes) < total_nodes,
    }


def legacy_neighbor_edges(edge_rows, neighbor_pairs):
    """改造前 `_kg_neighbors_unchecked` 的边定向段(et_map 逐边覆盖 → 正/反向
    命中 → "related" 兜底)。"""
    et_map = {}
    for s, t, et in (edge_rows or []):
        et_map[(s, t)] = et
    edges = []
    for s, t in neighbor_pairs:
        if (s, t) in et_map:
            edge_s, edge_t, et = s, t, et_map[(s, t)]
        elif (t, s) in et_map:
            edge_s, edge_t, et = t, s, et_map[(t, s)]
        else:
            edge_s, edge_t, et = s, t, "related"
        edges.append({"source_object_id": edge_s, "target_object_id": edge_t,
                      "edge_type": et})
    return edges


# ──────────────────────────────────────────────────────────── harness ──
class _Harness:
    """只借用被测的三个纯方法;`_annotate_edge_support` 本轮未动,恒等替身即可
    (oracle 也不跑它),它之外这条路径不碰数据库。"""

    _viz_label_maps = KnowledgeLifecycleService._viz_label_maps
    _viz_node = KnowledgeLifecycleService._viz_node
    _unified_graph_bounded = KnowledgeLifecycleService._unified_graph_bounded

    @staticmethod
    def _annotate_edge_support(notebook_id, edges):
        return edges


def _graph(n_nodes=200, seed=7):
    """含 degree 并列(大量 1 度节点)、多重边、双向边、自环与孤立节点的折叠图。"""
    rng = np.random.default_rng(seed)
    nodes = [
        {"id": f"k{i}", "object_type": ["concept", "claim"][i % 2],
         "payload": {"name": f"名字{i}"}}
        for i in range(n_nodes)
    ]
    types = ["relates", "supports", "part_of", "contradicts"]
    edges = []
    # 星形枢纽:少数高度数节点,其余大面积 degree 并列。
    for hub in range(3):
        for j in range(20):
            edges.append({"source_object_id": f"k{hub}",
                          "target_object_id": f"k{10 + hub * 20 + j}",
                          "edge_type": types[j % len(types)]})
    for _ in range(120):
        a, b = int(rng.integers(0, n_nodes)), int(rng.integers(0, n_nodes))
        edges.append({"source_object_id": f"k{a}", "target_object_id": f"k{b}",
                      "edge_type": types[int(rng.integers(0, len(types)))]})
    # 同一 (s,t) 的多重边:后写必须胜出(两条不同 edge_type)。
    edges.append({"source_object_id": "k5", "target_object_id": "k6",
                  "edge_type": "relates"})
    edges.append({"source_object_id": "k5", "target_object_id": "k6",
                  "edge_type": "contradicts"})
    # 反向边(邻居定向的 fallback 分支)与自环。
    edges.append({"source_object_id": "k7", "target_object_id": "k5",
                  "edge_type": "part_of"})
    edges.append({"source_object_id": "k8", "target_object_id": "k8",
                  "edge_type": "supports"})
    return {"nodes": nodes, "edges": edges}


def _index(full):
    viz_ids, viz_adj, viz_deg, viz_types, viz_names, payload = arrays_from_graph(full)
    idx = VizIndex(viz_ids=viz_ids, viz_adj=viz_adj, viz_deg=viz_deg,
                   viz_types=viz_types, viz_names=viz_names,
                   viz_edges=payload["edges"], manifest={})
    node_ids = set(viz_ids)
    rows = [[e["source_object_id"], e["target_object_id"], e["edge_type"]]
            for e in full["edges"]
            if e["source_object_id"] in node_ids and e["target_object_id"] in node_ids]
    return idx, rows


# ─────────────────────────────────────────────────────────── the tests ──
@pytest.mark.parametrize("limit", [None, 0, 1, 5, 37, 199, 200, 500])
def test_bounded_graph_matches_pre_change_implementation(limit):
    full = _graph()
    idx, rows = _index(full)
    assert len(rows) == len(idx.viz_edges)          # 边集无损

    new = _Harness()._unified_graph_bounded("nb", idx, limit)
    old = legacy_unified_graph_bounded(idx, rows, limit)
    assert new == old


def test_bounded_core_never_builds_the_index_wide_node_index():
    """有界核心已经握着 kept 下标,不得再经 viz_node_index() 绕一圈——那个全量
    id→下标 dict 一旦建起来就随索引常驻 LRU,正是本批要消灭的东西。

    判据是缓存位:跑完一次有界响应,`_viz_node_index_cache` 必须仍未建立。
    (把 `_viz_label_maps` 改回按 id 取数会立刻在此报红。)"""
    full = _graph(n_nodes=120, seed=13)
    idx, _ = _index(full)
    assert idx.__dict__.get("_viz_node_index_cache") is None

    _Harness()._unified_graph_bounded("nb", idx, 20)
    assert idx.__dict__.get("_viz_node_index_cache") is None, \
        "bounded core must not build the index-wide id→position map"

    # 反向对照:邻居路径本来就需要它(边定向),那里建起来是正当的。
    assert idx.viz_node_index()
    assert idx.__dict__.get("_viz_node_index_cache") is not None


@pytest.mark.parametrize("limit", [1, 9, 40])
def test_label_maps_stay_no_wider_than_the_requested_nodes(limit):
    """标签表的宽度必须是「本次要渲染的节点数」而不是「全库节点数」。

    没有这条,把它静默改回全量宽度(旧的 zip(viz_ids, viz_names))在功能上完全
    看不出来——响应逐位相同,只是每请求又付一次 O(N)。"""
    full = _graph(n_nodes=150, seed=17)
    idx, _ = _index(full)
    harness = _Harness()

    positions = list(range(min(limit, len(idx.viz_ids))))
    name_by_id, type_by_id = harness._viz_label_maps(idx, positions)
    assert len(name_by_id) <= len(positions)
    assert len(type_by_id) <= len(positions)
    assert len(name_by_id) < len(idx.viz_ids)

    # 端到端:有界响应内部建的表也不得宽于它返回的节点数。
    captured = {}
    real = _Harness._viz_label_maps

    def _spy(self, index, node_positions):
        node_positions = list(node_positions)
        captured["requested"] = len(node_positions)
        maps = real(self, index, node_positions)
        captured["width"] = len(maps[0])
        return maps

    harness_cls = type("Spy", (_Harness,), {"_viz_label_maps": _spy})
    out = harness_cls()._unified_graph_bounded("nb", idx, limit)
    assert captured["requested"] == len(out["nodes"])
    assert captured["width"] <= len(out["nodes"])
    assert captured["width"] < len(idx.viz_ids)


def test_bounded_graph_matches_on_edgeless_and_empty_graphs():
    harness = _Harness()
    lonely = {"nodes": [{"id": "k0", "object_type": "concept",
                         "payload": {"name": "只有一个点"}}], "edges": []}
    idx, rows = _index(lonely)
    for limit in (None, 0, 1, 9):
        assert harness._unified_graph_bounded("nb", idx, limit) == \
            legacy_unified_graph_bounded(idx, rows, limit)

    empty, rows = _index({"nodes": [], "edges": []})
    assert harness._unified_graph_bounded("nb", empty, 5) == \
        legacy_unified_graph_bounded(empty, rows, 5)


def test_degree_order_is_stable_like_python_sorted():
    """并列 degree 必须保持 viz_ids 原序——argsort 换成非稳定会在此报红。"""
    full = _graph()
    idx, _ = _index(full)
    expected = sorted(range(len(idx.viz_ids)), key=lambda i: -int(idx.viz_deg[i]))
    assert [int(i) for i in idx.viz_deg_order()] == expected


def test_neighbor_edge_orientation_matches_pre_change_implementation():
    full = _graph()
    idx, rows = _index(full)
    positions = idx.viz_node_index()
    edge_set = idx.viz_edges
    absent = object()

    # 覆盖三条分支:正向命中(含多重边)、反向命中、两边都无 → "related"。
    pairs = [("k5", "k6"), ("k6", "k5"), ("k5", "k7"), ("k8", "k8"),
             ("k0", "k10"), ("k10", "k0"), ("k198", "k199")]
    pairs += [(f"k{i}", f"k{i + 1}") for i in range(0, 60)]

    produced = []
    for s, t in pairs:
        et = edge_set.edge_type_at(positions.get(s), positions.get(t), default=absent)
        if et is not absent:
            edge_s, edge_t = s, t
        else:
            reverse = edge_set.edge_type_at(positions.get(t), positions.get(s),
                                            default=absent)
            if reverse is not absent:
                edge_s, edge_t, et = t, s, reverse
            else:
                edge_s, edge_t, et = s, t, "related"
        produced.append({"source_object_id": edge_s, "target_object_id": edge_t,
                         "edge_type": et})

    assert produced == legacy_neighbor_edges(rows, pairs)
    # 多重边:后写胜出(oracle 的 dict 覆盖语义)。
    assert edge_set.edge_type_at(positions["k5"], positions["k6"]) == "contradicts"


def test_batched_edge_type_lookup_matches_individual_legacy_semantics():
    """批量 API 与逐 pair 查询逐位一致,含重复 pair、反向、缺边和最后边胜出。"""
    full = _graph()
    idx, _ = _index(full)
    positions = idx.viz_node_index()
    pairs = [
        (positions["k5"], positions["k6"]),
        (positions["k5"], positions["k6"]),
        (positions["k6"], positions["k5"]),
        (positions["k8"], positions["k8"]),
        (positions["k198"], positions["k199"]),
        (None, positions["k0"]),
    ]
    absent = object()
    expected = [idx.viz_edges.edge_type_at(s, t, absent) for s, t in pairs]
    assert idx.viz_edges.edge_types_at_many(pairs, absent) == expected


def test_induced_edge_lookup_does_not_scan_a_kept_hubs_outgoing_segment():
    """复杂度形状:degree-top core 保留超级 hub 时也只能做 pair-range 二分。"""
    fan_out = 20_000
    ids = [f"n{i}" for i in range(fan_out + 1)]
    rows = [["n0", f"n{target}", "relates"]
            for target in range(1, fan_out + 1)]
    rows.append(["n1", "n2", "supports"])
    edge_set = VizEdgeSet.from_rows(rows, ids)
    edge_set.by_src()  # freeze the real auxiliary index before wrapping dst
    original_dst = edge_set.dst

    class _CountingDst:
        def __init__(self, values):
            self.values = values
            self.reads = 0

        def __getitem__(self, key):
            if isinstance(key, (int, np.integer)):
                self.reads += 1
            return self.values[key]

    counting = _CountingDst(original_dst)
    edge_set.dst = counting
    selected = edge_set.induced_edge_indices([0, 1, 2])
    assert counting.reads < 256
    assert counting.reads * 50 < len(edge_set)
    assert selected.tolist() == [0, 1, fan_out]

    counting.reads = 0
    assert edge_set.edge_type_at(0, fan_out) == "relates"
    assert counting.reads < 64

    counting.reads = 0
    assert edge_set.edge_types_at_many(
        [(0, 1), (0, fan_out), (0, fan_out)], default=None
    ) == ["relates", "relates", "relates"]
    assert counting.reads < 128


def test_edge_type_at_distinguishes_absent_from_falsy_edge_type():
    """「没有这条边」与「有边但 edge_type 为空」必须可分——历史靠
    `(s, t) in et_map` 免费拿到,紧凑实现靠 default 哨兵。"""
    edge_set = VizEdgeSet.from_rows([["a", "b", ""]], ["a", "b"])
    absent = object()
    assert edge_set.edge_type_at(0, 1, default=absent) == ""
    assert edge_set.edge_type_at(1, 0, default=absent) is absent


def test_from_rows_drops_rows_whose_endpoints_are_unknown():
    """端点不在 viz_ids 内的历史行被丢弃——消费侧的 `s in keep` 对它们本就恒假。
    (v9 之前那种 dict 形状的边行同理:不是三元组序列即丢弃,绝不猜测键名。)"""
    edge_set = VizEdgeSet.from_rows(
        [["a", "b", "relates"], ["a", "zz", "relates"],
         {"from_id": "a", "to_id": "b", "relation": "supports"}, ["a"]],
        ["a", "b"],
    )
    assert len(edge_set) == 1
    assert list(edge_set.rows(["a", "b"])) == [["a", "b", "relates"]]


def test_edge_set_survives_a_wider_than_builtin_edge_type_table():
    """edge_type 表是逐文件去重的字符串表,不硬编码内置 12 种边——管理员扩展
    类型必须原样往返。"""
    ids = [f"n{i}" for i in range(40)]
    rows = [[f"n{i}", f"n{i + 1}", f"admin_edge_{i}"] for i in range(39)]
    edge_set = VizEdgeSet.from_rows(rows, ids)
    assert len(edge_set.type_table) == 39
    assert list(edge_set.rows(ids)) == rows


def test_viz_neighbors_uses_the_cached_node_index(monkeypatch):
    """`_viz_dict` 必须把缓存的 id→下标映射带给 viz_neighbors;缺了它每次点邻居
    都重建一次全量映射。纯函数在缺键时仍要自建(向后兼容)。"""
    from app.services.kg import scale_index as si

    full = _graph(n_nodes=20, seed=3)
    idx, _ = _index(full)
    packed = KnowledgeLifecycleService._viz_dict(object(), idx)
    assert packed["viz_node_index"] is idx.viz_node_index()

    focus = idx.viz_ids[0]
    with_index = si.viz_neighbors(packed, focus, cap=50)
    without = si.viz_neighbors(
        {k: v for k, v in packed.items() if k != "viz_node_index"}, focus, cap=50)
    assert with_index == without


def test_compact_edge_set_matches_legacy_rows_from_arrays_from_graph():
    """构建期不再中转字符串三元组;紧凑边集还原出的行必须与图里的边逐位一致
    (顺序、edge_type 全等)。"""
    full = _graph(n_nodes=60, seed=11)
    idx, rows = _index(full)
    assert list(idx.viz_edges.rows(idx.viz_ids)) == rows
    assert isinstance(idx.viz_edges.src, np.ndarray)
    assert idx.viz_edges.src.dtype == np.int32
    assert idx.viz_edges.code.dtype == np.uint16


def test_by_src_index_is_consistent_with_the_raw_columns():
    full = _graph(n_nodes=80, seed=5)
    idx, _ = _index(full)
    edge_set = idx.viz_edges
    order, indptr = edge_set.by_src()
    assert len(indptr) == len(idx.viz_ids) + 1
    assert int(indptr[-1]) == len(edge_set)
    for node in range(len(idx.viz_ids)):
        lo, hi = int(indptr[node]), int(indptr[node + 1])
        segment = [int(i) for i in order[lo:hi]]
        assert all(int(edge_set.src[i]) == node for i in segment)
        # 段内按 (dst,原边下标) 严格排序，pair-range 才能二分；同一 pair
        # 的原下标升序保证最后一条仍是历史 dict 的 winner。
        assert segment == sorted(
            segment, key=lambda i: (int(edge_set.dst[i]), i)
        )
        assert set(segment) == {
            i for i in range(len(edge_set)) if int(edge_set.src[i]) == node
        }


def test_src_only_legacy_auxiliary_order_is_rejected_then_lazily_rebuilt():
    """旧辅助数组段内只保原序；dst 非单调时不可拿去二分，但事实边仍兼容。"""
    edge_set = VizEdgeSet.from_rows(
        [["a", "c", "first"], ["a", "b", "second"]],
        ["a", "b", "c"],
    )
    assert edge_set.install_by_src(
        np.asarray([0, 1], dtype=np.int64),
        np.asarray([0, 2, 2, 2], dtype=np.int64),
    ) is False
    assert "_by_src_cache" not in edge_set.__dict__
    order, _ = edge_set.by_src()
    assert order.tolist() == [1, 0]
    assert edge_set.edge_type_at(0, 2) == "first"


def test_persisted_auxiliary_order_is_src_dst_original_index():
    """写盘 helper 必须固化可二分三键序，而不是旧版的 src-only 稳定序。"""
    from app.services.kg.scale_index import viz_edge_npz_arrays

    edge_set = VizEdgeSet.from_rows(
        [["a", "c", "first"], ["a", "b", "middle"],
         ["a", "c", "last"], ["b", "a", "reverse"]],
        ["a", "b", "c"],
    )
    arrays = viz_edge_npz_arrays(edge_set)
    assert arrays["viz_edge_src_order"].tolist() == [1, 0, 2, 3]
    assert arrays["viz_edge_src_indptr"].tolist() == [0, 3, 4, 4]
    assert edge_set.edge_type_at(0, 2) == "last"


def test_scale_index_and_viz_index_share_the_lazy_derivations():
    """两个产物类必须同义(unified_graph 的有界分派鸭子类型地消费任一来源)。"""
    from app.services.kg.scale_index import ScaleIndex

    scale = ScaleIndex(
        node_ids=[], node_index={}, transition=sp.csr_matrix((0, 0)),
        idf=np.zeros(0), chunk_index=np.zeros(0, dtype=np.int32),
        ann_labels=[], ann_path="", manifest={},
        viz_ids=["a", "b", "c"], viz_deg=np.asarray([1, 3, 1], dtype=np.int32),
    )
    assert scale.viz_node_index() == {"a": 0, "b": 1, "c": 2}
    assert [int(i) for i in scale.viz_deg_order()] == [1, 0, 2]
    assert scale.viz_node_index() is scale.viz_node_index()   # 缓存,不重建
