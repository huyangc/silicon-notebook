"""KG 质量分析预计算产物的**后端中性**契约与纯折叠逻辑(T2)。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md` §3.2/§3.3/§3.4。

为什么单独一个模块:折叠发生在 `KnowledgeLifecycleService.rebuild_communities` 里
(那里已经握着 canonical 整数边图 `ew` 与 Louvain 的 membership),但它是**纯计算**——
不碰连接、不拼 SQL、不判 dialect。抽出来的收益有三个:

1. **两个后端拿到逐字相同的产物行。** store 只负责把这里算出的行落库,
   SQLite / PostgreSQL 不各自实现一份折叠,parity 因此是结构性的。
2. **可单测。** 头部板块集合的边界(恰好压线 / 全空 / 并列)与来源画像的并列消歧,
   都能不起库直接钉住。
3. **确定性。** GROUP BY 的行到达顺序在两个后端上都是未定义的,所以「最集中的板块」
   在计数并列时必须有一个与到达顺序无关的消歧规则(见 `SourceProfileFolder`)。

口径与 T1 的只读聚合一致(设计 §3.5):对象只算 `USABLE_STATUSES`;不挂来源的对象
(`source_id=''`)不进来源画像——它们共享同一个空 source_id,算进去等于伪造一个
「空来源」。**隐藏合成来源**(`source_type IN ('memory','knowhow')`)同样不进来源画像:
产品其余各处一律把 Memory 与 knowhow 投影当隐藏源,它们的标题是用户内容而不是元数据,
而且它们天生只连自己那一小片、会把「与主体板块最不连通的来源」这张榜的头部占满。排除
发生在 store 的那一次扫描里(`source_community_counts`),所以本模块折叠到的行里已经没有
它们,`sources = len(profiles)` 与读侧分页的 `total` 因此仍是同一个口径。
⚠ 排除的判据是「来源存在且类型隐藏」——**孤儿引用**(`source_id` 指向已删来源)照常进
画像,读侧靠 `source_missing` 报出来,那是本视图有意的诊断能力。
被排除的量由三条统计快照单独报出,不在这里凭空消失。

⚠ **账本 payload 的计数单位不统一,读的人必须知道是哪一种**(T4 的卡片最容易在这里
做错):

  · `head_members` / `total_members`(source_profiles 账本)是 **canonical 计数** ——
    它们数的是主题板块里的 canonical 节点,一个 canonical 可能由几十个对象合并而来。
  · `sources`(source_profiles 账本)是 **来源个数** —— `len(profiles)`,来源画像
    折叠结果里每个来源一行,数的是有画像的来源数,不是对象数。
  · 明细表 `kg_source_profiles` 里的 `n_objects` / `n_graph_objects` 才是
    **对象计数**(`knowledge_objects` 的行,按来源分别统计)。
  · `communities` 是板块个数;`edges` / `edges_total` 是板块**对**的个数;
    `cross_weight` / `intra_weight` 是**关系行**的条数。

  同屏并列 `total_members` 与 `n_graph_objects` 会是 apples/oranges——前者恒小于后者
  (合并把多个对象压成一个 canonical),两者相除得不到任何有意义的比例。
"""
from __future__ import annotations

import heapq
from typing import (
    Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple,
)


# 「全库头部板块集合」的覆盖阈值:按 size 降序累积覆盖到该 notebook **50%** canonical
# 成员为止的那些板块,就是「主体板块」。跨过阈值的那个板块**计入**头部集合(所以头部
# 集合恒覆盖 >= 50%,不是 <= 50%)。
#
# 0.5 是一个**产品口径参数**,不是调参得来的魔法数:它把「一半以上的知识落在哪几个
# 板块」定义成主体,其余是长尾。它会随产物一起写进账本 payload,报告因此可以自证
# 自己用的是哪个阈值 —— 日后改阈值,旧产物不会被误读成新口径。
MAINSTREAM_COVERAGE = 0.5

# 最大簇榜单快照持久化的条数(设计里 C1/A2 的展示口径就是 top 20)。
PRECOMPUTED_LARGEST_CLUSTERS = 20

# `kg_community_edges` 明细表**每次预计算最多落库的行数**(按 weight 降序取)。
#
# 为什么必须有上界:跨板块边的条数没有任何结构性约束——它随图密度增长,而生产 base 库
# 是 836 万边 / 约 171 万 canonical 成员(边/节点比 ≈ 4.9)。本机三个样本的比值只有
# 1.4,且「每板块跨边数」在样本里是 0 → 0.75 → 0.41(不单调),**撑不起任何外推**:
# 那三个点既不能证明生产上是 1%,也不能证明不是 15%。取 5~15% 这档,明细表就是 40~130
# 万行,而落库那一刻 `cross` dict 与截断后的行列表同时活着。这正是
# #340/#342/#347/#351/#352/#354 那条 OOM 轨道盯着的同一个库、同一个峰值时刻。
#
# ⚠ **这个上限只管「写出去多少」,管不了「算的时候占多少」**(codex 第 7 轮评审)。
# 聚合阶段的 `cross` dict 会长到**全部**跨板块对那么大,而那个规模同样没有结构性上界。
# 所以有界性是**另一件事**、由另一个机制保证:`CrossCommunityEdgeFolder.drain` 边消费
# 边释放 `ew`,聚合期间两份结构的条目数之和恒等于 `ew` 的初始条目数(见那段说明)。
# 两个机制缺一不可,别把其中一个当成另一个的替代。
#
# 20 万这个值的依据是**用途**而不是实测分位数(生产上没有可测的数据点,见上):俯瞰图
# 本来就只画 top-N 个板块与它们之间最重的边,20 万条已经比任何可读的图多两个数量级。
# 截断**绝不静默**:账本 payload 记 `edges`(落库行数)、`edges_total`(截断前的板块对
# 总数)、`truncated` 与 `edge_limit`,`cross_weight` 则始终是**全部**跨板块边权之和
# (它是一个图统计量,不该随展示上限变化)。
MAX_PERSISTED_COMMUNITY_EDGES = 200_000

