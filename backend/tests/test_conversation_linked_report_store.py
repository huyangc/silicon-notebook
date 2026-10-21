"""SQLite store behaviour of the report a conversation names: the in-flight
turn's ``ask_jobs.report_id`` (SQLite v99) and the last turn's ``linked_report``
receipt behind ``ConversationSummary`` / ``ConversationDetail``. PostgreSQL twin:
``tests/postgres/test_conversation_linked_report_store_pg.py``; the shared
bodies are in ``tests/conversation_linked_report_cases.py``."""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import AskResponse
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import conversation_linked_report_cases as cases
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _save_turn(repo):
    """The ensure_conversation -> save_answer sequence every ask handler uses."""
    store = repo._runtime.ask_state
    uid = repo.current_user().id

    def save(notebook_id, conversation_id, question, receipt):
        with store.database.write() as db:
            store.database.begin_guarded_write(db)
            conversation_id = store.ensure_conversation(
                db, notebook_id, conversation_id, question, uid)
        store.save_answer(
            notebook_id, conversation_id, question,
            AskResponse(
                conclusion="c", conversation_id=conversation_id, linked_report=receipt),
            uid,
        )
        return conversation_id

    return save


def test_job_row_records_the_report_the_turn_was_asked_about(repo):
    def stored_report_id(job_id: str) -> str:
        with repo._connect() as db:
            return db.execute(
                "SELECT report_id FROM ask_jobs WHERE id=?", (job_id,)
            ).fetchone()["report_id"]

    cases.assert_job_row_records_the_report(repo, stored_report_id)


def test_in_flight_turn_names_the_report_by_its_current_title(repo):
    cases.assert_in_flight_turn_names_the_report_by_its_current_title(repo)


def test_summary_and_detail_follow_the_last_turn(repo):
    cases.assert_summary_and_detail_follow_the_last_turn(repo, _save_turn(repo))


def test_unreadable_receipt_reads_as_no_report(repo):
    def overwrite_payload(conversation_id: str, receipt: object) -> None:
        with repo._connect() as db:
            db.execute(
                "UPDATE answers SET payload=? WHERE conversation_id=?",
                (json.dumps({"conclusion": "c", "linked_report": receipt}),
                 conversation_id),
            )

    cases.assert_unreadable_receipt_reads_as_no_report(
        repo, _save_turn(repo), overwrite_payload)
