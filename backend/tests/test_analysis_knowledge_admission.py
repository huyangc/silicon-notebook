"""Budget cuts must not masquerade as complete native knowledge projections."""
from app.domain.analysis_handoff import AnalysisEvidence, AnalysisFinding, AnalysisHandoff
from app.services.analysis_handoff import assemble_analysis_evidence
from app.services.retrieval import RetrievedKnowledge
from tests.test_evidence_context_service import _service


def _knowledge(monkeypatch):
    service = _service()
    monkeypatch.setattr(service.knowledge, "node_context", lambda *args, **kwargs: {
        "definition": "适用条件：工艺与负载均相同；精度约束满足后才可比较吞吐。" * 20,
        "occurrences": [], "steps": [{"name": f"step-{n}"} for n in range(10)],
    })
    item = RetrievedKnowledge(object_id="ko-critical", object_type="concept",
        payload={"name": "关键工艺条件"}, relevance=0.9)
    analysis = AnalysisHandoff(findings=(AnalysisFinding(
        "conditions", "条件对照", "需要共同满足限定条件", "supported", (item.object_id,)),))
    return service, item, analysis


def test_native_projection_distinguishes_its_usual_bounds_from_extra_budget_cuts(monkeypatch):
    service, item, _ = _knowledge(monkeypatch)
    complete, complete_map = service.knowledge_context("active", [item], budget_chars=2000)
    partial, partial_map = service.knowledge_context("active", [item], budget_chars=65)
    assert "step-7" in complete and "step-8" not in complete
    assert "analysis_projection_complete" not in complete_map["k1"]
    assert len(partial) == 65
    assert partial_map["k1"]["analysis_projection_complete"] is False


def test_partial_native_projection_is_restored_before_binding_analysis(monkeypatch):
    service, item, analysis = _knowledge(monkeypatch)
    context, id_map = service.knowledge_context("active", [item], budget_chars=65)
    def render(kind, value, **kwargs):
        assert kind == "knowledge"
        return service.knowledge_context("active", [value], **kwargs)
    result = assemble_analysis_evidence(context, id_map, analysis,
        (AnalysisEvidence("knowledge", item.object_id, item),),
        budget_chars=2000, key_offset=0, render=render)
    assert result.missing == ()
    assert result.bindings == {item.object_id: "k8001"}
    assert "step-7" in result.context_block
    assert "analysis_projection_complete" not in result.id_map["k8001"]


def test_insufficient_extra_budget_discloses_partial_native_projection(monkeypatch):
    service, item, analysis = _knowledge(monkeypatch)
    context, id_map = service.knowledge_context("active", [item], budget_chars=65)
    def render(kind, value, **kwargs):
        return service.knowledge_context("active", [value], **kwargs)
    result = assemble_analysis_evidence(context, id_map, analysis,
        (AnalysisEvidence("knowledge", item.object_id, item),),
        budget_chars=100, key_offset=0, render=render)
    assert result.bindings == {}
    assert result.missing == (item.object_id,)
    assert result.context_block == context and result.id_map == id_map
    assert result.added_chars == 0


def test_incomplete_projection_without_retained_value_is_still_missing(monkeypatch):
    service, item, analysis = _knowledge(monkeypatch)
    context, id_map = service.knowledge_context("active", [item], budget_chars=65)
    result = assemble_analysis_evidence(context, id_map, analysis, (),
        budget_chars=2000, key_offset=0,
        render=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not hydrate")))
    assert result.bindings == {} and result.missing == (item.object_id,)
