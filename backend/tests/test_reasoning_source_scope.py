"""Run-level source ceilings for the reasoning agent."""

import json
from types import SimpleNamespace

import pytest

from app.models.common import Evidence
from app.models.ask import AskIntentConfirmation, AskRequest, QueryIntentContract
from app.core.config import Settings
from app.services.ask_service import AskService
from app.services.evidence_scope import (
    ResolvedEvidenceScope,
    SourceIdentitySnapshot,
    issue_source_scope_preview_capability,
)
from app.services.reasoning_retrieval import (
    ReasoningRetriever,
    ReflectDecision,
    SubQuery,
)
from app.services.retrieval import RetrievedElement, RetrievedKnowledge


class _Settings:
    retrieval_top_n = 12
    reasoning_top_n_per_query = 3
    reasoning_top_n_cap = 36
    reasoning_max_steps = 4
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 10
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = False
    graph_ppr_enabled = False
    reasoning_ppr_prefetch = False
    exact_lookup_enabled = True
    exact_lookup_max_identifiers = 3
    reasoning_timeout_seconds = 5
    reasoning_max_retries = 0
    community_peers_topk = 4
    community_rerank_candidates = 20


def _evidence(source_id: str) -> Evidence:
    return Evidence(
        source_id=source_id,
        source_title=f"Manual {source_id}",
        element_id=f"el-{source_id}",
        element_type="paragraph",
        location_label="Commands",
        quoted_span=f"command from {source_id}",
        confidence=1.0,
    )


def _hit(source_id: str) -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=f"ko-{source_id}",
        object_type="claim",
        payload={"name": f"command-{source_id}"},
        evidence=[_evidence(source_id)],
        relevance=0.9,
        score=0.9,
        notebook_id="nb",
    )


class _Retrieval:
    def __init__(self):
        self.ppr_calls = 0
        self.element_scope_calls = []
        self.kg_scope_calls = []
        self.rerank_scope_calls = []

    def federated_retrieve(self, *args, **kwargs):
        if "allowed_source_keys" in kwargs:
            self.kg_scope_calls.append(set(kwargs["allowed_source_keys"]))
        return [_hit("A"), _hit("B"), _hit("C")]

    def retrieve_elements(self, *args, **kwargs):
        return [
            RetrievedElement(
                element_id=f"el-{sid}", source_id=sid,
                source_title=f"Manual {sid}", location_label="Commands",
                element_type="paragraph", text=f"command from {sid}", score=0.8,
            )
            for sid in ("A", "B", "C")
        ]

    def federated_retrieve_elements(self, *args, **kwargs):
        allowed = set(kwargs["allowed_source_keys"])
        self.element_scope_calls.append(allowed)
        return [
            item for item in self.retrieve_elements(*args)
            if ("nb", item.source_id) in allowed
        ]

    def retrieve_scored(self, *args, **kwargs):
        # Deliberately tries to re-introduce Manual C at final rerank.
        if "allowed_source_ids" in kwargs:
            self.rerank_scope_calls.append(set(kwargs["allowed_source_ids"]))
        return [_hit("C"), _hit("A"), _hit("B")]

    def ppr_retrieve(self, *args, **kwargs):
        self.ppr_calls += 1
        raise AssertionError("restricted PPR must have zero I/O")

    def exact_lookup_chunks(self, *args, **kwargs):
        raise AssertionError("restricted exact lookup must have zero I/O")


class _ScopeDirectory:
    def __init__(self):
        self.calls = 0

    def resolve_evidence_scope(self, notebook_id, refs, **kwargs):
        self.calls += 1
        names = {"Manual A": "A", "Manual B": "B"}
        return ResolvedEvidenceScope.selected(
            SourceIdentitySnapshot(
                source_id=names[ref], notebook_id=notebook_id,
                title=ref, source_file_name=f"{names[ref]}.pdf",
            )
            for ref in refs
        )


class _Models:
    def __init__(self, client=None):
        self.client = client or type("Client", (), {"configured": False})()

    def chat(self, workload_id):
        return self.client


class _Communities:
    pass


def _selected_scope(*source_ids: str) -> ResolvedEvidenceScope:
    return ResolvedEvidenceScope.selected(
        SourceIdentitySnapshot(
            source_id=source_id,
            notebook_id="nb",
            title=f"Manual {source_id}",
            source_file_name=f"{source_id}.pdf",
        )
        for source_id in source_ids
    )


