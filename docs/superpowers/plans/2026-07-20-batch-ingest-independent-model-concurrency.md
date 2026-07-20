# Batch Ingest Independent Model Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make source pipelines, traditional LLM calls, and embedding calls independently controllable across every model-using `batch_ingest` phase, with process-wide hard caps and truthful utilization reporting.

**Architecture:** Add batch-scoped concurrency primitives in a focused service module: a semaphore-style LLM gate and a bounded shared embedding executor. RuntimeModelProvider wraps every batch-resolved JSON-chat client with the active LLM gate, while SourceEmbeddingService routes batch embedding work through the active bounded executor. `batch_ingest` owns effective-value resolution, scheduler/settings installation, restoration, phase wiring, and reporting.

**Tech Stack:** Python 3.11+, FastAPI backend services, `threading`, `concurrent.futures`, Pydantic Settings, SQLite repository facade, pytest.

## Global Constraints

- `--workers` means source-pipeline concurrency and falls back to `KG_JOB_CONCURRENCY`.
- `--llm-conc` is the process-wide traditional LLM hard cap and falls back to `KG_EXTRACT_WORKERS`.
- `--embed-conc` is the process-wide embedding hard cap and falls back to `EMBED_CONCURRENCY`.
- Explicit CLI values override Settings; all effective concurrency values must be positive integers.
- `vectors-to-blob --workers` keeps its existing CPU process-pool meaning and CPU-aware default.
- No model-concurrency wait may hold a SQLite write transaction.
- The limits are active only during a `batch_ingest` model phase; normal FastAPI runtime behavior stays unchanged.
- Preserve request-user ContextVar propagation and user-specific model resolution.
- Preserve existing `emb-el`, `emb-ck`, `emb-kg`, and `emb-rel` task-name prefixes for diagnostics.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together.
- Do not add a `fangan_done.md` completion entry: this is operational hardening, not a newly completed product-spec feature.

---

## File Structure

- Create `backend/app/services/model_concurrency.py`
  - Own immutable snapshots, LLM permits, the bounded shared embedding executor, batch activation, and limited JSON-client proxies.
- Modify `backend/app/services/model_provider.py`
  - Apply the active LLM gate to every dynamically resolved primary/KG/rewrite/reasoning client without changing resolution policy.
- Modify `backend/app/services/source_embedding.py`
  - Route every batch-ingestion embedding call through the active bounded executor; retain existing local pools when no batch controller is active.
- Modify `backend/app/services/batch_ingest.py`
  - Resolve CLI/env precedence, install/restore the controller and KG scheduler, add `--llm-conc`, separate reparse workers from embedding, parallelize `kg --limit`, and report gate truth.
- Modify `backend/app/services/source_ingestion.py`
  - Make metadata source concurrency follow `kg_job_concurrency` instead of the fixed eight-worker ceiling.
- Create `backend/tests/test_model_concurrency.py`
  - Unit-test gates, bounded execution, client proxy behavior, exceptions, and activation cleanup.
- Modify `backend/tests/test_user_llm_client_resolve.py`
  - Pin dynamic per-user client limiting and ContextVar behavior.
- Modify `backend/tests/test_embed_concurrency.py`
  - Pin global cross-source embedding limits and task-name compatibility.
- Modify `backend/tests/test_batch_ingest.py`
  - Pin CLI resolution, controller restoration, phase wiring, independent peaks, `reparse --workers`, and parallel `kg --limit`.
- Modify `README.md`, `README_zh.md`, and `AGENTS.md`
  - Replace the multiplicative embedding contract with the three independent hard caps.

---

### Task 1: Batch-Scoped Concurrency Primitives

**Files:**
- Create: `backend/app/services/model_concurrency.py`
- Create: `backend/tests/test_model_concurrency.py`

**Interfaces:**
- Produces: `ConcurrencySnapshot(active: int, maximum: int, waiting: int)`.
- Produces: `ConcurrencyGate(maximum: int)`, with `slot()` and `snapshot()`.
- Produces: `BoundedEmbeddingExecutor(maximum: int)`, with `submit(fn, /, *args, task_prefix: str, **kwargs)`, `run(...)`, `snapshot()`, and `shutdown()`.
- Produces: `ModelConcurrencyState(llm, embedding)`.
- Produces: `activate_model_concurrency(llm_max: int, embed_max: int)` context manager.
- Produces: `current_model_concurrency() -> ModelConcurrencyState | None`.
- Produces: `LimitedJsonChatClient(delegate, gate)`.

- [ ] **Step 1: Write failing tests for permit accounting, exception release, and activation cleanup**

Add these tests to `backend/tests/test_model_concurrency.py`:

```python
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.model_concurrency import (
    BoundedEmbeddingExecutor,
    ConcurrencyGate,
    LimitedJsonChatClient,
    activate_model_concurrency,
    current_model_concurrency,
)


def test_gate_enforces_maximum_and_releases_after_exception():
    gate = ConcurrencyGate(2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def work(fail: bool = False):
        nonlocal active, peak
        with gate.slot():
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                if fail:
                    raise RuntimeError("boom")
            finally:
                with lock:
                    active -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(work, i == 0) for i in range(8)]
        for future in futures:
            try:
                future.result()
            except RuntimeError:
                pass

    assert peak == 2
    assert gate.snapshot().active == 0
    assert gate.snapshot().waiting == 0


def test_activation_is_process_visible_and_restored():
    assert current_model_concurrency() is None
    with activate_model_concurrency(llm_max=3, embed_max=2) as state:
        assert current_model_concurrency() is state
        assert state.llm.snapshot().maximum == 3
        assert state.embedding.snapshot().maximum == 2
    assert current_model_concurrency() is None


def test_non_positive_limits_are_rejected():
    with pytest.raises(ValueError, match="positive"):
        ConcurrencyGate(0)
    with pytest.raises(ValueError, match="positive"):
        BoundedEmbeddingExecutor(-1)
```

- [ ] **Step 2: Run the primitive tests and confirm the missing-module failure**

Run:

```bash
PYTHONPATH=backend pytest -q backend/tests/test_model_concurrency.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.services.model_concurrency'`.

