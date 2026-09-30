"""`memory_sql` 片段的判定矩阵 —— **两端吃同一张表**。

沿用 ``kg_extracted_parity_cases.py`` 的理由:判据在 SQLite 与 PostgreSQL 各有一份手抄的
方言 SQL(``?`` vs ``%s``),两边各造一套夹具就等于让每一端迎合自己那份实现。这里把
「什么样的数据该判成什么」写成与后端无关的一张表,``tests/test_memory_sql_contract.py``
(SQLite)与 ``tests/postgres/test_memory_sql_contract_pg.py``(PostgreSQL)各自 import。

世界(两个笔记本):

* 三位用户:alice、bob、carol;alice 与 bob 各有一条 Memory(``memory_items.created_by``),
  carol 没有。
* 笔记本一有六个来源:alice 的 Memory 来源、bob 的 Memory 来源、**孤儿** Memory 来源
  (``memory_id`` 指向不存在的 ``memory_items`` 行)、**无 memory_id** 的 Memory 来源、
  Knowhow 来源、普通上传来源。笔记本二只有一个普通来源。
* 每个来源一个知识对象、一条关系(普通来源另有几个只用于凑簇的对象);外加一个「没有
  归属来源」的对象(``source_id=''``)与一条关系(``source_id`` 为 NULL)——它们既不是
  Memory 也不该被连坐。
* 概念簇(笔记本、canonical_id、generation 三元一个簇):
  - 笔记本一 generation 0:纯净簇、混入 alice Memory 对象的簇、只含孤儿 Memory 对象的簇、
    只含无归属对象的簇、以及 ``can-gen``(只含普通来源的对象);
  - 笔记本一 generation 1(building 代):``can-gen`` 的 building 代成员是 bob 的 Memory
    对象 —— 它只该让**这一代**的 ``can-gen`` 被排除,published 代的不受牵连;
  - 笔记本二 generation 0:与笔记本一混入 Memory 的簇**同名**的 ``can-mixed``(簇 id 是
    ``K-<规范化种子名>``,不同笔记本会撞 id),成员只是普通对象 —— 笔记本一里的 Memory
    成员不得让它消失。

不是测试模块(没有 ``test_`` 前缀),pytest 不会收集它。
"""
from __future__ import annotations

NOTEBOOK = "nb-memory-sql"
NOTEBOOK2 = "nb-memory-sql-2"
OWNER = "u-owner"

USERS = ("u-alice", "u-bob", "u-carol", OWNER)

#: ``(memory id, created_by)``
MEMORY_ITEMS = (
    ("mem-alice", "u-alice"),
    ("mem-bob", "u-bob"),
)

#: ``(source id, source_type, memory_id)`` —— 笔记本一。
SOURCES = (
    ("src-mem-alice", "memory", "mem-alice"),
    ("src-mem-bob", "memory", "mem-bob"),
    ("src-mem-orphan", "memory", "mem-gone"),
    ("src-mem-null", "memory", None),
    ("src-knowhow", "knowhow", None),
    ("src-upload", "upload", None),
)

#: 笔记本二的来源(同一形状)。
SOURCES_NB2 = (("src-nb2-upload", "upload", None),)

#: ``(object id, source_id, object_type)`` —— 笔记本一:每个来源一个对象,外加无归属对象。
OBJECTS = (
    ("ko-mem-alice", "src-mem-alice", "concept"),
    ("ko-mem-bob", "src-mem-bob", "concept"),
    ("ko-mem-orphan", "src-mem-orphan", "concept"),
    ("ko-mem-null", "src-mem-null", "concept"),
    ("ko-knowhow", "src-knowhow", "concept"),
    ("ko-upload", "src-upload", "concept"),
    ("ko-mixed-plain", "src-upload", "concept"),
    ("ko-gen-plain", "src-upload", "concept"),
    ("ko-unowned", "", "claim"),
)

#: 笔记本二的对象。
OBJECTS_NB2 = (("ko-nb2-plain", "src-nb2-upload", "concept"),)

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

