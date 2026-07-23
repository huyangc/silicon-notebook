import threading
import time

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase


def _db(tmp_path, *, stats: bool = True):
    # 不传 sqlite_path:它是 Settings 上由 database_url 派生的**只读 @property**,
    # 而 model_config 是 extra="ignore" —— 传进来会被静默丢弃,是个假的隔离。
    # 真正的隔离来自 conftest 的 autouse fixture(把 DATABASE_URL 指到本测试的
    # tmp_path)。
    return SqliteDatabase(
        Settings(storage_dir=str(tmp_path / "s"),
                 db_write_lock_stats_enabled=stats),
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


# --------------------------------------------------------------- 公平性


def test_bulk_write_never_sleeps(tmp_path, monkeypatch):
    """bulk_write 不得靠 sleep 让路 —— 公平性归锁管,别再回到按批 sleep。

    历史:这里曾经是「每批提交后若 ``stats.waiters > 0`` 就 sleep 2ms」。它在仪器
    开着时纯属多余、把批量吞吐打到约 1/4(实测 4s 内 7375~8288 批 → 1578~2126 批),
    在仪器关着时又因为 ``stats is None`` 而永不执行——即在唯一需要它的配置里是死
    代码。直接对 sleep 下断言,比测吞吐更稳,也更直接地钉住那个具体的回归。

    ``monkeypatch.setattr(time, "sleep", ...)`` 替换的是 ``time`` 模块的全局
    属性,对本进程(本 ``-n 12`` worker)里的**所有**线程都生效,不只是本测试
    这条主线程。``db.bulk_write()`` 是同步调用、自己不开线程,所以唯一该被盯
    的调用路径就是本测试主线程这一条——但同一 worker 进程里可能还留着其它
    测试的后台线程(例如本文件里 ``_waiter``/``_worker`` 那一类),它们若恰好在
    这个补丁生效的窗口内调用了 ``time.sleep()``,会在**另一条线程**里触发
    ``_boom`` 抛出 ``AssertionError``——既不会让本测试真正失败或变红(异常发生
    在别的线程,不会传播到 pytest 所在的主线程),又会把一次与 bulk_write 毫无
    关系的后台 sleep 错误地归因成"bulk_write 调用了 sleep"(混进 unhandled
    thread exception 的输出里,干扰排障)。按 ``threading.current_thread()``
    只拦截本测试自己线程的 sleep 调用,其余线程的 sleep 照常放行到真实实现,
    消除这个误归因窗口。
    """
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    real_sleep = time.sleep
    test_thread = threading.current_thread()

    def _boom(seconds):
        if threading.current_thread() is test_thread:
            raise AssertionError("bulk_write 调用了 time.sleep(),让路机制不该回来")
        real_sleep(seconds)  # 无关的后台线程:照常睡,不误判本测试

    monkeypatch.setattr(time, "sleep", _boom)
    assert db.bulk_write([[1]] * 200, _apply) == 200


# -------------------------------------------------------------- 写锁(公平性)

_BARGE_ROUNDS = 200
_BARGE_TRIALS = 5


def _barges_while_a_waiter_is_parked(lock, rounds=_BARGE_ROUNDS):
    """已有一个等待者驻留(parked)时,持有者连续「释放 → 立刻抢回」能赢几次。

    barging 锁(RLock):release() 只是发个信号,等待者还得被 OS 唤醒才能去抢
    互斥量,而持有者此刻就在 CPU 上,几乎每次都赢 → 返回 rounds。
    有交接(handoff)的锁(threading.Lock):等待时间超过 PyMutex 的公平期限
    (约 1ms)后,release() 会把锁**直接交给**等待者,持有者的下一次 acquire
    立刻失败 → 返回 0。
    """
    parked = threading.Event()
    got = threading.Event()
    lock.acquire()

    def _waiter():
        parked.set()
        lock.acquire()          # 驻留:主线程正持有
        got.set()
        lock.release()

    # daemon=True:如果下面 `assert got.wait(timeout=30)` 真的超时(比如某次
    # 回归真把交接语义拆掉了,这条线程会永远堵在 lock.acquire() 里),assert
    # 失败会直接抛,跳过再往下的 t.join(timeout=10)——线程留在原地。非 daemon
    # 线程会让解释器退出时干等它一辈子;在本仓库 `-n 12` 的并行 worker 下,这会
    # 把"一条测试失败"变成"一整个 worker 卡死退不出"。daemon=True 后,即使
    # 真的卡住,worker 进程仍能在会话结束时正常退出——不影响这条 assert 该
    # 失败时照样失败,只是不再连累其它测试。
    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    assert parked.wait(timeout=10), "等待线程没起来"
    # ⚠ parked.set() 在 lock.acquire() 之前:parked.wait() 返回只证明等待线程
    # 「已经跑起来、执行到了 parked.set() 这一行」,不证明它已经真正调用
    # lock.acquire() 并进入内核态阻塞——这两者之间还有一条语句边界的调度缝隙
    # (Event.set() 内部唤醒等待者时甚至可能先把主线程切回去跑,等待线程自己
    # 还没来得及往下走一行)。这个缝隙无法从纯 Python 层面彻底关闭:一条线程
    # 一旦真正阻塞在 acquire() 里,它本身就没法再执行代码来"汇报"这件事——能
    # 汇报的只有"我马上要调用它了",不可能是"我已经在里面了"。下面这行
    # sleep(0.1) 因此身兼两职,不只是「等过 PyMutex ~1ms 公平期限」:它同时是
    # 唯一实际弥合"线程已启动"到"线程真正进入阻塞"这段调度缝隙的手段。100ms
    # 相对两者(微秒级调度延迟、~1ms 公平期限)都留了两个数量级以上的余量,
    # 缩短它会让这条缝隙的漏判风险回升,所以没有动这个数字。
    time.sleep(0.1)

    barges = 0
    for _ in range(rounds):
        lock.release()
        if not lock.acquire(blocking=False):
            break               # 等待者拿到了 → 发生了交接
        barges += 1
    else:
        lock.release()          # 一次都没交接出去,放行等待者

    assert got.wait(timeout=30), "等待线程始终没拿到锁"
    t.join(timeout=10)
    return barges


def test_write_lock_hands_off_to_a_parked_waiter_instead_of_barging():
    """机制守卫(确定性):写锁必须把锁交接给驻留的等待者,而不是让持有者抢回。

    Fix 3:这条测试是本项目"批量写满负荷时,交互写不会被饿死"这条验收口径**唯一**
    的证据来源。这里曾经还有一条按墙钟时间断言的端到端测试
    (``test_interactive_writer_is_not_starved_by_a_bulk_writer``,断言交互写最坏
    等待 <100ms),已删除而不是保留或放宽阈值,理由是实测出来的,不是推测:

    - 该测试自己的 docstring 记录过:仪器关闭、``-n 12``(本仓库 pytest.ini 固定
      的并行度)把 12 核打满后重测,每组 15 次重复,**正确实现**(threading.Lock)
      与**已知有饿死缺陷的实现**(threading.RLock)全都是 0/15 报错——检出率对
      两边同时归零。也就是说在这条测试实际跑的负载下,它通过与否**不携带任何
      关于代码对错的信息**;继续留着它,只是白付运行时间(该测试本身单次跑最长
      可达 20s)和真实的 flake 风险,换不到任何信号。
    - 根因是负载,不是阈值取值,所以"调阈值"救不了它:饿死这个现象需要批量线程
      能够**不被抢占地**连续跑;``-n 12`` 打满 12 核后,批量线程本身也频繁被
      OS 切走,排队的交互写因此白捡到本不该有的机会——不管 100ms 门槛改成
      10ms 还是 1000ms,同一个"批量写者其实没有连续占着 CPU"的前提都不成立,
      检出力不会因为改阈值而回来。这正是本任务被要求"不能只是放宽阈值"的原因:
      放宽只是把同一个已经测不出东西的断言换一个更松的数字,检出率还是 0。
    - 空载环境下那条测试确实有检出力(每组 20 次重复:正确实现 20/20 通过、
      RLock 19/20 报错),但本仓库的 pytest 没有"只在空载环境跑这条测试"的执行
      路径(``pytest.ini`` 固定 ``-n 12``),所以那份检出力在实际验收流程里从未
      真正生效过。

    下面这条 barge 探针不受上述问题影响:它断言的是一个结构性的布尔性质(锁被
    交接给驻留的等待者,还是被持有者抢回去了),不依赖"墙钟时间是否超过某个
    阈值",因此也不依赖持有者/等待者谁被调度器打断、机器有多满。这是本文件里
    唯一一条**不靠时间阈值、也不靠罕见竞态**的测试,所以它是「换回
    threading.RLock」这个变异的主检出器 —— 也是唯一一条在 ``-n 12`` 打满机器时
    仍然有检出力的。实测:

                        空载              12 个 CPU 燃烧器
        threading.Lock  0 次插队 300/300   0 次插队 200/200
        threading.RLock 中位 17 次插队,    中位 36 次插队,
                        2.0% 的试验为 0    2.5% 的试验为 0

    公平锁那侧是构造保证的(PyMutex 的交接),两种负载下合计 500/500 恰好为 0;
    RLock 那侧偶尔为 0(等待者赢下唤醒竞速)。所以取 5 次独立试验的最大值,漏检
    概率 0.025^5 ≈ 1e-8,且这个数**不随机器负载变化**。
    """
    worst = max(_barges_while_a_waiter_is_parked(threading.Lock())
                for _ in range(_BARGE_TRIALS))
    assert worst == 0, (
        f"写锁在有等待者驻留时仍被持有者抢回 {worst}/{_BARGE_ROUNDS} 次 —— "
        "这把锁没有交接语义,批量写者会把交互写饿死(见 _FairWriteLock 的 docstring)")


def test_the_database_actually_uses_a_handoff_write_lock(tmp_path):
    """上面那条测的是 threading.Lock 这个类型;这条钉住 SqliteDatabase 真的在用它。

    两条分开写:只测类型,换回 `self.write_lock = threading.RLock()` 时类型测试
    照样绿;只测数据库,失败信息又指不到具体是哪个性质坏了。

    ⚠ 这里同样要取 _BARGE_TRIALS 次试验的最大值,不能只跑一次:单次试验对 RLock
    有 2~2.5% 的漏检率,实测就在第一次变异验证里真的漏过一次(类型那半边绿了、
    单次插队探针也恰好返回 0)。
    """
    db = _db(tmp_path)
    # 类型断言按 threading.Lock() 的**实际类型**(_thread.lock)比,不能用
    # isinstance(x, threading.Lock) —— threading.Lock 是工厂函数,不是类。
    assert isinstance(db.write_lock, type(threading.Lock())), type(db.write_lock)
    worst = max(_barges_while_a_waiter_is_parked(db.write_lock)
                for _ in range(_BARGE_TRIALS))
    assert worst == 0, f"SqliteDatabase 的写锁在有等待者时被抢回 {worst} 次"


def _try_from_other_thread(lk, timeout):
    """在另一条线程里试着拿锁;拿到就立刻还回去。返回是否拿到。"""
    got = []

    def _run():
        ok = lk.acquire(timeout=timeout)
        got.append(ok)
        if ok:
            lk.release()

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert got, "探测线程没有在 10s 内返回"
    return got[0]




def test_write_lock_acquire_accepts_timeout_and_non_blocking_keywords(tmp_path):
    """写锁的调用约定:``acquire(timeout=...)`` 与 ``acquire(blocking=False)``
    都必须可用 —— 测试代码真的这么调(见 test_notebook_share_copy.py 里
    ``repo._write_lock.acquire(timeout=3)``),换锁实现时别把这个约定弄丢。"""
    db = _db(tmp_path)
    lk = db.write_lock
    lk.acquire()
    try:
        t0 = time.perf_counter()
        assert _try_from_other_thread(lk, 0.05) is False
        assert time.perf_counter() - t0 >= 0.04, "timeout 没有真的等"

        got = []
        t = threading.Thread(target=lambda: got.append(lk.acquire(blocking=False)))
        t.start()
        t.join(timeout=10)
        assert got == [False]
    finally:
        lk.release()
    assert _try_from_other_thread(lk, 1.0) is True


# --------------------------- 重入记账在 write() 里,不在锁里(Defect 1 的根治)

def _write_depth(db) -> int:
    return getattr(db._local, "write_depth", 0)


def test_nested_write_takes_the_process_lock_exactly_once(tmp_path):
    """写锁不可重入,重入由 write() 的 thread-local ``write_depth`` 记账:只有
    最外层 acquire/release,内层完全不碰锁。

    这条直接钉住那个纪律。如果谁把它改回"每层都 acquire",这把
    ``threading.Lock`` 会在内层 write() 上**自己把自己锁死**(不可重入),测试
    会挂在那里而不是悄悄退化;如果谁让内层也 release,锁会在最外层还没退出时
    就被放掉 —— 下面那条测试负责抓这一种。
    """
    db = _db(tmp_path)
    calls = []
    real = threading.Lock()

    class _RecordingLock:
        def acquire(self, *a, **kw):
            calls.append("acquire")
            return real.acquire(*a, **kw)

        def release(self):
            calls.append("release")
            return real.release()

    db.write_lock = _RecordingLock()
    with db.write() as outer:
        outer.execute("CREATE TABLE t (a INTEGER)")
        with db.write() as inner:
            inner.execute("CREATE TABLE u (a INTEGER)")
    assert calls == ["acquire", "release"], calls


def test_write_lock_stays_held_until_the_outermost_write_exits(tmp_path):
    """内层 write() 退出后锁必须**仍然被持有**,只有最外层退出才真正释放 ——
    与旧的可重入锁语义等价。内层若提前放锁,另一线程就能在外层事务还没提交时
    插进来写。"""
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    with db.write():
        with db.write():
            assert _try_from_other_thread(db.write_lock, 0.05) is False
        # 内层已退出,外层还在 —— 锁必须还握着
        assert _try_from_other_thread(db.write_lock, 0.05) is False
        assert _write_depth(db) == 1
    assert _write_depth(db) == 0
    assert _try_from_other_thread(db.write_lock, 1.0) is True


def test_exception_inside_write_releases_the_lock_and_restores_depth(tmp_path):
    """回归 Defect 1(旧 ``_FairWriteLock`` 的"两个持有者"损坏)。

    旧实现在 release() 里用 ``try: self._owner = None; finally:
    self._lock.release()`` 做补救。清 owner 一旦被打断,finally 仍会**无条件
    放掉底层锁**,留下"锁空闲、``_owner`` 仍指向本线程"的状态:本线程下一次
    acquire() 走重入快路径"成功"却什么都没持有,而另一线程同时能拿到那把空闲
    的锁 —— 两个线程同时进入写临界区。旧测试只断言"另一线程拿得到锁",而那
    在损坏状态下恰好**成立**,所以它一直是绿的。

    换成 ``threading.Lock`` 后这个状态在结构上就不存在了(没有 owner 可失步)。
    这条测试钉住等价的可观察不变量:异常穿过 write() 之后,锁必须真的空闲、
    深度必须归零,而且本线程的下一次 write() 必须**真的重新拿锁**(而不是走
    某种"以为自己还持有"的快路径)。
    """
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    with pytest.raises(RuntimeError):
        with db.write() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")

    assert _write_depth(db) == 0, "write_depth 没有恢复,后续写会被误判成嵌套而不加锁"
    assert _try_from_other_thread(db.write_lock, 1.0) is True, "锁被泄漏了"

    # 本线程的下一次 write() 必须真的重新 acquire:用一个记账锁验证,而不是
    # 只看"没抛异常"——旧缺陷下这里恰恰是"不抛异常但没拿锁"。
    calls = []
    real = threading.Lock()

    class _RecordingLock:
        def acquire(self, *a, **kw):
            calls.append("acquire")
            return real.acquire(*a, **kw)

        def release(self):
            calls.append("release")
            return real.release()

    db.write_lock = _RecordingLock()
    with db.write() as conn:
        conn.execute("INSERT INTO t VALUES (2)")
    assert calls == ["acquire", "release"], calls


def test_two_threads_are_never_both_inside_the_write_section(tmp_path):
    """Defect 1 的端到端形态:并发写者互斥必须成立。旧缺陷的可观察后果就是
    两个线程同时在写临界区里;这里用一个"临界区内计数器"直接抓它。"""
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    inside = []
    overlaps = []
    guard = threading.Lock()

    def _worker(n):
        for i in range(25):
            with db.write() as conn:
                with guard:
                    inside.append(n)
                    if len(inside) > 1:
                        overlaps.append(list(inside))
                conn.execute("INSERT INTO t VALUES (?)", (i,))
                with guard:
                    inside.remove(n)

    # daemon=True: same rationale as _waiter above in
    # _barges_while_a_waiter_is_parked — each _worker loops on `db.write()`,
    # which blocks forever in an unbounded `lock.acquire()` if the write lock
    # ever sticks. The join below is already bounded (timeout=60) and already
    # asserts the threads finished, but that only fails *this* test; a stuck,
    # non-daemon thread would still hang the whole `-n 12` worker at
    # interpreter shutdown afterward. daemon=True lets the worker exit even
    # then, without weakening the assertion below.
    threads = [threading.Thread(target=_worker, args=(n,), daemon=True)
               for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "写线程没有在 60s 内退出"
    assert overlaps == [], f"有 {len(overlaps)} 次两个线程同时在写临界区内"


class _LocalWhoseRestoreFails:
    """``threading.local()`` 的替身:把 write_depth **恢复**成 0 的那一次赋值
    改成抛异常,模拟异步异常恰好落在 write() 收尾处那条语句上。

    只让"恢复"那一次抛(depth 1→0),进入时的 0→1 照常,这样才测得到收尾路径。
    """

    def __init__(self):
        object.__setattr__(self, "_vals", {})

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_vals")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        vals = object.__getattribute__(self, "_vals")
        if name == "write_depth" and value == 0 and vals.get("write_depth") == 1:
            raise KeyboardInterrupt()  # 异步异常落在"恢复深度"这条语句上
        vals[name] = value


def test_interrupted_depth_restore_never_frees_the_lock(tmp_path):
    """不变量:**绝不在本线程仍自认处于写块内时放掉写锁。**

    这是 Defect 1 在新设计里的等价形态。write() 收尾要做两件事:恢复
    thread-local 的 ``write_depth``、以及放锁。如果这两件事分处不同的 finally
    (恢复在内层),那么恢复一旦被异步异常打断,外层 finally 仍会照常放锁 ——
    留下"锁空闲、而本线程 write_depth 仍 >0"的状态。该线程下一次 write() 会
    因为 depth>0 跳过 acquire、在**没有锁**的情况下写,而另一线程同时能拿到那
    把空闲的锁:两个写者同时进入临界区,和旧 _FairWriteLock 的 owner/count
    失步是同一类静默损坏。

    正确的写法是两者同处一个 finally、先恢复深度再放锁:恢复被打断时
    ``lock.release()`` 根本不会执行,最坏退化成"锁继续锁着"(可观测的停顿),
    而不是静默并发写。
    """
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    db._local = _LocalWhoseRestoreFails()
    with pytest.raises(KeyboardInterrupt):
        with db.write() as conn:
            conn.execute("INSERT INTO t VALUES (1)")

    believes_inside = getattr(db._local, "write_depth", 0) > 0
    lock_is_free = _try_from_other_thread(db.write_lock, 0.2)
    assert not (lock_is_free and believes_inside), (
        "写锁被放掉了,但本线程的 write_depth 仍 >0 —— 它的下一次 write() 会"
        "不加锁就写,而别的线程已经能拿到锁:两个写者同时在临界区内")
