from __future__ import annotations

import dataclasses
import threading
from types import MappingProxyType, SimpleNamespace

import pytest

from app.application.ask_reasoning import (
    CommittedReasoningAnswer,
    PreparedReasoningAsk,
    ReasoningEvidenceSnapshot,
    ReasoningIntentProjection,
    ReasoningResponseDraft,
    ReasoningRetrievalRuntime,
    ReasoningRunInput,
    RetrievedReasoningAsk,
    StageBoundaryError,
)
from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import AskRequest, AskResponse, Citation
from app.services.cancellation import AskCancelled
from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever
from app.services.retrieval_run import current_retrieval_run, retrieval_run
from app.services.source_scope import current_source_scope, source_scope_context
from tests.test_ask_service_boundary import _EnumerableKnowhow, _minimal_ask_service


def _projection() -> ReasoningIntentProjection:
    return ReasoningIntentProjection(
        resolved_question="confirmed question",
        result_scope="ranked",
        completeness_required=False,
        retrieval_effort="standard",
        entities=("entity",),
        constraints=("constraint",),
        excluded_topics=(),
        assumptions=(),
        expected_output="answer",
        mandatory_topics=("topic",),
    )


def _run_input() -> ReasoningRunInput:
    return ReasoningRunInput(
        notebook_id="nb",
        question="confirmed question",
        history="history",
        top_n=None,
        max_steps=None,
        intent_queries=("q1", "q2"),
        limits=ask_retrieval_limits("standard"),
        intent=_projection(),
    )


def _retriever(cancel_event=None) -> ReasoningRetriever:
    return ReasoningRetriever(
        retrieval=object(),
        model_clients=object(),
        communities=object(),
        settings=Settings(),
        cancel_event=cancel_event,
    )


def test_reasoning_stage_data_contracts_are_frozen_slotted_and_narrow():
    for contract in (
        ReasoningIntentProjection,
        PreparedReasoningAsk,
        ReasoningRunInput,
        ReasoningEvidenceSnapshot,
        RetrievedReasoningAsk,
        ReasoningResponseDraft,
        CommittedReasoningAnswer,
    ):
        params = contract.__dataclass_params__
        assert params.frozen is True
        assert "__dict__" not in contract.__slots__

    forbidden = {
        "payload", "repository", "service", "connection", "transaction",
        "future", "executor", "plugin", "profile_block",
    }
    for contract in (
        PreparedReasoningAsk,
        ReasoningRunInput,
        ReasoningEvidenceSnapshot,
        ReasoningResponseDraft,
    ):
        assert forbidden.isdisjoint(contract.__dataclass_fields__)

    run_input = _run_input()
    with pytest.raises(dataclasses.FrozenInstanceError):
        run_input.question = "changed"
    assert isinstance(run_input.intent_queries, tuple)
    assert isinstance(run_input.intent.as_mapping(), MappingProxyType)
    with pytest.raises(TypeError):
        run_input.intent.as_mapping()["result_scope"] = "complete"
    trace_detail = run_input.intent.as_json_mapping()
    assert trace_detail["entities"] == ["entity"]
    assert isinstance(trace_detail["entities"], list)


def test_reasoning_evidence_snapshot_breaks_container_aliases_without_deepcopy():
    hit = object()
    hits = [hit]
    attempted = [{"query": "q", "new": 1, "tries": 1}]
    result = ReasoningResult(top_hits=hits, attempted=attempted)

    snapshot = ReasoningEvidenceSnapshot.from_result(result)
    hits.append(object())
    attempted[0]["new"] = 99
    attempted.append({"query": "other"})

    assert snapshot.top_hits == (hit,)
    assert snapshot.top_hits[0] is hit
    assert tuple(snapshot.attempted[0].items()) == (
        ("query", "q"), ("new", 1), ("tries", 1)
    )
    assert len(snapshot.attempted) == 1
    with pytest.raises(TypeError):
        snapshot.attempted[0]["new"] = 2


def test_legacy_reasoning_result_stays_mutable_for_report_working_copies():
    result = ReasoningResult(top_hits=["baseline"], attempted=[])

    result.top_hits.append("deep-dive")
    result.attempted.append({"query": "follow-up"})

    assert result.top_hits == ["baseline", "deep-dive"]
    assert result.attempted == [{"query": "follow-up"}]


