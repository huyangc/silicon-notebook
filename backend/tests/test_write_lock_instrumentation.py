import threading
import time

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.services.sqlite_repository import SQLiteRepository


def _db(tmp_path, **over):
    kw = {"database_url": f"sqlite:///{tmp_path / 'db.sqlite'}",
          "storage_dir": str(tmp_path / "s")}
    kw.update(over)
    return SqliteDatabase(Settings(**kw), tmp_path)


def _repo(tmp_path, **over):
    """构造走完整 facade(SQLiteRepository)的仓库,而非直接构造 SqliteDatabase
    ——这样 with repo._write() as db: 才会经过 sqlite_repository.py 的 _write
    seam,再转发到 SqliteDatabase.write(),复现生产代码几乎全部走的两层
    @contextmanager 路径(见下面两个测试)。"""
    kw = {"database_url": f"sqlite:///{tmp_path / 'db.sqlite'}",
          "storage_dir": str(tmp_path / "s"),
          "event_log_enabled": False,
          "llm_log_enabled": False}
    kw.update(over)
    repo = SQLiteRepository(Settings(**kw))
    # 构造期的迁移会经 database.write() 记一次真实写(如 _migration_25 的凭据清退,
    # migrations.py 里 `with self.database.write()`),污染这些用例只想测的「facade
    # seam 调用点归属」。清一次,让 snapshot 只含用例自己那一次(或多次)写。
    stats = repo._runtime.database.stats
    if stats is not None:
        stats.reset()
    return repo


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
    """write() 的锁是 threading.Lock(不可重入,也不是 threading.RLock——见
    database.py 上方「进程级写锁说明」);嵌套写深度真实存在,由 write() 的
    write_depth 记账,内层重复计数会把 hold 算重。"""
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

    # daemon=True + bounded join: if the write lock ever sticks (exactly the
    # regression this file exists to catch), `_hog`'s `with db.write()` blocks
    # forever inside an unbounded `lock.acquire()`. An untimed `hog.join()`
    # would then hang this test forever instead of failing it, and — because
    # the thread was non-daemon — hang the whole `-n 12` worker at interpreter
    # shutdown with no diagnostic. With daemon=True the worker can still exit
    # even if `_hog` never finishes; the bounded join + is_alive() assertion
    # below turns the stuck-lock case into a clear failure instead of a hang.
    hog = threading.Thread(target=_hog, daemon=True)
    hog.start()
    assert started.wait(2.0)
    with db.write() as conn:
        conn.execute("INSERT INTO t VALUES (2)")
    hog.join(timeout=10.0)
    assert not hog.is_alive(), "hog 没有在 10s 内退出——写锁可能卡住了"

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


def test_exception_between_acquire_and_release_does_not_leak_the_lock(tmp_path):
    """Regression for the write() deadlock bug: anything that raises after
    lock.acquire() succeeds — here simulated via a patched exit_wait(), but in
    production a KeyboardInterrupt/MemoryError at any bytecode boundary in
    that stretch — must still release the process-wide write_lock. If it
    doesn't, every subsequent write() in the process blocks forever.

    Wraps write_lock in a _RecordingLock (same pattern as
    test_uses_the_current_write_lock_object_not_a_cached_one) so the test can
    assert the lock was genuinely *acquired* in the first place — not just
    that it looks free afterward. Without that assertion this test could pass
    vacuously: if a refactor moved exit_wait() to *before* lock.acquire(), the
    lock would never be taken at all, and "still acquirable from another
    thread" would trivially hold despite the real bug (an acquired-then-never
    -released lock) going untested.
    """
    db = _db(tmp_path)

    def _boom():
        raise RuntimeError("boom")

    db.stats.exit_wait = _boom

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

    with pytest.raises(RuntimeError):
        with db.write():
            pass  # unreachable: exit_wait() raises before the first yield

    assert calls == ["acquire", "release"], calls

    result = {}

    def _try_acquire():
        result["acquired"] = db.write_lock.acquire(timeout=0.5)
        if result["acquired"]:
            db.write_lock.release()

    other = threading.Thread(target=_try_acquire)
    other.start()
    other.join(2.0)
    assert result.get("acquired"), "write_lock leaked: still held after exit_wait() raised"


def test_record_runs_after_the_lock_is_released(tmp_path):
    """record() must be called *after* lock.release(), never from inside the
    write-lock critical section: (1) this workstream exists to shrink how
    long that lock is held, so timing the bookkeeping itself inside it would
    inflate every *other* writer's measured wait_ms with our own overhead;
    (2) a later task wires WriteLockStats' sink to the event logger, which
    does file IO on flush ticks — that IO must not run inside the global
    write lock either.

    Substitutes both write_lock and stats: write_lock becomes a wrapper that
    tracks its own held/free state (via a reentrancy-safe count, so nested
    write() calls can't falsely report "free" mid-hold), and stats.record()
    captures that state at the instant it runs. If record() ran before
    release() (the pre-fix bug), it would observe the lock as held.
    """
    db = _db(tmp_path)

    class _HeldTrackingLock:
        def __init__(self):
            self._real = threading.RLock()
            self._count = 0

        @property
        def held(self) -> bool:
            return self._count > 0

        def acquire(self, *a, **kw):
            ok = self._real.acquire(*a, **kw)
            if ok:
                self._count += 1
            return ok

        def release(self):
            self._count -= 1
            self._real.release()

    tracking_lock = _HeldTrackingLock()
    db.write_lock = tracking_lock

    held_at_record = []

    class _RecordingStats:
        def enter_wait(self):
            pass

        def exit_wait(self):
            pass

        def record(self, site, wait_ms, hold_ms):
            held_at_record.append(tracking_lock.held)

    db.stats = _RecordingStats()

    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    assert held_at_record == [False], held_at_record


