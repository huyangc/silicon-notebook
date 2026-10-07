"""PR-E8 (ledger B-12) on real PostgreSQL -- the twin of
``tests/test_promotion_provenance.py``: the same scenarios
(``promotion_provenance_cases.py``) through a real ``PostgresRepository``, plus
EXPLAIN pins for the two reads the approving transaction adds
(``promotion_provenance_store.plan_for_library``: sources and the original
elements, both by primary key through ``id_binding``) and for the
pipeline-exclusion predicate on the KG target page.
"""
from __future__ import annotations

import pytest

from app.services.embedding import FakeEmbedder
from tests import promotion_provenance_cases as cases
from tests.model_testkit import RecordingModelProvider, bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_promotion_provenance"),
]


def _sql(statement: str) -> str:
    return statement.replace("?", "%s")


@pytest.fixture
def repo(postgres_settings, monkeypatch):
    from app.repositories.postgres.repository import PostgresRepository

    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    postgres_settings.postgres_pool_max_size = 6
    postgres_settings.postgres_statement_timeout_seconds = 10
    repository = PostgresRepository(
        postgres_settings, model_provider=RecordingModelProvider()
    )
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    repository.settings.graph_ppr_enabled = False
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def world(repo):
    return cases.build(repo, _sql)


def test_pg_approval_writes_library_owned_provenance(world):
    cases.approval_writes_library_owned_provenance(world)


def test_pg_a_second_promotion_from_the_same_original_reuses_its_source(world):
    cases.a_second_promotion_from_the_same_original_reuses_its_source(world)


def test_pg_a_merge_rewrites_only_the_incoming_entries(world):
    cases.a_merge_rewrites_only_the_incoming_entries(world)


def test_pg_a_memory_promotion_is_titled_after_the_memory(world):
    cases.a_memory_promotion_is_titled_after_the_memory(world)


def test_pg_deleting_the_promotion_source_deletes_the_objects_it_supports(world):
    cases.deleting_the_promotion_source_deletes_the_objects_it_supports(world)


def test_pg_the_promotion_source_is_never_a_pipeline_target(world):
    cases.the_promotion_source_is_never_a_pipeline_target(world)


def test_pg_the_answer_context_reads_the_entry_as_the_librarys_own(world):
    cases.the_answer_context_reads_the_entry_as_the_librarys_own(world)


def test_pg_single_notebook_ask_through_a_mount_cites_the_promoted_object(world):
    cases.single_notebook_ask_through_a_mount_cites_the_promoted_object(world)


def test_pg_global_ask_cites_the_promoted_object(world):
    cases.global_ask_cites_the_promoted_object(world)


def test_pg_the_promoters_private_notebook_can_go(world):
    cases.the_promoters_private_notebook_can_go(world)


# ---------------------------------------------------------------------------
# EXPLAIN pins
# ---------------------------------------------------------------------------

class _CapturingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=None, **kwargs):
        self.statements.append((str(sql), tuple(params or ())))
        return self.connection.execute(sql, params, **kwargs)


def _plan(connection, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(
        f"EXPLAIN (COSTS OFF) {sql}", params, prepare=False
    ).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _bulk(database, notebook_id: str) -> None:
    """4,000 sources with two elements each in the promoter's notebook, so the
    planner has a real choice."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'sb-'||g,%s,'t','markdown',now(),now() FROM generate_series(0,3999) g",
            (notebook_id,),
        )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
            "metadata,created_at) SELECT 'eb-'||g,'sb-'||(g%%4000),'paragraph','p',"
            "%s,'{}'::jsonb,now() FROM generate_series(0,7999) g",
            ("text",),
        )
    import psycopg

    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        for table in ("sources", "source_elements"):
            raw.execute(f"VACUUM (ANALYZE) {table}")


def test_pg_approval_reads_keep_primary_key_paths(world):
    from app.repositories.postgres.promotion_provenance_store import plan_for_library

    database = world.repo._runtime.database
    _bulk(database, world.private)
    evidence = [cases.evidence("sb-7", "eb-7", "q"), cases.evidence("sb-9", "eb-9", "q")]
    with database.connect() as db:
        captured = _CapturingConnection(db)
        plan = plan_for_library(captured, world.base, evidence)
        assert [entry["origin_source_id"] for entry in plan.evidence] == ["sb-7", "sb-9"]
        assert len(captured.statements) == 2
        plans = [_plan(db, sql, params) for sql, params in captured.statements]
    sources_plan, elements_plan = plans
    assert "Seq Scan" not in sources_plan and "pk_sources" in sources_plan, sources_plan
    assert "Seq Scan" not in elements_plan, elements_plan
    assert "pk_source_elements" in elements_plan, elements_plan
    assert "pk_sources" in elements_plan, elements_plan


def test_pg_kg_target_page_keeps_its_plan_with_the_promotion_term(world):
    """The extra ``source_type <> 'promotion'`` conjunct is a filter on the
    rows the notebook index already drives; it adds no scan."""
    database = world.repo._runtime.database
    _bulk(database, world.base)
    with database.connect() as db:
        captured = _CapturingConnection(db)
        world.repo._runtime.knowledge.source_build_state_page(
            captured, world.base, None, "", 100)
        ((sql, params),) = captured.statements
        plan = _plan(db, sql, params)
    assert "promotion" in sql
    assert "Seq Scan on sources" not in plan, plan
