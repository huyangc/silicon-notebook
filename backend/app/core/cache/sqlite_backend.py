"""内容寻址缓存的 SQLite 实现：TTL + 容量上限（粗粒度 LRU）+ 按 tag 清空。

只实现实际需要的能力。刻意不做多进程栅栏、大对象磁盘外溢、事务等——单机单
进程部署用不到。

TTL 清理走两条独立的路径，缺一不可：① get 命中时对**被读到**的过期条目查+删；
② put 写路径上按时间节流地无条件清扫（_maybe_sweep_expired）。只有 ① 时，从此
再没被读到的过期条目会永不清理（缓存默认 2 GiB 很难满，容量淘汰那条清扫轮不到
它们），活得远超 ttl_seconds——弱化「删库后残留有上界」这个隐私保证。② 把兑现钉
在写路径上、与容量压力解耦；节流（_last_sweep，进程内不落盘）避免它上每一次 put
的热路径。

粗粒度 LRU：命中时不是每次都写 used_at，只有距上次刷新超过 refresh_window 才写。
cache hit 是热路径，逐次 UPDATE 会把"读"变成"写"，在高命中率场景（reparse 重跑）
下写放大非常可观。牺牲一点淘汰精度换掉绝大部分写。

⚠ 这个"一点精度"具体是多少，如实写在这里：refresh_window 默认 3600s，而
make_cache_backend() 刻意不暴露它——**生产上它恒为 3600**。一小时窗口内的命中不刷新
used_at，于是 used_at ≈ 写入时间，淘汰**退化为近似 FIFO**，反复被访问的条目并不会
因此延寿。同场景只改这一个参数的实测：refresh_window=0 时热条目存活 3/3，
refresh_window=3600 时 0/3。**刻意不改默认值**：调小它能换回热条目保护，代价是把
cache hit 这条最热的路径重新变成写。缓存条目丢了只是多打一次后端（可恢复），写放大
却是持续成本。测试 test_hot_entries_are_not_protected_at_the_production_refresh_window
锁的就是这个真实行为——将来若真做热条目保护，请连同这段说明一起改，别把断言改回 3/3。

total_bytes 在 meta 表里增量维护，避免每次裁剪都 SUM() 全表扫。进程崩溃可能让它
漂移，用 recount() 兜底重算。

连接**每线程复用一条**（threading.local），不是每次操作新建——建连接 + 三条
PRAGMA 在 get() 这种热路径上占掉约 98% 墙钟（实测 12.9k ops/s → 538k ops/s）。
与 app/repositories/sqlite/database.py 同一套做法。

批量删除一律走 WHERE 条件直删，不把 key 列表展开成 IN (?,…)：过期条目数与单个
tag 下的条目数都没有上限，展开后会撞部署机（Ubuntu 24.04，SQLite ≥ 3.32）的
SQLITE_LIMIT_VARIABLE_NUMBER=32766。本机 conda 的 SQLite 编到 250000，这类缺陷
在开发机上测不出来。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

# 一次 LRU 淘汰取多少条候选。这是裁剪循环的支点——原型第一版的真实缺陷正是
# "把整批候选无条件删光"，候选批大于剩余条目数时会一次清空缓存。提到模块级，
# 不让它以裸字面量藏在循环体里。同时它是 _delete_keys_freed_bytes 唯一的批量
# 来源，从而保证那里的 IN (?,…) 占位符数量天然有界。
EVICT_BATCH = 64


class _Conn(sqlite3.Connection):
    """复用连接 + 嵌套事务守卫（与 app/repositories/sqlite/database.py 同款）。

    连接改为线程内复用后，``with conn:`` 依旧是 **sqlite3 的事务上下文**
    （提交/回滚），从来就不负责关闭连接——语义与"每次新建连接"时完全一致。
    原生 ``__exit__`` 每次都 commit/rollback，一旦将来出现嵌套 ``with``，内层
    退出会提前提交外层未完成的写。用深度计数让**只有最外层**才 commit(无异常)/
    rollback(任一层异常)，保住"每个 with 块 = 一个逻辑事务边界"。

    不 override __init__（用 getattr 惰性属性），以兼容 sqlite3.connect(factory=)。
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


