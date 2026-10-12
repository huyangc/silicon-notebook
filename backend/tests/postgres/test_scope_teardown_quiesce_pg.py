"""The isolated-schema teardown never drops a schema under in-flight work.

``_isolated_postgres_scope`` ends with ``DROP SCHEMA ... CASCADE``, which takes
an ACCESS EXCLUSIVE lock on every table in the schema, one table at a time.
Fire-and-forget work that a test started -- the Memory KG ingest that
``create_memory_from_answer`` queues on the KG job pool is the case CI hit
(``test_promotion_provenance_pg``, PR #844 / #846) -- can still be inside a
transaction at that moment. When that transaction holds a row lock on a table
the drop has not reached yet and then writes a table the drop already holds,
the two wait on each other and PostgreSQL aborts the drop with
``DeadlockDetected`` in the teardown of an unrelated-looking test.

Each case here builds exactly that interleaving with a job on one of the
process pools the teardown drains: the job writes the table created second,
then watches (bounded) for the drop to start waiting on it, then writes the
table created first, which ``DROP SCHEMA`` locks first. Without the drain the
drop starts while the job holds its lock, so the job sees it waiting and the
next insert closes the cycle; with the drain the drop only starts after the
job has committed and the pool is idle.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from tests.postgres.conftest import _isolated_postgres_scope

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_scope_teardown_quiesce"),
]

# How long the job keeps its lock while watching for the drop. Long enough for
# an unguarded teardown (which issues the drop within milliseconds of the
# ``with`` block ending) to be seen waiting; it is the whole extra cost of a
# guarded run, where no drop ever appears.
_DROP_WATCH_SECONDS = 2.0


def _drop_is_waiting_on(base_url: str, schema: str) -> bool:
    import psycopg

    # pg_locks is server-wide: without the database filter, a waiting lock in
    # another database on the same server whose relation OID happens to equal
    # one of this schema's tables would join to it and read as our drop.
    with psycopg.connect(base_url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_locks l "
            "JOIN pg_class c ON c.oid = l.relation "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE l.database = (SELECT oid FROM pg_database "
            "WHERE datname = current_database()) "
            "AND n.nspname = %s AND NOT l.granted "
            "AND l.mode = 'AccessExclusiveLock' LIMIT 1",
            (schema,),
        ).fetchone()
    return row is not None


def _watch_for_waiting_drop(base_url: str, schema: str) -> bool:
    deadline = time.monotonic() + _DROP_WATCH_SECONDS
    while time.monotonic() < deadline:
        if _drop_is_waiting_on(base_url, schema):
            return True
        time.sleep(0.02)
    return False


def _submit_on_kg_job_pool(job) -> None:
    from app.services.kg import scheduler as kg_scheduler

    kg_scheduler.submit_job(job)


def _submit_on_light_maintenance_pool(job) -> None:
    from app.services import background_jobs

    background_jobs.submit(job, name="catalog-scope-teardown-quiesce")


@pytest.mark.parametrize(
    "submit",
    [_submit_on_kg_job_pool, _submit_on_light_maintenance_pool],
    ids=["kg_job_pool", "light_maintenance_pool"],
)
def test_the_schema_is_dropped_only_after_pool_work_has_committed(submit):
    import psycopg

    base_url = os.environ["TEST_POSTGRES_URL"]
    holding = threading.Event()
    outcome: dict[str, object] = {}

    with _isolated_postgres_scope(base_url) as scope:
        with psycopg.connect(scope.url, autocommit=True) as conn:
            conn.execute("CREATE TABLE locked_first (id int)")
            conn.execute("CREATE TABLE locked_second (id int)")

        def job() -> None:
            try:
                with psycopg.connect(scope.url) as conn:
                    conn.execute("INSERT INTO locked_second VALUES (1)")
                    holding.set()
                    outcome["drop_seen_waiting"] = _watch_for_waiting_drop(
                        base_url, scope.schema
                    )
                    conn.execute("INSERT INTO locked_first VALUES (1)")
                    conn.commit()
                outcome["committed"] = True
            except BaseException as exc:  # recorded: pools swallow job errors
                outcome["error"] = type(exc).__name__
                raise
            finally:
                holding.set()

        submit(job)
        assert holding.wait(10), "the pool never started the job"
        # leaving the block runs the teardown under test while the job still
        # holds its lock on ``locked_second``

    assert outcome == {"drop_seen_waiting": False, "committed": True}
    with psycopg.connect(base_url, autocommit=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (scope.schema,)
        ).fetchone() is None
