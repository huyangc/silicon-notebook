# 大库检索按磁盘索引身份缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `_scale_index(allow_stale=True)`(检索热路径的「取磁盘已索引部分」)按磁盘索引身份缓存、脱离 `kg_mutation_seq` churn,使大库严格推理在摄取进行时也恒定 O(1) 成本,不再每查询重载 ~10GB ANN。

**Architecture:** 统一进程缓存 `_scale_idx_cache`,查找规则按 `allow_stale` 分派:exact 调用方按 DB 版本 `cur` 校验(不变),stale 调用方按磁盘 `manifest.json` 版本校验并复用实例(ANN handle 随实例 memoize 存活);cold 加载走 per-nb 单飞锁防 N×8GB 尖峰。组合图缓存键在 delta 门控关时丢弃 churn 项。`_active_kg_delta` 门控前早退省掉无谓 COUNT。

**Tech Stack:** Python 3.13 / FastAPI / SQLite / hnswlib / scipy / pytest。

## Global Constraints

- 后端测试从 `backend/` 跑:`python -m pytest tests/<file> -q`;本机用系统 `python`(共享 conda),不建 venv。
- **绝不改** `_scale_index_version` / `_probe_scale_version_signal` 的 settings_tail(会让存量 manifest 全失配变 stale,PR#178 已守此边界)。
- version-exact 调用方(`_scale_index(nb)` 无 allow_stale:viz/status,行号 sqlite_repository.py 内 2320/7987/8021 一带)行为**字节不变**——drift 时仍返 None。
- 锁次序(镜像既有 `_scale_ver_lock`/`_scale_ver_locks`):全局锁只护锁表结构(get-or-create per-nb 锁),**绝不在全局锁内跑 load_scale_index**;任何时候不持 per-nb 锁去申请全局锁。
- stale-serve 与 `scale_search_include_delta` 无关地正确:ANN 核=磁盘已索引部分;flag=ON 时 delta 新鲜度来自检索侧既有 `⊕delta` 暴力块(不在本改动范围),不来自缓存核。
- TDD:每任务先写失败测试→跑失败→实现→跑通过→commit。
- Commit 中文 conventional,尾行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 收尾全量 `python -m pytest tests/ -q` 必须全绿(基线约 1750+ passed / 1 skipped)。
- 分支 `feat/scale-index-disk-identity`,worktree `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/scale-idx-cache`。⚠️ 只在此 worktree 跑 git,**绝不在** `/Users/hzf/workspace/silicon_notebook`(root checkout)跑 git(曾污染 root master)。提交前核 `pwd` 与 `git rev-parse --abbrev-ref HEAD`。

## 文件结构

全部改 `backend/app/services/sqlite_repository.py`,测试新建 `backend/tests/test_scale_idx_disk_cache.py`。三处编辑相互独立(可分任务独立评审):`_scale_index`(核心)、`_scale_combined_graph`(缓存键)、`_active_kg_delta`(早退)。新增两个小基础设施:`__init__` 里的 `_scale_idx_load_lock`/`_scale_idx_load_locks`(镜像既有 ver-lock),一个 `_read_manifest_version` 纯 helper。

---

### Task 1: `_read_manifest_version` 纯 helper + 磁盘版本探测测试

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(在 `_scale_index` 定义前新增一个方法,约行 7994 前)
- Test: `backend/tests/test_scale_idx_disk_cache.py`(新建)

**Interfaces:**
- Produces: `SQLiteRepository._read_manifest_version(out_dir: str) -> "list | None"` —— 读 `out_dir/manifest.json` 只取 `version` 字段;文件缺失/损坏/无 version → None。返回值类型与 `manifest.get("version")`(一个 list)一致,便于与 `idx.manifest.get("version")` 直接 `==` 比较。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_scale_idx_disk_cache.py`:

```python
"""大库检索按磁盘索引身份缓存:stale 实例按磁盘 manifest 版本复用,脱离 kg_mutation_seq
churn,使摄取期严格推理恒定 O(1)。"""
import json
import os

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_read_manifest_version(repo, tmp_path):
    out_dir = tmp_path / "idxdir"
    out_dir.mkdir()
    # 无 manifest → None
    assert repo._read_manifest_version(str(out_dir)) is None
    # 有 manifest → 返回 version list
    (out_dir / "manifest.json").write_text(json.dumps({"version": ["a", 3, "t"]}))
    assert repo._read_manifest_version(str(out_dir)) == ["a", 3, "t"]
    # 损坏 JSON → None(不抛)
    (out_dir / "manifest.json").write_text("{not json")
    assert repo._read_manifest_version(str(out_dir)) is None
    # 无 version 字段 → None
    (out_dir / "manifest.json").write_text(json.dumps({"n_nodes": 5}))
    assert repo._read_manifest_version(str(out_dir)) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py::test_read_manifest_version -q`
Expected: FAIL(`AttributeError: ... has no attribute '_read_manifest_version'`)

- [ ] **Step 3: 实现**

在 `_scale_index` 方法定义(`def _scale_index(self, notebook_id...`)紧邻之前插入:

```python
    def _read_manifest_version(self, out_dir: str):
        """廉价读 out_dir/manifest.json 的 version 字段(几 KB,sub-ms)。用于
        allow_stale 检索路径校验「进程缓存里的 stale 实例是否仍是当前磁盘索引」——
        磁盘索引只在 rebuild/fold 时换(新 version),与 kg_mutation_seq 无关。
        文件缺失/损坏/无 version → None(fail-soft,调用方回退到重新 load)。"""
        mpath = os.path.join(out_dir, "manifest.json")
        try:
            with open(mpath) as fh:
                return json.load(fh).get("version")
        except (OSError, ValueError):
            return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py::test_read_manifest_version -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_idx_disk_cache.py
git commit -m "feat(scale): _read_manifest_version 廉价磁盘索引身份探测

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: per-nb 单飞锁基础设施

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`__init__` 中 `_scale_ver_lock`/`_scale_ver_locks` 定义处附近,约行 329-330)
- Test: `backend/tests/test_scale_idx_disk_cache.py`(追加)

**Interfaces:**
- Produces: `self._scale_idx_load_lock: threading.Lock`(护锁表结构)与 `self._scale_idx_load_locks: Dict[str, threading.Lock]`(per-nb 加载锁),供 Task 3 的 `_scale_index` cold-load 单飞使用。

- [ ] **Step 1: 写失败测试(追加)**

```python
import threading


def test_load_lock_table_present(repo):
    assert isinstance(repo._scale_idx_load_lock, threading.Lock().__class__)
    assert repo._scale_idx_load_locks == {}
```

(注:`threading.Lock` 是工厂函数,类型用 `threading.Lock().__class__` 取实际锁类型。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py::test_load_lock_table_present -q`
Expected: FAIL(`AttributeError: ... '_scale_idx_load_lock'`)

- [ ] **Step 3: 实现**

在 `__init__` 中 `self._scale_ver_locks: Dict[str, threading.Lock] = {}`(约行 330)之后插入:

```python
        # per-nb 单飞:allow_stale 检索路径 cold-load ScaleIndex 时,防 N 个并发查询
        # 各自 load_scale_index + hnswlib.load_index(8GB)造成 N× 内存尖峰。锁次序同
        # _scale_ver_lock:全局锁只护锁表结构,绝不在全局锁内跑 load。
        self._scale_idx_load_lock = threading.Lock()
        self._scale_idx_load_locks: Dict[str, threading.Lock] = {}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py::test_load_lock_table_present -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_idx_disk_cache.py
git commit -m "feat(scale): per-nb scale-index 加载单飞锁表(防并发 8GB 重载尖峰)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `_scale_index` stale 分支按磁盘身份缓存 + 单飞(核心)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_scale_index`,当前 7994-8011)
- Test: `backend/tests/test_scale_idx_disk_cache.py`(追加)

**Interfaces:**
- Consumes: Task 1 `_read_manifest_version`、Task 2 `_scale_idx_load_lock`/`_scale_idx_load_locks`。
- Produces: `_scale_index(nb, allow_stale=True)` 在磁盘索引不变时跨查询返回**同一缓存实例**(handle 存活);`_scale_index(nb)`(exact)语义不变。

**当前代码(7994-8011,替换目标):**

```python
    def _scale_index(self, notebook_id: str, allow_stale: bool = False):
        """..."""
        from app.services.kg import scale_index as si
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        cur = self._scale_index_version(notebook_id)
        cached = self._scale_idx_cache.get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        idx = si.load_scale_index(out_dir)
        if idx is None:
            return None
        if idx.manifest.get("version") == cur:
            self._scale_idx_cache[notebook_id] = idx
            return idx
        return idx if allow_stale else None
```

- [ ] **Step 1: 写失败测试(追加)——恒定成本 + exact 不变 + 自愈 + 单飞**

需要一个已建索引再制造 delta 的构造。复用 `tests/test_scale_delta_policy.py` 的手法(source A 进水位 build,source B 之后加)。把下面 helper 与四个测试追加进 `test_scale_idx_disk_cache.py`:

```python
def _insert_source_chunk(repo, nb_id, sid, cid, text, day):
    now = f"2026-07-{day:02d}T00:00:00"
    with repo._write() as db:
        db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, text, "", "[]", now))
        v = repo.embedder.embed_query(text)
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (cid, nb_id, json.dumps(v), now))


def _indexed_nb_with_delta(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _insert_source_chunk(repo, nb.id, "sA", "cA", "alpha", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)                 # watermark={sA}; manifest.version=V0
    _insert_source_chunk(repo, nb.id, "sB", "cB", "bravo", 2)  # delta → cur != V0
    return nb


def test_stale_index_reused_across_queries(repo, monkeypatch):
    """摄取造成 cur != manifest.version 时,多次 allow_stale 调用只 load 一次磁盘。"""
    nb = _indexed_nb_with_delta(repo)
    import app.services.kg.scale_index as si
    calls = {"n": 0}
    real = si.load_scale_index
    monkeypatch.setattr(si, "load_scale_index", lambda d: (calls.__setitem__("n", calls["n"] + 1), real(d))[1])
    a = repo._scale_index(nb.id, allow_stale=True)
    b = repo._scale_index(nb.id, allow_stale=True)
    c = repo._scale_index(nb.id, allow_stale=True)
    assert a is not None and a is b is c          # 同一实例复用
    assert calls["n"] == 1                         # 只从磁盘 load 一次


def test_exact_caller_unchanged_on_delta(repo):
    """version-exact 调用方(无 allow_stale)在有 delta 时仍返 None,行为不变。"""
    nb = _indexed_nb_with_delta(repo)
    assert repo._scale_index(nb.id) is None


def test_stale_reload_after_disk_rebuild(repo):
    """磁盘 manifest 版本变(rebuild/fold)后,下次 stale 调用返回新实例(自愈)。"""
    nb = _indexed_nb_with_delta(repo)
    a = repo._scale_index(nb.id, allow_stale=True)
    repo.build_scale_index(nb.id)                  # 重建 → 新 manifest.version,收进 sB
    b = repo._scale_index(nb.id, allow_stale=True)
    assert b is not None and b is not a            # 新磁盘身份 → 新实例


def test_no_manifest_returns_none(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo._scale_index(nb.id, allow_stale=True) is None


def test_concurrent_cold_stale_single_flight(repo, monkeypatch):
    """并发 cold stale 调用只 load 一次(单飞)。"""
    import app.services.kg.scale_index as si
    nb = _indexed_nb_with_delta(repo)
    repo._scale_idx_cache.pop(nb.id, None)         # 清缓存造 cold
    calls = {"n": 0}
    real = si.load_scale_index
    import time

    def slow_load(d):
        calls["n"] += 1
        time.sleep(0.05)
        return real(d)
    monkeypatch.setattr(si, "load_scale_index", slow_load)
    import threading
    results = []
    threads = [threading.Thread(target=lambda: results.append(repo._scale_index(nb.id, allow_stale=True))) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert calls["n"] == 1                          # 单飞:只加载一次
    assert all(r is results[0] for r in results)    # 都拿到同一实例
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py -q -k "stale or exact_caller or concurrent"`
Expected: FAIL(`test_stale_index_reused_across_queries` 报 `calls['n']` > 1——当前每查询重 load;`test_concurrent...` 同理)

- [ ] **Step 3: 实现**

把 `_scale_index`(7994-8011)整体替换为:

```python
    def _scale_index(self, notebook_id: str, allow_stale: bool = False):
        """Return a valid ScaleIndex or None.

        exact(allow_stale=False):manifest.version == 当前 DB 版本 cur 才算有效,
        否则 None——viz/status 等要求与 DB 强一致的调用方语义不变。

        allow_stale=True(检索热路径「取磁盘已索引部分」):按**磁盘索引身份**
        (manifest.json 的 version)缓存复用。磁盘索引只在 rebuild/fold 时换,与
        kg_mutation_seq(每写 bump)无关——所以摄取造成 cur 漂移时,不再每查询重建
        stale 实例 + 重载 ~10GB ANN handle,而是复用同一进程缓存实例(handle memoize
        存活)。cold-load 走 per-nb 单飞锁,防 N 个并发查询各载 8GB 造成内存尖峰。
        stale-serve 与 scale_search_include_delta 无关地正确:ANN 核=磁盘已索引部分,
        flag=ON 时 delta 新鲜度来自检索侧 ⊕delta 暴力块,不来自这个核。"""
        from app.services.kg import scale_index as si
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        cur = self._scale_index_version(notebook_id)
        cached = self._scale_idx_cache.get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        if not allow_stale:
            # version-exact:字节不变——load,manifest==cur 才 cache 并返回,否则 None。
            idx = si.load_scale_index(out_dir)
            if idx is None:
                return None
            if idx.manifest.get("version") == cur:
                self._scale_idx_cache[notebook_id] = idx
                return idx
            return None
        # allow_stale:按磁盘身份复用。cached 若仍是当前磁盘索引(其 version == 磁盘
        # manifest version)→ 直接返回(handle 存活,零重载)。
        disk_ver = self._read_manifest_version(out_dir)
        if disk_ver is None:
            return None   # 无索引
        if cached is not None and cached.manifest.get("version") == disk_ver:
            return cached
        # cold:单飞加载。全局锁只护锁表,load 在 per-nb 锁内、不持全局锁。
        with self._scale_idx_load_lock:
            nb_lock = self._scale_idx_load_locks.get(notebook_id)
            if nb_lock is None:
                nb_lock = threading.Lock()
                self._scale_idx_load_locks[notebook_id] = nb_lock
        with nb_lock:
            # double-check:等锁期间别的线程可能已加载好当前磁盘索引。
            cached = self._scale_idx_cache.get(notebook_id)
            disk_ver = self._read_manifest_version(out_dir)
            if disk_ver is None:
                return None
            if cached is not None and cached.manifest.get("version") == disk_ver:
                return cached
            idx = si.load_scale_index(out_dir)
            if idx is None:
                return None
            self._scale_idx_cache[notebook_id] = idx
            return idx
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py -q`
Expected: 全 PASS

- [ ] **Step 5: 回归——既有 scale/delta 测试族**

Run: `cd backend && python -m pytest tests/test_scale_index.py tests/test_scale_index_repo.py tests/test_scale_delta_policy.py tests/test_auto_scale_index.py tests/test_scale_idx_cache_lru.py tests/test_ppr_retrieve.py -q`
Expected: 全 PASS(stale 复用是纯正确性增强,不改结果)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_idx_disk_cache.py
git commit -m "fix(scale): allow_stale 检索索引按磁盘身份缓存+单飞,脱离摄取 churn

摄取期 kg_mutation_seq 每写 bump 致 manifest.version!=cur 恒成立,旧 stale 分支
每查询新建实例+重载 ~8GB kg ANN + ~2GB chunk ANN。改为按磁盘 manifest version
复用进程缓存实例(handle memoize 存活),cold-load 单飞防 N×8GB 尖峰。exact 调用方
(viz/status)语义字节不变。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 组合图缓存键在 delta 门控关时丢弃 active churn

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_scale_combined_graph` version 元组,当前 9468-9476)
- Test: `backend/tests/test_scale_idx_disk_cache.py`(追加)

**Interfaces:**
- Consumes: Task 3 缓存的 stale 索引(base_indexes 里的实例)。
- Produces: `scale_search_include_delta=False` 时组合图缓存跨查询命中(不随 kg_mutation_seq churn 重建);flag=True 时仍含 `active_ver`(delta 变→重建)。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_combined_graph_cache_hits_under_ingestion(repo, monkeypatch):
    """flag 关:摄取(kg_mutation_seq 变)期间组合图缓存命中,不每查询 _load 重建。"""
    nb = _indexed_nb_with_delta(repo)
    base_indexes = [(nb.id, repo._scale_index(nb.id, allow_stale=True))]
    loads = {"n": 0}
    orig = repo._vector_cache.get

    def counting_get(key, version, loader):
        def wrapped():
            loads["n"] += 1
            return loader()
        return orig(key, version, wrapped)
    monkeypatch.setattr(repo._vector_cache, "get", counting_get)

    repo._scale_combined_graph(nb.id, base_indexes)
    _insert_source_chunk(repo, nb.id, "sC", "cC", "carol", 3)  # bump kg_mutation_seq
    repo._scale_combined_graph(nb.id, base_indexes)
    assert loads["n"] == 1     # flag 关:active churn 不进 key → 第二次命中缓存


def test_combined_graph_rebuilds_when_flag_on_and_delta_changes(repo, monkeypatch):
    """flag 开:delta 变仍触发组合图重建(active_ver 保留在 key 里)。"""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = _indexed_nb_with_delta(repo)
    base_indexes = [(nb.id, repo._scale_index(nb.id, allow_stale=True))]
    loads = {"n": 0}
    orig = repo._vector_cache.get

    def counting_get(key, version, loader):
        def wrapped():
            loads["n"] += 1
            return loader()
        return orig(key, version, wrapped)
    monkeypatch.setattr(repo._vector_cache, "get", counting_get)

    repo._scale_combined_graph(nb.id, base_indexes)
    _insert_source_chunk(repo, nb.id, "sC", "cC", "carol", 3)
    repo._scale_combined_graph(nb.id, base_indexes)
    assert loads["n"] == 2     # flag 开:delta 变 → 版本键变 → 重建
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py -q -k combined`
Expected: `test_combined_graph_cache_hits_under_ingestion` FAIL(`loads['n']==2`——当前 active_ver churn 使第二次 miss)

- [ ] **Step 3: 实现**

把 9471-9476 的 version 构造:

```python
        active_ver = tuple(self._scale_index_version(notebook_id))
        # flag 只入这个组合图缓存的 version 元组(不进 _scale_index_version 的
        # settings_tail——那会让所有存量索引 manifest 失配变 stale)。翻转开关即
        # 自然使这个缓存失效,不需要显式 invalidate。
        version = ("scale_combined", base_ver, active_ver,
                   bool(self.settings.scale_search_include_delta))
```

改为:

```python
        # flag 关(默认):组合图内容由 participants 磁盘 manifest 版本(已在 base_ver)
        # 完全决定——_active_kg_delta 返空、splice 空操作。故不把 churn 的
        # _scale_index_version 计入 key,使摄取期(kg_mutation_seq 每写 bump)缓存命中。
        # flag 开:delta 被 splice 进组合图,内容随 active 变,必须含 active_ver。
        # flag 本身也进 key,翻转开关自然失效,无需显式 invalidate。
        active_ver = (tuple(self._scale_index_version(notebook_id))
                      if self.settings.scale_search_include_delta else None)
        version = ("scale_combined", base_ver, active_ver,
                   bool(self.settings.scale_search_include_delta))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py -q -k combined && python -m pytest tests/test_ppr_retrieve.py tests/test_scale_xlayer_bridge_delta.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_idx_disk_cache.py
git commit -m "fix(scale): 组合图缓存键在 delta 门控关时丢弃 active churn 项

flag 关时组合图内容只由 participants 磁盘 manifest 版本决定,active_ver
(含 kg_mutation_seq)入 key 会让摄取期每查询重建 113 万节点 dict。flag 关→用 None
占位使缓存命中;flag 开→保留 active_ver(delta 变触发重建)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `_active_kg_delta` 门控前早退省 COUNT

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_active_kg_delta`,当前 9294-9301)
- Test: `backend/tests/test_scale_idx_disk_cache.py`(追加)

**Interfaces:**
- Produces: indexed 且 `scale_search_include_delta=False` 时 `_active_kg_delta` 提前 `return [], [], []`,不跑 `_index_delta` 的 delta-chunk 分批 COUNT。gate 结果与今日完全一致。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_active_kg_delta_skips_count_when_gated(repo, monkeypatch):
    """indexed + flag 关:_active_kg_delta 返 ([],[],[]) 且不调 _index_delta 的完整 COUNT。"""
    nb = _indexed_nb_with_delta(repo)
    calls = {"index_delta": 0}
    real = repo._index_delta
    monkeypatch.setattr(repo, "_index_delta",
                        lambda n: (calls.__setitem__("index_delta", calls["index_delta"] + 1), real(n))[1])
    out = repo._active_kg_delta(nb.id)
    assert out == ([], [], [])
    assert calls["index_delta"] == 0   # 门控早退:不触碰 _index_delta


def test_active_kg_delta_gathers_when_flag_on(repo, monkeypatch):
    """flag 开:仍 gather delta(不早退),保持既有行为。"""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = _indexed_nb_with_delta(repo)
    node_ids, edges, chunk_ids = repo._active_kg_delta(nb.id)
    assert "cB" in chunk_ids            # delta chunk 被 gather
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py -q -k active_kg_delta`
Expected: `test_active_kg_delta_skips_count_when_gated` FAIL(`calls['index_delta']==1`——当前先跑 `_index_delta` 再 gate)

- [ ] **Step 3: 实现**

当前 9294-9301:

```python
        delta = self._index_delta(notebook_id)
        if delta["indexed"] and not self.settings.scale_search_include_delta:
            # 同一原则第四处:已索引库的图基底只含已索引部分(核心 CSR 本身),水位后
            # delta 由 auto-fold 收进索引后自然可达。未索引的 active 小库(下方
            # src=None 整库 gather)是 two-tier 联邦的 active 层,不是 delta,
            # 不受此门控。
            return [], [], []
        src = delta["delta_sources"] if delta["indexed"] else None
```

改为(用廉价 manifest 存在性判 indexed,门控命中就早退,不跑 `_index_delta` 的 COUNT):

```python
        # 廉价门控早退:indexed(磁盘有 manifest)且 flag 关时,图基底只含已索引部分,
        # 直接返空——省掉 _index_delta 对 delta_sources 的分批 COUNT(生产 48,739 源、
        # 55 批,结果本会被丢弃)。gate 结果与「先 _index_delta 再判 indexed」一致:
        # _index_delta 的 indexed 恰是 manifest 是否存在。
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        if (os.path.exists(os.path.join(out_dir, "manifest.json"))
                and not self.settings.scale_search_include_delta):
            # 同一原则第四处:已索引库的图基底只含已索引部分(核心 CSR 本身),水位后
            # delta 由 auto-fold 收进索引后自然可达。未索引的 active 小库(下方
            # src=None 整库 gather)是 two-tier 联邦的 active 层,不是 delta,不受门控。
            return [], [], []
        delta = self._index_delta(notebook_id)
        src = delta["delta_sources"] if delta["indexed"] else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_scale_idx_disk_cache.py -q -k active_kg_delta && python -m pytest tests/test_ppr_retrieve.py tests/test_scale_delta_policy.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_idx_disk_cache.py
git commit -m "perf(scale): _active_kg_delta 门控命中时早退,省掉无谓 delta COUNT

indexed(磁盘有 manifest)且 flag 关时直接返空,不再先跑 _index_delta 的
48,739 源分批 COUNT(结果本被丢弃)。indexed 判定=manifest 存在,与原 gate 等价。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 全量验证 + runbook 说明 + PR

**Files:**
- Modify: `docs/superpowers/specs/2026-07-03-scale-index-disk-identity-cache-design.md`(可选:补一行「已实现」)

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿(基线 + 本分支新增 test_scale_idx_disk_cache.py 的用例;0 failed;既有 1 skipped 保持)

- [ ] **Step 2: 仓库检查脚本**

Run: `bash scripts/check.sh`
Expected: 绿

- [ ] **Step 3: rebase + push + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/scale-idx-cache
git fetch origin && git rebase origin/master
git push -u origin feat/scale-index-disk-identity
gh pr create --base master --title "fix(scale): 大库检索按磁盘索引身份缓存(脱离摄取 churn,严格推理恒定成本)" --body "$(cat <<'EOF'
## 问题
大库严格推理在摄取进行时,「初检索得到 N 候选」后的 PPR seed pass 冻结 ~30min。根因不是 delta 门控(PR#178 四处齐全且正确),而是 `_scale_index(allow_stale=True)` 的缓存版本键含 `kg_mutation_seq`(每写 bump)——有 delta 时 `manifest.version != cur` 恒成立,stale 实例永不进缓存,于是每查询重建实例 + 重载 ~8GB kg ANN + ~2GB chunk ANN。

## 修复(严格遵循「大库只检索已索引部分、与是否在新增无关」)
- **核心**:`allow_stale=True`(检索热路径「取磁盘已索引部分」)改按**磁盘 manifest 身份**缓存复用——磁盘索引只在 rebuild/fold 时换,与 kg_mutation_seq 无关。ANN handle 随实例 memoize 存活。cold-load 走 per-nb 单飞锁,防 N 个并发查询各载 8GB。exact 调用方(viz/status)语义字节不变。
- 组合图缓存键在 delta 门控关时丢弃 `active_ver` churn 项(内容只由磁盘 manifest 版本决定)。
- `_active_kg_delta` 门控命中时早退,省掉无谓的 48,739 源 delta COUNT。

## 取舍(用户确认)
恒定成本优先:大库检索恒定 O(1),delta 默认查不到、靠 auto-fold/重建最终收进索引后可见——正是「只检索已索引部分」的应有语义。逃生阀 `SCALE_SEARCH_INCLUDE_DELTA=true` 在需要强一致时回到含 delta 暴力慢路径。

## 净效果
摄取进行时一次严格推理查询:从「每查询重载 ~10GB ANN + 重建 113 万节点组合图」→「进程缓存命中,首次加载后 O(1),直到真正重建索引」。设计文档见 docs/superpowers/specs/2026-07-03-scale-index-disk-identity-cache-design.md。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review 结论

- **Spec 覆盖**:Part1(核心 stale 缓存)=Task 1+2+3;Part2(组合图键)=Task 4;Part3(早退)=Task 5;7 条不变量 → 测试映射:①stale-serve 与 flag 无关正确=Task4 flag-on 用例 + Task3 stale 复用;②exact 不变=test_exact_caller_unchanged_on_delta;③恒定成本=test_stale_index_reused_across_queries;④自愈=test_stale_reload_after_disk_rebuild;⑤单飞=test_concurrent_cold_stale_single_flight;⑥无索引→None=test_no_manifest_returns_none;⑦组合图键=test_combined_graph_* 两个。全部有任务 ✓
- **无占位符**:每步含真实代码/命令/期望输出;测试 fixture 明确来源(test_scale_delta_policy.py 手法)✓
- **类型一致**:`_read_manifest_version(out_dir)->list|None`(T1)与 T3 用 `disk_ver == idx.manifest.get("version")` 比较一致;`_scale_idx_load_lock`/`_scale_idx_load_locks`(T2)与 T3 用法一致;`_active_kg_delta` 返回 `([],[],[])` 三元组与既有签名一致 ✓
- **已知风险**:T3 的并发测试用 time.sleep 有轻微时序依赖(0.05s 窗口足够);combined 图测试依赖 `_vector_cache.get` 的 monkeypatch 拦截 loader 计数——若该缓存实现变化需同步。
