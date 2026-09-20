"""Shared NDJSON transport for request-local interactive work.

This module is deliberately smaller than the durable Ask/report job runtimes.
It is for work whose result still belongs to the current browser interaction:
model-backed previews and authoring suggestions.  The wire stays alive while
the worker is quiet, reports only content-free stage/elapsed metadata, and
cooperatively cancels providers that accept the supplied ``threading.Event``.

Durable jobs must not use this helper: navigation is allowed to detach those
jobs and their state is recovered from the database instead.

ONE EXCEPTION, and it is a transport rather than a runtime:
:func:`deliver_ask_events` drains a DURABLE job's delivery queue to NDJSON.  It
lives here because two route modules now need it (the notebook Ask stream and
the global Ask stream), and one route module reaching into another's private
helper is a cross-domain edge this codebase avoids by convention -- no guard
catches it (``test_route_domain_boundaries`` pins endpoint ownership, not
imports), which is the reason to keep the shared piece here.  It starts, cancels and owns nothing: a disconnect stops delivery to
this client only, and the durable worker keeps running by contract.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from app.services.cancellation import AskCancelled


# A protocol keepalive rather than a user-tunable quality/cost rail.  It is
# shared by every request-local interactive stream so a new model-backed UI
# action cannot accidentally inherit a proxy-sized silent window.
INTERACTIVE_STREAM_HEARTBEAT_SECONDS = 5.0

NDJSON_STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


class ClosingStreamingResponse(StreamingResponse):
    """Close the task generator even when the ASGI body send itself fails."""

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def ndjson_line(payload: dict[str, Any]) -> str:
    # Preserve the existing Ask stream's byte shape; compatibility tests and
    # older consumers may inspect the human-readable JSON spacing.
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _observe_detached_task(task: "asyncio.Task[Any]") -> None:
    """Consume a detached worker's terminal exception without delaying exit."""

    def consume(done: "asyncio.Task[Any]") -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except asyncio.CancelledError:
            pass

    task.add_done_callback(consume)


async def task_event_stream(
    request: Request,
    work: Callable[[], Any],
    *,
    stage: str,
    error_code: str,
    cancel_event: threading.Event | None = None,
    heartbeat_seconds: float = INTERACTIVE_STREAM_HEARTBEAT_SECONDS,
):
    """Yield one request-local task as started/heartbeat/final NDJSON events.

    ``stage`` and ``error_code`` are fixed call-site vocabulary, never exception
    text.  This keeps heartbeat/error frames content-free and safe for browser
    diagnostics.  The result itself is encoded only in the terminal ``final``
    frame.
    """
    cancellation = cancel_event or threading.Event()
    started_at = monotonic()
    task = asyncio.create_task(asyncio.to_thread(work))
    task_observed = False
    try:
        # Keep the first yield inside the lifetime guard too.  ASGI may lose
        # the client while sending ``started`` (or any later frame) and close
        # this generator at that suspension point; the worker still needs the
        # same cooperative cancellation signal in that case.
        yield ndjson_line({"event": "started", "stage": stage, "elapsed_ms": 0})

        while not task.done():
            done, _pending = await asyncio.wait(
                {task}, timeout=max(0.0, heartbeat_seconds)
            )
            if done:
                break
            if await request.is_disconnected():
                cancellation.set()
                _observe_detached_task(task)
                return
            yield ndjson_line({
                "event": "heartbeat",
                "stage": stage,
                "elapsed_ms": max(0, round((monotonic() - started_at) * 1000)),
            })

        try:
            result = task.result()
            task_observed = True
        except AskCancelled:
            task_observed = True
            yield ndjson_line({"event": "cancelled", "stage": stage})
            return
        except Exception:
            task_observed = True
            # Never serialize exception text. The stable code is useful in the
            # diagnostic console while the caller supplies the human fallback.
            yield ndjson_line({
                "event": "error",
                "stage": stage,
                "error": error_code,
            })
            return
        yield ndjson_line({
            "event": "final",
            "stage": stage,
            "result": jsonable_encoder(result),
        })
    finally:
        if not task.done():
            cancellation.set()
        if not task_observed:
            _observe_detached_task(task)


async def deliver_ask_events(
    events,
    request: Request,
    *,
    clock: Callable[[], float] = monotonic,
    heartbeat_seconds: float = INTERACTIVE_STREAM_HEARTBEAT_SECONDS,
    idle_sleep_seconds: float | None = None,
):
    """Drain one durable-job delivery queue to NDJSON — shared by every client
    that attaches to such a job (a fresh notebook Ask, a keyed re-submission
    that attached to an existing job, a global Ask job's push stream).

    ``clock`` and ``heartbeat_seconds`` are injected rather than read from this
    module's globals so a caller keeps its OWN heartbeat rail and its own
    monkeypatchable clock: ``ask_routes`` has had both since before this loop
    moved here, and binding them to this module would silently retarget them.

    客户端断连只停止本次流(break),**不** set cancel_event —— worker 脱离连接
    跑到完、答案照存。唯一取消入口是各自领域的 cancel 端点。

    ``idle_sleep_seconds``: how an IDLE connection waits for the next event.
    ``None`` (the notebook Ask, unchanged) blocks in a worker thread on the
    queue -- fine for a stream that lives exactly as long as one question. A
    global job's stream is opened by every page that is looking at a running
    turn and stays open for the whole run, and each blocked ``to_thread`` holds
    one slot of asyncio's DEFAULT executor (``min(32, cpu + 4)``), which the
    rest of the API shares: a few dozen watchers would starve every other
    ``to_thread`` call, this route's own authority check included. Passing a
    number makes the idle wait an ``asyncio.sleep`` on the event loop instead:
    no thread at all, at the price of delivery latency bounded by that number.
    队列的 close() 只通知**为本次连接服务的**跟随者(键重发接回既有 job 的轮询、
    全局问答的 ``global-ask-follow``)停下;真正执行的 worker 不读它。
    """
    last_delivery = clock()
    try:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                if await request.is_disconnected():
                    break
                try:
                    if idle_sleep_seconds is None:
                        event = await asyncio.to_thread(events.get, True, 0.1)
                    else:
                        await asyncio.sleep(idle_sleep_seconds)
                        event = events.get_nowait()
                except queue.Empty:
                    now = clock()
                    if now - last_delivery >= heartbeat_seconds:
                        # An empty NDJSON line is transport-only: it carries no
                        # notebook content and existing clients already ignore it.
                        yield "\n"
                        last_delivery = now
                    continue
            if event is None:
                break
            yield ndjson_line(event)
            last_delivery = clock()
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()


def task_stream_response(
    request: Request,
    work: Callable[[], Any],
    *,
    stage: str,
    error_code: str,
    cancel_event: threading.Event | None = None,
) -> StreamingResponse:
    return ClosingStreamingResponse(
        task_event_stream(
            request,
            work,
            stage=stage,
            error_code=error_code,
            cancel_event=cancel_event,
        ),
        media_type="application/x-ndjson",
        headers=NDJSON_STREAM_HEADERS,
    )
