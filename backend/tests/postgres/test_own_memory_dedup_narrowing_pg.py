"""PostgreSQL twin of tests/test_own_memory_dedup_narrowing.py: the owner's
duplicate groups, read in two narrow passes with the evidence test in SQL,
answer exactly what the full read they replaced answered."""
from __future__ import annotations

import pytest

from tests import own_memory_dedup_narrowing_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_own_memory_dedup_narrowing"),
]


@pytest.fixture
def world(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield cases.build(repository, lambda sql: sql.replace("?", "%s"))
    finally:
        repository.close()


def test_two_passes_answer_what_the_full_read_answered_pg(world):
    cases.two_passes_answer_what_the_full_read_answered(world)


def test_blocks_follow_the_published_seed_space_pg(world):
    cases.blocks_follow_the_published_seed_space(world)
