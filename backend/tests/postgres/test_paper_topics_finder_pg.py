"""PostgreSQL twin of ``tests/test_paper_topics_finder.py``: the synthetic
paper-topics corpus seeded through the same loader into a
``PostgresRepository``, and the same tier / recall / paging cases over the
canonical store SQL and the PostgreSQL chunk lanes."""
from __future__ import annotations

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookCreate
from tests import test_paper_topics_finder as cases
from tests.model_testkit import bind_all_embedding_clients
from tests.paper_topics_testkit import (
    RELATED_LINE,
    STRONG_LINE,
    TOPIC_EMBED_DIM,
    TopicEmbedder,
    seed_paper_topics,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_paper_topics"),
]


@pytest.fixture
def corpus(postgres_settings, monkeypatch):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.embed_dim = TOPIC_EMBED_DIM
    instance = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(instance, TopicEmbedder())
    monkeypatch.setattr(instance.settings, "find_sources_strong_relevance", STRONG_LINE)
    monkeypatch.setattr(instance.settings, "find_sources_related_relevance", RELATED_LINE)
    alice = instance._runtime.identity.create_user("a00123456", "password-12")
    token = set_request_user(alice)
    try:
        nb = instance.create_notebook(NotebookCreate(name="papers")).id
        keys = seed_paper_topics(instance, nb)
        yield instance, nb, keys
    finally:
        reset_request_user(token)
        instance.close()


test_the_literal_terms_only_find_labelled_papers = cases.test_the_literal_terms_only_find_labelled_papers
test_query_and_terms_recall_every_labelled_paper = cases.test_query_and_terms_recall_every_labelled_paper
test_the_semantic_leg_ranks_the_topic_first = cases.test_the_semantic_leg_ranks_the_topic_first
test_paging_the_corpus_is_one_ranking = cases.test_paging_the_corpus_is_one_ranking
test_a_narrower_allow_list_is_the_whole_source_set = cases.test_a_narrower_allow_list_is_the_whole_source_set
