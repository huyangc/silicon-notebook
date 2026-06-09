from app.services.retrieval import keyword_score, keyword_score_tokens, _tokens, _STOPWORDS
from app.services.retrieval import score_knowledge, _payload_text


def test_keyword_score_tokens_matches_string_version():
    query, text = "cascode output resistance", "the cascode raises output resistance"
    q_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    h_tokens = set(_tokens(text))
    assert abs(keyword_score_tokens(q_tokens, h_tokens) - keyword_score(query, text)) < 1e-12


def test_keyword_score_tokens_empty_query_is_zero():
    assert keyword_score_tokens(set(), {"a"}) == 0.0


def _obj(oid, name):
    return {"id": oid, "payload": {"name": name, "section_path": "1"}, "evidence": []}


def test_score_knowledge_pretokenized_equals_live():
    objs = [_obj("a", "cascode output resistance"), _obj("b", "current mirror")]
    live = score_knowledge("cascode resistance", objs, "claim")
    pre = {o["id"]: frozenset(_tokens(_payload_text(o["payload"]))) for o in objs}
    cached = score_knowledge("cascode resistance", objs, "claim", keyword_token_sets=pre)
    assert [(h.object_id, round(h.relevance, 9)) for h in live] == \
           [(h.object_id, round(h.relevance, 9)) for h in cached]


def test_score_knowledge_pretokenized_equals_live_with_evidence():
    from types import SimpleNamespace
    ev = [SimpleNamespace(quoted_span="gate oxide breakdown", element_id="e1")]
    objs = [
        {"id": "a", "payload": {"name": "cascode output resistance", "section_path": "1"}, "evidence": ev},
        {"id": "b", "payload": {"name": "current mirror", "section_path": "1"}, "evidence": []},
    ]
    live = score_knowledge("gate oxide cascode", objs, "claim")
    pre = {}
    for o in objs:
        ev_text = " ".join(e.quoted_span for e in o["evidence"])
        pre[o["id"]] = frozenset(_tokens(f"{_payload_text(o['payload'])} {ev_text}"))
    cached = score_knowledge("gate oxide cascode", objs, "claim", keyword_token_sets=pre)
    assert [(h.object_id, round(h.relevance, 9)) for h in live] == \
           [(h.object_id, round(h.relevance, 9)) for h in cached]
