# SQLite 连接复用（根治 batch_ingest fd 耗尽）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `SqliteDatabase.connect()` 从"每次新建连接"改成"每线程复用一条连接 + 嵌套事务守卫"，根治 `batch_ingest all` 高并发下的文件描述符耗尽（EMFILE），并消除每操作反复建连接/PRAGMA/mmap 的开销。

**Architecture:** 连接对象改用 `sqlite3.Connection` 子类 `_Conn`，其 `__enter__/__exit__` 带嵌套深度守卫（只最外层 commit/rollback）；`connect()` 用 `threading.local` 缓存并复用连接。233 处 `with connect() as db:` 与 6 处裸 `conn=connect()` 零改动、事务语义等价。新增 `close_local()` 显式释放口（生产调用者=knowledge_lifecycle 临时表清理）。`cache_size` 提为可配 env 控内存。纯连接层、无 schema 变更。

**Tech Stack:** Python 3.13、sqlite3（WAL）、pydantic-settings v2、pytest。

**Spec:** `docs/superpowers/specs/2026-07-13-sqlite-connection-reuse-design.md`

## Global Constraints

- 交互语言中文；commit message 结尾 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- **无 schema 变更**：不加 `_migration_N`、不 bump `SCHEMA_VERSION`（纯连接层）。
- 新增 env 变量**必须**用 `Field(default, validation_alias="ENV_NAME")`（pydantic-settings v2，`env=` 失效）。
- 所有命令从 `backend/` 目录运行（`cd backend`）。
- **不变量**（测试守护）：INV-1 同线程复用同一条连接；INV-2 连接不跨线程；INV-3 连接数=O(线程)不随操作数增长；INV-4 单层 `with` 成功提交/异常回滚；INV-5 嵌套只最外层提交、内层异常整体回滚；INV-6 `close_local()` 关闭并清除当前线程连接；INV-7 禁止对复用连接裸 `.close()`（释放走 `close_local()`）。
- DRY / YAGNI / TDD / 频繁提交。

---

### Task 1: `Settings.sqlite_cache_size_kb` 配置项

**Files:**
- Modify: `backend/app/core/config.py:218`
- Test: `backend/tests/test_sqlite_cache_size_config.py`（Create）

**Interfaces:**
- Produces: `Settings().sqlite_cache_size_kb -> int`（默认 `-16384`；env `SQLITE_CACHE_SIZE_KB`）。Task 2 的 `_new_connection` 消费它。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_sqlite_cache_size_config.py`:

```python
from app.core.config import Settings


def test_sqlite_cache_size_default(monkeypatch):
    monkeypatch.delenv("SQLITE_CACHE_SIZE_KB", raising=False)
    assert Settings().sqlite_cache_size_kb == -16384


def test_sqlite_cache_size_env_override(monkeypatch):
    monkeypatch.setenv("SQLITE_CACHE_SIZE_KB", "-8192")
    assert Settings().sqlite_cache_size_kb == -8192
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_sqlite_cache_size_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'sqlite_cache_size_kb'`

- [ ] **Step 3: 加字段**

在 `backend/app/core/config.py` 的 `db_busy_timeout_ms` 行（218）后新增一行：

```python
    db_busy_timeout_ms: int = Field(30000, validation_alias="DB_BUSY_TIMEOUT_MS")
    # 复用连接下每条连接长期持有 page cache;总内存 = 连接数 × |cache_size|。
    # 负值=KB。默认 16MB(低于旧 64MB)以在 O(线程数) 条连接下控总内存;可按部署上调。
    sqlite_cache_size_kb: int = Field(-16384, validation_alias="SQLITE_CACHE_SIZE_KB")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_sqlite_cache_size_config.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/core/config.py tests/test_sqlite_cache_size_config.py
git commit -m "$(printf 'feat(config): add sqlite_cache_size_kb (default -16384)\n\nConfigurable per-connection SQLite page cache; reused long-lived\nconnections make total memory = conns x |cache_size|, so lower the\ndefault from 64MB to 16MB and expose SQLITE_CACHE_SIZE_KB.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: `_Conn` 子类 + thread-local `connect()` + `close_local()`

