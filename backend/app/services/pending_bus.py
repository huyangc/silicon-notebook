"""进程内待办事件总线(单进程部署)。

- REST/流式端点共用 recompute 计算 snapshot。
- job(线程)完成 → mark_dirty/emit → 经 loop.call_soon_threadsafe 投递给 SSE 连接。
- 无连接时 emit 的瞬时事件入 per-user 内存缓冲(TTL),新连接 flush 补发(跨会话)。
- **snapshot 的 DB 计算在调用线程预算,loop 侧只 fan-out,绝不在 loop 里查 DB。**
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional


class PendingBus:
    def __init__(self, now: Callable[[], float] = time.monotonic, ttl: float = 1800.0):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._conns: dict[str, set[asyncio.Queue]] = {}          # 仅 loop 线程访问
        self._buffer: dict[str, list[tuple[float, dict]]] = {}    # 锁保护
        self._lock = threading.Lock()
        # ``_conns`` 的**订阅数镜像**(锁保护),供 job 线程跨线程读,见
        # ``has_subscribers``。``_conns`` 本身是 loop 线程私有的免锁结构,job 线程不
        # 能读它;而 ``mark_dirty`` 必须在**花掉一次 DB 重算之前**就知道这个 user 有
        # 没有人在看。两者在 register/unregister 里成对维护(唯一的两处写入点),
        # 一致性由 test_pending_bus 的镜像不变量用例钉住。
        self._subscribers: dict[str, int] = {}
        self._recompute: Callable[[str], dict] = lambda uid: {"count": 0, "items": []}
        self._now = now
        self._ttl = ttl
        # 每 user 上次发布 snapshot 的时刻,供 mark_dirty_throttled 限频(锁保护)。
        self._last_publish: dict[str, float] = {}

    # ---- 装配 ----
    def set_recompute(self, fn: Callable[[str], dict]) -> None:
        self._recompute = fn

    def bind_loop(self) -> None:
        """在 async(端点)上下文调用,记录主事件循环。"""
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.get_running_loop()

    # ---- 连接管理(loop 线程) ----
    def register(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        conns = self._conns.setdefault(user_id, set())
        if q not in conns:
            conns.add(q)
            with self._lock:
                self._subscribers[user_id] = self._subscribers.get(user_id, 0) + 1
        return q

    def unregister(self, user_id: str, q: asyncio.Queue) -> None:
        conns = self._conns.get(user_id)
        if conns and q in conns:
            conns.discard(q)
            # 只在**真的移除了一条**时才减:重复 unregister(同一条连接的 finally
            # 被走两次)否则会把计数减到负,进而在还有别人连着时把整个 user 判成
            # 无人订阅、静默吞掉推送。
            with self._lock:
                left = self._subscribers.get(user_id, 0) - 1
                if left > 0:
                    self._subscribers[user_id] = left
                else:
                    self._subscribers.pop(user_id, None)
            if not conns:
                self._conns.pop(user_id, None)

    def has_subscribers(self, user_id: str) -> bool:
        """该 user 此刻是否有 ≥1 条实时连接。**任何线程可调**(锁保护的镜像读)。

        存在的理由:``_fanout_snapshot`` 早就会在 ``_conns`` 为空时丢弃快照,但那时
        ``_recompute`` 已经跑完了——一次 ``pending_actions`` 是 6 个投影查询外加逐库
        的索引状态,40 本库的用户就是上百次往返。铃铛没打开时这些往返的结果**全部
        被扔掉**。本方法把那个判断提前到花钱之前。

        ``bind_loop`` 是进程级且一旦绑定就不再解绑,所以 ``mark_dirty`` 里原有的
        "loop is None 就返回"只在**进程内从没有人打开过铃铛**时挡得住;有一个人开过
        之后,全体用户的每一次发布都会白算一遍。
        """
        with self._lock:
            return self._subscribers.get(user_id, 0) > 0

    def online_user_ids(self) -> set[str]:
        """当前持有 ≥1 条连接(实时流)的 user_id 集合 = 在线用户。
        仅事件循环线程调用:与 register/unregister 同线程,免锁快照。
        跨线程要问"某个 user 有没有人在看"用 ``has_subscribers``(读锁保护的镜像)。"""
        return set(self._conns.keys())

    def flush_buffer(self, user_id: str) -> list[dict]:
        """取出并清空该 user 的缓冲事件(新连接补发用)。"""
        with self._lock:
            entries = self._buffer.pop(user_id, [])
        fresh = self._within_ttl(entries)
        return [ev for _, ev in fresh]

    # ---- 触发(job 线程调) ----
    def mark_dirty(self, user_id: str) -> None:
        """待办可能变化 → 重算 snapshot 并推给该 user 所有连接(无连接则忽略)。

        两道闸都在 ``_recompute`` **之前**:
        - loop 未绑定 → 进程内从没人打开过铃铛;
        - 该 user 无订阅 → 算出来的快照本来也会被 ``_fanout_snapshot`` 丢掉。

        第二道是**投递等价**的:没有订阅时,加不加这道闸,任何连接收到的消息都完全
        一样,差别只是这次重算跑不跑。唯一的行为收窄在一个窄窗口里:恰好在本次
        ``has_subscribers`` 之后、``_fanout_snapshot`` 之前接上来的连接,以前能捡到
        这一帧,现在捡不到。这条是刻意 fail-open 的——SSE 端点在 ``register`` 之前
        自己就用线程池算了一帧完整快照(见 ``system_routes.me_pending_stream`` 第 2
        步),新连接拿到的初始状态本来就来自它自己那次 REST 计算,而不是靠捡这一帧;
        窗口本身也只有一次重算那么长,而每次提问必发起止两帧,后一帧一定落在窗口外。
        """
        loop = self._get_loop()
        if loop is None:
            return  # 无人连接;存量待办持久,重开会拉到
        if not self.has_subscribers(user_id):
            return  # 没人在看这个 user 的铃铛;重算的结果只会被 fan-out 丢掉
        with self._lock:
            self._last_publish[user_id] = self._now()
        data = self._recompute(user_id)  # 在调用线程(job 线程)算,不阻塞 loop
        loop.call_soon_threadsafe(self._fanout_snapshot, user_id, data)

    def mark_dirty_throttled(self, user_id: str, min_interval: float = 2.0) -> bool:
        """高频进度点用的限频版:距上次发布不足 min_interval 就跳过,返回是否真发。

        recompute 是在调用线程做的 DB 计算(见 mark_dirty),补抽/构建这类循环里
        逐项调用会把 job 线程拖成查询风暴,故进度点一律走本方法。**起始与完成是
        必达时刻,直接用 mark_dirty 不节流**——否则末次进度可能被窗口吞掉,留下
        永远停在 k/N 的假进行中项。

        无 SSE 连接时(loop is None,或该 user 无订阅)连时间戳都不记,直接 False:
        没人看时零开销,也不会在无人期间攒下一个"刚发过"的窗口把重连后的首次进度
        挡掉。"""
        if self._get_loop() is None or not self.has_subscribers(user_id):
            return False
        now = self._now()
        with self._lock:
            if now - self._last_publish.get(user_id, 0.0) < min_interval:
                return False
        self.mark_dirty(user_id)  # 由它统一记时间戳并 fan-out
        return True

    def emit(self, user_id: str, event: dict) -> None:
        """瞬时事件(index_done 等):有连接 fan-out,无连接入缓冲。"""
        loop = self._get_loop()
        if loop is None:
            self._buffer_event(user_id, event)
            return
        loop.call_soon_threadsafe(self._fanout_or_buffer_event, user_id, event)

    # ---- loop 线程内(串行,无并发) ----
    def _fanout_snapshot(self, user_id: str, data: dict) -> None:
        conns = self._conns.get(user_id)
        if not conns:
            return
        for q in conns:
            q.put_nowait({"kind": "snapshot", "data": data})

    def _fanout_or_buffer_event(self, user_id: str, event: dict) -> None:
        conns = self._conns.get(user_id)
        if not conns:
            self._buffer_event(user_id, event)
            return
        for q in conns:
            q.put_nowait({"kind": "event", **event})

    # ---- 内部 ----
    def _get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        with self._lock:
            return self._loop

    def _buffer_event(self, user_id: str, event: dict) -> None:
        with self._lock:
            lst = self._buffer.setdefault(user_id, [])
            lst.append((self._now(), event))
            self._buffer[user_id] = self._within_ttl(lst)

    def _within_ttl(self, entries: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
        cutoff = self._now() - self._ttl
        return [(t, ev) for (t, ev) in entries if t >= cutoff]

    def _buffered_count(self, user_id: str) -> int:  # 测试辅助
        with self._lock:
            return len(self._within_ttl(self._buffer.get(user_id, [])))

    def reset(self) -> None:  # 测试辅助
        """清空全部跨测试可泄漏状态(见 tests/conftest.py 的 autouse 重置)。

        本类是进程级单例:缓冲事件、已关闭的 loop、连接集合若留给下一个测试,
        就会串味。生产期不该调用——进程内只有一条总线,清空即丢事件。
        """
        with self._lock:
            self._buffer.clear()
            self._last_publish.clear()
            self._subscribers.clear()   # 与 _conns 成对清空,否则镜像会残留计数
            self._loop = None
        self._conns.clear()


pending_bus = PendingBus()


def publish_snapshot(user_id: Optional[str], *, throttled: bool = False) -> None:
    """job 线程里发布一次待办快照的房内统一入口。

    长任务(KG 构建/索引构建/论文元数据补抽)在**登记完自身进行中状态之后**调用
    本函数,「进行中」项才会立刻出现在已连接的铃铛里;此前只有 job 结束时的
    notify_pending 会刷新,于是运行期间的项要等用户重连/刷新才看得到。

    - user_id 为空 → no-op(解析不出归属就别猜)。
    - throttled=True 供进度点用(限频,见 PendingBus.mark_dirty_throttled);
      起始与完成不节流。
    - fail-open:推送失败绝不冒泡打断正在跑的 job。
    - **该 user 的铃铛没打开时零开销**:``mark_dirty*`` 先查 ``has_subscribers``,
      不做那次 ``pending_actions`` 重算(见 ``PendingBus.mark_dirty``)。
    """
    if not user_id:
        return
    try:
        if throttled:
            pending_bus.mark_dirty_throttled(user_id)
        else:
            pending_bus.mark_dirty(user_id)
    except Exception:  # noqa: BLE001 - notification is fail-open
        pass
