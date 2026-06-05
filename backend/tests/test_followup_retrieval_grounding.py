import json, re, pytest


def test_settings_have_followup_and_evidence_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.followup_max_len == 12
    assert s.evidence_tau_low == 0.18
    assert s.evidence_tau_high == 0.35
    assert s.proc_min == 2

    monkeypatch.setenv("EVIDENCE_TAU_HIGH", "0.5")
    assert Settings().evidence_tau_high == 0.5


def test_looks_like_followup():
    from app.services.followup import looks_like_followup
    assert looks_like_followup("把这个流程按阶段画成流程图", 12) is True
    assert looks_like_followup("展开讲讲这个流程", 12) is True
    assert looks_like_followup("draw that flow as stages please now", 12) is True
    assert looks_like_followup("那 ECO 呢", 12) is True
    assert looks_like_followup("innovus中有哪些常见flow", 12) is False
    assert looks_like_followup("innovus是什么工具", 12) is False
    assert looks_like_followup("", 12) is False


def test_is_process_query_and_type_weight():
    from app.services.retrieval import is_process_query, type_weight
    assert is_process_query("展开讲讲RTL到GDSII的流程") is True
    assert is_process_query("把这个流程按阶段画成流程图") is True
    assert is_process_query("what are the place and route steps") is True
    assert is_process_query("innovus是什么工具") is False
    assert type_weight("procedure", False) == 0.7
    assert type_weight("claim", False) == 1.0
    assert type_weight("procedure", True) == 1.0
    assert type_weight("claim", True) == 0.9
    assert type_weight("concept", True) == 0.6


def _rk(oid, otype, score):
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=oid, object_type=otype, payload={},
                              score=score, relevance=score)


def test_ensure_procedure_quota_backfills_and_preserves_order():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    scored = [
        _rk("c1", "claim", 0.9), _rk("c2", "claim", 0.8), _rk("c3", "claim", 0.7),
        _rk("p1", "procedure", 0.6), _rk("p2", "procedure", 0.5), _rk("c4", "claim", 0.1),
    ]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    types = [h.object_type for h in out]
    assert types.count("procedure") == 2
    assert len(out) == 3
    assert out[0].object_id == "c1"
    assert [key(h) for h in out] == sorted((key(h) for h in out), reverse=True)

def test_ensure_procedure_quota_noop_when_enough():
    from app.services.retrieval import ensure_procedure_quota, type_weight
    key = lambda it: it.score * type_weight(it.object_type, True)
    scored = [_rk("p1", "procedure", 0.9), _rk("p2", "procedure", 0.8), _rk("c1", "claim", 0.7)]
    out = ensure_procedure_quota(scored, top_n=3, min_proc=2, key=key)
    assert [h.object_id for h in out] == ["p1", "p2", "c1"]


def _anchor(oid):
    from app.models.schemas import AnswerAnchor
    return AnswerAnchor(key="k1", object_id=oid, object_type="claim", label="x")


def test_classify_evidence_three_levels():
    from app.services.retrieval import classify_evidence
    strong = [_rk("a", "claim", 0.6), _rk("b", "claim", 0.2)]
    lvl, top = classify_evidence(strong, [_anchor("a")], True, 0.18, 0.35)
    assert lvl == "grounded" and top == 0.6
    weak = [_rk("a", "claim", 0.25)]
    lvl, _ = classify_evidence(weak, [_anchor("a")], True, 0.18, 0.35)
    assert lvl == "overview"
    lvl, _ = classify_evidence(weak, [_anchor("a")], True, 0.18, 0.35)
    assert lvl != "grounded"
    lvl, _ = classify_evidence(strong, [], True, 0.18, 0.35)
    assert lvl == "inferred"
    lvl, top = classify_evidence([], [], False, 0.18, 0.35)
    assert lvl == "inferred" and top == 0.0
