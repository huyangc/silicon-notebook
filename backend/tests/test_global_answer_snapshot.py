"""The anonymous snapshot of ONE finished MCP global job (answer link, T4).

``GlobalAskService.public_answer_job`` shares its live re-authorization with the
session share (``_public_snapshot``); these cases pin the new door and that the
shared path still answers the same predicate. ``test_global_ask_share_api.py``
keeps pinning the ``gshr-`` behaviour itself.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.global_ask import GlobalAskService
from app.services.sqlite_repository import SQLiteRepository
from tests.global_ask_share_cases import insert_conversation, insert_job


def _payload(job_id, *, status="done", resolved=("a",), cited=("a",)):
    return {
        "job_id": job_id, "status": status, "question": f"q-{job_id}",
        "resolved_notebook_ids": list(resolved),
        "cited_notebook_ids": list(cited),
        "answer": {"answer": f"answer-{job_id}", "citations": [
            {"notebook_id": nb} for nb in cited]},
    }


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None,
                        database_url=f"sqlite:///{tmp_path / 'snap.db'}",
                        storage_dir=str(tmp_path / "storage"))
    repo = SQLiteRepository(settings)
    readable = {"a", "b"}
    service = GlobalAskService(
        store=repo._runtime.global_ask_store,
        notebooks=lambda user: [],
        can_read=lambda nb, user: nb in readable and user == "owner",
        sources=SimpleNamespace(), settings=settings, ask=SimpleNamespace(),
    )
    yield service, readable
    service.close()
    repo.close()


def _seed(service, job_id, *, via="mcp", status="done", user="owner", **payload):
    store = service.store
    conversation_id, _ = insert_conversation(store, f"conv-{job_id}", user)
    insert_job(store, conversation_id, user, job_id, "2026-01-01T00:00:01",
               status=status, payload=_payload(job_id, status=status, **payload))
    with store.database.write() as db:
        db.execute(store._sql("UPDATE global_ask_jobs SET submitted_via=? WHERE id=?"),
                   (via, job_id))
    return job_id


def test_public_answer_job_returns_one_turn_for_a_done_mcp_job(env):
    service, _ = env
    _seed(service, "gask-1", resolved=("a", "b"), cited=("a",))
    row = service.public_answer_job(
        "gask-1", owner_id="owner", allowed_notebook_ids={"a", "b"})
    assert row is not None
    assert row["title"] == "" and row["shared_through_at"] == ""
    assert row["created_by"] == "owner"
    assert row["notebook_ids"] == frozenset({"a", "b"})
    assert row["created_at"] == "2026-01-01T00:00:01"
    assert [turn["question"] for turn in row["turns"]] == ["q-gask-1"]
    assert row["turns"][0]["payload"]["answer"] == "answer-gask-1"


def test_browser_job_running_job_and_foreign_owner_are_none(env):
    service, _ = env
    allowed = {"a", "b"}
    _seed(service, "gask-web", via="web")
    _seed(service, "gask-run", status="running")
    _seed(service, "gask-ok")
    assert service.public_answer_job("gask-web", owner_id="owner", allowed_notebook_ids=allowed) is None
    assert service.public_answer_job("gask-run", owner_id="owner", allowed_notebook_ids=allowed) is None
    assert service.public_answer_job("gask-ok", owner_id="someone-else", allowed_notebook_ids=allowed) is None
    assert service.public_answer_job("gask-missing", owner_id="owner", allowed_notebook_ids=allowed) is None
    assert service.public_answer_job("gask-ok", owner_id="", allowed_notebook_ids=allowed) is None
    assert service.public_answer_job("gask-ok", owner_id="owner", allowed_notebook_ids=allowed) is not None


def test_lost_read_on_a_cited_library_is_none(env):
    service, readable = env
    _seed(service, "gask-1", resolved=("a", "b"), cited=("a", "b"))
    assert service.public_answer_job(
        "gask-1", owner_id="owner", allowed_notebook_ids={"a", "b"}) is not None
    readable.discard("b")
    assert service.public_answer_job(
        "gask-1", owner_id="owner", allowed_notebook_ids={"a", "b"}) is None
    readable.add("b")  # restoring access revives the same link
    assert service.public_answer_job(
        "gask-1", owner_id="owner", allowed_notebook_ids={"a", "b"}) is not None


def test_scope_outside_the_token_allowlist_is_none(env):
    service, _ = env
    # Resolved libraries are inside the allowlist; the cited one is not.
    _seed(service, "gask-1", resolved=("a",), cited=("b",))
    assert service.public_answer_job(
        "gask-1", owner_id="owner", allowed_notebook_ids={"a"}) is None
    assert service.public_answer_job(
        "gask-1", owner_id="owner", allowed_notebook_ids={"a", "b"}) is not None
