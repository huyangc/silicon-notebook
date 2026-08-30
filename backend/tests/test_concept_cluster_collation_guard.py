"""`concept_cluster_detail_rows` 的**源码结构**守卫（PostgreSQL 侧;R3·T-B2)。

hub 簇成员的 keyset 分页比较键与排序键必须写同一个 collation。今天
`0001_initial.sql` 把每个 id 列都声明成 `text COLLATE "C"`，所以裸
`cc.member_object_id > %s` 行为已经与 `COLLATE "C"` 逐字相同——**任何行为用例
都钉不住它**（在本仓库的 knowledge_harness 上做过变异验证：去掉 ORDER BY 的
那半 collation，backend/tests/postgres/test_knowledge_store_conformance.py 里
两条 concept_cluster_detail_rows 用例、以及本文件加的比较键变体全部仍然通过，
因为列级 collation 已经把序钉死）。而一旦哪天某个 id 列的列级 collation 变
了，两种顺序会让 keyset 分页开始漏成员——漏成员不报错，只表现为「翻页翻不
完」。所以这一条只能按源码钉，先例是
`tests/test_image_backfill_transaction_guard.py::
test_postgres_keyset_compares_on_the_same_collation_it_orders_by`。
"""
from __future__ import annotations

import ast
from pathlib import Path


ADAPTER = Path(__file__).resolve().parents[1] / "app/repositories/postgres/knowledge_store.py"


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
    return "".join(joined)


def test_postgres_concept_cluster_keyset_compares_on_the_same_collation_it_orders_by():
    """比较键与排序键的 collation 必须一致（PG 侧；SQLite 没有这个轴——它的
    TEXT 列默认就是 BINARY collation，逐字节比较，天然与两条键同序）。"""
    sql = _sql_text(_function(ADAPTER, "concept_cluster_detail_rows"))
    assert 'ORDER BY cc.member_object_id COLLATE "C"' in sql, (
        "守卫已陈旧：排序键不再是 cc.member_object_id COLLATE \"C\""
    )
    assert 'AND cc.member_object_id COLLATE "C" > %s' in sql, (
        "keyset 比较键漏了 COLLATE \"C\"——今天列级 collation 恰好也是 C，所以行为"
        "用例全绿，但列级 collation 一变，比较序与排序序分叉，翻页会静默漏成员"
    )
    assert "AND cc.member_object_id > %s" not in sql, "残留了不带 collation 的比较键"


def test_postgres_concept_cluster_member_total_shares_the_page_query_predicate():
    """member_total 的 COUNT 必须复用分页查询的谓词形（design review B8，硬约束）：
    JOIN knowledge_objects ... AND ko.status != 'deprecated'。裸
    `COUNT(*) FROM concept_clusters` 会把 deprecated 成员算进去——行为已在
    backend/tests/test_unified_kg_repository.py 与
    backend/tests/postgres/test_knowledge_store_conformance.py 的
    member_total 用例上做过变异验证（改回裸 COUNT，两侧用例均转红）。"""
    sql = _sql_text(_function(ADAPTER, "concept_cluster_member_total"))
    assert "JOIN knowledge_objects ko ON ko.id=cc.member_object_id" in sql, (
        "守卫已陈旧：member_total 不再 JOIN knowledge_objects"
    )
    assert "AND ko.status!='deprecated'" in sql, (
        "member_total 漏了 deprecated 过滤——会把 deprecated 成员算进总量，"
        "翻页显示「还有更多」但实际已经翻完"
    )
