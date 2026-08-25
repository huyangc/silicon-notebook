"""KG 质量分析报告的只读聚合 service(T3)。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md` §3.2/§3.3/§3.5。
把 T2 落库的预计算产物 + 板块表**只读地**装配成一份带完整新鲜度标注的报告,供
`GET /notebooks/{id}/kg-analysis` 与 `.../kg-analysis/sources` 消费。

设计红线(与 `app/services/checkup.py` 同一套):

- **只读**:不写库、不调 LLM / embedding / rerank。
- **不 import facade**:collaborators 由构造方以窄 seam 注入(``database`` / ``unified_kg``
  / ``now``),本 service 只组装、不拼 SQL。
- **后端中性**:不判 dialect、不 import 任一侧 adapter。注入的 store 实例是后端相关的,
  同一套装配逻辑在 SQLite 与 PostgreSQL 上都跑。
- **不产用户文案**:响应里全是内部代号/枚举(``usable_live`` / ``never_computed`` /
  ``canonical`` …),面向用户的界面词由 T4 的前端映射层负责。本文件刻意不出中文用户
  文案(界面词汇守卫的红线)。

**绝不在请求路径上做全表扫(设计 §3.2)。** 五份产物里有三份是全表级重活(簇大小
直方图 / 最大簇榜单 / 边出处构成 —— 生产库 200 万簇行 / 836 万边,冷态与仓库那次
「835 万边冷扫 39 分钟」同量级),它们已经被 T2 在 `rebuild_communities` 那一批里算完
并落进 `kg_analysis_artifacts` 账本。本 service **只读账本 + 明细表**,一次都不调
`unified_kg.cluster_size_histogram` / `largest_clusters` / `relation_provenance_counts` /
`source_canonical_rows`(`tests/test_kg_analysis_service.py` 有一条 spy 守卫钉住这点)。

请求路径上真正跑的 SQL 只有五条,全部按行数有界:
  · `state_row`                  单行主键点读
  · `kg_analysis_artifact_rows`  ≤5 行复合主键点读
  · `kg_community_edges_top`     ≤20 万行(T2 硬上限)的分片扫 + 有界 top-N
  · `community_overview_on`      COUNT + `ORDER BY size DESC LIMIT ≤200` + ≤200 次有界 top-K
  · `kg_source_profile_page`     COUNT + 一页 ≤200 行 + ≤200 次主键点查(仅 /sources)

后两条里读**明细表**的那两条(`kg_community_edges_top` / `kg_source_profile_page`)还带
一道账本闸:账本行不在就整条跳过(见 `_detail_rows_are_readable`)。这不是性能优化而是
正确性 —— 无条件查会让同一份响应既说「这份产物不在」又把明细行发出去。

**随库规模线性增长的有两条,不是一条**(别把这句读成「只有板块列表要留意」):

  · `community_overview_on` —— 板块数没有结构性上限,而 `communities` 上**没有 size 索引**,
    所以 `ORDER BY size DESC LIMIT 200` 要排该 notebook 的全部板块行。它因此进了下面的
    记忆化:签名不变就不再跑。
  · `kg_source_profile_page` —— 两处随来源数增长:`COUNT(*)` 是 O(来源数)(走主键前缀,
    窄行);排序键 `mainstream_share` 上并列极多时(设计自己点名的形态:混杂库里一大片
    恰好 0.0),次级键 `source_id` 不在索引里,**每次请求**都要对整个并列组补一次临时
    B 树,`OFFSET` 还要逐行跳过。而且它**刻意不记忆化**(页是 limit × offset × order 的
    笛卡尔积,缓存全部页就是无界)。量级上可接受 —— 生产 base 库 48 836 个来源对 878 万
    对象是小量级 —— 但它是一项真实的每请求成本,不是「≤200 行主键点查」那么便宜。

证据强度如实说明,别把两个来源混成一个 —— 本机能测到的最大库只有 **1217 个板块**
(`nb-b37185f4ae`,只读实测:COUNT + top-200 排序冷 44 ms / 暖 0.8 ms,逐板块 top-5
共 200 次查询另计 43 ms);而设计 §3.3 记录的生产 base 库是 **88 580 个板块**,那个
数字来自设计里那次生产库只读查询,**不是本机实测**。也不要把本机的毫秒数线性外推
过去(冷态受随机 IO 支配,仓库有 835 万边冷扫 39 分钟的同形状事故)。记忆化在这里
买的就是那段没测过的区间。

新鲜度(设计 §3.3,**有真实教训**):在生产库上曾据 `communities` 的 88 580 个板块推出
「图散成一地」这样一个重大结论,随后得知该库尚未 rebuild —— 整个推断作废。所以本
service 的硬要求是**逐指标标注**,而不是整份报告挂一条横幅:

  · 每一份产物带自己的 ``built_at_seq`` / ``seq_behind`` / ``stale``;
  · 每一份产物带 ``basis``(口径来源),让「实时 USABLE 口径」与「上次社区重建的快照」
    这两种同屏并列的数字**可分辨**;
  · 每一份产物带 ``units``,让「canonical 计数 / 对象计数 / 板块对 / 关系行」这四种
    互相不可比的量不会被拼成一个比例;
  · 「缺失」与「为空」按**账本行的存在与否**区分,并且区分得出「从来没算过」、
    「合法缺席」与「本该有却没有」。

**边界**:这是 DFX 诊断工具,不是产品引导。职责到「如实标注」为止 —— 不折叠、不置灰、
不催促整理,也不替读者判断该不该信这些数字。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.kg_analysis_precompute import (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_KINDS,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_SOURCE_PROFILES,
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_SEQ_PAYLOAD_KEY,
    OPTIONAL_ARTIFACT_KINDS,
    artifact_cluster_seq,
    artifact_is_current,
    board_partition_cluster_seq,
    board_partition_is_current,
    required_artifact_kinds,
)
from app.services.knowledge_contracts import (
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
    RELATION_EXCLUSION_BUCKETS,
    RELATION_PROVENANCE_BUCKETS,
)

# ⚠ 本 service 刻意**不 import** app.repositories.sqlite/postgres 的任何东西 —— 它由
# 中性的 RepositoryRuntime 构造(database + unified_kg 两个 seam 都是后端相关的实例),
# 所以同一套装配逻辑在两个后端上都成立。

# --------------------------------------------------------------------- 口径来源
# 同一份报告里混着两种口径,不标注读者没有任何线索分辨(设计 §3.5 点名的那处):
# 簇大小直方图是**实时 USABLE 口径**(T1 查询,由 T2 在预计算时固化成快照),
# 板块规模则来自**上次社区重建**写下的划分。两者并列而不标来源,数字对不上时用户
# 会以为其中一个是错的。
BASIS_USABLE_LIVE = "usable_live"
BASIS_COMMUNITY_SNAPSHOT = "community_snapshot"
BASIS_UNIFIED_REBUILD_SNAPSHOT = "unified_rebuild_snapshot"

ARTIFACT_BASIS: Dict[str, str] = {
    ARTIFACT_CLUSTER_HISTOGRAM: BASIS_USABLE_LIVE,
    ARTIFACT_LARGEST_CLUSTERS: BASIS_USABLE_LIVE,
    ARTIFACT_RELATION_PROVENANCE: BASIS_USABLE_LIVE,
    ARTIFACT_COMMUNITY_EDGES: BASIS_COMMUNITY_SNAPSHOT,
    ARTIFACT_SOURCE_PROFILES: BASIS_COMMUNITY_SNAPSHOT,
}

# ----------------------------------------------------------------- 缺席的三种含义
# 判据是**账本行的存在与否**,不是明细表的行数(单一板块的图 legitimately 产出 0 条
# 跨板块边)。三种缺席必须分得开,否则「还没算过」会被读成「算过了,是空的」:
ABSENCE_NEVER_COMPUTED = "never_computed"   # 整个账本是空的:这个库从没跑过预计算
ABSENCE_EXPECTED = "expected"               # 合法缺席(唯一一档:零板块库不写来源画像)
ABSENCE_UNEXPECTED = "unexpected"           # 账本非空却少了这一份 —— 异常

LEDGER_EMPTY = "empty"
LEDGER_PARTIAL = "partial"
LEDGER_COMPLETE = "complete"

# ------------------------------------------------------------------ /sources 排序
# 升序 = 与主体板块最不连通的排前面,正是「哪些来源不属于这里」的答案。
ORDER_SPARSE = "sparse"
ORDER_CONNECTED = "connected"
SOURCE_ORDERS = (ORDER_SPARSE, ORDER_CONNECTED)

# --------------------------------------------------------------------------- 单位
# ⚠ 载荷里的计数**单位不统一**,而且不可互比。T2 的模块头已经记明这一点,这里把它
# 变成响应里的机器可读字段 —— T4 的卡片最容易在这里做错:同屏并列 `total_members`
# (canonical 计数)与 `n_graph_objects`(对象计数)是 apples/oranges,前者恒小于后者
# (合并把多个对象压成一个 canonical),两者相除得不到任何有意义的比例。
UNIT_OBJECTS = "objects"                     # knowledge_objects 的行(可用状态)
UNIT_CANONICAL = "canonical"                 # 合并后的 canonical 节点
UNIT_CLUSTER_MEMBER_ROWS = "cluster_member_rows"   # concept_clusters 的行
UNIT_CLUSTERS = "clusters"                   # 不同的 canonical 簇
UNIT_COMMUNITIES = "communities"             # 主题板块个数
UNIT_COMMUNITY_PAIRS = "community_pairs"     # 板块**对**(跨板块边的条数)
UNIT_RELATION_ROWS = "relation_rows"         # knowledge_relations 的行
UNIT_SOURCES = "sources"                     # 来源个数
UNIT_OBJECT_TYPES = "object_types"           # 不同的 object_type 个数
UNIT_RATIO = "ratio"                         # [0,1] 的比例
UNIT_SEQ = "seq"                             # 单调变更计数器(kg_mutation_seq 系)
UNIT_LEVEL = "level"                         # 社区层级(不是数量)

# 单位表按 **payload 内的叶子字段名** 索引:同一份载荷里一个字段名只有一种含义
# (跨载荷可以不同 —— `clusters` 在直方图里是整数、在最大簇榜单里是一个列表,所以
# 每份载荷各有一张表)。守卫 `test_every_numeric_field_declares_a_unit` 会遍历真实
# 载荷的每一个数值叶子,少一条就报红。
#
# `CLUSTER_SEQ_PAYLOAD_KEY`(簇世代的戳)只出现在**依赖合并结果**的那四份载荷里,
# 所以按那个集合展开而不是四处手抄 —— 分类改了,单位表跟着改,不会漏。
_CLUSTER_SEQ_UNIT: Dict[str, str] = {CLUSTER_SEQ_PAYLOAD_KEY: UNIT_SEQ}
_HISTOGRAM_UNITS: Dict[str, str] = {
    "member_rows": UNIT_CLUSTER_MEMBER_ROWS,
    "clusters": UNIT_CLUSTERS,
    "excluded_member_rows": UNIT_CLUSTER_MEMBER_ROWS,
    "empty_clusters": UNIT_CLUSTERS,
    "empty_cluster_member_rows": UNIT_CLUSTER_MEMBER_ROWS,
    "members": UNIT_CLUSTER_MEMBER_ROWS,
    "object_types": UNIT_OBJECT_TYPES,
}
_LARGEST_CLUSTERS_UNITS: Dict[str, str] = {
    "limit": UNIT_CLUSTERS,
    "members": UNIT_CLUSTER_MEMBER_ROWS,
}
_RELATION_PROVENANCE_UNITS: Dict[str, str] = {
    "counted": UNIT_RELATION_ROWS,
    "relink": UNIT_RELATION_ROWS,
    "total_rows": UNIT_RELATION_ROWS,
    # 分桶名是契约常量,展开而不是手抄 —— SQL / 契约 / 单位表三者不可能漂移。
    **{name: UNIT_RELATION_ROWS for name in RELATION_PROVENANCE_BUCKETS},
    **{name: UNIT_RELATION_ROWS for name in RELATION_EXCLUSION_BUCKETS},
}
_COMMUNITY_EDGES_UNITS: Dict[str, str] = {
    "level": UNIT_LEVEL,
    "edges": UNIT_COMMUNITY_PAIRS,
    "edges_total": UNIT_COMMUNITY_PAIRS,
    "edge_limit": UNIT_COMMUNITY_PAIRS,
    "cross_weight": UNIT_RELATION_ROWS,
    "intra_weight": UNIT_RELATION_ROWS,
    "communities": UNIT_COMMUNITIES,
}
_SOURCE_PROFILES_UNITS: Dict[str, str] = {
    "level": UNIT_LEVEL,
    "sources": UNIT_SOURCES,
    "mainstream_coverage": UNIT_RATIO,
    "head_communities": UNIT_COMMUNITIES,
    "head_members": UNIT_CANONICAL,
    "total_members": UNIT_CANONICAL,
}
ARTIFACT_UNITS: Dict[str, Dict[str, str]] = {
    kind: (
        {**units, **_CLUSTER_SEQ_UNIT}
        if kind in CLUSTER_DEPENDENT_ARTIFACT_KINDS
        else units
    )
    for kind, units in {
        ARTIFACT_CLUSTER_HISTOGRAM: _HISTOGRAM_UNITS,
        ARTIFACT_LARGEST_CLUSTERS: _LARGEST_CLUSTERS_UNITS,
        ARTIFACT_RELATION_PROVENANCE: _RELATION_PROVENANCE_UNITS,
        ARTIFACT_COMMUNITY_EDGES: _COMMUNITY_EDGES_UNITS,
        ARTIFACT_SOURCE_PROFILES: _SOURCE_PROFILES_UNITS,
    }.items()
}

BOARD_UNITS: Dict[str, str] = {
    "level": UNIT_LEVEL,
    "total": UNIT_COMMUNITIES,
    "returned": UNIT_COMMUNITIES,
    "limit": UNIT_COMMUNITIES,
    "top_members_limit": UNIT_CANONICAL,
    "size": UNIT_CANONICAL,
}
BOARD_EDGE_UNITS: Dict[str, str] = {
    "limit": UNIT_COMMUNITY_PAIRS,
    "returned": UNIT_COMMUNITY_PAIRS,
    "stored": UNIT_COMMUNITY_PAIRS,
    "stored_total": UNIT_COMMUNITY_PAIRS,
    "edge_limit": UNIT_COMMUNITY_PAIRS,
    "returned_weight": UNIT_RELATION_ROWS,
    "cross_weight": UNIT_RELATION_ROWS,
    "weight_coverage": UNIT_RATIO,
    "weight": UNIT_RELATION_ROWS,
}
STATE_UNITS: Dict[str, str] = {
    "kg_mutation_seq": UNIT_SEQ,
    "community_seq": UNIT_SEQ,
    "cluster_mutation_seq": UNIT_SEQ,
    "canonical_rel_seq": UNIT_SEQ,
    "object_count": UNIT_OBJECTS,
    "relation_count": UNIT_RELATION_ROWS,
    "cluster_count": UNIT_CLUSTERS,
}
SOURCE_PAGE_UNITS: Dict[str, str] = {
    "total": UNIT_SOURCES,
    "returned": UNIT_SOURCES,
    "limit": UNIT_SOURCES,
    "offset": UNIT_SOURCES,
    "n_objects": UNIT_OBJECTS,
    "n_graph_objects": UNIT_OBJECTS,
    "top_share": UNIT_RATIO,
    "community_spread": UNIT_COMMUNITIES,
    "mainstream_share": UNIT_RATIO,
}

# 记忆化的有界化:进程内只保留最近访问的 N 个 (notebook, 参数) 组合。参数来自请求
# 查询串,所以键的基数由客户端决定 —— LRU 上界是防「有人把 limit 从 1 试到 200」把
# 缓存撑爆的唯一手段。看板逐个打开,真实工作集是个位数。
_OVERVIEW_CACHE_MAX = 64


@dataclass(frozen=True)
class Freshness:
    """一份产物的新鲜度 + 口径来源 —— **两条相互独立的世代线**。

    ``built_at_seq`` 是它建于哪个 ``kg_mutation_seq``;``seq_behind`` 是当前 seq 减去
    它 —— **不 clamp 到 0**:账本比当前还新是一种不该发生的状态(库被手工改过),把它
    压成 0 等于替读者把异常藏起来。产物缺席时三个都是 None(不是 0 —— 0 会被读成
    「刚建好」)。

    ``built_at_cluster_seq`` / ``cluster_seq_behind`` 是**合并结果**那条线
    (``unified_kg_state.cluster_mutation_seq``)。它必须独立报,因为簇的写路径刻意
    不动 ``kg_mutation_seq``:只看 KG 世代的话,一次纯合并写入之后簇大小直方图与最大簇
    榜单已经陈旧,报告却会说「与当前一致」(codex 第 5 轮评审复现)。
    两个字段在**三种**情况下是 None,前两种读者不需要分辨:
      · 这份产物与合并结果无关(`relation_provenance` —— 它的 SQL 一个字都没提
        `concept_clusters`),没有落后量可报;
      · 产物缺席,或它压根没盖这个戳(库被手工改过);
      · 依赖板块的两份由「只补账本」那条路径产出 —— 它们描述的板块划分是库里现成的,
        建在哪一代合并结果上没有地方记,所以显式记着「无从判断」(见
        `kg_analysis_precompute.stamp_cluster_seq`)。这一档靠 ``stale is None`` 认。

    ``stale`` 是**两条线的三值合取**(判据 `artifact_is_current`,与写侧的闸同一个
    函数):任一条明确落后即 True;都对齐即 False;没有明确落后但有一条无从判断即
    **None** —— 那既不是「与当前一致」也不是「落后 N 代」,替读者选一个就是编。
    产物缺席时也是 None(靠 `ArtifactView.present` 分辨,消费方本来就要先看它)。
    """

    basis: str
    built_at_seq: Optional[int]
    seq_behind: Optional[int]
    stale: Optional[bool]
    built_at_cluster_seq: Optional[int] = None
    cluster_seq_behind: Optional[int] = None


@dataclass(frozen=True)
class ArtifactView:
    """账本里的一份产物(或它的缺席)。"""

    kind: str
    present: bool
    optional: bool
    absence: Optional[str]
    freshness: Freshness
    created_at: str
    units: Dict[str, str]
    payload: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class LastRebuild:
    """`unified_kg_state` 记下的**上一次 unified-KG 重建**时的规模快照。

    ⚠ 口径:``object_count`` 数的是 ``status != 'deprecated'`` 的对象。今天它与本报告
    的 USABLE 口径**恰好相等**(deprecated 是唯一的非可用状态),但那是状态词表的巧合
    而不是同一条判据 —— 一旦新增一个非可用状态,这两个数就会开始分岔。所以它单独一个
    ``basis``,不与实时口径的数字混在一起。
    """

    basis: str
    at: str
    object_count: int
    relation_count: int
    cluster_count: int
    units: Dict[str, str]


@dataclass(frozen=True)
class KgState:
    """`unified_kg_state` —— 全部新鲜度判断的参照系。

    ``present=False`` 表示这个 notebook 从没写过 KG——行缺失,或只有零历史的行
    (出生即认证的出处证书行、graph 清空后为保 certificate 重铸的行)。
    此时所有计数都是 0、所有 seq 都是 0/-1,报告仍然出得来,只是每一格都在说「没有」。
    """

    present: bool
    kg_mutation_seq: int
    community_seq: int
    cluster_mutation_seq: int
    canonical_rel_seq: int
    dirty: bool
    last_rebuild: LastRebuild
    units: Dict[str, str] = field(default_factory=lambda: dict(STATE_UNITS))


@dataclass(frozen=True)
class BoardOverview:
    """主题板块列表 —— **实时读** `communities` 表,而那张表本身是上次社区重建的快照。

    所以它 KG 世代那条线的戳是 ``unified_kg_state.community_seq``(板块建于哪个 KG
    状态),不是账本里的任何一个 seq;而**合并世代**那条线走账本里依赖板块的产物记下的
    ``built_at_cluster_seq``(= 板块划分建在哪一代合并结果上),判据与那两份产物共用 ——
    见 `_community_freshness`。
    """

    freshness: Freshness
    units: Dict[str, str]
    payload: Dict[str, Any]


@dataclass(frozen=True)
class BoardEdgeView:
    """跨板块边的 top-N + **两级截断**的显式披露。

    两级都必须说清,否则读者会把「图上这 200 条」当成「库里只有 200 条」:
      · 落库级(T2):`edges_total` 对板块对总数,`stored` 是实际落库行数,
        `stored_truncated` / `edge_limit` 说明是否触到 `MAX_PERSISTED_COMMUNITY_EDGES`;
      · 请求级(T3):`returned` ≤ `limit`,`weight_coverage` = 返回边权 / **全部**跨板块
        边权(`cross_weight` 是图统计量,不随任何一级上限变化),即这张图覆盖了多少。
    """

    present: bool
    freshness: Freshness
    limit: int
    returned: int
    returned_weight: int
    stored: int
    stored_total: int
    stored_truncated: bool
    edge_limit: int
    cross_weight: int
    weight_coverage: Optional[float]
    units: Dict[str, str]
    edges: List[Dict[str, Any]]


@dataclass(frozen=True)
class KgAnalysisOverview:
    notebook_id: str
    generated_at: str
    level: int
    ledger_state: str
    ledger_consistent: bool
    state: KgState
    artifacts: List[ArtifactView]
    boards: BoardOverview
    board_edges: BoardEdgeView


@dataclass(frozen=True)
class SourceProfilePage:
    notebook_id: str
    generated_at: str
    present: bool
    absence: Optional[str]
    freshness: Freshness
    kg_mutation_seq: int
    order: str
    limit: int
    offset: int
    total: int
    returned: int
    has_more: bool
    summary: Optional[Dict[str, Any]]
    units: Dict[str, str]
    rows: List[Dict[str, Any]]


class KgAnalysisService:
    """per-notebook 的只读 KG 质量分析报告装配器。

    collaborators 全部是窄 seam,便于单测直接构造(无需给 facade 打桩):

    - ``database``:**只当写事务的问询对象**。后端相关(sqlite/postgres 各自的
      database),由构造方注入,本 service 只读它的 ``in_write_transaction``(入口守卫用)。
      读连接一条都不从这里开 —— 全部经 ``unified_kg.read_snapshot()``,见下条。
    - ``unified_kg``:后端相关的 **UnifiedKgStore 实例**。本 service 只调它的**有界读**
      (`state_row` / `kg_analysis_artifact_rows` / `kg_community_edges_top` /
      `kg_source_profile_page` / `community_overview_on`),**绝不**调那四条全表重活。
      五条读全是 connection-taking 的,两个端点各骑**一个** `read_snapshot()`(后端中性
      的多语句共享快照,参数表为空 —— 两侧怎么兑现是 store 的事)。板块列表因此走
      `community_overview_on` 而不是自开快照的 `community_overview`:后者会在这一趟里
      另起一份库。
    - ``now``:时钟 seam。
    """

    def __init__(
        self,
        *,
        database: Any,
        unified_kg: Any,
        now: Callable[[], str],
    ) -> None:
        self._database = database
        self._unified_kg = unified_kg
        self._now = now
        # 进程内记忆化:(nb, 参数) -> (签名, (boards, edges))。只缓存**贵的那两块**;
        # 账本与 state 行每次都现读(它们是 ≤6 行的点读,而且正是签名本身)。
        self._overview_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._overview_cache_lock = threading.Lock()

    # ------------------------------------------------------------- 入口守卫
    def _reject_inside_write_transaction(self, what: str) -> None:
        """本 service 的读**不得**在调用方的写事务内跑。当场硬失败。

        本 service 的五条读**全部**是 connection-taking 的、骑 `read_snapshot()` 开出来
        的那条读连接(板块列表走 `community_overview_on`,不是那个自开快照的同名方法),
        所以 store 侧那道只装在自开连接入口上的同名守卫对它们一条都不生效 —— 这道必须
        在本 service 的入口自己有。而危害是一样的:

        1. 正确性:SQLite 的 `write()` 每次另开一条独立连接、PostgreSQL 每次从池里另借
           一条,所以在写事务里读到的是那个事务**提交前**的库 —— 一份过时的报告,而且
           不会有任何报错。
        2. 可用性(仅 SQLite):`write()` 拿的是**进程级写锁**,把报告装配挂进去等于让
           全库的写入排队等它。

        为什么要运行时守卫而不是只写注释:形状守卫抓不到这类违规 —— 这些读一张产物表
        之外的东西都不碰,在 SQLite 上还跑在另一条连接上。T1/T2 已经在这条特性上被
        「移动变异全绿」教训过一次。
        """
        if self._database.in_write_transaction:
            raise RuntimeError(
                f"{what}:KG 分析报告的只读装配被放进了写事务里。它会读到提交前的旧数据"
                "(不报错、静默过时),SQLite 上还会把进程级写锁按住整趟装配。"
                "把它挪到 write() 之外调用。"
            )

    # ---------------------------------------------------------------- 总览
    def overview(
        self,
        notebook_id: str,
        *,
        board_limit: int = 50,
        top_members: int = 5,
        edge_limit: int = 200,
    ) -> KgAnalysisOverview:
        """总览报告:状态 + 五份产物 + 板块列表 + 跨板块边 top-N。

        ``level`` **刻意不是参数**:三张产物表都没有 level 维度(T2 的决定 —— 一次预
        计算产出的永远是一套自洽的产物,它描述的 level 记在账本 payload 里)。让调用方
        指定一个 level 只会造出「板块列表是 level 1、跨板块边是 level 0」这种自相矛盾
        的报告。这里从账本读出产物描述的那个 level,并用它去取板块列表;账本缺席时落
        回 0(`rebuild_communities` 的默认层)。

        代价:一条 O(1) 点读 + 一条 ≤5 行点读 +(签名变了才跑)一次板块列表读与一次
        有界 top-N 边读。签名 = state 行的全部 seq/dirty + 账本每一行的 (kind, seq,
        created_at)。``created_at`` 必须进签名:一次 ``force=True`` 的重建会在**同一个**
        `kg_mutation_seq` 上重铸板块 id 并重写产物,只看 seq 的话缓存不会失效。

        ⚠ **签名里为什么没有「板块世代」这一项,以及缺了它靠什么补。** 板块 id 只由
        `replace_communities` 重铸,而写侧在**同一个事务**里作废依赖板块的两份账本行
        (见 `app.domain.kg_analysis_contracts.BOARD_DEPENDENT_ARTIFACT_KINDS`)—— 删掉一行就动了
        签名,这是这份缓存唯一 O(1) 的失效手段。世代只能由**写侧推**、不能由读侧拉:任何
        从 `communities` 现算的世代标记都是 O(板块数) 的读(生产 88 580 个板块,而
        `communities` 上没有 size 索引),那恰恰就是这份缓存要省掉的那笔钱。

        但那次作废在「账本里一行依赖板块的产物都没有」时是个 **no-op**(上一轮预计算
        失败),而 `force=True` 的重建不抬 `kg_mutation_seq` —— 那一档板块换了一整套、
        签名却一个字节没动。所以缓存**只在签名跟得住板块重铸时才写**,判据与代价见
        `_signature_tracks_board_recasts`。
        """
        self._reject_inside_write_transaction("KG 分析总览")
        # ⚠ **四条读,一个快照**(codex 第 8 轮 P2 定的形,第 12 轮 P2 补完)。裸
        # `connect()` 在两个后端上都是「每条语句各取一个快照」(SQLite 自动提交 /
        # PostgreSQL READ COMMITTED),而**为每条读各开一个 `read_snapshot()` 与这个一样
        # 坏** —— 快照要能跨读共享才有意义。这一趟并发的写有两种,各自会拼出一种谎:
        #
        #   · 预计算整批重写产物表:板块那一格的合并世代取自账本的
        #     `built_at_cluster_seq`,落后量却是拿 state 的 `cluster_mutation_seq` 减出来
        #     的(见 `_community_freshness`)—— 两次读劈在提交两侧,减出来是个负数,而负数
        #     在本契约里是「库被手工改过」的异常信号。凭空报一个假异常比不报更糟。
        #   · 社区重建整批重铸板块 id:账本/state 读在前、板块列表读在后,响应就会把
        #     **上一代的新鲜度戳**盖在**新一代的板块 id** 上;板块列表读在前、跨板块边读
        #     在后,就是**旧板块配新边** —— 俯瞰图照着 edges 画连线,画出来的是悬空线。
        #
        # 方言细节留在 store 里(见 `read_snapshot`),本 service 不判后端。
        with self._unified_kg.read_snapshot() as db:
            state_row = self._unified_kg.state_row(db, notebook_id)
            ledger = self._unified_kg.kg_analysis_artifact_rows(db, notebook_id)
            state = _state_view(state_row)
            level = _ledger_level(ledger)
            signature = _signature(state, ledger)
            key = (
                notebook_id, level,
                int(board_limit), int(top_members), int(edge_limit),
            )
            cached = self._cache_get(key, signature)
            if cached is None:
                # 骑上面那条快照的 connection-taking 入口(`community_overview` 那个自开
                # 快照的同名方法在这里是错的 —— 它会另起一份库)。
                boards_payload = self._unified_kg.community_overview_on(
                    db, notebook_id, level=level, limit=board_limit, top_k=top_members
                )
                # ⚠ 明细表**只在账本行在场时**才查(见 `_detail_rows_are_readable`)。
                # 账本说这份产物不在,却把明细表里的行画进俯瞰图,就是同屏既说「缺失」
                # 又把它的边画出来 —— 而那些行按定义是悬空的。顺带也省掉这次扫描。
                edge_rows: List[Tuple[str, str, int]] = []
                if _detail_rows_are_readable(ARTIFACT_COMMUNITY_EDGES, ledger):
                    edge_rows = self._unified_kg.kg_community_edges_top(
                        db, notebook_id, edge_limit
                    )
                cached = (boards_payload, edge_rows)
                # ⚠ 只在「板块一被重铸,签名必然跟着动」成立时才写缓存。不成立的那一档
                # (账本里一行依赖板块的产物都没有 → 同事务作废是 no-op)写进去就是一份
                # 会无限期说谎的缓存,完整理由见 `_signature_tracks_board_recasts`。
                if _signature_tracks_board_recasts(ledger):
                    self._cache_put(key, signature, cached)
        artifacts = _artifact_views(
            ledger, state.kg_mutation_seq, state.cluster_mutation_seq
        )
        boards_payload, edge_rows = cached
        return KgAnalysisOverview(
            notebook_id=notebook_id,
            generated_at=self._now(),
            level=level,
            ledger_state=_ledger_state(ledger),
            ledger_consistent=_ledger_consistent(ledger),
            state=state,
            artifacts=artifacts,
            boards=BoardOverview(
                freshness=_community_freshness(state, ledger),
                units=dict(BOARD_UNITS),
                payload=boards_payload,
            ),
            board_edges=_board_edge_view(
                ledger, state.kg_mutation_seq, state.cluster_mutation_seq,
                edge_rows, edge_limit,
            ),
        )

    # -------------------------------------------------------------- /sources
    def source_profiles(
        self,
        notebook_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        order: str = ORDER_SPARSE,
    ) -> SourceProfilePage:
        """来源画像的一页(默认按「与主体板块最不连通」排在前面)。

        **不记忆化**:页是参数化的(limit × offset × order),缓存全部页就是无界;而
        每页本身已经是「一次 COUNT + 一页 ≤200 行 + ≤200 次主键点查」,便宜到不值得
        为它承担一份会陈旧的缓存。

        ``present`` / ``absence`` 与总览里那一份同源(都看账本行在不在),所以两个端点
        对「这份产物在不在」永远给同一个答案。

        ⚠ 账本行不在时**连查都不查**明细表(见 `_detail_rows_are_readable`):
        ``present=False`` 却回一页行,就是同一份响应既说「这份产物不在」又把它的内容
        发出去。顺带省掉那次 `COUNT(*)`(O(来源数),生产 48 836 行)。
        """
        self._reject_inside_write_transaction("KG 分析来源画像")
        if order not in SOURCE_ORDERS:
            raise ValueError(
                f"KG 分析来源画像:order 只能是 {SOURCE_ORDERS} 之一,拿到 {order!r}"
            )
        limit = max(1, min(int(limit), KG_SOURCE_PAGE_MAX))
        offset = max(0, int(offset))
        total = 0
        rows: List[Dict[str, Any]] = []
        # ⚠ **四条语句,一个快照**(codex 第 8 轮 P2)。裸 `connect()` 在两个后端上都是
        # 「每条语句各取一个快照」(SQLite 自动提交 / PostgreSQL READ COMMITTED),而这
        # 一趟要读 state 行、账本、画像表的 COUNT 与那一页。并发的预计算是**整批重写**
        # 三张产物表:提交劈在中间,新写入的画像行就会被这一份响应盖上**上一代**账本的
        # 世代戳(`freshness` 与 `summary` 都取自那一行),`total` 与 `rows` 也可以互相
        # 对不上 —— 一份「有 48 836 个来源、这一页却是空的」的分页,读者无从分辨是并发
        # 还是数据坏了。方言细节留在 store 里(见 `read_snapshot`),本 service 不判后端。
        with self._unified_kg.read_snapshot() as db:
            state_row = self._unified_kg.state_row(db, notebook_id)
            ledger = self._unified_kg.kg_analysis_artifact_rows(db, notebook_id)
            if _detail_rows_are_readable(ARTIFACT_SOURCE_PROFILES, ledger):
                total, rows = self._unified_kg.kg_source_profile_page(
                    db,
                    notebook_id,
                    limit=limit,
                    offset=offset,
                    ascending=(order == ORDER_SPARSE),
                )
        state = _state_view(state_row)
        entry = ledger.get(ARTIFACT_SOURCE_PROFILES)
        return SourceProfilePage(
            notebook_id=notebook_id,
            generated_at=self._now(),
            present=entry is not None,
            absence=_absence(ARTIFACT_SOURCE_PROFILES, ledger),
            freshness=_artifact_freshness(
                ARTIFACT_SOURCE_PROFILES, entry, state.kg_mutation_seq,
                state.cluster_mutation_seq,
            ),
            kg_mutation_seq=state.kg_mutation_seq,
            order=order,
            limit=limit,
            offset=offset,
            total=total,
            returned=len(rows),
            has_more=(offset + len(rows)) < total,
            summary=(entry["payload"] if entry is not None else None),
            units=dict(SOURCE_PAGE_UNITS),
            rows=rows,
        )

    # ---------------------------------------------------------------- 缓存
    def _cache_get(self, key: tuple, signature: tuple):
        with self._overview_cache_lock:
            cached = self._overview_cache.get(key)
            if cached is not None and cached[0] == signature:
                self._overview_cache.move_to_end(key)
                return cached[1]
        return None

    def _cache_put(self, key: tuple, signature: tuple, value: tuple) -> None:
        with self._overview_cache_lock:
            self._overview_cache[key] = (signature, value)
            self._overview_cache.move_to_end(key)
            while len(self._overview_cache) > _OVERVIEW_CACHE_MAX:
                self._overview_cache.popitem(last=False)


# --------------------------------------------------------------- 纯装配函数
# 全部是纯函数(输入行 → 输出结构),不碰连接、不判后端 —— 便于逐条单测,也让
# 「同一批行在两个后端上装配出同一份报告」是结构性的而不是靠两边各自记得。


def _state_view(row: Any) -> KgState:
    """`unified_kg_state` 的行(或 None)→ 参照系。

    行缺失(从没写过 KG)不是错误:报告照出,每一格都在说「没有」。此时
    ``community_seq`` 取 -1(与列的默认值一致 = 从没建过社区),其余取 0。

    ``present`` 的语义是「有过 KG 历史」而**不是**「行存在」:自从
    create_notebook 在建库事务里写出生行(出处证书 source_index_backfilled,
    codex #601 R1 P2),零历史的行(kg_mutation_seq=0 且从未 rebuild)与行缺失
    对本视图是同一件事——按行存在判会让每个新建笔记本在分析页谎报一次
    「上次整理时的规模:0·0·0」,而前端正是用 ``present`` 区分「没有记录」
    与「规模是 0」的(kg-analysis-view.tsx 的 state.present 分支)。同一判据
    也覆盖 delete_notebook_kg 之后为保 certificate 重铸的行。
    """
    if row is None or (
        int(row["kg_mutation_seq"]) == 0 and not (row["last_rebuild_at"] or "")
    ):
        return KgState(
            present=False,
            kg_mutation_seq=0,
            community_seq=-1,
            cluster_mutation_seq=0,
            canonical_rel_seq=0,
            dirty=False,
            last_rebuild=LastRebuild(
                basis=BASIS_UNIFIED_REBUILD_SNAPSHOT,
                at="",
                object_count=0,
                relation_count=0,
                cluster_count=0,
                units=dict(STATE_UNITS),
            ),
        )
    return KgState(
        present=True,
        kg_mutation_seq=int(row["kg_mutation_seq"]),
        community_seq=int(row["community_seq"]),
        cluster_mutation_seq=int(row["cluster_mutation_seq"]),
        canonical_rel_seq=int(row["canonical_rel_seq"]),
        dirty=bool(row["dirty"]),
        last_rebuild=LastRebuild(
            basis=BASIS_UNIFIED_REBUILD_SNAPSHOT,
            at=str(row["last_rebuild_at"] or ""),
            object_count=int(row["object_count"]),
            relation_count=int(row["relation_count"]),
            cluster_count=int(row["cluster_count"]),
            units=dict(STATE_UNITS),
        ),
    )


def _ledger_level(ledger: Dict[str, Dict[str, Any]]) -> int:
    """产物描述的社区层级。

    两张明细表的账本 payload 都记了 `level`,取跨板块边那一份(它是**必需**的,来源
    画像可以合法缺席)。账本缺席时落回 0 —— `rebuild_communities` 的默认层,也是
    `communities` 表里唯一被写过的层。

    ⚠ 第 15 轮起写侧收紧了:那份不分 level 的账本只由 `DEFAULT_COMMUNITY_LEVEL`
    的构建写(见 `knowledge_lifecycle.rebuild_communities` 的方向五),所以这里读回来的
    实际上恒为默认层。**这一读仍然保留**:载荷里那个字段是产物自证「我描述哪一层」的
    唯一凭据,把它换成硬编码的 0 就等于把写侧的约束抄成一份读侧的假设 —— 两边一漂,
    报告会安静地把另一层的板块说成默认层的。
    """
    entry = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    if entry is None:
        return 0
    return int(entry["payload"].get("level", 0) or 0)


def _detail_rows_are_readable(kind: str, ledger: Dict[str, Dict[str, Any]]) -> bool:
    """这份产物的**明细表**这一趟能不能读 —— 判据只有一条:账本行在不在。

    两张明细表(`kg_community_edges` / `kg_source_profiles`)是被**单独查询**的,它们与
    账本之间没有外键、也没有任何数据库级约束保证同生同灭。正常写路径确实是同事务整批
    重写(`replace_kg_analysis_artifacts`)、同事务整批作废
    (`discard_board_dependent_kg_analysis_artifacts`),但那是**写侧的纪律**,不是读侧
    可以依赖的不变量:库被手工改过、一次迁移只删了一半、或者一个未来的写路径漏了一步,
    留下的就是「账本说没有、明细表里还有行」。

    此时无条件查明细表 = 同一份响应既报 ``present=False``,又把那些行发出去,让消费者
    (俯瞰图会照着画连线)呈现一份账本说不存在的产物。判据必须是账本行 —— 这是本特性
    从 T2 到 T3 反复声明的那条(见 `app.domain.kg_analysis_contracts.ARTIFACT_KINDS` 段头:
    「账本行的**存在与否**才是这份产物在不在的唯一判据,明细表的行数不是」)。

    ⚠ 反过来**不**成立:账本行在、明细表零行是完全正常的一档(单一板块的图
    legitimately 产出 0 条跨板块边),那一档必须照读、照报「在场且为空」。

    悬空的明细行不会被静默吞掉:账本档位(`_ledger_state`)与 `absence` 已经把「本该有
    却缺失」报成红档,读者看得到异常;而把悬空行也一并展示只会让那条异常自相矛盾。
    """
    return kind in ledger


def _signature_tracks_board_recasts(ledger: Dict[str, Dict[str, Any]]) -> bool:
    """板块被重铸时,签名会不会跟着动 —— 总览记忆化**唯一**的正确性前提。

    板块 id 只由 `replace_communities` 重铸,而它在**同一个事务**里作废依赖板块的账本行
    (`BOARD_DEPENDENT_ARTIFACT_KINDS`)。删掉一行就改了签名里的账本元组,这是这份缓存
    唯一 O(1) 的失效手段。

    ⚠ 但那次作废在**一行都没有**时是个 no-op(codex 第 10 轮 P2):上一轮预计算失败后
    账本可能整个为空,也可能只剩三条与板块无关的统计快照 —— 两种都没有任何一行会被它
    动到。而 `force=True` 的重建**不**抬 `kg_mutation_seq`,所以此时板块 id 已经换了一
    整套、签名却一个字节没动;若预计算再次失败,已预热的缓存会**无限期**继续吐上一套
    板块 id,直到 LRU 淘汰或进程重启,而端点自称读的是 live 快照。

    所以这一档**干脆不写缓存**。理由与代价:

    · 判据必须是「有没有会被那次作废动到的行」,不是「账本齐不齐」—— 它直接由作废覆盖
      的那个集合导出,那个集合改了这里自动跟着改。写成「齐全才缓存」会在「跨板块边在、
      来源画像缺」这种照样安全的一档白付一次板块列表读。
    · 代价是这一档每次请求都现读一次板块列表(生产 88 580 个板块,`communities` 上没有
      size 索引 —— 正是这份缓存要省的那笔钱)。但这一档是**异常状态**:预计算从没成功过
      或刚失败过,报告本来就在报「产物缺失」。不该为异常状态优化,更不该为它承担一份会
      无限期说谎的缓存。
    · 另两条路都不成立:签名里加一个从 `communities` 现算的「板块世代」是 O(板块数) 的
      读(第 7 轮已因此否决过一次,存进 `unified_kg_state` 则要加列 + bump SCHEMA);
      在替换时显式驱逐则跨层(缓存在 service 进程内、`replace_communities` 在 lifecycle
      层),而且离线 CLI 的重建跑在**另一个进程**里,根本驱逐不到。

    ⚠ **只门控写、不门控读**,而这不是漏了一半:安全态写下的那条记录带的是一份**含**
    依赖板块行的账本元组,不安全态的账本按定义不含任何一行,两个元组的 kind 集合不同、
    签名不可能相等,那条记录在这一档取不出来。再加一道读侧门控就是一条删掉也不会让任何
    测试报红的死代码 —— 本 PR 至今已抓出 17 个那种空守卫。
    """
    return any(kind in ledger for kind in BOARD_DEPENDENT_ARTIFACT_KINDS)


def _ledger_boards(ledger: Dict[str, Dict[str, Any]]) -> Optional[int]:
    """这一轮预计算记下的板块数;``None`` = 无从判断(跨板块边的账本行本身就缺席)。

    来源是跨板块边账本 payload 里的 ``communities`` —— 与写入口
    `check_artifact_payloads` 的复核**同一条**判据、同一个字段。它必须由账本自己回答,
    不能去数 `communities` 表:那是 O(板块数) 的读(生产 88 580 个板块),而且数的是
    **此刻**的板块,不是这一轮产物建在其上的那批。
    """
    entry = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    if entry is None:
        return None
    return int(entry["payload"].get("communities", 0) or 0)


def _ledger_state(ledger: Dict[str, Dict[str, Any]]) -> str:
    """账本整体处在哪一档:空 / 残缺 / 齐全。

    「必需」的集合与写侧共用 `required_artifact_kinds` —— 有板块时来源画像就是必需的,
    零板块时它合法缺席(见 `app.domain.kg_analysis_contracts.OPTIONAL_ARTIFACT_KINDS`)。

    ⚠ 这里**必须**跟着板块数走,不能只看那四份恒定必需的。否则「有板块却少了来源画像」
    这一档会同屏给出两个互相矛盾的说法:`_absence` 判「本该有却缺失」(红档),而档位
    却报「齐全」。一份能同时说这两句话的完整性报告本身就不可信 —— 读者无从知道该信哪
    一句,而这正是本视图存在的理由(codex 第 3 轮评审复现)。

    跨板块边账本本身缺席时 `_ledger_boards` 给 None:此时那四份必需的已经少了一份,
    档位落 `partial`,板块数是多少都不改变结论。
    """
    if not ledger:
        return LEDGER_EMPTY
    required = required_artifact_kinds(has_boards=bool(_ledger_boards(ledger)))
    return LEDGER_COMPLETE if required <= set(ledger) else LEDGER_PARTIAL


def _ledger_consistent(ledger: Dict[str, Dict[str, Any]]) -> bool:
    """在场的产物是不是**同一轮**预计算的产物。

    `replace_kg_analysis_artifacts` 把整批写在一个事务里、全部盖同一个 seq,所以正常
    情况下恒为 True。为 False 只可能是库被手工改过 —— 那种时候报告里的数字互相不可比,
    读者必须知道。
    """
    seqs = {int(entry["kg_mutation_seq"]) for entry in ledger.values()}
    return len(seqs) <= 1


def _absence(kind: str, ledger: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """这一份产物为什么不在(在场则 None)。

    合法缺席只有一档:零板块的库不写来源画像(T2 的 S4 决定 —— 那张表单独看是在说
    「所有来源都关联稀疏」,一句彻头彻尾的谎话,所以让它真的缺失)。判据是跨板块边
    账本里的 `communities == 0`,与写入口 `check_artifact_payloads` 的复核**同一条**。
    """
    if kind in ledger:
        return None
    if not ledger:
        return ABSENCE_NEVER_COMPUTED
    if kind not in OPTIONAL_ARTIFACT_KINDS:
        return ABSENCE_UNEXPECTED
    boards = _ledger_boards(ledger)
    if boards is None:
        # 跨板块边的账本行也不在 —— 判不出「这一轮到底有没有板块」,所以判不出这次
        # 缺席合不合法。存疑时报异常,不给一个可能是假的「合法缺席」。
        return ABSENCE_UNEXPECTED
    return ABSENCE_EXPECTED if boards == 0 else ABSENCE_UNEXPECTED


def _artifact_freshness(
    kind: str,
    entry: Optional[Dict[str, Any]],
    current_seq: int,
    current_cluster_seq: int,
) -> Freshness:
    """一份产物的两条世代线 + ``stale``。

    ⚠ ``stale`` **不是**在这里重新推一遍,而是调写侧的闸用的同一个
    `artifact_is_current`。两处各写一份判据必然漂,而漂出来的表现就是一份自相矛盾的
    报告 —— 闸说「不用重算」、读侧说「陈旧」,或者反过来(闸短路、读侧报「与当前
    一致」,而那正是 codex 第 5 轮报的那一条)。判据只写一次是这个特性反复被打回的
    那个教训(第 3 轮的 `_ledger_state` 与写侧 required 集合分岔是同一个病)。

    判据是**三值**的,第三值(``None`` = 板块产物由「只补账本」那条路径产出,板块划分
    建在哪一代合并结果上无从判断)在这里**原样透出去**,不折成 True/False:折成 False
    就是把一份混合世代的产物说成「与当前一致」(codex 第 7 轮 P2 报的正是这一条),
    折成 True 又是在没有依据的情况下报一个落后量。此时 ``built_at_cluster_seq`` /
    ``cluster_seq_behind`` 同样是 None,与 `_community_freshness` 里板块自己那一格
    (它的合并世代同样无从判断)说的是同一件事、用的是同一种表达。
    """
    if entry is None:
        return Freshness(
            basis=ARTIFACT_BASIS[kind],
            built_at_seq=None,
            seq_behind=None,
            stale=None,
        )
    built = int(entry["kg_mutation_seq"])
    payload = entry["payload"]
    built_cluster = artifact_cluster_seq(kind, payload)
    current = artifact_is_current(
        kind, built, payload,
        seq=current_seq, cluster_seq=current_cluster_seq,
    )
    return Freshness(
        basis=ARTIFACT_BASIS[kind],
        built_at_seq=built,
        # 不 clamp:账本比库还新是「被手工改过」的信号,压成 0 等于藏起异常。
        seq_behind=int(current_seq) - built,
        stale=(None if current is None else not current),
        built_at_cluster_seq=built_cluster,
        cluster_seq_behind=(
            None if built_cluster is None
            else int(current_cluster_seq) - built_cluster
        ),
    )


def _community_freshness(
    state: KgState, ledger: Dict[str, Dict[str, Any]]
) -> Freshness:
    """板块列表的新鲜度 —— **两条世代线**,与产物那几份同一套判据。

    KG 世代那条线的戳是 `community_seq`(板块建于哪个 KG 状态,而不是账本里的任何一个
    seq)。``community_seq == -1`` = 从没建过社区 → 与产物缺席同一种表达(三个 None),
    而不是谎报「落后 N 次」。

    ⚠ **合并世代那条线不能省,省掉就是谎报「与当前一致」**(codex 第 8 轮 P2)。
    簇写路径刻意不动 `kg_mutation_seq`,所以纯合并写入之后 `community_seq` 与
    `kg_mutation_seq` 仍然相等 —— 旧判据(只比 KG 世代)据此把这一格判成
    ``stale=False``,而同一屏上依赖板块的两份产物已经如实报「对不上合并进度」。
    一份能同时说出这两句话的报告本身就不可信。

    板块划分的合并世代由 `board_partition_cluster_seq` 从**账本**读(那不是「拿产物的
    属性冒充板块的属性」:第 7 轮起那个戳记的就是板块划分建在哪一代合并结果上,而板块
    一被重铸,那两行就在同事务里作废 —— 完整理由见该函数)。判据 `board_partition_is_current`
    与产物侧共用 `_generation_verdict` / `_cluster_generation_verdict`,所以这一格与依赖
    板块的那两份产物**不可能**再给出互相矛盾的说法。

    ⚠ 仍然存在的缺口(需要一列,本次不做):板块划分自己那张表里没有这个戳,账本被
    「只补账本」那一轮覆盖之后,上一轮记下的整数世代就没了 —— 那一档如实报「无从判断」
    (``stale=None``),不编一个 0。真正让板块跟上合并结果的仍是 `rebuild_unified_kg`
    收尾那次 `force=True` 的 `rebuild_communities`。
    """
    if state.community_seq < 0:
        return Freshness(
            basis=BASIS_COMMUNITY_SNAPSHOT,
            built_at_seq=None,
            seq_behind=None,
            stale=None,
        )
    built_cluster = board_partition_cluster_seq(ledger)
    current = board_partition_is_current(
        ledger,
        state.community_seq,
        seq=state.kg_mutation_seq,
        cluster_seq=state.cluster_mutation_seq,
    )
    return Freshness(
        basis=BASIS_COMMUNITY_SNAPSHOT,
        built_at_seq=state.community_seq,
        # 不 clamp,理由与 `_artifact_freshness` 同:比当前还新是异常信号,不是 0。
        seq_behind=state.kg_mutation_seq - state.community_seq,
        stale=(None if current is None else not current),
        built_at_cluster_seq=built_cluster,
        cluster_seq_behind=(
            None if built_cluster is None
            else state.cluster_mutation_seq - built_cluster
        ),
    )


def _artifact_views(
    ledger: Dict[str, Dict[str, Any]], current_seq: int, current_cluster_seq: int
) -> List[ArtifactView]:
    """**恒定长度、恒定顺序**的五条 —— 缺席的那几份也在列表里,带 absence 说明。

    刻意不「只返回在场的」:那样一来「这份产物不在」就要靠调用方自己去和 ARTIFACT_KINDS
    做差集,而漏做差集的表现是报告上少一张卡片、没有任何提示。
    """
    views: List[ArtifactView] = []
    for kind in ARTIFACT_KINDS:
        entry = ledger.get(kind)
        views.append(
            ArtifactView(
                kind=kind,
                present=entry is not None,
                optional=kind in OPTIONAL_ARTIFACT_KINDS,
                absence=_absence(kind, ledger),
                freshness=_artifact_freshness(
                    kind, entry, current_seq, current_cluster_seq
                ),
                created_at=(str(entry["created_at"]) if entry is not None else ""),
                units=dict(ARTIFACT_UNITS[kind]),
                payload=(entry["payload"] if entry is not None else None),
            )
        )
    return views


def _board_edge_view(
    ledger: Dict[str, Dict[str, Any]],
    current_seq: int,
    current_cluster_seq: int,
    edge_rows: List[Tuple[str, str, int]],
    limit: int,
) -> BoardEdgeView:
    entry = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    payload = entry["payload"] if entry is not None else {}
    cross_weight = int(payload.get("cross_weight", 0) or 0)
    returned_weight = sum(int(weight) for _src, _dst, weight in edge_rows)
    return BoardEdgeView(
        present=entry is not None,
        freshness=_artifact_freshness(
            ARTIFACT_COMMUNITY_EDGES, entry, current_seq, current_cluster_seq
        ),
        limit=max(1, min(int(limit), KG_COMMUNITY_EDGES_MAX)),
        returned=len(edge_rows),
        returned_weight=returned_weight,
        stored=int(payload.get("edges", 0) or 0),
        stored_total=int(payload.get("edges_total", 0) or 0),
        stored_truncated=bool(payload.get("truncated", False)),
        edge_limit=int(payload.get("edge_limit", 0) or 0),
        cross_weight=cross_weight,
        # 分母为 0 时给 None 而不是 0.0:「一条跨板块边都没有」与「覆盖了 0%」是两件
        # 不同的事,后者会被读成「图画漏了」。
        weight_coverage=(returned_weight / cross_weight) if cross_weight else None,
        units=dict(BOARD_EDGE_UNITS),
        edges=[
            {"src": src, "dst": dst, "weight": int(weight)}
            for src, dst, weight in edge_rows
        ],
    )


def _signature(state: KgState, ledger: Dict[str, Dict[str, Any]]) -> tuple:
    """记忆化的**廉价**失效键 —— 贵的那两块(板块列表 / 跨板块边)据此复用。

    含 state 行的全部 seq + dirty,以及账本每一行的 (kind, seq, created_at)。
    ``created_at`` 不可省:一次 ``force=True`` 的重建会在**同一个** `kg_mutation_seq`
    上重铸板块 id 并整批重写产物,只看 seq 的话缓存不会失效、板块 id 会串到上一套。

    ⚠ 这个键**追不到板块 id 本身**:它只经账本间接感知重铸(重铸同事务作废依赖板块的
    账本行)。账本里没有那样一行时它就是瞎的 —— 那一档由 `_signature_tracks_board_recasts`
    在写缓存前拦掉,不是靠往这里再加字段。
    """
    return (
        state.present,
        state.kg_mutation_seq,
        state.community_seq,
        state.cluster_mutation_seq,
        state.canonical_rel_seq,
        state.dirty,
        tuple(
            (kind, int(entry["kg_mutation_seq"]), str(entry["created_at"]))
            for kind, entry in sorted(ledger.items())
        ),
    )
