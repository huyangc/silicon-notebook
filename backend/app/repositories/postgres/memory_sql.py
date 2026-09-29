"""Canonical PostgreSQL Memory SQL fragments.

`sqlite/memory_sql.py` 的镜像(占位符 `%s`)。两类判据(读者按查看者判定 / 建图与拷贝按
「派生自 Memory」分类)、排除形式而不绑可读全集数组的理由(PG16 实测规划开销)、外层别名
约定、固定参数个数契约,全部写在 SQLite 那一份的模块 docstring 里,两份必须同修;这里
只登记 PG 侧独有的事实:

* 语句在 READ COMMITTED 下执行,两次读之间没有共享快照,所以排除必须与被排除的行在
  同一条语句里求值(这也是为什么这些片段都是相关子查询而不是先读 id 清单再相减)。
* `memory_items.created_by` / `sources.memory_id` 等列均为 `COLLATE "C"` 的 text,查看者
  参数按同一排序规则比较,与 SQLite 的字节序比较同义。
"""

from __future__ import annotations

import re

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 各片段内层子查询占用的别名;外层别名与之相同即绑错表。
_READABLE_INNER = frozenset({"rm"})
_FOREIGN_INNER = frozenset({"fs", "fm"})
_DERIVED_INNER = frozenset({"ds"})
_CLUSTER_INNER = frozenset({"mc", "mo", "ms"})


def _alias(name: str, reserved: frozenset[str]) -> str:
    if not _IDENT.match(name) or name in reserved:
        raise ValueError(f"unusable outer alias for a Memory SQL fragment: {name!r}")
    return name


def memory_source_readable(source_alias: str) -> str:
    """来源 `source_alias` 对查看者可读。恰好一个参数:查看者 id。"""
    a = _alias(source_alias, _READABLE_INNER)
    return (
        f"({a}.source_type <> 'memory' OR EXISTS ("
        "SELECT 1 FROM memory_items rm "
        f"WHERE rm.id = {a}.memory_id AND rm.created_by = %s))"
    )


def _foreign_memory_excluded(alias: str) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM sources fs "
        f"WHERE fs.id = {alias}.source_id AND fs.source_type = 'memory' "
        "AND NOT EXISTS (SELECT 1 FROM memory_items fm "
        "WHERE fm.id = fs.memory_id AND fm.created_by = %s))"
    )


def foreign_memory_object_excluded(object_alias: str) -> str:
    """知识对象 `object_alias` 的主来源不是别人的 Memory 来源。一个参数:查看者 id。"""
    return _foreign_memory_excluded(_alias(object_alias, _FOREIGN_INNER))


def foreign_memory_relation_excluded(relation_alias: str) -> str:
    """知识关系 `relation_alias` 的主来源不是别人的 Memory 来源。一个参数:查看者 id。"""
    return _foreign_memory_excluded(_alias(relation_alias, _FOREIGN_INNER))


def _memory_derived(alias: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM sources ds "
        f"WHERE ds.id = {alias}.source_id AND ds.source_type = 'memory')"
    )


def memory_derived_object(object_alias: str) -> str:
    """知识对象 `object_alias` 的主来源是 Memory 来源。零参数。"""
    return _memory_derived(_alias(object_alias, _DERIVED_INNER))


def memory_derived_relation(relation_alias: str) -> str:
    """知识关系 `relation_alias` 的主来源是 Memory 来源。零参数。"""
    return _memory_derived(_alias(relation_alias, _DERIVED_INNER))


def no_memory_member_cluster(cluster_alias: str) -> str:
    """概念簇 `cluster_alias`(取其 `notebook_id`/`canonical_id`/`generation`)整簇没有
    Memory 派生成员。零参数。

    `source_type` 不加限定词:三张 join 表里只有 sources(ms) 有这列,只能解析到它。
    内层引用与外层行同代(外层已按 published 谓词过滤,相关引用零新参数)。
    """
    c = _alias(cluster_alias, _CLUSTER_INNER)
    return (
        "NOT EXISTS (SELECT 1 FROM concept_clusters mc "
        "JOIN knowledge_objects mo ON mo.id = mc.member_object_id "
        "JOIN sources ms ON ms.id = mo.source_id "
        f"WHERE mc.notebook_id = {c}.notebook_id "
        f"AND mc.canonical_id = {c}.canonical_id "
        f"AND mc.generation = {c}.generation "
        "AND source_type = 'memory')"
    )
