"""``GlobalAskService.wait``: the MCP ``ask`` tool's blocking follow of a
global job, on the real service and store (no transport).

Four ways a job can be followed -- a live in-process feed, a job that is
already terminal, a job only the store knows about (another process runs it),
a job that disappears mid-wait -- plus the abandon signal.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.domain.cancellation import AskWaitAbandoned
from app.models.global_ask import GlobalAskJob, GlobalAskRequest
from app.services.global_ask import GlobalAskError
from tests.test_global_ask import finished, setup  # noqa: F401 - fixture
from tests.test_global_ask_stream import _park


def _in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _store_only_job(service, job_id="gask-elsewhere") -> GlobalAskJob:
    """A running job no feed in this process knows: the store follower's case."""
    job = GlobalAskJob.model_validate({
        "job_id": job_id, "conversation_id": "gconv-elsewhere", "status": "running",
        "question": "q", "created_at": "2026-10-09T00:00:00+00:00",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": ["a"],
    })
    service.store.create(job, "u", "req-elsewhere", "{}", "mcp", new_conversation=True)
    return job


def test_wait_follows_a_live_in_process_run_to_its_end(setup):
    service, _, _, _ = setup
    parked, resume = _park(service, before=("s0",), after=("s1",))
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    time.sleep(0.05)
    assert thread.is_alive(), "wait returned while the run was still parked"
    resume.set()
    thread.join(5)
    assert box["value"].status == "done"


def test_wait_returns_at_once_for_a_job_already_terminal(setup):
    service, _, _, _ = setup
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    finished(service, job)
    started = time.monotonic()
    assert service.wait(job.job_id, user_id="u").status == "done"
    assert time.monotonic() - started < 2


def test_wait_follows_a_job_only_the_store_knows_about(setup):
    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    service.follow_poll_max_seconds = 0.02
    job = _store_only_job(service)
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    time.sleep(0.1)
    assert thread.is_alive()
    job.status = "done"
    service.store.save(job, "u")
    thread.join(5)
    assert box["value"].status == "done"


def test_a_job_that_disappears_mid_wait_surfaces_as_not_found(setup):
    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    service.follow_poll_max_seconds = 0.02
    job = _store_only_job(service, "gask-going")
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    time.sleep(0.05)
    service.store.delete(job.conversation_id, "u")
    thread.join(5)
    assert isinstance(box.get("error"), GlobalAskError)
    assert box["error"].status_code == 404


def test_stop_abandons_the_wait_and_leaves_the_job_alone(setup):
    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    service.follow_poll_max_seconds = 0.02
    job = _store_only_job(service, "gask-abandoned")
    stop = threading.Event()
    thread, box = _in_thread(
        lambda: service.wait(job.job_id, user_id="u", stop=stop)
    )
    time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(box.get("error"), AskWaitAbandoned)
    assert service.store.job(job.job_id, "u").status == "running"


class _Clock:
    """A monotonic clock only the test moves, in whole seconds so every sum
    and difference is exactly representable (no float-equality traps)."""

    def __init__(self, now: float = 1000.0):
        self._now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class _Tap:
    """The delivery queue ``wait`` drains, with a handshake on what it has done.

    ``gets`` counts started polls and ``processed`` counts frames ``wait`` has
    fully handled (a frame is handled once its next poll starts), so a test
    waits for the loop instead of sleeping: ``settle`` returns once a poll that
    began after the call has run to completion -- i.e. ``wait`` has evaluated
    its stall window against whatever the clock reads now.
    """

    def __init__(self):
        self.ready = threading.Event()
        self.gets = 0
        self.processed = 0
        self._holding = False
        self._cv = threading.Condition()
        self._queue = None

    def attach(self, events):
        self._queue = events
        self.ready.set()
        return self

    def get(self, timeout=None):
        with self._cv:
            if self._holding:
                self.processed += 1
                self._holding = False
            self.gets += 1
            self._cv.notify_all()
        item = self._queue.get(timeout=timeout)  # raises queue.Empty
        with self._cv:
            self._holding = isinstance(item, dict)
        return item

    def __getattr__(self, name):  # close / closed / put ... of the real queue
        return getattr(self._queue, name)

    def wait_until(self, predicate) -> None:
        with self._cv:
            assert self._cv.wait_for(predicate, 5), "wait stopped polling"

    def settle(self) -> None:
        with self._cv:
            target = self.gets + 2
        self.wait_until(lambda: self.gets >= target)


def _tap_attach(service) -> _Tap:
    """Route ``attach`` through a :class:`_Tap` and shorten ``wait``'s poll."""
    tap = _Tap()
    real = service.attach
    service.attach = lambda *args, **kwargs: tap.attach(real(*args, **kwargs))
    service.wait_poll_seconds = 0.01
    return tap


def test_wait_defaults_to_the_real_monotonic_clock_and_half_second_poll(setup):
    """The seams the stall-window tests drive must default to production's
    behaviour: wall-clock monotonic time and a half-second poll."""
    service, _, _, _ = setup
    assert service._clock is time.monotonic
    assert service.wait_poll_seconds == 0.5


def test_a_foreign_job_with_no_progress_ends_the_wait_and_closes_the_follower(
    setup, monkeypatch,
):
    """Another process's job whose executor died stays ``running`` and the
    store follower never sends its sentinel. Past ``ATTACH_STALL_SECONDS``
    without a progress frame the wait closes the queue (ending the follower)
    and raises ``AskExecutorGone``; the job itself is left alone."""
    from app.domain.cancellation import AskExecutorGone
    from app.services import global_ask as global_ask_module

    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    service.follow_poll_max_seconds = 0.02
    clock = _Clock()
    service._clock = clock
    monkeypatch.setattr(global_ask_module, "ATTACH_STALL_SECONDS", 10.0)
    tap = _tap_attach(service)
    job = _store_only_job(service, "gask-stalled")
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    assert tap.ready.wait(5)
    tap.wait_until(lambda: tap.processed >= 2)  # started + the first snapshot
    clock.advance(9)  # inside the window
    tap.settle()
    assert thread.is_alive() and "error" not in box, "gave up inside the window"
    clock.advance(2)  # 11 s without a frame
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(box.get("error"), AskExecutorGone)
    assert tap.closed.is_set(), "the store follower was left running"
    assert service.store.job(job.job_id, "u").status == "running"


