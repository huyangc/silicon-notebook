"""Isolation and conservative selection of the ordinary reasoning seed."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import reasoning_stage_characterization_cases as cases
from tests import reasoning_stage_world as world


def _ordinary():
    return next(s for s in cases.SCENARIOS if s.name == "answer_sync")


@pytest.mark.parametrize("change", [
    {"world": cases.documents_world},
    {"env": {"ASK_REASONING_TOP_K": "1"}},
    {"gap_plugin": True},
    {"script": cases.Script(reflects=({"next_action": "answer"},))},
    {"script": cases.Script(plan_query="其他查询")},
    {"script": cases.Script(sheet_plan={"operation": "select"})},
    {"script": cases.Script(failures={"ask_answer": RuntimeError("down")})},
    {"script": cases.Script(cite="none")},
    {"script": cases.Script(answer_fails_on=("marker",))},
    {"script": cases.Script(uncited_on=("marker",))},
    {"script": cases.Script(cite_after="marker")},
])
def test_special_worlds_keep_the_original_path(change, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("a specialized scenario requested the ordinary seed")

    monkeypatch.setattr(world, "world_template", forbidden)
    scenario = dataclasses.replace(_ordinary(), **change)
    assert world.materialize(scenario, None, tmp_path) is None


def test_ordinary_seed_restores_identities_files_and_model_state_without_leaks(
        tmp_path_factory, tmp_path, monkeypatch):
    scenario = _ordinary()
    root, payload = world.materialize(scenario, tmp_path_factory, tmp_path)
    cases.scrub_environment(monkeypatch, root, f"sqlite:///{root / 'world.db'}")
    original_ids = dict(payload["sources"])
    notebook_id = payload["notebook_id"]
    storage_probe = root / "storage" / "private-test-file"
    repo = SQLiteRepository(Settings(_env_file=None))
    try:
        rig = cases.make_rig(repo, world.BACKEND, cases.Script(), root)
        world.restore_rig(rig, payload)
        assert rig.notebook_id == notebook_id
        assert rig.user_id == repo._runtime._current_user_id()
        assert rig.sources == original_ids
        original_name = repo.get_notebook(notebook_id).name
        source = repo.get_source(original_ids["layout.md"])
        source_path = Path(source.file_path)
        original_bytes = source_path.read_bytes()
        assert original_bytes == cases.LAYOUT_MD.encode()
        assert root in source_path.parents
        rig.sql("UPDATE notebooks SET name=? WHERE id=?", ("polluted", notebook_id))
        source_path.write_bytes(b"polluted source")
        storage_probe.write_bytes(b"private")
        rig.sources.clear()
        rig.models.reflects.append({"next_action": "wrong"})
        payload["sources"].clear()
        payload["model_state"]["reflects"].append("polluted payload")
    finally:
        cases._drain_background(repo)
        repo.close()

    next_root, next_payload = world.materialize(scenario, tmp_path_factory, tmp_path)
    assert next_root == root  # Stored absolute source paths remain valid.
    assert next_payload["sources"] == original_ids
    assert next_payload["model_state"] == {"reflects": []}
    assert not storage_probe.exists()
    assert source_path.read_bytes() == original_bytes
    repo = SQLiteRepository(Settings(_env_file=None))
    try:
        next_rig = cases.make_rig(repo, world.BACKEND, cases.Script(), next_root)
        world.restore_rig(next_rig, next_payload)
        assert next_rig is not rig
        assert next_rig.recorder is not rig.recorder
        assert next_rig.hooks is not rig.hooks
        assert next_rig.models is not rig.models
        assert next_rig.models.reflects == []
        assert repo.get_notebook(notebook_id).name == original_name
        assert repo.get_source(original_ids["layout.md"]).file_path == str(source_path)
    finally:
        cases._drain_background(repo)
        repo.close()


def test_seed_model_reflection_state_must_equal_a_fresh_default():
    recorder = cases.Recorder(cases.Canon())
    models = cases.ScriptedModels(recorder, cases.Script())
    rig = cases.Rig(None, world.BACKEND, recorder, cases.Hooks(), models, Path("unused"))
    assert world._model_state(rig) == {"reflects": []}
    models.reflects.append({"next_action": "answer"})
    with pytest.raises(AssertionError):
        world._model_state(rig)