# 产物账本 `kg_analysis_artifacts.kind` 的**全部**合法取值。
# 前三条的 payload 就是产物本身(T1 三个只读聚合的返回载荷);后两条是明细表
# (kg_community_edges / kg_source_profiles)的账本行,payload 只放汇总与口径参数。
#
# ⚠ 账本行的**存在与否**才是「这份产物在不在」的唯一判据,明细表的行数不是:
# 单一板块的图 legitimately 产出 0 条跨板块边,靠行数分不出「空」与「没算过」。
ARTIFACT_CLUSTER_HISTOGRAM = "cluster_size_histogram"
ARTIFACT_LARGEST_CLUSTERS = "largest_clusters"
ARTIFACT_RELATION_PROVENANCE = "relation_provenance"
ARTIFACT_COMMUNITY_EDGES = "community_edges"
ARTIFACT_SOURCE_PROFILES = "source_profiles"
ARTIFACT_KINDS = (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_SOURCE_PROFILES,
)
_ARTIFACT_KINDS = frozenset(ARTIFACT_KINDS)

# **唯一**允许合法缺席的产物。一个板块都没有的库(对象有、关系没有,或者关系全被
# min-size 过滤掉)算不出任何来源画像:每个来源的 `mainstream_share` 都会是 0.0,
# 而那张表单独看是在说「所有来源都关联稀疏」—— 一句彻头彻尾的谎话。账本 payload 里
# 的 `head_communities: 0` 分辨得出来,但明细表会被单独查询、单独渲染。
#
# 所以这一档的处置是**让产物真的缺失**(不写账本行、不写明细行),而不是写一张全 0 的
# 表再要求下游先读账本才敢信它。缺失是诚实的,全 0 不是。
OPTIONAL_ARTIFACT_KINDS = frozenset({ARTIFACT_SOURCE_PROFILES})
REQUIRED_ARTIFACT_KINDS = _ARTIFACT_KINDS - OPTIONAL_ARTIFACT_KINDS

# 依赖**板块划分**的两份产物 —— 它们的明细行(`kg_community_edges` /
# `kg_source_profiles`)直接存 `community_id`。
#
# ⚠ `replace_communities` 会**重铸板块 id**(`_new_id("cm")`),所以板块一被重写,
# 这两份的每一行立刻变成悬空引用。它们必须与重铸**同事务**作废,否则:
#   · 报告会把已经不存在的板块画进俯瞰图、列进来源画像的「最集中板块」;
#   · T3 的记忆化签名(state 的 seq + 账本行的 seq/created_at)一个字段都不会变 ——
#     `force=True` 在**同一个** `kg_mutation_seq` 上重铸板块、而随后的预计算又恰好
#     失败时,已预热的缓存会**无限期**继续吐上一套板块,直到 LRU 淘汰或进程重启。
#     作废账本行同时也就动了签名,这是这份缓存唯一 O(1) 的失效手段:签名里放不下
#     「板块世代」——任何从 `communities` 现算的世代标记都是 O(板块数) 的读,而那正是
#     这份缓存要省掉的那笔钱(生产 88 580 个板块)。
#
# 另三份(三条统计快照)与板块划分**无关**,刻意不在此列:作废它们只会平白丢掉一份
# 仍然可读、只是「落后 N 次变更」的快照。
#
# ⚠ 这两份的簇世代戳记的是**板块划分建在哪一代合并结果上**,不是「折叠时读到的
# cluster_map 是哪一代」——两者在「只补账本」那条路径上不是同一个数,而把后者当前者
# 盖上去,就是 codex 第 7 轮评审的 P2。判据与写法见 `stamp_cluster_seq`。
BOARD_DEPENDENT_ARTIFACT_KINDS = (ARTIFACT_COMMUNITY_EDGES, ARTIFACT_SOURCE_PROFILES)
_BOARD_DEPENDENT_ARTIFACT_KINDS = frozenset(BOARD_DEPENDENT_ARTIFACT_KINDS)

# 依赖**簇世代**(`unified_kg_state.cluster_mutation_seq`)的产物。
#
# ⚠ 为什么这是一条独立于 `kg_mutation_seq` 的判据:`concept_clusters` 的写路径
# (`write_clusters` / `append_clusters` / rebuild 的 cluster-map swap)**刻意不动**
# `kg_mutation_seq` —— 合并是幂等的重算,让它抬 KG 世代会把每一次「整理」都伪装成
# 一次内容变更。变化信号独立成列(见 `unified_kg_store.bump_cluster_seq`)。于是只看
# `kg_mutation_seq` 的新鲜度契约对**整条簇写路径完全失明**:簇变了、直方图与收敛率
# 跟着变了,而账本闸短路、读侧报「与当前一致」。codex 第 5 轮评审报的就是这一条。
#
# 分类的依据是**那几条查询到底读没读 `concept_clusters`**,不是「感觉上相关」:
#   · cluster_size_histogram  `FROM concept_clusters c LEFT JOIN knowledge_objects o
#                              … GROUP BY c.object_type, c.canonical_id` —— 它数的就是
#                              这张表的行;
#   · largest_clusters        `FROM concept_clusters c JOIN knowledge_objects o …
#                              WHERE c.object_type='concept' GROUP BY c.canonical_id`;
#   · community_edges         折叠的输入 `ew` 来自 `community_graph_rows`,那条
#                              `LEFT JOIN concept_clusters cs/ct` 把每条边的两端映射到
#                              canonical —— 簇一变,同一批关系折出来的板块对就不同;
#   · source_profiles         `source_community_counts` 的 `COALESCE(c.canonical_id,
#                              o.id)` 同样经 `concept_clusters` 再 join
#                              `community_members`。
#
# **`relation_provenance` 刻意不在此列**,而且这不是省事:它的 SQL 只有
# `knowledge_relations` 全扫 + 两次 `knowledge_objects` 端点探查,一个字都没提
# `concept_clusters`(见两侧 store 的 `relation_provenance_counts`)。它同时是五份里
# **最贵**的一份(生产 836 万边、每行两次随机 PK 探查,与仓库那次「835 万边冷扫
# 39 分钟」同量级)。把它一刀切进作废集合,等于每一次纯合并写入都白付一趟全表扫,
# 而它的输入一个字节都没变。
CLUSTER_DEPENDENT_ARTIFACT_KINDS = frozenset({
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_SOURCE_PROFILES,
})

