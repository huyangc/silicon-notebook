"""守卫：每一个真的被 ``record_change`` 写进流水的 ``kind``，都必须登记在
``knowhow_asset_refs.REGISTERED_KINDS`` 里。

为什么这条值得单独钉（生产热点整改批 E 评审 P2-3）：

``content_strings_in_payload`` 对未登记的 kind **抛异常**，这是刻意的封闭集合
——通用兜底「不认识就把整个 payload 当正文扫」会把 ``code_text`` 重新泄进
keeper 判定，正是那个函数存在的理由。但这个异常只在**孤儿图片清扫器**跑到那
条流水时才炸，而清扫是边缘触发的后台任务：新增一个 kind 却忘了登记，代码评审
和整套功能测试可以全绿，直到某个笔记本恰好又贴了图、又跑了清扫，才在后台抛出
来。既有的 ``test_knowhow_history_coverage_guard`` 钉的是「写事务有没有记流水」
这条上游契约，管不到「记下来的 kind 认不认得」这条下游契约。

判据是源码里 ``record_change(..., kind="...")`` 的**字面量取值集合**，两个后端
的 ``KnowhowStore`` 与共享的 ``KnowhowHistoryStore``（``revert`` 在那里写）各扫
一遍。非字面量的 kind 实参会被单独报红：AST 看不出它的取值，也就无法对账——真
需要动态 kind 时应显式登记豁免并说明理由，而不是让守卫默默漏过。

不经 import 定位源文件：``app.repositories.postgres`` 的导入会拖进 psycopg，而
本守卫跑在离线的 G1 泳道里，只需要读源码文本（与
``test_knowhow_history_coverage_guard`` 同一理由）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.repositories
from app.repositories.knowhow_asset_refs import (
    REGISTERED_KINDS,
    content_strings_in_payload,
)


_REPOSITORIES_DIR = Path(app.repositories.__file__).parent

#: 每一处调用 ``record_change`` 的源文件。两个后端的 KnowhowStore 各一份实现，
#: 外加共享的 history store（回退引擎自己写 ``revert`` 那条流水）。
RECORDING_SOURCES = {
    "sqlite_store": _REPOSITORIES_DIR / "sqlite" / "knowhow_store.py",
    "postgres_store": _REPOSITORIES_DIR / "postgres" / "knowhow_store.py",
    "history_store": _REPOSITORIES_DIR / "sqlite" / "knowhow_history_store.py",
}


def _record_change_kinds(path: Path) -> tuple[set[str], list[int]]:
    """``(字面量 kind 取值, 非字面量 kind 实参所在行号)``。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals: set[str] = set()
    dynamic: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if name != "record_change":
            continue
        for keyword in node.keywords:
            if keyword.arg != "kind":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literals.add(value.value)
            else:
                # 语义身份（表达式文本）而非行号——测试架构策略禁止把行号
                # 当断言身份载体，且表达式原文对定位更有信息量。
                dynamic.append(ast.unparse(value))
    return literals, dynamic


@pytest.mark.parametrize("label", sorted(RECORDING_SOURCES))
def test_every_recorded_kind_is_registered_for_the_asset_scan(label):
    literals, dynamic = _record_change_kinds(RECORDING_SOURCES[label])
    assert not dynamic, (
        f"{label}: record_change 的 kind 实参不是字面量（{dynamic}），"
        "AST 无法与 REGISTERED_KINDS 对账"
    )
    assert literals, f"{label}: 一个 record_change(kind=...) 都没扫到——守卫失效了"
    unregistered = sorted(literals - REGISTERED_KINDS)
    assert unregistered == [], (
        f"{label}: 这些 kind 会被写进 knowhow_changes，却没登记进 "
        "app/repositories/knowhow_asset_refs.py 的 REGISTERED_KINDS —— "
        "孤儿图片清扫器扫到它们时会抛 ValueError（后台任务里，功能测试看不见）："
        f"{unregistered}"
    )


def test_registered_kinds_are_all_actually_recorded_somewhere():
    """反向对账：登记表里不该有已经没人写的 kind。

    方向与上面相反、严格程度也不同：多登记一个不会让清扫器炸，只是让封闭集合
    慢慢变成一份过时清单，下一个作者读它时以为某个 kind 还活着。"""
    recorded: set[str] = set()
    for path in RECORDING_SOURCES.values():
        literals, _dynamic = _record_change_kinds(path)
        recorded |= literals
    stale = sorted(REGISTERED_KINDS - recorded)
    assert stale == [], f"REGISTERED_KINDS 里这些 kind 已经没有任何写入点：{stale}"


def test_every_registered_kind_is_routed_without_raising():
    """登记表与路由分支的对账：登记了却没有分支的 kind 只会静默返回 ``[]``。

    对没有 content_md 形状字段的 kind 来说 ``[]`` 是**正确**答案（列名、表标题
    里永远不会有图片引用），所以这里只断言「不抛」，不断言「非空」——真正的
    「新 kind 带正文却漏加分支」由评审与 spec §4.1 承担，AST 判不出来。"""
    for kind in sorted(REGISTERED_KINDS):
        assert content_strings_in_payload(kind, {}) == []


def test_unregistered_kind_still_raises():
    with pytest.raises(ValueError):
        content_strings_in_payload("brand_new_kind", {})
