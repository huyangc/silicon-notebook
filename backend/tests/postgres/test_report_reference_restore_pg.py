"""PostgreSQL twin of ``tests/test_report_reference_restore.py`` (plan
2026-10-21 T4).

The restore composes PostgreSQL's own statements -- the passage read with its
ceiling list bound as one JSON parameter and its Memory owner predicate, the
element and knowledge-object reads by primary key, the relation evidence read
-- so the same scenarios run here against the real store: current text, a
mounted library's items stamped with their library, deleted and re-parsed
items gone, nothing outside the ceiling, no other member's Memory, no Memory
with the channel closed, relations read back as their evidence element.
"""
from __future__ import annotations

import pytest

from tests import report_reference_restore_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_report_reference_restore"),
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
def test_report_reference_restore_pg(world, scenario):
    scenario(world)
