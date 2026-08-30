# backend/tests/test_review_queue_memo.py
"""R3 T-A2 — ``ReviewQueueMemo`` 的单元契约(T-A3 v4 加入 ``total``,codex #638 R1)。

这里全部是**纯**测试:memo 不碰数据库,seq 与冷算都是注入的可调用对象,所以每条
性质都能被单独钉住,而不是靠一个端到端场景顺带覆盖。端到端(审核循环真的不再
重算、rejected 真的失效、add_relations 豁口真的被堵、items/total 同版本一致)在
``backend/tests/test_edge_review_queue.py``。

``compute``/``top()`` 的返回值形状是 ``(items, total)``:v4 把队列真实总量从
``knowledge_counts_cache`` 的第 5 个 module-global memo挪进了这里,与排名 items
绑在同一个条目、同一把锁、同一次 compute 上,消灭了 v3 两次独立读可能跨版本
不一致的自相矛盾响应。
"""
import concurrent.futures
import heapq
import threading
import time

import pytest

from app.services.review_queue_memo import (
    REVIEW_QUEUE_MEMO_ITEMS,
    ReviewQueueMemo,
)


def _items(*rel_ids, status: str = "pending") -> list:
    return [
        {"rel_id": rid, "review_status": status, "review_priority": float(i)}
        for i, rid in enumerate(rel_ids)
    ]


def _value(*rel_ids, status: str = "pending", total=None) -> tuple:
    """``compute()``的标准返回形状:``(items, total)``。``total`` 默认等于条目数
    (最常见情形——排名 items 就是全部非 rejected 关系);测试总量与条目数刻意不同
    的场景(比如「memo 命中时 total 不重算」)显式传入。"""
    items = _items(*rel_ids, status=status)
    return items, (len(items) if total is None else total)


# ── 读序契约(硬)──────────────────────────────────────────────────────────

def test_cold_compute_must_read_the_seq_before_the_data():
    """变异锚点:把 ``top`` 改成「先取数、后读 seq」必须让本条报红。

    场景是真实的交错:冷算跑到一半时另一个写者提交并 bump 了 seq。正确的读序
    (seq 先)给出「内容 ≥ 标签」——条目挂在旧标签上,下一次读因 seq 不等而重算,
    多付一次冷算,方向保守。反序给出「陈旧内容 + 新鲜标签」,下一次读命中它,而且
    ``carry`` 还会把这份陈旧内容一路续下去。
    """
    memo = ReviewQueueMemo()
    world = {"seq": 1, "computes": 0}

    def read_seq() -> int:
        return world["seq"]

    def compute() -> tuple:
        world["computes"] += 1
        # 另一个写者在取数期间提交:seq 前进,内容随之更新。
        world["seq"] += 1
        return _value(f"r-at-seq-{world['seq']}")

    memo.top("nb", 5, read_seq, compute)
    # 标签必须是取数**之前**读到的那个 seq,不是取数之后的。
    assert memo.cached_seq("nb") == 1
    # 行为面判据(与上面的标签断言互为佐证):世界已经走到 seq=2,这条 seq=1 的
    # 条目不得再被端上来。
    memo.top("nb", 5, read_seq, compute)
    assert world["computes"] == 2


# ── 命中 / 隔离 ────────────────────────────────────────────────────────────

def test_same_seq_is_served_without_recomputing():
    memo = ReviewQueueMemo()
    calls = {"n": 0}

    def compute() -> tuple:
        calls["n"] += 1
        return _value("r1", "r2", "r3")

    first, first_total = memo.top("nb", 3, lambda: 9, compute)
    second, second_total = memo.top("nb", 3, lambda: 9, compute)
    assert calls["n"] == 1
    assert first == second
    assert first_total == second_total == 3


def test_a_new_seq_recomputes():
    memo = ReviewQueueMemo()
    calls = {"n": 0}

    def compute() -> tuple:
        calls["n"] += 1
        return _value(f"r{calls['n']}")

    memo.top("nb", 3, lambda: 9, compute)
    memo.top("nb", 3, lambda: 10, compute)
    assert calls["n"] == 2
    assert memo.cached_seq("nb") == 10