#: ``(notebook, canonical_id, canonical_name, generation, member object ids)``
CLUSTERS = (
    (NOTEBOOK, "can-clean", "clean-topic", 0, ("ko-upload", "ko-knowhow")),
    (NOTEBOOK, "can-mixed", "mixed-topic", 0, ("ko-mixed-plain", "ko-mem-alice")),
    (NOTEBOOK, "can-orphan", "orphan-topic", 0, ("ko-mem-orphan",)),
    (NOTEBOOK, "can-unowned", "unowned-topic", 0, ("ko-unowned",)),
    (NOTEBOOK, "can-gen", "gen-topic", 0, ("ko-gen-plain",)),
    (NOTEBOOK, "can-gen", "gen-topic", 1, ("ko-mem-bob",)),
    (NOTEBOOK2, "can-mixed", "mixed-topic-nb2", 0, ("ko-nb2-plain",)),
    # 「一条 Memory 的簇」的 canonical 臂(generation 2..8:不是 published 代,不碰
    # query_store 的黄金聚合;每簇一代,因为同一成员在同一代只能属于一个簇)。成员都是普通对象,是不是 Memory 的簇只看 canonical id。
    (NOTEBOOK, "K-~ko-mem-alice", "seeded-by-alice", 2, ("ko-upload",)),
    (NOTEBOOK, "KL-~ko-mem-bob", "seeded-by-bob", 3, ("ko-mixed-plain",)),
    (NOTEBOOK, "ko-mem-null", "canonical-is-memory", 4, ("ko-gen-plain",)),
    (NOTEBOOK, "K-~ko-upload", "seeded-by-upload", 5, ("ko-knowhow",)),
    (NOTEBOOK, "K-~ko-gone", "seeded-by-deleted", 6, ("ko-upload",)),
    (NOTEBOOK, "K-ko-mem-alice", "real-name-seed", 7, ("ko-upload",)),
    (NOTEBOOK, "K-~xko-mem-alice", "not-minted", 8, ("ko-upload",)),
)

#: ``cluster_seed_object_id`` 对每个 canonical id 取出的对象 id(没铸自对象 id 的为 None)。
CLUSTER_SEED_OBJECT_IDS = {
    "K-~ko-mem-alice": "ko-mem-alice",
    "KL-~ko-mem-bob": "ko-mem-bob",
    "KF-~ko-x": "ko-x",
    "KP-~ko-x": "ko-x",
    "K-~ko-gone": "ko-gone",
    "K-ko-mem-alice": None,
    "K-~xko-mem-alice": None,
    "ko-mem-null": None,
    "can-clean": None,
    "K-~": None,
    "K-~ko": None,
    "KLM-~ko-x": None,
    "k-~ko-x": None,
    "": None,
}

ALL_SOURCE_IDS = frozenset(s[0] for s in SOURCES + SOURCES_NB2)
ALL_OBJECT_IDS = frozenset(o[0] for o in OBJECTS + OBJECTS_NB2)
ALL_RELATION_IDS = frozenset(r[0] for r in RELATIONS)

#: Knowhow 与普通来源对每个人可读(本谓词不管它们)。
_ORDINARY_SOURCES = frozenset({"src-knowhow", "src-upload", "src-nb2-upload"})

#: 查看者 -> 可读的来源集合。孤儿/无 memory_id 的 Memory 来源对所有人失败即关;
#: 空查看者、NULL 查看者、不存在的查看者读不到任何 Memory 来源。
READABLE_SOURCES = {
    "u-alice": _ORDINARY_SOURCES | {"src-mem-alice"},
    "u-bob": _ORDINARY_SOURCES | {"src-mem-bob"},
    "u-carol": _ORDINARY_SOURCES,
    "": _ORDINARY_SOURCES,
    None: _ORDINARY_SOURCES,
    "u-nobody": _ORDINARY_SOURCES,
}

#: 「派生自 Memory」(与查看者无关):主 source 是 Memory 来源,含孤儿与无 memory_id。
MEMORY_DERIVED_OBJECTS = frozenset(
    {"ko-mem-alice", "ko-mem-bob", "ko-mem-orphan", "ko-mem-null"}
)
MEMORY_DERIVED_RELATIONS = frozenset(
    {"kr-mem-alice", "kr-mem-bob", "kr-mem-orphan", "kr-mem-null"}
)


def _kept_objects(own: frozenset[str]) -> frozenset[str]:
    return (ALL_OBJECT_IDS - MEMORY_DERIVED_OBJECTS) | own


def _kept_relations(own: frozenset[str]) -> frozenset[str]:
    return (ALL_RELATION_IDS - MEMORY_DERIVED_RELATIONS) | own


#: 查看者 -> 排除「别人的 Memory 来源」之后保留的对象/关系。本人 Memory 保留;别人的、
#: 孤儿、无 memory_id 的一律排除;无归属对象/关系与普通来源照旧保留。
FOREIGN_EXCLUDED_KEEPS_OBJECTS = {
    "u-alice": _kept_objects(frozenset({"ko-mem-alice"})),
    "u-bob": _kept_objects(frozenset({"ko-mem-bob"})),
    "u-carol": _kept_objects(frozenset()),
    "": _kept_objects(frozenset()),
    None: _kept_objects(frozenset()),
}
FOREIGN_EXCLUDED_KEEPS_RELATIONS = {
    "u-alice": _kept_relations(frozenset({"kr-mem-alice"})),
    "u-bob": _kept_relations(frozenset({"kr-mem-bob"})),
    "u-carol": _kept_relations(frozenset()),
    "": _kept_relations(frozenset()),
    None: _kept_relations(frozenset()),
}