class SqliteCacheBackend:
    def __init__(
        self,
        path: str,
        *,
        size_limit: int = 2 * 2**30,
        ttl_seconds: float = 90 * 86400,
        refresh_window: float = 3600.0,
        headroom: float = 0.9,
        sweep_interval: float = 3600.0,
    ) -> None:
        self.path = path
        self.size_limit = size_limit
        self.ttl_seconds = ttl_seconds
        self.refresh_window = refresh_window
        self.headroom = headroom      # 裁到上限的 90%，避免每次 put 都触发裁剪
        self.sweep_interval = sweep_interval  # 写路径上过期清扫的节流间隔（见 _maybe_sweep_expired）
        self._lock = threading.Lock()
        self._local = threading.local()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS cache ("
                "  key TEXT PRIMARY KEY, value TEXT NOT NULL,"
                "  tag TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL,"
                "  created_at REAL NOT NULL, used_at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_cache_used ON cache(used_at);"
                "CREATE INDEX IF NOT EXISTS idx_cache_tag ON cache(tag);"
                # created_at 上的索引服务于 _sweep_expired 的两次条件扫描，而那条
                # 路径长在 put() 里（热路径，且在全局锁内）。无索引时实测 63 MB
                # 上限下最坏单次 put 要 75 ms，线性外推到 2 GiB 默认上限约 2.5 s
                # ——一次 put 卡住所有并发的 get/put。
                "CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at);"
                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);"
                "INSERT OR IGNORE INTO meta(k, v) VALUES ('total_bytes', 0);"
            )
        # 进程内命中计数（不落盘：重启归零即可，用于观察当前进程的缓存效用）。
        self._hits = 0
        self._misses = 0
        # 写路径过期清扫的节流时钟（不落盘，与 hits/misses 同款）。0 = 本进程第一次 put
        # 必清一次，之后每 sweep_interval 秒至多清一次——把「删库后残留有上界」这个隐私
        # 保证的兑现钉在写路径上，不再依赖容量是否触顶。
        self._last_sweep = 0.0
        # 启动即校准 total_bytes。增量计量会被**进程崩溃**（写了 cache 行还没写
        # meta 就被 SIGKILL）打漂，而漂移只累积不自愈，虚高到越过 size_limit 就会
        # 触发"为满足幻影字节把健康条目全删光"。代价是一条 SELECT SUM(size)，每个
        # 进程只付一次；作为对照，此前 recount() 在 app/ 与 scripts/ 下零调用方，
        # 文档声称的"漂移兜底"实际并不存在。
        self.recount()

    def _new_connection(self) -> _Conn:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            factory=_Conn,
            check_same_thread=True,  # 显式:连接不跨线程(threading.local 保证)
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")   # 缓存丢了可重建，不值 fsync
        return conn

    def _connect(self) -> _Conn:
        """返回**本线程复用**的连接（首次懒建）。

        线程结束时 threading.local 释放引用，连接随之关闭；fd 用量是 O(线程数)
        而非 O(操作数)。所有公开方法都在 self._lock 下操作，同一连接不会被并发
        使用。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ CacheBackend
    def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT value, created_at, used_at FROM cache WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                self._misses += 1
                return None
            if now - row["created_at"] > self.ttl_seconds:
                self._delete_keys_freed_bytes(db, [key])
                self._misses += 1
                return None
            if now - row["used_at"] > self.refresh_window:      # 粗粒度 LRU
                db.execute("UPDATE cache SET used_at=? WHERE key=?", (now, key))
            self._hits += 1
            return row["value"]

    def put(self, key: str, value: str, tag: str = "") -> None:
        now = time.time()
        size = len(value.encode("utf-8"))
        with self._lock, self._connect() as db:
            prev = db.execute("SELECT size FROM cache WHERE key=?", (key,)).fetchone()
            db.execute(
                "INSERT INTO cache(key, value, tag, size, created_at, used_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, tag=excluded.tag, size=excluded.size, "
                "created_at=excluded.created_at, used_at=excluded.used_at",
                (key, value, tag, size, now, now),
            )
            db.execute(
                "UPDATE meta SET v = v + ? WHERE k='total_bytes'",
                (size - (prev["size"] if prev else 0),),
            )
            # TTL 清理必须独立于容量压力：_evict_if_needed 没超 size_limit 就早返回
            # （默认 2 GiB 很难满），那时再没被读到的过期条目永不清理，活得远超
            # ttl_seconds。这里在写路径上按时间节流地无条件清扫（不看是否超限），排在
            # _evict_if_needed 之前——先归还过期空间，淘汰再据扣减后的 total 判断。
            self._maybe_sweep_expired(db, now)
            self._evict_if_needed(db)

    # ------------------------------------------------------------------ 淘汰
    def _delete_keys_freed_bytes(
        self, db: sqlite3.Connection, keys: List[str]
    ) -> int:
        """按 key 删除，返回**释放的字节数**（不是行数）。

        keys 只来自 LRU 候选批（≤ EVICT_BATCH 条）与单 key 的过期落空，占位符
        数量因此天然有界。**不要**把"按 tag 清空"或"过期清扫"改成先 SELECT key
        再喂进来——那两处的条目数没有上限，会撞 SQLITE_LIMIT_VARIABLE_NUMBER。
        """
        if not keys:
            return 0
        marks = ",".join("?" * len(keys))
        freed = db.execute(
            f"SELECT COALESCE(SUM(size),0) s FROM cache WHERE key IN ({marks})", keys
        ).fetchone()["s"]
        db.execute(f"DELETE FROM cache WHERE key IN ({marks})", keys)
        db.execute("UPDATE meta SET v = v - ? WHERE k='total_bytes'", (freed,))
        return freed

    def _sweep_expired(self, db: sqlite3.Connection) -> int:
        """删掉所有过期条目，返回释放的字节数。

        按 created_at 条件直删。过期条目数没有上限（一次上限调整就可能让几十万条
        同时过期），先 SELECT key 再展开成 IN (?,…) 会在部署机上抛 "too many SQL
        variables"。这条路径长在 put() 里，而调用方按"缓存故障不影响主流程"用
        except Exception 吞掉降级为 miss——一旦抛异常，缓存会**无声地永久停止写入**。
        """
        cutoff = time.time() - self.ttl_seconds
        freed = db.execute(
            "SELECT COALESCE(SUM(size),0) s FROM cache WHERE created_at < ?", (cutoff,)
        ).fetchone()["s"]
        db.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
        db.execute("UPDATE meta SET v = v - ? WHERE k='total_bytes'", (freed,))
        return freed

    def _maybe_sweep_expired(self, db: sqlite3.Connection, now: float) -> None:
        """写路径上按时间节流地清扫过期条目，**独立于**容量是否触顶。

        为什么不能只靠 _evict_if_needed 里那次清扫：它没超 size_limit 就早返回，缓存
        没填满时过期条目永不被清（TTL 只在 get 时对被读到的条目查+删，never-again-read
        的残留不受任何约束），活得远超 ttl_seconds——这弱化了「删库后残留有上界」这个
        隐私理由。

        为什么要节流：_sweep_expired 走 idx_cache_created 范围删，过期少时很便宜，但
        put 是热路径，不能让它上**每一次** put。节流时钟放进程内（_last_sweep，不落盘），
        距上次清扫不到 sweep_interval 就跳过；到点才清一次，并把时钟推到 now。清扫本身
        走条件删（无 IN 展开），对部署机的变量上限安全。"""
        if now - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = now
        self._sweep_expired(db)

    def _evict_if_needed(self, db: sqlite3.Connection) -> None:
        total = db.execute(
            "SELECT v FROM meta WHERE k='total_bytes'").fetchone()["v"]
        if total <= self.size_limit:
            return
        target = int(self.size_limit * self.headroom)
        # 先清过期条目（免费空间），不够再按 used_at 升序淘汰最冷的。
        total -= self._sweep_expired(db)
        while total > target:
            # 取一批候选，但只删到刚好达标为止——不能把整批无条件删光，否则当候选
            # 批大于剩余条目数时会一次清空缓存，把热条目一起带走。
            rows = db.execute(
                "SELECT key, size FROM cache ORDER BY used_at ASC LIMIT ?",
                (EVICT_BATCH,),
            ).fetchall()
            if not rows:
                break
            victims: List[str] = []
            remaining = total
            for row in rows:
                if remaining <= target:
                    break
                victims.append(row["key"])
                remaining -= row["size"]
            if not victims:
                break
            total -= self._delete_keys_freed_bytes(db, victims)

    # -------------------------------------------------------------- CacheAdmin
    def evict_tag(self, tag: str) -> int:
        """清空某个 tag（通常是模型名）下的全部条目，返回删除行数。

        换模型后清缓存正是条目数最大的场景，所以这里**不取 key 列表**：一条聚合
        拿到行数与字节数，一条条件 DELETE 落库，与条目数无关。
        """
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM cache WHERE tag=?",
                (tag,),
            ).fetchone()
            db.execute("DELETE FROM cache WHERE tag=?", (tag,))
            db.execute(
                "UPDATE meta SET v = v - ? WHERE k='total_bytes'", (row["s"],))
            return row["n"]

    def clear(self) -> int:
        with self._lock, self._connect() as db:
            n = db.execute("SELECT COUNT(*) n FROM cache").fetchone()["n"]
            db.execute("DELETE FROM cache")
            db.execute("UPDATE meta SET v=0 WHERE k='total_bytes'")
            return n

    def stats(self) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM cache").fetchone()
            by_tag = {r["tag"]: r["n"] for r in db.execute(
                "SELECT tag, COUNT(*) n FROM cache GROUP BY tag").fetchall()}
            # 计数快照必须在锁内一次取齐：并发 get() 下分三次读会让同一份返回值里
            # 的 hit_rate 与 hits/misses 互相对不上，读者据此排查问题会被误导。
            hits, misses = self._hits, self._misses
        reads = hits + misses
        return {
            "entries": row["n"], "bytes": row["b"], "by_tag": by_tag,
            "hits": hits, "misses": misses,
            "hit_rate": (hits / reads) if reads else 0.0,
        }

    # ------------------------------------------------------------------ 运维
    def recount(self) -> int:
        """重算 total_bytes，修正进程崩溃导致的漂移。"""
        with self._lock, self._connect() as db:
            n = db.execute(
                "SELECT COALESCE(SUM(size),0) s FROM cache").fetchone()["s"]
            db.execute("UPDATE meta SET v=? WHERE k='total_bytes'", (n,))
            return n

    def volume(self) -> int:
        with self._lock, self._connect() as db:
            return db.execute(
                "SELECT v FROM meta WHERE k='total_bytes'").fetchone()["v"]

    def __len__(self) -> int:
        with self._lock, self._connect() as db:
            return db.execute("SELECT COUNT(*) n FROM cache").fetchone()["n"]