def test_limit_slices_the_cached_ranking_but_not_the_total():
    """``limit`` 只截 items——``total`` 是队列真实总量,与 ``limit`` 无关,任何
    切片深度读到的都是同一个数。"""
    memo = ReviewQueueMemo()
    ranking = _items("r1", "r2", "r3", "r4")
    memo.top("nb", 4, lambda: 1, lambda: (ranking, 4))
    items, total = memo.top("nb", 2, lambda: 1, lambda: (None, None))
    assert [i["rel_id"] for i in items] == ["r1", "r2"]
    assert total == 4
    items0, total0 = memo.top("nb", 0, lambda: 1, lambda: (None, None))
    assert items0 == []
    assert total0 == 4


def test_returned_items_are_detached_from_the_memo():
    """返回值不与 memo 共享任何可变对象:调用方(乃至 API 序列化层)怎么改都
    碰不到缓存里的那份。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 1, lambda: _value("r1", "r2"))

    handed_out, _total = memo.top("nb", 5, lambda: 1, lambda: (None, None))
    handed_out[0]["review_status"] = "MUTATED"
    handed_out.append({"rel_id": "injected"})
    del handed_out[0]

    fresh, fresh_total = memo.top("nb", 5, lambda: 1, lambda: (None, None))
    assert [i["rel_id"] for i in fresh] == ["r1", "r2"]
    assert {i["review_status"] for i in fresh} == {"pending"}
    assert fresh_total == 2


def test_two_readers_do_not_share_the_returned_objects():
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 1, lambda: _value("r1"))
    a, _ = memo.top("nb", 5, lambda: 1, lambda: (None, None))
    b, _ = memo.top("nb", 5, lambda: 1, lambda: (None, None))
    assert a == b
    assert a is not b
    assert a[0] is not b[0]


# ── total 的独立性(T-A3 v4)───────────────────────────────────────────────

def test_total_is_not_recomputed_on_a_memo_hit():
    """T-A3 v4 主判据 / 变异锚点:memo 命中时 ``total`` 必须是**第一次** compute
    的那份,不能在命中路径上悄悄再跑一次独立的计数——这正是 v3 两次独立读会
    跨版本不一致的病灶。用一个「每次真冷算都吐出不同 total」的 world 模拟:如果
    实现被改回「items 走缓存、total 另外单独算一次」,``total_calls`` 会变成 2、
    两次读到的 total 也会不相等,本条立刻报红。"""
    memo = ReviewQueueMemo()
    world = {"total_calls": 0}

    def compute() -> tuple:
        world["total_calls"] += 1
        return _items("r1", "r2"), 100 + world["total_calls"]

    _items1, total1 = memo.top("nb", 5, lambda: 1, compute)
    _items2, total2 = memo.top("nb", 5, lambda: 1, compute)

    assert world["total_calls"] == 1, "命中不得触发第二次冷算"
    assert total1 == total2 == 101, (
        "第二次读到的 total 必须是缓存的那份(101),不是重新算出的 102——"
        "否则就是 v3 病灶重现:total 与 items 可能来自不同版本"
    )


def test_total_survives_a_limit_bypassing_read_at_the_same_seq():
    """同一个 seq 下,不同 ``limit`` 的多次读必须看到同一个 ``total``(它不随
    ``limit`` 变化,是队列的真实总量)。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 1, lambda: _value("r1", "r2", "r3", total=30))
    _, total_a = memo.top("nb", 1, lambda: 1, lambda: (None, None))
    _, total_b = memo.top("nb", 0, lambda: 1, lambda: (None, None))
    assert total_a == total_b == 30


# ── carry-forward ─────────────────────────────────────────────────────────

