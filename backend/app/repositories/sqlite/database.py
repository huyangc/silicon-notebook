from __future__ import annotations

import sqlite3
import sys
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Iterator

from app.core.config import Settings
from app.core import diagnostics_runtime as diagnostics
from app.repositories.scale_build_lock import UNSUPPORTED_SCALE_BUILD_LOCK
from app.repositories.sqlite.write_lock_stats import WriteLockStats


_FETCHMANY_OMITTED = object()


class _DiagnosticCursor(sqlite3.Cursor):
    """SQLite cursor that retains sanitized statement identity, never values."""

    _diag_sql: diagnostics.SqlMetadata | None = None
    _diag_mode = "read"
    _diag_operation = "sqlite.read"

    def _remember(self, sql: Any) -> None:
        try:
            self._diag_sql = diagnostics.normalize_sql_metadata(sql)
        except Exception:
            self._diag_sql = None

    def _scope(self):
        metadata = self._diag_sql
        if metadata is None:
            return nullcontext()
        return diagnostics.sql_scope(
            self._diag_mode,
            metadata,
            self._diag_operation,
        )

    def execute(self, sql, parameters=(), /):
        self._remember(sql)
        with diagnostics.sql_scope(self._diag_mode, sql, self._diag_operation):
            return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):
        self._remember(sql)
        with diagnostics.sql_scope(self._diag_mode, sql, self._diag_operation):
            return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script, /):
        self._remember(sql_script)
        with diagnostics.sql_scope(
            self._diag_mode, sql_script, self._diag_operation
        ):
            return super().executescript(sql_script)

    def fetchone(self, /):
        with self._scope():
            return super().fetchone()

    def fetchmany(self, /, size=_FETCHMANY_OMITTED):
        with self._scope():
            if size is _FETCHMANY_OMITTED:
                return super().fetchmany()
            return super().fetchmany(size)

    def fetchall(self, /):
        with self._scope():
            return super().fetchall()

    def __next__(self, /):
        with self._scope():
            return super().__next__()

    def close(self, /):
        try:
            return super().close()
        finally:
            self._diag_sql = None


class _Conn(sqlite3.Connection):
    """复用连接 + 嵌套事务守卫。

    一条 thread-local 连接会被同线程内多处 ``with connect() as db:`` 复用。
    sqlite3.Connection 原生 ``with`` 在每次 ``__exit__`` 都 commit/rollback,
    嵌套时内层会提前提交外层未完成的写。本子类用深度计数,使**只有最外层**
    ``with`` 才 commit(无异常)/rollback(任一层异常),从而保持"每个 with 块=
    一个逻辑事务边界"的原语义,让 233 处调用点零改动、语义等价。

    不 override __init__(用 getattr 惰性属性),以兼容 sqlite3.connect(factory=)。

    ⚠ 写务必走 SqliteDatabase.write():本守卫使嵌套 `with` 只最外层提交,且内层异常会
    污染最外层事务(整体 rollback)。因此**不得在 connect() 返回的复用(读)连接上直接执行
    写并在外层捕获内层异常**——会静默丢写 / 破坏增量提交(INV-8)。生产中复用连接实际只读,
    所有写经 write()(独立连接、每次独立提交)。
    """

    _diag_mode = "read"
    _diag_operation = "sqlite.read"

    def cursor(self, factory=None):
        cursor_factory = _DiagnosticCursor if factory is None else factory
        cursor = super().cursor(cursor_factory)
        if isinstance(cursor, _DiagnosticCursor):
            cursor._diag_mode = self._diag_mode
            cursor._diag_operation = self._diag_operation
            cursor._diag_sql = None
        return cursor

    def execute(self, sql, parameters=(), /):
        cursor = self.cursor()
        return cursor.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):
        cursor = self.cursor()
        return cursor.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script, /):
        with diagnostics.sql_scope(
            self._diag_mode,
            sql_script,
            self._diag_operation,
        ):
            return super().executescript(sql_script)

    def __enter__(self) -> "_Conn":
        self._txn_depth = getattr(self, "_txn_depth", 0) + 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        depth = getattr(self, "_txn_depth", 1) - 1
        self._txn_depth = depth
        if exc_type is not None:
            self._txn_failed = True
        if depth <= 0:
            self._txn_depth = 0
            failed = getattr(self, "_txn_failed", False)
            self._txn_failed = False
            if failed:
                self.rollback()
            else:
                self.commit()
        return False  # 不吞异常(与 sqlite3.Connection.__exit__ 一致)


