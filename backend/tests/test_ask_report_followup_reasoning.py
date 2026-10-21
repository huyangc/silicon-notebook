"""The reasoning engine reading the deep report a question attaches (plan
2026-10-21 T8): the outline in the planning history and every reflect round,
the ``report`` trace step, the report block and the evidence partition in the
final synthesis, one synthesis for a turn that reads the report (D8), the
cards, the grounding and the receipt, the retrieval-only output -- on a real
SQLite repository with recording model fakes.

The end-to-end scenarios are shared with PostgreSQL
(``tests/ask_report_followup_reasoning_cases.py``); the rest is SQLite only.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from app.application.ask_reasoning import ReasoningIntentProjection
from app.core.config import Settings, get_settings
from app.domain.report_followup import (
    REASON_NO_SYNTHESIS,
    REPORT_EVIDENCE_NOT_LOADED,
    ReportFollowup,
    ReportLinkVerdict,
    RestoredReportEvidence,
)
from app.domain.retrieval_experience import project_run_step
from app.models.ask import AskRequest, LinkedReportReceipt, TraceStep
from app.services.ask_service import AskService
from app.services.embedding import FakeEmbedder
from app.services.prompts import ATTACHED_REPORT_ANSWER_RULE
from app.services.reasoning_retrieval import OutlineSection
from app.services.report_followup import (
    REPORT_BLOCK_HEADER,
    report_digest,
    report_followup_context,
    report_trace_step,
)
from app.services.retrieval_experience_projection import project_run
from app.services.sqlite_repository import SQLiteRepository
from tests import ask_report_followup_reasoning_cases as cases
from tests.model_testkit import (
    RecordingModelProvider,
    bind_all_embedding_clients,
    bind_chat_client,
)
from tests.report_reference_restore_cases import as_request_user
from tests.test_ask_service_boundary import _EnumerableKnowhow, _minimal_ask_service


@pytest.fixture
def make_world(tmp_path, monkeypatch):
    """A world on a fresh repository built with the given Settings fields."""
    repos: list = []
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    def make(**settings: Any):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f'reasoning-{len(repos)}.db'}")
        repo = SQLiteRepository(
            Settings(_env_file=None, **settings), model_provider=RecordingModelProvider(),
        )
        repos.append(repo)
        return cases.build_world(repo, postgres=False)

    yield make
    for repo in repos:
        repo.close()


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda scenario: scenario.__name__)
def test_ask_report_followup_reasoning(make_world, scenario):
    scenario(make_world())


# ------------------------------------------------------- planning and reflection
class _Agent:
    """The reasoning agent: plans one sub-query, then answers at the first
    reflect round. Every prompt it is shown is kept, by kind."""

    configured = True
    model = "fake-agent"

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {"plan": [], "reflect": []}

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            self.prompts["plan"].append(prompt)
            return json.dumps({"sub_queries": [{"query": "两种工艺的依据"}]})
        if "next_action" in schema_hint:
            self.prompts["reflect"].append(prompt)
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({})


def _real_retrieval(world) -> _Agent:
    agent = _Agent()
    bind_chat_client(world.repo, "reasoning_agent", agent)
    bind_all_embedding_clients(world.repo, FakeEmbedder(dim=get_settings().embed_dim))
    return agent


def test_planning_and_every_reflect_round_see_the_outline(make_world, monkeypatch):
    """The real retriever: its planning prompt (a request without a reviewed
    intent plans) carries the outline in its history, and the reflect round
    carries it too -- title and section titles, never the body."""
    world = make_world()
    monkeypatch.setattr(AskService, "_memory_hits", lambda self, *a, **k: [])
    report = cases.insert_report(world, "rep-plan")
    agent = _real_retrieval(world)
    answer_llm = cases.bind_answer(world, "答。")

    response = cases.ask(world, report)

    assert agent.prompts["plan"] and agent.prompts["reflect"]
    for prompt in (*agent.prompts["plan"], *agent.prompts["reflect"]):
        assert cases.OUTLINE in prompt
        assert "甲的温度窗口较窄" not in prompt and "两种工艺在温度窗口上不同" not in prompt
    assert REPORT_BLOCK_HEADER in answer_llm.prompts[-1]
    assert response.linked_report.status == "read"


def test_a_conflicting_turn_plans_and_reflects_on_the_title_alone(make_world, monkeypatch):
    world = make_world()
    monkeypatch.setattr(AskService, "_memory_hits", lambda self, *a, **k: [])
    report = cases.insert_report(world, "rep-plan-conflict")
    agent = _real_retrieval(world)
    answer_llm = cases.bind_answer(world, "答。")

    cases.ask(world, report, source_scope={
        "mode": "include", "source_ids": ["src-a"], "narrowed": True,
    })

    assert agent.prompts["plan"] and agent.prompts["reflect"]
    for prompt in (*agent.prompts["plan"], *agent.prompts["reflect"]):
        assert cases.TITLE_LINE in prompt
        assert "Report sections" not in prompt
        assert not any(title in prompt for title in cases.SECTIONS)
    assert all(REPORT_BLOCK_HEADER not in prompt for prompt in answer_llm.prompts)


def test_without_a_report_planning_and_reflection_are_unchanged(make_world, monkeypatch):
    world = make_world()
    monkeypatch.setattr(AskService, "_memory_hits", lambda self, *a, **k: [])
    agent = _real_retrieval(world)
    cases.bind_answer(world, "答。")

    response = cases.ask(world, None)

    assert agent.prompts["plan"] and agent.prompts["reflect"]
    assert not any(
        "Attached report" in prompt
        for prompt in (*agent.prompts["plan"], *agent.prompts["reflect"])
    )
    assert response.linked_report is None
    assert "report" not in [step.step_type for step in cases.steps_of(response)]


# ------------------------------------------------------------- the trace step
def test_the_report_step_accounts_for_reading_the_report(make_world, monkeypatch):
    """Judging and reading the report happens at the engine entry, before the
    trace exists; the ``report`` step carries that time, so the trace still
    covers the whole run."""
    world = make_world()
    report = cases.insert_report(world, "rep-timed")
    real = AskService._read_report_followup

    def slow(self, *args, **kwargs):
        time.sleep(0.05)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AskService, "_read_report_followup", slow)
    cases.bind_answer(world, "答[k1]。")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report)

    step = next(step for step in cases.steps_of(response) if step.step_type == "report")
    assert step.duration_ms >= 50


def test_the_report_step_says_which_sections_a_long_report_reads(make_world):
    """At the smallest report-context budget the research question picks the
    section it is about; the step names it, the receipt agrees, and only
    that section's cited items are read back into the step's count."""
    world = make_world(ask_report_context_max_chars=2000)
    report = cases.insert_report(world, "rep-long", content=cases.content_md(
        bodies=(cases.SECTION_ONE + "甲" * 1200, cases.SECTION_TWO + "乙沿用参考库" * 200),
    ))
    answer_llm = cases.bind_answer(world, "乙[k1001]。")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report, question="乙沿用参考库的概念依据是什么？")

    block = cases.report_block_of(answer_llm.prompts[0])
    assert "## 1. 工艺甲\n(body not loaded this turn: over the report-context budget)" in block
    assert "## 2. 工艺乙\n乙沿用参考库的概念 [k1001]，并结合个人记忆 [k3001]。" in block
    receipt = response.linked_report
    assert [(s.index, s.read) for s in receipt.sections] == [(1, False), (2, True)]
    assert (receipt.evidence_restored, receipt.evidence_over_budget) == (2, 0)
    step = next(step for step in cases.steps_of(response) if step.step_type == "report")
    assert step.summary == (
        f"已读取关联报告《{cases.TITLE}》：执行摘要与第 2 节（共 2 节，按与问题的相关度选取）"
        "；复原引用证据 2 条，1 条已失效"
    )


def test_the_report_step_counts_what_was_read_back_and_the_receipt_what_was_loaded(
    make_world,
):
    """At the smallest evidence budget the partition cannot hold every cited
    item: the trace step (written before the partition) counts the items read
    back, the receipt splits them into loaded and not loaded, and the
    not-loaded markers say so in the block."""
    world = make_world(ask_report_evidence_budget_chars=2000)
    world.execute("UPDATE chunks SET text={ph} WHERE id={ph}", ("甲段落" * 1000, "c-a"))
    report = cases.insert_report(world, "rep-budget")
    answer_llm = cases.bind_answer(world, "甲[k2]。")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report, question="甲的温度窗口较窄的依据")

    block = cases.report_block_of(answer_llm.prompts[0])
    assert REPORT_EVIDENCE_NOT_LOADED in block
    receipt = response.linked_report
    assert receipt.evidence_over_budget > 0
    step = next(step for step in cases.steps_of(response) if step.step_type == "report")
    assert step.detail["evidence_restored"] == (
        receipt.evidence_restored + receipt.evidence_over_budget
    ) == 5
    assert step.detail["evidence_over_budget"] == 0


def test_the_experience_projection_ignores_the_report_step():
    """The global experience library aggregates retrieval actions only: a
    run with a ``report`` step projects exactly as the same run without it."""
    intent = TraceStep(step_type="intent", summary="", detail=ReasoningIntentProjection(
        resolved_question="q", result_scope="ranked", completeness_required=False,
        retrieval_effort="standard", entities=("a",), constraints=(), excluded_topics=(),
        assumptions=(), expected_output="", mandatory_topics=(),
    ).as_json_mapping())
    synthesis = TraceStep(
        step_type="synthesis", summary="", detail={"anchors": 1, "anchor_evidence_ids": ["c-a"]},
    )
    report = report_trace_step(LinkedReportReceipt(
        report_id="rep", title="题", status="read", evidence_restored=3,
    ), digest=None)

    def projected(*steps):
        return project_run({"run_id": "run-1", "steps": [
            project_run_step(step.model_dump()) for step in steps
        ]})

    assert projected(intent, synthesis) is not None
    assert projected(intent, report, synthesis) == projected(intent, synthesis)


# ------------------------------------------------------------- one synthesis
def _outline() -> tuple:
    return (
        OutlineSection(id="s1", title="第一节", evidence_keys=["c-a"]),
        OutlineSection(id="s2", title="第二节", evidence_keys=["c-b"]),
    )


@pytest.mark.parametrize("attached", [False, True])
def test_a_turn_that_reads_the_report_keeps_one_synthesis(make_world, attached):
    """Exhaustive effort with a two-section outline: without a report the
    answer is written section by section; a turn that reads the report is
    written in one synthesis that carries the report block (D8)."""
    world = make_world()
    report = cases.insert_report(world, "rep-sections") if attached else None
    answer_llm = cases.bind_answer(world, "答[k1]。")
    with cases.stubbed_retrieval(cases.C_A, cases.C_B, outline=_outline()):
        response = cases.ask(world, report, retrieval_effort="exhaustive")

    if attached:
        assert len(answer_llm.prompts) == 1
        assert REPORT_BLOCK_HEADER in answer_llm.prompts[0]
        assert response.linked_report.status == "read"
    else:
        assert len(answer_llm.prompts) == 2
        assert all(REPORT_BLOCK_HEADER not in prompt for prompt in answer_llm.prompts)


# ------------------------------------------------------------- the answer
def test_the_not_loaded_mark_never_reaches_a_reasoning_answer(make_world):
    world = make_world()
    report = cases.insert_report(world, "rep-scrub")
    cases.bind_answer(world, f"甲的温度窗口较窄[k2]，其依据{REPORT_EVIDENCE_NOT_LOADED}未载入。")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report)

    assert response.answer == "甲的温度窗口较窄[k2]，其依据未载入。"
    assert REPORT_EVIDENCE_NOT_LOADED not in response.conclusion
    with as_request_user(world.alice):
        stored = world.repo.get_conversation(response.conversation_id).turns[-1].response
    assert REPORT_EVIDENCE_NOT_LOADED not in stored.answer + stored.conclusion


def test_an_ambiguous_question_about_a_report_is_answered_not_refused(make_world):
    """No reviewed intent, wording the deterministic gate would ask about
    ("这个结论"): the entry gate admits it because it names a report, and
    the engine's own compatibility gate stands down the same way instead of
    failing the admitted run."""
    world = make_world()
    report = cases.insert_report(world, "rep-ambiguous")
    cases.bind_answer(world, "甲的温度窗口较窄[k2]。")
    question = "这个结论的依据是什么？"
    payload = AskRequest(question=question, mode="reasoning", report_id=report)
    with as_request_user(world.alice):
        admitted = world.repo._runtime.ask_service().resolve_reasoning_followup(
            world.nb, payload,
        )
    assert admitted.gate_message == ""

    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report, question=question)

    assert response.answer and response.linked_report.status == "read"


def test_without_a_report_the_reasoning_answer_is_unchanged(make_world):
    world = make_world()
    answer_llm = cases.bind_answer(world, "答[k1]。")
    with cases.stubbed_retrieval(cases.C_B) as calls:
        response = cases.ask(world, None)

    assert calls == [{"question": calls[0]["question"], "history": "", "report_outline": ""}]
    prompt = answer_llm.prompts[0]
    assert REPORT_BLOCK_HEADER not in prompt and ATTACHED_REPORT_ANSWER_RULE not in prompt
    assert "Evidence the attached report cited" not in prompt
    assert response.linked_report is None
    assert "linked_report" not in response.model_dump_json()
    synthesis = cases.steps_of(response)[-1]
    assert "included_report_evidence" not in synthesis.detail


def test_a_readable_report_is_answered_even_when_retrieval_finds_nothing(make_world):
    """The turn's own retrieval comes back empty: the report's cited items
    are evidence of their own, so the answer is still written from them (the
    chunk engine's rule), and the retrieval-only output carries them."""
    world = make_world()
    report = cases.insert_report(world, "rep-only")
    answer_llm = cases.bind_answer(world, "甲的温度窗口较窄[k1]。")
    with cases.stubbed_retrieval():
        response = cases.ask(world, report)
        evidence = cases.ask(world, report, output="evidence")

    assert len(answer_llm.prompts) == 1
    assert "甲的温度窗口较窄 [k1]" in cases.report_block_of(answer_llm.prompts[0])
    assert [(a.key, a.object_id) for a in response.anchors] == [("k1", "c-a")]
    assert response.evidence_level == "grounded"
    assert response.linked_report.status == "read"
    assert evidence.evidence_kind != "none"
    assert {item.key for item in evidence.items if item.key} == {
        "k1", "k4001", "k4002", "k1001", "k3001",
    }


def _passage_with_image(world, element_ids: list[str]) -> None:
    world.execute(
        "INSERT INTO source_elements (id,source_id,element_type,location_label,"
        "text,metadata,created_at) VALUES ('el-a-img','src-a','image','p2','图',{js},{ph})",
        (json.dumps({"asset_id": "asset-a", "caption": "甲的温度曲线"}), cases.NOW),
    )
    world.execute(
        "UPDATE chunks SET element_ids={js} WHERE id={ph}", (json.dumps(element_ids), "c-a"),
    )


def test_a_bound_restored_passage_brings_its_images(make_world):
    """The restored passage's element ids join the anchor image candidates:
    the anchor on it shows the passage's image, which is not its first
    element."""
    world = make_world()
    _passage_with_image(world, ["el-a1", "el-a2", "el-a-img"])
    report = cases.insert_report(world, "rep-image")
    cases.bind_answer(world, "甲的温度窗口较窄[k2]。")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report)

    anchor = response.anchors[0]
    assert (anchor.key, anchor.object_id) == ("k2", "c-a")
    assert [image.asset_id for image in anchor.images] == ["asset-a"]


