"""``vacuum_until_all_visible``: the precondition every visibility-map plan pin
settles on (``tests/postgres/conftest.py``).

An ANALYZE -- an autovacuum worker's above all -- keeps its snapshot for its
whole sample, so one that began before a seed committed keeps the seed's
pages from going all-visible until it is done. In the lane that is an
autoanalyze of a big or bloated table of the same database (the catalogs
grow with every test schema), and it outlasted the old 10 s bound. The
helper waits such maintenance out for longer, and still fails at once --
naming the session -- on an ordinary transaction that holds the horizon. In
both cases it never returns on an incomplete map.
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

# A sample of 300 x 10000 rows over two md5 columns: the ANALYZE takes about
# 1.5 s locally, far past the 0.3 s base bound the tests use.
_SLOW_ANALYZE_ROWS = 300_000
_BASE_BOUND = 0.3


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


def test_an_analyze_holding_the_horizon_is_waited_out(postgres_settings):
    url = postgres_settings.database_url
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE settle_slow_analyze WITH (autovacuum_enabled = off) AS "
            "SELECT g AS id, md5(g::text) AS a, md5((g * 3)::text) AS b "
            "FROM generate_series(1, %s) g",
            (_SLOW_ANALYZE_ROWS,),
        )
        connection.execute(
            "ALTER TABLE settle_slow_analyze ALTER a SET STATISTICS 10000, "
            "ALTER b SET STATISTICS 10000"
        )
    analyzer = psycopg.connect(url, autocommit=True)
    analyzer_pid = analyzer.info.backend_pid
    analyzing = threading.Thread(
        target=analyzer.execute, args=("ANALYZE settle_slow_analyze",), daemon=True
    )
    try:
        analyzing.start()
        with psycopg.connect(url, autocommit=True) as watcher:
            deadline = time.monotonic() + 10
            while not watcher.execute(
                "SELECT 1 FROM pg_stat_progress_analyze WHERE pid = %s", (analyzer_pid,)
            ).fetchone():
                assert analyzing.is_alive() and time.monotonic() < deadline, (
                    "the ANALYZE never showed up in pg_stat_progress_analyze")
                time.sleep(0.01)
        # The seed commits while the ANALYZE's snapshot is open: it holds the horizon.
        _seed_target(url)
        started = time.monotonic()
        vacuum_until_all_visible(url, "settle_target", timeout=_BASE_BOUND,
                                 maintenance_timeout=60)
        waited = time.monotonic() - started
        # A full map means the ANALYZE's snapshot is gone: it has finished.
        analyzing.join(5)
        assert not analyzing.is_alive()
    finally:
        analyzing.join(60)
        analyzer.close()
    pages, all_visible = _relation_map(url, "settle_target")
    assert pages > 0 and all_visible == pages, (pages, all_visible)
    assert waited > _BASE_BOUND, (
        f"the ANALYZE ended within the base bound ({waited:.2f}s): the test no longer "
        "makes the helper wait past it; seed a slower ANALYZE")


def test_an_ordinary_transaction_holding_the_horizon_fails_at_the_base_bound(
    postgres_settings,
):
    url = postgres_settings.database_url
    with psycopg.connect(url, autocommit=True) as holder:
        holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        holder.execute("SELECT txid_current()")
        holder_pid = holder.info.backend_pid
        try:
            _seed_target(url)
            started = time.monotonic()
            with pytest.raises(AssertionError) as failure:
                vacuum_until_all_visible(url, "settle_target", timeout=_BASE_BOUND,
                                         maintenance_timeout=60)
            waited = time.monotonic() - started
        finally:
            holder.execute("ROLLBACK")
    message = str(failure.value)
    assert "visibility map of settle_target still incomplete" in message, message
    assert f"'pid': {holder_pid}" in message, message
    assert "'blocking': True" in message and "'maintenance': False" in message, message
    assert waited < 5, f"an ordinary holder must not get the maintenance budget ({waited:.1f}s)"
