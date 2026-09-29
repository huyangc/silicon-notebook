"""Memory 来源可读性与「派生自 Memory」判定的 SQL 片段 —— 两条判据的唯一定义点。

镜像 `access_sql.py` / `mount_sql.py` 的模式,理由同款:Memory 是创建者私有的,而它
派生出的来源(`sources.source_type='memory'`,`sources.memory_id` → `memory_items.id`)、
知识对象、关系与概念簇会流进检索、KG 与来源读取的各个读者。若每个读者各写一份
「这条 Memory 派生行能不能给这个人看」的手写谓词,任何一份漂移都不会有测试自然抓到。
故判据只在这里定义一次,读者一律 import。`postgres/memory_sql.py` 是它的 PostgreSQL 镜像
(占位符 `%s`),两份必须同修。

## 两类判据,两种形状

**读者按查看者判定**(需要一个参数 = 查看者 id):

* `memory_source_readable(source_alias)` —— 这条**来源**对查看者可读吗。非 Memory 来源
  (Knowhow、普通来源)一律可读;Memory 来源只有 `memory_items.created_by` 等于查看者时
  可读。`memory_id` 为空、或指向不存在的 `memory_items` 行(孤儿)的 Memory 来源
  **对所有人失败即关**;空查看者(`''` / 无 actor 的后台路径)读不到任何 Memory 来源。
  恰好一个参数。
* `foreign_memory_object_excluded(object_alias)` /
  `foreign_memory_relation_excluded(relation_alias)` —— 这条知识对象 / 关系的**主**
  `source_id` **不**指向别人的 Memory 来源(排除形式)。没有归属来源的行、归属普通来源
  的行照旧保留。恰好一个参数(查看者)。

**建图 / 拷贝按「派生自 Memory」分类**(零参数,与查看者无关):

* `memory_derived_object(object_alias)` / `memory_derived_relation(relation_alias)` ——
  知识对象 / 关系派生自 Memory ⇔ 其主 `source_id` 指向 `source_type='memory'` 的来源。
  依据:`store_kg` 每个对象都新铸 id、带 `source_generation` 时拒收其他来源的证据,关系
  补全与重链只在单一来源内,所以「主来源」就是派生关系的全部真值。
* `no_memory_member_cluster(cluster_alias)` —— 整个概念簇里没有任何成员派生自 Memory
  (`concept_clusters.canonical_name` 是代表名整簇复制,代表可能选自私有 Memory 派生的
  对象,只过滤成员行洗不掉名字,所以取名字的查询按整簇排除)。零参数。

## 为什么是排除形式、而不绑「可读全集」数组

可读判定一律写成对 `memory_items` 的相关 EXISTS 子查询,只绑一个标量(查看者),绝不把
「查看者可读的来源 id 全集」作为数组参数绑进语句:PG16 实测,绑可读全集数组每条语句约
20 ms 规划,同一连接约 10 次后切通用计划会慢 5–20 倍;排除形式对「库里没有别人的
Memory」的常见情形没有任何额外成本(调用方在无外人 Memory 时根本不拼这些片段)。

关联子查询都落在主键点查上:`memory_items.id`、`sources.id` 均为主键。Memory 存放在
`memory_items` 而不是让来源表冗余一份属主列,是为了让「谁创建」只有一个真源——`sources`
上没有第二份可以漂移的属主。

## 约定

* 每个片段都是**语句内**的相关子查询(codex #520 R2 P1:排除必须与被排除的行由同一次
  求值决定,跨查询相减/排除清单会被并发的 Memory 增删漏掉)。
* 外层别名由调用方传入;内层别名(`rm` / `fs` / `fm` / `ds` / `mc` / `mo` / `ms`)固定,
  传入与之相同的外层别名会把相关引用绑到内层表上、静默改变语义,所以 `_alias` 直接拒绝。
* 参数个数固定,由 `test_memory_sql_contract.py` 断言:readable / foreign 各 1 个
  (`?`),derived / cluster 各 0 个。新增读者时消费的参数个数不许因别名而变。
* Knowhow 不在本模块的判据内:Knowhow 投影是笔记本级共享的,M1 只针对 Memory。
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
        f"WHERE rm.id = {a}.memory_id AND rm.created_by = ?))"
    )


def _foreign_memory_excluded(alias: str) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM sources fs "
        f"WHERE fs.id = {alias}.source_id AND fs.source_type = 'memory' "
        "AND NOT EXISTS (SELECT 1 FROM memory_items fm "
        "WHERE fm.id = fs.memory_id AND fm.created_by = ?))"
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
    内层引用与外层行同代(外层已按 published 谓词过滤,相关引用零新参数)——「按整簇
    排除 memory」只在同一代内判定,双代窗口不跨代误判。
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
