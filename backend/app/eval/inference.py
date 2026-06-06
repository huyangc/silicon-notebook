"""推断问答评测:跑 repo.ask + LLM-judge。"""
from __future__ import annotations
import json
from typing import Any, Dict, List

import yaml

_JUDGE_SCHEMA = ('{"correctness":0,"inference_quality":0,'
                 '"grounding_consistency":true,"fabricated_citation":false,"reason":""}')


def load_questions(path: str) -> List[Dict[str, Any]]:
    data = yaml.safe_load(open(path, encoding="utf-8"))
    assert isinstance(data, list) and data, "questions.yaml 应为非空列表"
    return data


def judge_prompt(question: str, expected_points: List[str], answer: str,
                 evidence_level: str, expected_behavior: str) -> List[Dict[str, str]]:
    points = "; ".join(expected_points)
    user = (
        "你是严格的问答评委。根据【期望要点】评判【系统答案】,只看是否正确与推断是否恰当。\n"
        f"问题:{question}\n"
        f"期望要点:{points}\n"
        f"期望行为:{expected_behavior}(grounded=应有据引用 / use_neighbor=应用到邻居 / "
        "synthesize=应综合多个事实 / refuse_or_infer=KG 无据应说明或标(推断),不得伪造引用)\n"
        f"系统答案:{answer}\n"
        f"系统自报 evidence_level:{evidence_level}\n\n"
        "评分:correctness 0/1/2(覆盖要点且无事实错误);inference_quality 0/1/2"
        "(该综合时综合、该标推断时标);grounding_consistency(evidence_level 是否相符);"
        "fabricated_citation(是否给推断句/无关项强加 [k] 引用,L4 尤其关注)。"
        "reason 一句话。"
    )
    return [{"role": "user", "content": user}]


def parse_judge(raw: str) -> Dict[str, Any]:
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {"correctness": 0, "inference_quality": 0,
                "grounding_consistency": False, "fabricated_citation": False,
                "reason": "parse_error"}
    def _int(k):
        v = d.get(k, 0)
        return v if isinstance(v, int) and 0 <= v <= 2 else 0
    return {
        "correctness": _int("correctness"),
        "inference_quality": _int("inference_quality"),
        "grounding_consistency": bool(d.get("grounding_consistency", False)),
        "fabricated_citation": bool(d.get("fabricated_citation", False)),
        "reason": str(d.get("reason", ""))[:200],
    }
