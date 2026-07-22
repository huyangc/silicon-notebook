# 内容寻址抽取缓存 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让内容相同的输入不再重复付出 LLM 与 embedding 代价，缓存组件高内聚且可低成本替换。

**Architecture:** 新建高内聚模块 `app/core/cache/`，对外只暴露工厂 `make_cache_backend()` 与两层 Protocol（`CacheBackend` 仅 get/put，`CacheAdmin` 可选）。LLM 侧无需装饰器——产品路径的 KG 抽取与 ask 已走在带缓存钩子的 `OpenAICompatibleClient` 上，只需把它的缓存来源改为工厂并补齐淘汰/TTL/tag；embed 侧新增 `CachedEmbedder` 装饰器，在 `make_embedder()` 工厂内包装。

**Tech Stack:** Python 3.13 · SQLite(WAL) · pytest · pydantic-settings v2

**设计文档:** `docs/superpowers/specs/2026-07-22-content-addressed-extraction-cache-design.md`

## Global Constraints

- **不新增生产依赖。** `diskcache` 仅用于 Task 2 的差分测试，通过
  `pytest.importorskip("diskcache")` 引入，**不写入 `backend/requirements.txt`**；
  未安装时该测试自动跳过。
- **配置项必须用 `validation_alias`。** `backend/app/core/config.py` 是
  pydantic-settings v2，`Field(env=...)` 静默失效。
- **改 schema 必须追加新的 `_migration_N` 并 bump `SCHEMA_VERSION`**，禁止塞进已封版
  的旧迁移（版本闸会对已部署库短路，`IF NOT EXISTS` 救不了未执行到的语句）。当前
  `SCHEMA_VERSION = 23`，最新迁移 `_migration_23`。
- **守卫必须做变异验证**：加完守卫后，把代码改回违规形态，确认测试真的转红，再改回来。
  只做"删除"变异不够，涉及位置约束的还要做"移动"变异。
- **测试命令**一律在仓库根执行：`cd backend && python -m pytest <路径> -v`。
  解释器用 `/opt/homebrew/Caskroom/miniconda/base/bin/python`（本机后端解释器）。
- **缓存故障永不影响主流程**：所有缓存读写以 try/except 包裹，异常退化为 miss。

---

### Task 1: cache 模块骨架 —— Protocol 分层 + NoCacheBackend + 契约测试套件

**Files:**
- Create: `backend/app/core/cache/__init__.py`
- Create: `backend/app/core/cache/backend.py`
- Test: `backend/tests/test_cache_backend_contract.py`

**Interfaces:**
- Consumes: 无（本计划起点）
- Produces:
  - `CacheBackend` Protocol：`get(key: str) -> Optional[str]`、`put(key: str, value: str, tag: str = "") -> None`
  - `CacheAdmin` Protocol：`evict_tag(tag: str) -> int`、`clear() -> int`、`stats() -> dict`
  - `NoCacheBackend` 类：实现 `CacheBackend`，永远 miss
  - 均由 `from app.core.cache import ...` 导出

- [ ] **Step 1: 写 Protocol 定义**

创建 `backend/app/core/cache/backend.py`：

```python
"""缓存后端的接口契约。

分两层是刻意的：CacheBackend 只有 get/put 两个必需方法，任何简单 KV 组件都能
实现；运维能力归入可选的 CacheAdmin，由调用方用 isinstance 探测。若把 evict_tag/
stats 并入必需接口，将来换任何 KV 组件都得先补齐管理方法，可替换性即告失效。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """内容寻址缓存的最小契约：key -> 字符串值。

    `tag` 是可选参数，不支持分组的实现直接忽略即可——降级为"无法按 tag 清空",
    不影响正确性。
    """

    def get(self, key: str) -> Optional[str]: ...

    def put(self, key: str, value: str, tag: str = "") -> None: ...


@runtime_checkable
class CacheAdmin(Protocol):
    """可选的运维能力。后端未实现时，管理入口如实降级提示。"""

    def evict_tag(self, tag: str) -> int: ...

    def clear(self) -> int: ...

    def stats(self) -> dict: ...


class NoCacheBackend:
    """永远 miss。用于测试隔离与显式关闭缓存的场合。"""

    def get(self, key: str) -> Optional[str]:
        return None

    def put(self, key: str, value: str, tag: str = "") -> None:
        pass
```

- [ ] **Step 2: 写模块公开面**

创建 `backend/app/core/cache/__init__.py`：

```python
"""缓存模块的唯一公开面。

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。
具体实现类（SqliteCacheBackend 等）不对外导出——替换组件时只改本模块内部，
调用方零改动。该约束由 tests/test_cache_cohesion_guard.py 强制。
"""
from __future__ import annotations

from app.core.cache.backend import CacheAdmin, CacheBackend, NoCacheBackend

__all__ = ["CacheBackend", "CacheAdmin", "NoCacheBackend"]
```

- [ ] **Step 3: 写契约测试套件（先只覆盖 NoCacheBackend）**

创建 `backend/tests/test_cache_backend_contract.py`：

```python
"""Protocol 契约套件——只测接口行为，不碰实现细节。

新增 backend 时把它加进 _BACKENDS，跑通即可切换。这是可替换性的另一半：
接口能插上不等于行为正确。
"""
import pytest

from app.core.cache import CacheBackend, NoCacheBackend


def _make_noop(tmp_path):
    return NoCacheBackend()


class MinimalBackend:
    """只实现 CacheBackend 两个必需方法的后端——可替换性的活体标尺。

    未来的 Redis/memcached 后端就是这个形状（TTL 与 LRU 交给服务端配置，
    不实现 CacheAdmin）。若有人把 stats/evict_tag 悄悄变成事实上的必需方法，
    本参数化项会立刻转红。不要因为"它看起来没用"而删掉它。
    """

    def __init__(self):
        self._d = {}

    def get(self, key):
        return self._d.get(key)

    def put(self, key, value, tag=""):
        self._d[key] = value


def _make_minimal(tmp_path):
    return MinimalBackend()


# 新 backend 在此登记：(名字, 构造函数, 是否真正持久化)
_BACKENDS = [
    ("noop", _make_noop, False),
    ("minimal", _make_minimal, True),
]


@pytest.fixture(params=_BACKENDS, ids=[b[0] for b in _BACKENDS])
def backend_case(request, tmp_path):
    name, factory, persists = request.param
    return factory(tmp_path), persists


def test_satisfies_protocol(backend_case):
    backend, _ = backend_case
    assert isinstance(backend, CacheBackend)


def test_missing_key_returns_none(backend_case):
    backend, _ = backend_case
    assert backend.get("absent") is None


def test_put_then_get(backend_case):
    backend, persists = backend_case
    backend.put("k", "v")
    assert backend.get("k") == ("v" if persists else None)


def test_overwrite_takes_effect(backend_case):
    backend, persists = backend_case
    backend.put("k", "v1")
    backend.put("k", "v2")
    assert backend.get("k") == ("v2" if persists else None)


def test_tag_argument_is_accepted(backend_case):
    """tag 是可选参数——不支持分组的实现必须能安全忽略它，而不是报错。"""
    backend, persists = backend_case
    backend.put("k", "v", tag="model-x")
    assert backend.get("k") == ("v" if persists else None)


def test_empty_string_value_roundtrips(backend_case):
    """空串是合法值，与"不存在"必须可区分（None vs ""）。"""
    backend, persists = backend_case
    backend.put("k", "")
    assert backend.get("k") == ("" if persists else None)
```

