"""The deletion window's two new scans stay on the partial index (PostgreSQL
twin of ``tests/test_notebook_delete_grace_plans.py``).

``QueryStore.pending_delete_notices`` (every bell snapshot, every notebook
list load) and ``NotebookDeleteJobStore.list_due_pending`` (every sweep tick)
are captured from the production methods on a VACUUM ANALYZEd world of a few
thousand notebooks, none or a few inside a window. In the custom plan and in
the generic plan (``EXPLAIN (GENERIC_PLAN)``, what a prepared statement may
switch to) the notebooks side is ``idx_notebooks_pending_delete_due`` and no
plan node scans ``notebooks`` sequentially: with nothing pending the
statement reads an empty index.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any

import pytest

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_delete_grace_plans"),
]

_NOW = "2026-10-22T08:00:00+00:00"
_INDEX = "idx_notebooks_pending_delete_due"


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def seed(repo: Any, *, pending: int) -> str:
    database = repo._runtime.database
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,"
            "updated_at) SELECT 'gp-u-'||g,'gp-u-'||g||'@x','u','user','active',"
            "'gp-u-'||g,%s,%s FROM generate_series(0,59) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "SELECT 'gp-nb-'||i,'NB '||i,'gp-u-'||(i%%60),"
            "CASE WHEN i%%97=3 THEN 'deleting' ELSE 'ready' END,%s,%s "
            "FROM generate_series(0,2999) i",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "SELECT 'gp-nb-'||i,'gp-u-'||((i+1)%%60),'reader',%s "
            "FROM generate_series(0,2999) i",
            (_NOW,),
        )
        db.execute(
            "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) SELECT 'gp-m-'||g,'gp-nb-'||(g%%3000),"
            "'gp-u-'||((g+1)%%60),'ask_answer','confirmed','t','b',%s,%s "
            "FROM generate_series(0,19999) g",
            (_NOW, _NOW),
        )
        if pending:
            db.execute(
                "UPDATE notebooks SET status='pending_delete',status_before_delete='ready',"
                "delete_after=%s WHERE id IN (SELECT 'gp-nb-'||(i*7) "
                "FROM generate_series(0,%s::int) i)",
                ("2026-10-23T08:00:00+00:00", pending - 1),
            )
    import psycopg

    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE)")
    return "gp-u-1"


@contextmanager
def recorded(repo: Any):
    database = repo._runtime.database
    statements: list[tuple[str, Any]] = []
    connect = database.connect

    class Recording:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
            statements.append((str(sql), params))
            return self._inner.execute(sql, params, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    @contextmanager
    def recording(*args: Any, **kwargs: Any):
        with connect(*args, **kwargs) as db:
            yield Recording(db)

    database.connect = recording
    try:
        yield statements
    finally:
        database.connect = connect


def _nodes(plan: Any):
    if isinstance(plan, dict):
        if "Node Type" in plan:
            yield plan
        for value in plan.values():
            yield from _nodes(value)
    elif isinstance(plan, list):
        for item in plan:
            yield from _nodes(item)


def _plans(repo: Any, sql: str, params: Any) -> dict[str, list[dict]]:
    counter = iter(range(1, 10_000))
    generic = re.sub(r"%s", lambda _m: f"${next(counter)}", sql).replace("%%", "%")
    with repo._runtime.database.connect() as db:
        custom = db.execute("EXPLAIN (COSTS OFF, FORMAT JSON) " + sql, params).fetchone()
        prepared = db.execute(
            "EXPLAIN (GENERIC_PLAN, COSTS OFF, FORMAT JSON) " + generic
        ).fetchone()
    return {
        "custom": list(_nodes(custom["QUERY PLAN"])),
        "generic": list(_nodes(prepared["QUERY PLAN"])),
    }


@pytest.mark.parametrize("pending", [0, 5])
def test_the_window_scans_read_the_partial_index(repo, pending):
    user = seed(repo, pending=pending)
    runtime = repo._runtime
    with recorded(repo) as statements:
        runtime.queries.pending_delete_notices(user, now=_NOW)
        runtime.notebook_delete_jobs.list_due_pending(now=_NOW)
    window = [(sql, params) for sql, params in statements if "delete_after" in sql]
    assert len(window) == 2
    for sql, params in window:
        for mode, nodes in _plans(repo, sql, params).items():
            on_notebooks = [n for n in nodes if n.get("Relation Name") == "notebooks"]
            text = json.dumps(nodes)
            assert on_notebooks, (mode, text)
            # The outer notebooks access is the partial index ...
            assert on_notebooks[0].get("Index Name") == _INDEX, (mode, text)
            # ... and nothing reads notebooks sequentially.
            assert not any(n["Node Type"] == "Seq Scan" for n in on_notebooks), (mode, text)
