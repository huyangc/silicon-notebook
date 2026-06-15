from app.services.retrieval import score_chunks, RetrievedChunk


def _ck(cid, text):
    return {"chunk_id": cid, "source_id": "s1", "source_title": "Doc",
            "section_path": "1", "text": text, "element_ids": ["e1"]}


def test_score_chunks_keyword_only_filters_floor():
    chunks = [_ck("c1", "deepseek mixture of experts routing"),
              _ck("c2", "unrelated cooking recipe tomato")]
    out = score_chunks("deepseek experts routing", chunks, query_vector=None, chunk_sims=None, limit=10)
    ids = [c.chunk_id for c in out]
    assert "c1" in ids and "c2" not in ids      # c2 低于 RELEVANCE_FLOOR 被丢
    assert all(isinstance(c, RetrievedChunk) for c in out)
    assert out[0].relevance > 0 and out[0].object_id == out[0].chunk_id


def test_score_chunks_caps_to_limit_sorted():
    chunks = [_ck(f"c{i}", f"shared term token{i}") for i in range(20)]
    out = score_chunks("shared term", chunks, query_vector=None, chunk_sims=None, limit=5)
    assert len(out) == 5
    assert all(out[i].score >= out[i+1].score for i in range(len(out)-1))


def test_score_chunks_uses_semantic_sims():
    chunks = [_ck("c1", "no keyword overlap here")]
    # 仅语义信号(关键词 0): chunk_sims 给高余弦 → 仍能过 floor。
    out = score_chunks("totally different words", chunks,
                       query_vector=[0.1]*4, chunk_sims={"c1": 0.9}, limit=10)
    assert [c.chunk_id for c in out] == ["c1"]
    assert out[0].relevance >= 0.5
