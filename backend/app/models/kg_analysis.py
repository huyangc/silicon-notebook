"""KG 质量分析视图(T3)的 API 响应模型。

`KgAnalysisService`(`app/services/kg_analysis.py`)产出内部 dataclass;这里是它的
pydantic 传输层。字段全是**内部契约**:``basis`` / ``absence`` / ``ledger_state`` /
``order`` / ``units`` 里的取值都是内部代号 —— 面向用户的界面词由 T4 的前端映射层负责,
后端不出中文用户文案(界面词汇守卫红线)。

三条读这份契约时必须先知道的事(否则 T4 极易做错卡片):

1. **单位不统一,而且不可互比。** 每个块都带 ``units``(字段名 → 单位代号):
   ``canonical``(合并后的节点)/ ``objects``(knowledge_objects 行)/
   ``cluster_member_rows``(concept_clusters 行)/ ``community_pairs``(板块对)/
   ``relation_rows``(关系行)/ ``sources`` / ``ratio`` / ``seq`` / ``level``。
   同屏并列 ``head_members``(canonical)与 ``n_graph_objects``(objects)是
   apples/oranges,相除得不到任何有意义的比例。

2. **口径来源必须一起渲染。** 每个块带 ``basis``:``usable_live`` = 实时 USABLE 口径
   (在预计算那一刻算的);``community_snapshot`` = 上次社区重建写下的划分;
   ``unified_rebuild_snapshot`` = 上次 unified-KG 重建时的规模。三者同屏并列而不标来源,
   读者没有任何线索分辨为什么数字对不上。

3. **缺失 ≠ 为空。** ``present`` 说的是账本行在不在(唯一判据),``absence`` 说的是
   为什么不在:``never_computed``(整个库没跑过预计算)/ ``expected``(零板块库不写
   来源画像,唯一合法的一档)/ ``unexpected``(账本非空却少了这一份 —— 异常)/
   ``upgrade_pending``(升级后还没生成:账本每一行都建于账本契约戳出现之前,缺的是
   新版本才有的问题清单 —— 灰色,不是异常)。
   一张空的跨板块边表是 legitimate 的(单一板块的图),靠行数分不出来。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class Freshness(BaseModel):
    """一份产物的新鲜度 + 口径来源。

    ``built_at_seq`` 是它建于哪个 ``kg_mutation_seq``;``seq_behind`` = 当前 seq 减去它
    (**不 clamp 到 0**:负值意味着账本比当前还新,是库被手工改过的信号,压成 0 等于
    把异常藏起来)。产物缺席 / 社区从没建过时三个都是 null,不是 0 —— 0 会被读成
    「刚刚建好」。

    ``built_at_cluster_seq`` / ``cluster_seq_behind`` 是**合并结果**那条世代线
    (``cluster_mutation_seq``),必须独立于 KG 世代:合并的写路径刻意不动
    ``kg_mutation_seq``,所以只看前一条的话,一次纯合并之后簇大小直方图与最大簇榜单
    已经陈旧、报告却会说「与当前一致」。两个字段为 null 有三种情形:这份产物与合并
    结果无关(``relation_provenance`` —— 它的查询只读关系表);它缺席 / 没盖这个戳;
    或者它是**依赖主题板块**的那两份、而这一轮只补了账本 —— 那时板块划分是库里现成的,
    建在哪一代合并结果上没有地方记,产物因此是混合世代的,只能如实报「无从判断」。

    ``stale`` 是两条线的**三值**合取:任一条明确落后即 true;两条都对齐即 false;
    没有明确落后但有一条无从判断即 **null**(上面第三种情形,以及产物缺席)。
    null 不等于 false —— 把「无从判断」显示成「与当前一致」正是这个视图存在的理由的
    反面;消费方拿 ``present`` 分辨「缺席」与「在场但世代无从判断」。
    """

    basis: str
    built_at_seq: Optional[int] = None
    seq_behind: Optional[int] = None
    stale: Optional[bool] = None
    built_at_cluster_seq: Optional[int] = None
    cluster_seq_behind: Optional[int] = None


class ArtifactView(BaseModel):
    """账本里的一份预计算产物(或它的缺席)。

    ``payload`` 是产物本身(三条统计快照)或它的汇总与口径参数(两张明细表的账本行);
    形状随 ``kind`` 变,故声明成自由 dict。``optional`` 标出「这一份允许合法缺席吗」
    —— 只有 ``source_profiles`` 是 true。
    """

    kind: str
    present: bool
    optional: bool
    absence: Optional[str] = None
    freshness: Freshness
    created_at: str = ""
    units: dict[str, str]
    payload: Optional[dict[str, Any]] = None


class LastRebuild(BaseModel):
    """上一次 unified-KG 重建时的规模快照 —— 与当前真实计数一比即可量化陈旧程度。

    ⚠ ``object_count`` 数的是「非 deprecated」的对象。今天它与本报告的 USABLE 口径
    恰好相等(deprecated 是唯一的非可用状态),但那是状态词表的巧合而不是同一条判据。
    """

    basis: str
    at: str = ""
    object_count: int
    relation_count: int
    cluster_count: int
    units: dict[str, str]


class KgState(BaseModel):
    """``unified_kg_state`` —— 全部新鲜度判断的参照系。

    ``present=false`` = 这个 notebook 连状态行都没有(从没写过 KG)。
    ``community_seq = -1`` = 从没建过社区。
    """

    present: bool
    kg_mutation_seq: int
    community_seq: int
    cluster_mutation_seq: int
    canonical_rel_seq: int
    dirty: bool
    last_rebuild: LastRebuild
    units: dict[str, str]


class BoardOverview(BaseModel):
    """主题板块列表。**实时读**板块表,而那张表本身是上次社区重建的快照 —— 所以它的
    戳是 ``community_seq``,不是账本里的任何一个 seq。

    ``payload``:``{level, total, returned, truncated, limit, top_members_limit,
    communities: [{id, board_key, size, top_members, top_members_truncated}]}``。
    ``id`` 每次重新划分都重铸;``board_key`` 是板块的跨代身份(新旧成员互为最佳且
    Jaccard ≥ 1/2 时沿用),供快照对比认出「同一个板块」。
    """

    freshness: Freshness
    units: dict[str, str]
    payload: dict[str, Any]


class BoardEdge(BaseModel):
    src: str
    dst: str
    weight: int


class BoardEdgeView(BaseModel):
    """跨板块边的 top-N + **两级截断**的显式披露。

    · 落库级(预计算):``stored`` 落库行数 / ``stored_total`` 截断前的板块对总数 /
      ``stored_truncated`` / ``edge_limit``。
    · 请求级(本次):``returned`` ≤ ``limit``;``weight_coverage`` = 返回边权 ÷
      ``cross_weight``(全部跨板块边权,图统计量,不随任何一级上限变化)。
      一条跨板块边都没有时 ``weight_coverage`` 是 null 而不是 0 —— 后者会被读成
      「这张图画漏了」。
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
    weight_coverage: Optional[float] = None
    units: dict[str, str]
    edges: list[BoardEdge]


