from __future__ import annotations

from concurrent.futures import CancelledError
import threading

import pytest

from app.services.model_circuit_breaker import (
    FailureKind,
    ServiceCircuitBreaker,
    classify_provider_failure,
)
from app.services.model_work import (
    MalformedModelResponse,
    ModelProviderError,
    ModelQueueFull,
    ModelQueueTimeout,
    ModelServiceUnavailable,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StatusError(Exception):
    def __init__(self, status_code: int, *, code: str = "") -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.code = code


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("connection failed"),
        TimeoutError("provider timed out"),
        StatusError(429),
        StatusError(500),
        StatusError(503),
        MalformedModelResponse("empty success payload"),
    ],
)
def test_transient_provider_failures_are_classified(error: Exception):
    assert classify_provider_failure(error) is FailureKind.TRANSIENT


@pytest.mark.parametrize(
    "error",
    [
        StatusError(401),
        StatusError(403),
        ModelProviderError("unknown model", code="unknown_model"),
        ModelProviderError("unsupported endpoint", code="protocol_mismatch"),
        ModelProviderError("unsupported capability", code="capability_mismatch"),
    ],
)
def test_fatal_provider_failures_are_classified(error: Exception):
    assert classify_provider_failure(error) is FailureKind.FATAL


@pytest.mark.parametrize(
    "error",
    [
        ModelQueueFull(support_id="mdl-full"),
        ModelQueueTimeout(support_id="mdl-timeout"),
        ModelServiceUnavailable(support_id="mdl-unavailable"),
        CancelledError(),
        ValueError("local validation"),
        RuntimeError("local persistence"),
    ],
)
def test_local_queue_and_cancellation_failures_do_not_affect_breaker(
    error: Exception,
):
    assert classify_provider_failure(error) is FailureKind.IGNORED


def test_three_consecutive_transient_failures_open_and_success_resets_counter():
    clock = FakeClock()
    breaker = ServiceCircuitBreaker(clock=clock)

    first = breaker.admit()
    assert breaker.record_failure(first, ConnectionError("one")).opened is False
    second = breaker.admit()
    assert breaker.record_failure(second, TimeoutError("two")).opened is False

    successful = breaker.admit()
    breaker.record_success(successful)

    for number in range(2):
        permit = breaker.admit()
        transition = breaker.record_failure(
            permit, ConnectionError(f"after reset {number}")
        )
        assert transition.opened is False
    assert breaker.state == "closed"

    third = breaker.admit()
    assert breaker.record_failure(third, StatusError(503)).opened is True
    assert breaker.state == "open"


def test_fatal_failure_opens_immediately():
    breaker = ServiceCircuitBreaker(clock=FakeClock())
    permit = breaker.admit()

    transition = breaker.record_failure(permit, StatusError(401))

    assert transition.opened is True
    assert transition.failure_kind is FailureKind.FATAL
    assert breaker.state == "open"


def test_malformed_success_payload_uses_transient_threshold():
    breaker = ServiceCircuitBreaker(clock=FakeClock())

    for expected_open in (False, False, True):
        permit = breaker.admit()
        transition = breaker.record_failure(
            permit, MalformedModelResponse("empty payload")
        )
        assert transition.opened is expected_open


def _open_with_transient_failures(breaker: ServiceCircuitBreaker) -> None:
    for _ in range(3):
        permit = breaker.admit()
        breaker.record_failure(permit, ConnectionError("unavailable"))


def test_cooldown_admits_exactly_one_half_open_probe_and_failure_reopens():
    clock = FakeClock()
    breaker = ServiceCircuitBreaker(clock=clock)
    _open_with_transient_failures(breaker)

    clock.advance(29.999)
    with pytest.raises(ModelServiceUnavailable):
        breaker.admit()

    clock.advance(0.001)
    probe = breaker.admit()
    assert breaker.state == "half_open"
    with pytest.raises(ModelServiceUnavailable):
        breaker.admit()

    transition = breaker.record_failure(probe, TimeoutError("probe failed"))
    assert transition.opened is True
    assert breaker.state == "open"

    with pytest.raises(ModelServiceUnavailable):
        breaker.admit()
    clock.advance(30)
    next_probe = breaker.admit()
    breaker.record_success(next_probe)
    assert breaker.state == "closed"


def test_manual_probe_cannot_bypass_half_open_cooldown():
    clock = FakeClock()
    breaker = ServiceCircuitBreaker(clock=clock)
    _open_with_transient_failures(breaker)

    with pytest.raises(ModelServiceUnavailable):
        breaker.admit(allow_half_open=True)
    assert breaker.state == "open"

    clock.advance(30)
    probe = breaker.admit()
    breaker.record_success(probe)
    assert breaker.state == "closed"


def test_half_open_permit_is_single_flight_under_concurrent_admission():
    clock = FakeClock()
    breaker = ServiceCircuitBreaker(clock=clock)
    _open_with_transient_failures(breaker)
    clock.advance(30)
    contenders = 12
    start = threading.Barrier(contenders + 1)
    lock = threading.Lock()
    permits = []
    unavailable = 0

    def contend() -> None:
        nonlocal unavailable
        start.wait()
        try:
            permit = breaker.admit()
        except ModelServiceUnavailable:
            with lock:
                unavailable += 1
        else:
            with lock:
                permits.append(permit)

    threads = [threading.Thread(target=contend) for _ in range(contenders)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    assert len(permits) == 1
    assert unavailable == contenders - 1
    breaker.record_success(permits[0])
    assert breaker.state == "closed"


def test_stale_closed_permit_cannot_heal_a_newly_opened_breaker():
    breaker = ServiceCircuitBreaker(clock=FakeClock())
    stale_success = breaker.admit()
    permits = [breaker.admit() for _ in range(3)]
    for permit in permits:
        breaker.record_failure(permit, ConnectionError("down"))
    assert breaker.state == "open"

    transition = breaker.record_success(stale_success)

    assert transition.changed is False
    assert breaker.state == "open"


def test_abandoned_half_open_permit_does_not_leave_breaker_stuck():
    clock = FakeClock()
    breaker = ServiceCircuitBreaker(clock=clock)
    _open_with_transient_failures(breaker)
    clock.advance(30)
    probe = breaker.admit()

    breaker.abandon(probe)

    assert breaker.state == "open"
    replacement = breaker.admit()
    breaker.record_success(replacement)
    assert breaker.state == "closed"


def test_new_process_breaker_starts_closed_despite_persisted_health_error():
    breaker = ServiceCircuitBreaker(
        clock=FakeClock(), persisted_status="error"
    )

    permit = breaker.admit()

    assert breaker.state == "closed"
    breaker.record_success(permit)
