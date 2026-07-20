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
