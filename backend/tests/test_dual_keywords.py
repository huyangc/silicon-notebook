from app.services.query_rewrite import expand_query, ExpandedQuery


class _FakeClient:
    configured = True
    def __init__(self, raw): self._raw = raw
    def chat_json(self, messages, schema_hint, **kw): return self._raw


def test_expand_query_parses_dual_keywords():
    raw = ('{"query":"how does cascode boost output resistance",'
           '"high_level_keywords":["output resistance","gain boosting"],'
           '"low_level_keywords":["cascode","r_ds"],'
           '"sub_queries":[{"query":"cascode output resistance"}]}')
    exp = expand_query(_FakeClient(raw), "cascode 怎么提高输出电阻")
    assert exp.high_level_keywords == ["output resistance", "gain boosting"]
    assert exp.low_level_keywords == ["cascode", "r_ds"]
    assert exp.sub_queries[0].query  # 子查询仍在


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
