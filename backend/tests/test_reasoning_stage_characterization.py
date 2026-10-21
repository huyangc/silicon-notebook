"""``AskService._run_reasoning_stage`` characterization on SQLite.

The world, the scenarios, the observation and the behaviour assertions live in
``tests/reasoning_stage_characterization_cases.py`` (its docstring explains what is
observed and what is asserted); PostgreSQL runs the same scenarios in
``tests/postgres/test_reasoning_stage_characterization_pg.py``. This module only says
which tables SQLite has.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite import knowledge_counts_cache
from app.services.sqlite_repository import SQLiteRepository
from tests import reasoning_stage_characterization_cases as cases

_FTS_SHADOW = ("_fts_config", "_fts_content", "_fts_data", "_fts_docsize", "_fts_idx")


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return [n for n in names if not n.startswith("sqlite_") and not n.endswith(_FTS_SHADOW)]


BACKEND = cases.Backend(
    name="sqlite",
    placeholder="?",
    tables=_tables,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_run_reasoning_stage_characterization(scenario, tmp_path, monkeypatch):
    cases.scrub_environment(monkeypatch, tmp_path, f"sqlite:///{tmp_path / 'ask.db'}")
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = SQLiteRepository(
        Settings(_env_file=None),
        gap_consult_host=cases.make_gap_host() if scenario.gap_plugin else None,
    )
    try:
        observation = cases.observe(repo, BACKEND, scenario, tmp_path, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)


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