def test_a_restored_passage_card_takes_no_image_of_its_own(make_world):
    """The bound restored passage's card names its first element -- here the
    image. Like a retrieved passage's card it takes no image of its own: the
    anchor on the same passage already charged that image to the answer's
    image budget (as on the chunk engine, whose report cards never do)."""
    world = make_world()
    _passage_with_image(world, ["el-a-img", "el-a1", "el-a2"])
    report = cases.insert_report(world, "rep-image-card")
    cases.bind_answer(world, "甲的温度窗口较窄[k2]。")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report)

    assert [image.asset_id for image in response.anchors[0].images] == ["asset-a"]
    card = next(c for c in response.citations if c.element_id == "el-a-img")
    assert card.images == []


def test_the_context_bound_adds_what_the_report_partition_took(make_world):
    """The retrieval-only output's ``budget_chars`` is the bound its context
    never exceeds; the report partition has a budget of its own, so the bound
    grows by exactly what the partition took -- not by its nominal budget,
    and not by nothing."""
    world = make_world()
    report = cases.insert_report(world, "rep-bound")
    cases.bind_answer(world, "unused")
    with cases.stubbed_retrieval(cases.C_B):
        plain = cases.ask(world, None, output="evidence")
        attached = cases.ask(world, report, output="evidence")

    took = attached.context_chars - plain.context_chars
    assert took > 0
    assert attached.budget_chars - plain.budget_chars == took


