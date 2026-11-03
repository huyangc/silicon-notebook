"""SQLite Memory store write side of auto memory suggestions (plan T4b),
reached through the runtime seats ``repo._runtime.memory_store`` and
``repo._runtime.memory_suggest_store``. PostgreSQL twin:
``tests/postgres/test_memory_suggest_writes_pg.py``; shared cases:
``tests/memory_suggest_write_cases.py``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_write_cases as cases


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def env(tmp_path: Path) -> cases.Env:
    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    return cases.Env(
        store=repo._runtime.memory_store,
        suggest=repo._runtime.memory_suggest_store,
        run=run,
        rows=rows,
        json=lambda value: json.dumps(value, ensure_ascii=False),
    )


def test_member_exit_snapshot_counts_memories_and_suggestions_apart(env):
    cases.assert_member_exit_snapshot_counts_memories_and_suggestions_apart(env)


def test_exit_claim_writes_nothing_finish_deletes_job_row(env):
    cases.assert_exit_claim_writes_nothing_finish_deletes_job_row(env)


def test_finish_keeps_a_suggestion_adopted_after_the_claim(env):
    cases.assert_finish_keeps_a_suggestion_adopted_after_the_claim(env)


def test_exit_race_suggestion_between_purge_and_finish(env):
    cases.assert_exit_race_suggestion_between_purge_and_finish(env)


def test_finish_member_exit_still_counts_all_states(env):
    cases.assert_finish_member_exit_still_counts_all_states(env)


def test_create_suggestions_revoked_when_disabled(env):
    cases.assert_create_suggestions_revoked_when_disabled(env)


def test_create_suggestions_revoked_when_job_row_deleted(env):
    cases.assert_create_suggestions_revoked_when_job_row_deleted(env)


def test_create_suggestions_revoked_when_token_mismatch(env):
    cases.assert_create_suggestions_revoked_when_token_mismatch(env)


def test_create_suggestions_revoked_without_read_access(env):
    cases.assert_create_suggestions_revoked_without_read_access(env)


def test_repeated_suggestion_answer_once_conflict_is_skipped(env):
    cases.assert_repeated_suggestion_answer_once_conflict_is_skipped(env)


def test_create_suggestions_rejects_malformed_writes(env):
    cases.assert_create_suggestions_rejects_malformed_writes(env)


def test_adopt_from_answer_cas(env):
    cases.assert_adopt_from_answer_cas(env)


def test_replace_rejected_suggestion(env):
    cases.assert_replace_rejected_suggestion(env)


def test_concurrent_manual_save_noop(env):
    cases.assert_concurrent_manual_save_noop(env)


def test_resync_children_touch_only_when_capture_enabled(env):
    cases.assert_resync_children_touch_only_when_capture_enabled(env)


def test_capture_triggers_fire_on_every_update_of_the_resent_children(env):
    """The re-send relies on the SQLite capture triggers of
    ``memory_provenance`` / ``memory_revisions`` logging ANY update: the
    generator renders them ``AFTER UPDATE`` with no ``WHEN`` clause (a
    ``WHEN`` would sit between ``ON "<table>"`` and ``BEGIN``)."""
    for table in ("memory_provenance", "memory_revisions"):
        found = env.rows(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (f"sync_capture_{table}_update",),
        )
        assert len(found) == 1, table
        sql = found[0]["sql"]
        assert f'AFTER UPDATE ON "{table}" BEGIN ' in sql, sql
        assert " WHEN " not in sql.split(" BEGIN ", 1)[0], sql
