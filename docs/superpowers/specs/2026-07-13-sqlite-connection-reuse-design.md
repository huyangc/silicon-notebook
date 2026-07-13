# SQLite 连接复用（根治 batch_ingest fd 耗尽）设计

**状态**：已批准方案，待 review 本 spec
**日期**：2026-07-13
**分支**：`worktree-sqlite-conn-reuse`

## 1. 背景与根因

生产环境 `python scripts/batch_ingest.py all --workers 8`（近 4 万源批量）跑到中途开始刷屏：

```
sqlite3.OperationalError: unable to open database file      # job 线程 _connect 开不出 DB
OSError: [Errno 24] Too many open files: '.../batch_ingest/nb-*.jsonl'   # 主线程 _log open 崩
... 之后一堵 "cannot schedule new futures after shutdown"    # 主线程崩→解释器 finalize→在飞 job 刷墙
```

**根因 = 文件描述符（fd）耗尽（EMFILE）**，不是内存、不是线程数。证据链：

- `SqliteDatabase.connect()`（`backend/app/repositories/sqlite/database.py:23`）**每次调用新建一条 sqlite 连接**，无池化、无复用。WAL 模式下每连接最多占 **3 个 fd**（`db` + `-wal` + `-shm`）。
- 全库 **233 处** `with self.database.connect() as db:` + **6 处**裸 `conn = connect()`。而 `with sqlite3.Connection` 的 `__exit__` **只 commit/rollback 事务、并不 close 连接**（CPython sqlite3 语义），连接靠 GC 才回收 → 高并发下连接积压。
- `--workers 8` → 8 job 线程 + 8 后台 embed daemon + 每源 embed 子池（emb-el/kg/rel/ck 各最多 8）+ window 池，每个线程在一次 `process_source`/`run_extraction` 里**嵌套多次** `get_source`/`source_elements`/`store_kg`，每次一条新连接 × 3 fd，叠加 LLM/embed 的 HTTP socket fd + manifest/log 文件句柄 → 顶穿 `ulimit -n`（Linux 默认常见 1024）。
- fd 触顶后：job 线程 `_connect` 报 `unable to open database file`；**主线程** `run_all` 的 `_log` 写 manifest `open()` 抛 `OSError: Too many open files`（未捕获）→ 主线程崩 → `run_all` 抛出 → 进程 finalize → 剩余在飞 job 的 `submit_window` 撞上 finalize 中的窗口池，刷 `cannot schedule new futures after shutdown`。

`--workers 4` 侥幸没撞峰值，8 撞了。fd 峰值 ≈ `O(并发线程数 × 每操作嵌套连接数 × 3)`，与源总数无关，但超大批量让"总有一刻峰值顶穿"变成必然。

**每次 `connect()` 的重量级开销**（`database.py:24-34`）也是性能浪费：`sqlite3.connect` + 建 `-shm` 映射 + `PRAGMA mmap_size=268435456`（256MB mmap）+ `PRAGMA cache_size=-65536`（64MB page cache）+ 另外 6 条 PRAGMA，全部**每操作反复建了又拆**。

## 2. 目标 / 非目标

**目标**

- G1：进程 fd 用量从 `O(操作数)` 降到 `O(线程数)`，稳定有界，`batch_ingest all --workers 8/16` 大批量不再 EMFILE。
- G2：性能提升——每线程只付一次"建连接 + 8 PRAGMA + mmap + cache"，之后复用（含 statement cache）。
- G3：233 处 `with connect() as db:` 与 6 处裸 `conn = connect()` 的**事务语义完全不变**，调用点零改动。
- G4：内存不因复用而膨胀（长命连接总内存 ≤ 现状并发峰值）。

**非目标**

- 不改 `write_lock` 串行写模型、不改 WAL、不换存储引擎。
- 不做连接坏死自愈重试（YAGNI，见 §11）。
- 不改 `batch_ingest` 的并发编排 / embed 子池 fan-out 结构（削 fan-out 是可选后续，不在本 spec）。
- 不改主线程 `_log` 的 OSError 兜底（fd 根治后不再触发；如需额外健壮化另开）。

## 3. 设计总览（方案 A：thread-local 连接复用）

核心：`SqliteDatabase` 为**每个线程**缓存一条长命 sqlite 连接，`connect()` 复用它而非新建。为在复用同一条连接时保住"每个 `with` 块=独立事务边界"的语义，连接对象用 `sqlite3.Connection` 子类 `_Conn`，其 `__enter__/__exit__` 带**嵌套深度守卫**：只有最外层 `with` 才 commit/rollback。

