# SQLite 写锁瘦身 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让批量写（重聚类、关系层、mention 桥、backfill）运行期间，交互写不再出现可感卡顿，并用实测指标而非代码审查来验收。

**Architecture:** 先给 `SqliteDatabase.write()` 装上 wait/hold 仪器（新模块 `write_lock_stats.py` 持有直方图与等待者计数，`database.py` 只负责采点），再建一个规模可调的合成基准把部署量级的分布量出来；然后做唯一一处零语义风险的改造（scratch 暂存分批提交）并引入 `bulk_write()` 让路原语；最后用基准数字决定要不要动三处整表替换。

**Tech Stack:** Python 3.13、SQLite（WAL）、pytest、FastAPI（仅间接）、纯 stdlib 的 `scripts/diag.py`。

## Global Constraints

- 设计来源：`docs/superpowers/specs/2026-07-21-sqlite-write-lock-slimming-design.md`。任务与 spec 冲突时以 spec 为准。
- **不改算法语义**：聚类、关系层、mention 桥的计算结果必须逐位不变；本计划只改写入的事务切分与调度。
- **不改 `concept_clusters` 的任何读者。**
- SQL 只能出现在 `backend/app/repositories/sqlite/` 层；服务层不得出现裸 SQL（`callers_static` 约束）。
- 新环境变量必须用 `Field(..., validation_alias="NAME")`；`pydantic-settings` v2 下 `Field(env=)` 无效，`Settings(field=...)` kwarg 也无效（构造要用 alias 名）。
- `scripts/diag.py` 及其离线子命令：**纯 stdlib、零 `app` 依赖、只读、脱敏**。
- 新增用户可运行 CLI 必须在同一 PR 内写进 `README.md` 与 `README_zh.md`。
- 测试文件末尾追加内容（如 `slow` 档）不移动既有行号；若确实移动了行号，重跑 `test_repository_surface_manifest` 的行号基线。
- 每个任务结束跑一次 `cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests -x -q -m "not slow"` 确认无回归；全部完成后跑 `scripts/check.sh`。
- 测试命令一律带 `SILICON_NOTEBOOK_ENV_FILE=""`，避免继承仓库根 `.env` 打到真实模型服务、污染 `llm_cache.db`。
- 每条 `git commit` 消息末尾追加一行（计划里的 commit 步骤为求简洁省略了它，实际提交必须带）：
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- 本仓库 PR 走 **Rebase and merge**，特性分支必须保持线性：rebase 到 `master`，不要把 `master` merge 进来。

---

### Task 1: `WriteLockStats` —— 纯数据结构的观测容器

**Files:**
- Create: `backend/app/repositories/sqlite/write_lock_stats.py`
- Test: `backend/tests/test_write_lock_stats.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）
- Produces:
  - `class WriteLockStats(warn_ms: float = 200.0, flush_interval_s: float = 60.0, sink: Callable[[dict], None] | None = None)`
  - `stats.waiters -> int`（属性）
  - `stats.enter_wait() -> None` / `stats.exit_wait() -> None`
  - `stats.record(site: str, wait_ms: float, hold_ms: float) -> None`
  - `stats.snapshot() -> dict`，形如
    `{"sites": {site: {"count": int, "wait_max_ms": float, "hold_max_ms": float, "wait_p99_ms": float, "hold_p99_ms": float}}}`
  - `stats.reset() -> None`
  - 模块常量 `BUCKETS_MS: tuple[float, ...]`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_write_lock_stats.py`：

```python
from app.repositories.sqlite.write_lock_stats import WriteLockStats


def test_waiters_counts_up_and_down():
    s = WriteLockStats()
    assert s.waiters == 0
    s.enter_wait()
    s.enter_wait()
    assert s.waiters == 2
    s.exit_wait()
    assert s.waiters == 1
    s.exit_wait()
    assert s.waiters == 0


def test_snapshot_aggregates_per_site():
    s = WriteLockStats()
    s.record("a.py:1", wait_ms=1.0, hold_ms=10.0)
    s.record("a.py:1", wait_ms=3.0, hold_ms=30.0)
    s.record("b.py:2", wait_ms=5.0, hold_ms=50.0)
    snap = s.snapshot()
    assert snap["sites"]["a.py:1"]["count"] == 2
    assert snap["sites"]["a.py:1"]["hold_max_ms"] == 30.0
    assert snap["sites"]["b.py:2"]["count"] == 1
    assert snap["sites"]["b.py:2"]["wait_max_ms"] == 5.0


def test_p99_reports_bucket_upper_bound():
    """50/50 分布下 p99 必须落在大值那一侧的桶上界(1000ms 桶)。

    刻意不用「99 小 + 1 大」:那种分布的 p99 本来就是小值,断言 p99>=900 是错的。
    """
    s = WriteLockStats()
    for _ in range(50):
        s.record("a.py:1", wait_ms=0.5, hold_ms=0.5)
    for _ in range(50):
        s.record("a.py:1", wait_ms=900.0, hold_ms=900.0)
    p99 = s.snapshot()["sites"]["a.py:1"]["hold_p99_ms"]
    assert p99 == 1000.0


def test_p99_of_an_all_fast_site_stays_small():
    s = WriteLockStats()
    for _ in range(100):
        s.record("a.py:1", wait_ms=0.5, hold_ms=0.5)
    assert s.snapshot()["sites"]["a.py:1"]["hold_p99_ms"] == 1.0


def test_memory_is_bounded_by_bucket_count():
    """1e4 次 record 之后,每个 site 的内存占用不随样本数增长。"""
    s = WriteLockStats()
    from app.repositories.sqlite.write_lock_stats import BUCKETS_MS
    for i in range(10_000):
        s.record("a.py:1", wait_ms=float(i % 7), hold_ms=float(i % 13))
    site = s._sites["a.py:1"]
    assert len(site.wait_buckets) == len(BUCKETS_MS)
    assert len(site.hold_buckets) == len(BUCKETS_MS)


def test_violation_goes_to_sink_immediately():
    seen = []
    s = WriteLockStats(warn_ms=100.0, sink=seen.append)
    s.record("a.py:1", wait_ms=1.0, hold_ms=5.0)
    assert seen == []
    s.record("a.py:1", wait_ms=1.0, hold_ms=250.0)
    assert len(seen) == 1
    assert seen[0]["kind"] == "db_write_lock_slow"
    assert seen[0]["site"] == "a.py:1"
    assert seen[0]["hold_ms"] == 250.0


def test_violation_sink_is_rate_limited_per_site():
    """同一 site 的连续违规不得逐次刷屏:每个刷新窗口内每 site 最多一条。"""
    seen = []
    s = WriteLockStats(warn_ms=100.0, flush_interval_s=1e6, sink=seen.append)
    for _ in range(50):
        s.record("a.py:1", wait_ms=1.0, hold_ms=250.0)
    assert len(seen) == 1


def test_reset_clears_sites():
    s = WriteLockStats()
    s.record("a.py:1", wait_ms=1.0, hold_ms=1.0)
    s.reset()
    assert s.snapshot()["sites"] == {}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_stats.py -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.repositories.sqlite.write_lock_stats'`

- [ ] **Step 3: 实现**

创建 `backend/app/repositories/sqlite/write_lock_stats.py`：

```python
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
            return BUCKETS_MS[i]
    return BUCKETS_MS[-1]


@dataclass
class _Site:
    count: int = 0
    wait_max_ms: float = 0.0
    hold_max_ms: float = 0.0
    wait_buckets: List[int] = field(
        default_factory=lambda: [0] * len(BUCKETS_MS))
    hold_buckets: List[int] = field(
        default_factory=lambda: [0] * len(BUCKETS_MS))
    warned_at: float = 0.0


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
            if violation is not None:
                self.sink(violation)
            if flush is not None:
                self.sink(flush)

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
            }
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def reset(self) -> None:
        with self._lock:
            self._sites.clear()
            self._waiters = 0
            self._last_flush = time.monotonic()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_stats.py -q
```
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/repositories/sqlite/write_lock_stats.py backend/tests/test_write_lock_stats.py
git commit -m "feat(sqlite): add bounded write-lock wait/hold statistics container"
```

---

### Task 2: 把仪器接进 `SqliteDatabase.write()`

**Files:**
- Modify: `backend/app/repositories/sqlite/database.py`（`SqliteDatabase.__init__`、`write()`）
- Modify: `backend/app/core/config.py`（新增三个设置项）
- Test: `backend/tests/test_write_lock_instrumentation.py`

**Interfaces:**
- Consumes: Task 1 的 `WriteLockStats`
- Produces:
  - `SqliteDatabase.stats: WriteLockStats | None`（`db_write_lock_stats_enabled=False` 时为 `None`）
  - Settings: `db_write_lock_stats_enabled: bool`（alias `DB_WRITE_LOCK_STATS`，默认 `True`）、
    `db_write_lock_warn_ms: int`（alias `DB_WRITE_LOCK_WARN_MS`，默认 `200`）、
    `db_write_lock_flush_seconds: int`（alias `DB_WRITE_LOCK_FLUSH_SECONDS`，默认 `60`）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_write_lock_instrumentation.py`：

