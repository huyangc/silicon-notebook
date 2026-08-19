"""守卫:`AgentProfileStore` 的历史追加与它的写入必须同事务(``with ... .write()``)。

判据形状照抄 `test_group_store_transaction_guard.py`(它自己又照抄
`test_knowhow_history_coverage_guard.py`),两个后端各扫一遍——SQLite 与 PostgreSQL
是两份独立实现,只在一侧加守卫等于给另一侧留后门。

**钉什么**:`write_block` / `clear_block` 里,历史环的追加(`_append_history`)与它写
回去的那条 SQL(`agent_notebook_profile`)必须落在写事务的 ``with`` 块**体内**。

**为什么只有静态判据拦得住**:把 `_append_history` 挪到 ``with`` 块**之前**,代码
照常工作、现有每一条用例照常全绿——单线程测试观察不到任何差别。真正的后果是
`history_json` 与 `value`/`revision` 不再原子:块外算好的历史是按**读那一刻**的
`history_json` 拼的,而事务内的 CAS 允许在这之间落进另一次写入(SQLite 侧靠
`begin_immediate` 对跨进程写者也成立,PG 侧靠 `FOR UPDATE`)。结果是一条 before/after
记录指向一个已经不存在的 before,或者干脆覆盖掉别人刚追加的那一条——历史面板上
少一次编辑,而没有任何报错。这与 knowhow 变更历史那条红线是同一件事:「写事务的
最后一步追加流水」,`record_change` 挪出块外就丢原子性。

⚠ 本守卫**只钉「在不在事务体内」**,不钉块内位置、也不钉具体写法。位置(「最后一步」)
与措辞由代码评审承担——按行号或按语句序钉住会在每次无害重排上误红。

不经 import 定位源文件:`app.repositories.postgres` 的导入会拖进 psycopg,而本守卫跑
在离线的 G1 泳道里,只需要读源码文本。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.repositories


_REPOSITORIES_DIR = Path(app.repositories.__file__).parent

#: 每个直接后端一份 ``AgentProfileStore`` 实现,都要过同一条契约。
BACKEND_STORES = {
    "sqlite": _REPOSITORIES_DIR / "sqlite" / "agent_profile_store.py",
    "postgres": _REPOSITORIES_DIR / "postgres" / "agent_profile_store.py",
}

#: 方法 -> 它的写事务体内必须出现的**符号**集合(函数名 / 属性名皆可)。
#:
#: `_append_history` 是两侧共用的那个模块级别名(两边都
#: ``from app.repositories.ports import append_profile_history as _append_history``);
#: 用别名而不是原名,是因为守卫钉的是「调用点在不在事务里」,而调用点写的就是别名。
REQUIRED_IN_WRITE_TRANSACTION: dict[str, frozenset[str]] = {
    "write_block": frozenset({"_append_history"}),
    "clear_block": frozenset({"_append_history"}),
}

#: 只出现在下面那张 SQL 表里、不需要符号断言的方法。两张表的并集才是「被钉住的
#: 方法集合」,自检按并集算——否则只登记在 SQL 表里的方法会在改名后静默脱钩。
_SQL_ONLY_METHODS = frozenset({"settle"})

#: 方法 -> 它的写事务体内必须出现的 **SQL 片段**,按 **后端** 键控。
#:
#: 与上面那张表互补:符号集合证明「历史是在事务里算的」,这张表证明「写回历史的那条
#: 语句也在同一个事务里」。少了它,把 SQL 挪出去而把纯算术留在里面同样能骗过守卫。
#:
#: 按后端键控(而不是两侧共用一张)是因为两个后端的并发原语本来就不同:PostgreSQL
#: 侧的 ``write_block`` 额外必须钉住 ``FOR SHARE``——它是这次生成检查的行锁,SQLite
#: 侧没有对应片段(那边靠 ``begin_immediate`` 的进程级写序列化,同样的校验不需要、
#: 也不会出现任何行锁 SQL)。两侧共用一张表会让 SQLite 的字符串常量集合里去凑一个
#: 永远不存在的 ``FOR SHARE``,或者反过来让 PostgreSQL 那半的行锁悄悄脱离静态判据。
REQUIRED_SQL_IN_WRITE_TRANSACTION: dict[str, dict[str, frozenset[str]]] = {
    "sqlite": {
        # ``claim_token``:P2 的 claim 代际校验必须与它放行的那次写入同事务。挪到
        # ``with`` 块之前,单线程测试同样全绿——而真正的后果是校验退回「尽力而为」:
        # 成员被移出(``clear_job_row``)可以落在校验与写入之间,被移出的成员的私有块
        # 就这样被一个已经作废的 run 重建出来。P1 的写前 ``job_row`` 探针正是这个
        # 形状,R4 登记的就是它。
        "write_block": frozenset({"agent_notebook_profile", "claim_token"}),
        "clear_block": frozenset({"agent_notebook_profile"}),
        # ``settle`` 的 CAS 没落地时,「为什么」必须在**同一个**事务里读出来:出了
        # 事务,一次并发移除就能把 ``superseded``(新一代持有链路,别动它的块)读成
        # ``gone``(行没了,抹掉这些块)——把「放手」翻成「删除」。两条片段分别钉住
        # 带代际的 CAS 与那次判别读。
        "settle": frozenset({
            "AND claim_token=",
            "SELECT 1 FROM agent_profile_jobs",
        }),
    },
    "postgres": {
        # 质量评审 P2-2:``FOR SHARE`` 是这个后端让 claim 代际校验与 SQLite 的
        # ``begin_immediate`` 一样原子的那个锁——不锁住这一行,一次并发的
        # ``clear_job_row``(成员移出)就能落在校验读与后续写之间,把生成检查
        # 变回「尽力而为」。少了它,单线程测试照样全绿,只有这条静态判据拦得住。
        "write_block": frozenset(
            {"agent_notebook_profile", "claim_token", "FOR SHARE"}
        ),
        "clear_block": frozenset({"agent_notebook_profile"}),
        "settle": frozenset({
            "AND claim_token=",
            "SELECT 1 FROM agent_profile_jobs",
        }),
    },
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
        if isinstance(node, ast.ClassDef) and node.name == "AgentProfileStore"
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


def _sql_fragments(stmts) -> set[str]:
    """一段语句里出现的字符串常量,不下钻进嵌套作用域。"""
    return {
        child.value
        for child in _walk_statements_same_scope(stmts)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_both_backend_stores_are_present():
    """两份实现都要被扫到——文件改名/搬家不能让某个后端悄悄脱离守卫。"""
    missing = sorted(name for name, path in BACKEND_STORES.items() if not path.is_file())
    assert missing == [], (
        f"这些后端的 AgentProfileStore 源文件找不到了:{missing}。"
        "文件搬家后请更新 BACKEND_STORES,不要让某个后端脱离历史原子性守卫。"
    )


def test_the_sql_fragment_table_is_keyed_by_every_backend():
    """`REQUIRED_SQL_IN_WRITE_TRANSACTION` 按后端键控之后,少注册一个后端不能
    悄悄让它退回「共用一张表」——那正是 PG 专属的 ``FOR SHARE`` 片段可能被
    误套到 SQLite 头上、或整个从守卫里消失的形态。"""
    assert set(REQUIRED_SQL_IN_WRITE_TRANSACTION) == set(BACKEND_STORES)


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_guard_actually_sees_the_methods_it_claims_to_pin(backend: str):
    """守卫自检:被钉的方法都存在、且都真的开了写事务。

    少了这条,`write_block` 改名之后守卫会安静地什么都不检查而保持全绿——
    「加了守卫 ≠ 有效」的典型形态。
    """
    methods = _method_nodes(BACKEND_STORES[backend])
    pinned = (
        set(REQUIRED_IN_WRITE_TRANSACTION)
        | set(REQUIRED_SQL_IN_WRITE_TRANSACTION[backend])
        | set(_SQL_ONLY_METHODS)
    )
    missing = sorted(pinned - set(methods))
    assert missing == [], (
        f"{backend}:守卫登记的方法不存在了:{missing}。改名/删除时同步更新"
        " REQUIRED_IN_WRITE_TRANSACTION / REQUIRED_SQL_IN_WRITE_TRANSACTION,"
        "不要让它静默失效。"
    )
    without_write = sorted(
        name for name in pinned if not _write_transactions(methods[name])
    )
    assert without_write == [], (
        f"{backend}:这些方法不再开写事务:{without_write}。历史与值的原子性现在靠什么保证?"
    )


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_history_append_lives_inside_the_write_transaction(backend: str):
    """核心断言:历史环的追加必须出现在**某一个**写事务的 ``with`` 体内。

    「某一个」而不是「每一个」:一个方法将来可能合法地开出一个探测事务 + 一个变更
    事务。今天两个后端的每个被钉方法都恰好一个写事务,两种口径等价。
    """
    methods = _method_nodes(BACKEND_STORES[backend])
    offenders: list[str] = []
    for name, required in sorted(REQUIRED_IN_WRITE_TRANSACTION.items()):
        blocks = _write_transactions(methods[name])
        for symbol in sorted(required):
            if not any(symbol in _symbols(block.body) for block in blocks):
                offenders.append(f"{name} 缺 {symbol}")
    assert offenders == [], (
        f"{backend} 后端 AgentProfileStore 的历史追加不在写事务体内:{offenders}。"
        "历史与 value/revision 必须同事务——挪到 with 块之外,块外算好的历史是按"
        "「读那一刻」的 history_json 拼的,而事务内的 CAS 允许另一次写入落在中间:"
        "一条 before/after 会指向一个已经不存在的 before,或者覆盖掉别人刚追加的那条。"
        "这类退化在单线程测试里全绿,只有这条静态判据拦得住。"
    )


@pytest.mark.parametrize("backend", sorted(BACKEND_STORES))
def test_history_write_sql_lives_inside_the_write_transaction(backend: str):
    """核心断言之二:把历史写回去的那条 SQL 也必须在同一个事务里。

    上面那条只证明「历史是在事务里算的」;把 SQL 挪出去、纯算术留在里面同样能骗过
    它,而那正是丢原子性的形态。
    """
    methods = _method_nodes(BACKEND_STORES[backend])
    offenders: list[str] = []
    for name, required in sorted(REQUIRED_SQL_IN_WRITE_TRANSACTION[backend].items()):
        blocks = _write_transactions(methods[name])
        for fragment in sorted(required):
            found = any(
                any(fragment in text for text in _sql_fragments(block.body))
                for block in blocks
            )
            if not found:
                offenders.append(f"{name} 缺写 {fragment} 的语句")
    assert offenders == [], (
        f"{backend} 后端 AgentProfileStore 的块写入 SQL 不在写事务体内:{offenders}。"
    )