def test_confirmed_scope_excludes_third_manual_and_blocks_unsafe_io():
    retrieval = _Retrieval()
    directory = _ScopeDirectory()
    retriever = ReasoningRetriever(
        retrieval=retrieval,
        model_clients=_Models(),
        communities=_Communities(),
        settings=_Settings(),
        collection_enumeration=directory,
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="command mapping")]
    decisions = iter([
        ReflectDecision(
            next_action="search_evidence",
            evidence_query="command mapping",
            # A call may narrow the confirmed A+B ceiling without rewriting it.
            evidence_source_refs=["Manual A"],
        ),
        ReflectDecision(next_action="ppr_retrieve", ppr_query="command mapping"),
        ReflectDecision(next_action="answer", sufficient=True),
    ])
    retriever.reflect = lambda *args, **kwargs: next(decisions)

    result = retriever.run(
        "nb", "compare the commands", evidence_scope=_selected_scope("A", "B")
    )

    assert result.evidence_scope.restricted is True
    assert result.evidence_scope.allowed_source_ids == {"A", "B"}
    assert {hit.object_id for hit in result.top_hits} == {"ko-A", "ko-B"}
    assert all(
        evidence.source_id in {"A", "B"}
        for hit in result.top_hits
        for evidence in hit.evidence
    )
    # The explicit tool call searched a call-local A-only subset; the run
    # ceiling itself remains the user-confirmed A+B maximum.
    assert {element.source_id for element in result.elements} == {"A"}
    assert retrieval.ppr_calls == 0
    assert retrieval.element_scope_calls == [{("nb", "A")}]
    assert retrieval.kg_scope_calls[0] == {("nb", "A"), ("nb", "B")}
    assert {("nb", "A")} in retrieval.kg_scope_calls
    assert retrieval.rerank_scope_calls == [{"A", "B"}]
    assert directory.calls == 1
    assert not [step for step in result.trace if step.step_type == "source_scope"]


def test_all_source_run_cannot_establish_selected_scope_after_retrieval():
    retrieval = _Retrieval()
    retriever = ReasoningRetriever(
        retrieval=retrieval,
        model_clients=_Models(),
        communities=_Communities(),
        settings=_Settings(),
        collection_enumeration=_ScopeDirectory(),
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="command mapping")]
    retriever.reflect = lambda *args, **kwargs: ReflectDecision(
        next_action="search_evidence",
        evidence_query="command mapping",
        evidence_source_refs=["Manual A", "Manual B"],
    )

    with pytest.raises(ValueError, match="confirmed before retrieval"):
        retriever.run("nb", "compare the commands")


def test_selected_scope_resolution_failure_propagates_instead_of_using_old_pool():
    class _MissingDirectory:
        def resolve_evidence_scope(self, *args, **kwargs):
            raise ValueError("not found")

    retriever = ReasoningRetriever(
        retrieval=_Retrieval(), model_clients=_Models(),
        communities=_Communities(), settings=_Settings(),
        collection_enumeration=_MissingDirectory(),
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="command mapping")]
    retriever.reflect = lambda *args, **kwargs: ReflectDecision(
        next_action="search_evidence", evidence_query="command mapping",
        evidence_source_refs=["Missing Manual"],
    )

    with pytest.raises(ValueError, match="not found"):
        retriever.run(
            "nb", "compare the commands", evidence_scope=_selected_scope("A", "B")
        )


def test_restricted_run_rejects_malformed_enumeration_action_with_zero_io():
    class _NoEnumerationIO(_ScopeDirectory):
        def enumerate_elements(self, *args, **kwargs):
            raise AssertionError("restricted enumeration must have zero I/O")

        def enumerate_kg_objects(self, *args, **kwargs):
            raise AssertionError("restricted enumeration must have zero I/O")

    retriever = ReasoningRetriever(
        retrieval=_Retrieval(), model_clients=_Models(),
        communities=_Communities(), settings=_Settings(),
        collection_enumeration=_NoEnumerationIO(),
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="command mapping")]
    decisions = iter([
        ReflectDecision(next_action="enumerate_elements", enumerate_kind="formula"),
        ReflectDecision(next_action="answer", sufficient=True),
    ])
    retriever.reflect = lambda *args, **kwargs: next(decisions)

    result = retriever.run(
        "nb", "compare the commands", evidence_scope=_selected_scope("A", "B")
    )

    assert any(
        step.detail.get("reason") == "source_scope_unsafe_channel"
        for step in result.trace
    )
    assert result.enumerations == []


def test_reflect_parser_distinguishes_inherited_scope_from_explicit_empty_refs():
    class _Client:
        configured = True

        def __init__(self, payload):
            self.payload = payload
            self.schema = ""

        def chat_json(self, messages, schema_hint, **kwargs):
            self.schema = schema_hint
            return json.dumps(self.payload)

    inherited = _Client({
        "next_action": "search_evidence",
        "search_evidence": {"query": "command mapping"},
    })
    retriever = ReasoningRetriever(
        retrieval=_Retrieval(), model_clients=_Models(inherited),
        communities=_Communities(), settings=_Settings(),
        collection_enumeration=_ScopeDirectory(),
    )
    decision = retriever.reflect("q", "candidates")
    assert decision.evidence_source_refs is None
    assert '"search_evidence":{"query":""}' in inherited.schema
    assert '"source_refs":[""]' not in inherited.schema

    explicit_empty = _Client({
        "next_action": "search_evidence",
        "search_evidence": {"query": "command mapping", "source_refs": []},
    })
    retriever.model_clients = _Models(explicit_empty)
    decision = retriever.reflect("q", "candidates")
    assert decision.evidence_source_refs == []


