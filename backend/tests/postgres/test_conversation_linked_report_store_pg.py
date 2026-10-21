"""PostgreSQL twin of ``tests/test_conversation_linked_report_store.py``: the
in-flight turn's ``ask_jobs.report_id`` (0080) and the last turn's
``linked_report`` receipt behind ``ConversationSummary`` /
``ConversationDetail``. The shared bodies are in
``tests/conversation_linked_report_cases.py``."""
from __future__ import annotations

import pytest

from app.models.schemas import AskResponse, TraceStep
from app.repositories.postgres._store_utils import jsonb
from tests import conversation_linked_report_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_conversation_linked_report_store"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _save_turn(repo):
    """The ensure_conversation -> save_answer sequence every ask handler uses."""
    store = repo._runtime.ask_state
    uid = repo.current_user().id

    def save(notebook_id, conversation_id, question, receipt, reasoning=False):
        with store.database.write() as db:
            conversation_id = store.ensure_conversation(
                db, notebook_id, conversation_id, question, uid)
        store.save_answer(
            notebook_id, conversation_id, question,
            AskResponse(
                conclusion="c", conversation_id=conversation_id, linked_report=receipt,
                reasoning_trace=(
                    [TraceStep(step_type="answer", summary="s")] if reasoning else [])),
            uid,
        )
        return conversation_id

    return save


def test_job_row_records_the_report_the_turn_was_asked_about(postgres_repository):
    repo = postgres_repository

    def stored_report_id(job_id: str) -> str:
        with repo._runtime.database.connect() as db:
            return db.execute(
                "SELECT report_id FROM ask_jobs WHERE id=%s", (job_id,)
            ).fetchone()["report_id"]

    cases.assert_job_row_records_the_report(repo, stored_report_id)


def test_in_flight_turn_names_the_report_by_its_current_title(postgres_repository):
    cases.assert_in_flight_turn_names_the_report_by_its_current_title(
        postgres_repository)


def test_summary_and_detail_follow_the_last_turn(postgres_repository):
    cases.assert_summary_and_detail_follow_the_last_turn(
        postgres_repository, _save_turn(postgres_repository))


def test_unreadable_receipt_reads_as_no_report(postgres_repository):
    repo = postgres_repository

    def overwrite_payload(conversation_id: str, receipt: object) -> None:
        with repo._runtime.database.write() as db:
            db.execute(
                "UPDATE answers SET payload=%s WHERE conversation_id=%s",
                (jsonb({"conclusion": "c", "linked_report": receipt}),
                 conversation_id),
            )

    cases.assert_unreadable_receipt_reads_as_no_report(
        repo, _save_turn(repo), overwrite_payload)
