# SQLite 写入提速与去锁（方案 C）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不降并发前提下消除 `backend/app/services/sqlite_repository.py` 批量抽取时的 `database is locked`，并提升写入吞吐。

**Architecture:** 四层叠加，仅改一个文件：C1 PRAGMA 让 commit 更快；C2 用进程内写锁 + `_write()` 让所有写串行排队（不裸抢 SQLite 写锁，**这是去锁的正确性根因**）；C3 嵌入"并发算向量 + 单事务写"；C4 `store_kg` 大事务切块。C2 修正确性，C1/C3/C4 保吞吐。

**Tech Stack:** Python 标准库 `sqlite3` / `threading` / `contextlib`、pytest。

**约定（所有任务通用）：**
- 工作目录 worktree 根；测试从 `backend/` 跑：`cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest ... -q`。
- 提交前确认分支：`git rev-parse --abbrev-ref HEAD` == `claude/pedantic-taussig-0c79f1`。

---

## File Structure

**修改**：`backend/app/services/sqlite_repository.py`（顶部 import、`__init__`、`_connect`、新增 `_write`、所有写事务块、`_embed_objects_batch`、`_embed_source`、`store_kg`）。
**新建**：`backend/tests/test_sqlite_write_optimization.py`（PRAGMA、并发去锁、写审计、嵌入单写、store_kg 切块）。

---

## Task 1: C1 — `_connect` 增加提速 PRAGMA

**Files:** Modify `backend/app/services/sqlite_repository.py:150-156`; Test `backend/tests/test_sqlite_write_optimization.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_sqlite_write_optimization.py`:

```python
import threading
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_connect_sets_performance_pragmas(repo):
    with repo._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1        # NORMAL
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2         # MEMORY
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 268435456  # 256MB
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -65536    # 64MB
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_connect_sets_performance_pragmas -q`
Expected: FAIL（synchronous 默认 2=FULL，其余未设）。

- [ ] **Step 3: 改 `_connect`**

把 `_connect`（行 150-156）的 `return connection` 之前追加 4 行，使其为：

```python
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.settings.db_busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.settings.db_busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA cache_size = -65536")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA mmap_size = 268435456")
        return connection
```

