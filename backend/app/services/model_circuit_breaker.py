from __future__ import annotations

import time
import threading
from concurrent.futures import CancelledError
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Literal

from app.services.model_work import (
    MalformedModelResponse,
    ModelProviderError,
    ModelSchedulingError,
    ModelServiceUnavailable,
)


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    FATAL = "fatal"
    IGNORED = "ignored"


_FATAL_CODES = frozenset(
    {
        "unknown_model",
        "model_not_found",
        "model_rejected",
        "protocol_mismatch",
        "unsupported_protocol",
        "capability_mismatch",
        "unsupported_capability",
    }
)
_CONNECTION_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "PoolTimeout",
        "ReadTimeout",
        "TimeoutException",
        "WriteTimeout",
    }
)


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(error: BaseException) -> int | None:
    for candidate in _exception_chain(error):
        raw = getattr(candidate, "status_code", None)
        if raw is None:
            raw = getattr(getattr(candidate, "response", None), "status_code", None)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _provider_code(error: BaseException) -> str:
    for candidate in _exception_chain(error):
        raw = getattr(candidate, "code", "")
        if raw:
            return str(raw).strip().lower().replace("-", "_")
        body = getattr(candidate, "body", None)
        if isinstance(body, dict):
            body_error = body.get("error")
            if isinstance(body_error, dict) and body_error.get("code"):
                return str(body_error["code"]).strip().lower().replace("-", "_")
    return ""


def classify_provider_failure(error: BaseException) -> FailureKind:
    if isinstance(error, (ModelSchedulingError, CancelledError)):
        return FailureKind.IGNORED
    if isinstance(error, MalformedModelResponse):
        # A malformed body (empty content, bad JSON, truncation) is model
        # *behavior*, not provider *availability*: the HTTP round trip to
        # the service already succeeded — a real connection failure would
        # instead surface as ConnectionError/TimeoutError/a 5xx status and
        # is still classified TRANSIENT further down. The call site already
        # owns this failure mode end to end (per-call retry via
        # reasoning_max_retries, then a run-level degrade after repeated
        # failures in reasoning_retrieval/ask_service); having the breaker
        # also count it toward TRANSIENT_THRESHOLD double-charges the same
        # event and, under concurrency, a handful of malformed replies alone
        # can trip a 30s cooldown that has nothing to do with the service
        # being down. This isinstance check must stay ahead of the
        # `isinstance(error, ModelProviderError)` fallback below —
        # MalformedModelResponse is one of its subclasses, so if that
        # fallback ran first it would reclassify this back to TRANSIENT.
        return FailureKind.IGNORED

    status = _status_code(error)
    if status in (401, 403):
        return FailureKind.FATAL
    if status == 429 or (status is not None and status >= 500):
        return FailureKind.TRANSIENT
    if _provider_code(error) in _FATAL_CODES:
        return FailureKind.FATAL

    for candidate in _exception_chain(error):
        if isinstance(candidate, (ConnectionError, TimeoutError)):
            return FailureKind.TRANSIENT
        if type(candidate).__name__ in _CONNECTION_ERROR_NAMES:
            return FailureKind.TRANSIENT
    if isinstance(error, ModelProviderError):
        return FailureKind.TRANSIENT
    return FailureKind.IGNORED


@dataclass(frozen=True)
class BreakerPermit:
    epoch: int
    half_open: bool


@dataclass(frozen=True)
class BreakerTransition:
    state: Literal["closed", "open", "half_open"]
    failure_kind: FailureKind | None = None
    changed: bool = False
    opened: bool = False