# 簇世代盖在账本 payload 里的字段名。
#
# ⚠ **刻意不加列。** `kg_analysis_artifacts` 的 `payload` 两侧分别是 JSON 文本与
# jsonb,加字段不需要迁移;而加一列要追加 `_migration_N` + bump `SCHEMA_VERSION`,
# 波及全仓的迁移计数断言。世代是**每行**盖的(不是整批一个),这样每一份产物都能
# 独立回答「我建在哪一代合并结果上」—— 与 `kg_mutation_seq` 那一列同样的粒度。
CLUSTER_SEQ_PAYLOAD_KEY = "built_at_cluster_seq"


def stamp_cluster_seq(
    payloads: Mapping[str, dict], cluster_seq: int, *, partition_rebuilt: bool
) -> Dict[str, dict]:
    """把簇世代盖进**依赖簇**的那几份 payload,返回新的 payload 表。

    只盖 `CLUSTER_DEPENDENT_ARTIFACT_KINDS`:给 `relation_provenance` 也盖一个,读侧
    就会替它报一个它根本不依赖的落后量,而那是**假的新鲜度信息** —— 一次纯合并写入
    之后它会显示「落后 1 代合并」,可它的两个输入表一个字节都没变。

    刻意在 service 侧盖、在 store 侧(`check_artifact_payloads`)查:两件事放同一个
    函数里,守卫就恒真了 —— 那正是本 PR 已经抓出 15 个的「空守卫」形态。

    ⚠ **``partition_rebuilt`` 分开了两种「建在哪一代合并结果上」**(codex 第 7 轮评审
    的 P2)。两条统计快照(簇大小直方图 / 最大簇榜单)当场从 `concept_clusters` 重算,
    它们的世代**就是** ``cluster_seq``,没有第二种可能。依赖板块的那两份不是:

      · ``partition_rebuilt=True``(全量重建那一轮):板块划分刚在这一代合并结果上跑
        出来,折叠用的 canonical 边图也是这一代 —— 戳 ``cluster_seq``,名副其实。
      · ``partition_rebuilt=False``(「只补账本」那条路径):板块划分是**库里现成的**,
        它建在哪一代合并结果上**没有地方记**(`communities` / `unified_kg_state` 都没有
        这一列,见设计 §3.3 的已知缺口)。而边与来源映射是按**当前**的 cluster_map 现
        算的 —— 产物因此是个混合世代的东西。给它盖上当前簇世代,报告就会说「与当前
        一致」,那比「陈旧但内部自洽」更糟:陈旧至少是诚实的。所以这一档显式记
        ``None`` = 无从判断。

    为什么是**显式 None** 而不是干脆不写这个键:不写的话「忘了盖」与「盖不出来」在读
    侧长得一模一样,`check_artifact_payloads` 那道「漏盖必须硬失败」的守卫也就跟着废了。
    键必须在,值可以是 None —— 两个后端的 JSON / jsonb 都存得下 null。

    为什么不改成「簇世代一变就重建板块划分」(codex 给的另一条路):那与设计
    §3.3 明写的决定相反(让板块跟上合并结果的是 `rebuild_unified_kg` 收尾那次
    `force=True`),而且要在**恰恰是为了避免那笔钱才存在**的补账本路径上,付一次
    生产 836 万边的 Louvain + 171 万成员行重写。更硬的一点:它治不了本条 —— 账本
    整个为空(刚部署的 B1 形态)时根本没有证据判断簇世代动没动过,那一档只能靠
    「不知道就说不知道」。
    """
    return {
        kind: (
            {
                **payload,
                CLUSTER_SEQ_PAYLOAD_KEY: (
                    int(cluster_seq)
                    if partition_rebuilt or kind not in _BOARD_DEPENDENT_ARTIFACT_KINDS
                    else None
                ),
            }
            if kind in CLUSTER_DEPENDENT_ARTIFACT_KINDS
            else payload
        )
        for kind, payload in payloads.items()
    }


def artifact_cluster_seq(kind: str, payload: Mapping[str, object]) -> Optional[int]:
    """这份产物记下的簇世代;``None`` = 无从判断。

    三种 ``None``,前两种刻意不区分(**下游对它们的处置相同**):
      · 这个 kind 与簇世代无关(`relation_provenance`)—— 没有落后量可报;
      · 依赖簇却没盖戳(库被手工改过,或者是本次修复**之前**写下的行)—— 判不出
        它建在哪一代合并结果上,那就当它不新鲜:闸会补跑、读侧报「未知」。
    第三种要区分,由 `artifact_cluster_seq_is_unknown` 单独回答:依赖板块的两份**显式**
    记下 ``None``(板块划分是库里现成的,建在哪一代无从判断,见 `stamp_cluster_seq`)。
    它不是「漏盖」,补跑也补不出来,所以闸不能拿它当「要重算」。
    """
    if kind not in CLUSTER_DEPENDENT_ARTIFACT_KINDS:
        return None
    value = payload.get(CLUSTER_SEQ_PAYLOAD_KEY)
    # bool 是 int 的子类,`True` 会被 `isinstance(value, int)` 放行并当成 1。
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def artifact_cluster_seq_is_unknown(
    kind: str, payload: Mapping[str, object]
) -> bool:
    """这一份是不是**显式**记着「板块划分建在哪一代合并结果上无从判断」。

    只有依赖板块的两份可能是这一档(`stamp_cluster_seq` 只对它们写 ``None``),而且
    必须是**键在、值为 None**:键干脆不在是「漏盖」,值是别的类型是「被改坏」——
    那两种都判不新鲜,而这一档判「无从判断」。三者的差别是这条修复的全部要害,
    所以判据写在一处、由 `check_artifact_payloads` 在写入口反向封死。
    """
    return (
        kind in _BOARD_DEPENDENT_ARTIFACT_KINDS
        and CLUSTER_SEQ_PAYLOAD_KEY in payload
        and payload[CLUSTER_SEQ_PAYLOAD_KEY] is None
    )