```python
import threading
import time

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase


def _db(tmp_path, **over):
    kw = {"sqlite_path": str(tmp_path / "db.sqlite"),
          "storage_dir": str(tmp_path / "s")}
    kw.update(over)
    return SqliteDatabase(Settings(**kw), tmp_path)


def test_records_call_site_of_the_caller_not_contextlib(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:                                  # <-- 这一行
        conn.execute("CREATE TABLE t (a INTEGER)")
    expected_line = test_records_call_site_of_the_caller_not_contextlib \
        .__code__.co_firstlineno + 2
    sites = db.stats.snapshot()["sites"]
    assert len(sites) == 1
    site = next(iter(sites))
    assert site == f"test_write_lock_instrumentation.py:{expected_line}", site


def test_hold_ms_reflects_time_inside_the_block(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
        time.sleep(0.05)
    hold = next(iter(db.stats.snapshot()["sites"].values()))["hold_max_ms"]
    assert hold >= 50.0


def test_nested_write_is_measured_once_at_the_outermost_level(tmp_path):
    """write() 的锁是 RLock,嵌套写深度真实存在;内层重复计数会把 hold 算重。"""
    db = _db(tmp_path)
    with db.write() as outer:
        outer.execute("CREATE TABLE t (a INTEGER)")
        with db.write() as inner:
            inner.execute("CREATE TABLE u (a INTEGER)")
    sites = db.stats.snapshot()["sites"]
    assert sum(s["count"] for s in sites.values()) == 1, sites


def test_wait_ms_measures_queueing_behind_another_writer(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    db.stats.reset()

    started = threading.Event()

    def _hog():
        with db.write() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            started.set()
            time.sleep(0.2)

    hog = threading.Thread(target=_hog)
    hog.start()
    assert started.wait(2.0)
    with db.write() as conn:
        conn.execute("INSERT INTO t VALUES (2)")
    hog.join()

    waits = [s["wait_max_ms"] for s in db.stats.snapshot()["sites"].values()]
    assert max(waits) >= 100.0, waits


def test_waiters_counter_returns_to_zero_after_contention(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    assert db.stats.waiters == 0


def test_waiters_counter_does_not_leak_when_block_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            conn.execute("CREATE TABLE t (a INTEGER)")
            raise RuntimeError("boom")
    assert db.stats.waiters == 0


def test_uses_the_current_write_lock_object_not_a_cached_one(tmp_path):
    """sqlite_repository.py 存在 _write_lock 的 setter(测试会替换锁对象);
    仪器若在 __init__ 缓存了锁,替换后就会用错的锁。

    判据必须是「替换后的锁**确实被 acquire 了**」,而不是「替换后的锁现在空闲」
    —— 后者在缓存 bug 下同样成立(那把锁压根没被碰过),测不出东西。
    """
    db = _db(tmp_path)
    calls = []
    real = threading.RLock()

    class _RecordingLock:
        def acquire(self, *a, **kw):
            calls.append("acquire")
            return real.acquire(*a, **kw)

        def release(self):
            calls.append("release")
            return real.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    db.write_lock = _RecordingLock()
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    assert calls == ["acquire", "release"], calls


def test_disabled_flag_turns_stats_off_entirely(tmp_path):
    db = _db(tmp_path, db_write_lock_stats_enabled=False)
    assert db.stats is None
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
```

> 注意：`Settings(db_write_lock_stats_enabled=False)` 这种按字段名构造在本仓库
> 的 `pydantic-settings` v2 配置下**可能失效**（见 Global Constraints）。若
> `test_disabled_flag_turns_stats_off_entirely` 因此不生效，改用
> `monkeypatch.setenv("DB_WRITE_LOCK_STATS", "false")` 后再构造 `Settings()`，
> 并保留断言不变。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_instrumentation.py -q
```
Expected: FAIL — `AttributeError: 'SqliteDatabase' object has no attribute 'stats'`

- [ ] **Step 3: 加设置项**

在 `backend/app/core/config.py` 里 `db_busy_timeout_ms`（第 234 行）之后追加：

```python
    db_write_lock_stats_enabled: bool = Field(
        True, validation_alias="DB_WRITE_LOCK_STATS")
    db_write_lock_warn_ms: int = Field(
        200, validation_alias="DB_WRITE_LOCK_WARN_MS")
    db_write_lock_flush_seconds: int = Field(
        60, validation_alias="DB_WRITE_LOCK_FLUSH_SECONDS")
```

- [ ] **Step 4: 改 `database.py`**

在 `backend/app/repositories/sqlite/database.py` 顶部补 import：

```python
import sys
from time import perf_counter

from app.repositories.sqlite.write_lock_stats import WriteLockStats
```

在 `SqliteDatabase.__init__` 的 `self._local = threading.local()` 之后追加：

```python
        self.stats: WriteLockStats | None = (
            WriteLockStats(
                warn_ms=settings.db_write_lock_warn_ms,
                flush_interval_s=settings.db_write_lock_flush_seconds,
            )
            if settings.db_write_lock_stats_enabled
            else None
        )
```

在模块内加一个调用点解析函数（放在 `class SqliteDatabase` 之前）：

```python
def _caller_site(skip: int) -> str:
    """返回 `filename:lineno` 形式的调用点。

    用 sys._getframe 而非 traceback.extract_stack —— 后者会格式化整个栈,
    相对一次 DB 写是不可忽略的开销;前者是常数级。
    skip 的正确值由 test_records_call_site_of_the_caller_not_contextlib 钉死:
    @contextmanager 会在真实调用者与本函数之间插入 contextlib 的帧。
    """
    try:
        frame = sys._getframe(skip)
    except ValueError:
        return "?"
    name = frame.f_code.co_filename.rsplit("/", 1)[-1]
    return f"{name}:{frame.f_lineno}"
```

把 `write()` 整体替换为：

```python
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务:进程内写串行(write_lock)。每次用**独立新连接**(非线程复用读连接),
        使每个 write() 独立提交 —— 保留嵌套增量提交(节点向量 backfill 每批 flush
        独立落库、中断可续跑)的崩溃恢复语义(INV-8)。用完即 close(写串行,写连接峰值
        = 嵌套写深度、fd 用完即还)。深度守卫不作用于 write()。

        仪器:分开采 wait_ms(排队拿锁=用户感知的卡顿)与 hold_ms(持锁=谁害的),
        只在最外层采(RLock 可重入,内层重复计数会把 hold 算重)。lock 每次现读,
        因为 sqlite_repository 暴露了 _write_lock 的 setter。
        """
        stats = self.stats
        depth = getattr(self._local, "write_depth", 0)
        outermost = depth == 0 and stats is not None
        lock = self.write_lock
        site = _caller_site(3) if outermost else ""
        wait_started = perf_counter() if outermost else 0.0
        if outermost:
            stats.enter_wait()
        try:
            lock.acquire()
        finally:
            if outermost:
                stats.exit_wait()
        wait_ms = (perf_counter() - wait_started) * 1000.0 if outermost else 0.0
        hold_started = perf_counter() if outermost else 0.0
        self._local.write_depth = depth + 1
        try:
            conn = self._new_connection()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        finally:
            self._local.write_depth = depth
            lock.release()
            if outermost:
                stats.record(
                    site, wait_ms, (perf_counter() - hold_started) * 1000.0
                )
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_instrumentation.py -q
```
Expected: 8 passed

若 `test_records_call_site_of_the_caller_not_contextlib` 报出的是 `contextlib.py:NNN`
或 `database.py:NNN`，说明 `_caller_site(3)` 的 skip 值不对：把 3 调成让断言通过的值
（`@contextmanager` 的帧层数随 Python 版本可能不同），**不要改测试的期望值**。

- [ ] **Step 6: 确认既有 SQLite 测试无回归**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_sqlite_database_component.py tests/test_sqlite_connection_reuse.py tests/test_sqlite_write_optimization.py -q
```
Expected: all passed

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/sqlite/database.py backend/app/core/config.py backend/tests/test_write_lock_instrumentation.py
git commit -m "feat(sqlite): instrument write() with wait/hold timing per call site"
```

---

### Task 3: 把观测事件接到 `events.jsonl`

**Files:**
- Modify: `backend/app/services/repository_runtime.py`（构造 `SqliteDatabase` 之后接 sink）
- Test: `backend/tests/test_write_lock_events.py`

**Interfaces:**
- Consumes: Task 1 的 `WriteLockStats.sink`、Task 2 的 `SqliteDatabase.stats`
- Produces: `events.jsonl` 中的两种事件 —— `kind="db_write_lock_slow"`（单次违规，每 site 每窗口一条）与 `kind="db_write_lock_stats"`（周期聚合快照）

- [ ] **Step 1: 定位接线点**

```bash
grep -n "SqliteDatabase(" backend/app/services/repository_runtime.py
grep -n "event_log\b" backend/app/services/repository_runtime.py | head -5
```

记下 `SqliteDatabase(...)` 的赋值行号与 `EventLogger` 实例的属性名（下一步要用）。

- [ ] **Step 2: 写失败测试**

创建 `backend/tests/test_write_lock_events.py`：

```python
import json

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def test_slow_write_emits_a_db_write_lock_slow_event(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("DB_WRITE_LOCK_WARN_MS", "1")   # 任何写都算「慢」
    repo = SQLiteRepository(Settings())

    seen = []
    repo._runtime.database.stats.sink = seen.append
    with repo._runtime.database.write() as conn:
        conn.execute("CREATE TABLE probe (a INTEGER)")

    kinds = [e["kind"] for e in seen]
    assert "db_write_lock_slow" in kinds, seen
    slow = next(e for e in seen if e["kind"] == "db_write_lock_slow")
    assert "site" in slow and "wait_ms" in slow and "hold_ms" in slow
    # 脱敏:事件里不得出现 SQL、参数值或文件绝对路径
    blob = json.dumps(slow)
    assert "CREATE TABLE" not in blob
    assert str(tmp_path) not in blob


def test_runtime_wires_the_event_log_as_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    assert repo._runtime.database.stats is not None
    assert repo._runtime.database.stats.sink is not None
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_events.py -q
```
Expected: `test_runtime_wires_the_event_log_as_sink` FAIL — `assert None is not None`

- [ ] **Step 4: 接线**

在 `backend/app/services/repository_runtime.py` 里，`SqliteDatabase(...)` 赋值与
`EventLogger` 都就绪之后（用 Step 1 记下的实际属性名替换 `self.event_log`）追加：

```python
        # 写锁观测的出口:走既有 events.jsonl,不新起日志通道。sink 挂在
        # database 实例上(而非模块级单例),使并发/多实例测试彼此隔离。
        if self.database.stats is not None:
            self.database.stats.sink = self.event_log.emit
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_events.py -q
```
Expected: 2 passed

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/repository_runtime.py backend/tests/test_write_lock_events.py
git commit -m "feat(sqlite): emit write-lock slow/aggregate events to events.jsonl"
```

---

### Task 4: `scripts/diag.py locks` 子命令 + README

**Files:**
- Modify: `scripts/diag.py`（模块 docstring、新增 `_cmd_locks`、`SUBCOMMANDS`）
- Modify: `README.md`、`README_zh.md`
- Test: `backend/tests/test_diag_unified.py`（**追加到文件末尾**，不移动既有行号）

**Interfaces:**
- Consumes: Task 3 写进 `events.jsonl` 的 `db_write_lock_slow` / `db_write_lock_stats`
- Produces: `python3 scripts/diag.py locks [--log PATH] [--top N]`，按 site 打印 count / wait_max / hold_max / wait_p99 / hold_p99

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_diag_unified.py` **末尾**：

```python
def test_locks_subcommand_aggregates_write_lock_events(tmp_path, capsys):
    """diag.py locks 从 events.jsonl 离线聚合写锁事件。纯 stdlib、零 app 依赖。"""
    import json
    import sys

    log = tmp_path / "events.jsonl"
    rows = [
        {"kind": "db_write_lock_slow", "site": "a.py:1",
         "wait_ms": 5.0, "hold_ms": 800.0},
        {"kind": "db_write_lock_slow", "site": "a.py:1",
         "wait_ms": 7.0, "hold_ms": 1200.0},
        {"kind": "db_write_lock_slow", "site": "b.py:2",
         "wait_ms": 300.0, "hold_ms": 10.0},
        {"kind": "ask_stage", "stage": "retrieve", "ms": 3},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                   encoding="utf-8")

    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import diag
    rc = diag.main(["locks", "--log", str(log)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "a.py:1" in out and "b.py:2" in out
    assert "1200" in out          # a.py:1 的 hold_max
    assert "ask_stage" not in out  # 不相关事件被过滤


def test_locks_subcommand_is_registered(tmp_path):
    import sys
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import diag
    assert "locks" in diag.SUBCOMMANDS
```

> `_REPO_ROOT` 若在 `test_diag_unified.py` 中尚未定义，在文件末尾这两个测试之前
> 补一行 `_REPO_ROOT = Path(__file__).resolve().parents[2]`（并确认 `Path` 已 import）。
> 先跑 `grep -n "_REPO_ROOT\|^from pathlib\|^import" backend/tests/test_diag_unified.py | head` 确认。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_diag_unified.py -q -k locks
```
Expected: FAIL — `AssertionError: assert 'locks' in {...}`

- [ ] **Step 3: 实现子命令**

在 `scripts/diag.py` 的 `_cmd_base_recall` 之后、`SUBCOMMANDS` 之前插入：

```python
def _read_lock_events(path, kinds=("db_write_lock_slow",)):
    """按行读 events.jsonl,只留写锁事件。坏行跳过(日志可能被截断)。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("kind") in kinds:
                out.append(rec)
    return out