class ServiceCircuitBreaker:
    TRANSIENT_THRESHOLD = 3
    COOLDOWN_SECONDS = 30.0

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        persisted_status: object | None = None,
    ) -> None:
        del persisted_status
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._consecutive_transient = 0
        self._opened_at: float | None = None
        self._epoch = 0
        self._half_open_permit: BreakerPermit | None = None

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        with self._lock:
            return self._state

    def admit(self, *, allow_half_open: bool = False) -> BreakerPermit:
        del allow_half_open
        with self._lock:
            if self._state == "closed":
                return BreakerPermit(epoch=self._epoch, half_open=False)
            if self._state == "half_open":
                raise ModelServiceUnavailable()

            assert self._opened_at is not None
            cooldown_elapsed = (
                self._clock() - self._opened_at >= self.COOLDOWN_SECONDS
            )
            if not cooldown_elapsed:
                raise ModelServiceUnavailable()
            self._state = "half_open"
            permit = BreakerPermit(epoch=self._epoch, half_open=True)
            self._half_open_permit = permit
            return permit

    def abandon(self, permit: BreakerPermit) -> None:
        with self._lock:
            if (
                self._state == "half_open"
                and self._half_open_permit == permit
                and permit.epoch == self._epoch
            ):
                self._half_open_permit = None
                self._state = "open"

    def record_success(self, permit: BreakerPermit) -> BreakerTransition:
        with self._lock:
            if permit.epoch != self._epoch:
                return BreakerTransition(state=self._state)
            if permit.half_open:
                if self._state != "half_open" or self._half_open_permit != permit:
                    return BreakerTransition(state=self._state)
                self._state = "closed"
                self._half_open_permit = None
                self._opened_at = None
                self._consecutive_transient = 0
                self._epoch += 1
                return BreakerTransition(state="closed", changed=True)
            if self._state != "closed":
                return BreakerTransition(state=self._state)
            changed = self._consecutive_transient != 0
            self._consecutive_transient = 0
            return BreakerTransition(state="closed", changed=changed)

    def record_failure(
        self, permit: BreakerPermit, error: BaseException
    ) -> BreakerTransition:
        failure_kind = classify_provider_failure(error)
        with self._lock:
            if permit.epoch != self._epoch:
                return BreakerTransition(
                    state=self._state, failure_kind=failure_kind
                )
            if permit.half_open:
                if self._state != "half_open" or self._half_open_permit != permit:
                    return BreakerTransition(
                        state=self._state, failure_kind=failure_kind
                    )
                if failure_kind is FailureKind.IGNORED:
                    if isinstance(error, MalformedModelResponse):
                        # The half-open probe's whole job is to test remote
                        # availability, and a malformed body already answers
                        # that question: the provider round-tripped a
                        # response, it just wasn't usable content (a model
                        # behavior problem with its own retry/degrade path,
                        # see classify_provider_failure above) — that is
                        # strictly *more* evidence of availability than the
                        # generic IGNORED case below (local scheduling
                        # errors / cancellation, which say nothing about the
                        # remote service either way). Treating this probe as
                        # failed would still bounce back through "open" and
                        # allow an immediate retry (opened_at below is left
                        # untouched), so no user-visible request is denied
                        # either way — but leaving it as a "failure" would
                        # discard the consecutive-transient reset that a
                        # closed breaker gets, and would keep charging any
                        # later genuine connection failure against a breaker
                        # that never got to prove itself recovered. Close it
                        # like a real success instead.
                        self._half_open_permit = None
                        self._state = "closed"
                        self._opened_at = None
                        self._consecutive_transient = 0
                        self._epoch += 1
                        return BreakerTransition(
                            state="closed", failure_kind=failure_kind, changed=True
                        )
                    # Other IGNORED half-open outcomes (ModelSchedulingError,
                    # CancelledError, and anything unclassified) carry no
                    # signal about the remote service in either direction —
                    # unlike the malformed-response case above, they may not
                    # even have reached the provider. Don't declare success,
                    # but don't spend a fresh 30s cooldown either: bounce
                    # back to "open" without resetting _opened_at (or the
                    # epoch), so the very next admit() call finds the
                    # cooldown already elapsed and immediately grants another
                    # half-open probe.
                    self._half_open_permit = None
                    self._state = "open"
                    return BreakerTransition(
                        state="open", failure_kind=failure_kind, changed=True
                    )
                self._open_locked()
                return BreakerTransition(
                    state="open",
                    failure_kind=failure_kind,
                    changed=True,
                    opened=True,
                )
            if self._state != "closed" or failure_kind is FailureKind.IGNORED:
                return BreakerTransition(
                    state=self._state, failure_kind=failure_kind
                )
            if failure_kind is FailureKind.FATAL:
                self._open_locked()
                return BreakerTransition(
                    state="open",
                    failure_kind=failure_kind,
                    changed=True,
                    opened=True,
                )

            self._consecutive_transient += 1
            if self._consecutive_transient < self.TRANSIENT_THRESHOLD:
                return BreakerTransition(
                    state="closed", failure_kind=failure_kind, changed=True
                )
            self._open_locked()
            return BreakerTransition(
                state="open",
                failure_kind=failure_kind,
                changed=True,
                opened=True,
            )

    def _open_locked(self) -> None:
        self._state = "open"
        self._opened_at = self._clock()
        self._half_open_permit = None
        self._consecutive_transient = 0
        self._epoch += 1
