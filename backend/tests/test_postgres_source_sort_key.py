"""PostgreSQL 来源排序键的纯函数契约(codex PR#403 R2 P2)。

纯 Python、不连服务器——故放在主测试根,而不是 `backend/tests/postgres/`(那个目录
整个打了 `postgres_integration` 标记,无服务器时全部 skip,于是这条在默认 lane 里
根本不会跑,而它要防的正是一个**不需要服务器就能推翻**的排序错误)。

守的是什么:来源清单按 `(created_at, id)` 排,排序键是适配器归一化出来的文本。
`timestamptz` 回来的 UTC offset 不是一列之内的常量,所以「isoformat 后按字典序比」
只有在所有键共用同一个 offset 时才等价于 `ORDER BY created_at`。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.repositories.postgres.source_store import _sort_key


def test_sort_key_normalizes_aware_datetimes_to_utc():
    """带时区的时刻必须先归一到 UTC 再 isoformat。

    跨 DST 转换时,相隔一小时的两行可能读成 `…01:30:00+02:00` 与 `…01:30:00+01:00`。
    按字符串字典序比,是先比墙钟数字、再比 offset 文本——于是 `+01:00` 排在 `+02:00`
    前面,而它其实是**更晚**的那个瞬间。PostgreSQL 的 `ORDER BY` 比的是瞬间,所以
    不归一的话,来源清单会在 DST fold 附近静默地按一个来源页签从没显示过的顺序排出
    来(只在那一小时里错,平时看不出来)。
    """
    earlier = datetime(2026, 10, 25, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    later = datetime(2026, 10, 25, 1, 30, tzinfo=timezone(timedelta(hours=1)))
    # 前提:墙钟数字相同、offset 不同,且 later 确实是更晚的瞬间。
    assert earlier.isoformat()[:19] == later.isoformat()[:19]
    assert earlier < later
    # 归一之后字典序 == 瞬间序(这正是 ORDER BY created_at 给出的顺序)。
    assert _sort_key(earlier) < _sort_key(later)
    # 反例留在案上:不归一的形式给出**相反**的顺序,这就是这条守卫存在的理由。
    assert earlier.isoformat() > later.isoformat()
    # 同一瞬间的两种 offset 表示必须产出同一个键,否则它们会互相插队。
    assert _sort_key(later) == _sort_key(later.astimezone(timezone.utc))
    # 如实说明这条钉的是**性质**而不是具体时区:归一到任何一个固定 offset 都能让
    # 字典序等于瞬间序,所以「改成归一到 +08:00」这种变异不会红——它确实等价。
    # UTC 是可读性/可排查性上的选择,不是正确性上的唯一解。真正被钉住的是
    # 「所有键共用同一个 offset」,而这正是不归一时会被打破的那条。


def test_sort_key_leaves_naive_and_text_values_alone():
    """naive datetime 没有 offset(本来就是单一约定),文本列原样透传,NULL → ""。

    给 naive 值编一个时区是猜,而猜出来的顺序不会比原样更接近 SQL 的顺序。
    """
    naive = datetime(2026, 10, 25, 1, 30)
    assert _sort_key(naive) == naive.isoformat()
    assert _sort_key("2026-07-30T00:00:00") == "2026-07-30T00:00:00"
    assert _sort_key(None) == ""
    # naive 之间的相对顺序不受影响。
    assert _sort_key(datetime(2026, 1, 1)) < _sort_key(datetime(2026, 1, 2))
