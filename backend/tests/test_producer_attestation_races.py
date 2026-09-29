"""PR-D D3/D4/D7 -- the non-federated producers register what they cite.

Document overview and collection enumeration read the element text themselves
and register the FULL stored body (``attest_read``); table analysis cites a row
element it never read and registers it by pointer (``attest_pointers``). Each
case here runs the real producer on a real SQLite repository, then the real
terminal check after one mutation of the cited element; the PostgreSQL twin is
``tests/postgres/test_producer_attestation_races_pg.py`` and both share
``tests/producer_attestation_testkit.py``.

What "registered correctly" means, and what each case pins:

* untouched -> the citation carries no ``verification`` (an unregistered one
  would be ``unverifiable``; one hashed from the excerpt or filed under the
  wrong source would be ``changed``);
* UPDATE -> ``changed``; DELETE -> ``source_gone``; same text re-inserted under
  the same id -> passes;
* an id already dangling before the question is never minted as an element
  citation (J2);
* outside a global run nothing is read and the output is unchanged (J8).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.evidence_fingerprint import element_text_sha
from app.models.notebooks import NotebookCreate
from app.services.evidence_attestation import evidence_attestation_seat
from tests.producer_attestation_testkit import (
    CITED, RACE_EXPECTATIONS, SIBLING, SOURCE, TEXTS, cited, enumeration_citations,
    global_run, mutate, overview_citations, seed, table_citations, terminal_check,
)


PRODUCERS = ("document_overview", "collection_enumeration", "table_analysis")
_READERS = ("document_overview", "collection_enumeration")


@pytest.fixture
def world(tmp_path):
    from app.services.sqlite_repository import SQLiteRepository

    repository = SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'attest.db'}",
        storage_dir=str(tmp_path / "storage"), event_log_enabled=False,
        llm_log_enabled=False,
    ))
    try:
        notebook = repository.create_notebook(NotebookCreate(name="attest"))
        database = repository._runtime.database
        seed(database, "?", notebook.id)
        yield _World(repository, database, notebook.id, tmp_path)
    finally:
        repository.close()


class _World:
    marker = "?"

    def __init__(self, repository, database, notebook_id, tmp_path):
        self.repository = repository
        self.database = database
        self.notebook_id = notebook_id
        self.tmp_path = tmp_path
        self.sources = repository._runtime.source_store

    def produce(self, producer: str) -> list:
        if producer == "document_overview":
            return overview_citations(self.sources, self.notebook_id)
        if producer == "collection_enumeration":
            return enumeration_citations(self.repository, self.notebook_id)
        return table_citations(self.tmp_path, self.notebook_id)


@pytest.mark.parametrize("mutation", sorted(RACE_EXPECTATIONS))
@pytest.mark.parametrize("producer", PRODUCERS)
def test_producer_race_before_the_terminal_read(world, producer, mutation):
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
    # The snapshot is the digest of the WHOLE stored body under its real source.
    assert run.state.evidence[CITED] == (SOURCE, element_text_sha(TEXTS[CITED]))


@pytest.mark.parametrize("producer", _READERS)
def test_a_reading_producer_cannot_cite_an_id_dangling_before_the_question(
    world, producer,
):
    """J2 for the reading producers: they cite only rows they just read, so an
    element deleted before the question is simply not there to cite."""
    mutate(world.database, world.marker, "delete")
    with global_run(world.sources, world.notebook_id) as run:
        citations = world.produce(producer)

    assert CITED not in {citation.element_id for citation in citations}
    response = terminal_check(world.sources, run, world.notebook_id, citations)
    assert [citation.element_id for citation in response.citations] == [SIBLING]
    assert response.citations[0].verification is None


def test_table_analysis_drops_a_row_locator_dangling_before_the_question(world):
    """J2 for the pointer producer: the dead row element is not minted; the
    receipt stays a source-level citation (J3) and passes the source check."""
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
        "method": "pointers", "elements": 1, "live": 0, "dead": 1,
    }]


class _RecordingReader:
    def __init__(self):
        self.reads: list = []

    def evidence_fingerprints(self, element_ids):
        self.reads.append(tuple(element_ids))
        return {}


@pytest.mark.parametrize("producer", PRODUCERS)
def test_outside_a_global_run_nothing_is_read_and_nothing_changes(world, producer):
    """J8: a notebook ask installs no run plan; the producers' output is the
    same with or without a seat, and even a dangling row keeps its locator."""
    if producer == "table_analysis":
        mutate(world.database, world.marker, "delete")
    bare = [citation.model_dump() for citation in world.produce(producer)]
    reader = _RecordingReader()
    events: list = []
    with evidence_attestation_seat(reader, emit=events.append):
        seated = [citation.model_dump() for citation in world.produce(producer)]

    assert seated == bare
    assert reader.reads == [] and events == []
    if producer == "table_analysis":
        assert [citation["element_id"] for citation in bare] == [CITED]


@pytest.mark.parametrize("producer", PRODUCERS)
def test_producer_events_are_content_free_counts(world, producer):
    with global_run(world.sources, world.notebook_id) as run:
        world.produce(producer)

    assert run.events
    for event in run.events:
        assert event["kind"] == "producer_evidence_attested"
        assert event["producer"] == producer
        assert event["method"] == ("pointers" if producer == "table_analysis" else "read")
        assert set(event) <= {"kind", "producer", "method", "elements", "live", "dead"}
        rendered = repr(event)
        assert not any(token in rendered for token in (CITED, SIBLING, SOURCE))
        assert not any(text[:4] in rendered for text in TEXTS.values())