def _cluster_generation_verdict(
    kind: str, payload: Mapping[str, object], cluster_seq: int
) -> Optional[bool]:
    """**合并世代那条线**单独的裁决(三值),给下面的核心当一个输入。

    ``True`` = 建在当前这一代合并结果上,或这个 kind 压根没有这条线
    (`relation_provenance` —— 它的 SQL 一个字都没提 `concept_clusters`);
    ``False`` = 明确落后,**或者**戳漏盖 / 被改坏(见 `artifact_cluster_seq`:判不出来
    就当它不新鲜,闸会补跑、读侧报陈旧);
    ``None`` = **显式**记着「无从判断」(依赖板块的两份由「只补账本」那条路径产出,
    板块划分建在哪一代合并结果上没有地方记,见 `stamp_cluster_seq`)。
    """
    if kind not in CLUSTER_DEPENDENT_ARTIFACT_KINDS:
        return True
    built_cluster_seq = artifact_cluster_seq(kind, payload)
    if built_cluster_seq is None:
        return None if artifact_cluster_seq_is_unknown(kind, payload) else False
    return built_cluster_seq == int(cluster_seq)


def _generation_verdict(
    built_seq: int, seq: int, cluster: Optional[bool]
) -> Optional[bool]:
    """两条世代线的**三值合取** —— 全特性唯一的新鲜度判据核心。

    KG 世代那条线在这里比(它永远判得出来:两边都是整数);合并世代那条线由调用方
    先裁决好传进来(可能是「无从判断」)。合取是 Kleene 的:任一条为假即假,否则有
    未知即未知,都真才真。

    ⚠ **三个消费方共用它,一个字都不许在别处重写**:产物(`artifact_is_current`)、
    板块划分(`board_partition_is_current`)、以及它们各自的写侧闸与读侧落后量。
    本 PR 已经因为「同一件事在两处各判一遍、然后分岔」被抓过三次(第 3 轮
    `_ledger_state` 与写侧 required 集合、第 6 轮分档漏判新维度、第 8 轮板块那一格
    仍在用只看 KG 世代的旧判据),所以判据落在这一个函数里。
    """
    if int(built_seq) != int(seq):
        return False
    return None if cluster is None else bool(cluster)


def artifact_is_current(
    kind: str,
    built_seq: int,
    payload: Mapping[str, object],
    *,
    seq: int,
    cluster_seq: int,
) -> Optional[bool]:
    """这一份产物是不是建在**当前**的 (KG 世代, 簇世代) 上 —— 唯一的判据函数。

    **三值**:``True`` = 两条世代线都对齐;``False`` = 至少一条明确落后;
    ``None`` = 没有任何一条明确落后,但有一条**无从判断**(依赖板块的产物由「只补
    账本」那条路径产出时,板块划分建在哪一代合并结果上没有地方记,见
    `stamp_cluster_seq`)。三值是 Kleene 合取:任一条为假即假,否则有未知即未知。

    ⚠ **写侧的闸与读侧的落后量必须共用它。** 闸判「要不要重算」、读侧判「落后多少」,
    两处一旦各写一份判据就会漂,而漂出来的表现是自相矛盾的报告:闸说新鲜、读侧说陈旧,
    或者反过来。本 PR 第 3 轮评审(`_ledger_state` 与写侧 required 集合分岔)就是同一
    个病的另一处发作,所以这一次直接把判据收成一个函数;两个消费方各自把第三值映射成
    自己那句话,而**映射规则也各只写一处**:
      · `analysis_ledger_is_current`(预计算的新鲜度闸):``is not False`` —— 未知那一
        档补跑也补不出来(补账本换不掉库里现成的板块划分),据它重算就是每次调用都白
        跑一遍全部重活,永不收敛;
      · `kg_analysis._artifact_freshness`(报告里的 `stale`):``None`` 原样透出去 ——
        「无从判断」既不是「与当前一致」也不是「落后 N 代」,替读者选一个就是编。

    与簇无关的 kind 只看 KG 世代;依赖簇的 kind 两个世代都要对齐,而**漏盖戳(键不在
    或值被改坏)仍然等于不新鲜**(见 `artifact_cluster_seq`)。
    """
    return _generation_verdict(
        built_seq, seq, _cluster_generation_verdict(kind, payload, cluster_seq)
    )


def board_partition_cluster_seq(
    ledger: Mapping[str, Mapping[str, object]]
) -> Optional[int]:
    """**板块划分**建在哪一代合并结果上;``None`` = 无从判断。

    ⚠ 记录点不是 `communities` / `unified_kg_state`(那里确实没有这一列 —— 设计 §3.3
    的已知缺口),而是**依赖板块的产物账本**里的 `built_at_cluster_seq`:第 7 轮起那个
    戳记的就是「板块划分建在哪一代合并结果上」,不是「折叠时读到的 cluster_map 是哪一
    代」(见 `stamp_cluster_seq` 的 ``partition_rebuilt`` 那段)。

    读它是**合法的**,而不是拿一份产物的属性去冒充另一件东西的属性:
      · `replace_communities` 重铸板块 id 的**同一个事务**里就作废这两行
        (`BOARD_DEPENDENT_ARTIFACT_KINDS`),所以「行在」⟹「它描述的正是当前这套划分」;
      · 那一轮划分是现算的(``partition_rebuilt=True``)就记整数,是沿用库里现成的
        (只补账本)就显式记 ``None``。

    账本行缺席(从没算过 / 重铸后预计算失败)同样是 ``None``:那时确实没有任何证据。
    """
    entry = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    if entry is None:
        return None
    return artifact_cluster_seq(
        ARTIFACT_COMMUNITY_EDGES, entry["payload"]        # type: ignore[arg-type]
    )


def board_partition_is_current(
    ledger: Mapping[str, Mapping[str, object]],
    built_seq: int,
    *,
    seq: int,
    cluster_seq: int,
) -> Optional[bool]:
    """**板块划分**是不是建在当前的 (KG 世代, 簇世代) 上 —— 与产物**同一套**判据。

    ``built_seq`` 是 `unified_kg_state.community_seq`(板块建于哪个 KG 状态);合并世代
    那条线由 `board_partition_cluster_seq` 的那个记录点回答,裁决走与产物逐字相同的
    `_cluster_generation_verdict`,合取走同一个 `_generation_verdict`。

    ⚠ **为什么必须共用而不是在读侧另写一份**(codex 第 8 轮 P2):簇写路径刻意不动
    `kg_mutation_seq`,所以纯合并写入之后 `community_seq` 与 `kg_mutation_seq` 仍然
    相等 —— 只比 KG 世代的旧判据据此报「与当前一致」,而同一屏上依赖板块的两份产物
    (第 7 轮修复后)已经如实报「对不上合并进度」。同一件事(这套板块划分建在哪一代
    合并结果上)在两处各判一遍,然后分岔成两句互相矛盾的话,而这正是本视图存在的
    理由的反面。判据合流之后,那两份产物与板块那一格拿的是**同一个** `built_at_cluster_seq`,
    结构上不可能再分岔。
    """
    return _generation_verdict(
        built_seq,
        seq,
        (
            None
            if (entry := ledger.get(ARTIFACT_COMMUNITY_EDGES)) is None
            else _cluster_generation_verdict(
                ARTIFACT_COMMUNITY_EDGES,
                entry["payload"],                         # type: ignore[arg-type]
                cluster_seq,
            )
        ),
    )


