"""``AskService._run_reasoning_stage`` / ``_draft_reasoning_response`` characterization
on SQLite.

The world, the scenarios, the observation and the behaviour assertions live in
``tests/reasoning_stage_characterization_cases.py`` (its docstring explains what is
observed and what is asserted); PostgreSQL runs the same scenarios in
``tests/postgres/test_reasoning_stage_characterization_pg.py``. Ordinary SQLite
library seeds are copied per test; every Ask and its full observation still runs.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import reasoning_stage_characterization_cases as cases
from tests import reasoning_stage_world as world

BACKEND = world.BACKEND


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_run_reasoning_stage_characterization(scenario, tmp_path_factory, tmp_path, monkeypatch):
    seeded = world.materialize(scenario, tmp_path_factory, tmp_path)
    root, payload = seeded if seeded is not None else (tmp_path, None)
    db_name = "world.db" if seeded is not None else "ask.db"
    cases.scrub_environment(monkeypatch, root, f"sqlite:///{root / db_name}")
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = SQLiteRepository(
        Settings(_env_file=None),
        gap_consult_host=cases.make_gap_host() if scenario.gap_plugin else None,
    )
    try:
        observation = cases.observe(
            repo, BACKEND, scenario, root, monkeypatch,
            prepare_world=(lambda rig: world.restore_rig(rig, payload, scenario)) if seeded else None,
        )
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


def test_draft_run_refuses_a_field_its_phase_has_not_set():
    """The draft stage's phases run out of order fail loudly; a produced
    ``None`` (no sectioned synthesis) is a value, not "unset"."""
    import dataclasses

    from app.services.ask_service import _ReasoningDraftRun

    inputs = {field.name: None for field in dataclasses.fields(_ReasoningDraftRun)
              if field.default is dataclasses.MISSING}
    run = _ReasoningDraftRun(**inputs)
    with pytest.raises(RuntimeError, match="before the phase that sets it"):
        run.ready("citations")
    run.sectioned = None
    assert run.ready("sectioned") is None
