"""The KG findings list end to end on PostgreSQL: the twin of
``tests/test_kg_findings_publish.py`` / ``tests/test_kg_findings_read.py``.

The parity case compares the PostgreSQL run of the synthetic world
(``tests/kg_findings_world.py``) with the SAME fixture the SQLite run is held
to — detail rows, ledger payload, overview summary and every zone's page,
byte for byte."""
from __future__ import annotations

import json

import pytest

from app.domain.kg_merge_seed import concept_seed_v1, concept_seed_v2
from tests.kg_findings_world import EXPECTED_SNAPSHOT, seed_world, world_snapshot

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

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


def test_no_hidden_member_text_is_served_on_postgres(postgres_repository):
    """codex #941 R3 (P1), PostgreSQL form: the hidden Knowhow members' seeds
    stored in F4's evidence decide (a rejection keyed by the hidden-only seed
    still moves the group to "passed") but never reach a served byte — not in
    any zone's page, not anywhere in the overview, in any case."""
    from tests.kg_findings_world import KNOWHOW_HIDDEN_SEED_NAME, hidden_text_served

    repository = postgres_repository
    notebook_id = _built(repository)
    assert hidden_text_served(repository, notebook_id) == []
    page = repository._runtime.kg_analysis.findings_page(notebook_id)
    f4 = next(r for r in page.rows if r["rule_id"] == "F4")
    assert "member_seeds" not in f4["evidence"] and "seeds_truncated" not in f4["evidence"]
    with repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,seed_a,seed_b,created_at,updated_at) "
            "VALUES ('mc-h',%s,'K-llama 3 1','K-llama 31',0.9,'rejected',%s,'llama 31',"
            "now(),now())",
            (notebook_id, concept_seed_v1(KNOWHOW_HIDDEN_SEED_NAME)))
    assert _status(repository, notebook_id, "passed") == {
        ("F4", "llama31"): ("decided_not_same", None)}
    assert hidden_text_served(repository, notebook_id) == []
    # The same rejection minted in seed space 2, keyed by the hidden member's
    # v2 seed (PR-4), decides the group too and is projected out the same way.
    with repository._runtime.database.write() as db:
        db.execute("DELETE FROM concept_merge_candidates WHERE id = 'mc-h'")
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,seed_a,seed_b,seed_version,created_at,updated_at) "
            "VALUES ('mc-h2',%s,'K-llama3 1','K-llama31',0.9,'rejected',%s,'llama31',2,"
            "now(),now())",
            (notebook_id, concept_seed_v2(KNOWHOW_HIDDEN_SEED_NAME)))
    assert _status(repository, notebook_id, "passed") == {
        ("F4", "llama31"): ("decided_not_same", None)}
    assert hidden_text_served(repository, notebook_id) == []


def test_a_full_unified_rebuild_publishes_findings_on_postgres(postgres_repository):
    """PostgreSQL form of the hook check: the split ``rebuild_unified_kg``
    publishes the findings through its (fail-open) derived tail, and an
    unchanged rerun takes the skip path that stamps the merge code."""
    from app.domain.kg_analysis_contracts import ARTIFACT_FINDINGS
    from app.services.kg_analysis_precompute import analysis_ledger_is_current

    repository = postgres_repository
    notebook_id = seed_world(repository, postgres=True).notebook_id
    lifecycle = repository._runtime.knowledge_lifecycle
    events: list = []
    original_emit = lifecycle.event_log.emit
    lifecycle.event_log.emit = lambda event: (events.append(event), original_emit(event))[1]
    try:
        assert lifecycle.rebuild_unified_kg(notebook_id, force=True) > 0
    finally:
        lifecycle.event_log.emit = original_emit
    assert [e for e in events if str(e.get("kind", "")).endswith("_failed")] == []
    store = repository._runtime.unified_kg
    with store.read_snapshot() as db:
        ledger = store.kg_analysis_artifact_rows(db, notebook_id)
        state = store.state_row(db, notebook_id)
        rows = store.kg_analysis_finding_rows(db, notebook_id)
    assert ARTIFACT_FINDINGS in ledger
    assert analysis_ledger_is_current(ledger, int(state["kg_mutation_seq"]),
                                      int(state["cluster_mutation_seq"]), has_boards=True)
    assert ("F5", "notebook") in [(r["rule_id"], r["subject_key"]) for r in rows]

    with repository._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET cluster_code_version="
                   "'algo=1;embed_dim=1;runtime_dim=0' WHERE notebook_id=%s", (notebook_id,))
    service = repository._runtime.kg_analysis
    assert service.overview(notebook_id).merge_code.matches is False
    seqs = (int(state["kg_mutation_seq"]), int(state["cluster_mutation_seq"]))
    lifecycle.rebuild_unified_kg(notebook_id)                     # unchanged: skip path
    with store.read_snapshot() as db:
        after = store.state_row(db, notebook_id)
    assert (int(after["kg_mutation_seq"]), int(after["cluster_mutation_seq"])) == seqs
    assert service.overview(notebook_id).merge_code.matches is True
