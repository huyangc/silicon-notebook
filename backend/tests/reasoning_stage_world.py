"""Only the ordinary SQLite reasoning library seed is reusable.

Uploaded sources keep absolute paths, so PINNED materialization replaces the
live world for every test. No repository, model, recorder or hook is reused.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookSummary
from app.models.sources import SourceDetail, UploadedSourceSummary
from app.repositories.sqlite import knowledge_counts_cache
from app.services.sqlite_repository import SQLiteRepository
from tests import reasoning_stage_characterization_cases as cases
from tests.world_templates import PINNED, world_template

_FTS_SHADOW = ("_fts_config", "_fts_content", "_fts_data", "_fts_docsize", "_fts_idx")


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return [n for n in names if not n.startswith("sqlite_") and not n.endswith(_FTS_SHADOW)]


BACKEND = cases.Backend(
    name="sqlite", placeholder="?", tables=_tables,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


def eligible(scenario: cases.Scenario) -> bool:
    """No custom world, environment, extension or model script is templated."""
    return (scenario.world is cases.library_world and not scenario.env
            and scenario.gap_plugin is False
            and type(scenario.script) is cases.Script and scenario.script == cases.Script())


def _model_state(rig: cases.Rig) -> dict:
    # Ingestion uses these already-bound clients. If a future seed consumes
    # scripted reflection replies, fail instead of silently
    # handing Ask a fresh state different from the self-seeded path.
    models = rig.models
    assert models.recorder is rig.recorder
    assert models.script == cases.Script()
    assert models.reflects == []
    assert not rig.recorder.active
    assert rig.recorder.timeline == [] and rig.recorder.worker == []
    return {"reflects": list(models.reflects)}


def _checked_result(result, reread, expected_type):
    assert type(result) is expected_type
    actual = result.model_dump(mode="python")
    expected = reread.model_dump(mode="python")
    assert actual == expected, {key: (actual.get(key), expected.get(key))
                                for key in actual.keys() | expected.keys()
                                if actual.get(key) != expected.get(key)}
    return result


def _check_seed_returns(repo, patch):
    """A correct seed write must never conceal an incorrect creation return."""
    catalog = repo._runtime.catalog
    create = catalog.create_notebook
    upload = repo.upload_sources
    process = repo.process_source

    def checked_create(*args, **kwargs):
        result = create(*args, **kwargs)
        # create_notebook returns the creator's summary; a context-free
        # get_notebook intentionally projects an anonymous viewer instead.
        token = set_request_user(repo.current_user())
        try:
            reread = catalog.get_notebook(result.id)
        finally:
            reset_request_user(token)
        return _checked_result(result, reread, NotebookSummary)

    def checked_upload(*args, **kwargs):
        results = upload(*args, **kwargs)
        assert type(results) is list and len(results) == 1
        for result in results:
            reread = UploadedSourceSummary(**repo.get_source(result.id).model_dump(), reused=False)
            _checked_result(result, reread, UploadedSourceSummary)
        return results

    def checked_process(*args, **kwargs):
        result = process(*args, **kwargs)
        return _checked_result(result, repo.get_source(result.id), SourceDetail)

    patch.setattr(catalog, "create_notebook", checked_create)
    patch.setattr(repo, "upload_sources", checked_upload)
    patch.setattr(repo, "process_source", checked_process)


def _open(root: Path) -> SQLiteRepository:
    # world_templates starts from the process baseline, while this corpus
    # deliberately strips all deployment settings. Use the corpus's exact
    # scrub both during build and reopen, not the helper's generic defaults.
    with pytest.MonkeyPatch.context() as patch:
        cases.scrub_environment(patch, root, f"sqlite:///{root / 'world.db'}")
        return SQLiteRepository(Settings(_env_file=None))


def _build(root: Path) -> dict:
    with pytest.MonkeyPatch.context() as patch:
        cases.scrub_environment(patch, root, f"sqlite:///{root / 'world.db'}")
        repo = SQLiteRepository(Settings(_env_file=None))
        try:
            rig = cases.make_rig(repo, BACKEND, cases.Script(), root)
            cases._clear_process_caches(BACKEND)
            initial_state = _model_state(rig)
            _check_seed_returns(repo, patch)
            cases.library_world(rig)
            cases._drain_background(repo)
            assert _model_state(rig) == initial_state
            return {"notebook_id": rig.notebook_id, "user_id": rig.user_id,
                    "sources": dict(rig.sources), "model_state": initial_state}
        finally:
            cases._drain_background(repo)
            repo.close()


def materialize(scenario, tmp_path_factory, tmp_path):
    if not eligible(scenario):
        return None
    template = world_template(
        tmp_path_factory, "reasoning-stage-ordinary-library", _build, _open,
        layout=PINNED, env=cases._SCENARIO_ENV,
    )
    return template.materialize(tmp_path)


def restore_rig(rig: cases.Rig, payload: dict) -> None:
    assert _model_state(rig) == payload["model_state"]
    rig.notebook_id = payload["notebook_id"]
    rig.user_id = payload["user_id"]
    rig.sources = dict(payload["sources"])
