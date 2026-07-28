from __future__ import annotations

from app.services.retrieval import (
    RetrievalSupport,
    RetrievedChunk,
    is_graph_only_chunk,
    merge_retrieval_supports,
    select_with_graph_reserve,
    truncate_by_tokens,
)


def _chunk(chunk_id: str, tokens: int, *supports: RetrievalSupport, relevance=0.8):
    # est_tokens uses ceil(chars/3.5); 7 chars are exactly two estimated tokens.
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id="s",
        source_title="source",
        section_path="",
        text="x" * (tokens * 3),
        element_ids=[f"e-{chunk_id}"],
        relevance=relevance,
        retrieval_supports=tuple(supports),
    )


def test_support_union_keeps_best_score_and_strictest_review_snapshot():
    merged = merge_retrieval_supports(
        (RetrievalSupport("relation", "relation", "r1", 0.4, "verified"),),
        (RetrievalSupport("relation", "relation", "r1", 0.9, "rejected"),),
        (RetrievalSupport("semantic", "chunk", "c1", 0.5),),
    )
    assert len(merged) == 2
    assert merged[0].score == 0.9
    assert merged[0].review_status_snapshot == "rejected"


def test_direct_and_graph_support_is_not_graph_only():
    chunk = _chunk(
        "both", 2,
        RetrievalSupport("semantic", "chunk", "both", 0.8),
        RetrievalSupport("ppr", "ppr", "", 0.7),
    )
    assert not is_graph_only_chunk(chunk)


def test_graph_reserve_evicts_lowest_direct_without_exceeding_budget():
    ranked = [
        _chunk("direct-1", 3, RetrievalSupport("semantic", "chunk", "direct-1", 0.9)),
        _chunk("direct-2", 3, RetrievalSupport("lexical", "chunk", "direct-2", 0.8)),
        _chunk("graph", 3, RetrievalSupport("ppr", "ppr", "", 0.7)),
    ]
    selected = select_with_graph_reserve(ranked, max_tokens=6, reserve=1)
    assert [chunk.chunk_id for chunk in selected] == ["direct-1", "graph"]


def test_rejected_relation_support_is_not_reserved():
    ranked = [
        _chunk("direct", 4, RetrievalSupport("semantic", "chunk", "direct", 0.9)),
        _chunk(
            "graph", 2,
            RetrievalSupport("relation", "relation", "r1", 0.9, "rejected"),
        ),
    ]
    assert [chunk.chunk_id for chunk in select_with_graph_reserve(
        ranked, max_tokens=4, reserve=1
    )] == ["direct"]


def test_rejected_relation_cannot_be_bypassed_by_additional_ppr_support():
    ranked = [
        _chunk("direct", 4, RetrievalSupport("semantic", "chunk", "direct", 0.9)),
        _chunk(
            "graph", 2,
            RetrievalSupport("relation", "relation", "r1", 0.9, "rejected"),
            RetrievalSupport("ppr", "ppr", "", 0.9),
        ),
    ]
    assert [chunk.chunk_id for chunk in select_with_graph_reserve(
        ranked, max_tokens=4, reserve=1
    )] == ["direct"]


def test_reserve_zero_is_characterization_equivalent_to_legacy_truncation():
    ranked = [
        _chunk("direct", 3, RetrievalSupport("semantic", "chunk", "direct", 0.9)),
        _chunk("graph", 3, RetrievalSupport("ppr", "ppr", "", 0.8)),
    ]
    expected = truncate_by_tokens(ranked, lambda chunk: chunk.text, 3)
    actual = select_with_graph_reserve(ranked, max_tokens=3, reserve=0)
    assert [chunk.chunk_id for chunk in actual] == [chunk.chunk_id for chunk in expected]


def test_graph_candidate_without_citable_elements_is_not_reserved():
    direct = _chunk(
        "direct", 4, RetrievalSupport("semantic", "chunk", "direct", 0.9)
    )
    graph = _chunk("graph", 2, RetrievalSupport("ppr", "ppr", "", 0.9))
    graph.element_ids = []
    assert [chunk.chunk_id for chunk in select_with_graph_reserve(
        [direct, graph], max_tokens=4, reserve=1
    )] == ["direct"]


def test_unusable_first_graph_candidate_does_not_starve_later_candidate():
    ranked = [
        _chunk("direct-1", 3, RetrievalSupport("semantic", "chunk", "direct-1", 0.9)),
        _chunk("direct-2", 3, RetrievalSupport("lexical", "chunk", "direct-2", 0.8)),
        _chunk("graph-too-large", 20, RetrievalSupport("ppr", "ppr", "", 0.9)),
        _chunk("graph-fit", 3, RetrievalSupport("ppr", "ppr", "", 0.8)),
    ]
    assert [chunk.chunk_id for chunk in select_with_graph_reserve(
        ranked, max_tokens=6, reserve=1
    )] == ["direct-1", "graph-fit"]


def test_global_first_oversize_semantics_are_preserved():
    ranked = [
        _chunk("oversize", 20, RetrievalSupport("semantic", "chunk", "oversize", 0.9)),
        _chunk("graph", 2, RetrievalSupport("ppr", "ppr", "", 0.8)),
    ]
    assert [chunk.chunk_id for chunk in select_with_graph_reserve(
        ranked, max_tokens=5, reserve=1
    )] == ["oversize"]
