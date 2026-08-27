from __future__ import annotations

import asyncio
import json
import threading

import pytest
from starlette.requests import ClientDisconnect, Request

from app.api.task_stream import task_event_stream, task_stream_response


class _Request:
    def __init__(self, disconnected: bool = False) -> None:
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


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
        terminal = None
        for _ in range(100):
            event = json.loads(await anext(stream))
            if event["event"] == "final":
                terminal = event
                break
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
    def work():
        raise RuntimeError("private upstream detail")

    async def run():
        stream = task_event_stream(
            _Request(),
            work,
            stage="preview",
            error_code="preview_failed",
            heartbeat_seconds=0,
        )
        events = []
        async for line in stream:
            events.append(json.loads(line))
        return events

    events = asyncio.run(run())
    assert events[-1] == {
        "event": "error",
        "stage": "preview",
        "error": "preview_failed",
    }
    assert "private upstream detail" not in json.dumps(events)


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
    assert worker_stopped.wait(timeout=1)


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
    assert worker_stopped.wait(timeout=1)


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
    assert worker_stopped.wait(timeout=1)