- [ ] **Step 4: 运行测试**

```bash
cd backend && python -m pytest tests/test_cache_backend_contract.py -v
```

Expected: 12 passed（6 项 × `noop` / `minimal` 两个后端）

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/cache/ backend/tests/test_cache_backend_contract.py
git commit -m "feat(cache): 缓存后端 Protocol 分层与契约测试套件"
```

---

### Task 2: SqliteCacheBackend —— TTL + 容量上限 + 按 tag 清空

**Files:**
- Create: `backend/app/core/cache/sqlite_backend.py`
- Modify: `backend/tests/test_cache_backend_contract.py`（把 sqlite 登记进 `_BACKENDS`）
- Test: `backend/tests/test_cache_sqlite_backend.py`

**Interfaces:**
- Consumes: Task 1 的 `CacheBackend` / `CacheAdmin` Protocol
- Produces: `SqliteCacheBackend(path: str, *, size_limit: int, ttl_seconds: float, refresh_window: float = 3600.0, headroom: float = 0.9)`，实现 `get`/`put`/`evict_tag`/`clear`/`stats`/`recount`/`volume`/`__len__`

- [ ] **Step 1: 写失败测试（先写淘汰相关的核心风险项）**

创建 `backend/tests/test_cache_sqlite_backend.py`：

```python
"""SqliteCacheBackend 的行为测试。淘汰逻辑是自研缓存的核心风险面，逐项覆盖。"""
import time

import pytest

from app.core.cache.sqlite_backend import SqliteCacheBackend


def _mk(tmp_path, **kw):
    kw.setdefault("size_limit", 10**9)
    kw.setdefault("ttl_seconds", 90 * 86400)
    return SqliteCacheBackend(str(tmp_path / "cache.db"), **kw)


def test_put_get_roundtrip(tmp_path):
    c = _mk(tmp_path)
    c.put("k", "v")
    assert c.get("k") == "v"


def test_ttl_expiry(tmp_path):
    c = _mk(tmp_path, ttl_seconds=0.5)
    c.put("k", "v")
    assert c.get("k") == "v"
    time.sleep(0.6)
    assert c.get("k") is None
    assert c.volume() == 0, "过期条目必须同时归还容量计量"


def test_evict_tag_only_removes_that_tag(tmp_path):
    c = _mk(tmp_path)
    c.put("a", "va", tag="modelA")
    c.put("b", "vb", tag="modelB")
    assert c.evict_tag("modelA") == 1
    assert c.get("a") is None
    assert c.get("b") == "vb"


def test_overwrite_updates_volume_by_delta(tmp_path):
    c = _mk(tmp_path)
    c.put("x", "1" * 5000)
    c.put("y", "2" * 5000)
    before = c.volume()
    c.put("x", "3" * 100)
    assert c.volume() == before - 5000 + 100


def test_recount_matches_incremental_volume(tmp_path):
    c = _mk(tmp_path)
    for i in range(10):
        c.put(f"k{i}", "z" * 500)
    assert c.recount() == c.volume()


def test_eviction_keeps_utilization_high(tmp_path):
    """裁剪后容量利用率必须接近上限。

    "缓存被清得只剩零星几条"是 cull_limit 类缺陷的特征——只断言"没超限"抓不到它。
    """
    limit = 200_000
    c = _mk(tmp_path, size_limit=limit)
    for i in range(40):
        c.put(f"k{i}", "z" * 20_000)
    assert c.volume() <= limit
    assert c.volume() >= limit * 0.5, f"裁剪过度：只剩 {c.volume()} / {limit}"


def test_eviction_does_not_wipe_cache_when_batch_exceeds_entry_count(tmp_path):
    """候选批大于剩余条目数时不得清空缓存（原型第一版的真实缺陷）。"""
    limit = 100_000
    c = _mk(tmp_path, size_limit=limit)
    for i in range(8):                      # 8 × 20KB = 160KB > 100KB，必触发裁剪
        c.put(f"k{i}", "z" * 20_000)
    assert len(c) > 0, "裁剪把缓存清空了"
    assert c.volume() <= limit


def test_hot_entries_survive_eviction(tmp_path):
    """热条目保护。

    注意：LRU 看的是最后访问时间而非访问次数。有效用例必须在灌入冷数据的过程中
    交错访问热条目，否则热条目的 used_at 仍旧早于所有冷条目，被淘汰是正确行为。
    """
    c = _mk(tmp_path, size_limit=200_000, refresh_window=0)
    for i in range(5):
        c.put(f"h{i}", "z" * 20_000)
    for i in range(5, 20):
        c.put(f"c{i}", "z" * 20_000)
        for j in range(3):
            c.get(f"h{j}")                  # h0-h2 持续被访问
    alive = [f"h{j}" for j in range(3) if c.get(f"h{j}") is not None]
    assert len(alive) == 3, f"热条目被误删：只剩 {alive}"


def test_coarse_lru_avoids_write_amplification(tmp_path):
    """refresh_window 内的重复命中不应产生 used_at 写入。

    cache hit 是热路径，逐次 UPDATE 会把"读"变成"写"。这里通过观察 used_at
    是否变化来验证节流生效。
    """
    c = _mk(tmp_path, refresh_window=3600)
    c.put("k", "v")
    first = c._used_at("k")
    time.sleep(0.05)
    c.get("k")
    assert c._used_at("k") == first, "refresh_window 内不应刷新 used_at"


def test_stats_reports_entries_and_tags(tmp_path):
    c = _mk(tmp_path)
    c.put("a", "va", tag="m1")
    c.put("b", "vb", tag="m1")
    c.put("c", "vc", tag="m2")
    s = c.stats()
    assert s["entries"] == 3
    assert s["by_tag"] == {"m1": 2, "m2": 1}


def test_clear_empties_everything(tmp_path):
    c = _mk(tmp_path)
    c.put("a", "va")
    assert c.clear() == 1
    assert len(c) == 0 and c.volume() == 0
```

- [ ] **Step 2: 运行测试验证全部失败**

```bash
cd backend && python -m pytest tests/test_cache_sqlite_backend.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.cache.sqlite_backend'`

- [ ] **Step 3: 实现 SqliteCacheBackend**

创建 `backend/app/core/cache/sqlite_backend.py`：

```python
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
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd backend && python -m pytest tests/test_cache_sqlite_backend.py -v
```

Expected: 11 passed

- [ ] **Step 5: 把 sqlite 登记进契约套件**

修改 `backend/tests/test_cache_backend_contract.py`，替换 `_BACKENDS` 定义段：

```python
from app.core.cache import CacheBackend, NoCacheBackend
from app.core.cache.sqlite_backend import SqliteCacheBackend


def _make_noop(tmp_path):
    return NoCacheBackend()


def _make_sqlite(tmp_path):
    return SqliteCacheBackend(str(tmp_path / "contract.db"))


# 新 backend 在此登记：(名字, 构造函数, 是否真正持久化)
_BACKENDS = [
    ("noop", _make_noop, False),
    ("sqlite", _make_sqlite, True),
]
```

> 契约测试是模块内部的白盒测试，允许 import 具体实现类；Task 4 的内聚守卫会把
> `backend/tests/test_cache_backend_contract.py` 列入豁免名单。

- [ ] **Step 6: 运行契约套件**