# ------------------------------------------------------------ 进程级写锁说明
#
# ``SqliteDatabase.write_lock`` 是一把**普通的 ``threading.Lock``**(在
# ``__init__`` 里创建),既不是 ``threading.RLock``,也不再是本模块曾经自建的
# 可重入锁 ``_FairWriteLock``。下面三件事必须一起看,少看一件就会有人"顺手"
# 改回去。
#
# 一、为什么不能用 ``threading.RLock``
#
# 它(``_thread.RLock``)是纯 barging 锁:release() 只是唤醒等待者,等待者仍要
# 和释放线程**抢**同一把互斥量;而释放线程此刻正在 CPU 上跑。于是一个
# ``release(); <几条字节码>; acquire()`` 的循环几乎每次都赢 —— 批量写者把交互
# 写饿死。实测(CPython 3.13.11, macOS/arm64;每组 3 次重复,单等待者 + 紧循环
# 持有者):
#
#     threading.RLock  hold=500us gap=0 : 最坏等待 8.0s / 10.9s / 90.1s
#     threading.RLock  hold=2ms   gap=0 : 最坏等待 198.2s / 50.2s / 38.9s
#     threading.Lock   hold=2ms   gap=0 : 最坏等待 8.7ms / 9.0ms / 9.0ms
#
# 差别不在 OS,在 CPython:``threading.Lock`` 是 ``_thread.lock``,底层
# ``PyMutex`` 实现了 **eventual fairness** —— 等待超过约 1ms 的 waiter 在
# release 时被**直接交接**(handoff),不再参与抢。
#
# ⚠ 不要"顺手"改回 RLock:公平性是 ``write()`` 唯一的防饿死机制,而默认配置下
# RLock 的不公平**测不出来** —— 写锁仪器(``_caller_site()`` 栈行走 + ``stats``
# 的两次加锁)恰好落在释放与重新获取之间,顺带充当了让路点。关掉
# ``DB_WRITE_LOCK_STATS`` 这层保护就消失,交互写会被批量写饿到秒级。守卫是
# tests/test_bulk_write_fairness.py 里那条确定性的"插队"探针。
#
# ⚠ 这份公平性是 CPython **≥3.13** 的实现细节,不是 ``threading`` 模块的文档
# 承诺。3.13 之前 ``threading.Lock`` 由更老的信号量式实现支撑,没有交接语义,
# 和 RLock 一样会 barge。``docs/deployment-and-configuration.md`` / ``_zh.md``
# 的 Python Environment/环境一节已把支持下限提到 Python 3.13+，原因正是这份
# 依赖；这里没有做成运行时断言，这段注释是这份依赖最详细的记录处。
#
# 二、可重入性不在锁里,在 ``write()`` 里
#
# 这把锁**不可重入**,但同线程嵌套 ``write()`` 是真实存在的。``write()`` 用
# thread-local 的 ``write_depth`` 记账:只有 ``depth == 0``(最外层)才
# acquire/release,内层只加减深度、完全不碰锁。于是"锁从最外层 acquire 一直
# 持有到最外层 release"这个可观察语义,与旧的可重入锁完全一致 —— 而**不需要**
# 在 Python 层维护 owner/count。全库没有第二个地方直接 acquire 这把锁(所有写
# 都经 ``write()``),所以这个记账覆盖了全部重入来源。
#
# 三、为什么这很重要(删掉 ``_FairWriteLock`` 的全部理由)
#
# 一旦在 Python 层维护 owner/count,就必须在"拿到底层锁"与"写好 owner/count"
# 之间、以及"清掉 owner"与"放掉底层锁"之间保持一致 —— 而这两处都跨越字节码
# 边界,异步异常(Ctrl-C 的 KeyboardInterrupt)能落进去。旧实现在 release() 里
# 用 ``try: self._owner = None; finally: self._lock.release()`` 做"补救",结果
# 反而更糟:清 owner 那一步一旦被打断,finally 仍会**无条件放掉底层锁**,留下
# "底层锁空闲、``_owner`` 仍指向某线程"的状态 —— 该线程下一次 acquire() 走重入
# 快路径"成功"却什么都没持有,同时另一线程能拿到空闲的底层锁,于是**两个线程
# 同时进入写临界区**(静默的并发写损坏)。这不是推演:该状态可稳定复现,并已
# 端到端演示出两个持有者。
#
# ``threading.Lock`` 没有 owner 概念,acquire/release 各是**一次 C 调用**,没有
# 任何 Python 层记账可以失步 —— 这一整类缺陷从结构上消失了。任何线程都能
# release 它这件事在这里不构成弱化:``write()`` 是唯一的 acquire 者,且总在自己
# 的 ``finally`` 里、同一条线程上 release。
#
# 四、残留窗口(如实说明,纯 Python 关不掉)
#
# CPython 在字节码边界检查待处理信号,而"``lock.acquire()`` 返回"与"进入那个
# 负责 release 的 try"之间必然隔着一个边界。异步异常正好落在这里,底层锁就会
# 停在"锁着"且再没有人会去放它 —— 进程级写死锁。实测(真 SIGALRM→
# KeyboardInterrupt,竞争态 acquire):
#
#     旧 _FairWriteLock : 750 次落在函数内的中断中,251 次留下不可恢复的锁死
#     现 threading.Lock : 717 次中 207 次
#
# 两者同量级 —— 这个窗口不是本次改动引入的,也没有被本次改动放大;它是"acquire
# 之后、try 之前"那条语句边界的固有属性。除非用 C 扩展(一段不释放 GIL 的临界
# 区)或直接屏蔽信号,纯 Python 做不到把"拿锁"和"装好 finally"变成一个原子步。
# 接受它的理由:① 它退化成"锁死"(进程停住、可观测、栈一看就知道卡在哪),而
# 不是"两个写者同时提交"(静默损坏);② 本应用里唯一现实的异步异常来源是
# SIGINT,而收到 SIGINT 就意味着进程正在退出,此后不再有写者需要这把锁。