**Files:**
- Modify: `backend/app/repositories/sqlite/database.py`（整文件重写，见 Step 3）
- Test: `backend/tests/test_sqlite_connection_reuse.py`（Create）

**Interfaces:**
- Consumes: `Settings().sqlite_cache_size_kb`（Task 1）。
- Produces:
  - `SqliteDatabase.connect() -> _Conn`（thread-local 复用；同线程多次返回同一对象）。
  - `SqliteDatabase.close_local() -> None`（关闭并清除当前线程连接；幂等）。Task 3/4 消费。
  - `SqliteDatabase._new_connection() -> _Conn`（内部；测试用 monkeypatch 计数）。
  - `_Conn(sqlite3.Connection)`：`__enter__` 返回 self、`__exit__` 深度守卫（最外层 commit/rollback）。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_sqlite_connection_reuse.py`:

```python
import sqlite3
import threading
import concurrent.futures as cf

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'reuse.db'}")
    return SqliteDatabase(Settings(), tmp_path)


def _rows_from_other_thread(db, sql):
    """从另一条连接(另一线程)读,验证对其它连接的可见性(WAL 快照)。"""
    out = {}
    def run():
        out["rows"] = db.connect().execute(sql).fetchall()
    t = threading.Thread(target=run)
    t.start()
    t.join()
    return out["rows"]


def test_reuse_same_thread_returns_same_connection(db):  # INV-1
    c1 = db.connect()
    c2 = db.connect()
    assert c1 is c2


def test_new_connection_called_once_per_thread(db, monkeypatch):  # INV-1
    calls = []
    orig = db._new_connection
    monkeypatch.setattr(db, "_new_connection", lambda: (calls.append(1), orig())[1])
    db.connect()
    db.connect()
    with db.connect() as c:
        c.execute("SELECT 1")
    assert len(calls) == 1


def test_isolation_across_threads(db):  # INV-2
    grabbed = {}
    def grab(name):
        grabbed[name] = db.connect()
    for name in ("a", "b"):
        t = threading.Thread(target=grab, args=(name,))
        t.start()
        t.join()
    main = db.connect()
    assert grabbed["a"] is not grabbed["b"]
    assert grabbed["a"] is not main


def test_with_commits_on_success(db):  # INV-4
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")
    rows = _rows_from_other_thread(db, "SELECT x FROM t")
    assert [r["x"] for r in rows] == [1]


def test_with_rolls_back_on_exception(db):  # INV-4
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute("INSERT INTO t VALUES (99)")
            raise RuntimeError("boom")
    rows = _rows_from_other_thread(db, "SELECT x FROM t")
    assert rows == []


