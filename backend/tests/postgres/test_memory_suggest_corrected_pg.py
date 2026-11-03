"""PostgreSQL twins of the corrected-signal observable contracts."""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from app.core.request_context import set_request_user
from tests import memory_suggest_corrected_cases as cases
from tests.memory_suggest_job_cases import Env

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def env(postgres_settings):
    from app.services.postgres_repository import PostgresRepository
    repo = PostgresRepository(postgres_settings)
    database = repo._runtime.database
    def run(sql, params=()):
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)
    def rows(sql, params=()):
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]
    try:
        yield Env(runtime=repo._runtime, run=run, rows=rows, json=Jsonb)
    finally:
        set_request_user(None)
        repo.close()


@pytest.mark.parametrize("case", cases.CASES)
def test_corrected_contract(env, case):
    cases.CASES[case](env)
