"""Memory 来源可读性与「派生自 Memory」判定的 SQL 片段 —— 两条判据的共享定义点。

镜像 `access_sql.py` / `mount_sql.py` 的模式,理由同款:Memory 是创建者私有的,而它
派生出的来源(`sources.source_type='memory'`,`sources.memory_id` → `memory_items.id`)、
知识对象、关系与概念簇会流进检索、KG 与来源读取的各个读者。若每个读者各写一份
「这条 Memory 派生行能不能给这个人看」的手写谓词,任何一份漂移都不会有测试自然抓到。
所以这两条判据在这里定义,新读者一律 import,不再手写。`postgres/memory_sql.py` 是它的
PostgreSQL 镜像(占位符 `%s`),两份必须同修,与 `access_sql.py` / `mount_sql.py` 一样。

现状要说清:两个 `source_store` 已迁入本模块——`hidden_source_ids` 的「对该用户可读」
就是 `memory_source_readable('s')`(KG 查看者规则据此判定「另一位成员的 Memory」),
`MEMORY_SOURCE_TYPE_PREDICATE` 由 `memory_source_type_predicate()` 渲染;
`sharing_store.source_notebook_id(viewer_id=...)`(来源/元素读取端点与 MCP
`get_cited_element` 的属主闸)同样消费 `memory_source_readable`,于是「谁的 Memory 进
天花板」与「谁能打开这条来源」是同一个定义。`postgres/chunk_store.py` 里两处 Memory
属主判断仍各自带着一份手写谓词,随改动该读者的变更迁入本模块。
`MEMORY_SOURCE_TYPE`/`memory_source_type_predicate` 与
`source_store.MEMORY_SOURCE_TYPE_PREDICATE` 的一致性仍由 `test_memory_sql_contract.py`
的漂移守卫钉住。

## 两类判据,两种形状

**读者按查看者判定**(需要一个参数 = 查看者 id):

* `memory_source_readable(source_alias)` —— 这条**来源**对查看者可读吗。非 Memory 来源
  (Knowhow、普通来源)一律可读;Memory 来源只有 `memory_items.created_by` 等于查看者时
  可读。`memory_id` 为空、或指向不存在的 `memory_items` 行(孤儿)的 Memory 来源
  **对所有人失败即关**;空查看者(`''` / `None` / 无 actor 的后台路径)读不到任何 Memory
  来源。恰好一个参数。
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
  对象,只过滤成员行洗不掉名字,所以取名字的查询按整簇排除)。零参数。簇 id 是
  `K-<规范化种子名>`,不同笔记本可以撞同一个 canonical_id,所以相关条件同时钉
  notebook 与 generation。
* `memory_derived_in_notebook(row_alias)` —— 同 `memory_derived_object`,另钉「来源属于行
  所在的笔记本」(写入不变量,合法数据上同义),让集合按本笔记本构建。零参数。
* `memory_cluster(cluster_alias)` / `no_memory_cluster(cluster_alias)` —— 「一条 Memory 的簇」
  的唯一定义(拷贝整簇不带、删除清理整簇删除共用):有 Memory 派生成员,**或** canonical id
  按 `cluster_seed_object_id` 铸自一个 Memory 派生对象的 id。零参数。
* `cluster_seed_object_id(cluster_alias)` —— canonical id 若按 `kg_merge.seed_or_unique` 的
  `<类型前缀>~<对象 id>` 形式铸成,取出那个对象 id,否则 NULL。零参数。

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
* 外层别名由调用方传入;内层别名(`rm` / `fs` / `fm` / `ds` / `mc` / `mo` / `ms` / `mk` /
  `mks`)固定。
  SQL 标识符不区分大小写,所以 `_alias` 按小写比较,并要求整串是裸标识符(`fullmatch`,
  不接受尾部换行、带引号或带 schema 的写法);传入与内层同名的外层别名会把相关引用绑到
  内层表上、静默改变语义(例如 `MC` 会让簇片段退化成恒真的 `mc.x = mc.x`),所以直接拒绝。
* **参数契约**:每个片段在语句文本里**它所在的位置**恰好消费固定个数的位置参数——
  readable / foreign 各 1 个(`?`,查看者),derived / cluster / seed 各 0 个。调用方按片段在
  语句文本中的先后位置排列参数;个数由 `test_memory_sql_contract.py` 断言,嵌进更大
  语句时参数夹在别的参数中间的行为也由它断言。
* `'memory'` 字面量只在 `MEMORY_SOURCE_TYPE` 出现一次;所有片段经它或
  `memory_source_type_predicate` 渲染,守卫再断言它与 `source_store` 的常量一致。
  簇片段另有 `cluster_seed_object_id` 的铸造形状字面量(`'K-~ko-'` / `'K'` / `'-~ko-'`),
  守卫逐个登记。
* Knowhow 不在本模块的判据内:Knowhow 投影是笔记本级共享的,M1 只针对 Memory。
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
        f"WHERE rm.id = {a}.memory_id AND rm.created_by = ?))"
    )


def _foreign_memory_excluded(alias: str) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM sources fs "
        f"WHERE fs.id = {alias}.source_id AND {memory_source_type_predicate('fs.source_type')} "
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


def memory_cluster(cluster_alias: str) -> str:
    """概念簇 `cluster_alias`(取其 `notebook_id`/`canonical_id`/`generation`)是**某条
    Memory 的簇**:同一笔记本同一代里有 Memory 派生成员,**或**它的 canonical id 是按
    (见 `cluster_seed_object_id`)一个 Memory 派生对象的 id 铸的。零参数。canonical id
    一律带类型前缀(`kg_merge`),从不等于裸对象 id,所以不比较 `canonical_id = 对象 id`。

    「一条 Memory 的簇」只有这一个定义:拷贝(E5-1)用它的否定 `no_memory_cluster` 整簇
    不带,删除清理(E5-2)用它整簇删除——簇名与描述整簇复制到每个成员行,可能取自那个
    Memory 对象,只删 Memory 成员行洗不掉它们。成员臂与 `no_memory_member_cluster` 同一段
    文本;canonical 臂在对象还在时才判得出(对象已删的簇无从证明是不是 Memory 的,由调用方
    自行决定怎么处理,这里不猜)。
    """
    c = _alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER)
    return f"({_memory_member_arm(c)} OR {_memory_canonical_arm(c)})"


def no_memory_cluster(cluster_alias: str) -> str:
    """`memory_cluster` 的否定(同一对臂,两个 NOT EXISTS 以便规划成反连接)。零参数。"""
    c = _alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER)
    return f"(NOT {_memory_member_arm(c)} AND NOT {_memory_canonical_arm(c)})"
