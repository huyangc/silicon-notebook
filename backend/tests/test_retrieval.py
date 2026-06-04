from app.services.retrieval import keyword_score

def test_keyword_score_ignores_stopwords():
    # Verbose phrasing must not dilute the score: only content tokens count.
    # Basis after dropping stopwords (what/is/and/are/its) -> {engram, problems};
    # "problems" is a genuine content word absent from the short KG name, so it
    # remains in the denominator. The point is the score is no longer crushed by
    # the function words (raw token basis would be 8 -> 0.125).
    concise = keyword_score("engram", "Engram is a memory module")
    verbose = keyword_score("what is engram and what are its problems", "Engram is a memory module")
    assert concise == 1.0
    # Without stopword filtering this would be 1/8 = 0.125; with filtering the
    # basis is the 2 content tokens (engram hits) -> 0.5.
    assert verbose == 0.5
