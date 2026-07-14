from app.services.query_rewrite import expand_query, ExpandedQuery


class _FakeClient:
    configured = True
    def __init__(self, raw): self._raw = raw
    def chat_json(self, messages, schema_hint, **kw): return self._raw


def test_expand_query_missing_keywords_defaults_empty():
    raw = '{"query":"x","sub_queries":[{"query":"x"}]}'
    exp = expand_query(_FakeClient(raw), "x")
    assert exp.high_level_keywords == [] and exp.low_level_keywords == []


def test_expand_query_unconfigured_fallback_has_empty_keywords():
    class Off: configured = False
    exp = expand_query(Off(), "anything")
    assert isinstance(exp, ExpandedQuery)
    assert exp.high_level_keywords == [] and exp.low_level_keywords == []
    assert exp.sub_queries  # 始终 >=1
