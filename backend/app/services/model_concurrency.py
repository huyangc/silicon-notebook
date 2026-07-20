from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
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


class _AdmissionPermit:
    def __init__(self, semaphore: Any) -> None:
        self._semaphore = semaphore
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._semaphore.release()


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
        acquired = False
        try:
            acquired = bool(self._admission.acquire())
        finally:
            with self._lock:
                self._waiting -= 1
        if not acquired:
            raise RuntimeError("embedding admission was not acquired")

        permit = _AdmissionPermit(self._admission)

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
                permit.release()

        try:
            future = self._executor.submit(invoke)
        except BaseException:
            permit.release()
            raise
        future.add_done_callback(
            lambda completed: permit.release() if completed.cancelled() else None
        )
        return future

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


@dataclass(frozen=True)
class ModelConcurrencyState:
    llm: ConcurrencyGate
    embedding: BoundedEmbeddingExecutor


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
        state.embedding.shutdown()
        with _state_lock:
            if _active_state is state:
                _active_state = None


class LimitedJsonChatClient:
    def __init__(self, delegate: Any, gate: ConcurrencyGate) -> None:
        self._delegate = delegate
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def chat_json(self, *args: Any, **kwargs: Any) -> Any:
        with self._gate.slot():
            return self._delegate.chat_json(*args, **kwargs)
