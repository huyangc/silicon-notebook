"""PostgreSQL twin of the end-to-end half of tests/test_chunk_recall_memo.py:
with the run's chunk-recall memo on, a global 8-library reasoning run and a
single-library run hand on byte-identical element candidates, chunk recalls
and answers, with fewer physical recalls (``tests/chunk_recall_memo_cases.py``)."""
from __future__ import annotations

import pytest

from tests import chunk_recall_memo_cases as cases
from tests.model_testkit import RecordingModelProvider

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_chunk_recall_memo"),
]


@pytest.fixture(params=cases.LANES)
def world(request, postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(
        postgres_settings.model_copy(update={
            "embed_dim": cases.EMBED_DIM,
            "model_services_config": "",
            "llm_log_enabled": False,
        }),
        model_provider=RecordingModelProvider(),
    )
    try:
        yield cases.build(repository, lane=request.param)
    finally:
        repository.close()


def test_a_global_run_reuses_the_recall_byte_identically_pg(world, monkeypatch):
    cases.global_run_reuses_the_recall_byte_identically(world, monkeypatch)


def test_a_single_library_run_is_byte_identical_pg(world, monkeypatch):
    cases.single_library_run_is_byte_identical(world, monkeypatch)
