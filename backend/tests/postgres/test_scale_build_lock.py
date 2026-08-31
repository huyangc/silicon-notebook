"""W-CLI T-W1 — the real cross-process per-notebook scale-build lock.

These are the properties only a live server can demonstrate: two sessions
cannot both hold the claim, a dead session surrenders it, and the holder can
prove to itself that it still owns it before the destructive swap.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.repositories.postgres import database as database_module
from app.repositories.postgres.database import (
    PostgresDatabase,
    PostgresDatabaseClosedError,
)
from app.repositories.scale_build_lock import (
    ScaleBuildLock,
    advisory_lock_key,
    advisory_lock_oid,
)

pytestmark = pytest.mark.postgres_integration

ROOT = Path(__file__).resolve().parents[3]
NAMESPACE = database_module._SCALE_BUILD_LOCK_NAMESPACE


def _second_process(postgres_settings) -> PostgresDatabase:
    """A separate pool and separate sessions — the offline CLI's shape."""
    return PostgresDatabase(postgres_settings, ROOT)


def _holder_pids(postgres_database, notebook_id: str) -> list[int]:
    key = advisory_lock_key(notebook_id)
    with postgres_database.connect() as conn:
        rows = conn.execute(
            "SELECT pid FROM pg_locks "
            "WHERE locktype = 'advisory' AND granted "
            "AND classid = %s::oid AND objid = %s::oid",
            (advisory_lock_oid(NAMESPACE), advisory_lock_oid(key)),
        ).fetchall()
    return sorted(int(row["pid"]) for row in rows)


def _negative_key_notebook_id() -> str:
    """An identifier whose normalized advisory key is negative.

    Roughly half of all identifiers land here; the two-argument advisory form
    plus the unsigned ``pg_locks`` rendering is what keeps them verifiable.
    """
    for index in range(1000):
        candidate = f"nb-negative-{index}"
        if advisory_lock_key(candidate) < 0:
            return candidate
    raise AssertionError("no negative advisory key found")


def test_a_second_process_cannot_take_a_held_scale_build_lock(
    postgres_database, postgres_settings
):
    other = _second_process(postgres_settings)
    handle = postgres_database.try_scale_build_lock("nb-exclusive")
    assert handle is not None
    try:
        assert other.try_scale_build_lock("nb-exclusive") is None
        # A different notebook is unaffected: the claim is per-notebook, not a
        # database-wide maintenance gate.
        neighbour = other.try_scale_build_lock("nb-neighbour")
        assert neighbour is not None
        neighbour.release()
    finally:
        handle.release()
        other.close()

    regained = postgres_database.try_scale_build_lock("nb-exclusive")
    assert regained is not None
    regained.release()


def test_a_held_lock_verifies_itself_and_a_released_one_does_not(
    postgres_database,
):
    handle = postgres_database.try_scale_build_lock("nb-verify")
    assert isinstance(handle, ScaleBuildLock)
    assert handle.supported is True

    assert handle.verify_held() is True
    assert handle.verify_held() is True  # re-verification is repeatable

    handle.release()
    assert handle.verify_held() is False
    handle.release()  # idempotent


def test_a_negative_advisory_key_is_still_self_verifiable(postgres_database):
    """``pg_locks`` stores the key unsigned. Comparing against the signed value
    would report "not held" for half of all notebooks — every one of which
    would then refuse its own swap after a completed build."""
    notebook_id = _negative_key_notebook_id()
    handle = postgres_database.try_scale_build_lock(notebook_id)
    assert handle is not None
    try:
        assert advisory_lock_key(notebook_id) < 0
        assert handle.verify_held() is True
        assert len(_holder_pids(postgres_database, notebook_id)) == 1
    finally:
        handle.release()
    assert _holder_pids(postgres_database, notebook_id) == []


