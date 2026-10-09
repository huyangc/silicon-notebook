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
`read_reference` 的属主闸)同样消费 `memory_source_readable`,于是「谁的 Memory 进
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

#: 拒绝信息:`ChunkStore` 的两个写方法、KG 发布、Knowhow 表传输在目标是 Memory 来源时抛出所用的
#: 固定文案,不带任何内容。同步导入不用它:整轮拒绝时用自己的句子(带来源数量与 id、说明怎么处理),
#: 见 `migration/sync/import_.py`。
MEMORY_SOURCE_NOT_CHUNKED = "memory sources are not chunked"

#: 拒绝信息:Knowhow 表传输的 payload 里有 chunk 行不指向 payload 自己的来源。
TRANSFER_CHUNK_SOURCE_MISMATCH = "transfer chunk rows must belong to the transferred source"

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


def foreign_memory_in_notebook_excluded(row_alias: str) -> str:
    """行 `row_alias`(知识对象或关系,取其 `source_id` 与 `notebook_id`)的主来源不是
    **本笔记本里**别人的 Memory 来源。一个参数:查看者 id。

    与 `foreign_memory_object_excluded` 同一判据,多钉一条 `fs.notebook_id = {a}.notebook_id`
    (理由同 `memory_derived_in_notebook`):行与它的来源同属一个笔记本是写入不变量,所以
    在合法数据上两者逐行同义;多出来的这条让规划器把外层的 `notebook_id = $1` 传进内层,
    反连接的小侧只按本笔记本的 Memory 来源构建(`idx_sources_nb_hidden_type`),成本随
    本库、不随全站 Memory 的用量增长。KG 读者(列表、计数、图、搜索、邻居)用这一形。
    """
    a = _alias(row_alias, _FOREIGN_INNER)
    return (
        "NOT EXISTS (SELECT 1 FROM sources fs "
        f"WHERE fs.id = {a}.source_id AND fs.notebook_id = {a}.notebook_id "
        f"AND {memory_source_type_predicate('fs.source_type')} "
        "AND NOT EXISTS (SELECT 1 FROM memory_items fm "
        "WHERE fm.id = fs.memory_id AND fm.created_by = ?))"
    )


def memory_viewer_filter(row_alias: str, viewer_id: "str | None") -> "tuple[str, tuple]":
    """KG 读者的 ``viewer_id`` 关键字的唯一解释(E4-4):返回 ``(" AND <谓词>", 参数)``,
    调用方把它接在自己的 WHERE 之后、按文本位置放参数。

    * ``None`` → ``("", ())``:不拼任何东西,语句文本与隔离之前逐字相同(内部调用方、
      库里没有外人 Memory 时服务层根本不传)。
    * ``""`` → 排除**全部** Memory 派生行,查看者本人的也不例外(``memory:read`` 通道
      关闭、或没有身份的读取)。用零参数的 `memory_derived_in_notebook`,不把空串当成
      一个查看者去比 `created_by`。
    * 其余 → 只排除本笔记本里**别人的** Memory 派生行(`foreign_memory_in_notebook_excluded`,
      一个参数)。
    """
    if viewer_id is None:
        return "", ()
    if not viewer_id:
        return f" AND NOT {memory_derived_in_notebook(row_alias)}", ()
    return f" AND {foreign_memory_in_notebook_excluded(row_alias)}", (viewer_id,)


def own_memory_source(source_alias: str) -> str:
    """来源 `source_alias` 是查看者**本人**的 Memory 来源。一个参数:查看者 id。

    即 `source_type = 'memory'` 且 `memory_source_readable`——读者用它从本库的 Memory
    来源(`idx_sources_nb_hidden_type`)驱动,读出查看者自己的 Memory 派生行(共享工件
    之上的本人叠加、本人 Memory 计数)。空查看者读不到任何行。
    """
    a = _alias(source_alias, _READABLE_INNER)
    return (
        f"{memory_source_type_predicate(f'{a}.source_type')} "
        f"AND {memory_source_readable(a)}"
    )


def hidden_type_index_term(source_alias: str) -> str:
    """规划器提示,语义上冗余(每条 Memory 来源都满足):`idx_sources_nb_hidden_type` 是
    **部分**索引(`WHERE source_type IN ('memory','knowhow')`),SQLite 只有在查询里原样
    重复这条谓词时才会用它——单写 `source_type = 'memory'` 规划器推不出蕴含,会把本库
    全部来源走一遍。由本库 Memory 来源**驱动**的读(本人叠加、本人 Memory 计数、KG 搜索
    的外人 id 集合)与它并列写。零参数。PostgreSQL 能自己证明蕴含,镜像只为双后端同名。
    """
    a = _alias(source_alias, _READABLE_INNER)
    return f"{a}.source_type IN ('{MEMORY_SOURCE_TYPE}','knowhow')"


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