- 读、写共用这一条 thread-local 复用连接；写仍走 `write()` + `write_lock`（`RLock`）串行化，写事务嵌套由深度守卫收敛到最外层提交。
- fd = `1 × 活跃线程数`（稳定几十）。
- 长命池线程（KG job 池 / window 池）自然复用、连接长期存活。
- 短命池线程（embed 子池等 `with ThreadPoolExecutor` 每源新建销毁的）在 worker 任务边界 `try/finally: db.close_local()` 确定性释放，不靠 GC。

## 4. 组件与接口

全部改动集中在 `backend/app/repositories/sqlite/database.py`（外加 §6 列出的短命池 worker 边界）。

### 4.1 `_Conn(sqlite3.Connection)` —— 嵌套事务守卫

```python
class _Conn(sqlite3.Connection):
    """复用连接的事务包装:嵌套 `with conn:` 只在最外层 commit/rollback,
    使一条 thread-local 连接跨 233 处 `with connect() as db:` 时,仍保持
    '每个 with 块=一个逻辑事务边界'的语义(最外层成功即提交/任一层异常即整体回滚)。
    不 override __init__(用 getattr 惰性属性),以兼容 sqlite3.connect(factory=...) 的调用签名。"""

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
        return False   # 不吞异常(与 sqlite3.Connection.__exit__ 一致)
```

**语义等价论证（读路径）**：现状每个 `with connect() as db:` 是独立连接独立事务——成功 commit、异常 rollback；嵌套调用是不同连接，各自回滚。复用 + 深度守卫后：最外层成功 commit、最外层异常 rollback；内层异常置 `_txn_failed` → 冒泡到最外层整体 rollback。两种模型下"写在异常时都被回滚、成功时都被提交"，对外可观察结果一致。**深度守卫只作用于 `connect()`（读路径）复用连接**。

**⚠反例与写路径例外（关键修正）**：上述"等价"在**写路径**有一个反例——**嵌套增量提交的崩溃恢复**。现状节点向量 backfill 是「外层 `with connect() as db:` 遍历（读连接 A） + 内层每批 `write()` flush（独立连接 B，立即提交）」；外层若中途崩溃，B 已提交的前几批**保留**、可续跑（见 `test_node_embed_incremental`、[[resumable-rebuild-stages-state]]、[[offline-batch-ingest-state]] PR#132）。若让读、写**共用**一条复用连接 + 深度守卫，内层 write 变 depth≥2 → 不再独立提交 → 外层崩则**全丢**。因此 **`write()` 必须用独立连接、不复用读连接**（见 §4.4），使每个 `write()` 保持独立提交语义。深度守卫仅用于 `connect()`（读嵌套无害、偶有 connect-写嵌套时防提前提交）。

### 4.2 `connect()` —— thread-local 复用

```python
def __init__(self, settings, root_dir):
    ...
    self._local = threading.local()

def _new_connection(self) -> _Conn:
    conn = sqlite3.connect(
        self.db_path,
        timeout=self.settings.db_busy_timeout_ms / 1000,
        factory=_Conn,                 # ← 返回带守卫的子类
        check_same_thread=True,        # 显式:连接不跨线程(thread-local 保证)
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(self.settings.db_busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA cache_size = {int(self.settings.sqlite_cache_size_kb)}")  # §7 可配,默认 -16384(16MB)
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")
    return conn

def connect(self) -> sqlite3.Connection:
    conn = getattr(self._local, "conn", None)
    if conn is None:
        conn = self._new_connection()
        self._local.conn = conn
    return conn
```

- 返回**真 `_Conn` 对象** → 233 处 `with connect() as db:`（db 即复用连接、块尾按深度守卫提交）和 6 处裸 `conn = connect()`（直接拿真连接用）**均零改动**。
- 同线程多次 `connect()` 返回同一条；不同线程各自 `threading.local` 独立 → `check_same_thread=True` 不误触。

### 4.3 `close_local()` —— 短命线程边界释放

```python
def close_local(self) -> None:
    """关闭并清除当前线程的复用连接。用于短命线程(embed 子池等)在任务结束时
    确定性归还 fd,不依赖 GC。长命池线程无需调用(连接长期复用)。幂等。"""
    conn = getattr(self._local, "conn", None)
    if conn is not None:
        self._local.conn = None
        try:
            conn.close()
        except sqlite3.Error:
            pass
```

facade `SqliteRepository` 增一个薄 delegate `close_local(self)` → `self._runtime.database.close_local()`，供服务层短命池 worker 调用。

