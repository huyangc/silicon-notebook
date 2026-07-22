"""SqliteCacheBackend 的行为测试。淘汰逻辑是自研缓存的核心风险面，逐项覆盖。"""
import sqlite3
import threading
import time

import pytest

from app.core.cache.sqlite_backend import SqliteCacheBackend

# SQLite ≥ 3.32 的上游默认 SQLITE_LIMIT_VARIABLE_NUMBER，也是部署机
# （Ubuntu 24.04）的实际值。本机 conda 的 SQLite 编到 250000。
DEPLOY_VARIABLE_LIMIT = 32766


def _mk(tmp_path, **kw):
    kw.setdefault("size_limit", 10**9)
    kw.setdefault("ttl_seconds", 90 * 86400)
    return SqliteCacheBackend(str(tmp_path / "cache.db"), **kw)


def _used_at(cache, key):
    """直接读库取 used_at。

    生产类上不挂测试专用访问器——只为一条断言存在的 `_used_at()` 属于测试代码
    泄漏进生产。
    """
    conn = sqlite3.connect(cache.path)
    try:
        row = conn.execute(
            "SELECT used_at FROM cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _clamp_to_deploy_variable_limit(cache):
    """把复用连接的变量上限压到部署机水位，让本机也能复现 too many SQL variables。

    连接是 thread-local 复用的，压一次即对本线程后续所有操作生效——这也正是
    "每次操作新建连接"时做不到的事。
    """
    cache._connect().setlimit(
        sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, DEPLOY_VARIABLE_LIMIT)


def test_put_get_roundtrip(tmp_path):
    c = _mk(tmp_path)
    c.put("k", "v")
    assert c.get("k") == "v"


def test_ttl_expiry(tmp_path):
    c = _mk(tmp_path, ttl_seconds=0.5)
    c.put("k", "v")
    assert c.get("k") == "v"
    time.sleep(0.6)
    assert c.get("k") is None
    assert c.volume() == 0, "过期条目必须同时归还容量计量"


def test_evict_tag_only_removes_that_tag(tmp_path):
    c = _mk(tmp_path)
    c.put("a", "va", tag="modelA")
    c.put("b", "vb", tag="modelB")
    assert c.evict_tag("modelA") == 1
    assert c.get("a") is None
    assert c.get("b") == "vb"


def test_overwrite_updates_volume_by_delta(tmp_path):
    c = _mk(tmp_path)
    c.put("x", "1" * 5000)
    c.put("y", "2" * 5000)
    before = c.volume()
    c.put("x", "3" * 100)
    assert c.volume() == before - 5000 + 100


def test_recount_matches_incremental_volume(tmp_path):
    c = _mk(tmp_path)
    for i in range(10):
        c.put(f"k{i}", "z" * 500)
    assert c.recount() == c.volume()


def test_eviction_keeps_utilization_high(tmp_path):
    """每次裁剪之后容量利用率都必须回到 headroom 水位。

    ⚠ 断言点必须落在**每一次** put 之后，不能只看终态：清空是中间态现象，后续
    put 会把缓存重新填回来，终态断言因此形同虚设（实测"无条件删整批"的缺陷版本
    只有循环次数 N∈{33..37,44,45} 时才会被终态断言抓到）。
    """
    limit = 200_000
    entry = 20_000
    c = _mk(tmp_path, size_limit=limit)
    target = int(limit * c.headroom)
    evicted_once = False
    for i in range(40):
        before = c.volume()
        c.put(f"k{i}", "z" * entry)
        after = c.volume()
        assert after <= limit, f"第 {i + 1} 次 put 后超限：{after} > {limit}"
        if after < before + entry:          # 本次 put 触发了裁剪
            evicted_once = True
        if evicted_once:
            # 裁剪只删到刚好达标为止，利用率不得掉到 headroom 水位以下。
            assert after >= target, (
                f"第 {i + 1} 次 put 后裁剪过度：{after} < {target}（上限 {limit}）"
            )
    assert evicted_once, "用例没有触发裁剪，等于什么都没测"


def test_eviction_does_not_wipe_cache_when_batch_exceeds_entry_count(tmp_path):
    """候选批大于剩余条目数时不得清空缓存（原型第一版的真实缺陷）。

    ⚠ 同上：清空发生在第 6 次 put（此时 6 条 < 候选批 64 条），第 7、8 次 put 又
    把缓存填了回来。只断言终态时，"无条件删整批"的缺陷版本仅在 N∈{6,12} 才转红。
    """
    limit = 100_000
    entry = 20_000
    c = _mk(tmp_path, size_limit=limit)
    target = int(limit * c.headroom)
    for i in range(8):                      # 8 × 20KB = 160KB > 100KB，必触发裁剪
        c.put(f"k{i}", "z" * entry)
        assert len(c) > 0, f"第 {i + 1} 次 put 后裁剪把缓存清空了"
        assert c.volume() <= limit, f"第 {i + 1} 次 put 后超限：{c.volume()}"
        # 未触发裁剪时下界是累计写入量；触发后不得低于 headroom 水位减一条的大小。
        floor = min((i + 1) * entry, target - entry)
        assert c.volume() >= floor, (
            f"第 {i + 1} 次 put 后裁剪过度：{c.volume()} < {floor}"
        )


def test_hot_entries_survive_eviction(tmp_path):
    """热条目保护。

    注意：LRU 看的是最后访问时间而非访问次数。有效用例必须在灌入冷数据的过程中
    交错访问热条目，否则热条目的 used_at 仍旧早于所有冷条目，被淘汰是正确行为。
    """
    c = _mk(tmp_path, size_limit=200_000, refresh_window=0)
    for i in range(5):
        c.put(f"h{i}", "z" * 20_000)
    for i in range(5, 20):
        c.put(f"c{i}", "z" * 20_000)
        for j in range(3):
            c.get(f"h{j}")                  # h0-h2 持续被访问
    alive = [f"h{j}" for j in range(3) if c.get(f"h{j}") is not None]
    assert len(alive) == 3, f"热条目被误删：只剩 {alive}"


def test_expired_entries_are_swept_before_lru_eviction(tmp_path):
    """过期清扫分支必须真的被执行到——它需要"短 TTL"与"会越限"同时成立。

    只有短 TTL（size_limit 很大）会在 _evict_if_needed 的第一个 guard 就 return；
    只有越限（TTL 默认 90 天）则永远没有过期条目。两者都不碰这段代码。

    断言用可观测副作用证明清扫真的跑了：若跳过清扫直接走 LRU，4 条旧条目里只会
    被删掉 1 条（110KB → 90KB 即达标），len 是 4 而不是 1。
    """
    limit = 100_000
    c = _mk(tmp_path, size_limit=limit, ttl_seconds=0.3)
    for i in range(4):
        c.put(f"old{i}", "z" * 20_000)      # 80KB，未越限，不触发裁剪
    time.sleep(0.5)                         # 4 条全部过期
    c.put("fresh", "z" * 30_000)            # 110KB > 100KB，触发裁剪

    assert c.get("fresh") == "z" * 30_000, "过期清扫误删了未过期条目"
    assert len(c) == 1, f"过期条目未被清扫：还剩 {len(c)} 条"
    assert c.volume() == 30_000, f"清扫未归还容量计量：{c.volume()}"


def test_evict_tag_survives_deploy_variable_limit(tmp_path):
    """按 tag 清空的条目数没有上限，不得展开成 IN (?,…)。

    tag 是模型名，"换模型后清掉它的缓存"正是 key 数最大的场景。先 SELECT key 再
    展开占位符会在部署机上抛 sqlite3.OperationalError: too many SQL variables。
    """
    n = DEPLOY_VARIABLE_LIMIT + 1_000       # 必须越过变量上限
    c = _mk(tmp_path)
    _clamp_to_deploy_variable_limit(c)
    for i in range(n):
        c.put(f"k{i}", "v", tag="modelA")
    c.put("keep", "v", tag="modelB")

    assert c.evict_tag("modelA") == n
    assert len(c) == 1 and c.get("keep") == "v", "误伤了其他 tag"
    assert c.volume() == 1, f"未按删除量归还容量计量：{c.volume()}"


def test_expired_sweep_survives_deploy_variable_limit(tmp_path):
    """过期条目数同样没有上限，不得展开成 IN (?,…)。

    这条路径长在 put() 里：调用方按"缓存故障不影响主流程"用 except Exception 吞掉
    降级为 miss，所以一旦抛异常，缓存会**无声地永久停止写入**，没有任何信号。
    """
    n = DEPLOY_VARIABLE_LIMIT + 1_000
    c = _mk(tmp_path, ttl_seconds=0.5)
    _clamp_to_deploy_variable_limit(c)
    for i in range(n):
        c.put(f"k{i}", "v")                 # size_limit 极大，此时不触发裁剪
    time.sleep(0.6)                         # n 条全部过期
    c.size_limit = 1_000                    # 让下一次 put 必然越限
    c.put("fresh", "v" * 100)

    assert len(c) == 1, f"过期清扫未删净：还剩 {len(c)} 条"
    assert c.get("fresh") == "v" * 100
    assert c.volume() == 100, f"清扫未归还容量计量：{c.volume()}"


def test_concurrent_put_get_is_consistent(tmp_path):
    """多线程并发 put/get：连接改为线程内复用后必须不出错、不丢数据。

    thread-local 保证连接不跨线程（check_same_thread=True 会直接报错），
    self._lock 保证同一时刻只有一个写者、事务边界不交错。
    """
    c = _mk(tmp_path)
    n_threads, per_thread = 8, 250
    errors = []

    def worker(t):
        try:
            for i in range(per_thread):
                key, value = f"t{t}-{i}", f"v{t}-{i}"
                c.put(key, value, tag=f"m{t % 3}")
                assert c.get(key) == value
        except Exception as exc:            # 线程内异常必须带回主线程
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    assert not any(th.is_alive() for th in threads), "并发 worker 超时未结束"
    assert not errors, f"并发操作抛异常：{errors[:3]}"
    assert len(c) == n_threads * per_thread, f"并发写丢数据：只剩 {len(c)} 条"
    for t in range(n_threads):
        for i in range(per_thread):
            assert c.get(f"t{t}-{i}") == f"v{t}-{i}", f"t{t}-{i} 丢失或被写坏"
    assert c.volume() == c.recount(), "并发写后增量计量与实际不符"


def test_coarse_lru_avoids_write_amplification(tmp_path):
    """refresh_window 内的重复命中不应产生 used_at 写入。

    cache hit 是热路径，逐次 UPDATE 会把"读"变成"写"。这里通过观察 used_at
    是否变化来验证节流生效。
    """
    c = _mk(tmp_path, refresh_window=3600)
    c.put("k", "v")
    first = _used_at(c, "k")
    time.sleep(0.05)
    c.get("k")
    assert _used_at(c, "k") == first, "refresh_window 内不应刷新 used_at"


def test_stats_reports_entries_and_tags(tmp_path):
    c = _mk(tmp_path)
    c.put("a", "va", tag="m1")
    c.put("b", "vb", tag="m1")
    c.put("c", "vc", tag="m2")
    s = c.stats()
    assert s["entries"] == 3
    assert s["by_tag"] == {"m1": 2, "m2": 1}


def test_clear_empties_everything(tmp_path):
    c = _mk(tmp_path)
    c.put("a", "va")
    assert c.clear() == 1
    assert len(c) == 0 and c.volume() == 0


def test_differential_against_diskcache(tmp_path):
    """与 diskcache 对照验证 **KV 语义**：put/get/覆盖/落空判定。

    刻意用极大的 size_limit 让两边都不触发淘汰。**不要**把淘汰纳入对比：
    diskcache 的 `volume()` 计的是 SQLite 文件物理页数（含 schema/WAL/freelist），
    本实现计的是逻辑内容字节，两者量纲不同，无法互为 oracle；且 diskcache 默认
    `disk_min_file_size=32KB`，小于该值的条目走内联存储、`size` 记账为 0，其
    LRU-by-size 判据根本不被触发。实测把淘汰纳入对比会得到 23~37 处分歧且随 seed
    漂移（seed 42→37、7→29、99→23），而仅比 KV 语义时 4 个 seed 全部归零。

    淘汰行为由本文件其余测试独立覆盖——我们本就不认同 diskcache 的裁剪
    语义（`cull_limit` 按固定条数剔除而非删到刚好达标），它不该充当淘汰的标准答案。

    diskcache 仅作开发期参照实现，不是生产依赖——未安装即跳过。
    """
    diskcache = pytest.importorskip("diskcache")
    import random

    rng = random.Random(42)
    no_evict = 10**9
    a = _mk(tmp_path, size_limit=no_evict, refresh_window=0)
    b = diskcache.Cache(
        str(tmp_path / "dc"), size_limit=no_evict,
        eviction_policy="least-recently-used", cull_limit=1,
    )
    mismatch = 0
    for _ in range(400):
        key = f"k{rng.randint(0, 25)}"
        if rng.random() < 0.4:
            value = "v" * rng.randint(5_000, 15_000)
            a.put(key, value)
            b.set(key, value)
        else:
            ra, rb = a.get(key), b.get(key)
            if (ra is None) != (rb is None) or (ra is not None and ra != rb):
                mismatch += 1
    b.close()
    assert mismatch == 0, f"与参照实现有 {mismatch} 处 KV 语义分歧"
