"""The deletion window's two new scans stay on the partial index (SQLite).

``QueryStore.pending_delete_notices`` runs on every bell snapshot and every
notebook-list load, and ``NotebookDeleteJobStore.list_due_pending`` on every
sweep tick; almost always no notebook is inside a window. Both statements
are driven by ``idx_notebooks_pending_delete_due`` (``WHERE
status='pending_delete'``), so with nothing pending they read an empty index
instead of every notebook. Captured from the production methods on an
ANALYZEd world of a few hundred notebooks; the PostgreSQL twin is
``tests/postgres/test_notebook_delete_grace_plans_pg.py``.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

_NOW = "2026-10-22T08:00:00+00:00"
_INDEX = "idx_notebooks_pending_delete_due"
N_NOTEBOOKS, N_USERS = 400, 30


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    try:
        yield repository
    finally:
        repository.close()


def seed(repo: Any, *, pending: int) -> str:
    users = [f"gp-u-{g}" for g in range(N_USERS)]
    with repo._runtime.database.write() as db:
        db.executemany(
            "INSERT INTO users (id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            [(u, f"{u}@x", u, "user", "active", u, _NOW, _NOW) for u in users],
        )
        db.executemany(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            [(f"gp-nb-{i}", f"NB {i}", users[i % N_USERS],
              "deleting" if i % 97 == 3 else "ready", _NOW, _NOW)
             for i in range(N_NOTEBOOKS)],
        )
        db.executemany(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,?,?)",
            [(f"gp-nb-{i}", users[(i + 1) % N_USERS], "reader", _NOW)
             for i in range(N_NOTEBOOKS)],
        )
        db.executemany(
            "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(f"gp-m-{g}", f"gp-nb-{g % N_NOTEBOOKS}", users[(g + 1) % N_USERS],
              "ask_answer", "confirmed", "t", "b", _NOW, _NOW) for g in range(4000)],
        )
        for i in range(pending):
            db.execute(
                "UPDATE notebooks SET status='pending_delete',status_before_delete='ready',"
                "delete_after=? WHERE id=?",
                ("2026-10-23T08:00:00+00:00", f"gp-nb-{i * 7}"),
            )
        db.execute("ANALYZE")
    return users[1]


@contextmanager
def recorded(repo: Any):
    database = repo._runtime.database
    statements: list[tuple[str, Any]] = []
    connect = database.connect

    class Recording:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: str, params: Any = ()) -> Any:
            statements.append((sql, params))
            return self._inner.execute(sql, params)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def __enter__(self) -> "Recording":
            self._inner.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._inner.__exit__(*exc)

    database.connect = lambda *a, **k: Recording(connect(*a, **k))
    try:
        yield statements
    finally:
        database.connect = connect


def plan(repo: Any, sql: str, params: Any) -> list[str]:
    with repo._runtime.database.connect() as db:
        return [row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()]


@pytest.mark.parametrize("pending", [0, 3])
def test_the_window_scans_read_the_partial_index(repo, pending):
    user = seed(repo, pending=pending)
    runtime = repo._runtime
    with recorded(repo) as statements:
        runtime.queries.pending_delete_notices(user, now=_NOW)
        runtime.queries.pending_delete_notices(user, now=_NOW, notebook_id="gp-nb-7")
        runtime.notebook_delete_jobs.list_due_pending(now=_NOW)
    window = [(sql, params) for sql, params in statements if "delete_after" in sql]
    assert len(window) == 3
    notices, one, due = (plan(repo, sql, params) for sql, params in window)
    # The audience list and the due scan: the partial index drives, no notebook
    # scan anywhere in the plan.
    for lines in (notices, due):
        assert _INDEX in lines[0], lines
        assert not any(
            line.startswith(("SCAN nb", "SCAN notebooks")) and _INDEX not in line
            for line in lines
        ), lines
    # The one-notebook explanation is a primary-key probe.
    assert lines_start_with_pk(one), one


def lines_start_with_pk(lines: list[str]) -> bool:
    return lines[0].startswith("SEARCH nb USING INDEX sqlite_autoindex_notebooks_1 (id=?)")