```bash
cd backend && python -m pytest tests/test_cache_backend_contract.py -v
```

Expected: 18 passed（6 项 × `noop` / `minimal` / `sqlite` 三个后端）

- [ ] **Step 7: 加差分测试（与 diskcache 对照）**

在 `backend/tests/test_cache_sqlite_backend.py` 末尾追加：

```python
def test_differential_against_diskcache(tmp_path):
    """同一随机读写序列下，与 diskcache(cull_limit=1) 的命中/落空判定必须一致。

    diskcache 仅作开发期的参照实现，不是生产依赖——未安装即跳过。
    注意必须用 cull_limit=1：默认的 10 会按固定条数剔除而非删到刚好达标，
    在大条目下会过度清空。
    """
    diskcache = pytest.importorskip("diskcache")
    import random

    rng = random.Random(42)
    limit = 150_000
    a = _mk(tmp_path, size_limit=limit, refresh_window=0)
    b = diskcache.Cache(
        str(tmp_path / "dc"), size_limit=limit,
        eviction_policy="least-recently-used", cull_limit=1,
    )
    mismatch = 0
    for _ in range(400):
        key = f"k{rng.randint(0, 25)}"
        if rng.random() < 0.4:
            value = "v" * rng.randint(5_000, 15_000)
            a.put(key, value)
            b.set(key, value)
        else:
            ra, rb = a.get(key), b.get(key)
            if (ra is None) != (rb is None) or (ra is not None and ra != rb):
                mismatch += 1
    b.close()
    assert mismatch == 0, f"与参照实现有 {mismatch} 处判定分歧"
```

- [ ] **Step 8: 运行差分测试**

```bash
cd backend && python -m pytest tests/test_cache_sqlite_backend.py -v
```

Expected: 12 passed，或差分那项 SKIPPED（未装 diskcache）。若要真跑：
`python -m pip install --target /tmp/dcprobe diskcache && PYTHONPATH=/tmp/dcprobe python -m pytest ...`

- [ ] **Step 9: 提交**

```bash
git add backend/app/core/cache/sqlite_backend.py backend/tests/test_cache_sqlite_backend.py backend/tests/test_cache_backend_contract.py
git commit -m "feat(cache): SqliteCacheBackend 实现 TTL/容量上限/按 tag 清空"
```

---

### Task 3: 唯一构造点 + 接入 LLM 侧 + 配置 + 测试隔离 + 默认开

**Files:**
- Create: `backend/app/core/cache/policy.py`
- Modify: `backend/app/core/cache/__init__.py`
- Modify: `backend/app/core/config.py:172-173`
- Modify: `backend/app/core/llm.py:11`、`:107-125`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_cache_factory.py`

**Interfaces:**
- Consumes: Task 1 的 Protocol、Task 2 的 `SqliteCacheBackend`
- Produces:
  - `make_cache_backend(settings) -> CacheBackend`（唯一构造点，由 `app.core.cache` 导出）
  - `llm_key(model, messages, schema_hint) -> str`（由 `app.core.cache` 导出，行为与原
    `app.core.llm_cache.cache_key` 逐字节一致）
  - 新配置项 `llm_cache_size_limit: int`、`llm_cache_ttl_days: int`

- [ ] **Step 1: 写 policy.py（key 计算从存储中分离）**

创建 `backend/app/core/cache/policy.py`：

```python
"""缓存策略：key 怎么算、什么不该缓存。与存储实现分离——换 backend 不碰本文件。"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List


def llm_key(model: str, messages: List[Dict[str, str]], schema_hint: str) -> str:
    """LLM 响应的缓存键 = sha256(model + messages 全文 + schema_hint)。

    prompt 全文进 key 意味着改 prompt 即自动全冷，不需要维护版本号。
    temperature 是 chat_json 的固定常量，刻意排除；若将来加了 per-call
    temperature，必须并入 key。

    注意：本函数的输出必须与历史实现逐字节一致，否则已有缓存全部失效。
    """
    payload = json.dumps(
        {"model": model, "messages": messages, "schema": schema_hint},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def embed_key(model: str, truncated_text: str) -> str:
    """单条文本的向量缓存键。

    传入的必须是**截断后**的文本（DashscopeEmbedder 内部先做
    `t[:embed_truncate_chars]` 才发 API）。用原文取哈希会让两个前 N 字符相同的
    长文本各占一条缓存却拿到完全相同的向量，白白损失命中率；对截断后文本取哈希
    还顺带捕获了 embed_truncate_chars 的配置变更。
    """
    payload = json.dumps({"model": model, "text": truncated_text},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 2: 写工厂与失败测试**

创建 `backend/tests/test_cache_factory.py`：

```python
"""唯一构造点的行为。换缓存组件时只改 make_cache_backend，调用方零改动。"""
from app.core.cache import NoCacheBackend, llm_key, make_cache_backend
from app.core.config import Settings


def test_disabled_returns_noop(tmp_path):
    s = Settings(LLM_CACHE_ENABLED=False, LLM_CACHE_PATH=str(tmp_path / "c.db"))
    assert isinstance(make_cache_backend(s), NoCacheBackend)


def test_enabled_returns_working_backend(tmp_path):
    s = Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=str(tmp_path / "c.db"))
    backend = make_cache_backend(s)
    backend.put("k", "v", tag="m")
    assert backend.get("k") == "v"


def test_relative_path_is_anchored_to_repo_root(tmp_path):
    """相对路径锚定逻辑归工厂所有——调用方不该知道它。"""
    s = Settings(LLM_CACHE_ENABLED=True, LLM_CACHE_PATH=".local/test_cache_factory.db")
    backend = make_cache_backend(s)
    backend.put("k", "v")
    assert backend.get("k") == "v"


def test_llm_key_is_stable_and_content_addressed():
    msgs = [{"role": "user", "content": "hello"}]
    assert llm_key("m", msgs, "{}") == llm_key("m", msgs, "{}")
    assert llm_key("m", msgs, "{}") != llm_key("m2", msgs, "{}")


def test_llm_key_matches_legacy_implementation():
    """key 算法不得漂移，否则存量缓存全部失效。"""
    from app.core.llm_cache import cache_key as legacy
    msgs = [{"role": "user", "content": "中文 content"}]
    assert llm_key("model-x", msgs, '{"a":""}') == legacy("model-x", msgs, '{"a":""}')
```

- [ ] **Step 3: 运行测试验证失败**

```bash
cd backend && python -m pytest tests/test_cache_factory.py -v
```

Expected: FAIL — `ImportError: cannot import name 'make_cache_backend'`

- [ ] **Step 4: 实现工厂**

覆写 `backend/app/core/cache/__init__.py`：

```python
"""缓存模块的唯一公开面。

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。
具体实现类（SqliteCacheBackend 等）不对外导出——替换组件时只改本模块内部，
调用方零改动。该约束由 tests/test_cache_cohesion_guard.py 强制。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.cache.backend import CacheAdmin, CacheBackend, NoCacheBackend
from app.core.cache.policy import embed_key, llm_key

__all__ = [
    "CacheBackend", "CacheAdmin", "NoCacheBackend",
    "make_cache_backend", "llm_key", "embed_key",
]


