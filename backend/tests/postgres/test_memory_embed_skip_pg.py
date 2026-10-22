"""PostgreSQL twin of tests/test_memory_embed_skip.py: Memory recall embeds the
query only when some candidate has a stored vector, with hits byte-identical to
the always-embed recall it replaced."""
from __future__ import annotations

import pytest

from tests import memory_embed_skip_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_embed_skip"),
]


@pytest.fixture
def world(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield cases.build(repository)
    finally:
        repository.close()


def test_no_memory_never_embeds_pg(world):
    cases.no_memory_never_embeds(world)


def test_memory_without_vectors_never_embeds_and_scores_alike_pg(world):
    cases.memory_without_vectors_never_embeds_and_scores_alike(world)


def test_memory_with_a_vector_embeds_once_pg(world):
    cases.memory_with_a_vector_embeds_once(world)


def test_closed_channel_is_unchanged_pg(world):
    cases.closed_channel_is_unchanged(world)
