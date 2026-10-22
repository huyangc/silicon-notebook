"""``AskService._run_reasoning_stage`` / ``_draft_reasoning_response`` characterization
on SQLite.

The world, the scenarios, the observation and both assertion layers live in
``tests/reasoning_stage_characterization_cases.py`` (its docstring explains the two
layers); PostgreSQL runs the same scenarios in
``tests/postgres/test_reasoning_stage_characterization_pg.py``. This module only says
which tables SQLite has and how its statements are traced.

Re-record the split oracle (never needed for a zero-behaviour-change refactor)::

    REGEN_DRAFT_REASONING_CHARACTERIZATION=1 PYTHONPATH=backend \\
      python -m pytest backend/tests/test_reasoning_stage_characterization.py -n0
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import knowledge_counts_cache
from app.repositories.sqlite.database import SqliteDatabase
from app.services.sqlite_repository import SQLiteRepository
from tests import reasoning_stage_characterization_cases as cases

ORACLE_DIR = Path(__file__).parent / "fixtures" / "draft_reasoning_characterization" / "sqlite"
REGEN = os.environ.get("REGEN_DRAFT_REASONING_CHARACTERIZATION") == "1"

_FTS_SHADOW = ("_fts_config", "_fts_content", "_fts_data", "_fts_docsize", "_fts_idx")


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return [n for n in names if not n.startswith("sqlite_") and not n.endswith(_FTS_SHADOW)]


def _application_statement(statement: str) -> bool:
    """Statements the application itself issued: FTS5 runs nested statements of its own
    (``-- `` comments, quoted ``'main'.`` shadow tables) that change with the SQLite
    version and are not the Ask's."""
    return not statement.startswith("--") and "'main'." not in statement


@contextlib.contextmanager
def _record_sql(repo, recorder: cases.Recorder, monkeypatch):
    def trace(statement: str) -> None:
        if recorder.on_thread() and _application_statement(statement):
            recorder.timeline.append({"sql": recorder.canon.text(" ".join(statement.split()))})

    original = SqliteDatabase._new_connection

    def new_connection(self, *args, **kwargs):
        connection = original(self, *args, **kwargs)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(SqliteDatabase, "_new_connection", new_connection)
    local = getattr(repo._runtime.database._local, "conn", None)
    if local is not None:
        local.set_trace_callback(trace)
    try:
        yield
    finally:
        if local is not None:
            with contextlib.suppress(Exception):  # closed by a reconnect mid-run
                local.set_trace_callback(None)


BACKEND = cases.Backend(
    name="sqlite",
    placeholder="?",
    tables=_tables,
    record_sql=_record_sql,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_run_reasoning_stage_characterization(scenario, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    cases.scrub_environment(monkeypatch, tmp_path, f"sqlite:///{tmp_path / 'ask.db'}")
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = SQLiteRepository(
        Settings(_env_file=None),
        gap_consult_host=cases.make_gap_host() if scenario.gap_plugin else None,
    )
    try:
        observation = cases.observe(repo, BACKEND, scenario, storage, tmp_path, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)
    cases.assert_matches_oracle(observation, ORACLE_DIR / f"{scenario.name}.json", regen=REGEN)


def test_split_oracle_covers_every_scenario():
    """One oracle file per scenario, no stray files."""
    if REGEN:
        return
    assert sorted(p.stem for p in ORACLE_DIR.glob("*.json")) == sorted(
        s.name for s in cases.SCENARIOS)


def test_run_state_refuses_a_field_its_phase_has_not_set():
    """Phases run out of order fail loudly instead of reading an empty default;
    a produced ``None`` (no Knowhow batch) is a value, not "unset"."""
    from app.services.ask_service import _ReasoningStageRun

    run = _ReasoningStageRun(prepared=None, runtime=None, intent_contract=None,
                             on_trace=None, cancel_event=None, pre_trace=[])
    with pytest.raises(RuntimeError, match="before the phase that sets it"):
        run.ready("trace")
    run.structured_batch = None
    assert run.ready("structured_batch") is None