def make_cache_backend(settings: Any) -> CacheBackend:
    """缓存的唯一诞生处：读开关、解析路径、选实现。

    换缓存组件只需改本函数——所有消费者都从这里取，不自行构造、不解析路径、
    不读配置项。
    """
    if not getattr(settings, "llm_cache_enabled", False):
        return NoCacheBackend()
    from app.core.cache.sqlite_backend import SqliteCacheBackend

    raw = getattr(settings, "llm_cache_path", "") or ".local/llm_cache_v2.db"
    path = Path(raw)
    if not path.is_absolute():
        # 相对路径锚定到仓库根。此规则归缓存模块所有，调用方不该知道。
        path = Path(__file__).resolve().parents[4] / raw
    return SqliteCacheBackend(
        str(path),
        size_limit=int(getattr(settings, "llm_cache_size_limit", 2 * 2**30)),
        ttl_seconds=float(getattr(settings, "llm_cache_ttl_days", 90)) * 86400.0,
    )
```

> 路径深度校验：本文件位于 `backend/app/core/cache/__init__.py`，`parents[0]`=cache、
> `[1]`=core、`[2]`=app、`[3]`=backend、`[4]`=仓库根。与被替换的 `llm.py` 版本
> （`parents[3]`，因其少一层目录）对应一致。

- [ ] **Step 5: 加配置项**

修改 `backend/app/core/config.py`，把第 172-173 行替换为：

```python
    llm_cache_enabled: bool = Field(True, validation_alias="LLM_CACHE_ENABLED")
    llm_cache_path: str = Field(".local/llm_cache_v2.db", validation_alias="LLM_CACHE_PATH")
    # 容量上限(字节)与条目寿命(天)。TTL 必须长——reparse/reextract 那种几万源重跑
    # 可能几个月后才发生，届时命中率接近 100%，短 TTL 会毁掉最大的收益场景。
    llm_cache_size_limit: int = Field(2 * 2**30, validation_alias="LLM_CACHE_SIZE_LIMIT")
    llm_cache_ttl_days: int = Field(90, validation_alias="LLM_CACHE_TTL_DAYS")
```

> 换新文件名 `llm_cache_v2.db` 是刻意的：旧 `llm_cache.db` 的表结构（缺
> tag/size/used_at）不兼容，不迁移、直接弃用，缓存冷启动重建即可。

- [ ] **Step 6: 测试隔离（务必在默认开生效前落地）**

修改 `backend/tests/conftest.py`，在文件顶部
`os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")` 之后插入：

```python
# 缓存默认开，但测试进程必须强制关闭：带真 .env 跑全量时，共享的缓存文件会让
# 断言读到上一次运行的响应，制造大规模假失败/假成功。
os.environ["LLM_CACHE_ENABLED"] = "false"
```

> 用 `os.environ[...]=` 而非 `setdefault`：这是不可协商的隔离，不允许外部环境覆盖。

- [ ] **Step 7: 接入 LLM 侧**

修改 `backend/app/core/llm.py` 第 11 行的 import：

```python
from app.core.cache import CacheBackend, llm_key
```

把第 113-125 行的 `_get_cache` 整体替换为：

```python
    def _get_cache(self):
        if self._cache is None:
            from app.core.cache import make_cache_backend
            self._cache = make_cache_backend(self.settings)
        return self._cache
```

并把第 225 行的 `cache_key(...)` 调用改为 `llm_key(...)`：

```python
                    ckey = llm_key(model, full_messages, response_schema_hint)
```

> `_get_cache` 现在恒返回一个 backend（关闭时是 `NoCacheBackend`），不再返回
> `None`。调用处 `if cache is not None` 依然成立且行为不变——NoCacheBackend 永远
> miss。**不要**顺手删掉写入处的 `content != "{}"` 条件，那是防退化固化的关键守卫
> （Task 3 Step 9 会为它加锁定测试）。

- [ ] **Step 8: 运行相关测试**

```bash
cd backend && python -m pytest tests/test_cache_factory.py tests/test_cache_backend_contract.py tests/test_cache_sqlite_backend.py -v
```

Expected: 全部通过

- [ ] **Step 9: 加空响应保护的锁定测试**

创建测试并追加到 `backend/tests/test_cache_factory.py`：

```python
def test_empty_json_fallback_is_never_cached():
    """llm.py 的 `content != "{}"` 是防退化固化的关键守卫。

    输出预算烧光时 chat_json 会落到 "{}" 回退；缓存它等于把一次偶发退化永久
    固化在这条 prompt 上。本测试锁定该条件的存在。
    变异验证：删掉 llm.py 里的 `and content != "{}"` 后，本测试必须转红。
    """
    import inspect

    from app.core.llm import OpenAICompatibleClient

    src = inspect.getsource(OpenAICompatibleClient.chat_json)
    # 先切到写入块，再断言条件——避免 [\s\S]*? 越过块边界匹配到别处。
    idx = src.find("cache.put(")
    assert idx != -1, "chat_json 里找不到缓存写入点"
    window = src[max(0, idx - 400):idx]
    assert 'content != "{}"' in window, (
        "缓存写入处丢失了空响应守卫：'{}' 回退会被固化"
    )
```

- [ ] **Step 10: 变异验证空响应守卫**

```bash
cd backend
# 先确认能改到（而不是替换了一个不存在的字面量）
grep -c 'and content != "{}"' app/core/llm.py     # 必须输出 1
perl -pi -e 's/ and content != "\{\}"//' app/core/llm.py
python -m pytest tests/test_cache_factory.py::test_empty_json_fallback_is_never_cached -v
# Expected: FAILED —— 守卫有效
git checkout app/core/llm.py
python -m pytest tests/test_cache_factory.py::test_empty_json_fallback_is_never_cached -v
# Expected: PASSED —— 已还原
```

- [ ] **Step 11: 跑一遍受影响的既有测试**

```bash
cd backend && python -m pytest tests/test_debug_logs.py tests/test_model_errors.py tests/test_architecture_documentation.py -v
```

Expected: 全部通过（若 `test_architecture_documentation.py` 因新增模块报错，按其提示同步文档措辞）

- [ ] **Step 12: 提交**

```bash
git add backend/app/core/cache/ backend/app/core/config.py backend/app/core/llm.py backend/tests/conftest.py backend/tests/test_cache_factory.py
git commit -m "feat(cache): 唯一构造点接入 LLM 侧，缓存默认开并隔离测试进程"
```

---

### Task 4: 内聚导入守卫

**Files:**
- Test: `backend/tests/test_cache_cohesion_guard.py`

**Interfaces:**
- Consumes: Task 1-3 建立的 `app/core/cache/` 模块
- Produces: 无运行时产物；一条可执行的架构约束

- [ ] **Step 1: 写守卫测试**

创建 `backend/tests/test_cache_cohesion_guard.py`：

```python
"""内聚约束：具体缓存实现类不得泄漏到模块之外。

这条约束是"将来能低成本替换缓存组件"的保障——消费者只依赖 Protocol 与工厂，
换实现时只改 app/core/cache/ 内部。
"""
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_CACHE_PKG = _BACKEND / "app" / "core" / "cache"

# 允许 import 具体实现的位置：模块自身，以及模块的白盒测试。
_EXEMPT = {
    _CACHE_PKG,
    _BACKEND / "tests" / "test_cache_backend_contract.py",
    _BACKEND / "tests" / "test_cache_sqlite_backend.py",
    _BACKEND / "tests" / "test_cache_cohesion_guard.py",
}

_FORBIDDEN = ("app.core.cache.sqlite_backend", "SqliteCacheBackend")


def _is_exempt(path: Path) -> bool:
    return any(path == e or e in path.parents for e in _EXEMPT)


def _python_files():
    for path in _BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts or _is_exempt(path):
            continue
        yield path


def test_concrete_backend_is_not_imported_outside_the_cache_module():
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in _FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {token}")
    assert not offenders, (
        "具体缓存实现泄漏到模块之外，替换组件将不再是局部改动：\n  "
        + "\n  ".join(offenders)
    )


def test_guard_actually_scans_files():
    """守卫自身的健全性：确保扫描范围非空，否则上面那条会假绿。"""
    assert sum(1 for _ in _python_files()) > 50


def test_public_surface_exports_factory_and_protocols():
    import app.core.cache as cache_pkg

    for name in ("make_cache_backend", "CacheBackend", "CacheAdmin", "NoCacheBackend"):
        assert name in cache_pkg.__all__, f"{name} 应在公开面中"
```

