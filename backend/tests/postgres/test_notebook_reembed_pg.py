"""Notebook re-embed job on real PostgreSQL.

The twin of ``tests/test_notebook_reembed.py``: the same world and checks from
``tests/notebook_reembed_cases.py``; this module only says how PostgreSQL stores a row
(JSON columns as jsonb) and binds its placeholders. The single-flight guard is
process-local and backend-neutral, so it is pinned on SQLite only.
"""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.postgres import maintenance as postgres_maintenance
from app.services.notebook_reembed import NotebookReembedService
from app.services.repository_factory import create_repository
from tests import notebook_reembed_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_reembed"),
]


def _insert(db, table: str, values: dict) -> None:
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
        [Jsonb(values[c]) if isinstance(values[c], (dict, list)) else values[c] for c in columns],
    )


BACKEND = cases.Backend(
    name="postgres", insert=_insert, placeholder="%s",
    maintenance_module=postgres_maintenance, relation_page_attr="_RELATION_PAGE_SIZE",
    json_param=Jsonb,
)


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in cases.ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def repo(env):
    repository = create_repository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def test_every_vector_comes_back_once_pg(repo):
    cases.check_every_vector_comes_back_once(repo, BACKEND)


def test_backfill_job_fills_knowhow_without_touching_the_graph_pg(repo):
    cases.check_backfill_job_fills_knowhow_without_touching_the_graph(repo, BACKEND)


def test_knowhow_fill_skips_a_chunk_rewritten_mid_call_pg(repo):
    cases.check_knowhow_fill_skips_a_chunk_rewritten_mid_call(repo, BACKEND)


def test_offline_missing_only_embed_keeps_knowhow_on_its_workload_pg(repo):
    cases.check_offline_missing_only_embed_keeps_knowhow_on_its_workload(repo, BACKEND)


def test_object_vectors_mark_the_graph_dirty_pg(repo):
    cases.check_object_vectors_mark_the_graph_dirty(repo, BACKEND)


def test_model_failure_is_visible_and_resumes_pg(repo):
    cases.check_model_failure_is_visible_and_resumes(repo, BACKEND)


def test_unbound_workload_is_visible_pg(repo):
    cases.check_unbound_workload_is_visible(repo, BACKEND)


def test_a_failed_discovery_fails_its_leg_pg(repo, monkeypatch):
    cases.check_a_failed_discovery_fails_its_leg(repo, BACKEND, monkeypatch)


def test_a_pipeline_switch_aborts_the_pass_pg(repo, monkeypatch):
    cases.check_a_pipeline_switch_aborts_the_pass(repo, BACKEND, monkeypatch)


def test_relation_backfill_pages_pg(repo, monkeypatch):
    cases.check_relation_backfill_pages(repo, BACKEND, monkeypatch)


def test_object_leg_keeps_its_count_when_it_fails_pg(repo, monkeypatch):
    cases.check_object_leg_keeps_its_count_when_it_fails(repo, BACKEND, monkeypatch)


def test_knowhow_fill_discovers_ids_then_reads_page_by_page_pg(repo, monkeypatch):
    cases.check_knowhow_fill_discovers_ids_then_reads_page_by_page(repo, BACKEND, monkeypatch)


def test_conditional_chunk_vector_write_pg(repo):
    cases.check_conditional_chunk_vector_write(repo, BACKEND)


def test_blank_chunks_are_never_a_gap_pg(repo):
    cases.check_blank_chunks_are_never_a_gap(repo, BACKEND)


def test_orphan_knowhow_sources_are_not_counted_pg(repo):
    cases.check_orphan_knowhow_sources_are_not_counted(repo, BACKEND)


def test_one_failing_knowhow_table_does_not_stop_the_others_pg(repo, monkeypatch):
    cases.check_one_failing_knowhow_table_does_not_stop_the_others(repo, BACKEND, monkeypatch)


def test_offline_embed_messages_pg(repo, capsys):
    cases.check_offline_embed_messages(repo, BACKEND, capsys)


def test_an_embedder_that_writes_nothing_is_never_done_pg(repo, monkeypatch):
    cases.check_an_embedder_that_writes_nothing_is_never_done(repo, BACKEND, monkeypatch)


def test_object_backfill_never_overwrites_a_newer_vector_pg(repo):
    cases.check_object_backfill_never_overwrites_a_newer_vector(repo, BACKEND)


def test_relation_backfill_never_overwrites_or_mismatches_pg(repo):
    cases.check_relation_backfill_never_overwrites_or_mismatches(repo, BACKEND)


def test_pipeline_switch_while_waiting_for_a_table_lock_pg(repo, monkeypatch):
    cases.check_pipeline_switch_while_waiting_for_a_table_lock(repo, BACKEND, monkeypatch)


def test_pipeline_switch_between_the_halves_pg(repo, monkeypatch):
    cases.check_pipeline_switch_between_the_halves(repo, BACKEND, monkeypatch)


def test_relation_discovery_page_is_one_index_range_pg(repo):
    """Each relation-backfill page is one range of
    ``idx_knowledge_relations_nb_source_id`` in index order: no Sort node (the old
    ``id > %s ORDER BY id`` had no ``(notebook_id, id)`` index to follow). Judged with
    seqscan/bitmapscan off like the sibling EXPLAIN pins: the test bed is tiny."""
    with repo._runtime.database.write() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        plan = "\n".join(
            str(next(iter(dict(row).values()))) for row in db.execute(
                "EXPLAIN (COSTS OFF) " + postgres_maintenance._MISSING_RELATION_PAGE_SQL,
                ("nb", "a", "b", 5),
            ).fetchall()
        )
    assert "idx_knowledge_relations_nb_source_id" in plan, plan
    assert "Sort" not in plan, plan


def test_relation_backfill_in_a_fresh_process_pg(env):
    """A process whose retrieval has never been composed (the offline relation
    backfill, or a job that runs before the first query) still fills relation vectors:
    the backfill composes retrieval itself instead of reading the unset attribute."""
    builder = create_repository(Settings())
    try:
        nb = cases.build_world(builder, BACKEND).notebook_id
        cases.delete_vectors(builder, BACKEND, nb, tables=("relation_embeddings",))
    finally:
        builder.close()
    fresh = create_repository(Settings())
    try:
        assert fresh._runtime.retrieval is None  # nothing has composed retrieval yet
        embedders = cases.bind_embedders(fresh)
        status = NotebookReembedService(fresh).run(nb, legs=["kg_relations"])
        assert status.state == "succeeded", status
        assert status.progress["kg_relations"].embedded >= 3
        assert embedders["relation_embedding"].calls
    finally:
        fresh.close()
