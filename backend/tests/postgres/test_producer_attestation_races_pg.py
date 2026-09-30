"""PR-D D3/D4/D7 PostgreSQL twins: the non-federated producers register what
they cite, and racing the terminal read reaches the same verdicts as on SQLite
(``tests/test_producer_attestation_races.py``).

PostgreSQL is the primary environment and the authoritative half: here the
terminal read hashes element bodies IN SQL, while the reading producers
(document overview, collection enumeration) hash the text they read in
process. An untouched element passing proves the two digests agree for rows
read through the real PostgreSQL adapters; the pointer producer (table
analysis) reads its snapshot through the same in-SQL hash.
"""
from __future__ import annotations

import pytest

from app.domain.evidence_fingerprint import element_text_sha
from app.models.notebooks import NotebookCreate
from tests.producer_attestation_testkit import (
    CITED, RACE_EXPECTATIONS, SIBLING, SOURCE, TEXTS, cited, enumeration_citations,
    global_run, mutate, nothing_to_register_case, one_row_page_case,
    overview_citations, refused_row_case, seed, table_citations, terminal_check,
)


pytestmark = pytest.mark.postgres_integration

PRODUCERS = ("document_overview", "collection_enumeration", "table_analysis")


class _World:
    marker = "%s"

    def __init__(self, repository, notebook_id, tmp_path):
        self.repository = repository
        self.database = repository._runtime.database
        self.sources = repository._runtime.source_store
        self.notebook_id = notebook_id
        self.tmp_path = tmp_path

    def produce(self, producer: str) -> list:
        if producer == "document_overview":
            return overview_citations(self.sources, self.notebook_id)
        if producer == "collection_enumeration":
            return enumeration_citations(self.repository, self.notebook_id)
        return table_citations(self.tmp_path, self.notebook_id)


@pytest.fixture
def world(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        notebook = repository.create_notebook(NotebookCreate(name="attest"))
        seed(repository._runtime.database, "%s", notebook.id)
        yield _World(repository, notebook.id, tmp_path)
    finally:
        repository.close()


@pytest.mark.parametrize("mutation", sorted(RACE_EXPECTATIONS))
@pytest.mark.parametrize("producer", PRODUCERS)
def test_postgres_producer_race_before_the_terminal_read(world, producer, mutation):
    with global_run(world.sources, world.notebook_id) as run:
        card = cited(world.produce(producer))
    assert card.source_id == SOURCE and card.notebook_id == world.notebook_id

    mutate(world.database, world.marker, mutation)
    response = terminal_check(world.sources, run, world.notebook_id, [card])

    expected = RACE_EXPECTATIONS[mutation]
    assert response.answer == "答案"
    assert response.citations[0].verification == expected
    if expected is None:
        assert response.citation_check is None
        assert response.evidence_level == "grounded"
    else:
        assert getattr(response.citation_check, expected) == 1
    assert run.state.evidence[CITED] == (SOURCE, element_text_sha(TEXTS[CITED]))


@pytest.mark.parametrize("producer", ("document_overview", "collection_enumeration"))
def test_postgres_reading_producer_cannot_cite_an_id_dangling_before_the_question(
    world, producer,
):
    mutate(world.database, world.marker, "delete")
    with global_run(world.sources, world.notebook_id) as run:
        citations = world.produce(producer)

    assert CITED not in {citation.element_id for citation in citations}
    response = terminal_check(world.sources, run, world.notebook_id, citations)
    assert [citation.element_id for citation in response.citations] == [SIBLING]
    assert response.citations[0].verification is None


def test_postgres_enumeration_snapshot_is_the_listed_text_not_a_later_read(world):
    """The executor registers at LISTING time. An edit landing after the listing
    and before the card is minted must read as ``changed``: the model already
    saw the old text, and a snapshot taken by any later read would silently
    accept the new one."""
    with global_run(world.sources, world.notebook_id) as run:
        card = cited(enumeration_citations(
            world.repository, world.notebook_id,
            between=lambda: mutate(world.database, world.marker, "update"),
        ))

    response = terminal_check(world.sources, run, world.notebook_id, [card])
    assert response.citations[0].verification == "changed"
    assert run.state.evidence[CITED] == (SOURCE, element_text_sha(TEXTS[CITED]))


def test_postgres_table_analysis_drops_a_row_locator_dangling_before_the_question(world):
    mutate(world.database, world.marker, "delete")
    with global_run(world.sources, world.notebook_id) as run:
        [card] = world.produce("table_analysis")

    assert card.element_id == "" and card.source_id == SOURCE
    assert CITED not in run.state.evidence
    response = terminal_check(world.sources, run, world.notebook_id, [card])
    assert response.citations[0].verification is None
    assert response.citation_check is None
    assert run.events == [{
        "kind": "producer_evidence_attested", "producer": "table_analysis",
        "method": "pointers", "elements": 1, "read": 1, "attested": 0, "live": 0, "dead": 1, "unknown": 0,
        "already_unknown": 0,
    }]


def test_postgres_enumeration_registers_only_the_rows_it_emitted(world):
    refused_row_case(world)


def test_postgres_enumeration_registers_a_one_row_page(world):
    one_row_page_case(world)


@pytest.mark.parametrize("producer", PRODUCERS)
def test_postgres_a_producer_with_nothing_to_cite_registers_nothing(world, producer):
    nothing_to_register_case(world, producer)
