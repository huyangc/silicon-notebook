"""Stable cancellation token shape used by application ports."""
from __future__ import annotations

import threading
import time
from typing import Optional


CancelEvent = Optional[threading.Event]


class CoreCancellation(Exception):
    """Base class for request cancellation that extension hosts must propagate."""


class AskCancelled(CoreCancellation):
    """Raised when an in-flight Ask request is cancelled by the client."""


def raise_if_cancelled(cancel_event: CancelEvent) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AskCancelled()


def sleep_or_cancel(seconds: float, cancel_event: CancelEvent) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        raise_if_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))