class FindingSeq(BaseModel):
    """两条世代线的一对戳(KG 写入 / 合并结果)。"""

    kg_seq: Optional[int] = None
    cluster_seq: Optional[int] = None


class FindingsRelink(BaseModel):
    """「补上关联」的持久标记(设计 2026-10-19 §4.5)。``may_change=false`` = 跑过、之后
    没有新内容进来:它确定且幂等,再跑一次也补不上 —— F5 移出待处理、不放按钮。"""

    attempted: bool
    at_kg_seq: Optional[int] = None
    at: Optional[str] = None
    may_change: Optional[bool] = None


class FindingsSummary(BaseModel):
    """问题清单的账本摘要(总览里那一格;只读账本 + 标记,不读明细)。

    ``present=false`` 时 ``absence`` 说为什么(``never_computed`` / ``unexpected`` /
    ``upgrade_pending`` —— 最后一档是灰色:升级后还没生成,不是数据坏了),其余为 null /
    空;「还没生成」绝不渲染成「没有问题」。``rules`` 按规则代号为键(F1…F5、S1),每条
    ``{status: ran|skipped, reason?, total, persisted, truncated, …计数}``;S1 的样例名在
    ``dev_items``。``structure`` 是只依赖连通分量的 S3 结构数;``comparison`` 是与上一次的
    对比摘要(不可比时 ``comparable=false`` + ``reason``)。单位见 ``units``。"""

    present: bool
    absence: Optional[str] = None
    freshness: Freshness
    rules_version: Optional[int] = None
    built_at: Optional[FindingSeq] = None
    created_at: str = ""
    counts: Optional[dict[str, Any]] = None
    rules: dict[str, dict[str, Any]]
    dev_items: list[dict[str, Any]]
    structure: Optional[dict[str, Any]] = None
    comparison: Optional[dict[str, Any]] = None
    relink: Optional[FindingsRelink] = None
    units: dict[str, str]


class MergeCodeState(BaseModel):
    """上次合并建于哪一版合并规则(``stored``)与当前代码(``current``)。``matches``:
    true / false;**null = 还没合并过**(``stored`` 为空串,如删除知识图谱之后)——
    那不是「合并规则已更新」。"""

    stored: str
    current: str
    matches: Optional[bool] = None


