"""推断问答评测:跑 repo.ask + LLM-judge。"""
from __future__ import annotations
import json
import pathlib
from typing import Any, Dict, List

import yaml

_QUESTIONS_PATH = pathlib.Path(__file__).resolve().parent / "questions.yaml"

_JUDGE_SCHEMA = ('{"correctness":0,"inference_quality":0,'
                 '"grounding_consistency":true,"fabricated_citation":false,"reason":""}')


def load_questions(path=None) -> List[Dict[str, Any]]:
    """path 省略时用与本模块同目录的 questions.yaml(不依赖 CWD)。"""
    p = path or _QUESTIONS_PATH
    data = yaml.safe_load(open(p, encoding="utf-8"))
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


def run_inference(notebook_id: str,
                  questions_path: str = "backend/app/eval/questions.yaml"
                  ) -> List[Dict[str, Any]]:
    """对每题:repo.ask -> LLM-judge。返回逐题结果(含 judge)。"""
    from app.core.config import Settings
    from app.models.ask import AskRequest
    from app.services.kg.client import make_client
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(Settings())
    judge_client = make_client("EVAL_JUDGE_")
    assert judge_client.configured, "EVAL_JUDGE_OPENAI_COMPAT_* 未配置"
    questions = load_questions(questions_path)
    rows: List[Dict[str, Any]] = []
    for q in questions:
        resp = repo.ask(notebook_id, AskRequest(question=q["question"]))
        msgs = judge_prompt(q["question"], q["expected_points"], resp.answer or resp.conclusion,
                            resp.evidence_level, q["expected_behavior"])
        try:
            judged = parse_judge(judge_client.chat_json(msgs, _JUDGE_SCHEMA))
        except Exception as exc:  # judge 调用失败不应中断整轮
            judged = {"correctness": 0, "inference_quality": 0,
                      "grounding_consistency": False, "fabricated_citation": False,
                      "reason": f"judge_error: {type(exc).__name__}"}
        rows.append({
            "id": q["id"], "level": q["level"], "question": q["question"],
            "answer": resp.answer or resp.conclusion,
            "evidence_level": resp.evidence_level,
            "anchors": len(resp.anchors), "top_relevance": resp.top_relevance,
            "judge": judged,
        })
        print(f"[infer] {q['id']} {q['level']} -> correctness={judged['correctness']} "
              f"evidence={resp.evidence_level}", flush=True)
    return rows
