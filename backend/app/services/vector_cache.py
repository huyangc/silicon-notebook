"""进程内每-notebook 向量字典缓存（单用户单进程足够）。
版本键变化（向量行数/最新时间戳）即自动重载；删除时可显式 invalidate。

single-flight: 同 key 并发 miss 只有一个线程跑 loader()，其余线程排队等待
同一个结果，避免分钟级 / GB 级构建被并行触发 K 次（瞬时内存 ×K）。

LRU: OrderedDict 保序，命中 move_to_end 刷新新鲜度，超过 max_entries 淘汰
最旧条目——只影响性能（淘汰后下次重新构建），不影响正确性。

分池（热路径修复批 2 · R2-4，审计 ASK-3）: 三道上限，从紧到松：

1. **每配额桶**上限 ``per_family_entries``(默认 8,单位是**笔记本**)。桶名
   取 key 第一个冒号之后的全部后缀(``{nb}:matrix:relation_embeddings`` →
   ``matrix:relation_embeddings``;``{nb}:kwtok`` → ``kwtok``),所以每个桶每库
   恰好一条,「上限 = 同时保温几个笔记本」by construction 成立。这是本项真正
   要买的东西:改造前只有一道全进程 32 条的总上限,而单个大库自己就要占十几条
   (四个矩阵 + kwtok + ppr_graph + entchunk + elemchunk + clustermap +
   edge_centrality + …),**两个活跃库即互相挤兑**,被挤掉的恰好是冷载最贵的
   那些(GB 级矩阵、整表 dict)。分桶之后每张 embedding 表各自保住最近用过的
   8 个库,谁也挤不掉谁 —— 详见 ``quota_bucket`` 处「为什么不是按族 ×4」。
2. **全局条目**上限 ``max_entries``:族数乘以族上限的兜底,防止「族名意外
   爆炸」这类退化。它不再是常态下的约束条件(默认值随本项从 32 提到 128
   = 16 族 × 8),所以也不再是挤兑的来源。
3. **全局字节预算** ``max_bytes``:真正的内存兜底。条目字节按类型估算
   (numpy 数组用 ``nbytes``;dict/set/大序列按条目数 × 一个名义单价;估不出
   的类型按一个名义条目大小计)——**是数量级估计,不是精确会计**,它的职责是
   在常驻集合真的膨胀时开始按 LRU 回收,而不是给分配器做账。设 0 即关闭。
   一条**自己就超过整条预算**的条目不驻留:值照常返回给本次调用方,但不进
   缓存(下次同 key 再 ``get`` 会重新 load),并打一条 warning。否则它会永久
   驻留在一个明确说了「常驻不超过 N」的预算里,还会把整个缓存饿死——它自己
   占满预算,之后任何新条目一进来就把别人全逐光。

命中/未命中/淘汰计数见 ``stats()``,淘汰同时打日志(族上限 debug、总上限与
字节预算 info——后者说明进程真的到内存兜底了,值得在运维日志里看见)。

锁次序（避免死锁）: 任何时候都不持有 per-key 锁去申请全局锁；全局锁只保护
锁表 / _store 的结构性操作（增删条目、移动顺序），从不在全局锁内执行 loader。
拿全局锁 -> 释放全局锁 -> 拿 per-key 锁 -> 跑 loader（不持全局锁）-> 拿全局锁
写回结果，这样全局锁与 per-key 锁不会同时被同一线程持有去等待对方，不构成
环路等待。

锁表回收: per-key 锁条目在使用计数归零（没有线程在用它）时立即从锁表中
pop，不无界增长；仍在使用（in-flight load 或排队等待者）时保留。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Hashable, Tuple

_log = logging.getLogger("silicon_notebook.vector_cache")

# 字节估算的两个名义单价。都是**数量级**常数,不是精确会计——预算的职责是在
# 常驻集合真的膨胀时按 LRU 开始回收,不是替分配器记账。
# · _CONTAINER_ITEM_BYTES:一条 dict/set 条目的粗略常驻成本(哈希表槽 + 一个
#   小 tuple/str 键 + 一个值)。8.35M 行的 canonical-relations dict 因此估到
#   ~2GB,实测量级 3.6GB —— 同一个数量级,够用。
# · _NOMINAL_ENTRY_BYTES:估不出大小的类型(rustworkx 图对象等)按这个计,于是
#   这类条目上的字节预算自然退化成一道条目数上限,与条目上限的语义一致。
_CONTAINER_ITEM_BYTES = 256
_NOMINAL_ENTRY_BYTES = 1 << 20
# 元素级递归只在**小**容器上做,元组/列表与 dict 同规则:
# · ``{nb}:matrix:*`` 的值是 ``(ids, ndarray)`` 二元组;
# · ``{nb}:scale_combined`` 的值是 5 个键的 **record 型 dict**,里面装着 CSR
#   矩阵、百万级 list/dict/set 与一个 float64 数组。
# 不递归进去这两种就分别只看见「一个 2 元容器」和「一个 5 键 dict」——评审实测
# 后者被低估 3593×(恒记 1280 字节),16GiB 预算永远不会触发。
# 反过来,大容器(几百万条的 ids 列表)不做逐元素递归:那会把估算变成一次全量
# 遍历,按条目数 × 名义单价即可。
_RECURSE_MAX_ITEMS = 8
# 递归深度上限:record dict → 元组 → ndarray 这类嵌套两三层就到底了,给个上限
# 免得任何意料之外的自引用结构把估算变成无限递归。
_RECURSE_MAX_DEPTH = 3
# scipy 稀疏矩阵的常驻大头就是这几个 ndarray(CSR/CSC 是 data/indices/indptr,
# COO 是 data/row/col)。鸭子类型探测,不为估算把 scipy 拉进导入链。
_SPARSE_ARRAY_ATTRS = ("data", "indices", "indptr", "row", "col")


def estimate_entry_bytes(value: Any, _depth: int = 0) -> int:
    """一条缓存值的常驻字节**数量级**估计(见上面几个常数的说明)。"""
    try:
        import numpy as np
    except Exception:                                   # pragma: no cover
        np = None
    if np is not None and isinstance(value, np.ndarray):
        return int(value.nbytes)
    if isinstance(value, (str, bytes)):
        return len(value)
    if isinstance(value, (tuple, list)):
        if len(value) <= _RECURSE_MAX_ITEMS and _depth < _RECURSE_MAX_DEPTH:
            return sum(
                estimate_entry_bytes(item, _depth + 1) for item in value
            ) or _NOMINAL_ENTRY_BYTES
        return len(value) * _CONTAINER_ITEM_BYTES
    if isinstance(value, dict):
        # record 型小 dict(``scale_combined``)递归其 **values**;大 dict
        # (clustermap / edge_support 这类百万条映射)按条目数计价。
        if len(value) <= _RECURSE_MAX_ITEMS and _depth < _RECURSE_MAX_DEPTH:
            return sum(
                estimate_entry_bytes(item, _depth + 1) for item in value.values()
            ) or _NOMINAL_ENTRY_BYTES
        return len(value) * _CONTAINER_ITEM_BYTES
    if isinstance(value, (set, frozenset)):
        return len(value) * _CONTAINER_ITEM_BYTES
    if np is not None:
        # scipy 稀疏矩阵:三个(或两个)ndarray 就是它的全部常驻体量。
        sparse_bytes = sum(
            int(getattr(value, name).nbytes)
            for name in _SPARSE_ARRAY_ATTRS
            if isinstance(getattr(value, name, None), np.ndarray)
        )
        if sparse_bytes:
            return sparse_bytes
    # rustworkx 图(``ppr_graph`` / ``fed_rxgraph``):没有 nbytes,但节点数与
    # 边数是它体量的真信号(每个节点还挂着一个 payload dict)。
    node_count = getattr(value, "num_nodes", None)
    edge_count = getattr(value, "num_edges", None)
    if callable(node_count) and callable(edge_count):
        try:
            return (int(node_count()) + int(edge_count())) * _CONTAINER_ITEM_BYTES
        except Exception:                               # pragma: no cover
            pass
    return _NOMINAL_ENTRY_BYTES


# 配额桶:LRU 上限**按桶**执法,而每个桶每库恰好一条,所以「上限 = 同时保温几个
# 笔记本」这个不变量对每个桶都是 by construction 成立的。
#
# 桶名取 key 的第一个冒号之后的**全部**后缀,所以 ``{nb}:matrix:relation_embeddings``
# 自成一桶,与 ``matrix:chunk_embeddings`` 互不挤占;而 ``{nb}:kwtok`` 这类每库
# 一条的键族,桶 == 族。
#
# 为什么不是「matrix 归一族、额度 ×4」(codex PR#634 R2 P1 打掉的第一版):族淘汰
# 是**逐条**的,而 LRU 序里多库的四张矩阵是交错的,第 9 个库进场会从 4 个既有库
# 各剥一张矩阵 —— 8 库额度实际只剩 5 个全暖库,被剥的若是 relation 矩阵就直接触发
# ``_retrieve_relations_scored`` 的打分跳过。按变体分桶之后,每个桶各自保住它那张
# 表最近用过的 N 个库,``relation_embeddings`` 的 N 库常驻是独立成立的,不会被
# knowledge/element/chunk 三张表的活动稀释。
#
# 也没有选「以 notebook 为单位整组淘汰」那条:它要从 key 反解 nb 前缀、要在淘汰时
# 扫全族凑出「同一库的全部变体」,而按变体分桶用一行 partition 就得到同样的不变量,
# 且对「某条路径只加载了其中一张表」这种不齐的现实更稳。
def quota_bucket(key: str) -> str:
    """LRU 上限的执法桶名(见上)。``{nb}:matrix:relation_embeddings`` →
    ``matrix:relation_embeddings``;``{nb}:kwtok`` → ``kwtok``;没有冒号的裸键
    → ``""``。"""
    _head, separator, tail = key.partition(":")
    return tail if separator else ""


def key_family(key: str) -> str:
    """键族名:``{notebook}:{family}[:{variant}]`` 的中段。

    ``{nb}:matrix:knowledge_embeddings`` → ``matrix``(四张 embedding 表因此
    归一族);``{nb}:kwtok`` → ``kwtok``;没有冒号的 key(测试里的裸键)→ ``""``,
    统一落进一个匿名族。notebook id 形如 ``nb-<hex>``,不含冒号。
    """
    head, separator, tail = key.partition(":")
    if not separator:
        return ""
    family, _, _variant = tail.partition(":")
    return family


class _KeyLockEntry:
    __slots__ = ("lock", "refcount")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refcount = 0


class VectorCache:
    def __init__(
        self,
        max_entries: int = 128,
        *,
        per_family_entries: int = 8,
        max_bytes: int = 16 * 1024 ** 3,
    ) -> None:
        self._store: "OrderedDict[str, Tuple[Hashable, dict]]" = OrderedDict()
        self._max_entries = max_entries
        self._per_family_entries = per_family_entries
        self._max_bytes = max_bytes
        self._bytes: "Dict[str, int]" = {}
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions: "Dict[str, int]" = {}
        self._global_lock = threading.Lock()
        self._key_locks: Dict[str, _KeyLockEntry] = {}

    # ────────────────────────────────────────────────────── accounting ──
    def _drop_locked(self, key: str, reason: str) -> None:
        """调用方必须已持有 ``_global_lock``。"""
        self._store.pop(key, None)
        self._total_bytes -= self._bytes.pop(key, 0)
        family = key_family(key)
        self._evictions[family] = self._evictions.get(family, 0) + 1
        if reason == "family":
            _log.debug("vector_cache evict key=%s family=%s reason=%s", key, family, reason)
        else:
            _log.info(
                "vector_cache evict key=%s family=%s reason=%s entries=%d est_bytes=%d",
                key, family, reason, len(self._store), self._total_bytes,
            )

    def _enforce_limits_locked(self, keep: str) -> None:
        """三道上限,从紧到松(见模块 docstring)。``keep`` 是刚写入的 key,
        任何一道都不得把它淘汰掉——否则调用方立刻又得冷载一次同一个值。"""
        family = key_family(keep)

        if self._max_bytes > 0:
            oversized = self._bytes.get(keep, 0)
            if oversized > self._max_bytes:
                # 单条**自己**就超过整条预算 → 直接不驻留,而且必须排在**一切**
                # 淘汰之前(codex PR#634 R1 P1-2 提出、R2 P2-1 纠正位置)。
                #
                # 它一旦被留下,下一次插入的字节回收永远到不了预算之下,会把每一条
                # 比它旧的条目一路逐光(实测:3 条常驻小条目全毁,最后连它自己也
                # 没保住)。但反过来,先跑淘汰再拒绝它同样是错的:那是**为一个注定
                # 留不下的条目**清空常驻集合,白毁一遍 —— R1 的修法只把它排在字节
                # 循环之前,桶配额与全局条目两个循环仍排在它前面,于是「桶已满时
                # 插入一条超额条目」照样会先逐掉一条合法条目再拒绝新值。所以判断
                # 排在最前,一条都不逐。
                #
                # 值照常返回给调用方(本次调用不受影响),只是不进缓存:下次同
                # key 再 get 会重新 load。想让它常驻就该把
                # VECTOR_CACHE_MAX_BYTES 调大。
                self._store.pop(keep, None)
                self._total_bytes -= self._bytes.pop(keep, 0)
                self._evictions[family] = self._evictions.get(family, 0) + 1
                _log.warning(
                    "vector_cache entry not retained: key=%s family=%s "
                    "est_bytes=%d exceeds max_bytes=%d on its own; the value is "
                    "still returned to the caller but will be reloaded next time",
                    keep, family, oversized, self._max_bytes,
                )
                return

        if self._per_family_entries > 0:
            bucket = quota_bucket(keep)
            in_bucket = [k for k in self._store if quota_bucket(k) == bucket]
            while len(in_bucket) > self._per_family_entries:
                victim = in_bucket.pop(0)      # 全局 LRU 序 → 桶内最旧的在前
                if victim == keep:
                    continue
                self._drop_locked(victim, "family")
        if self._max_entries > 0:
            while len(self._store) > self._max_entries:
                victim = next(iter(self._store))
                if victim == keep:
                    break
                self._drop_locked(victim, "entries")
        if self._max_bytes > 0:
            while self._total_bytes > self._max_bytes and len(self._store) > 1:
                victim = next(iter(self._store))
                if victim == keep:
                    break
                self._drop_locked(victim, "bytes")

    def stats(self) -> dict:
        """命中/未命中/淘汰与每族驻留量的只读快照(运维观察 + 测试用)。"""
        with self._global_lock:
            per_family: Dict[str, int] = {}
            for key in self._store:
                family = key_family(key)
                per_family[family] = per_family.get(family, 0) + 1
            total = self._hits + self._misses
            return {
                "entries": len(self._store),
                "estimated_bytes": self._total_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total else 0.0,
                "entries_by_family": per_family,
                "evictions_by_family": dict(self._evictions),
                "max_entries": self._max_entries,
                "per_family_entries": self._per_family_entries,
                "max_bytes": self._max_bytes,
            }

    def _acquire_key_lock(self, key: str) -> _KeyLockEntry:
        with self._global_lock:
            entry = self._key_locks.get(key)
            if entry is None:
                entry = _KeyLockEntry()
                self._key_locks[key] = entry
            entry.refcount += 1
        return entry

    def _release_key_lock(self, key: str, entry: _KeyLockEntry) -> None:
        with self._global_lock:
            entry.refcount -= 1
            if entry.refcount <= 0 and self._key_locks.get(key) is entry:
                del self._key_locks[key]

    def get(self, key: str, version: Hashable, loader: Callable[[], dict]) -> dict:
        with self._global_lock:
            cached = self._store.get(key)
            if cached is not None and cached[0] == version:
                self._store.move_to_end(key)
                self._hits += 1
                return cached[1]
            self._misses += 1

        entry = self._acquire_key_lock(key)
        try:
            with entry.lock:
                # double-check：等待锁期间可能已被别的线程构建好了。
                # ⚠ 这条路径**不再**计 hit(codex #634 R5 P2):本次 get() 已经在
                # 上面记过一次 miss,再计 hit 会让并发 follower 把一次调用记成
                # 「一 miss + 一 hit」,stats() 的总数与命中率双双失真。每次公开
                # get() 恰好记一个结果——首检命中记 hit,其余一律记 miss(即便
                # follower 复用了 leader 的构建成果,对调用方它仍是一次未预热的
                # 访问,按 miss 口径如实反映冷载压力)。
                with self._global_lock:
                    cached = self._store.get(key)
                    if cached is not None and cached[0] == version:
                        self._store.move_to_end(key)
                        return cached[1]

                # loader 绝不在全局锁内运行。
                value = loader()
                # 字节估算同样在锁外做:它可能要走一遍 numpy 的 nbytes /
                # len(),没有理由占着全局锁。
                estimated = estimate_entry_bytes(value)

                with self._global_lock:
                    self._total_bytes -= self._bytes.pop(key, 0)
                    self._store[key] = (version, value)
                    self._store.move_to_end(key)
                    self._bytes[key] = estimated
                    self._total_bytes += estimated
                    self._enforce_limits_locked(keep=key)
                return value
        finally:
            self._release_key_lock(key, entry)

    def invalidate(self, key: str) -> None:
        with self._global_lock:
            self._store.pop(key, None)
            self._total_bytes -= self._bytes.pop(key, 0)

    def peek(self, key: str, version: Hashable) -> bool:
        """True 当且仅当 key 已缓存且版本匹配当前 version —— 不触发 loader、
        不做 single-flight、不 move_to_end(不影响 LRU 新鲜度/淘汰序,纯只读
        探测)。用于「加载前先问一句值不值得加载」的调用点(如大库场景下
        relation_embeddings 矩阵是否已经暖在缓存里,冷则宁可跳过语义打分也
        不要现懒加载 GB 级矩阵)。version 计算本身通常很便宜(COUNT/MAX 聚合
        查询),peek 省的是 loader() 本身(JSON 解析 + build_matrix)。"""
        with self._global_lock:
            cached = self._store.get(key)
            return cached is not None and cached[0] == version

    def keys(self) -> list:
        """Lock-guarded snapshot of the current cache keys — a read-only
        listing for key-family sweeps (e.g. evicting every '*:fed_rxgraph'
        entry on KG mutation, see RetrievalSnapshotCache.invalidate_kg).
        Returns a list copy, safe to iterate while other threads mutate; does
        not touch LRU freshness."""
        with self._global_lock:
            return list(self._store)


class LRUProcessCache:
    """Thread-safe, bounded dict-like LRU cache — the plain-dict-with-no-cap
    sibling of VectorCache for callers that don't need version-keying or
    single-flight (VectorCache's `get(key, version, loader)` contract), just
    `.get`/`[key] = value`/`.pop` semantics with an eviction cap.

    Used for _scale_idx_cache / _viz_idx_cache (sqlite_repository.py):
    each entry is a ScaleIndex/VizIndex — numpy arrays + a memoized hnsw
    handle, tens-of-MB to GB per notebook. Before this class they were plain
    dicts: every notebook ever touched stayed resident until process
    restart. Eviction here just drops the Python reference (GC frees the
    numpy arrays; hnswlib.Index has no explicit close()/context-manager API —
    dropping the last reference is the correct and only cleanup it needs).

    move_to_end on both read (`get`) and write (`__setitem__`) hits refresh
    recency; `popitem(last=False)` evicts the least-recently-used entry once
    size exceeds `max_entries` on insert. `pop` mirrors dict.pop's signature
    (used by cache-invalidation call sites, e.g. after an atomic on-disk
    index swap)."""

    def __init__(self, max_entries: int = 8) -> None:
        self._store: "OrderedDict[str, object]" = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get(self, key: str, default=None):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                return self._store[key]
            return default

    def __setitem__(self, key: str, value) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def __getitem__(self, key: str):
        with self._lock:
            value = self._store[key]  # raises KeyError, matches dict semantics
            self._store.move_to_end(key)
            return value

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._store

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def pop(self, key: str, default=None):
        with self._lock:
            return self._store.pop(key, default)


class LargeAwareLRUCache(LRUProcessCache):
    """LRUProcessCache that additionally caps the number of resident LARGE
    entries. A ScaleIndex for a multi-million-object base library holds ANN
    matrices tens of GB in size; the plain count cap (max_entries=8) lets 8 of
    them accumulate to hundreds of GB → OOM (memory-hardening PR-4, audit P1-4).

    ``is_large(value) -> bool`` classifies an entry; a large entry counts against
    ``max_large`` (a small number), a small entry only against ``max_entries``.
    On insert of a large entry, once the resident large count exceeds
    ``max_large`` the least-recently-used LARGE entry is evicted (the just-inserted
    one is most-recently-used, so it is kept). Small entries evict by
    ``max_entries`` exactly as before, so a burst of small libraries can never
    push a still-hot large index out ahead of an idle small one and vice-versa.
    Eviction just drops the reference (GC frees numpy/hnsw), so an evicted index
    cold-loads from disk on next access — a cache miss like any cold start.

    SCOPE (codex PR#359 P1-b, consciously deferred): this bounds the RESIDENT
    (cached, idle) large indexes. It does NOT bound the transient peak when more
    than max_large large notebooks are cold-loaded CONCURRENTLY — each in-flight
    request still holds the ScaleIndex it just loaded even after this evicts the
    cache's reference, so N simultaneous cold loads can momentarily hold N indexes.
    Bounding that peak needs a pre-load cross-notebook admission gate + refcounting
    over active index users, a separate concurrency change tracked as a follow-up.
    This class still strictly improves on the plain max_entries=8 cap (fewer
    resident large indexes, so a lower concurrent ceiling too)."""

    def __init__(self, max_entries: int, *, max_large: int, is_large) -> None:
        super().__init__(max_entries)
        self._max_large = max_large
        self._is_large = is_large

    def __setitem__(self, key: str, value) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            # Large cap FIRST (codex PR#359 r1 P2): a large insert over the large
            # cap must evict the oldest LARGE entry, not let the total cap below
            # first drop a small LRU entry — that would leave the cache one under
            # max_entries and cold-reload the small index for nothing. Only a
            # newly-inserted large entry can breach the large cap, so skip the O(n)
            # scan for small inserts.
            if self._is_large(value):
                large_keys = [k for k, v in self._store.items() if self._is_large(v)]
                while len(large_keys) > self._max_large:
                    del self._store[large_keys.pop(0)]  # LRU order → oldest large first
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
