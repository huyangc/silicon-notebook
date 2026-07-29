from __future__ import annotations

from app.services.retrieval import (
    RetrievalSupport,
    RetrievedChunk,
    exact_section_reserve_rule,
    graph_reserve_rule,
    is_graph_only_chunk,
    merge_retrieval_supports,
    select_with_graph_reserve,
    select_with_reserves,
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


# --------------------------------------------------------- multiple reserves
def test_two_reserves_share_one_budget_without_evicting_each_other():
    """The failure mode this rules out: the second reserve fills its floor by
    throwing out what the first reserve just rescued, so both report success
    and only one of them actually holds."""
    ranked = [
        _chunk("direct-1", 3, RetrievalSupport("semantic", "chunk", "direct-1", 0.9)),
        _chunk("direct-2", 3, RetrievalSupport("lexical", "chunk", "direct-2", 0.8)),
        _chunk("direct-3", 3, RetrievalSupport("lexical", "chunk", "direct-3", 0.7)),
        _chunk("graph", 3, RetrievalSupport("ppr", "ppr", "", 0.7)),
        _chunk("exact", 3, RetrievalSupport("lexical", "chunk", "exact", 0.1)),
    ]
    selected = select_with_reserves(ranked, 9, (
        graph_reserve_rule(1),
        exact_section_reserve_rule(1, {"exact"}),
    ))
    ids = [chunk.chunk_id for chunk in selected]
    assert "graph" in ids and "exact" in ids, ids
    assert sum(len(chunk.text) for chunk in selected) / 3.5 <= 9


def test_an_inert_reserve_protects_nothing():
    """A reserve set to 0 must be invisible: enabling the exact reserve cannot
    start protecting graph-only chunks that `chunk_graph_reserve=0` (the
    shipped default) has never protected."""
    ranked = [
        _chunk("direct-1", 3, RetrievalSupport("semantic", "chunk", "direct-1", 0.9)),
        _chunk("graph", 3, RetrievalSupport("ppr", "ppr", "", 0.7)),
        _chunk("exact", 3, RetrievalSupport("lexical", "chunk", "exact", 0.1)),
    ]
    selected = select_with_reserves(ranked, 6, (
        graph_reserve_rule(0),
        exact_section_reserve_rule(1, {"exact"}),
    ))
    assert [chunk.chunk_id for chunk in selected] == ["direct-1", "exact"]


def test_naturally_selected_holders_of_another_rule_stay_evictable():
    """质量评审阻塞项 3 的复现:exact 规则「天然入选」(非救回)的 chunk 不该对
    graph 规则免疫驱逐——否则 CHUNK_GRAPH_RESERVE>0 + 带标识符问题时 graph 保底
    静默失效。跨 rule 保护只覆盖真被救回(rescued)的 chunk。"""
    ranked = [
        _chunk("exact-1", 3, RetrievalSupport("lexical", "chunk", "exact-1", 0.9)),
        _chunk("exact-2", 3, RetrievalSupport("lexical", "chunk", "exact-2", 0.8)),
        _chunk("exact-3", 3, RetrievalSupport("lexical", "chunk", "exact-3", 0.7)),
        _chunk("graph", 3, RetrievalSupport("ppr", "ppr", "", 0.7)),
    ]
    selected = select_with_reserves(ranked, 9, (
        graph_reserve_rule(1),
        exact_section_reserve_rule(4, {"exact-1", "exact-2", "exact-3"}),
    ))
    ids = [chunk.chunk_id for chunk in selected]
    # 与旧单 rule select_with_graph_reserve(reserve=1) 的结果保持一致:graph 保底
    # 逐出末位 direct 候选;exact 规则不得为救回 exact-3 再逐出 graph。
    assert ids == ["exact-1", "exact-2", "graph"], ids


def test_exact_reserve_never_creates_a_second_oversize_exception():
    ranked = [
        _chunk("oversize", 20, RetrievalSupport("semantic", "chunk", "oversize", 0.9)),
        _chunk("exact", 2, RetrievalSupport("lexical", "chunk", "exact", 0.1)),
    ]
    assert [chunk.chunk_id for chunk in select_with_reserves(
        ranked, 5, (exact_section_reserve_rule(4, {"exact"}),)
    )] == ["oversize"]


def test_exact_reserve_is_bounded_by_its_setting():
    ranked = [
        _chunk("direct", 3, RetrievalSupport("semantic", "chunk", "direct", 0.9)),
        _chunk("exact-1", 3, RetrievalSupport("lexical", "chunk", "exact-1", 0.1)),
        _chunk("exact-2", 3, RetrievalSupport("lexical", "chunk", "exact-2", 0.1)),
        _chunk("exact-3", 3, RetrievalSupport("lexical", "chunk", "exact-3", 0.1)),
    ]
    selected = select_with_reserves(ranked, 6, (
        exact_section_reserve_rule(1, {"exact-1", "exact-2", "exact-3"}),
    ))
    assert [chunk.chunk_id for chunk in selected] == ["direct", "exact-1"]


def test_naturally_selected_quota_holder_of_another_rule_is_not_evicted():
    """Codex #399 P2:他 rule 的**配额内持有块**不可被驱逐——无论它是被救进来
    的还是天然入选的。graph 块天然入选时 graph rule 配额已满、不产生 rescue,
    此前 exact 插入会把它当普通候选驱逐,静默击穿 CHUNK_GRAPH_RESERVE。保护
    只覆盖配额内前 reserve 个持有块:超额持有块仍可驱逐,否则会反向复活
    「大 exact 节挡死 graph 驱逐」的旧 bug(见 two_reserves 用例)。"""
    ranked = [
        _chunk("direct-1", 3, RetrievalSupport("semantic", "chunk", "direct-1", 0.9)),
        _chunk("direct-2", 3, RetrievalSupport("lexical", "chunk", "direct-2", 0.8)),
        _chunk("graph", 3, RetrievalSupport("ppr", "ppr", "", 0.7)),
        _chunk("exact", 3, RetrievalSupport("lexical", "chunk", "exact", 0.1)),
    ]
    selected = select_with_reserves(ranked, 9, (
        graph_reserve_rule(1),
        exact_section_reserve_rule(1, {"exact"}),
    ))
    ids = [chunk.chunk_id for chunk in selected]
    # 驱逐受害者必须是 direct-2(最后一个普通候选),不是天然入选的 graph。
    assert "graph" in ids and "exact" in ids, ids
    assert "direct-2" not in ids, ids
