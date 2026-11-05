"""The real Ask stages preserve analysis in every synthesis route."""
import json
import re

import pytest

from app.domain.analysis_handoff import AnalysisEvidence, AnalysisFinding, AnalysisHandoff
from app.models.schemas import AskRequest
from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever
from app.services.response_drafting import DefaultResponseDraftStage
from tests import test_reasoning_outline_synthesis as outline_cases
from tests.test_reasoning_outline_synthesis import (
    _ask_service,
    _chunk,
    _element,
    _notebook,
    _section,
    _stub_run,
    _synthesis_detail,
)

repo = outline_cases.repo
_synthetic_elements_are_not_dangling = outline_cases._synthetic_elements_are_not_dangling


ASSESSMENT = "条件对照结论：需要同时满足精度与吞吐要求"
QUALIFIER = "限定条件：在相同工艺和负载下比较"


def _analysis(key):
    return AnalysisHandoff(
        findings=(AnalysisFinding("comparison", "方案适用性", ASSESSMENT,
                                  "supported", (key,)),),
        unresolved=(QUALIFIER,), stop_reason="evidence_sufficient",
    )


def _payload(prompt):
    for line in prompt.splitlines():
        if line.startswith('{"findings":'):
            return json.loads(line)
    pytest.fail("synthesis did not receive the analysis snapshot")


class AnalysisWriter:
    configured = True
    model = "analysis-writer"

    def __init__(self, *, fail_second_section=False):
        self.prompts = []
        self.payloads = []
        self.fail_second_section = fail_second_section

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[-1]["content"]
        if '"relevant"' in schema_hint:
            return json.dumps({"relevant": []})
        self.prompts.append(prompt)
        payload = _payload(prompt)
        self.payloads.append(payload)
        # Two empty replies fail one section; the final ordinary attempt
        # subsequently succeeds, exercising the real retry/fallback seam.
        if self.fail_second_section and len(self.prompts) in (2, 3):
            return json.dumps({"answer": "", "grounded": False})
        available = payload["findings"][0]["available_evidence"]
        cite = available[0]["citation"] if available else ""
        return json.dumps({"answer": f"比较结论{cite}。", "grounded": True}, ensure_ascii=False)


def _ask(repo, notebook, writer, *, effort="exhaustive"):
    service = _ask_service(repo, writer)
    return service.ask_reasoning(notebook.id, AskRequest(
        question="比较不同实现流程在精度和吞吐方面的条件与局限",
        mode="reasoning", retrieval_effort=effort,
    ), user_id=repo.current_user().id)


def _result(dependency):
    return ReasoningResult(
        elements=[_element("e1", "第一方面"), _element("e2", "第二方面")],
        outline=[_section("s1", "第一方面", "e1"), _section("s2", "第二方面", "e2")],
        analysis=_analysis(dependency.key), analysis_evidence=(dependency,),
    )


@pytest.mark.parametrize("effort", ["standard", "exhaustive"])
def test_analysis_dependency_is_citable_in_single_and_every_section(repo, monkeypatch, effort):
    notebook = _notebook(repo)
    dependency = AnalysisEvidence("chunk", "critical", _chunk(
        "critical", "完整关键证据：吞吐提升的前提是精度约束得到满足"))
    _stub_run(monkeypatch, _result(dependency))
    writer = AnalysisWriter()
    response = _ask(repo, notebook, writer, effort=effort)

    assert len(writer.prompts) == (2 if effort == "exhaustive" else 1)
    expected_handles = []
    for prompt, payload in zip(writer.prompts, writer.payloads):
        assert ASSESSMENT in prompt and QUALIFIER in prompt
        assert dependency.value.text in prompt
        assert payload["stop_reason"] == "evidence_sufficient"
        finding = payload["findings"][0]
        assert finding["missing_evidence_keys"] == []
        expected_handles.append(finding["available_evidence"][0]["citation"][1:-1])
    assert {anchor.key for anchor in response.anchors} == set(expected_handles)
    assert {anchor.object_id for anchor in response.anchors} == {"critical"}
    assert response.evidence_level == "grounded"
    if effort == "exhaustive":
        assert expected_handles == ["k8001", "k18001"]
        assert all(item["grounded"] for item in _synthesis_detail(response)["section_grounded"])