- [ ] **Step 3: Implement the immutable snapshot, LLM gate, and batch activation**

Create `backend/app/services/model_concurrency.py` with:

```python
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator


@dataclass(frozen=True)
class ConcurrencySnapshot:
    active: int
    maximum: int
    waiting: int


class ConcurrencyGate:
    def __init__(self, maximum: int) -> None:
        if int(maximum) <= 0:
            raise ValueError("model concurrency must be a positive integer")
        self.maximum = int(maximum)
        self._condition = threading.Condition()
        self._active = 0
        self._waiting = 0

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._condition:
            self._waiting += 1
            try:
                while self._active >= self.maximum:
                    self._condition.wait()
                self._active += 1
            finally:
                self._waiting -= 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify()

    def snapshot(self) -> ConcurrencySnapshot:
        with self._condition:
            return ConcurrencySnapshot(
                active=self._active,
                maximum=self.maximum,
                waiting=self._waiting,
            )


@dataclass(frozen=True)
class ModelConcurrencyState:
    llm: ConcurrencyGate
    embedding: "BoundedEmbeddingExecutor"


_state_lock = threading.Lock()
_active_state: ModelConcurrencyState | None = None


def current_model_concurrency() -> ModelConcurrencyState | None:
    with _state_lock:
        return _active_state


@contextmanager
def activate_model_concurrency(
    *, llm_max: int, embed_max: int
) -> Iterator[ModelConcurrencyState]:
    global _active_state
    state = ModelConcurrencyState(
        llm=ConcurrencyGate(llm_max),
        embedding=BoundedEmbeddingExecutor(embed_max),
    )
    with _state_lock:
        if _active_state is not None:
            state.embedding.shutdown()
            raise RuntimeError("model concurrency is already active")
        _active_state = state
    try:
        yield state
    finally:
        with _state_lock:
            if _active_state is state:
                _active_state = None
        state.embedding.shutdown()
```

Keep `BoundedEmbeddingExecutor` referenced by the activation function in the same module; Python resolves it when the context manager is entered, after module initialization is complete.

- [ ] **Step 4: Add failing tests for bounded embedding execution and task prefixes**

Append:

```python
def test_bounded_embedding_executor_caps_work_and_preserves_task_prefix():
    executor = BoundedEmbeddingExecutor(2)
    lock = threading.Lock()
    active = 0
    peak = 0
    names = set()

    def work(value: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            names.add(threading.current_thread().name)
        try:
            time.sleep(0.03)
            return value * 2
        finally:
            with lock:
                active -= 1

    try:
        with ThreadPoolExecutor(max_workers=6) as callers:
            futures = [
                callers.submit(executor.run, work, i, task_prefix="emb-el")
                for i in range(6)
            ]
            assert [future.result() for future in futures] == [i * 2 for i in range(6)]
    finally:
        executor.shutdown()

    assert peak == 2
    assert names
    assert len(names) <= 2
    assert all(name.startswith("emb-el") for name in names)
    assert executor.snapshot().active == 0
```

- [ ] **Step 5: Implement the bounded shared embedding executor**

Add the following above `ModelConcurrencyState`:

```python
from concurrent.futures import Future, ThreadPoolExecutor


class BoundedEmbeddingExecutor:
    def __init__(self, maximum: int) -> None:
        if int(maximum) <= 0:
            raise ValueError("model concurrency must be a positive integer")
        self.maximum = int(maximum)
        self._executor = ThreadPoolExecutor(
            max_workers=self.maximum,
            thread_name_prefix="emb-global",
        )
        self._admission = threading.BoundedSemaphore(self.maximum)
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0
        self._closed = False

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        task_prefix: str,
        **kwargs: Any,
    ) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("embedding executor is closed")
            self._waiting += 1
        self._admission.acquire()
        with self._lock:
            self._waiting -= 1

        def invoke() -> Any:
            thread = threading.current_thread()
            original_name = thread.name
            with self._lock:
                self._active += 1
            thread.name = f"{task_prefix}-{original_name}"
            try:
                return fn(*args, **kwargs)
            finally:
                thread.name = original_name
                with self._lock:
                    self._active -= 1
                self._admission.release()

        try:
            return self._executor.submit(invoke)
        except BaseException:
            self._admission.release()
            raise

    def run(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        task_prefix: str,
        **kwargs: Any,
    ) -> Any:
        return self.submit(
            fn, *args, task_prefix=task_prefix, **kwargs
        ).result()

    def snapshot(self) -> ConcurrencySnapshot:
        with self._lock:
            return ConcurrencySnapshot(
                active=self._active,
                maximum=self.maximum,
                waiting=self._waiting,
            )

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
```

- [ ] **Step 6: Add and implement the limited JSON-client proxy**

Append this test:

```python
class _FakeJsonClient:
    configured = True
    model = "fake-model"

    def chat_json(self, messages, schema="", **kwargs):
        if messages == "fail":
            raise RuntimeError("chat failed")
        time.sleep(0.02)
        return '{"ok": true}'


def test_limited_json_client_delegates_attributes_and_releases_on_error():
    gate = ConcurrencyGate(1)
    client = LimitedJsonChatClient(_FakeJsonClient(), gate)
    assert client.configured is True
    assert client.model == "fake-model"
    assert client.chat_json([], "{}") == '{"ok": true}'
    with pytest.raises(RuntimeError, match="chat failed"):
        client.chat_json("fail")
    assert gate.snapshot().active == 0
```

Add:

```python
class LimitedJsonChatClient:
    def __init__(self, delegate: Any, gate: ConcurrencyGate) -> None:
        self._delegate = delegate
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def chat_json(self, *args: Any, **kwargs: Any) -> Any:
        with self._gate.slot():
            return self._delegate.chat_json(*args, **kwargs)
```

- [ ] **Step 7: Run tests and commit the primitives**

Run:

```bash
PYTHONPATH=backend pytest -q backend/tests/test_model_concurrency.py
```

Expected: all tests pass.

Commit:

```bash
git add backend/app/services/model_concurrency.py backend/tests/test_model_concurrency.py
git commit -m "feat: add batch model concurrency primitives"
```

---

