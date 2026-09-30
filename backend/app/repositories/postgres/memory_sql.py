"""Canonical PostgreSQL Memory SQL fragments.

`sqlite/memory_sql.py` 的镜像(占位符 `%s`)。两类判据(读者按查看者判定 / 建图与拷贝按
「派生自 Memory」分类)、排除形式而不绑可读全集数组的理由(PG16 实测规划开销)、外层别名
约定(按小写比较、整串 `fullmatch`)、固定参数契约(每个片段在语句文本中它所在的位置消费
固定个数的 `%s`)、以及「现状:哪些既有读者仍自带手写谓词」,全部写在 SQLite 那一份的模块
docstring 里,两份必须同修;这里只登记 PG 侧独有的事实:

* 语句在 READ COMMITTED 下执行,两次读之间没有共享快照,所以排除必须与被排除的行在
  同一条语句里求值(这也是为什么这些片段都是相关子查询而不是先读 id 清单再相减)。
* `memory_items.created_by` / `sources.memory_id` 等列均为 `COLLATE "C"` 的 text,查看者
  参数按同一排序规则比较,与 SQLite 的字节序比较同义。
"""

from __future__ import annotations

import re

#: Memory 派生来源的 `sources.source_type` 取值 —— 本模块里唯一的 `'memory'` 字面量。
MEMORY_SOURCE_TYPE = "memory"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 各片段内层子查询占用的别名(小写);外层别名与之相同(不分大小写)即绑错表。
_READABLE_INNER = frozenset({"rm"})
_FOREIGN_INNER = frozenset({"fs", "fm"})
_DERIVED_INNER = frozenset({"ds"})
_CLUSTER_INNER = frozenset({"mc", "mo", "ms"})
_CANONICAL_INNER = frozenset({"mk", "mks"})


def _alias(name: str, reserved: frozenset[str]) -> str:
    if not _IDENT.fullmatch(name) or name.lower() in reserved:
        raise ValueError(f"unusable outer alias for a Memory SQL fragment: {name!r}")
    return name


def memory_source_type_predicate(column: str = "source_type") -> str:
    """`{column} = 'memory'`。不带参数时渲染出与 `source_store.MEMORY_SOURCE_TYPE_PREDICATE`
    相同的文本(未加限定词的 `source_type` 列)。"""
    return f"{column} = '{MEMORY_SOURCE_TYPE}'"


def memory_source_readable(source_alias: str) -> str:
    """来源 `source_alias` 对查看者可读。恰好一个参数:查看者 id。

    前提:`memory_items.created_by` 永不为空串——它是 NOT NULL 且外键指向
    `users(id)`,用户 id 由系统生成,不存在 id 为 `''` 的用户,所有写入 Memory 的
    路径都按真实用户建行。所以查看者传 `''`(无 actor 的后台路径、没有 `memory:read`
    的 Agent 令牌)时本片段对任何 Memory 来源都为假;`foreign_memory_*` 两个片段依赖
    同一前提(查看者 `''` 时所有 Memory 派生行都算「别人的」)。
    """
    a = _alias(source_alias, _READABLE_INNER)
    return (
        f"({a}.source_type <> '{MEMORY_SOURCE_TYPE}' OR EXISTS ("
        "SELECT 1 FROM memory_items rm "
        f"WHERE rm.id = {a}.memory_id AND rm.created_by = %s))"
    )


def _foreign_memory_excluded(alias: str) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM sources fs "
        f"WHERE fs.id = {alias}.source_id AND {memory_source_type_predicate('fs.source_type')} "
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
        f"WHERE ds.id = {alias}.source_id AND {memory_source_type_predicate('ds.source_type')})"
    )


def memory_derived_object(object_alias: str) -> str:
    """知识对象 `object_alias` 的主来源是 Memory 来源。零参数。"""
    return _memory_derived(_alias(object_alias, _DERIVED_INNER))


def memory_derived_relation(relation_alias: str) -> str:
    """知识关系 `relation_alias` 的主来源是 Memory 来源。零参数。"""
    return _memory_derived(_alias(relation_alias, _DERIVED_INNER))


def memory_derived_in_notebook(row_alias: str) -> str:
    """行 `row_alias`(取其 `source_id` 与 `notebook_id`)的主来源是**本笔记本的** Memory
    来源。零参数。

    与 `memory_derived_object` 同一判据,多钉一条 `ds.notebook_id = {a}.notebook_id`:行与
    它的来源同属一个笔记本是写入不变量,所以在合法数据上两者逐行同义;多出来的这条让
    规划器把外层的 `notebook_id = $1` 传进内层,「Memory 来源集合」只按本笔记本构建,
    成本随本库、不随全站 Memory 的用量增长(拷贝快照与分享预览的计数,见 sharing_store)。
    """
    a = _alias(row_alias, _DERIVED_INNER)
    return (
        "EXISTS (SELECT 1 FROM sources ds "
        f"WHERE ds.id = {a}.source_id AND ds.notebook_id = {a}.notebook_id "
        f"AND {memory_source_type_predicate('ds.source_type')})"
    )


def no_memory_member_cluster(cluster_alias: str) -> str:
    """概念簇 `cluster_alias`(取其 `notebook_id`/`canonical_id`/`generation`)整簇没有
    Memory 派生成员。零参数。

    `source_type` 不加限定词:三张 join 表里只有 sources(ms) 有这列,只能解析到它。
    内层引用与外层行同笔记本、同代(外层已按 published 谓词过滤,相关引用零新参数)——
    「按整簇排除 memory」只在同一笔记本的同一代内判定:别的笔记本里撞同名的
    canonical_id、双代窗口里 building 代的成员,都不得跨界误判。
    """
    return "NOT " + _memory_member_arm(_alias(cluster_alias, _CLUSTER_INNER))


