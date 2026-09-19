"""Bounded parallel cross-library retrieval: concurrency, context and order.

Serial fan-out made one global question cost the SUM of its libraries (13
libraries, 18 seconds of retrieval in production). These tests pin the three
properties that make the parallel form safe rather than merely faster:
libraries really do overlap, each worker inherits the run-local context the
retrievers depend on, and nothing user-visible depends on which thread wins.

Concurrency is proven with barriers and events, never with wall-clock
assertions -- this repository has repeatedly seen timing assertions turn into
CI flakes.
"""
from __future__ import annotations

from threading import Barrier, Event
import threading

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.models.global_ask import GlobalAskRequest
from app.services.global_ask import GlobalAskError
from app.repositories.read_budget import current_read_budget
from app.services.retrieval_run import current_retrieval_run
from app.services.source_scope import current_source_scope
from tests.test_global_ask import setup, finished, _stage_job_threads


def _hit(nb):
    return RetrievedChunk(
        chunk_id=f"c-{nb}", source_id=f"s-{nb}", source_title=f"Source {nb}",
        section_path="Section", text=f"evidence {nb}", element_ids=[f"e-{nb}"],
        relevance=0.8,
    )


def _libraries(service, readable, names):
    readable.clear()
    readable.update(names)
    return sorted(names)


def test_libraries_retrieve_at_the_same_time(setup):
    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})
    # A lone job may use the whole retrieval pool, so exactly that many
    # libraries must be inside `retrieve` at once before any may return. The
    # parties count is DERIVED from the setting: hard-coding it would silently
    # stop testing anything the day the default changes.
    parties = min(len(names), service.settings.global_ask_retrieval_concurrency)
    assert parties >= 2, "a single-slot pool cannot demonstrate parallelism"
    together = Barrier(parties)

    def retrieve(nb, query):
        together.wait(timeout=5)
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done"
    assert result.searched_notebook_ids == names


def test_retrieval_threads_inherit_the_run_context_and_prepared_embedding(setup):
    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b"})
    key = "compare batteries"[: service.settings.embed_truncate_chars]
    service.prepare_query = lambda query: current_retrieval_run().memoized_embedding(
        query[: service.settings.embed_truncate_chars], lambda: [1.0, 0.0],
    )
    observed = {}

    def retrieve(nb, query):
        run = current_retrieval_run()
        observed[nb] = {
            "run_id": None if run is None else run.run_id,
            # The single model-stage embedding must be visible to the worker;
            # without the copied context each library would re-embed.
            "embedding": None if run is None else run.peek_embedding(key),
            "budget": current_read_budget() is not None,
            "scope": current_source_scope().source_ids,
            "thread": threading.current_thread().name,
        }
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    result = finished(service, service.start(
        GlobalAskRequest(question="compare batteries"), user_id="u",
    ))
    assert result.status == "done"
    assert sorted(observed) == names
    assert len({row["run_id"] for row in observed.values()}) == 1
    assert all(row["run_id"] for row in observed.values())
    for nb, row in observed.items():
        assert row["embedding"] == [1.0, 0.0]
        assert row["budget"]
        assert row["scope"] == frozenset({f"s-{nb}"})
        assert row["thread"].startswith("global-ask-retrieve")


def test_evidence_order_follows_resolved_scope_not_completion_order(setup):
    service, readable, _, syntheses = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})
    # The first resolved library finishes LAST; ordering must not notice.
    released = Event()
    arrived = Barrier(len(names) - 1)

    def retrieve(nb, query):
        if nb == names[0]:
            assert released.wait(5)
        else:
            arrived.wait(timeout=5)
            released.set()
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done"
    assert result.searched_notebook_ids == names
    # peer_evidence reserves one passage per lane in lane order, so the model's
    # evidence order is the resolved participant order too.
    assert [chunk.notebook_id for chunk in syntheses[-1][1]] == names


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 1}], indirect=True)
def test_revoked_authority_fails_the_request_and_stops_unstarted_libraries(setup):
    service, readable, retrieved, syntheses = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})
    current = [list(names)]

    def retrieve(nb, query):
        retrieved.append(nb)
        current[0] = []
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    job = service.start(GlobalAskRequest(question="q"), user_id="u",
                        allowed_notebook_ids=names, authority_check=lambda: current[0])
    result = finished(service, job)
    assert result.status == "failed" and result.response is None
    # One slot means the revocation lands before the next library is admitted;
    # the remaining two must never reach retrieval at all.
    assert retrieved == [names[0]]
    assert not syntheses


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 1}], indirect=True)
def test_cancellation_inside_one_library_stops_the_rest(setup):
    service, readable, retrieved, syntheses = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})

    def retrieve(nb, query):
        retrieved.append(nb)
        for cancel_event in list(service._events.values()):
            cancel_event.set()
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "cancelled" and result.response is None
    assert retrieved == [names[0]]
    assert not syntheses


