"""`backfill-images` 仓储半的**源码结构**守卫（双后端）。

两条：`apply_image_backfill` 的单写事务语义，以及候选分页 SQL 的 keyset 形状。
两者的共同点是**行为测试证明不了**——把某条写挪进第二个事务，行照样都在；
把比较键上的 `COLLATE "C"` 去掉，今天的 schema 下行为逐字相同。

## 一、单写事务

v46 红线：每条活库够得着的 chunk 写路径都必须在**同一写事务**内维护
`chunk_elements` 反查行；元素换代还必须同事务推进 `sources.updated_at`。

行为测试只能证明这些行"最后确实在库里"——把某一半挪到第二个写事务里，它们
照样都在，测试全绿（实测：把反查行插入移出 `with` 再另开一个事务，21 条行为
用例一条都不红）。原子性只有从**源码结构**上才判得出来，所以这条守卫按 AST
钉：函数体里恰好一个 `with ... .write()`，且四条写语句全部落在它体内。

先例是 `test_knowhow_history_coverage_guard.py`（同样按"落在写事务 with 块**体
内**"判，同样不钉它在块内的位置）。

## 二、keyset 比较键的 collation

`image_backfill_source_page` 的 `ORDER BY` 是 `id COLLATE "C"`，比较键必须写成
同一个 collation。今天 `0001_initial.sql` 把每个 id 列都声明成 `text COLLATE
"C"`，所以裸 `id > %s` 行为相同、**任何行为用例都钉不住它**（已在真 PG 上做过
变异验证：去掉那半 collation，7 条 PG 用例全绿）。而一旦哪天某个 id 列的列级
collation 变了，两种顺序会让 keyset 分页开始漏源——漏源不报错，只表现为"这批
图没补上"。所以这一条只能按源码钉。
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

#: 必须落在同一个写事务里的语句。按 SQL 片段判而不是按行号——占位符方言不同，
#: 但语句形状两侧逐字对等。
#:
#: 前两条是 **CAS 读**：它们证明"计划快照到此刻仍然成立"，所以必须与它们授权的那
#: 批写在**同一个**事务里。挪到事务外（哪怕只早一个语句）就退化成一次 TOCTOU 检查
#: ——并发重解析恰好落在读与写之间时，检查通过而写下去的仍是脏数据。
REQUIRED_WRITES = (
    # 元素代次信号：COUNT + MAX(created_at) 一次读回。
    "MAX(created_at) AS newest",
    # 每个目标 chunk 的现值，与计划快照里的旧 element_ids 比对。
    "SELECT element_ids FROM chunks WHERE id=",
    "INTO source_elements",  # SQLite/PG 的 INSERT 前缀不同（OR IGNORE / ON CONFLICT）
    # 就地补齐（`image_backfill.EnrichedImage`）：它描述的资产行已经写进
    # `notebook_assets` 了，落在第二个事务里就会留下"资产在、元素还指不到它"的
    # 中间态，而回滚只删得掉本次写的资产、删不掉一次已提交的半程。
    "UPDATE source_elements SET metadata",
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


def test_postgres_keyset_compares_on_the_same_collation_it_orders_by():
    """比较键与排序键的 collation 必须一致（PG 侧；SQLite 没有这个轴）。"""
    sql = _sql_text(_function(ADAPTERS["postgres"], "image_backfill_source_page"))
    assert 'ORDER BY id COLLATE "C"' in sql, "守卫已陈旧：排序键不再是 id COLLATE \"C\""
    assert 'AND id COLLATE "C" > %s' in sql, (
        "keyset 比较键漏了 COLLATE \"C\"——今天列级 collation 恰好也是 C，所以行为"
        "用例全绿，但列级 collation 一变，比较序与排序序分叉，分页会静默漏源"
    )
    assert "AND id > %s" not in sql, "残留了不带 collation 的比较键"