def test_nested_inner_does_not_commit_early(db):  # INV-5
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with db.connect() as outer:
        outer.execute("INSERT INTO t VALUES (1)")
        with db.connect() as inner:
            assert inner is outer
            inner.execute("INSERT INTO t VALUES (2)")
        # 内层退出但外层未退出 → 另一连接尚看不到(未提交)
        assert _rows_from_other_thread(db, "SELECT x FROM t") == []
    # 外层退出后才提交
    rows = _rows_from_other_thread(db, "SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1, 2]


def test_nested_inner_failure_rolls_back_all(db):  # INV-5
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.connect() as outer:
            outer.execute("INSERT INTO t VALUES (1)")
            with db.connect():
                outer.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("boom")
    assert _rows_from_other_thread(db, "SELECT x FROM t") == []


def test_close_local_releases_and_rebuilds(db):  # INV-6
    c1 = db.connect()
    db.close_local()
    with pytest.raises(sqlite3.ProgrammingError):
        c1.execute("SELECT 1")           # 原连接已关
    c2 = db.connect()
    assert c2 is not c1
    c2.execute("SELECT 1")               # 新连接可用
    db.close_local()
    db.close_local()                     # 幂等,不抛


def test_connection_count_bounded_under_concurrency(db, monkeypatch):  # INV-3
    lock = threading.Lock()
    calls = []
    orig = db._new_connection
    def counting():
        with lock:
            calls.append(1)
        return orig()
    monkeypatch.setattr(db, "_new_connection", counting)
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    K = 4
    def op(i):
        for _ in range(20):
            with db.connect() as c:
                c.execute("INSERT INTO t VALUES (?)", (i,))
    with cf.ThreadPoolExecutor(max_workers=K) as ex:
        list(ex.map(op, range(K * 5)))
    # 连接数 = 用到的线程数(≤ K 个 worker + 1 主线程),不随 400 次操作增长
    assert len(calls) <= K + 1


def test_write_lock_serialization_preserved(db):  # 回归 write() 语义
    with db.write() as d:
        d.execute("CREATE TABLE t(x INTEGER)")
    def worker(i):
        with db.write() as d:
            d.execute("INSERT INTO t VALUES (?)", (i,))
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(200)))
    rows = _rows_from_other_thread(db, "SELECT COUNT(*) AS n FROM t")
    assert rows[0]["n"] == 200
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_sqlite_connection_reuse.py -v`
Expected: FAIL — `test_reuse_same_thread_returns_same_connection`（现状每次新建，`c1 is c2` 为假）、`test_close_local_releases_and_rebuilds`（`AttributeError: ... 'close_local'`）等。

- [ ] **Step 3: 重写 database.py**

将 `backend/app/repositories/sqlite/database.py` **整文件替换**为：

```python
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
```

> **⚠ 关键(Task 2 fix 已并入)**：`write()` 必须用**独立连接**、不复用读连接。若读写共用一条复用连接 + 深度守卫，节点向量 backfill 的「外层 `with connect()` 读 + 内层 `write()` 增量 flush」会被卷进同一事务、内层不再独立提交 → 外层中途崩溃则增量全丢（`test_node_embed_incremental` 失败）。独立连接保留每-write-独立-提交。INV-5 深度守卫只作用于 `connect()`（读路径）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_sqlite_connection_reuse.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 跑连接相关回归（快速冒烟）**

Run: `cd backend && python -m pytest tests/test_kg_repository.py tests/test_repository_facade_contract.py -q`
Expected: PASS（连接层改动不破坏既有 repository 契约）

- [ ] **Step 6: 提交**

```bash
cd backend && git add app/repositories/sqlite/database.py tests/test_sqlite_connection_reuse.py
git commit -m "$(printf 'feat(db): thread-local SQLite connection reuse with nested-txn guard\n\nSqliteDatabase.connect() now caches one _Conn per thread instead of\nopening a fresh WAL connection (up to 3 fds) every call. _Conn subclass\nguards nested `with conn:` so only the outermost commits/rolls back,\npreserving per-site transaction semantics with zero call-site changes.\nAdds close_local() to release+reset the current thread connection.\nCuts fd usage from O(ops) to O(threads); fixes batch_ingest EMFILE.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: facade `close_local()` delegate

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_connect` 方法后，约 890-891）
- Test: `backend/tests/test_sqlite_connection_reuse.py`（追加一个用例）

**Interfaces:**
- Consumes: `SqliteDatabase.close_local()`（Task 2）。
- Produces: `SQLiteRepository.close_local() -> None`。Task 4 的 facade wire `lambda: self.close_local()` 消费。

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_sqlite_connection_reuse.py` 末尾追加：

```python
def test_facade_close_local(tmp_path, monkeypatch):  # facade delegate
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'facade.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    c1 = repo._connect()
    repo.close_local()
    c2 = repo._connect()
    assert c2 is not c1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_sqlite_connection_reuse.py::test_facade_close_local -v`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute 'close_local'`

- [ ] **Step 3: 加 delegate**

在 `backend/app/services/sqlite_repository.py` 的 `_connect` 方法（`def _connect(self)` → `return self._runtime.database.connect()`）之后紧接着新增：

```python
    def close_local(self) -> None:
        """关闭并清除当前线程的复用 DB 连接(短命线程/大扫描/临时表清理)。
        委托 runtime-owned SqliteDatabase。见 [[sqlite 连接复用]] INV-6/7。"""
        self._runtime.database.close_local()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_sqlite_connection_reuse.py::test_facade_close_local -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/services/sqlite_repository.py tests/test_sqlite_connection_reuse.py
