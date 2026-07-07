"""Task 8: expand_query 解析可选 comparison 字段(chunk 模式社区兄弟触发)。"""
from app.services.query_rewrite import expand_query


class _LLM:
    configured = True

    def __init__(self, payload):
        self._p = payload

    def chat_json(self, *a, **k):
        return self._p


def test_expand_parses_comparison():
    ex = expand_query(_LLM(
        '{"query":"q","sub_queries":[{"query":"a"}],"comparison":{"focal":"DeepSeek-V4"}}'),
        "分析DeepSeek-V4相比其他llm")
    assert ex.comparison == {"focal": "DeepSeek-V4"}


def test_expand_comparison_absent_is_none():
    ex = expand_query(_LLM('{"query":"q","sub_queries":[{"query":"a"}]}'), "介绍DeepSeek-V4")
    assert ex.comparison is None


def test_expand_comparison_empty_focal_is_none():
    ex = expand_query(_LLM(
        '{"query":"q","sub_queries":[{"query":"a"}],"comparison":{"focal":"  "}}'), "q")
    assert ex.comparison is None


def test_expand_fallback_has_no_comparison():
    ex = expand_query(_LLM("not json"), "q")  # 坏 JSON → fallback
    assert ex.comparison is None
