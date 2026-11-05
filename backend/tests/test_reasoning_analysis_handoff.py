"""Analysis is a stage artifact, rather than text visible only in the trace."""
import json
import re

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.models.schemas import AskRequest
from app.services.reasoning_retrieval import ReasoningRetriever
from tests import test_reasoning_ask as reasoning_cases
from tests.test_reasoning_ask import _bind_reasoning, _seed

arepo = reasoning_cases.arepo


class AnalysisModel:
    configured = True

    def __init__(self):
        self.answer_inputs = []

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
        if "next_action" in schema_hint:
            ids = re.findall(r"id=([^\s)]+)", str(messages))
            return json.dumps({
                "next_action": "answer", "sufficient": True,
                "reason": "已完成条件对照",
                "analysis": {
                    "findings": [{"id": "flow", "aspect": "流程适用条件",
                        "conclusion": "交接结论：先满足工艺约束再比较实现流程",
                        "status": "supported", "evidence_keys": ids[:1]}],
                    "unresolved": ["尚未验证跨工艺的可比性"],
                },
            }, ensure_ascii=False)
        if '"relevant"' in schema_hint:
            return json.dumps({"relevant": []})
        self.answer_inputs.append(str(messages))
        return json.dumps({"answer": "流程比较须满足适用条件。", "grounded": False})


def test_analysis_survives_real_retrieval_to_answer_boundary(arepo):
    notebook = _seed(arepo)
    model = AnalysisModel()
    _bind_reasoning(arepo, model)
    arepo.ask(notebook.id, AskRequest(
        question="比较RTL到GDSII流程的适用条件与局限", mode="reasoning"))
    assert model.answer_inputs
    assert "交接结论：先满足工艺约束再比较实现流程" in model.answer_inputs[0]
    assert "尚未验证跨工艺的可比性" in model.answer_inputs[0]


def _snapshot(conclusion):
    return {"findings": [{"id": "condition", "aspect": "适用条件",
            "conclusion": conclusion, "status": "uncertain", "evidence_keys": []}],
            "unresolved": ["仍缺少对照实验"]}


class RoundModel(AnalysisModel):
    def __init__(self, decisions):
        super().__init__()
        self.decisions = iter(decisions)
        self.reflect_inputs = []

    def chat_json(self, messages, schema_hint, **kwargs):
        if "next_action" in schema_hint:
            self.reflect_inputs.append(messages[-1]["content"])
            decision = next(self.decisions)
            if isinstance(decision, Exception):
                raise decision
            return json.dumps(decision, ensure_ascii=False)
        return super().chat_json(messages, schema_hint, **kwargs)


def test_next_round_receives_snapshot_and_final_turn_replaces_it(arepo):
    notebook = _seed(arepo)
    model = RoundModel([
        {"next_action": "add_subquery", "sufficient": False,
         "new_sub_query": {"query": "工艺适用条件"}, "analysis": _snapshot("初步判断")},
        {"next_action": "answer", "sufficient": True, "analysis": _snapshot("修订后的判断")},
    ])
    _bind_reasoning(arepo, model)
    result = ReasoningRetriever.from_repository(arepo, arepo.settings).run(
        notebook.id, "RTL到GDSII流程", max_steps=2)
    assert len(model.reflect_inputs) == 2
    assert "初步判断" in model.reflect_inputs[1]
    assert result.analysis.findings[0].conclusion == "修订后的判断"
    assert result.analysis.stop_reason == "evidence_sufficient"


@pytest.mark.parametrize("last,steps,expected", [
    ({"next_action": "answer", "sufficient": False}, 2, "retrieval_exhausted"),
    ({"next_action": "add_subquery", "new_sub_query": {"query": "其他工艺"}}, 1, "budget_exhausted"),
    (RuntimeError("unavailable"), 2, "model_failure"),
])
def test_termination_does_not_discard_the_last_analysis(arepo, last, steps, expected):
    notebook = _seed(arepo)
    if steps == 1:
        last = {**last, "analysis": _snapshot("保留适用条件")}
        decisions = [last]
    else:
        decisions = [
            {"next_action": "add_subquery", "new_sub_query": {"query": "工艺适用条件"},
             "analysis": _snapshot("保留适用条件")}, last]
    model = RoundModel(decisions)
    _bind_reasoning(arepo, model)
    result = ReasoningRetriever.from_repository(arepo, arepo.settings).run(
        notebook.id, "RTL到GDSII流程", max_steps=steps)
    assert result.analysis.findings[0].conclusion == "保留适用条件"
    assert result.analysis.stop_reason == expected


def test_invalid_final_snapshot_is_disclosed_and_preserves_previous(arepo):
    notebook = _seed(arepo)
    model = RoundModel([
        {"next_action": "add_subquery", "new_sub_query": {"query": "工艺适用条件"},
         "analysis": _snapshot("仍有效的判断")},
        {"next_action": "answer", "analysis": {"findings": "invalid", "unresolved": []}},
    ])
    _bind_reasoning(arepo, model)
    result = ReasoningRetriever.from_repository(arepo, arepo.settings).run(
        notebook.id, "RTL到GDSII流程", max_steps=2)
    assert result.analysis.findings[0].conclusion == "仍有效的判断"
    assert result.analysis.issues == ("invalid_analysis_update",)


@pytest.mark.parametrize("update", [_snapshot("最新修订判断"), {"findings": [], "unresolved": []}])
def test_unavailable_action_still_hands_off_its_analysis_update_at_budget_end(arepo, update):
    notebook = _seed(arepo)
    model = RoundModel([
        {"next_action": "add_subquery", "new_sub_query": {"query": "工艺适用条件"},
         "analysis": _snapshot("旧判断")},
        {"next_action": "update_outline", "analysis": update},
    ])
    _bind_reasoning(arepo, model)
    result = ReasoningRetriever.from_repository(arepo, arepo.settings).run(
        notebook.id, "RTL到GDSII流程", max_steps=2,
        limits=ask_retrieval_limits("standard"))
    assert any(step.detail.get("reason") == "action_not_offered" for step in result.trace)
    assert [finding.conclusion for finding in result.analysis.findings] == [
        finding["conclusion"] for finding in update["findings"]]
    assert result.analysis.unresolved == tuple(update["unresolved"])
    assert result.analysis.stop_reason == "budget_exhausted"