def test_preview_resolves_selected_scope_and_reserves_confirmation_slot():
    question = "只依据《Manual A》回答"

    class _IntentClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "normalized_question": question,
                "source_refs": ["Manual A"],
                "ambiguities": [
                    {"question": f"blocking {index}", "required": True}
                    for index in range(8)
                ],
            })

    service = object.__new__(AskService)
    service.model_clients = SimpleNamespace(chat=lambda _workload: _IntentClient())
    service.settings = Settings()
    service.collection_enumeration = _ScopeDirectory()

    contract = service.preview_reasoning_intent("nb", question)

    assert contract.source_scope is not None
    assert [item.source_id for item in contract.source_scope.sources] == ["A"]
    assert len(contract.ambiguities) == 8
    assert contract.ambiguities[-1].id == "source_scope_confirmation"
    assert contract.needs_clarification is True


def test_execution_reauthorizes_snapshot_and_rejects_changed_source_set():
    service = object.__new__(AskService)
    service.collection_enumeration = _ScopeDirectory()
    intent = QueryIntentContract(
        objective="只依据《Manual A》回答",
        resolved_question="只依据《Manual A》回答",
        source_refs=["Manual A"],
        source_scope=AskService._intent_scope_snapshot(_selected_scope("A")),
        clarification_answers=[{
            "id": "source_scope_confirmation", "question": "确认范围？", "answer": "确认",
        }],
    )
    preview = intent.model_copy(update={
        "source_scope": intent.source_scope.model_copy(update={
            "preview_capability": issue_source_scope_preview_capability("nb", intent),
        }),
    })
    intent = intent.model_copy(update={"source_scope": preview.source_scope})
    assert service._confirmed_evidence_scope(
        "nb", intent, preview_contract=preview,
    ).allowed_source_ids == {"A"}

    class _ChangedDirectory:
        def resolve_evidence_scope(self, *args, **kwargs):
            return _selected_scope("B")

    service.collection_enumeration = _ChangedDirectory()
    with pytest.raises(ValueError, match="已删除、变更或不再可用"):
        service._confirmed_evidence_scope("nb", intent, preview_contract=preview)


def test_selected_scope_requires_issued_capability_and_exact_confirmation():
    service = object.__new__(AskService)
    service.collection_enumeration = _ScopeDirectory()
    intent = QueryIntentContract(
        objective="只依据《Manual A》回答",
        resolved_question="只依据《Manual A》回答",
        source_refs=["Manual A"],
        source_scope=AskService._intent_scope_snapshot(_selected_scope("A")),
    )
    with pytest.raises(ValueError, match="确认已失效"):
        service._confirmed_evidence_scope("nb", intent, preview_contract=intent)

    preview = intent.model_copy(update={
        "source_scope": intent.source_scope.model_copy(update={
            "preview_capability": issue_source_scope_preview_capability("nb", intent),
        }),
    })
    with pytest.raises(ValueError, match="明确确认"):
        service._confirmed_evidence_scope("nb", preview, preview_contract=preview)


def test_explicit_source_scope_cannot_be_downgraded_to_unsigned_all_sources():
    service = object.__new__(AskService)
    question = "只依据《Manual A》回答"
    payload = AskRequest(
        question=question,
        mode="reasoning",
        intent=AskIntentConfirmation(
            contract=QueryIntentContract(
                objective=question,
                resolved_question=question,
            ),
            resolved_question=question,
            answers=[],
        ),
    )

    with pytest.raises(ValueError, match="重新确认问题理解"):
        service.validate_reasoning_submission("nb", payload)


def test_federated_elements_group_mounted_sources_and_skip_empty_participant():
    from contextlib import contextmanager

    from app.services.retrieval_candidates import CandidateRetrievalService

    calls = []

    class _Notebooks:
        @staticmethod
        def participant_tiers(_db, _active):
            return ["active", "base", "unselected"], {
                "active": "personal", "base": "base", "unselected": "base"
            }

    class _Candidate:
        notebooks = _Notebooks()

        @contextmanager
        def _connect(self):
            yield object()

        def _retrieve_elements(
            self, notebook_id, query, limit=8, *, allowed_source_ids=None
        ):
            calls.append((notebook_id, tuple(allowed_source_ids or ())))
            return [RetrievedElement(
                element_id=f"el-{notebook_id}",
                source_id=allowed_source_ids[0],
                source_title=notebook_id,
                location_label="Commands",
                element_type="paragraph",
                text=query,
                score=0.9 if notebook_id == "base" else 0.8,
            )]

    hits = CandidateRetrievalService._federated_retrieve_elements_impl(
        _Candidate(), "active", "mapping",
        allowed_source_keys={("active", "A"), ("base", "B")},
        limit=8,
    )

    assert calls == [("active", ("A",)), ("base", ("B",))]
    assert [hit.source_id for hit in hits] == ["B", "A"]
