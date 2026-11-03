"""Edit permission predicate and access-level lookup on SQLite (share-edit P1-T1).

The scenarios live in ``tests/notebook_edit_access_cases.py`` and run unchanged
on PostgreSQL through ``tests/postgres/test_notebook_edit_access_pg.py``. This
file adds the SQLite cost pin of the access-level statement.
"""
from __future__ import annotations

import re

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.notebook_edit_access_cases import CASES, build_world


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'edit-access.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_edit_access(repo, case):
    CASES[case](build_world(repo, postgres=False))


class _RecordingConnection:
    """Records every ``execute`` and forwards it unchanged."""

    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def execute(self, sql, params=()):
        self._statements.append((sql, tuple(params)))
        return self._connection.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._connection, name)


_ACCESS_TABLE_SCAN = re.compile(
    r"\bSCAN (?:TABLE )?(notebook_grants|notebook_members|group_members|"
    r"lag|lagm|laga|leg|legm|lega|lrm|lrg|lrgm|lrga)\b"
)


def test_access_level_plan_has_no_scan(repo, monkeypatch):
    """The statement ``notebook_access_level`` really executes (recorded, not
    rebuilt here) reaches every access table through an index: no ``SCAN`` of
    ``notebook_grants`` / ``notebook_members`` / ``group_members`` under any of
    the aliases the CASE arms use. The world holds every arm's rows, so the
    planner has nothing to skip."""
    from contextlib import contextmanager

    world = build_world(repo, postgres=False)
    database = repo._runtime.database
    statements: list[tuple[str, tuple]] = []
    original = database.connect

    @contextmanager
    def recording(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield _RecordingConnection(connection, statements)

    monkeypatch.setattr(database, "connect", recording)
    access = repo._runtime.sharing_store.notebook_access_level(
        world.notebook, world.users["editor"]
    )
    monkeypatch.undo()
    assert access is not None and access.level == "edit"
    assert len(statements) == 1, statements
    sql, params = statements[0]
    with database.connect() as db:
        plan = [
            str(row["detail"]) for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        ]
    text = "\n".join(plan)
    assert not _ACCESS_TABLE_SCAN.search(text), text
    # every arm of the CASE is an index probe of its own (none folded into a scan)
    for alias in ("lag", "lagm", "laga", "leg", "legm", "lega", "lrm", "lrg", "lrgm", "lrga"):
        assert re.search(rf"\bSEARCH {alias} USING (?:COVERING )?INDEX\b", text), (alias, text)
