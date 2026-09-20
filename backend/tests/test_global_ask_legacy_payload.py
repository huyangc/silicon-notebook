"""D1-1: history-read compatibility for the ``response``→``answer`` shape
migration on ``GlobalAskJob``.

``_run`` still only ever writes ``response`` today (see the docstring on
``GlobalAskJob.response``); ``mode``/``trace``/``answer`` are groundwork for a
later task that routes the global engine through ``AskService.ask``. These
tests pin two things ahead of that switch:

  * a genuinely pre-migration payload (no ``mode``/``trace``/``answer`` keys
    at all -- what every row persisted before this task looks like) still
    round-trips through ``GlobalAskJob.model_validate_json`` and through
    ``GlobalAskStore.completed_history``'s SQL projection;
  * a new-shape payload (``answer`` populated, ``response`` absent) reads
    back through the same paths;
  * the three ``global_answer_*`` projection helpers in
    ``app.models.global_ask`` prefer ``answer`` over the legacy ``response``
    and fall back correctly when either/both are absent.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.ask import AskResponse, Citation, TraceStep
from app.models.global_ask import (
    GlobalAskAnswer,
    GlobalAskJob,
    GlobalNotebookScope,
    global_answer_citations,
    global_answer_text,
    global_answer_trace,
)
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'global_legacy.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    yield repo._runtime.global_ask_store
    repo.close()


def _insert_conversation(store, conversation_id, user_id):
    with store.database.write() as db:
        db.execute(store._sql(
            "INSERT INTO global_ask_conversations"
            "(id,user_id,title,scope_json,submitted_via,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)"
        ), (conversation_id, user_id, "对话标题",
            json.dumps({"mode": "all", "notebook_ids": []}), "web",
            "2024-01-01T00:00:00", "2024-01-01T00:00:00"))


def _insert_job_row(store, *, job_id, conversation_id, user_id, status, payload, created_at):
    with store.database.write() as db:
        db.execute(store._sql(
            "INSERT INTO global_ask_jobs"
            "(id,conversation_id,user_id,client_request_id,request_json,status,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)"
        ), (job_id, conversation_id, user_id, None, "{}", status,
            json.dumps(payload, ensure_ascii=False), created_at))


def _legacy_payload(job_id, conversation_id, *, question, answer_text):
    """A payload shaped exactly like every row persisted before this task:
    no ``mode``/``trace``/``answer`` keys at the top level at all."""
    scope = {"mode": "all", "notebook_ids": []}
    return {
        "job_id": job_id,
        "conversation_id": conversation_id,
        "status": "done",
        "question": question,
        "created_at": "2024-01-01T00:00:00",
        "notebook_scope": scope,
        "resolved_notebook_ids": ["nb1"],
        "searched_notebook_ids": ["nb1"],
        "cited_notebook_ids": ["nb1"],
        "skipped_notebooks": [],
        "degraded_notebook_ids": [],
        "error": None,
        "response": {
            "answer_id": f"ans-{job_id}",
            "question": question,
            "answer": answer_text,
            "grounded": True,
            "anchors": [],
            "citations": [
                Citation(
                    label="[1]", source_id="s1", element_id="e1",
                    location_label="第一段", quoted_span="引用片段",
                ).model_dump(mode="json")
            ],
            "created_at": "2024-01-01T00:00:00",
            "notebook_scope": scope,
            "resolved_notebook_ids": ["nb1"],
            "searched_notebook_ids": ["nb1"],
            "cited_notebook_ids": ["nb1"],
            "skipped_notebooks": [],
            "degraded_notebook_ids": [],
            "completeness_notice": "回答仅使用本次命中的有限原文，不代表逐篇穷尽检查。",
        },
    }


def _new_shape_job(job_id, conversation_id, *, question, answer_text):
    """A job whose only populated answer shape is the new ``answer`` field
    (``AskResponse``), with ``response`` left at its default ``None``."""
    scope = GlobalNotebookScope(mode="all", notebook_ids=[])
    return GlobalAskJob(
        job_id=job_id, conversation_id=conversation_id, status="done",
        question=question, created_at="2024-01-01T00:00:00",
        notebook_scope=scope, resolved_notebook_ids=["nb1"],
        searched_notebook_ids=["nb1"], cited_notebook_ids=["nb1"],
        mode="chunk",
        trace=[TraceStep(step_type="answer", summary="synthesis")],
        answer=AskResponse(
            conclusion="done", answer=answer_text, grounded=True,
            citations=[Citation(
                label="[1]", source_id="s2", element_id="e2",
                location_label="第二段", quoted_span="新引用",
            )],
        ),
    )


def test_legacy_response_row_still_reads(store):
    conversation_id, user_id = "conv-legacy", "u1"
    _insert_conversation(store, conversation_id, user_id)
    payload = _legacy_payload(
        "job-legacy", conversation_id, question="旧问题", answer_text="旧答案文本",
    )
    # The payload has no mode/trace/answer keys at all -- confirm it still
    # validates through the model (not just through the SQL projection).
    assert "mode" not in payload and "trace" not in payload and "answer" not in payload
    job = GlobalAskJob.model_validate(payload)
    assert job.mode == "chunk" and job.trace == [] and job.answer is None
    assert global_answer_text(job) == "旧答案文本"

    _insert_job_row(
        store, job_id="job-legacy", conversation_id=conversation_id, user_id=user_id,
        status="done", payload=payload, created_at="2024-01-01T00:00:00",
    )
    history = store.completed_history(conversation_id, user_id, 10)
    assert len(history) == 1
    assert history[0]["question"] == "旧问题"
    assert history[0]["answer"] == "旧答案文本"
    assert history[0]["notebook_ids"] == ["nb1"]


def test_new_answer_row_reads(store):
    conversation_id, user_id = "conv-new", "u1"
    _insert_conversation(store, conversation_id, user_id)
    job = _new_shape_job(
        "job-new", conversation_id, question="新问题", answer_text="新答案文本",
    )
    assert job.response is None
    payload = json.loads(job.model_dump_json())
    assert payload["response"] is None
    assert payload["answer"]["answer"] == "新答案文本"

    _insert_job_row(
        store, job_id="job-new", conversation_id=conversation_id, user_id=user_id,
        status="done", payload=payload, created_at="2024-01-02T00:00:00",
    )
    history = store.completed_history(conversation_id, user_id, 10)
    assert len(history) == 1
    assert history[0]["question"] == "新问题"
    assert history[0]["answer"] == "新答案文本"


def test_both_shapes_in_one_conversation_history(store):
    conversation_id, user_id = "conv-mixed", "u1"
    _insert_conversation(store, conversation_id, user_id)

    legacy_payload = _legacy_payload(
        "job-1", conversation_id, question="第一问", answer_text="第一答",
    )
    _insert_job_row(
        store, job_id="job-1", conversation_id=conversation_id, user_id=user_id,
        status="done", payload=legacy_payload, created_at="2024-01-01T00:00:00",
    )

    new_job = _new_shape_job(
        "job-2", conversation_id, question="第二问", answer_text="第二答",
    )
    new_payload = json.loads(new_job.model_dump_json())
    _insert_job_row(
        store, job_id="job-2", conversation_id=conversation_id, user_id=user_id,
        status="done", payload=new_payload, created_at="2024-01-02T00:00:00",
    )

    # completed_history orders created_at DESC, id DESC -- newest turn first.
    history = store.completed_history(conversation_id, user_id, 10)
    assert [turn["question"] for turn in history] == ["第二问", "第一问"]
    assert [turn["answer"] for turn in history] == ["第二答", "第一答"]

    # store.job()/`_job` also parses both shapes back into a full model.
    legacy_job = store.job("job-1", user_id)
    new_job_read = store.job("job-2", user_id)
    assert legacy_job.response.answer == "第一答"
    assert legacy_job.answer is None
    assert new_job_read.answer.answer == "第二答"
    assert new_job_read.response is None


def test_projection_helpers_prefer_answer_over_legacy():
    scope = GlobalNotebookScope(mode="all", notebook_ids=[])
    base_kwargs = dict(
        job_id="j", conversation_id="c", status="done", question="q",
        created_at="t", notebook_scope=scope, resolved_notebook_ids=[],
    )

    # Neither shape populated: empty defaults, no crash.
    empty_job = GlobalAskJob(**base_kwargs)
    assert global_answer_text(empty_job) == ""
    assert global_answer_citations(empty_job) == []
    assert global_answer_trace(empty_job) == []

    # Legacy-only: falls back to response.
    legacy_citation = Citation(
        label="[1]", source_id="s1", element_id="e1",
        location_label="L1", quoted_span="span1",
    )
    legacy_job = GlobalAskJob(
        **base_kwargs,
        response=GlobalAskAnswer(
            answer_id="a1", question="q", answer="legacy-answer",
            citations=[legacy_citation], created_at="t", notebook_scope=scope,
            resolved_notebook_ids=[], searched_notebook_ids=[], cited_notebook_ids=[],
        ),
    )
    assert global_answer_text(legacy_job) == "legacy-answer"
    assert global_answer_citations(legacy_job) == [legacy_citation]
    assert global_answer_trace(legacy_job) == []

    # Both populated: answer wins over response for text/citations/trace.
    new_citation = Citation(
        label="[1]", source_id="s2", element_id="e2",
        location_label="L2", quoted_span="span2",
    )
    trace_step = TraceStep(step_type="answer", summary="s")
    both_job = GlobalAskJob(
        **base_kwargs,
        trace=[trace_step],
        response=GlobalAskAnswer(
            answer_id="a1", question="q", answer="legacy-answer",
            citations=[legacy_citation], created_at="t", notebook_scope=scope,
            resolved_notebook_ids=[], searched_notebook_ids=[], cited_notebook_ids=[],
        ),
        answer=AskResponse(
            conclusion="done", answer="new-answer", citations=[new_citation],
            reasoning_trace=[trace_step],
        ),
    )
    assert global_answer_text(both_job) == "new-answer"
    assert global_answer_citations(both_job) == [new_citation]
    assert global_answer_trace(both_job) == [trace_step]

    # answer present but its own trace empty: falls back to job.trace (the
    # `(job.answer.reasoning_trace if job.answer else None) or job.trace`
    # contract from the plan).
    sparse_trace_job = GlobalAskJob(
        **base_kwargs,
        trace=[trace_step],
        answer=AskResponse(conclusion="done", answer="new-answer"),
    )
    assert global_answer_trace(sparse_trace_job) == [trace_step]
