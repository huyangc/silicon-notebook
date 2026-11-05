"""PostgreSQL twins of bounded suggestion expiry through maintenance (T13)."""
from __future__ import annotations

import itertools

import pytest
from psycopg.types.json import Jsonb

from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.memory_suggest_store import MemorySuggestStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
from tests import memory_suggest_adoption_cases as adoption
from tests import memory_suggest_expiry_cases as cases
from tests import memory_suggest_write_cases as writes

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def world(postgres_database):
    database = postgres_database
    assert PostgresMigrator(database).migrate() == POSTGRES_SCHEMA_MANIFEST.postgres_version
    counter = itertools.count(1)

    def new_id(prefix):
        return f"{prefix}-expiry-{next(counter)}"

    def run(sql, params=()):
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql, params=()):
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    return adoption.build_world(writes.Env(
        store=MemoryStore(database, new_id=new_id, now=lambda: writes.NOW),
        suggest=MemorySuggestStore(database, new_id=new_id),
        run=run, rows=rows, json=Jsonb,
    ))


def test_30_days_to_rejected_expired_revision_and_event(world):
    cases.assert_30_days_to_rejected_expired_revision_and_event(world)


def test_adopted_and_manual_and_agent_candidates_untouched(world):
    cases.assert_adopted_and_manual_and_agent_candidates_untouched(world)


def test_page_limit_500_idempotent(world):
    cases.assert_page_limit_500_idempotent(world)


def test_expiry_runs_when_deployment_switch_off(world):
    cases.assert_expiry_runs_when_deployment_switch_off(world)
