from __future__ import annotations

from collections import Counter
from concurrent.futures import CancelledError, Future
from queue import Empty, Queue
import threading
import time

import pytest

from app.services import model_work as model_work_module
from app.services.model_scheduler import ServiceScheduler
from app.services.model_work import (
    ModelPriority,
    ModelQueueFull,
    ModelQueueTimeout,
    ModelServiceUnavailable,
    make_model_work_context,
    model_work_scope,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._conditions: set[threading.Condition] = set()
        self.waits: Queue[float | None] = Queue()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds
            conditions = tuple(self._conditions)
        for condition in conditions:
            with condition:
                condition.notify_all()

    def wait(
        self, condition: threading.Condition, timeout: float | None
    ) -> None:
        with self._lock:
            self._conditions.add(condition)
        self.waits.put(timeout)
        condition.wait(timeout)

    def wait_for_timeout(self, expected: float) -> None:
        while True:
            try:
                timeout = self.waits.get(timeout=2)
            except Empty as exc:  # pragma: no cover - assertion diagnostic
                raise AssertionError(f"dispatcher never waited for {expected}") from exc
            if timeout is not None and timeout == pytest.approx(expected):
                return


def work_context(
    *,
    actor: str,
    workload: str = "ask_answer",
    priority: ModelPriority = ModelPriority.INTERACTIVE,
    deadline_at: float | None = None,
    cancel_event: threading.Event | None = None,
) -> model_work_module.ModelWorkContext:
    return model_work_module.ModelWorkContext(
        actor_id=actor,
        workload_id=workload,
        priority=priority,
        parent_id="test",
        support_id=f"mdl-{actor}-{workload}",
        deadline_at=time.monotonic() + 30 if deadline_at is None else deadline_at,
        cancel_event=cancel_event,
    )


def blocking_call(
    started: threading.Event, release: threading.Event, result: object = None
):
    def invoke():
        started.set()
        assert release.wait(2)
        return result

    return invoke


def test_work_context_uses_request_actor_support_id_and_fixed_deadlines(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(model_work_module, "request_user_id", lambda: "user-7")

    contexts = {
        priority: make_model_work_context(
            workload_id=f"work-{priority.value}",
            priority=priority,
            parent_id="request-1",
            clock=clock,
        )
        for priority in ModelPriority
    }

    assert {context.actor_id for context in contexts.values()} == {"user-7"}
    assert all(context.support_id.startswith("mdl-") for context in contexts.values())
    assert len({context.support_id for context in contexts.values()}) == 3
    assert contexts[ModelPriority.INTERACTIVE].deadline_at == 1_030
    assert contexts[ModelPriority.REPORT].deadline_at == 1_300
    assert contexts[ModelPriority.BACKGROUND].deadline_at == 2_800


def test_work_scope_overrides_priority_and_parent_only(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(model_work_module, "request_user_id", lambda: None)

    with model_work_scope(priority=ModelPriority.REPORT, parent_id="report-9"):
        context = make_model_work_context(
            workload_id="retrieval_query_embedding",
            priority=ModelPriority.INTERACTIVE,
            parent_id="inner",
            clock=clock,
        )

    assert context.actor_id == "system"
    assert context.workload_id == "retrieval_query_embedding"
    assert context.priority is ModelPriority.REPORT
    assert context.parent_id == "report-9"
    assert context.deadline_at == 1_300


def test_same_service_workloads_share_one_peak():
    scheduler = ServiceScheduler("general", maximum=2)
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def invoke():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(2)
        finally:
            with lock:
                active -= 1

    try:
        futures = [
            scheduler.submit(
                context=work_context(actor=f"u{i}", workload=workload),
                invoke=invoke,
            )
            for i, workload in enumerate(
                ["ask_answer", "source_summary", "kg_extract"]
            )
        ]
        assert two_started.wait(2)
        assert scheduler.snapshot().active == 2
        assert peak == 2
        release.set()
        assert [future.result(timeout=2) for future in futures] == [None] * 3
    finally:
        release.set()
        scheduler.shutdown()


def test_different_services_have_independent_slots():
    first = ServiceScheduler("general", maximum=1)
    second = ServiceScheduler("reasoning", maximum=1)
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    try:
        first_future = first.submit(
            context=work_context(actor="u1"),
            invoke=blocking_call(first_started, release, "first"),
        )
        second_future = second.submit(
            context=work_context(actor="u2", workload="reasoning_agent"),
            invoke=blocking_call(second_started, release, "second"),
        )

        assert first_started.wait(2)
        assert second_started.wait(2)
        assert first.snapshot().active == second.snapshot().active == 1
        release.set()
        assert first_future.result(timeout=2) == "first"
        assert second_future.result(timeout=2) == "second"
    finally:
        release.set()
        first.shutdown()
        second.shutdown()


def test_total_queue_is_bounded_at_ten_times_maximum():
    scheduler = ServiceScheduler("general", maximum=1)
    started = threading.Event()
    release = threading.Event()
    try:
        active = scheduler.submit(
            context=work_context(actor="active"),
            invoke=blocking_call(started, release),
        )
        assert started.wait(2)
        queued = [
            scheduler.submit(
                context=work_context(actor=f"actor-{number}"),
                invoke=lambda: None,
            )
            for number in range(10)
        ]

        rejected = scheduler.submit(
            context=work_context(actor="overflow"), invoke=lambda: None
        )

        assert scheduler.snapshot().queued == 10
        with pytest.raises(ModelQueueFull):
            rejected.result(timeout=0)
        release.set()
        active.result(timeout=2)
        assert [future.result(timeout=2) for future in queued] == [None] * 10
    finally:
        release.set()
        scheduler.shutdown()


def test_actor_queue_is_bounded_at_twice_maximum_across_lanes():
    scheduler = ServiceScheduler("general", maximum=1)
    started = threading.Event()
    release = threading.Event()
    try:
        active = scheduler.submit(
            context=work_context(actor="active"),
            invoke=blocking_call(started, release),
        )
        assert started.wait(2)
        first = scheduler.submit(
            context=work_context(actor="same", priority=ModelPriority.INTERACTIVE),
            invoke=lambda: "first",
        )
        second = scheduler.submit(
            context=work_context(actor="same", priority=ModelPriority.BACKGROUND),
            invoke=lambda: "second",
        )

        rejected = scheduler.submit(
            context=work_context(actor="same", priority=ModelPriority.REPORT),
            invoke=lambda: "third",
        )

        with pytest.raises(ModelQueueFull):
            rejected.result(timeout=0)
        assert scheduler.snapshot().queued == 2
        release.set()
        active.result(timeout=2)
        assert {first.result(timeout=2), second.result(timeout=2)} == {
            "first",
            "second",
        }
    finally:
        release.set()
        scheduler.shutdown()


def test_dispatch_pattern_is_eight_interactive_two_report_one_background():
    scheduler = ServiceScheduler("weighted", maximum=1)
    seed_started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    lock = threading.Lock()

    def record(label: str):
        def invoke():
            with lock:
                order.append(label)

        return invoke

    try:
        seed = scheduler.submit(
            context=work_context(actor="seed", priority=ModelPriority.INTERACTIVE),
            invoke=blocking_call(seed_started, release),
        )
        assert seed_started.wait(2)
        futures = []
        for number in range(7):
            futures.append(
                scheduler.submit(
                    context=work_context(
                        actor=f"interactive-{number}",
                        priority=ModelPriority.INTERACTIVE,
                    ),
                    invoke=record("interactive"),
                )
            )
        for number in range(2):
            futures.append(
                scheduler.submit(
                    context=work_context(
                        actor=f"report-{number}", priority=ModelPriority.REPORT
                    ),
                    invoke=record("report"),
                )
            )
        futures.append(
            scheduler.submit(
                context=work_context(
                    actor="background", priority=ModelPriority.BACKGROUND
                ),
                invoke=record("background"),
            )
        )

        release.set()
        seed.result(timeout=2)
        for future in futures:
            future.result(timeout=2)

        assert order == ["interactive"] * 7 + ["report"] * 2 + ["background"]
    finally:
        release.set()
        scheduler.shutdown()


def test_actors_round_robin_within_one_priority_lane():
    scheduler = ServiceScheduler("round-robin", maximum=1)
    seed_started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    lock = threading.Lock()

    def record(label: str):
        def invoke():
            with lock:
                order.append(label)

        return invoke

    try:
        seed = scheduler.submit(
            context=work_context(actor="seed"),
            invoke=blocking_call(seed_started, release),
        )
        assert seed_started.wait(2)
        futures = [
            scheduler.submit(context=work_context(actor="a"), invoke=record("a1")),
            scheduler.submit(context=work_context(actor="a"), invoke=record("a2")),
            scheduler.submit(context=work_context(actor="b"), invoke=record("b1")),
            scheduler.submit(context=work_context(actor="b"), invoke=record("b2")),
            scheduler.submit(context=work_context(actor="c"), invoke=record("c1")),
        ]

        release.set()
        seed.result(timeout=2)
        for future in futures:
            future.result(timeout=2)

        assert order == ["a1", "b1", "c1", "a2", "b2"]
    finally:
        release.set()
        scheduler.shutdown()


def test_expired_queued_work_never_calls_provider():
    clock = FakeClock()
    scheduler = ServiceScheduler("deadline", maximum=1, clock=clock)
    seed_started = threading.Event()
    release = threading.Event()
    invoked = threading.Event()
    try:
        seed = scheduler.submit(
            context=work_context(actor="seed", deadline_at=clock() + 100),
            invoke=blocking_call(seed_started, release),
        )
        assert seed_started.wait(2)
        expired = scheduler.submit(
            context=work_context(actor="late", deadline_at=clock() + 5),
            invoke=lambda: invoked.set(),
        )

        clock.advance(5)
        assert scheduler.snapshot().queued == 0

        with pytest.raises(ModelQueueTimeout):
            expired.result(timeout=0)
        assert not invoked.is_set()
        release.set()
        seed.result(timeout=2)
    finally:
        release.set()
        scheduler.shutdown()


def test_queued_deadline_completes_without_unrelated_scheduler_activity():
    clock = FakeClock()
    scheduler = ServiceScheduler(
        "deadline-wakeup", maximum=1, clock=clock, wait=clock.wait
    )
    seed_started = threading.Event()
    release = threading.Event()
    invoked = threading.Event()
    try:
        seed = scheduler.submit(
            context=work_context(actor="seed", deadline_at=clock() + 100),
            invoke=blocking_call(seed_started, release),
        )
        assert seed_started.wait(2)
        expired = scheduler.submit(
            context=work_context(actor="late", deadline_at=clock() + 5),
            invoke=lambda: invoked.set(),
        )
        clock.wait_for_timeout(5)

        clock.advance(5)

        with pytest.raises(ModelQueueTimeout):
            expired.result(timeout=2)
        assert not invoked.is_set()
        release.set()
        seed.result(timeout=2)
    finally:
        release.set()
        scheduler.shutdown()


def test_pre_start_cancellation_is_a_lazy_tombstone_without_a_slot():
    scheduler = ServiceScheduler("cancel-before", maximum=1)
    seed_started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    invoked = threading.Event()
    try:
        seed = scheduler.submit(
            context=work_context(actor="seed"),
            invoke=blocking_call(seed_started, release),
        )
        assert seed_started.wait(2)
        future = scheduler.submit(
            context=work_context(actor="cancelled", cancel_event=cancelled),
            invoke=lambda: invoked.set(),
        )

        cancelled.set()
        assert scheduler.snapshot().queued == 0

        assert future.cancelled()
        assert not invoked.is_set()
        release.set()
        seed.result(timeout=2)
    finally:
        release.set()
        scheduler.shutdown()


def test_queued_cancellation_is_polled_without_unrelated_scheduler_activity():
    clock = FakeClock()
    scheduler = ServiceScheduler(
        "cancel-wakeup", maximum=1, clock=clock, wait=clock.wait
    )
    seed_started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    invoked = threading.Event()
    try:
        seed = scheduler.submit(
            context=work_context(actor="seed", deadline_at=clock() + 100),
            invoke=blocking_call(seed_started, release),
        )
        assert seed_started.wait(2)
        future = scheduler.submit(
            context=work_context(
                actor="cancelled",
                deadline_at=clock() + 100,
                cancel_event=cancelled,
            ),
            invoke=lambda: invoked.set(),
        )
        clock.wait_for_timeout(0.1)

        cancelled.set()
        clock.advance(0.1)

        with pytest.raises(CancelledError):
            future.result(timeout=2)
        assert future.cancelled()
        assert not invoked.is_set()
        release.set()
        seed.result(timeout=2)
    finally:
        release.set()
        scheduler.shutdown()


def test_in_flight_cancellation_reaches_provider_signal():
    scheduler = ServiceScheduler("cancel-running", maximum=1)
    cancel_event = threading.Event()
    started = threading.Event()

    def invoke():
        started.set()
        assert cancel_event.wait(2)
        raise CancelledError()

    try:
        future = scheduler.submit(
            context=work_context(actor="u1", cancel_event=cancel_event),
            invoke=invoke,
        )
        assert started.wait(2)
        assert future.cancel() is False
        cancel_event.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
        assert scheduler.snapshot().active == 0
        assert scheduler.snapshot().breaker_state == "closed"
    finally:
        cancel_event.set()
        scheduler.shutdown()


def test_breaker_open_drains_queue_fails_fast_and_recovers_half_open():
    clock = FakeClock()
    scheduler = ServiceScheduler("breaker", maximum=1, clock=clock)
    try:
        for number in range(2):
            failed = scheduler.submit(
                context=work_context(
                    actor=f"failed-{number}", deadline_at=clock() + 100
                ),
                invoke=lambda: (_ for _ in ()).throw(ConnectionError("down")),
            )
            with pytest.raises(ConnectionError):
                failed.result(timeout=2)

        third_started = threading.Event()
        release_third = threading.Event()

        def third_failure():
            third_started.set()
            assert release_third.wait(2)
            raise ConnectionError("still down")

        third = scheduler.submit(
            context=work_context(actor="third", deadline_at=clock() + 100),
            invoke=third_failure,
        )
        assert third_started.wait(2)
        queued = scheduler.submit(
            context=work_context(actor="queued", deadline_at=clock() + 100),
            invoke=lambda: "must not run",
        )

        release_third.set()
        with pytest.raises(ConnectionError):
            third.result(timeout=2)
        with pytest.raises(ModelServiceUnavailable):
            queued.result(timeout=2)
        assert scheduler.snapshot().breaker_state == "open"

        rejected = scheduler.submit(
            context=work_context(actor="rejected", deadline_at=clock() + 100),
            invoke=lambda: "must not run",
        )
        with pytest.raises(ModelServiceUnavailable):
            rejected.result(timeout=0)

        clock.advance(30)
        recovered = scheduler.submit(
            context=work_context(actor="probe", deadline_at=clock() + 100),
            invoke=lambda: "recovered",
        )
        assert recovered.result(timeout=2) == "recovered"
        assert scheduler.snapshot().breaker_state == "closed"
    finally:
        scheduler.shutdown()


def test_retry_and_backoff_stay_inside_one_slot():
    scheduler = ServiceScheduler("retry", maximum=1)
    first_attempt = threading.Event()
    in_backoff = threading.Event()
    finish_backoff = threading.Event()
    second_started = threading.Event()
    attempts = 0

    def retrying_invoke():
        nonlocal attempts
        attempts += 1
        first_attempt.set()
        in_backoff.set()
        assert finish_backoff.wait(2)
        attempts += 1
        return "retried"

    try:
        first = scheduler.submit(
            context=work_context(actor="first"), invoke=retrying_invoke
        )
        assert first_attempt.wait(2)
        second = scheduler.submit(
            context=work_context(actor="second"),
            invoke=lambda: second_started.set() or "second",
        )
        assert in_backoff.is_set()
        assert scheduler.snapshot().active == 1
        assert scheduler.snapshot().queued == 1
        assert not second_started.is_set()

        finish_backoff.set()
        assert first.result(timeout=2) == "retried"
        assert attempts == 2
        assert second.result(timeout=2) == "second"
        assert second_started.is_set()
    finally:
        finish_backoff.set()
        scheduler.shutdown()


def test_snapshot_reports_oldest_wait_and_busy_without_sleep():
    clock = FakeClock()
    scheduler = ServiceScheduler("snapshot", maximum=1, clock=clock)
    started = threading.Event()
    release = threading.Event()
    try:
        active = scheduler.submit(
            context=work_context(actor="active", deadline_at=clock() + 100),
            invoke=blocking_call(started, release),
        )
        assert started.wait(2)
        queued = scheduler.submit(
            context=work_context(actor="queued", deadline_at=clock() + 100),
            invoke=lambda: None,
        )
        clock.advance(2.25)

        snapshot = scheduler.snapshot()

        assert snapshot.active == 1
        assert snapshot.maximum == 1
        assert snapshot.queued == 1
        assert snapshot.oldest_wait_ms == 2_250
        assert snapshot.busy is True
        release.set()
        active.result(timeout=2)
        queued.result(timeout=2)
    finally:
        release.set()
        scheduler.shutdown()


def test_submit_shutdown_race_completes_every_future_exactly_once():
    scheduler = ServiceScheduler("shutdown-race", maximum=2)
    contender_count = 16
    start = threading.Barrier(contender_count + 2)
    futures: list[Future] = []
    futures_lock = threading.Lock()
    submitters: list[threading.Thread] = []

    def submit(number: int) -> None:
        start.wait()
        future = scheduler.submit(
            context=work_context(actor=f"u-{number}"), invoke=lambda: number
        )
        with futures_lock:
            futures.append(future)

    for number in range(contender_count):
        thread = threading.Thread(target=submit, args=(number,))
        thread.start()
        submitters.append(thread)

    shutdown_done = threading.Event()

    def shutdown() -> None:
        start.wait()
        scheduler.shutdown(wait=True)
        shutdown_done.set()

    shutdown_thread = threading.Thread(target=shutdown)
    shutdown_thread.start()
    start.wait()

    for thread in submitters:
        thread.join(2)
        assert not thread.is_alive()
    shutdown_thread.join(2)
    assert shutdown_done.is_set()

    callback_counts: Counter[int] = Counter()
    callback_lock = threading.Lock()

    def completed_once(future: Future) -> None:
        with callback_lock:
            callback_counts[id(future)] += 1

    assert len(futures) == contender_count
    for future in futures:
        future.add_done_callback(completed_once)
        assert future.done()
        if not future.cancelled():
            try:
                future.result(timeout=0)
            except ModelServiceUnavailable:
                pass
    assert callback_counts == Counter({id(future): 1 for future in futures})


def test_running_completion_and_shutdown_give_each_future_one_terminal_outcome():
    scheduler = ServiceScheduler("shutdown-completion", maximum=1)
    active_started = threading.Event()
    release_active = threading.Event()
    active_done = threading.Event()
    queued_done = threading.Event()
    callback_counts: Counter[str] = Counter()
    callback_lock = threading.Lock()

    def count(label: str, done: threading.Event):
        def callback(_future: Future) -> None:
            with callback_lock:
                callback_counts[label] += 1
            done.set()

        return callback

    active = scheduler.submit(
        context=work_context(actor="active"),
        invoke=blocking_call(active_started, release_active, "active-result"),
    )
    assert active_started.wait(2)
    queued = scheduler.submit(
        context=work_context(actor="queued"), invoke=lambda: "must-not-run"
    )
    active.add_done_callback(count("active", active_done))
    queued.add_done_callback(count("queued", queued_done))

    shutdown_done = threading.Event()

    def shutdown() -> None:
        scheduler.shutdown(wait=True)
        shutdown_done.set()

    shutdown_thread = threading.Thread(target=shutdown)
    shutdown_thread.start()
    assert queued_done.wait(2)
    assert queued.cancelled()
    assert not shutdown_done.is_set()

    release_active.set()
    assert active_done.wait(2)
    shutdown_thread.join(2)

    assert active.result(timeout=0) == "active-result"
    assert shutdown_done.is_set()
    assert callback_counts == Counter({"active": 1, "queued": 1})


def test_shutdown_reaps_dispatcher_and_executor_threads():
    service_id = "thread-cleanup"
    scheduler = ServiceScheduler(service_id, maximum=2)
    futures = [
        scheduler.submit(context=work_context(actor=f"u-{i}"), invoke=lambda: None)
        for i in range(4)
    ]
    for future in futures:
        future.result(timeout=2)

    scheduler.shutdown()

    leaked = [
        thread.name
        for thread in threading.enumerate()
        if service_id in thread.name and thread.is_alive()
    ]
    assert leaked == []
