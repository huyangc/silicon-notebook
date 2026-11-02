"""The default draft stage owns synthesis without retaining its orchestrator."""
from __future__ import annotations

import ast
import gc
import weakref
from pathlib import Path

import pytest

from app.application.ask_reasoning import ReasoningRetrievalRuntime, ResponseDraftInput
from app.models.ask import QueryIntentContract
from app.services import ask_service, response_drafting
from app.services.response_drafting import DefaultResponseDraftStage
from tests.test_ask_reasoning_stage_contract import _prepared_ask
from tests.test_ask_service_boundary import _minimal_ask_service


def test_default_stage_outlives_core_and_still_builds_the_complete_draft():
    service = _minimal_ask_service()
    drafting = service.response_draft_stage
    owner = weakref.ref(service)
    del service
    gc.collect()
    assert owner() is None
    assert ask_service.DefaultResponseDraftStage is DefaultResponseDraftStage
    assert ask_service._UNSET is response_drafting._UNSET

    prepared = _prepared_ask()
    stage = ResponseDraftInput(
        prepared=prepared,
        intent_contract=QueryIntentContract(objective="q", resolved_question="q"),
        top_hits=(), elements=(), trace=(), chunks=(), chains=(), enumerations=(),
        collection_map_text="", outline=(), outline_evidence=(), historical_chunks=(),
        memory_hits=(), structured_batch=None, completeness_unavailable=False,
        kg_required=False, candidate_manifest=None,
    )
    runtime = ReasoningRetrievalRuntime(
        scope=None, retrieval_run=None, cancellation=None, trace_sink=None,
        connection_probe=None,
    )

    draft = drafting.draft_response(stage, runtime)

    assert draft.response.mode == "reasoning"
    assert draft.response.llm_mode == "deterministic"
    assert draft.response.conclusion == ask_service._NO_RETRIEVAL_EVIDENCE_MESSAGE
    assert draft.response.answer_id == "" and draft.response.answer == ""
    assert draft.response.reasoning_trace is None
    assert draft.response.model_errors == []
    assert draft.conversation_id == prepared.conversation_id
    assert draft.user_id == prepared.user_id and draft.job_id == prepared.job_id
    assert draft.question == prepared.question and draft.asked_at == prepared.asked_at


def _ownership_errors(tree: ast.AST) -> list[str]:
    """Reject a core backreference or any persistence/job-control seat."""
    denied = {
        "AskService", "ask_state", "_service", "_save_answer",
        "_commit_reasoning_draft", "save_answer", "save_answer_for_job",
        "begin_durable_job", "finish_job", "cancellations",
    }
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "app.services.ask_service", "app.services.repository_runtime",
            "app.services.repository_facade",
        }:
            errors.append(node.module)
        elif isinstance(node, ast.Name) and node.id in denied:
            errors.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in denied:
            errors.append(node.attr)
        elif isinstance(node, ast.arg) and node.arg in denied:
            errors.append(node.arg)
    return errors


def test_response_draft_module_cannot_reach_core_or_persistence():
    source = Path(response_drafting.__file__).read_text(encoding="utf-8")
    assert _ownership_errors(ast.parse(source)) == []


@pytest.mark.parametrize("mutation", [
    "from app.services.ask_service import AskService",
    "self._service = service",
    "self.ask_state.save_answer_for_job()",
    "self._commit_reasoning_draft(stage, runtime)",
])
def test_draft_ownership_guard_detects_forbidden_dependency_mutations(mutation):
    assert _ownership_errors(ast.parse(mutation))