### 4.4 `write()` —— 独立连接（保留增量提交崩溃恢复）

```python
@contextmanager
def write(self):
    """写事务:进程内写串行(write_lock)。每次用**独立新连接**(非线程复用读连接),
    使每个 write() 独立提交——保留嵌套增量提交(节点向量 backfill 每批 flush 独立
    落库、中断可续跑)的崩溃恢复语义(见 §4.1 反例)。用完即 close:写经 write_lock
    串行,写连接峰值 = 嵌套写深度(极小),fd 用完即还。"""
    with self.write_lock:
        conn = self._new_connection()
        try:
            with conn:               # _Conn 单层 depth0 → 独立 commit/rollback
                yield conn
        finally:
            conn.close()
```

**为何写不复用**：复用读连接会把内层 `write()` 卷进外层 `connect()` 的事务深度，使增量 flush 不再独立提交（§4.1 反例）。独立连接使每个 `write()`（无论被什么外层包裹、是否嵌套）都独立提交，**完全保留现状写语义**。`write_lock`（RLock）保证跨线程写串行；写连接峰值 = 同线程嵌套写深度（极小），用完 `close()`、不泄漏。写路径 fd/PRAGMA 开销不降，但写相对读少、且写串行 + fsync 主导，PRAGMA 开销可接受；fd 大头是读（233 处 `with connect()`），由 §4.2 复用消除。

## 5. 关键不变量（测试守护）

- **INV-1 复用**：同一线程内 N 次 `connect()` 只创建 1 条连接（`_new_connection` 调用 1 次）。
- **INV-2 隔离**：连接绝不跨线程复用；不同线程各自独立连接。
- **INV-3 fd 有界**：一批高并发多操作后，活跃连接数 = `O(线程数)`，不随操作数增长。
- **INV-4 事务边界**：单层 `with connect() as db:` 成功即提交、异常即回滚（同现状）。
- **INV-5 嵌套原子**：嵌套 `with` 只最外层提交；任一层异常 → 最外层整体回滚，无提前提交。
- **INV-6 短命回收**：短命线程 `close_local()` 后其连接被关闭、fd 归还。
- **INV-7 禁止裸 close**：任何代码都**不得对 `connect()` 返回的连接调裸 `.close()`**（会关闭复用连接却不清 `_local.conn` → 下次 `connect()` 返回坏连接）；释放一律走 `close_local()`（同时清 thread-local）。
- **INV-8 写独立提交（增量崩溃恢复）**：`write()` 用独立连接，每个 `write()` 独立提交，即使被外层 `with connect()`（读）包裹或与其它 `write()` 嵌套——外层中途崩溃时已提交的增量 flush 必须保留、可续跑（回归 `test_node_embed_incremental`）。深度守卫**不作用于** `write()`。

## 6. 连接生命周期

| 线程类别 | 来源 | 连接策略 |
|---|---|---|
| 长命池 | KG job 池 / window 池（`app/services/kg/scheduler.py`）、backend 请求线程 | 复用，长期存活，不主动 close（fd 稳定 = 池容量级） |
| 短命 DB 线程 | 后台 `embed-<sid>` daemon（`source_ingestion.py:544`，每源新建 + `join()`；内部写向量 → `connect()`） | **无需显式 close_local**（YAGNI）：并发数 = job 池、有界；线程结束时其 `threading.local` 连接槽随线程销毁被清理、连接自动 `close()`（fd 天然有界 ≈ job+embed_daemon 并发 × 1 连接 ≪ ulimit）。`close_local()` API 仍提供作显式释放口 |
| 不碰 DB 的短命池 | embed 子池 `emb-el/kg/rel/ck`（`source_embedding.py`，worker `_embed_only` 只算 embedding、DB 写在池外主线程）、纯 LLM/HTTP 池 | 无需改 |
| 主线程 | CLI / uvicorn 主 | 复用（1 条），进程退出随之释放 |

**短命池落地**：实现期审计每个短命 `with ThreadPoolExecutor` 的 worker 是否调 `connect()`（碰 DB）；碰 DB 的在 worker 函数体外层加 `try/finally: repo.close_local()`（或等价的 `self.database.close_local()`）。不碰 DB 的池（纯 LLM/embed/HTTP）无需改。plan 阶段逐个列出并落。

**裸 `close()` 审计（INV-7）**：全库 `.close()` 复核后，只有一处真正关闭 SqliteDatabase 复用连接，需改：

