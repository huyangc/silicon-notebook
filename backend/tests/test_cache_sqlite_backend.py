"""SqliteCacheBackend 的行为测试。淘汰逻辑是自研缓存的核心风险面，逐项覆盖。"""
import sqlite3
import threading
import time

import pytest

from app.core.cache.sqlite_backend import SqliteCacheBackend

# 变量上限回归只需要真实越过连接自己的 SQLite limit，不需要把部署机的
# 32766 行全量复制进每一次测试。把测试连接主动压到一个小而非平凡的边界；若生产
# 代码退回 `IN (?, …)` 展开，LIMIT + 1 个 key 仍会真实触发同一个
# `sqlite3.OperationalError: too many SQL variables`。
TEST_VARIABLE_LIMIT = 128


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


def _poke_timestamps(cache, key, *, created_at=None, used_at=None):
    """直接改写 created_at/used_at，绕过 get()/put() 里两者耦合的语义。

    公开 API 做不到"已过期但 used_at 很新"或"未过期但 used_at 很旧"这类组合：
    put() 总是把两者一起刷到当前时间；get() 一旦发现已过期就直接删行，根本碰
    不到"顺手刷新 used_at"这条分支。构造这类状态组合只能直接写库，与
    `_used_at()` 读库是同一枚硬币的两面。
    """
    conn = sqlite3.connect(cache.path)
    try:
        if created_at is not None:
            conn.execute(
                "UPDATE cache SET created_at=? WHERE key=?", (created_at, key))
        if used_at is not None:
            conn.execute(
                "UPDATE cache SET used_at=? WHERE key=?", (used_at, key))
        conn.commit()
    finally:
        conn.close()


def _clamp_to_test_variable_limit(cache):
    """把复用连接压到小型测试水位，确定性复现 too many SQL variables。

    连接是 thread-local 复用的，压一次即对本线程后续所有操作生效——这也正是
    "每次操作新建连接"时做不到的事。

    这里验证的是「无界 key 集不能展开成 SQL placeholders」这一性质；SQLite 对
    128 和部署机 32766 使用同一 limit 机制，因此灌入三万多行只会增加 I/O，不会
    增加任何分支或断言强度。

    自检：如果连接不是被复用的（比如退回"每次操作新建连接"），下面这行 setlimit
    压的就是一条随手丢弃的连接，后续操作会在本机 250000 上限的新连接上跑，两条
    回归测试会假绿。用自检把这个隐式依赖钉成显式断言——一旦连接复用被破坏，
    这里立刻转红，而不是让后面的测试在错误的前提下"侥幸"通过。
    """
    cache._connect().setlimit(
        sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, TEST_VARIABLE_LIMIT)
    assert cache._connect().getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) == (
        TEST_VARIABLE_LIMIT
    ), "变量上限没有压在被复用的连接上——clamp 对后续操作不生效"
    placeholders = ",".join("?" for _ in range(TEST_VARIABLE_LIMIT + 1))
    with pytest.raises(sqlite3.OperationalError, match="too many SQL variables"):
        cache._connect().execute(
            f"SELECT 1 WHERE 1 IN ({placeholders})",
            [1] * (TEST_VARIABLE_LIMIT + 1),
        )


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


def test_expiry_sweep_predicate_is_index_backed(tmp_path):
    """_sweep_expired 的 created_at 条件必须走索引，不能全表扫。

    这条路径长在 put() 里、且在全局锁内：无索引时实测 63 MB 上限下最坏单次 put
    要 75 ms，线性外推到 2 GiB 默认上限约 2.5 s——一次 put 卡住所有并发读写。

    断言查询计划而不是断言索引名：后者只证明"有个叫这名字的索引"，建错列照样
    绿。EXPLAIN QUERY PLAN 直接回答"优化器真的用上了吗"。
    变异验证：删掉 sqlite_backend 建表脚本里的 idx_cache_created 后本测试转红。
    """
    c = _mk(tmp_path)
    c.put("k", "v")
    conn = sqlite3.connect(c.path)
    try:
        plans = [
            " ".join(str(col) for col in row)
            for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT COALESCE(SUM(size),0) FROM cache WHERE created_at < ?", (0,)
            ).fetchall()
        ]
    finally:
        conn.close()
    joined = " | ".join(plans)
    assert "USING INDEX" in joined and "created_at" in joined, (
        f"过期清扫在全表扫描 created_at，大库上每次 put 都会被它拖住：{joined}"
    )