#: 整簇没有 Memory 派生成员的簇,以 ``"<notebook>/<canonical_id>/<generation>"`` 表示。
#: 笔记本一 gen 0 混入 alice Memory 对象的簇与孤儿簇整簇排除;只含无归属对象的簇
#: (source_id='' 匹配不到任何来源)保留;``can-gen`` 的 gen 0 保留而 gen 1(含 bob 的
#: Memory 对象)排除;笔记本二的 ``can-mixed`` 不受笔记本一同名簇里 Memory 成员牵连。
NO_MEMORY_MEMBER_CLUSTERS = frozenset(
    {
        f"{NOTEBOOK}/can-clean/0",
        f"{NOTEBOOK}/can-unowned/0",
        f"{NOTEBOOK}/can-gen/0",
        f"{NOTEBOOK2}/can-mixed/0",
        f"{NOTEBOOK}/K-~ko-mem-alice/2",
        f"{NOTEBOOK}/KL-~ko-mem-bob/3",
        f"{NOTEBOOK}/ko-mem-null/4",
        f"{NOTEBOOK}/K-~ko-upload/5",
        f"{NOTEBOOK}/K-~ko-gone/6",
        f"{NOTEBOOK}/K-ko-mem-alice/7",
        f"{NOTEBOOK}/K-~xko-mem-alice/8",
    }
)

#: 「一条 Memory 的簇」(``memory_cluster``):有 Memory 派生成员的簇(成员臂,按笔记本、代),
#: 加上 canonical id 就是 / 铸自一个 Memory 派生对象的簇(canonical 臂)。铸自普通对象、
#: 铸自已不存在的对象、真名种子恰好长得像对象 id、以及不是 ``~ko-`` 紧跟前缀的写法都不算。
MEMORY_CLUSTERS = frozenset(
    {
        f"{NOTEBOOK}/can-mixed/0",
        f"{NOTEBOOK}/can-orphan/0",
        f"{NOTEBOOK}/can-gen/1",
        f"{NOTEBOOK}/K-~ko-mem-alice/2",
        f"{NOTEBOOK}/KL-~ko-mem-bob/3",
        f"{NOTEBOOK}/ko-mem-null/4",
    }
)
ALL_CLUSTERS = frozenset(f"{nb}/{cid}/{gen}" for nb, cid, _name, gen, _m in CLUSTERS)

#: 旧 ``_NOT_MEMORY_OWNED_SQL``(E0 之前的手写文本,外层别名 ``o``),用作差分参照:
#: 新片段的否定必须与它在同一份数据上逐行同义。
PRE_E0_NOT_MEMORY_OWNED = (
    "NOT EXISTS (SELECT 1 FROM sources "
    "WHERE sources.id = o.source_id AND source_type = 'memory')"
)

#: ``QueryStore`` 两个聚合在笔记本一上的黄金结果(硬编码,与新旧实现都无关)。
#: ``top_concept_names`` 只看 published 代(此处为 0):``can-gen`` 的 gen 1 成员是
#: bob 的 Memory 对象,不得让 published 代的 ``gen-topic`` 消失。
QUERY_STORE_TYPE_COUNTS = {"concept": 4, "claim": 1}
QUERY_STORE_TOP_CONCEPTS = [
    ("clean-topic", 2),
    ("gen-topic", 1),
    ("unowned-topic", 1),
]

#: 外层别名校验必须拒绝的写法(不分大小写地撞内层别名、尾部换行、带引号、带 schema)。
BAD_ALIASES = {
    "readable": ("rm", "RM", "Rm", "o\n", '"s"', "public.s", "1x", "a b", ""),
    "foreign": ("fs", "FS", "fm", "Fm", "o\n", '"o"', "public.o", "1x", ""),
    "derived": ("ds", "DS", "Ds", "o\n", '"o"', "public.o", "1x", ""),
    "cluster": ("mc", "MC", "Mc", "mo", "MO", "ms", "Ms", "c\n", '"c"', "public.c", ""),
    "memory_cluster": (
        "mc", "MO", "ms", "mk", "MK", "mks", "Mks", "c\n", '"c"', "public.c", "",
    ),
    "seed": ("c\n", '"c"', "public.c", "1x", ""),
}
