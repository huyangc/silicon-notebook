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
