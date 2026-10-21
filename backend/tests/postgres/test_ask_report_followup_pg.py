"""PostgreSQL twin of the report-followup scenarios in
``tests/test_ask_report_followup_service.py`` (plan 2026-10-21 T6).

The verdict a question about a deep report gets, and the restore of the
report's cited items, run here end to end against PostgreSQL's own stores:
the report row read through its gate, the two owner maps, the asker's
participant set, the production default ceiling the Ask entry installs, the
restore, and the receipt every such answer is saved with.
"""
from __future__ import annotations

import pytest

from tests import ask_report_followup_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_ask_report_followup"),
]


@pytest.fixture
def world(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield cases.build_world(repository, postgres=True)
    finally:
        repository.close()


@pytest.mark.parametrize(
    "scenario", cases.SCENARIOS, ids=lambda scenario: scenario.__name__,
)
def test_ask_report_followup_pg(world, scenario):
    scenario(world)
