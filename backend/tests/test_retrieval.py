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


def test_fuse_custom_weights_shift_balance():
    from app.services.retrieval import _fuse
    # 默认 0.4/0.6: 语义为 0 时融合分 = keyword * 0.4/(0.4+0.6) = 0.4
    assert abs(_fuse(1.0, 0.0, True) - 0.4) < 1e-9
    # keyword-heavy 0.7/0.3: 同输入下关键词权重更高
    assert abs(_fuse(1.0, 0.0, True, w_keyword=0.7, w_semantic=0.3) - 0.7) < 1e-9


def test_score_knowledge_passes_weights_through():
    from app.services.retrieval import score_knowledge
    objs = [{"id": "o1", "payload": {"name": "RTL synthesis flow"}, "evidence": []}]
    # 纯关键词(无向量)下,提高 w_keyword 不应改变 keyword-only 融合分(归一化抵消),
    # 但调用必须接受参数且不报错,返回命中。
    hits = score_knowledge("RTL synthesis", objs, "claim", w_keyword=0.7, w_semantic=0.3)
    assert hits and hits[0].object_id == "o1"
