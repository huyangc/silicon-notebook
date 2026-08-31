"""规模化 KG 检索的紧凑基底：scipy CSR 图 + 个性化 PPR + active 拼接 + 构建/加载。

设计见 docs/superpowers/specs/2026-06-29-base-kg-scale-retrieval-design.md。
本模块尽量纯函数、可单测；DB/IO 由 sqlite_repository 包装层提供数据。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np
import scipy.sparse as sp

_logger = logging.getLogger(__name__)


def encode_viz_edges(edges) -> "np.ndarray":
    """把 viz 边列表序列化成 uint8 一维字节数组,供 np.savez 落进 viz.npz。

    历史写法是把 ``json.dumps(edges)`` 当作**单个 unicode 标量**塞给 np.savez。
    numpy 字符串数组的单元素 itemsize 用 C ``int`` 存(unicode 每字符 4 字节),
    上限约 512Mi 字符(``(2**31-1)//4``);超大 base 图的边 JSON 会溢出并触发
    ``TypeError: string too large to store inside array``。改存 uint8 一维数组:
    元素数走 ``npy_intp``(64 位),且 numpy 以 ``force_zip64`` 写 npz 条目,
    彻底绕开这个 2^31 上限。与 decode_viz_edges 成对使用。

    **生产写入端已不再使用本函数**:现在落的是 ``viz_edge_*`` 紧凑数组(见
    VizEdgeSet)。保留它只为构造旧格式产物的测试,以及与 decode_viz_edges 配对
    说明那条兼容读路径。"""
    payload = json.dumps(edges or []).encode("utf-8")
    return np.frombuffer(payload, dtype=np.uint8)


def decode_viz_edges(raw) -> list:
    """从 viz.npz 读回边列表,兼容两种落盘格式。

    新格式:encode_viz_edges 写的 uint8 一维字节数组 → 解码 UTF-8 再 json。
    旧格式:历史 ``json.dumps(...)`` 存成的 0-d unicode 标量 → 直接 ``str()``。
    老索引不重建也能继续加载(与 has_chunk_ann 的 older-index-stays-valid 同性质)。

    本函数只服务**旧格式**产物:现在的写入端落的是 ``viz_edge_*`` 紧凑数组
    (见 VizEdgeSet)。保留它是为了让盘上已有的 viz.npz 不重建也能加载。"""
    arr = np.asarray(raw)
    if arr.dtype == np.uint8:
        text = arr.tobytes().decode("utf-8")
    else:
        text = str(raw)
    return json.loads(text)


_VIZ_EDGE_SRC_KEY = "viz_edge_src"
_VIZ_EDGE_LEGACY_KEY = "viz_edges"
_VIZ_EDGE_ORDER_KEY = "viz_edge_src_order"
_VIZ_EDGE_INDPTR_KEY = "viz_edge_src_indptr"
_VIZ_DEG_ORDER_KEY = "viz_deg_order"
_VIZ_AUX_VALIDATION_CHUNK_ROWS = 1_000_000


def _viz_code_dtype(n_types: int):
    """edge_type 码的最窄整型。内置 12 种边 + 管理员扩展类型都远在 uint16 内,
    但绝不硬编码上限——表撑破 uint16 就自动升 uint32。"""
    return np.uint16 if n_types <= np.iinfo(np.uint16).max else np.uint32


@dataclass
class VizEdgeSet:
    """折叠 viz 图的有向边集,紧凑列式表示。

    历史表示是 ``list[[src_id, dst_id, edge_type], ...]``:每条边三个 Python 对象
    加一个 list,在千万边的 base 库上是几个 GB 的常驻堆,而且**每次**
    unified_graph/kg_neighbors 请求都要整表 Python 循环一遍。这里改成三条并列
    数组:端点存 ``viz_ids`` 下标(int32),edge_type 存**逐文件**去重字符串表的
    下标(内置 12 种边不硬编码,管理员扩展类型照样进表)。

    新产物把 ``by_src()`` 的稳定置换与分段边界一同落盘,请求加载后直接复用;
    旧产物仍惰性构建并缓存,保持 older-index-stays-valid。并发首次访问老产物
    最多重复算一次同样的结果(幂等,写的是同一个值),因此不加锁。
    """

    src: "np.ndarray"        # int32,viz_ids 下标
    dst: "np.ndarray"        # int32,viz_ids 下标
    code: "np.ndarray"       # uint16/uint32,type_table 下标
    type_table: list         # 逐文件唯一的 edge_type 值(保持原值,不强转 str)
    n_nodes: int             # viz_ids 长度(by_src 的 indptr 长度依赖它)

    # dataclass 字段之外的惰性缓存(无注解 → 不是字段,不进 __init__/__eq__)。
    _by_src_cache = None

    def __len__(self) -> int:
        return int(self.src.shape[0])

    def rows(self, viz_ids):
        """惰性还原成历史的 ``[src_id, dst_id, edge_type]`` 三元组序列(原边序)。

        只给需要字符串形态的少数出口用(测试/诊断);热路径一律直接吃数组。"""
        table = self.type_table
        src, dst, code = self.src, self.dst, self.code
        for i in range(len(self)):
            yield [viz_ids[int(src[i])], viz_ids[int(dst[i])],
                   table[int(code[i])]]

    def by_src(self):
        """(order, indptr):按 ``(src,dst,原边下标)`` 排序的边下标置换及
        source 分段边界。

        dst 次序让热路径能在高出度 source 段上间接二分，不读取整段；原边下标
        是末级键，所以同一 (s,t) 的最后一项仍精确复现历史字典覆盖语义。
        """
        cache = self.__dict__.get("_by_src_cache")
        if cache is None:
            original = np.arange(len(self), dtype=np.int64)
            order = np.lexsort((original, self.dst, self.src))
            indptr = np.zeros(int(self.n_nodes) + 1, dtype=np.int64)
            if len(self):
                counts = np.bincount(self.src, minlength=int(self.n_nodes))
                indptr[1:] = np.cumsum(counts[: int(self.n_nodes)])
            cache = (order, indptr)
            self._by_src_cache = cache
        return cache

    def install_by_src(self, order, indptr) -> bool:
        """校验并安装落盘的 by-source 辅助数组。

        辅助数组缺失/损坏不影响图的事实数据:返回 False,调用方随后仍可走
        ``by_src`` 的惰性兼容路径。验证既检查置换,也严格检查
        ``(src,dst,原边下标)`` 次序和 source 边界,避免旧版仅按 src 排序的辅助
        数组被误当成可二分的新索引。
        """
        try:
            order = np.asarray(order).ravel()
            indptr = np.asarray(indptr).ravel()
        except (TypeError, ValueError):
            return False
        n_edges = len(self)
        n_nodes = int(self.n_nodes)
        if (
            not np.issubdtype(order.dtype, np.integer)
            or not np.issubdtype(indptr.dtype, np.integer)
            or len(order) != n_edges
            or len(indptr) != n_nodes + 1
        ):
            return False
        order = order.astype(np.int64, copy=False)
        indptr = indptr.astype(np.int64, copy=False)
        if (
            int(indptr[0]) != 0
            or int(indptr[-1]) != n_edges
            or bool(np.any(indptr[1:] < indptr[:-1]))
        ):
            return False
        if n_edges:
            if bool(np.any(order < 0)) or bool(np.any(order >= n_edges)):
                return False
            # A bool marker proves permutation-ness with one byte/edge; bincount
            # would transiently allocate an int64 array as wide as the graph.
            seen = np.zeros(n_edges, dtype=bool)
            seen[order] = True
            if not bool(seen.all()):
                return False
            del seen
            previous = None
            # Strict lexicographic validation in bounded windows: do not hold
            # src[order], dst[order] and several boolean masks at E width.
            for start in range(0, n_edges, _VIZ_AUX_VALIDATION_CHUNK_ROWS):
                stop = min(n_edges, start + _VIZ_AUX_VALIDATION_CHUNK_ROWS)
                chunk_order = order[start:stop]
                chunk_sources = self.src[chunk_order]
                chunk_targets = self.dst[chunk_order]
                if previous is not None:
                    first = (
                        int(chunk_sources[0]), int(chunk_targets[0]),
                        int(chunk_order[0]),
                    )
                    if first < previous:
                        return False
                same_source = chunk_sources[1:] == chunk_sources[:-1]
                if (
                    bool(np.any(chunk_sources[1:] < chunk_sources[:-1]))
                    or bool(np.any(
                        same_source
                        & (chunk_targets[1:] < chunk_targets[:-1])
                    ))
                    or bool(np.any(
                        same_source
                        & (chunk_targets[1:] == chunk_targets[:-1])
                        & (chunk_order[1:] < chunk_order[:-1])
                    ))
                ):
                    return False
                previous = (
                    int(chunk_sources[-1]), int(chunk_targets[-1]),
                    int(chunk_order[-1]),
                )
            counts = np.bincount(self.src, minlength=n_nodes)
            if not np.array_equal(np.diff(indptr), counts[:n_nodes]):
                return False
        self._by_src_cache = (order, indptr)
        return True

    def _pair_edge_range(self, order, lo: int, hi: int, target: int):
        """在一个 source 段内按间接 dst 值二分，返回目标 pair 的半开区间。

        不能先做 ``self.dst[order[lo:hi]]``：高出度 hub 会因此仍读取完整邻接段。
        标量间接探针把未命中 target 的成本保持在 O(log degree)。
        """
        left, right = int(lo), int(hi)
        while left < right:
            middle = (left + right) // 2
            if int(self.dst[int(order[middle])]) < target:
                left = middle + 1
            else:
                right = middle
        first = left
        right = int(hi)
        while left < right:
            middle = (left + right) // 2
            if int(self.dst[int(order[middle])]) <= target:
                left = middle + 1
            else:
                right = middle
        return first, left

    def induced_edge_indices(self, node_indices) -> "np.ndarray":
        """原边序返回 ``node_indices`` 诱导子图中的边下标。

        对每个 kept source×kept target 在 dst-sorted source 段内间接二分，只
        读取命中 pair 的原边下标。因此即使 kept source 是百万出度 hub，小 core
        也不会扫描整个邻接段。最后按原边下标排序，精确保留旧输出顺序。
        全节点路径直接返回连续下标，不经过辅助索引。
        """
        keep = {
            int(value) for value in node_indices
            if 0 <= int(value) < int(self.n_nodes)
        }
        if not keep or not len(self):
            return np.zeros(0, dtype=np.int64)
        if len(keep) >= int(self.n_nodes):
            return np.arange(len(self), dtype=np.int64)
        order, indptr = self.by_src()
        matches = []
        targets = sorted(keep)
        for source in sorted(keep):
            if not (0 <= source < int(self.n_nodes)):
                continue
            lo, hi = int(indptr[source]), int(indptr[source + 1])
            if lo >= hi:
                continue
            for target in targets:
                pair_lo, pair_hi = self._pair_edge_range(
                    order, lo, hi, target
                )
                if pair_lo < pair_hi:
                    matches.append(order[pair_lo:pair_hi])
        if not matches:
            return np.zeros(0, dtype=np.int64)
        selected = matches[0] if len(matches) == 1 else np.concatenate(matches)
        # The auxiliary order groups pairs; legacy output follows the original
        # edge stream. Sorting original indices is precisely that order.
        return np.sort(selected, kind="stable")

    def edge_types_at_many(self, pairs, default=None) -> list:
        """批量查询有向边类型,结果与 ``pairs`` 对齐。

        每个不同 (source,target) 在 dst-sorted source 段内只做两次间接二分；
        同一 (s,t) 多边仍取原序最后一条。高出度焦点不会为一个小邻居窗口扫描
        整段，更不会为每个邻居重复扫描。
        """
        pairs = list(pairs)
        result = [default] * len(pairs)
        if not pairs or not len(self):
            return result
        wanted_pairs: dict = {}
        for result_index, pair in enumerate(pairs):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            source, target = pair
            if source is None or target is None:
                continue
            source = int(source)
            if not (0 <= source < int(self.n_nodes)):
                continue
            wanted_pairs.setdefault((source, int(target)), []).append(result_index)
        if not wanted_pairs:
            return result

        order, indptr = self.by_src()
        table = self.type_table
        for (source, target), slots in wanted_pairs.items():
            lo, hi = int(indptr[source]), int(indptr[source + 1])
            pair_lo, pair_hi = self._pair_edge_range(order, lo, hi, target)
            if pair_lo >= pair_hi:
                continue
            # original index is the final sort key, so the last position is the
            # same last-write winner as the historical dict.
            edge_index = int(order[pair_hi - 1])
            edge_type = table[int(self.code[edge_index])]
            for result_index in slots:
                result[result_index] = edge_type
        return result

    def edge_type_at(self, s_idx, t_idx, default=None):
        """有向边 (s_idx → t_idx) 的 edge_type;没有这条边返回 ``default``。

        同一 (s,t) 有多条边时取**最后一条**——与历史
        ``for s, t, et in edges: et_map[(s, t)] = et`` 的逐边覆盖逐位一致。

        ``default`` 让调用方能把「没有这条边」与「有这条边但 edge_type 恰是
        None/空」区分开(历史实现靠 ``(s, t) in et_map`` 区分)。"""
        if s_idx is None or t_idx is None or not len(self):
            return default
        s_idx = int(s_idx)
        if not (0 <= s_idx < int(self.n_nodes)):
            return default
        order, indptr = self.by_src()
        lo, hi = int(indptr[s_idx]), int(indptr[s_idx + 1])
        if lo >= hi:
            return default
        pair_lo, pair_hi = self._pair_edge_range(
            order, lo, hi, int(t_idx)
        )
        if pair_lo >= pair_hi:
            return default
        return self.type_table[int(self.code[int(order[pair_hi - 1])])]

    @classmethod
    def from_columns(cls, src, dst, code, type_table, n_nodes: int) -> "VizEdgeSet":
        """从**已经是下标**的三列构建(构建期路径)。只负责选最窄的码宽,不做
        越界过滤——调用方(arrays_from_graph)按 viz_ids 现场解析的下标本就合法。"""
        table = list(type_table)
        return cls(
            src=np.asarray(src, dtype=np.int32),
            dst=np.asarray(dst, dtype=np.int32),
            code=np.asarray(code, dtype=_viz_code_dtype(len(table))),
            type_table=table,
            n_nodes=int(n_nodes),
        )

    @classmethod
    def empty(cls, n_nodes: int) -> "VizEdgeSet":
        return cls(
            src=np.zeros(0, dtype=np.int32),
            dst=np.zeros(0, dtype=np.int32),
            code=np.zeros(0, dtype=np.uint16),
            type_table=[],
            n_nodes=int(n_nodes),
        )

    @classmethod
    def from_rows(cls, rows, viz_ids) -> "VizEdgeSet":
        """从历史三元组列表构建(旧 viz.npz 的加载路径 + 传 list 的调用方)。

        端点不在 viz_ids 里的行被丢弃:``arrays_from_graph`` 本就保证端点 ⊆
        viz_ids,而消费侧的 ``s in keep`` 过滤对这类行也一律为假,丢弃与保留在
        输出上等价(只影响 total_edges 这一个计数,且仅对 v9 之前那种连三元组
        形状都不是的远古产物)。"""
        node_index = {node_id: i for i, node_id in enumerate(viz_ids)}
        src: list = []
        dst: list = []
        code: list = []
        table: list = []
        seen: dict = {}
        dropped = 0
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                dropped += 1
                continue
            source_index = node_index.get(row[0])
            target_index = node_index.get(row[1])
            if source_index is None or target_index is None:
                dropped += 1
                continue
            edge_type = row[2]
            slot = seen.get(edge_type)
            if slot is None:
                slot = len(table)
                seen[edge_type] = slot
                table.append(edge_type)
            src.append(source_index)
            dst.append(target_index)
            code.append(slot)
        if dropped:
            _logger.debug("viz edge rows dropped during decode: %d", dropped)
        return cls.from_columns(src, dst, code, table, len(viz_ids))

    @classmethod
    def from_arrays(cls, src, dst, code, type_table, n_nodes: int) -> "VizEdgeSet":
        """从落盘的紧凑数组构建。三条数组长度必须一致(不一致说明那次写入不
        完整 → raise,由 load 侧统一判「产物不可信」);越界端点/类型码按行丢弃
        并记 warning(只计数,无正文),丢行本身还会被 load 侧的
        ``n_viz_edges`` 对账逮住,不至于安静地少掉边。"""
        src = np.asarray(src).ravel()
        dst = np.asarray(dst).ravel()
        code = np.asarray(code).ravel()
        if not (len(src) == len(dst) == len(code)):
            raise ValueError("viz edge arrays must be equal length")
        table = list(type_table)
        src = src.astype(np.int32, copy=False)
        dst = dst.astype(np.int32, copy=False)
        n_nodes = int(n_nodes)
        valid = (
            (src >= 0) & (src < n_nodes)
            & (dst >= 0) & (dst < n_nodes)
            & (code >= 0) & (code < len(table))
        )
        if len(src) and not bool(valid.all()):
            # warning, not debug: 紧凑数组是本进程自己写的,越界只可能来自半截
            # 写入或被改坏的产物——那是运维要看见的事。只记计数,不带任何正文。
            _logger.warning(
                "viz edge rows dropped as out of range: %d",
                int(len(src) - int(valid.sum())),
            )
            src, dst, code = src[valid], dst[valid], code[valid]
        return cls(src=src, dst=dst,
                   code=code.astype(_viz_code_dtype(len(table)), copy=False),
                   type_table=table, n_nodes=n_nodes)


def viz_edge_set_from_payload(viz_payload, viz_ids) -> VizEdgeSet:
    """把写入端的 ``viz_payload["edges"]`` 归一成 VizEdgeSet。

    构建器现在直接产出 VizEdgeSet;历史/测试调用方传三元组 list 也照收。

    边集的 ``n_nodes`` 必须与这次要落盘的 ``viz_ids`` 等长——端点存的是下标,
    对不上就是把边接到了另一份节点表上。响亮失败,绝不落一份自洽性已破的产物。"""
    edges = (viz_payload or {}).get("edges")
    if isinstance(edges, VizEdgeSet):
        if edges.n_nodes != len(viz_ids):
            raise ValueError(
                "viz edge set node count does not match viz_ids "
                f"({edges.n_nodes} != {len(viz_ids)})"
            )
        return edges
    return VizEdgeSet.from_rows(edges or [], viz_ids)


def viz_edge_count_is_authoritative(z) -> bool:
    """读回的边数能否拿去跟 manifest 的 ``n_viz_edges`` 对账。

    - 紧凑键:**能**。数组是这套代码自己写的,少一条就是产物坏了。
    - 一个边键都没有:**也能**。没有任何写入端产出过这种形状(零边图落的是
      长度为 0 的紧凑数组,旧写入端落的是 ``viz_edges``),所以它只可能是被裁
      过/半截写入——绝不能静默当成「这图本来就没有边」。
    - 旧 JSON 边键:**不能**。``from_rows`` 对远古形状的边行有合法丢弃(v9 之前
      的 dict 形状),老索引不能因此被判损坏(older-index-stays-valid)。"""
    files = set(getattr(z, "files", ()) or ())
    return _VIZ_EDGE_SRC_KEY in files or _VIZ_EDGE_LEGACY_KEY not in files


def viz_edge_npz_arrays(edge_set: VizEdgeSet) -> dict:
    """VizEdgeSet → np.savez 的关键字参数(紧凑键)。"""
    order, indptr = edge_set.by_src()
    return {
        "viz_edge_src": np.asarray(edge_set.src, dtype=np.int32),
        "viz_edge_dst": np.asarray(edge_set.dst, dtype=np.int32),
        "viz_edge_code": np.asarray(edge_set.code),
        "viz_edge_types": np.asarray(edge_set.type_table, dtype=object),
        _VIZ_EDGE_ORDER_KEY: np.asarray(order, dtype=np.int64),
        _VIZ_EDGE_INDPTR_KEY: np.asarray(indptr, dtype=np.int64),
    }


def viz_edge_set_from_npz(z, viz_ids) -> VizEdgeSet:
    """从 viz.npz 读回边集。紧凑键优先;只有旧 ``viz_edges`` JSON 键时一次性转
    成紧凑数组后释放中间 list(不常驻);两者都没有 → 空边集。"""
    files = set(getattr(z, "files", ()) or ())
    if _VIZ_EDGE_SRC_KEY in files:
        edge_set = VizEdgeSet.from_arrays(
            z["viz_edge_src"], z["viz_edge_dst"], z["viz_edge_code"],
            list(z["viz_edge_types"]), len(viz_ids),
        )
        if _VIZ_EDGE_ORDER_KEY in files and _VIZ_EDGE_INDPTR_KEY in files:
            edge_set.install_by_src(
                z[_VIZ_EDGE_ORDER_KEY], z[_VIZ_EDGE_INDPTR_KEY])
        return edge_set
    if _VIZ_EDGE_LEGACY_KEY in files:
        return VizEdgeSet.from_rows(
            decode_viz_edges(z[_VIZ_EDGE_LEGACY_KEY]), viz_ids)
    return VizEdgeSet.empty(len(viz_ids))


class VizArraysMixin:
    """ScaleIndex / VizIndex 共享的 viz 惰性派生索引。

    两个 dataclass 的 viz_* 字段是鸭子类型对等的(unified_graph 的有界分派与
    kg_neighbors 消费任一来源),这些派生索引因此也必须在两侧同义。惰性 +
    缓存:不看 KG 视图的部署一分钱不付,看的部署只付一次。"""

    def viz_node_index(self) -> dict:
        """id → viz_ids 下标。重复 id 取**最后**一次出现(与历史
        ``{n: v for n, v in zip(ids, ...)}`` 的后写覆盖一致)。"""
        cache = self.__dict__.get("_viz_node_index_cache")
        if cache is None:
            cache = {node_id: i for i, node_id in enumerate(self.viz_ids or [])}
            self._viz_node_index_cache = cache
        return cache

    def viz_deg_order(self) -> "np.ndarray":
        """度数降序的节点下标(**稳定**,并列保持 viz_ids 原序)——与历史
        ``sorted(range(n), key=lambda i: -int(deg[i]))`` 逐位等价。"""
        cache = self.__dict__.get("_viz_deg_order_cache")
        if cache is None:
            deg = self.viz_deg
            if deg is None:
                cache = np.zeros(0, dtype=np.int64)
            else:
                cache = np.argsort(
                    -np.asarray(deg, dtype=np.int64), kind="stable"
                )
            self._viz_deg_order_cache = cache
        return cache

    def install_viz_deg_order(self, order) -> bool:
        """校验并安装落盘的稳定 degree order;坏/旧辅助数组回退惰性重算。"""
        try:
            order = np.asarray(order).ravel()
            deg = np.asarray(self.viz_deg)
        except (TypeError, ValueError):
            return False
        n_nodes = len(self.viz_ids or [])
        if (
            not np.issubdtype(order.dtype, np.integer)
            or len(order) != n_nodes
            or deg.ndim != 1
            or len(deg) != n_nodes
            or not np.issubdtype(deg.dtype, np.integer)
        ):
            return False
        order = order.astype(np.int64, copy=False)
        if n_nodes:
            if bool(np.any(order < 0)) or bool(np.any(order >= n_nodes)):
                return False
            seen = np.zeros(n_nodes, dtype=bool)
            seen[order] = True
            if not bool(seen.all()):
                return False
        deg = deg.astype(np.int64, copy=False)
        ordered_deg = deg[order]
        if (
            n_nodes > 1
            and (
                bool(np.any(ordered_deg[1:] > ordered_deg[:-1]))
                or bool(np.any(
                    (ordered_deg[1:] == ordered_deg[:-1])
                    & (order[1:] < order[:-1])
                ))
            )
        ):
            return False
        self._viz_deg_order_cache = order
        return True


def viz_deg_order_array(viz_deg) -> "np.ndarray":
    """构建期生成可持久化的稳定 degree order。"""
    if viz_deg is None:
        return np.zeros(0, dtype=np.int64)
    return np.argsort(-np.asarray(viz_deg, dtype=np.int64), kind="stable")


def viz_arrays_shape_error(viz_ids, viz_deg, viz_types, viz_names,
                           viz_adj=None) -> str | None:
    """Validate the shared core viz arrays before installing auxiliaries.

    Missing auxiliary keys are a legacy compatibility case, but a short or
    non-integral degree vector is corrupt fact data: attempting to validate a
    persisted order against it used to raise IndexError from the request-time
    loader. Return a stable counts-only detail for the loader's unusable path.
    """
    n_nodes = len(viz_ids or [])
    try:
        degree = np.asarray(viz_deg)
    except (TypeError, ValueError):
        return "viz_deg 类型非法"
    if (
        degree.ndim != 1
        or len(degree) != n_nodes
        or not np.issubdtype(degree.dtype, np.integer)
    ):
        return "viz_deg 与 viz_ids 形状或类型不一致"
    if len(viz_types or []) != n_nodes or len(viz_names or []) != n_nodes:
        return "viz 标签数组与 viz_ids 长度不一致"
    if viz_adj is not None and tuple(viz_adj.shape) != (n_nodes, n_nodes):
        return "viz_adj 与 viz_ids 形状不一致"
    return None


@dataclass
class ScaleIndex(VizArraysMixin):
    node_ids: list
    node_index: dict
    transition: "sp.csr_matrix"
    idf: "np.ndarray"
    chunk_index: "np.ndarray"
    ann_labels: list
    ann_path: str
    manifest: dict
    # Folded concept-level viz graph (Task 4 / SP1). None when an older index has
    # no viz.npz (has_viz absent in manifest). Used to serve a bounded core for
    # the KG view + neighbors without re-deriving/folding the full graph.
    viz_ids: list = None          # folded node ids (canonical_id or raw object_id)
    viz_adj: "sp.csr_matrix" = None   # undirected adjacency over folded nodes
    viz_deg: "np.ndarray" = None  # int32 degree (viz_adj.getnnz(axis=1))
    viz_types: list = None        # object_type per folded node
    viz_names: list = None        # display name per folded node (hydration-free)
    viz_edges: "VizEdgeSet" = None  # directed-deduped folded edges (compact columnar)
    chunk_ann_labels: list = None   # chunk_id 列表(与 chunk_ann.bin 行对齐);无则 None
    chunk_ann_path: str = None      # chunk hnsw 文件路径;无则 None
    chunk_ann_source_names: list = None  # source code → source_id
    chunk_ann_source_codes: "np.ndarray" = None  # ANN row → source code
    chunk_ann_source_counts: "np.ndarray" = None  # source code → row count
    relation_ann_labels: list = None   # relation_id 列表(与 relation_ann.bin 行对齐);无则 None
    relation_ann_path: str = None      # relation hnsw 文件路径;无则 None
    ann_handle: object = None        # 惰性缓存的 hnswlib KG ANN handle(不落盘)
    chunk_ann_handle: object = None  # 惰性缓存的 chunk ANN handle(不落盘)
    relation_ann_handle: object = None  # 惰性缓存的 relation ANN handle(不落盘)


def _unusable(out_dir: str, detail: str):
    """索引产物不可信时的统一出口:记一条 warning(含目录与具体失配项,便于运维
    定位)并返回 None。None 对调用方等价于「没建过」,会触发全量重建。**绝不
    raise**——加载在检索热路径上,响亮地退化成「无索引」远好过把错配的索引
    静默返回。"""
    _logger.warning("scale index at %s is unusable: %s", out_dir, detail)
    return None


MANIFEST_LIBRARY_KEY = "library_versions"
_LIBRARY_VERSIONS: "dict | None" = None


def runtime_library_versions() -> dict:
    """本进程构建 scale 工件所用的三个库版本(W-CLI T-W3)。

    写进 manifest 的 ``library_versions``,给**异机构建**留下比对依据:工件本身
    可移植(manifest 无机器绑定、npy/npz 平台无关、hnswlib ``.bin`` 是数据),
    但 ``requirements`` 只有下界 ``>=``,两台机器的 hnswlib 漂移是常态,而
    ``.bin`` **没有格式版本头** —— 版本不符时 ``load_index`` 可能不报错而读出
    垃圾,被 open_ann 的 fail-open 吞成「静默零召回」。所以版本必须随工件落盘。

    取值优先用已导入模块的 ``__version__``(numpy/scipy 有),hnswlib 没有该属性
    → 回落发行版元数据。任何一项取不到就**不写这个键**:版本发现绝不能让一次
    多小时的构建失败,缺键在读侧等价于「未知,不判失配」(older-index-stays-valid)。
    进程级 memo:版本在进程生命周期内不会变。
    """
    global _LIBRARY_VERSIONS
    if _LIBRARY_VERSIONS is None:
        import sys
        from importlib.metadata import version as _distribution_version

        found: dict = {}
        for name in ("hnswlib", "numpy", "scipy"):
            value = getattr(sys.modules.get(name), "__version__", None)
            if not value:
                try:
                    value = _distribution_version(name)
                except Exception:  # noqa: BLE001 — 见 docstring:绝不失败构建。
                    value = None
            if value:
                found[name] = str(value)
        _LIBRARY_VERSIONS = found
    return dict(_LIBRARY_VERSIONS)


def _manifest_count(manifest: dict, key: str):
    """取 manifest 里的计数字段。缺键(老索引根本没写过 n_ann/n_viz_nodes 这类
    键)或不是整数 → None,表示「这一项不参与校验」。older-index-stays-valid:
    缺键一律放行,绝不能把老索引判成损坏。"""
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def estimated_ann_bytes(manifest: dict, runtime_dim: int) -> int:
    """Estimated resident bytes of a ScaleIndex's ANN matrices — its kg (n_ann),
    chunk (n_chunk_ann) and relation (n_relation_ann) hnsw handles, each
    rows×runtime_dim×4 (float32). This is the dominant term of a large index's
    footprint and drives the LargeAwareLRUCache 'is large' classification
    (PR-4 / codex PR#359 r1 P1 — n_nodes alone misses a chunk/relation-ANN-heavy
    source base). A missing count contributes 0 (older indexes stay classified
    small rather than being over-evicted on an absent key)."""
    rows = sum(
        (_manifest_count(manifest, key) or 0)
        for key in ("n_ann", "n_chunk_ann", "n_relation_ann")
    )
    return rows * int(runtime_dim) * 4


def _first_mismatch(checks) -> str | None:
    """checks: [(名称, 期望, 实际)]。期望为 None 表示该项不参与校验(缺键)。
    返回第一条失配的人可读描述;全部通过返回 None。"""
    for label, expected, actual in checks:
        if expected is not None and expected != actual:
            return f"{label}不一致(期望 {expected},实际 {actual})"
    return None


def load_scale_index(out_dir: str):
    """从 out_dir 加载持久化的 ScaleIndex。manifest 不存在或目录不存在时返回 None。
    ANN 索引不预加载（延迟由调用方按需用 ann_path 打开）。

    加载时顺带做一层零成本的自洽校验:manifest 计数 vs 数组长度、数组之间的
    形状关系。任一产物读不出来、或任一项失配 → 记 warning 并返回 None(与
    「没建过」等价,调用方据此走全量重建)。这一层是为了兜住已经躺在盘上的坏
    索引——历史上全量重建直写活目录,崩在中途会留下「旧 manifest + 半截数组」
    这种看着能加载、其实描述的是别的数据的目录。
    校验一律只在 manifest 里该键存在时生效(older-index-stays-valid),可选产物
    (viz / chunk_ann / relation_ann)只在本次真的读出来了才比对——文件不存在时
    维持既有的「跳过该可选产物」语义,不升级成整份索引损坏。"""
    mpath = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(mpath):
        return None
    try:
        with open(mpath) as fh:
            manifest = json.load(fh)
    except Exception as exc:  # noqa: BLE001 — 半截/损坏的 manifest 也是「不可信」
        return _unusable(out_dir, f"manifest.json 读不出来({exc!r})")
    if not isinstance(manifest, dict):
        return _unusable(out_dir, "manifest.json 不是一个对象")

    # 核心产物:任一缺失/反序列化失败都视为损坏(broad except 是刻意的——任何
    # 读不出来的理由都指向同一个结论:这份索引不能用)。
    try:
        transition = sp.load_npz(os.path.join(out_dir, "graph.npz"))
        node_ids = list(np.load(os.path.join(out_dir, "node_ids.npy"), allow_pickle=True))
        idf = np.load(os.path.join(out_dir, "idf.npy"))
        chunk_index = np.load(os.path.join(out_dir, "chunk_index.npy"))
        ann_labels = list(np.load(os.path.join(out_dir, "ann_labels.npy"), allow_pickle=True))
    except Exception as exc:  # noqa: BLE001
        return _unusable(out_dir, f"核心产物读不出来({exc!r})")

    n_nodes = len(node_ids)
    detail = _first_mismatch([
        ("manifest.n_nodes 与 node_ids.npy 行数",
         _manifest_count(manifest, "n_nodes"), n_nodes),
        # 写入端两条路径都保证转移阵是 len(node_ids) 阶方阵:
        # build_transition_arrays 直接按 (n, n) 建,fold 走 splice_active 同样按
        # combined_ids 的长度建。冻结的 v9 老工件也满足(既有兼容用例已在断言)。
        ("graph.npz 行数与 node_ids.npy 行数", n_nodes, int(transition.shape[0])),
        ("graph.npz 列数与 node_ids.npy 行数", n_nodes, int(transition.shape[1])),
        # idf 同理:full 逐 node_id append,fold 里 fold_arrays 按 len(node_ids)
        # 开数组,两条路径都恰好一一对应。
        ("idf.npy 长度与 node_ids.npy 行数", n_nodes, int(len(idf))),
        ("manifest.n_ann 与 ann_labels.npy 行数",
         _manifest_count(manifest, "n_ann"), len(ann_labels)),
    ])
    if detail is not None:
        return _unusable(out_dir, detail)

    viz_ids = viz_adj = viz_deg = viz_types = viz_names = viz_edges = None
    viz_deg_order = None
    viz_edges_authoritative = False
    chunk_ann_labels = chunk_ann_path = None
    chunk_ann_source_names = None
    chunk_ann_source_codes = chunk_ann_source_counts = None
    relation_ann_labels = relation_ann_path = None
    try:
        if manifest.get("has_viz"):
            viz_npz = os.path.join(out_dir, "viz.npz")
            viz_adj_path = os.path.join(out_dir, "viz_adj.npz")
            if os.path.exists(viz_npz) and os.path.exists(viz_adj_path):
                with np.load(viz_npz, allow_pickle=True) as z:
                    viz_ids = list(z["viz_ids"])
                    viz_deg = z["viz_deg"]
                    viz_types = list(z["viz_types"])
                    viz_names = list(z["viz_names"])
                    viz_edges = viz_edge_set_from_npz(z, viz_ids)
                    if _VIZ_DEG_ORDER_KEY in z.files:
                        viz_deg_order = np.asarray(z[_VIZ_DEG_ORDER_KEY])
                    viz_edges_authoritative = viz_edge_count_is_authoritative(z)
                viz_adj = sp.load_npz(viz_adj_path)
                viz_detail = viz_arrays_shape_error(
                    viz_ids, viz_deg, viz_types, viz_names, viz_adj
                )
                if viz_detail is not None:
                    return _unusable(out_dir, viz_detail)

        if manifest.get("has_chunk_ann"):
            labpath = os.path.join(out_dir, "chunk_ann_labels.npy")
            if os.path.exists(labpath):
                chunk_ann_labels = list(np.load(labpath, allow_pickle=True))
                chunk_ann_path = os.path.join(out_dir, "chunk_ann.bin")

        if manifest.get("has_chunk_ann_sources"):
            names_path = os.path.join(out_dir, "chunk_ann_source_names.npy")
            codes_path = os.path.join(out_dir, "chunk_ann_source_codes.npy")
            counts_path = os.path.join(out_dir, "chunk_ann_source_counts.npy")
            if not all(os.path.exists(path) for path in (
                names_path, codes_path, counts_path
            )):
                return _unusable(out_dir, "chunk ANN 来源映射文件缺失")
            names_array = np.load(names_path, allow_pickle=True)
            codes_array = np.load(codes_path)
            counts_array = np.load(counts_path)
            if (
                names_array.ndim != 1
                or codes_array.ndim != 1
                or counts_array.ndim != 1
                or not np.issubdtype(codes_array.dtype, np.signedinteger)
                or not np.issubdtype(counts_array.dtype, np.integer)
                or any(
                    not isinstance(value, (str, np.str_)) or not str(value)
                    for value in names_array
                )
                or len({str(value) for value in names_array})
                != len(names_array)
            ):
                return _unusable(out_dir, "chunk ANN 来源映射类型或维度非法")
            chunk_ann_source_names = [str(value) for value in names_array]
            chunk_ann_source_codes = np.asarray(codes_array, dtype=np.int64)
            chunk_ann_source_counts = np.asarray(counts_array, dtype=np.int64)
            if (
                np.any(chunk_ann_source_codes < -1)
                or (
                    not chunk_ann_source_names
                    and np.any(chunk_ann_source_codes >= 0)
                )
                or (
                    chunk_ann_source_names
                    and np.any(
                        chunk_ann_source_codes
                        >= len(chunk_ann_source_names)
                    )
                )
            ):
                return _unusable(out_dir, "chunk ANN 来源代码越界")
            actual_counts = np.bincount(
                chunk_ann_source_codes[chunk_ann_source_codes >= 0],
                minlength=len(chunk_ann_source_names),
            )
            if (
                np.any(chunk_ann_source_counts < 0)
                or not np.array_equal(
                    actual_counts.astype(np.int64, copy=False),
                    chunk_ann_source_counts,
                )
            ):
                return _unusable(out_dir, "chunk ANN 来源计数与行映射不一致")

        if manifest.get("has_relation_ann"):
            rlabpath = os.path.join(out_dir, "relation_ann_labels.npy")
            if os.path.exists(rlabpath):
                relation_ann_labels = list(np.load(rlabpath, allow_pickle=True))
                relation_ann_path = os.path.join(out_dir, "relation_ann.bin")
    except Exception as exc:  # noqa: BLE001
        return _unusable(out_dir, f"可选产物读不出来({exc!r})")

    # 可选产物的计数与主索引出自同一次写入,失配即证明那次写入不完整——与主
    # 产物一视同仁判损坏。
    optional_checks = []
    if viz_ids is not None:
        optional_checks.append((
            "manifest.n_viz_nodes 与 viz.npz 节点数",
            _manifest_count(manifest, "n_viz_nodes"), len(viz_ids)))
    if viz_edges is not None and viz_edges_authoritative:
        # 只对紧凑键对账:数组是这套代码自己写的,manifest 说有 N 条就必须读回
        # N 条。缺键(半截写入/被裁过的产物)会让 viz_edge_set_from_npz 安静地
        # 给出空边集,整个 KG 视图退化成「只有点没有边」而无人察觉——这一条把
        # 它变成响亮的「索引不可信」。旧 JSON 键路径**不**对账:from_rows 对远
        # 古形状的边行有合法丢弃,老索引不能因此被判损坏。
        optional_checks.append((
            "manifest.n_viz_edges 与 viz.npz 边数",
            _manifest_count(manifest, "n_viz_edges"), len(viz_edges)))
    if chunk_ann_labels is not None:
        optional_checks.append((
            "manifest.n_chunk_ann 与 chunk_ann_labels.npy 行数",
            _manifest_count(manifest, "n_chunk_ann"), len(chunk_ann_labels)))
    if chunk_ann_source_codes is not None:
        optional_checks.extend([
            (
                "chunk_ann_source_codes.npy 与 chunk ANN 行数",
                len(chunk_ann_labels or []),
                len(chunk_ann_source_codes),
            ),
            (
                "chunk_ann_source_counts.npy 与 source names 行数",
                len(chunk_ann_source_names or []),
                len(chunk_ann_source_counts),
            ),
        ])
    if relation_ann_labels is not None:
        optional_checks.append((
            "manifest.n_relation_ann 与 relation_ann_labels.npy 行数",
            _manifest_count(manifest, "n_relation_ann"), len(relation_ann_labels)))
    detail = _first_mismatch(optional_checks)
    if detail is not None:
        return _unusable(out_dir, detail)

    index = ScaleIndex(
        node_ids=node_ids,
        node_index={n: i for i, n in enumerate(node_ids)},
        transition=transition,
        idf=idf,
        chunk_index=chunk_index,
        ann_labels=ann_labels,
        ann_path=os.path.join(out_dir, "ann.bin"),
        manifest=manifest,
        viz_ids=viz_ids,
        viz_adj=viz_adj,
        viz_deg=viz_deg,
        viz_types=viz_types,
        viz_names=viz_names,
        viz_edges=viz_edges,
        chunk_ann_labels=chunk_ann_labels,
        chunk_ann_path=chunk_ann_path,
        chunk_ann_source_names=chunk_ann_source_names,
        chunk_ann_source_codes=chunk_ann_source_codes,
        chunk_ann_source_counts=chunk_ann_source_counts,
        relation_ann_labels=relation_ann_labels,
        relation_ann_path=relation_ann_path,
    )
    if viz_deg_order is not None:
        # Invalid auxiliary data is safe to ignore: previous compact artifacts
        # may carry the older src-only edge order, and both helpers have exact
        # lazy rebuilds. Core viz array corruption was rejected above.
        index.install_viz_deg_order(viz_deg_order)
    return index


def personalized_ppr(
    transition: "sp.csr_matrix",
    reset: "np.ndarray",
    damping: float = 0.5,
    tol: float = 1e-8,
    max_iter: int = 100,
    stats: dict = None,
) -> "np.ndarray":
    """个性化 PageRank 幂迭代。

    transition: 列随机转移阵 A（A[j,i] = 边 i->j 的归一化权重，按 i 的出度归一）。
    reset:      personalization 向量（非负；内部归一为和=1 作为 teleport 分布）。
    返回稳态分布 x（和=1，dtype 跟随 transition：float32 输入 → float32 输出，
    float64 输入 → float64 输出）；全零 reset → 全零向量（调用方据此回退 dense）。
    stats: 可选出参 dict,写入 {"iters": 实际迭代轮数}(纯观测,不影响数值)。
    """
    s = float(reset.sum())
    if s <= 0:
        if stats is not None:
            stats["iters"] = 0
        return np.zeros(transition.shape[0], dtype=np.float64)
    # dtype 跟随转移阵(float32 时迭代全程 float32,SpMV 带宽减半);residual 求和
    # 用 float64 累积消掉求和噪声。float32 的 L1 残差噪声地板≈eps×Σ|x|≈1e-7,
    # tol 低于它会永远达不到→空转满 max_iter 反而更慢,防御 clamp 到 1e-6。
    dt = transition.dtype if transition.dtype in (np.float32, np.float64) else np.float64
    eff_tol = max(float(tol), 1e-6) if dt == np.float32 else float(tol)
    p = reset.astype(dt) / s
    x = p.copy()
    d = float(damping)
    iters = 0
    for _ in range(max_iter):
        iters += 1
        x_new = (1.0 - d) * p + d * transition.dot(x)
        x_new += (1.0 - float(x_new.sum())) * p
        if float(np.abs(x_new - x).sum(dtype=np.float64)) < eff_tol:
            x = x_new
            break
        x = x_new
    if stats is not None:
        stats["iters"] = iters
    total = float(x.sum())
    return x / total if total > 0 else x


def build_transition(
    node_ids: List[str],
    edges: List[Tuple[str, str, float]],
) -> Tuple["sp.csr_matrix", Dict[str, int]]:
    """边列表 -> 列随机转移阵 A（A[j,i]=i->j 归一化权重）。

    端点不在 node_ids 的边丢弃（防悬空）。out-degree 加权归一。返回 (A_csr, index)。
    调用方负责把无向边拆成正反两条。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    if not edges:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    src = np.fromiter((index.get(s, -1) for s, _, _ in edges), dtype=np.int64, count=len(edges))
    tgt = np.fromiter((index.get(t, -1) for _, t, _ in edges), dtype=np.int64, count=len(edges))
    w = np.fromiter((float(wt) for _, _, wt in edges), dtype=np.float64, count=len(edges))
    keep = (src >= 0) & (tgt >= 0)
    src, tgt, w = src[keep], tgt[keep], w[keep]
    if src.size == 0:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    M = sp.csr_matrix((w, (tgt, src)), shape=(n, n), dtype=np.float64)   # A[j,i]=i->j
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index


def build_transition_arrays(
    node_ids: List[str],
    src: "np.ndarray",
    tgt: "np.ndarray",
    w: "np.ndarray",
) -> Tuple["sp.csr_matrix", Dict[str, int]]:
    """Array fast-path of build_transition: edges already as int-indexed
    (src, tgt, w) numpy arrays (int32/int32/float32-or-float64) instead of
    (str, str, float) tuples — skips the Python-level id→index dict lookups
    per edge that build_transition does via index.get(s, -1)/index.get(t, -1)
    generator expressions (490k+ edges at scale, each a Python-object round
    trip). Caller is responsible for encoding edges against `index =
    {nid: i for i, nid in enumerate(node_ids)}` and for dropping/keeping
    dangling endpoints as it sees fit (this function does NOT re-validate
    src/tgt bounds — pass already-valid indices only). Output is element-wise
    identical to build_transition given the same node_ids and the same
    (decoded) edge set.
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    if src.size == 0:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    src64 = src.astype(np.int64, copy=False)
    tgt64 = tgt.astype(np.int64, copy=False)
    w64 = w.astype(np.float64, copy=False)
    M = sp.csr_matrix((w64, (tgt64, src64)), shape=(n, n), dtype=np.float64)  # A[j,i]=i->j
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index


def _persist_ann(out_dir, bin_name, prebuilt, vectors, n_labels, dim, ef) -> None:
    """Save one hnsw index to out_dir/bin_name. Reuse ``prebuilt`` when its
    element count matches ``n_labels`` (build() now pre-builds every ANN inline
    and frees the matrix before persist — so at scale the matrices never coexist
    with their indexes here); otherwise build from ``vectors`` (float32, may be
    None → empty index). Mirrors the frozen hnsw params (cosine / M=16 /
    random_seed=42). Never emits a row-count-mismatched .bin (defensive count
    check before trusting a prebuilt handle)."""
    import hnswlib

    idx = None
    if prebuilt is not None:
        try:
            if int(prebuilt.get_current_count()) == n_labels:
                idx = prebuilt
        except Exception:  # noqa: BLE001 — defensive: never trust a broken handle
            idx = None
    if idx is None:
        vecs = (
            np.asarray(vectors, dtype=np.float32)
            if vectors is not None and len(vectors)
            else None
        )
        # A count-mismatched (or absent) prebuilt handle with NO matrix to
        # rebuild from would silently write an EMPTY index alongside n_labels
        # labels — itself a row-count mismatch that collapses recall. Fail loud
        # rather than corrupt: build() always pre-builds from the same rows as
        # the labels, so this only fires on a genuinely inconsistent caller.
        if vecs is None and n_labels > 0:
            raise ValueError(
                f"_persist_ann({bin_name}): {n_labels} labels but no usable "
                f"vectors and no size-matching prebuilt index — refusing to "
                f"write a row-count-mismatched .bin"
            )
        n_rows = int(vecs.shape[0]) if vecs is not None else 0
        use_dim = int(vecs.shape[1]) if n_rows > 0 else dim
        if use_dim < 1:
            use_dim = 1
        idx = hnswlib.Index(space="cosine", dim=use_dim)
        idx.init_index(
            max_elements=max(1, n_rows or n_labels),
            ef_construction=ef, M=16, random_seed=42,
        )
        if n_rows > 0:
            idx.add_items(vecs, np.arange(n_rows))
    idx.save_index(os.path.join(out_dir, bin_name))


def save_scale_index(
    out_dir: str,
    *,
    node_ids: List[str],
    transition: "sp.csr_matrix",
    idf: List[float],
    chunk_index: List[int],
    ann_labels: List[str],
    manifest: dict,
    ann_vectors=None,
    viz_ids: List[str] = None,
    viz_adj: "sp.csr_matrix" = None,
    viz_deg=None,
    viz_types: List[str] = None,
    viz_names: List[str] = None,
    viz_payload: dict = None,
    chunk_ann_vectors=None,
    chunk_ann_labels: List[str] = None,
    chunk_ann_source_names: List[str] = None,
    chunk_ann_source_codes=None,
    chunk_ann_source_counts=None,
    relation_ann_vectors=None,
    relation_ann_labels: List[str] = None,
    prebuilt_ann=None,
    prebuilt_chunk_ann=None,
    prebuilt_relation_ann=None,
    ef_construction: int = 200,
) -> dict:
    """把构建好的数组落盘到 out_dir。

    ann_vectors: (m, dim) float32 numpy array（只含有 embeddings 的 KG 节点）。
    ann_labels: 对应 kg 节点 object_id 列表（与 ann_vectors 行对齐）。
    写出 7 个文件：graph.npz, node_ids.npy, idf.npy, chunk_index.npy,
    ann.bin, ann_labels.npy, manifest.json。返回传入的 manifest dict。

    relation_ann_vectors/relation_ann_labels: 镜像 chunk_ann_vectors/labels——
    只在传入 relation_ann_labels 非空时才写 relation_ann.bin +
    relation_ann_labels.npy 并置 manifest["has_relation_ann"]=True/
    n_relation_ann。旧索引（无这两个参数）不受影响——manifest 缺该键，
    load_scale_index 照常跳过，老索引继续可加载（同 has_chunk_ann 的
    older-index-stays-valid 性质）。

    当传入折叠 viz 图（viz_ids 非空）时额外写 viz.npz + viz_adj.npz 并把
    manifest["has_viz"]=True，供 unified_graph/neighbors 有界分派直接消费。

    prebuilt_ann: 调用方已建好并 add_items 完毕的 hnswlib.Index（比如
    emb_synonym_edges 那次 KNN 复用的同一个 index）。传入时跳过本函数自己的
    init_index+add_items，直接 save_index——避免 490k 节点规模下对同一批
    KG 向量重复构建 hnsw（流水线里最贵的计算）。防御性对齐检查：其元素数
    （get_current_count()）必须等于 len(ann_labels)，否则视为不可信（调用方
    传错/竞态）而回退到本函数自建，绝不静默产出行数错位的 ann.bin。
    """
    import hnswlib

    os.makedirs(out_dir, exist_ok=True)

    sp.save_npz(os.path.join(out_dir, "graph.npz"), transition)
    np.save(os.path.join(out_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
    np.save(os.path.join(out_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
    np.save(os.path.join(out_dir, "chunk_index.npy"), np.asarray(chunk_index, dtype=np.int32))
    np.save(os.path.join(out_dir, "ann_labels.npy"), np.asarray(ann_labels, dtype=object))

    # Folded viz graph (Task 4). Persisted only when provided; manifest flag lets
    # load_scale_index skip when absent (older indexes stay valid).
    if viz_ids is not None and viz_adj is not None:
        np.savez(
            os.path.join(out_dir, "viz.npz"),
            viz_ids=np.asarray(viz_ids, dtype=object),
            viz_deg=np.asarray(viz_deg, dtype=np.int32),
            viz_deg_order=viz_deg_order_array(viz_deg),
            viz_types=np.asarray(viz_types, dtype=object),
            viz_names=np.asarray(viz_names, dtype=object),
            **viz_edge_npz_arrays(
                viz_edge_set_from_payload(viz_payload, viz_ids)
            ),
        )
        sp.save_npz(os.path.join(out_dir, "viz_adj.npz"), viz_adj.tocsr())
        manifest = {**manifest, "has_viz": True}

    # KG dim: prefer the actual matrix width when a matrix is still passed
    # (fallback / older callers), else the manifest's built dim (build() frees
    # ann_vectors before persist now, so it is normally None here).
    dim = int(manifest.get("dim", 1))
    if ann_vectors is not None and len(ann_vectors):
        _av = np.asarray(ann_vectors, dtype=np.float32)
        if _av.shape[0] > 0:
            dim = int(_av.shape[1])
    if dim < 1:
        dim = 1

    _persist_ann(out_dir, "ann.bin", prebuilt_ann, ann_vectors,
                 len(ann_labels), dim, ef_construction)

    # Chunk-level ANN (Task 1): persisted only when chunk labels are provided;
    # reuses build()'s pre-built index (matrix already freed) or rebuilds from a
    # passed matrix. manifest flag lets load_scale_index skip when absent.
    if chunk_ann_labels:
        _persist_ann(out_dir, "chunk_ann.bin", prebuilt_chunk_ann,
                     chunk_ann_vectors, len(chunk_ann_labels), dim, ef_construction)
        np.save(os.path.join(out_dir, "chunk_ann_labels.npy"), np.asarray(chunk_ann_labels, dtype=object))
        manifest = {**manifest, "has_chunk_ann": True, "n_chunk_ann": len(chunk_ann_labels)}
        if (
            chunk_ann_source_names is not None
            and chunk_ann_source_codes is not None
            and chunk_ann_source_counts is not None
        ):
            source_codes = np.asarray(chunk_ann_source_codes, dtype=np.int32)
            source_counts = np.asarray(chunk_ann_source_counts, dtype=np.int64)
            if len(source_codes) != len(chunk_ann_labels):
                raise ValueError("chunk ANN source codes must align with labels")
            if len(source_counts) != len(chunk_ann_source_names):
                raise ValueError("chunk ANN source counts must align with names")
            np.save(
                os.path.join(out_dir, "chunk_ann_source_names.npy"),
                np.asarray(chunk_ann_source_names, dtype=object),
            )
            np.save(
                os.path.join(out_dir, "chunk_ann_source_codes.npy"), source_codes
            )
            np.save(
                os.path.join(out_dir, "chunk_ann_source_counts.npy"), source_counts
            )
            manifest = {**manifest, "has_chunk_ann_sources": True}

    # Relation-level ANN (mirrors chunk): pre-built index or rebuild from matrix.
    if relation_ann_labels:
        _persist_ann(out_dir, "relation_ann.bin", prebuilt_relation_ann,
                     relation_ann_vectors, len(relation_ann_labels), dim, ef_construction)
        np.save(os.path.join(out_dir, "relation_ann_labels.npy"), np.asarray(relation_ann_labels, dtype=object))
        manifest = {**manifest, "has_relation_ann": True, "n_relation_ann": len(relation_ann_labels)}

    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)

    return manifest


def save_fold_core(out_dir, node_ids, transition, idf, chunk_index) -> None:
    """Persist the non-ANN fold files in their historical checkpoint order."""
    sp.save_npz(os.path.join(out_dir, "graph.npz"), transition)
    np.save(os.path.join(out_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
    np.save(os.path.join(out_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
    np.save(
        os.path.join(out_dir, "chunk_index.npy"),
        np.asarray(chunk_index, dtype=np.int32),
    )


def save_fold_ann(out_dir, index_name, labels_name, ann, labels) -> None:
    """Persist one fold ANN handle and its row-aligned object labels."""
    ann.save_index(os.path.join(out_dir, index_name))
    np.save(os.path.join(out_dir, labels_name), np.asarray(labels, dtype=object))


def save_fold_chunk_sources(out_dir, names, codes, counts) -> None:
    """Persist the compact chunk ANN source-filter sidecar during a fold."""
    np.save(
        os.path.join(out_dir, "chunk_ann_source_names.npy"),
        np.asarray(names, dtype=object),
    )
    np.save(
        os.path.join(out_dir, "chunk_ann_source_codes.npy"),
        np.asarray(codes, dtype=np.int32),
    )
    np.save(
        os.path.join(out_dir, "chunk_ann_source_counts.npy"),
        np.asarray(counts, dtype=np.int64),
    )


def copy_fold_viz(live_dir, temporary_dir) -> None:
    """Carry the optional UI-only viz artifacts into a fold directory."""
    for filename in ("viz.npz", "viz_adj.npz"):
        source = os.path.join(live_dir, filename)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(temporary_dir, filename))


def save_fold_manifest(out_dir, manifest) -> None:
    """Write the fold manifest last, immediately before the directory swap."""
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)


def csr_to_edges(
    node_ids: List[str],
    transition: "sp.csr_matrix",
) -> List[Tuple[str, str, float]]:
    """Reconstruct an edge list (src, dst, 1.0) from a column-stochastic CSR
    transition matrix.  Mirrors the base-edge reconstruction inside splice_active.
    A[target_row, source_col] → edge source->target = node_ids[col]->node_ids[row].

    NOT on the hot path any more: the multi-base combined-graph loop that used
    to feed its output into splice_active now calls ``splice_csr`` (which does
    the same coordinate mapping in numpy, without one Python tuple per nnz).
    Kept as the readable reference decoding — and as the oracle the splice_csr
    equivalence tests compare against.
    """
    coo = transition.tocoo()
    return [(node_ids[i], node_ids[j], 1.0) for i, j in zip(coo.col, coo.row)]


def _column_stochastic(
    n: int,
    src: "np.ndarray",
    tgt: "np.ndarray",
    w: "np.ndarray",
) -> "sp.csr_matrix":
    """(src, tgt, w) 索引三元组 -> 列随机 CSR A（A[j,i]=i->j 按 i 出度归一）。

    splice_active / splice_csr 共用的收尾算术,逐位复刻 splice_active 的历史实现
    （COO 重复坐标由 scipy 累加,零列除数 clamp 到 1.0,右乘 diags 归一）。抽出来
    只为让两个 splice 入口不出现第二份写法——数值行为一个字都没改。
    """
    if src.size == 0:
        return sp.csr_matrix((n, n), dtype=np.float64)
    M = sp.csr_matrix((w, (tgt, src)), shape=(n, n), dtype=np.float64)
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr()


def splice_csr(
    base_ids: List[str],
    base_transition: "sp.csr_matrix",
    extra_ids: List[str],
    extra_transition: "sp.csr_matrix",
) -> Tuple[List[str], "sp.csr_matrix"]:
    """CSR⊕CSR 版的 splice：把另一份**已建好的**转移阵并进 base，按 id 合一。

    ⚠ 两侧的边权都**重置为 1.0**、只取稀疏结构——转移阵已列归一,权重信息本就
    有损,这是 splice_active 对 base 侧的既有约定,这里对 extra 侧同样成立。要保
    留权重的调用方请走 ``splice_active`` 的三元组入口。

    与 ``splice_active(base_ids, base_A, extra_ids,
    csr_to_edges(extra_ids, extra_transition))`` **逐位等价**,但不物化那份长度
    等于 extra nnz 的 Python tuple 列表、也不为每条边做两次 dict 查找：extra 的
    COO 坐标经一次 numpy 花式索引直接 remap 到 combined 下标空间。多参考库组合
    图（_scale_combined_graph）逐库拼接时,extra 是整座 base 图,nnz 可达百万级,
    tuple 往返本身就是主要成本。

    等价性论证（三步都是逐位的,不是"数值上接近"）：
    1. id 去重序：``base_ids + [e for e in extra_ids if e not in base_set]``,
       与 splice_active 对 active_ids 的写法逐字相同;
    2. extra 边解码：csr_to_edges 产出 ``(extra_ids[col], extra_ids[row], 1.0)``,
       splice_active 随后用 ``index.get(...)`` 把两端映回 combined 下标——等于
       ``remap[col] / remap[row]``,其中 ``remap[i] = index[extra_ids[i]]``（本函数
       预先算好的映射数组）。extra_ids 全部在 combined_ids 里,故不可能出现
       splice_active 里那条 ``-1`` 悬空丢弃分支;extra_ids 内部若有重复 id,
       ``index`` 这个 dict 推导是后写覆盖,两边读到的都是**最后一次出现**的下标
       ——两条路径共用同一个 dict,等价性不受影响;
    3. 收尾：base 边（src=col,tgt=row,权 1.0）在前、extra 边在后拼接,喂给同一个
       ``_column_stochastic``——与 splice_active 的拼接顺序和算术完全相同,连 COO
       重复坐标的累加顺序都一样。
    """
    base_set = set(base_ids)
    combined_ids = list(base_ids) + [e for e in extra_ids if e not in base_set]
    index = {nid: i for i, nid in enumerate(combined_ids)}
    n = len(combined_ids)
    base_coo = base_transition.tocoo()
    # base 结构还原（与 splice_active 同一语义）：source=col, target=row, 权 1.0。
    # combined 前 len(base_ids) 项与 base 对齐,下标可直接复用。
    base_src = base_coo.col.astype(np.int64)
    base_tgt = base_coo.row.astype(np.int64)
    extra_coo = extra_transition.tocoo()
    if extra_coo.nnz:
        # 旧下标 -> combined 下标的映射数组;逐边 Python 循环换成一次花式索引。
        remap = np.fromiter(
            (index[nid] for nid in extra_ids),
            dtype=np.int64,
            count=len(extra_ids),
        )
        src = np.concatenate([base_src, remap[extra_coo.col.astype(np.int64)]])
        tgt = np.concatenate([base_tgt, remap[extra_coo.row.astype(np.int64)]])
    else:
        src, tgt = base_src, base_tgt
    w = np.ones(src.size, dtype=np.float64)
    return combined_ids, _column_stochastic(n, src, tgt, w)


def splice_csr_many(
    base_ids: List[str],
    base_transition: "sp.csr_matrix",
    extras: "Sequence[Tuple[List[str], sp.csr_matrix]]",
) -> Tuple[List[str], "sp.csr_matrix"]:
    """Combine several indexed graphs with one final CSR construction.

    This is exactly the historical left fold of :func:`splice_csr`, including
    its deliberately order-sensitive structural reset.  After every fold the
    accumulated transition is decoded as an unweighted structure.  Therefore,
    immediately before the final fold, all edges from the base and earlier
    extras have weight ``1`` regardless of how many earlier libraries shared
    that coordinate; an edge also present in the *last* extra has weight ``2``.

    We reproduce that result without materialising and normalising a growing
    CSR once per participant: map every prefix graph into the final coordinate
    space, collapse the prefix to a structural union once, append the last
    extra's structure once, then run the shared column-normalisation once.
    Node append order and duplicate-id remapping follow ``splice_csr`` one fold
    at a time, so even its edge-case index semantics remain unchanged.
    """
    extras = list(extras)
    if not extras:
        return list(base_ids), base_transition
    if len(extras) == 1:
        # There is no earlier fold whose next round would reset the base to
        # structure. Delegating preserves even a deliberately non-canonical
        # input CSR with duplicate stored coordinates byte-for-byte.
        extra_ids, extra_transition = extras[0]
        return splice_csr(
            list(base_ids), base_transition, list(extra_ids), extra_transition
        )

    combined_ids = list(base_ids)
    base_coo = base_transition.tocoo()
    prefix_src = [base_coo.col.astype(np.int64)]
    prefix_tgt = [base_coo.row.astype(np.int64)]
    last_src = np.empty(0, dtype=np.int64)
    last_tgt = np.empty(0, dtype=np.int64)

    for pos, (extra_ids, extra_transition) in enumerate(extras):
        # Keep the per-fold snapshot rule from splice_csr: duplicate new ids
        # inside one extra are appended exactly as before, and the dict below
        # resolves them to their final occurrence for that fold.
        base_set = set(combined_ids)
        combined_ids.extend(e for e in extra_ids if e not in base_set)
        index = {nid: i for i, nid in enumerate(combined_ids)}
        extra_coo = extra_transition.tocoo()
        if extra_coo.nnz:
            remap = np.fromiter(
                (index[nid] for nid in extra_ids),
                dtype=np.int64,
                count=len(extra_ids),
            )
            mapped_src = remap[extra_coo.col.astype(np.int64)]
            mapped_tgt = remap[extra_coo.row.astype(np.int64)]
        else:
            mapped_src = np.empty(0, dtype=np.int64)
            mapped_tgt = np.empty(0, dtype=np.int64)

        if pos == len(extras) - 1:
            last_src, last_tgt = mapped_src, mapped_tgt
        else:
            prefix_src.append(mapped_src)
            prefix_tgt.append(mapped_tgt)

    n = len(combined_ids)
    raw_prefix_src = np.concatenate(prefix_src)
    raw_prefix_tgt = np.concatenate(prefix_tgt)
    prefix = sp.csr_matrix(
        (
            np.ones(raw_prefix_src.size, dtype=np.float64),
            (raw_prefix_tgt, raw_prefix_src),
        ),
        shape=(n, n),
        dtype=np.float64,
    )
    # Every previous splice's next round sees structure only, so collapse all
    # prefix multiplicity before the final extra is added.
    if prefix.nnz:
        prefix.data.fill(1.0)
    prefix_coo = prefix.tocoo()
    src = np.concatenate([prefix_coo.col.astype(np.int64), last_src])
    tgt = np.concatenate([prefix_coo.row.astype(np.int64), last_tgt])
    weights = np.ones(src.size, dtype=np.float64)
    return combined_ids, _column_stochastic(n, src, tgt, weights)


def splice_active(
    base_ids: List[str],
    base_transition: "sp.csr_matrix",
    active_ids: List[str],
    active_edges: List[Tuple[str, str, float]],
) -> Tuple[List[str], "sp.csr_matrix"]:
    """把 active 的节点/边并入 base，按 id 合一（共享 canonical_id 自然合并）。
    返回 (combined_ids, combined_transition)。base 边从 base_transition 的稀疏结构还原
    （转移阵已列归一，权重信息有损，v1 用结构 + 权重 1.0 重算；等价测试证 top-k 稳健）。
    向量化：base 边直接复用 CSR/COO 结构数组（source=col, target=row，权重 1.0），
    active 边映射进 combined 索引空间后拼接，一次性建列随机阵，避免逐边 Python 循环。"""
    base_set = set(base_ids)
    combined_ids = list(base_ids) + [a for a in active_ids if a not in base_set]
    index = {nid: i for i, nid in enumerate(combined_ids)}
    n = len(combined_ids)
    coo = base_transition.tocoo()
    # base 结构还原:source=base_ids[col], target=base_ids[row],权重 1.0(原语义)。
    # combined 前 len(base_ids) 项与 base 对齐,索引可直接复用。
    base_src = coo.col.astype(np.int64)
    base_tgt = coo.row.astype(np.int64)
    base_w = np.ones(coo.nnz, dtype=np.float64)
    if active_edges:
        a_src = np.fromiter((index.get(s, -1) for s, _, _ in active_edges), dtype=np.int64, count=len(active_edges))
        a_tgt = np.fromiter((index.get(t, -1) for _, t, _ in active_edges), dtype=np.int64, count=len(active_edges))
        a_w = np.fromiter((float(w) for _, _, w in active_edges), dtype=np.float64, count=len(active_edges))
        keep = (a_src >= 0) & (a_tgt >= 0)
        src = np.concatenate([base_src, a_src[keep]])
        tgt = np.concatenate([base_tgt, a_tgt[keep]])
        w = np.concatenate([base_w, a_w[keep]])
    else:
        src, tgt, w = base_src, base_tgt, base_w
    return combined_ids, _column_stochastic(n, src, tgt, w)


def fold_arrays(base_node_ids, base_transition, base_idf, base_chunk_index,
                delta_node_ids, delta_edges, delta_chunk_ids, delta_idf_map):
    """把 delta splice 进 base 索引数组。base_node_ids 是 combined 的前缀,故 base
    的 chunk_index 位置不变;新节点追加在后。返回 (node_ids, transition, idf, chunk_index)。"""
    node_ids, transition = splice_active(list(base_node_ids), base_transition,
                                         list(delta_node_ids), list(delta_edges))
    base_n = len(base_node_ids)
    # idf:前缀复用 base.idf,新节点用 delta_idf_map(缺省 1.0)
    idf = np.ones(len(node_ids), dtype=np.float64)
    idf[:base_n] = np.asarray(base_idf, dtype=np.float64)[:base_n]
    for i in range(base_n, len(node_ids)):
        idf[i] = float(delta_idf_map.get(node_ids[i], 1.0))
    # chunk_index:base chunk 位置(前缀不变)+ 新 delta chunk 位置
    pos = {nid: i for i, nid in enumerate(node_ids)}
    chunk_index = list(np.asarray(base_chunk_index, dtype=np.int64)) + \
        [pos[c] for c in delta_chunk_ids if c in pos and pos[c] >= base_n]
    return node_ids, transition, idf, np.asarray(chunk_index, dtype=np.int32)


def add_items_to_ann(src_bin, dim, add_vectors, base_count):
    """load 现有 hnsw(base_count 个)→ resize 容纳 base_count+len(add)→ add_items
    (labels 从 base_count 递增)→ 返回 index(调用方 save)。add_vectors: (m,dim) float32。
    add_vectors 为空时纯 load(仍 resize 到 base_count 以保 max_elements 合法)。"""
    import hnswlib
    add = np.asarray(add_vectors, dtype=np.float32) if len(add_vectors) else None
    n_add = int(add.shape[0]) if add is not None else 0
    idx = hnswlib.Index(space="cosine", dim=dim)
    idx.load_index(src_bin, max_elements=base_count + n_add)
    if n_add:
        idx.add_items(add, np.arange(base_count, base_count + n_add))
    return idx


def viz_core(viz: dict, limit: int) -> dict:
    """折叠 viz 图取度数 top-N + 诱导边。viz: {viz_ids, viz_adj(csr), viz_deg, viz_types}。
    返回 {nodes:[{id,type,degree}], edges:[{source,target}], total_nodes, total_edges, truncated}。"""
    ids, adj, deg, types = viz["viz_ids"], viz["viz_adj"], viz["viz_deg"], viz["viz_types"]
    total = len(ids)
    n = total if (not limit or limit <= 0) else min(limit, total)
    top = np.argsort(-deg)[:n]
    keep = set(int(i) for i in top)
    nodes = [{"id": ids[i], "type": types[i], "degree": int(deg[i])} for i in top]
    coo = adj.tocoo()
    seen = set(); edges = []
    for r, c in zip(coo.row.tolist(), coo.col.tolist()):
        if r in keep and c in keep and r < c and (r, c) not in seen:
            seen.add((r, c)); edges.append({"source": ids[r], "target": ids[c]})
    return {"nodes": nodes, "edges": edges, "total_nodes": total,
            "total_edges": int(adj.nnz // 2), "truncated": n < total}


def viz_neighbors(viz: dict, node_id: str, cap: int = 50) -> dict:
    """折叠图中 node_id 的 1-hop 邻域(≤cap),返回 {nodes,edges}。未知 id → 空。

    ``viz["viz_node_index"]`` 是调用方缓存好的 id→下标映射(ScaleIndex/VizIndex
    的 ``viz_node_index()``)。缺这个键时自建——保持纯函数、向后兼容,只是每次
    调用重付一次 O(N)。"""
    ids, adj, deg, types = viz["viz_ids"], viz["viz_adj"], viz["viz_deg"], viz["viz_types"]
    index = viz.get("viz_node_index")
    if index is None:
        index = {nid: i for i, nid in enumerate(ids)}
    i = index.get(node_id)
    if i is None:
        return {"nodes": [], "edges": []}
    nbr = [int(j) for j in adj.getrow(i).indices][:max(0, cap)]
    keep = [i] + nbr
    nodes = [{"id": ids[j], "type": types[j], "degree": int(deg[j])} for j in keep]
    edges = [{"source": node_id, "target": ids[j]} for j in nbr]
    return {"nodes": nodes, "edges": edges}
