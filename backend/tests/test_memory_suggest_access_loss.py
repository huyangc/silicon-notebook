"""SQLite: losing read access clears the auto-memory suggestion job row
(plan T10 / G17), through the runtime's sharing service and group store.
PostgreSQL twin: ``tests/postgres/test_memory_suggest_access_loss_pg.py``;
shared cases: ``tests/memory_suggest_access_loss_cases.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_access_loss_cases as cases


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def env(tmp_path: Path) -> cases.Env:
    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    return cases.Env(rt=repo._runtime, run=run, rows=rows)


def test_remove_member_clears_job(env):
    cases.assert_remove_member_clears_job(env)


def test_kick_all_clears_jobs(env):
    cases.assert_kick_all_clears_jobs(env)


def test_kick_all_samples_loss_clock_after_member_deletion(env):
    cases.assert_kick_all_samples_loss_clock_after_member_deletion(env)


def test_self_exit_clears_job(env):
    cases.assert_self_exit_clears_job(env)


def test_group_self_leave_and_admin_remove_clear_job(env):
    cases.assert_group_leave_clears_job(env)


def test_admin_demotion_clears_admin_only_job(env):
    cases.assert_admin_demotion_clears_admin_only_job(env)


def test_delete_grant_clears_job(env):
    cases.assert_delete_grant_clears_job(env)


def test_delete_group_grants_for_notebook_clears_job(env):
    cases.assert_delete_group_grants_for_notebook_clears_job(env)


def test_delete_group_clears_jobs(env):
    cases.assert_delete_group_clears_jobs(env)


def test_keeps_job_when_access_survives_via_other_grant(env):
    cases.assert_keeps_job_when_access_survives_via_other_grant(env)


def test_keeps_job_when_access_survives_via_another_group(env):
    cases.assert_keeps_job_when_access_survives_via_another_group(env)


def test_keeps_job_when_access_survives_via_everyone(env):
    cases.assert_keeps_job_when_access_survives_via_everyone(env)


@pytest.mark.parametrize("lose", cases.REGRANT_PATHS)
def test_regaining_access_starts_a_new_window(env, lose):
    cases.assert_regrant_after_loss_starts_new_window(env, lose)


@pytest.mark.parametrize("kinds", cases.INDEPENDENT_REVOCATIONS)
def test_independent_revocations_clear_only_the_last_read_window(env, kinds):
    first, second = cases.independent_revocations(env, kinds)
    first(env.rt.database)
    assert env.has_job(), "a surviving independent read path must keep the claim"
    second(env.rt.database)
    cases.assert_revocation_window_replaced(env)


def test_member_hygiene_preserves_live_window(env):
    cases.assert_member_hygiene_preserves_live_window(env)


def test_delayed_old_completion_cannot_reopen_membership_window(env):
    cases.assert_delayed_old_completion_cannot_reopen_membership_window(env)


def test_owner_and_public_read_period_semantics(env):
    cases.assert_owner_and_public_read_period_semantics(env)


@pytest.mark.parametrize("survives", (False, True))
def test_read_loss_boundary_is_open_at_full_microsecond_precision(env, survives):
    cases.assert_read_loss_boundary_is_open_at_full_microsecond_precision(env, survives=survives)


@pytest.mark.parametrize("path", cases.READ_PERIOD_PATHS)
@pytest.mark.parametrize("with_job", (False, True))
@pytest.mark.parametrize("survives", (False, True))
def test_delayed_completion_uses_continuous_read_period(env, path, with_job, survives):
    cases.assert_delayed_completion_read_period(env, path, with_job=with_job, survives=survives)


def test_enable_samples_service_clock_after_waiting_for_read_loss(env):
    from contextlib import contextmanager
    from copy import copy
    from threading import Event, Thread
    from types import SimpleNamespace

    from app.services.memory_suggest_service import MemorySuggestService

    env.seed()
    env.store.set_memory_mode(cases.FRIEND, "manual", now=cases.at(0))
    env.grant("original", "user", cases.FRIEND)
    env.ask("old-before-enable", 4)
    env.rt.groups.now = lambda: cases.at(5)
    written, release, attempted = Event(), Event(), Event()
    clock, sampled, errors = {"now": cases.at(3)}, [], []

    class ControlledWrite:
        def __init__(self, *, hold):
            self.hold = hold

        @contextmanager
        def write(self, **kwargs):
            if not self.hold:
                attempted.set()
            with env.rt.database.write(**kwargs) as db:
                yield db
                if self.hold:
                    written.set()
                    assert release.wait(timeout=20)

    def run(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    def revoke():
        groups = copy(env.rt.groups)
        groups.database = ControlledWrite(hold=True)
        groups.delete_user_grant(cases.NB, cases.FRIEND)

    def now():
        sampled.append(clock["now"])
        return clock["now"]

    def enable():
        store = copy(env.store)
        store.database = ControlledWrite(hold=False)
        settings = SimpleNamespace(auto_memory_suggest_enabled=True, auto_memory_suggest_clarified_enabled=True)
        MemorySuggestService(store, None, settings, now).set_mode(cases.FRIEND, "suggest")

    workers = [Thread(target=lambda: run(revoke)), Thread(target=lambda: run(enable))]
    try:
        workers[0].start()
        assert written.wait(timeout=20)
        workers[1].start()
        assert attempted.wait(timeout=20)
        clock["now"] = cases.at(6)
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=30)
        assert not any(worker.is_alive() for worker in workers)
    assert not errors, errors
    assert sampled == [cases.at(6)]
    assert env.store.user_state(cases.FRIEND)["enabled_at"] == cases.at(6)
    env.grant("restored", "user", cases.FRIEND)
    env.ask("new-after-enable", 7)
    env.signal("old-before-enable", minute=8)
    boundary, cursor_id = env.cursor()
    rows = env.store.clarified_scan(cases.FRIEND, cases.NB, cursor_created_at=boundary.isoformat(),
                                    cursor_id=cursor_id, inclusive=True, limit=20)
    assert [row["id"] for row in rows] == ["new-after-enable"]