def _aggregate_locks(records):
    """按 site 聚合。返回 [(site, count, wait_max, hold_max, wait_p99, hold_p99)]
    按 hold_max 降序 —— 排最前的就是最该改的那处。"""
    by_site = {}
    for r in records:
        site = str(r.get("site") or "?")
        agg = by_site.setdefault(site, {"waits": [], "holds": []})
        agg["waits"].append(float(r.get("wait_ms") or 0.0))
        agg["holds"].append(float(r.get("hold_ms") or 0.0))

    def _p99(values):
        if not values:
            return 0.0
        ordered = sorted(values)
        # nearest-rank ceiling,与 diag.py latency 的分位口径一致
        idx = max(0, math.ceil(len(ordered) * 0.99) - 1)
        return ordered[idx]

    rows = [
        (site, len(a["holds"]), max(a["waits"]), max(a["holds"]),
         _p99(a["waits"]), _p99(a["holds"]))
        for site, a in by_site.items()
    ]
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows


def _cmd_locks(rest) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="diag.py locks",
        description="SQLite 写锁的 wait/hold 分布,来自 events.jsonl 的 "
                    "db_write_lock_slow 事件。wait=排队(用户感知的卡顿),"
                    "hold=持锁(谁害的)。")
    ap.add_argument("--log", default=str(_DEFAULT_EVENTS),
                    help=f"events JSONL 路径(默认 {_DEFAULT_EVENTS})")
    ap.add_argument("--top", type=int, default=20, metavar="N",
                    help="只打印 hold_max 最大的 N 个调用点(默认 20)")
    args = ap.parse_args(rest)
    rows = _aggregate_locks(_read_lock_events(args.log))
    if not rows:
        print("没有 db_write_lock_slow 事件 —— 要么没有慢写,"
              "要么 DB_WRITE_LOCK_WARN_MS 设得太高。")
        return 0
    print(f"{'site':<44}{'n':>7}{'wait_max':>11}{'hold_max':>11}"
          f"{'wait_p99':>11}{'hold_p99':>11}")
    for site, n, wmax, hmax, wp99, hp99 in rows[:max(1, args.top)]:
        print(f"{site:<44}{n:>7}{wmax:>11.1f}{hmax:>11.1f}"
              f"{wp99:>11.1f}{hp99:>11.1f}")
    return 0
```

在 `SUBCOMMANDS` 字典里追加一项：

```python
    "locks": _cmd_locks,
```

在模块 docstring 的子命令清单里追加：

```
    locks        SQLite 写锁的 wait/hold 分布(按调用点),来自 events.jsonl。
                 纯 stdlib、只读,不 import app。
```

以及在 docstring 顶部的示例块里追加一行：

```
    python3 scripts/diag.py locks --top 20
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_diag_unified.py -q
```
Expected: all passed

- [ ] **Step 5: 确认零 app 依赖没被破坏**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/upbeat-faraday-638d23 && python3 -c "
import sys; sys.path.insert(0, 'scripts')
import diag
assert not [m for m in sys.modules if m == 'app' or m.startswith('app.')], 'diag.py 拉起了 app'
print('ok: diag.py 无 app 依赖')
"
```
Expected: `ok: diag.py 无 app 依赖`

- [ ] **Step 6: 写 README（两份都要）**

在 `README.md` 里 `scripts/diag.py` 已有的用法段落中追加：

```markdown
- `python3 scripts/diag.py locks [--log PATH] [--top N]` — aggregate SQLite
  write-lock contention by call site from `events.jsonl`. `wait` is how long a
  writer queued for the lock (what users feel as a stalled page); `hold` is how
  long a writer held it (who caused it). Sorted by `hold_max`, worst first.
  Tune the capture threshold with `DB_WRITE_LOCK_WARN_MS` (default 200).
```

在 `README_zh.md` 的对应段落追加：

```markdown
- `python3 scripts/diag.py locks [--log PATH] [--top N]` —— 从 `events.jsonl`
  按调用点聚合 SQLite 写锁争用。`wait` 是写者排队等锁的时长（用户感知为「页面卡住」），
  `hold` 是持锁时长（谁害的）。按 `hold_max` 降序，最该改的排最前。
  采集阈值由 `DB_WRITE_LOCK_WARN_MS` 控制（默认 200）。
```

- [ ] **Step 7: 提交**

```bash
git add scripts/diag.py README.md README_zh.md backend/tests/test_diag_unified.py
git commit -m "feat(diag): add locks subcommand for write-lock contention"
```

---

### Task 5: 合成基准

**Files:**
- Create: `backend/tests/test_write_lock_benchmark.py`
- Test: 同一个文件（基准本身就是测试）

**Interfaces:**
- Consumes: Task 2 的 `SqliteDatabase.stats`
- Produces:
  - `seed_notebook(repo, n_objects: int) -> str`（返回 notebook_id）
  - `measure_rebuild(repo, notebook_id: str) -> dict`（返回 `stats.snapshot()["sites"]`）
  - `report(sites: dict) -> str`（人类可读表格，供 Task 8 粘贴）

**为什么要它：** 开发机上的库通常只有 10^4–10^5 量级对象，量不出部署环境（10^5–10^6）的分布。整个计划的「改造点 2/3/4 做不做」只以这个基准为准。

- [ ] **Step 1: 写基准（默认档，常跑）**

创建 `backend/tests/test_write_lock_benchmark.py`：

