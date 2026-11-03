"""PostgreSQL: losing read access clears the auto-memory suggestion job row
(plan T10 / G17). SQLite twin: ``tests/test_memory_suggest_access_loss.py``;
shared cases: ``tests/memory_suggest_access_loss_cases.py``."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from copy import copy
from types import SimpleNamespace

import pytest

from tests import memory_suggest_access_loss_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def env(postgres_settings) -> cases.Env:
    from app.services.postgres_repository import PostgresRepository

    # Two controlled writers plus the independent lock-state observer.
    postgres_settings.postgres_pool_max_size = 8
    repository = PostgresRepository(postgres_settings)
    database = repository._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    try:
        yield cases.Env(rt=repository._runtime, run=run, rows=rows)
    finally:
        repository.close()


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


class _ControlledWrite:
    def __init__(self, inner, entered, release=None):
        self.inner, self.entered, self.release = inner, entered, release
        self.pid = None

    @contextmanager
    def write(self, **kwargs):
        with self.inner.write(**kwargs) as db:
            self.pid = db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            if self.release is None:
                self.entered.set()
            yield db
            if self.release is not None:
                self.entered.set()
                assert self.release.wait(timeout=20)


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


@pytest.mark.parametrize("kinds", cases.INDEPENDENT_REVOCATIONS)
def test_independent_read_revocations_serialize_before_cleanup(env, kinds):
    """Hold the first *real* revoke after cleanup, before commit. The other
    either completes (the bug) or waits on its common root lock (the fix).
    pg_blocking_pids observes the latter, without a guessed sleep/race window.
    """
    first, second = cases.independent_revocations(env, kinds)
    database = env.rt.database
    written, release, entered, finished = (threading.Event() for _ in range(4))
    one = _ControlledWrite(database, written, release)
    two = _ControlledWrite(database, entered)
    errors = []

    def run(action, wrapper, done=None):
        try:
            action(wrapper)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    workers = [threading.Thread(target=run, args=(first, one)),
               threading.Thread(target=run, args=(second, two, finished))]
    try:
        workers[0].start()
        assert written.wait(timeout=20)
        workers[1].start()
        assert entered.wait(timeout=20)
        deadline = time.monotonic() + 20
        while not finished.is_set():
            with database.connect() as db:
                blocked = db.execute(
                    "SELECT %s = ANY(pg_blocking_pids(%s)) AS blocked", (one.pid, two.pid),
                ).fetchone()["blocked"]
            if blocked:
                break
            assert time.monotonic() < deadline, "second revoke neither completed nor waited"
            finished.wait(timeout=0.01)
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=30)
        assert not any(worker.is_alive() for worker in workers)
    assert not errors, errors
    cases.assert_revocation_window_replaced(env)


def test_delayed_notification_waits_for_read_loss_then_regrant_uses_new_period(env):
    env.seed()
    env.rt.groups.now = lambda: cases.at(5)
    env.grant("original", "user", cases.FRIEND)
    env.ask("old", 1)  # Completion has not yet created any job.
    written, release, entered, finished = (threading.Event() for _ in range(4))
    one = _ControlledWrite(env.rt.database, written, release)
    two = _ControlledWrite(env.rt.database, entered)
    groups, suggest = copy(env.rt.groups), copy(env.store)
    groups.database, suggest.database = one, two
    results, errors = [], []

    def revoke():
        try:
            groups.delete_user_grant(cases.NB, cases.FRIEND)
        except BaseException as exc:
            errors.append(exc)

    def notify():
        try:
            results.append(suggest.note_signal(cases.NB, cases.FRIEND, ask_job_id="old", now=cases.at(12)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    workers = [threading.Thread(target=revoke), threading.Thread(target=notify)]
    try:
        workers[0].start()
        assert written.wait(timeout=20)
        workers[1].start()
        assert entered.wait(timeout=20)
        deadline = time.monotonic() + 20
        while True:
            with env.rt.database.connect() as db:
                blocked = db.execute("SELECT %s=ANY(pg_blocking_pids(%s)) AS b", (one.pid, two.pid)).fetchone()["b"]
            if blocked:
                break
            assert not finished.is_set(), "notification crossed the uncommitted root removal"
            assert time.monotonic() < deadline
            finished.wait(timeout=0.01)
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=30)
        assert not any(worker.is_alive() for worker in workers)
    assert not errors, errors
    assert results == [0] and not env.has_job()
    env.grant("restored", "user", cases.FRIEND)
    env.ask("new", 11)
    assert env.signal("old", minute=12) == 1
    boundary, cursor_id = env.cursor()
    rows = env.store.clarified_scan(cases.FRIEND, cases.NB, cursor_created_at=boundary.isoformat(),
                                    cursor_id=cursor_id, inclusive=True, limit=20)
    assert [row["id"] for row in rows] == ["new"]

def _controlled_pair(env, first_action, second_action, *, must_run_in_parallel=False, on_blocked=None):
    written, release, entered, finished = (threading.Event() for _ in range(4))
    one = _ControlledWrite(env.rt.database, written, release)
    two = _ControlledWrite(env.rt.database, entered)
    errors = []
    blocked = False

    def run(action, db, done=None):
        try:
            action(db)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    workers = [threading.Thread(target=run, args=(first_action, one)),
               threading.Thread(target=run, args=(second_action, two, finished))]
    try:
        workers[0].start()
        assert written.wait(timeout=20)
        workers[1].start()
        assert entered.wait(timeout=20)
        deadline = time.monotonic() + 20
        while not finished.is_set():
            with env.rt.database.connect() as db:
                blocked = db.execute("SELECT %s=ANY(pg_blocking_pids(%s)) AS b", (one.pid, two.pid)).fetchone()["b"]
            if blocked:
                assert not must_run_in_parallel, "independent shared read-loss transactions serialized globally"
                if on_blocked is not None:
                    on_blocked()
                break
            assert time.monotonic() < deadline
            finished.wait(timeout=0.01)
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=30)
        assert not any(worker.is_alive() for worker in workers)
    assert not errors, errors
    return blocked


@pytest.mark.parametrize("gain", ("new_member", "promote", "invite", "transfer"))
@pytest.mark.parametrize("gain_first", (False, True))
def test_group_read_gain_serializes_with_other_arm_loss_and_preserves_claim(env, gain, gain_first):
    """Both orders serialize: only gain-before-loss is a continuous read period."""
    env.seed()
    env.group()
    if gain in ("new_member", "invite"):
        env.run("DELETE FROM group_members WHERE group_id=? AND user_id=?", (cases.GROUP, cases.FRIEND))
    principal = "group" if gain in ("new_member", "invite") else "group_admins"
    env.grant("group-arm", principal, cases.GROUP)
    env.grant("direct-arm", "user", cases.FRIEND)
    env.rt.groups.now = lambda: cases.at(5)
    env.ask("continuous-old", 1)
    env.signal("continuous-old")
    claimed = env.store.claim(cases.NB, cases.FRIEND, claim_token="continuous-token", now=cases.at(2))
    assert claimed is not None
    if gain == "invite":
        env.rt.groups.issue_invite(cases.GROUP, token="continuous-invite", actor_id=cases.OWNER)

    def add_arm(db):
        groups = copy(env.rt.groups)
        groups.database = db
        if gain in ("new_member", "promote"):
            groups.upsert_member(cases.GROUP, cases.FRIEND,
                                 role="member" if gain == "new_member" else "admin", added_by=cases.OWNER)
        elif gain == "invite":
            assert groups.join_by_invite("continuous-invite", user_id=cases.FRIEND) is not None
        else:
            groups.transfer_group_owner(cases.GROUP, new_owner_id=cases.FRIEND, actor_id=cases.OWNER)

    def revoke(db):
        groups = copy(env.rt.groups)
        groups.database = db
        assert groups.delete_user_grant(cases.NB, cases.FRIEND)

    blocked = _controlled_pair(env, add_arm if gain_first else revoke, revoke if gain_first else add_arm)
    assert blocked, "a read-path change crossed the other's uncommitted notebook root"
    if gain_first:
        assert env.rows("SELECT claim_token FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?",
                        (cases.NB, cases.FRIEND)) == [{"claim_token": "continuous-token"}]
        assert env.cursor()[1] == "continuous-old"
        assert env.rows("SELECT 1 FROM memory_suggest_read_fences WHERE notebook_id=? AND owner_id=?",
                        (cases.NB, cases.FRIEND)) == []
    else:
        assert not env.has_job()
        env.ask("after-regrant", 7)
        env.signal("continuous-old", minute=8)
        boundary, cursor_id = env.cursor()
        rows = env.store.clarified_scan(cases.FRIEND, cases.NB, cursor_created_at=boundary.isoformat(),
                                        cursor_id=cursor_id, inclusive=True, limit=20)
        assert [row["id"] for row in rows] == ["after-regrant"]


def test_enable_samples_service_clock_after_waiting_for_read_loss(env):
    from app.services.memory_suggest_service import MemorySuggestService

    env.seed()
    env.store.set_memory_mode(cases.FRIEND, "manual", now=cases.at(0))
    env.grant("original", "user", cases.FRIEND)
    env.ask("old-before-enable", 4)
    env.rt.groups.now = lambda: cases.at(5)
    clock = {"now": cases.at(3)}
    sampled = []

    def now():
        sampled.append(clock["now"])
        return clock["now"]

    def revoke(db):
        groups = copy(env.rt.groups)
        groups.database = db
        groups.delete_user_grant(cases.NB, cases.FRIEND)

    def enable(db):
        store = copy(env.store)
        store.database = db
        settings = SimpleNamespace(auto_memory_suggest_enabled=True, auto_memory_suggest_clarified_enabled=True)
        MemorySuggestService(store, None, settings, now).set_mode(cases.FRIEND, "suggest")

    assert _controlled_pair(env, revoke, enable, on_blocked=lambda: clock.update(now=cases.at(6)))
    assert sampled == [cases.at(6)]
    assert env.store.user_state(cases.FRIEND)["enabled_at"] == cases.at(6)
    env.grant("restored", "user", cases.FRIEND)
    env.ask("new-after-enable", 7)
    env.signal("old-before-enable", minute=8)
    boundary, cursor_id = env.cursor()
    rows = env.store.clarified_scan(cases.FRIEND, cases.NB, cursor_created_at=boundary.isoformat(),
                                    cursor_id=cursor_id, inclusive=True, limit=20)
    assert [row["id"] for row in rows] == ["new-after-enable"]


def test_uncommitted_enable_cannot_escape_the_read_loss_candidate_set(env):
    env.seed()
    env.store.set_memory_mode(cases.FRIEND, "manual", now=cases.at(0))
    env.grant("original", "user", cases.FRIEND)
    env.ask("old-during-enable", 4)
    env.rt.groups.now = lambda: cases.at(5)

    def enable(db):
        store = copy(env.store)
        store.database = db
        store.set_memory_mode(cases.FRIEND, "suggest", now=cases.at(3))

    def revoke(db):
        groups = copy(env.rt.groups)
        groups.database = db
        groups.delete_user_grant(cases.NB, cases.FRIEND)

    _controlled_pair(env, enable, revoke)
    env.grant("restored", "user", cases.FRIEND)
    env.ask("new", 11)
    assert env.signal("old-during-enable", minute=12) == 1
    boundary, cursor_id = env.cursor()
    rows = env.store.clarified_scan(cases.FRIEND, cases.NB, cursor_created_at=boundary.isoformat(),
                                    cursor_id=cursor_id, inclusive=True, limit=20)
    assert [row["id"] for row in rows] == ["new"]


def test_read_loss_mode_barrier_keeps_independent_notebooks_parallel(env):
    env.seed()
    other = "nb-independent"
    env.run("INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (other, "n", cases.OWNER, "ready", cases.NOW, cases.NOW))
    env.grant("one", "user", cases.FRIEND)
    env.run("INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
            ("two", other, "user", cases.FRIEND, "viewer", cases.OWNER, cases.NOW))

    def revoke(db, notebook):
        groups = copy(env.rt.groups)
        groups.database = db
        groups.delete_user_grant(notebook, cases.FRIEND)

    _controlled_pair(env, lambda db: revoke(db, cases.NB), lambda db: revoke(db, other),
                     must_run_in_parallel=True)
