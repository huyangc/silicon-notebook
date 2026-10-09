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


class AskWaitAbandoned(Exception):
    """A caller that was only WAITING for someone else's Ask job stopped
    waiting (its own call was cancelled). The job itself is untouched."""


class AskExecutorGone(Exception):
    """An attached Ask job still reads ``running`` but nothing that could
    finish it is known to be alive (not executing in this process, and past
    ``ATTACH_STALL_SECONDS`` without progress)."""


class AskFollowFailed(Exception):
    """Following an attached Ask job failed (its progress reader broke) and
    a retry did not recover, while the job still reads ``running``. The job
    is untouched; its result can be read later."""


# How long an Ask job NOT executing in this process -- a notebook job
# (``AskService._wait_for_job``) or a global one (``GlobalAskService.wait``)
# -- may show no progress before an attached caller stops waiting for it. No
# heartbeat column exists to prove another worker is still on a job, so
# progress is the evidence: a new trace step (or the status leaving
# ``running``) restarts the window. A healthy job on another worker that keeps
# tracing is followed for as long as it runs; a row orphaned by a crash
# (before the startup sweep marks it failed) does not hold a waiter forever.
ATTACH_STALL_SECONDS = 1_800.0
