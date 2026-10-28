"""Web export / import of single notebooks on SQLite. The checks live in
``tests/notebook_transfer_web_cases.py``; ``tests/postgres/test_notebook_transfer_web_pg.py``
runs them on PostgreSQL (the Settings rails: ``tests/test_notebook_transfer_settings.py``)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests import notebook_transfer_web_cases as cases
from tests.test_notebook_archive_export import BACKEND


def _client(monkeypatch, tmp_path, **env: str) -> TestClient:
    cases.configure_environment(monkeypatch, tmp_path, f"sqlite:///{tmp_path / 'web.db'}")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from app.api import deps
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def world(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    from app.api import deps

    try:
        yield cases.build_world(client, deps.repository(), BACKEND, tmp_path, monkeypatch)
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


def test_export_needs_the_owner_and_allows_a_mirror(world):
    cases.check_export_needs_the_owner_and_allows_a_mirror(world)


def test_jobs_of_another_user_are_not_found(world):
    cases.check_jobs_of_another_user_are_not_found(world)


def test_document_count_trusts_no_label(world):
    cases.check_document_count_trusts_no_label(world)


def test_non_admins_are_held_to_the_document_limit(world):
    cases.check_non_admins_are_held_to_the_document_limit(world)


def test_413_for_export_and_upload(world):
    cases.check_413_for_export_and_upload(world)


def test_the_upload_streams_to_disk(world):
    cases.check_the_upload_streams_to_disk(world)


def test_bad_uploads_are_refused(world):
    cases.check_bad_uploads_are_refused(world)


def test_tickets_are_single_use_and_expire(world):
    cases.check_tickets_are_single_use_and_expire(world)


def test_a_ticket_is_bound_to_its_job_and_user(world):
    cases.check_a_ticket_is_bound_to_its_job_and_user(world)


def test_two_concurrent_redemptions_have_one_winner(world):
    cases.check_two_concurrent_redemptions_have_one_winner(world)


def test_the_ticket_never_reaches_a_log(tmp_path, monkeypatch, caplog):
    log_dir = tmp_path / "logs"
    client = _client(
        monkeypatch, tmp_path, EVENT_LOG_ENABLED="true", EVENT_LOG_DIR=str(log_dir),
        LLM_LOG_ENABLED="true", LLM_LOG_PATH=str(log_dir / "llm.jsonl"),
    )
    from app.api import deps

    try:
        world = cases.build_world(client, deps.repository(), BACKEND, tmp_path, monkeypatch)
        cases.check_the_ticket_never_reaches_a_log(world, caplog, log_dir)
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


def test_finished_jobs_and_stray_files_expire(world):
    cases.check_finished_jobs_and_stray_files_expire(world)


def test_a_restart_fails_the_jobs_that_were_running(world):
    cases.check_a_restart_fails_the_jobs_that_were_running(world)


def test_export_download_import_round_trip(world):
    cases.check_export_download_import_round_trip(world)


def test_an_import_without_legs_still_asks_for_the_index(world):
    cases.check_an_import_without_legs_still_asks_for_the_index(world)


def test_a_failed_reembed_builds_no_index(world):
    cases.check_a_failed_reembed_builds_no_index(world)


def test_delete_cancels_a_running_export(world):
    cases.check_delete_cancels_a_running_export(world)


def test_delete_cancels_a_queued_or_running_import(world):
    cases.check_delete_cancels_a_queued_or_running_import(world)


def test_an_inflating_archive_is_refused_before_decompression(world):
    cases.check_an_inflating_archive_is_refused_before_decompression(world)


def test_each_user_has_at_most_two_active_jobs(world):
    cases.check_each_user_has_at_most_two_active_jobs(world)


def test_a_finished_export_supersedes_the_older_ones(world):
    cases.check_a_finished_export_supersedes_the_older_ones(world)


def test_a_download_closes_its_file_and_counts_only_when_sent_whole(world):
    cases.check_a_download_closes_its_file_and_counts_only_when_sent_whole(world)


def test_a_published_import_survives_a_restart_and_a_failure(world):
    cases.check_a_published_import_survives_a_restart_and_a_failure(world)


def test_every_run_ends_in_a_terminal_state(world):
    cases.check_every_run_ends_in_a_terminal_state(world)


def test_the_expiry_timer_has_one_live_arming(world):
    cases.check_the_expiry_timer_has_one_live_arming(world)


def test_reads_never_scan_the_directories(world):
    cases.check_reads_never_scan_the_directories(world)


def test_concurrent_uploads_hold_their_slots(world):
    cases.check_concurrent_uploads_hold_their_slots(world)


def test_a_deleted_running_job_keeps_its_slot(world):
    cases.check_a_deleted_running_job_keeps_its_slot(world)


def test_delete_of_a_running_import_waits_for_its_outcome(world):
    cases.check_delete_of_a_running_import_waits_for_its_outcome(world)


def test_a_crash_right_after_publish_reads_as_imported(world):
    cases.check_a_crash_right_after_publish_reads_as_imported(world)


def test_admins_are_not_counted_and_tables_cannot_hide_more_than_exist(world):
    cases.check_admins_are_not_counted_and_tables_cannot_hide_more_than_exist(world)


def test_a_sweep_never_removes_a_live_upload(world):
    cases.check_a_sweep_never_removes_a_live_upload(world)


def test_a_sweep_never_removes_the_files_of_a_deleted_running_export(world):
    cases.check_a_sweep_never_removes_the_files_of_a_deleted_running_export(world)


def test_a_notebook_with_an_empty_knowhow_table_round_trips(world):
    cases.check_a_notebook_with_an_empty_knowhow_table_round_trips(world)