def test_typed_retrieval_stage_preserves_arguments_scope_run_and_identity(monkeypatch):
    cancel_event = threading.Event()
    retriever = _retriever(cancel_event)
    captured = {}
    hit = object()

    def legacy_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ReasoningResult(top_hits=[hit])

    monkeypatch.setattr(retriever, "run", legacy_run)
    probe = SimpleNamespace(is_connection_held=lambda: False)
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-1"]}
    ):
        scope = current_source_scope()
        with retrieval_run(
            run_kind="ask_reasoning", actor_id="user", cancel_event=cancel_event
        ) as run:
            runtime = ReasoningRetrievalRuntime(
                scope=scope,
                retrieval_run=run,
                cancellation=cancel_event,
                trace_sink=None,
                connection_probe=probe,
            )
            snapshot = retriever.run_stage(_run_input(), runtime)

    assert snapshot.top_hits == (hit,)
    assert captured["args"][:3] == ("nb", "confirmed question", "history")
    assert captured["kwargs"]["intent_queries"] == ["q1", "q2"]
    assert captured["kwargs"]["limits"] is ask_retrieval_limits("standard")
    assert captured["kwargs"]["intent_detail"]["result_scope"] == "ranked"


@pytest.mark.parametrize(
    "probe",
    [
        SimpleNamespace(is_connection_held=lambda: True),
        SimpleNamespace(is_connection_held=lambda: object()),
        object(),
    ],
)
def test_typed_retrieval_stage_rejects_held_or_malformed_connection_before_run(
    monkeypatch, probe,
):
    cancel_event = threading.Event()
    retriever = _retriever(cancel_event)
    called = []
    monkeypatch.setattr(
        retriever, "run", lambda *args, **kwargs: called.append(True)
    )
    runtime = ReasoningRetrievalRuntime(
        scope=None,
        retrieval_run=None,
        cancellation=cancel_event,
        trace_sink=None,
        connection_probe=probe,
    )

    with pytest.raises(StageBoundaryError):
        retriever.run_stage(_run_input(), runtime)
    assert called == []


def test_typed_retrieval_stage_rejects_authority_replacement(monkeypatch):
    cancel_event = threading.Event()
    retriever = _retriever(cancel_event)
    monkeypatch.setattr(
        retriever, "run", lambda *args, **kwargs: ReasoningResult()
    )
    runtime = ReasoningRetrievalRuntime(
        scope=object(),
        retrieval_run=None,
        cancellation=cancel_event,
        trace_sink=None,
        connection_probe=None,
    )
    with pytest.raises(StageBoundaryError, match="scope changed"):
        retriever.run_stage(_run_input(), runtime)


def test_typed_retrieval_stage_binds_scope_notebook_and_run_cancellation(
    monkeypatch,
):
    run_cancel = threading.Event()
    other_cancel = threading.Event()
    retriever = _retriever(other_cancel)
    called = []
    monkeypatch.setattr(
        retriever, "run", lambda *args, **kwargs: called.append(True)
    )
    with source_scope_context(
        "other-notebook", {"mode": "include", "source_ids": ["source-1"]}
    ):
        scope = current_source_scope()
        runtime = ReasoningRetrievalRuntime(
            scope=scope,
            retrieval_run=None,
            cancellation=other_cancel,
            trace_sink=None,
            connection_probe=None,
        )
        with pytest.raises(StageBoundaryError, match="scope notebook changed"):
            retriever.run_stage(_run_input(), runtime)

    with retrieval_run(
        run_kind="ask_reasoning", actor_id="user", cancel_event=run_cancel
    ) as run:
        runtime = ReasoningRetrievalRuntime(
            scope=None,
            retrieval_run=run,
            cancellation=other_cancel,
            trace_sink=None,
            connection_probe=None,
        )
        with pytest.raises(
            StageBoundaryError, match="run cancellation authority changed"
        ):
            retriever.run_stage(_run_input(), runtime)
    assert called == []


def test_typed_retrieval_stage_rejects_raising_connection_probe_before_run(
    monkeypatch,
):
    cancel_event = threading.Event()
    retriever = _retriever(cancel_event)
    called = []
    monkeypatch.setattr(
        retriever, "run", lambda *args, **kwargs: called.append(True)
    )

    def broken_probe():
        raise RuntimeError("probe failed")

    runtime = ReasoningRetrievalRuntime(
        scope=None,
        retrieval_run=None,
        cancellation=cancel_event,
        trace_sink=None,
        connection_probe=SimpleNamespace(is_connection_held=broken_probe),
    )
    with pytest.raises(StageBoundaryError, match="connection probe failed"):
        retriever.run_stage(_run_input(), runtime)
    assert called == []


