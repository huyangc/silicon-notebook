from __future__ import annotations

import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.cancellation import AskCancelled
from app.services.retrieval_run import (
    current_retrieval_run,
    memoized_query_embedding,
    retrieval_fanout_slot,
    retrieval_run,
)


class _Log:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_nested_run_restores_outer_token_and_blank_context_is_isolated():
    assert current_retrieval_run() is None
    with retrieval_run(run_kind="outer") as outer:
        assert current_retrieval_run() is outer
        assert contextvars.Context().run(current_retrieval_run) is None
        observed = []

        class _ContextLog:
            def emit(self, _event):
                observed.append(current_retrieval_run())

        with retrieval_run(run_kind="inner", event_log=_ContextLog()) as inner:
            assert inner is not outer
            assert current_retrieval_run() is inner
        assert observed == [inner]
        assert current_retrieval_run() is outer
    assert current_retrieval_run() is None


def test_copy_context_threads_share_one_embedding_single_flight():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def compute():
        calls.append("physical")
        entered.set()
        assert release.wait(timeout=2)
        return [0.1, 0.2]

    with retrieval_run(run_kind="report_generation") as state:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                contextvars.copy_context().run,
                memoized_query_embedding,
                "same-query",
                compute,
            )
            assert entered.wait(timeout=2)
            second = pool.submit(
                contextvars.copy_context().run,
                memoized_query_embedding,
                "same-query",
                compute,
            )
            release.set()
            assert first.result(timeout=2) == [0.1, 0.2]
            assert second.result(timeout=2) == [0.1, 0.2]
        assert calls == ["physical"]
        assert state.embedding_requests == 1
        assert state.embedding_hits == 1


def test_failed_embedding_is_not_cached():
    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return [1.0]

    with retrieval_run(run_kind="report_planning") as state:
        with pytest.raises(RuntimeError, match="transient"):
            memoized_query_embedding("q", compute)
        assert memoized_query_embedding("q", compute) == [1.0]
        assert state.embedding_requests == 2
        assert state.embedding_errors == 1


def test_leaf_fanout_is_bounded_and_outer_workers_do_not_deadlock():
    active = 0
    peak = 0
    lock = threading.Lock()
    start = threading.Barrier(6)
    pair = threading.Barrier(2)

    def leaf(value):
        nonlocal active, peak
        # Make every worker contend for the same two slots before either of
        # the first pair can finish, so the wait accounting is deterministic.
        start.wait(timeout=2)
        with retrieval_fanout_slot():
            with lock:
                active += 1
                peak = max(peak, active)
            pair.wait(timeout=2)
            with lock:
                active -= 1
            return value

    with retrieval_run(run_kind="report_generation", fanout_limit=2) as state:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, leaf, index)
                for index in range(6)
            ]
            assert [future.result(timeout=3) for future in futures] == list(range(6))
        assert peak == 2
        assert state.fanout_acquires == 6

    # Orchestrators intentionally hold no slot while waiting for a child leaf.
    # With a one-slot run two such outer workers must still both complete.
    with retrieval_run(run_kind="report_generation", fanout_limit=1):
        with ThreadPoolExecutor(max_workers=2) as leaf_pool:
            def orchestrate(value):
                child = leaf_pool.submit(
                    contextvars.copy_context().run,
                    lambda: _one_leaf(value),
                )
                return child.result(timeout=2)

            def _one_leaf(value):
                with retrieval_fanout_slot():
                    return value

            with ThreadPoolExecutor(max_workers=2) as outer_pool:
                futures = [
                    outer_pool.submit(
                        contextvars.copy_context().run, orchestrate, value
                    )
                    for value in (1, 2)
                ]
                assert sorted(future.result(timeout=3) for future in futures) == [1, 2]


def test_cancelled_waiter_never_enters_leaf_when_only_slot_is_occupied():
    cancel_event = threading.Event()
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_started = threading.Event()
    waiter_leaf_executed = threading.Event()

    def holder():
        with retrieval_fanout_slot():
            holder_entered.set()
            assert release_holder.wait(timeout=2)

    def waiter():
        waiter_started.set()
        with retrieval_fanout_slot():
            waiter_leaf_executed.set()

    with retrieval_run(
        run_kind="report_generation",
        fanout_limit=1,
        cancel_event=cancel_event,
    ) as state:
        with ThreadPoolExecutor(max_workers=2) as pool:
            holder_future = pool.submit(contextvars.copy_context().run, holder)
            assert holder_entered.wait(timeout=2)
            waiter_future = pool.submit(contextvars.copy_context().run, waiter)
            assert waiter_started.wait(timeout=2)
            deadline = time.monotonic() + 2
            while state.fanout_waits < 1 and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            assert state.fanout_waits == 1
            cancel_event.set()
            with pytest.raises(AskCancelled):
                waiter_future.result(timeout=2)
            assert not waiter_leaf_executed.is_set()
            assert state.fanout_acquires == 1  # holder only
            assert state.fanout_wait_ms > 0
            release_holder.set()
            holder_future.result(timeout=2)


