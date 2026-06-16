from app.services.retrieval import quota_fuse
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
