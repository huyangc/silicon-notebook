"""`memory_sql` 片段的判定矩阵 —— **两端吃同一张表**。

沿用 ``kg_extracted_parity_cases.py`` 的理由:判据在 SQLite 与 PostgreSQL 各有一份手抄的
方言 SQL(``?`` vs ``%s``),两边各造一套夹具就等于让每一端迎合自己那份实现。这里把
「什么样的数据该判成什么」写成与后端无关的一张表,``tests/test_memory_sql_contract.py``
(SQLite)与 ``tests/postgres/test_memory_sql_contract_pg.py``(PostgreSQL)各自 import。

世界(一个笔记本):

* 三位用户:alice、bob、carol;alice 与 bob 各有一条 Memory(``memory_items.created_by``),
  carol 没有。
* 六个来源:alice 的 Memory 来源、bob 的 Memory 来源、**孤儿** Memory 来源(``memory_id``
  指向不存在的 ``memory_items`` 行)、**无 memory_id** 的 Memory 来源、Knowhow 来源、
  普通上传来源。
* 每个来源一个知识对象、一条关系(普通来源另有一个只用于凑簇的对象);外加一个「没有归属来源」的对象(``source_id=''``)
  与一条关系(``source_id`` 为 NULL)——它们既不是 Memory 也不该被连坐。
* 四个概念簇:纯净簇、混入 alice Memory 对象的簇、只含孤儿 Memory 对象的簇、只含无归属
  对象的簇。

不是测试模块(没有 ``test_`` 前缀),pytest 不会收集它。
"""
from __future__ import annotations

NOTEBOOK = "nb-memory-sql"
OWNER = "u-owner"

USERS = ("u-alice", "u-bob", "u-carol", OWNER)

#: ``(memory id, created_by)``
MEMORY_ITEMS = (
    ("mem-alice", "u-alice"),
    ("mem-bob", "u-bob"),
)

#: ``(source id, source_type, memory_id)``
SOURCES = (
    ("src-mem-alice", "memory", "mem-alice"),
    ("src-mem-bob", "memory", "mem-bob"),
    ("src-mem-orphan", "memory", "mem-gone"),
    ("src-mem-null", "memory", None),
    ("src-knowhow", "knowhow", None),
    ("src-upload", "upload", None),
)

#: ``(object id, source_id, object_type)`` —— 每个来源一个对象,外加无归属对象。
OBJECTS = (
    ("ko-mem-alice", "src-mem-alice", "concept"),
    ("ko-mem-bob", "src-mem-bob", "concept"),
    ("ko-mem-orphan", "src-mem-orphan", "concept"),
    ("ko-mem-null", "src-mem-null", "concept"),
    ("ko-knowhow", "src-knowhow", "concept"),
    ("ko-upload", "src-upload", "concept"),
    ("ko-mixed-plain", "src-upload", "concept"),
    ("ko-unowned", "", "claim"),
)

#: ``(relation id, source_id)`` —— 每个来源一条,外加 source_id 为 NULL 的一条。
RELATIONS = (
    ("kr-mem-alice", "src-mem-alice"),
    ("kr-mem-bob", "src-mem-bob"),
    ("kr-mem-orphan", "src-mem-orphan"),
    ("kr-mem-null", "src-mem-null"),
    ("kr-knowhow", "src-knowhow"),
    ("kr-upload", "src-upload"),
    ("kr-no-source", None),
)

#: ``(canonical_id, canonical_name, member object ids)``
CLUSTERS = (
    ("can-clean", "clean-topic", ("ko-upload", "ko-knowhow")),
    ("can-mixed", "mixed-topic", ("ko-mixed-plain", "ko-mem-alice")),
    ("can-orphan", "orphan-topic", ("ko-mem-orphan",)),
    ("can-unowned", "unowned-topic", ("ko-unowned",)),
)

ALL_SOURCE_IDS = frozenset(s[0] for s in SOURCES)
ALL_OBJECT_IDS = frozenset(o[0] for o in OBJECTS)
ALL_RELATION_IDS = frozenset(r[0] for r in RELATIONS)
ALL_CLUSTER_IDS = frozenset(c[0] for c in CLUSTERS)