_MAX_CALLER_WALK = 20  # 安全阀:真实栈深约 4~6 帧,给足余量防栈形状意外时死循环


def _is_contextlib_frame(frame) -> bool:
    """contextlib 的 @contextmanager 机制帧(_GeneratorContextManager.__enter__
    等)。按模块名判断——真实调用者与 write() 之间,以及 facade 的 _write()
    与它自己的调用者之间,每层 `with` 都会各插入一帧。"""
    return frame.f_globals.get("__name__") == "contextlib"


def _is_write_frame(frame) -> bool:
    """SqliteDatabase.write() 自己的生成器帧。_WRITE_GENERATOR_CODE 定义在本
    模块下方(类体之后);此处延迟到调用时才解析该全局名——_caller_site 只会
    在 write() 运行期间被调用,那时全模块早已加载完毕,定义顺序无关。"""
    return frame.f_code is _WRITE_GENERATOR_CODE


def _is_facade_write_seam_frame(frame) -> bool:
    """RepositoryFacade._write()(app/services/repository_facade.py)的生成器
    帧——facade 兼容层,几乎所有应用代码经它转发到 SqliteDatabase.write(),
    是调用者与这里之间插入的第二层 @contextmanager。不跳过它,记录到的调用点
    会永远是它自己的那一行(seam 的 yield),所有真实调用者都会坍缩成同一个桶。

    只精确匹配这一个帧(文件名 + 限定名),不是整文件跳过:repository_facade.py
    里别的方法即便也开 database.write(),它们各有自己的函数名/行号,不会被这里
    误判、错记成它们各自调用者的行——因为下面按 co_qualname 精确锁定
    ``RepositoryFacade._write`` 这一个方法。

    不对 repository_facade 模块做 import 来比较 code 对象:该模块(经
    repository_runtime)本就 import 这个模块,反向 import 会成环,也会倒转
    这个代码库别处强制的 repositories/services 分层。改用运行时直接从帧自身
    取 filename + co_qualname(3.11+;更老版本没有该属性时退化到 co_name),
    不需要跨模块引用。
    """
    code = frame.f_code
    if Path(code.co_filename).name != "repository_facade.py":
        return False
    qualname = getattr(code, "co_qualname", None)
    if qualname is not None:
        return qualname == "RepositoryFacade._write"
    return code.co_name == "_write"


def _is_bulk_write_frame(frame) -> bool:
    """SqliteDatabase.bulk_write() 自己的帧(Task 7)。bulk_write 是一层**新**
    包装:它在自己的批循环里调用 self.write(),比 write() 本身更靠外一层——
    与 _is_facade_write_seam_frame 修的 facade 坍缩缺陷同一形状,只是包装层
    从 SQLiteRepository._write() 换成了 bulk_write 自己。不跳过这一帧,经
    bulk_write 转发的每一个调用者都会坍缩成同一个桶:bulk_write 那个
    `with self.write() as conn:` 循环所在的那一行(database.py:<该行>),
    把所有真实调用者的身份信息全部抹掉——这正是本任务要求必须堵住的坑
    (已经用四个任务的时间在 facade 那层验证过一次,不能在这里重演)。

    bulk_write 是普通方法(没有 @contextmanager),所以按 code 对象身份匹配
    即可(_BULK_WRITE_CODE,定义在类体之后,理由与 _WRITE_GENERATOR_CODE
    相同:必须等类属性真正存在才能取到 .__code__)。"""
    return frame.f_code is _BULK_WRITE_CODE