def test_records_the_application_line_through_the_facade_seam_not_the_seam_itself(tmp_path):
    """回归本任务修的缺陷:几乎所有生产写调用不是直接调 db.write(),而是经
    SQLiteRepository._write()(sqlite_repository.py 的 facade 兼容层)转发
    ——中间多套一层 @contextmanager。旧实现用固定 skip 深度取帧,经这条路径
    时永远落在 967 行 seam 自己那一行,把所有真实调用者坍缩成同一个桶,仪器
    就此失去区分调用者的能力。"""
    repo = _repo(tmp_path)
    with repo._write() as db:                                  # <-- 这一行
        db.execute("CREATE TABLE t (a INTEGER)")
    expected_line = (
        test_records_the_application_line_through_the_facade_seam_not_the_seam_itself
        .__code__.co_firstlineno + 7
    )
    sites = repo._runtime.database.stats.snapshot()["sites"]
    assert len(sites) == 1, sites
    site = next(iter(sites))
    assert not site.startswith("sqlite_repository.py"), site
    assert not site.startswith("contextlib.py"), site
    assert site == f"test_write_lock_instrumentation.py:{expected_line}", site


def _open_write_site_a(repo):
    with repo._write() as db:                                  # site A
        db.execute("CREATE TABLE t (a INTEGER)")


def _open_write_site_b(repo):
    with repo._write() as db:                                  # site B
        db.execute("CREATE TABLE u (a INTEGER)")


def test_two_different_facade_callers_produce_two_different_sites(tmp_path):
    """这是四选一重构决策实际依赖的性质:仪器必须能把「经 facade 走的两个
    不同调用者」区分开,而不是把两者都记成 seam 自己的同一行——那正是本任务
    要修的缺陷会默默造成的后果(旧实现下这个断言会失败,两者都记成
    'sqlite_repository.py:967')。"""
    repo = _repo(tmp_path)
    _open_write_site_a(repo)
    _open_write_site_b(repo)
    sites = repo._runtime.database.stats.snapshot()["sites"]
    assert len(sites) == 2, sites
    for site in sites:
        assert not site.startswith("sqlite_repository.py"), sites
        assert not site.startswith("contextlib.py"), sites


def _bulk_apply(conn, batch):
    conn.executemany("INSERT INTO t (a) VALUES (?)", [(x,) for x in batch])


def _bulk_write_site_a(db):
    db.bulk_write([[1, 2]], _bulk_apply)                       # site A


def _bulk_write_site_b(db):
    db.bulk_write([[3, 4]], _bulk_apply)                       # site B


def test_two_different_bulk_write_callers_produce_two_different_sites(tmp_path):
    """回归 Task 7 的陷阱 1:bulk_write() 是一层**新**包装——它在自己的批循环里
    调用 self.write(),比 write() 本身更靠外一层。与上面
    test_two_different_facade_callers_produce_two_different_sites 修的 facade
    坍缩缺陷同一形状:不把 bulk_write 自己的帧注册为可跳过的包装帧,经它转发的
    每个调用者都会坍缩成同一个桶——bulk_write 那个 for 循环所在的
    database.py 那一行,把所有真实调用者的身份信息全部抹掉。"""
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    db.stats.reset()
    _bulk_write_site_a(db)
    _bulk_write_site_b(db)
    sites = db.stats.snapshot()["sites"]
    assert len(sites) == 2, sites
    for site in sites:
        assert not site.startswith("database.py"), sites
        assert not site.startswith("contextlib.py"), sites


def test_unresolved_sites_counter_increments_when_frame_walk_degrades(tmp_path, monkeypatch):
    """Fix 4(task 8 证据缺口修复):帧走查退化到 fallback("?")必须被计数,
    否则归因坏掉的一轮跑起来和正常一轮看着一模一样。用 _MAX_CALLER_WALK=0
    确定性地复现退化(walk 一次都不迭代,直接落到末尾的 `return "?"`)——
    不依赖某个具体栈形状,不用碰 sys._getframe 本身。"""
    from app.repositories.sqlite import database as database_module
    db = _db(tmp_path)
    monkeypatch.setattr(database_module, "_MAX_CALLER_WALK", 0)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    sites = db.stats.snapshot()["sites"]
    assert list(sites) == ["?"], sites
    assert db.stats.unresolved_sites == 1
