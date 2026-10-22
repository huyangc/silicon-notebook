"""``AskService._run_reasoning_stage`` / ``_draft_reasoning_response`` characterization
on real PostgreSQL.

The twin of ``tests/test_reasoning_stage_characterization.py``: the same world,
scenarios and both assertion layers from ``tests/reasoning_stage_characterization_cases.py``;
this module only says which tables the isolated schema has and how statements are
recorded (every ``Cursor.execute`` / ``executemany`` on the Ask's thread, the pool's
own lend/return probes left out).

Re-record the split oracle (never needed for a zero-behaviour-change refactor)::

    REGEN_DRAFT_REASONING_CHARACTERIZATION=1 TEST_POSTGRES_URL=... PYTHONPATH=backend \\
      python -m pytest backend/tests/postgres/test_reasoning_stage_characterization_pg.py -n0
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
from psycopg import Cursor

from app.core.config import Settings
from app.repositories.postgres import knowledge_counts_cache
from app.services.repository_factory import create_repository
from tests import reasoning_stage_characterization_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_reasoning_stage_characterization"),
]

ORACLE_DIR = (Path(__file__).parents[1] / "fixtures" / "draft_reasoning_characterization"
              / "postgres")
REGEN = os.environ.get("REGEN_DRAFT_REASONING_CHARACTERIZATION") == "1"


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
    lend/return time, their count follows connection reuse, not the Ask."""
    return query in ("", "RESET ALL") or query.startswith("SELECT set_config('statement_timeout'")


def _any_lists_as_sets(text: str, params):
    """``= ANY(%s)`` id lists the store builds from a Python set: their order
    follows the hash seed, so they are compared as sets."""
    if "ANY(" not in text or not isinstance(params, (list, tuple)):
        return params
    return [sorted(value) if isinstance(value, list)
            and all(isinstance(item, str) for item in value) else value
            for value in params]


@contextlib.contextmanager
def _record_sql(repo, recorder: cases.Recorder, monkeypatch):
    real_execute, real_executemany = Cursor.execute, Cursor.executemany

    def note(kind: str, query, params) -> None:
        text = " ".join(str(query).split())
        if recorder.on_thread() and not _pool_housekeeping(text):
            recorder.timeline.append({
                kind: recorder.canon.text(text),
                "params": recorder.canon(_any_lists_as_sets(text, params)),
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
    placeholder="%s",
    tables=_tables,
    record_sql=_record_sql,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_run_reasoning_stage_characterization_pg(scenario, postgres_scope, tmp_path,
                                                 monkeypatch):
    storage = tmp_path / "storage"
    cases.scrub_environment(monkeypatch, tmp_path, postgres_scope.url)
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = create_repository(
        Settings(_env_file=None),
        gap_consult_host=cases.make_gap_host() if scenario.gap_plugin else None,
    )
    try:
        observation = cases.observe(repo, BACKEND, scenario, storage, tmp_path, monkeypatch)
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
