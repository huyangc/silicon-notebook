"""``SourceFinder`` over the synthetic paper-topics corpus (design §8, layer 1):
40 seeded papers, six labelled topics, the deterministic ``TopicEmbedder`` and
the real retrieval stack (both legs, real chunk lanes).

Nothing here asserts a relevance value or a threshold number -- the lines are
pinned to fixed test values (they are configuration, calibrated per embedding
model by ``app.eval.find_sources_calibrate``) and every assertion is about
tiers, membership, order and recall against the labels.  The PostgreSQL twin
is ``tests/postgres/test_paper_topics_finder_pg.py``.
"""
from __future__ import annotations

import pytest

from app.services.source_finder import FindSourcesRequest, SourceFinder
from tests.paper_topics_testkit import load_labels, sqlite_corpus

TOPICS = sorted(load_labels())


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    yield from sqlite_corpus(tmp_path, monkeypatch)


def _finder(instance) -> SourceFinder:
    runtime = instance._runtime
    return SourceFinder(
        database=runtime.database,
        sources=runtime.source_store,
        retrieval=instance.retrieval,
        enumeration=runtime.collection_enumeration,
        settings=instance.settings,
        paper_card_version=1,
    )


def _find_all(instance, nb, request: FindSourcesRequest) -> list:
    """Every page of one search (MCP口径: the notebook's visible sources)."""
    finder = _finder(instance)
    allowed = instance.all_visible_source_ids(nb)
    result = finder.find(nb, request, allowed_source_ids=allowed)
    items = list(result.items)
    while result.next_cursor:
        result = finder.find(nb, FindSourcesRequest(
            query=request.query, terms=request.terms, kind=request.kind,
            cursor=result.next_cursor,
        ), allowed_source_ids=allowed)
        items.extend(result.items)
    return items


def _by_key(keys, items) -> dict:
    back = {source_id: key for key, source_id in keys.items()}
    return {back[item.source_id]: item.tier for item in items}


def _positives(topic: str) -> set:
    labels = load_labels()[topic]
    return set(labels["strong"]) | set(labels["related"])


@pytest.mark.parametrize("topic", TOPICS)
def test_the_literal_terms_only_find_labelled_papers(corpus, topic):
    """The metadata leg is threshold-free: every paper it returns is labelled
    for the topic, and the strong tier holds labelled papers only."""
    instance, nb, keys = corpus
    labels = load_labels()[topic]
    found = _by_key(keys, _find_all(instance, nb, FindSourcesRequest(
        terms=tuple(labels["terms"]))))
    assert found, topic
    assert set(found) <= _positives(topic)
    assert {key for key, tier in found.items() if tier == "strong"} <= _positives(topic)


@pytest.mark.parametrize("topic", TOPICS)
def test_query_and_terms_recall_every_labelled_paper(corpus, topic):
    instance, nb, keys = corpus
    labels = load_labels()[topic]
    found = _by_key(keys, _find_all(instance, nb, FindSourcesRequest(
        query=labels["query"], terms=tuple(labels["terms"]))))
    assert _positives(topic) <= set(found), sorted(_positives(topic) - set(found))
    # Every labelled-strong paper is in a tier, and the strong tier is mostly
    # labelled papers (a lexical hit on an off-topic paper may still land there).
    strong = [key for key, tier in found.items() if tier == "strong"]
    assert strong
    assert sum(key in _positives(topic) for key in strong) / len(strong) >= 0.8


@pytest.mark.parametrize("topic", TOPICS)
def test_the_semantic_leg_ranks_the_topic_first(corpus, topic, monkeypatch):
    """Query only, with the lines opened wide (nothing strong by relevance,
    everything with a passage related): the order is the passage relevance, and
    the labelled papers fill the head of it."""
    instance, nb, keys = corpus
    monkeypatch.setattr(instance.settings, "find_sources_strong_relevance", 1.0)
    monkeypatch.setattr(instance.settings, "find_sources_related_relevance", 0.0)
    positives = _positives(topic)
    items = _find_all(instance, nb, FindSourcesRequest(
        query=load_labels()[topic]["query"], kind="all"))
    head = list(_by_key(keys, items[:len(positives)]))
    assert sum(key in positives for key in head) / len(positives) >= 0.8, head


def test_paging_the_corpus_is_one_ranking(corpus):
    instance, nb, _keys = corpus
    labels = load_labels()["rag"]
    request = FindSourcesRequest(query=labels["query"], terms=tuple(labels["terms"]), limit=3)
    finder = _finder(instance)
    allowed = instance.all_visible_source_ids(nb)
    paged = _find_all(instance, nb, request)
    whole = finder.find(nb, FindSourcesRequest(
        query=request.query, terms=request.terms, limit=20), allowed_source_ids=allowed)
    assert [item.source_id for item in paged][:20] == [item.source_id for item in whole.items]
    assert [item.rank for item in paged] == list(range(1, len(paged) + 1))
    assert len({item.source_id for item in paged}) == len(paged)


def test_a_narrower_allow_list_is_the_whole_source_set(corpus):
    instance, nb, keys = corpus
    labels = load_labels()["asr"]
    allowed = [keys["asr-01"], keys["asr-02"], keys["det-01"]]
    result = _finder(instance).find(nb, FindSourcesRequest(
        query=labels["query"], terms=tuple(labels["terms"]), kind="all"),
        allowed_source_ids=allowed)
    assert {item.source_id for item in result.items} <= set(allowed)
    assert {keys["asr-01"], keys["asr-02"]} <= {item.source_id for item in result.items}
    assert result.filtered_scope_total == 3
