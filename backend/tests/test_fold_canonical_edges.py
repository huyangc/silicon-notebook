"""`_fold_canonical_edges`:建图循环的两条契约。

1. 折叠语义不变:丢掉空端点与自环,canonical 下标按**首次出现序**分配(Louvain 对边序
   敏感),每条边原样喂给 builder。
2. 循环中途抛异常时,行流必须被 `close()`。PostgreSQL 上这条流是 `cursor().stream()`
   生成器:被丢下却仍被栈帧引用时,连接处于 ACTIVE 状态,调用方 `with self._connect()`
   离开时的 `rollback()` 会**永久阻塞**,而不是把异常抛出去(psycopg 3.3 实测)。这条
   用例用一个带 `finally` 的纯 Python 生成器代替真实游标 —— 少了 `closing`,`closed`
   为空、用例变红,而不是把测试进程挂住。
"""
from __future__ import annotations

import pytest

from app.services.knowledge_lifecycle import _fold_canonical_edges


class _Builder:
    def __init__(self, boom_on: int | None = None) -> None:
        self.edges: list[tuple[int, int]] = []
        self._boom_on = boom_on

    def add(self, left: int, right: int) -> None:
        if self._boom_on is not None and len(self.edges) + 1 == self._boom_on:
            raise RuntimeError("boom")
        self.edges.append((left, right))


def test_fold_skips_blank_endpoints_and_self_loops_and_numbers_by_first_appearance():
    rows = [
        {"s": "b", "t": "a"},      # b -> 0, a -> 1
        {"s": "a", "t": "a"},      # 自环:丢
        {"s": "", "t": "c"},       # 空端点:丢
        {"s": "c", "t": None},     # 空端点:丢
        {"s": "c", "t": "b"},      # c -> 2
        {"s": "b", "t": "a"},      # 重复边照样喂(合并在 build 里做)
    ]
    can2idx: dict[str, int] = {}
    builder = _Builder()

    _fold_canonical_edges((row for row in rows), can2idx, builder)

    assert can2idx == {"b": 0, "a": 1, "c": 2}
    assert builder.edges == [(0, 1), (2, 0), (0, 1)]


def test_fold_closes_the_row_stream_when_the_loop_raises():
    closed: list[bool] = []

    def stream():
        try:
            yield {"s": "a", "t": "b"}
            yield {"s": "b", "t": "c"}
            yield {"s": "c", "t": "d"}
        finally:
            closed.append(True)

    rows = stream()  # 测试栈帧一直引用着它:不靠回收,只能靠 fold 自己 close
    with pytest.raises(RuntimeError, match="boom"):
        _fold_canonical_edges(rows, {}, _Builder(boom_on=2))

    assert closed == [True]


def test_fold_leaves_an_exhausted_stream_closed_exactly_once():
    closed: list[bool] = []

    def stream():
        try:
            yield {"s": "a", "t": "b"}
        finally:
            closed.append(True)

    _fold_canonical_edges(stream(), {}, _Builder())

    assert closed == [True]