def test_carry_retags_and_rewrites_only_that_relation():
    memo = ReviewQueueMemo()
    before, before_total = memo.top("nb", 5, lambda: 7, lambda: _value("r1", "r2", "r3"))

    memo.carry("nb", 7, 8, "r2", "verified")

    assert memo.cached_seq("nb") == 8
    after, after_total = memo.top("nb", 5, lambda: 8, lambda: (None, None))
    assert [i["rel_id"] for i in after] == [i["rel_id"] for i in before]
    assert [i["review_status"] for i in after] == [
        "pending", "verified", "pending",
    ]
    # 排序输入(priority)一位不动 —— review_status 不参与打分。
    assert [i["review_priority"] for i in after] == [
        i["review_priority"] for i in before
    ]
    assert after_total == before_total


def test_carry_preserves_total_across_a_status_flip():
    """T-A3 v4 主判据:verified<->pending 翻转不改变非 rejected 集合的大小,
    所以 ``carry`` 必须原样保留 ``total``,只挪标签——绝不重算。变异锚点:如果
    ``carry`` 被改成「顺带 total-=0 之外的任何调整」或掉了 total 字段,本条报红。"""
    memo = ReviewQueueMemo()
    _items0, total0 = memo.top("nb", 5, lambda: 7, lambda: _value("r1", "r2", total=42))
    assert total0 == 42

    memo.carry("nb", 7, 8, "r1", "verified")

    # compute 若被调用会吐出一个明显不同的 total(999),用来证明这次读没有冷算。
    after_items, after_total = memo.top(
        "nb", 5, lambda: 8, lambda: (_items("should-not-be-used"), 999)
    )
    assert after_total == 42
    assert [i["review_status"] for i in after_items] == ["verified", "pending"]


def test_carry_retags_even_when_the_relation_is_not_in_the_top_m():
    """落榜的边被审核同样要 retag:它是否上榜只由 ``review_priority`` 决定,而
    priority 不含 ``review_status``,所以这次迁移对榜单毫无影响——丢掉条目只会
    白付一次冷算。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 7, lambda: _value("r1", "r2"))

    memo.carry("nb", 7, 8, "not-on-the-board", "verified")

    assert memo.cached_seq("nb") == 8
    after, after_total = memo.top("nb", 5, lambda: 8, lambda: (None, None))
    assert [i["rel_id"] for i in after] == ["r1", "r2"]
    assert {i["review_status"] for i in after} == {"pending"}
    assert after_total == 2


def test_carry_drops_the_entry_when_the_seq_does_not_match():
    """别的写者插了队(或这本从来没暖过这个版本):整条丢弃,不猜。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 7, lambda: _value("r1"))

    memo.carry("nb", 5, 9, "r1", "verified")   # expected 5 ≠ cached 7

    assert memo.cached_seq("nb") is None


def test_carry_on_a_cold_notebook_is_a_no_op():
    memo = ReviewQueueMemo()
    memo.carry("nb", 1, 2, "r1", "verified")
    assert memo.cached_seq("nb") is None


