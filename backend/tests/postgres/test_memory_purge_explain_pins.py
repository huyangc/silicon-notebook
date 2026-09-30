"""EXPLAIN pins for the Memory purge's SQL (member exit, hard and bulk delete).

Two kinds:

* ``derived_memory_sources`` under the REAL planner on analysed data (100k
  sources): the statement repeats ``idx_sources_memory_id``'s partial-index
  predicate, so both the custom plan and the generic plan (psycopg prepares
  at the 5th execution; PostgreSQL may switch to a generic plan from about
  the 11th) use that index. Without ``memory_id <> ''`` both were a
  parallel sequential scan of ``sources`` (quality review P2-1: 36 ms vs
  0.46 ms at 300k rows).
* Every statement a real purge page issues — captured from an actual exit
  of the shared scenario world, so the pin follows the code, not a copy of
  its SQL — has an index path: planned with sequential and bitmap scans
  disabled, in the style of ``test_memory_sql_explain_pins.py`` (the world is
  small, so without that switch every plan would be a sequential scan and
  the pin would say nothing).
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from tests.memory_purge_cases import EMBED_DIM, build_world, self_exit

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_purge_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


class _Recording:
    def __init__(self, inner, statements):
        self._inner = inner
        self._statements = statements

    def execute(self, sql, params=None, *args, **kwargs):
        self._statements.append((str(sql), params))
        return self._inner.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextmanager
def _recorded(repo, monkeypatch):
    database = repo._runtime.database
    statements: list[tuple[str, object]] = []
    for name in ("connect", "write"):
        original = getattr(database, name)

        def recording(*args, _original=original, **kwargs):
            @contextmanager
            def manager():
                with _original(*args, **kwargs) as db:
                    yield _Recording(db, statements)

            return manager()

        monkeypatch.setattr(database, name, recording)
    try:
        yield statements
    finally:
        monkeypatch.undo()


def _plan(connection, sql: str, params) -> str:
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def test_derived_memory_sources_uses_the_partial_index_on_analysed_data(
    repo, monkeypatch
):
    import psycopg
    import psycopg.sql

    database = repo._runtime.database
    owner = repo.create_user("p00100001", "pw123456")
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb-pin','N','','','ready',%s,%s,%s,'personal')",
            (owner.id, _NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||g,'nb-pin','t',"
            "CASE WHEN g%%50=0 THEN 'memory' ELSE 'upload' END,"
            "CASE WHEN g%%50=0 THEN 'mem-'||g WHEN g%%2=0 THEN '' END,%s,%s "
            "FROM generate_series(0,99999) g",
            (_NOW, _NOW),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) sources")
    refs = [(f"mem-{g}", "nb-pin") for g in range(0, 10000, 50)]
    with _recorded(repo, monkeypatch) as statements:
        found = repo._runtime.memory_service.store.derived_memory_sources(refs)
    assert len(found) == 200
    [(sql, params)] = statements
    with database.connect() as connection:
        custom = _plan(connection, sql, params)
        connection.execute("SET plan_cache_mode = force_generic_plan")
        connection.execute(
            "PREPARE pin_derived(text[]) AS " + sql.replace("%s", "$1")
        )
        generic = "\n".join(
            str(row["QUERY PLAN"])
            # A utility statement takes no bind parameters: the array goes in
            # as a literal (the prepared statement's own parameter stays $1).
            for row in connection.execute(
                psycopg.sql.SQL("EXPLAIN (COSTS OFF) EXECUTE pin_derived({})").format(
                    psycopg.sql.Literal(list(params[0]))
                )
            ).fetchall()
        )
        connection.execute("DEALLOCATE pin_derived")
        connection.execute("RESET plan_cache_mode")
    for plan in (custom, generic):
        assert "idx_sources_memory_id" in plan, plan
        assert "Seq Scan" not in plan, plan


_SMALL_TABLES = ("unified_kg_state", "notebooks", "notebook_members", "users")


def test_every_statement_of_a_purge_page_has_an_index_path(repo, monkeypatch):
    world = build_world(repo, postgres=True)
    with _recorded(repo, monkeypatch) as statements:
        assert self_exit(world, world.alice, world.shared, 1) == 1
    planned = [
        (sql, params) for sql, params in statements
        if sql.lstrip().upper().startswith(("SELECT", "DELETE", "UPDATE", "WITH"))
    ]
    assert len(planned) >= 20, planned
    with repo._runtime.database.connect() as connection:
        connection.execute("SET enable_seqscan = off")
        connection.execute("SET enable_bitmapscan = off")
        offenders = []
        for sql, params in planned:
            plan = _plan(connection, sql, params)
            scans = [
                line for line in plan.splitlines()
                if "Seq Scan on " in line
                and not any(f"Seq Scan on {table}" in line for table in _SMALL_TABLES)
            ]
            if scans:
                offenders.append((sql, scans))
        connection.execute("RESET enable_seqscan")
        connection.execute("RESET enable_bitmapscan")
    assert offenders == [], offenders
