import asyncio
from app.services.pending_bus import PendingBus


def test_emit_buffers_when_no_connection():
    bus = PendingBus()
    bus.set_recompute(lambda uid: {"count": 0, "items": []})
    # 从没有连接过 → loop 未绑定 → event 入缓冲
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb1"})
    assert bus._buffered_count("u1") == 1


def test_ttl_prunes_old_events():
    clock = {"t": 1000.0}
    bus = PendingBus(now=lambda: clock["t"], ttl=100.0)
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb1"})
    clock["t"] = 1050.0
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb2"})
    clock["t"] = 1130.0  # nb1(age 130s)已超 TTL;nb2(age 80s)未超
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb3"})
    assert bus._buffered_count("u1") == 2  # nb2, nb3 存活;nb1 被 prune


def test_fanout_and_flush_via_loop():
    async def scenario():
        bus = PendingBus()
        bus.set_recompute(lambda uid: {"count": 1, "items": [{"type": "x"}]})
        bus.bind_loop()  # 在 async 上下文绑定当前 loop

        # 无连接时 emit → 缓冲(bind_loop 后 emit 经 call_soon_threadsafe 投递,
        # 需让出一次 loop 才会真正写入 _buffer,呼应生产环境里重连必经的网络往返)
        bus.emit("u1", {"event": "index_done", "notebook_id": "nbA"})
        await asyncio.sleep(0)

        # 建立连接 → flush 缓冲补发 + 能收到后续 fan-out
        q = bus.register("u1")
        try:
            drained = bus.flush_buffer("u1")
            assert [d["notebook_id"] for d in drained] == ["nbA"]
            assert bus._buffered_count("u1") == 0

            # mark_dirty(有连接) → snapshot 进队列
            bus.mark_dirty("u1")
            await asyncio.sleep(0.01)
            msg = q.get_nowait()
            assert msg["kind"] == "snapshot"
            assert msg["data"]["count"] == 1

            # emit(有连接) → event 进队列(不缓冲)
            bus.emit("u1", {"event": "index_done", "notebook_id": "nbB"})
            await asyncio.sleep(0.01)
            msg2 = q.get_nowait()
            assert msg2["kind"] == "event"
            assert msg2["notebook_id"] == "nbB"
        finally:
            bus.unregister("u1", q)

    asyncio.run(scenario())


def test_submit_notify_pending_marks_dirty(monkeypatch):
    import app.services.background_jobs as bj
    called = []
    monkeypatch.setattr(bj.pending_bus, "mark_dirty", lambda uid: called.append(uid))
    # 让 job 线程内解析到某 uid
    monkeypatch.setattr(bj, "_resolve_job_user", lambda: "user-a")
    done = __import__("threading").Event()
    t = bj.submit(lambda: done.set(), name="t", notify_pending=True)
    done.wait(2.0)
    t.join(2.0)
    assert called == ["user-a"]


def test_submit_without_notify_does_not_mark(monkeypatch):
    import app.services.background_jobs as bj
    called = []
    monkeypatch.setattr(bj.pending_bus, "mark_dirty", lambda uid: called.append(uid))
    done = __import__("threading").Event()
    t = bj.submit(lambda: done.set(), name="t")  # notify_pending 默认 False
    done.wait(2.0)
    t.join(2.0)
    assert called == []


def test_online_user_ids_reflects_register_unregister():
    bus = PendingBus()
    assert bus.online_user_ids() == set()
    q1 = bus.register("user-aaa")
    bus.register("user-bbb")
    assert bus.online_user_ids() == {"user-aaa", "user-bbb"}
    # 同一 user 第二条连接不改变成员集合
    q1b = bus.register("user-aaa")
    assert bus.online_user_ids() == {"user-aaa", "user-bbb"}
    # 断开 user-aaa 的一条,仍在线(还有一条)
    bus.unregister("user-aaa", q1)
    assert "user-aaa" in bus.online_user_ids()
    # 断开最后一条 → 下线
    bus.unregister("user-aaa", q1b)
    assert bus.online_user_ids() == {"user-bbb"}
    # 返回的是快照,修改它不影响内部状态
    snap = bus.online_user_ids()
    snap.add("user-zzz")
    assert "user-zzz" not in bus.online_user_ids()


def test_mark_dirty_throttled_limits_publish_rate():
    """进度点限频:窗口内只发一次,窗口过后放行。

    recompute 是调用线程里的 DB 计算,补抽/构建循环里逐项 mark_dirty 会把 job
    线程拖成查询风暴——进度点必须走限频版。用假时钟精确控制窗口,不睡真实时间。
    """
    clock = {"t": 1000.0}

    async def scenario():
        bus = PendingBus(now=lambda: clock["t"])
        calls: list = []
        bus.set_recompute(lambda uid: calls.append(uid) or {"count": 0, "items": []})
        bus.bind_loop()
        q = bus.register("u1")
        try:
            assert bus.mark_dirty_throttled("u1", 2.0) is True    # 首次放行
            assert bus.mark_dirty_throttled("u1", 2.0) is False   # 窗口内挡掉
            clock["t"] = 1001.9
            assert bus.mark_dirty_throttled("u1", 2.0) is False   # 仍在窗口内
            clock["t"] = 1002.1
            assert bus.mark_dirty_throttled("u1", 2.0) is True    # 窗口过后放行
            assert len(calls) == 2, "被挡掉的调用不该触发 recompute"

            # 不节流的 mark_dirty 会重置窗口(完成时刻必达,且不该被随后的
            # 进度点紧接着再推一次)
            clock["t"] = 1002.2
            bus.mark_dirty("u1")
            assert bus.mark_dirty_throttled("u1", 2.0) is False
        finally:
            bus.unregister("u1", q)

    asyncio.run(scenario())


def test_mark_dirty_throttled_is_free_without_connections():
    """无 SSE 连接(loop 未绑定)时直接 False,且不记时间戳。

    没人看时零开销是一等约束;更关键的是别在无人期间攒下一个"刚发过"的窗口,
    否则用户重连后的第一次进度会被那个陈旧窗口挡掉。
    """
    clock = {"t": 500.0}
    bus = PendingBus(now=lambda: clock["t"])
    calls: list = []
    bus.set_recompute(lambda uid: calls.append(uid) or {"count": 0, "items": []})

    assert bus.mark_dirty_throttled("u1") is False
    assert calls == []
    assert bus._last_publish == {}, "无连接时不该留下时间戳"


def test_publish_snapshot_is_fail_open_and_skips_empty_uid():
    """房内统一入口:uid 为空 → no-op;底层抛错 → 吞掉(绝不打断正在跑的 job)。"""
    from app.services import pending_bus as pb_module

    calls: list = []
    original = pb_module.pending_bus.mark_dirty
    try:
        pb_module.pending_bus.mark_dirty = lambda uid: calls.append(uid)
        pb_module.publish_snapshot(None)
        pb_module.publish_snapshot("")
        assert calls == [], "解析不出归属就别猜"
        pb_module.publish_snapshot("u1")
        assert calls == ["u1"]

        def _boom(uid):
            raise RuntimeError("bus down")

        pb_module.pending_bus.mark_dirty = _boom
        pb_module.publish_snapshot("u1")  # 不得抛
    finally:
        pb_module.pending_bus.mark_dirty = original