- [ ] **Step 2: 运行守卫**

```bash
cd backend && python -m pytest tests/test_cache_cohesion_guard.py -v
```

Expected: 3 passed

- [ ] **Step 3: 变异验证（删除式）**

```bash
cd backend
printf '\nfrom app.core.cache.sqlite_backend import SqliteCacheBackend  # 变异\n' >> app/services/embedding.py
grep -c "SqliteCacheBackend" app/services/embedding.py     # 必须输出 1，确认改到了
python -m pytest tests/test_cache_cohesion_guard.py::test_concrete_backend_is_not_imported_outside_the_cache_module -v
# Expected: FAILED，且报错里列出 app/services/embedding.py
git checkout app/services/embedding.py
```

- [ ] **Step 4: 变异验证（豁免名单不可过宽）**

```bash
cd backend
# 把 tests 整个目录塞进豁免——守卫的自检项应当仍然通过，但覆盖面塌陷必须可见
python - <<'PY'
from pathlib import Path
p = Path("tests/test_cache_cohesion_guard.py")
s = p.read_text(encoding="utf-8")
assert '_BACKEND / "tests" / "test_cache_backend_contract.py",' in s
p.write_text(s.replace(
    '_BACKEND / "tests" / "test_cache_backend_contract.py",',
    '_BACKEND / "tests",'), encoding="utf-8")
PY
printf '\nfrom app.core.cache.sqlite_backend import SqliteCacheBackend  # 变异\n' >> tests/test_model_errors.py
python -m pytest tests/test_cache_cohesion_guard.py -v
# Expected: PASSED —— 证明过宽的豁免会让守卫失明，因此豁免必须逐文件列举
git checkout tests/test_cache_cohesion_guard.py tests/test_model_errors.py
python -m pytest tests/test_cache_cohesion_guard.py -v
# Expected: 3 passed —— 已还原
```

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_cache_cohesion_guard.py
git commit -m "test(cache): 内聚导入守卫，锁定具体实现不泄漏到模块之外"
```

---

### Task 5: CachedEmbedder + 健康探针绕过缓存

**Files:**
- Create: `backend/app/services/cached_embedder.py`
- Modify: `backend/app/services/embedding.py:42-78`（`make_embedder` 签名与返回）
- Modify: `backend/app/services/model_status.py:240`
- Test: `backend/tests/test_cached_embedder.py`

**Interfaces:**
- Consumes: `app.core.cache` 的 `make_cache_backend`、`embed_key`
- Produces:
  - `CachedEmbedder(inner, backend, model: str, truncate_chars: int)`，实现 `embed_texts`/`embed_query`，其余属性经 `__getattr__` 透传
  - `make_embedder(settings, *, provider=None, base_url=None, api_key=None, model=None, cache: bool = True)`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_cached_embedder.py`：

```python
"""CachedEmbedder：per-text 内容寻址，批量部分命中必须顺序对齐。"""
from app.core.cache import NoCacheBackend
from app.core.cache.sqlite_backend import SqliteCacheBackend
from app.services.cached_embedder import CachedEmbedder


class RecordingEmbedder:
    """记录每次收到的文本，返回可辨识的确定性向量。"""

    dim = 4

    def __init__(self):
        self.calls = []

    def _vec(self, text):
        return [float(len(text)), float(sum(map(ord, text[:1])) if text else 0), 0.0, 1.0]

    def embed_texts(self, texts):
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _mk(tmp_path, truncate_chars=2000):
    inner = RecordingEmbedder()
    backend = SqliteCacheBackend(str(tmp_path / "c.db"))
    return inner, CachedEmbedder(inner, backend, model="m1",
                                 truncate_chars=truncate_chars)


def test_second_call_is_served_from_cache(tmp_path):
    inner, cached = _mk(tmp_path)
    first = cached.embed_texts(["alpha"])
    second = cached.embed_texts(["alpha"])
    assert first == second
    assert len(inner.calls) == 1, "第二次不应再打后端"


def test_partial_hit_only_requests_missing_texts_in_order(tmp_path):
    """命中项与未命中项交错——顺序错配是静默灾难（向量张冠李戴）。"""
    inner, cached = _mk(tmp_path)
    cached.embed_texts(["b", "d"])              # 预热 b、d
    inner.calls.clear()
    out = cached.embed_texts(["a", "b", "c", "d", "e"])
    assert inner.calls == [["a", "c", "e"]], "只应请求未命中的三条"
    assert out == [inner._vec(t) for t in ["a", "b", "c", "d", "e"]], "顺序错配"
    assert len(out) == 5


def test_duplicate_texts_in_one_batch_cause_one_backend_call(tmp_path):
    inner, cached = _mk(tmp_path)
    out = cached.embed_texts(["x", "y", "x"])
    assert inner.calls == [["x", "y"]], "同批重复文本只应请求一次"
    assert out[0] == out[2] == inner._vec("x")


def test_key_is_based_on_truncated_text(tmp_path):
    """两个前 N 字符相同的长文本发给 API 的内容相同，必须共用同一条缓存。"""
    inner, cached = _mk(tmp_path, truncate_chars=5)
    cached.embed_texts(["abcde-TAIL-1"])
    inner.calls.clear()
    cached.embed_texts(["abcde-TAIL-2"])
    assert inner.calls == [], "截断后内容相同，应当命中缓存"


def test_length_mismatch_response_is_not_cached(tmp_path):
    """后端返回长度与输入不符时，不得写入缓存（否则毒化后续请求）。"""

    class BadEmbedder(RecordingEmbedder):
        def embed_texts(self, texts):
            self.calls.append(list(texts))
            return []                            # 长度不符

    inner = BadEmbedder()
    backend = SqliteCacheBackend(str(tmp_path / "c.db"))
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    cached.embed_texts(["a"])
    assert backend.stats()["entries"] == 0


def test_attributes_pass_through(tmp_path):
    inner, cached = _mk(tmp_path)
    assert cached.dim == 4
    assert cached.embed_query("q") == inner._vec("q")


def test_noop_backend_disables_caching(tmp_path):
    inner = RecordingEmbedder()
    cached = CachedEmbedder(inner, NoCacheBackend(), model="m1", truncate_chars=2000)
    cached.embed_texts(["a"])
    cached.embed_texts(["a"])
    assert len(inner.calls) == 2
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd backend && python -m pytest tests/test_cached_embedder.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.cached_embedder'`

- [ ] **Step 3: 实现 CachedEmbedder**

创建 `backend/app/services/cached_embedder.py`：

