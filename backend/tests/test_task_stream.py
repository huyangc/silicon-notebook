from __future__ import annotations

import asyncio
import json
import threading

import pytest
from starlette.requests import ClientDisconnect, Request

from app.api.task_stream import task_event_stream, task_stream_response


# Every bound in this module exists to turn a hang into a readable failure,
# never to assert latency.  The behaviour under test is ordering -- a heartbeat
# precedes the terminal frame, a closed stream cancels its worker -- so the
# wall clock is free to be generous.  A real regression still reds because it
# never produces the frame at all; a loaded runner no longer does.
_HANG_TIMEOUT_SECONDS = 30.0

_TERMINAL_EVENTS = frozenset({"final", "error", "cancelled"})


class _Request:
    def __init__(self, disconnected: bool = False) -> None:
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def _drain_to_terminal(stream, *, on_event=None) -> dict:
    """Consume ``stream`` until it emits a terminal frame, and return that frame.

    Draining is bounded by the worker finishing, not by a frame count.  With
    ``heartbeat_seconds=0`` the generator emits heartbeats as fast as the
    consumer takes them -- on the order of 20k/second -- so any fixed cap
    spends itself in single-digit milliseconds and becomes a race against
    thread scheduling rather than an assertion about the stream.

    Non-terminal frames are inspected through ``on_event`` as they arrive
    instead of being accumulated, for the same reason: how many of them appear
    is a property of the scheduler, not of the behaviour under test.
    """
    seen = 0
    last: dict | None = None

    async def drain() -> dict | None:
        nonlocal seen, last
        async for line in stream:
            event = json.loads(line)
            if event["event"] in _TERMINAL_EVENTS:
                return event
            seen += 1
            last = event
            if on_event is not None:
                on_event(event)
        return None

    try:
        terminal = await asyncio.wait_for(drain(), timeout=_HANG_TIMEOUT_SECONDS)
    except TimeoutError:
        pytest.fail(
            f"stream emitted no terminal event within {_HANG_TIMEOUT_SECONDS}s "
            f"({seen} non-terminal frames seen, last={last!r})"
        )
    if terminal is None:
        pytest.fail(
            "stream ended without a terminal event "
            f"({seen} non-terminal frames seen, last={last!r})"
        )
    return terminal


def test_task_stream_emits_content_free_heartbeat_then_final():
    gate = threading.Event()

    def work():
        gate.wait()
        return {"value": "result"}

    async def run():
        stream = task_event_stream(
            _Request(),
            work,
            stage="preview",
            error_code="preview_failed",
            heartbeat_seconds=0,
        )
        started = json.loads(await anext(stream))
        heartbeat = json.loads(await anext(stream))
        gate.set()
        terminal = await _drain_to_terminal(stream)
        await stream.aclose()
        return started, heartbeat, terminal

    started, heartbeat, terminal = asyncio.run(run())
    assert started == {"event": "started", "stage": "preview", "elapsed_ms": 0}
    assert heartbeat["event"] == "heartbeat"
    assert set(heartbeat) == {"event", "stage", "elapsed_ms"}
    assert terminal == {
        "event": "final",
        "stage": "preview",
        "result": {"value": "result"},
    }


def test_task_stream_redacts_worker_exception_text():
    secret = "private upstream detail"

    def work():
        raise RuntimeError(secret)

    async def run():
        stream = task_event_stream(
            _Request(),
            work,
            stage="preview",
            error_code="preview_failed",
            heartbeat_seconds=0,
        )
        leaked = []

        def reject_leak(event):
            if secret in json.dumps(event):
                leaked.append(event)

        terminal = await _drain_to_terminal(stream, on_event=reject_leak)
        return terminal, leaked

    terminal, leaked = asyncio.run(run())
    assert terminal == {
        "event": "error",
        "stage": "preview",
        "error": "preview_failed",
    }
    assert leaked == []
    assert secret not in json.dumps(terminal)


def test_task_stream_disconnect_sets_cooperative_cancellation():
    cancelled = threading.Event()
    worker_stopped = threading.Event()

    def work():
        cancelled.wait()
        worker_stopped.set()

    async def run():
        stream = task_event_stream(
            _Request(disconnected=True),
            work,
            stage="preview",
            error_code="preview_failed",
            cancel_event=cancelled,
            heartbeat_seconds=0,
        )
        assert json.loads(await anext(stream))["event"] == "started"
        try:
            await anext(stream)
        except StopAsyncIteration:
            pass

    asyncio.run(run())
    assert cancelled.is_set()
    assert worker_stopped.wait(timeout=_HANG_TIMEOUT_SECONDS)


def test_task_stream_aclose_after_started_cancels_worker():
    cancelled = threading.Event()
    worker_stopped = threading.Event()

    def work():
        cancelled.wait()
        worker_stopped.set()

    async def run():
        stream = task_event_stream(
            _Request(),
            work,
            stage="preview",
            error_code="preview_failed",
            cancel_event=cancelled,
        )
        assert json.loads(await anext(stream))["event"] == "started"
        await stream.aclose()

    asyncio.run(run())
    assert cancelled.is_set()
    assert worker_stopped.wait(timeout=_HANG_TIMEOUT_SECONDS)


def test_task_stream_asgi_send_failure_cancels_worker():
    cancelled = threading.Event()
    worker_stopped = threading.Event()

    def work():
        cancelled.wait()
        worker_stopped.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/task",
        "raw_path": b"/task",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    response = task_stream_response(
        request,
        work,
        stage="preview",
        error_code="preview_failed",
        cancel_event=cancelled,
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client socket closed")

    async def run():
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)

    asyncio.run(run())
    assert cancelled.is_set()
    assert worker_stopped.wait(timeout=_HANG_TIMEOUT_SECONDS)
