import json
import pytest
from app.eval.inference import load_questions, judge_prompt, parse_judge


def test_load_questions():
    qs = load_questions()
    assert len(qs) == 30
    assert {q["level"] for q in qs} == {"L1", "L2", "L3", "L4"}
    assert all(q.get("question") and q.get("expected_points") for q in qs)


def test_judge_prompt_includes_answer_and_points():
    msgs = judge_prompt(question="什么是 cascode?", expected_points=["提高输出电阻"],
                        answer="cascode 提高输出电阻[k1]", evidence_level="grounded",
                        expected_behavior="grounded")
    assert isinstance(msgs, list) and msgs[-1]["role"] == "user"
    blob = msgs[-1]["content"]
    assert "提高输出电阻" in blob and "cascode 提高输出电阻" in blob


def test_parse_judge_ok_and_garbage():
    j = parse_judge(json.dumps({"correctness": 2, "inference_quality": 1,
                                "grounding_consistency": True,
                                "fabricated_citation": False, "reason": "好"}))
    assert j["correctness"] == 2 and j["fabricated_citation"] is False
    bad = parse_judge("not json")
    assert bad["correctness"] == 0 and "parse_error" in bad["reason"]


@pytest.mark.parametrize("raw", ["[]", "null", "true", "7", '"text"'])
def test_parse_judge_non_object_returns_parse_error(raw):
    result = parse_judge(raw)
    assert result["correctness"] == 0
    assert result["reason"] == "parse_error"


@pytest.mark.parametrize("invalid_score", [True, False, "2", 1.5, -1, 3])
def test_parse_judge_does_not_count_wrong_typed_or_out_of_range_scores(invalid_score):
    result = parse_judge(json.dumps({
        "correctness": invalid_score, "inference_quality": invalid_score,
    }))
    assert type(result["correctness"]) is int
    assert result["correctness"] == result["inference_quality"] == 0


@pytest.mark.parametrize(("value", "expected"), [
    (True, True), (False, False), ("true", True), ("false", False),
    (" TRUE ", True), ("false text", False), (1, False),
    (["false"], False), ({"value": True}, False),
])
def test_parse_judge_explicit_boolean_semantics(value, expected):
    result = parse_judge(json.dumps({
        "grounding_consistency": value, "fabricated_citation": value,
        "reason": {"text": "wrong type"},
    }))
    assert result["grounding_consistency"] is expected
    assert result["fabricated_citation"] is expected
    assert result["reason"] == ""