def _caller_site() -> str:
    """返回 `filename:lineno` 形式的调用点——应用代码里真正打开写块的那一行。

    用 sys._getframe 逐帧走栈而非 traceback.extract_stack —— 后者会格式化整个
    栈,相对一次 DB 写是不可忽略的开销;前者只做帧间的等值比较,常数级。

    起点是本函数的直接调用者,即 write() 自己的生成器帧(frame 1;frame 0 是
    _caller_site 自己)。从那里向外走,跳过已知的"包装帧":
      - contextlib 的 @contextmanager 机制帧;
      - write() 自己的生成器帧;
      - facade 兼容层 SQLiteRepository._write() 的生成器帧;
      - bulk_write() 自己的帧(Task 7 新增的第二层包装,见
        _is_bulk_write_frame——bulk_write 在自己的批循环里调用 self.write(),
        不跳过它,经它转发的每个调用者都会坍缩成 bulk_write 那一行)。
    第一个不属于上述四类的帧,就是真正打开写块的调用点——无论调用方是直接
    调 db.write()(帧序里只有一层 contextlib),经 facade 的 _write() 转发
    (帧序里 write→contextlib→_write→contextlib→调用者,两层 contextlib 夹着
    seam 那一帧),还是经 bulk_write() 转发(帧序里 write→contextlib→
    bulk_write→调用者,bulk_write 自己不是生成器,它和它的调用者之间不再多
    一层 contextlib),都能落到同一条真实调用行上。

    walk 有界(_MAX_CALLER_WALK)防止栈形状意外(例如未来再套一层无法识别的
    包装)时死循环;栈比预期浅时 sys._getframe 抛 ValueError,与走到上限一样,
    退化返回 "?"(与原实现一致——宁可如实报告"测不出",不要把包装帧的行误记
    成调用点)。
    """
    depth = 1
    for _ in range(_MAX_CALLER_WALK):
        try:
            frame = sys._getframe(depth)
        except ValueError:
            return "?"
        if (
            _is_contextlib_frame(frame)
            or _is_write_frame(frame)
            or _is_facade_write_seam_frame(frame)
            or _is_bulk_write_frame(frame)
        ):
            depth += 1
            continue
        name = Path(frame.f_code.co_filename).name
        return f"{name}:{frame.f_lineno}"
    return "?"