git commit -m "$(printf 'feat(repo): expose close_local() facade delegate\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: knowledge_lifecycle 裸 `close()` → `close_local()`（INV-7 + 临时表语义）

**Files:**
- Modify: `backend/app/services/knowledge_lifecycle.py`（`__init__` 参数/赋值；`scan_db.close()` @1831）
- Modify: `backend/app/services/repository_runtime.py`（`wire_knowledge_lifecycle` 签名 @721 + `KnowledgeLifecycleService(...)` 构造 @810）
- Modify: `backend/app/services/sqlite_repository.py`（facade `wire_knowledge_lifecycle` 调用 @453）
- Test: `backend/tests/test_sqlite_connection_reuse.py`（追加静态断言）

**Interfaces:**
- Consumes: `SQLiteRepository.close_local()`（Task 3）。
- Produces: `KnowledgeLifecycleService._close_local` seam。

**背景**：`knowledge_lifecycle.py:1831` 的 `scan_db.close()` **有副作用**——mention-alias DF 扫描用 `claim_name_rows` 建的**临时表靠连接关闭而蒸发**（见 1804-1805 注释）。复用连接后不能裸 close（会关掉线程复用连接却不清 `_local.conn` → 后续拿坏连接），但也不能简单删（临时表会残留）。用 `close_local()` 恰好兼得。

- [ ] **Step 1: 追加失败测试（INV-7 静态守卫）**

在 `backend/tests/test_sqlite_connection_reuse.py` 末尾追加：

```python
def test_no_bare_close_on_reused_conn_in_knowledge_lifecycle():  # INV-7
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "services" / "knowledge_lifecycle.py").read_text(encoding="utf-8")
    assert "scan_db.close()" not in src, "复用连接不得裸 close;用 self._close_local()"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_sqlite_connection_reuse.py::test_no_bare_close_on_reused_conn_in_knowledge_lifecycle -v`
Expected: FAIL（当前 `scan_db.close()` 仍在）

- [ ] **Step 3a: knowledge_lifecycle 注入 `close_local`**

在 `backend/app/services/knowledge_lifecycle.py` `__init__` 参数表，`connect:` 声明行后加一行：

```python
        connect: Callable[[], sqlite3.Connection],
        close_local: Callable[[], None],
        write: Callable[[], Any],
```

在赋值处 `self._connect = connect` 后加一行：

```python
        self._connect = connect
        self._close_local = close_local
```

- [ ] **Step 3b: `scan_db.close()` → `self._close_local()`**

`backend/app/services/knowledge_lifecycle.py:1830-1831`：

```python
            finally:
                scan_db.close()
```

改为：

```python
            finally:
                self._close_local()   # 关连接(临时表蒸发)+清 thread-local(下次 connect 重建)
```

- [ ] **Step 3c: repository_runtime `wire_knowledge_lifecycle` 透传**

在 `backend/app/services/repository_runtime.py` 的 `wire_knowledge_lifecycle` 签名里，`connect: Callable[[], Any],` 后加一行：

```python
        connect: Callable[[], Any],
        close_local: Callable[[], None],
        write: Callable[[], Any],
```

在其 `KnowledgeLifecycleService(...)` 构造里，`connect=connect,` 后加一行：

```python
            connect=connect,
            close_local=close_local,
            write=write,
```

- [ ] **Step 3d: facade 注入 `close_local`**

在 `backend/app/services/sqlite_repository.py` 的 `self._runtime.wire_knowledge_lifecycle(` 调用里，`connect=lambda: self._connect(),` 后加一行：

```python
            connect=lambda: self._connect(),
            close_local=lambda: self.close_local(),
            write=lambda: self._write(),
```

- [ ] **Step 4: 跑测试确认通过 + 构造回归**

Run: `cd backend && python -m pytest tests/test_sqlite_connection_reuse.py -v`
Expected: PASS（含新静态守卫）