def test_section_failure_keeps_analysis_and_dependency_on_ordinary_fallback(repo, monkeypatch):
    notebook = _notebook(repo)
    dependency = AnalysisEvidence("chunk", "critical", _chunk("critical", "必须保留的跨方面证据"))
    _stub_run(monkeypatch, _result(dependency))
    writer = AnalysisWriter(fail_second_section=True)
    response = _ask(repo, notebook, writer)
    assert len(writer.prompts) == 4
    assert _synthesis_detail(response)["outline_fallback"] is True
    assert "## 第一方面" not in response.answer
    for prompt in writer.prompts:
        assert ASSESSMENT in prompt and QUALIFIER in prompt
        assert dependency.value.text in prompt
    final_cite = writer.payloads[-1]["findings"][0]["available_evidence"][0]["citation"][1:-1]
    assert [(anchor.key, anchor.object_id) for anchor in response.anchors] == [(final_cite, "critical")]


def test_analysis_with_no_delivered_evidence_still_discloses_its_unresolved_dependencies(repo, monkeypatch):
    notebook = _notebook(repo)
    _stub_run(monkeypatch, ReasoningResult(analysis=_analysis("unavailable")))
    writer = AnalysisWriter()
    response = _ask(repo, notebook, writer, effort="standard")
    assert len(writer.prompts) == 1
    assert ASSESSMENT in writer.prompts[0] and QUALIFIER in writer.prompts[0]
    assert writer.payloads[0]["findings"][0]["missing_evidence_keys"] == ["unavailable"]
    assert "相关结论仍需核实" in response.answer
    assert not response.grounded


@pytest.mark.parametrize("kind", ["chunk", "element"])
def test_ordinary_synthesis_restores_a_partial_dependency_with_a_complete_citation(
    repo, monkeypatch, kind
):
    notebook = _notebook(repo)
    text = "完整限定条件必须一起进入最终合成。" * 30
    item = _chunk("critical", text) if kind == "chunk" else _element("critical", text)
    dependency = AnalysisEvidence(kind, "critical", item)
    _stub_run(monkeypatch, _result(dependency))
    original = DefaultResponseDraftStage._assemble_reasoning_context
    def small_source_partition(self, *args, **kwargs):
        kwargs["chunk_context_chars"] = 100
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DefaultResponseDraftStage, "_assemble_reasoning_context", small_source_partition)
    writer = AnalysisWriter()
    response = _ask(repo, notebook, writer, effort="standard")
    assert text in writer.prompts[0]
    finding = writer.payloads[0]["findings"][0]
    assert finding["missing_evidence_keys"] == []
    assert finding["available_evidence"][0]["citation"] == "[k8001]"
    assert [(anchor.key, anchor.object_id) for anchor in response.anchors] == [("k8001", "critical")]
    assert response.evidence_level == "grounded"


@pytest.mark.parametrize("kind", ["chunk", "element"])
@pytest.mark.parametrize("effort", ["standard", "exhaustive"])
def test_partial_dependency_is_disclosed_in_summary_and_answer(repo, monkeypatch, kind, effort):
    notebook = _notebook(repo)
    repo.settings.reasoning_analysis_evidence_chars = 0
    full_text = "不可丢失的限定条件" * 20000
    item = _chunk("oversized", full_text) if kind == "chunk" else _element("oversized", full_text)
    dependency = AnalysisEvidence(kind, "oversized", item)
    _stub_run(monkeypatch, _result(dependency))
    writer = AnalysisWriter()
    response = _ask(repo, notebook, writer, effort=effort)
    assert writer.payloads
    for payload in writer.payloads:
        finding = payload["findings"][0]
        assert finding["available_evidence"] == []
        assert finding["missing_evidence_keys"] == ["oversized"]
        assert finding["review_required"] is True
    assert "分析所依赖的部分材料未能完整纳入本次回答；相关结论仍需核实。" in response.answer
    assert _synthesis_detail(response)["analysis_missing_evidence"] == 1


