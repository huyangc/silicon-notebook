"""Contract models, settings and vocabulary of "ask about a finished report".

Pins the part that must not move for everything that does not use the feature:
a turn, evidence result, conversation or in-flight job that attached no report
serializes exactly as it did before the feature existed (the new keys are
``exclude_if`` fields, absent when unset), and the request spelling of
``report_id`` is one rule for both Ask request models.
"""
from __future__ import annotations

import dataclasses
import json
from typing import get_args

import pytest
from pydantic import ValidationError

from app.domain import report_followup as rf
from app.models.ask import (
    ASK_REPORT_ID_MAX_CHARS,
    ActiveAskJob,
    AnswerAnchor,
    AskEvidence,
    AskEvidenceItem,
    AskEvidenceKind,
    AskIntentPreviewRequest,
    AskRequest,
    AskResponse,
    ConversationDetail,
    ConversationSummary,
    ConversationTurn,
    LinkedReportReceipt,
    LinkedReportRef,
    LinkedReportSection,
    ReportLinkCheck,
    ReportLinkCheckRequest,
)
from app.models.reports import ReportFollowupScope
from app.models.source_scope import BaseNotebookScope, SourceScope


def _keys_anywhere(value: object) -> set[str]:
    """Every dict key at any depth of a decoded JSON value."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= _keys_anywhere(item)
    elif isinstance(value, list):
        for item in value:
            found |= _keys_anywhere(item)
    return found


def _plain_response() -> AskResponse:
    return AskResponse(
        answer_id="a1",
        asked_at="2026-10-21T00:00:00+00:00",
        conclusion="结论",
        answer="回答 [k1]",
        grounded=True,
        evidence_level="grounded",
        anchors=[
            AnswerAnchor(
                key="k1",
                object_id="chunk-1",
                object_type="chunk",
                label="标题",
                snippet="片段",
                source_id="s1",
            )
        ],
        mode="chunk",
        conversation_id="c1",
        retrieval_query="q",
        top_relevance=0.5,
    )


def _receipt(**overrides) -> LinkedReportReceipt:
    fields = dict(report_id="rep-1", title="一份报告", status="read")
    fields.update(overrides)
    return LinkedReportReceipt(**fields)


# ---------------------------------------------------------------- unchanged ---


def test_models_without_a_report_serialize_without_any_linked_report_key():
    response = _plain_response()
    evidence = AskEvidence(
        mode="chunk",
        evidence_kind="retrieval",
        items=[AskEvidenceItem(key="k1", kind="chunk", text="x")],
    )
    summary = ConversationSummary(
        id="c1", notebook_id="n1", title="T", updated_at="u", turn_count=2
    )
    job = ActiveAskJob(job_id="j1", question="q", mode="chunk", trace=[{"a": 1}])
    detail = ConversationDetail(
        id="c1",
        notebook_id="n1",
        turns=[ConversationTurn(answer_id="a1", question="q", response=response)],
        active_job=job,
    )

    for model in (response, evidence, summary, job, detail):
        assert "linked_report" not in _keys_anywhere(json.loads(model.model_dump_json()))
        assert "linked_report" not in _keys_anywhere(model.model_dump())


def test_unlinked_shapes_keep_their_exact_key_sets():
    # The literal key lists are the pre-feature shapes: a new unconditional key
    # on any of these would change every stored and streamed payload.
    assert list(ConversationSummary(id="c", notebook_id="n").model_dump()) == [
        "id",
        "notebook_id",
        "title",
        "updated_at",
        "turn_count",
        "used_reasoning",
        "submitted_via",
    ]
    assert list(ActiveAskJob(job_id="j").model_dump()) == [
        "job_id",
        "question",
        "asked_at",
        "mode",
        "trace",
    ]
    assert list(
        ConversationDetail(id="c", notebook_id="n").model_dump()
    ) == [
        "id",
        "notebook_id",
        "title",
        "updated_at",
        "turn_count",
        "used_reasoning",
        "submitted_via",
        "turns",
        "active_job",
    ]
    assert "linked_report" not in AskEvidence(
        mode="chunk", evidence_kind="none"
    ).model_dump()


# --------------------------------------------------------------- report_id ---


@pytest.mark.parametrize("model", [AskRequest, AskIntentPreviewRequest])
def test_report_id_is_stripped_and_blank_reads_as_no_report(model):
    base = {"question": "追问"}

    assert model(**base).report_id is None
    assert model(**base, report_id=None).report_id is None
    assert model(**base, report_id="").report_id is None
    assert model(**base, report_id="   \t ").report_id is None
    assert model(**base, report_id="  rep-1 ").report_id == "rep-1"
    assert model(**base, report_id="rep-1").report_id == "rep-1"


@pytest.mark.parametrize("model", [AskRequest, AskIntentPreviewRequest])
def test_report_id_over_the_rail_is_rejected_not_clipped(model):
    ok = "r" * ASK_REPORT_ID_MAX_CHARS
    assert model(question="q", report_id=ok).report_id == ok

    with pytest.raises(ValidationError) as caught:
        model(question="q", report_id="r" * (ASK_REPORT_ID_MAX_CHARS + 1))
    assert caught.value.errors()[0]["loc"] == ("report_id",)


@pytest.mark.parametrize("model", [AskRequest, AskIntentPreviewRequest])
def test_report_id_length_is_judged_after_the_strip(model):
    ok = "r" * ASK_REPORT_ID_MAX_CHARS
    # Padding does not count against the rail ...
    assert model(question="q", report_id=f"  {ok}\t ").report_id == ok
    # ... but a stripped id that is still too long is refused, never clipped.
    with pytest.raises(ValidationError):
        model(question="q", report_id=f" {ok}x ")


def test_ask_requests_without_report_id_stay_valid_for_existing_clients():
    # The body a pre-feature client sends still validates, and the new field
    # is optional in the schema the API publishes: making it required (or
    # giving it a non-null default) would break every such client.
    for model in (AskRequest, AskIntentPreviewRequest):
        assert model.model_validate({"question": "q"}).report_id is None
        schema = model.model_json_schema()
        assert schema["required"] == ["question"]
        assert schema["properties"]["report_id"]["default"] is None


def test_report_link_check_request_rail():
    request = ReportLinkCheckRequest(report_id="rep-1")
    assert request.mode == "chunk"
    assert request.source_scope is None and request.base_scope is None

    for bad in ("", "   \t ", "r" * (ASK_REPORT_ID_MAX_CHARS + 1)):
        with pytest.raises(ValidationError):
            ReportLinkCheckRequest(report_id=bad)
    with pytest.raises(ValidationError):
        ReportLinkCheckRequest()  # report_id is required


def test_report_link_check_request_spells_report_id_like_the_ask_request():
    # Strip first, then judge the length: the same id means the same report on
    # the pre-send check and on the question that follows it.
    ok = "r" * ASK_REPORT_ID_MAX_CHARS
    assert ReportLinkCheckRequest(report_id="  rep-1 ").report_id == "rep-1"
    assert ReportLinkCheckRequest(report_id=f" {ok} ").report_id == ok
    assert AskRequest(question="q", report_id=f" {ok} ").report_id == ok
    with pytest.raises(ValidationError):
        ReportLinkCheckRequest(report_id=f" {ok}x ")


def test_report_link_check_reason_is_present_exactly_when_unreadable():
    readable = ReportLinkCheck(report_id="rep-1", title="题", readable=True)
    assert (readable.reason_code, readable.reason) == ("", "")

    refused = ReportLinkCheck(
        report_id="rep-1",
        title="题",
        readable=False,
        reason_code=rf.REASON_SCOPE_SOURCES,
        reason=rf.REASON_PREFIX + "它引用的 3 份资料不在当前检索范围内",
    )
    assert refused.readable is False

    with pytest.raises(ValidationError):  # readable, yet names a reason
        ReportLinkCheck(
            report_id="rep-1", title="题", readable=True, reason_code="scope_sources"
        )
    with pytest.raises(ValidationError):  # unreadable without saying why
        ReportLinkCheck(report_id="rep-1", title="题", readable=False)


# ----------------------------------------------------------------- receipt ---


def test_read_receipt_serializes_its_sections_and_counts():
    receipt = _receipt(
        sections=[
            LinkedReportSection(index=1, title="背景", read=True),
            LinkedReportSection(index=2, title="结论", read=False),
        ],
        summary_truncated=True,
        evidence_restored=18,
        evidence_gone=2,
        evidence_over_budget=1,
        memory_used=["mem-1"],
    )

    dumped = json.loads(receipt.model_dump_json())

    assert dumped == {
        "report_id": "rep-1",
        "title": "一份报告",
        "status": "read",
        "sections": [
            {"index": 1, "title": "背景", "read": True},
            {"index": 2, "title": "结论", "read": False},
        ],
        "summary_truncated": True,
        "evidence_restored": 18,
        "evidence_gone": 2,
        "evidence_over_budget": 1,
        "memory_used": ["mem-1"],
    }
    assert LinkedReportReceipt.model_validate(dumped) == receipt


def test_receipt_optional_fields_follow_the_exclude_if_convention():
    bare = json.loads(_receipt().model_dump_json())

    assert set(bare) == {
        "report_id",
        "title",
        "status",
        "evidence_restored",
        "evidence_gone",
        "evidence_over_budget",
    }
    assert bare["evidence_restored"] == bare["evidence_gone"] == 0
    assert bare["evidence_over_budget"] == 0


def test_skipped_receipt_carries_no_sections_or_memory():
    skipped = json.loads(
        _receipt(
            status="skipped",
            reason_code=rf.REASON_SCOPE_SOURCES,
            reason=rf.REASON_PREFIX + "它引用的 3 份资料不在当前检索范围内",
        ).model_dump_json()
    )

    assert skipped["status"] == "skipped"
    assert skipped["reason_code"] == "scope_sources"
    assert skipped["reason"].startswith("本轮不读取这份报告：")
    for absent in ("sections", "summary_truncated", "memory_used"):
        assert absent not in skipped
    assert (
        skipped["evidence_restored"],
        skipped["evidence_gone"],
        skipped["evidence_over_budget"],
    ) == (0, 0, 0)


@pytest.mark.parametrize(
    "carried",
    [
        {"sections": [LinkedReportSection(index=1, title="背景", read=True)]},
        {"summary_truncated": True},
        {"evidence_restored": 1},
        {"evidence_gone": 1},
        {"evidence_over_budget": 1},
        {"memory_used": ["mem-1"]},
    ],
    ids=[
        "sections",
        "summary_truncated",
        "evidence_restored",
        "evidence_gone",
        "evidence_over_budget",
        "memory_used",
    ],
)
def test_skipped_receipt_cannot_claim_anything_was_read(carried):
    with pytest.raises(ValidationError) as caught:
        _receipt(status="skipped", reason_code=rf.REASON_READ_FAILED, **carried)
    assert next(iter(carried)) in str(caught.value)

    # The same fields are legitimate on a turn that did read the report.
    assert _receipt(status="read", **carried).status == "read"


def test_skipped_receipt_check_applies_when_loading_a_stored_payload():
    stored = {
        "report_id": "rep-1",
        "title": "题",
        "status": "skipped",
        "evidence_restored": 3,
    }
    with pytest.raises(ValidationError):
        LinkedReportReceipt.model_validate(stored)


def test_receipt_status_is_read_or_skipped_only():
    with pytest.raises(ValidationError):
        _receipt(status="pending")


def test_receipt_title_is_kept_whole():
    title = "报告题目" * 1000  # the report question rail is 4000 characters
    assert _receipt(title=title).title == title


def test_receipt_round_trips_through_the_models_that_carry_it():
    receipt = _receipt(status="skipped", reason_code="read_failed", reason="x")

    response = AskResponse(conclusion="c", linked_report=receipt)
    assert json.loads(response.model_dump_json())["linked_report"]["report_id"] == "rep-1"
    assert AskResponse.model_validate_json(response.model_dump_json()) == response

    evidence = AskEvidence(mode="chunk", evidence_kind="none", linked_report=receipt)
    assert json.loads(evidence.model_dump_json())["linked_report"]["status"] == "skipped"
    assert AskEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_report_refs_appear_on_the_job_and_the_conversation_summary():
    ref = LinkedReportRef(report_id="rep-1", title="一份报告")

    job = ActiveAskJob(job_id="j1", linked_report=ref)
    assert json.loads(job.model_dump_json())["linked_report"] == {
        "report_id": "rep-1",
        "title": "一份报告",
    }

    summary = ConversationSummary(id="c1", notebook_id="n1", linked_report=ref)
    assert json.loads(summary.model_dump_json())["linked_report"]["report_id"] == "rep-1"

    # A deleted report reads back with an empty title, never a missing ref.
    assert LinkedReportRef(report_id="rep-1").title == ""

    detail = ConversationDetail(
        id="c1", notebook_id="n1", linked_report=ref, active_job=job
    )
    dumped = json.loads(detail.model_dump_json())
    assert dumped["linked_report"]["report_id"] == "rep-1"
    assert dumped["active_job"]["linked_report"]["report_id"] == "rep-1"


# ------------------------------------------------------------ follow-up scope ---


def test_followup_scope_defaults_to_the_whole_notebook_shape():
    scope = ReportFollowupScope(
        report_id="rep-1",
        title="题",
        source_scope=SourceScope(mode="exclude", source_ids=[]),
        base_scope=BaseNotebookScope(mode="exclude", notebook_ids=[]),
    )

    assert scope.dropped_sources == 0
    assert scope.dropped_libraries == 0
    assert scope.fell_back_to_all is False
    dumped = json.loads(scope.model_dump_json())
    assert dumped["source_scope"]["mode"] == "exclude"
    assert dumped["source_scope"]["source_ids"] == []
    assert dumped["base_scope"]["mode"] == "exclude"
    assert dumped["base_scope"]["notebook_ids"] == []


# --------------------------------------------------------- domain vocabulary ---


def test_every_reason_code_has_text_except_the_two_memory_clauses():
    assert len(set(rf.REASON_CODES)) == len(rf.REASON_CODES) == 8
    assert set(rf.REASON_CLAUSES) == set(rf.REASON_CODES) - {rf.REASON_MEMORY_CLOSED}
    assert rf.REASON_CLAUSE_MEMORY_CLOSED_NARROWED != rf.REASON_CLAUSE_MEMORY_CLOSED_CHANNEL
    assert rf.REASON_PREFIX == "本轮不读取这份报告："


def test_reason_clause_templates_fill_with_counts_and_names():
    assert (
        rf.REASON_CLAUSES[rf.REASON_LIBRARY_UNAVAILABLE].format(n=2)
        == "它引用的 2 个参考库本轮无法使用"
    )
    assert (
        rf.REASON_CLAUSES[rf.REASON_SCOPE_LIBRARIES].format(names="《甲》《乙》")
        == "它引用的参考库《甲》《乙》没有勾选"
    )
    assert (
        rf.REASON_CLAUSES[rf.REASON_SCOPE_SOURCES].format(n=7)
        == "它引用的 7 份资料不在当前检索范围内"
    )
    # Clauses with nothing to fill are safe to format with no arguments.
    for code in (
        rf.REASON_ENGINE_UNSUPPORTED,
        rf.REASON_REPORT_UNAVAILABLE,
        rf.REASON_READ_FAILED,
        rf.REASON_NO_SYNTHESIS,
    ):
        assert rf.REASON_CLAUSES[code].format() == rf.REASON_CLAUSES[code]


def test_reason_clauses_are_immutable():
    with pytest.raises(TypeError):
        rf.REASON_CLAUSES["scope_sources"] = "x"  # type: ignore[index]


def test_gate_exceptions_are_distinct():
    assert not issubclass(rf.LinkedReportNotFound, rf.LinkedReportNotReady)
    assert not issubclass(rf.LinkedReportNotReady, rf.LinkedReportNotFound)


def test_domain_values_are_frozen_and_default_empty():
    digest = rf.ReportDigest(
        title="题",
        summary="摘要",
        sections=(
            rf.ReportSection(index=1, title="背景", body="正文 [k1]", keys=("k1",)),
            rf.ReportSection(index=2, title="结论"),
        ),
    )
    assert digest.section_titles == ("背景", "结论")
    assert digest.references == () and digest.memory_used == ()
    assert digest.sections[1].body == "" and digest.sections[1].keys == ()

    verdict = rf.ReportLinkVerdict()
    assert verdict.readable is True
    assert (verdict.reason_code, verdict.reason, verdict.gone) == ("", "", 0)
    assert verdict.unavailable_libs == frozenset() and verdict.uses_memory is False

    restored = rf.RestoredReportEvidence()
    assert restored.chunks == restored.elements == restored.knowledge == ()
    assert restored.memory == () and restored.gone == 0
    assert dict(restored.key_by_object) == {}

    selection = rf.SectionSelection()
    assert selection.read_indices == () and selection.summary_truncated is False

    followup = rf.ReportFollowup(
        report_id="rep-1", title="题", digest=digest, verdict=verdict
    )
    assert followup.restored is None

    for value in (digest, verdict, restored, selection, followup, digest.sections[0]):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, "x")


def test_verdict_readable_is_derived_from_the_reason_code():
    assert rf.ReportLinkVerdict().readable is True
    refused = rf.ReportLinkVerdict(
        reason_code=rf.REASON_SCOPE_SOURCES, reason=rf.REASON_PREFIX + "x"
    )
    assert refused.readable is False

    # "readable with a reason" cannot even be written down.
    with pytest.raises(TypeError):
        rf.ReportLinkVerdict(readable=True, reason_code="scope_sources")  # type: ignore[call-arg]
    with pytest.raises(dataclasses.FrozenInstanceError):
        refused.reason_code = ""  # type: ignore[misc]


def test_restored_kinds_are_evidence_kinds():
    assert set(rf.RESTORED_KINDS) == set(get_args(rf.RestoredKind))
    assert set(rf.RESTORED_KINDS) <= set(get_args(AskEvidenceKind))
    assert len(rf.RESTORED_KINDS) == len(set(rf.RESTORED_KINDS)) == 4


def test_restored_key_by_object_is_a_read_only_copy_with_known_kinds():
    source = {
        (rf.RESTORED_KIND_CHUNK, "chunk-1"): ["k1", "k4"],
        (rf.RESTORED_KIND_ELEMENT, "el-1"): ["k2"],
    }
    restored = rf.RestoredReportEvidence(key_by_object=source)

    # Values are normalised to tuples; the caller's dict is not shared.
    assert restored.key_by_object[("chunk", "chunk-1")] == ("k1", "k4")
    source[("chunk", "chunk-2")] = ["k9"]
    assert ("chunk", "chunk-2") not in restored.key_by_object
    with pytest.raises(TypeError):
        restored.key_by_object[("kg", "ko-1")] = ("k3",)  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        restored.key_by_object = {}  # type: ignore[misc]

    # An empty default is read-only too, and never shared between instances.
    empty = rf.RestoredReportEvidence()
    with pytest.raises(TypeError):
        empty.key_by_object[("kg", "ko-1")] = ()  # type: ignore[index]
    assert empty.key_by_object is not rf.RestoredReportEvidence().key_by_object

    # A relation reference restores as its evidence element: there is no
    # "relation" kind to key by.
    with pytest.raises(ValueError):
        rf.RestoredReportEvidence(key_by_object={("relation", "rel-1"): ("k5",)})  # type: ignore[dict-item]
