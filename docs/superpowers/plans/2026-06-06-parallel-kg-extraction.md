# 全局并行 KG 抽取调度（两级并发）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 KG 抽取并发收敛为一个独立模块的两级共享池——窗口池（全局 LLM 窗口并发 `KG_EXTRACT_WORKERS`，FIFO）+ 作业池（文档级并发 `KG_JOB_CONCURRENCY`），覆盖逐个/批量上传，并让抽取期 ask 不被饿死。

**Architecture:** 新建 `kg/scheduler.py`，进程内两个独立 `ThreadPoolExecutor` 单例（窗口池/作业池，必须分离以免死锁）。`extract_graph` 不再自建池、改用 `submit_window`；上传分发从顺序 BackgroundTask 改为 `submit_job`。`core/llm.py` 给 OpenAI client 配 httpx 连接池容量 = `KG_EXTRACT_WORKERS + KG_ASK_RESERVE`，保证 ask 永远有连接。

**Tech Stack:** Python / `concurrent.futures` / FastAPI / httpx / OpenAI SDK。

**Spec:** `docs/superpowers/specs/2026-06-06-parallel-kg-extraction-design.md`

**约定**：
- Python：`/opt/homebrew/Caskroom/miniconda/base/bin/python`
- 测试：`cd backend && PYTHONPATH=. $PY -m pytest tests/<file> -q`
- 工作区：worktree `feat/parallel-kg-extraction`（基于 origin/master）。**不**碰 root master（另有 agent 在改）。

---

## 文件结构

- **Create** `backend/app/services/kg/scheduler.py` — 两个进程级单例池 + `submit_window`/`submit_job`/getters/测试用 `configure`/`reset`。
- **Modify** `backend/app/core/config.py` — 加 `kg_job_concurrency`、`kg_ask_reserve`。
- **Modify** `backend/app/services/kg_ingest.py` — `extract_graph` 用 `submit_window`，删 `workers` 形参与自建池。
- **Modify** `backend/app/services/sqlite_repository.py` — `_run_extraction` 调用去掉 `workers=`。
- **Modify** `backend/app/core/llm.py` — OpenAI client 配 httpx 连接池容量。
- **Modify** `backend/app/api/routes.py` — 上传分发改 `submit_job`。
- **Tests**：`backend/tests/test_kg_scheduler.py`（新）、`backend/tests/test_parallel_extraction_wiring.py`（新）。

---

## Task 1: config 旋钮

**Files:** Modify `backend/app/core/config.py:54`（`kg_extract_workers` 行后）; Test `backend/tests/test_parallel_extraction_wiring.py`（新）

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_parallel_extraction_wiring.py`：
```python
def test_settings_concurrency_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_job_concurrency == 8
    assert s.kg_ask_reserve == 64
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "3")
    monkeypatch.setenv("KG_ASK_RESERVE", "16")
    s2 = Settings()
    assert s2.kg_job_concurrency == 3 and s2.kg_ask_reserve == 16
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_parallel_extraction_wiring.py::test_settings_concurrency_knobs -q`
Expected: FAIL（AttributeError: no attribute 'kg_job_concurrency'）。

- [ ] **Step 3: 实现** — 在 `config.py` 的 `kg_extract_workers: int = Field(16, env="KG_EXTRACT_WORKERS")` 一行后插入：
```python
    # 同时抽取的文档数上限（作业池容量）。窗口级并发仍由 KG_EXTRACT_WORKERS 全局封顶。
    kg_job_concurrency: int = Field(8, env="KG_JOB_CONCURRENCY")
    # LLM 连接池为交互式 ask 预留的连接数（连接池容量 = KG_EXTRACT_WORKERS + 此值）。
    kg_ask_reserve: int = Field(64, env="KG_ASK_RESERVE")
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_parallel_extraction_wiring.py::test_settings_concurrency_knobs -q`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add backend/app/core/config.py backend/tests/test_parallel_extraction_wiring.py
git commit -m "feat(config): 作业并发 KG_JOB_CONCURRENCY + ask 连接预留 KG_ASK_RESERVE"
```

---

## Task 2: scheduler 模块（两个全局池）

**Files:** Create `backend/app/services/kg/scheduler.py`; Test `backend/tests/test_kg_scheduler.py`（新）

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_kg_scheduler.py`：
```python
import threading, time
import pytest


