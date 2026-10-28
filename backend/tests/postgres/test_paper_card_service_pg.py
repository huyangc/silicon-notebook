"""PostgreSQL end-to-end: ``PaperCardService`` generates a card from real element
rows (metadata step included), stores it with the UTC basis and reads it back as a
cache hit. SQLite twin: ``tests/test_paper_card_service.py``."""
from __future__ import annotations

import pytest

from app.models.schemas import NotebookCreate
from app.services.paper_cards import PAPER_CARD_VERSION
from tests import paper_card_testkit as kit
from tests.model_testkit import RecordingModelProvider

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_paper_card_service"),
]


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(
        postgres_settings,
        model_provider=RecordingModelProvider(parallelism_by_workload={"paper_metadata": 2}),
    )
    try:
        yield repository
    finally:
        repository.close()


def test_card_end_to_end_on_postgres(repo):
    model = kit.FakePaperModel()
    with kit.scheduled_paper_model(repo, model):
        _end_to_end(repo, model)


def _end_to_end(repo, model):
    nb = repo.create_notebook(NotebookCreate(name="papers")).id
    kit.seed_source(repo, nb, "pg-s1", filler=40)
    kit.seed_source(repo, nb, "pg-s2", doc_type="textbook")
    service = repo._runtime.paper_cards
    items = [(nb, "pg-s1"), (nb, "pg-s2")]

    first = service.get_cards(items, allowed=items, generate_limit=4)

    assert [(r.source_id, r.status) for r in first] == [("pg-s1", "generated"), ("pg-s2", "not_paper")]
    assert (model.meta_calls, model.card_calls) == (1, 1)
    card = first[0].card
    assert card["metrics"][0]["anchor"] == "el-pg-s1-0041"   # the table element
    assert card["tldr"]["anchors"] == ["el-pg-s1-0000"]
    store = repo._runtime.source_store
    basis = store.source_card_basis("pg-s1")
    assert basis.endswith("+00:00")   # UTC ISO, independent of the session time zone
    with repo._runtime.database.connect() as db:
        row = store.paper_cards_for_sources(db, ["pg-s1"], notebook_id=nb)["pg-s1"]
    assert (row["status"], row["basis"], row["card_version"]) == ("ready", basis, PAPER_CARD_VERSION)
    assert row["card"] == card

    again = service.get_cards(items, allowed=items, generate_limit=4)

    assert [(r.status, r.card) for r in again] == [("ready", card), ("not_paper", {})]
    assert model.card_calls == 1

    kit.replace_elements(repo, "pg-s1", created_at="2026-02-01T08:00:00+08:00", filler=40)
    [regenerated, _] = service.get_cards(items, allowed=items, generate_limit=4)
    assert regenerated.status == "generated"
    assert model.card_calls == 2
