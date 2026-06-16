from app.services.query_rewrite import normalize_terms


def test_splits_letter_digit_boundaries():
    assert normalize_terms("gpt4") == "gpt 4"
    assert normalize_terms("v100 gpu") == "v 100 gpu"
    assert normalize_terms("llama3 vs mistral7b") == "llama 3 vs mistral 7b"


def test_leaves_clean_text_untouched():
    assert normalize_terms("deepseek v2 改进") == "deepseek v2 改进"
    assert normalize_terms("") == ""


from app.services.query_rewrite import expand_query, ExpandedQuery
import json as _json


class _FakeLLM:
    def __init__(self, payload, configured=True, raise_exc=False):
        self.configured = configured; self._p = payload; self._raise = raise_exc
    def chat_json(self, messages, schema_hint, **kw):
        if self._raise: raise RuntimeError("boom")
        return _json.dumps(self._p)


def test_expand_parses_subqueries_and_english():
    llm = _FakeLLM({"query_en": "diff between DeepSeek V3 and V2",
                    "sub_queries": [{"query": "DeepSeek V3 improvements"},
                                    {"query": "DeepSeek V2 architecture"}]})
    ex = expand_query(llm, "deepseekv3相比deepseekv2有什么改进")
    assert isinstance(ex, ExpandedQuery)
    assert ex.query_en == "diff between DeepSeek V3 and V2"
    assert [s.query for s in ex.sub_queries] == ["DeepSeek V3 improvements", "DeepSeek V2 architecture"]


def test_expand_caps_and_dedups_and_drops_empty():
    subs = [{"query": f"q{i}"} for i in range(8)] + [{"query": "q0"}, {"query": "  "}]
    ex = expand_query(_FakeLLM({"query_en": "x", "sub_queries": subs}), "q", max_subqueries=4)
    assert len(ex.sub_queries) == 4 and len({s.query for s in ex.sub_queries}) == 4


def test_expand_want_types_keeps_kg_types():
    llm = _FakeLLM({"query_en": "x", "sub_queries": [
        {"query": "what is MLA", "types": ["concept", "bogus"], "prefer": "semantic"}]})
    ex = expand_query(llm, "MLA 是什么", want_types=True)
    s = ex.sub_queries[0]
    assert s.types == ["concept"] and s.prefer == "semantic"   # 过滤非法 type


def test_expand_fallback_on_unconfigured_exception_empty():
    for llm in (_FakeLLM({}, configured=False), _FakeLLM({}, raise_exc=True),
                _FakeLLM({"sub_queries": []}), _FakeLLM({"query_en": "x", "sub_queries": "nope"})):
        ex = expand_query(llm, "gpt4 对比")
        assert [s.query for s in ex.sub_queries] == ["gpt 4 对比"]   # 回退=normalize_terms(原问)
