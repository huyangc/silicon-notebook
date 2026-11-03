"""SQLite: ``MemoryService``'s auto user-memory suggestion paths and the
switch service (plan T8), over the runtime's real Memory stores. PostgreSQL
twin: ``tests/postgres/test_memory_suggest_adoption_pg.py``; shared cases:
``tests/memory_suggest_adoption_cases.py``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_adoption_cases as cases
from tests import memory_suggest_write_cases as writes


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def world(tmp_path: Path) -> cases.World:
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

    return cases.build_world(writes.Env(
        store=repo._runtime.memory_store,
        suggest=repo._runtime.memory_suggest_store,
        run=run,
        rows=rows,
        json=lambda value: json.dumps(value, ensure_ascii=False),
    ))


def test_create_suggestions_events_without_vector_or_kg(world):
    cases.assert_create_suggestions_events_without_vector_or_kg(world)


def test_two_adoption_entries_identical_side_effects(world):
    cases.assert_two_adoption_entries_identical_side_effects(world)


def test_save_from_answer_adopts_open_suggestion(world):
    cases.assert_save_from_answer_adopts_open_suggestion(world)


def test_save_from_answer_replaces_dismissed_suggestion(world):
    cases.assert_save_from_answer_replaces_dismissed_suggestion(world)


def test_save_from_answer_returns_other_rows_unchanged(world):
    cases.assert_save_from_answer_returns_other_rows_unchanged(world)


def test_save_from_answer_concurrent_insert_returns_existing(world):
    cases.assert_save_from_answer_concurrent_insert_returns_existing(world)


def test_clarified_suggestion_does_not_occupy_answer_index(world):
    cases.assert_clarified_suggestion_does_not_occupy_answer_index(world)


def test_answer_memory_link_view(world):
    cases.assert_answer_memory_link_view(world)


def test_origin_filters_keep_manual_answers_apart_from_all_suggestion_states(world):
    cases.assert_origin_filters_keep_manual_answers_apart_from_all_suggestion_states(world)


def test_exit_with_only_drafts_ends_membership(world):
    cases.assert_exit_with_only_drafts_ends_membership(world)


def test_exit_ack_compares_your_memories_only(world):
    cases.assert_exit_ack_compares_your_memories_only(world)


def test_exit_keeps_a_suggestion_adopted_after_the_claim(world):
    cases.assert_exit_keeps_a_suggestion_adopted_after_the_claim(world)


def test_exit_of_only_drafts_keeps_one_adopted_after_the_claim(world):
    cases.assert_exit_of_only_drafts_keeps_one_adopted_after_the_claim(world)


def test_exit_ack_mismatch_leaves_the_job_row(world):
    cases.assert_exit_ack_mismatch_leaves_the_job_row(world)


def test_export_header_and_type_labels(world):
    cases.assert_export_header_and_type_labels(world)


def test_transfer_rejects_unadopted(world):
    cases.assert_transfer_rejects_unadopted(world)


def test_enable_refused_when_wiring_off(world):
    cases.assert_enable_refused_when_wiring_off(world)


def test_disable_delete_unadopted_removes_only_candidates_keeps_rejected(world):
    cases.assert_disable_delete_unadopted_removes_only_candidates_keeps_rejected(world)


def test_disable_delete_keeps_a_suggestion_adopted_after_the_page_read(world):
    cases.assert_disable_delete_keeps_a_suggestion_adopted_after_the_page_read(world)


def test_expire_suggestions_ignores_deployment_switch_and_emits_events(world):
    cases.assert_expire_suggestions_ignores_deployment_switch_and_emits_events(world)
