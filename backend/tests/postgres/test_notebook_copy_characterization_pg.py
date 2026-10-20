"""``NotebookCopyService.copy_notebook`` characterization on real PostgreSQL.

The twin of ``tests/test_notebook_copy_characterization.py``: the same world, scenarios
and both assertion layers from ``tests/notebook_copy_characterization_cases.py``; this
module only says how PostgreSQL stores a row (JSON columns as jsonb), which tables the
isolated schema has, and how statements are recorded (every ``Cursor.execute`` /
``executemany`` on the copy's thread, the pool's own lend/return probes left out).

Re-record the split oracle (never needed for a zero-behaviour-change refactor)::

    REGEN_NOTEBOOK_COPY_CHARACTERIZATION=1 TEST_POSTGRES_URL=... PYTHONPATH=backend \\
      python -m pytest backend/tests/postgres/test_notebook_copy_characterization_pg.py -n0
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
from psycopg import Cursor
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.factory import create_repository
from app.repositories.postgres import knowledge_counts_cache
from tests import notebook_copy_characterization_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_copy_characterization"),
]

ORACLE_DIR = Path(__file__).parents[1] / "fixtures" / "notebook_copy_characterization" / "postgres"
REGEN = os.environ.get("REGEN_NOTEBOOK_COPY_CHARACTERIZATION") == "1"


def _insert(db, table: str, values: dict) -> None:
    values = dict(values)
    if table == "users":
        values.update(username=values["id"], password_hash="", password_salt="",
                      password_iterations=0)
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
        [Jsonb(values[c]) if c in cases.JSON_COLUMNS else values[c] for c in columns],
    )


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    return [row["table_name"] for row in rows]


def _pool_housekeeping(query: str) -> bool:
    """The pool's own lend probe and return reset (``PostgresDatabase``): issued at
    lend/return time, their count follows connection reuse, not the copy."""
    return query in ("", "RESET ALL") or query.startswith("SELECT set_config('statement_timeout'")


@contextlib.contextmanager
def _record_sql(repo, recorder: cases.Recorder, monkeypatch):
    real_execute, real_executemany = Cursor.execute, Cursor.executemany

    def note(kind: str, query, params) -> None:
        text = " ".join(str(query).split())
        if recorder.on_thread() and not _pool_housekeeping(text):
            recorder.timeline.append({
                kind: recorder.canon.text(text), "params": recorder.canon(params),
            })

    def execute(self, query, params=None, *args, **kwargs):
        note("sql", query, params)
        return real_execute(self, query, params, *args, **kwargs)

    def executemany(self, query, params_seq, *args, **kwargs):
        params_seq = list(params_seq)
        note("sql_many", query, params_seq)
        return real_executemany(self, query, params_seq, *args, **kwargs)

    monkeypatch.setattr(Cursor, "execute", execute)
    monkeypatch.setattr(Cursor, "executemany", executemany)
    yield


BACKEND = cases.Backend(
    name="postgres",
    insert=_insert,
    tables=_tables,
    record_sql=_record_sql,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_copy_notebook_characterization_pg(scenario, postgres_scope, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(storage))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = create_repository(Settings())
    try:
        observation = cases.observe(repo, BACKEND, scenario, storage, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)
    cases.assert_matches_oracle(observation, ORACLE_DIR / f"{scenario.name}.json", regen=REGEN)


def test_split_oracle_covers_every_scenario_pg():
    """One oracle file per scenario, no stray files."""
    if REGEN:
        return
    assert sorted(p.stem for p in ORACLE_DIR.glob("*.json")) == sorted(
        s.name for s in cases.SCENARIOS)
