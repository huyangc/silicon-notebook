"""PostgreSQL twin of tests/test_memory_embed_share.py: Memory recall and the
retrieval stage share one embed of the research question, with Memory scores
and the stage's vector recall byte-identical to the unshared embeds."""
from __future__ import annotations

import pytest

from tests import memory_embed_share_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_embed_share"),
]


def _repo(postgres_settings, runtime_dim: int):
    from app.services.postgres_repository import PostgresRepository

    return PostgresRepository(postgres_settings.model_copy(update={
        "embed_dim": cases.EMBED_DIM,
        "embed_runtime_dim": runtime_dim,
        "model_services_config": "",
    }))


@pytest.fixture(params=cases.RUNTIME_DIMS, ids=lambda d: f"runtime_dim={d}")
def world(request, postgres_settings):
    repository = _repo(postgres_settings, request.param)
    try:
        yield cases.build(repository, request.param)
    finally:
        repository.close()


@pytest.fixture(params=cases.RUNTIME_DIMS, ids=lambda d: f"runtime_dim={d}")
def offline_world(request, postgres_settings):
    repository = _repo(postgres_settings, request.param)
    try:
        yield cases.build(repository, request.param, bind=False)
    finally:
        repository.close()


def test_memory_first_embeds_once_and_scores_alike_pg(world):
    cases.memory_first_embeds_once_and_scores_alike(world)


def test_retrieval_first_shares_only_an_untruncated_vector_pg(world):
    cases.retrieval_first_shares_only_an_untruncated_vector(world)


def test_different_texts_embed_each_pg(world):
    cases.different_texts_embed_each(world)


def test_different_embedders_embed_each_pg(world):
    cases.different_embedders_embed_each(world)


def test_unconfigured_embedder_never_shares_pg(world):
    cases.unconfigured_embedder_never_shares(world)


def test_offline_mode_never_shares_pg(offline_world):
    cases.offline_mode_never_shares(offline_world)


def test_failed_embeds_are_unchanged_pg(world):
    cases.failed_embeds_are_unchanged(world)


def test_outside_a_run_nothing_is_shared_pg(world):
    cases.outside_a_run_nothing_is_shared(world)


def test_no_vector_memory_is_unchanged_pg(world):
    cases.no_vector_memory_is_unchanged(world)