### Task 2: Apply the LLM Gate at Dynamic Model Resolution

**Files:**
- Modify: `backend/app/services/model_provider.py`
- Modify: `backend/tests/test_user_llm_client_resolve.py`
- Modify: `backend/tests/test_model_concurrency.py`

**Interfaces:**
- Consumes: `current_model_concurrency()` and `LimitedJsonChatClient`.
- Produces: `RuntimeModelProvider._limit_json_client(client)`.
- Contract: without an active batch state, all existing client identity behavior is unchanged.
- Contract: with an active batch state, all role resolution returns a proxy bound to the same LLM gate.

- [ ] **Step 1: Write failing tests for inactive identity and active user-specific limiting**

Append to `backend/tests/test_user_llm_client_resolve.py`:

```python
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.services.model_concurrency import activate_model_concurrency


class _ConcurrentJsonClient:
    configured = True
    model = "limited-model"

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def chat_json(self, messages, schema="", **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.03)
            return "{}"
        finally:
            with self.lock:
                self.active -= 1


def test_batch_gate_limits_primary_and_kg_clients_together(tmp_path):
    repo = _repo(tmp_path)
    raw = _ConcurrentJsonClient()
    repo.llm_client = raw

    with activate_model_concurrency(llm_max=2, embed_max=1):
        primary = repo.llm_client
        kg = repo.kg_llm_client
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(primary.chat_json if i % 2 else kg.chat_json, [], "{}")
                for i in range(8)
            ]
            for future in futures:
                future.result()

    assert raw.peak == 2
    assert repo.llm_client is raw
```

The final assertion pins the existing no-gate identity contract used by `test_no_user_config_fallback_to_system_and_setter`.

- [ ] **Step 2: Run the focused test and confirm it exceeds the requested peak**

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_user_llm_client_resolve.py::test_batch_gate_limits_primary_and_kg_clients_together
```

Expected: FAIL because raw clients are returned and `raw.peak` reaches more than 2.

- [ ] **Step 3: Implement gate-aware client wrapping**

Add imports:

```python
from app.services.model_concurrency import (
    LimitedJsonChatClient,
    current_model_concurrency,
)
```

Add:

```python
def _limit_json_client(self, client: Any) -> Any:
    state = current_model_concurrency()
    if state is None:
        return client
    return LimitedJsonChatClient(client, state.llm)
```

Do not retain proxies in RuntimeModelProvider: a batch scope is short-lived, and keeping a proxy
cache would retain completed gates across repeated programmatic batch runs. Primary and KG roles
that resolve to the same raw client receive lightweight proxies sharing the same gate; one
top-level `chat_json` still acquires exactly once.

Refactor `_llm_for_role` so it resolves `raw` exactly as before, then returns
`self._limit_json_client(raw)`:

```python
def _llm_for_role(self, role: str):
    cfg = self.identity.resolve_model_config(self.identity.current_user(), role)
    if cfg.source == "user":
        raw = self._user_llm_cached(cfg)
    elif cfg.source == "none":
        raw = _UNCONFIGURED_LLM
    else:
        raw = self._system_llm_for(role)
    return self._limit_json_client(raw)