```python
"""写锁瘦身的合成基准。

默认档(20k 对象)每次都跑,当回归守卫用;大规模档(--benchmark-scale 或 slow 标记)
按需跑,用来量部署量级(10^5-10^6)的分布。

种子数据用 repo.store_kg 的公开 API 造(与 test_rebuild_streaming.py 同款),
不写裸 SQL —— 服务层/测试层都不该出现裸 SQL(callers_static 约束)。
"""
import os

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate

_BATCH = 2000


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def seed_notebook(repo, n_objects: int) -> str:
    """造 n_objects 个 concept/claim,名字有意重复(每 8 个一组同名),
    使聚类真的产生多成员簇 —— 全唯一名会让 concept_clusters 退化成 1:1,
    量不出真实的整表替换代价。"""
    nb = repo.create_notebook(NotebookCreate(name="bench"))
    made = 0
    while made < n_objects:
        batch = []
        for i in range(made, min(made + _BATCH, n_objects)):
            kind = "concept" if i % 2 == 0 else "claim"
            batch.append({
                "local_id": f"o{i}",
                "object_type": kind,
                "payload": {"name": f"{kind}-{i // 8}", "section_path": ""},
                "evidence": [],
            })
        repo.store_kg(nb.id, None, batch, [])
        made += len(batch)
    return nb.id


def measure_rebuild(repo, notebook_id: str) -> dict:
    db = repo._runtime.database
    assert db.stats is not None, "基准需要仪器打开(DB_WRITE_LOCK_STATS)"
    db.stats.reset()
    repo.rebuild_unified_kg(notebook_id, force=True)
    return db.stats.snapshot()["sites"]


def report(sites: dict) -> str:
    rows = sorted(sites.items(), key=lambda kv: kv[1]["hold_max_ms"],
                  reverse=True)
    out = [f"{'site':<44}{'n':>7}{'hold_max':>11}{'hold_p99':>11}"
           f"{'wait_max':>11}"]
    for site, s in rows:
        out.append(f"{site:<44}{s['count']:>7}{s['hold_max_ms']:>11.1f}"
                   f"{s['hold_p99_ms']:>11.1f}{s['wait_max_ms']:>11.1f}")
    return "\n".join(out)


def test_benchmark_default_scale_reports_hold_distribution(repo, capsys):
    """默认档:20k 对象。这一档不设门槛断言(小规模下几乎必然达标),
    只保证基准本身可跑、报表可读 —— 门槛断言在 Task 6/7 之后加。"""
    nb = seed_notebook(repo, 20_000)
    sites = measure_rebuild(repo, nb)
    assert sites, "rebuild 期间没有采到任何 write() 样本"
    with capsys.disabled():
        print("\n=== write-lock benchmark (20k objects) ===")
        print(report(sites))


@pytest.mark.slow
def test_benchmark_large_scale_reports_hold_distribution(repo, capsys):
    """大规模档:默认 300k 对象,用 BENCH_OBJECTS 覆盖。

    跑法:
      cd backend && SILICON_NOTEBOOK_ENV_FILE="" BENCH_OBJECTS=500000 \\
        python -m pytest tests/test_write_lock_benchmark.py -m slow -s -q
    """
    n = int(os.environ.get("BENCH_OBJECTS", "300000"))
    nb = seed_notebook(repo, n)
    sites = measure_rebuild(repo, nb)
    with capsys.disabled():
        print(f"\n=== write-lock benchmark ({n} objects) ===")
        print(report(sites))
```

- [ ] **Step 2: 跑默认档**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_benchmark.py -q -m "not slow" -s -n0
```
Expected: PASS，并打印出一张按 `hold_max` 降序的表。表头一两行应当是
`knowledge_lifecycle.py:1503` 与 `knowledge_lifecycle.py:1586` 附近的调用点 —— 若不是，
说明 spec §2.1 的定位有误，**停下来先核对再继续**（不要盲目往下做改造）。

- [ ] **Step 3: 记录基线数字**

把 Step 2 的输出粘进本文件 Task 8 的「基线（改造前）」表格。这是后面判断改造是否
真的有效的唯一对照。

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_write_lock_benchmark.py
git commit -m "test: add synthetic write-lock benchmark at configurable scale"
```

---

### Task 6: 改造点 1 —— scratch 暂存分批提交

**Files:**
- Modify: `backend/app/services/knowledge_lifecycle.py:1496-1521`（`_stream_seed_reps` 的 Pass A2）
- Test: `backend/tests/test_write_lock_scratch_batching.py`

**Interfaces:**
- Consumes: Task 2 的 `SqliteDatabase.stats`
- Produces: 无新公开接口（纯内部事务切分）

**背景：** 当前 Pass A2 把「整个全库扫描 + 每对象的 Python 计算（`_fast_loads`、
`seed_or_unique`、alias 匹配）」整个圈在一个 `self._write()` 里，而它写的
`kg_cluster_scratch` **没有任何读者**。这是纯粹的白占锁，拆开零语义风险。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_write_lock_scratch_batching.py`：

```python
import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo, n):
    """只造 concept —— 单一类型使 insert_scratch_rows 的调用次数完全可预期
    (n/1000 批),让下面的守卫能同时挡住「合并成一个事务」和「把提交移出循环」
    两种退化。混类型会让计数变成 4 个类型的和,两种退化都可能蒙混过关。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": f"o{i}", "object_type": "concept",
         "payload": {"name": f"c-{i // 8}", "section_path": ""},
         "evidence": []}
        for i in range(n)
    ], [])
    return nb.id


def test_scratch_staging_uses_a_fresh_transaction_per_batch(repo, monkeypatch):
    """判据:每批 scratch 插入必须落在**不同的写连接**上。

    write() 每次新建连接、用完即 close,所以「同一个 conn 收了多批」等价于
    「这些批共处一个事务」。用连接身份判定,而不是数 write() 样本数 —— 后者会被
    同文件里的其他写点(clear_scratch_run / 簇写入 / finish_rebuild_state)污染,
    在改造前就已经 >1,守卫会假绿。

    列表持有连接对象本身(而非 id),防止对象被回收后 id 复用导致误判。
    """
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    seen_conns = []
    original = UnifiedKgStore.insert_scratch_rows

    def _spy(db, rows):
        seen_conns.append(db)
        return original(db, rows)

    monkeypatch.setattr(UnifiedKgStore, "insert_scratch_rows",
                        staticmethod(_spy))

    nb = _seed(repo, 5_000)
    repo.rebuild_unified_kg(nb, force=True)

    assert len(seen_conns) >= 5, (
        "5000 个 concept 应产生 5 批(批大小 1000);批数不足说明提交被移出了循环",
        len(seen_conns))
    assert len({id(c) for c in seen_conns}) == len(seen_conns), (
        "多个 scratch 批共用同一个写连接 = 仍在一个大事务里",
        len(seen_conns), len({id(c) for c in seen_conns}))


def test_clustering_result_is_bit_identical_after_batching(repo):
    """分批提交不得改变聚类结果。同名对象必须仍然并进同一个 canonical。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "mosfet", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "BJT", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id, force=True)
    cmap = repo.cluster_map(nb.id)
    assert len(cmap) == 3
    assert len(set(cmap.values())) == 2


def test_scratch_rows_are_cleaned_up_after_rebuild(repo):
    """分批提交后,中断/正常结束的 scratch 清理语义不变。"""
    nb = _seed(repo, 3_000)
    repo.rebuild_unified_kg(nb, force=True)
    with repo._connect() as db:
        left = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb,)).fetchone()["c"]
    assert left == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_scratch_batching.py -q
```
Expected: `test_scratch_staging_commits_in_batches_not_one_transaction` FAIL —
断言消息含「scratch 暂存仍是一个大事务」

- [ ] **Step 3: 实现**

把 `backend/app/services/knowledge_lifecycle.py` 的 Pass A2（当前第 1496–1521 行）
从「读游标外、写事务包整个循环」改成「循环在事务外、每满一批开一个短事务」：

```python
        with self._connect() as rdb:
            # ORDER BY rowid: canonical-name selection here is first-seen per seed
            # (seed_first_name), so the stream order must be deterministic and
            # independent of which index the planner happens to pick — otherwise
            # adding/removing an index silently changes canonical names + desc-cache
            # keys. rowid = insertion order, matching the historical behaviour.
            cur = self.unified_kg.stream_seed_rows(rdb, notebook_id, object_type)
            # 写锁瘦身:Python 计算(_fast_loads / seed_or_unique / alias 匹配)留在
            # 事务外,只有 executemany 进写锁。kg_cluster_scratch 没有读者,分批提交
            # 不产生任何可见中间态;run_id 隔离由 stream_scratch_rows 的过滤保证。
            for r in cur:
                pay = _fast_loads(r["payload"] or "{}")
                name = pay.get("name", "")
                # Pass the full payload-bearing object so seed_fn can use it
                # (e.g. seed_procedure appends a steps signature from payload).
                # Mirrors the legacy cluster_objects(tobjs={name,payload}, ...).
                seed = seed_or_unique(
                    _seed_with_alias({"name": name, "payload": pay}, seed_fn, alias_map),
                    r["id"])
                members_count[seed] = members_count.get(seed, 0) + 1
                seed_first_name.setdefault(seed, name)
                buf.append((notebook_id, run_id, r["id"], seed))
                if len(buf) >= 1000:
                    with self._write() as wdb:
                        self.unified_kg.insert_scratch_rows(wdb, buf)
                    buf.clear()
            if buf:
                with self._write() as wdb:
                    self.unified_kg.insert_scratch_rows(wdb, buf)
                buf.clear()
```

**注意：** `for r in cur` 现在在写事务外迭代。`cur` 来自 `self._connect()` 的线程复用
读连接，WAL 下读不被写阻塞，语义不变。`buf` 的 1000 行批边界保持不变，因此
`insert_scratch_rows` 的调用序列与改造前逐字相同 —— 只是提交点变多了。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_scratch_batching.py -q
```
Expected: 3 passed

