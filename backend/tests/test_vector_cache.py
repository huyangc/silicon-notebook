import threading
import time

import pytest

from app.services.vector_cache import VectorCache


def test_cache_hit_and_version_invalidation():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"e1": [1.0, 0.0]}

    c = VectorCache()
    v1 = c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    v2 = c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    assert v1 == v2 and calls["n"] == 1          # 同版本命中，不重复 loader

    c.get("nb1", version=("count=2", "ts=20"), loader=loader)
    assert calls["n"] == 2                        # 版本变 -> 重新 loader

    c.invalidate("nb1")
    c.get("nb1", version=("count=2", "ts=20"), loader=loader)
    assert calls["n"] == 3                        # 失效后重载


def test_single_flight_concurrent_miss():
    """N 个线程并发 get 同 key/同 version 的 miss，慢 loader 只应被调用一次，
    且所有线程拿到同一个对象（不是各自构建的副本）。"""
    calls = {"n": 0}
    calls_lock = threading.Lock()
    proceed = threading.Event()
    N = 8

    def loader():
        with calls_lock:
            calls["n"] += 1
        # 阻塞在这里，直到主线程放行，模拟分钟级 GB 构建，给其余线程
        # 充足时间在 get() 内部排队等待，而不是各自都跑进 loader。
        proceed.wait(timeout=5)
        return object()

    c = VectorCache()
    results = [None] * N
    errors = [None] * N

    def worker(i):
        try:
            results[i] = c.get("nb1", version="v1", loader=loader)
        except Exception as e:  # pragma: no cover - diagnostic aid only
            errors[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()

    # 等到至少一个线程进了 loader（说明其它线程此时应该在排队，而不是也在跑 loader）。
    started = time.monotonic()
    while calls["n"] < 1 and time.monotonic() - started < 5:
        time.sleep(0.01)
    # 再给一小段时间，让没有 single-flight 时会并发闯入 loader 的线程有机会闯入。
    time.sleep(0.2)
    assert calls["n"] == 1, "single-flight 失效：并发 miss 期间 loader 被调用不止一次"

    proceed.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert all(e is None for e in errors), errors
    assert calls["n"] == 1
    first = results[0]
    assert all(r is first for r in results)


def test_loader_exception_propagates_and_not_cached():
    """loader 抛异常时 get 必须把异常传播给调用方，且不能把失败结果缓存住；
    随后一次成功的 get 应正常缓存。并发等待方各自重试各自的 loader，不会
    卡死或拿到别人的异常。"""
    state = {"attempt": 0}

    def flaky_loader():
        state["attempt"] += 1
        if state["attempt"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    c = VectorCache()
    with pytest.raises(RuntimeError):
        c.get("nb1", version="v1", loader=flaky_loader)

    # 失败没有被缓存：store 里不应该有 nb1，且再次 get 会重新调用 loader。
    assert "nb1" not in c._store
    value = c.get("nb1", version="v1", loader=flaky_loader)
    assert value == {"ok": True}
    assert state["attempt"] == 2


def test_loader_exception_concurrent_waiters_each_retry():
    """并发场景下，率先跑 loader 的线程失败时，其余等待中的线程不会跟着拿
    同一个异常卡死，而是各自重试自己的 loader（最终都能成功返回)。"""
    calls = {"n": 0}
    calls_lock = threading.Lock()
    first_may_fail = threading.Event()
    N = 5

    def loader():
        with calls_lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 1:
            # 第一个进 loader 的线程先卡住，让其余线程有机会在 get() 内部排队，
            # 然后失败——校验等待方不会被这次失败一并拖死。
            first_may_fail.wait(timeout=5)
            raise RuntimeError("first attempt fails")
        return "ok"

    c = VectorCache()
    results = [None] * N
    errors = [None] * N

    def worker(i):
        try:
            results[i] = c.get("nb1", version="v1", loader=loader)
        except Exception as e:
            errors[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()

    started = time.monotonic()
    while calls["n"] < 1 and time.monotonic() - started < 5:
        time.sleep(0.01)
    time.sleep(0.2)
    first_may_fail.set()

    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    # 不应该有全局死锁；每个线程要么拿到 "ok"，要么拿到自己重试时的异常。
    for r, e in zip(results, errors):
        assert (r == "ok") or (e is not None)
    # 至少有线程最终拿到了成功结果（重试语义生效，不是永久失败传染）。
    assert "ok" in results


def test_lru_eviction():
    calls = {}

    def make_loader(key):
        def loader():
            calls[key] = calls.get(key, 0) + 1
            return f"value-{key}"
        return loader

    c = VectorCache(max_entries=3)
    c.get("a", version=1, loader=make_loader("a"))
    c.get("b", version=1, loader=make_loader("b"))
    c.get("c", version=1, loader=make_loader("c"))
    assert list(c._store.keys()) == ["a", "b", "c"]

    # 命中 a 刷新新鲜度：a 不再是最旧的，b 才是。
    c.get("a", version=1, loader=make_loader("a"))
    assert calls["a"] == 1  # 命中，不重新 loader

    # 插入第 4 个 key 触发淘汰：应淘汰最旧的 b（不是 a，因为 a 刚被访问过）。
    c.get("d", version=1, loader=make_loader("d"))
    assert len(c._store) == 3
    assert "b" not in c._store
    assert "a" in c._store and "c" in c._store and "d" in c._store

    # 被淘汰的 b 再次 get 时 loader 应再次被调用（重新构建）。
    c.get("b", version=1, loader=make_loader("b"))
    assert calls["b"] == 2


def test_version_replace_in_place():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return calls["n"]

    c = VectorCache(max_entries=3)
    c.get("a", version=1, loader=loader)
    c.get("b", version=1, loader=loader)
    assert len(c._store) == 2

    # 同 key 版本变化：原位替换，不新增条目。
    c.get("a", version=2, loader=loader)
    assert len(c._store) == 2
    assert calls["n"] == 3


def test_invalidate_under_concurrency():
    """invalidate 与并发 get/reload 交错时不应死锁，且 invalidate 后的
    get 会重新加载。"""
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def loader():
        with calls_lock:
            calls["n"] += 1
        time.sleep(0.01)
        return "v"

    c = VectorCache()
    c.get("nb1", version="v1", loader=loader)
    assert calls["n"] == 1

    stop = threading.Event()
    errors = []

    def getter_worker():
        while not stop.is_set():
            try:
                c.get("nb1", version="v1", loader=loader)
            except Exception as e:  # pragma: no cover - diagnostic aid
                errors.append(e)

    def invalidator_worker():
        for _ in range(20):
            c.invalidate("nb1")
            time.sleep(0.005)

    getters = [threading.Thread(target=getter_worker) for _ in range(4)]
    invalidator = threading.Thread(target=invalidator_worker)
    for t in getters:
        t.start()
    invalidator.start()
    invalidator.join(timeout=10)
    assert not invalidator.is_alive()
    stop.set()
    for t in getters:
        t.join(timeout=10)
        assert not t.is_alive()

    assert errors == []
    # 收尾后再 get 一次，确认缓存仍然可用（无死锁遗留的坏状态）。
    result = c.get("nb1", version="v1", loader=loader)
    assert result == "v"


def test_peek_true_when_warm_and_version_matches():
    """peek 命中当且仅当 key 已缓存且版本匹配——不触发 loader。"""
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"e1": [1.0]}

    c = VectorCache()
    assert c.peek("nb1", version=("count=1", "ts=10")) is False
    assert calls["n"] == 0, "peek 绝不应该触发 loader"

    c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    assert calls["n"] == 1
    assert c.peek("nb1", version=("count=1", "ts=10")) is True
    assert calls["n"] == 1, "peek 命中后仍不应触发 loader"


def test_peek_false_when_version_stale():
    """已缓存但版本不匹配(数据已变)→ peek 返回 False,不算暖。"""
    c = VectorCache()
    c.get("nb1", version=("count=1", "ts=10"), loader=lambda: {"e1": [1.0]})
    assert c.peek("nb1", version=("count=2", "ts=20")) is False


def test_peek_does_not_disturb_lru_order():
    """peek 是纯只读探测:不 move_to_end,不影响 LRU 淘汰序。"""
    c = VectorCache(max_entries=2)
    c.get("a", version=1, loader=lambda: "va")
    c.get("b", version=1, loader=lambda: "vb")
    assert list(c._store.keys()) == ["a", "b"]

    # peek "a"（最旧的）不应该把它刷新为最新。
    assert c.peek("a", version=1) is True
    assert list(c._store.keys()) == ["a", "b"]

    # 插入第三个 key 触发淘汰：仍应淘汰 "a"（peek 未刷新其新鲜度）。
    c.get("c", version=1, loader=lambda: "vc")
    assert "a" not in c._store
    assert "b" in c._store and "c" in c._store


def test_store_iteration_compat():
    """_store 要保持 dict-like，可直接迭代 key 做 endswith 过滤，兼容
    sqlite_repository._invalidate_unified_cache 的用法。"""
    c = VectorCache(max_entries=10)
    c.get("nb1:matrix:knowledge_embeddings", version=1, loader=lambda: {})
    c.get("nb1:kwtok", version=1, loader=lambda: {})
    c.get("nb2:fed_rxgraph", version=1, loader=lambda: {})
    c.get("nb3:fed_rxgraph", version=1, loader=lambda: {})

    fed_keys = [k for k in c._store if k.endswith(":fed_rxgraph")]
    assert set(fed_keys) == {"nb2:fed_rxgraph", "nb3:fed_rxgraph"}

    for key in fed_keys:
        c.invalidate(key)
    assert "nb2:fed_rxgraph" not in c._store
    assert "nb3:fed_rxgraph" not in c._store
    assert "nb1:matrix:knowledge_embeddings" in c._store
    assert "nb1:kwtok" in c._store


# ───────────────────── R2-4(热路径修复批 2 / 审计 ASK-3):分池与字节预算 ──
def test_key_family_derivation_folds_the_matrix_variants():
    from app.services.vector_cache import key_family

    assert key_family("nb-1:matrix:knowledge_embeddings") == "matrix"
    assert key_family("nb-1:matrix:relation_embeddings") == "matrix"
    assert key_family("nb-1:kwtok") == "kwtok"
    assert key_family("nb-2:fed_rxgraph") == "fed_rxgraph"
    assert key_family("bare-key-without-colon") == ""


_EMBEDDING_TABLES = (
    "knowledge_embeddings", "element_embeddings",
    "relation_embeddings", "chunk_embeddings",
)


def _warm_matrices(cache, notebooks):
    """按生产形状预热:**每库四条**(四张 embedding 表),不是每库两条。"""
    for notebook in notebooks:
        for table in _EMBEDDING_TABLES:
            cache.get(f"{notebook}:matrix:{table}", version=1, loader=lambda: {"m": 1})


def _resident_matrix_notebooks(cache, notebooks):
    return [
        notebook for notebook in notebooks
        if all(cache.peek(f"{notebook}:matrix:{t}", version=1)
               for t in _EMBEDDING_TABLES)
    ]


def test_families_do_not_evict_each_other():
    """R2-4 的核心断言,写成改造前/后的对照:同一份工作负载,旧形态(单一全局
    上限、无分池)会把活跃库的矩阵挤掉,分池之后它们留得住。

    现场(改造前):只有一道全进程 32 条的总上限,而单个大库自己就要占十几条
    (四个矩阵 + kwtok + ppr_graph + entchunk/elemchunk + clustermap +
    edge_centrality + …),两个活跃库即互相挤兑,被挤掉的恰是 GB 级冷载。

    夹具按**生产形状**:每库 4 条矩阵,库数(3)乘以 4 = 12 条,大于旧形态的
    总上限 8。族额度取 per_family_entries=2 → matrix 族 2 库 × 4 表 = 8 条,
    所以第 3 个库确实会在族内淘汰一个 —— 这里要钉的是「别的族灌多少都挤不掉
    matrix 族」,族内自己的上限由下一条用例管。

    **变异锚点**:去掉 ``_enforce_limits_locked`` 里的每族分池 → ``pooled``
    退化成 ``legacy`` 的行为,这条报红。
    """
    def workload(cache):
        _warm_matrices(cache, ("nb-a", "nb-b"))
        for i in range(20):
            cache.get(f"nb-flood-{i}:kwtok", version=1, loader=lambda: {"k": i})
            cache.get(f"nb-flood-{i}:ppr_graph", version=1, loader=lambda: {"g": i})

    legacy = VectorCache(max_entries=12, per_family_entries=0, max_bytes=0)
    pooled = VectorCache(max_entries=12, per_family_entries=2, max_bytes=0)
    workload(legacy)
    workload(pooled)

    assert _resident_matrix_notebooks(legacy, ("nb-a", "nb-b")) == [], (
        "改造前的形态本来就会把矩阵挤光——夹具若不再复现这一点,下面那条断言"
        "就失去了对照意义")
    assert _resident_matrix_notebooks(pooled, ("nb-a", "nb-b")) == ["nb-a", "nb-b"], (
        "分池之后,别的键族灌多少都不该挤掉矩阵族的常驻条目")


def test_three_participant_federation_keeps_every_matrix_warm():
    """P1-1(评审实测复现):族上限的**单位是笔记本**,不是条目。

    matrix 族每库占 4 条(四张 embedding 表)。若把 per_family_entries=8 当成
    8 个**条目**,这个族只装得下 2 个库 —— 一次 3 个参与库的联邦提问必然挤兑
    (而全局 128 的上限还空着一百多个槽),被逐出的矩阵会让
    ``_vector_matrix_warm`` 的 peek 判冷,``_retrieve_relations_scored`` 整段
    跳过关系语义打分:那是**问答质量**红线,不是命中率问题。

    这里用生产默认(128 / 8 / 关闭字节预算)跑一次 3 库联邦形状,断言三个库的
    四张矩阵全暖。

    **变异锚点**:把 ``_family_quota`` 改回 ``return self._per_family_entries``
    (即丢掉每库变体数换算)→ 只剩 8 条 = 2 个库,这条报红。
    """
    cache = VectorCache(max_entries=128, per_family_entries=8, max_bytes=0)
    participants = ("nb-active", "nb-base-1", "nb-base-2")
    _warm_matrices(cache, participants)
    # 联邦提问还会碰这些库的别的键族;它们不该反过来影响 matrix 族。
    for notebook in participants:
        for family in ("kwtok", "clustermap", "entchunk", "elemchunk"):
            cache.get(f"{notebook}:{family}", version=1, loader=lambda: {})

    assert _resident_matrix_notebooks(cache, participants) == list(participants)
    assert cache.stats()["entries_by_family"]["matrix"] == 12


def test_matrix_family_quota_holds_eight_notebooks_and_evicts_the_ninth():
    """族额度换算的正向断言:8 库 × 4 表 = 32 条常驻,第 9 个库进来才开始淘汰
    (而且淘汰的是族内最旧那一库的表,不是别的族)。"""
    cache = VectorCache(max_entries=1024, per_family_entries=8, max_bytes=0)
    notebooks = [f"nb-{i}" for i in range(8)]
    _warm_matrices(cache, notebooks)
    assert cache.stats()["entries_by_family"]["matrix"] == 32
    assert _resident_matrix_notebooks(cache, notebooks) == notebooks

    _warm_matrices(cache, ("nb-8",))
    assert cache.stats()["entries_by_family"]["matrix"] == 32
    assert _resident_matrix_notebooks(cache, notebooks) == notebooks[1:]
    assert _resident_matrix_notebooks(cache, ("nb-8",)) == ["nb-8"]


def test_family_cap_evicts_within_the_family_only():
    """族内仍然按 LRU 淘汰(族上限是真的上限,不是「无上限」),而且只淘汰本族。

    用 ``kwtok``(每库一条,单位换算系数 1)钉族内语义;``matrix`` 族的换算由
    ``test_matrix_family_quota_holds_eight_notebooks_and_evicts_the_ninth``
    单独钉。
    """
    c = VectorCache(max_entries=128, per_family_entries=2, max_bytes=0)
    c.get("nb-1:kwtok", version=1, loader=lambda: {})
    c.get("nb-2:kwtok", version=1, loader=lambda: {})
    c.get("nb-9:clustermap", version=1, loader=lambda: {})
    c.get("nb-3:kwtok", version=1, loader=lambda: {})       # 第 3 个 kwtok

    assert not c.peek("nb-1:kwtok", version=1)              # 族内最旧的被淘汰
    assert c.peek("nb-2:kwtok", version=1)
    assert c.peek("nb-3:kwtok", version=1)
    assert c.peek("nb-9:clustermap", version=1)             # 别的族毫发无伤
    assert c.stats()["evictions_by_family"] == {"kwtok": 1}


def test_byte_budget_evicts_by_global_lru_and_never_the_new_entry():
    """字节预算是最后一道兜底:超预算按全局 LRU 回收,但绝不回收刚写进去的那条
    (否则调用方立刻又得冷载同一个值,变成每次必冷)。

    **变异锚点**:把 ``_enforce_limits_locked`` 的字节那一段删掉 → 第一条断言
    (旧条目被回收)报红;把 ``keep`` 保护去掉 → 第二条(新条目仍在)报红。
    """
    from app.services.vector_cache import _CONTAINER_ITEM_BYTES

    per_entry = 1000 * _CONTAINER_ITEM_BYTES
    c = VectorCache(max_entries=128, per_family_entries=8,
                    max_bytes=int(per_entry * 2.5))
    for i in range(4):
        c.get(f"nb-{i}:kwtok", version=1,
              loader=lambda: {str(n): n for n in range(1000)})

    assert not c.peek("nb-0:kwtok", version=1)
    assert not c.peek("nb-1:kwtok", version=1)
    assert c.peek("nb-3:kwtok", version=1), "刚写入的条目绝不能被字节预算淘汰"
    assert c.stats()["estimated_bytes"] <= int(per_entry * 2.5)


def test_byte_budget_disabled_when_zero():
    c = VectorCache(max_entries=128, per_family_entries=8, max_bytes=0)
    for i in range(4):
        c.get(f"nb-{i}:kwtok", version=1,
              loader=lambda: {str(n): n for n in range(1000)})
    assert all(c.peek(f"nb-{i}:kwtok", version=1) for i in range(4))


def test_estimate_entry_bytes_sees_numpy_matrices_through_the_id_tuple():
    """``{nb}:matrix:*`` 的值是 ``(ids, ndarray)``:估算必须看穿这个二元组拿到
    矩阵的 nbytes,否则最大的那一族在预算里几乎不占分量。"""
    import numpy as np

    from app.services.vector_cache import (
        _CONTAINER_ITEM_BYTES,
        _NOMINAL_ENTRY_BYTES,
        estimate_entry_bytes,
    )

    matrix = np.zeros((512, 64), dtype=np.float32)
    ids = [f"ko-{i}" for i in range(512)]
    assert estimate_entry_bytes((ids, matrix)) >= matrix.nbytes
    # 大 dict(clustermap / edge_support 这类百万条映射)按条目数计价。
    assert estimate_entry_bytes({str(i): i for i in range(100)}) == (
        100 * _CONTAINER_ITEM_BYTES)
    # 估不出大小的类型退化成一个名义条目大小 → 预算在这类条目上等价于条目数上限。
    assert estimate_entry_bytes(object()) == _NOMINAL_ENTRY_BYTES


def test_estimate_entry_bytes_recurses_into_record_dicts_with_sparse_payloads():
    """P1-2(评审实测:低估 3593×):``{nb}:scale_combined`` 的值是一个 **5 个键
    的 record 型 dict**,里面装着 CSR 矩阵、百万级 list/dict/set 与一个 float64
    数组。只按「5 条 dict 条目」计价 = 恒记 1280 字节,16GiB 预算永远不会触发,
    而全局条目上限又被抬到了 128 —— 最坏常驻是改造前的三倍。

    估算必须(a)递归进小 record dict 的 values,(b)认得 scipy 稀疏矩阵的
    ``data/indices/indptr`` 三个 ndarray。

    **变异锚点**:去掉 dict 的小容器递归分支(退回一律 ``len × 单价``)→ 估算
    掉回 5×256=1280,这条报红;去掉 scipy 分支 → CSR 记成名义 1MiB,量级断言
    同样报红。
    """
    import numpy as np
    import scipy.sparse as sp

    from app.services.vector_cache import _CONTAINER_ITEM_BYTES, estimate_entry_bytes

    csr = sp.random(4000, 4000, density=0.01, format="csr", dtype=np.float32)
    real_sparse_bytes = csr.data.nbytes + csr.indices.nbytes + csr.indptr.nbytes
    idf = np.ones(4000, dtype=np.float64)
    combined = {
        "combined_ids": [f"ko-{i}" for i in range(4000)],
        "combined_A": csr,
        "combined_index": {f"ko-{i}": i for i in range(4000)},
        "combined_chunk_ids": {f"c-{i}" for i in range(4000)},
        "combined_idf": idf,
    }

    estimated = estimate_entry_bytes(combined)
    assert estimated >= real_sparse_bytes + idf.nbytes, (
        f"record dict 的稀疏矩阵与数组必须被看见:估 {estimated},"
        f"仅 CSR+idf 就有 {real_sparse_bytes + idf.nbytes}")
    # 而且必须远大于「按 5 条 dict 条目计价」的那个数量级(评审复现的低估点)。
    assert estimated > 100 * (5 * _CONTAINER_ITEM_BYTES)
    assert estimate_entry_bytes(csr) == real_sparse_bytes


def test_estimate_entry_bytes_sizes_rustworkx_graphs_by_nodes_and_edges():
    """``{nb}:ppr_graph`` / ``{active}:fed_rxgraph`` 的值里装着 rustworkx 图。
    它没有 ``nbytes``,落到名义 1MiB 就等于在预算里不存在;节点数 + 边数是它体量
    的真信号(每个节点还挂一个 payload dict)。

    **变异锚点**:去掉 ``num_nodes``/``num_edges`` 分支 → 估算掉回名义 1MiB,
    这条报红。
    """
    import rustworkx as rx

    from app.services.vector_cache import _NOMINAL_ENTRY_BYTES, estimate_entry_bytes

    graph = rx.PyDiGraph()
    indices = [graph.add_node({"kind": "ko"}) for _ in range(20000)]
    for a, b in zip(indices, indices[1:]):
        graph.add_edge(a, b, 1.0)

    estimated = estimate_entry_bytes(graph)
    assert estimated > _NOMINAL_ENTRY_BYTES, (
        f"两万节点的图不该被记成一个名义条目:估 {estimated}")
    # ppr_graph 的缓存值是 (graph, key_to_idx, chunk_idx_to_id) 三元组 —— 小元组
    # 递归必须把图那一项算进去。
    assert estimate_entry_bytes((graph, {}, {})) >= estimated


def test_stats_reports_hits_misses_and_family_occupancy():
    c = VectorCache(max_entries=128, per_family_entries=8, max_bytes=0)
    c.get("nb-1:kwtok", version=1, loader=lambda: {})     # miss
    c.get("nb-1:kwtok", version=1, loader=lambda: {})     # hit
    c.get("nb-1:matrix:a", version=1, loader=lambda: {})  # miss

    stats = c.stats()
    assert stats["hits"] == 1 and stats["misses"] == 2
    assert stats["hit_rate"] == pytest.approx(1 / 3)
    assert stats["entries_by_family"] == {"kwtok": 1, "matrix": 1}
    assert stats["entries"] == 2


def test_invalidate_releases_the_byte_accounting():
    """invalidate 必须把字节账一起销掉,否则预算会被幽灵条目慢慢吃光,最终把
    真正驻留的条目全部误逐(缓存自己把自己饿死)。"""
    c = VectorCache(max_entries=128, per_family_entries=8, max_bytes=1 << 30)
    c.get("nb-1:kwtok", version=1, loader=lambda: {str(n): n for n in range(1000)})
    assert c.stats()["estimated_bytes"] > 0
    c.invalidate("nb-1:kwtok")
    assert c.stats()["estimated_bytes"] == 0
