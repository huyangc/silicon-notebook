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
