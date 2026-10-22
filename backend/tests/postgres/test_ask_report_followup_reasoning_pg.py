"""PostgreSQL twin of the reasoning scenarios in
``tests/test_ask_report_followup_reasoning.py`` (plan 2026-10-21 T8).

A reasoning turn about a deep report, end to end against PostgreSQL's own
stores: the report row read through its gate, the verdict under the
production default ceiling, the restore of the report's cited items, the
evidence partition and its renderers, the cards, the trace's ``report`` step,
the receipt saved with the answer, and the retrieval-only output.
"""
from __future__ import annotations

import pytest

from tests import ask_report_followup_reasoning_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_ask_report_followup_reasoning"),
]


@pytest.fixture
def world(postgres_settings):
    from app.services.postgres_repository import PostgresRepository
    from tests.model_testkit import RecordingModelProvider

    repository = PostgresRepository(
        postgres_settings, model_provider=RecordingModelProvider(),
    )
    try:
        yield cases.build_world(repository, postgres=True)
    finally:
        repository.close()


@pytest.mark.parametrize(
    "scenario", cases.SCENARIOS, ids=lambda scenario: scenario.__name__,
)
def test_ask_report_followup_reasoning_pg(world, scenario):
    scenario(world)