def cluster_seed_object_id(cluster_alias: str, column: str = "canonical_id") -> str:
    """簇 `cluster_alias` 的 canonical id 若是**按对象 id 铸的**,取出那个对象 id;否则 NULL。
    零参数。

    `kg_merge.seed_or_unique`:名字退化(只剩符号)的对象不共簇,种子回退成 `~<对象 id>`,
    canonical id 即 `<类型前缀>~<对象 id>`;类型前缀是 `K-`(概念)或 `K` 加一个字母再加
    `-`(`KL-` / `KF-` / `KP-`),对象 id 一律以 `ko-` 开头。只认这两种长度的前缀后紧跟
    `~ko-` 的写法:`_norm` 族归一化器会洗掉 `~`,真名种子不可能长成这样;`_norm_formula`
    理论上能留下 `~`,但要恰好是 `~ko-...` 开头的公式才会被误读,实际不可达。只用
    `substr` 与等值比较,两个后端文本逐字相同(不用 LIKE:PostgreSQL 的 `%` 在带参语句里
    要转义成 `%%`,会让两份文本分叉)。

    `column` 默认读簇行的 `canonical_id`;Memory 清除用同一条规则判合并候选的
    `canonical_a` / `canonical_b`(候选点名的簇 id 是否铸自被清除的对象),不另写一份。
    """
    col = f"{_alias(cluster_alias, frozenset())}.{_alias(column, frozenset())}"
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

    与 `memory_cluster` 的成员臂同一判据(同一笔记本、同一代)。那条臂写成按簇行相关的
    EXISTS,放进逐簇行求值的语句里代价是 O(Σ簇大小²)(实测一个 2000 成员的簇每条语句
    13–15 s);按簇行过滤的读者改读这份集合、在集合上做差。集合每次拷贝读一次,大小不超过
    本库 Memory 对象数 × 代数。

    实际计划(不是「从 Memory 一侧驱动」这么理想):PostgreSQL 把本库的对象与本库的簇行做
    Hash Join(concept_clusters 扫一遍),再按主键逐对象探测 sources——代价随本库规模线性
    增长;SQLite(没有统计信息)按笔记本索引走本库的对象,逐个按主键探测来源,对 Memory
    对象按 ``idx_clusters_member (member_object_id=?)`` 探测簇行——同样随本库规模线性增长,
    从不按簇行把整簇走一遍。
    SQLite 专有的一处:``+mc.notebook_id``。本仓库的 SQLite 从不跑 ANALYZE(没有
    ``sqlite_stat1``),裸的 ``mc.notebook_id = ms.notebook_id`` 会让规划器改用
    ``idx_clusters_nb_canonical_member_gen (notebook_id=?)``,每个 Memory 对象把本库全部簇行
    走一遍(实测 4000 个 Memory 对象 1426 ms);一元 ``+`` 让这一项不参与选索引,计划回到
    ``idx_clusters_member (member_object_id=?)``(6 ms),结果集合不变。两端文本除这一个
    ``+`` 外逐字相同(契约测试按此比对)。
    """
    return (
        "SELECT DISTINCT mc.canonical_id, mc.generation FROM sources ms "
        "JOIN knowledge_objects mo ON mo.source_id = ms.id AND mo.notebook_id = ms.notebook_id "
        "JOIN concept_clusters mc ON mc.member_object_id = mo.id "
        "AND +mc.notebook_id = ms.notebook_id "
        f"WHERE ms.notebook_id = ? AND {memory_source_type_predicate('ms.source_type')}"
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
    `_source_clustering_current`),这一形态由那条规则覆盖。删除清理(E5-2,
    `purge_memory_review_rows_on`)与这里共用同一条铸造规则(`cluster_seed_object_id`),此外
    按成员认出整簇——清理时 Memory 对象还在,真名种子的簇因此也能认出;合并候选另认没有簇行
    带着的桥接 id(`kg_merge.purge_bridge_canonical_ids`)。拷贝时 Memory 对象可能已经不在,
    所以这里只有成员臂与铸造臂。
    """
    c = _alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER)
    return f"({_memory_member_arm(c)} OR {_memory_canonical_arm(c)})"


def no_memory_cluster(cluster_alias: str) -> str:
    """`memory_cluster` 的否定(同一对臂,两个 NOT EXISTS 以便规划成反连接)。零参数。"""
    c = _alias(cluster_alias, _CLUSTER_INNER | _CANONICAL_INNER)
    return f"(NOT {_memory_member_arm(c)} AND NOT {_memory_canonical_arm(c)})"