class SqliteDatabase:
    """进程内 SQLite 连接来源。**每线程复用一条连接**(threading.local),而非每次
    新建——把 fd 用量从 O(操作数) 降到 O(线程数),并省掉每操作反复建连接/PRAGMA/
    mmap 的开销。连接为 _Conn(嵌套事务守卫)。写仍经 write()+write_lock 串行。"""

    def __init__(self, settings: Settings, root_dir: Path) -> None:
        self.settings = settings
        self.root_dir = root_dir
        self.db_path = self.resolve_path(settings.sqlite_path)
        # 见本模块上方「进程级写锁说明」:必须是 threading.Lock(公平/有交接),
        # 不可重入 —— 重入由 write() 的 write_depth 记账,不是锁的职责。
        self.write_lock = threading.Lock()
        self._local = threading.local()
        self.stats: WriteLockStats | None = (
            WriteLockStats(
                warn_ms=settings.db_write_lock_warn_ms,
                flush_interval_s=settings.db_write_lock_flush_seconds,
            )
            if settings.db_write_lock_stats_enabled
            else None
        )

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    def _new_connection(
        self,
        mode: str = "read",
        operation: str = "sqlite.read",
    ) -> _Conn:
        pending = getattr(self._local, "new_connection_diagnostics", None)
        if pending is not None:
            mode, operation = pending
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.settings.db_busy_timeout_ms / 1000,
            factory=_Conn,
            check_same_thread=True,  # 显式:连接不跨线程(threading.local 保证)
        )
        conn._diag_mode = mode
        conn._diag_operation = operation
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {int(self.settings.db_busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA cache_size = {int(self.settings.sqlite_cache_size_kb)}")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        return conn

    def connect(self) -> sqlite3.Connection:
        """返回本线程复用的连接(首次懒建)。返回真 _Conn 对象,故 233 处
        `with connect() as db:` 与裸 `conn = connect()` 均零改动。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def is_connection_held(self) -> bool:
        """Whether this execution is inside a live read/write DB boundary."""
        conn = getattr(self._local, "conn", None)
        return bool(
            getattr(self._local, "write_depth", 0) > 0
            or (conn is not None and getattr(conn, "_txn_depth", 0) > 0)
        )

    @property
    def in_write_transaction(self) -> bool:
        """本线程此刻是否处在 ``write()`` 块内(含嵌套)。

        给「这条读路径绝不能在写事务里跑」这类调用契约当**运行时**守卫用,而不是只写
        在注释里。为什么必须是 thread-local 的 ``write_depth`` 而不是连接上的
        ``in_transaction``:``write()`` 每次另开一条独立连接,所以本线程复用的**读**
        连接在写事务期间 ``in_transaction`` 仍是 False —— 用它判等于没判。

        只读一个 thread-local 属性,没有锁、没有 IO,可以放在热路径上。
        """
        return getattr(self._local, "write_depth", 0) > 0

    def close_local(self) -> None:
        """关闭并清除**当前线程**的复用连接。用于:
        - 需靠 close 清理临时表的路径(如 mention-alias DF 扫描);
        - 短命线程/大扫描后显式归还连接。
        幂等;清 _local.conn 使下次 connect() 重建、绝不返回坏连接(INV-7)。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        """Backend-neutral lifecycle close for the current process thread."""
        self.close_local()

    @contextmanager
    def write(
        self, *, operation: str = "sqlite.write"
    ) -> Iterator[sqlite3.Connection]:
        """写事务:进程内写串行(write_lock)。每次用**独立新连接**(非线程复用读连接),
        使每个 write() 独立提交 —— 保留嵌套增量提交(节点向量 backfill 每批 flush
        独立落库、中断可续跑)的崩溃恢复语义(INV-8)。用完即 close(写串行,写连接峰值
        = 嵌套写深度、fd 用完即还)。深度守卫不作用于 write()。

        写锁**不可重入**(threading.Lock),重入由本方法的 thread-local
        ``write_depth`` 记账:只有最外层(``depth == 0``)才 acquire/release,
        内层只加减深度、完全不碰锁 —— 可观察语义与旧的可重入锁一致(锁自最外层
        acquire 起持有到最外层 release),但不再需要在 Python 层维护 owner/count。
        为什么这很重要、以及仍然存在的残留中断窗口,见本模块上方「进程级写锁
        说明」。

        两套仪器并存,量的是不同的东西,都必须留着:

        - ``diagnostics``(``app.core.diagnostics_runtime``)是**实时**视图:此刻
          谁在排队、谁在持锁、持了多久,供 /diagnostics 端点与事故取证使用。它自己
          按**线程身份**记账重入(``write_acquired`` 命中同线程持有者时只 ``depth
          += 1``),因此**每一层**嵌套都要调用——包括不碰锁的内层;少调一层,它的
          depth 就会失衡。
        - ``self.stats``(``WriteLockStats``)是**历史**聚合:按调用点分桶的
          wait_ms(排队拿锁=用户感知的卡顿)/ hold_ms(持锁=谁害的)分布。它**只在
          最外层**采:内层既没排队也不真正独占,重复计数会把 hold 算重。

        lock 每次现读,因为 sqlite_repository 暴露了 _write_lock 的 setter。
        ``stats.record()`` 故意放在 ``lock.release()`` **之后**调用:本仪器自己也是
        这套瘦身工作的对象——记在锁内会把仪器自身开销、以及未来接上事件日志 sink 后
        落盘 IO 的耗时,都算进其他写者的 wait_ms,量出来的数字就失真了。
        """
        stats = self.stats
        depth = getattr(self._local, "write_depth", 0)
        outermost = depth == 0 and stats is not None
        # 是否由本层负责这把不可重入锁的 acquire/release。与 outermost 分开:
        # outermost 还要求 stats 已启用,而关掉仪器时锁照样必须被拿。
        take_lock = depth == 0
        lock = self.write_lock
        site = _caller_site() if outermost else ""
        if outermost and site == "?":
            # Degraded frame walk (stack shallower than expected, or the
            # bounded walk in _caller_site() exhausted _MAX_CALLER_WALK
            # without resolving) — see WriteLockStats.mark_unresolved_site's
            # docstring. Counted here, before wait_started/lock.acquire(), so
            # it never pollutes wait_ms/hold_ms for this or any other writer.
            stats.mark_unresolved_site()
        # Live view: registered at *every* nesting level (it de-duplicates by
        # thread identity itself — begin_write_wait returns None when this
        # thread already holds the lock, so an inner level never shows up as a
        # phantom waiter). Opened last before the wait actually starts so its
        # reported wait duration is not inflated by the frame walk above.
        waiter = diagnostics.begin_write_wait(operation)
        wait_started = perf_counter() if outermost else 0.0
        # Pre-bound (not just assigned inside the try below) so the
        # releasing finally can always reference them — even if something
        # raises before their "real" assignment runs (e.g. exit_wait()
        # below, or a KeyboardInterrupt at that bytecode boundary) — without
        # risking UnboundLocalError, which would blow up *before*
        # lock.release() and leak the lock. hold_started uses None rather
        # than 0.0 as its sentinel so an early failure reports hold_ms=0.0
        # instead of a nonsense multi-second reading from perf_counter()'s
        # arbitrary epoch.
        wait_ms = 0.0
        hold_started: float | None = None
        bumped_depth = False
        if outermost:
            stats.enter_wait()
        if take_lock:
            try:
                lock.acquire()
            except BaseException:
                # Deliberately do NOT release here. In the overwhelmingly common
                # case acquire() raised without ever taking the lock (e.g. it was
                # interrupted while blocked), so there is nothing to release and
                # calling release() would either raise "release unlocked lock" or
                # — far worse — hand away a lock another thread legitimately
                # holds, since threading.Lock has no owner check.
                #
                # The one case this cannot repair is the residual window
                # documented at the top of this module: a signal checked at the
                # *return* of acquire(), where the lock WAS taken but no Python
                # statement recording that fact has run yet. Nothing at this
                # level can distinguish that from "never acquired", which is
                # precisely why it is irreducible in pure Python. It degrades to
                # a stuck (visible) lock, never to two concurrent writers.
                #
                # Still balance the waiter count against enter_wait() above, or a
                # stuck +1 permanently poisons the fairness signal; and retire the
                # live-view waiter entry, or /diagnostics shows a queue that no
                # longer exists.
                if outermost:
                    stats.exit_wait()
                diagnostics.write_wait_cancelled(waiter)
                raise
        # Lock is held from here on (when take_lock): every statement through
        # the release in the finally below must live inside this try. CPython
        # checks for pending signals at bytecode boundaries, so a
        # KeyboardInterrupt (or e.g. MemoryError on the thread-local store)
        # landing on *any* of these statements — not just the body — would
        # otherwise leak the process-wide write lock permanently; every later
        # write() in the process then blocks forever.
        #
        # Reaching this line is also exactly the condition for "this level owes
        # a diagnostics.write_released()": the acquire above either succeeded or
        # was skipped (nested level). That is why the finally can call it
        # unconditionally rather than gating on a flag assigned *after* the
        # acquire — an interrupt landing between the two would otherwise strand a
        # phantom holder in the live view forever.
        try:
            if outermost:
                stats.exit_wait()
            wait_ms = (perf_counter() - wait_started) * 1000.0 if outermost else 0.0
            hold_started = perf_counter() if outermost else None
            diagnostics.write_acquired(waiter, operation)
            self._local.write_depth = depth + 1
            bumped_depth = True
            # 让新连接带上 write 身份(mode/operation),供 _DiagnosticCursor 的
            # sql_scope 归因。用 thread-local 传参而非改 _new_connection 的调用
            # 形参,是为了让 connect() 那条只读路径零改动;marker 哨兵保证嵌套
            # write() 退出时精确还原外层的值(而不是删掉它)。
            marker = object()
            previous = getattr(self._local, "new_connection_diagnostics", marker)
            self._local.new_connection_diagnostics = ("write", operation)
            try:
                conn = self._new_connection()
            finally:
                if previous is marker:
                    del self._local.new_connection_diagnostics
                else:
                    self._local.new_connection_diagnostics = previous
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        finally:
            # ⚠ 顺序是这里最要紧的不变量:**先恢复深度,再放锁**,而且两件事
            # 必须在**同一个** finally 里。
            #
            # 反例(本次修复前的写法):把深度恢复放进一个更内层的 finally。
            # 它一旦被异步异常打断,外层 finally 仍会**照常放锁**,留下"锁空闲、
            # 而本线程的 write_depth 仍 >0"的状态 —— 本线程下一次 write() 会
            # 因为 depth>0 而跳过 acquire,直接在没有锁的情况下写,同时别的线程
            # 能拿到那把空闲的锁:两个写者同时进入临界区。这正是旧
            # _FairWriteLock 用 owner/count 时那一类静默损坏,换个字段重演。
            #
            # 现在这个顺序下,恢复深度若被打断,``lock.release()`` 就不会执行,
            # 最坏结果是"锁继续锁着"(进程停住、可观测),而不是静默的并发写。
            # 不变量:**绝不在本线程仍自认处于写块内时放掉这把锁。**
            #
            # 深度用赋值恢复而非自减:只在调用方严格 LIFO 退出(普通嵌套
            # `with`/`try`)时正确。非 LIFO 退出(例如乱序的
            # contextlib.ExitStack)会让它卡在 >=1 —— 后果比"静默关掉该线程的
            # 仪器"更重:take_lock 同样由 depth==0 判定,该线程后续每一次
            # write() 都会因 depth>=1 而**跳过 acquire**,在没有拿到进程写锁
            # 的情况下直接写——与另一线程持锁并发写同一个 SQLite 文件,且不
            # 产生任何报错。observed_depth 记下这一刻的实际深度,供下面
            # release 之后的运行时哨兵比对(这里只读一次值,不比较/不抛出,
            # 不拉长临界区)。
            observed_depth = getattr(self._local, "write_depth", 0)
            self._local.write_depth = depth
            # hold_ms is measured here, immediately before release — not
            # after — so it still reflects the full time the lock was held.
            # record() itself runs *after* lock.release(): it must never run
            # from inside the critical section (see the docstring above),
            # and it is wrapped so it can never replace an in-flight
            # exception from the try above, nor stop the lock from being
            # released.
            hold_ms = (
                (perf_counter() - hold_started) * 1000.0
                if hold_started is not None
                else 0.0
            )
            # Release last, and only if this level took it. `take_lock` is
            # computed *before* the acquire, so this decision never depends on a
            # flag assigned after it — an interrupt can leave the lock held
            # (stuck, visible) but can never make us release a lock we did not
            # take, nor skip releasing one we did.
            if take_lock:
                lock.release()
            # Live view: one release per level, matching the write_acquired()
            # above. It is a no-op when this thread is not the registered
            # holder, so the interrupted-instrumentation path cannot corrupt it.
            # After lock.release(): it takes the diagnostics runtime's own lock,
            # which has no business inside this critical section.
            diagnostics.write_released()
            if outermost and hold_started is not None:
                try:
                    stats.record(site, wait_ms, hold_ms)
                except Exception:
                    # Observability must never intrude on the operation
                    # it observes.
                    pass
            if bumped_depth and observed_depth != depth + 1:
                # 上面「深度用赋值恢复」那段注释所说的非 LIFO 退出的运行时
                # 哨兵。目前不可达(backend/app 里没有在裸 `with` 之外调用
                # write() 的地方,附近也没有 ExitStack)——放在这里是为了让它
                # 一旦真的可达,也不会静默滑过去,而是当场炸出来,而不是表现成
                # 日后某次静默的并发写损坏。放在 lock.release() 之后:抛出既
                # 不会延迟本层本该释放的锁,也不会拉长临界区。
                raise RuntimeError(
                    f"write() 非 LIFO 退出:恢复前观察到 write_depth="
                    f"{observed_depth},本层期望值是 {depth + 1}——该线程的写锁"
                    "记账已经错位,它的下一次 write() 可能会跳过 acquire、在"
                    "没有拿到进程写锁的情况下与另一线程并发写"
                )

    def bulk_write(
        self,
        batches: "Iterable[list]",
        apply: "Callable[[sqlite3.Connection, list], None]",
    ) -> int:
        """批量写原语:每批一个独立短事务,批间释放写锁。

        公平性由锁本身保证,这里**不 sleep**:``write_lock`` 是
        ``threading.Lock``(靠 PyMutex 的交接语义,见本模块上方「进程级写锁
        说明」里的实测数字),排队的交互写在一批的持锁时间内就会拿到锁,与批数
        无关。

        这里曾经有一段"每批提交后若 ``stats.waiters > 0`` 就 sleep 2ms 让路"的
        代码。它在两种配置下都是错的,故删除:

        - ``DB_WRITE_LOCK_STATS`` 开(默认):写锁仪器恰好落在释放与重新获取之间,
          批量写者本就抢不回锁(实测被插队批数恒 ≤3),sleep 纯属多余——而代价是
          有交互写活动时批量吞吐掉到约 1/4(实测 4s 内 7375~8288 批 → 1578~2126
          批),交互写延迟反而略升(中位 0.4ms → 0.8ms)。
        - ``DB_WRITE_LOCK_STATS`` 关:``self.stats is None``,这段代码**永远不会
          执行**,而恰恰是这个配置下 RLock 的饿死才真正发作(实测交互写最坏等待
          0.7~2.5s)。即"让路"在唯一需要它的配置里是死代码。

        真正的修复是把锁换成公平锁(``threading.Lock``),它对**所有**写者生效,
        且不受观测开关影响。

        返回处理的批数。某批抛异常时,该批回滚,**之前已提交的批保留**
        (这正是分批提交的意义:中断可续跑)。
        """
        count = 0
        for batch in batches:
            with self.write() as conn:
                apply(conn, batch)
            count += 1
        return count

    @staticmethod
    def begin_immediate(conn: sqlite3.Connection) -> None:
        """在 write() 递出的连接上显式开启 SQLite 写事务。

        write_lock 只互斥**本进程**写者；离线 CLI/维护进程共库时，write() 块内
        首条 DML 之前的 SELECT 不开启 SQLite 事务，另一进程可在「读到检查通过」
        与「首条写入」之间提交变更。调用方（store 的条件删除/一致性快照、投影
        终写守卫）在块首调用本方法，让检查与写入对跨进程写者也原子。事务由
        write() 的 ``with conn:`` 在块尾统一 commit/rollback。SQL 收在本层
        （repositories/sqlite），服务层不出现裸 SQL（callers_static 约束）。"""
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def begin_guarded_write(conn: sqlite3.Connection) -> None:
        """Backend-neutral guarded-write seam used by domain services."""
        conn.execute("BEGIN IMMEDIATE")

    @contextmanager
    def table_projection_lock(self, table_id: str) -> Iterator[None]:
        """Backend-neutral projection seam.

        SQLite keeps its existing process-local scheduler and serialized write
        transactions. PostgreSQL overrides this with a cross-process lock.
        """
        del table_id
        yield

    def scale_build_claim_held_anywhere(self, notebook_id: str) -> bool:
        """SQLite has no cross-process scale-build claim to probe — the
        serving process's in-memory ``building`` set is the only mutex this
        backend ever had, and the caller consults that set first (codex #676
        R6 P2 is a PostgreSQL-topology finding)."""
        del notebook_id
        return False

    def try_scale_build_lock(self, notebook_id: str):
        """SQLite has no cross-process scale-build lock — say so explicitly.

        Returning the UNSUPPORTED sentinel (rather than a nullcontext that
        would read as "granted") is what lets the offline build CLI refuse a
        SQLite deployment instead of racing an in-process build over the same
        ``{scale_dir}.tmp``. The serving process falls back to its in-process
        ``building`` claim, which is the only mutex this backend ever had.

        The other two ``ScaleBuildLockAttempt`` values never occur here: there
        is no lock session to run out of and nothing that can fail, so this
        backend answers unconditionally.
        """
        del notebook_id
        return UNSUPPORTED_SCALE_BUILD_LOCK

# _caller_site() 里 _is_write_frame() 用来识别 write() 自己那一帧的 code 对象。
# 放在类体之后取(而非放进类体内部或 _caller_site 旁):必须等 @contextmanager
# 完成装饰、SqliteDatabase.write 这个类属性真正存在之后才能取到
# __wrapped__.__code__。_caller_site 定义在类之前,但函数体内的全局名到"调用
# 时"才解析(后期绑定)——它只会在 write() 运行期间被调用,那时全模块(包括这
# 一行)早已加载完毕,和源码里两者谁先谁后无关。
_WRITE_GENERATOR_CODE = SqliteDatabase.write.__wrapped__.__code__

# _caller_site() 里 _is_bulk_write_frame() 用来识别 bulk_write() 自己那一帧的
# code 对象(Task 7)。同样放在类体之后取,理由与 _WRITE_GENERATOR_CODE 一致。
# bulk_write 不是 @contextmanager(它是一个普通方法,内部自己跑一个 for 循环
# 反复打开 write()),所以直接取 .__code__,不需要先解包 .__wrapped__。
_BULK_WRITE_CODE = SqliteDatabase.bulk_write.__code__
