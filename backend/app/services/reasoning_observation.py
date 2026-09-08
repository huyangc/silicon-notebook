"""动作观察账(设计稿 2026-09-07 §6.1)。

这个模块是**纯数据 + 纯函数**:一条 `ActionObservation` 的形状、一份从既有
`TraceStep` 的**结构化 detail** 折出观察的对照表,以及一次有界渲染。它不做
I/O、不读 settings、不认识 `ReasoningRetriever`。

**产地:轨迹记账点的一个 v2 专用转换器,不是各执行器里的 `state.observe(...)`。**
两条路都能满足「观察由实际执行结果产生」,选这一条有三个具体理由:

1. `run()` 是零松弛长度天花板下的热函数,它的动作分发链有十几条 `elif`。在每
   一条里插一句 `state.observe(...)` 就是十几行,而这十几行没有一行是新逻辑
   ——它们只是把执行处刚算出来、马上要写进 trace detail 的同一批数字再抄一遍。
   抄写点越多,漏抄一处的概率越高,而漏掉的那一条在观察账里表现为「这个动作
   从没执行过」——恰恰是设计稿点名禁止的那种谎报。
2. 首轮 seed 与循环动作因此**天然**共用同一次转换(设计稿 §6.1 的硬要求):
   两者都经过同一个 `record(TraceStep(...))`,不需要在 `_first_round_*` 里再挂
   一套并行的观察调用。
3. trace detail 已经是**结构化具名键**(`found` / `new` / `returned` /
   `reason` / `phase` …),不是自然语言。转换读的是这些键,绝不解析 summary 那
   句中文,也绝不从模型的 `reason` 反推发生过什么。

这条路的代价是它对 detail 的键**有依赖**:执行处改名一个键,观察就会静默变成
0。所以下面的 `TRACE_OBSERVATION_CONTRACT` 把「每个 step_type 读哪些键」写成一
份可断言的表,`tests/test_reasoning_retrieval.py` 用 AST 扫描 `reasoning_retrieval`
里所有 `TraceStep(step_type=...)` 与它对应的 detail 字面量,任何一处漂移当场报红。

**观察是投影,不是第二份权威。** visited / 方向身份 / 精查名称 / 枚举
cursor-coverage / outline-overflow 各自仍然是唯一权威,防重复、熔断、配额都只
读它们。这里的 `duplicate` 状态是对**已经发生**的那次拦截的描述,不参与任何判
据;把它接进判据就等于凭空造出一份可以与旧状态分叉的账。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import List, Mapping, Optional, Sequence, Tuple

from app.services.reasoning_actions import (
    ADD_SUBQUERY_ACTION, CONSULT_MEMORY, ENUMERATE_ELEMENTS,
    ENUMERATE_KG_OBJECTS, EXACT_LOOKUP_ACTION, EXPAND_COMMUNITY_ACTION,
    EXPAND_GRAPH_ACTION, FOLLOW_CHAIN_ACTION, PPR_RETRIEVE_ACTION,
    SEARCH_CHUNKS_ACTION, SEARCH_ELEMENTS_ACTION, UPDATE_OUTLINE,
)

# --- 执行状态(设计稿 §6.1 的七态,闭集) -----------------------------------
#: 真的检索到了新东西。
STATUS_SUCCESS = "success"
#: 执行了,但一条新证据都没拿到。**不是**失败——通道是通的,只是这个问法空手。
STATUS_EMPTY = "empty"
#: 与先前**成功完成**的同一请求重复,被既有身份判据拦下,零 I/O。
STATUS_DUPLICATE = "duplicate"
#: 这一轮这条通道根本不存在(没接线/没图/范围不允许/配额耗尽),零 I/O。
STATUS_UNAVAILABLE = "unavailable"
#: 载荷本身不成立(缺参数、非白名单值、字段矛盾),零 I/O。
STATUS_INVALID = "invalid"
#: 执行过程中抛了(库读失败、下游异常)。与 `empty` 严格区分:一个是"查了没有",
#: 一个是"没查成"。
STATUS_FAILED = "failed"
#: 有结果,但已知被截断/只有部分臂成功。
STATUS_PARTIAL = "partial"

OBSERVATION_STATUSES: Tuple[str, ...] = (
    STATUS_SUCCESS, STATUS_EMPTY, STATUS_DUPLICATE, STATUS_UNAVAILABLE,
    STATUS_INVALID, STATUS_FAILED, STATUS_PARTIAL,
)

PHASE_SEED = "seed"
PHASE_ACTION = "action"

#: 模型 `reason` 回喂时的截长。它是"当时为什么选这一步"的一句话,不是思考过程;
#: 原样重放会让近期观察那一块每轮顶掉半屏预算。
PURPOSE_CHARS = 80
#: 规范化请求身份在观察行里的宽度上限(模型自由文本可以是一整段)。
REQUEST_CHARS = 60
#: 稳定原因码在观察行里的宽度上限。今天的原因码全是服务端字面量(最长的
#: `unavailable_action:` 前缀加一个动作名也远在这个数之内),但其中的
#: `unavailable_action:`/`missing_argument:` 两族后缀来自**模型提交的载荷**,
#: 而这一格与请求身份、目的一样会被拼进 `- #N …；原因=…` 那一行。不截长就等于
#: 让模型自己决定这一行有多长,不折叠就等于让它决定这一行有几行。
REASON_CHARS = 60

_STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    STATUS_SUCCESS: "有新证据",
    STATUS_EMPTY: "执行了但零新增",
    STATUS_DUPLICATE: "与先前成功请求重复,未执行",
    STATUS_UNAVAILABLE: "本轮不可用,未执行",
    STATUS_INVALID: "载荷不成立,未执行",
    STATUS_FAILED: "执行失败",
    STATUS_PARTIAL: "部分结果(已知截断)",
})


@dataclass(frozen=True)
class ActionObservation:
    """一次动作的观察。全部字段都来自**实际执行结果**或服务端已持有的状态。"""

    seq: int
    phase: str
    action_id: str
    #: 规范化请求身份(types/prefer/方向等参与身份的参数已折进来)。
    request: str
    #: 当时的动作目的 = 模型那一轮的 `reason`,截长。历史模型判断,不是证据。
    purpose: str
    status: str
    #: 通道返回了多少条(含重复/已有)。不是所有通道都能区分,不能区分时 = new。
    returned: int
    #: 真正新进候选池的条数。
    new: int
    #: 就地升级(同一条证据换成更强的那份)的条数。今天没有任何通道上报它,所以
    #: 恒为 0 并且不渲染——留字段而不是留一个假数字。
    upgraded: int
    #: 已知截断(枚举 has_more / 邻居超上限 / 词法臂失败)。
    truncated: bool
    #: 稳定原因码,取自执行处已有的 skip reason,不新造同义词。
    reason: str
    #: 这条通道执行后还剩多少额度,一句话。空串 = 这个动作不扣配额。
    #: 今天唯一的写入方是 run 的**全局剩余步数**(`剩余步数 N`),不是这条通道
    #: 自己的专用配额(元素检索次数、枚举行/页/载荷…)——那些各有自己的披露口径,
    #: 混进同一格会让模型把"还能再枚举多少行"读成"还能再走多少步"。
    budget_left: str
    #: 本次真正新增的候选标识(取自 trace detail 的 `result_ids`,已在那里按
    #: `TRACE_RESULT_IDS_MAX` 截过)。证据卡的"本轮新增"档读它。
    result_ids: Tuple[str, ...] = ()
    #: 这条**链**到目前为止的累计返回数(只有枚举有:续跑把同一个集合分页列
    #: 出来)。与 `returned`(本次这一页返回了几条)是两个数,渲染成两格——把累计
    #: 摆在"返回"那一格上,模型会把「第 3 页返回 3 条」读成「这一次返回了 9 条」。
    #: 命名刻意不叫 `total`:枚举 detail 自己的 `total` 键(见
    #: `collection_enumeration`)说的是另一件事——那份 detail 从不流进这个字段
    #: (这里读的是 `returned_total`,见 `total_key`),但两个同名不同义的 `total`
    #: 挤在同一段讨论里容易让人以为它们是同一个数。
    chain_total: int = 0


# --- step_type → 动作的对照表 -----------------------------------------------
# 每一行的第三列是"新增条数"读哪个 detail 键,第四列是"返回条数"读哪个键(为空
# 表示这条通道分不出返回与新增,两者相等)。第五列是判定 partial 的截断键。
@dataclass(frozen=True)
class _StepContract:
    action_id: str
    new_key: str
    returned_key: str = ""
    truncation_keys: Tuple[str, ...] = ()
    #: 备用的新增键。`retrieve` 一个 step_type 服务两种形状:首轮初检索写
    #: `count`(池子总量,那一刻等于新增),子查询/补种写 `new`。
    fallback_new_key: str = ""
    #: **附加**新增键:与主键相加,不是二选一。`retrieve` 的 `new` 只说 KG 候选
    #: (它与同一份 detail 的 `result_ids` 一一对应,见
    #: `_search_passages_if_graphless` 的说明),无图库上那条通道恒空手,该方向
    #: 真正到手的证据全在原文半的 `chunks_found` 里。只读 `new` 就会让一次真的
    #: 捞到 5 段原文的 add_subquery 在观察账上显示成「执行了但零新增」——模型据
    #: 此换问法另起炉灶,白丢已经到手的证据。这与 `attempted` 那条账目的口径
    #: (记**证据总数**)因此是同一个:两处都不能只数 KG 半。
    extra_new_keys: Tuple[str, ...] = ()
    #: 链级累计返回数(只有枚举有)。单独一格渲染,不占"返回"那一格。
    total_key: str = ""
    #: 这条通道的**计数键说的是"返回了几条"而不是"新增了几条"**,新增只能数
    #: `result_ids`(它装的就是真正进池子的那批)。目前只有 `expand`:它的
    #: `found` 是 `self.neighbors()` 返回的邻居总数,里面可能一条都不是新的
    #: (最常见的形状:两个节点互为邻居,第二次展开返回 1、新增 0)。把它当新增
    #: 就是设计稿点名禁止的"新增为零不是返回零"。
    new_from_result_ids: bool = False


TRACE_OBSERVATION_CONTRACT: Mapping[str, _StepContract] = MappingProxyType({
    # 首轮初检索 / 已确认方向补种 / reflect 的 add_subquery —— 同一条联邦 KG
    # 检索通道的三个入口,所以折成同一个动作 id;seed 与 action 由 phase 区分。
    "retrieve": _StepContract(ADD_SUBQUERY_ACTION, "new",
                              fallback_new_key="count",
                              extra_new_keys=("chunks_found",)),
    "ppr": _StepContract(PPR_RETRIEVE_ACTION, "found"),
    "exact_lookup": _StepContract(EXACT_LOOKUP_ACTION, "found"),
    "search_chunks": _StepContract(
        SEARCH_CHUNKS_ACTION, "found", truncation_keys=("keyword_failed",)),
    "expand": _StepContract(
        EXPAND_GRAPH_ACTION, "", returned_key="found",
        truncation_keys=("neighbor_truncated", "result_ids_truncated"),
        new_from_result_ids=True),
    "expand_community": _StepContract(
        EXPAND_COMMUNITY_ACTION, "new", returned_key="peers"),
    "follow_chain": _StepContract(FOLLOW_CHAIN_ACTION, "count"),
    # `returned_total` 是**整条链**的累计(续跑把它一路加上去),不是这一次返回
    # 了几条。它因此走 `total_key` 单独渲染:摆在"返回"那一格上,一次只列出 3 条
    # 的续跑会显示成「返回9」,而模型下一步要不要再续跑,读的正是这个数。
    "enumerate": _StepContract(
        ENUMERATE_ELEMENTS, "returned", total_key="returned_total",
        truncation_keys=("has_more", "truncated_reason")),
    # `search_elements` 的成功步历史上就叫 `fallback`(降级查原文)。
    "fallback": _StepContract(SEARCH_ELEMENTS_ACTION, "found"),
    "consult_memory": _StepContract(CONSULT_MEMORY, "entries"),
    "outline": _StepContract(UPDATE_OUTLINE, "sections"),
})

#: 这些 step_type 永远不是一次动作观察:它们是 run 级的叙述而不是某个动作的
#: 执行结果。列成显式集合(而不是"表里没有就忽略")是为了让漂移守卫能区分
#: 「新增了一个 step_type 但忘了登记」与「这个 step_type 本来就不该有观察」。
NON_ACTION_STEP_TYPES: frozenset = frozenset({
    "plan", "profile", "experience", "reflect", "answer",
})

# --- skip reason → 状态 -----------------------------------------------------
# 表里没有的 reason 一律落到 `unavailable`(最保守的那一档:说"这条路本轮没走
# 成"永远为真),而不是猜成 empty —— 把"没执行"记成"已检索"是设计稿点名的谎报。
#: 「这一次请求与先前**成功完成**的同一请求重复,被既有身份判据拦下」。
#: `empty_or_visited` 在 v2 下的唯一含义就是「这个节点已经展开过」:
#: `object_id` 为空那一半在解析期已经被 `missing_argument:object_id` 拦掉,
#: 走不到这条 skip(legacy 没有那道闸,所以这个码在 legacy 里仍是二义的——但
#: legacy 不构造账本)。归 `unavailable` 会把一次「你已经看过它了」说成「这条
#: 通道本轮不存在」,模型于是去换通道而不是换节点。
_DUPLICATE_REASONS: frozenset = frozenset({
    "duplicate_subquery", "duplicate_exact_lookup", "duplicate_follow_chain",
    "already_enumerated", "no_focal_or_done", "empty_or_visited",
})
_INVALID_REASONS: frozenset = frozenset({
    "missing_new_sub_query", "missing_exact_term", "missing_chain_start",
    "exact_term_not_identifier", "chain_start_not_candidate",
    "enumeration_kind", "enumeration_rejected", "enumeration_conflict",
    "enumeration_source_unresolved", "outline_empty",
    "outline_repair_structure",
    # v2 §7.1:模型宣布证据已足却一个必答方面都没自评,整轮退回并追问一次。
    # 归 invalid 而不是 unavailable:通道好好的,是这份载荷不成立。
    "missing_assessment",
})
_FAILED_REASONS: frozenset = frozenset({
    "community_error", "enumeration_unavailable", "consult_memory_unavailable",
    "kg_unavailable", "kg_gap_unavailable", "collection_map_unavailable",
})
#: 「执行了、通道也在,只是这一轮没有可送达的新内容」——与 unavailable 不同。
_EMPTY_REASONS: frozenset = frozenset({
    "consult_memory_nothing_new", "consult_memory_block_full",
})
#: 与任何一次动作都无关的 run 级 skip(熔断、终态披露、能力收尾…):不产生观察。
NON_ACTION_SKIP_REASONS: frozenset = frozenset({
    "stale_circuit_breaker", "outline_evidence_overflow_unresolved",
    "intent_coverage_incomplete", "initial_evidence_empty",
    "no_executable_action", "outline_overflow_repair_declined",
    # T4:run 收尾的结束原因披露。它是整次 run 的叙述,不是某一次动作的执行
    # 结果——记成观察会在账里多出一条谁都没请求过的"动作"。
    "retrieval_termination",
})

#: v2 把一份可识别但不可执行/参数不合法的载荷折成的原因码前缀(见
#: `parse_reflect_v2`)。前缀式的三族分别落 unavailable / invalid。
_UNAVAILABLE_REASON_PREFIX = "unavailable_action:"
#: `invalid_assessment:` 是 T4 的方面自评越界(方面 id 不合法、同一方面重复、
#: 每方面键数或 gap 超限…)。与另外两族同理落 invalid:载荷本身不成立,零 I/O。
_INVALID_REASON_PREFIXES = (
    "missing_argument:", "invalid_argument:", "invalid_assessment:")
#: v2 §5.2:反思调用本身失败、但这一轮不收尾(连续失败还没到两轮)。落
#: `failed` 而不是 invalid:载荷没有不成立——**根本没有载荷**,是调用炸了。
#: 后缀是稳定的兜底原因码(`output_budget_exhausted` / `empty` / provider 码),
#: 它逐字进观察行的「原因=」那一格,模型据此知道该缩短输出还是该换个问法。
#: 这个 action_id 恒是 `REFLECT_INVALID_ACTION` 伪动作,不在 `ACTION_DEFINITIONS`
#: 里,所以它**不会**被 `_retrieval_degraded` / `_unrecovered_channels` 当成一条
#: 检索通道的故障——那两处只看产证据的动作。
_FAILED_REASON_PREFIXES = ("model_degraded:",)


def status_for_skip(reason: str) -> str:
    """一条 skip 的稳定原因码 → 执行状态。表外一律 `unavailable`(保守档)。"""
    if reason in _DUPLICATE_REASONS:
        return STATUS_DUPLICATE
    if reason in _INVALID_REASONS:
        return STATUS_INVALID
    if reason in _FAILED_REASONS:
        return STATUS_FAILED
    if reason in _EMPTY_REASONS:
        return STATUS_EMPTY
    if reason.startswith(_UNAVAILABLE_REASON_PREFIX):
        return STATUS_UNAVAILABLE
    if any(reason.startswith(p) for p in _FAILED_REASON_PREFIXES):
        return STATUS_FAILED
    if any(reason.startswith(p) for p in _INVALID_REASON_PREFIXES):
        return STATUS_INVALID
    if reason in ("unknown_action", "invalid_sufficient",
                  "invalid_arguments_object", "unexpected_arguments",
                  "sufficient_with_retrieval_action"):
        return STATUS_INVALID
    return STATUS_UNAVAILABLE


def _int(detail: Mapping, key: str) -> int:
    """一个计数键 → 非负整数。**列表按长度计**。

    执行处的计数有两种写法:一个数(`found` / `new` / `returned`),或者那批东西
    本身(`peers` 是同类实体名的列表,`sections` 是大纲节的列表)。只认 `int` 的话
    后一种恒读成 0——一次真的纳入了 5 个同类实体的横向对比,在观察账上与一次
    彻底空手长得一模一样。
    """
    value = detail.get(key)
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


#: 观察行的字段分隔符(`；`,`render_observation_row` 用它 `"；".join(parts)`)
#: 与证据卡的字段分隔符(`|`/`｜`,`render_card` 用 `" | ".join(parts)`)。模型
#: 自己写的 `reason`/`request`/`purpose` 里带上这四个字符中的任何一个,都能在
#: 渲染出来的那一行里冒充出一个新字段——例如 `reason = "推进；余额=剩余步数 99"`
#: 会在观察行里多出一个看起来来自服务端的 `余额=`。折成全角逗号:字面文字仍然
#: 保留(它原本就是这条证据/这次决定的内容),只是不再能被读成分隔符。
_ROW_SEPARATORS = "；;|｜"


def _clip(text: str, limit: int) -> str:
    """折叠成一行 + 去控制字符 + 归一分隔符 + 截长。

    与 `agent_profile_block._clean` / `retrieval_experience_block._clean` 同款的
    伪造防御,理由也一样:这些值会被拼进 `- #N …；请求=…；目的=…` 那一行,而其中
    的请求身份、目的、以及两族原因码后缀都来自模型自己提交的载荷。带字面换行的
    一个 `reason` 能在渲染出来的账里伪造出第二条观察行,甚至一整个
    `【动作观察账 — …】` 块头;`.split()` 只管空白,余下的 C0/C1 控制字符(它们能
    让终端与部分渲染器把后面的内容当另一段)在这里一并去掉。分隔符归一见
    `_ROW_SEPARATORS`。
    """
    text = " ".join(str(text or "").split())
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for sep in _ROW_SEPARATORS:
        text = text.replace(sep, "，")
    return text if len(text) <= limit else f"{text[:limit]}…"


@dataclass
class _PendingDecision:
    """一次 reflect 决定给下一条观察带的上下文。"""

    action_id: str
    request: str
    purpose: str
    budget_left: str
    #: 这一次请求里**用户/模型写的那段自然语言**,不含参与身份的规范化参数
    #: (`types=…` / `prefer=…` / `dir=both`)、也不含 `ko-…` 这类内部标识。
    #: 证据卡的摘录窗口拿它当检索词来源;身份串只进观察行。两者混用的话,摘录
    #: 会去正文里找 "prefer=balanced" 这种在任何文档里都不存在的词。
    query_text: str = ""


class ActionObservationLedger:
    """run 内的观察账。只在 v2 总闸开着时被构造(关闭态零新状态)。

    它有**四个**写入口,刻意分工:

    * `note_decision` —— 模型这一轮选了什么、为什么、还剩多少额度。这些只有
      `ReflectDecision` 与 run 的额度账知道,trace detail 里没有。
    * `observe` —— 由 `_TraceRecorder` 在每次记账时调用,把**实际执行结果**折成
      观察。它绝不读 `_PendingDecision.purpose` 去推断发生了什么,只借它给这条
      观察标上"当时的动作目的"。
    * `note_fresh_ids` / `note_failed` —— **侧信道**(见下面各自的说明)。它们
      存在的理由是 legacy 的 trace 键集是冻结基线:有三条通道的执行结果今天
      根本没有以「本轮新增了这些标识」的形状写进 detail,而往 detail 里加新键
      会改动关闭态的账目。侧信道只在 v2 挂了账本时被调用(`observer is None`
      时调用方一次都不进来),关闭态因此零调用、零分配。
    """

    __slots__ = ("_rows", "_pending", "_turn_start", "_extra_fresh",
                 "_failed_reason")

    def __init__(self) -> None:
        self._rows: List[ActionObservation] = []
        self._pending: Optional[_PendingDecision] = None
        self._turn_start = 0
        self._extra_fresh: List[str] = []
        self._failed_reason = ""

    @property
    def rows(self) -> Tuple[ActionObservation, ...]:
        return tuple(self._rows)

    @property
    def last_request(self) -> str:
        """上一次决定的规范化请求身份。只进观察行,不进摘录检索词。"""
        return self._pending.request if self._pending is not None else ""

    @property
    def last_query(self) -> str:
        """上一次决定里那段**原始查询文本**。证据卡的摘录窗口用它当检索词。"""
        return self._pending.query_text if self._pending is not None else ""

    def fresh_result_ids(self) -> Tuple[str, ...]:
        """上一轮决定之后**真的新进池子**的候选标识,保序去重。

        口径刻意是"上一次 `note_decision` 之后记下的观察",而不是"最后一条观察":
        一轮里可能落多条 trace 步(例如枚举续跑),漏掉其中一条就会让证据卡把刚
        到手的证据当成历史材料。首轮没有决定,`_turn_start` 恒为 0,于是整个首轮
        播种的产出都算"本轮新增"——那正是第一次 reflect 眼里的事实。

        两个来源:trace detail 的 `result_ids`,以及侧信道 `note_fresh_ids`。
        后者排在后面,所以同一条证据两处都报时,顺序仍由 trace 决定。
        """
        out: List[str] = []
        seen: set = set()
        for row in self._rows[self._turn_start:]:
            for key in row.result_ids:
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        for key in self._extra_fresh:
            if key not in seen:
                seen.add(key)
                out.append(key)
        return tuple(out)

    def note_decision(
        self, action_id: str, request: str, purpose: str, budget_left: str,
        query_text: str = "",
    ) -> None:
        self._pending = _PendingDecision(
            action_id=action_id,
            request=_clip(request, REQUEST_CHARS),
            purpose=_clip(purpose, PURPOSE_CHARS),
            budget_left=budget_left,
            query_text=query_text,
        )
        self._turn_start = len(self._rows)
        # 与 `_turn_start` 同一次翻页:上一轮的新增到此为止,下一轮从零开始。
        self._extra_fresh = []

    def note_fresh_ids(self, ids: Sequence[str]) -> None:
        """侧信道:本轮**真的新进候选池**的标识,由执行处直接送来。

        为什么不走 trace detail:`search_elements`(detail 只有 query/found)、
        `expand_community`(只有 peers/new)、无图 run 的原文半(它的 detail 刻意
        只承载 KG object_id,见 `_search_passages_if_graphless`)这三条通道今天
        都不写本次新增的标识,而 legacy 的 trace 键集是冻结基线——往里加键会改
        动关闭态的账目与前端读法。于是"本轮新增"那一档对这三条通道结构性失效:
        刚查到的高分元素永远进不了下一轮的证据卡。

        它**只喂证据卡的选取顺序**,不进观察行的计数(那些数各有自己的权威键),
        更不进任何判重/配额判据。
        """
        for key in ids:
            key = str(key)
            if key:
                self._extra_fresh.append(key)

    def note_failed(self, reason: str) -> None:
        """侧信道:紧接着要记账的那一步,其实**没查成**(不是"查了没有")。

        seed 路径的 fail-open 把异常吞成 `[]`,调用方随后照常记一条 `found: 0`
        的成功步——于是"数据库读炸了"与"这个问法在库里真的没有内容"在观察账上
        完全同形,而这两件事该导致的下一步正好相反(重试/换通道 vs 换问法)。
        detail 里没有能表达这件事的键(加一个就改了关闭态的键集),所以走侧信道。

        标记只对**下一条**观察生效,并且无条件在下一次 `observe` 时清掉——即便
        那一次没折出观察行,也不许把标记留给再下一条不相干的步。
        """
        self._failed_reason = reason

    def observe(self, step) -> None:
        failed_reason, self._failed_reason = self._failed_reason, ""
        row = observation_from_step(
            step, seq=len(self._rows) + 1, pending=self._pending,
            failed_reason=failed_reason)
        if row is not None:
            self._rows.append(row)


def _phase_of(detail: Mapping, pending: Optional[_PendingDecision]) -> str:
    """seed 还是 action。detail 自带的 `phase` 键是权威,它才知道自己是谁。

    没有这个键时才回退到"这一轮模型选过动作吗":首轮的确定性播种发生在任何一次
    reflect 之前,那时 `pending` 恒为 None——所以 seed 不会被冒充成"模型主动
    采用的工具"(设计稿 §6.1)。
    """
    declared = str(detail.get("phase", "") or "")
    if declared == PHASE_SEED:
        return PHASE_SEED
    if declared:
        return PHASE_ACTION
    return PHASE_ACTION if pending is not None else PHASE_SEED


def _enumerate_action(detail: Mapping) -> str:
    """两个枚举动作共用一个 step_type;detail 的 `collection` 键区分它们。

    `_run_enumeration` 把三个集合折成一个 `collection ∈ {elements, kg_objects,
    sources}`,子类型一律写进同一把 `kind` 键——它**从不**写 `object_type`。所以
    按 `object_type` 判等于恒为假:一次 `enumerate_kg_objects` 在观察账上会显示
    成 `enumerate_elements`,而模型据这一行决定下一步再枚举哪个集合。

    `sources`(文档目录)不是这两个动作 id 中的任何一个:模型选哪个动作都可能落
    到它。保守落在元素枚举那一档——两者的预算池与覆盖账目本来就是共用的,这一格
    只影响观察行的字面。
    """
    if str(detail.get("collection", "") or "") == "kg_objects":
        return ENUMERATE_KG_OBJECTS
    return ENUMERATE_ELEMENTS


def observation_from_step(
    step, *, seq: int, pending: Optional[_PendingDecision],
    failed_reason: str = "",
) -> Optional[ActionObservation]:
    """一条 `TraceStep` → 一条观察,或 None(这一步不是动作执行)。

    只读结构化 `detail`;`summary` 那句中文一个字都不读。

    ``failed_reason`` 来自侧信道(`ActionObservationLedger.note_failed`):这一步
    的某条臂 fail-open 吞了异常。它只改状态与原因码,不改任何计数——已经到手的
    那部分证据是真的。
    """
    step_type = str(getattr(step, "step_type", "") or "")
    detail = getattr(step, "detail", None) or {}
    if not isinstance(detail, Mapping):
        detail = {}
    if step_type in NON_ACTION_STEP_TYPES:
        return None
    result_ids = tuple(
        str(x) for x in (detail.get("result_ids") or []) if str(x))
    if step_type == "skip":
        reason = str(detail.get("reason", "") or "")
        if reason in NON_ACTION_SKIP_REASONS:
            return None
        if pending is None:
            # 首轮阶段的 skip(没接线、没图…)也是真实发生的事,但它没有动作
            # 身份可言;记一条以 seed 为阶段、动作 id 取自原因码的观察只会造出
            # 一个模型无从对照的名字。这里不记——首轮的空手另有 first-round
            # 提示负责,那条提示才是模型要的下一步。
            return None
        return ActionObservation(
            seq=seq, phase=PHASE_ACTION, action_id=pending.action_id,
            request=pending.request, purpose=pending.purpose,
            status=status_for_skip(reason), returned=0, new=0, upgraded=0,
            truncated=False, reason=_clip(reason, REASON_CHARS),
            budget_left=pending.budget_left,
        )
    contract = TRACE_OBSERVATION_CONTRACT.get(step_type)
    if contract is None:
        return None
    action_id = contract.action_id
    if step_type == "enumerate":
        action_id = _enumerate_action(detail)
    if contract.new_from_result_ids:
        new = len(result_ids)
    else:
        new = _int(detail, contract.new_key)
        if not new and contract.fallback_new_key:
            new = _int(detail, contract.fallback_new_key)
        # 附加新增键与主键**相加**(见 `extra_new_keys`):无图 run 的原文半是
        # 同一次动作的另一半产出,不是另一个动作。
        for key in contract.extra_new_keys:
            new += _int(detail, key)
    returned = (
        _int(detail, contract.returned_key) if contract.returned_key else new)
    returned = max(returned, new)
    truncated = any(bool(detail.get(k)) for k in contract.truncation_keys)
    if failed_reason:
        # 「没查成」与「查了没有」严格区分。已经到手的那部分是真的,所以有新增时
        # 是 partial(部分臂成功)而不是 failed。
        status = STATUS_PARTIAL if new else STATUS_FAILED
    elif step_type == "outline":
        # 大纲不产生证据:它的"成功"是这份结构被接纳,不是新增了几条候选。
        status = STATUS_SUCCESS
    elif truncated and new:
        # 截断只在**真的拿到了新东西**的那一步才是 partial。返回了一堆全是旧的
        # 邻居、同时还被上限截断,对模型而言是"这条路这一轮空手"(外加一条截断
        # 提示),不是"部分成功"。
        status = STATUS_PARTIAL
    elif new:
        status = STATUS_SUCCESS
    else:
        status = STATUS_EMPTY
    phase = _phase_of(detail, pending)
    return ActionObservation(
        seq=seq, phase=phase, action_id=action_id,
        request=(pending.request if pending is not None and phase == PHASE_ACTION
                 else _request_from_detail(detail)),
        purpose=(pending.purpose
                 if pending is not None and phase == PHASE_ACTION else ""),
        status=status, returned=returned, new=new,
        upgraded=_int(detail, "upgraded"), truncated=truncated,
        reason=_clip(
            failed_reason or detail.get("truncated_reason", ""), REASON_CHARS),
        budget_left=(pending.budget_left
                     if pending is not None and phase == PHASE_ACTION else ""),
        result_ids=result_ids,
        chain_total=_int(detail, contract.total_key) if contract.total_key else 0,
    )


def _request_from_detail(detail: Mapping) -> str:
    """seed 阶段的请求身份:它没有模型决定,只能从执行结果自己的具名键来。"""
    for key in ("query", "term", "object_id", "focal", "kind", "object_type",
                "collection"):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value, REQUEST_CHARS)
    terms = detail.get("terms")
    if isinstance(terms, (list, tuple)) and terms:
        return _clip("、".join(str(t) for t in terms), REQUEST_CHARS)
    return ""


# --- 有界渲染 ---------------------------------------------------------------
OBSERVATION_BLOCK_TITLE = "【动作观察账 — 服务端记录的实际执行结果】"
HISTORY_NOTE = (
    "（上面每行的“目的”是当时那一轮模型自己写下的判断，属于历史记录，"
    "不证明证据充分、范围合法或通道可用。）"
)


def render_observation_row(row: ActionObservation) -> str:
    parts = [
        f"#{row.seq} [{row.phase}] {row.action_id}",
    ]
    if row.request:
        parts.append(f"请求={row.request}")
    if row.action_id == UPDATE_OUTLINE and row.status == STATUS_SUCCESS:
        # 大纲不是一条检索通道:它的产出是一份被接纳的结构,不是证据。沿用检索
        # 那套标签会渲染出「有新证据；新增0」——同一行里既说有又说没有,而两种
        # 读法都讲得通。被拒的那几种(`outline_empty` 等)走 skip,状态不是
        # success,因此仍按原因码渲染,不会被这一格说成"已更新"。
        parts.append(f"大纲已更新，{row.new} 节")
    else:
        parts.append(_STATUS_LABELS.get(row.status, row.status))
        if row.status in (STATUS_SUCCESS, STATUS_PARTIAL, STATUS_EMPTY):
            parts.append(
                f"返回{row.returned}/新增{row.new}"
                if row.returned != row.new else f"新增{row.new}")
        if row.chain_total and row.chain_total != row.returned:
            # 「这一次返回 N 条」之外再说一句「这条链累计 M 条」。
            parts.append(f"累计{row.chain_total}")
    if row.upgraded:
        parts.append(f"升级{row.upgraded}")
    if row.truncated and row.status != STATUS_FAILED:
        # `failed` 与 `truncated` 能同时成立(见 `note_failed`:某条臂真的没查
        # 成,同一步里另一条臂又被自己的截断键标了位)。这时状态标签已经说了
        # "执行失败",不能再叠一句"已知截断"——那是"这条路本来是通的、只是被
        # 上限切了一刀"的意思,与"根本没查成"是两件事,不能读起来像同一件。
        # `status_for_skip` 也能产出 `failed`,但那条 skip 分支从不设
        # `truncated=True`,所以这里的 `truncated=True ∧ failed` 组合只可能
        # 来自 `note_failed`,判据因此是准确的,不是近似。
        parts.append("已知截断" + (f"({row.reason})" if row.reason else ""))
    elif row.reason:
        parts.append(f"原因={row.reason}")
    if row.budget_left:
        parts.append(f"余额={row.budget_left}")
    if row.purpose:
        parts.append(f"目的={row.purpose}")
    return "- " + "；".join(parts)


def render_observations(
    rows: Sequence[ActionObservation], *, recent: int, state_chars: int,
) -> str:
    """最近若干条观察的有界投影。

    两道界同时生效:条数(`recent`)与字符(`state_chars`,可压缩区的总预算)。装不
    下时**明确说省略了几条**——一份悄悄少了两行的账目比没有账目更危险,模型会把
    "没写"读成"没发生"。

    `state_chars` 是**硬界**:披露后缀本身也算进去。按"先装行、最后再拼一句
    `（更早的 N 条未列出）`"的写法,那句话的长度不在任何一次预算判断里,整块因此
    可以稳定超出预算十几个字符——而这一块的存在理由就是给可压缩区定一个上限。
    所以下面在拼完整块之后回头量一次,超了就再丢最早的一行(丢一行必然让披露数
    +1,那句后缀最多长一位,所以循环单调收敛)。

    **下界保护**:丢到只剩最新一行、那一行自己还是装不下时,不再继续丢成空
    块——空字符串在模型眼里与"这一轮什么都没发生"没有区别,而这里恰恰相反:
    发生了,只是账目装不下。这时改成把这最后一行截到预算内,省略披露(`更早的
    N 条未列出`)照旧跟着走;只有连标题、提示语和这句披露本身都装不进预算的
    极端值,才真的交回空块。
    """
    if not rows:
        return ""
    total = len(rows)
    selected = list(rows[-recent:]) if recent > 0 else []
    lines: List[str] = []
    used = len(OBSERVATION_BLOCK_TITLE) + len(HISTORY_NOTE)
    dropped = total - len(selected)
    # 从最近的一条往回装:预算不够时先丢最早的,而不是丢最新的。
    for row in reversed(selected):
        text = render_observation_row(row)
        if used + len(text) + 1 > state_chars and lines:
            dropped += 1
            continue
        used += len(text) + 1
        lines.append(text)
    lines.reverse()
    head = OBSERVATION_BLOCK_TITLE
    while lines:
        head = OBSERVATION_BLOCK_TITLE
        if dropped > 0:
            head = f"{head}（更早的 {dropped} 条未列出）"
        text = "\n".join([head, *lines, HISTORY_NOTE])
        if len(text) <= state_chars:
            return text
        if len(lines) == 1:
            break
        lines.pop(0)
        dropped += 1
    if not lines:
        return ""
    overhead = len(head) + len(HISTORY_NOTE) + 2
    floor = state_chars - overhead
    if floor <= 0:
        return ""
    newest = lines[0]
    if len(newest) > floor:
        newest = newest[:floor - 1] + "…"
    return "\n".join([head, newest, HISTORY_NOTE])
