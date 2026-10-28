"""PostgreSQL twin of ``tests/test_concept_description_rewrite.py`` (PostgreSQL
is the canonical backend): the same shared scenarios, plus the one property
only this backend has -- the publish never waits on a cluster row another
transaction holds (``FOR UPDATE ... SKIP LOCKED``): it skips that canonical and
writes the rest."""
from __future__ import annotations

import psycopg
import pytest

from app.services.embedding import FakeEmbedder
from tests import concept_description_rewrite_cases as cases
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_concept_description_rewrite"),
]


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def test_pg_dry_run_counts_without_calling_or_writing(repo):
    cases.assert_dry_run_counts_without_calling_or_writing(repo)


def test_pg_rewrites_every_description_through_the_write_contract(repo):
    cases.assert_rewrites_every_description_through_the_write_contract(repo)


def test_pg_resume_skips_what_was_already_rewritten(repo):
    cases.assert_resume_skips_what_was_already_rewritten(repo)


def test_pg_interrupt_keeps_finished_results(repo):
    cases.assert_interrupt_keeps_finished_results(repo)


def test_pg_failed_inputs_are_fail_soft(repo, caplog):
    cases.assert_failed_inputs_are_fail_soft(repo, caplog)


def test_pg_other_notebooks_are_untouched(repo):
    cases.assert_other_notebooks_are_untouched(repo)


def test_pg_a_rebuild_in_flight_defers_the_publish(repo):
    cases.assert_a_rebuild_in_flight_defers_the_publish(repo)


def test_pg_an_append_between_plan_and_publish_is_not_overwritten(repo):
    cases.assert_an_append_between_plan_and_publish_is_not_overwritten(repo)


def test_pg_a_rebuild_published_mid_run_is_replanned(repo):
    cases.assert_a_rebuild_published_mid_run_is_replanned(repo)


def test_pg_unavailable_without_a_bound_model(repo):
    cases.assert_unavailable_without_a_bound_model(repo)


def test_pg_publish_skips_a_canonical_whose_row_another_transaction_holds(
    repo, postgres_settings
):
    """Another transaction (standing in for a source delete or a Knowhow
    reprojection mid-way) holds one MOSFET row. The publish does not wait
    (the fixture's lock_timeout is 1 s: a wait would fail the run), skips
    MOSFET and writes cascode; once the row is free, a rerun finishes with no
    further model call."""
    nb = cases.build_notebook(repo)
    new = cases.DescModel("model-new")
    bind_chat_client(repo, cases.WORKLOAD, new)
    with psycopg.connect(postgres_settings.database_url) as holder:
        holder.execute(
            "SELECT id FROM concept_clusters WHERE notebook_id=%s "
            "AND canonical_name='MOSFET' LIMIT 1 FOR UPDATE",
            (nb,),
        ).fetchone()
        report = cases.rewrite(repo, nb)
        assert (report.status, report.rewritten, report.moved) == ("incomplete", 1, 1)
        holder.rollback()
    texts = cases._texts(repo, nb)
    assert texts["cascode"] == "model-new::cascode"
    assert texts["MOSFET"] == "model-old::MOSFET"
    again = cases.rewrite(repo, nb)
    assert (again.status, again.model_calls, again.rewritten) == ("done", 0, 1)
    assert new.calls == 2
    assert cases._texts(repo, nb)["MOSFET"] == "model-new::MOSFET"


def test_pg_a_canonical_held_only_in_part_is_not_written_in_part(repo, postgres_settings):
    """Three MOSFET members, the third quoting the first word for word.
    Another transaction holds the third member's row: what the publish holds
    still yields the same input (the duplicate quote adds nothing), so only the
    unlocked row count in the same transaction can tell the canonical was not
    held whole. MOSFET is skipped as moved -- none of its rows written -- and
    cascode is written; the rerun finishes MOSFET with no model call."""
    nb = cases.build_notebook(repo)
    cases._add_source(repo, nb, "s3")
    repo.store_kg(nb, "s3", [cases._concept("a3", "Mosfet", "mosfet is a transistor")], [])
    repo._runtime.knowledge_lifecycle.rebuild_unified_kg(nb, force=True)
    (mosfet,) = [name for name in cases.descriptions(repo, nb) if name.lower() == "mosfet"]
    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT cc.id FROM concept_clusters cc JOIN knowledge_objects k "
            "ON k.id=cc.member_object_id WHERE cc.notebook_id=%s AND k.source_id='s3' "
            "AND cc.generation=(SELECT cluster_generation FROM unified_kg_state "
            "WHERE notebook_id=%s)", (nb, nb)).fetchone()
        members = db.execute(
            "SELECT COUNT(*) AS n FROM concept_clusters WHERE notebook_id=%s "
            "AND canonical_name=%s AND generation=(SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id=%s)", (nb, mosfet, nb)).fetchone()["n"]
    assert row is not None and members == 3
    new = cases.DescModel("model-new")
    bind_chat_client(repo, cases.WORKLOAD, new)
    with psycopg.connect(postgres_settings.database_url) as holder:
        holder.execute("SELECT id FROM concept_clusters WHERE id=%s FOR UPDATE",
                       (row["id"],)).fetchone()
        report = cases.rewrite(repo, nb)
        holder.rollback()
    assert (report.status, report.rewritten, report.moved) == ("incomplete", 1, 1)
    assert cases._texts(repo, nb)[mosfet] == f"model-old::{mosfet}"  # no row of it written
    assert cases._texts(repo, nb)["cascode"] == "model-new::cascode"
    again = cases.rewrite(repo, nb)
    assert (again.status, again.model_calls, again.rewritten) == ("done", 0, 1)
    assert cases._texts(repo, nb)[mosfet] == f"model-new::{mosfet}"


def test_pg_pages_are_bounded_by_rows_and_drop_the_rebuild_checkpoint_once(repo, monkeypatch):
    cases.assert_pages_are_bounded_by_rows_and_drop_the_rebuild_checkpoint_once(
        repo, monkeypatch)


def test_pg_hub_canonicals_are_written_in_batches(repo, monkeypatch):
    cases.assert_hub_canonicals_are_written_in_batches(repo, monkeypatch)


def test_pg_a_hub_stops_when_the_state_moves_after_its_verification(repo, monkeypatch):
    cases.assert_a_hub_stops_when_the_state_moves_after_its_verification(repo, monkeypatch)


def test_pg_kg_delete_drops_the_staged_texts_only(repo):
    cases.assert_kg_delete_drops_the_staged_texts_only(repo)


def test_pg_a_completed_notebook_is_answered_from_its_record(repo):
    cases.assert_a_completed_notebook_is_answered_from_its_record(repo)