def _memory_member_arm(c: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM concept_clusters mc "
        "JOIN knowledge_objects mo ON mo.id = mc.member_object_id "
        "JOIN sources ms ON ms.id = mo.source_id "
        f"WHERE mc.notebook_id = {c}.notebook_id "
        f"AND mc.canonical_id = {c}.canonical_id "
        f"AND mc.generation = {c}.generation "
        f"AND {memory_source_type_predicate()})"
    )


def cluster_seed_object_id(cluster_alias: str) -> str:
    """簇 `cluster_alias` 的 canonical id 若是**按对象 id 铸的**,取出那个对象 id;否则 NULL。
    零参数。

    `kg_merge.seed_or_unique`:名字退化(只剩符号)的对象不共簇,种子回退成 `~<对象 id>`,
    canonical id 即 `<类型前缀>~<对象 id>`;类型前缀是 `K-`(概念)或 `K` 加一个字母再加
    `-`(`KL-` / `KF-` / `KP-`),对象 id 一律以 `ko-` 开头。只认这两种长度的前缀后紧跟
    `~ko-` 的写法:`_norm` 族归一化器会洗掉 `~`,真名种子不可能长成这样;`_norm_formula`
    理论上能留下 `~`,但要恰好是 `~ko-...` 开头的公式才会被误读,实际不可达。只用
    `substr` 与等值比较,两个后端文本逐字相同(不用 LIKE:PostgreSQL 的 `%` 在带参语句里
    要转义成 `%%`,会让两份文本分叉)。
    """
    col = f"{_alias(cluster_alias, frozenset())}.canonical_id"
    return (
        f"(CASE WHEN substr({col}, 1, 6) = 'K-~ko-' THEN substr({col}, 4) "
        f"WHEN substr({col}, 1, 1) = 'K' AND substr({col}, 3, 5) = '-~ko-' "
        f"THEN substr({col}, 5) END)"
    )


def _memory_canonical_arm(c: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM knowledge_objects mk JOIN sources mks ON mks.id = mk.source_id "
        f"WHERE mk.id = {cluster_seed_object_id(c)} "
        f"AND {memory_source_type_predicate('mks.source_type')})"
    )


def memory_seed_cluster(cluster_alias: str) -> str:
    """概念簇 `cluster_alias` 的 canonical id 是按一个 Memory 派生对象的 id 铸的(见
    `cluster_seed_object_id`):一行一次主键探测,只对 `~ko-` 形态的行真正命中。零参数。"""
    return _memory_canonical_arm(_alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER))


def memory_member_cluster_keys() -> str:
    """本笔记本里**含 Memory 派生成员**的每个簇的 `(canonical_id, generation)`。一个参数:
    笔记本 id。

    从 Memory 一侧驱动:本库的 Memory 来源 → 它们的对象(`source_id` 索引)→ 这些对象的簇
    成员行(`member_object_id` 索引),集合随本库 Memory 的规模、每个笔记本只建一次。与
    `memory_cluster` 的成员臂同一判据(同一笔记本、同一代);那条臂写成按簇行相关的
    EXISTS,放进逐簇行求值的语句里代价是 O(Σ簇大小²)(实测一个 2000 成员的簇每条语句
    13–15 s),所以按簇行过滤的读者读这份集合、在集合上做差,而不是逐行探测。
    """
    return (
        "SELECT DISTINCT mc.canonical_id, mc.generation FROM sources ms "
        "JOIN knowledge_objects mo ON mo.source_id = ms.id "
        "JOIN concept_clusters mc ON mc.member_object_id = mo.id "
        "AND mc.notebook_id = ms.notebook_id "
        f"WHERE ms.notebook_id = %s AND {memory_source_type_predicate('ms.source_type')}"
    )


def memory_cluster(cluster_alias: str) -> str:
    """概念簇 `cluster_alias`(取其 `notebook_id`/`canonical_id`/`generation`)是**某条
    Memory 的簇**:同一笔记本同一代里有 Memory 派生成员(成员臂;按簇行过滤的读者改读
    `memory_member_cluster_keys`,同一判据),**或**它的 canonical id 是按一个 Memory 派生对象
    的 id 铸的(`memory_seed_cluster`)。零参数。

    canonical 臂只认**按对象 id 铸的**种子(`K-~ko-…` 与 `Kx-~ko-…`,见
    `cluster_seed_object_id`);canonical id 一律带类型前缀,从不等于裸对象 id。**真名种子**
    (`K-<规范化名字>`)在这里认不出来:Memory 对象还在簇里时由成员臂认出;Memory 被删之后
    只剩共享成员带着它起的名字——删除必然标脏,拷贝对脏源库一个簇都不带(sharing_store 的
    `_source_clustering_current`),这一形态由那条规则覆盖。删除清理(E5-2)按对象算出该对象
    能铸出的 canonical id(`kg_merge.minted_canonical_ids`,另加桥接 id),能认出真名种子——
    它比这里宽,两处刻意不同:清理时 Memory 对象还在,拷贝时可能已经不在。
    """
    c = _alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER)
    return f"({_memory_member_arm(c)} OR {_memory_canonical_arm(c)})"


def no_memory_cluster(cluster_alias: str) -> str:
    """`memory_cluster` 的否定(同一对臂,两个 NOT EXISTS 以便规划成反连接)。零参数。"""
    c = _alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER)
    return f"(NOT {_memory_member_arm(c)} AND NOT {_memory_canonical_arm(c)})"
