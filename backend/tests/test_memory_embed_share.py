"""Memory recall and the retrieval stage share one embed of the research
question (SQLite; scenarios in ``memory_embed_share_cases``, shared with
``postgres/test_memory_embed_share_pg.py``)."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_embed_share_cases as cases


def _repo(tmp_path, monkeypatch, runtime_dim: int) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(cases.EMBED_DIM))
    monkeypatch.setenv("EMBED_RUNTIME_DIM", str(runtime_dim))
    return SQLiteRepository(Settings(_env_file=None, model_services_config=""))


@pytest.fixture(params=cases.RUNTIME_DIMS, ids=lambda d: f"runtime_dim={d}")
def world(request, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, request.param)
    try:
        yield cases.build(repo, request.param)
    finally:
        repo.close()


@pytest.fixture(params=cases.RUNTIME_DIMS, ids=lambda d: f"runtime_dim={d}")
def offline_world(request, tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, request.param)
    try:
        yield cases.build(repo, request.param, bind=False)
    finally:
        repo.close()


def test_memory_first_embeds_once_and_scores_alike(world):
    cases.memory_first_embeds_once_and_scores_alike(world)


def test_retrieval_first_shares_only_an_untruncated_vector(world):
    cases.retrieval_first_shares_only_an_untruncated_vector(world)


def test_different_texts_embed_each(world):
    cases.different_texts_embed_each(world)


def test_different_embedders_embed_each(world):
    cases.different_embedders_embed_each(world)


def test_unconfigured_embedder_never_shares(world):
    cases.unconfigured_embedder_never_shares(world)


def test_offline_mode_never_shares(offline_world):
    cases.offline_mode_never_shares(offline_world)


def test_failed_embeds_are_unchanged(world):
    cases.failed_embeds_are_unchanged(world)


def test_outside_a_run_nothing_is_shared(world):
    cases.outside_a_run_nothing_is_shared(world)


def test_no_vector_memory_is_unchanged(world):
    cases.no_vector_memory_is_unchanged(world)


def test_report_run_keeps_the_hand_over_bounded(world):
    cases.report_run_keeps_the_hand_over_bounded(world)


def test_scout_shares_across_libraries(world):
    cases.scout_shares_across_libraries(world)