def test_startup_recount_repairs_drifted_volume(tmp_path):
    """构造时校准 total_bytes：增量计量被进程崩溃打漂后，重启必须自愈。

    漂移只累积不自愈，虚高到越过 size_limit 就会触发"为满足幻影字节把健康条目
    全删光"，且 meta 仍虚高 → 缓存永久停在 0 条。此前 recount() 在 app/ 与
    scripts/ 下零调用方，文档声称的"漂移兜底"实际不存在。

    这里直接改写 meta 模拟崩溃遗留（公开 API 造不出这个状态），再新建一个指向
    同一文件的 backend——等价于重启。
    变异验证：删掉 __init__ 末尾的 self.recount() 后本测试转红。
    """
    c = _mk(tmp_path)
    c.put("k", "z" * 1000)
    real = c.volume()

    conn = sqlite3.connect(c.path)          # 模拟：写了 cache 行还没写 meta 就被 SIGKILL
    try:
        conn.execute("UPDATE meta SET v=? WHERE k='total_bytes'", (real * 50,))
        conn.commit()
    finally:
        conn.close()

    restarted = SqliteCacheBackend(str(tmp_path / "cache.db"))
    assert restarted.volume() == real, (
        f"重启没有校准漂移的计量：{restarted.volume()} != {real}"
    )


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
            # 裁剪只保证删到"刚好达标"——循环一旦发现 remaining <= target 就
            # 立即停手，最后一个被删条目可能把 remaining 从略高于 target 打到
            # 最多低于 target 一个条目的量，下界因此是 target - entry 而不是
            # target 本身。原断言 `after >= target` 能过纯粹因为
            # 200000/180000/20000 三者恰好整除、淘汰后总是精确落回 target，
            # entry 一旦改成不整除的数就会假红（对齐 :128 附近兄弟测试的写法）。
            floor = min((i + 1) * entry, target - entry)
            assert after >= floor, (
                f"第 {i + 1} 次 put 后裁剪过度：{after} < {floor}（上限 {limit}）"
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


def _run_hot_entry_scenario(cache):
    """灌 5 条"热"数据，再灌 15 条冷数据，期间持续访问 h0-h2。

    返回 (存活的热条目, 存活的冷条目)。LRU 看的是最后访问时间而非访问次数，所以
    必须在灌冷数据的**过程中**交错访问热条目——否则热条目的 used_at 早于所有冷
    条目，被淘汰是正确行为，用例等于什么都没测。
    """
    for i in range(5):
        cache.put(f"h{i}", "z" * 20_000)
    for i in range(5, 20):
        cache.put(f"c{i}", "z" * 20_000)
        for j in range(3):
            cache.get(f"h{j}")              # h0-h2 持续被访问
    hot = [f"h{j}" for j in range(3) if cache.get(f"h{j}") is not None]
    cold = [f"c{i}" for i in range(5, 20) if cache.get(f"c{i}") is not None]
    return hot, cold


def test_hot_entries_are_not_protected_at_the_production_refresh_window(tmp_path):
    """⚠ 如实记录一个已知权衡：**生产配置下没有热条目保护**。

    refresh_window 默认 3600s，而 make_cache_backend() 不暴露这个参数——生产上它
    恒为 3600。命中在这一小时窗口内不刷新 used_at（那正是粗粒度 LRU 换掉写放大的
    方式），于是 used_at ≈ 写入时间，淘汰**退化为近似 FIFO**：反复被访问的条目
    并不会因此延寿。

    实测（同场景只改这一个参数）：
        refresh_window=0        hot survivors = 3/3
        refresh_window=3600.0   hot survivors = 0/3     ← 生产默认值
    `0/3` 正是设计文档里用来否决 diskcache 的那个数字。

    **刻意不改默认值**：把 refresh_window 调小能换回热条目保护，但代价是把 cache
    hit 这条最热的路径重新变成写——正是粗粒度 LRU 要消除的写放大，在高命中率场景
    （reparse 重跑）下非常可观。诚实记录优于加复杂度：缓存条目丢了只是多打一次
    后端，写放大却是持续成本。

    本测试锁的就是这个真实行为。将来若真要做热条目保护（例如把 refresh_window
    接进工厂、或改用访问计数），本测试会转红——那时应当连同这段说明一起更新，而
    不是把断言改回 3/3 了事。
    """
    c = _mk(tmp_path, size_limit=200_000)     # 不传 refresh_window = 生产默认 3600
    assert c.refresh_window == 3600.0, "前提失效：本测试要跑的是生产默认值"

    hot, cold = _run_hot_entry_scenario(c)

    assert hot == [], (
        f"热条目在生产配置下竟然存活了（{hot}）——refresh_window 的语义变了？"
        "若确实做了热条目保护，请连同本测试的说明一并更新。"
    )
    # 近似 FIFO 的正面特征：活下来的是**最后写入**的那批冷条目，连续且靠后。
    assert cold, "全被淘汰了，场景没构造对"
    assert cold == sorted(cold, key=lambda k: int(k[1:])), "存活集合不连续"
    assert cold[-1] == "c19", f"最后写入的条目没活下来，不是 FIFO 形态：{cold}"
    assert "c5" not in cold, f"最早写入的冷条目还活着，不是 FIFO 形态：{cold}"


def test_hot_entries_survive_eviction_with_fine_grained_lru(tmp_path):
    """LRU 机器本身是对的：refresh_window=0（每次命中都刷 used_at）时热条目全活。

    ⚠ 这个配置**生产上到不了**——make_cache_backend() 不暴露 refresh_window。保留
    本测试是为了区分两件事：「淘汰逻辑写错了」与「粗粒度 LRU 是一个刻意的权衡」。
    上面那条锁生产行为，这条锁机器正确性；将来若把 refresh_window 接进工厂，这条
    就是它的验收测试。
    """
    c = _mk(tmp_path, size_limit=200_000, refresh_window=0)
    hot, _ = _run_hot_entry_scenario(c)
    assert len(hot) == 3, f"细粒度 LRU 下热条目仍被误删：只剩 {hot}"


def test_expired_entries_are_swept_before_lru_eviction(tmp_path):
    """过期清扫必须先于 LRU 淘汰执行——不只是"两者最终都会跑"。

    只看终态集合锁不住顺序：sweep 是无条件的，不管它在 while 循环之前还是之后
    调用，同一批过期行迟早都会被删掉；唯一的差异是 LRU 用来判断"是否需要淘汰、
    删多少"的 total 有没有先扣掉过期条目的字节数。若 LRU 先跑（用未扣减的 total
    做判断），它可能顺手把一个明明没过期、只是恰好排在最前面的冷条目也搭上，
    等 sweep 之后再跑时这个冷条目早就被冤枉删掉了——而这正是"顺序错了"的可观测
    后果。

    构造对照：
    - alive_but_cold：未过期，但 used_at 很旧——LRU 按 used_at 升序排最前，
      "先淘汰它"是错误决策的产物。
    - expired_but_hot：已过期（created_at 早于 ttl 截止线），但 used_at 很新——
      只有 sweep 会碰它，LRU 按 used_at 排序会把它当"最近使用"跳过。

    先清扫（生产代码顺序）：total 先被 expired_but_hot 的字节数打下去，直接达标，
    LRU 循环不必再动 alive_but_cold，它应该存活。
    后清扫（缺陷顺序）：LRU 用清扫前、虚高的 total 判断还需要腾多少空间，会把
    alive_but_cold 当垫背删掉；sweep 随后依旧会删掉 expired_but_hot——两条路径
    的存活集合因此不同，且都不是"len 从 4 变到几"这种容易被终态断言糊弄过去的
    信号。
    """
    limit = 100_000
    entry_cold = 20_000
    entry_hot = 60_000
    entry_fresh = 30_000
    c = _mk(tmp_path, size_limit=limit, ttl_seconds=100_000)  # 长 TTL，靠直接改写造过期，不靠 sleep
    now = time.time()

    c.put("alive_but_cold", "z" * entry_cold)
    _poke_timestamps(c, "alive_but_cold", used_at=now - 10_000)   # 未过期，但 used_at 最旧

    c.put("expired_but_hot", "z" * entry_hot)
    _poke_timestamps(
        c, "expired_but_hot",
        created_at=now - 200_000,   # 早于 ttl 截止线（now - 100_000），已过期
        used_at=now - 100,          # 但 used_at 比 alive_but_cold 新，LRU 眼里它更"热"
    )

    c.put("fresh", "z" * entry_fresh)   # 20K+60K+30K=110K > 100K，触发裁剪

    assert c.get("expired_but_hot") is None, "已过期条目必须被清扫，不因 used_at 新而幸免"
    assert c.get("alive_but_cold") == "z" * entry_cold, (
        "先清扫达标后不该殃及未过期的冷条目——若 LRU 先于 sweep 跑，"
        "会把它错当淘汰候选删掉"
    )
    assert c.get("fresh") == "z" * entry_fresh
    assert len(c) == 2, f"存活集合不对：还剩 {len(c)} 条"
    assert c.volume() == entry_cold + entry_fresh, (
        f"清扫/淘汰未归还容量计量：{c.volume()}"
    )


def test_evict_tag_survives_deploy_variable_limit(tmp_path):
    """按 tag 清空的条目数没有上限，不得展开成 IN (?,…)。

    tag 是模型名，"换模型后清掉它的缓存"正是 key 数最大的场景。先 SELECT key 再
    展开占位符会在部署机上抛 sqlite3.OperationalError: too many SQL variables。
    """
    n = TEST_VARIABLE_LIMIT + 1             # 必须真实越过连接变量上限
    c = _mk(tmp_path)
    _clamp_to_test_variable_limit(c)
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
    n = TEST_VARIABLE_LIMIT + 1
    c = _mk(tmp_path, ttl_seconds=100_000)
    _clamp_to_test_variable_limit(c)
    for i in range(n):
        c.put(f"k{i}", "v")                 # size_limit 极大，此时不触发裁剪
    # 用持久化状态构造过期集合，不让墙钟 sleep 成为语义前提。生产查询只关心
    # created_at 与 cutoff 的关系；一次 bulk UPDATE 也避免为造状态再做 n 次写事务。
    with c._connect() as db:
        db.execute(
            "UPDATE cache SET created_at=?",
            (time.time() - c.ttl_seconds - 1,),
        )
    # 129B 过期数据 + 100B fresh > 200B，而清扫后 100B <= 90% headroom；
    # 因而必经容量清扫路径，同时不会把作为对照的 fresh 再按 LRU 淘汰。
    c.size_limit = 200
    c.put("fresh", "v" * 100)

    assert len(c) == 1, f"过期清扫未删净：还剩 {len(c)} 条"
    assert c.get("fresh") == "v" * 100
    assert c.volume() == 100, f"清扫未归还容量计量：{c.volume()}"


def test_expired_entries_are_swept_on_writes_independent_of_capacity(tmp_path):
    """TTL 清理必须独立于容量压力。_evict_if_needed 没超 size_limit 就早返回（默认
    2 GiB 很难满），缓存远未满时从此再没被读到的过期条目会永不清理，活得远超
    ttl_seconds——弱化「删库后残留有上界」的隐私保证。put 的写路径按节流无条件清扫，
    不看是否超限。

    关键：断言只看 len()/volume()/stats() 直读，**绝不** get() 那两个过期 key——get 自己
    也查 TTL 并顺手删掉过期条目，会把「写路径没独立清扫」这个 bug 糊成假绿。

    变异验证：去掉 put 里的 self._maybe_sweep_expired(db, now) → 缓存远未满、
    _evict_if_needed 早返回 → 两条过期条目永不被写路径清扫 → len 停在 3 → 本测试转红。
    """
    c = _mk(tmp_path, size_limit=10**9, ttl_seconds=100_000, sweep_interval=0)
    now = time.time()
    c.put("stale1", "z" * 100)
    c.put("stale2", "z" * 100)
    # 造两条已过期条目（created_at 早于 ttl 截止线）。总量 300 字节 ≪ 10^9，绝不触发淘汰。
    _poke_timestamps(c, "stale1", created_at=now - 200_000)
    _poke_timestamps(c, "stale2", created_at=now - 200_000)

    c.put("fresh", "z" * 100)   # 一次写；容量远未满，_evict_if_needed 必早返回

    # 直读终态，绝不 get() 过期 key（那会掩盖 bug）。
    assert len(c) == 1, f"过期条目必须被写路径独立清扫；还剩 {len(c)} 条说明清扫没跑"
    assert c.volume() == 100, f"清扫未归还容量计量：{c.volume()}"
    assert c.stats()["entries"] == 1
    assert c.get("fresh") == "z" * 100, "未过期条目不受影响"


def test_write_path_sweep_is_throttled_not_every_put(tmp_path, monkeypatch):
    """写路径清扫必须节流，不能上每一次 put 的热路径。默认 sweep_interval=3600：进程
    第一次 put 清一次（_last_sweep 初值 0，「删库后残留有上界」也靠它兑现），此后一
    小时窗口内的 put 都跳过。size_limit=10^9 保证 _evict_if_needed 永不触发它内部那次
    清扫，于是计数只反映写路径节流清扫。

    变异验证：去掉节流、每次 put 都清 → 计数变 5；或去掉写路径清扫 → 计数变 0。
    两者都让本测试转红。"""
    c = _mk(tmp_path, size_limit=10**9, ttl_seconds=100_000)  # 不传 sweep_interval=生产默认 3600
    calls = []
    real = c._sweep_expired
    monkeypatch.setattr(c, "_sweep_expired", lambda db: calls.append(1) or real(db))
    for i in range(5):
        c.put(f"k{i}", "v")
    assert len(calls) == 1, (
        f"一小时窗口内多次 put 只清扫一次（首次），实际清扫了 {len(calls)} 次"
    )


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


def test_concurrent_put_same_key_keeps_volume_accounting_correct(tmp_path):
    """并发 put **同一个 key**：meta 的增量计量不能跟真实内容漂移。

    上面那条并发测试里各线程互不相交的 key，测不到锁真正要防的事：put() 是
    "先 SELECT prev size，再按 delta 写 meta"——两步之间没有原子性。若两个线程
    都在对方提交前读到同一个 prev（比如都读到 None），二者都会把自己的完整
    size 计入 meta，而不是净差值，total_bytes 就会比 SUM(size) 虚高，且这种
    漂移只会累积、不会自愈。

    对内容寻址缓存而言，"相同内容 → 相同 key → 并发重复 put" 不是边角情况而是
    最典型的场景（比如多个请求同时算出同一份待缓存内容）。self._lock 存在的
    意义就是让这组"读 prev、写行、写 meta"原子化；去掉它，这条测试必须转红。
    """
    c = _mk(tmp_path)
    n_threads, per_thread = 10, 30
    key = "shared-key"
    errors = []

    def worker(t):
        try:
            for i in range(per_thread):
                c.put(key, f"v{t}-{i}-" + "z" * (t * 7 + i), tag=f"m{t % 3}")
        except Exception as exc:            # 线程内异常必须带回主线程
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    assert not any(th.is_alive() for th in threads), "并发 worker 超时未结束"
    assert not errors, f"并发操作抛异常：{errors[:3]}"
    assert len(c) == 1, f"同一个 key 并发 put 后应只剩 1 行，实际 {len(c)} 行"
    assert c.volume() == c.recount(), (
        f"同 key 并发 put 导致 meta 增量计量漂移：volume()={c.volume()} "
        f"!= recount()={c.recount()}"
    )


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


def test_differential_against_reference_mapping(tmp_path):
    """用内存映射逐步对照 put/get/覆盖/落空语义。

    容量和淘汰由本文件其余边界测试覆盖；这个随机序列只验证基础 KV 状态机，
    因而不需要一个 CI 中未声明、长期导致整套门禁固定 skip 的外部 oracle。
    """
    import random

    rng = random.Random(42)
    no_evict = 10**9
    a = _mk(tmp_path, size_limit=no_evict, refresh_window=0)
    reference: dict[str, str] = {}
    mismatch = 0
    for _ in range(400):
        key = f"k{rng.randint(0, 25)}"
        if rng.random() < 0.4:
            value = "v" * rng.randint(5_000, 15_000)
            a.put(key, value)
            reference[key] = value
        else:
            ra, rb = a.get(key), reference.get(key)
            if (ra is None) != (rb is None) or (ra is not None and ra != rb):
                mismatch += 1
    assert mismatch == 0, f"与参照实现有 {mismatch} 处 KV 语义分歧"
