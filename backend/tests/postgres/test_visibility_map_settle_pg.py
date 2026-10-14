"""``vacuum_until_all_visible``: the precondition every visibility-map plan pin
settles on (``tests/postgres/conftest.py``).

An ANALYZE -- an autovacuum worker's above all -- keeps its snapshot for its
whole sample, so one that began before a seed committed keeps the seed's
pages from going all-visible until it is done. In the lane that is an
autoanalyze of a big or bloated table of the same database (the catalogs
grow with every test schema), and it outlasted the old 10 s bound. The
helper waits such maintenance out for longer, and still fails at the base
bound -- naming the session -- on an ordinary transaction that holds the
horizon. In both cases it never returns on an incomplete map.

Nothing here is timed. The maintenance holder is a real ANALYZE held open by
an advisory lock: the analyzed table has an expression index whose function
waits for a shared advisory lock the test holds exclusively, so the ANALYZE
(listed in ``pg_stat_progress_analyze``, its snapshot taken) blocks until the
test releases the lock. The test releases it from the helper's
``on_maintenance_wait`` seam -- so it also proves the helper took that
branch, with the ANALYZE judged blocking maintenance.
"""
from __future__ import annotations

import threading
import time

import psycopg
import pytest

from tests.postgres.conftest import vacuum_until_all_visible

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_visibility_map_settle"),
]

_GATE_KEY = 7_311_015
_BASE_BOUND = 0.2


def _relation_map(url: str, table: str) -> tuple[int, int]:
    with psycopg.connect(url) as connection:
        return connection.execute(
            "SELECT relpages, relallvisible FROM pg_class WHERE oid = to_regclass(%s)",
            (table,),
        ).fetchone()


def _seed_target(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE settle_target AS SELECT g AS id FROM generate_series(1, 5000) g"
        )


def _wait_until(probe, what: str, deadline: float = 30.0) -> None:
    """Poll a condition the test is waiting for another session to reach
    (a lock wait, a progress row); the bound only turns a hang into a red."""
    end = time.monotonic() + deadline
    while not probe():
        assert time.monotonic() < end, f"never observed: {what}"
        time.sleep(0.01)


def test_an_analyze_holding_the_horizon_is_waited_out(postgres_settings):
    url = postgres_settings.database_url
    with psycopg.connect(url, autocommit=True) as setup:
        setup.execute(
            "CREATE FUNCTION settle_gate_wait(v int) RETURNS int IMMUTABLE "
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"PERFORM pg_advisory_lock_shared({_GATE_KEY}); "
            f"PERFORM pg_advisory_unlock_shared({_GATE_KEY}); "
            "RETURN v; END $$"
        )
        setup.execute(
            "CREATE TABLE settle_gate WITH (autovacuum_enabled = off) AS "
            "SELECT g AS id FROM generate_series(1, 100) g"
        )
        setup.execute("CREATE INDEX settle_gate_expr ON settle_gate (settle_gate_wait(id))")

    gate = psycopg.connect(url, autocommit=True)
    gate.execute("SELECT pg_advisory_lock(%s)", (_GATE_KEY,))
    analyzer = psycopg.connect(url, autocommit=True)
    analyzer_pid = analyzer.info.backend_pid
    analyzing = threading.Thread(
        target=analyzer.execute, args=("ANALYZE settle_gate",), daemon=True
    )
    later = psycopg.connect(url, autocommit=True)
    judged: list[list[dict]] = []
    released = False

    def on_maintenance_wait(holders):
        nonlocal released
        judged.append(holders)
        mine = [h for h in holders if h["pid"] == analyzer_pid]
        if not released and mine and mine[0]["blocking"] and mine[0]["maintenance"]:
            gate.execute("SELECT pg_advisory_unlock(%s)", (_GATE_KEY,))
            released = True

    try:
        analyzing.start()
        with psycopg.connect(url, autocommit=True) as watcher:
            # The ANALYZE has its snapshot and is parked on the gate.
            _wait_until(lambda: watcher.execute(
                "SELECT 1 FROM pg_locks l JOIN pg_stat_progress_analyze p "
                "ON p.pid = l.pid WHERE l.pid = %s AND l.locktype = 'advisory' "
                "AND NOT l.granted", (analyzer_pid,)).fetchone(),
                "the ANALYZE waiting on the advisory gate")
        _seed_target(url)
        # An ordinary transaction that took its xid after the seed: listed as
        # a holder, never blocking. READ COMMITTED, so between statements it
        # holds no snapshot -- a snapshot's xmin would be the oldest xid running
        # anywhere on the server, possibly older than the seed.
        later.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
        later.execute("SELECT txid_current()")
        later_pid = later.info.backend_pid

        vacuum_until_all_visible(url, "settle_target", timeout=_BASE_BOUND,
                                 maintenance_timeout=60,
                                 on_maintenance_wait=on_maintenance_wait)

        analyzing.join(30)
        assert not analyzing.is_alive()
    finally:
        if not released:
            gate.execute("SELECT pg_advisory_unlock(%s)", (_GATE_KEY,))
        analyzing.join(30)
        later.close()
        analyzer.close()
        gate.close()

    assert released, f"the helper never judged the ANALYZE blocking maintenance: {judged}"
    first = judged[0]
    assert any(h["pid"] == later_pid and not h["blocking"] and not h["maintenance"]
               for h in first), first
    pages, all_visible = _relation_map(url, "settle_target")
    assert pages > 0 and all_visible == pages, (pages, all_visible)


def test_an_ordinary_transaction_holding_the_horizon_fails_at_the_base_bound(
    postgres_settings,
):
    url = postgres_settings.database_url

    def on_maintenance_wait(holders):
        pytest.fail(f"an ordinary holder was given the maintenance wait: {holders}")

    with psycopg.connect(url, autocommit=True) as holder:
        holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        holder.execute("SELECT txid_current()")
        holder_pid = holder.info.backend_pid
        try:
            _seed_target(url)
            with pytest.raises(AssertionError) as failure:
                vacuum_until_all_visible(url, "settle_target", timeout=_BASE_BOUND,
                                         maintenance_timeout=60,
                                         on_maintenance_wait=on_maintenance_wait)
        finally:
            holder.execute("ROLLBACK")
    message = str(failure.value)
    assert "visibility map of settle_target still incomplete" in message, message
    assert f"'pid': {holder_pid}" in message, message
    assert "'blocking': True" in message and "'maintenance': False" in message, message
