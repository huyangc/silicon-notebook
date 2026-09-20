"""全局问答的**推送传输**:叶子 fan-out、服务端接线与 HTTP 端点。

这一层不拥有任何持久状态:作业照旧由 `POST /ask` 建立、由 `GET /jobs/{id}` 读取,
推送只是给**正在看**的客户端的加速器。所以这里的用例只问三件事——帧的次序与内容、
「中途接入不丢步也不错位」这条唯一不变量,以及断连绝不取消作业。

作业层的替身与夹具直接复用 ``test_global_ask``(同一个 ``_EngineDouble``、同一个
``setup``),推送不该有自己的第二套作业替身。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.models.ask import TraceStep
from app.models.global_ask import GlobalAskRequest
from app.services.global_ask import GlobalAskError
from app.services.global_ask_feed import JobFeed
from tests.test_global_ask import (  # noqa: F401 - ``setup`` is a fixture
    _EngineDouble, _LibraryUnavailable, finished, setup,
)


# --- 叶子:JobFeed ---------------------------------------------------------


def _queue():
    """A delivery queue in the shape ``ask_execution.new_delivery_queue`` makes."""
    events: queue.Queue = queue.Queue()
    closed = threading.Event()
    events.closed = closed
    events.close = closed.set
    return events


def _drain(events):
    """Everything already queued, without blocking."""
    frames = []
    while True:
        try:
            frames.append(events.get_nowait())
        except queue.Empty:
            return frames


def test_a_subscriber_joining_mid_publish_gets_the_snapshot_and_then_every_event():
    """The one invariant: no gap and no loss when publish and subscribe race.

    Driven deterministically rather than with a sleep: the snapshot callable
    parks inside ``subscribe``, so the feed's lock is demonstrably held while
    the producer appends its step and calls ``publish``. The publisher can
    therefore only be delivered AFTER this subscriber is registered, whichever
    way the two threads are scheduled -- which is exactly the property the
    callables exist for.
    """
    feed = JobFeed()
    events = _queue()
    trace: list = []
    entered, release = Event(), Event()

    def snapshot():
        entered.set()
        assert release.wait(5)
        return {"event": "progress", "trace_offset": 0, "steps": list(trace)}

    subscriber = Thread(target=lambda: feed.subscribe(events, snapshot))
    subscriber.start()
    assert entered.wait(5)
    # Appended before its publish, exactly as ``_append_trace`` does it.
    trace.append("s0")
    publisher = Thread(target=lambda: feed.publish(
        lambda: {"event": "progress", "trace_offset": 0, "steps": list(trace)},
    ))
    publisher.start()
    release.set()
    subscriber.join(5)
    publisher.join(5)
    trace.append("s1")
    feed.publish(lambda: {"event": "progress", "trace_offset": 1, "steps": ["s1"]})

    frames = _drain(events)
    assert frames[0] == {"event": "progress", "trace_offset": 0, "steps": ["s0"]}
    assert frames[1] == {"event": "progress", "trace_offset": 0, "steps": ["s0"]}
    assert frames[2] == {"event": "progress", "trace_offset": 1, "steps": ["s1"]}
    # Re-delivery is harmless by contract; applying the frames in order is what
    # has to reconstruct the producer's own list exactly.
    assert _apply(frames) == ["s0", "s1"]


def test_a_closed_subscriber_is_dropped_and_never_gets_the_terminal():
    feed = JobFeed()
    gone, live = _queue(), _queue()
    assert feed.subscribe(gone, lambda: None)
    assert feed.subscribe(live, lambda: None)
    gone.close()
    feed.publish(lambda: {"event": "progress"})
    feed.close({"event": "final"})

    assert _drain(gone) == []
    assert _drain(live) == [{"event": "progress"}, {"event": "final"}, None]


def test_close_delivers_the_terminal_then_the_sentinel_exactly_once():
    feed = JobFeed()
    events = _queue()
    assert feed.subscribe(events, lambda: None)
    feed.close({"event": "final"})
    assert _drain(events) == [{"event": "final"}, None]

    # A cancelled job is ended twice -- by the cancelling request and by its own
    # worker moments later. The second terminal must not land behind a sentinel
    # the consumer already stopped at.
    feed.close({"event": "gone"})
    feed.publish(lambda: {"event": "progress"})
    assert _drain(events) == []
    # And a newcomer is told there is no live run, rather than being handed a
    # stream that will never end.
    assert feed.subscribe(_queue(), lambda: {"event": "progress"}) is False


# --- 服务端接线 ------------------------------------------------------------


def _apply(frames):
    """The reader rule from the contract, as a client implements it."""
    trace: list = []
    for frame in frames:
        if not isinstance(frame, dict) or frame.get("event") != "progress":
            continue
        offset = frame["trace_offset"]
        if offset <= len(trace):
            trace = trace[:offset] + frame["steps"]
    return trace


def _summaries(frames):
    return [step["summary"] for step in _apply(frames)]


def _step(summary):
    return TraceStep(step_type="retrieve", summary=summary)


def _capture_on_trace(engine):
    """Expose the run's ``on_trace`` so a parked synthesis can emit steps.

    The engine double hands ``on_trace`` no further, and the parking point the
    job-layer tests use is ``synthesize`` -- which does not receive it. Stashing
    it on the way in is what lets one test place a step before an attach and
    another step after it, with no timing assumption between them.
    """
    original = engine.ask
    holder: dict = {}

    def ask(notebook_id, payload, **kwargs):
        holder["on_trace"] = kwargs.get("on_trace")
        return original(notebook_id, payload, **kwargs)

    engine.ask = ask
    return holder


def _collect(events, timeout=5):
    """Every frame up to (and not including) the ``None`` sentinel."""
    frames = []
    while True:
        frame = events.get(timeout=timeout)
        if frame is None:
            return frames
        frames.append(frame)


def _park(service, before=(), after=()):
    """Park the run inside synthesis; ``(parked, resume, job_starter)``.

    ``before``/``after`` are trace-step summaries emitted on either side of the
    parking point, so a test can attach exactly between them.
    """
    holder = _capture_on_trace(service.ask)
    parked, resume = Event(), Event()

    def synthesize(question, chunks, names, history, cancel):
        for summary in before:
            holder["on_trace"](_step(summary))
        parked.set()
        assert resume.wait(5)
        for summary in after:
            holder["on_trace"](_step(summary))
        return "answer", False, [], []

    service.ask.synthesize = synthesize
    return parked, resume


def test_a_local_running_job_streams_started_snapshot_progress_and_final(setup):
    service, _, _, _ = setup
    parked, resume = _park(service, before=("第一步",), after=("第二步",))
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)

    events = service.attach(job.job_id, user_id="u")
    assert events.get(timeout=5) == {
        "event": "started", "job_id": job.job_id,
        "conversation_id": job.conversation_id,
    }
    resume.set()
    frames = _collect(events)

    assert frames[0]["event"] == "progress" and frames[0]["trace_offset"] == 0
    assert frames[-1]["event"] == "final"
    assert frames[-1]["job"]["status"] == "done"
    assert frames[-1]["job"]["job_id"] == job.job_id
    assert _summaries(frames) == ["第一步", "第二步"]


def test_a_reader_attaching_mid_run_sees_every_step_exactly_once(setup):
    service, _, _, _ = setup
    parked, resume = _park(service, before=("s0", "s1"), after=("s2", "s3"))
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)

    events = service.attach(job.job_id, user_id="u")
    resume.set()
    frames = _collect(events)

    snapshot = next(frame for frame in frames if frame["event"] == "progress")
    # The first progress frame is a FULL snapshot of what already happened...
    assert snapshot["trace_offset"] == 0
    assert [step["summary"] for step in snapshot["steps"]] == ["s0", "s1"]
    # ...and applying the rest of the frames over it yields each step once.
    assert _summaries(frames) == ["s0", "s1", "s2", "s3"]


def test_coverage_frames_carry_whole_lists_not_deltas(setup):
    service, _, _, _ = setup

    def retrieve(notebook_id, query):
        if notebook_id == "b":
            raise _LibraryUnavailable("timeout")
        return _EngineDouble._default_retrieve(notebook_id, query)

    service.ask.retrieve = retrieve
    parked, resume = _park(service)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)

    events = service.attach(job.job_id, user_id="u")
    resume.set()
    frames = _collect(events)

    coverage = [
        frame for frame in frames
        if frame["event"] == "progress" and frame["searched_notebook_ids"]
    ]
    assert coverage, frames
    for frame in coverage:
        assert frame["searched_notebook_ids"] == ["a"]
        assert frame["degraded_notebook_ids"] == []
        assert [row["notebook_id"] for row in frame["skipped_notebooks"]] == ["b"]
        assert frame["skipped_notebooks"][0]["reason"]


def test_cancel_reaches_the_reader_without_waiting_for_the_worker(setup):
    service, _, _, _ = setup
    parked, resume = _park(service)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)
    events = service.attach(job.job_id, user_id="u")
    assert events.get(timeout=5)["event"] == "started"

    service.cancel(job.job_id, user_id="u")
    frames = _collect(events)

    assert frames[-1]["event"] == "final"
    assert frames[-1]["job"]["status"] == "cancelled"
    # The worker is still inside synthesis: the terminal came from the
    # cancelling call, not from an unwind the reader had to wait out.
    assert not resume.is_set()
    resume.set()
    assert finished(service, job).status == "cancelled"


def test_cancel_with_discard_ends_the_stream_as_gone(setup):
    service, _, _, _ = setup
    parked, resume = _park(service)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)
    events = service.attach(job.job_id, user_id="u")
    assert events.get(timeout=5)["event"] == "started"

    service.cancel(job.job_id, user_id="u", discard=True)
    frames = _collect(events)

    assert frames[-1] == {"event": "gone"}
    assert service.store.job(job.job_id, "u") is None
    resume.set()


def test_an_already_terminal_job_yields_started_and_final_only(setup):
    service, _, _, _ = setup
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert finished(service, job).status == "done"

    events = service.attach(job.job_id, user_id="u")
    frames = _collect(events)

    assert [frame["event"] for frame in frames] == ["started", "final"]
    assert frames[1]["job"]["status"] == "done"


def test_a_foreign_or_unknown_job_is_refused_before_any_frame(setup):
    service, _, _, _ = setup
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert finished(service, job).status == "done"

    with pytest.raises(GlobalAskError) as unknown:
        service.attach("gask-nope", user_id="u")
    assert unknown.value.status_code == 404
    with pytest.raises(GlobalAskError) as foreign:
        service.attach(job.job_id, user_id="other")
    assert foreign.value.status_code == 404


def test_a_job_with_no_local_feed_is_followed_from_the_store(setup):
    """Running elsewhere (another process, or a feed this one lost) → poll."""
    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    parked, resume = _park(service)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)
    with service._lock:
        service._feeds.pop(job.job_id)
        service._live.pop(job.job_id)

    events = service.attach(job.job_id, user_id="u")
    assert events.get(timeout=5)["event"] == "started"
    assert events.get(timeout=5)["event"] == "progress"
    resume.set()
    frames = _collect(events)

    assert frames[-1]["event"] == "final"
    assert frames[-1]["job"]["status"] == "done"


def test_closing_the_queue_stops_the_follower_and_never_cancels_the_job(setup):
    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    parked, resume = _park(service)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)
    with service._lock:
        service._feeds.pop(job.job_id)
        service._live.pop(job.job_id)
    events = service.attach(job.job_id, user_id="u")
    assert events.get(timeout=5)["event"] == "started"

    events.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(
            thread.name == "global-ask-follow" and thread.is_alive()
            for thread in threading.enumerate()
        ):
            break
        Event().wait(0.01)
    else:
        pytest.fail("the follower outlived the connection it served")

    resume.set()
    assert finished(service, job).status == "done"


def test_the_http_stream_returns_ndjson_lines_in_order(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import global_ask_routes
    from app.api.deps import get_current_user

    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    monkeypatch.setattr(global_ask_routes, "global_ask_service", lambda: service)
    app = FastAPI()
    app.include_router(global_ask_routes.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u")

    with TestClient(app) as client:
        job = service.start(GlobalAskRequest(question="q"), user_id="u")
        assert finished(service, job).status == "done"
        with client.stream(
            "GET", f"/api/global-ask/jobs/{job.job_id}/stream",
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(
                "application/x-ndjson")
            frames = [
                json.loads(line) for line in response.iter_lines() if line.strip()
            ]
        assert [frame["event"] for frame in frames] == ["started", "final"]
        assert frames[1]["job"]["status"] == "done"
        # The authority refusal is a real status code, not a frame inside a 200.
        missing = client.get("/api/global-ask/jobs/gask-nope/stream")
        assert missing.status_code == 404 and missing.headers["X-User-Message"] == "1"
