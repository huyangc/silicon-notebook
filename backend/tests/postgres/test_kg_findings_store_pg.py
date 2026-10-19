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


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

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


def test_source_states_read_the_latest_extraction_and_hide_synthetic_titles(
    postgres_repository,
):
    cases.check_source_states(postgres_repository, postgres=True)


def test_display_names_come_from_the_sources_own_visible_objects(postgres_repository):
    cases.check_display_names(postgres_repository, postgres=True)


def test_display_names_are_batch_invariant(postgres_repository):
    cases.check_display_names_across_batches(postgres_repository, postgres=True)


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


def test_old_detail_read_stays_on_the_primary_key(postgres_repository):
    """The publish transaction's in-transaction read of the old detail walks
    ``pk_kg_analysis_findings`` (an index scan in key order: no sort node, no
    sequential scan), and it may run inside the write transaction."""
    rt = postgres_repository._runtime
    world = cases.seed(postgres_repository, postgres=True)
    cases.replace(rt, world.nb, cases.FINDINGS_V1)
    with rt.database.write() as db:
        db.execute("ANALYZE kg_analysis_findings")
        db.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            row["QUERY PLAN"]
            for row in db.execute(
                "EXPLAIN SELECT rule_id, subject_key, subject_kind, subject_label, "
                "subject_refs, severity, impact_objects, evidence, "
                "first_seen_kg_seq, first_seen_cluster_seq "
                "FROM kg_analysis_findings WHERE notebook_id=%s "
                "ORDER BY rule_id, subject_key LIMIT %s",
                (world.nb, 5001),
            ).fetchall()
        )
        assert "pk_kg_analysis_findings" in plan, plan
        assert "Sort" not in plan, plan
        assert len(rt.unified_kg.kg_analysis_finding_rows(db, world.nb)) == 3
