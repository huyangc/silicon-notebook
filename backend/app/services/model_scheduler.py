from __future__ import annotations

import time
import threading
from collections import OrderedDict, deque
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from app.services.model_circuit_breaker import BreakerPermit, ServiceCircuitBreaker
from app.services.model_work import (
    ModelPriority,
    ModelQueueFull,
    ModelQueueTimeout,
    ModelServiceUnavailable,
    ModelWorkContext,
    SchedulerSnapshot,
)


T = TypeVar("T")


_DISPATCH_PATTERN = (
    *(ModelPriority.INTERACTIVE for _ in range(8)),
    *(ModelPriority.REPORT for _ in range(2)),
    ModelPriority.BACKGROUND,
)


@dataclass
class _QueuedWork(Generic[T]):
    context: ModelWorkContext
    invoke: Callable[[], T]
    future: Future[T]
    enqueued_at: float
    permit: BreakerPermit


class ServiceScheduler:
    _CANCELLATION_POLL_SECONDS = 0.1

    def __init__(
        self,
        service_id: str,
        *,
        maximum: int,
        clock: Callable[[], float] | None = None,
        breaker: ServiceCircuitBreaker | None = None,
        wait: Callable[[threading.Condition, float | None], None] | None = None,
    ) -> None:
        if int(maximum) <= 0:
            raise ValueError("model service concurrency must be positive")
        self.service_id = str(service_id)
        self.maximum = int(maximum)
        self._clock = clock or time.monotonic
        self._wait = wait or (
            lambda condition, timeout: condition.wait(timeout)
        )
        self._breaker = breaker or ServiceCircuitBreaker(clock=self._clock)
        self._queue_limit = 10 * self.maximum
        self._actor_limit = 2 * self.maximum
        self._condition = threading.Condition(threading.RLock())
        self._lanes: dict[
            ModelPriority, OrderedDict[str, deque[_QueuedWork]]
        ] = {priority: OrderedDict() for priority in ModelPriority}
        self._actor_queued: dict[str, int] = {}
        self._queued = 0
        self._active = 0
        self._dispatch_index = 0
        self._shutdown = False
        self._shutdown_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.maximum,
            thread_name_prefix=f"model-{self.service_id}",
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name=f"model-dispatch-{self.service_id}",
            daemon=True,
        )
        self._dispatcher.start()

    def submit(
        self,
        *,
        context: ModelWorkContext,
        invoke: Callable[[], T],
        allow_half_open: bool = False,
    ) -> Future[T]:
        future: Future[T] = Future()
        stale: list[tuple[_QueuedWork, str]] = []
        error: BaseException | None = None
        cancel_new = False
        with self._condition:
            stale = self._collect_stale_locked(self._clock())
            if self._shutdown:
                error = ModelServiceUnavailable(support_id=context.support_id)
            elif context.cancel_event is not None and context.cancel_event.is_set():
                cancel_new = True
            elif self._clock() >= context.deadline_at:
                error = ModelQueueTimeout(support_id=context.support_id)
            elif (
                self._queued >= self._queue_limit
                or self._actor_queued.get(context.actor_id, 0) >= self._actor_limit
            ):
                error = ModelQueueFull(support_id=context.support_id)
            else:
                try:
                    permit = self._breaker.admit(
                        allow_half_open=allow_half_open
                    )
                except ModelServiceUnavailable:
                    error = ModelServiceUnavailable(support_id=context.support_id)
                else:
                    item = _QueuedWork(
                        context=context,
                        invoke=invoke,
                        future=future,
                        enqueued_at=self._clock(),
                        permit=permit,
                    )
                    lane = self._lanes[context.priority]
                    actor_queue = lane.get(context.actor_id)
                    if actor_queue is None:
                        actor_queue = deque()
                        lane[context.actor_id] = actor_queue
                    actor_queue.append(item)
                    self._queued += 1
                    self._actor_queued[context.actor_id] = (
                        self._actor_queued.get(context.actor_id, 0) + 1
                    )
                    future.add_done_callback(self._wake_if_cancelled)
                    self._condition.notify()
        self._settle_stale(stale)
        if cancel_new:
            future.cancel()
        elif error is not None:
            future.set_exception(error)
        return future

    def snapshot(self) -> SchedulerSnapshot:
        with self._condition:
            stale = self._collect_stale_locked(self._clock())
            oldest = self._oldest_wait_ms_locked()
            active = self._active
            queued = self._queued
            busy = active >= self.maximum or queued >= self._queue_limit
        self._settle_stale(stale)
        return SchedulerSnapshot(
            active=active,
            maximum=self.maximum,
            queued=queued,
            oldest_wait_ms=oldest,
            breaker_state=self._breaker.state,
            busy=busy,
        )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._shutdown_lock:
            with self._condition:
                self._shutdown = True
                queued = self._take_all_queued_locked()
                self._condition.notify_all()
            for item in queued:
                self._breaker.abandon(item.permit)
                item.future.cancel()
            self._dispatcher.join()
            self._executor.shutdown(wait=wait, cancel_futures=False)

    def _wake_if_cancelled(self, future: Future) -> None:
        if not future.cancelled():
            return
        with self._condition:
            self._condition.notify()

    def _dispatch_loop(self) -> None:
        while True:
            stale: list[tuple[_QueuedWork, str]] = []
            item: _QueuedWork | None = None
            with self._condition:
                stale = self._collect_stale_locked(self._clock())
                if not stale:
                    if self._shutdown:
                        return
                    if self._active < self.maximum and self._queued:
                        item = self._take_next_locked()
                        self._active += 1
                    else:
                        self._wait(
                            self._condition, self._next_wait_timeout_locked()
                        )
                        continue
            if stale:
                self._settle_stale(stale)
                continue
            assert item is not None
            if not item.future.set_running_or_notify_cancel():
                self._breaker.abandon(item.permit)
                self._finish_active()
                continue
            try:
                self._executor.submit(self._execute, item)
            except BaseException:
                self._breaker.abandon(item.permit)
                self._set_exception_once(
                    item.future,
                    ModelServiceUnavailable(support_id=item.context.support_id),
                )
                self._finish_active()

    def _execute(self, item: _QueuedWork) -> None:
        try:
            if self._clock() >= item.context.deadline_at:
                raise ModelQueueTimeout(support_id=item.context.support_id)
            if (
                item.context.cancel_event is not None
                and item.context.cancel_event.is_set()
            ):
                raise CancelledError()
            result = item.invoke()
        except BaseException as error:
            transition = self._breaker.record_failure(item.permit, error)
            self._set_exception_once(item.future, error)
            if transition.opened:
                self._drain_unavailable()
        else:
            self._breaker.record_success(item.permit)
            self._set_result_once(item.future, result)
        finally:
            self._finish_active()

    def _finish_active(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def _drain_unavailable(self) -> None:
        with self._condition:
            queued = self._take_all_queued_locked()
            self._condition.notify_all()
        for item in queued:
            self._breaker.abandon(item.permit)
            self._set_exception_once(
                item.future,
                ModelServiceUnavailable(support_id=item.context.support_id),
            )

    def _take_all_queued_locked(self) -> list[_QueuedWork]:
        queued: list[_QueuedWork] = []
        for lane in self._lanes.values():
            for actor_queue in lane.values():
                queued.extend(actor_queue)
            lane.clear()
        self._queued = 0
        self._actor_queued.clear()
        return queued

    def _take_next_locked(self) -> _QueuedWork:
        for _ in range(len(_DISPATCH_PATTERN)):
            priority = _DISPATCH_PATTERN[self._dispatch_index]
            self._dispatch_index = (self._dispatch_index + 1) % len(
                _DISPATCH_PATTERN
            )
            lane = self._lanes[priority]
            if not lane:
                continue
            actor_id, actor_queue = lane.popitem(last=False)
            item = actor_queue.popleft()
            if actor_queue:
                lane[actor_id] = actor_queue
            self._decrement_queued_locked(actor_id)
            return item
        raise AssertionError("queued model work was not present in any lane")

    def _collect_stale_locked(
        self, now: float
    ) -> list[tuple[_QueuedWork, str]]:
        stale: list[tuple[_QueuedWork, str]] = []
        for lane in self._lanes.values():
            for actor_id, actor_queue in list(lane.items()):
                kept: deque[_QueuedWork] = deque()
                for item in actor_queue:
                    cancelled = item.future.cancelled() or (
                        item.context.cancel_event is not None
                        and item.context.cancel_event.is_set()
                    )
                    if cancelled:
                        stale.append((item, "cancel"))
                        self._decrement_queued_locked(actor_id)
                    elif now >= item.context.deadline_at:
                        stale.append((item, "timeout"))
                        self._decrement_queued_locked(actor_id)
                    else:
                        kept.append(item)
                if kept:
                    lane[actor_id] = kept
                else:
                    lane.pop(actor_id, None)
        return stale

    def _settle_stale(self, stale: list[tuple[_QueuedWork, str]]) -> None:
        for item, reason in stale:
            self._breaker.abandon(item.permit)
            if reason == "cancel":
                item.future.cancel()
            else:
                self._set_exception_once(
                    item.future,
                    ModelQueueTimeout(support_id=item.context.support_id),
                )

    def _decrement_queued_locked(self, actor_id: str) -> None:
        self._queued -= 1
        remaining = self._actor_queued[actor_id] - 1
        if remaining:
            self._actor_queued[actor_id] = remaining
        else:
            del self._actor_queued[actor_id]

    def _oldest_wait_ms_locked(self) -> int:
        oldest: float | None = None
        for lane in self._lanes.values():
            for actor_queue in lane.values():
                for item in actor_queue:
                    if oldest is None or item.enqueued_at < oldest:
                        oldest = item.enqueued_at
        if oldest is None:
            return 0
        return max(0, int((self._clock() - oldest) * 1_000))

    def _next_wait_timeout_locked(self) -> float | None:
        if not self._queued:
            return None
        now = self._clock()
        earliest_deadline: float | None = None
        polls_cancellation = False
        for lane in self._lanes.values():
            for actor_queue in lane.values():
                for item in actor_queue:
                    if (
                        earliest_deadline is None
                        or item.context.deadline_at < earliest_deadline
                    ):
                        earliest_deadline = item.context.deadline_at
                    polls_cancellation = polls_cancellation or (
                        item.context.cancel_event is not None
                    )
        assert earliest_deadline is not None
        timeout = max(0.0, earliest_deadline - now)
        if polls_cancellation:
            timeout = min(timeout, self._CANCELLATION_POLL_SECONDS)
        return timeout

    @staticmethod
    def _set_result_once(future: Future, result: object) -> None:
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _set_exception_once(future: Future, error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)
