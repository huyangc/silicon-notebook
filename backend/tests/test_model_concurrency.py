import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

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


def test_activation_remains_registered_until_owned_executor_drains(monkeypatch):
    work_started = threading.Event()
    release_work = threading.Event()
    scope_ready = threading.Event()
    close_scope = threading.Event()
    shutdown_started = threading.Event()
    scope_finished = threading.Event()
    holder = {}

    original_shutdown = BoundedEmbeddingExecutor.shutdown

    def observed_shutdown(self):
        state = holder.get("state")
        if state is not None and self is state.embedding:
            shutdown_started.set()
        return original_shutdown(self)

    monkeypatch.setattr(BoundedEmbeddingExecutor, "shutdown", observed_shutdown)

    def blocking_work():
        work_started.set()
        release_work.wait(2)

    def run_scope():
        with activate_model_concurrency(llm_max=1, embed_max=1) as state:
            holder["state"] = state
            state.embedding.submit(blocking_work, task_prefix="drain")
            scope_ready.set()
            close_scope.wait(2)
        scope_finished.set()

    thread = threading.Thread(target=run_scope)
    thread.start()
    try:
        assert scope_ready.wait(2)
        assert work_started.wait(2)
        close_scope.set()
        assert shutdown_started.wait(2)
        assert current_model_concurrency() is holder["state"]
        with pytest.raises(RuntimeError, match="already active"):
            with activate_model_concurrency(llm_max=1, embed_max=1):
                pass
    finally:
        release_work.set()
        thread.join(2)

    assert scope_finished.is_set()
    assert current_model_concurrency() is None


class _ManualExecutor:
    def __init__(self):
        self.entries = []

    def submit(self, fn):
        future = Future()
        self.entries.append((future, fn))
        return future

    def run(self, future):
        for queued_future, fn in self.entries:
            if queued_future is future:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn())
                    except BaseException as exc:
                        future.set_exception(exc)
                return
        raise AssertionError("future was not submitted")

    def shutdown(self, **kwargs):
        pass


class _CountingSemaphore:
    def __init__(self, maximum):
        self.maximum = maximum
        self.available = maximum
        self.releases = 0

    def acquire(self):
        if self.available <= 0:
            raise AssertionError("admission permit leaked")
        self.available -= 1
        return True

    def release(self):
        self.available += 1
        self.releases += 1
        if self.available > self.maximum:
            raise AssertionError("admission permit released twice")


def test_cancelled_queued_embedding_futures_release_admission_once():
    executor = BoundedEmbeddingExecutor(1)
    manual = _ManualExecutor()
    semaphore = _CountingSemaphore(1)
    executor._executor = manual
    executor._admission = semaphore
    try:
        for _ in range(3):
            future = executor.submit(lambda: "cancelled", task_prefix="cancel")
            assert future.cancel()
            assert semaphore.available == 1

        future = executor.submit(lambda: "completed", task_prefix="cancel")
        manual.run(future)
        assert future.result() == "completed"
        assert semaphore.available == 1
        assert semaphore.releases == 4
    finally:
        executor.shutdown()


class _InterruptedSemaphore:
    def __init__(self):
        self.releases = 0

    def acquire(self):
        raise KeyboardInterrupt

    def release(self):
        self.releases += 1


def test_interrupted_admission_restores_waiting_without_releasing():
    executor = BoundedEmbeddingExecutor(1)
    semaphore = _InterruptedSemaphore()
    executor._admission = semaphore
    try:
        with pytest.raises(KeyboardInterrupt):
            executor.submit(lambda: None, task_prefix="interrupt")
        assert executor.snapshot().waiting == 0
        assert semaphore.releases == 0
    finally:
        executor.shutdown()


class _BlockingSemaphore:
    def __init__(self):
        self.acquire_started = threading.Event()
        self.allow_acquire = threading.Event()
        self.releases = 0

    def acquire(self):
        self.acquire_started.set()
        assert self.allow_acquire.wait(2)
        return True

    def release(self):
        self.releases += 1


def test_executor_snapshot_reports_live_waiting_admission():
    executor = BoundedEmbeddingExecutor(1)
    manual = _ManualExecutor()
    semaphore = _BlockingSemaphore()
    executor._executor = manual
    executor._admission = semaphore
    futures = []
    errors = []

    def submit():
        try:
            futures.append(executor.submit(lambda: None, task_prefix="waiting"))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert semaphore.acquire_started.wait(2)
        snapshot = executor.snapshot()
        assert snapshot.waiting == 1
        assert snapshot.active == 0
        semaphore.allow_acquire.set()
        thread.join(2)
        assert not thread.is_alive()
        assert not errors
        assert futures[0].cancel()
        assert semaphore.releases == 1
    finally:
        semaphore.allow_acquire.set()
        thread.join(2)
        executor.shutdown()


class _FailingExecutor:
    def submit(self, fn):
        raise RuntimeError("executor unavailable")

    def shutdown(self, **kwargs):
        pass


def test_executor_submit_failure_releases_admission_and_clears_waiting():
    executor = BoundedEmbeddingExecutor(1)
    semaphore = _CountingSemaphore(1)
    executor._executor = _FailingExecutor()
    executor._admission = semaphore
    try:
        with pytest.raises(RuntimeError, match="executor unavailable"):
            executor.submit(lambda: None, task_prefix="failure")
        assert executor.snapshot().active == 0
        assert executor.snapshot().waiting == 0
        assert semaphore.available == 1
        assert semaphore.releases == 1
    finally:
        executor.shutdown()