@pytest.fixture(autouse=True)
def _reset_pools():
    from app.services.kg import scheduler
    scheduler.reset()
    yield
    scheduler.reset()


def _peak_counter():
    state = {"cur": 0, "peak": 0}
    lock = threading.Lock()
    def task():
        with lock:
            state["cur"] += 1; state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)
        with lock:
            state["cur"] -= 1
        return "ok"
    return state, task


def test_window_pool_caps_concurrency():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    state, task = _peak_counter()
    futs = [scheduler.submit_window(task) for _ in range(6)]
    assert [f.result() for f in futs] == ["ok"] * 6
    assert state["peak"] <= 2


def test_job_pool_caps_concurrency():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=4, job_workers=2)
    state, task = _peak_counter()
    futs = [scheduler.submit_job(task) for _ in range(5)]
    [f.result() for f in futs]
    assert state["peak"] <= 2


def test_submit_window_returns_result():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    assert scheduler.submit_window(lambda a, b: a + b, 2, 3).result() == 5


def test_getters_reflect_config():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=7, job_workers=3)
    assert scheduler.max_workers() == 7 and scheduler.job_concurrency() == 3
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_scheduler.py -q`
Expected: FAIL（ModuleNotFoundError: app.services.kg.scheduler）。

- [ ] **Step 3: 实现** — 新建 `backend/app/services/kg/scheduler.py`：
```python
"""Process-global concurrency for KG extraction.

Two SEPARATE singleton thread pools:
- window pool  (max=KG_EXTRACT_WORKERS): every extract_window LLM call; FIFO,
  the single global cap shared across all documents (intra- + inter-doc).
- job pool     (max=KG_JOB_CONCURRENCY): one process_source per document.

They MUST be separate: a job thread blocks waiting on window futures; if it
held a window-pool slot the pools could deadlock. With two pools, even when all
job threads are blocked the window pool keeps draining windows.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
from typing import Any, Callable

from app.core.config import Settings

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_window_pool: cf.ThreadPoolExecutor | None = None
_job_pool: cf.ThreadPoolExecutor | None = None
_window_max = 0
_job_max = 0


def _build(window_workers: int, job_workers: int) -> None:
    global _window_pool, _job_pool, _window_max, _job_max
    _window_max = max(1, window_workers)
    _job_max = max(1, job_workers)
    _window_pool = cf.ThreadPoolExecutor(
        max_workers=_window_max, thread_name_prefix="kg-window")
    _job_pool = cf.ThreadPoolExecutor(
        max_workers=_job_max, thread_name_prefix="kg-job")


def _ensure() -> None:
    if _window_pool is not None and _job_pool is not None:
        return
    with _lock:
        if _window_pool is None or _job_pool is None:
            s = Settings()
            _build(s.kg_extract_workers, s.kg_job_concurrency)


def submit_window(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> cf.Future:
    """Submit one window (LLM call) to the global window pool."""
    _ensure()
    return _window_pool.submit(fn, *args, **kwargs)


def submit_job(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> cf.Future:
    """Submit one document-extraction job to the job pool (fire-and-forget;
    callee handles its own errors/status). A done-callback logs any unexpected
    exception so it is never silently swallowed."""
    _ensure()
    fut = _job_pool.submit(fn, *args, **kwargs)
    fut.add_done_callback(_log_job_exception)
    return fut


def _log_job_exception(fut: cf.Future) -> None:
    try:
        exc = fut.exception()
    except cf.CancelledError:
        return
    if exc is not None:
        _logger.error("kg extraction job failed", exc_info=exc)


def max_workers() -> int:
    _ensure()
    return _window_max


def job_concurrency() -> int:
    _ensure()
    return _job_max


def configure(*, window_workers: int | None = None, job_workers: int | None = None) -> None:
    """Test-only: rebuild both pools with explicit sizes (falls back to settings
    for any omitted value)."""
    with _lock:
        s = Settings()
        _shutdown_locked()
        _build(
            window_workers if window_workers is not None else s.kg_extract_workers,
            job_workers if job_workers is not None else s.kg_job_concurrency,
        )


def reset() -> None:
    """Test-only: shut down both pools so the next use rebuilds them."""
    with _lock:
        _shutdown_locked()


def _shutdown_locked() -> None:
    global _window_pool, _job_pool
    for pool in (_window_pool, _job_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _window_pool = None
    _job_pool = None
```

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_scheduler.py -q`
Expected: PASS（4 passed）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg/scheduler.py backend/tests/test_kg_scheduler.py
git commit -m "feat(kg): 全局抽取调度模块(窗口池+作业池, 分离防死锁)"
```

---

## Task 3: `extract_graph` 改用窗口池 + `_run_extraction` 去 workers

**Files:** Modify `backend/app/services/kg_ingest.py:134-163` 与顶部 import; Modify `backend/app/services/sqlite_repository.py`（extract_graph 调用处）; Test `backend/tests/test_kg_scheduler.py`（追加）

- [ ] **Step 1: 写失败测试** — 追加到 `tests/test_kg_scheduler.py`（验证 extract_graph 走全局窗口池、且跨并发调用峰值 ≤ 窗口上限）：
```python
def _kg_json():
    import json
    return json.dumps({"nodes": [{"local_id": "a", "type": "Concept",
                                  "name": "x", "ev": 0}], "edges": []})


def test_extract_graph_goes_through_window_pool(monkeypatch):
    from app.services.kg import scheduler
    from app.services import kg_ingest
    scheduler.configure(window_workers=2, job_workers=2)
    seen = {"n": 0}
    real = scheduler.submit_window
    def spy(fn, /, *a, **k):
        seen["n"] += 1
        return real(fn, *a, **k)
    monkeypatch.setattr(kg_ingest, "submit_window", spy)

    class FakeLLM:
        def chat_json(self, messages, hint):
            return _kg_json()

    # raw_text with 3 short paragraphs + tiny window target => 3 windows
    raw = "Para one alpha.\n\nPara two beta.\n\nPara three gamma."
    g = kg_ingest.extract_graph(FakeLLM(), raw, "doc.md", "textbook", n=20, m=0)
    assert seen["n"] >= 1                 # windows went through the global pool
    assert g.nodes                        # produced a graph


def test_window_cap_holds_across_concurrent_extract_graph(monkeypatch):
    from app.services.kg import scheduler
    from app.services import kg_ingest
    import threading, time
    scheduler.configure(window_workers=3, job_workers=4)
    cur = {"v": 0, "peak": 0}; lock = threading.Lock()

    class SlowLLM:
        def chat_json(self, messages, hint):
            with lock:
                cur["v"] += 1; cur["peak"] = max(cur["peak"], cur["v"])
            time.sleep(0.05)
            with lock:
                cur["v"] -= 1
            return _kg_json()

    raw = "\n\n".join(f"Para {i} word{i}." for i in range(6))  # ~6 windows each
    errs = []
    def run():
        try:
            kg_ingest.extract_graph(SlowLLM(), raw, "d.md", "textbook", n=20, m=0)
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    assert cur["peak"] <= 3               # global cap held across 2 docs (old code: up to 2x)
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_scheduler.py -q`
Expected: 两个新增用例 FAIL（`test_extract_graph_goes_through_window_pool`：`kg_ingest` 尚无 `submit_window` 名 → AttributeError；`test_window_cap_holds_across_concurrent_extract_graph`：旧实现每文档自建池，两文档峰值 >3）。Task 2 的用例仍 PASS。

- [ ] **Step 3a: 改 `kg_ingest.py` 顶部 import** — 在 `from app.services.kg.models import ...` 一行后加：
```python
from app.services.kg.scheduler import submit_window
```
并删除不再使用的 `import concurrent.futures as cf`（确认全文件仅 extract_graph 用过它）。`_WORKERS = 16` 这行删除（不再使用）。

- [ ] **Step 3b: 改 `extract_graph`** — 把（`kg_ingest.py:134-163`）：
```python
def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, workers: int = _WORKERS) -> KnowledgeGraph:
    """..."""
    pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file,
                                                          None, n, m) if els]
    nodes: List[Node] = []
    edges: List[Edge] = []
    failed = 0
    if pairs:
        workers = max(1, min(workers, len(pairs)))
        # pool.submit + per-future .result() (NOT pool.map, which aborts on the
        # first exception): one window's network failure must not abort the rest.
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(extract_window, client, els, w.section_path,
                                doc_type, idx)
                    for idx, (w, els) in enumerate(pairs)]
            for fut in futs:
                try:
                    ns, es = fut.result()
                    nodes += ns
                    edges += es
                except Exception:
                    failed += 1
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
```
替换为：
```python
def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450) -> KnowledgeGraph:
    """..."""
    pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file,
                                                          None, n, m) if els]
    nodes: List[Node] = []
    edges: List[Edge] = []
    failed = 0
    if pairs:
        # Submit every window to the process-global window pool (one global cap
        # across all docs, FIFO). Collect only THIS doc's futures so per-source
        # completion semantics are unchanged. submit + per-future .result()
        # (NOT a barrier): one window's failure must not abort the rest.
        futs = [submit_window(extract_window, client, els, w.section_path,
                              doc_type, idx)
                for idx, (w, els) in enumerate(pairs)]
        for fut in futs:
            try:
                ns, es = fut.result()
                nodes += ns
                edges += es
            except Exception:
                failed += 1
    nodes, edges = canonicalize(nodes, edges, doc_id=source_file)
```
（保留 docstring 原文；保留其后的 `return KnowledgeGraph(...)`。）

- [ ] **Step 3c: 改 `_run_extraction` 调用** — 把 `sqlite_repository.py` 里：
```python
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
                workers=self.settings.kg_extract_workers,
            )
```
替换为（去掉 `workers=` 这一行）：
```python
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
            )
```
（上面的 `plan_window_size(len(raw_text), self.settings.kg_extract_workers, ...)` 不动。）

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_scheduler.py tests/kg -q`
Expected: PASS（含既有 kg 套件）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg_ingest.py backend/app/services/sqlite_repository.py backend/tests/test_kg_scheduler.py
git commit -m "feat(kg): extract_graph 走全局窗口池(删每文档自建池)"
```

---

## Task 4: LLM 客户端连接池（ask 优先）

**Files:** Modify `backend/app/core/llm.py:44-57` 与顶部 import; Test `backend/tests/test_parallel_extraction_wiring.py`（追加）

- [ ] **Step 1: 写失败测试** — 追加到 `tests/test_parallel_extraction_wiring.py`：
```python
def test_llm_client_connection_pool_sized(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("KG_EXTRACT_WORKERS", "100")
    monkeypatch.setenv("KG_ASK_RESERVE", "16")
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient(Settings())
    client = c.client()
    limits = client._client._limits  # underlying httpx client limits
    assert limits.max_connections == 116          # 100 + 16
    assert limits.max_keepalive_connections == 16
```
> 注：OpenAI SDK 的底层 httpx client 是 `OpenAI()._client`，其 `_limits` 暴露所配 `httpx.Limits`。若该私有路径在本 SDK 版本不可用，改为断言我们传入的 `httpx.Client` 配置（见 Step 3 把 limit 值挂到一个可读处，或直接断言 `c.client()` 不抛错且 `client.max_retries == 0`）。

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_parallel_extraction_wiring.py::test_llm_client_connection_pool_sized -q`
Expected: FAIL（默认 max_connections 非 116）。

- [ ] **Step 3: 实现** — `core/llm.py` 顶部加 `import httpx`；把 client 构造（44-57）改为：
```python
    def client(self) -> OpenAI:
        if not (self.settings.openai_compat_base_url and self.settings.openai_compat_api_key):
            raise RuntimeError("OpenAI-compatible API settings are not configured")
        if self._client is None:
            # Connection pool sized to the global extraction cap PLUS a reserve
            # for interactive ask, so ask never waits behind extraction for a
            # free connection. (Default httpx max_connections is only 1000.)
            timeout = self.settings.openai_compat_timeout_seconds
            max_conn = self.settings.kg_extract_workers + self.settings.kg_ask_reserve
            http_client = httpx.Client(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=max_conn,
                    max_keepalive_connections=self.settings.kg_ask_reserve,
                ),
            )
            self._client = OpenAI(
                api_key=self.settings.openai_compat_api_key,
                base_url=self.settings.openai_compat_base_url,
                timeout=timeout,
                # Don't let the SDK silently retry connection errors 2x: a stalled
                # connection would otherwise block ~3x the timeout per call. We
                # fail fast and let the caller (per-window extraction) drop it.
                max_retries=0,
                http_client=http_client,
            )
        return self._client
```
> 关键：自带 `httpx.Client` 必须显式设 `timeout`（httpx 默认 5s 会误杀长抽取调用）。

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_parallel_extraction_wiring.py -q`
Expected: PASS。若 `_client._limits` 私有路径不可用，按 Step 1 注释回退断言并说明。

- [ ] **Step 5: Commit**
```bash
git add backend/app/core/llm.py backend/tests/test_parallel_extraction_wiring.py
git commit -m "feat(llm): 连接池容量=抽取上限+ask预留, 自带httpx显式超时"
```

---

## Task 5: 上传分发改作业池

**Files:** Modify `backend/app/api/routes.py`（import + `upload_sources` 第 193 行 lambda）; Test `backend/tests/test_parallel_extraction_wiring.py`（追加）

- [ ] **Step 1: 写失败测试** — 追加（用 TestClient 上传一个小文件，断言分发走 `submit_job(process_source, …)` 而非顺序 BackgroundTask；mock `submit_job` 拦截、不真正跑抽取）：
```python
def test_upload_dispatches_via_submit_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.kg import scheduler

    calls = []
    monkeypatch.setattr(scheduler, "submit_job",
                        lambda fn, /, *a, **k: calls.append((fn, a)) or None)
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/sources",
                    files=[("files", ("a.md", b"# Title\n\nsome text", "text/markdown"))])
    assert r.status_code == 200
    assert len(calls) == 1                       # one job dispatched
    assert calls[0][1][0].startswith("src-")     # called with the new source_id
```

- [ ] **Step 2: 跑测试，确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_parallel_extraction_wiring.py::test_upload_dispatches_via_submit_job -q`
Expected: FAIL（当前走 `background_tasks.add_task`，`submit_job` 未被调用）。

- [ ] **Step 3: 实现** —
(a) `routes.py` 顶部 import 区加：
```python
from app.services.kg import scheduler as kg_scheduler
```
(b) 把 `upload_sources` 里（`routes.py:193`）：
```python
            scheduler=lambda source_id: background_tasks.add_task(repo.process_source, source_id),
```
改为：
```python
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
```
(c) `upload_sources` 的 `background_tasks: BackgroundTasks` 形参已不再使用，删除它（同时 `grep -n "BackgroundTasks" backend/app/api/routes.py` 确认是否还有其它端点用到；若已无引用，删除顶部 `from fastapi import ... BackgroundTasks` 中的该名）。

- [ ] **Step 4: 跑测试，确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_parallel_extraction_wiring.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add backend/app/api/routes.py backend/tests/test_parallel_extraction_wiring.py
git commit -m "feat(api): 上传分发改作业池 submit_job(替代顺序 BackgroundTask)"
```

---

## Task 6: 全量校验

**Files:** 无（仅运行校验）

- [ ] **Step 1: 后端全量**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -q`
Expected: 全绿（含既有 kg/conversations/ask 等；extract_graph 结果不变）。

- [ ] **Step 2: check.sh**
Run: `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`
Expected: EXIT 0。前端 lint 需 `frontend/node_modules`（worktree 无、仅 root 有）；如仅因缺它失败，先 `ln -sfn /Users/hzf/workspace/silicon_notebook/frontend/node_modules <worktree>/frontend/node_modules` 再跑、跑完 `rm -f`。可选：把 `app/services/kg/scheduler.py` 加进 check.sh 的 py_compile 清单。

- [ ] **Step 3: Commit（如对齐 check.sh）**
```bash
git add -A && git commit -m "test: 对齐 check.sh py_compile(scheduler)"
```

---

## 自检：spec 覆盖

- 窗口级全局池(FIFO) → Task 2 + Task 3。✓
- 作业级并发(文档间, KG_JOB_CONCURRENCY) → Task 2(作业池) + Task 5(分发)。✓
- 两池分离防死锁 → Task 2(独立池) + 测试。✓
- 两种上传都快 → Task 5(批量并发) + Task 3(空闲槽复用) + 跨源峰值测试。✓
- ask 优先(连接池+端点留余) → Task 4。✓
- 配置 KG_EXTRACT_WORKERS 语义/KG_JOB_CONCURRENCY/KG_ASK_RESERVE → Task 1 + Task 4 + 说明。✓
- plan_window_size 不动 → 未触碰。✓

## 风险与回归保护
- extract_graph 结果不变：Task 3 跑既有 `tests/kg`；新测验证走全局池 + 跨源峰值 ≤ 上限。
- 连接池私有断言脆弱：Task 4 Step 1 提供回退断言。
- 作业池放大后台 embedding 并发：见 spec 风险；默认 KG_JOB_CONCURRENCY=8 可接受。
- 删 `cf`/`_WORKERS`/`BackgroundTasks` 形参：均需确认无其它引用（plan 内已注明 grep 确认）。
```
