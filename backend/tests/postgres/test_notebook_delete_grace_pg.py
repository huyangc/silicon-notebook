"""The one-day deletion window on PostgreSQL.

Runs the exact scenarios of ``tests/test_notebook_delete_grace.py`` (defined
once in ``tests/notebook_delete_grace_cases.py``) against the production
PostgreSQL repository, plus the races only real concurrent transactions can
stage: cancel against promotion in both orders (with the two callers' clocks
disagreeing about the deadline), two sweepers promoting the same window, and
an export whose snapshot predates the promotion and the Memory delete.

Each race parks one transaction right after its compare-and-set statement
(still uncommitted, row lock held) through ``_PausingDatabase`` -- a wrapper
around the production database that only adds the pause, never any SQL of
its own -- and runs the other side on a second connection.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable

import pytest

from app.repositories.ports import NotebookDeleteDueError
from tests.notebook_delete_grace_cases import (
    CASES,
    DEADLINE,
    EMBED_DIM,
    T0,
    build_world,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_delete_grace"),
]


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    postgres_settings.postgres_pool_max_size = 4
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_delete_grace(repo, monkeypatch, case):
    CASES[case](build_world(repo, postgres=True, monkeypatch=monkeypatch))


# ------------------------------------------------------------------ races


class _PausingConnection:
    """Forwards everything to the real connection; after the first statement
    ``matches(sql)`` accepts, signals ``paused`` and waits for ``resume``."""

    def __init__(self, real: Any, gate: "_Gate") -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_gate", gate)

    def execute(self, sql: str, params: Any = None) -> Any:
        result = self._real.execute(sql, params)
        self._gate.after(sql)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._real, name, value)


class _Gate:
    def __init__(self, matches: Callable[[str], bool]) -> None:
        self.matches = matches
        self.paused = threading.Event()
        self.resume = threading.Event()
        self._fired = False

    def after(self, sql: str) -> None:
        if not self._fired and self.matches(sql):
            self._fired = True
            self.paused.set()
            assert self.resume.wait(20), "the race was never released"


class _PausingDatabase:
    def __init__(self, real: Any, gate: _Gate) -> None:
        self._real = real
        self._gate = gate

    @contextmanager
    def write(self, *args: Any, **kwargs: Any):
        with self._real.write(*args, **kwargs) as connection:
            yield _PausingConnection(connection, self._gate)

    @contextmanager
    def connect(self, *args: Any, **kwargs: Any):
        with self._real.connect(*args, **kwargs) as connection:
            yield _PausingConnection(connection, self._gate)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _paused_twin(store: Any, gate: _Gate) -> Any:
    """The same production store class over the pausing database."""
    twin = object.__new__(type(store))
    twin.__dict__.update(store.__dict__)
    twin.database = _PausingDatabase(store.database, gate)
    return twin


def _run(fn: Callable[[], Any]) -> tuple[threading.Thread, list]:
    box: list = []

    def target() -> None:
        try:
            box.append(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            box.append(("raised", exc))

    thread = threading.Thread(target=target)
    thread.start()
    return thread, box


def _blocked_on_the_row(repo: Any, *, timeout: float = 10.0) -> None:
    """Wait until the second side of the race is queued behind the parked
    transaction's row lock: another backend of this database, active, waiting
    on a lock, and running the window's CAS (``UPDATE notebooks SET``) -- not
    just any lock wait somewhere, so the test really covers "queued, then
    re-evaluated against the committed row" (same filter as
    ``test_concurrency.py``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with repo._runtime.database.connect() as db:
            waiting = db.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE pid<>pg_backend_pid() AND datname=current_database() "
                "AND wait_event_type='Lock' AND state='active' "
                "AND query ILIKE %s",
                ("%UPDATE notebooks SET%",),
            ).fetchone()["n"]
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the second transaction never queued on the row lock")


def _is_cas(sql: str) -> bool:
    return sql.lstrip().startswith("UPDATE notebooks SET")


