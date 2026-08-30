# backend/tests/test_review_queue_memo.py
"""R3 T-A2 — ``ReviewQueueMemo`` 的单元契约。

这里全部是**纯**测试:memo 不碰数据库,seq 与冷算都是注入的可调用对象,所以每条
性质都能被单独钉住,而不是靠一个端到端场景顺带覆盖。端到端(审核循环真的不再
重算、rejected 真的失效、add_relations 豁口真的被堵)在
``backend/tests/test_edge_review_queue.py``。
"""
import heapq
import threading

from app.services.review_queue_memo import (
    REVIEW_QUEUE_MEMO_ITEMS,
    ReviewQueueMemo,
)


def _items(*rel_ids, status: str = "pending") -> list:
    return [
        {"rel_id": rid, "review_status": status, "review_priority": float(i)}
        for i, rid in enumerate(rel_ids)
    ]


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

    def compute() -> list:
        world["computes"] += 1
        # 另一个写者在取数期间提交:seq 前进,内容随之更新。
        world["seq"] += 1
        return _items(f"r-at-seq-{world['seq']}")

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

    def compute() -> list:
        calls["n"] += 1
        return _items("r1", "r2", "r3")

    first = memo.top("nb", 3, lambda: 9, compute)
    second = memo.top("nb", 3, lambda: 9, compute)
    assert calls["n"] == 1
    assert first == second


def test_a_new_seq_recomputes():
    memo = ReviewQueueMemo()
    calls = {"n": 0}

    def compute() -> list:
        calls["n"] += 1
        return _items(f"r{calls['n']}")

    memo.top("nb", 3, lambda: 9, compute)
    memo.top("nb", 3, lambda: 10, compute)
    assert calls["n"] == 2
    assert memo.cached_seq("nb") == 10


def test_limit_slices_the_cached_ranking():
    memo = ReviewQueueMemo()
    ranking = _items("r1", "r2", "r3", "r4")
    memo.top("nb", 4, lambda: 1, lambda: ranking)
    assert [i["rel_id"] for i in memo.top("nb", 2, lambda: 1, lambda: [])] == [
        "r1", "r2",
    ]
    assert memo.top("nb", 0, lambda: 1, lambda: []) == []


def test_returned_items_are_detached_from_the_memo():
    """返回值不与 memo 共享任何可变对象:调用方(乃至 API 序列化层)怎么改都
    碰不到缓存里的那份。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 1, lambda: _items("r1", "r2"))

    handed_out = memo.top("nb", 5, lambda: 1, lambda: [])
    handed_out[0]["review_status"] = "MUTATED"
    handed_out.append({"rel_id": "injected"})
    del handed_out[0]

    fresh = memo.top("nb", 5, lambda: 1, lambda: [])
    assert [i["rel_id"] for i in fresh] == ["r1", "r2"]
    assert {i["review_status"] for i in fresh} == {"pending"}


def test_two_readers_do_not_share_the_returned_objects():
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 1, lambda: _items("r1"))
    a = memo.top("nb", 5, lambda: 1, lambda: [])
    b = memo.top("nb", 5, lambda: 1, lambda: [])
    assert a == b
    assert a is not b
    assert a[0] is not b[0]


# ── carry-forward ─────────────────────────────────────────────────────────

def test_carry_retags_and_rewrites_only_that_relation():
    memo = ReviewQueueMemo()
    before = memo.top("nb", 5, lambda: 7, lambda: _items("r1", "r2", "r3"))

    memo.carry("nb", 7, 8, "r2", "verified")

    assert memo.cached_seq("nb") == 8
    after = memo.top("nb", 5, lambda: 8, lambda: [])
    assert [i["rel_id"] for i in after] == [i["rel_id"] for i in before]
    assert [i["review_status"] for i in after] == [
        "pending", "verified", "pending",
    ]
    # 排序输入(priority)一位不动 —— review_status 不参与打分。
    assert [i["review_priority"] for i in after] == [
        i["review_priority"] for i in before
    ]


def test_carry_retags_even_when_the_relation_is_not_in_the_top_m():
    """落榜的边被审核同样要 retag:它是否上榜只由 ``review_priority`` 决定,而
    priority 不含 ``review_status``,所以这次迁移对榜单毫无影响——丢掉条目只会
    白付一次冷算。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 7, lambda: _items("r1", "r2"))

    memo.carry("nb", 7, 8, "not-on-the-board", "verified")

    assert memo.cached_seq("nb") == 8
    after = memo.top("nb", 5, lambda: 8, lambda: [])
    assert [i["rel_id"] for i in after] == ["r1", "r2"]
    assert {i["review_status"] for i in after} == {"pending"}