@pytest.mark.parametrize("output", ["answer", "evidence"])
def test_real_closing_rerank_cannot_erase_analysis_knowledge_dependency(repo, monkeypatch, output):
    notebook = _notebook(repo, kg=("关键工艺限制",))
    closing_calls = []
    def evict(self, notebook_id, question, collected, used_queries, top_n, outline, detail):
        closing_calls.append(tuple(collected))
        return [], []
    monkeypatch.setattr(ReasoningRetriever, "_closing_rerank", evict)

    class RealLoopModel(AnalysisWriter):
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "关键工艺限制"}]})
            if "next_action" in schema_hint:
                body = messages[-1]["content"].split("Candidates so far:", 1)[-1]
                keys = re.findall(r"\(id=([^)]+)\)", body)
                assert keys
                return json.dumps({"next_action": "answer", "sufficient": True,
                    "analysis": {"findings": [{"id": "limit", "aspect": "工艺条件",
                        "conclusion": ASSESSMENT, "status": "supported",
                        "evidence_keys": [keys[0]]}], "unresolved": [QUALIFIER]}}, ensure_ascii=False)
            return super().chat_json(messages, schema_hint, **kwargs)

    writer = RealLoopModel()
    if output == "evidence":
        _ask_service(repo, writer)
        evidence = repo.ask(notebook.id, AskRequest(
            question="关键工艺限制的适用条件", mode="reasoning", retrieval_effort="standard"),
            submitted_via="mcp", output="evidence")
        assert closing_calls and closing_calls[0]
        assert not writer.prompts
        assert evidence.items and evidence.evidence_kind == "retrieval"
        assert evidence.counts.by_kind["kg"].selected == 1
        assert any(item.anchor and item.anchor.object_id in closing_calls[0] for item in evidence.items)
        return
    response = _ask(repo, notebook, writer, effort="standard")
    assert closing_calls and closing_calls[0]
    assert len(writer.prompts) == 1
    assert "关键工艺限制" in writer.prompts[0]
    assert ASSESSMENT in writer.prompts[0]
    assert writer.payloads[0]["findings"][0]["missing_evidence_keys"] == []
    assert response.anchors
    assert {anchor.object_id for anchor in response.anchors} <= set(closing_calls[0])


@pytest.mark.parametrize("kind", ["chunk", "element"])
@pytest.mark.parametrize("extra_budget", [0, 12000])
def test_evidence_only_uses_the_same_dependency_admission(repo, monkeypatch, kind, extra_budget):
    notebook = _notebook(repo)
    repo.settings.reasoning_analysis_evidence_chars = extra_budget
    text = "完整限定条件应当保留" * 30
    item = _chunk("critical", text) if kind == "chunk" else _element("critical", text)
    dependency = AnalysisEvidence(kind, "critical", item)
    _stub_run(monkeypatch, ReasoningResult(
        analysis=_analysis("critical"), analysis_evidence=(dependency,)))
    original = DefaultResponseDraftStage._assemble_reasoning_context
    def small_source_partition(self, *args, **kwargs):
        kwargs["chunk_context_chars"] = 0
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DefaultResponseDraftStage, "_assemble_reasoning_context", small_source_partition)
    writer = AnalysisWriter()
    _ask_service(repo, writer)
    evidence = repo.ask(notebook.id, AskRequest(question="比较实现流程的适用条件", mode="reasoning"),
                        submitted_via="mcp", output="evidence")
    assert writer.prompts == []
    assert evidence.counts.by_kind[kind].selected == 1
    if extra_budget:
        delivered = [value for value in evidence.items if value.anchor]
        assert len(delivered) == 1
        assert delivered[0].kind == kind
        assert delivered[0].anchor.object_id == "critical"
        assert text in delivered[0].text
        assert not evidence.notice
    else:
        assert "相关结论仍需核实" in evidence.notice
