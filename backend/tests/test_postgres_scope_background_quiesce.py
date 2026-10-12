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


_QUIESCE_THREAD = "quiesce-under-test"


def _on_quiesce_thread() -> bool:
    return threading.current_thread().name == _QUIESCE_THREAD


class _Handshake:
    """Orders a chain against the quiesce with events, never sleeps.

    ``parked_on_light``: the quiesce has entered its wait on the light pool,
    so it has already looked at every pool it will look at before that one;
    only then does the light job hand off. ``handed_off``: the follow-up has
    been submitted. ``progressed``: the quiesce has either returned, or has
    begun a real (non-zero) wait on the follow-up's pool after the hand-off --
    whichever happens first decides the case, so the outcome never depends on
    how fast either thread is scheduled.
    """

    def __init__(self, monkeypatch, follow_up_pool: str) -> None:
        self.parked_on_light = threading.Event()
        self.handed_off = threading.Event()
        self.progressed = threading.Event()
        handshake = self
        executor_wait_idle = background_jobs._MaintenanceExecutor.wait_idle
        kg_wait_idle = kg_scheduler.wait_idle

        def wait_idle(executor, deadline):
            if _on_quiesce_thread():
                if executor.pool == background_jobs._LIGHT_POOL:
                    handshake.parked_on_light.set()
                elif executor.pool == follow_up_pool and handshake.handed_off.is_set():
                    handshake.progressed.set()
            return executor_wait_idle(executor, deadline)

        def waiting_on_kg(timeout):
            if (
                follow_up_pool == "kg" and timeout > 0 and _on_quiesce_thread()
                and handshake.handed_off.is_set()
            ):
                handshake.progressed.set()
            return kg_wait_idle(timeout)

        monkeypatch.setattr(background_jobs._MaintenanceExecutor, "wait_idle", wait_idle)
        monkeypatch.setattr(kg_scheduler, "wait_idle", waiting_on_kg)

    def run_quiesce(self, timeout: float = 10.0):
        outcome: dict[str, object] = {}

        def run():
            try:
                background_quiesce.quiesce_process_background_work(timeout)
                outcome["returned"] = True
            except BaseException as exc:  # pragma: no cover - reported below
                outcome["error"] = exc
            finally:
                self.progressed.set()

        thread = threading.Thread(target=run, name=_QUIESCE_THREAD)
        thread.start()
        return thread, outcome


def _drive_chain(handshake, hand_off_to, follow_up_submit) -> tuple[bool, dict]:
    """A light-pool job waits until the quiesce is parked on the light pool,
    hands a blocked follow-up to ``follow_up_submit`` and returns. Answers
    whether the quiesce returned before the follow-up finished."""
    follow_up_may_finish = threading.Event()
    follow_up_done = threading.Event()

    def follow_up():
        follow_up_may_finish.wait(10)
        follow_up_done.set()

    def light():
        handshake.parked_on_light.wait(10)
        follow_up_submit(follow_up)
        handshake.handed_off.set()

    background_jobs.submit(light, name=f"catalog-quiesce-to-{hand_off_to}")
    thread, outcome = handshake.run_quiesce()
    try:
        assert handshake.progressed.wait(10), "the quiesce neither waited nor returned"
        returned_early = "returned" in outcome and not follow_up_done.is_set()
    finally:
        handshake.parked_on_light.set()
        follow_up_may_finish.set()
        thread.join(10)
    return returned_early, outcome


@pytest.mark.parametrize(
    "follow_up_pool_exists",
    [True, False],
    ids=["into_an_existing_pool", "into_a_pool_created_while_waiting"],
)
def test_quiesce_waits_for_work_chained_from_one_maintenance_pool_into_another(
    monkeypatch, follow_up_pool_exists,
):
    """A light-pool job submits heavy-pool follow-up and returns. Waiting on
    a snapshot of the pools taken once would see the heavy pool idle (or not
    see it at all) before the hand-off, then the light pool idle after it,
    and return while the follow-up runs."""
    if follow_up_pool_exists:
        background_jobs.submit(lambda: None, name="rebuildkg-quiesce-warm").join(5)
    handshake = _Handshake(monkeypatch, background_jobs._HEAVY_POOL)
    returned_early, outcome = _drive_chain(
        handshake, "heavy",
        lambda job: background_jobs.submit(job, name="rebuildkg-quiesce-follow-up"),
    )
    assert not returned_early, "quiesce returned while the chained follow-up was still running"
    assert outcome == {"returned": True}


def test_quiesce_waits_for_kg_work_handed_off_from_a_maintenance_pool(monkeypatch):
    handshake = _Handshake(monkeypatch, "kg")
    returned_early, outcome = _drive_chain(handshake, "kg", kg_scheduler.submit_job)
    assert not returned_early, "quiesce returned while the handed-off KG job was still running"
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
        assert time.monotonic() - began < 10.0, "a later scope waited out the bound again"
        assert "First timeout:" in str(later.value)
        assert background_quiesce._first_timeout in str(later.value)
    finally:
        release.set()
    assert kg_scheduler.wait_idle(5)
    # once the stuck work has ended, a later scope proceeds without waiting
    background_quiesce.quiesce_process_background_work(30.0)
