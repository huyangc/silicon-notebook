"""In-process fan-out of ONE running global job to the clients watching it.

A leaf module on purpose: stdlib only, no store, no service, no models. It is
the push half of the global Ask transport, sitting on top of a job that stays
durable without it -- a client that never subscribes, or loses its connection,
still recovers everything by polling ``GET /jobs/{id}``.

THE ONE INVARIANT, and why the callables are callables
------------------------------------------------------
A reader that subscribes while the worker is publishing must see every step
exactly once and in order. That is only true if "read the current state" and
"register for what comes next" are ONE atomic step, which is why
:meth:`JobFeed.subscribe` takes a ``snapshot`` callable rather than a
pre-computed frame and :meth:`JobFeed.publish` takes a ``make_event`` callable
rather than a frame: both are evaluated UNDER this object's single lock. The
producer appends a step to the job before publishing it, so with the lock held
the step is either already inside the snapshot the newcomer gets, or it is
delivered to that newcomer afterwards -- never both-ways-missed.

⛔ NOTHING SLOW UNDER THE LOCK. The callables must read memory the caller
already owns; a store read or a database write inside one would serialize every
subscriber of the job behind it. Delivery is a ``put`` on an unbounded queue,
which never blocks, so a slow reader costs memory rather than a stalled worker.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class JobFeed:
    """Fan-out for one job: subscribe with a snapshot, publish, close once.

    Subscribers are the delivery queues of ``ask_execution.new_delivery_queue``
    -- a plain ``queue.Queue`` carrying a ``closed`` event the consumer sets
    when nobody is reading any more. A subscriber whose ``closed`` is set is
    dropped at the next publish rather than at disconnect: the consumer side is
    a route, and asking it to reach back into this object would make the
    transport own the feed's bookkeeping.
    """

    __slots__ = ("_lock", "_subscribers", "_closed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list = []
        self._closed = False

    def subscribe(self, events, snapshot: Callable[[], Any]) -> bool:
        """Deliver ``snapshot()`` to ``events`` and register it for the rest.

        ``False`` when the feed is already closed: the caller then has no live
        run to attach to and must fall back to reading the store, which is the
        only place a finished job's terminal state exists. Nothing is delivered
        in that case, so the caller owns the whole stream it hands back.
        """
        with self._lock:
            if self._closed:
                return False
            frame = snapshot()
            if frame is not None:
                events.put(frame)
            self._subscribers.append(events)
            return True

    def watched(self) -> bool:
        """Is anybody still reading? Lets a publisher skip work -- an authority
        re-check, say -- that only matters when a frame will actually leave."""
        with self._lock:
            if self._closed:
                return False
            self._subscribers = [
                events for events in self._subscribers if not self._is_closed(events)
            ]
            return bool(self._subscribers)

    def publish(self, make_event: Callable[[], Any]) -> None:
        """Evaluate ``make_event()`` and fan it out. A no-op once closed.

        Evaluated under the lock even when there is no subscriber at all: the
        cost is one cheap call, and making it conditional would mean the frame
        a late subscriber gets was built at a different instant than the one
        the early subscribers got.
        """
        with self._lock:
            if self._closed:
                return
            live = [
                events for events in self._subscribers
                if not self._is_closed(events)
            ]
            self._subscribers = live
            if not live:
                return
            frame = make_event()
            if frame is None:
                return
            for events in live:
                events.put(frame)

    def close(self, terminal_event: Any) -> None:
        """Deliver the terminal frame, then the ``None`` sentinel, once.

        The FIRST terminal wins and every later ``close``/``publish`` is a
        no-op: a cancelled job is ended by the cancelling request AND by its
        own worker unwinding moments later, and a reader must not be told the
        job finished twice -- the second frame would arrive after the sentinel
        the consumer already stopped at, and leak a queue that nobody drains.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers, self._subscribers = self._subscribers, []
        for events in subscribers:
            if self._is_closed(events):
                continue
            if terminal_event is not None:
                events.put(terminal_event)
            events.put(None)

    @staticmethod
    def _is_closed(events) -> bool:
        """Has this subscriber's consumer gone away?

        ``getattr`` rather than an attribute access: the queue carries
        ``closed`` by convention (``new_delivery_queue`` attaches it), and a
        plain ``queue.Queue`` handed in by a caller that built its own must
        still be deliverable rather than crashing the publishing worker.
        """
        closed = getattr(events, "closed", None)
        return closed is not None and closed.is_set()
