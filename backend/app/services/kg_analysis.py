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
`source_community_counts`(`tests/test_kg_analysis_service.py` 有一条 spy 守卫钉住这点)。

请求路径上真正跑的 SQL 只有五条,全部按行数有界:
  · `state_row`                  单行主键点读
  · `kg_analysis_artifact_rows`  ≤5 行复合主键点读
  · `kg_community_edges_top`     ≤20 万行(T2 硬上限)的分片扫 + 有界 top-N
  · `community_overview`         COUNT + `ORDER BY size DESC LIMIT ≤200` + ≤200 次有界 top-K
  · `kg_source_profile_page`     COUNT + 一页 ≤200 行 + ≤200 次主键点查(仅 /sources)

**随库规模线性增长的有两条,不是一条**(别把这句读成「只有 community_overview 要留意」):

  · `community_overview` —— 板块数没有结构性上限,而 `communities` 上**没有 size 索引**,
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
    OPTIONAL_ARTIFACT_KINDS,
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
    ARTIFACT_CLUSTER_HISTOGRAM: _HISTOGRAM_UNITS,
    ARTIFACT_LARGEST_CLUSTERS: _LARGEST_CLUSTERS_UNITS,
    ARTIFACT_RELATION_PROVENANCE: _RELATION_PROVENANCE_UNITS,
    ARTIFACT_COMMUNITY_EDGES: _COMMUNITY_EDGES_UNITS,
    ARTIFACT_SOURCE_PROFILES: _SOURCE_PROFILES_UNITS,
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
    """一份产物的新鲜度三件套 + 口径来源。

    ``built_at_seq`` 是它建于哪个 ``kg_mutation_seq``;``seq_behind`` 是当前 seq 减去
    它 —— **不 clamp 到 0**:账本比当前还新是一种不该发生的状态(库被手工改过),把它
    压成 0 等于替读者把异常藏起来。产物缺席时三个都是 None(不是 0 —— 0 会被读成
    「刚建好」)。
    """

    basis: str
    built_at_seq: Optional[int]
    seq_behind: Optional[int]
    stale: Optional[bool]


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

    ``present=False`` 表示这个 notebook 连 `unified_kg_state` 行都没有(从没写过 KG)。
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

    所以它的新鲜度戳是 ``unified_kg_state.community_seq``(板块建于哪个 KG 状态),
    不是账本里的任何一个 seq。
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

    - ``database``:一个读快照来源。后端相关(sqlite/postgres 各自的 database),由构造
      方注入,本 service 只当它是「能 connect() 出读连接、并能回答 in_write_transaction
      的东西」。
    - ``unified_kg``:后端相关的 **UnifiedKgStore 实例**。本 service 只调它的**有界读**
      (`state_row` / `kg_analysis_artifact_rows` / `kg_community_edges_top` /
      `kg_source_profile_page` / `community_overview`),**绝不**调那四条全表重活。
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

        ``unified_kg.community_overview`` 自己也有一道同名守卫(它自开连接),但本
        service 的其余四条读是 connection-taking 的、骑本方法开的读连接 —— 那道守卫
        对它们不生效。而危害是一样的:

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

        ⚠ **签名里为什么没有「板块世代」这一项,以及为什么不需要。** 板块 id 只由
        `replace_communities` 重铸,而写侧在**同一个事务**里作废依赖板块的两份账本行
        (见 `kg_analysis_precompute.BOARD_DEPENDENT_ARTIFACT_KINDS`)。板块一换,账本
        必变、签名必变 —— 包括「force 重建成功、随后的预计算恰好失败」那一档:那正是
        评审报出来的窗口,而它此前**不是**一个瞬时窗口而是永久的(账本一行没动、seq
        也没变,缓存会一直吐上一套板块,直到 LRU 淘汰或进程重启,而本方法自称读的是
        live 快照)。

        为什么世代只能由**写侧推**、不能由读侧拉:任何从 `communities` 现算的世代标记
        都是 O(板块数) 的读(生产 88 580 个板块,而 `communities` 上没有 size 索引),
        那恰恰就是这份缓存要省掉的那笔钱 —— 把它放进签名等于每次请求都付一遍。
        """
        self._reject_inside_write_transaction("KG 分析总览")
        with self._database.connect() as db:
            state_row = self._unified_kg.state_row(db, notebook_id)
            ledger = self._unified_kg.kg_analysis_artifact_rows(db, notebook_id)
        state = _state_view(state_row)
        level = _ledger_level(ledger)
        artifacts = _artifact_views(ledger, state.kg_mutation_seq)
        signature = _signature(state, ledger)
        key = (notebook_id, level, int(board_limit), int(top_members), int(edge_limit))
        cached = self._cache_get(key, signature)
        if cached is None:
            # ⚠ 顺序:板块列表先取(它自开只读快照),再开一条读连接取边。刻意**不**把
            # 两者套进同一个 connect():SQLite 上那是同一条线程复用连接(没差),但
            # PostgreSQL 上会变成嵌套借池连接。跨查询本来就没有共享快照(T2 的
            # `_precompute_kg_analysis` 已如实记过这一点),所以套起来买不到一致性。
            boards_payload = self._unified_kg.community_overview(
                notebook_id, level=level, limit=board_limit, top_k=top_members
            )
            with self._database.connect() as db:
                edge_rows = self._unified_kg.kg_community_edges_top(
                    db, notebook_id, edge_limit
                )
            cached = (boards_payload, edge_rows)
            self._cache_put(key, signature, cached)
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
                freshness=_community_freshness(state),
                units=dict(BOARD_UNITS),
                payload=boards_payload,
            ),
            board_edges=_board_edge_view(
                ledger, state.kg_mutation_seq, edge_rows, edge_limit
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
        """
        self._reject_inside_write_transaction("KG 分析来源画像")
        if order not in SOURCE_ORDERS:
            raise ValueError(
                f"KG 分析来源画像:order 只能是 {SOURCE_ORDERS} 之一,拿到 {order!r}"
            )
        limit = max(1, min(int(limit), KG_SOURCE_PAGE_MAX))
        offset = max(0, int(offset))
        with self._database.connect() as db:
            state_row = self._unified_kg.state_row(db, notebook_id)
            ledger = self._unified_kg.kg_analysis_artifact_rows(db, notebook_id)
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
                ARTIFACT_SOURCE_PROFILES, entry, state.kg_mutation_seq
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
    """
    if row is None:
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
    """
    entry = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    if entry is None:
        return 0
    return int(entry["payload"].get("level", 0) or 0)


