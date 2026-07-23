from __future__ import annotations

import threading
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# 固定桶(毫秒上界)。用桶而非样本列表,使每 site 的内存 O(len(BUCKETS_MS)) 恒定
# —— 一次重聚类可能产生几十万次 record,留样本列表会把观测本身变成内存事故。
BUCKETS_MS: tuple[float, ...] = (
    1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0,
    500.0, 1000.0, 2000.0, 5000.0, 10000.0, float("inf"),
)

# 上报用的"最大有限桶":BUCKETS_MS 末尾的 inf 只是哨兵,不是可上报的数值。
_MAX_FINITE_BUCKET_MS: float = BUCKETS_MS[-2]


def _bucket_index(ms: float) -> int:
    idx = bisect_left(BUCKETS_MS, ms)
    return min(idx, len(BUCKETS_MS) - 1)


def _percentile(buckets: List[int], q: float) -> float:
    total = sum(buckets)
    if total == 0:
        return 0.0
    target = total * q
    seen = 0
    for i, c in enumerate(buckets):
        seen += c
        if seen >= target:
            # BUCKETS_MS[-1] 是 inf 哨兵,只用于让 _bucket_index 不越界,
            # 不能对外上报——json.dumps 会把它吐成非法的裸 token
            # `Infinity`,写进 events.jsonl 后外部 JSON 读者会解析失败。
            # 落在这个桶时按最大有限桶封顶上报,语义变成"≥10s"。
            return min(BUCKETS_MS[i], _MAX_FINITE_BUCKET_MS)
    return _MAX_FINITE_BUCKET_MS


@dataclass
class _Site:
    count: int = 0
    wait_max_ms: float = 0.0
    hold_max_ms: float = 0.0
    wait_buckets: List[int] = field(
        default_factory=lambda: [0] * len(BUCKETS_MS))
    hold_buckets: List[int] = field(
        default_factory=lambda: [0] * len(BUCKETS_MS))
    # -inf, not 0.0: time.monotonic()'s epoch is platform-defined (in practice
    # ~= process/system uptime), so a 0.0 sentinel compared as `now - warned_at
    # >= flush_interval_s` would wrongly suppress the first-ever warning
    # whenever flush_interval_s exceeds current uptime. -inf makes "never
    # warned" always satisfy the gate, regardless of the clock's absolute value.
    warned_at: float = float("-inf")


class WriteLockStats:
    """进程级写锁观测:等待者计数 + 每调用点的 wait/hold 分布。

    wait_ms = 排队拿锁的时长(= 用户感知的「页面卡住」);
    hold_ms = 持锁时长(= 谁害的)。两者必须分开,否则无法区分「我很慢」和
    「我被别人拖慢」。

    本类不认识 SQLite,也不认识 EventLogger —— 只吃数字、吐快照、按需回调
    sink。这样它能被单测直接驱动,不必拉起数据库。
    """

    def __init__(
        self,
        warn_ms: float = 200.0,
        flush_interval_s: float = 60.0,
        sink: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.warn_ms = float(warn_ms)
        self.flush_interval_s = float(flush_interval_s)
        self.sink = sink
        self._lock = threading.Lock()
        self._sites: Dict[str, _Site] = {}
        self._waiters = 0
        self._last_flush = time.monotonic()
        self._unresolved_sites = 0

    # ----------------------------------------------------------- waiters
    @property
    def waiters(self) -> int:
        with self._lock:
            return self._waiters

    def enter_wait(self) -> None:
        with self._lock:
            self._waiters += 1

    def exit_wait(self) -> None:
        with self._lock:
            if self._waiters > 0:
                self._waiters -= 1

    # ------------------------------------------------- degraded attribution
    @property
    def unresolved_sites(self) -> int:
        with self._lock:
            return self._unresolved_sites

    def mark_unresolved_site(self) -> None:
        """`database._caller_site()` 退化到 fallback(`"?"`)时调一次——帧结构
        意外(比预期浅,或包装层多到把 `_MAX_CALLER_WALK` 走穿)导致这次 write()
        测不出真实调用点。这种退化正常运行下应恒为 0,但没有任何默认信号会
        主动提示它发生过,所以显式计数——基准/监控都能看到「归因已经失真」,
        而不是把 `"?"` 这一行当成又一个正常调用点悄悄汇总掉。"""
        with self._lock:
            self._unresolved_sites += 1

    # ------------------------------------------------------------ record
    def record(self, site: str, wait_ms: float, hold_ms: float) -> None:
        now = time.monotonic()
        violation: Optional[dict] = None
        flush: Optional[dict] = None
        with self._lock:
            s = self._sites.get(site)
            if s is None:
                s = self._sites[site] = _Site()
            s.count += 1
            s.wait_max_ms = max(s.wait_max_ms, wait_ms)
            s.hold_max_ms = max(s.hold_max_ms, hold_ms)
            s.wait_buckets[_bucket_index(wait_ms)] += 1
            s.hold_buckets[_bucket_index(hold_ms)] += 1
            over = wait_ms >= self.warn_ms or hold_ms >= self.warn_ms
            # 每 site 每个刷新窗口最多报一条,避免一个病态循环刷爆 events.jsonl。
            if over and (now - s.warned_at) >= self.flush_interval_s:
                s.warned_at = now
                violation = {
                    "kind": "db_write_lock_slow",
                    "site": site,
                    "wait_ms": round(wait_ms, 2),
                    "hold_ms": round(hold_ms, 2),
                    "warn_ms": self.warn_ms,
                }
            if (now - self._last_flush) >= self.flush_interval_s:
                self._last_flush = now
                flush = self._snapshot_locked()
                flush["kind"] = "db_write_lock_stats"
        if self.sink is not None:
            # sink 只负责观测上报,不能反过来打断被观测的写入:接上真实事件
            # 日志后,磁盘满/日志轮转/坏 handler 都可能让它抛异常。这里只吞
            # Exception,不吞 BaseException——KeyboardInterrupt/SystemExit
            # 必须照常传播,否则一次写锁记录会被日志系统拖垮。
            if violation is not None:
                try:
                    self.sink(violation)
                except Exception:
                    pass
            if flush is not None:
                try:
                    self.sink(flush)
                except Exception:
                    pass

    # ---------------------------------------------------------- snapshot
    def _snapshot_locked(self) -> dict:
        return {
            "sites": {
                name: {
                    "count": s.count,
                    "wait_max_ms": round(s.wait_max_ms, 2),
                    "hold_max_ms": round(s.hold_max_ms, 2),
                    "wait_p99_ms": _percentile(s.wait_buckets, 0.99),
                    "hold_p99_ms": _percentile(s.hold_buckets, 0.99),
                }
                for name, s in self._sites.items()
            },
            "unresolved_sites": self._unresolved_sites,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def reset(self) -> None:
        with self._lock:
            self._sites.clear()
            self._waiters = 0
            self._last_flush = time.monotonic()
            self._unresolved_sites = 0