```python
"""向量的内容寻址缓存装饰器。

per-text 而非 per-batch：否则批次边界一变（embed_batch_size 调整、上游 chunk
数量变化）就全部 miss。

缓存的是后端返回的原始维度向量。4096→1024 的运行时截断发生在消费侧（原向量作为
真相源保留不改写），因此本层与维度决策相互独立。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.core.cache import embed_key


class CachedEmbedder:
    def __init__(self, inner: Any, backend: Any, *, model: str,
                 truncate_chars: int) -> None:
        self._inner = inner
        self._backend = backend
        self._model = model
        self._truncate_chars = truncate_chars

    def __getattr__(self, name: str) -> Any:
        # dim / embed_query / model_status 身份绑定等一律透传。
        return getattr(self._inner, name)

    def _key(self, text: str) -> str:
        # 必须对截断后的文本取键——后端内部同样只发送截断后的内容。
        return embed_key(self._model, text[:self._truncate_chars])

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        texts = list(texts)
        if not texts:
            return []
        keys = [self._key(t) for t in texts]
        cached: Dict[str, List[float]] = {}
        for key in set(keys):
            try:
                raw = self._backend.get(key)
            except Exception:      # 缓存故障退化为 miss，绝不影响主流程
                raw = None
            if raw is not None:
                vec = _decode(raw)
                if vec is not None:
                    cached[key] = vec

        # 未命中的去重后按原序请求：同批重复文本只打一次后端。
        missing: List[str] = []
        missing_keys: List[str] = []
        seen = set()
        for text, key in zip(texts, keys):
            if key in cached or key in seen:
                continue
            seen.add(key)
            missing.append(text)
            missing_keys.append(key)

        if missing:
            vectors = self._inner.embed_texts(missing)
            # 长度不符说明后端异常——不写缓存，也不假装对齐。
            if len(vectors) == len(missing):
                for key, vec in zip(missing_keys, vectors):
                    cached[key] = list(vec)
                    try:
                        self._backend.put(key, _encode(vec), tag=self._model)
                    except Exception:
                        pass
            else:
                return list(vectors)

        return [cached[key] for key in keys]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]


def _encode(vector: Any) -> str:
    import json
    return json.dumps([float(x) for x in vector])


def _decode(raw: str) -> Any:
    import json
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    return [float(x) for x in value]
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd backend && python -m pytest tests/test_cached_embedder.py -v
```

Expected: 7 passed

- [ ] **Step 5: 在 make_embedder 里接线，并加 cache 开关**

修改 `backend/app/services/embedding.py` 的 `make_embedder`：签名新增 `cache` 形参，
并在返回 `DashscopeEmbedder` 的分支上包装。把该函数的签名与 `configured` 分支替换为：

```python
def make_embedder(
    settings: Settings,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    cache: bool = True,
) -> Embedder:
```

并把 `if configured:` 分支体替换为：

```python
    if configured:
        from app.services.embedding_dashscope import DashscopeEmbedder
        embedder: Embedder = DashscopeEmbedder(
            settings,
            base_url=base_url_value,
            api_key=api_key_value,
            model=model_value,
        )
        if cache:
            # 包装置于 bind_model_status_identity 之内层，不破坏身份绑定与
            # model_error 上报通道。
            from app.core.cache import make_cache_backend
            from app.services.cached_embedder import CachedEmbedder
            embedder = CachedEmbedder(
                embedder,
                make_cache_backend(settings),
                model=model_value or "",
                truncate_chars=int(getattr(settings, "embed_truncate_chars", 2000)),
            )
        return bind_model_status_identity(embedder, config)
```

- [ ] **Step 6: 健康探针绕过缓存**

修改 `backend/app/services/model_status.py` 第 240 行附近的 `make_embedder(` 调用，
补上 `cache=False`：

```python
            make_embedder(
                settings,
                provider=cfg.provider,
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
                cache=False,   # 健康探针必须打到真实服务：命中缓存会造成假绿
            )
```

> 若该调用的实参与上面不完全一致，只追加 `cache=False,` 一行，其余保持原样。

- [ ] **Step 7: 加健康探针守卫测试**

追加到 `backend/tests/test_cached_embedder.py`：

```python
def test_health_probe_bypasses_cache():
    """模型故障时探针若命中缓存会显示假绿——必须绕过。

    变异验证：把 model_status.py 里的 cache=False 改成 True 后，本测试必须转红。
    """
    import inspect

    from app.services import model_status

    src = inspect.getsource(model_status)
    idx = src.find("make_embedder(")
    assert idx != -1, "model_status 里找不到 make_embedder 调用"
    window = src[idx:idx + 400]
    assert "cache=False" in window, "健康探针未绕过缓存，模型故障会被缓存掩盖成假绿"


def test_make_embedder_returns_uncached_when_cache_false():
    from app.core.config import Settings
    from app.services.embedding import make_embedder

    e = make_embedder(Settings(), cache=False)
    assert e.__class__.__name__ != "CachedEmbedder"
```

- [ ] **Step 8: 变异验证健康探针守卫**

```bash
cd backend
grep -c "cache=False" app/services/model_status.py      # 必须输出 1
perl -pi -e 's/cache=False/cache=True/' app/services/model_status.py
python -m pytest tests/test_cached_embedder.py::test_health_probe_bypasses_cache -v
# Expected: FAILED
git checkout app/services/model_status.py
python -m pytest tests/test_cached_embedder.py -v
# Expected: 9 passed
```

- [ ] **Step 9: 跑既有 embedding / model_status 测试**

```bash
cd backend && python -m pytest tests/test_embedding.py tests/test_model_status_service.py tests/test_source_embedding_service.py -v
```

Expected: 全部通过

- [ ] **Step 10: 提交**

```bash
git add backend/app/services/cached_embedder.py backend/app/services/embedding.py backend/app/services/model_status.py backend/tests/test_cached_embedder.py
git commit -m "feat(cache): embedding 内容寻址缓存，健康探针显式绕过"
```

---

### Task 6: 可观测 —— 命中率统计与缓存现状查询

**Files:**
- Modify: `backend/app/core/cache/backend.py`（`NoCacheBackend` 补 `stats`）
- Modify: `backend/app/core/cache/sqlite_backend.py`（`stats` 增加命中计数）
- Test: `backend/tests/test_cache_observability.py`

**Interfaces:**
- Consumes: Task 2 的 `SqliteCacheBackend.stats()`
- Produces: `stats()` 返回 `{"entries": int, "bytes": int, "by_tag": dict, "hits": int, "misses": int, "hit_rate": float}`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_cache_observability.py`：

```python
"""没有埋点就无法证明缓存在工作——命中率是决定何时清理/是否有效的唯一依据。"""
from app.core.cache.sqlite_backend import SqliteCacheBackend


def _mk(tmp_path):
    return SqliteCacheBackend(str(tmp_path / "c.db"))


def test_stats_counts_hits_and_misses(tmp_path):
    c = _mk(tmp_path)
    c.get("absent")                 # miss
    c.put("k", "v")
    c.get("k")                      # hit
    c.get("k")                      # hit
    s = c.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert abs(s["hit_rate"] - 2 / 3) < 1e-9


def test_expired_read_counts_as_miss(tmp_path):
    import time

    c = SqliteCacheBackend(str(tmp_path / "c.db"), ttl_seconds=0.3)
    c.put("k", "v")
    time.sleep(0.4)
    assert c.get("k") is None
    assert c.stats()["misses"] == 1 and c.stats()["hits"] == 0


