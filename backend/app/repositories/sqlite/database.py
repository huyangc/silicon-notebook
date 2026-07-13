from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.config import Settings


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


class SqliteDatabase:
    """进程内 SQLite 连接来源。**每线程复用一条连接**(threading.local),而非每次
    新建——把 fd 用量从 O(操作数) 降到 O(线程数),并省掉每操作反复建连接/PRAGMA/
    mmap 的开销。连接为 _Conn(嵌套事务守卫)。写仍经 write()+write_lock 串行。"""

    def __init__(self, settings: Settings, root_dir: Path) -> None:
        self.settings = settings
        self.root_dir = root_dir
        self.db_path = self.resolve_path(settings.sqlite_path)
        self.write_lock = threading.RLock()
        self._local = threading.local()

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    def _new_connection(self) -> _Conn:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.settings.db_busy_timeout_ms / 1000,
            factory=_Conn,
            check_same_thread=True,  # 显式:连接不跨线程(threading.local 保证)
        )
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

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务:进程内写串行(write_lock)。每次用**独立新连接**(非线程复用读连接),
        使每个 write() 独立提交 —— 保留嵌套增量提交(节点向量 backfill 每批 flush
        独立落库、中断可续跑)的崩溃恢复语义(INV-8)。用完即 close(写串行,写连接峰值
        = 嵌套写深度、fd 用完即还)。深度守卫不作用于 write()。"""
        with self.write_lock:
            conn = self._new_connection()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
