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
    from app.repositories.postgres.repository import PostgresRepository

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