def reusable_artifact_payloads(
    ledger: Mapping[str, Mapping[str, object]], seq: int, cluster_seq: int
) -> Dict[str, dict]:
    """账本里**可以原样搬到这一轮**的 payload —— 只可能是与簇无关的那几份。

    为什么这不是投机取巧:`relation_provenance` 的两个输入表(`knowledge_relations` +
    `knowledge_objects`)的**每一条**写路径都会 bump `kg_mutation_seq`(那正是
    `unified_kg_store.graph_seq_row` 逐条核过的覆盖面),所以「账本行的 seq == 这一轮
    的 seq」⟹ 它的输入一个字节都没变 ⟹ 重算必然得到逐字相同的载荷。这与新鲜度闸赖以
    成立的是**同一条**不变式;闸敢据它跳过整轮预计算,这里就敢据它跳过一份产物。

    收益不是理论上的:纯合并写入(`write_clusters` / `append_clusters` / 整理时的
    cluster-map swap)不动 `kg_mutation_seq`,所以簇世代闸新触发的每一次补账本,若不
    复用就要白跑一趟 `relation_provenance` 的全表扫 —— 生产 836 万边、每行两次随机
    PK 探查,与仓库那次「835 万边冷扫 39 分钟」同量级。

    ⚠ **`force=True` 不复用**(由调用方决定不传本函数的结果)。`force` 是「用户明确
    要求重算」,是抽取口径改了、代码修了之后唯一的人工恢复手段;在它上面省这一趟,
    等于宣布一份按旧代码算出的载荷只要 KG 没变就永远换不掉。省下的那点钱远不值。

    ⚠ **「这一份还是当前的吗」也走 `artifact_is_current`,不在这里手抄一遍**。这里曾经
    内联 `int(entry["kg_mutation_seq"]) == int(seq)`;对与簇无关的 kind 而言那**恰好**
    等价(它们没有第二条世代线),但「恰好等价的第二份判据」正是本 PR 反复被打回的那个
    形态 —— 第 3 轮、第 6 轮、第 8 轮各发作过一次,而且每次都是从「现在明明一样」开始的。
    两个筛选条件因此分工明确:``kind not in CLUSTER_DEPENDENT_ARTIFACT_KINDS`` 是**策略**
    (只有与簇无关的载荷才允许原样搬),`artifact_is_current(...) is True` 是**判据**
    (它是不是建在当前世代上),判据只此一处。
    """
    return {
        kind: dict(entry["payload"])                # type: ignore[arg-type]
        for kind, entry in ledger.items()
        if kind not in CLUSTER_DEPENDENT_ARTIFACT_KINDS
        and artifact_is_current(
            kind,
            int(entry["kg_mutation_seq"]),          # type: ignore[arg-type]
            entry["payload"],                       # type: ignore[arg-type]
            seq=seq,
            cluster_seq=cluster_seq,
        ) is True
    }


def check_artifact_payloads(payloads: Mapping[str, dict]) -> None:
    """账本写入口的守卫:**多写**与**少写**都硬失败。

    只拒未知 kind 是不够的。「产物在不在 = 账本行在不在」这条判据依赖五行(空板块库
    是四行)一起写出来;少写一行,下游看到的是「这份产物从来没算过」,而实际上是这一轮
    忘了算 —— 两者在读侧完全无法区分,而且没有任何报错。

    ``ARTIFACT_SOURCE_PROFILES`` 是唯一允许缺席的一份,理由见
    ``OPTIONAL_ARTIFACT_KINDS``;而且它只在**真的一个板块都没有**时才可以缺席,
    这一条由 `community_edges` 账本里的 ``communities`` 计数当场复核 —— 否则
    「允许缺席」就会变成「随便漏一份也不报错」。

    **簇世代的戳同样是硬要求**:依赖簇的四份必须带 ``built_at_cluster_seq`` 这个键
    (由 service 侧的 `stamp_cluster_seq` 盖),不依赖簇的 `relation_provenance` 必须
    **不带**。漏盖的表现极其隐蔽 —— 读侧只会把它报成「合并世代未知」、闸每次都判它不
    新鲜,于是这个库的预计算**永远**重跑,而没有任何报错;多盖则相反,给一份根本不依赖
    合并的产物编造一个落后量。两个方向都在这里拦。

    ⚠ 值的类型分两档(codex 第 7 轮 P2):两条统计快照当场从 `concept_clusters` 重算,
    世代永远是知道的,**必须是 int**;依赖板块的两份可以是 ``None``(= 板块划分是库里
    现成的、建在哪一代无从判断),但**键必须在** —— 允许「键干脆不写」就等于把「漏盖」
    和「盖不出来」揉成一档,上面那道守卫当场作废。
    """
    unknown = sorted(set(payloads) - _ARTIFACT_KINDS)
    if unknown:
        raise ValueError(
            f"KG 分析产物账本:契约外的 kind {unknown};"
            f"合法取值见 app.services.kg_analysis_precompute.ARTIFACT_KINDS"
        )
    missing = sorted(REQUIRED_ARTIFACT_KINDS - set(payloads))
    if missing:
        raise ValueError(
            f"KG 分析产物账本:缺少必需的 kind {missing}。账本是整批重写的,"
            "少写一行 = 下游把它读成「从来没算过」,而且不会有任何报错"
        )
    if ARTIFACT_SOURCE_PROFILES not in payloads:
        boards = int(payloads[ARTIFACT_COMMUNITY_EDGES].get("communities", 0) or 0)
        if boards:
            raise ValueError(
                f"KG 分析产物账本:{ARTIFACT_SOURCE_PROFILES} 只有在一个主题板块都没有时"
                f"才允许缺席,但这一轮有 {boards} 个板块"
            )
    for kind in sorted(payloads):
        payload = payloads[kind]
        stamped = CLUSTER_SEQ_PAYLOAD_KEY in payload
        depends = kind in CLUSTER_DEPENDENT_ARTIFACT_KINDS
        known = artifact_cluster_seq(kind, payload) is not None
        unknown = artifact_cluster_seq_is_unknown(kind, payload)
        if depends and not (known or unknown):
            raise ValueError(
                f"KG 分析产物账本:{kind} 依赖合并结果,payload 必须带 "
                f"{CLUSTER_SEQ_PAYLOAD_KEY}(见 stamp_cluster_seq):两条统计快照是整数,"
                "依赖板块的两份可以是 None(板块划分建在哪一代无从判断)但键必须在。"
                "漏盖不会报错,只会让这个库的预计算永远重跑、读侧永远报「合并世代未知」"
            )
        if not depends and stamped:
            raise ValueError(
                f"KG 分析产物账本:{kind} 不依赖合并结果,不该带 "
                f"{CLUSTER_SEQ_PAYLOAD_KEY} —— 盖了它,读侧就会替一份输入根本没变的"
                "产物编造一个落后量"
            )


