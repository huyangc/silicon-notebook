"""`apply_image_backfill` 的**单写事务**语义守卫（双后端）。

v46 红线：每条活库够得着的 chunk 写路径都必须在**同一写事务**内维护
`chunk_elements` 反查行；元素换代还必须同事务推进 `sources.updated_at`。

行为测试只能证明这些行"最后确实在库里"——把某一半挪到第二个写事务里，它们
照样都在，测试全绿（实测：把反查行插入移出 `with` 再另开一个事务，21 条行为
用例一条都不红）。原子性只有从**源码结构**上才判得出来，所以这条守卫按 AST
钉：函数体里恰好一个 `with ... .write()`，且四条写语句全部落在它体内。

先例是 `test_knowhow_history_coverage_guard.py`（同样按"落在写事务 with 块**体
内**"判，同样不钉它在块内的位置）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ADAPTERS = {
    "sqlite": Path(__file__).resolve().parents[1]
    / "app/repositories/sqlite/maintenance.py",
    "postgres": Path(__file__).resolve().parents[1]
    / "app/repositories/postgres/maintenance.py",
}

#: 必须落在同一个写事务里的四条写。按 SQL 片段判而不是按行号——占位符方言不同，
#: 但语句形状两侧逐字对等。
REQUIRED_WRITES = (
    "INTO source_elements",  # SQLite/PG 的 INSERT 前缀不同（OR IGNORE / ON CONFLICT）
    "UPDATE chunks SET element_ids",
    "INTO chunk_elements",
    "UPDATE sources SET updated_at",
)


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name}: {name} not found")


def _sql_text(node: ast.AST) -> str:
    """把相邻字符串拼接还原成完整语句，供片段匹配。"""
    joined: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            joined.append(child.value)
        elif isinstance(child, ast.JoinedStr):  # pragma: no cover - 本函数不用 f-string
            joined.append("<fstring>")
    return "".join(joined)


@pytest.mark.parametrize("backend", sorted(ADAPTERS))
def test_apply_image_backfill_uses_exactly_one_write_transaction(backend):
    function = _function(ADAPTERS[backend], "apply_image_backfill")
    with_nodes = [node for node in ast.walk(function) if isinstance(node, ast.With)]
    assert len(with_nodes) == 1, (
        f"{backend}: apply_image_backfill 必须恰好一个写事务；"
        f"发现 {len(with_nodes)} 个 with 块——第二个事务会让反查行/updated_at 与"
        "它们描述的 chunk 写入不再原子提交"
    )
    context = with_nodes[0].items[0].context_expr
    assert (
        isinstance(context, ast.Call)
        and isinstance(context.func, ast.Attribute)
        and context.func.attr == "write"
    ), f"{backend}: 那个 with 必须是 `database.write()` 写事务本身"


@pytest.mark.parametrize("backend", sorted(ADAPTERS))
@pytest.mark.parametrize("statement", REQUIRED_WRITES)
def test_every_write_lands_inside_that_transaction(backend, statement):
    function = _function(ADAPTERS[backend], "apply_image_backfill")
    body_sql = "".join(
        _sql_text(statement_node)
        for with_node in ast.walk(function)
        if isinstance(with_node, ast.With)
        for statement_node in with_node.body
    )
    whole_sql = _sql_text(function)
    assert statement in whole_sql, f"{backend}: 找不到 {statement!r}（守卫已陈旧）"
    assert statement in body_sql, (
        f"{backend}: {statement!r} 不在写事务体内——它必须与同一批 chunk 写入原子提交"
    )