def test_scope_above_the_product_ceiling_is_rejected_not_truncated(setup):
    service, readable, retrieved, _ = setup
    _libraries(service, readable, {f"library-{index}" for index in range(9)})
    with pytest.raises(GlobalAskError) as error:
        service.start(GlobalAskRequest(question="q"), user_id="u")
    assert error.value.status_code == 422
    assert "8" in error.value.message
    assert not retrieved


@pytest.mark.parametrize("variable,value", [
    ("GLOBAL_ASK_MAX_NOTEBOOKS", "9"),
    ("GLOBAL_ASK_RETRIEVAL_CONCURRENCY", "9"),
    ("GLOBAL_ASK_RETRIEVAL_CONCURRENCY", "0"),
])
def test_over_ceiling_configuration_fails_validation(monkeypatch, variable, value):
    """The participant ceiling is a product decision, not a deployment knob:
    raising it in the environment must fail loudly at startup."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(Exception) as error:
        Settings(_env_file=None)
    assert variable.lower() in str(error.value).lower() or "less than or equal" in str(error.value)


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 2}], indirect=True)
def test_a_second_job_gets_retrieval_slots_while_the_first_is_running(setup, monkeypatch):
    """The retrieval pool is shared and FIFO. A job that submitted all of its
    libraries at once owned every slot, and the next questioner's libraries sat
    in the queue until that job's own phase budget expired -- zero searched
    libraries and an evidence-free answer. Each live job must keep a share.

    The handshake, not a clock, is the proof: every library waits until a
    library of the OTHER job is also executing, which only a fair window can
    satisfy when the pool has fewer slots than there are queued libraries.
    """
    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})
    questions = ("first question", "second question")
    running = {question: Event() for question in questions}

    def retrieve(nb, query):
        running[query].set()
        for question, signal in running.items():
            assert signal.wait(5), f"{question} never got a retrieval slot"
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    staged = []
    monkeypatch.setattr("app.services.global_ask.threading.Thread.start",
                        _stage_job_threads(staged))
    # Both jobs must be registered BEFORE either starts collecting, otherwise
    # the first one legitimately sees itself as the only live job.
    jobs = [service.start(GlobalAskRequest(question=question), user_id="u")
            for question in questions]
    assert len(staged) == 2
    monkeypatch.undo()
    for thread in staged:
        thread.start()
    for job in jobs:
        result = finished(service, job)
        assert result.status == "done", result.error
        assert result.searched_notebook_ids == names


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 2}], indirect=True)
def test_a_failed_library_releases_the_slots_the_others_are_holding(setup):
    """One library's authority failure loses the whole request. A library that
    is still executing must notice and let go of its retrieval slot (and the
    database connection behind it) instead of running out its own budget and
    starving the next user.

    Two slots: the first two libraries start together, the first revokes
    authority and returns, the third is admitted next and fails its
    ``_check``. The second is still inside ``retrieve`` at that moment -- it is
    the one that used to keep its slot for the rest of its 5s budget.
    """
    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})
    current = [list(names)]
    aborted, holding = Event(), Event()

    def retrieve(nb, query):
        if nb == names[0]:
            # Revoke only once the peer is demonstrably INSIDE retrieval, so
            # this tests slot release and not a lost race at its ``_check``.
            assert holding.wait(5)
            current[0] = []
            return [_hit(nb)], [], None
        budget = current_read_budget()
        holding.set()
        while True:
            try:
                budget.check()
            except Exception:
                aborted.set()
                raise
            Event().wait(0.01)

    service.retrieve = retrieve
    job = service.start(GlobalAskRequest(question="q"), user_id="u",
                        allowed_notebook_ids=names, authority_check=lambda: current[0])
    # The per-library budget is 5s; the abort has to arrive long before that.
    assert aborted.wait(3), (
        "an in-flight library kept its retrieval slot after the job failed"
    )
    assert finished(service, job).status == "failed"


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 4}], indirect=True)
def test_every_progress_save_keeps_the_resolved_receipt_order(setup):
    """A poller must never watch the receipt order jump around: each
    intermediate save is a subsequence of ``resolved_notebook_ids``, even when
    libraries finish in exactly the opposite order."""
    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b", "c", "d"})
    gates = {name: Event() for name in names}
    saved = []
    save_progress = service.store.save_progress

    def recording_save(job, user_id):
        saved.append(list(job.searched_notebook_ids))
        return save_progress(job, user_id)

    service.store.save_progress = recording_save

    def retrieve(nb, query):
        # Release strictly in reverse resolved order.
        position = names.index(nb)
        if position == len(names) - 1:
            gates[nb].set()
        assert gates[nb].wait(5)
        if position:
            gates[names[position - 1]].set()
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done"
    assert result.searched_notebook_ids == names
    assert saved and saved[0] == [names[-1]]
    for snapshot in saved:
        assert snapshot == [name for name in names if name in snapshot], snapshot


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 1}], indirect=True)
def test_per_library_budget_starts_when_the_library_starts(setup, monkeypatch):
    """A library queued behind the concurrency bound must not be charged for
    its wait. With one slot the second library starts long after the first, so
    its budget has to be measured from its own start, not from submission."""
    import time as real_time
    from types import SimpleNamespace

    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b"})
    clock = [real_time.monotonic()]
    monkeypatch.setattr("app.services.global_ask.time",
                        SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = Event(), Event()
    observed = {}

    def retrieve(nb, query):
        observed[nb] = (clock[0], current_read_budget().deadline)
        if nb == names[0]:
            entered.set()
            assert release.wait(5)
        return [_hit(nb)], [], None

    service.retrieve = retrieve
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    # Well past the per-library budget, well inside the phase budget.
    clock[0] += service.settings.global_ask_notebook_timeout_seconds * 2
    release.set()
    assert finished(service, job).status == "done"
    budget = service.settings.global_ask_notebook_timeout_seconds
    for name in names:
        started, deadline = observed[name]
        assert deadline - started == budget, name


@pytest.mark.parametrize("setup", [{"global_ask_retrieval_concurrency": 1}], indirect=True)
def test_never_started_libraries_say_so_and_leave_a_reason_code(setup, monkeypatch):
    """A library still queued when the phase budget expired issued no query.
    Its receipt must not blame the selected scope, and the machine-readable
    cause must still reach telemetry -- the retrieval module never saw it."""
    import time as real_time
    from types import SimpleNamespace

    service, readable, _, _ = setup
    names = _libraries(service, readable, {"a", "b"})
    events = []
    service.event_log = SimpleNamespace(emit=events.append)
    clock = [real_time.monotonic()]
    monkeypatch.setattr("app.services.global_ask.time",
                        SimpleNamespace(monotonic=lambda: clock[0]))
    save_progress = service.store.save_progress

    def burn_the_phase_budget(job, user_id):
        # Between the first library finishing and the second being admitted --
        # the one moment where the second has issued nothing at all.
        clock[0] += service.settings.global_ask_retrieval_timeout_seconds + 1
        return save_progress(job, user_id)

    service.store.save_progress = burn_the_phase_budget
    service.retrieve = lambda nb, query: ([_hit(nb)], [], None)
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))
    assert result.status == "done"
    assert result.searched_notebook_ids == [names[0]]
    assert [row.notebook_id for row in result.skipped_notebooks] == [names[1]]
    assert result.skipped_notebooks[0].reason == "检索未开始，请稍后重试。"
    assert [event["reason"] for event in events] == ["queue_deadline"]
    assert events[0]["kind"] == "global_retrieval_skipped"
    assert events[0]["notebook_id"] == names[1]
