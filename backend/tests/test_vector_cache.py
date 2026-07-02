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
