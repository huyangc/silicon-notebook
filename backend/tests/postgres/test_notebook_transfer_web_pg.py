"""Web export / import of single notebooks on real PostgreSQL: the twin of
``tests/test_notebook_transfer_web.py``, running the same checks
(``tests/notebook_transfer_web_cases.py``) through the HTTP routes against the
production PostgreSQL repository -- the streaming export inside its REPEATABLE READ
read, the import's writes and compensation, the document limit's override read and
the account status the download ticket checks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests import notebook_transfer_web_cases as cases
from tests.postgres.test_notebook_archive_export_pg import BACKEND

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_transfer_web"),
]


def _client(postgres_scope, tmp_path, monkeypatch, **env: str) -> TestClient:
    cases.configure_environment(monkeypatch, tmp_path, postgres_scope.url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _close() -> None:
    from app.api import deps
    from app.core.config import get_settings

    deps.repository().close()
    deps.repository.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def world(postgres_scope, tmp_path, monkeypatch):
    client = _client(postgres_scope, tmp_path, monkeypatch)
    from app.api import deps

    try:
        yield cases.build_world(client, deps.repository(), BACKEND, tmp_path, monkeypatch)
    finally:
        _close()


def test_export_needs_the_owner_and_allows_a_mirror_pg(world):
    cases.check_export_needs_the_owner_and_allows_a_mirror(world)


def test_jobs_of_another_user_are_not_found_pg(world):
    cases.check_jobs_of_another_user_are_not_found(world)


def test_document_count_trusts_no_label_pg(world):
    cases.check_document_count_trusts_no_label(world)


def test_non_admins_are_held_to_the_document_limit_pg(world):
    cases.check_non_admins_are_held_to_the_document_limit(world)


def test_413_for_export_and_upload_pg(world):
    cases.check_413_for_export_and_upload(world)


def test_the_upload_streams_to_disk_pg(world):
    cases.check_the_upload_streams_to_disk(world)


def test_tickets_are_single_use_and_expire_pg(world):
    cases.check_tickets_are_single_use_and_expire(world)


def test_a_ticket_is_bound_to_its_job_and_user_pg(world):
    cases.check_a_ticket_is_bound_to_its_job_and_user(world)


def test_two_concurrent_redemptions_have_one_winner_pg(world):
    cases.check_two_concurrent_redemptions_have_one_winner(world)


def test_the_ticket_never_reaches_a_log_pg(postgres_scope, tmp_path, monkeypatch, caplog):
    log_dir = tmp_path / "logs"
    client = _client(
        postgres_scope, tmp_path, monkeypatch, EVENT_LOG_ENABLED="true",
        EVENT_LOG_DIR=str(log_dir), LLM_LOG_ENABLED="true",
        LLM_LOG_PATH=str(log_dir / "llm.jsonl"),
    )
    from app.api import deps

    try:
        world = cases.build_world(client, deps.repository(), BACKEND, tmp_path, monkeypatch)
        cases.check_the_ticket_never_reaches_a_log(world, caplog, log_dir)
    finally:
        _close()


def test_finished_jobs_and_stray_files_expire_pg(world):
    cases.check_finished_jobs_and_stray_files_expire(world)


def test_a_restart_fails_the_jobs_that_were_running_pg(world):
    cases.check_a_restart_fails_the_jobs_that_were_running(world)


def test_export_download_import_round_trip_pg(world):
    cases.check_export_download_import_round_trip(world)


def test_a_failed_reembed_builds_no_index_pg(world):
    cases.check_a_failed_reembed_builds_no_index(world)


def test_delete_cancels_a_running_export_pg(world):
    cases.check_delete_cancels_a_running_export(world)


def test_delete_cancels_a_queued_or_running_import_pg(world):
    cases.check_delete_cancels_a_queued_or_running_import(world)


def test_an_inflating_archive_is_refused_before_decompression_pg(world):
    cases.check_an_inflating_archive_is_refused_before_decompression(world)


def test_each_user_has_at_most_two_active_jobs_pg(world):
    cases.check_each_user_has_at_most_two_active_jobs(world)


def test_a_finished_export_supersedes_the_older_ones_pg(world):
    cases.check_a_finished_export_supersedes_the_older_ones(world)


def test_a_download_closes_its_file_and_counts_only_when_sent_whole_pg(world):
    cases.check_a_download_closes_its_file_and_counts_only_when_sent_whole(world)


def test_a_published_import_survives_a_restart_and_a_failure_pg(world):
    cases.check_a_published_import_survives_a_restart_and_a_failure(world)


def test_every_run_ends_in_a_terminal_state_pg(world):
    cases.check_every_run_ends_in_a_terminal_state(world)


def test_reads_never_scan_the_directories_pg(world):
    cases.check_reads_never_scan_the_directories(world)


def test_concurrent_uploads_hold_their_slots_pg(world):
    cases.check_concurrent_uploads_hold_their_slots(world)


def test_a_deleted_running_job_keeps_its_slot_pg(world):
    cases.check_a_deleted_running_job_keeps_its_slot(world)


def test_delete_of_a_running_import_waits_for_its_outcome_pg(world):
    cases.check_delete_of_a_running_import_waits_for_its_outcome(world)


def test_a_crash_right_after_publish_reads_as_imported_pg(world):
    cases.check_a_crash_right_after_publish_reads_as_imported(world)


def test_admins_are_not_counted_and_tables_cannot_hide_more_than_exist_pg(world):
    cases.check_admins_are_not_counted_and_tables_cannot_hide_more_than_exist(world)


def test_a_sweep_never_removes_a_live_upload_pg(world):
    cases.check_a_sweep_never_removes_a_live_upload(world)


def test_a_sweep_never_removes_the_files_of_a_deleted_running_export_pg(world):
    cases.check_a_sweep_never_removes_the_files_of_a_deleted_running_export(world)


def test_a_notebook_with_an_empty_knowhow_table_round_trips_pg(world):
    cases.check_a_notebook_with_an_empty_knowhow_table_round_trips(world)


def test_refusals_say_whether_to_upgrade_or_retry_pg(world):
    cases.check_refusals_say_whether_to_upgrade_or_retry(world)


def test_a_retry_with_the_same_upload_id_returns_its_job_pg(world):
    cases.check_a_retry_with_the_same_upload_id_returns_its_job(world)


def test_the_upload_status_answers_authoritatively_pg(world):
    cases.check_the_upload_status_answers_authoritatively(world)
