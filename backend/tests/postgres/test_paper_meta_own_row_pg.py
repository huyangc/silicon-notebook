"""PostgreSQL runner for ``tests/paper_meta_own_row_cases.py`` plus the
PostgreSQL plan pins of the narrowed readers (custom and generic plans).

SQLite twin: ``tests/test_paper_meta_own_row.py``.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.core.config import Settings
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.source_store import PAPER_META_ELIGIBLE_SQL
from tests import paper_meta_own_row_cases as cases
from tests.model_testkit import RecordingModelProvider

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_paper_meta_own_row"),
]

PH = "%s"
_INDEX = "idx_source_paper_meta_source_nb"


def _settings(url: str, storage: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=url,
        storage_dir=str(storage),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def repo(postgres_scope, tmp_path, monkeypatch):
    from app.services.postgres_repository import PostgresRepository

    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repository = PostgresRepository(
        _settings(postgres_scope.url, tmp_path / "storage"),
        model_provider=RecordingModelProvider(),
    )
    repository.postgres_url = postgres_scope.url
    try:
        yield repository
    finally:
        repository.close()


def test_backfill_finds_and_heals_a_legacy_row(repo, monkeypatch):
    cases.case_backfill_finds_and_heals_a_legacy_row(repo, PH, monkeypatch)


def test_display_joins_drop_a_legacy_row(repo):
    cases.case_display_joins_drop_a_legacy_row(repo, PH)


def test_well_formed_rows_read_byte_identically(repo):
    cases.case_well_formed_rows_read_byte_identically(
        repo, PH, eligible_sql=PAPER_META_ELIGIBLE_SQL,
        missing_order=' ORDER BY s.created_at,s.id COLLATE "C"',
    )


def _seed_volume(repo, world: cases.World) -> None:
    """3000 more sources in the target notebook (2000 with a metadata row) and
    40 neighbour notebooks of 100 sources (70 with a row), so per-notebook
    selectivity looks like a multi-tenant deployment. Each metadata row carries
    a production-sized raw_json (the LLM payload, about a kilobyte): with
    near-empty rows a sequential scan of the table is as cheap as the index
    and the pin would measure the fixture, not the reader. Then VACUUM ANALYZE
    so index-only scans are costed on a settled visibility map."""
    now = normalize_timestamp("2026-07-22T10:00:00+00:00")
    neighbours = [f"nb-pmor-{index:02d}" for index in range(40)]
    with repo._runtime.source_store.database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for notebook_id in neighbours:
            db.execute(
                "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (notebook_id, notebook_id, world.owner_id, now, now),
            )
        for notebook_id, prefix, count, with_meta in (
            (world.notebook_id, "pmor-big", 3000, 2000),
            *((nb, f"pmor-{nb[-2:]}", 100, 70) for nb in neighbours),
        ):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,"
                "updated_at) SELECT %s||'-'||lpad(g::text,5,'0'), %s, 'Doc '||g, 'pdf', "
                "'extracted','parsed','d.pdf','uploads/d.pdf',1,'h'||g,'','',%s,%s "
                "FROM generate_series(0,%s) g",
                (prefix, notebook_id, now, now, count - 1),
            )
            db.execute(
                "INSERT INTO source_paper_meta(source_id,notebook_id,is_paper,paper_title,"
                "raw_json,created_at,updated_at) SELECT %s||'-'||lpad(g::text,5,'0'), %s, "
                "(g %% 7 <> 0)::int, 'Paper '||g, "
                "jsonb_build_object('llm', repeat('abstract text ', 80)), %s, %s "
                "FROM generate_series(0,%s) g",
                (prefix, notebook_id, now, now, with_meta - 1),
            )
    with psycopg.connect(repo.postgres_url, autocommit=True) as connection:
        connection.execute("SET statement_timeout = 0")
        for table in ("sources", "source_paper_meta", "notebooks"):
            connection.execute(f"VACUUM ANALYZE {table}")


def _plans(connection, sql: str, params) -> tuple[str, str]:
    custom = "\n".join(
        str(row["QUERY PLAN"]) for row in connection.execute(
            f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    )
    numbered = sql
    for index in range(1, len(params) + 1):
        numbered = numbered.replace("%s", f"${index}", 1)
    connection.execute("SET LOCAL plan_cache_mode = force_generic_plan")
    connection.execute(f"PREPARE pmor_pin AS {numbered}")
    literals = ",".join("'" + str(value).replace("'", "''") + "'" for value in params)
    generic = "\n".join(
        str(row["QUERY PLAN"]) for row in connection.execute(
            f"EXPLAIN (COSTS OFF) EXECUTE pmor_pin({literals})").fetchall()
    )
    connection.execute("DEALLOCATE pmor_pin")
    connection.execute("RESET plan_cache_mode")
    return custom, generic


def test_paper_meta_own_row_plans_stay_covered(repo, monkeypatch):
    """Plan pins, no planner knobs. The missing-metadata consumers and the
    analytics counts reach source_paper_meta only through the covering
    ``idx_source_paper_meta_source_nb`` (Index Only Scan), never the primary
    key's heap probe or a notebook-range scan with a per-row join filter --
    the shape ``m.notebook_id = s.notebook_id`` (an equivalence-class key)
    produced on mid-size notebooks before the narrowing was spelled
    ``IS NOT DISTINCT FROM``. Custom and generic plans both. The statements are
    captured from the real calls.

    The pin reads cost-based plans, so it depends on the statistics the seed
    leaves behind (VACUUM ANALYZE after a production-wide load): it pins the
    readers' shape under realistic statistics, not a plan the planner could
    never leave."""
    world = cases.build_world(repo)
    _seed_volume(repo, world)
    runtime = repo._runtime
    store = runtime.source_store
    captured: list[tuple[str, tuple]] = []
    original = psycopg.Connection.execute

    def spying_execute(self, query, params=None, **kwargs):
        text = str(query)
        if "source_paper_meta" in text and text.lstrip().upper().startswith("SELECT"):
            captured.append((text, tuple(params or ())))
        return original(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", spying_execute)
    try:
        store.sources_missing_paper_meta(world.notebook_id)
        repo.maintenance.paper_metadata_source_ids_page(world.notebook_id, limit=500)
        with store.database.connect() as db:
            runtime.queries.notebook_paper_meta_missing(db, world.notebook_id)
        runtime.queries.notebook_analytics(world.notebook_id)
    finally:
        monkeypatch.setattr(psycopg.Connection, "execute", original)

    assert len(captured) == 4, [sql for sql, _ in captured]
    with store.database.connect() as connection:
        for sql, params in captured:
            assert "m.notebook_id IS NOT DISTINCT FROM s.notebook_id" in sql, sql
            for plan in _plans(connection, sql, params):
                assert f"Index Only Scan using {_INDEX} on source_paper_meta m" in plan, plan
                assert "pk_source_paper_meta" not in plan, plan
                assert "idx_source_paper_meta_nb" not in plan, plan
                assert "Seq Scan on source_paper_meta" not in plan, plan