def _ledger_state(ledger: Dict[str, Dict[str, Any]]) -> str:
    """账本整体处在哪一档:空 / 残缺 / 齐全。

    「齐全」= 四份必需的都在(来源画像是唯一允许缺席的一份,见
    `kg_analysis_precompute.OPTIONAL_ARTIFACT_KINDS`)。
    """
    if not ledger:
        return LEDGER_EMPTY
    required = set(ARTIFACT_KINDS) - set(OPTIONAL_ARTIFACT_KINDS)
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
    edges = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    if edges is None:
        return ABSENCE_UNEXPECTED
    boards = int(edges["payload"].get("communities", 0) or 0)
    return ABSENCE_EXPECTED if boards == 0 else ABSENCE_UNEXPECTED


def _artifact_freshness(
    kind: str, entry: Optional[Dict[str, Any]], current_seq: int
) -> Freshness:
    if entry is None:
        return Freshness(
            basis=ARTIFACT_BASIS[kind],
            built_at_seq=None,
            seq_behind=None,
            stale=None,
        )
    built = int(entry["kg_mutation_seq"])
    behind = int(current_seq) - built
    return Freshness(
        basis=ARTIFACT_BASIS[kind],
        built_at_seq=built,
        seq_behind=behind,
        stale=behind != 0,
    )


def _community_freshness(state: KgState) -> Freshness:
    """板块列表的新鲜度:它读的是 `communities` 表,戳是 `community_seq`。

    ``community_seq == -1`` = 从没建过社区 → 与产物缺席同一种表达(三个 None),
    而不是谎报「落后 N 次」。
    """
    if state.community_seq < 0:
        return Freshness(
            basis=BASIS_COMMUNITY_SNAPSHOT,
            built_at_seq=None,
            seq_behind=None,
            stale=None,
        )
    behind = state.kg_mutation_seq - state.community_seq
    return Freshness(
        basis=BASIS_COMMUNITY_SNAPSHOT,
        built_at_seq=state.community_seq,
        seq_behind=behind,
        stale=behind != 0,
    )


def _artifact_views(
    ledger: Dict[str, Dict[str, Any]], current_seq: int
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
                freshness=_artifact_freshness(kind, entry, current_seq),
                created_at=(str(entry["created_at"]) if entry is not None else ""),
                units=dict(ARTIFACT_UNITS[kind]),
                payload=(entry["payload"] if entry is not None else None),
            )
        )
    return views


def _board_edge_view(
    ledger: Dict[str, Dict[str, Any]],
    current_seq: int,
    edge_rows: List[Tuple[str, str, int]],
    limit: int,
) -> BoardEdgeView:
    entry = ledger.get(ARTIFACT_COMMUNITY_EDGES)
    payload = entry["payload"] if entry is not None else {}
    cross_weight = int(payload.get("cross_weight", 0) or 0)
    returned_weight = sum(int(weight) for _src, _dst, weight in edge_rows)
    return BoardEdgeView(
        present=entry is not None,
        freshness=_artifact_freshness(ARTIFACT_COMMUNITY_EDGES, entry, current_seq),
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