class KgAnalysisResponse(BaseModel):
    """总览。``artifacts`` **恒为六条、恒定顺序**,缺席的那几份也在里面(带 absence),
    这样「少了一份产物」不会表现成「少了一张卡片、没有任何提示」。

    ``ledger_consistent=false`` = 在场的产物不是同一轮预计算的(正常写路径不可能产生,
    只可能是库被手工改过);此时报告里的数字互相不可比。
    """

    notebook_id: str
    generated_at: str
    level: int
    ledger_state: str
    ledger_consistent: bool
    state: KgState
    artifacts: list[ArtifactView]
    boards: BoardOverview
    board_edges: BoardEdgeView
    findings_summary: FindingsSummary
    merge_code: MergeCodeState


class FindingSubject(BaseModel):
    """一条问题指向什么:``kind`` = source / source_pair / concept_group / notebook;
    ``key`` 是稳定主键(F2 两个来源 id 用 ``|`` 拼;F4 是 squash key);``label`` 是发布时
    存下的标题或名字快照(来源删了也说得出是谁)。"""

    kind: str
    key: str
    label: str = ""


class FindingAction(BaseModel):
    """建议动作(从明细派生,不落库):``kind`` = delete_source / reparse_source /
    review_merge / relink;``capability`` 是对应既有入口的守卫名;``source_id`` 是删除 /
    重新解析对准的那一篇。出不出按钮由前端按既有入口的同一判据决定,后端仍是权限的
    唯一判据。"""

    kind: str
    capability: str = ""
    source_id: Optional[str] = None


class FindingImpact(BaseModel):
    objects: int


class FindingLiveSource(BaseModel):
    """读这一页时问题涉及的来源**此刻**的样子(主键点查)。隐藏来源(Memory / knowhow)
    的标题置空。``processing`` = 正在排队 / 解析 / 抽取。"""

    source_id: str
    exists: bool
    title: str = ""
    source_type: str = ""
    processing: bool = False


class FindingRow(BaseModel):
    """一条问题。``status``(读侧派生):open / handled(已处理,更新后核验,仍在待处理
    区)/ dismissed / decided_not_same(F4,用户已判定不是同一个)/ relink_spent(F5,
    补上关联已跑过、之后没有新内容)。``handled_reason``:source_deleted /
    source_processing / source_reprocessed / merge_confirmed / relinked。证据只放判据真正
    用到的数与 ≤5 个样例名(来自可见来源),单位在页的 ``evidence_units``。"""

    rule_id: str
    severity: str
    subject: FindingSubject
    subject_refs: list[str]
    evidence: dict[str, Any]
    impact: FindingImpact
    actions: list[FindingAction]
    first_seen: FindingSeq
    status: str
    handled_reason: Optional[str] = None
    dismissed_at: Optional[str] = None
    live_sources: list[FindingLiveSource]


class FindingsPageResponse(BaseModel):
    """问题清单的一页(一个区):``zone`` = open(待处理:open + handled)/ passed
    (decided_not_same + relink_spent)/ dismissed。``built_at`` 是整份清单建于哪一代(不
    逐行记);``total`` 是这个区的全部行数,``has_more`` 让分页不必靠 ``returned < limit``
    猜。账本行不在时 ``present=false``、``rows`` 为空。"""

    notebook_id: str
    generated_at: str
    present: bool
    absence: Optional[str] = None
    freshness: Freshness
    built_at: Optional[FindingSeq] = None
    rules_version: Optional[int] = None
    zone: str
    limit: int
    offset: int
    total: int
    returned: int
    has_more: bool
    units: dict[str, str]
    evidence_units: dict[str, dict[str, str]]
    rows: list[FindingRow]


class SourceProfileRow(BaseModel):
    """一个来源的板块画像。

    ``n_graph_objects`` 是**落进主题板块**的对象数(四类对象都算,不是
    ``object_type='concept'`` 的对象数),它是 ``top_share`` / ``mainstream_share``
    的分母。``source_missing`` = 这个 source_id 在来源表里已经不在了(``source_id``
    没有外键,历史清理会留下孤儿引用)——标题为空到底是「没标题」还是「已删除」,
    读报告的人有权分辨。
    """

    source_id: str
    title: str = ""
    source_missing: bool = False
    n_objects: int
    n_graph_objects: int
    top_community_id: str = ""
    top_share: float
    community_spread: int
    mainstream_share: float


class SourceProfilePageResponse(BaseModel):
    """来源画像的一页。

    ``order``:``sparse`` = 与主体板块最不连通的排前面(「哪些来源不属于这里」的答案),
    ``connected`` = 反向。``total`` 是该 notebook 的全部画像行数,``has_more`` 让分页
    不必靠 ``returned < limit`` 猜。``summary`` 是账本行的 payload(口径参数:覆盖阈值、
    头部板块数、head/total 成员数),产物缺席时为 null。
    """

    notebook_id: str
    generated_at: str
    present: bool
    absence: Optional[str] = None
    freshness: Freshness
    kg_mutation_seq: int
    order: str
    limit: int
    offset: int
    total: int
    returned: int
    has_more: bool
    summary: Optional[dict[str, Any]] = None
    units: dict[str, str]
    rows: list[SourceProfileRow]