- [ ] **Step 5: 确认聚类语义零漂移**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_rebuild_streaming.py tests/test_cross_doc_merge.py tests/test_kg_merge.py tests/test_unified_kg_repository.py -q
```
Expected: all passed（这四个文件是聚类语义的既有守卫，一个都不能红）

- [ ] **Step 6: 变异验证 —— 「删除」变异**

把 Step 3 的两处 `with self._write() as wdb:` 改回外层单事务（即恢复改造前的形态），
然后：

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_scratch_batching.py -q -k batches
echo "exit=$?"
```
Expected: FAIL，`exit=1`。若仍然 PASS，说明守卫无效（多半是断言用的调用点前缀
`knowledge_lifecycle.py:` 匹配到了别的写点）—— 先把断言收紧到具体行号再继续。

改回正确形态，重跑确认变绿。

- [ ] **Step 7: 变异验证 —— 「移动」变异**

只删除「删」不够：把批提交**移出** `for` 循环（放到循环之后一次性提交），再跑：

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_scratch_batching.py -q -k batches
echo "exit=$?"
```
Expected: FAIL，`exit=1`。

改回正确形态，重跑确认变绿。

- [ ] **Step 8: 跑基准看效果**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_write_lock_benchmark.py -q -m "not slow" -s -n0
```
把输出粘进 Task 8 的「改造点 1 之后」表格。

- [ ] **Step 9: 提交**

```bash
git add backend/app/services/knowledge_lifecycle.py backend/tests/test_write_lock_scratch_batching.py
git commit -m "perf(kg): commit cluster-scratch staging in batches, off the write lock"
```

---

### Task 7: `bulk_write()` 让路原语

**Files:**
- Modify: `backend/app/repositories/sqlite/database.py`（新增 `bulk_write`）
- Modify: `backend/app/services/repository_runtime.py:915`（给 `KnowledgeLifecycleService` 注入）
- Modify: `backend/app/services/knowledge_lifecycle.py:98-160`（构造参数 + `self._bulk_write`）
- Modify: `backend/app/services/knowledge_lifecycle.py`（Task 6 改的两处改走 `_bulk_write`）
- Modify: `backend/app/repositories/ownership_manifest.py`（登记 `_bulk_write`）
- Test: `backend/tests/test_bulk_write_fairness.py`

**Interfaces:**
- Consumes: Task 1 的 `WriteLockStats.waiters`
- Produces:
  - `SqliteDatabase.bulk_write(batches: Iterable[list], apply: Callable[[sqlite3.Connection, list], None], yield_seconds: float = 0.002) -> int`
    —— 每个 batch 一个独立短事务；每批提交后若 `stats.waiters > 0` 则让路；返回处理的批数。
  - `KnowledgeLifecycleService._bulk_write`（注入的同名可调用对象）

**为什么需要：** `write_lock` 是 `threading.RLock`，**不保证公平**。批量写者
`for batch: with write(): ...` 连续抢锁时，交互写仍可能长时间拿不到锁。光把事务改短
不够。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_bulk_write_fairness.py`：

```python
import threading
import time

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase


def _db(tmp_path):
    return SqliteDatabase(
        Settings(sqlite_path=str(tmp_path / "db.sqlite"),
                 storage_dir=str(tmp_path / "s")),
        tmp_path,
    )


def _apply(conn, batch):
    conn.executemany("INSERT INTO t (a) VALUES (?)", [(x,) for x in batch])


