from app.services.retrieval import score_relations, RELEVANCE_FLOOR


def _rel(rid, text):
    return {"id": rid, "source_object_id": "s", "target_object_id": "t",
            "edge_type": "derived_from", "text": text}


def test_score_relations_keyword_only_full_match_is_one():
    # 无向量、关键词全命中 → _fuse 归一化后 relevance == 1.0(与 score_knowledge 同尺)
    hits = score_relations("cascode", [_rel("r1", "Regulated Cascode —derived_from→ Cascode.")])
    assert hits and hits[0].relation_id == "r1"
    assert abs(hits[0].relevance - 1.0) < 1e-9


def test_score_relations_uses_explicit_sims_and_stays_bounded():
    # 语义路径用显式 sims(测试 embedder 无语义);relevance ∈ [0,1]
    hits = score_relations("zzz no keyword overlap", [_rel("r1", "alpha beta gamma")],
                           query_vector=[0.1] * 4, relation_sims={"r1": 0.9})
    assert hits and 0.0 <= hits[0].relevance <= 1.0
    assert hits[0].relevance > 0.5  # 语义 0.9 主导


def test_score_relations_drops_below_floor():
    # 关键词 0、无向量 → relevance 0 < floor → 丢弃
    hits = score_relations("totally unrelated terms", [_rel("r1", "alpha beta")])
    assert hits == []


def test_score_relations_sorted_desc():
    rels = [_rel("r1", "cascode mirror"), _rel("r2", "cascode output resistance gain")]
    hits = score_relations("cascode output resistance", rels)
    scores = [h.relevance for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].relation_id == "r2"  # 更全匹配 query 的边排前
