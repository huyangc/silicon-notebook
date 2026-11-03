"""PostgreSQL: cross-environment sync carries only ADOPTED auto user-memory
suggestions (plan T11; design §6.6 / D15 (a)). SQLite lane:
``tests/test_sync_memory_suggestions.py``; shared cases:
``tests/sync_memory_suggestion_cases.py``.

Two live schemas -- a source and a target -- for the two export paths the
filter has to hold on: the full table scan (an extra WHERE on the keyset scan,
the children read through their parent join) and the change-log window (the
upserts dropped before they become rows), plus the second adoption entry.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.core.request_context import set_request_user
from tests import memory_suggest_adoption_cases as adoption
from tests import memory_suggest_write_cases as writes
from tests import sync_memory_suggestion_cases as cases
from tests.postgres.conftest import _isolated_postgres_scope

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_import"),
]


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    yield
    set_request_user(None)


def _side(url: str, storage: Path, stack: ExitStack):
    from app.services.postgres_repository import PostgresRepository

    settings = Settings(
        database_url=url,
        storage_dir=str(storage),
        _env_file=None,
        event_log_enabled=False,
        llm_log_enabled=False,
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )
    repo = PostgresRepository(settings)
    stack.callback(repo.close)
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    return cases.Side(settings=settings, run=run, rows=rows, json=Jsonb), repo


@pytest.fixture
def pair(postgres_scope, tmp_path):
    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with ExitStack() as stack:
        source_scope = stack.enter_context(_isolated_postgres_scope(base_url))
        source, source_repo = _side(source_scope.url, tmp_path / "source-storage", stack)
        target, _ = _side(postgres_scope.url, tmp_path / "target-storage", stack)
        world = adoption.build_world(writes.Env(
            store=source_repo._runtime.memory_store,
            suggest=source_repo._runtime.memory_suggest_store,
            run=source.run,
            rows=source.rows,
            json=Jsonb,
        ))
        yield cases.Pair(source=source, target=target, world=world, out=tmp_path / "out")


def test_full_package_excludes_unadopted_and_children_pg(pair):
    cases.assert_full_package_excludes_unadopted_and_children(pair)


def test_confirm_adoption_resends_parent_provenance_and_revisions_pg(pair):
    cases.assert_confirm_adoption_resends_parent_provenance_and_revisions(pair)


def test_answer_adoption_resends_parent_provenance_and_revisions_pg(pair):
    cases.assert_answer_adoption_resends_parent_provenance_and_revisions(pair)


def test_target_sync_origin_stamped_on_adopted_pg(pair):
    cases.assert_target_sync_origin_stamped_on_adopted(pair)


def test_capture_disabled_no_touch_pg(pair):
    cases.assert_capture_disabled_no_touch(pair)


def test_draft_deleted_replays_as_absent_pg(pair):
    cases.assert_draft_deleted_replays_as_absent(pair)


def test_verify_row_scopes_accepts_filtered_package_pg(pair):
    cases.assert_verify_row_scopes_accepts_filtered_package(pair)


def test_deprecated_adopted_syncs_pg(pair):
    cases.assert_deprecated_adopted_syncs(pair)


def test_full_import_keeps_target_own_suggestions_pg(pair):
    cases.assert_full_import_keeps_target_own_suggestions(pair)