def test_typed_retrieval_stage_rejects_non_contract_legacy_result(monkeypatch):
    cancel_event = threading.Event()
    retriever = _retriever(cancel_event)
    monkeypatch.setattr(retriever, "run", lambda *args, **kwargs: object())
    runtime = ReasoningRetrievalRuntime(
        scope=None,
        retrieval_run=None,
        cancellation=cancel_event,
        trace_sink=None,
        connection_probe=None,
    )

    with pytest.raises(StageBoundaryError, match="invalid reasoning retrieval result"):
        retriever.run_stage(_run_input(), runtime)


def test_typed_retrieval_stage_rejects_non_core_cancellation_token(monkeypatch):
    token = object()
    retriever = _retriever(token)
    called = []
    monkeypatch.setattr(
        retriever, "run", lambda *args, **kwargs: called.append(True)
    )
    runtime = ReasoningRetrievalRuntime(
        scope=None,
        retrieval_run=None,
        cancellation=token,  # type: ignore[arg-type]
        trace_sink=None,
        connection_probe=None,
    )

    with pytest.raises(StageBoundaryError, match="cancellation authority"):
        retriever.run_stage(_run_input(), runtime)
    assert called == []


def test_public_ask_owns_one_scope_run_and_cancellation_authority(monkeypatch):
    service = _minimal_ask_service()
    cancel_event = threading.Event()
    seen = {}

    def prepare(notebook_id, payload, *, user_id, job_id, runtime):
        seen["prepare_runtime"] = runtime
        seen["prepare_scope"] = current_source_scope()
        seen["prepare_run"] = current_retrieval_run()
        return object()

    def execute(prepared, runtime):
        seen["execute_runtime"] = runtime
        seen["execute_scope"] = current_source_scope()
        seen["execute_run"] = current_retrieval_run()
        return CommittedReasoningAnswer(
            response=AskResponse(conclusion="ok")
        )

    monkeypatch.setattr(service, "_prepare_reasoning_ask", prepare)
    monkeypatch.setattr(service, "_run_reasoning_stage", execute)
    response = service.ask(
        "nb",
        AskRequest(question="q", mode="reasoning"),
        user_id="user",
        cancel_event=cancel_event,
    )

    runtime = seen["prepare_runtime"]
    assert response.conclusion == "ok"
    assert seen["execute_runtime"] is runtime
    assert seen["prepare_scope"] is runtime.scope
    assert seen["execute_scope"] is runtime.scope
    assert seen["prepare_run"] is runtime.retrieval_run
    assert seen["execute_run"] is runtime.retrieval_run
    assert runtime.retrieval_run.run_kind == "ask_reasoning"
    assert runtime.retrieval_run.actor_id == "user"
    assert runtime.cancellation is cancel_event
    assert runtime.retrieval_run.cancel_event is cancel_event


def test_ask_stage_binds_run_actor_to_explicit_persistence_user():
    service = _minimal_ask_service()
    cancel_event = threading.Event()
    with retrieval_run(
        run_kind="ask_reasoning",
        actor_id="other-user",
        cancel_event=cancel_event,
    ):
        with pytest.raises(StageBoundaryError, match="actor authority changed"):
            service.ask_reasoning(
                "nb",
                AskRequest(question="q", mode="reasoning"),
                user_id="user",
                cancel_event=cancel_event,
            )


def test_commit_stage_preserves_the_shared_response_object_graph(monkeypatch):
    service = _minimal_ask_service()
    citation = Citation(
        label="source",
        source_id="source-1",
        element_id="element-1",
        location_label="§1",
        quoted_span="evidence",
    )
    response = AskResponse(
        conclusion="ok",
        citations=[citation, citation],
    )
    assert response.citations[0] is response.citations[1]
    monkeypatch.setattr(service, "_save_answer", lambda *args, **kwargs: "answer-1")
    runtime = ReasoningRetrievalRuntime(
        scope=None,
        retrieval_run=None,
        cancellation=None,
        trace_sink=None,
        connection_probe=None,
    )
    draft = ReasoningResponseDraft(
        notebook_id="nb",
        question="q",
        response=response,
        conversation_id="conv",
        user_id="user",
        job_id="",
        asked_at="",
    )

    committed = service._commit_reasoning_draft(draft, runtime)

    assert committed.response is response
    assert committed.response.citations[0] is citation
    assert committed.response.citations[1] is citation
    assert committed.response.answer_id == "answer-1"