def required_artifact_kinds(*, has_boards: bool) -> set:
    """这一轮**必须**出现在账本里的 kind 集合。

    「有板块 → `source_profiles` 是必需的」这条判据有三个执行点:写入口
    `check_artifact_payloads`(少写就硬失败,它刻意分成两条错误文案,所以自己展开这条
    判据而不调本函数)、预计算的新鲜度闸 `analysis_ledger_is_current`(缺就补跑)、
    以及 T3 报告里的「账本齐不齐」(`kg_analysis._ledger_state`)。后两处共用本函数。

    为什么必须共用:各写一遍就会漂,而漂出来的表现是一份**自相矛盾的报告** —— 同一份
    产物被 `_absence` 判成「本该有却缺失」(红档),却同屏被账本档位判成「齐全」。
    codex 第 3 轮评审就是这么复现的(`_ledger_state` 当时只看四份必需的)。

    ``has_boards=False``(一个板块都没有)时 `source_profiles` 合法缺席,理由见
    `OPTIONAL_ARTIFACT_KINDS`:那一档必须仍然算「齐全」,否则空板块库永远齐不了,
    新鲜度闸也会让它每次调用都重跑一遍全部重活。
    """
    required = set(REQUIRED_ARTIFACT_KINDS)
    if has_boards:
        required.add(ARTIFACT_SOURCE_PROFILES)
    return required


def analysis_ledger_is_current(
    ledger: Mapping[str, Mapping[str, object]],
    seq: int,
    cluster_seq: int,
    *,
    has_boards: bool,
) -> bool:
    """账本是否已经**齐全地**建在当前的 (KG 世代, 簇世代) 上。

    这是预计算**自己的**新鲜度闸,与「社区图要不要重建」是两件事(见
    `rebuild_communities` 里的 B1 说明)。判据只看账本:齐不齐、戳对不对。
    任一必需 kind 缺席、或任一行不满足 `artifact_is_current` → 不新鲜,要补跑。

    两条判据都是**共享的**,一个字都不在这里重写:
      · 「齐不齐」= `required_artifact_kinds`(与写入口、T3 的账本档位同一条);
      · 「戳对不对」= `artifact_is_current`(与 T3 报告里的 `stale` 同一条)。

    ⚠ 判据是三值的,这里只把 ``False`` 当「要补跑」。第三值(``None`` = 依赖板块的
    产物由补账本路径产出、板块划分建在哪一代无从判断)**补跑也补不出来**:补账本用的
    还是库里现成的那套划分,再跑一遍只会写出一份同样无从判断的产物。据它重算 = 每次
    调用都白跑一遍全部重活、永不收敛,而那正是这道闸当初为 B1 加进来时要避免的形态。
    真正让它变回「有据可查」的是板块被重建的那一轮(KG 变了,或者 `force=True`)。

    ``ledger`` 是 `kg_analysis_artifact_rows` 的返回形状
    (``{kind: {kg_mutation_seq, payload, created_at}}``)—— 簇世代盖在 payload 里
    (刻意不加列,见 `CLUSTER_SEQ_PAYLOAD_KEY`),所以闸必须拿到 payload 才判得了。
    """
    if not required_artifact_kinds(has_boards=has_boards) <= set(ledger):
        return False
    return all(
        artifact_is_current(
            kind,
            int(entry["kg_mutation_seq"]),          # type: ignore[arg-type]
            entry["payload"],                       # type: ignore[arg-type]
            seq=seq,
            cluster_seq=cluster_seq,
        ) is not False
        for kind, entry in ledger.items()
    )


def head_community_ids(
    sizes: Sequence[Tuple[str, int]], coverage: float = MAINSTREAM_COVERAGE
) -> Tuple[frozenset, int, int]:
    """按 size 降序累积覆盖 ``coverage`` 比例的板块集合。

    返回 ``(头部板块 id 集合, 头部覆盖的成员数, 全部成员数)``。成员数是
    **canonical 计数**(见模块头的单位说明),不是对象计数。

    排序键是 ``(-size, community_id)`` —— 并列时按 id 升序,与仓库里其它「规模降序 +
    id 升序」的读取口径(`community_member_ids` / `community_overview`)对齐,也让结果
    与 `kept_rows` 的构造顺序无关。

    跨过阈值的那个板块**计入**头部:否则 coverage=0.5 在「两个等大板块」上会返回空集
    (第一个板块只覆盖 50%,严格大于才收就一个都不收),而那显然不是「主体板块」的意思。

    全库为空(没有板块、或板块全空)时返回空集与 0/0 —— 调用方据此**整份跳过**来源
    画像(见 `OPTIONAL_ARTIFACT_KINDS`),而不是写一张 mainstream_share 全 0 的表。
    """
    total = sum(max(0, int(size)) for _cid, size in sizes)
    if total <= 0:
        return frozenset(), 0, 0
    threshold = coverage * total
    head: List[str] = []
    covered = 0
    for community_id, size in sorted(sizes, key=lambda item: (-int(item[1]), item[0])):
        if covered >= threshold:
            break
        head.append(community_id)
        covered += max(0, int(size))
    return frozenset(head), covered, total


