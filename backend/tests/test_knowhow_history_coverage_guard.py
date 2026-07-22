"""守卫：KnowhowStore 里每一个写事务都必须记流水，除非显式豁免。

白名单是"允许不记"的封闭集合 —— 将来新增的写方法默认**报红**，逼着
作者显式决定它算不算用户可见变更。这正是 anchor 特性（PR#281→#286）
那次"宽容默认把 wire 错误降级成静默失败"的反面教训。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.repositories.sqlite import knowhow_store


#: 允许不记流水的方法 —— 它们不改用户可见内容。
EXEMPT_METHODS = frozenset({
    "bump_knowhow_mutation_seq",          # 纯投影调度计数器
    "set_knowhow_row_projection",          # 投影状态机
    "set_knowhow_row_projection_if_table_seq",
    "set_knowhow_hidden_source",           # 隐藏合成源接线
    "insert_notebook_asset",               # 资产表，不属于 knowhow 表内容
    "delete_source_asset_rows",
    "delete_knowhow_table",                # 表连同流水一起 CASCADE 消失
})


def _method_nodes() -> dict[str, ast.FunctionDef]:
    source = Path(inspect.getsourcefile(knowhow_store)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KnowhowStore"
    )
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _opens_write_transaction(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.With):
            continue
        for item in child.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "write"
            ):
                return True
    return False


def _calls_record_change(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "record_change":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "record_change":
                return True
    return False


def test_every_write_transaction_records_history_or_is_exempt():
    methods = _method_nodes()
    writers = {
        name: node for name, node in methods.items() if _opens_write_transaction(node)
    }

    assert writers, "没扫到任何写事务——守卫自身失效了，先修守卫"

    missing = sorted(
        name
        for name, node in writers.items()
        if name not in EXEMPT_METHODS and not _calls_record_change(node)
    )
    assert missing == [], (
        f"这些 KnowhowStore 写方法没有记变更流水：{missing}。"
        "要么在其写事务的最后调用 record_change，要么把它加进 EXEMPT_METHODS "
        "并在那里写清为什么它不算用户可见变更。"
    )


def test_exempt_list_has_no_stale_entries():
    """白名单里不能留下已经不存在、或已经不再开写事务的方法名。"""
    methods = _method_nodes()
    writers = {
        name for name, node in methods.items() if _opens_write_transaction(node)
    }
    stale = sorted(EXEMPT_METHODS - writers)
    assert stale == [], f"EXEMPT_METHODS 里这些条目已过时：{stale}"
