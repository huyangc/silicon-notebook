"""The KG findings list end to end on PostgreSQL: the twin of
``tests/test_kg_findings_publish.py`` / ``tests/test_kg_findings_read.py``.

The parity case compares the PostgreSQL run of the synthetic world
(``tests/kg_findings_world.py``) with the SAME fixture the SQLite run is held
to — detail rows, ledger payload, overview summary and every zone's page,
byte for byte."""
from __future__ import annotations

import json

import pytest

from app.domain.kg_merge_seed import concept_seed_v1
from tests.kg_findings_world import EXPECTED_SNAPSHOT, seed_world, world_snapshot

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _built(repository) -> str:
    world = seed_world(repository, postgres=True)
    repository._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)
    return world.notebook_id


def test_the_world_snapshot_matches_the_sqlite_fixture(postgres_repository):
    notebook_id = _built(postgres_repository)
    expected = json.loads(EXPECTED_SNAPSHOT.read_text(encoding="utf-8"))
    assert world_snapshot(postgres_repository, notebook_id) == expected


def _status(repository, notebook_id, zone="open") -> dict:
    page = repository._runtime.kg_analysis.findings_page(notebook_id, zone=zone)
    return {(r["rule_id"], r["subject"]["key"]): (r["status"], r["handled_reason"])
            for r in page.rows}


def test_read_side_statuses_on_postgres(postgres_repository):
    """The PostgreSQL forms of the read side's reads: deleted source → handled;
    a rejection keyed by seeds → passed; dismissal → its own zone; a relink
    with nothing to add → F5 spent."""
    repository = postgres_repository
    notebook_id = _built(repository)
    with repository._runtime.database.write() as db:
        db.execute("DELETE FROM sources WHERE id='src-off'")
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,seed_a,seed_b,created_at,updated_at) "
            "VALUES ('mc-r',%s,%s,%s,0.9,'rejected',%s,%s,now(),now())",
            (notebook_id, "K-" + concept_seed_v1("Llama 3.1"),
             "K-" + concept_seed_v1("Llama 31"), concept_seed_v1("Llama 3.1"),
             concept_seed_v1("Llama 31")))
    assert _status(repository, notebook_id)[("F1", "src-off")] == (
        "handled", "source_deleted")
    assert _status(repository, notebook_id, "passed") == {
        ("F4", "llama31"): ("decided_not_same", None)}
    service = repository._runtime.kg_analysis
    assert service.dismiss_finding(notebook_id, "F3", "src-broken", actor_user_id="u") is True
    assert _status(repository, notebook_id, "dismissed") == {
        ("F3", "src-broken"): ("dismissed", None)}
    repository._runtime.knowledge_lifecycle.relink_notebook_kg(notebook_id)
    assert service.overview(notebook_id).findings_summary.relink["may_change"] is False
    assert ("F5", "notebook") in _status(repository, notebook_id, "passed")