def test_cancel_signal_is_a_noop_without_a_fanout_gate():
    cancel_event = threading.Event()
    cancel_event.set()
    executed = []
    with retrieval_run(
        run_kind="ask_chunk", cancel_event=cancel_event, fanout_limit=None
    ):
        with retrieval_fanout_slot():
            executed.append(True)
    assert executed == [True]


def test_retrieval_run_event_is_content_free_and_correlates_reports():
    log = _Log()
    sensitive_query = "USER-QUESTION-DO-NOT-LOG"
    with retrieval_run(
        run_kind="report_planning",
        event_log=log,
        fanout_limit=2,
        correlation_id="rep-safe-id",
    ):
        assert memoized_query_embedding(sensitive_query, lambda: [0.5]) == [0.5]
        with retrieval_fanout_slot():
            pass

    assert len(log.events) == 1
    event = log.events[0]
    assert event["kind"] == "retrieval_run_stats"
    assert event["run_kind"] == "report_planning"
    assert event["correlation_id"] == "rep-safe-id"
    assert sensitive_query not in json.dumps(event, ensure_ascii=False)
    assert not ({"notebook_id", "source_id", "query", "title", "text"} & set(event))


def test_retrieval_run_event_does_not_echo_an_unregistered_run_kind():
    log = _Log()
    with retrieval_run(run_kind="SECRET-USER-CONTENT", event_log=log):
        pass
    assert log.events[0]["run_kind"] == "unknown"
    assert "SECRET-USER-CONTENT" not in json.dumps(log.events[0])


def test_retrieval_run_actor_is_core_only_and_never_emitted():
    log = _Log()
    with retrieval_run(
        run_kind="ask_chunk",
        actor_id="secret-user-id",
        event_log=log,
    ) as state:
        assert state.actor_id == "secret-user-id"

    assert "actor_id" not in log.events[0]
    assert "secret-user-id" not in json.dumps(log.events[0])


def test_actor_identity_survives_the_report_worker_copy_context():
    observed = []

    def worker():
        state = current_retrieval_run()
        observed.append(state.actor_id if state is not None else None)

    with retrieval_run(run_kind="report_generation", actor_id="report-actor"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contextvars.copy_context().run, worker).result(timeout=2)

    assert observed == ["report-actor"]


def test_ask_and_report_entrypoints_install_their_actor_identity():
    from types import SimpleNamespace

    from app.models.ask import AskRequest
    from app.services.ask_service import AskService
    from app.services.report_engine import ReportEngine

    observed = []
    ask = object.__new__(AskService)
    ask.event_log = None
    ask.ask_chunk = lambda *_args, **_kwargs: observed.append(
        ("ask", current_retrieval_run().actor_id)
    )
    AskService.ask(
        ask,
        "notebook",
        AskRequest(question="question", mode="chunk"),
        user_id="ask-actor",
    )

    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(event_log=None)
    report.settings = SimpleNamespace(report_retrieval_fanout=2)
    report.user_id = "report-actor"
    report.cancel_event = None
    report._plan_outline_run = lambda *_args, **_kwargs: observed.append(
        ("plan", current_retrieval_run().actor_id)
    )
    report._generate_run = lambda *_args, **_kwargs: observed.append(
        ("generate", current_retrieval_run().actor_id)
    )
    ReportEngine.plan_outline(report, "notebook", "report", "question")
    ReportEngine.generate(report, "notebook", "report", "question")

    assert observed == [
        ("ask", "ask-actor"),
        ("plan", "report-actor"),
        ("generate", "report-actor"),
    ]


def test_event_log_base_exception_still_restores_contextvar():
    class _InterruptingLog:
        def emit(self, _event):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        with retrieval_run(
            run_kind="report_planning", event_log=_InterruptingLog()
        ):
            assert current_retrieval_run() is not None
    assert current_retrieval_run() is None