def test_carry_drops_the_entry_when_the_seq_does_not_match():
    """别的写者插了队(或这本从来没暖过这个版本):整条丢弃,不猜。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 7, lambda: _items("r1"))

    memo.carry("nb", 5, 9, "r1", "verified")   # expected 5 ≠ cached 7

    assert memo.cached_seq("nb") is None


def test_carry_on_a_cold_notebook_is_a_no_op():
    memo = ReviewQueueMemo()
    memo.carry("nb", 1, 2, "r1", "verified")
    assert memo.cached_seq("nb") is None


def test_carry_does_not_mutate_a_list_already_handed_out():
    """copy-on-write:``top`` 在锁外做拷贝,靠的就是这条——carry 换新 list,不改
    任何还在别人手上的引用。"""
    memo = ReviewQueueMemo()
    memo.top("nb", 5, lambda: 7, lambda: _items("r1", "r2"))
    in_flight = memo.top("nb", 5, lambda: 7, lambda: [])

    memo.carry("nb", 7, 8, "r1", "verified")

    assert [i["review_status"] for i in in_flight] == ["pending", "pending"]


# ── single-flight ─────────────────────────────────────────────────────────

def test_concurrent_cold_misses_run_one_compute():
    memo = ReviewQueueMemo()
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def compute() -> list:
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return _items("r1")

    out: list = []

    def reader() -> None:
        out.append(memo.top("nb", 5, lambda: 4, compute))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads[0].start()
    assert entered.wait(10)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(10)

    assert len(calls) == 1
    assert len(out) == 4
    assert all(result == _items("r1") for result in out)
    # 等待者拿到的也必须是自己的那份拷贝,不是 leader 的那个 list。
    assert len({id(result) for result in out}) == 4


def test_a_failed_cold_compute_is_not_cached_and_wakes_the_waiters():
    memo = ReviewQueueMemo()
    state = {"calls": 0}
    entered = threading.Event()
    release = threading.Event()

    def compute() -> list:
        state["calls"] += 1
        if state["calls"] == 1:
            entered.set()
            assert release.wait(10)
            raise RuntimeError("cold ranking failed")
        return _items("r1")

    errors: list = []
    out: list = []

    def leader() -> None:
        try:
            memo.top("nb", 5, lambda: 3, compute)
        except RuntimeError as exc:
            errors.append(exc)

    leader_thread = threading.Thread(target=leader)
    leader_thread.start()
    assert entered.wait(10)
    waiter = threading.Thread(
        target=lambda: out.append(memo.top("nb", 5, lambda: 3, compute))
    )
    waiter.start()
    release.set()
    leader_thread.join(10)
    waiter.join(10)

    assert errors, "the failing leader must still raise to its own caller"
    assert out == [_items("r1")], "the waiter must retry, not inherit the failure"
    assert memo.cached_seq("nb") == 3


# ── epoch / LRU ───────────────────────────────────────────────────────────

def test_invalidate_during_a_cold_compute_rejects_the_writeback():
    memo = ReviewQueueMemo()
    entered = threading.Event()
    release = threading.Event()

    def compute() -> list:
        entered.set()
        assert release.wait(10)
        return _items("r1")

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

    def compute() -> list:
        entered.set()
        assert release.wait(10)
        return _items("r1")

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
    memo.top("nb-a", 5, lambda: 1, lambda: _items("r1"))
    memo.top("nb-b", 5, lambda: 1, lambda: _items("r2"))
    memo.invalidate()
    assert memo.cached_seq("nb-a") is None
    assert memo.cached_seq("nb-b") is None


def test_per_notebook_invalidate_does_not_touch_a_sibling():
    memo = ReviewQueueMemo()
    memo.top("nb-a", 5, lambda: 1, lambda: _items("r1"))
    memo.top("nb-b", 5, lambda: 1, lambda: _items("r2"))
    memo.invalidate("nb-a")
    assert memo.cached_seq("nb-a") is None
    assert memo.cached_seq("nb-b") == 1


def test_the_store_is_bounded():
    memo = ReviewQueueMemo(max_notebooks=2)
    for index in range(5):
        memo.top(f"nb-{index}", 5, lambda: 1, lambda: _items("r1"))
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
    assert REVIEW_QUEUE_MEMO_ITEMS == 1000