def test_a_progressing_foreign_job_is_followed_past_the_stall_window(
    setup, monkeypatch,
):
    """The window counts time WITHOUT progress: a foreign job that keeps
    adding trace steps is followed well past it, to its terminal state."""
    from app.models.ask import TraceStep
    from app.services import global_ask as global_ask_module

    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    service.follow_poll_max_seconds = 0.02
    clock = _Clock()
    service._clock = clock
    monkeypatch.setattr(global_ask_module, "ATTACH_STALL_SECONDS", 10.0)
    tap = _tap_attach(service)
    job = _store_only_job(service, "gask-progressing")
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    assert tap.ready.wait(5)
    tap.wait_until(lambda: tap.processed >= 2)  # started + the first snapshot
    for step in range(12):  # 72 s of progress, seven times the window
        clock.advance(6)  # each gap is inside the window; the sum is not
        job.trace.append(TraceStep(step_type="retrieve", summary=f"step {step}"))
        service.store.save(job, "u")
        tap.wait_until(lambda: tap.processed >= 3 + step)  # this step's frame
        assert thread.is_alive(), "the wait gave up on a job that kept progressing"
    job.status = "done"
    service.store.save(job, "u")
    thread.join(5)
    assert box.get("error") is None, box.get("error")
    assert box["value"].status == "done"


def test_a_live_in_process_run_is_followed_past_the_stall_window(setup, monkeypatch):
    """A job THIS process runs is followed however long it stays quiet."""
    from app.services import global_ask as global_ask_module

    service, _, _, _ = setup
    clock = _Clock()
    service._clock = clock
    monkeypatch.setattr(global_ask_module, "ATTACH_STALL_SECONDS", 0.0)
    tap = _tap_attach(service)
    parked, resume = _park(service, before=("s0",), after=("s1",))
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert parked.wait(5)
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    assert tap.ready.wait(5)
    tap.settle()
    clock.advance(3600)  # far past the (zero) window, then a few empty polls
    tap.settle()
    assert thread.is_alive(), "a live local run was given up on"
    resume.set()
    thread.join(5)
    assert box.get("error") is None, box.get("error")
    assert box["value"].status == "done"


def _failed_follow_queue(job_id: str):
    """What ``_follow`` delivers when its own store read broke: ``started``,
    an ``error`` frame, the sentinel -- while the job still reads running."""
    from app.services.ask_execution import new_delivery_queue

    events = new_delivery_queue()
    events.put({"event": "started", "job_id": job_id, "conversation_id": "c"})
    events.put({"event": "error", "error": "跟踪失败"})
    events.put(None)
    return events


def test_a_follower_error_on_a_running_job_is_followed_again_not_returned(setup):
    """An ``error`` frame then the sentinel is not the end of the job: the row
    still reads running, so ``wait`` follows again (no-progress clock carried
    over) and returns only the terminal job."""
    service, _, _, _ = setup
    service.follow_poll_seconds = 0.01
    service.follow_poll_max_seconds = 0.02
    job = _store_only_job(service, "gask-follow-retry")
    real_attach = service.attach
    attaches: list = []

    def attach(job_id, **kwargs):
        attaches.append(job_id)
        if len(attaches) == 1:
            return _failed_follow_queue(job_id)
        return real_attach(job_id, **kwargs)

    service.attach = attach
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    time.sleep(0.2)
    assert thread.is_alive(), "wait returned a still-running job"
    job.status = "done"
    service.store.save(job, "u")
    thread.join(5)
    assert box.get("error") is None, box.get("error")
    assert box["value"].status == "done"
    assert len(attaches) == 2


def test_a_follower_that_fails_twice_raises_instead_of_returning_running(setup):
    """A second follower failure on a job that still reads running raises
    ``AskFollowFailed`` (``unavailable`` over MCP) -- never a running page."""
    from app.domain.cancellation import AskFollowFailed

    service, _, _, _ = setup
    job = _store_only_job(service, "gask-follow-broken")
    service.attach = lambda job_id, **kwargs: _failed_follow_queue(job_id)
    thread, box = _in_thread(lambda: service.wait(job.job_id, user_id="u"))
    thread.join(5)
    assert not thread.is_alive(), "wait kept re-following a broken follower"
    assert isinstance(box.get("error"), AskFollowFailed)
    assert service.store.job(job.job_id, "u").status == "running"


def test_a_key_replayed_for_another_question_is_coded_and_maps_to_invalid_argument(setup):
    """The real ``replay`` raises the coded 409 the MCP mapping keys on."""
    from app.api.mcp_tools.global_ask import global_ask_tool_error
    from app.services.global_ask import REQUEST_KEY_REUSED

    service, _, _, _ = setup
    job = service.start(
        GlobalAskRequest(question="first question", client_request_id="key-1"),
        user_id="u",
    )
    finished(service, job)
    with pytest.raises(GlobalAskError) as reused:
        service.replay(
            GlobalAskRequest(question="another question", client_request_id="key-1"),
            user_id="u",
        )
    assert reused.value.reason == REQUEST_KEY_REUSED
    mapped = global_ask_tool_error(reused.value)
    assert mapped.code == "invalid_argument"
