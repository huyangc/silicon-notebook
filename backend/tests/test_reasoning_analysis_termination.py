"""Terminal outline repair and external evidence preserve the analysis contract."""
import json

import pytest

from app.domain.analysis_handoff import AnalysisHandoff, parse_analysis_update, render_analysis_handoff
from app.services.analysis_handoff import assemble_analysis_evidence
from app.services.reasoning_retrieval import ReasoningRetriever, _external_block_text
from tests import test_reasoning_external_evidence as external_cases
from tests import test_reasoning_outline as outline_cases
from tests.test_reasoning_external_evidence import _ask, _bind, _external
from tests.test_reasoning_outline import (
    _OUTLINE_MAX_EVIDENCE,
    _outline_action,
    _retriever,
    _seed,
    _SeqLLM,
)

arepo = external_cases.arepo
repo = outline_cases.repo


def _analysis(conclusion, refs=()):
    return {"findings": [{"id": "conditions", "aspect": "比较条件",
            "conclusion": conclusion, "status": "supported", "evidence_keys": list(refs)}],
            "unresolved": []}


@pytest.mark.parametrize("boundary,expected", [
    ("sufficient", "evidence_sufficient"), ("stale", "retrieval_exhausted"),
])
def test_terminal_outline_repair_retains_stop_reason_and_accepts_analysis_update(
    repo, monkeypatch, boundary, expected,
):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr("app.services.reasoning_retrieval.outline_binding_keys",
                        lambda *_args: set(old_keys + [new_key]))
    repo.settings.reasoning_stale_limit = 2 if boundary == "stale" else 99
    overflow = dict(_outline_action([
        {"id": "a", "title": "条件分析", "evidence": [new_key]},
    ]), sufficient=boundary == "sufficient", analysis=_analysis("纠错前的判断"))
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "条件分析", "evidence": old_keys}]),
        overflow,
        dict(_outline_action([{
            "id": "a", "title": "条件分析", "evidence": [new_key],
            "remove_evidence": [old_keys[0]],
        }]), sufficient=False, analysis=_analysis("纠错轮更新后的判断")),
    ])
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "分析比较条件", limits=limits, max_steps=6)
    assert len(llm.reflect_prompts) == 3  # There was budget left at termination.
    assert result.outline[0].evidence_keys == old_keys[1:] + [new_key]
    assert result.analysis.findings[0].conclusion == "纠错轮更新后的判断"
    assert result.analysis.stop_reason == expected


def test_external_analysis_public_identity_maps_to_the_actual_synthesis_citation(arepo, monkeypatch):
    from tests.test_reasoning_external_evidence import _seed as seed_external

    notebook = seed_external(arepo)
    external = _external()
    first_round = ReasoningRetriever._run_first_round

    def with_external(self, state):
        first_round(self, state)
        state.external_evidence.append(external)
        state.external_block_text = _external_block_text(state.external_evidence, "")

    monkeypatch.setattr(ReasoningRetriever, "_run_first_round", with_external)

    class Model:
        configured = True

        def __init__(self):
            self.reflect_prompts = []
            self.answer_prompts = []

        def chat_json(self, messages, schema_hint, **kwargs):
            prompt = messages[-1]["content"]
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            if "next_action" in schema_hint:
                self.reflect_prompts.append(prompt)
                return json.dumps({"next_action": "answer", "sufficient": True,
                    "analysis": _analysis("外部研究支持该适用条件", ("x1",))}, ensure_ascii=False)
            if '"relevant"' in schema_hint:
                return json.dumps({"relevant": []})
            self.answer_prompts.append(prompt)
            return json.dumps({"answer": "适用条件得到外部研究支持[k6001]。", "grounded": True})

    model = Model()
    _bind(arepo, model)
    response = _ask(arepo, notebook)
    assert model.reflect_prompts and model.answer_prompts
    assert "(id=x1)" in model.reflect_prompts[0]
    for prompt in model.reflect_prompts + model.answer_prompts:
        assert external.plugin_id not in prompt
    payload = next(json.loads(line) for line in model.answer_prompts[0].splitlines()
                   if line.startswith('{"findings":'))
    finding = payload["findings"][0]
    assert finding["available_evidence"] == [{"key": "x1", "citation": "[k6001]"}]
    assert finding["missing_evidence_keys"] == []
    assert "相关结论仍需核实" not in response.answer
    assert [(anchor.key, anchor.object_id) for anchor in response.anchors] == [("k6001", external.key)]


def test_external_aliases_bind_only_the_entries_actually_admitted():
    analysis = parse_analysis_update(_analysis("比较外部研究", ("x1", "x2")), AnalysisHandoff())
    result = assemble_analysis_evidence(
        "k6001: only the second external item was admitted",
        {"k6001": {"object_id": "ext:private_plugin:2"}}, analysis, (),
        budget_chars=0, key_offset=0,
        render=lambda *_args, **_kwargs: pytest.fail("external aliases must not hydrate"),
        aliases={"x1": "ext:private_plugin:1", "x2": "ext:private_plugin:2"},
    )
    assert result.bindings == {"x2": "k6001"}
    assert result.missing == ("x1",)
    block = render_analysis_handoff(analysis, result.bindings)
    assert "private_plugin" not in block
    assert "[k6001]" in block
