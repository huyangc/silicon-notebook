"""Global Ask turn retention after a participant library is deleted -- PostgreSQL.

Runs the SAME case list as the SQLite twin (``tests/test_global_ask_retention.py``);
the scenarios live in ``tests/global_ask_retention_cases.py``. The plan pin
below is PostgreSQL-only: the stamp in the delete transaction must read the
matching jobs through ``idx_global_ask_jobs_participants`` rather than parse
every payload.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.global_ask_retention import participant_sql
from tests.global_ask_retention_cases import CASE_IDS, CASES


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_global_ask_retention"),
]


@pytest.fixture
def repo(postgres_scope, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(Settings(
        _env_file=None,
        database_url=postgres_scope.url,
        storage_dir=str(tmp_path),
        event_log_enabled=False,
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=5,
        postgres_lock_timeout_seconds=2,
    ))
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_retention_contract(repo, case):
    case(repo)


def test_the_detail_read_and_a_deletion_take_their_locks_in_one_order(repo, postgres_scope):
    """The activity detail (``guarded_admin_job_record``) and the notebook
    delete's finalize must not deadlock.

    Finalize locks ``notebooks FOR UPDATE`` first and stamps
    ``global_ask_jobs`` later. The interleaving that used to deadlock: a
    finalize holding the notebook while the detail read, holding the job row
    ``FOR SHARE``, waits for that notebook -- then finalize's stamp waits for
    the job row. A direct connection plays finalize's two steps around the
    detail read; the read must be parked on the NOTEBOOK (holding no job lock),
    so the stamp goes through and both finish without ``deadlock_detected``.
    """
    import threading
    import time

    import psycopg

    from tests.global_ask_retention_cases import OWNER, build

    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], new_conversation=True)
    result: dict = {}

    def detail_read():
        try:
            with world.store.guarded_admin_job_record("job-1", OWNER, reader_id=OWNER) as record:
                result["record"] = record
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            result["error"] = exc

    with psycopg.connect(postgres_scope.url) as finalize, \
            psycopg.connect(postgres_scope.url, autocommit=True) as probe:
        finalize.execute("SET lock_timeout = '20s'")
        finalize.execute("SELECT id FROM notebooks WHERE id='nb-b' FOR UPDATE")
        reader = threading.Thread(target=detail_read)
        reader.start()
        deadline = time.monotonic() + 30
        parked = False
        while time.monotonic() < deadline and reader.is_alive():
            parked = probe.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND query ILIKE '%%FROM notebooks%%'"
            ).fetchone()[0] >= 1
            if parked:
                break
            time.sleep(0.01)
        assert parked, "the detail read never waited on the notebook the deletion holds"
        # Finalize's stamp: under the old order the reader already held this
        # row FOR SHARE, and the server would abort one side with a deadlock.
        finalize.execute(
            "UPDATE global_ask_jobs SET retained_until=%s WHERE id='job-1'",
            ("2027-04-17T00:00:00+00:00",),
        )
        finalize.commit()
    reader.join(30)
    assert "error" not in result, result.get("error")
    assert result["record"] is not None and result["record"]["job"].job_id == "job-1"


class _Recorder:
    """Stands in for a store's ``database``: hands out the real connections and
    records every statement, so a plan pin explains the EXACT text the store
    sends rather than a copy that could drift from it."""

    def __init__(self, database):
        self._database = database
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self._database, name)

    def connect(self):
        recorder = self
        inner = self._database.connect()

        class _Context:
            def __enter__(self_inner):
                connection = inner.__enter__()

                class _Proxy:
                    def execute(self, sql, params=()):
                        recorder.statements.append((sql, tuple(params)))
                        return connection.execute(sql, params)

                    def __getattr__(self, name):
                        return getattr(connection, name)

                return _Proxy()

            def __exit__(self_inner, *exc):
                return inner.__exit__(*exc)

        return _Context()


def _plan(repo, sql, params):
    with repo._runtime.database.connect() as db:
        db.execute("SET LOCAL enable_seqscan = off")
        return "\n".join(
            row["QUERY PLAN"] for row in db.execute("EXPLAIN " + sql, params).fetchall()
        )


def test_the_conversation_gate_probes_the_partial_retention_index(repo, monkeypatch):
    """The conversation read gate's decisive probe ("does this conversation
    hold an expired turn") is answered from ``idx_global_ask_jobs_retained``
    -- on the owner's conversation list and on the administrator's per-user
    conversation count alike -- so a deployment without a single stamp pays an
    index probe that finds nothing, not a walk over each conversation's jobs."""
    from tests.global_ask_retention_cases import OWNER, build

    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a"], new_conversation=True)

    store_recorder = _Recorder(world.store.database)
    monkeypatch.setattr(world.store, "database", store_recorder)
    world.store.list_conversations(OWNER, 50, 0)
    listing = next(sql for sql in store_recorder.statements
                   if "FROM global_ask_conversations c WHERE c.user_id" in sql[0])
    assert "idx_global_ask_jobs_retained" in _plan(repo, *listing)

    queries = repo._runtime.queries
    query_recorder = _Recorder(queries.database)
    monkeypatch.setattr(queries, "database", query_recorder)
    queries.list_user_usage()
    count = next(sql for sql in query_recorder.statements
                 if "FROM global_ask_conversations gc" in sql[0])
    assert "idx_global_ask_jobs_retained" in _plan(repo, *count)


def test_the_stamp_reads_participants_through_the_gin_index(repo):
    with repo._runtime.database.connect() as db:
        db.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            row["QUERY PLAN"] for row in db.execute(
                "EXPLAIN UPDATE global_ask_jobs SET retained_until=%s WHERE "
                + participant_sql(dialect="postgres", marker="%s"),
                ("2026-10-19T00:00:00+00:00", "nb-x"),
            ).fetchall()
        )
    assert "idx_global_ask_jobs_participants" in plan
