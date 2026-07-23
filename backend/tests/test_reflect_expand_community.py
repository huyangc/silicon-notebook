"""Task 6: reflect() 的 expand_community 动作解析。

对比/广度题里,模型可选 next_action=expand_community 并给出 community_focal
(焦点实体名);reflect() 必须把该动作纳入白名单并解析出 community_focal。
"""
import json

from app.services.reasoning_retrieval import ReasoningRetriever


class _StubLLM:
    """按预置 JSON 应答 chat_json;configured 可控。"""

    def __init__(self, payload, configured=True):
        self._payload = payload
        self.configured = configured

    def chat_json(self, *a, **k):
        return self._payload


def _make_rr(payload, configured=True):
    client = _StubLLM(payload, configured)
    repo = type("R", (), {"chat": lambda self, workload_id: client})()
    settings = type("S", (), {"reasoning_timeout_seconds": 1,
                              "reasoning_max_retries": 0})()
    return ReasoningRetriever(
        retrieval=object(),
        model_clients=repo,
        communities=object(),
        settings=settings,
    )


def test_reflect_parses_expand_community():
    rr = _make_rr(json.dumps({
        "next_action": "expand_community",
        "community_focal": "DeepSeek-V4",
        "reason": "need peers",
    }))
    d = rr.reflect("q", "candidates")
    assert d.next_action == "expand_community"
    assert d.community_focal == "DeepSeek-V4"


def test_reflect_community_focal_defaults_empty():
    """未提供 community_focal 的其它动作,该字段解析为空串(不报错)。"""
    rr = _make_rr(json.dumps({"next_action": "answer", "sufficient": True}))
    d = rr.reflect("q", "candidates")
    assert d.community_focal == ""


def test_reflect_unknown_action_still_falls_back_to_answer():
    """白名单守卫:非枚举动作退回 answer(确保新增 expand_community 未放开兜底)。"""
    rr = _make_rr(json.dumps({"next_action": "teleport", "community_focal": "X"}))
    d = rr.reflect("q", "candidates")
    assert d.next_action == "answer"


def test_reflect_community_focal_stripped():
    rr = _make_rr(json.dumps({
        "next_action": "expand_community",
        "community_focal": "  Qwen-X  ",
    }))
    d = rr.reflect("q", "candidates")
    assert d.next_action == "expand_community"
    assert d.community_focal == "Qwen-X"