class CrossCommunityEdgeFolder:
    """把逐条 canonical 边按 membership 折成**跨板块**边的累加器。

    两条喂入路径共用同一个累加器,所以两条路径产出的 `kg_community_edges` 逐字相同:

      · 全量重建:`fold_cross_community_edges` 直接遍历 `rebuild_communities` 手里的
        整数边权 `ew`(这一步不新起任何扫描);
      · 只补账本(见 `rebuild_communities` 的 B1 说明):按 canonical 逐行喂,每行
        weight=1。`ew` 只是「同一对 canonical 出现了几次」的预聚合,逐行累加与先聚合
        再折叠对每个板块对的合计**恒等**,所以两条路径不会给出不同的产物。

    ⚠ **内存:聚合阶段本身必须有界,落库上限管不了它**(codex 第 7 轮评审)。
    `_cross` 的条目数没有任何结构性上界 —— Louvain 的目标函数倾向于减少跨社区边,但
    「倾向」不是保证,而只补账本那条路径用的还是**库里现成的**划分(它建在另一代合并
    结果上,跨板块比例可以任意高)。最坏情况下 `_cross` 与 `ew` 同量级:生产 836 万条
    int-tuple 键、约 1.4 GB。两份同时驻留就是在 437 GB 库的峰值时刻再叠一个峰值。

    所以喂入走 `drain`:**每消化一条 `ew` 的条目就释放一条**,聚合期间
    ``len(ew) + len(_cross)`` 恒 ≤ `ew` 的初始条目数,而且 `_cross` 的条目更便宜
    (键是两个**已存在**的板块 id 字符串的引用,不像 `ew` 的键那样每条都是一个新分配的
    int 二元组)。换句话说:折叠**不新增**同量级的结构,只是把 `ew` 就地换成它的折叠结果。

    刻意**不**做 DB 侧聚合:SQLite 的 `PRAGMA temp_store = MEMORY`(见
    `repositories/sqlite/database.py`)让 GROUP BY 的临时 B 树也落在进程内存里,一分钱
    没省,还要为 836 万行的临时表按住写锁;而设计 §3.35 给 SQLite 侧定的标准正是
    「正确、有界、不 OOM、不长时间持锁」。也刻意**不**做分批 + 归并:那需要外部排序的
    脚手架,而 `total_edges` / `top_edges` 要求**精确**(账本的 `edges_total` 与俯瞰图的
    边权都不能是近似值),分批只会把复杂度换个地方放。

    落库前再按 weight 降序截到 ``MAX_PERSISTED_COMMUNITY_EDGES``(`top_edges`,
    `heapq.nsmallest` 的峰值是 O(limit),不是 O(板块对数))—— 绝不为了排序再造一份
    完整列表。
    """

    __slots__ = ("_cross", "_intra", "_cross_weight")

    def __init__(self) -> None:
        self._cross: Dict[Tuple[str, str], int] = {}
        self._intra = 0
        self._cross_weight = 0

    def add(
        self, src: Optional[str], dst: Optional[str], weight: int
    ) -> None:
        """累加一条(已映射到板块的)边。``None`` = 该端点不属于任何保留下来的板块。

        落选板块(min-size 过滤掉的)与图外 canonical 的边被自然丢弃 —— 它们在
        `communities` 里也不存在,落进明细表就是悬空引用。
        """
        if src is None or dst is None:
            return
        weight = int(weight)
        if src == dst:
            self._intra += weight
            return
        key = (src, dst) if src <= dst else (dst, src)   # 无向归一
        self._cross[key] = self._cross.get(key, 0) + weight
        self._cross_weight += weight

    def drain(
        self,
        edge_weights: MutableMapping[Tuple[int, int], int],
        community_of_index: Sequence[Optional[str]],
    ) -> None:
        """把 ``edge_weights`` **就地搬空**进本累加器 —— 消化一条、释放一条。

        ⚠ **这是破坏性的,而且必须是。** 遍历 + 保留(``for … in ew.items()``)会让
        整张 `ew`(生产 836 万 int-tuple 键、约 1.4 GB)在 `_cross` 长到同量级的整个
        过程中一直驻留,峰值就是两者之和。`popitem` 是 dict 上唯一能「边遍历边删」的
        操作(``for`` 循环里删会 `RuntimeError`),它 O(1)、不触发缩表,取出的键值对
        一旦被 `add` 消化就立刻可回收。

        结果与遍历版**逐字相同**:每个板块对的合计是整数加法(可交换可结合),而
        `top_edges` 的排序键 ``(-weight, src, dst)`` 在板块对上是全序(无并列),
        所以消费顺序不进入产物。`test_cross_edge_folder_row_stream_matches_the_
        preaggregated_fold` 与两条喂入路径的等价性因此都不受影响。

        调用方(`_precompute_kg_analysis`)在这之后**不得**再用那张 dict;
        `rebuild_communities` 里 Louvain 早已跑完,`ew` 到这一步的唯一用途就是本次折叠。
        """
        while edge_weights:
            (left, right), weight = edge_weights.popitem()
            self.add(community_of_index[left], community_of_index[right], weight)

    @property
    def intra_weight(self) -> int:
        """两端落在同一板块的边权合计(板块内密度,给账本汇总用)。"""
        return self._intra

    @property
    def cross_weight(self) -> int:
        """**全部**跨板块边权合计——不受落库上限影响。"""
        return self._cross_weight

    @property
    def total_edges(self) -> int:
        """截断**前**的跨板块板块对总数。"""
        return len(self._cross)

    def top_edges(self, limit: int) -> List[Tuple[str, str, int]]:
        """按 ``(-weight, src, dst)`` 取前 ``limit`` 条,结果已定序。

        `heapq.nsmallest(n, it, key)` 等价于 `sorted(it, key=key)[:n]`(确定性),
        但峰值内存是 O(n) 而不是 O(板块对数)。返回的列表长度有硬上界,调用方据此
        流式喂给分批 `executemany`,全程只有这一份有界物化。
        """
        limit = max(0, int(limit))
        picked = heapq.nsmallest(
            limit, self._cross.items(), key=lambda item: (-item[1], item[0])
        )
        return [(key[0], key[1], weight) for key, weight in picked]