```

- [ ] **Step 4: Add a ContextVar regression test**

Add a test that configures a user primary model, sets the request user, enters
`activate_model_concurrency`, resolves `repo.kg_llm_client`, and asserts the proxy delegates
`base_url == "https://u/v1"` and `model == "m-u"`. This pins that limiting happens after
user-role resolution:

```python
def test_batch_gate_preserves_user_model_resolution(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings(
        "user-local",
        {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m-u"}},
    )
    tok = set_request_user(repo.current_user())
    try:
        with activate_model_concurrency(llm_max=1, embed_max=1):
            client = repo.kg_llm_client
            assert client.base_url == "https://u/v1"
            assert client.model == "m-u"
    finally:
        reset_request_user(tok)
```

- [ ] **Step 5: Run model-resolution tests and commit**

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_model_concurrency.py \
  backend/tests/test_user_llm_client_resolve.py
```

Expected: all tests pass.

Commit:

```bash
git add backend/app/services/model_provider.py \
  backend/tests/test_user_llm_client_resolve.py \
  backend/tests/test_model_concurrency.py
git commit -m "feat: enforce batch-wide LLM concurrency"
```

---

### Task 3: Route Embedding Through One Bounded Shared Executor

**Files:**
- Modify: `backend/app/services/source_embedding.py`
- Modify: `backend/tests/test_embed_concurrency.py`
- Modify: `backend/tests/test_kg_object_embed_concurrency.py`

**Interfaces:**
- Consumes: `current_model_concurrency()`.
- Produces: `SourceEmbeddingService._map_embedding_batches(fn, batches, task_prefix)`.
- Produces: `SourceEmbeddingService._run_embedding_call(fn, task_prefix)`.
- Contract: active batch state uses the shared bounded executor; inactive state preserves existing local-pool behavior and prefixes.

- [ ] **Step 1: Write a failing cross-source global-cap test**

Extend the test helper so two sources can be embedded concurrently under one batch state:

```python
from concurrent.futures import ThreadPoolExecutor
from app.services.model_concurrency import activate_model_concurrency


def test_two_sources_share_one_embedding_hard_cap(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    source_ids = [
        _insert_source_with_elements(repo, nb.id, 40),
        _insert_source_with_elements(repo, nb.id, 40),
        _insert_source_with_elements(repo, nb.id, 40),
    ]
    emb = _ConcEmbedder(dim=8)
    repo.embedder = emb

    with activate_model_concurrency(llm_max=8, embed_max=2):
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(repo._embed_source, sid) for sid in source_ids]
            for future in futures:
                future.result()

    assert emb.max_concurrent == 2
```

- [ ] **Step 2: Run the focused test and verify the multiplicative failure**

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_embed_concurrency.py::test_two_sources_share_one_embedding_hard_cap
```

Expected: FAIL because each source still owns an independent local pool and peak concurrency exceeds 2.

- [ ] **Step 3: Add reusable batch-aware execution helpers**

Import:

```python
from app.services.model_concurrency import current_model_concurrency
```

Add to `SourceEmbeddingService`:

```python
def _map_embedding_batches(
    self,
    fn: Callable[[Any], list],
    batches: list,
    *,
    task_prefix: str,
) -> list[list]:
    state = current_model_concurrency()
    if state is not None:
        futures = [
            state.embedding.submit(fn, batch, task_prefix=task_prefix)
            for batch in batches
        ]
        return [future.result() for future in futures]

    workers = max(1, min(self.settings.embed_concurrency, len(batches)))
    with _cf.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=task_prefix
    ) as pool:
        return list(pool.map(fn, batches))

def _run_embedding_call(
    self, fn: Callable[[], Any], *, task_prefix: str
) -> Any:
    state = current_model_concurrency()
    if state is None:
        return fn()
    return state.embedding.run(fn, task_prefix=task_prefix)
```

The existing typing import already provides `Any` and `Callable`; keep those imports and add no
new dependency.

- [ ] **Step 4: Replace each local batch pool with the helper**

For `embed_source`, replace the local executor block with:

```python
rows = []
for part in self._map_embedding_batches(
    _embed_only, batches, task_prefix="emb-el"
):
    rows.extend(part)
```

For `embed_objects_batch`, preserve incremental flush/progress ordering by iterating the returned
parts with their original batch indexes:

```python
parts = self._map_embedding_batches(
    _embed_only, batches, task_prefix="emb-kg"
)
for bi, part in enumerate(parts, 1):
    buf.extend(part)
    done += len(batches[bi - 1])
    if bi % commit_every == 0:
        self.flush_object_vectors(notebook_id, buf)
        buf = []
        if progress:
            progress(done, total)
```

For `embed_relations_batch`, replace the local executor with:

```python
rows = []
for part in self._map_embedding_batches(
    _embed_only, batches, task_prefix="emb-rel"
):
    rows.extend(part)
```

For `embed_chunks_batch`, replace the local executor with:

```python
out = []
for part in self._map_embedding_batches(
    _emb, batches, task_prefix="emb-ck"
):
    out.extend(part)
```

In `embed_knowledge`, keep its best-effort exception policy and wrap only the network call:

```python
try:
    vector = self._run_embedding_call(
        lambda: self.embedder().embed_query(text[:2000]),
        task_prefix="emb-kg",
    )
except Exception:
    return
```

In `embed_chunk_ids`, preserve exception propagation and wrap only `embed_texts`:

```python
vectors = self._run_embedding_call(
    lambda: embedder.embed_texts(texts),
    task_prefix="emb-ck",
)
```

Do not wrap database writes.

- [ ] **Step 5: Pin prefixes and object/chunk behavior under active batch state**

Update the existing prefix test to enter:

```python
with activate_model_concurrency(llm_max=2, embed_max=2):
    repo._embed_source(sid)
```

Add an active-state case to `test_kg_object_embed_concurrency.py` that embeds 35 objects under
`embed_max=2`, asserts all 35 persisted, and asserts `emb.max_concurrent == 2`.

- [ ] **Step 6: Run embedding tests and commit**

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_model_concurrency.py \
  backend/tests/test_embed_concurrency.py \
  backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_chunk_embed.py \
  backend/tests/test_relation_embed.py
```

Expected: all tests pass; existing inactive-state concurrency tests remain green.

Commit:

```bash
git add backend/app/services/source_embedding.py \
  backend/tests/test_embed_concurrency.py \
  backend/tests/test_kg_object_embed_concurrency.py
git commit -m "feat: cap embedding concurrency across batch sources"
```

---

### Task 4: Resolve, Install, Restore, and Report Effective CLI Limits

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `backend/tests/test_batch_ingest.py`

**Interfaces:**
- Consumes: `activate_model_concurrency`, `ModelConcurrencyState`.
- Produces: `EffectiveConcurrency(workers, llm, embedding, *_source)`.
- Produces: `_resolve_effective_concurrency(args, settings, phase)`.
- Produces: `_batch_concurrency_scope(repo, effective)`.
- Produces: reporter fields `llm_active/max/waiting` and `embed_active/max/waiting`.

- [ ] **Step 1: Write failing parser/default/validation tests**

Add:

```python
def test_model_concurrency_cli_omission_inherits_settings(monkeypatch):
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "11")
    monkeypatch.setenv("KG_EXTRACT_WORKERS", "13")
    monkeypatch.setenv("EMBED_CONCURRENCY", "3")
    args = bi.build_arg_parser().parse_args(["reparse", "--notebook-id", "nb-x"])
    effective = bi._resolve_effective_concurrency(args, Settings(), "reparse")
    assert (effective.workers, effective.llm, effective.embedding) == (11, 13, 3)
    assert (
        effective.workers_source,
        effective.llm_source,
        effective.embedding_source,
    ) == ("env", "env", "env")


def test_model_concurrency_cli_overrides_settings(monkeypatch):
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "11")
    monkeypatch.setenv("KG_EXTRACT_WORKERS", "13")
    monkeypatch.setenv("EMBED_CONCURRENCY", "3")
    args = bi.build_arg_parser().parse_args([
        "reparse", "--notebook-id", "nb-x",
        "--workers", "20", "--llm-conc", "16", "--embed-conc", "2",
    ])
    effective = bi._resolve_effective_concurrency(args, Settings(), "reparse")
    assert (effective.workers, effective.llm, effective.embedding) == (20, 16, 2)
    assert effective.workers_source == "cli"
    assert effective.llm_source == "cli"
    assert effective.embedding_source == "cli"


@pytest.mark.parametrize(
    "flag", ["--workers", "--llm-conc", "--embed-conc"]
)
def test_model_concurrency_rejects_non_positive_values(flag):
    args = bi.build_arg_parser().parse_args([
        "reparse", "--notebook-id", "nb-x", flag, "0",
    ])
    with pytest.raises(ValueError, match="positive"):
        bi._resolve_effective_concurrency(args, Settings(), "reparse")


def test_main_reports_non_positive_model_concurrency(repo, capsys):
    nb_id = bi.ensure_notebook(repo, None, "nb-invalid-concurrency")
    rc = bi.main([
        "reparse",
        "--notebook-id",
        nb_id,
        "--embed-conc",
        "0",
        "--allow-no-embed",
    ])
    assert rc == 2
    assert "positive integer" in capsys.readouterr().err
```

- [ ] **Step 2: Run focused tests and verify missing symbol/argument failures**

Run:

```bash
PYTHONPATH=backend pytest -q backend/tests/test_batch_ingest.py \
  -k "model_concurrency_cli or model_concurrency_rejects"
```

Expected: FAIL because `--llm-conc` and `_resolve_effective_concurrency` do not exist.

- [ ] **Step 3: Implement effective-value resolution**

Add:

```python
from contextlib import contextmanager
from dataclasses import dataclass
from app.services.model_concurrency import (
    ModelConcurrencyState,
    activate_model_concurrency,
    current_model_concurrency,
)


@dataclass(frozen=True)
class EffectiveConcurrency:
    workers: int
    llm: int
    embedding: int
    workers_source: str
    llm_source: str
    embedding_source: str


def _positive(value: int, name: str) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _resolve_effective_concurrency(
    args: argparse.Namespace, settings: Settings, phase: str
) -> EffectiveConcurrency:
    workers_default = (
        _BACKFILL_DEFAULT_WORKERS
        if phase == "vectors-to-blob"
        else settings.kg_job_concurrency
    )
    return EffectiveConcurrency(
        workers=_positive(
            args.workers if args.workers is not None else workers_default,
            "--workers",
        ),
        llm=_positive(
            args.llm_conc
            if args.llm_conc is not None
            else settings.kg_extract_workers,
            "--llm-conc",
        ),
        embedding=_positive(
            args.embed_conc
            if args.embed_conc is not None
            else settings.embed_concurrency,
            "--embed-conc",
        ),
        workers_source="cli" if args.workers is not None else "env",
        llm_source="cli" if args.llm_conc is not None else "env",
        embedding_source="cli" if args.embed_conc is not None else "env",
    )
```

Change parser defaults for `--workers` and `--embed-conc` to `None`, add:

```python
p.add_argument(
    "--llm-conc",
    type=int,
    default=None,
    help="传统 LLM 全局并发硬上限；省略时继承 KG_EXTRACT_WORKERS",
)
```

Update the other two help strings to state their environment fallbacks and independent semantics.
Remove the old pre-resolution block that assigns `args.workers = 4`.

In `main`, catch `ValueError` from `_resolve_effective_concurrency`, print
`error: {exception}` to stderr, and return `2` before repository phase work begins.

- [ ] **Step 4: Write a failing scope-restoration test**

Add:

```python
def test_batch_concurrency_scope_configures_and_restores(repo):
    from app.services.kg import scheduler
    from app.services.model_concurrency import current_model_concurrency

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    old_window = scheduler.max_workers()
    old_job = scheduler.job_concurrency()
    effective = bi.EffectiveConcurrency(
        workers=7, llm=5, embedding=2,
        workers_source="cli", llm_source="cli", embedding_source="cli",
    )

    with bi._batch_concurrency_scope(repo, effective):
        assert scheduler.job_concurrency() == 7
        assert scheduler.max_workers() == 5
        assert repo.settings.embed_concurrency == 2
        assert current_model_concurrency() is not None

    assert current_model_concurrency() is None
    assert scheduler.job_concurrency() == old_job
    assert scheduler.max_workers() == old_window
    assert (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    ) == old_settings
```

Add the failure-precedence regression:

```python
def test_batch_scope_restore_failure_does_not_mask_phase_error(
    repo, monkeypatch, capsys
):
    from app.services.kg import scheduler

    effective = bi.EffectiveConcurrency(
        workers=2,
        llm=2,
        embedding=1,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    real_configure = scheduler.configure
    calls = 0

    def flaky_configure(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("restore failed")
        return real_configure(**kwargs)

    monkeypatch.setattr(scheduler, "configure", flaky_configure)
    try:
        with pytest.raises(ValueError, match="phase failed"):
            with bi._batch_concurrency_scope(repo, effective):
                raise ValueError("phase failed")
        assert "failed to restore KG scheduler" in capsys.readouterr().err
    finally:
        scheduler.reset()
```

- [ ] **Step 5: Implement the scope and restoration**

Add:

```python
@contextmanager
def _batch_concurrency_scope(
    repo: BatchIngestRepository,
    effective: EffectiveConcurrency,
):
    from app.services.kg import scheduler

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    old_window = scheduler.max_workers()
    old_job = scheduler.job_concurrency()
    repo.settings.kg_job_concurrency = effective.workers
    repo.settings.kg_extract_workers = effective.llm
    repo.settings.embed_concurrency = effective.embedding
    scheduler.configure(
        window_workers=effective.llm,
        job_workers=effective.workers,
    )
    phase_error: BaseException | None = None
    try:
        with activate_model_concurrency(
            llm_max=effective.llm,
            embed_max=effective.embedding,
        ) as state:
            yield state
    except BaseException as exc:
        phase_error = exc
        raise
    finally:
        (
            repo.settings.kg_job_concurrency,
            repo.settings.kg_extract_workers,
            repo.settings.embed_concurrency,
        ) = old_settings
        try:
            scheduler.configure(
                window_workers=old_window,
                job_workers=old_job,
            )
        except Exception as restore_error:
            if phase_error is None:
                raise
            print(
                "warning: failed to restore KG scheduler after phase error: "
                f"{type(restore_error).__name__}: {restore_error}",
                file=sys.stderr,
            )
```

Install this scope once around every model-using phase dispatch in `main`. Keep the pure
`vectors-to-blob` and `backfill-source-index` early-return paths outside it. Run `metadata` inside
the scope but before the general EMBED-required guard, because metadata requires an LLM but no
embedder. The remaining model-aware phases enter the scope after their existing provider
validation and notebook resolution. Print:

```python
print(
    "concurrency: "
    f"source={effective.workers}({effective.workers_source}) "
    f"llm={effective.llm}({effective.llm_source}) "
    f"embedding={effective.embedding}({effective.embedding_source})",
    flush=True,
)
```

Pass only phase-owned resolved values, never raw optional argparse fields: `effective.workers` to
`run_ingest`, `effective.embedding` to embedding/backfill helpers, while scheduler-using phases
consume `effective.workers` and `effective.llm` from the already installed scope.

- [ ] **Step 6: Pin the effective-value startup line**

Add:

```python
def test_main_prints_effective_concurrency(repo, monkeypatch, capsys):
    nb_id = bi.ensure_notebook(repo, None, "nb-effective-concurrency")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(
        bi,
        "run_reparse",
        lambda repo, notebook_id, **kwargs: {
            "targets": 0,
            "reparsed": 0,
            "failed": 0,
            "clusters": 0,
            "nodes_embedded": 0,
        },
    )

    rc = bi.main([
        "reparse",
        "--notebook-id",
        nb_id,
        "--workers",
        "32",
        "--llm-conc",
        "24",
        "--embed-conc",
        "4",
        "--allow-no-embed",
        "--no-rebuild",
    ])

    assert rc == 0
    assert (
        "concurrency: source=32(cli) llm=24(cli) embedding=4(cli)"
        in capsys.readouterr().out
    )
```

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_batch_ingest.py::test_main_prints_effective_concurrency
```

Expected: PASS after main dispatch is inside `_batch_concurrency_scope`.

- [ ] **Step 7: Replace thread-name estimates with gate snapshots**

Change `_PoolReporter._loop` to read the active model state:

```python
state = current_model_concurrency()
llm_snapshot = state.llm.snapshot() if state is not None else None
embed_snapshot = state.embedding.snapshot() if state is not None else None
```

Change `_format_pool_snapshot` to render:

```text
LLM {active}/{maximum} waiting={waiting}
embedding {active}/{maximum} waiting={waiting}
source {job_active}/{job_max}
```

Keep source completion/rebuild labels. Emit the six model fields into the manifest pool event.
Add this pure formatting test:

```python
from app.services.model_concurrency import ConcurrencySnapshot


def test_pool_snapshot_reports_gate_truth():
    line = bi._format_pool_snapshot(
        "17:52:33",
        {
            "window_active": 7,
            "window_max": 24,
            "job_active": 29,
            "job_max": 32,
        },
        llm=ConcurrencySnapshot(active=23, maximum=24, waiting=5),
        embedding=ConcurrencySnapshot(active=4, maximum=4, waiting=18),
        done=5,
        total=40,
    )
    assert "LLM 23/24 waiting=5" in line
    assert "embedding 4/4 waiting=18" in line
    assert "source 29/32" in line
    assert "源完成 5/40" in line
```

- [ ] **Step 8: Run CLI/controller tests and commit**

Run:

```bash
PYTHONPATH=backend pytest -q backend/tests/test_batch_ingest.py \
  -k "concurrency or pool_snapshot or arg_parser"
```

Expected: all selected tests pass.

Commit:

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat: add independent batch concurrency controls"
```

---

### Task 5: Wire Independent Limits Through Every Model Phase

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/tests/test_batch_ingest.py`

**Interfaces:**
- Consumes: the controller installed by Task 4.
- Produces: one scheduler owner: `_batch_concurrency_scope`.
- Contract: `run_all`, `run_kg`, and `run_reparse` submit work to the already configured scheduler and never resize either scheduler pool.
- Contract: `conc` remains the helper-level embedding value for minimal call-site churn; it never configures a source job pool.
- Contract: `kg --limit` and metadata use source concurrency independently of LLM/embedding caps.

- [ ] **Step 1: Write a failing single-owner scheduler test**

Replace the old tests that expected `run_all`/`run_reparse` to resize scheduler pools with:

```python
def test_run_reparse_does_not_reconfigure_scheduler(repo, monkeypatch):
    from app.services.kg import scheduler

    configure_calls = []
    monkeypatch.setattr(
        scheduler, "configure", lambda **kwargs: configure_calls.append(kwargs)
    )
    monkeypatch.setattr(
        repo.maintenance, "source_ids", lambda notebook_id: []
    )
    monkeypatch.setattr(
        repo.maintenance, "sources_with_elements", lambda notebook_id: set()
    )

    bi.run_reparse(
        repo,
        bi.ensure_notebook(repo, None, "nb-reparse-owner"),
        conc=2,
        no_rebuild=True,
    )

    assert configure_calls == []
```

Add the equivalent assertion for `run_all` with no input files and a stubbed rebuild. These tests
pin that a later phase helper cannot reset `--llm-conc` by calling
`scheduler.configure(job_workers=...)`, whose omitted window value would otherwise fall back to a
fresh `Settings`.

```python
def test_run_all_does_not_reconfigure_scheduler(repo, monkeypatch):
    from app.services.kg import scheduler

    configure_calls = []
    monkeypatch.setattr(
        scheduler, "configure", lambda **kwargs: configure_calls.append(kwargs)
    )
    monkeypatch.setattr(
        repo,
        "rebuild_unified_kg",
        lambda notebook_id, progress=None, force=False, fresh=False: 0,
    )
    monkeypatch.setattr(
        bi, "backfill_node_embeddings", lambda repo, notebook_id, conc: 0
    )

    bi.run_all(
        repo,
        bi.ensure_notebook(repo, None, "nb-all-owner"),
        [],
        conc=2,
        report_interval=0,
    )

    assert configure_calls == []
```

- [ ] **Step 2: Remove phase-local scheduler resizing**

Keep the existing phase-local temporary embedding setting where compatibility requires it:

```python
repo.settings.embed_concurrency = conc
```

Delete `_sched.configure(...)` from `run_all` and `_sched_mod.configure(...)` from `run_reparse`.
Do not add a scheduler call to `run_kg`. Update their docstrings: the outer
`_batch_concurrency_scope` owns both pools and the phase helpers only submit jobs.

Remove the now-unused `workers` parameter from `run_all` and update its call sites. `run_ingest`
retains `workers`, because it owns a file-parsing executor rather than the KG scheduler.

The CLI still passes `effective.embedding` as `conc` to existing helper-level embedding
backfills. It does not pass `effective.workers` into scheduler-using phase helpers; that value has
already been installed before dispatch.

- [ ] **Step 3: Write a failing `kg --limit` source-concurrency test**

Create several target IDs, replace `maintenance.run_extraction` with a sleeping fake that records
active source calls, install `workers=3` through `_batch_concurrency_scope`, and invoke
`run_kg(limit=N, no_rebuild=True)`. Assert peak source concurrency is 3:

```python
def test_run_kg_limit_parallelizes_sources_with_workers(repo, monkeypatch):
    lock = threading.Lock()
    active = 0
    peak = 0
    targets = [f"src-{i}" for i in range(6)]

    monkeypatch.setattr(repo.maintenance, "source_ids", lambda notebook_id: targets)
    monkeypatch.setattr(
        repo.maintenance, "kg_covered_source_ids", lambda notebook_id: set()
    )
    monkeypatch.setattr(repo, "llm_client", _StubLLM())

    def extract(source_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(repo.maintenance, "run_extraction", extract)
    effective = bi.EffectiveConcurrency(
        workers=3,
        llm=8,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    with bi._batch_concurrency_scope(repo, effective):
        bi.run_kg(
            repo,
            bi.ensure_notebook(repo, None, "nb-kg-limit"),
            limit=6,
            no_rebuild=True,
        )
    assert peak == 3
```

Add `threading` and `time` imports to the test module.

- [ ] **Step 4: Parallelize the limited KG path**

Add `workers` to `run_kg`, configure only the job pool from it, and replace the serial loop with
`submit_job` plus `as_completed`. Each submitted callable must set status, run extraction, set the
terminal status, and return success/error without letting one source cancel the others. Preserve
the existing result counts, log records, reporter progress, and source failure isolation.

Use this shape:

```python
def _extract_one(sid: str) -> tuple[str, Exception | None]:
    try:
        mnt.set_source_status(sid, "extracting")
        mnt.run_extraction(sid)
        mnt.set_source_status(sid, "extracted")
        return sid, None
    except Exception as exc:
        return sid, exc

futures = {submit_job(_extract_one, sid): sid for sid in targets}
for i, future in enumerate(as_completed(futures), 1):
    sid, error = future.result()
    if error is None:
        res["extracted"] += 1
        log({"phase": "kg", "source_id": sid, "status": "extracted"})
    else:
        res["failed"] += 1
        log({
            "phase": "kg",
            "source_id": sid,
            "status": "failed",
            "error": str(error),
        })
    reporter.done = i
```

- [ ] **Step 5: Make metadata source concurrency follow `--workers`**

In `SourceIngestionService.backfill_paper_metadata`, replace:

```python
workers = max(1, min(8, int(getattr(self.settings, "kg_extract_workers", 4))))
```

with:

```python
workers = max(
    1,
    min(
        int(getattr(self.settings, "kg_job_concurrency", 4)),
        len(targets),
    ),
)
```

Add this service test to `backend/tests/test_batch_ingest.py`:

```python
from types import SimpleNamespace


def test_paper_metadata_backfill_uses_source_job_concurrency(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb-meta-workers")
    service = repo._runtime.source_ingestion
    source_ids = [f"src-meta-{i}" for i in range(12)]
    repo.settings.kg_job_concurrency = 12
    repo.settings.kg_extract_workers = 3
    lock = threading.Lock()
    active = 0
    peak = 0

    monkeypatch.setattr(
        service.sources,
        "sources_missing_paper_meta",
        lambda notebook_id, include_existing=False: source_ids,
    )
    monkeypatch.setattr(
        service.sources,
        "get_source",
        lambda source_id: SimpleNamespace(
            id=source_id,
            notebook_id=nb_id,
            type="document",
        ),
    )
    monkeypatch.setattr(service, "_publish_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service, "_notify_paper_meta_done", lambda *args, **kwargs: None
    )

    def ensure(source, force=False):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
            return "stored"
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(service, "ensure_paper_metadata", ensure)
    counts = service.backfill_paper_metadata(nb_id)

    assert counts["total"] == 12
    assert counts["stored"] == 12
    assert peak == 12
```

The LLM hard-cap behavior is separately pinned by
`test_batch_gate_limits_primary_and_kg_clients_together`; this test isolates the metadata source
pool so a future fixed-eight ceiling cannot hide behind the model gate.

- [ ] **Step 6: Add the independent-peak integration test**

Add this deterministic reparse orchestration test. It uses the real KG job scheduler and active
model controller, while replacing storage/parsing with a protocol-sized fake so only concurrency
semantics affect the result:

```python
from types import SimpleNamespace

from app.services.model_concurrency import (
    LimitedJsonChatClient,
    activate_model_concurrency,
    current_model_concurrency,
)
from app.services.kg import scheduler as kg_scheduler


class _PeakRecorder:
    configured = True
    model = "peak-recorder"

    def __init__(self, delay=0.05):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def _call(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.active -= 1

    def chat_json(self, messages, schema="", **kwargs):
        self._call()
        return "{}"

    def embed(self):
        self._call()
        return [0.0]


class _FakeMaintenance:
    def __init__(self, source_ids):
        self._source_ids = source_ids

    def sources_with_elements(self, notebook_id):
        return set()

    def source_ids(self, notebook_id):
        return list(self._source_ids)


class _ReparseConcurrencyRepo:
    def __init__(self, llm, embed):
        self.settings = SimpleNamespace(
            kg_auto_extract=False,
            kg_incremental_fusion_enabled=True,
            kg_job_concurrency=8,
            kg_extract_workers=6,
            embed_concurrency=8,
        )
        self.maintenance = _FakeMaintenance(
            [f"src-peak-{i}" for i in range(12)]
        )
        self._llm = llm
        self._embed = embed

    def process_source(self, source_id):
        state = current_model_concurrency()
        assert state is not None
        LimitedJsonChatClient(self._llm, state.llm).chat_json([], "{}")
        state.embedding.run(
            self._embed.embed,
            task_prefix="emb-el",
        )
        return SimpleNamespace(id=source_id)


def test_reparse_llm_and_embedding_peaks_are_independent():
    llm = _PeakRecorder()
    embed = _PeakRecorder()
    repo = _ReparseConcurrencyRepo(llm, embed)

    try:
        kg_scheduler.configure(window_workers=6, job_workers=8)
        with activate_model_concurrency(llm_max=6, embed_max=2) as state:
            result = bi.run_reparse(
                repo,
                "nb-peak",
                conc=2,
                no_rebuild=True,
                report_interval=0,
            )
            llm_snapshot = state.llm.snapshot()
            embed_snapshot = state.embedding.snapshot()
    finally:
        kg_scheduler.reset()

    assert result["reparsed"] == 12
    assert result["failed"] == 0
    assert 4 <= llm.peak <= 6
    assert embed.peak == 2
    assert llm_snapshot.active == llm_snapshot.waiting == 0
    assert embed_snapshot.active == embed_snapshot.waiting == 0
```

- [ ] **Step 7: Update existing helper calls and remove obsolete multiplicative assertions**

Wrap direct phase-helper tests that depend on scheduler capacity in
`_batch_concurrency_scope`. Change tests that previously asserted `conc` configured both settings
and job pool so they assert:

- `_batch_concurrency_scope` configures both scheduler pools;
- `conc` configures only `repo.settings.embed_concurrency`;
- both values restore at their owning scope.

Delete obsolete phase-level scheduler spies. The controller restoration test from Task 4 is the
single authoritative wiring test.

- [ ] **Step 8: Run phase tests and commit**

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_batch_ingest.py \
  backend/tests/test_pipeline_concurrency.py \
  backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_embed_concurrency.py
```

Expected: all tests pass.

Commit:

```bash
git add backend/app/services/batch_ingest.py \
  backend/app/services/source_ingestion.py \
  backend/tests/test_batch_ingest.py
git commit -m "fix: decouple batch source and model concurrency"
```

---

### Task 6: Synchronize Operational Documentation

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the final CLI and reporting behavior from Tasks 4–5.
- Produces: one consistent English/Chinese/developer concurrency contract.

- [ ] **Step 1: Update the English README**

In the offline batch-ingest concurrency section:

- add `--llm-conc`;
- define the three independent controls;
- state CLI-over-env precedence and the three environment fallbacks;
- replace “peak embedding concurrency ≈ `--workers × --embed-conc`” with
  “embedding peak is hard-capped by `--embed-conc` across all sources”;
- include:

```bash
PYTHONPATH=backend python scripts/batch_ingest.py reparse \
  --notebook-id nb-xxxx \
  --workers 32 \
  --llm-conc 24 \
  --embed-conc 4 \
  --pool-report-interval 5
```

- update the sample reporter line to use LLM/embedding `active/max/waiting`.

- [ ] **Step 2: Mirror the same contract in the Chinese README**

Use the same parameter names, defaults, example values, phase coverage, and hard-cap semantics.
Do not retain wording that describes `--embed-conc` as per-document concurrency.

- [ ] **Step 3: Update AGENTS.md**

In the ingestion/concurrency architecture guidance, record:

```text
batch_ingest uses three independent controls: --workers for source jobs,
--llm-conc for a process-wide traditional-LLM hard cap, and --embed-conc
for a process-wide embedding hard cap. CLI values override
KG_JOB_CONCURRENCY, KG_EXTRACT_WORKERS, and EMBED_CONCURRENCY respectively;
omitted CLI values inherit them. Never reintroduce workers × embed-conc
per-source pool multiplication.
```

Also state that gate waits must not hold SQLite write transactions.

- [ ] **Step 4: Run documentation contract searches**

Run:

```bash
rg -n -S "workers ×.*embed|workers \\*.*embed|peak embedding concurrency|embedding 峰值并发" \
  README.md README_zh.md AGENTS.md
```

Expected: no old multiplicative contract remains; any match states the old behavior is prohibited.

Run:

```bash
rg -n -S -- "--llm-conc|--embed-conc|--workers" README.md README_zh.md AGENTS.md
```

Expected: all three files document all three controls consistently.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: document independent batch model limits"
```

---

### Task 7: Full Verification and Completion Audit

**Files:**
- Verify only; fix failures in the owning files from Tasks 1–6.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: evidence that repository and frontend completion gates pass.

- [ ] **Step 1: Run focused concurrency tests**

Run:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_model_concurrency.py \
  backend/tests/test_user_llm_client_resolve.py \
  backend/tests/test_embed_concurrency.py \
  backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_batch_ingest.py \
  backend/tests/test_pipeline_concurrency.py
```

Expected: all tests pass.

- [ ] **Step 2: Run repository checks**

Run:

```bash
scripts/check.sh
```

Expected: exit code 0 with every backend/static/smoke check green.

- [ ] **Step 3: Run the frontend completion gate**

Run:

```bash
cd frontend && npm run build
```

Expected: production build succeeds with exit code 0.

- [ ] **Step 4: Verify CLI help and resolved-value output**

Run:

```bash
PYTHONPATH=backend python scripts/batch_ingest.py --help
```

Expected: help lists `--workers`, `--llm-conc`, and `--embed-conc` with independent semantics and
environment fallbacks.

Run the automated CLI-output regression added in Task 4:

```bash
PYTHONPATH=backend pytest -q \
  backend/tests/test_batch_ingest.py::test_main_prints_effective_concurrency
```

Expected: PASS, proving that a reparse invocation with explicit `32/24/4` prints:

```text
concurrency: source=32(cli) llm=24(cli) embedding=4(cli)
```

- [ ] **Step 5: Audit spec coverage and working tree**

Run:

```bash
git status --short
git diff --check
```

Expected: no unintended files, no whitespace errors, and only deliberate uncommitted verification
fixes if a prior command exposed one.

Confirm:

- LLM peak can exceed embedding peak;
- LLM never exceeds `--llm-conc`;
- embedding never exceeds `--embed-conc`;
- source jobs use `--workers`;
- controller cleanup restores state;
- all three documentation files agree.

- [ ] **Step 6: Handle verification failures in the owning task**

If a verification command fails, return to the task that owns the failing file, add a focused
regression test there, make the smallest fix, rerun that task's focused command, and use that
task's explicit `git add` file list. Do not create an empty or catch-all verification commit.
