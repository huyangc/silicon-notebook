"""SQLite: cross-environment sync carries only ADOPTED auto user-memory
suggestions (plan T11; design §6.6 / D15 (a)). PostgreSQL twin:
``tests/postgres/test_sync_memory_suggestions_pg.py``; shared cases:
``tests/sync_memory_suggestion_cases.py``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.repositories.postgres import memory_sql as postgres_memory_sql
from app.repositories.sqlite import memory_sql as sqlite_memory_sql
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_adoption_cases as adoption
from tests import memory_suggest_write_cases as writes
from tests import sync_memory_suggestion_cases as cases


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    yield
    set_request_user(None)


def _side(root: Path, opened: list) -> tuple[cases.Side, SQLiteRepository]:
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite:///{root / 'sync.db'}",
        storage_dir=str(root / "storage"),
        _env_file=None,
        event_log_enabled=False,
        llm_log_enabled=False,
    )
    repo = SQLiteRepository(settings)
    opened.append(repo)
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def as_json(value):
        return json.dumps(value, ensure_ascii=False)

    return cases.Side(settings=settings, run=run, rows=rows, json=as_json), repo


@pytest.fixture
def pair(tmp_path: Path):
    opened: list[SQLiteRepository] = []
    try:
        source, source_repo = _side(tmp_path / "a", opened)
        target, _ = _side(tmp_path / "b", opened)
        world = adoption.build_world(writes.Env(
            store=source_repo._runtime.memory_store,
            suggest=source_repo._runtime.memory_suggest_store,
            run=source.run,
            rows=source.rows,
            json=source.json,
        ))
        yield cases.Pair(source=source, target=target, world=world, out=tmp_path / "out")
    finally:
        for repo in opened:
            repo.close()


def test_row_filter_fragment_is_the_same_sql_on_both_backends():
    """The manifest's filter is built from the PostgreSQL module's fragment and
    runs on SQLite too; that is sound only while the two copies agree."""
    assert sqlite_memory_sql.unadopted_suggestion("t0") == (
        postgres_memory_sql.unadopted_suggestion("t0")
    )


def test_full_package_excludes_unadopted_and_children(pair):
    cases.assert_full_package_excludes_unadopted_and_children(pair)


def test_confirm_adoption_resends_parent_provenance_and_revisions(pair):
    cases.assert_confirm_adoption_resends_parent_provenance_and_revisions(pair)


def test_answer_adoption_resends_parent_provenance_and_revisions(pair):
    cases.assert_answer_adoption_resends_parent_provenance_and_revisions(pair)


def test_target_sync_origin_stamped_on_adopted(pair):
    cases.assert_target_sync_origin_stamped_on_adopted(pair)


def test_capture_disabled_no_touch(pair):
    cases.assert_capture_disabled_no_touch(pair)


def test_draft_deleted_replays_as_absent(pair):
    cases.assert_draft_deleted_replays_as_absent(pair)


def test_verify_row_scopes_accepts_filtered_package(pair):
    cases.assert_verify_row_scopes_accepts_filtered_package(pair)


def test_deprecated_adopted_syncs(pair):
    cases.assert_deprecated_adopted_syncs(pair)


def test_full_import_keeps_target_own_suggestions(pair):
    cases.assert_full_import_keeps_target_own_suggestions(pair)
