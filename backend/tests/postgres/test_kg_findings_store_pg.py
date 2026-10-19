"""KG analysis findings store primitives on PostgreSQL (0077): the twin of
``tests/test_kg_findings_store.py``. Same world, same checks, same literal
expectations (``tests/kg_findings_store_cases.py``) -- the two backends must
return identical values."""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres import unified_kg_store as store_module
from tests import kg_findings_store_cases as cases


pytestmark = pytest.mark.postgres_integration

# The plan pins (the id-list statements and the old-detail read, on a
# production-shaped corpus with the planner left alone) live in
# tests/postgres/test_kg_findings_plan_pins.py.


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_findings_are_replaced_wholesale_with_the_ledger(postgres_repository):
    cases.check_findings_replace_wholesale(postgres_repository, postgres=True)


def test_findings_writes_refuse_malformed_rows_and_enforce_the_cap(
    postgres_repository, monkeypatch,
):
    cases.check_findings_write_guards(
        postgres_repository, postgres=True, monkeypatch=monkeypatch,
        store_module=store_module)


def test_marks_upsert_delete_prune_and_cap(postgres_repository, monkeypatch):
    cases.check_marks(
        postgres_repository, postgres=True, monkeypatch=monkeypatch,
        store_module=store_module)


@pytest.fixture
def concurrent_repository(postgres_settings):
    """Room for two writers plus the checks: three pooled connections, and lock
    and statement timeouts above the case's 3 s barrier (the default test
    settings give a lock wait 1 s)."""
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings.model_copy(update={
        "postgres_pool_max_size": 3,
        "postgres_pool_acquire_timeout_seconds": 10,
        "postgres_statement_timeout_seconds": 20,
        "postgres_lock_timeout_seconds": 15,
    }))
    try:
        yield repository
    finally:
        repository.close()


def test_the_mark_cap_holds_under_concurrent_writers(concurrent_repository, monkeypatch):
    cases.check_mark_cap_under_concurrency(
        concurrent_repository, postgres=True, monkeypatch=monkeypatch,
        store_module=store_module)


def test_indexing_publication_clears_findings_and_the_code_version(postgres_repository):
    cases.check_indexing_publish_clears_findings(postgres_repository, postgres=True)


def test_source_states_read_the_latest_extraction_and_hide_synthetic_titles(
    postgres_repository,
):
    cases.check_source_states(postgres_repository, postgres=True)


def test_display_names_come_from_the_sources_own_visible_objects(postgres_repository):
    cases.check_display_names(postgres_repository, postgres=True)


def test_display_names_are_batch_invariant(postgres_repository):
    cases.check_display_names_across_batches(postgres_repository, postgres=True)


def test_member_names_cover_every_member_and_flag_the_hidden_ones(postgres_repository):
    cases.check_member_names(postgres_repository, postgres=True)


def test_merge_candidate_states_filter_the_notebook_slice(postgres_repository):
    cases.check_candidate_states(postgres_repository, postgres=True)


def test_code_version_stamp_is_guarded_by_the_input_version(postgres_repository):
    cases.check_code_version_stamp(postgres_repository, postgres=True)


def test_the_check_constraint_backs_the_mark_kind_vocabulary(postgres_repository):
    world = cases.seed(postgres_repository, postgres=True)
    with pytest.raises(psycopg.errors.CheckViolation):
        with postgres_repository._runtime.database.write() as db:
            db.execute(
                "INSERT INTO kg_analysis_marks (notebook_id, rule_id, subject_key, "
                "kind, at_kg_seq, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (world.nb, "F1", "src-a", "bogus", 1, cases.T1),
            )