- `backend/app/services/knowledge_lifecycle.py:1831` `scan_db.close()`（`scan_db = self._connect()`，mention-alias DF 扫描）→ **改为 `self._close_local()`**（经 `wire_knowledge_lifecycle` 注入 `close_local` 回调）。⚠此处 `close()` **有副作用**：该扫描用 `claim_name_rows` 建的**临时表靠连接关闭而蒸发**（见 1804-1805 注释「连接关闭即整表蒸发」），故**不能简单删除**。`close_local()` 恰好兼得——关闭连接（临时表蒸发 + 释放内存）+ 清 `_local.conn`（下次 `connect()` 重建新连接、不留坏连接），既保原语义又符合 INV-7。这也是 `close_local()` 的真实生产调用者。

**不改的两处**：`backend/app/repositories/sqlite/maintenance.py:798` `conn.close()` 属于 `ReadOnlySQLiteInspector`（独立 `mode=ro` 任意路径只读检查器，eval/validation 工具用，自建 `sqlite3.connect(...?mode=ro)` 且 open/close 配对，**不经 SqliteDatabase、非复用连接**）；`backend/app/services/parsers.py:77` `workbook.close()` 是 openpyxl 工作簿。plan 阶段以 `grep -rn "\.close()"` 复核无其它遗漏的复用连接裸关闭。

## 7. 性能与内存

- **省重复开销**：复用后每线程一次性付"建连接 + 8 PRAGMA + 256MB mmap 映射 + cache 分配 + `-shm` 映射"，之后所有操作复用（含 sqlite statement cache）。这是纯性能收益，尤其抵消 mmap/shm 的反复建销 syscall。
- **内存控制**：复用长命连接会长期持有 page cache，总内存 = `连接数 × cache_size`。为避免膨胀，`cache_size` 提为可配置：新增 `Settings.sqlite_cache_size_kb`，**默认 `-16384`（16MB，负值=KB）**，低于现状的 64MB。批量峰值 ~50 线程 × 16MB ≈ 0.8GB，低于现状"并发峰值各 64MB 反复建销"的抖动。
  - 环境映射用 `validation_alias="SQLITE_CACHE_SIZE_KB"`（pydantic-settings v2，`Field(env=...)` 失效，必须 `validation_alias`）。
  - `.env.example` / `README` / `README_zh` 补该项说明（通用口径）。
- **权衡说明**：cache_size 下调轻微影响大结果集查询的缓存命中；对写密集的批量摄取无感，对 backend 在线读（连接数少）影响有限。16MB 为保守默认，可按部署经 env 上调。

## 8. WAL / checkpoint

- **不 pin WAL**：读路径为 sqlite3 默认 autocommit（`isolation_level=""` 下 SELECT 不隐式 BEGIN），且每个 `with connect() as db:` 块尾经守卫 commit，结束任何隐式事务 → 长命读连接不会 pin 住旧 WAL 页阻碍 checkpoint。
- **checkpoint**：保持 WAL 默认 auto-checkpoint（1000 页）。批量摄取收尾（`run_all` 末尾 rebuild 之后）可选一次 `PRAGMA wal_checkpoint(TRUNCATE)` 截断 WAL——列为可选加固，非 P0。

## 9. 兼容性与迁移

- **无 schema 变更**（纯连接层），无 `_migration_N`、无 `SCHEMA_VERSION` bump。
- 233 处 `with connect() as db:` + 6 处裸 `conn = connect()` 零改动。
- 唯一新增调用面 = §6 短命池 worker 的 `close_local()`。
- 新增 env `SQLITE_CACHE_SIZE_KB`（有默认，向后兼容）。
- 部署无需重启协调（下次进程启动即生效；不影响运行中的 backend）。

## 10. 测试计划（TDD）

新增 `backend/tests/test_sqlite_connection_reuse.py`：