def test_killing_the_lock_session_releases_the_claim(
    postgres_database, postgres_settings
):
    """The scenario ``verify_held`` exists for: a managed instance's idle
    reaper, a failover or an operator terminates the holder, the advisory lock
    is released silently, and the build must not go on to swap."""
    handle = postgres_database.try_scale_build_lock("nb-terminated")
    assert handle is not None
    pids = _holder_pids(postgres_database, "nb-terminated")
    assert len(pids) == 1

    with postgres_database.connect() as conn:
        conn.execute("SELECT pg_terminate_backend(%s)", (pids[0],))
        conn.commit()

    assert handle.verify_held() is False
    other = _second_process(postgres_settings)
    try:
        regained = other.try_scale_build_lock("nb-terminated")
        assert regained is not None
        regained.release()
    finally:
        other.close()
        handle.release()


def test_the_lock_session_is_named_and_keeps_its_idle_reaper_disabled(
    postgres_database,
):
    """Both settings are applied AFTER ``_restore_session_defaults``' RESET ALL.
    Moving either above it leaves the session reset to the pool defaults —
    which is exactly how a silently reaped lock session happens."""
    handle = postgres_database.try_scale_build_lock("nb-session")
    assert handle is not None
    try:
        pids = _holder_pids(postgres_database, "nb-session")
        with postgres_database.connect() as conn:
            names = conn.execute(
                "SELECT application_name FROM pg_stat_activity WHERE pid = %s",
                (pids[0],),
            ).fetchone()
        assert names["application_name"] == "silicon-notebook-scale-build-lock"

        # Read on the holder's own session: the GUC is session scoped.
        connection = handle._connection
        if connection.info.server_version >= 140000:
            row = connection.execute("SHOW idle_session_timeout").fetchone()
            connection.commit()
            assert row["idle_session_timeout"] == "0"
    finally:
        handle.release()


def test_scale_build_lock_sessions_are_bounded(postgres_database):
    """Each claim pins one non-pooled connection for the whole build; the
    budget is a connection budget, and exhausting it reads as "busy"."""
    capacity = postgres_database._scale_build_lock_capacity
    handles = [
        postgres_database.try_scale_build_lock(f"nb-budget-{index}")
        for index in range(capacity)
    ]
    try:
        assert all(handle is not None for handle in handles)
        assert postgres_database.try_scale_build_lock("nb-budget-over") is None
    finally:
        for handle in handles:
            if handle is not None:
                handle.release()

    # The budget comes back with the handles.
    extra = postgres_database.try_scale_build_lock("nb-budget-over")
    assert extra is not None
    extra.release()


def test_a_refused_claim_returns_its_session_budget(postgres_database):
    """A lock somebody else holds must not leak this process's budget — a leak
    would turn one busy notebook into a permanently unbuildable service."""
    held = postgres_database.try_scale_build_lock("nb-refused")
    assert held is not None
    try:
        for _ in range(postgres_database._scale_build_lock_capacity + 3):
            assert postgres_database.try_scale_build_lock("nb-refused") is None
        spare = postgres_database.try_scale_build_lock("nb-refused-other")
        assert spare is not None
        spare.release()
    finally:
        held.release()


def test_scale_build_lock_after_close_never_connects(
    postgres_database, monkeypatch
):
    calls: list[str] = []

    def refuse(*args, **kwargs):
        calls.append("connect")
        raise AssertionError("closed database must not open a lock session")

    monkeypatch.setattr(
        database_module._SafeDiagnosticConnection, "connect", refuse
    )
    postgres_database.close()

    with pytest.raises(PostgresDatabaseClosedError):
        postgres_database.try_scale_build_lock("nb-after-close")
    assert calls == []


def test_concurrent_claimants_produce_exactly_one_winner(postgres_database):
    """The claim is the thing that makes "one builder per notebook" true, so
    it is asserted under real contention rather than inferred."""
    winners: list[ScaleBuildLock] = []
    winners_lock = threading.Lock()
    start = threading.Event()

    def contend() -> None:
        start.wait(timeout=5)
        handle = postgres_database.try_scale_build_lock("nb-contended")
        if handle is not None:
            with winners_lock:
                winners.append(handle)

    workers = [threading.Thread(target=contend) for _ in range(6)]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()

    try:
        assert len(winners) == 1
        assert winners[0].verify_held() is True
    finally:
        for handle in winners:
            handle.release()
