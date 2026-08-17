"""守卫:`GroupStore` 的并发不变量必须落在写事务的 ``with`` 块**体内**。

判据形状照抄 `test_knowhow_history_coverage_guard.py`(同一条「检查必须在事务里」
的契约,同一套 AST 判据),两个后端各扫一遍——SQLite 与 PostgreSQL 是两份独立实现,
只在一侧加守卫等于给另一侧留后门。

钉三件事,每一件都**不报错、只静默走样**,所以只有静态判据拦得住:

1. **最后一名组管理员保护**(`upsert_member` / `remove_member`)。检查(`_admin_count_on`)
   与抛出(`LastGroupAdminError`)必须与写入同事务。挪到 ``with`` 块之前,两个并发的
   「把最后一个管理员降级」各自都会读到 `count == 1` 然后**都提交**,留下一个零管理员
   的组;而那个组的每一个管理端点都要求组管理员身份,除了系统管理员的运维旁路之外
   再没有别的恢复路径。挪出去之后代码照常工作、测试照常全绿——这正是要防的形态。

2. **群组存在性复核**(`upsert_member` / `remove_member` / `create_grant`)。路由层那次
   前置检查与写入之间,一个并发的删组请求可以整个跑完。复核在事务外 = SQLite 撞
   `group_members.group_id` 的外键抛 `IntegrityError`(用户拿到 500,正确答案是 404),
   PG 则是 `FOR UPDATE` 等完之后对着一行已经没有的记录继续往下走。

3. **授权边的组管理员复核**(`create_grant`)。这条边一落库就**立刻**给整组人读权,
   所以「发起者是不是这个组的管理员」必须与插入同事务;在路由层查、在 store 里插,
   中间那个窗口足够让发起者被移出或降级,而边照发不误。

⚠ 本守卫**只钉「在不在事务体内」**,不钉块内位置、也不钉具体写法。位置与措辞由代码
评审承担——按行号或按语句序钉住会在每次无害重排上误红。

不经 import 定位源文件:`app.repositories.postgres` 的导入会拖进 psycopg,而本守卫跑
在离线的 G1 泳道里,只需要读源码文本。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.repositories


_REPOSITORIES_DIR = Path(app.repositories.__file__).parent

#: 每个直接后端一份 ``GroupStore`` 实现,都要过同一条契约。
BACKEND_STORES = {
    "sqlite": _REPOSITORIES_DIR / "sqlite" / "group_store.py",
    "postgres": _REPOSITORIES_DIR / "postgres" / "group_store.py",
}

#: 方法 -> 它的写事务体内必须出现的**符号**集合(函数名 / 属性名 / 异常名皆可)。
#:
#: 用符号而不是行为断言,是因为这是**静态**守卫:它要在「有人把检查挪出 with 块」
#: 这一刻报红,而那种改动在运行时是全绿的(单线程测试观察不到竞态)。真并发行为由
#: `backend/tests/postgres/test_concurrency.py` 的 PG 用例承担,两者互补。
REQUIRED_IN_WRITE_TRANSACTION: dict[str, frozenset[str]] = {
    "upsert_member": frozenset({
        "_admin_count_on",        # 最后一名组管理员的计数
        "LastGroupAdminError",    # 以及它的抛出
        "_require_group_on|_lock_group_on",  # 群组存在性复核(两后端各自的拼写)
    }),
    "remove_member": frozenset({
        "_admin_count_on",
        "LastGroupAdminError",
        "_require_group_on|_lock_group_on",
    }),
    "create_grant": frozenset({
        "_require_group_on|_lock_group_on",   # 组还在
        "_role_on",                            # 发起者的组内角色
        "GroupAdminRequiredError",             # 以及它的抛出
    }),
}

#: 会开出新作用域的节点 —— 遍历时不下钻(嵌套函数里的调用不属于事务的执行流)。
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _walk_same_scope(node: ast.AST):
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
            continue  # 定义在事务里,但不属于事务的执行流
        yield from _walk_same_scope(stmt)


def _method_nodes(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GroupStore"
    )
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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


def _symbols(stmts) -> set[str]:
    """一段语句里出现的函数名 / 属性名 / 裸名字,不下钻进嵌套作用域。"""
    found: set[str] = set()
    for child in _walk_statements_same_scope(stmts):
        if isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.Name):
            found.add(child.id)
    return found


def _satisfied(required: str, present: set[str]) -> bool:
    """``"a|b"`` 表示两后端各自的拼写,任一命中即可;不含 ``|`` 就是精确要求。"""
    return any(alternative in present for alternative in required.split("|"))


def test_both_backend_stores_are_present():
    """两份实现都要被扫到——文件改名/搬家不能让某个后端悄悄脱离守卫。"""
    missing = sorted(name for name, path in BACKEND_STORES.items() if not path.is_file())
    assert missing == [], (
        f"这些后端的 GroupStore 源文件找不到了:{missing}。"
        "文件搬家后请更新 BACKEND_STORES,不要让某个后端脱离并发契约守卫。"
    )


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_guard_actually_sees_the_methods_it_claims_to_pin(backend: str):
    """守卫自检:被钉的方法都存在、且都真的开了写事务。

    少了这条,`upsert_member` 改名之后守卫会安静地什么都不检查而保持全绿——
    「加了守卫 ≠ 有效」的典型形态。
    """
    methods = _method_nodes(BACKEND_STORES[backend])
    missing = sorted(set(REQUIRED_IN_WRITE_TRANSACTION) - set(methods))
    assert missing == [], (
        f"{backend}:守卫登记的方法不存在了:{missing}。改名/删除时同步更新"
        " REQUIRED_IN_WRITE_TRANSACTION,不要让它静默失效。"
    )
    without_write = sorted(
        name for name in REQUIRED_IN_WRITE_TRANSACTION if not _write_transactions(methods[name])
    )
    assert without_write == [], (
        f"{backend}:这些方法不再开写事务:{without_write}。它们的不变量现在靠什么保证?"
    )


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_concurrency_invariants_live_inside_the_write_transaction(backend: str):
    """核心断言:每个不变量的符号必须出现在**某一个**写事务的 ``with`` 体内。

    「某一个」而不是「每一个」:一个方法将来可能合法地开出一个探测事务 + 一个变更
    事务。今天两个后端的每个被钉方法都恰好一个写事务,两种口径等价。
    """
    methods = _method_nodes(BACKEND_STORES[backend])
    offenders: list[str] = []
    for name, required in sorted(REQUIRED_IN_WRITE_TRANSACTION.items()):
        blocks = _write_transactions(methods[name])
        for symbol in sorted(required):
            if not any(_satisfied(symbol, _symbols(block.body)) for block in blocks):
                offenders.append(f"{name} 缺 {symbol}")
    assert offenders == [], (
        f"{backend} 后端 GroupStore 的这些并发不变量不在写事务体内:{offenders}。"
        "检查与写入必须同事务——挪到 with 块之外,两个并发请求会各自读到通过的"
        "条件然后都提交(最后一名组管理员被同时降级 / 组被并发删掉之后仍然插成员行)。"
        "这类退化在单线程测试里全绿,只有这条静态判据拦得住。"
    )