def test_hit_rate_is_zero_when_no_reads(tmp_path):
    assert _mk(tmp_path).stats()["hit_rate"] == 0.0
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd backend && python -m pytest tests/test_cache_observability.py -v
```

Expected: FAIL — `KeyError: 'hits'`

- [ ] **Step 3: 实现命中计数**

在 `backend/app/core/cache/sqlite_backend.py` 的 `__init__` 末尾追加：

```python
        # 进程内命中计数（不落盘：重启归零即可，用于观察当前进程的缓存效用）。
        self._hits = 0
        self._misses = 0
```

在 `get()` 中记账 —— 把 `get` 方法体替换为：

```python
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
                self._delete(db, [key])
                self._misses += 1
                return None
            if now - row["used_at"] > self.refresh_window:      # 粗粒度 LRU
                db.execute("UPDATE cache SET used_at=? WHERE key=?", (now, key))
            self._hits += 1
            return row["value"]
```

把 `stats()` 方法体替换为：

```python
    def stats(self) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM cache").fetchone()
            by_tag = {r["tag"]: r["n"] for r in db.execute(
                "SELECT tag, COUNT(*) n FROM cache GROUP BY tag").fetchall()}
        reads = self._hits + self._misses
        return {
            "entries": row["n"], "bytes": row["b"], "by_tag": by_tag,
            "hits": self._hits, "misses": self._misses,
            "hit_rate": (self._hits / reads) if reads else 0.0,
        }
```

- [ ] **Step 4: 让 NoCacheBackend 完整实现 CacheAdmin**

在 `backend/app/core/cache/backend.py` 的 `NoCacheBackend` 中追加：

```python
    # NoCacheBackend 顺带实现 CacheAdmin（零成本）。但这**不意味着** stats/
    # evict_tag 是必需能力：只实现 CacheBackend 两个方法的后端（例如把 TTL 与
    # LRU 交给服务端配置的 Redis 后端）是完全合法的。消费侧必须
    # isinstance(backend, CacheAdmin) 探测后再调用，见 test_cache_admin_is_optional。
    def evict_tag(self, tag: str) -> int:
        return 0

    def clear(self) -> int:
        return 0

    def stats(self) -> dict:
        return {"entries": 0, "bytes": 0, "by_tag": {},
                "hits": 0, "misses": 0, "hit_rate": 0.0}
```

- [ ] **Step 5: 加「CacheAdmin 必须保持可选」的守卫**

追加到 `backend/tests/test_cache_observability.py`：

```python
def test_cache_admin_is_optional_and_consumers_probe_before_calling():
    """只实现 CacheBackend 的后端必须能正常工作——这是"将来能换 Redis"的命脉。

    Redis 后端只需 get/put 两个方法（TTL 走 SET ... EX，容量与 LRU 走 redis.conf
    的 maxmemory + maxmemory-policy），不实现 CacheAdmin 是合法且预期的形态。
    任何无条件调用 backend.stats()/evict_tag() 的消费侧代码都会在换后端时崩溃。
    """
    from pathlib import Path

    from app.core.cache import CacheAdmin, CacheBackend

    class OnlyGetPut:
        def __init__(self):
            self._d = {}

        def get(self, key):
            return self._d.get(key)

        def put(self, key, value, tag=""):
            self._d[key] = value

    minimal = OnlyGetPut()
    assert isinstance(minimal, CacheBackend), "两个方法就该满足 CacheBackend"
    assert not isinstance(minimal, CacheAdmin), "CacheAdmin 必须保持可选"

    # 消费侧不得无条件调用运维方法。扫描 app/ 下对 backend 运维方法的裸调用。
    backend_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in backend_dir.rglob("*.py"):
        if "__pycache__" in path.parts or "core/cache/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for call in (".stats()", ".evict_tag(", ".clear()"):
            if call in text and "CacheAdmin" not in text:
                offenders.append(f"{path.name}: {call}")
    assert not offenders, (
        "疑似无条件调用缓存运维方法，换成只实现 CacheBackend 的后端会崩：\n  "
        + "\n  ".join(offenders)
    )
```

> `isinstance` 对 `runtime_checkable` Protocol **只检查方法名是否存在，不检查签名**。
> 这是 Python 的已知限制——所以契约测试套件不是可选项，它才是行为正确性的真正保障。

- [ ] **Step 6: 运行测试**

```bash
cd backend && python -m pytest tests/test_cache_observability.py tests/test_cache_sqlite_backend.py tests/test_cache_backend_contract.py -v
```

Expected: 全部通过。若 `test_cache_admin_is_optional_and_consumers_probe_before_calling`
报出 offenders，说明确有消费侧裸调用运维方法——改为 `isinstance(backend, CacheAdmin)`
探测后再调，不要放宽守卫。

- [ ] **Step 7: 提交**

```bash
git add backend/app/core/cache/ backend/tests/test_cache_observability.py
git commit -m "feat(cache): 命中率统计，并锁定 CacheAdmin 的可选性"
```

---

### Task 7: 顺带修复 —— file_hash 索引 + UI 上传同 notebook 去重

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py:15`（`SCHEMA_VERSION`）、末尾新增 `_migration_24`
- Modify: `backend/app/services/source_ingestion.py:339-378`（`upload_sources`）
- Test: `backend/tests/test_upload_dedup.py`

**Interfaces:**
- Consumes: `self.sources`、`maintenance.source_id_by_hash`
- Produces: `upload_sources` 对同 notebook 内已存在的相同 `file_hash` 不再新建源，直接返回既有源

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_upload_dedup.py`：

```python
"""UI 上传的同 notebook 去重——对齐 batch_ingest 既有行为。

跨 notebook 刻意不去重：用户通常确实想在自己库里拥有这份文件，且跨用户共享
source 行会引爆权限、删除级联与归属问题。
"""
from app.models.sources import UploadedSourceFile


def _upload(repo, notebook_id, name="a.txt", content=b"hello world"):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name=name, content_type="text/plain", content=content)],
        scheduler=None,
    )


def test_same_content_twice_in_one_notebook_creates_one_source(repo, notebook_id):
    first = _upload(repo, notebook_id)
    second = _upload(repo, notebook_id)
    assert first[0].id == second[0].id, "同 notebook 内相同内容应复用既有源"
    assert len(repo.list_sources(notebook_id)) == 1


def test_different_content_still_creates_a_new_source(repo, notebook_id):
    _upload(repo, notebook_id, content=b"one")
    _upload(repo, notebook_id, content=b"two")
    assert len(repo.list_sources(notebook_id)) == 2


def test_same_content_in_another_notebook_is_not_deduped(repo, notebook_id, other_notebook_id):
    a = _upload(repo, notebook_id)
    b = _upload(repo, other_notebook_id)
    assert a[0].id != b[0].id, "跨 notebook 刻意不去重"
```

> **实现者注意**：`repo` / `notebook_id` / `other_notebook_id` fixture 需按本仓库
> 既有测试的写法提供。先在 `backend/tests/` 下找一个已有的、构造真实
> `SQLiteRepository` 与 notebook 的测试（例如 `test_chunk_store_component.py`
> 或 `test_source_embedding_service.py`）照搬其 fixture 构造方式，不要自造新模式。

- [ ] **Step 2: 运行测试验证失败**

```bash
cd backend && python -m pytest tests/test_upload_dedup.py -v
```

Expected: FAIL —— 第一项因创建了两个源而失败

- [ ] **Step 3: 加 file_hash 索引迁移**

在 `backend/app/repositories/sqlite/migrations.py` 末尾（`_migration_23` 之后、
`_recover_interrupted_jobs` 之前）新增：

```python
    def _migration_24(self) -> None:
        """按内容哈希查源（上传去重 / batch_ingest 续跑）此前是全表扫。"""
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_notebook_file_hash "
                "ON sources(notebook_id, file_hash)"
            )