def test_bulk_write_commits_each_batch_separately(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    batches = [[1, 2], [3, 4], [5, 6]]
    n = db.bulk_write(batches, _apply)
    assert n == 3
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 6


def test_bulk_write_partial_failure_keeps_earlier_batches(tmp_path):
    """第 3 批炸掉时,前两批必须已经落库(独立提交,不是一个大事务)。"""
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    def _boom(conn, batch):
        if 5 in batch:
            raise RuntimeError("boom")
        _apply(conn, batch)

    with pytest.raises(RuntimeError):
        db.bulk_write([[1, 2], [3, 4], [5, 6]], _boom)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 4


def test_interactive_writer_is_not_starved_by_a_bulk_writer(tmp_path):
    """公平性:批量写满负荷时,一个交互写必须在有界时间内拿到锁。

    没有让路机制时,RLock 的不公平会让交互写等到批量写全部跑完。
    """
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    stop = threading.Event()
    waits = []

    def _bulk():
        # 足够多的批,使交互写必然落在批量写运行期间
        batches = ([list(range(100))] * 400)
        db.bulk_write(batches, _apply)
        stop.set()

    worker = threading.Thread(target=_bulk)
    worker.start()
    time.sleep(0.05)                      # 让批量写先跑起来

    for _ in range(20):
        t0 = time.perf_counter()
        with db.write() as conn:
            conn.execute("INSERT INTO t (a) VALUES (-1)")
        waits.append((time.perf_counter() - t0) * 1000.0)

    worker.join(timeout=60)
    assert stop.is_set(), "bulk_write 没有在 60s 内跑完"

    worst = max(waits)
    assert worst < 100.0, f"交互写最坏等待 {worst:.1f}ms 超过 100ms 门槛: {sorted(waits)}"


def test_bulk_write_does_not_sleep_when_nobody_is_waiting(tmp_path):
    """无人等待时不得让路 —— 否则空闲情况下白白拉长批量任务。"""
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    t0 = time.perf_counter()
    db.bulk_write([[1]] * 200, _apply, yield_seconds=0.05)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"无争用时仍在 sleep,耗时 {elapsed:.2f}s"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_bulk_write_fairness.py -q
```
Expected: FAIL — `AttributeError: 'SqliteDatabase' object has no attribute 'bulk_write'`

- [ ] **Step 3: 实现 `bulk_write`**

在 `backend/app/repositories/sqlite/database.py` 的 `write()` 之后追加：

```python
    def bulk_write(
        self,
        batches: "Iterable[list]",
        apply: "Callable[[sqlite3.Connection, list], None]",
        yield_seconds: float = 0.002,
    ) -> int:
        """批量写原语:每批一个独立短事务,批间给等待中的交互写让路。

        为什么不能只把事务改短:write_lock 是 threading.RLock,**不保证公平**。
        一个 `for batch: with write(): ...` 的批量写者连续抢锁时,刚释放锁的线程
        往往立刻重新拿到,交互写可能饿到批量任务结束。所以每批提交后检查
        stats.waiters,有人等就让出时间片。

        无人等待时不 sleep —— 空闲情况下不为公平性付延迟。

        ⚠ 只供后台/批量线程调用,**不得在事件循环线程上调用**(会 sleep)。

        返回处理的批数。某批抛异常时,该批回滚,**之前已提交的批保留**
        (这正是分批提交的意义:中断可续跑)。
        """
        import time as _time

        stats = self.stats
        count = 0
        for batch in batches:
            with self.write() as conn:
                apply(conn, batch)
            count += 1
            if stats is not None and stats.waiters > 0:
                _time.sleep(yield_seconds)
        return count
```

顶部 import 补上 `Callable`：

```python
from typing import Callable, Iterable, Iterator
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_bulk_write_fairness.py -q
```
Expected: 4 passed

若 `test_interactive_writer_is_not_starved_by_a_bulk_writer` 仍红，把 `yield_seconds`
的默认值往上调（0.002 → 0.005）再跑；**不要放宽 100ms 门槛**，那是 spec §6 的验收线。

> ⚠ 这是本计划里唯一一个**基于墙钟时间的硬阈值**，本仓库有性能硬阈值变脆的前科。
> 若它在负载高的机器上偶发变红：把交互写的采样数从 20 提到 200 后取
> 真正的 P99（而不是最坏值），或给它加 `@pytest.mark.slow`。
> **无论如何不要把 100ms 调大** —— 那等于把验收线本身删掉。

- [ ] **Step 5: 注入到 `KnowledgeLifecycleService`**

`backend/app/services/knowledge_lifecycle.py` 构造参数表里，在
`write: Callable[[], Any],`（约第 116 行）之后追加：

```python
        bulk_write: Callable[..., int],
```

在 `self._write = write`（约第 157 行）之后追加：

```python
        self._bulk_write = bulk_write
```

`backend/app/services/repository_runtime.py` 第 915 行的
`KnowledgeLifecycleService(` 调用里，在传 `write=` 的那一行之后追加：

```python
            bulk_write=self.database.bulk_write,
```

- [ ] **Step 6: 让 Task 6 的两处走 `_bulk_write`**

把 Task 6 Step 3 的循环改成攒批 + 一次 `_bulk_write`（语义等价，但让路生效）：

```python
            def _batches():
                local: List[tuple] = []
                for r in cur:
                    pay = _fast_loads(r["payload"] or "{}")
                    name = pay.get("name", "")
                    seed = seed_or_unique(
                        _seed_with_alias({"name": name, "payload": pay}, seed_fn, alias_map),
                        r["id"])
                    members_count[seed] = members_count.get(seed, 0) + 1
                    seed_first_name.setdefault(seed, name)
                    local.append((notebook_id, run_id, r["id"], seed))
                    if len(local) >= 1000:
                        yield list(local)
                        local.clear()
                if local:
                    yield list(local)

            self._bulk_write(
                _batches(),
                lambda wdb, rows: self.unified_kg.insert_scratch_rows(wdb, rows),
            )
```

- [ ] **Step 7: 登记 manifest**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests/test_repository_ownership.py tests/test_repository_facade_contract.py tests/test_repository_surface_contract.py tests/test_architecture_documentation.py -q
```

若报出 `_bulk_write` 未登记，在 `backend/app/repositories/ownership_manifest.py` 的
成员归属字典里（`'_write_lock': 'SqliteDatabase',` 附近，字典按字母序）插入：

```python
    '_bulk_write': 'KnowledgeLifecycleService',
```

重跑上面四个测试直到全绿。若报的是别的形状（例如需要 `SurfaceMember` 条目），
按报错信息给出的字段补齐，**不要**把断言放宽或加白名单绕过。

- [ ] **Step 8: 全量回归**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest tests -q -m "not slow"
```
Expected: all passed

- [ ] **Step 9: 提交**

```bash
git add backend/app/repositories/sqlite/database.py backend/app/services/knowledge_lifecycle.py backend/app/services/repository_runtime.py backend/app/repositories/ownership_manifest.py backend/tests/test_bulk_write_fairness.py
git commit -m "feat(sqlite): add bulk_write primitive that yields to waiting writers"
```

---

### Task 8: 验收门 —— 用基准数字决定改造点 2/3/4

**Files:**
- Modify: `docs/superpowers/plans/2026-07-21-sqlite-write-lock-slimming.md`（本文件，填下面的表格）
- Modify: `docs/superpowers/specs/2026-07-21-sqlite-write-lock-slimming-design.md`（§5.5 记录判定结果）

**Interfaces:**
- Consumes: Task 5 的基准、Task 6/7 的改造
- Produces: 一个明确的判定 —— 改造点 2/3/4 做 / 不做，以及支撑它的数字

**这一步不是形式主义：** spec §5.5 明确写了「只有在 §5.3 + §5.4 完成后基准仍不达标时
才做」。没有这一步，后面三处整表替换就是凭直觉动刀。

- [ ] **Step 1: 跑大规模档基准**

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" BENCH_OBJECTS=500000 \
  python -m pytest tests/test_write_lock_benchmark.py -m slow -s -q -n0
```

⚠ `pytest.ini` 的 `addopts` 固定 `-n 12`，xdist 开着时 `-s` 拿不到 worker 的
stdout——不加 `-n0` 会通过但一张表都不打印（同 Task 8 Step 1 的更正，见下）。

这一档会跑很久（种子数据 50 万对象）。若机器扛不住，降到 `BENCH_OBJECTS=200000`
并在下面的表格里注明实际规模。

- [ ] **Step 2: 填表**

**基线（改造前，Task 5 Step 3 记录 —— 已知失真,仅作历史记录,不可用于判定）：**

默认档（20k 对象，`backend/tests/test_write_lock_benchmark.py::test_benchmark_default_scale_reports_hold_distribution`）：

| site | n | hold_max | hold_p99 | wait_max |
|---|---|---|---|---|
| sqlite_repository.py:967 | 19 | 48.2 | 50.0 | 0.0 |
| unified_kg_store.py:399 | 1 | 0.5 | 1.0 | 0.0 |

大规模档（300k 对象，`-m slow`，`BENCH_OBJECTS` 默认值）：

| site | n | hold_max | hold_p99 | wait_max |
|---|---|---|---|---|
| sqlite_repository.py:967 | 19 | 2156.1 | 5000.0 | 0.0 |
| unified_kg_store.py:399 | 1 | 0.7 | 1.0 | 0.0 |

⚠ **这两张表是 `_caller_site` 有 facade 坍缩缺陷时测的,已知失真,不能用于
Task 8 的判定,仅保留作历史记录(不静默改写历史)。** `sqlite_repository.py:967`
是 `SQLiteRepository._write()`（`with self._runtime.database.write() as db:`
所在行）—— 每一个走 facade 注入 `write=lambda: self._write()` 的服务方法都会在
这里合并成同一个 site,因为旧版 `_caller_site(skip=3)` 的固定 `skip=3` 只在
单层 `@contextmanager` 下正确,facade 的 `_write()` 又包了一层
`@contextmanager`,双层嵌套下全部坍缩到 `_write()` 自己那一行,§2.1 的四处
候选调用点完全无法互相区分。此缺陷已修复(`_caller_site` 改为有界向外走查、
按帧类型识别包装帧,而非固定 skip 深度)。

---

**基线（已修正 —— 权威数字,Task 8 应采信这一版）：**

修正分两层,缺一都不够：

1. **归因修复**（上面失真的原因）——已修好,且默认档基准现在自带形状断言
   （`assert "?" not in sites`、no site 落在 `database.py`/`sqlite_repository.py`、
   `len(sites) >= 3`），attribution 一旦再坍缩会直接把这条回归测试测红,不会
   再悄悄退化成能通过的假绿。
2. **种子数据改造**——修归因当时用的仍是旧种子（只有 concept/claim 两种类型、
   `source_id=None`、`relations=[]`），即使归因修好了,§2.1 candidate #3/#4
   （`unified_kg_store.py:502`/`:529`,即 `replace_canonical_relations`/
   `replace_mention_bridge`）在那份种子下也只有近乎 0 的 hold_ms 可测——
   `replace_canonical_relations` 没有 relations 可折叠,
   `replace_mention_bridge` 需要「概念簇成员横跨 >=2 个来源」才会产出跨源
   别名（`knowledge_lifecycle.rebuild_mention_bridge` 的 cross 过滤,
   ~2172-2181 行）,旧种子只有一个（`None`）来源,这个前提永远不成立。现在
   的 `seed_notebook` 造 4 个来源 × 四种类型（concept/claim/formula/procedure,
   rebuild_unified_kg 对每种类型各跑一遍,~1937-1944 行）、批内对象两两连边、
   claim 文本里嵌入对应 concept 组的规范名以确定性触发 mention 匹配。

默认档(20k 对象,4 来源 × 4 类型)：

| site | n | hold_max | hold_p99 | wait_max |
|---|---|---|---|---|
| knowledge_lifecycle.py:1503 | 4 | 37.3 | 50.0 | 0.0 |
| knowledge_lifecycle.py:1586 | 4 | 29.7 | 50.0 | 0.0 |
| knowledge_lifecycle.py:2180 | 1 | 10.4 | 20.0 | 0.0 |
| knowledge_lifecycle.py:1480 | 4 | 6.7 | 10.0 | 0.0 |
| knowledge_lifecycle.py:1983 | 1 | 2.4 | 5.0 | 0.0 |
| knowledge_lifecycle.py:1958 | 1 | 2.2 | 5.0 | 0.0 |
| knowledge_lifecycle.py:2280 | 1 | 2.2 | 5.0 | 0.0 |
| knowledge_lifecycle.py:1969 | 1 | 1.9 | 2.0 | 0.0 |
| knowledge_lifecycle.py:2061 | 1 | 1.7 | 2.0 | 0.0 |
| knowledge_lifecycle.py:2285 | 1 | 0.6 | 1.0 | 0.0 |
| unified_kg_store.py:399 | 1 | 0.5 | 1.0 | 0.0 |

大规模档(300k 对象,`-m slow`,`BENCH_OBJECTS` 默认值)：

| site | n | hold_max | hold_p99 | wait_max |
|---|---|---|---|---|
| knowledge_lifecycle.py:1586 | 4 | 647.0 | 1000.0 | 0.0 |
| knowledge_lifecycle.py:1503 | 4 | 518.9 | 1000.0 | 0.0 |
| knowledge_lifecycle.py:2180 | 1 | 166.3 | 200.0 | 0.0 |
| knowledge_lifecycle.py:1480 | 4 | 48.7 | 50.0 | 0.0 |
| knowledge_lifecycle.py:1983 | 1 | 41.7 | 50.0 | 0.0 |
| knowledge_lifecycle.py:2061 | 1 | 21.1 | 50.0 | 0.0 |
| knowledge_lifecycle.py:1969 | 1 | 20.5 | 50.0 | 0.0 |
| knowledge_lifecycle.py:2280 | 1 | 17.6 | 20.0 | 0.0 |
| knowledge_lifecycle.py:1958 | 1 | 3.5 | 5.0 | 0.0 |
| unified_kg_store.py:399 | 1 | 0.7 | 1.0 | 0.0 |
| knowledge_lifecycle.py:2285 | 1 | 0.7 | 1.0 | 0.0 |

对照 §2.1 的四个候选点,现在全部可测、全部有非零 hold_ms：

- **#1** `knowledge_lifecycle.py:1503`（`_stream_seed_reps` 的 scratch 缓冲写）
  —— 300k 档 hold_max 518.9ms,本表第二高。
- **#2** `knowledge_lifecycle.py:1586`（`_write_cluster_map_streamed` 的
  `concept_clusters` 整表替换）—— 300k 档 hold_max 647.0ms,本表最高。
- **#3** `knowledge_lifecycle.py:2061`（调用
  `unified_kg_store.replace_canonical_relations`,即 §2.1 的
  `unified_kg_store.py:502`）—— 300k 档 hold_max 21.1ms。
- **#4** `knowledge_lifecycle.py:2180`（调用
  `unified_kg_store.replace_mention_bridge`,即 §2.1 的
  `unified_kg_store.py:529`）—— 300k 档 hold_max **166.3ms**,是 #3 的近 8 倍,
  本表第三高,排在 #1/#2 之后、其余所有派生层（communities 等）之前。

⚠ **这张表仍不是「全覆盖」,读表前先看这几条盲区**：

- 种子数据是合成的:同名对象固定每 8 个一组、固定横跨全部 4 个来源、别名的
  文档频率精心避开了 `mention_alias_df_cap`/`mention_alias_df_floor` 的丢弃
  阈值。真实部署的簇大小分布、跨源程度、别名泛化度大概率更不均匀,#3/#4 的
  绝对数字是「这四个候选点确实都有真实、可测量的开销」的存在性证据,不是
  生产环境的精确预测值。
- 本基准的 repo fixture 只注入 `FakeEmbedder`,不配置 LLM
  (`kg_llm_client.configured=False`)。概念簇描述生成
  (`knowledge_lifecycle.py` ~1804 起的 `_desc_ran` 分支)和 merge-review 的
  LLM 裁决路径因此在整个基准运行期间从未真正执行——这两条路径各自可能有
  自己的写锁占用,本表完全测不出来。
- `communities`(`knowledge_lifecycle.py:2280`/`:2285`)的图是从本基准的合成
  关系边构造的,边密度、社区结构都与真实部署的知识图谱相差很大,数字仅供
  量级参考。
- 表中数字来自单机单次运行,存在正常的运行间抖动(本任务内重复测同一 20k
  档,`knowledge_lifecycle.py:1503` 的 hold_max 在 33~37ms 间浮动)——量级和
  相对排序才是 Task 8 应该依赖的信号,不是精确到个位数的基准线。

**改造点 1 + bulk_write 之后（本步骤实测 —— 权威数字）：**

⚠ **跑法更正:** `pytest.ini` 的 `addopts` 是 `-n 12 --dist loadgroup`,xdist 开着时
`-s` 拿不到 worker 的 stdout —— 按 Task 8 brief 里原样的命令跑,测试会**通过但一张表
都不打印**。必须显式加 `-n0`(顺带也让计时不受 12 个并行 worker 干扰)：

```bash
cd backend && SILICON_NOTEBOOK_ENV_FILE="" python -m pytest \
  tests/test_write_lock_benchmark.py -q -m "not slow" -s -n0
cd backend && SILICON_NOTEBOOK_ENV_FILE="" BENCH_OBJECTS=500000 python -m pytest \
  tests/test_write_lock_benchmark.py -m slow -s -q -n0
```

行号已随 Task 6/7 的改动整体下移,对应关系：`:1503`→`:1553`(改造点 1)、
`:1586`→`:1625`(改造点 2)、`:2061`→`:2122`(改造点 3)、`:2180`→`:2241`(改造点 4)、
`:1480`→`:1482`。`:2040` 是 **Task 6 新增的**(见下)。

默认档(20k 对象,4 来源 × 4 类型)：

```
=== write-lock benchmark (20000 objects) ===
site                                              n   hold_max   hold_p99   wait_max
knowledge_lifecycle.py:1625                       4       27.6       50.0        0.0
knowledge_lifecycle.py:2241                       1        9.5       10.0        0.0
knowledge_lifecycle.py:1482                       4        5.9       10.0        0.0
knowledge_lifecycle.py:1553                      20        5.2       10.0        0.0
knowledge_lifecycle.py:2341                       1        2.3        5.0        0.0
knowledge_lifecycle.py:2040                       1        2.1        5.0        0.0
knowledge_lifecycle.py:2002                       1        2.0        5.0        0.0
knowledge_lifecycle.py:2013                       1        1.6        2.0        0.0
knowledge_lifecycle.py:2122                       1        1.4        2.0        0.0
knowledge_lifecycle.py:2346                       1        0.5        1.0        0.0
unified_kg_store.py:399                           1        0.5        1.0        0.0
```

大规模档(**500k 对象**,`-m slow`,`BENCH_OBJECTS=500000`;机器 12 核 / 24GB,
整跑 87s,未降档)：

```
=== write-lock benchmark (500000 objects) ===
site                                              n   hold_max   hold_p99   wait_max
knowledge_lifecycle.py:1625                       4     1240.8     2000.0        0.0
knowledge_lifecycle.py:2241                       1      296.5      500.0        0.0
knowledge_lifecycle.py:1482                       4       79.9      100.0        0.0
knowledge_lifecycle.py:2040                       1       67.2      100.0        0.0
knowledge_lifecycle.py:2013                       1       33.2       50.0        0.0
knowledge_lifecycle.py:2122                       1       33.0       50.0        0.0
knowledge_lifecycle.py:2341                       1       31.6       50.0        0.0
knowledge_lifecycle.py:1553                     500       24.6        5.0        0.0
knowledge_lifecycle.py:2002                       1        3.5        5.0        0.0
unified_kg_store.py:399                           1        0.7        1.0        0.0
knowledge_lifecycle.py:2346                       1        0.6        1.0        0.0
```

改造点 1 生效确认：`:1553` 的 n 从 4 变成 500(每批一次独立提交),300k 档
hold_max 518.9ms → 500k 档 **24.6ms**——规模涨了 1.67 倍、持锁反而降到 1/21。
`hold_p99=5.0` 且 `hold_max=24.6`:这是唯一一个 n 大到 p99 有意义的 site,
说明批量预备段整体贴在 5ms 以下。

⚠ **`hold_p99` 这一列在其余 site 上没有信息量。** `_percentile` 返回的是**桶上界**
(`write_lock_stats.py:_percentile`),而其余 site 的 n 都是 1~4 —— 「P99」等于
「最大那个样本落在哪个桶」,恒 ≥ `hold_max`。**判定一律以 `hold_max` 为准。**

**`wait_max` 恒为 0 是构造使然,不是结论。** `measure_rebuild` 单线程跑
`rebuild_unified_kg`,全程只有一个写者,不可能有人排队。这张表**不能**用来验收
§6 的交互写 `wait_ms` 门槛,见 Step 3。

- [x] **Step 3: 对照 spec §6 的门槛判定**

**先补一个基准测不出来的量：写锁在重聚类期间的时间占比。**
`wait_max=0` 让本基准对「交互写等多久」零直接证据,但 `write_lock` 现在是
**交接锁**(`threading.Lock`,PyMutex 公平期限 ~1ms),这个性质由
`backend/tests/test_bulk_write_fairness.py` 的两条确定性探针钉住：
`test_write_lock_hands_off_to_a_parked_waiter_instead_of_barging` 与
`test_the_database_actually_uses_a_handoff_write_lock`(各 5 次试验 × 200 轮,
插队次数必须为 0;实测 500/500 为 0,且**不随机器负载变化**)。

交接语义给出一条桥：**排队者在它到达时那次持锁释放的瞬间就拿到锁**,所以

> 交互写 `wait_ms` ≈ 到达瞬间那次写的剩余 `hold_ms`

于是 wait 的分布由各 site 的**时间占比**(不是调用次数)决定。为此额外测了一次带
计时的 500k 重聚类(脚本在 scratchpad,非仓库产物;工作负载复用基准自己的
`seed_notebook`,只在 `rebuild_unified_kg` 外面加了一只表,并包住 `record()` 取
精确 hold 总和 —— 快照只暴露 max 与分桶 p99,`count×max` 对 n=500 的批量 site
会高估 10 倍以上)。两次独立运行,结果稳定在 1% 以内：

| site | n | hold 总和 | 占重聚类墙钟 | hold_max |
|---|---|---|---|---|
| `:1625` concept_clusters 整表替换 | 4 | 4283ms | **19.7%** | 1375ms |
| `:1553` scratch 预备(改造点 1) | 500 | 840ms | 3.9% | 25.6ms |
| `:2241` mention 桥 | 1 | 324ms | 1.5% | 323.6ms |
| `:1482` clear_scratch(入口) | 4 | 230ms | 1.1% | 87.0ms |
| `:2040` clear_scratch(finally) | 1 | 71ms | 0.33% | 70.9ms |
| 其余 6 处合计 | 6 | 109ms | 0.5% | ≤35.3ms |
| **合计持锁** | | **5858ms** | **26.9%** | |
| **锁空闲** | | | **73.1%** | |

重聚类墙钟 21.8s。**重聚类期间写锁有 26.9% 的时间被占着,其中 73% 是那一处
`concept_clusters` 整表替换。**

把它翻成 wait 分位数(到达时刻在重聚类窗口内均匀分布 —— 这正是 §6 括号里
「并发批量任务满负荷时」的口径)。落进一段时长 d 的持锁里、偏移 u 处的到达,
等待 d−u,即在该段内 wait ~ U[0,d]：

于是「等待超过 w」的概率 = `Σ_i max(0, d_i − w) / T`,P99 就是让这个和等于
`1% × T = 218ms` 的那个 w。

- `P(wait>0) = 26.9%`;
- 超过 100ms 的到达时间质量 = `Σ max(0, d_i − 100)` = **4107ms**,
  即 **`P(wait>100ms) ≈ 18.8%`** —— P81 就已经超门槛;
- **P99 ≈ 1.0~1.2s**(四次切换等长时 1016ms;按实测 max 1375ms + 其余三次等分的
  最大离散情形 1157ms),是 100ms 门槛的 10 倍以上。

| 类别 | 门槛 | 实测 | 达标? |
|---|---|---|---|
| 交互写 `wait_ms` P99（批量满负荷时） | < 100ms | **≈1.0~1.2s**;且 `P(wait>100ms)≈18.8%`(由时间占比 + 交接锁推出,基准本身 `wait_max=0` 无直接证据) | ❌ **否** |
| 常态写 `hold_ms` | < 50ms | 重聚类内 `:1482` **87.0ms**、`:2040` **70.9ms**;摄取路径 `knowledge_lifecycle.py:253` **max 334.8ms / 均值 171.3ms** | ❌ **否**(见下方限定) |
| 批量预备段每批 `hold_ms` | < 50ms | `:1553` max **24.6ms**、p99 **5.0ms**(n=500) | ✅ **是** |
| 原子切换段 `hold_ms` | < 1s | `:1625` **1240.8ms**(三次独立运行 1240.8 / 1375.3 / 1460.2) | ❌ **否** |

三条限定,免得把上表读过头：

1. **「常态写」那一行的两半证据强度不同。** `:1482`/`:2040` 是重聚类管线内部的写,
   §6 对「常态写」的定义是「per-source 提交、上传、Ask 落库」,严格说它们哪一行都
   不属于 —— 但它们既不是每批预备写也不是原子切换,按最严的读法只能记在这一行,
   且确实超了 50ms。`knowledge_lifecycle.py:253`(`store_kg` 的对象落库,**正是**
   §6 点名的 per-source 提交)是从**种子阶段**捞出来的:基准的 `measure_rebuild`
   在重聚类前 `stats.reset()`,把整个摄取路径丢掉了,**§6 的「常态写」这一行在
   本基准的设计里根本没有被覆盖**。而它一被量出来就是 171ms 均值。⚠ 但基准用
   `_BATCH=2000` 一次 `store_kg`,真实单来源远小于此,这个数**不能**直接当生产
   per-source 提交的判决,只能说明「这一行仍然无证据,且看起来有风险」。
2. **交互写那一行是推出来的,不是量出来的。** 支撑它的是(a)交接锁的确定性探针,
   (b)实测的时间占比。端到端那条
   `test_interactive_writer_is_not_starved_by_a_bulk_writer` 确实直接量 wait 并
   断言 <100ms,但它自己的 docstring 就写明:它跑在**关掉仪器**的配置下、用的是
   单列表 + 100 行批的微负载,且在 `-n 12` 打满机器时**检出率归零**。它钉住的是
   门槛数字与粗暴回归,证不了这里的 P99。
3. **`hold_p99` 列不参与判定**(见上,n=1~4 时它就是桶上界)。

**生产会比这张表更差,每一条已知失真都朝同一个方向推：**

| 失真 | 方向 |
|---|---|
| 不配 LLM,概念簇描述(`_desc_ran` 分支)全程没执行 —— 描述文本本该写进 `concept_clusters` 行 | 切换段 payload ↑ |
| `EMBED_DIM=16` vs 生产 1024（64 倍向量字节） | 向量写 ↑ |
| 全新库里只有一个 notebook —— B 树浅、页少、`DELETE ... WHERE notebook_id=?` 几乎不扫 | 切换段 ↑ |
| 合成名字(`concept-5`)vs 真实多词规范名 + section_path | 全线 ↑ |
| 无 WAL 压力/无碎片/无并发读者/本地 NVMe | 全线 ↑ |

**规模外推**(同一段代码,Task 6/7 未触碰 `:1625`/`:2241`,300k 基线可直接比)：
`:1625` 647ms@300k → 1375ms@500k,即 ~N^1.42;部署上限 10^6 时 **≈3.7s**。
`:2241` 166ms@300k → 324ms@500k,即 ~N^1.30;10^6 时 **≈0.8s**。

⚠ 一个未拆开的混杂因素:§5.3 记的已知限制(分批提交把 Pass A2 的读游标变成长活
快照,挡住 WAL checkpoint)会让 WAL 变大,**可能**顺带抬高同一次运行里后续写的
耗时,包括 `:1625`。要干净归因得在 Task 6 之前的提交上重跑 500k。没做,因为结论
不依赖它:1240.8ms 超门槛 24%,而上面五条失真全部朝上。

**判定(对照本 Step 开头的判定规则)：**「仅原子切换段超 1s」这一支命中。交互写那一行
同时超标,但**是同一个原因导致的**,不是独立故障 —— 判定规则要求的「先查 `bulk_write`
的让路是否真的生效」这条排查已做:让路机制不但生效,而且已从按批 sleep 改成锁级交接
并有确定性守卫,`:1553` 的 24.6ms / p99 5.0ms 就是它生效的证据。所以不适用
「先查让路」那一支。

→ **改造点 2 必做**（1240.8ms > 1s，且吃掉全部持锁时间的 73%）；
**改造点 4 也做、排在 2 之后**（边际结论：只做 2 会把交互写 P99 留在 ≈106ms，
只超门槛 6%，这个 margin 比建模失真还小；支撑它的主要是 10^6 规模的外推）；
**改造点 3 不做**（35.3ms，两个数量级余量）。三者的完整推导、以及
「§6 第 1 行与第 4 行在交接锁下不可能同时成立」这一条需要用户拍板的口径冲突，
记在 **spec §5.5.1**。

- [x] **Step 4: 记录判定并提交**

```bash
git add docs/superpowers/plans/2026-07-21-sqlite-write-lock-slimming.md docs/superpowers/specs/2026-07-21-sqlite-write-lock-slimming-design.md
git commit -m "docs: record write-lock benchmark results and gate decision"
```

---

### Task 9: 收尾 —— 全量验证与 PR

**Files:** 无新增

- [ ] **Step 1: 全量检查**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/upbeat-faraday-638d23 && scripts/check.sh
```
Expected: 全绿

- [ ] **Step 2: 前端回归（本计划不改前端，作为回归）**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/upbeat-faraday-638d23/frontend && npm run build
```
Expected: build 成功

> ⚠ 严禁在 worktree 里跑 `npm install`（会写穿主 checkout 的真实依赖树）。
> `node_modules` 是指向主 checkout 的软链，由 SessionStart hook 维护。

- [ ] **Step 3: rebase 到 master 并提 PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/upbeat-faraday-638d23
git fetch origin master
git rebase origin/master
scripts/check.sh
git push -u origin HEAD
gh pr create --base master \
  --title "perf(sqlite): slim the write lock so bulk writes stop stalling pages" \
  --body "$(cat <<'EOF'
## 背景

部署环境出现 `database is locked` + 网页卡住。根因是 `SqliteDatabase.write()` 的
进程级写锁被若干长写事务长期占住 —— 跨进程表现为 `busy_timeout` 超时报错，
同进程表现为页面转圈。

设计文档：`docs/superpowers/specs/2026-07-21-sqlite-write-lock-slimming-design.md`

## 这个 PR 做了什么

- `write()` 加 wait/hold 仪器（按调用点，桶式直方图，内存 O(1)）
- 观测事件进 `events.jsonl`，新增 `scripts/diag.py locks` 离线聚合
- 合成基准（规模可调，`slow` 档跑到部署量级）
- 改造点 1：cluster-scratch 暂存改分批提交，Python 计算移出写锁
- 新增 `bulk_write()` 原语：分批提交 + 批间给等待中的交互写让路

## 这个 PR 没做什么

三处整表替换（`concept_clusters` / `canonical_relations` / `mention_edges`）
按 spec §5.5 的门槛驱动原则，由基准数字决定。**500k 档实测判定（spec §5.5.1）：
改造点 2 做、改造点 4 做（次序在 2 之后）、改造点 3 不做**，均需 schema 迁移，
另起计划，不在本 PR。

本 PR 之后仍未达标的门槛（§6 第 1/2/4 行）与一处需要用户拍板的口径冲突
（1s 的原子切换段在交接锁下与 100ms 的交互写 wait P99 互相矛盾）见 spec §5.5.1。

姊妹设计（在线 KG repair）在
`docs/superpowers/specs/2026-07-21-notebook-kg-repair-design.md`，本 PR 是它的前置条件。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec 覆盖检查：**

| Spec 章节 | 对应任务 |
|---|---|
| §5.1 仪器（wait/hold 分开、调用点、重入、动态读锁、waiters、事件输出、开关、diag 子命令、README×2） | Task 1、2、3、4 |
| §5.2 合成基准（规模可调、不调模型、slow 档、进测试） | Task 5 |
| §5.3 改造点 1（scratch 分批提交） | Task 6 |
| §5.4 `bulk_write()`（分批 + 让路 + 不在事件循环调用 + manifest 登记） | Task 7 |
| §5.5 改造点 2/3/4（门槛驱动） | Task 8 已判定 → spec §5.5.1：**2 做、4 做、3 不做**，另起计划 |
| §5.6 逃生口（generation 列，默认不做） | Task 8 判定后升级为**待用户拍板的两个选项之一**（见 spec §5.5.1）；本计划仍不实现 |
| §6 验收门槛（四档） | Task 7 Step 4 的 100ms 断言 + Task 8 Step 3 的对照表（实测 3 行未达标） |
| §7 变异验证（删除 + 移动两种，先确认改到了真代码） | Task 6 Step 6、Step 7 |
| §8 仓库验收（check.sh、frontend build、架构文档、surface manifest、README×2） | Task 4 Step 6、Task 7 Step 7、Task 9 |
| §9 与姊妹文档的关系 | Task 9 的 PR 正文 |

**已知取舍：**

- Task 8 是一个**判定门**而非实现任务。这是刻意的：spec §5.5 明确要求门槛驱动，
  预先写出可能不需要的三处整表替换代码违反 YAGNI，且那三处涉及 schema 迁移，
  应当在拿到数字后单独立计划。
- Task 2 的 `_caller_site(3)` 里的 `3` 由测试钉死而非由我断言正确 —— `@contextmanager`
  插入的帧层数随 Python 版本可能变化，让测试来定值比让计划来猜更可靠。
