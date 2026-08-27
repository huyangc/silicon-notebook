"""Shared NDJSON transport for request-local interactive work.

This module is deliberately smaller than the durable Ask/report job runtimes.
It is for work whose result still belongs to the current browser interaction:
model-backed previews and authoring suggestions.  The wire stays alive while
the worker is quiet, reports only content-free stage/elapsed metadata, and
cooperatively cancels providers that accept the supplied ``threading.Event``.

Durable jobs must not use this helper: navigation is allowed to detach those
jobs and their state is recovered from the database instead.
"""
from __future__ import annotations

import asyncio
import json
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


class _TaskStreamingResponse(StreamingResponse):
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


def task_stream_response(
    request: Request,
    work: Callable[[], Any],
    *,
    stage: str,
    error_code: str,
    cancel_event: threading.Event | None = None,
) -> StreamingResponse:
    return _TaskStreamingResponse(
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