@pytest.mark.parametrize("first", ["cancel", "promote"])
def test_cancel_racing_the_promotion_lands_exactly_one(repo, monkeypatch, first):
    """Cancel (its clock: one second before the deadline) against the
    promotion (its clock: the deadline) on two connections, in both orders.
    The first CAS commits; the second, queued on the row lock, re-evaluates
    against the committed row and matches nothing -- so exactly one lands,
    each side reports it consistently, and no extra job row appears."""
    world = build_world(repo, postgres=True, monkeypatch=monkeypatch)
    world.request()
    store = repo._runtime.notebook_delete_jobs
    gate = _Gate(_is_cas)
    parked = _paused_twin(store, gate)
    cancel_at, promote_at = DEADLINE - timedelta(seconds=1), DEADLINE
    if first == "cancel":
        first_thread, first_box = _run(lambda: parked.cancel_pending(world.doomed, now=cancel_at))
        assert gate.paused.wait(10)
        second_thread, second_box = _run(
            lambda: store.promote_due_pending(world.doomed, now=promote_at)
        )
    else:
        first_thread, first_box = _run(
            lambda: parked.promote_due_pending(world.doomed, now=promote_at)
        )
        assert gate.paused.wait(10)
        second_thread, second_box = _run(lambda: store.cancel_pending(world.doomed, now=cancel_at))
    _blocked_on_the_row(repo)
    gate.resume.set()
    first_thread.join(20)
    second_thread.join(20)
    [(first_kind, first_value)] = first_box
    [(second_kind, second_value)] = second_box
    if first == "cancel":
        assert (first_kind, first_value) == ("ok", "cancelled")
        assert (second_kind, second_value) == ("ok", None)
        assert world.status()["status"] == "draft"
        assert world.jobs() == []
    else:
        assert first_kind == "ok" and first_value["status"] == "queued"
        assert second_kind == "raised" and isinstance(second_value, NotebookDeleteDueError)
        assert world.status()["status"] == "deleting"
        assert [row["status"] for row in world.jobs()] == ["queued"]


def test_two_sweepers_promote_a_window_once(repo, monkeypatch):
    world = build_world(repo, postgres=True, monkeypatch=monkeypatch)
    world.request()
    store = repo._runtime.notebook_delete_jobs
    gate = _Gate(_is_cas)
    parked = _paused_twin(store, gate)
    first_thread, first_box = _run(lambda: parked.promote_due_pending(world.doomed, now=DEADLINE))
    assert gate.paused.wait(10)
    second_thread, second_box = _run(lambda: store.promote_due_pending(world.doomed, now=DEADLINE))
    _blocked_on_the_row(repo)
    gate.resume.set()
    first_thread.join(20)
    second_thread.join(20)
    assert first_box[0][0] == "ok" and first_box[0][1]["status"] == "queued"
    assert second_box == [("ok", None)]
    assert [row["status"] for row in world.jobs()] == ["queued"]


def test_an_export_begun_before_the_promotion_is_complete_and_blocks_nothing(repo, monkeypatch):
    """The export's snapshot is taken inside the window; the promotion and a
    phase-3-style delete of every Memory row then commit while the export is
    still paging (they are not blocked by it). The file still holds every
    item: the deadline means "start before it"."""
    world = build_world(repo, postgres=True, monkeypatch=monkeypatch)
    world.request()
    memory_store = repo._runtime.memory_store
    gate = _Gate(lambda sql: "AS window_open" in sql)
    parked = _paused_twin(memory_store, gate)
    thread, box = _run(
        lambda: parked.memory_export_snapshot(
            world.doomed, world.member.id, page_size=1, max_bytes=10**6, now=T0
        )
    )
    assert gate.paused.wait(10)
    started = time.monotonic()
    assert repo._runtime.notebook_delete_jobs.promote_due_pending(world.doomed, now=DEADLINE)
    world.sql.write("DELETE FROM memory_items WHERE notebook_id=?", (world.doomed,))
    assert time.monotonic() - started < 5  # neither waited on the export
    gate.resume.set()
    thread.join(20)
    [(kind, snapshot)] = box
    assert kind == "ok", snapshot
    assert {item.id for item in snapshot.items} == {
        *world.confirmed, world.candidate, world.proposed,
    }


def test_the_transfer_claim_rides_a_session_that_is_not_reaped(repo, monkeypatch):
    """The window transfer's claim is a session advisory lock: its session
    turns ``idle_session_timeout`` off and carries keepalives and
    ``tcp_user_timeout`` (as the scale-build lock's does), so a hosted
    instance cannot reap the idle holder and release the claim silently. A
    second claim on the same key is refused while it is held and granted once
    it is released."""
    database = repo._runtime.database
    real = database._open_claim_connection
    opened: list = []

    def recording():
        connection = real()
        opened.append(connection)
        return connection

    monkeypatch.setattr(database, "_open_claim_connection", recording)
    claim = repo._runtime.memory_store.delete_window_transfer_claim
    with claim("nb-claim", "u-claim") as held:
        assert held
        [session] = opened
        setting = session.execute("SHOW idle_session_timeout").fetchone()
        assert next(iter(setting.values())) == "0"
        parameters = session.info.get_parameters()
        assert parameters.get("keepalives") == "1"
        assert parameters.get("tcp_user_timeout") == "6000"
        with claim("nb-claim", "u-claim") as second:
            assert not second
    with claim("nb-claim", "u-claim") as again:
        assert again