Run: `cd backend && python -c "from app.core.config import Settings; from app.services.sqlite_repository import SQLiteRepository; import os; os.environ['DATABASE_URL']='sqlite:///:memory:'; SQLiteRepository(Settings()); print('wired ok')"`
Expected: 打印 `wired ok`（KnowledgeLifecycleService 新参数注入无缺参 TypeError）

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/services/knowledge_lifecycle.py app/services/repository_runtime.py app/services/sqlite_repository.py tests/test_sqlite_connection_reuse.py
git commit -m "$(printf 'fix(kg): use close_local() instead of bare close() on reused conn\n\nknowledge_lifecycle mention-alias scan relied on conn.close() to drop\nits temp table; with reused connections that bare close() would orphan\nthe thread-local (INV-7). Route through close_local() which closes the\nconn (temp table evaporates) and clears _local.conn so the next\nconnect() rebuilds. Injects close_local seam via wire_knowledge_lifecycle.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: 文档（`SQLITE_CACHE_SIZE_KB`）

**Files:**
- Modify: `.env.example`、`README.md`、`README_zh.md`（各补一行；保持通用部署口径）
- Test: 无（纯文档）

**Interfaces:** 无。

- [ ] **Step 1: `.env.example` 补一行**

在 `.env.example` 里 `DB_BUSY_TIMEOUT_MS` 附近（若无则数据库配置段）新增：

```bash
# SQLite per-connection page cache (KB; negative = KB). Connections are reused
# per-thread, so total memory ~= threads x |value|. Default 16MB; raise for
# read-heavy online serving, lower for many-worker batch ingest.
SQLITE_CACHE_SIZE_KB=-16384
```

- [ ] **Step 2: README.md / README_zh.md 补说明**

在两个 README 的环境变量/配置表里各加一行（通用口径，勿写机器特定路径）：

- `README.md`: `| SQLITE_CACHE_SIZE_KB | -16384 | Per-connection SQLite page cache in KB (negative = KB). Connections are reused per-thread; total memory ≈ threads × |value|. |`
- `README_zh.md`: `| SQLITE_CACHE_SIZE_KB | -16384 | 每连接 SQLite 页缓存(KB,负值=KB)。连接按线程复用,总内存≈线程数×|值|。 |`

（若两 README 用的是列表而非表格，则按其既有格式补一条等义说明。）

- [ ] **Step 3: 提交**

```bash
git add .env.example README.md README_zh.md
git commit -m "$(printf 'docs: document SQLITE_CACHE_SIZE_KB\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## 收尾（全部 Task 后）

- [ ] **全量回归**：`cd backend && python -m pytest -q`，期望零 fail（连接层改动波及面广，重点确认 ingest/kg/ask/repository 全绿）。
- [ ] **INV-7 全库复核**：`cd backend && grep -rn "\.close()" app/repositories app/services | grep -iv "workbook\|ReadOnlySQLiteInspector\|http\|client\|socket"`，确认无其它对复用连接的裸 close。
- [ ] **rebase → push → PR**：分支 rebase 到 `origin/master` 保持线性 → push → `gh pr create --base master`（合并按钮=Rebase and merge）。PR 描述含根因（fd 耗尽实证）+ 方案 A + 不变量 + "运维:先 `ulimit -n 65536` 已缓解，本 PR 根治"。

## Self-Review 记录

- **Spec 覆盖**：G1(fd O(线程))=Task 2；G2(性能/省 PRAGMA)=Task 2；G3(调用点零改动/语义等价)=Task 2 `_Conn` + INV-4/5 测试；G4(内存)=Task 1 cache_size。§4.1~4.4 全部落 Task 2。§6 裸 close=Task 4。§7 env=Task 1+Task 5。INV-1~7 全挂测试。
- **短命线程**（§6）：判定 embed daemon 天然有界、无需注入，`close_local` API 仍备（Task 2/3），生产调用者=Task 4。计划不含 embed daemon 注入（YAGNI）。
- **类型一致**：`close_local`/`_close_local`/`_new_connection`/`sqlite_cache_size_kb` 跨 Task 命名一致；wire 透传链 connect↔close_local 并列。
- **无占位符**：每步给完整代码/命令/期望输出。