# ------------------------------------------------------------- no synthesis
def test_an_unconfigured_answer_model_reads_no_report(make_world, monkeypatch):
    """The answer model is not configured: the run short-circuits before the
    report step, and the receipt says no answer model was called."""
    world = make_world()
    report = cases.insert_report(world, "rep-unconfigured")
    monkeypatch.setattr(AskService, "_primary_llm_unconfigured", lambda self: True)
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report)

    receipt = response.linked_report
    assert (receipt.status, receipt.reason_code) == ("skipped", REASON_NO_SYNTHESIS)
    assert "report" not in [step.step_type for step in cases.steps_of(response)]


def test_an_answer_client_that_never_runs_reads_no_report(make_world):
    """The registry is empty and no answer client is bound: the turn
    retrieves but never calls the answer model, and the receipt says so."""
    world = make_world()
    report = cases.insert_report(world, "rep-offline")
    with cases.stubbed_retrieval(cases.C_B):
        response = cases.ask(world, report)

    receipt = response.linked_report
    assert (receipt.status, receipt.reason_code) == ("skipped", REASON_NO_SYNTHESIS)
    assert receipt.sections == []


def test_a_structured_enumeration_reads_no_report():
    """A complete Knowhow enumeration answers without the answer model: the
    turn's receipt is ``no_synthesis`` and its trace has no report step."""
    row = {
        "question": cases.TITLE, "references": [], "memory_used": [],
        "sections": [{"title": title} for title in cases.SECTIONS],
        "content_md": cases.content_md(),
    }
    followup = ReportFollowup(
        "rep-structured", cases.TITLE, report_digest(row), ReportLinkVerdict(),
        RestoredReportEvidence(),
    )
    service = _minimal_ask_service()
    service.knowhow_store = _EnumerableKnowhow()

    with report_followup_context(followup):
        response = service.ask_reasoning("nb", AskRequest(
            question="所有方法有哪些？", mode="reasoning", retrieval_effort="overview",
            report_id="rep-structured",
        ), user_id="user")

    assert response.llm_mode == "structured"
    receipt = response.linked_report
    assert (receipt.report_id, receipt.status, receipt.reason_code) == (
        "rep-structured", "skipped", REASON_NO_SYNTHESIS,
    )
    assert "report" not in [step.step_type for step in response.reasoning_trace or []]
