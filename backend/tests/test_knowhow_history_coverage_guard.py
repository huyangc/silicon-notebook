"""守卫：KnowhowStore 里每一个写事务都必须记流水，除非显式豁免。

白名单是「允许不记」的封闭集合 —— 将来新增的写方法默认**报红**，逼着
作者显式决定它算不算用户可见变更。这正是 anchor 特性（PR#281→#286）
那次「宽容默认把 wire 错误降级成静默失败」的反面教训。

两个后端各扫一遍：SQLite 与 PostgreSQL 的 ``KnowhowStore`` 是两份独立实现，
共用同一条「每条写路径都要记流水」的契约，只在一侧加守卫等于给另一侧留后门。
两份白名单目前逐字相同，所以共用一份；将来若某后端合法地多出/少掉一个豁免项，
把它拆成 per-backend 映射，而不是把守卫放宽。

流水必须记在写事务的 ``with`` 块**体内**——数据提交与流水追加同事务才有
原子性，块外追加就是两个事务。所以判据是「写事务体内有没有 ``record_change``」，
不是「方法体内有没有」：后者会让「把调用挪出 with 块」这种退化保持全绿。

刻意不检查 ``record_change`` 是否是写事务的**最后一步**：那个位置断言在
AST 上只能按「with 体最后一条语句」近似，遇到尾随的 return/计数器 bump 就会
误红。块内位置由代码评审和 `architecture.md` §3.7 的契约条目承担。

一个方法开多个写事务时，只要求**其中一个**记流水。当前两个后端都没有这种
方法（写事务方法各自恰好一个 ``with``），改成「每个都要记」在今天等价，但会
给将来「一个探测事务 + 一个变更事务」的合法写法留下无处可逃的误红——真出现
多写事务方法时应重新评估这一档，而不是当它不存在。

不经 import 定位源文件：``app.repositories.postgres`` 的导入会拖进 psycopg，
而本守卫跑在离线的 G1 泳道里，只需要读源码文本。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.repositories


_REPOSITORIES_DIR = Path(app.repositories.__file__).parent

#: 每个直接后端一份 ``KnowhowStore`` 实现，都要过同一条契约。
BACKEND_STORES = {
    "sqlite": _REPOSITORIES_DIR / "sqlite" / "knowhow_store.py",
    "postgres": _REPOSITORIES_DIR / "postgres" / "knowhow_store.py",
}

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


def _method_nodes(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KnowhowStore"
    )
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


#: 会开出新作用域的节点 —— 遍历时不下钻，见 ``_walk_same_scope``。
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _walk_same_scope(node: ast.AST):
    """像 ``ast.walk``，但不进入 ``node`` 之下任何嵌套函数/类/lambda 的体内。

    ``ast.walk`` 会下钻进嵌套作用域，于是写事务里一个**定义了却从未调用**的
    局部 helper（``def _log(): record_change(...)``）就足以骗过守卫，而那个
    事务其实一条流水都没记。同理，嵌套函数里的 ``with ...write()`` 也不该让
    外层方法算作写方法。判据要落在事务自己的执行流上。

    ``node`` 自身定义的作用域**要**遍历（否则传入方法节点就什么都扫不到）；
    调用方若要把一批语句当同一作用域扫，必须走 ``_walk_statements_same_scope``，
    它会先把「语句本身就是个嵌套作用域定义」这种情况挡掉——直接对那种语句调用
    本函数会把它当根，反而把嵌套体遍历个遍。
    """
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _NESTED_SCOPES):
                continue
            pending.append(child)


def _walk_statements_same_scope(stmts):
    for stmt in stmts:
        if isinstance(stmt, _NESTED_SCOPES):
            continue  # 定义在事务里，但不属于事务的执行流
        yield from _walk_same_scope(stmt)


def _write_transactions(node: ast.AST) -> list[ast.With]:
    blocks: list[ast.With] = []
    for child in _walk_same_scope(node):
        if not isinstance(child, ast.With):
            continue
        for item in child.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "write"
            ):
                blocks.append(child)
                break
    return blocks


def _calls_record_change(stmts) -> bool:
    for child in _walk_statements_same_scope(stmts):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "record_change":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "record_change":
                return True
    return False


def _records_inside_a_write_transaction(node: ast.AST) -> bool:
    """流水必须记在写事务**体内**，扫整个方法体是不够的。

    数据提交与流水追加同事务才有原子性。若只在方法级找 `record_change`，
    把它挪到 `with` 块之后（或塞进一个不相干的分支/嵌套函数）仍然全绿，
    而这恰是守卫要防的退化。所以只在检出的写事务体内找。

    注意这仍**不是**「最后一步」断言——块内位置不做要求，见模块 docstring。
    """
    return any(
        _calls_record_change(block.body)
        for block in _write_transactions(node)
    )


def _writers(path: Path) -> dict[str, ast.FunctionDef]:
    return {
        name: node
        for name, node in _method_nodes(path).items()
        if _write_transactions(node)
    }


def test_both_backend_stores_are_present():
    """两份实现都要被扫到——文件改名/搬家不能让某个后端悄悄脱离守卫。"""
    missing = sorted(name for name, path in BACKEND_STORES.items() if not path.is_file())
    assert missing == [], (
        f"这些后端的 KnowhowStore 源文件找不到了：{missing}。"
        "文件搬家后请更新 BACKEND_STORES，不要让某个后端脱离流水覆盖守卫。"
    )


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_every_write_transaction_records_history_or_is_exempt(backend: str):
    writers = _writers(BACKEND_STORES[backend])

    assert writers, f"{backend}：没扫到任何写事务——守卫自身失效了，先修守卫"

    missing = sorted(
        name
        for name, node in writers.items()
        if name not in EXEMPT_METHODS and not _records_inside_a_write_transaction(node)
    )
    assert missing == [], (
        f"{backend} 后端这些 KnowhowStore 写方法没有在写事务内记变更流水：{missing}。"
        "要么在其写事务的最后调用 record_change（必须在 with 块**体内**，"
        "块外追加流水就丢了原子性），要么把它加进 EXEMPT_METHODS "
        "并在那里写清为什么它不算用户可见变更。"
    )


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_exempt_list_has_no_stale_entries(backend: str):
    """白名单里不能留下已经不存在、或已经不再开写事务的方法名。"""
    stale = sorted(EXEMPT_METHODS - set(_writers(BACKEND_STORES[backend])))
    assert stale == [], f"{backend} 后端 EXEMPT_METHODS 里这些条目已过时：{stale}"
