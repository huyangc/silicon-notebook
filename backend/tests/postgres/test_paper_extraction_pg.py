"""PostgreSQL twin of the passage-selection cases of
``tests/test_paper_extraction.py``: the outline, per-field similarities, the
FTS fallback (``chunk_fts_search`` restricted to the one source) and the
element mapping over the canonical store SQL."""
from __future__ import annotations

import pytest

from app.models.schemas import NotebookCreate
from tests import paper_card_testkit as card_kit
from tests import test_paper_extraction as cases
from tests.model_testkit import RecordingModelProvider, bind_all_embedding_clients
from tests.paper_topics_testkit import TOPIC_EMBED_DIM, TopicEmbedder

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_paper_extraction"),
]


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.embed_dim = TOPIC_EMBED_DIM
    postgres_settings.chunk_target_chars = 60
    repository = PostgresRepository(
        postgres_settings,
        model_provider=RecordingModelProvider(parallelism_by_workload={"paper_metadata": 4}),
    )
    bind_all_embedding_clients(repository, TopicEmbedder())
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def nb(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def model(repo):
    fake = cases.FakeExtractionModel()
    with card_kit.scheduled_paper_model(repo, fake):
        yield fake


@pytest.fixture
def service(repo):
    return repo._runtime.paper_extraction


@pytest.fixture
def events(service, monkeypatch):
    seen: list[dict] = []
    original = service._event_log.emit

    def capture(event, **kwargs):
        if event.get("kind") == "paper_extraction":
            seen.append(dict(event))
        else:
            original(event, **kwargs)

    monkeypatch.setattr(service._event_log, "emit", capture)
    return seen


test_each_field_selects_its_own_passages = cases.test_each_field_selects_its_own_passages
test_each_field_keeps_only_its_top_passages_of_a_long_source = (
    cases.test_each_field_keeps_only_its_top_passages_of_a_long_source)
test_without_vectors_each_field_falls_back_to_this_sources_fts = (
    cases.test_without_vectors_each_field_falls_back_to_this_sources_fts)
test_an_ungrounded_number_is_dropped_and_a_grounded_one_kept = (
    cases.test_an_ungrounded_number_is_dropped_and_a_grounded_one_kept)
test_sources_outside_the_allowed_set_or_missing_are_refused = (
    cases.test_sources_outside_the_allowed_set_or_missing_are_refused)
