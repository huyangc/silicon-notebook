"""KG 质量分析预计算产物的**后端中性**契约与纯折叠逻辑(T2)。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md` §3.2/§3.3/§3.4。

为什么单独一个模块:折叠发生在 `KnowledgeLifecycleService.rebuild_communities` 里
(那里已经握着 canonical 整数边表与 Louvain 的 membership),但它是**纯计算**——
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
    Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple,
)

import numpy as np

# canonical 节点下标与边权的存储类型(设计 §3.32)。int32 是刻意的:节点下标的上界是
# canonical 成员数(生产 ~171 万),边权的上界是关系行数(生产 836 万),两者都离
# 2^31 有三个数量级的余量,而 int64 会让整张边表凭空翻一倍。
EDGE_INDEX_DTYPE = np.int32
EDGE_WEIGHT_DTYPE = np.int32

# 「这一列已经放掉了」的占位:共享同一个零长数组,不为每次释放再分配一个对象。
_EMPTY_INDEX_COLUMN = np.empty(0, dtype=EDGE_INDEX_DTYPE)


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
# 万行,而落库那一刻折叠结果与截断后的行列表同时活着。这正是
# #340/#342/#347/#351/#352/#354 那条 OOM 轨道盯着的同一个库、同一个峰值时刻。
#
# ⚠ **这个上限只管「写出去多少」,管不了「算的时候占多少」**(codex 第 7 轮评审)。
# 折叠结果的板块对数会长到**全部**跨板块对那么大,而那个规模同样没有结构性上界。
# 所以有界性是**另一件事**、由另一个机制保证:边表与折叠结果都是列式 int32 数组
# (设计 §3.32),同样条数下比 dict 小一个多数量级,中间量用完即释放,而且折叠一开始
# 就把输入边表 `release()` 掉。两个机制缺一不可,别把其中一个当成另一个的替代。
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
# ⚠ **但这个手段只在「确实有一行可作废」时成立**(codex 第 10 轮 P2):上一轮预计算失败
# 后账本可能整个为空、也可能只剩三条与板块无关的统计快照,那两种形态下这次作废是个
# **no-op** —— 板块换了一整套,签名一个字节没动。补法不在写侧(这里已经没有更多东西可
# 删了),而在读侧:`kg_analysis._signature_tracks_board_recasts` 在那一档干脆不写缓存。
# 改动本常量覆盖的集合时,那道判据自动跟着改 —— 它就是按这个集合导出的。
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
#   · community_edges         折叠的输入边表来自 `community_graph_rows`,那条
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


class CanonicalEdgeTable:
    """canonical 整数边图的**列式**表示:三个等长的 numpy 整数数组(设计 §3.32)。

    ``src[i] <= dst[i]``(无向归一),``(src[i], dst[i])`` 两两不同,``weight[i]`` 是
    这一对 canonical 在 `community_graph_rows` 里出现的次数。行序 = **首次出现序**,
    与它取代的那张 `dict[(int, int), int]` 的插入序逐字一致 —— 见 `CanonicalEdgeTableBuilder.build`
    里那段说明,那不是审美问题,Louvain 的输出会随边序变。

    为什么不是 dict(codex 第 2/7/9 轮连续三轮 P1 的共同根源,设计 §3.32):
    ``dict[(int, int), int]`` 的每条边都要一个新分配的 int 二元组 + 一对 int 对象 +
    哈希表槽位,生产 836 万边约 1.4 GB;而且**折叠它**必然要么造第二份同量级的 dict、
    要么依赖 `popitem`(它 O(1) 但**不缩哈希表**,源头照样占着)。三个 int32 数组
    把同一份数据压到约 100 MB,折叠则是纯向量化、中间量全是数组、用完即 `del`。
    本机 20 万边实测:dict 峰值 42.5 MB vs 三数组 2.4 MB。

    ⚠ 本对象是**一次性**的:`fold_cross_community_edges` 会在折叠开始时调用
    `release()` 把三个数组放掉(见那里的说明)。调用方在折叠之后不得再用它。
    """

    __slots__ = ("src", "dst", "weight", "n_nodes")

    def __init__(
        self,
        src: "np.ndarray",
        dst: "np.ndarray",
        weight: "np.ndarray",
        n_nodes: int,
    ) -> None:
        self.src = src
        self.dst = dst
        self.weight = weight
        self.n_nodes = int(n_nodes)

    def __len__(self) -> int:
        """唯一边(= canonical 对)的条数。"""
        return int(self.src.size)

    def igraph_edges(self) -> "np.ndarray":
        """igraph `Graph(n=…, edges=…)` 直接吃的 ``(M, 2)`` 整数数组。

        ⚠ 这是一份**拷贝**(`column_stack`),不是视图 —— 但它是**临时值**:按
        ``_ig.Graph(n=…, edges=table.igraph_edges())`` 这样写,igraph 把它拷进自己的
        C 结构之后这份就当场回收。第 7 轮评审抓到的 `list(ew.keys())` 别名(那份拷贝
        与 dict 共享同一批 key 元组、一个活着另一个一个字节都放不掉)在数组模型下**不
        存在**:column_stack 出来的是独立缓冲区,和 `src`/`dst` 没有任何共享。
        """
        return np.column_stack([self.src, self.dst])

    def release(self) -> None:
        """把三个数组换成空数组 —— 调用方栈帧上的那个引用因此不再钉住 GB 级缓冲。

        ⚠ 这是**必须**的,不是礼貌:`rebuild_communities` 把本对象作为**实参**传给
        `_precompute_kg_analysis`,调用方的局部名在整个调用期间都活着,被调用方
        `del` 自己那个形参一个字节也放不掉。唯一能在调用进行中放掉的办法,就是让
        持有者自己把引用换掉。语义上等价于旧模型里 `drain` 的「搬空 `ew`」。
        """
        self.src = np.empty(0, dtype=EDGE_INDEX_DTYPE)
        self.dst = np.empty(0, dtype=EDGE_INDEX_DTYPE)
        self.weight = np.empty(0, dtype=EDGE_WEIGHT_DTYPE)


class CanonicalEdgeTableBuilder:
    """按行喂入 canonical 节点下标对,攒出一张 `CanonicalEdgeTable`。

    喂入侧(`rebuild_communities` 的图扫描)本来就是 Python 逐行的,所以这里也逐行收;
    但收进的是**分块的 int32 缓冲**而不是 Python 容器:8.36 万…836 万条边在 list 里
    是「指针数组 + 每条一个 int 对象」,在 int32 块里就是 4 字节。块满了换新块,
    `build` 时逐块拷进一条连续数组、拷完一块放掉一块。

    ⚠ **一次性**:`build` 会把攒下的块全部消化掉,不能建第二次(第二次会拿到一张空表,
    而空表在这条路径上意味着「这个库一条边都没有」——一个静默的假答案)。
    """

    # 每块 6.5 万条 = 每块 256 KB × 2 列。块太小则拼接的块数多,块太大则小库(绝大多数
    # notebook 只有几千条边)白占内存。
    _CHUNK = 1 << 16

    __slots__ = ("_full", "_src", "_dst", "_fill", "_rows", "_built")

    def __init__(self) -> None:
        self._full: List[List["np.ndarray"]] = []
        self._src = np.empty(self._CHUNK, dtype=EDGE_INDEX_DTYPE)
        self._dst = np.empty(self._CHUNK, dtype=EDGE_INDEX_DTYPE)
        self._fill = 0
        self._rows = 0
        self._built = False

    def add(self, left: int, right: int) -> None:
        """喂一条边(两个 canonical 节点下标)。方向在这里归一,与旧 dict 键逐字同规则。"""
        if left > right:
            left, right = right, left
        if self._fill == self._CHUNK:
            self._full.append([self._src, self._dst])
            self._src = np.empty(self._CHUNK, dtype=EDGE_INDEX_DTYPE)
            self._dst = np.empty(self._CHUNK, dtype=EDGE_INDEX_DTYPE)
            self._fill = 0
        self._src[self._fill] = left
        self._dst[self._fill] = right
        self._fill += 1
        self._rows += 1

    def build(self, n_nodes: int) -> CanonicalEdgeTable:
        """聚合成唯一边表。**行序 = 首次出现序**,这是本方法唯一难的地方。

        ⚠ **Louvain 对边的输入顺序敏感。** 旧模型喂给 igraph 的是 `list(ew.keys())`,
        而 Python dict 保持插入顺序 —— 也就是每对 canonical 的**首次出现序**。数组化
        之后若按排序序或任何别的顺序喂,板块划分会漂:已部署库的 `communities`、
        `community_seq`、以及一切依赖板块的产物跟着全漂。这不是性能问题,是正确性问题
        (设计 §3.32 的不变量 1,有真实库的逐字比对)。

        所以聚合是「稳定排序 + 取每组最小原下标 + 按该下标重排」:
          1. 复合键 ``src * n_nodes + dst``(int64,``n_nodes`` 上界 ~171 万 → 键上界
             ~2.9e12,离 int64 有六个数量级余量);
          2. ``argsort(kind="stable")`` —— 稳定是**必需**的:它保证同键组内的原下标
             递增,于是每组第一个就是该边的首次出现位置;
          3. 组边界用相邻不等取,组内条数就是权重;
          4. 按「每组首次出现位置」再排一次,得到首次出现序。
        """
        if self._built:
            raise RuntimeError(
                "CanonicalEdgeTableBuilder.build 只能调一次:攒下的块已经被消化掉,"
                "再建一次只会拿到一张空表 —— 而空表在这条路径上意味着「一条边都没有」"
            )
        self._built = True
        n_nodes = int(n_nodes)
        total = int(self._rows)
        src_raw = self._drain_column(0)
        dst_raw = self._drain_column(1)
        if total == 0:
            return CanonicalEdgeTable(
                np.empty(0, dtype=EDGE_INDEX_DTYPE),
                np.empty(0, dtype=EDGE_INDEX_DTYPE),
                np.empty(0, dtype=EDGE_WEIGHT_DTYPE),
                n_nodes,
            )
        key = src_raw.astype(np.int64)
        key *= max(n_nodes, 1)
        key += dst_raw
        order = np.argsort(key, kind="stable")
        sorted_key = key[order]
        del key
        starts = _group_starts(sorted_key)
        del sorted_key
        counts = np.diff(starts, append=total).astype(EDGE_WEIGHT_DTYPE)
        first_seen = order[starts]          # stable 排序 ⇒ 组内最小原下标 = 首次出现
        del order, starts
        by_first_seen = np.argsort(first_seen)
        picked = first_seen[by_first_seen]
        del first_seen
        table = CanonicalEdgeTable(
            src_raw[picked], dst_raw[picked], counts[by_first_seen], n_nodes
        )
        return table

    def _drain_column(self, column: int) -> "np.ndarray":
        """把某一列的全部块拷进一条连续数组,**拷完一块就放掉一块**。

        刻意不用 `np.concatenate(parts)`:那要求全部块与结果同时在场(峰值 2M),
        而逐块拷 + 当场丢引用的峰值是「结果 + 尚未拷完的块」。返回的是**独立缓冲**,
        不是建表块的视图 —— 视图会让整块 65536 槽的缓冲跟着结果一起活下去。
        """
        buffers = self._full
        tail = (self._src if column == 0 else self._dst)[: self._fill]
        joined = np.empty(self._rows, dtype=EDGE_INDEX_DTYPE)
        at = 0
        for pair in buffers:
            chunk = pair[column]
            joined[at:at + chunk.size] = chunk
            pair[column] = _EMPTY_INDEX_COLUMN     # 这一块到此为止
            at += chunk.size
        joined[at:at + tail.size] = tail
        if column == 0:
            self._src = _EMPTY_INDEX_COLUMN
        else:
            self._dst = _EMPTY_INDEX_COLUMN
        return joined


def _group_starts(sorted_keys: "np.ndarray") -> "np.ndarray":
    """已排序键数组里每个相等分组的起始下标。空数组 → 空结果。"""
    size = int(sorted_keys.size)
    if size == 0:
        return np.empty(0, dtype=np.intp)
    is_start = np.empty(size, dtype=bool)
    is_start[0] = True
    np.not_equal(sorted_keys[1:], sorted_keys[:-1], out=is_start[1:])
    starts = np.flatnonzero(is_start)
    del is_start
    return starts


class CrossCommunityEdgeFold:
    """向量化折叠的**结果**:跨板块边按板块对聚合后的列式表示(设计 §3.32)。

    每条边的状态全部在 ndarray 里,**没有任何按边计数的 Python 容器** —— 这正是
    第 2/7/9 轮那三个 P1 想要的东西:折叠不再造第二份同量级的 dict,也不再需要一个
    「不缩哈希表」的 `popitem` 来假装有界。

    ``_pair_lo`` / ``_pair_hi`` 存的是**板块的整数序号**,而序号是按板块 id 字符串
    **排序**分配的(见 `fold_cross_community_edges`)。这条不是实现细节,是产物契约:
    正因为整数序等于字符串字典序,`np.minimum`/`np.maximum` 的无向归一才与旧代码
    ``(src, dst) if src <= dst else (dst, src)`` 的**字符串**比较逐字同解,`top_edges`
    的并列消歧也才等价于按 ``(src_id, dst_id)`` 升序。

    数组按 ``(lo, hi)`` 升序排好(聚合的副产品),所以 `top_edges` 只要按 ``-weight``
    做一次**稳定**排序就等价于旧的 ``key=(-weight, src, dst)`` 全序。
    """

    __slots__ = (
        "_board_ids", "_pair_lo", "_pair_hi", "_pair_weight", "_intra", "_cross_weight",
    )

    def __init__(
        self,
        board_ids: Sequence[str],
        pair_lo: "np.ndarray",
        pair_hi: "np.ndarray",
        pair_weight: "np.ndarray",
        intra_weight: int,
        cross_weight: int,
    ) -> None:
        self._board_ids = board_ids
        self._pair_lo = pair_lo
        self._pair_hi = pair_hi
        self._pair_weight = pair_weight
        self._intra = int(intra_weight)
        self._cross_weight = int(cross_weight)

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
        return int(self._pair_weight.size)

    def top_edges(self, limit: int) -> List[Tuple[str, str, int]]:
        """按 ``(-weight, src, dst)`` 取前 ``limit`` 条,结果已定序。

        与 `CrossCommunityEdgeFolder.top_edges` 逐字等价(那是标量参照实现,
        `test_cross_edge_folder_row_stream_matches_the_preaggregated_fold` 钉两者相同)。
        实现不同:那边是 `heapq.nsmallest`,这边是对已按 ``(lo, hi)`` 升序的数组做一次
        按 ``-weight`` 的**稳定** argsort —— 稳定性就是并列时的 ``(src, dst)`` 升序。

        ⚠ argsort 的下标数组是 O(板块对数) 的临时量(836 万对 ≈ 67 MB)。它比旧模型
        整份 `_cross` dict(约 1.4 GB)小一个多数量级,而且**只活在本方法内**;返回的
        列表长度有硬上界,调用方据此流式喂给分批 `executemany`。
        """
        limit = max(0, int(limit))
        size = int(self._pair_weight.size)
        if limit == 0 or size == 0:
            return []
        take = min(limit, size)
        # weight 恒 >= 1,取负在 int32 内安全(只有 INT32_MIN 取负会溢出)。
        order = np.argsort(np.negative(self._pair_weight), kind="stable")
        ids = self._board_ids
        lo, hi, weight = self._pair_lo, self._pair_hi, self._pair_weight
        return [
            (ids[int(lo[i])], ids[int(hi[i])], int(weight[i]))
            for i in order[:take]
        ]


class CrossCommunityEdgeFolder:
    """把逐条 canonical 边按 membership 折成跨板块边的**标量参照实现**。

    ⚠ 生产路径**不**走这里:`rebuild_communities` 折的是一张 `CanonicalEdgeTable`,
    走向量化的 `fold_cross_community_edges` → `CrossCommunityEdgeFold`。本类存在的
    理由只有一个,而且是硬理由 —— 它是那条向量化实现的**差分对照**:
    `test_cross_edge_folder_row_stream_matches_the_preaggregated_fold` 拿「逐行喂、
    每行 weight=1」的本类与「喂预聚合边表」的向量化折叠比对,要求两者的
    `top_edges` / `intra_weight` / `cross_weight` 逐字相同。

    这条对照在数组化之后**变强了**而不是变弱:数组化之前两条路径共用同一个 `add`,
    比对基本恒真;现在两边是两份独立实现(dict 累加 vs argsort + reduceat),
    归一方向、并列消歧、权重合计任何一处写歪都会当场分叉。

    ⚠ 所以**不要**因为「生产没人调 `add`」就删掉它 —— 删掉等于把那条差分对照掏空。
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
    edges: CanonicalEdgeTable,
    community_of_index: Sequence[Optional[str]],
) -> CrossCommunityEdgeFold:
    """把 canonical 整数边表按 membership **向量化**折叠成跨板块边(设计 §3.32)。

    ⚠ **`edges` 会被释放**(第一步就调 `CanonicalEdgeTable.release`):折叠不是「读一张
    表、建另一张同量级的表」,而是把边表就地换成它的折叠结果。调用方在这之后不得再用它,
    这一点与旧模型的「搬空 `ew`」是同一个契约,只是不再依赖那个**不缩哈希表**的
    `popitem`。

    ``community_of_index[i]`` 是节点下标 ``i`` 的板块 id(不属于任何保留板块则
    ``None``)。**刻意用按下标的 list 而不是 canonical→板块的 dict**:调用方手里已经
    有 `can2idx`,再造一份同基数(生产 ~171 万条)的 `str -> str` 映射就是在边表还占着
    内存的那一刻凭空多几百 MB。

    ⚠ **板块序号按 id 字符串排序分配。** 旧代码的无向归一是
    ``(src, dst) if src <= dst else (dst, src)`` —— 拿**字符串**比大小。向量化之后
    比的是整数序号,只有序号按字符串排序分配,`np.minimum`/`np.maximum` 才与它同解;
    否则同一对板块的 (src, dst) 会左右颠倒,`kg_community_edges` 的每一行都变样。

    ⚠ **中间量全部是数组、且用完即 `del`。** 峰值不再是「边表 + 折叠结果」两份同量级
    dict(生产各约 1.4 GB),而是几个 int32/int64 列(836 万边合计几百 MB),
    且随流程逐段回落。折叠结果的条目数**仍然**没有结构性上界(Louvain 的目标函数
    只是「倾向于」减少跨社区边,而补账本那条路径用的是库里现成的划分),所以落库
    有界照旧靠 `MAX_PERSISTED_COMMUNITY_EDGES`,两个机制缺一不可。

    刻意**不**做 DB 侧聚合:SQLite 的 `PRAGMA temp_store = MEMORY`(见
    `repositories/sqlite/database.py`)让 GROUP BY 的临时 B 树也落在进程内存里,一分钱
    没省,还要为 836 万行的临时表按住写锁;而设计 §3.35 给 SQLite 侧定的标准正是
    「正确、有界、不 OOM、不长时间持锁」。
    """
    board_ids = sorted({cid for cid in community_of_index if cid is not None})
    rank_of = {cid: rank for rank, cid in enumerate(board_ids)}
    board_of_index = np.full(len(community_of_index), -1, dtype=EDGE_INDEX_DTYPE)
    for index, community_id in enumerate(community_of_index):
        if community_id is not None:
            board_of_index[index] = rank_of[community_id]
    del rank_of
    src, dst, weight = edges.src, edges.dst, edges.weight
    edges.release()
    left_board = board_of_index[src]
    del src
    right_board = board_of_index[dst]
    del dst, board_of_index
    joined = (left_board >= 0) & (right_board >= 0)
    same_board = joined & (left_board == right_board)
    intra_weight = int(weight[same_board].sum(dtype=np.int64))
    crossing = joined & ~same_board
    del joined, same_board
    left_cross = left_board[crossing]
    del left_board
    right_cross = right_board[crossing]
    del right_board
    pair_weight = weight[crossing]
    del weight, crossing
    cross_weight = int(pair_weight.sum(dtype=np.int64))
    lo = np.minimum(left_cross, right_cross)
    hi = np.maximum(left_cross, right_cross)
    del left_cross, right_cross
    # 复合键 ``lo * K + hi``:K = 板块数,上界 = canonical 节点数(生产 ~171 万),
    # 键上界 ~2.9e12,离 int64 有六个数量级余量。排序后同一板块对必然相邻。
    key = lo.astype(np.int64)
    key *= max(len(board_ids), 1)
    key += hi
    order = np.argsort(key, kind="stable")
    starts = _group_starts(key[order])
    del key
    lo = lo[order]
    hi = hi[order]
    pair_weight = pair_weight[order]
    del order
    folded = CrossCommunityEdgeFold(
        board_ids,
        lo[starts],
        hi[starts],
        # ⚠ `dtype=` 不能省:`np.add.reduceat` 与 `np.sum` 同规则,int32 输入默认按
        # 平台整型(int64)累加,权重列会凭空翻一倍。每组的合计上界是关系行数,int32 够。
        (np.add.reduceat(pair_weight, starts, dtype=EDGE_WEIGHT_DTYPE) if starts.size
         else np.empty(0, dtype=EDGE_WEIGHT_DTYPE)),
        intra_weight,
        cross_weight,
    )
    del lo, hi, pair_weight, starts
    return folded


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
