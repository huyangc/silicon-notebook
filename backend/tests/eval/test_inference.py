import json
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