- [ ] **Step 4: 跑确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_connect_sets_performance_pragmas -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_sqlite_write_optimization.py
git commit -m "perf(db): C1 _connect 增加 synchronous=NORMAL/cache/temp_store/mmap PRAGMA"
```

---

## Task 2: C2 — 写锁 + `_write()` + 全量写转换 + 去锁/审计测试

**Files:** Modify `backend/app/services/sqlite_repository.py`（顶部 import、`__init__:127-142`、`_connect` 后新增 `_write`、所有写事务块）；Test `backend/tests/test_sqlite_write_optimization.py`

- [ ] **Step 1: 写并发去锁 + 写审计两个失败测试**

追加到 `backend/tests/test_sqlite_write_optimization.py`:

```python
def test_concurrent_store_kg_no_database_locked(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    errors = []

    def worker(k):
        try:
            objs = [{"local_id": f"{k}-{i}", "object_type": "concept",
                     "payload": {"name": f"c{k}-{i}"}, "evidence": []} for i in range(60)]
            repo.store_kg(nb.id, None, objs, [])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    assert n == 8 * 60


def test_all_writes_go_through_write_lock():
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "sqlite_repository.py"
    lines = src.read_text(encoding="utf-8").splitlines()
    WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
    ALLOW = {"_migrate", "_seed"}            # 起步单线程, 不并发, 豁免
    cur = None
    in_block = False
    block_indent = 0
    offenders = []
    for i, ln in enumerate(lines, 1):
        m = re.match(r"\s*def (\w+)\(", ln)
        if m:
            cur = m.group(1)
        if "with self._connect() as db:" in ln:
            in_block = True
            block_indent = len(ln) - len(ln.lstrip())
            continue
        if in_block:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= block_indent:
                in_block = False
            elif WRITE.search(ln) and cur not in ALLOW:
                offenders.append((i, cur, ln.strip()[:70]))
    assert not offenders, f"这些写仍走 _connect()(应改 _write()): {offenders}"
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_all_writes_go_through_write_lock -q`
Expected: FAIL（当前所有写都走 `_connect()`，offenders 一大串）。

- [ ] **Step 3: 顶部补 import**

在 `backend/app/services/sqlite_repository.py` 顶部 import 区加入（与现有 import 同段，按风格放好）：

```python
import threading
from contextlib import contextmanager
```

- [ ] **Step 4: `__init__` 加写锁**

在 `__init__`（行 127-142）的 `self._vector_cache = VectorCache()` 之后、`self._migrate()` 之前插入一行：

```python
        self._write_lock = threading.RLock()
```

- [ ] **Step 5: 新增 `_write()` 上下文管理器**

紧接 `_connect`（即新加的 `return connection` 之后、`_migrate` 之前）插入：

```python
    @contextmanager
    def _write(self):
        """串行化写事务：进程内同一时刻只有一个写者进 SQLite，并发写线程在
        Python 层排队而非裸抢 SQLite 写锁（后者即 `database is locked` 的根因）。
        纯读保持用 _connect()，不受影响（WAL 支持并发读）。"""
        with self._write_lock:
            with self._connect() as db:
                yield db
```

- [ ] **Step 6: 把所有写事务块改用 `_write()`（审计驱动的机械转换）**

规则：凡 `with self._connect() as db:` 的块内执行 `INSERT/UPDATE/DELETE/REPLACE`，把该行的 `_connect()` 改为 `_write()`；**纯读块保持 `_connect()`**；`_migrate`/`_seed` 不改。

执行方式（迭代到审计通过）：
1. 运行审计测试列出 offenders：
   `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_all_writes_go_through_write_lock -q`
2. 对它报出的每个 `(行号, 方法名)`，定位其所属的 `with self._connect() as db:` 行，改成 `with self._write() as db:`。
3. 重跑审计，直到 offenders 为空。

辅助定位全部候选块：`grep -n "with self._connect() as db:" backend/app/services/sqlite_repository.py`（共 85 处，其中写块需转换；读块如 `relations_for_notebook`、各 `get_*`/`list_*`/检索保持不变）。

> 注意：`store_kg`、`_embed_objects_batch`、`_embed_source` 三个写块在本步也一并改为 `_write()`；它们会在 Task 3/4 进一步重构，但仍保持用 `_write()`。

- [ ] **Step 7: 跑确认两测试通过 + 全量回归**

Run:
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py -q
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
```
Expected: write_optimization 全 PASS（含 audit 与 concurrent）；全量套件 220+ passed 不回归。若审计仍报 offender，回 Step 6 继续转换。

- [ ] **Step 8: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_sqlite_write_optimization.py
git commit -m "fix(db): C2 写锁+_write() 串行化所有写, 消除 database is locked(审计+并发测试兜底)"
```

---

## Task 3: C3 — 嵌入"并发算向量 + 单事务写"

**Files:** Modify `backend/app/services/sqlite_repository.py`（`_embed_objects_batch:1330-1375`、`_embed_source:1247-1302`）；Test `backend/tests/test_sqlite_write_optimization.py`

- [ ] **Step 1: 写失败测试（单写事务）**

追加到 `backend/tests/test_sqlite_write_optimization.py`:

```python
@pytest.fixture
def embed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")     # embedder_configured == True
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    r = SQLiteRepository(Settings())

    class _FakeEmbedder:
        def embed_texts(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    r.embedder = _FakeEmbedder()
    return r


def test_embed_objects_batch_writes_in_one_transaction(embed_repo, monkeypatch):
    nb = embed_repo.create_notebook(NotebookCreate(name="nb"))
    writes = {"n": 0}
    import contextlib
    real_write = embed_repo._write

    @contextlib.contextmanager
    def counting_write():
        writes["n"] += 1
        with real_write() as db:
            yield db

    monkeypatch.setattr(embed_repo, "_write", counting_write)
    items = [{"_oid": f"ko-{i}", "payload": {"name": f"concept number {i}"}} for i in range(35)]
    embed_repo._embed_objects_batch(nb.id, items)

    assert writes["n"] == 1                       # 35项/批10 = 4批, 但只 1 次写事务
    with embed_repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    assert n == 35
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_embed_objects_batch_writes_in_one_transaction -q`
Expected: FAIL（当前每批各自 `_write()`，writes["n"]==4 而非 1）。

- [ ] **Step 3: 重写 `_embed_objects_batch`**

把 `_embed_objects_batch`（行 1330-1375）整体替换为：

```python
    def _embed_objects_batch(self, notebook_id: str, items: List[dict]) -> None:
        """并发 COMPUTE payload 向量, 再用一次写事务持久化到 knowledge_embeddings。
        每批计算失败照旧 log + 跳过(best-effort)。"""
        if not self.settings.embedder_configured:
            return
        pending = []
        for it in items:
            text = _payload_text(it["payload"]).strip()
            if text:
                pending.append((it["_oid"], text[:2000]))
        if not pending:
            return
        import concurrent.futures as _cf

        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        ensure = getattr(self.embedder, "_ensure", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # noqa: BLE001 — warm-up only
                pass

        def _embed_only(batch) -> list:
            texts = [t for _, t in batch]
            try:
                vectors = self.embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed kg-objects batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc,
                )
                return []
            return [(oid, vec) for (oid, _), vec in zip(batch, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-kg") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        if not rows:
            return
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(oid, notebook_id, json.dumps(vec), now) for oid, vec in rows],
            )
```

- [ ] **Step 4: 同样重写 `_embed_source` 的写入部分**

把 `_embed_source`（行 1271-1301，即内嵌 `_embed_and_store` 定义到方法结尾）替换为：

```python
        def _embed_only(els: list) -> list:
            texts = [el.text[:trunc] for el in els]
            try:
                vectors = self.embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed batch failed (%d elements) for source %s: %s",
                    len(els), source_id, exc,
                )
                return []
            return [(el.id, vector) for el, vector in zip(els, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-el") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        now = _now()
        if rows:
            with self._write() as db:
                db.executemany(
                    "INSERT OR REPLACE INTO element_embeddings "
                    "(element_id, source_id, notebook_id, vector, created_at) VALUES (?,?,?,?,?)",
                    [(eid, source_id, notebook_id, json.dumps(vec), now) for eid, vec in rows],
                )
        self.event_log.logger.info(
            "embedded %s/%s elements for source %s", len(rows), len(pending), source_id
        )
```

- [ ] **Step 5: 跑确认通过 + 回归**

Run:
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py tests/test_embed_concurrency.py tests/test_embed_resilience.py tests/test_ask_vector_matrix.py -q
```
Expected: PASS（含已有嵌入并发/韧性测试不回归）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_sqlite_write_optimization.py
git commit -m "perf(db): C3 嵌入改并发算向量+单事务写(knowledge/element 都改)"
```

---

## Task 4: C4 — `store_kg` 大事务切块（executemany）

**Files:** Modify `backend/app/services/sqlite_repository.py:1784-1844`；Test `backend/tests/test_sqlite_write_optimization.py`

- [ ] **Step 1: 写失败测试（跨块）**

追加到 `backend/tests/test_sqlite_write_optimization.py`:

```python
def test_store_kg_chunks_large_insert(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"L{i}", "object_type": "concept",
             "payload": {"name": f"c{i}"}, "evidence": []} for i in range(2500)]
    rels = [{"source_local_id": "L0", "target_local_id": f"L{i}",
             "edge_type": "about", "evidence": []} for i in range(1, 2500)]
    n_obj, n_rel = repo.store_kg(nb.id, None, objs, rels)
    assert n_obj == 2500 and n_rel == 2499
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 2500
        assert db.execute("SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 2499
        # 跨块关系 remap 正确: 任取一条, 端点都应是真实 ko id
        r = db.execute("SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=? LIMIT 1", (nb.id,)).fetchone()
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE id=?", (r["source_object_id"],)).fetchone()["c"] == 1
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE id=?", (r["target_object_id"],)).fetchone()["c"] == 1
```

- [ ] **Step 2: 跑确认通过（当前单事务也满足计数）**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_store_kg_chunks_large_insert -q`
Expected: PASS（此测试是落库正确性的回归护栏；切块改造后必须仍 PASS）。

> 说明：C4 是性能重构，行为不变（结果集相同），故测试在改造前后都应 PASS。该测试锁定"切块后不丢数据、跨块关系 remap 正确"。

- [ ] **Step 3: 重写 `store_kg` 为分块 executemany**

把 `store_kg`（行 1784-1844）整体替换为：

```python
    def store_kg(self, notebook_id: str, source_id: Optional[str],
                 objects: List[dict], relations: List[dict]) -> Tuple[int, int]:
        """Insert KG nodes/edges (remapping local ids to DB ids), embeds payload.

        分块写入(每块 CHUNK 行, 各自一个 _write() 事务), 避免单源 2.6万行塞一个
        事务长时间持锁。本地 id->DB id 在分块前一次性预分配, 跨块关系仍能正确
        remap。代价: 失整源原子性(崩溃可能留半本); _run_extraction 逐源自清 +
        可重跑兜底。Relations 引用不到的 local id 静默跳过。"""
        CHUNK = 1000
        now = _now()
        local_to_id: Dict[str, str] = {}
        for obj in objects:
            local_to_id[obj["local_id"]] = f"ko-{uuid4().hex[:10]}"
            obj["_oid"] = local_to_id[obj["local_id"]]   # _embed_objects_batch 依赖
        db_relations = []
        for rel in relations:
            s = local_to_id.get(rel["source_local_id"])
            t = local_to_id.get(rel["target_local_id"])
            if not s or not t:
                continue
            db_relations.append({
                "source_object_id": s, "target_object_id": t,
                "edge_type": rel["edge_type"], "evidence": rel.get("evidence", []),
            })

        for i in range(0, len(objects), CHUNK):
            chunk = objects[i:i + CHUNK]
            with self._write() as db:
                db.executemany(
                    "INSERT INTO knowledge_objects "
                    "(id, notebook_id, object_type, status, owner, payload, evidence, "
                    "source_candidate_id, source_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'approved', '', ?, ?, NULL, ?, ?, ?)",
                    [(o["_oid"], notebook_id, o["object_type"],
                      json.dumps(o["payload"], ensure_ascii=False),
                      json.dumps(o["evidence"], ensure_ascii=False),
                      source_id or '', now, now) for o in chunk],
                )
        for i in range(0, len(db_relations), CHUNK):
            chunk = db_relations[i:i + CHUNK]
            with self._write() as db:
                db.executemany(
                    "INSERT INTO knowledge_relations "
                    "(id, notebook_id, source_id, source_object_id, target_object_id, "
                    "edge_type, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(f"rel-{uuid4().hex[:10]}", notebook_id, source_id,
                      r["source_object_id"], r["target_object_id"], r["edge_type"],
                      json.dumps(r["evidence"], ensure_ascii=False), now) for r in chunk],
                )
        self._embed_objects_batch(notebook_id, objects)
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)
        return len(objects), len(db_relations)
```

- [ ] **Step 4: 跑确认通过 + 回归**

Run:
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py tests/test_kg_repository.py tests/test_unified_kg_repository.py tests/test_kg_ingest.py -q
```
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_sqlite_write_optimization.py
git commit -m "perf(db): C4 store_kg 大事务切块(CHUNK=1000 executemany), 缩短持锁"
```

---

## Task 5: 全量验证

- [ ] **Step 1: 全量 backend 测试 + check.sh**

Run:
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
cd /Users/hzf/workspace/silicon_notebook && PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```
Expected: pytest 全 PASS（不低于改造前 220 passed/1 skipped 基线 + 本计划新增测试）；check.sh PASS。失败则修复后重跑，不跳过。

---

## Task 6: 系统性写吞吐基准（离线，无 LLM）

**Files:** Create `scripts/bench_sqlite_writes.py`；Test `backend/tests/test_sqlite_write_optimization.py`

> 目的：在单写者(`_write` 串行)下，N 个并发 writer 各写 M 条 `knowledge_objects`，测吞吐(rec/s) + 确认无 `database is locked`。**纯离线**：不配 `EMBED_*` → `embedder_configured` False → `store_kg` 不触发嵌入，纯写。两种模式：`thread`(贴合 app 单进程多线程，`_write_lock` 串行) / `process`(多进程，SQLite WAL 自身单写者，测 SQLite 裸吞吐)。

- [ ] **Step 1: 写基准 pytest 护栏（小规模, 进 CI）**

追加到 `backend/tests/test_sqlite_write_optimization.py`:

```python
def test_write_throughput_smoke_no_lock(repo, capsys):
    import time
    nb = repo.create_notebook(NotebookCreate(name="bench"))
    WORKERS, RECORDS = 64, 100
    errors = []

    def work(w):
        try:
            objs = [{"local_id": f"{w}-{i}", "object_type": "concept",
                     "payload": {"name": f"c{w}-{i}"}, "evidence": []} for i in range(RECORDS)]
            repo.store_kg(nb.id, None, objs, [])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=work, args=(w,)) for w in range(WORKERS)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    elapsed = time.perf_counter() - t0
    total = WORKERS * RECORDS
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    print(f"\n[bench] {WORKERS}w x {RECORDS} = {total} rows in {elapsed:.2f}s = {total / elapsed:,.0f} rec/s")
    assert not errors, errors
    assert n == total
```

- [ ] **Step 2: 跑确认通过（无锁 + 计数正确）**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_write_optimization.py::test_write_throughput_smoke_no_lock -q -s`
Expected: PASS，并打印一行 `[bench] 64w x 100 = 6400 rows in ...s = ... rec/s`。

- [ ] **Step 3: 写完整基准脚本**

Create `scripts/bench_sqlite_writes.py`:

```python
"""离线 SQLite 写吞吐基准（无 LLM/无嵌入）。
单写者下 N 个并发 writer 各写 RECORDS 条 knowledge_objects, 测吞吐 + 确认无锁。
用法（repo 根）:
  PYTHONPATH=backend python scripts/bench_sqlite_writes.py --workers 1000 --records 100 --mode thread
  PYTHONPATH=backend python scripts/bench_sqlite_writes.py --workers 1000 --records 100 --mode process
"""
import argparse
import os
import tempfile
import time


def _make_repo(db_path):
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("SILICON_NOTEBOOK_STORAGE_DIR", db_path + "_storage")
    os.environ["EVENT_LOG_ENABLED"] = "false"
    os.environ["LLM_LOG_ENABLED"] = "false"
    # 不配 EMBED_* → embedder_configured False → store_kg 不嵌入(纯写基准)
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    return SQLiteRepository(Settings())


def _objs(worker, n):
    return [{"local_id": f"{worker}-{i}", "object_type": "concept",
             "payload": {"name": f"concept {worker}-{i}"}, "evidence": []} for i in range(n)]


def _run_threads(repo, nb_id, workers, records):
    import threading
    errors = []

    def work(w):
        try:
            repo.store_kg(nb_id, None, _objs(w, records), [])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    return time.perf_counter() - t0, errors


def _proc_work(args):
    db_path, nb_id, w, records = args
    repo = _make_repo(db_path)
    try:
        repo.store_kg(nb_id, None, _objs(w, records), [])
        return None
    except Exception as exc:  # noqa: BLE001
        return repr(exc)


def _run_processes(db_path, nb_id, workers, records):
    import multiprocessing as mp
    cap = min(workers, (os.cpu_count() or 4) * 8)
    t0 = time.perf_counter()
    with mp.Pool(cap) as pool:
        results = pool.map(_proc_work, [(db_path, nb_id, w, records) for w in range(workers)])
    return time.perf_counter() - t0, [r for r in results if r]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1000)
    ap.add_argument("--records", type=int, default=100)
    ap.add_argument("--mode", choices=["thread", "process"], default="thread")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="bench_sqlite_")
    db_path = os.path.join(tmp, "bench.db")
    repo = _make_repo(db_path)
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="bench"))
    total = a.workers * a.records

    if a.mode == "thread":
        elapsed, errors = _run_threads(repo, nb.id, a.workers, a.records)
    else:
        elapsed, errors = _run_processes(db_path, nb.id, a.workers, a.records)

    repo2 = _make_repo(db_path)
    with repo2._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    locked = len([e for e in errors if "lock" in e.lower()])
    print(f"mode={a.mode} workers={a.workers} records/worker={a.records} total={total}")
    print(f"elapsed={elapsed:.2f}s  throughput={total / elapsed:,.0f} rec/s")
    print(f"locked_errors={locked}  other_errors={len(errors) - locked}")
    print(f"stored={n}/{total}  {'OK' if n == total and not errors else 'MISMATCH/ERRORS'}")
    if errors[:3]:
        print("sample errors:", errors[:3])


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: py_compile + 跑 1000×100 基准（thread 与 process 各一次, 记录数字）**

Run:
```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile scripts/bench_sqlite_writes.py
cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/bench_sqlite_writes.py --workers 1000 --records 100 --mode thread
cd /Users/hzf/workspace/silicon_notebook && PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/bench_sqlite_writes.py --workers 1000 --records 100 --mode process
```
Expected：两次都 `locked_errors=0`、`stored=100000/100000 OK`，并打印 `throughput=... rec/s`。把两组吞吐数字记录到本任务下作为结论。

- [ ] **Step 5: 提交**

```bash
git add scripts/bench_sqlite_writes.py backend/tests/test_sqlite_write_optimization.py
git commit -m "test(db): 离线 SQLite 写吞吐基准(1000writer×100条, thread/process), 验证单写无锁"
```

---

## 自检（Self-Review）

- **Spec 覆盖**：C1=Task1；C2(写锁+_write+全量转换+审计)=Task2；C3(嵌入算写分离, knowledge+element)=Task3；C4(store_kg 切块)=Task4；并发去锁测试=Task2 Step1；写完整性审计=Task2 Step1；回归=Task5；离线写吞吐基准(thread/process, 1000w×100)=Task6。全覆盖。
- **占位扫描**：无 TBD/TODO；C2 Step6 的"机械转换"由审计测试精确枚举待改项并迭代到空，非占位。
- **类型/命名一致**：`_write()`、`self._write_lock`、`_embed_only`(替代旧 `_embed_and_store`)、`CHUNK`、PRAGMA 值（synchronous=1/temp_store=2/mmap=268435456/cache=-65536）在各任务间一致。
- **顺序/依赖**：C1 独立；C2 引入 `_write()`（C3/C4 依赖）；C3/C4 重构的方法在 C2 Step6 已先转为 `_write()`，再在 C3/C4 内保持 `_write()` 重构——无回退冲突。