def test_carry_does_not_mutate_a_list_already_handed_out():
    """F1(codex 复审):CoW 的真正保证点在 memo **内部**,不在 ``top()`` 的返回值
    上——``top()`` 已经用 ``_slice_copy`` 把返回值和内部状态彻底隔离开,所以对着
    ``top()`` 的返回值搞「就地改」变异根本测不出任何东西(旧版本这条测试就是
    这样断言的,评审实测把 ``carry`` 改成原地 mutate、26 次全绿)。这里直接够
    进 ``_store`` 内部:carry 前后分别记下 items list 与被改那条 item 的
    ``id()``,两者都必须换成新对象,旧对象的内容也不能被那次 carry 动过。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 7, lambda: _value("r1", "r2"))

    before_list = memo._store["nb"][1]
    before_item = next(item for item in before_list if item["rel_id"] == "r1")

    memo.carry("nb", 7, 8, "r1", "verified")

    after_list = memo._store["nb"][1]
    after_item = next(item for item in after_list if item["rel_id"] == "r1")

    assert after_list is not before_list, (
        "carry must swap in a brand-new list, not mutate the old one in place"
    )
    assert after_item is not before_item, (
        "the touched item must be a new dict, not an in-place edit of the old one"
    )
    assert before_item["review_status"] == "pending", (
        "the old (pre-carry) dict must be left untouched by the mutation"
    )
    assert after_item["review_status"] == "verified"


# ── single-flight ─────────────────────────────────────────────────────────
#
# 这一节里凡是让某个线程执行 ``assert release.wait(10)`` 的用例,一律改用
# ``concurrent.futures`` 拿 ``Future``:线程内部的断言失败(或任何异常)必须能
# 传到主线程的断言里,而不是被 Python 的默认线程异常钩子悄悄吞掉、只在 pytest
# 里冒一条 ``PytestUnhandledThreadExceptionWarning`` ——旧写法下超时会被判定
# 为「线程整个失败了但主线程看不到」,测试反而可能因为主线程侧的弱断言继续
# 走通,是一种假绿(codex 复审实测复现)。

def test_concurrent_cold_misses_run_one_compute():
    memo = ReviewQueueMemo()
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def compute() -> tuple:
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return _value("r1")

    def reader() -> tuple:
        return memo.top("nb", 5, lambda: 4, compute)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        leader_future = pool.submit(reader)
        assert entered.wait(10)
        follower_futures = [pool.submit(reader) for _ in range(3)]
        release.set()
        out = [leader_future.result(timeout=10)] + [
            future.result(timeout=10) for future in follower_futures
        ]

    assert len(calls) == 1
    assert len(out) == 4
    assert all(result == (_items("r1"), 1) for result in out)
    # 等待者拿到的也必须是自己的那份拷贝,不是 leader 的那个 list。
    assert len({id(result[0]) for result in out}) == 4


def test_a_failed_cold_compute_propagates_to_the_waiter_without_a_serial_retry():
    """P2-4(codex 复审):single-flight 的 follower 必须直接继承 leader 的异常,
    不再各自转正、串行重跑同一次注定会失败的冷算(旧契约是 follower ``continue``
    重试,下面这条精确取代那份旧断言)。失败之后 memo 里什么都不缓存,包括
    ``cached_seq``。"""
    memo = ReviewQueueMemo()
    state = {"calls": 0}
    entered = threading.Event()
    release = threading.Event()

    def compute() -> tuple:
        state["calls"] += 1
        entered.set()
        assert release.wait(10)
        raise RuntimeError("cold ranking failed")

    def call() -> tuple:
        return memo.top("nb", 5, lambda: 3, compute)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        leader_future = pool.submit(call)
        assert entered.wait(10)
        waiter_future = pool.submit(call)
        release.set()
        with pytest.raises(RuntimeError, match="cold ranking failed"):
            leader_future.result(timeout=10)
        with pytest.raises(RuntimeError, match="cold ranking failed"):
            waiter_future.result(timeout=10)

    assert state["calls"] == 1, "the waiter must inherit the failure, not recompute"
    assert memo.cached_seq("nb") is None


def test_all_concurrent_followers_inherit_the_leaders_error_without_serial_recompute():
    """P2-4 加用例:N 个并发 follower 全部要快速拿到同一型异常。变异锚点——把
    follower 分支从「继承异常」改回旧的「continue 重试」——会让 ``calls`` 从 1
    涨回 N,墙钟也跟着从 1 倍冷算耗时涨到 N 倍(串行重跑同一次必败的冷算)。"""
    memo = ReviewQueueMemo()
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()
    compute_seconds = 0.05

    def compute() -> tuple:
        calls["n"] += 1
        entered.set()
        assert release.wait(10)
        time.sleep(compute_seconds)
        raise RuntimeError("cold ranking failed")

    def call() -> tuple:
        return memo.top("nb", 5, lambda: 9, compute)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        leader_future = pool.submit(call)
        assert entered.wait(10)
        follower_futures = [pool.submit(call) for _ in range(5)]
        release.set()
        started = time.monotonic()
        for future in [leader_future, *follower_futures]:
            with pytest.raises(RuntimeError, match="cold ranking failed"):
                future.result(timeout=10)
        elapsed = time.monotonic() - started

    assert calls["n"] == 1, "only the leader may run the cold ranking"
    assert elapsed < compute_seconds * 5, (
        "followers must not serially re-run the failing cold path"
    )

    # 失败后新请求仍从头当 leader(不做负缓存):同一个 seq,一个全新的、
    # 会成功的 compute 必须真的被调用并生效,不被上一次的失败钉死。
    fresh, fresh_total = memo.top("nb", 5, lambda: 9, lambda: _value("recovered"))
    assert (fresh, fresh_total) == (_items("recovered"), 1)
    assert calls["n"] == 1, "the fresh call must run its OWN compute, not the failing one"


def test_a_blocked_cold_compute_for_one_notebook_does_not_block_another():
    """P2-2(codex 复审):``top`` 绝不能在持有全局锁的情况下跑 ``compute()``——
    否则 nb-A 的一次慢冷算会把 nb-B 的读也一起拖住。变异锚点:把
    ``value = compute()`` 挪进 ``with self._lock:`` 块内,本条会因为 nb-B 的
    调用超时而报红(评审实测这个变异在旧的两条「靠线程超时」的用例上 20/20
    仍然绿)。用 ``try/finally`` 保证不管 nb-B 那一步是否超时,``release`` 都会
    被设置,阻塞的 nb-A leader 不会把整个测试进程吊死。"""
    memo = ReviewQueueMemo()
    entered = threading.Event()
    release = threading.Event()

    def blocked_compute() -> tuple:
        entered.set()
        assert release.wait(10)
        return _value("a1")

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        blocked_future = pool.submit(memo.top, "nb-A", 5, lambda: 1, blocked_compute)
        assert entered.wait(10)

        other_future = pool.submit(
            memo.top, "nb-B", 5, lambda: 1, lambda: _value("b1")
        )
        # nb-B 不需要等 nb-A 的 compute() 结束——给一个远小于 10s 门的短超时。
        other = other_future.result(timeout=1)
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert other == (_items("b1"), 1)
    assert blocked_future.result(timeout=10) == (_items("a1"), 1)


def test_writeback_from_a_slow_stale_leader_does_not_downgrade_a_fresher_entry():
    """P2-3(codex 复审):慢 leader 用旧 seq 算出来的值,不能在一个更新的 seq
    已经写回之后,姗姗来迟地把 store 覆盖回旧版本。变异锚点:去掉写回前的单调
    守卫(``existing is None or existing[0] <= seq``),本条报红。"""
    memo = ReviewQueueMemo()
    entered = threading.Event()
    release = threading.Event()

    def slow_compute() -> tuple:
        entered.set()
        assert release.wait(10)
        return _value("stale")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        slow_future = pool.submit(memo.top, "nb", 5, lambda: 1, slow_compute)
        assert entered.wait(10)

        # 一个更新的 seq 在慢 leader 仍卡在 compute() 里的时候完成并写回。
        fresh, fresh_total = memo.top("nb", 5, lambda: 2, lambda: _value("fresh"))
        assert (fresh, fresh_total) == (_items("fresh"), 1)

        release.set()
        stale_result = slow_future.result(timeout=10)

    # 慢 leader 仍然把它自己算出来的值交给它自己的调用方——写回守卫只挡
    # store,不改变 leader 对自己调用者的承诺。
    assert stale_result == (_items("stale"), 1)
    assert memo.cached_seq("nb") == 2
    after, after_total = memo.top("nb", 5, lambda: 2, lambda: (None, None))
    assert [item["rel_id"] for item in after] == ["fresh"]
    assert after_total == 1


# ── epoch / LRU ───────────────────────────────────────────────────────────

def test_invalidate_during_a_cold_compute_rejects_the_writeback():
    memo = ReviewQueueMemo()
    entered = threading.Event()
    release = threading.Event()

    def compute() -> tuple:
        entered.set()
        assert release.wait(10)
        return _value("r1")

    reader = threading.Thread(target=lambda: memo.top("nb", 5, lambda: 2, compute))
    reader.start()
    assert entered.wait(10)
    memo.invalidate("nb")
    release.set()
    reader.join(10)

    assert memo.cached_seq("nb") is None


def test_evicting_the_epoch_table_fails_closed():
    """被挤出 ``_epochs`` 的 notebook 的代次不能静默退回默认值 0。

    变异锚点:删掉 ``invalidate`` 淘汰分支里的 ``_global_epoch += 1``,本条报红——
    在途写回会把 invalidate **之前**的快照重新钉回来,而且没有后续 seq bump 兜底
    的边缘上可能无限期陈旧。
    """
    memo = ReviewQueueMemo(max_notebooks=2)
    entered = threading.Event()
    release = threading.Event()

    def compute() -> tuple:
        entered.set()
        assert release.wait(10)
        return _value("r1")

    reader = threading.Thread(target=lambda: memo.top("nb", 5, lambda: 2, compute))
    reader.start()
    assert entered.wait(10)
    memo.invalidate("nb")                 # nb 的代次前进
    memo.invalidate("a")
    memo.invalidate("b")                  # 越界:把 nb 的代次条目挤出 _epochs
    release.set()
    reader.join(10)

    assert memo.cached_seq("nb") is None


def test_invalidate_all_clears_every_notebook():
    memo = ReviewQueueMemo()
    memo.top("nb-a", 5, lambda: 1, lambda: _value("r1"))
    memo.top("nb-b", 5, lambda: 1, lambda: _value("r2"))
    memo.invalidate()
    assert memo.cached_seq("nb-a") is None
    assert memo.cached_seq("nb-b") is None


def test_per_notebook_invalidate_does_not_touch_a_sibling():
    memo = ReviewQueueMemo()
    memo.top("nb-a", 5, lambda: 1, lambda: _value("r1"))
    memo.top("nb-b", 5, lambda: 1, lambda: _value("r2"))
    memo.invalidate("nb-a")
    assert memo.cached_seq("nb-a") is None
    assert memo.cached_seq("nb-b") == 1


def test_the_store_is_bounded():
    memo = ReviewQueueMemo(max_notebooks=2)
    for index in range(5):
        memo.top(f"nb-{index}", 5, lambda: 1, lambda: _value("r1"))
    resident = [
        f"nb-{index}" for index in range(5)
        if memo.cached_seq(f"nb-{index}") is not None
    ]
    assert resident == ["nb-3", "nb-4"]


# ── 切片等价性 ────────────────────────────────────────────────────────────

def test_a_deep_nlargest_prefix_equals_a_shallow_nlargest():
    """memo 端出 ``nlargest(M)[:limit]``,冷路径以前算 ``nlargest(limit)`` —— 两者
    必须逐位一致,**包括并列**(nlargest 的装饰键带严格递减的计数器,并列按输入序
    解决,而输入序在前缀上不变)。这条是 ``review_queue`` 走 memo 的前提。"""
    priorities = [3.0, 1.0, 3.0, 2.0, 3.0, 1.0, 2.0, 0.0]
    items = [
        {"rel_id": f"r{index}", "review_priority": value}
        for index, value in enumerate(priorities)
    ]
    key = lambda item: item["review_priority"]      # noqa: E731
    deep = heapq.nlargest(len(items), items, key=key)
    for limit in range(len(items) + 2):
        assert deep[:limit] == heapq.nlargest(limit, items, key=key)


def test_the_memo_depth_is_the_advertised_constant():
    """P1(codex 复审):M 降到 200——够覆盖 ``review_queue`` 两处未传 limit 的
    默认值(service/facade 的 200、路由的 100),不需要 1000 那么深。"""
    assert REVIEW_QUEUE_MEMO_ITEMS == 200
