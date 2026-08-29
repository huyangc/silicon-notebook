"""Server-side concurrency gate for notebook full-text search.

Z8 (P0 止血, 热路径修复批 0): ``search_notebook`` runs an unindexed three-leg
ILIKE scan (sources/elements/...); on a large notebook a single call already
takes tens of seconds, and BEFORE this module there was zero server-side
limit on how many of those scans could run at once. The frontend's own
collection view fans out one search per *visible* notebook in parallel
(``frontend/app/collection-search.ts`` -- ``SEARCH_FANOUT_LIMIT``), and the
MCP ``search_notebook_context`` tool is a second, independent entry point
into the very same repository call. Three users typing in a shared workspace
at once was enough to exhaust the whole DB connection pool (default size 10).

A single process-wide semaphore, awaited by both call sites, caps how many
searches run concurrently across the WHOLE process -- not per notebook, per
user, or per entry point. ``SEARCH_CONCURRENCY_LIMIT`` mirrors the frontend's
own ``SEARCH_FANOUT_LIMIT`` (4): a lone user's own collection-view fan-out
can still run fully parallel end to end, while a second concurrent typist is
made to wait rather than pile another full-table scan onto the pool.

Why the gate is an ``asyncio`` semaphore and not a ``threading`` one
-------------------------------------------------------------------
The waiter's cost model is the whole point. This gate's first shape was a
``threading.BoundedSemaphore`` acquired with a blocking ``with`` from a
worker thread -- and that turned a search burst into a *whole-API* outage
(P1, 批 0 评审). Every blocked waiter sat on one of Starlette's 40 anyio
worker-thread tokens, which every synchronous endpoint in the process shares:
10 users x the frontend's 4-way fan-out = 40 concurrent searches, of which 4
ran and 36 held a token each, and ``/notebooks``, the source list, checkup
and upload could not get a single thread for as long as the burst lasted.
The pre-gate behaviour was *more* recoverable -- the same burst merely hit
the connection-pool timeout and 5xx'd fast.

So the waiting moved onto the event loop. A waiter here is a suspended
coroutine: no thread, no connection, no anyio token -- effectively free, and
capped only by however many requests the loop is already holding. Only the
<= 4 winners are charged anything, one thread and one connection each, for
the duration of the scan itself. Both entry points therefore acquire this
gate FROM the event loop, via ``run_under_search_gate``, and only afterwards
hand the blocking search body to a worker thread.

A cancelled request keeps its permit until its thread really finishes
--------------------------------------------------------------------
Both entry points go through ``run_under_search_gate`` rather than a plain
``async with``, and the difference is the whole point (codex #627 R3 P1). A
client that disconnects, times out, or is caught by a shutdown gets its
request task cancelled -- but the worker thread already inside the ILIKE
scan cannot be stopped, and neither can the DB connection it holds. With
``async with`` the permit is handed back the instant the cancellation
unwinds the block, so the next four requests walk in while the abandoned
scan is *still running*: the real number of concurrent scans climbs past the
ceiling and the connection pool is exhausted again -- exactly the failure
the gate exists to stop, now reachable just by cancelling requests.

The permit is therefore tied to the WORK, not to the awaiting coroutine: it
is released from a done-callback on the task that owns the thread, so it
comes back when the scan genuinely ends -- normally, on error, or long after
the requester walked away. The cost model this leaves is honest: a cancelled
search still costs its thread and its connection for as long as the scan
takes, and still counts against the ceiling for that whole time. It just
never costs MORE than an uncancelled one.

Deliberately still no timeout on acquisition. A timeout that gave up and
rejected a waiting request would silently shrink that request's result
coverage -- a quality regression -- whereas simply waiting only delays the
reply, which preserves the existing "search everything, always" contract.
Now that waiting is free of thread and connection cost, the argument for a
rejection path is weaker than it was, not stronger. Deliberately not
narrowed to per-notebook/per-user scope, and deliberately not a fix to the
ILIKE scan's cost itself: this is the minimal batch-0 stopgap the P0 audit
called for; the follow-up batch that gives the scan an index is the place to
reconsider whether this gate is still needed at all.

Single-loop assumption
----------------------
``asyncio.Semaphore`` is loop-affine, so this module hands out a lazily
constructed singleton rather than a module-level instance: at import time
there is no running loop yet (the app object is built before uvicorn starts
one), and constructing the semaphore then would bind it to nothing useful.
The singleton is created on first access *from inside the event loop*.

The assumption that makes one instance correct is that the process runs a
single event loop: uvicorn with one worker, and the MCP server mounted as an
ASGI sub-app of the very same FastAPI app (``app.main`` --
``app.mount("/mcp", ...)``), so both entry points live on that one loop.
Rather than trusting that silently, ``search_concurrency_gate()`` enforces
it: it raises if called with no running loop at all, and raises if a second,
still-live loop asks for the gate -- a state in which a shared semaphore
would silently stop gating (``Semaphore.acquire`` only notices a foreign loop
on the contended path, so the failure would otherwise be invisible exactly
when the gate matters). A previously bound loop that has been *closed* is a
finished loop, not a competing one, so the gate rebinds to the new loop
instead of failing; that is what makes the module usable across successive
``asyncio.run`` calls without a test-only reset hook.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.config import get_settings

T = TypeVar("T")

_gate: asyncio.Semaphore | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None


def search_concurrency_limit() -> int:
    """The configured ceiling (``SEARCH_CONCURRENCY_LIMIT``, default 4).

    A deployment-tunable cost budget (codex #627 R2 P2), validated ``ge=1`` in
    ``Settings``. The default mirrors the frontend's own ``SEARCH_FANOUT_LIMIT``
    (``frontend/app/collection-search.ts``): a lone user's collection-wide
    fan-out is never throttled by their own parallelism, only a genuinely
    concurrent second caller waits. Read at gate construction time — i.e. once
    per event loop, not per call.
    """
    return int(get_settings().search_concurrency_limit)


def search_concurrency_gate() -> asyncio.Semaphore:
    """The one process-wide search gate, constructed on first use.

    Shared by both entry points: the HTTP ``GET /notebooks/{id}/search``
    route (``app.api.ask_routes.search_notebook``) and the MCP
    ``search_notebook_context`` tool (``app.api.mcp_tools.memory_context``).
    Neither should acquire this object directly: go through
    ``run_under_search_gate``, which owns the acquire/release protocol that
    keeps a cancelled request's permit tied to its still-running thread. This
    accessor is the construction/identity seam underneath it.

    Raises ``RuntimeError`` when there is no running event loop, and when a
    second live loop asks for the gate (see "Single-loop assumption").
    """
    global _gate, _gate_loop

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:  # no running loop at all
        raise RuntimeError(
            "search_concurrency_gate() must be called from inside the event "
            "loop: the gate exists so that search waiters are suspended "
            "coroutines rather than blocked worker threads. Acquire it with "
            "`async with` in the async caller, then hand the blocking search "
            "body to asyncio.to_thread."
        ) from exc

    if _gate is None or _gate_loop is None or _gate_loop.is_closed():
        # First use in this process, or the loop the gate was bound to has
        # finished. Either way nothing can still be holding a permit. The
        # ceiling is read from Settings here — once per loop binding — so a
        # deployment tunes it via SEARCH_CONCURRENCY_LIMIT without code edits.
        _gate = asyncio.Semaphore(search_concurrency_limit())
        _gate_loop = loop
    elif _gate_loop is not loop:
        raise RuntimeError(
            "search_concurrency_gate() was reached from a second live event "
            "loop; this process is assumed to run exactly one (single-worker "
            "uvicorn, MCP mounted as a sub-app of the same FastAPI app). One "
            "asyncio.Semaphore cannot gate two loops -- it would silently "
            "stop throttling instead of failing."
        )
    return _gate


async def run_under_search_gate(work: Callable[[], Awaitable[T]]) -> T:
    """Run one search under the gate, holding the permit until ``work`` ends.

    ``work`` is called once, on the event loop, after a permit is in hand; it
    must return the awaitable that owns the blocking scan (``asyncio.to_thread``
    for the HTTP route, ``_run_with_progress`` for the MCP tool).

    The protocol, and why it is not a plain ``async with`` (codex #627 R3 P1):

    * The permit is released from a done-callback on the task wrapping
      ``work``, so it returns when the WORK finishes -- not when this
      coroutine stops waiting for it. A worker thread mid-ILIKE-scan cannot
      be cancelled, so releasing on cancellation would let the next caller in
      while the abandoned scan still holds its thread and its connection, and
      the real concurrency would climb past the ceiling.
    * ``asyncio.shield`` is what makes those two moments differ: cancelling
      our caller raises here and leaves the inner task running to completion.
      The cancellation still propagates -- the requester is not made to wait
      for a scan nobody wants any more -- it just does not take the permit
      with it. (Note the MCP entry point had the mirror-image bug rather than
      this one: ``anyio.to_thread.run_sync`` does not abandon its thread, so
      a bare ``async with`` there held the CALLER for the whole abandoned
      scan instead of releasing early. Both are fixed by the same shape.)
    * If the abandoned scan then fails, Python 3.13+ ``shield`` reports that
      exception through the loop's exception handler ("exception in shielded
      future"). That is accepted, not worked around: the failure really did
      happen and really was discarded, and one ERROR on a
      disconnect-plus-failure path is worth more than silence. It is not an
      "exception was never retrieved" leak, and it strands nothing -- the
      permit is already back by then.
    * A cancellation raised by ``acquire`` itself costs nothing: nothing is
      registered yet, and ``asyncio.Semaphore.acquire`` puts back a permit it
      was handed but could not return to us (CPython's own cancellation
      bookkeeping), so there is no permit to leak on that path.
    * ``release`` runs on the event loop from the callback, and runs on every
      outcome -- success, exception, or cancellation of the inner task -- so
      a failing scan cannot strand a permit either.
    """
    gate = search_concurrency_gate()
    await gate.acquire()
    task = asyncio.get_running_loop().create_task(work())
    task.add_done_callback(lambda _finished: gate.release())
    return await asyncio.shield(task)
