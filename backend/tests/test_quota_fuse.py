from app.services.retrieval import (
    RetrievalSupport,
    RetrievedChunk,
    quota_fuse,
    quota_fuse_baseline_first,
)
from dataclasses import dataclass


@dataclass
class _H:
    object_id: str
    relevance: float


def test_round_robin_balances_across_subqueries():
    a1, a2, b1 = _H("a1", .9), _H("a2", .8), _H("b1", .7)
    collected = {h.object_id: h for h in (a1, a2, b1)}
    per_q = [{"a1": a1, "a2": a2}, {"b1": b1}]      # 子查询A 命中 a1/a2;子查询B 命中 b1
    res, counts = quota_fuse(collected, per_q, top_n=2)
    assert {h.object_id for h in res} == {"a1", "b1"}   # 各组轮流取队首,B 的 b1 不被 A 通吃挤掉
    assert counts == [1, 1, 0]                          # [A, B, 兜底]


def test_fallback_group_when_unscored():
    x = _H("x", 0.0)
    res, counts = quota_fuse({"x": x}, [{}, {}], top_n=5)
    assert [h.object_id for h in res] == ["x"] and counts == [0, 0, 1]


def test_question_supplement_cannot_evict_multi_query_baseline():
    baseline = RetrievedChunk(
        chunk_id="baseline", source_id="s", source_title="s", section_path="",
        text="baseline", relevance=0.2,
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", "baseline", 0.2),
        ),
    )
    supplemental = RetrievedChunk(
        chunk_id="supplement", source_id="s", source_title="s", section_path="",
        text="supplement", relevance=0.99,
        retrieval_supports=(
            RetrievalSupport("generated_question", "chunk", "supplement", 0.99),
        ),
    )
    collected = {item.chunk_id: item for item in (supplemental, baseline)}

    selected, counts = quota_fuse_baseline_first(
        collected,
        [{"supplement": supplemental, "baseline": baseline}],
        top_n=1,
    )

    assert [item.chunk_id for item in selected] == ["baseline"]
    assert counts == [1, 0]


# ---------------------------------------------------------------------------
# enforce_active_floor:当前笔记本在**最终选择**里的保底份额
# ---------------------------------------------------------------------------


def _chunk(chunk_id: str, relevance: float, *, notebook_id: str = "",
           text: str = "", supports=()) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"src-{chunk_id}", source_title="t",
        section_path="", text=text or f"text-{chunk_id}",
        score=relevance, relevance=relevance, notebook_id=notebook_id,
        retrieval_supports=tuple(supports),
    )


def _question_only(chunk_id: str, relevance: float, *, notebook_id: str = ""):
    return _chunk(
        chunk_id, relevance, notebook_id=notebook_id, supports=(
            RetrievalSupport("generated_question", "chunk", chunk_id, relevance),
        ),
    )


def test_enforce_active_floor_is_inert_without_a_peer_hit():
    """全是当前库的命中(单库路径就是这个形态)→ 原样返回同一个 list 对象。"""
    from app.services.retrieval import enforce_active_floor

    selected = [_chunk(f"a{i}", 0.9 - i * 0.1) for i in range(3)]
    pool = selected + [_chunk("a9", 0.1)]

    assert enforce_active_floor(selected, pool, 2) is selected


def test_enforce_active_floor_is_inert_without_a_floor():
    from app.services.retrieval import enforce_active_floor

    selected = [_chunk("b1", 0.9, notebook_id="b")]
    pool = selected + [_chunk("a1", 0.3)]

    assert enforce_active_floor(selected, pool, 0) is selected
    assert enforce_active_floor([], pool, 4) == []


def test_enforce_active_floor_replaces_peers_from_the_tail():
    """只替换 peer、优先替换补充项、然后从尾部往前;长度与其余位置不变。"""
    from app.services.retrieval import enforce_active_floor

    selected = [
        _chunk("a-kept", 0.5),
        _chunk("b1", 0.9, notebook_id="b"),
        _question_only("b-q", 0.95, notebook_id="b"),
        _chunk("b2", 0.8, notebook_id="b"),
        _chunk("b3", 0.7, notebook_id="b"),
    ]
    spare = [_chunk("a1", 0.4), _chunk("a2", 0.3)]

    out = enforce_active_floor(selected, selected + spare, 3)

    assert len(out) == len(selected)
    assert [hit.chunk_id for hit in out] == [
        "a-kept", "b1", "a1", "b2", "a2",
    ], "补充项先让位,然后是排在最后的 peer;当前库命中一条不动"


def test_enforce_active_floor_is_capped_by_the_available_candidates():
    from app.services.retrieval import enforce_active_floor

    selected = [_chunk(f"b{i}", 0.9, notebook_id="b") for i in range(4)]
    out = enforce_active_floor(selected, selected + [_chunk("a1", 0.2)], 3)

    assert [hit.chunk_id for hit in out] == ["b0", "b1", "b2", "a1"]


def test_enforce_active_floor_deduplicates_by_text():
    """同一段正文的多个来源副本只花掉一个保底席位——与 `peer_evidence` 同一把尺。"""
    from app.services.retrieval import enforce_active_floor

    selected = [_chunk(f"b{i}", 0.9, notebook_id="b") for i in range(4)]
    copies = [
        _chunk(f"a{i}", 0.4 - i * 0.01, text="同一段正文") for i in range(4)
    ]

    out = enforce_active_floor(selected, selected + copies, 3)

    local = [hit for hit in out if not hit.notebook_id]
    assert len(local) == 1, f"同一段正文占了 {len(local)} 个保底席位"
    assert len(out) == 4


def test_enforce_active_floor_never_duplicates_what_is_already_selected():
    from app.services.retrieval import enforce_active_floor

    already = _chunk("a1", 0.4, text="同一段正文")
    selected = [already, *(_chunk(f"b{i}", 0.9, notebook_id="b") for i in range(3))]
    twin = _chunk("a2", 0.39, text="同一段正文")

    out = enforce_active_floor(selected, selected + [twin], 2)

    assert [hit.chunk_id for hit in out] == [hit.chunk_id for hit in selected]


def test_enforce_active_floor_is_deterministic():
    from app.services.retrieval import enforce_active_floor

    selected = [_chunk(f"b{i}", 0.9, notebook_id="b") for i in range(4)]
    spare = [_chunk("a1", 0.3), _chunk("a2", 0.3), _chunk("a3", 0.2)]

    first = enforce_active_floor(list(selected), selected + spare, 2)
    second = enforce_active_floor(list(selected), selected + spare, 2)

    assert [hit.chunk_id for hit in first] == [hit.chunk_id for hit in second]


def test_quota_fuse_baseline_first_reads_the_floor_off_the_mapping():
    """普通 dict 没有这个属性 → 零变化;带属性的映射 → 融合后兑现保底。"""
    from app.services.chunk_federation import with_active_reserve

    local = [_chunk(f"a{i}", 0.2 - i * 0.01) for i in range(4)]
    peers = [_chunk(f"b{i}", 0.9, notebook_id="b") for i in range(8)]
    rows = {hit.chunk_id: hit for hit in [*peers, *local]}
    # 一个子查询、一组:组内纯按相关度排,强参考库通吃——这正是保底要挡的形态。
    groups = [dict(rows)]

    plain, _counts = quota_fuse_baseline_first(dict(rows), groups, 8)
    assert sum(1 for hit in plain if not hit.notebook_id) == 0

    carried, _counts = quota_fuse_baseline_first(
        with_active_reserve(dict(rows), 3), groups, 8,
    )
    assert len(carried) == 8
    assert sum(1 for hit in carried if not hit.notebook_id) >= 3
