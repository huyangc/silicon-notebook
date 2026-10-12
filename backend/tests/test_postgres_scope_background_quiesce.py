"""The PostgreSQL scope teardown's pre-drop quiesce
(``tests/postgres/background_quiesce.py``) without a database: it must see
follow-up work chained between pools, and a stuck pool must cost one bounded
wait per process, not one per remaining scope.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services import background_jobs
from app.services.kg import scheduler as kg_scheduler
from tests.postgres import background_quiesce


@pytest.fixture(autouse=True)
def _fresh_quiesce_state(monkeypatch):
    monkeypatch.setattr(background_quiesce, "_first_timeout", None)
    assert kg_scheduler.wait_idle(5), "KG pools were busy before the test"


def _run_quiesce_in_thread(timeout: float = 10.0):
    outcome: dict[str, object] = {}

    def run():
        try:
            background_quiesce.quiesce_process_background_work(timeout)
            outcome["returned"] = True
        except BaseException as exc:  # pragma: no cover - reported below
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="quiesce-under-test")
    thread.start()
    return thread, outcome


@pytest.mark.parametrize(
    "follow_up_pool_exists",
    [True, False],
    ids=["into_an_existing_pool", "into_a_pool_created_while_waiting"],
)
def test_quiesce_waits_for_work_chained_from_one_maintenance_pool_into_another(
    follow_up_pool_exists,
):
    """A light-pool job submits heavy-pool follow-up and returns. Waiting on
    a snapshot of the pools taken once would see the heavy pool idle (or not
    see it at all) before the hand-off, then the light pool idle after it,
    and return while the follow-up runs."""
    if follow_up_pool_exists:
        background_jobs.submit(lambda: None, name="rebuildkg-quiesce-warm").join(5)
    light_may_hand_off = threading.Event()
    follow_up_may_finish = threading.Event()
    follow_up_started = threading.Event()
    follow_up_done = threading.Event()

    def follow_up():
        follow_up_started.set()
        follow_up_may_finish.wait(10)
        follow_up_done.set()

    def light():
        light_may_hand_off.wait(10)
        background_jobs.submit(follow_up, name="rebuildkg-quiesce-follow-up")

    background_jobs.submit(light, name="catalog-quiesce-chain")
    thread, outcome = _run_quiesce_in_thread()
    try:
        # let the quiesce pass the pools that are idle now and park on the
        # light one, then hand off
        time.sleep(0.2)
        light_may_hand_off.set()
        assert follow_up_started.wait(5)
        thread.join(1.0)
        follow_up_finished_when_quiesce_returned = (
            outcome.get("returned") is not True or follow_up_done.is_set()
        )
    finally:
        light_may_hand_off.set()
        follow_up_may_finish.set()
        thread.join(10)
    assert follow_up_finished_when_quiesce_returned, (
        "quiesce returned while the chained follow-up was still running"
    )
    assert outcome == {"returned": True}


def test_quiesce_waits_for_kg_work_handed_off_from_a_maintenance_pool():
    hand_off = threading.Event()
    kg_may_finish = threading.Event()
    kg_started = threading.Event()
    kg_done = threading.Event()

    def kg_job():
        kg_started.set()
        kg_may_finish.wait(10)
        kg_done.set()

    def light():
        hand_off.wait(10)
        kg_scheduler.submit_job(kg_job)

    background_jobs.submit(light, name="catalog-quiesce-to-kg")
    thread, outcome = _run_quiesce_in_thread()
    try:
        time.sleep(0.2)
        hand_off.set()
        assert kg_started.wait(5)
        thread.join(1.0)
        kg_finished_when_quiesce_returned = (
            outcome.get("returned") is not True or kg_done.is_set()
        )
    finally:
        hand_off.set()
        kg_may_finish.set()
        thread.join(10)
    assert kg_finished_when_quiesce_returned
    assert outcome == {"returned": True}


def test_a_stuck_pool_costs_one_bounded_wait_and_later_scopes_fail_at_once():
    release = threading.Event()
    started = threading.Event()

    def stuck():
        started.set()
        release.wait(30)

    kg_scheduler.submit_job(stuck)
    try:
        assert started.wait(5)
        began = time.monotonic()
        with pytest.raises(background_quiesce.BackgroundWorkStuck) as first:
            background_quiesce.quiesce_process_background_work(0.3)
        assert time.monotonic() - began >= 0.3
        assert "'job_active': 1" in str(first.value)

        began = time.monotonic()
        with pytest.raises(background_quiesce.BackgroundWorkStuck) as later:
            background_quiesce.quiesce_process_background_work(30.0)
        assert time.monotonic() - began < 1.0, "a later scope waited out the bound again"
        assert "First timeout:" in str(later.value)
        assert background_quiesce._first_timeout in str(later.value)
    finally:
        release.set()
    assert kg_scheduler.wait_idle(5)
    # once the stuck work has ended, a later scope proceeds without waiting
    background_quiesce.quiesce_process_background_work(30.0)