@pytest.mark.parametrize("authoritative_held,fake_held", [(True, False), (False, True)])
def test_commit_stage_requires_the_injected_connection_probe_identity(
    monkeypatch, authoritative_held, fake_held,
):
    service = _minimal_ask_service()
    service.retrieval_connection_probe = SimpleNamespace(
        is_connection_held=lambda: authoritative_held
    )
    saves = []
    monkeypatch.setattr(service, "_save_answer", lambda *args, **kwargs: saves.append(True))
    runtime = ReasoningRetrievalRuntime(
        scope=None,
        retrieval_run=None,
        cancellation=None,
        trace_sink=None,
        connection_probe=SimpleNamespace(is_connection_held=lambda: fake_held),
    )
    draft = ReasoningResponseDraft(
        notebook_id="nb",
        question="q",
        response=AskResponse(conclusion="ok"),
        conversation_id="conv",
        user_id="user",
        job_id="",
        asked_at="",
    )

    with pytest.raises(StageBoundaryError, match="connection authority changed"):
        service._commit_reasoning_draft(draft, runtime)
    assert saves == []


def test_commit_stage_rejects_wrong_run_kind_and_empty_actor(monkeypatch):
    service = _minimal_ask_service()
    saves = []
    monkeypatch.setattr(service, "_save_answer", lambda *args, **kwargs: saves.append(True))
    cancel_event = threading.Event()
    with retrieval_run(
        run_kind="report_generation",
        actor_id="victim",
        cancel_event=cancel_event,
    ) as run:
        runtime = ReasoningRetrievalRuntime(
            scope=None,
            retrieval_run=run,
            cancellation=cancel_event,
            trace_sink=None,
            connection_probe=None,
        )
        draft = ReasoningResponseDraft(
            notebook_id="nb",
            question="q",
            response=AskResponse(conclusion="ok"),
            conversation_id="conv",
            user_id="victim",
            job_id="",
            asked_at="",
        )
        with pytest.raises(StageBoundaryError, match="run kind changed"):
            service._commit_reasoning_draft(draft, runtime)

        empty_actor = dataclasses.replace(draft, user_id="")
        with pytest.raises(StageBoundaryError, match="actor authority"):
            service._commit_reasoning_draft(empty_actor, runtime)
    assert saves == []


def test_memory_return_cancellation_prevents_unconfigured_answer_persistence():
    cancel_event = threading.Event()
    service = _minimal_ask_service()
    service.model_clients.primary_unconfigured = lambda: True
    saves = []
    service._save_answer = lambda *args, **kwargs: saves.append(True)

    class Memory:
        def notebook_memory_hits(self, *_args):
            cancel_event.set()
            return []

    service.memory_retriever = Memory()
    with pytest.raises(AskCancelled):
        service.ask_reasoning(
            "nb",
            AskRequest(question="q", mode="reasoning"),
            user_id="user",
            cancel_event=cancel_event,
        )
    assert saves == []


def test_memory_return_cancellation_prevents_structured_fallback_persistence():
    cancel_event = threading.Event()
    service = _minimal_ask_service()
    service.model_clients.primary_unconfigured = lambda: True
    service.knowhow_store = _EnumerableKnowhow()
    saves = []
    service._save_answer = lambda *args, **kwargs: saves.append(True)

    class Memory:
        def notebook_memory_hits(self, *_args):
            cancel_event.set()
            return []

    service.memory_retriever = Memory()
    with pytest.raises(AskCancelled):
        service.ask_reasoning(
            "nb",
            AskRequest(
                question="列出所有方法并比较优缺点",
                mode="reasoning",
            ),
            user_id="user",
            cancel_event=cancel_event,
        )
    assert saves == []


def test_stage_boundary_failure_is_not_swallowed_as_empty_retrieval(monkeypatch):
    service = _minimal_ask_service()
    saves = []
    service._save_answer = lambda *args, **kwargs: saves.append(True)

    def fail_stage(self, stage, runtime):
        raise StageBoundaryError("authority mismatch")

    monkeypatch.setattr(ReasoningRetriever, "run_stage", fail_stage)
    with pytest.raises(StageBoundaryError, match="authority mismatch"):
        service.ask_reasoning(
            "nb",
            AskRequest(question="q", mode="reasoning"),
            user_id="user",
        )
    assert saves == []


def test_application_stage_contracts_do_not_import_implementation_layers():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app/application/ask_reasoning.py"
    ).read_text(encoding="utf-8")
    assert "app.services" not in source
    assert "app.repositories" not in source
    assert "app.extensions" not in source