1. **test_reuse_same_thread（INV-1）**：monkeypatch/spy `SqliteDatabase._new_connection` 计数；同线程连续多次 `connect()` + 多次 `with connect()`，断言只建 1 条。
2. **test_isolation_across_threads（INV-2）**：两线程各 `connect()`，断言拿到不同连接对象、各建 1 条、`check_same_thread` 不报错。
3. **test_fd_bounded（INV-3）**：`ThreadPoolExecutor(max_workers=K)` 跑 M≫K 个读写操作，断言 `_new_connection` 总调用数 = 各线程一次（≤ 实际使用线程数），不随 M 增长（可移植地以"连接创建次数"代理 fd 数）。
4. **test_single_with_commit_and_rollback（INV-4）**：单层 `with connect() as db: INSERT` 成功后另连接可见；`with` 内抛异常后该 INSERT 不可见（回滚）。
5. **test_nested_outermost_only_commits（INV-5a）**：外层 `with` 内嵌内层 `with`（都写），内层块结束时**外层写尚不可见**（未提前提交）；外层结束后才可见。
6. **test_nested_inner_failure_rolls_back_all（INV-5b）**：外层写 + 内层 `with` 抛异常冒泡，断言外层写被整体回滚。
7. **test_close_local_releases（INV-6）**：`connect()` 后 `close_local()`，断言底层连接已关闭（对其 `execute` 抛 `ProgrammingError`），再 `connect()` 重建新连接。
8. **test_write_lock_serialization_preserved**：并发多线程 `write()` INSERT，断言无 `database is locked`、数据全部落库（回归 write_lock 语义）。
9. **test_write_uses_independent_connection（INV-8）**：外层 `with connect() as a:` 内嵌 `write()`（拿到连接 b），断言 `a is not b`；且外层 `with` 尚未退出时，内层 `write()` 已提交的行可从**第三条连接**（另一线程）读到（证明 write 独立提交、不被外层读连接的事务深度卷入）。增量崩溃恢复由既有 `test_node_embed_incremental` 回归守护（改独立连接后应恢复绿）。

**受本改动影响、需同步更新的既有测试**（连接层默认值/语义变更的直接 fallout，非弱化）：
- `test_sqlite_write_optimization.py::test_connect_sets_performance_pragmas` 与 `test_sqlite_database_component.py::test_connection_pragmas`：断言 `PRAGMA cache_size == -65536` → 改为 `-16384`（Task 1/2 把默认从 64MB 降到 16MB 并读 `settings.sqlite_cache_size_kb`）。
- `test_node_context_steps.py::test_node_context_legacy_fallback_query_is_bound_by_section_path`：靠 monkeypatch 模块级 `sqlite3.connect` 拦截每次连接来断言 SQL；连接复用后每线程只建一次连接，拦截失效 → 改为对 `repo._connect()` 直接 `set_trace_callback(...)`（等价断言绑定的 SQL，不弱化）。

回归：跑 `backend/` 全量 pytest（现状约 2800+ 用例）确保零破坏，重点 `test_*repository*` / ingest / kg / ask / `test_node_embed_incremental`。

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 嵌套 `with connect()` 提前提交破坏一致性 | `_Conn` 深度守卫（§4.1）+ INV-5 测试；这是本方案的核心正确性保证 |
| 现有代码存在"故意让内层独立提交"的依赖 | 审计写事务嵌套点；语义论证表明复用模型不弱于现状（成功都提交、失败都回滚）。plan 阶段 grep 嵌套 `with .*connect()` 复核 |
| 短命线程连接泄漏（未 close_local） | §6 逐个短命池落 `try/finally`；INV-3 fd 测试兜底 |
| cache_size 下调影响在线查询 | 提为可配置 env，默认 16MB 保守值，可按部署上调 |
| 调用点裸 `close()` 关掉复用连接 → 后续拿坏连接 | 审计定位 1 处（`knowledge_lifecycle.py:1831`，且该 close 兼清临时表），改 `close_local()`（关连接清临时表 + 清 `_local.conn`）；INV-7 立规 + plan `grep` 复核；test_close_local_releases 守 |
| 坏连接（误关/DB 文件消失）复用卡死线程 | **不做自愈**（YAGNI）：极罕见，发生则该操作抛错传播，与现状失败行为一致；`close_local()` 提供手动重置口 |

## 12. 回滚

纯连接层改动、无数据迁移。回滚 = revert 本 PR 单个 commit 组即可，无需数据修复。运行中服务不受影响（下次启动生效）。

## 13. 交付物

- `backend/app/repositories/sqlite/database.py`：`_Conn` + thread-local `connect()` + `close_local()`。
- `backend/app/core/config.py`：`sqlite_cache_size_kb`（`validation_alias`）。
- `close_local()` API：`database.py` + facade delegate（`sqlite_repository.py`）——复用连接的显式释放口（关连接 + 清 `_local.conn`）。生产调用者 = `knowledge_lifecycle.py:1831`（临时表清理）；INV-6/7 测试守护。
- §6 裸 `close()` 必改点：`knowledge_lifecycle.py:1831` `scan_db.close()` → `self._close_local()`（+ 经 `wire_knowledge_lifecycle` 注入 `close_local` 回调）。
- `backend/tests/test_sqlite_connection_reuse.py`。
- `.env.example` / `README.md` / `README_zh.md` 补 `SQLITE_CACHE_SIZE_KB`。
- 最后：rebase → push → PR（base master，Rebase and merge）。