def fold_cross_community_edges(
    edge_weights: MutableMapping[Tuple[int, int], int],
    community_of_index: Sequence[Optional[str]],
) -> CrossCommunityEdgeFolder:
    """把 canonical 整数边图按 membership 折叠成跨板块边。

    ⚠ **`edge_weights` 会被搬空**(见 `CrossCommunityEdgeFolder.drain`):折叠不再是
    「读一张表、建另一张同量级的表」,而是把 `ew` 就地换成它的折叠结果。调用方在这之后
    不得再用它。

    ``community_of_index[i]`` 是节点下标 ``i`` 的板块 id(不属于任何保留板块则
    ``None``)。**刻意用按下标的 list 而不是 canonical→板块的 dict**:调用方手里已经
    有 `can2idx`,再造一份同基数(生产 ~171 万条)的 `str -> str` dict 就是在 `ew`
    还占着 1.4 GB 的那一刻凭空多几百 MB。list 的每个槽只是一个已存在的板块 id 字符串
    的引用,基数再大也只有指针数组本身的开销。

    效率:这是 `rebuild_communities` 已经握在手里的 `ew` 的一次**消费**,不新起任何扫描。
    Louvain 的目标函数本来就在最小化跨社区边,所以结果通常远小于 `ew`(本机真实库实测:
    49109 条唯一边 → 503 对跨板块)——但**那三个本机样本不足以外推到生产**,而且只补
    账本那条路径用的是库里现成的划分(见 `drain`),所以既不能靠它保证聚合有界(靠
    `drain`),也不能靠它保证落库有界(靠 `MAX_PERSISTED_COMMUNITY_EDGES`)。
    """
    folder = CrossCommunityEdgeFolder()
    folder.drain(edge_weights, community_of_index)
    return folder


class SourceProfileFolder:
    """把 ``(source_id, community_id, n)`` 的库内分组结果**流式**折成每来源一行。

    为什么是流式:分组结果的行数是「来源数 × 该来源触及的板块数」(本机真实库
    84 个来源 × 平均 26 = 2183 行;生产 4.8 万来源可以到百万级)。累加器只按**来源**
    开条目,所以 **Python 侧**内存是 O(来源数) 而不是 O(分组数) —— 每来源的板块计数在
    `add` 里当场消化掉,从不落成 dict。红线「绝不把百万行拉进 Python 聚合」因此成立。

    ⚠ **但「进程内存 O(来源数)」是假的,别这么读。** 分组发生在库内,而 SQLite 侧
    `PRAGMA temp_store = MEMORY`(见 `repositories/sqlite/database.py`)意味着那个
    `USE TEMP B-TREE FOR GROUP BY` 的临时 B 树**不能落盘**:进程还要额外扛下
    O(分组数) 的库内临时结构。这里省掉的是 Python 那一份,不是全部。同一段说明也适用于
    `cluster_size_histogram`(它有**两个** temp B 树,内层无条件约 200 万行)。

    字段口径(设计给定):
      n_objects        该来源的可用对象总数(含没进任何板块的)
      n_graph_objects  其中**落进主题板块**的对象数 —— 下面三个比例的分母
      top_community_id 该来源最集中的那个板块
      top_share        top 板块占 n_graph_objects 的比例
      community_spread 该来源散布到了多少个板块
      mainstream_share 落在全库头部板块集合里的比例

    ⚠ `n_graph_objects` 的分母口径:是「进了板块的对象」,不是 ``object_type='concept'``
    的对象。板块建在 canonical 概念图上,四类对象都可能作为端点进图;按类型切分母会让
    「该来源与主体有多连通」这个问题变成「该来源有多少 concept 型对象」,答非所问。
    分类型的量由簇大小直方图那份快照按 object_type 分组报出。
    (这个字段一度叫 `n_concepts` —— 名字与口径正相反,改名只为让名字说实话,口径一字未动。)

    ⚠ **并列消歧**:GROUP BY 的行到达顺序在两个后端上都是未定义的,所以计数并列时
    取 ``community_id`` 字典序更小的那个。没有这条,同一份数据在 SQLite 与 PostgreSQL
    上可以给出不同的 `top_community_id`,而两边都「没错」—— parity 会以最难查的方式碎掉。
    """

    __slots__ = ("_head", "_rows")

    def __init__(self, head_communities: frozenset) -> None:
        self._head = head_communities
        # source_id -> [n_objects, n_graph_objects, spread, top_id, top_n, mainstream_n]
        self._rows: Dict[str, list] = {}

    def add(self, source_id: str, community_id: "str | None", count: int) -> None:
        count = int(count)
        entry = self._rows.get(source_id)
        if entry is None:
            entry = [0, 0, 0, "", 0, 0]
            self._rows[source_id] = entry
        entry[0] += count
        if not community_id:
            # 该来源没进任何板块的那一撮(LEFT JOIN 的 NULL 组)。它只抬 n_objects,
            # 不进分母、不算 spread —— 否则「没进图」会被当成「散布到一个板块」。
            return
        entry[1] += count
        entry[2] += 1
        if count > entry[4] or (count == entry[4] and (
            not entry[3] or community_id < entry[3]
        )):
            entry[3] = community_id
            entry[4] = count
        if community_id in self._head:
            entry[5] += count

    def rows(self) -> List[Tuple[str, int, int, str, float, int, float]]:
        """按 source_id 升序返回落库行(顺序确定,便于两个后端逐字比对)。"""
        out: List[Tuple[str, int, int, str, float, int, float]] = []
        for source_id in sorted(self._rows):
            (n_objects, n_graph_objects, spread, top_id, top_n,
             mainstream_n) = self._rows[source_id]
            denominator = float(n_graph_objects) if n_graph_objects else 0.0
            out.append((
                source_id,
                n_objects,
                n_graph_objects,
                top_id,
                (top_n / denominator) if denominator else 0.0,
                spread,
                (mainstream_n / denominator) if denominator else 0.0,
            ))
        return out


def batched(rows: Iterable[tuple], size: int) -> Iterator[List[tuple]]:
    """把一个可迭代的落库行流切成定长批 —— 供两侧 store 的分批 `executemany` 共用。

    存在的理由是**不再物化第二份完整列表**:store 拿到的行本身可能已是一份有界物化
    (见 `CrossCommunityEdgeFolder.top_edges`),再 `[... for ... in edges]` 一遍就是
    在落库那一刻又多一份同样大的拷贝。
    """
    batch: List[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
