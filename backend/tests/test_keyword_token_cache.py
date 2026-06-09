from app.services.retrieval import keyword_score, keyword_score_tokens, _tokens, _STOPWORDS


def test_keyword_score_tokens_matches_string_version():
    query, text = "cascode output resistance", "the cascode raises output resistance"
    q_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    h_tokens = set(_tokens(text))
    assert abs(keyword_score_tokens(q_tokens, h_tokens) - keyword_score(query, text)) < 1e-12


def test_keyword_score_tokens_empty_query_is_zero():
    assert keyword_score_tokens(set(), {"a"}) == 0.0