```

并把第 15 行改为：

```python
SCHEMA_VERSION = 24
```

- [ ] **Step 4: 实现上传去重**

在 `backend/app/services/source_ingestion.py` 的 `upload_sources` 中，把
`digest = hashlib.sha256(file.content).hexdigest()` 之后、`stored_path = ...` 之前
插入去重短路：

```python
            digest = hashlib.sha256(file.content).hexdigest()
            # 同 notebook 内相同内容直接复用既有源，与 batch_ingest 的 already_ingested
            # 行为一致（此前 UI 路径会建出重复源）。跨 notebook 刻意不去重。
            existing_id = self.sources.source_id_by_hash(notebook_id, digest)
            if existing_id:
                imported.append(self.sources.get_source(existing_id))
                continue
```

> `source_id_by_hash` 目前挂在 `maintenance` 上
> （`backend/app/repositories/sqlite/maintenance.py`）。若 `self.sources` 上没有该
> 方法，按本仓库的 facade 约定，在 `SourceStore` 上新增一个**一跳委托**的同名方法，
> 并同步登记到 `backend/app/repositories/ownership_manifest.py`——不要在服务层直接写
> SQL，也不要跨层直接摸 maintenance。

- [ ] **Step 5: 运行测试**

```bash
cd backend && python -m pytest tests/test_upload_dedup.py -v
```

Expected: 3 passed

- [ ] **Step 6: 跑 schema 与架构守卫**

```bash
cd backend && python -m pytest tests/test_schema_registry_service.py tests/test_repository_surface_contract.py tests/test_architecture_module_boundaries.py -v
```

Expected: 全部通过。若 `test_repository_surface_contract.py` 因新增委托方法而红，按其
提示重新生成契约夹具（只跑 `--rebaseline`，不要手改冻结产物）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/sqlite/migrations.py backend/app/services/source_ingestion.py backend/app/repositories/ backend/tests/test_upload_dedup.py
git commit -m "feat(sources): file_hash 索引与 UI 上传同 notebook 去重"
```

---

### Task 8: 全量验证与文档

**Files:**
- Modify: `README.md`、`README_zh.md`（新配置项说明）

- [ ] **Step 1: 跑完整后端测试**

```bash
cd backend && python -m pytest -x -q
```

Expected: 全绿。若有失败，先判断是否本计划引入——本仓库存在已知偶发失败，
不要盲目认领（对比 merge-base 的运行结果）。

- [ ] **Step 2: 确认测试进程确实没吃缓存**

```bash
cd backend && python -m pytest tests/test_cache_factory.py -v -s
ls -la ../.local/llm_cache_v2.db 2>/dev/null && echo "警告：测试写出了缓存文件" || echo "OK：测试未产生缓存文件"
```

Expected: `OK：测试未产生缓存文件`（`tmp_path` 下的除外）

- [ ] **Step 3: 补文档**

在 `README.md` 与 `README_zh.md` 的环境变量说明处，加入四个配置项（保持通用产品口径，
不写本机绝对路径）：

- `LLM_CACHE_ENABLED`（默认 `true`）：内容寻址缓存总开关。相同内容的重复抽取/
  向量化直接复用既有结果。
- `LLM_CACHE_PATH`（默认 `.local/llm_cache_v2.db`）：缓存文件位置。
- `LLM_CACHE_SIZE_LIMIT`（默认 2GiB）：容量上限，超出后按最近最少使用淘汰。
- `LLM_CACHE_TTL_DAYS`（默认 90）：条目寿命上限。

- [ ] **Step 4: 提交**

```bash
git add README.md README_zh.md
git commit -m "docs: 内容寻址缓存的配置项说明"
```

- [ ] **Step 5: 提 PR**

```bash
git fetch origin && git rebase origin/master
git push -u origin HEAD
gh pr create --base master --title "feat(cache): 内容寻址抽取缓存" --body "$(cat <<'EOF'
## 背景

大规模使用下内容相同的文件被反复完整处理。实测 extract 占 pipeline 93.4%
（275 个配对样本上是 parse 的 182 倍），故缓存目标锁定 LLM 与 embedding 调用，
parse 缓存不做。

## 变更

- 新增高内聚模块 `app/core/cache/`：唯一构造点 + 两层 Protocol（`CacheBackend`
  仅 get/put，运维能力归可选的 `CacheAdmin`），换组件为局部改动
- 自研 `SqliteCacheBackend`：TTL + 容量上限（粗粒度 LRU）+ 按 tag 清空
- LLM 侧零装饰器接入（产品路径本就走带缓存钩子的 `OpenAICompatibleClient`）
- 新增 `CachedEmbedder`，健康探针显式绕过缓存
- 顺带：`sources` 加 `file_hash` 索引，UI 上传同 notebook 去重

## 设计文档

`docs/superpowers/specs/2026-07-22-content-addressed-extraction-cache-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## 计划自检

**1. Spec 覆盖度**

| spec 要求 | 对应任务 |
|---|---|
| 自研 SqliteCacheBackend（TTL/容量/tag） | Task 2 |
| Protocol 分两层 | Task 1 |
| 唯一构造点 | Task 3 |
| 策略与存储分离（`policy.py`） | Task 3 Step 1 |
| 导入守卫 + 变异验证 | Task 4 |
| Protocol 契约测试套件 | Task 1 Step 3 / Task 2 Step 5 |
| CacheAdmin 保持可选（Redis 替换路径） | Task 1 Step 3（`MinimalBackend`）/ Task 6 Step 5 |
| 差分测试（diskcache oracle） | Task 2 Step 7 |
| 空响应保护（保持既有） | Task 3 Step 9-10 |
| 测试隔离 | Task 3 Step 6 / Task 8 Step 2 |
| 默认开 + 四个配置项 | Task 3 Step 5 |
| embed per-text key + 截断后取哈希 | Task 5 |
| 批量部分命中顺序对齐 + 重复文本 | Task 5 Step 1 |
| 健康探针绕过缓存 | Task 5 Step 6-8 |
| 属性透传 | Task 5 Step 1（`test_attributes_pass_through`） |
| 可观测（命中率） | Task 6 |
| file_hash 索引 + 上传去重 | Task 7 |
| 文档 | Task 8 |

**2. 未覆盖项**：spec 提到的"事件日志附 `cache_hits`/`cache_misses`"未单独成任务——
Task 6 提供了 `stats()` 数据源，把它接进 pipeline 事件属于可选增强，故意留到真机
验证命中率之后再决定，避免此刻为未证实的收益增加埋点。

**3. 类型一致性**：`make_cache_backend(settings) -> CacheBackend`、
`llm_key(model, messages, schema_hint) -> str`、`embed_key(model, truncated_text) -> str`、
`CachedEmbedder(inner, backend, *, model, truncate_chars)` 在各任务间引用一致。
`stats()` 的返回结构在 Task 2 定义、Task 6 扩展，`NoCacheBackend` 同步补齐。