#: Knowhow 与普通来源对每个人可读(本谓词不管它们)。
_ORDINARY_SOURCES = frozenset({"src-knowhow", "src-upload"})

#: 查看者 -> 可读的来源集合。孤儿/无 memory_id 的 Memory 来源对所有人失败即关;
#: 空查看者、不存在的查看者读不到任何 Memory 来源。
READABLE_SOURCES = {
    "u-alice": _ORDINARY_SOURCES | {"src-mem-alice"},
    "u-bob": _ORDINARY_SOURCES | {"src-mem-bob"},
    "u-carol": _ORDINARY_SOURCES,
    "": _ORDINARY_SOURCES,
    "u-nobody": _ORDINARY_SOURCES,
}

#: 「派生自 Memory」(与查看者无关):主 source 是 Memory 来源,含孤儿与无 memory_id。
MEMORY_DERIVED_OBJECTS = frozenset(
    {"ko-mem-alice", "ko-mem-bob", "ko-mem-orphan", "ko-mem-null"}
)
MEMORY_DERIVED_RELATIONS = frozenset(
    {"kr-mem-alice", "kr-mem-bob", "kr-mem-orphan", "kr-mem-null"}
)

_ALICE_MEMORY_SOURCE_OBJECTS = frozenset({"ko-mem-alice"})
_BOB_MEMORY_SOURCE_OBJECTS = frozenset({"ko-mem-bob"})
_ALICE_MEMORY_SOURCE_RELATIONS = frozenset({"kr-mem-alice"})
_BOB_MEMORY_SOURCE_RELATIONS = frozenset({"kr-mem-bob"})


def _kept_objects(own: frozenset[str]) -> frozenset[str]:
    return (ALL_OBJECT_IDS - MEMORY_DERIVED_OBJECTS) | own


def _kept_relations(own: frozenset[str]) -> frozenset[str]:
    return (ALL_RELATION_IDS - MEMORY_DERIVED_RELATIONS) | own


#: 查看者 -> 排除「别人的 Memory 来源」之后保留的对象/关系。本人 Memory 保留;别人的、
#: 孤儿、无 memory_id 的一律排除;无归属对象/关系与普通来源照旧保留。
FOREIGN_EXCLUDED_KEEPS_OBJECTS = {
    "u-alice": _kept_objects(_ALICE_MEMORY_SOURCE_OBJECTS),
    "u-bob": _kept_objects(_BOB_MEMORY_SOURCE_OBJECTS),
    "u-carol": _kept_objects(frozenset()),
    "": _kept_objects(frozenset()),
}
FOREIGN_EXCLUDED_KEEPS_RELATIONS = {
    "u-alice": _kept_relations(_ALICE_MEMORY_SOURCE_RELATIONS),
    "u-bob": _kept_relations(_BOB_MEMORY_SOURCE_RELATIONS),
    "u-carol": _kept_relations(frozenset()),
    "": _kept_relations(frozenset()),
}

#: 整簇没有 Memory 派生成员的簇:混入 alice Memory 对象的簇与孤儿簇整簇排除;
#: 只含无归属对象的簇(source_id='' 匹配不到任何来源)保留。
NO_MEMORY_MEMBER_CLUSTERS = frozenset({"can-clean", "can-unowned"})

#: 旧 ``_NOT_MEMORY_OWNED_SQL``(E0 之前的手写文本,外层别名 ``o``),用作差分参照:
#: 新片段的否定必须与它在同一份数据上逐行同义。
PRE_E0_NOT_MEMORY_OWNED = (
    "NOT EXISTS (SELECT 1 FROM sources "
    "WHERE sources.id = o.source_id AND source_type = 'memory')"
)

#: ``QueryStore`` 两个聚合在这个世界上的黄金结果(硬编码,与新旧实现都无关)。
QUERY_STORE_TYPE_COUNTS = {"concept": 3, "claim": 1}
QUERY_STORE_TOP_CONCEPTS = [("clean-topic", 2), ("unowned-topic", 1)]
