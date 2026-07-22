"""内容寻址缓存的 SQLite 实现：TTL + 容量上限（粗粒度 LRU）+ 按 tag 清空。

只实现实际需要的能力。刻意不做多进程栅栏、大对象磁盘外溢、事务等——单机单
进程部署用不到。

粗粒度 LRU：命中时不是每次都写 used_at，只有距上次刷新超过 refresh_window 才写。
cache hit 是热路径，逐次 UPDATE 会把"读"变成"写"，在高命中率场景（reparse 重跑）
下写放大非常可观。牺牲一点淘汰精度换掉绝大部分写。

total_bytes 在 meta 表里增量维护，避免每次裁剪都 SUM() 全表扫。进程崩溃可能让它
漂移，用 recount() 兜底重算。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional


class SqliteCacheBackend:
    def __init__(
        self,
        path: str,
        *,
        size_limit: int = 2 * 2**30,
        ttl_seconds: float = 90 * 86400,
        refresh_window: float = 3600.0,
        headroom: float = 0.9,
    ) -> None:
        self.path = path
        self.size_limit = size_limit
        self.ttl_seconds = ttl_seconds
        self.refresh_window = refresh_window
        self.headroom = headroom      # 裁到上限的 90%，避免每次 put 都触发裁剪
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS cache ("
                "  key TEXT PRIMARY KEY, value TEXT NOT NULL,"
                "  tag TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL,"
                "  created_at REAL NOT NULL, used_at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_cache_used ON cache(used_at);"
                "CREATE INDEX IF NOT EXISTS idx_cache_tag ON cache(tag);"
                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);"
                "INSERT OR IGNORE INTO meta(k, v) VALUES ('total_bytes', 0);"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")   # 缓存丢了可重建，不值 fsync
        return conn

    # ------------------------------------------------------------ CacheBackend
    def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT value, created_at, used_at FROM cache WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            if now - row["created_at"] > self.ttl_seconds:
                self._delete(db, [key])
                return None
            if now - row["used_at"] > self.refresh_window:      # 粗粒度 LRU
                db.execute("UPDATE cache SET used_at=? WHERE key=?", (now, key))
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
            self._evict_if_needed(db)

    # ------------------------------------------------------------------ 淘汰
    def _delete(self, db: sqlite3.Connection, keys: List[str]) -> int:
        if not keys:
            return 0
        marks = ",".join("?" * len(keys))
        freed = db.execute(
            f"SELECT COALESCE(SUM(size),0) s FROM cache WHERE key IN ({marks})", keys
        ).fetchone()["s"]
        db.execute(f"DELETE FROM cache WHERE key IN ({marks})", keys)
        db.execute("UPDATE meta SET v = v - ? WHERE k='total_bytes'", (freed,))
        return freed

    def _evict_if_needed(self, db: sqlite3.Connection) -> None:
        total = db.execute(
            "SELECT v FROM meta WHERE k='total_bytes'").fetchone()["v"]
        if total <= self.size_limit:
            return
        target = int(self.size_limit * self.headroom)
        # 先清过期条目（免费空间），不够再按 used_at 升序淘汰最冷的。
        cutoff = time.time() - self.ttl_seconds
        expired = [r["key"] for r in db.execute(
            "SELECT key FROM cache WHERE created_at < ?", (cutoff,)).fetchall()]
        total -= self._delete(db, expired)
        while total > target:
            # 取一批候选，但只删到刚好达标为止——不能把整批无条件删光，否则当候选
            # 批大于剩余条目数时会一次清空缓存，把热条目一起带走。
            rows = db.execute(
                "SELECT key, size FROM cache ORDER BY used_at ASC LIMIT 64"
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
            total -= self._delete(db, victims)

    # -------------------------------------------------------------- CacheAdmin
    def evict_tag(self, tag: str) -> int:
        with self._lock, self._connect() as db:
            keys = [r["key"] for r in db.execute(
                "SELECT key FROM cache WHERE tag=?", (tag,)).fetchall()]
            self._delete(db, keys)
            return len(keys)

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
            return {"entries": row["n"], "bytes": row["b"], "by_tag": by_tag}

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

    def _used_at(self, key: str) -> Optional[float]:
        """测试用：读取条目的 used_at，用于验证粗粒度 LRU 的节流。"""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT used_at FROM cache WHERE key=?", (key,)).fetchone()
            return row["used_at"] if row else None

    def __len__(self) -> int:
        with self._lock, self._connect() as db:
            return db.execute("SELECT COUNT(*) n FROM cache").fetchone()["n"]
