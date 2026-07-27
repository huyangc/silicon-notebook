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
「空来源」。被排除的量由三条统计快照单独报出,不在这里凭空消失。

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
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


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
# 万行,而落库那一刻 `cross` dict 与截断后的行列表同时活着,`rebuild_communities` 的
# 栈帧上还压着 `ew`(836 万 int-tuple 键 dict)。这正是 #340/#342/#347/#351/#352/#354
# 那条 OOM 轨道盯着的同一个库、同一个峰值时刻。
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


def check_artifact_payloads(payloads: Mapping[str, dict]) -> None:
    """账本写入口的守卫:**多写**与**少写**都硬失败。

    只拒未知 kind 是不够的。「产物在不在 = 账本行在不在」这条判据依赖五行(空板块库
    是四行)一起写出来;少写一行,下游看到的是「这份产物从来没算过」,而实际上是这一轮
    忘了算 —— 两者在读侧完全无法区分,而且没有任何报错。

    ``ARTIFACT_SOURCE_PROFILES`` 是唯一允许缺席的一份,理由见
    ``OPTIONAL_ARTIFACT_KINDS``;而且它只在**真的一个板块都没有**时才可以缺席,
    这一条由 `community_edges` 账本里的 ``communities`` 计数当场复核 —— 否则
    「允许缺席」就会变成「随便漏一份也不报错」。
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


def analysis_ledger_is_current(
    ledger_seqs: Mapping[str, int], seq: int, *, has_boards: bool
) -> bool:
    """账本是否已经**齐全地**建在 ``seq`` 这个 KG 状态上。

    这是预计算**自己的**新鲜度闸,与「社区图要不要重建」是两件事(见
    `rebuild_communities` 里的 B1 说明)。判据只看账本:齐不齐、戳对不对。
    任一必需 kind 缺席、或任一行的戳与 ``seq`` 不同 → 不新鲜,要补跑。

    ``has_boards`` 决定 `source_profiles` 算不算必需:没有板块时它合法缺席
    (见 `OPTIONAL_ARTIFACT_KINDS`),此时不能因为它不在就永远判成「不新鲜」——
    那会让空板块库每次调用都重跑一遍全部重活。
    """
    required = set(REQUIRED_ARTIFACT_KINDS)
    if has_boards:
        required.add(ARTIFACT_SOURCE_PROFILES)
    if not required <= set(ledger_seqs):
        return False
    return all(int(value) == int(seq) for value in ledger_seqs.values())


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

    内存:结果最多和板块**对**数一样大(不是边数)。落库前再按 weight 降序截到
    ``MAX_PERSISTED_COMMUNITY_EDGES``(`top_edges`,`heapq.nsmallest` 的峰值是 O(limit),
    不是 O(板块对数))—— 绝不为了排序再造一份完整列表。
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
    edge_weights: Mapping[Tuple[int, int], int],
    community_of_index: Sequence[Optional[str]],
) -> CrossCommunityEdgeFolder:
    """把 canonical 整数边图按 membership 折叠成跨板块边。

    ``community_of_index[i]`` 是节点下标 ``i`` 的板块 id(不属于任何保留板块则
    ``None``)。**刻意用按下标的 list 而不是 canonical→板块的 dict**:调用方手里已经
    有 `can2idx`,再造一份同基数(生产 ~171 万条)的 `str -> str` dict 就是在 `ew`
    还占着 1.4 GB 的那一刻凭空多几百 MB。list 的每个槽只是一个已存在的板块 id 字符串
    的引用,基数再大也只有指针数组本身的开销。

    效率:这是 `rebuild_communities` 已经握在手里的 `ew` 的一次遍历,**不新起任何扫描**。
    Louvain 的目标函数本来就在最小化跨社区边,所以结果通常远小于 `ew`(本机真实库实测:
    49109 条唯一边 → 503 对跨板块)——但**那三个本机样本不足以外推到生产**,落库上限
    因此是硬的,见 `MAX_PERSISTED_COMMUNITY_EDGES`。
    """
    folder = CrossCommunityEdgeFolder()
    for (left, right), weight in edge_weights.items():
        folder.add(community_of_index[left], community_of_index[right], weight)
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
