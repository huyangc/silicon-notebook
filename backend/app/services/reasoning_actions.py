"""reflect 动作的能力投影(设计稿 2026-09-07 §5.1)。

这个模块是**纯数据 + 一次纯内存投影**:动作定义(id / 参数形状 / 是否产生证据 /
预算类别 / 重复身份 / 依赖条件)与由一组运行时事实算出的不可变
``ReflectCapabilities``。它不做任何 I/O、不读 settings、不认识
``ReasoningRetriever``——所有运行时事实由调用方以 ``ReflectCapabilityFacts``
一次性喂进来。

**为什么必须只有一次投影。** 模型看到的动作说明(prompt)、schema 的
``next_action`` 枚举与参数分支、以及解析时的白名单,是同一件事的三处呈现。这三
处各算一遍的历史后果在本仓库已经反复出现:prompt 允许而执行处必然 skip(白烧一
轮反思预算),或者反过来,模型调用一个它从没被告知的动作。所以这里产出**一个**
对象,三处都从它读;任何一处不同步在结构上就不可能发生,而不是靠人复核。

**快照不是权限凭据。** 这份投影只回答"这一轮值不值得把这个动作摆给模型看"。
真正执行前,`scope`/`actor`/取消的实时校验一律照跑(执行处的纵深防御分支保留),
范围漂移与权限失效仍走既有的 fail-closed / StageBoundaryError 边界。

**不可用原因是有界闭集。** 它进 prompt(让模型换通道而不是重复撞墙)也进
trace,所以必须是稳定机器码,不是自由文本。取值复用执行处早已存在的 skip
``reason``,不新造同义词。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

from app.services.reports.policy import (
    OUTLINE_MAX_EVIDENCE,
    OUTLINE_MAX_SECTIONS,
    OUTLINE_TITLE_CHARS,
)


# --- 动作 id(13 个,与 legacy 完全一致——v2 不新增也不删减动作) -------------
ANSWER_ACTION = "answer"
ADD_SUBQUERY_ACTION = "add_subquery"
SEARCH_ELEMENTS_ACTION = "search_elements"
SEARCH_CHUNKS_ACTION = "search_chunks"
EXACT_LOOKUP_ACTION = "exact_lookup"
EXPAND_GRAPH_ACTION = "expand_graph"
PPR_RETRIEVE_ACTION = "ppr_retrieve"
EXPAND_COMMUNITY_ACTION = "expand_community"
FOLLOW_CHAIN_ACTION = "follow_chain"
ENUMERATE_ELEMENTS = "enumerate_elements"
ENUMERATE_KG_OBJECTS = "enumerate_kg_objects"
CONSULT_MEMORY = "consult_memory"
UPDATE_OUTLINE = "update_outline"

# 服务端在 v2 下对一份"可识别但本轮不可执行/参数不合法"的载荷折出来的**伪动作
# id**。它不进 prompt、不进 schema 枚举、也不是模型可以选的东西:`run()` 据它记
# 一条 invalid/unavailable 观察,零工具 I/O,再落到链尾与其它 skip 同一份
# no_progress/stale 记账(设计稿 §5.2)。
REFLECT_INVALID_ACTION = "__reflect_invalid__"

# ``enumerate.scope`` 的两个合法值:来源清单要不要把挂载并勾选的参考库一起列进来。
#
# **刻意是字符串枚举而不是布尔**(见 prompts.py 同处的通用纪律注释):
# ``model_json._validate_against_example`` 对布尔字段是**硬类型**校验——模型吐
# `"true"` / `"yes"` 会 `invalid_boolean`,整轮 reflect 被打成兜底,与 F1 修的
# 根因同类;而字符串枚举字段享受 F1 立的空串宽容规则(留空 = 这一轮不用它)。
# 枚举值本身自描述,模型不必去猜「true 是哪一边」。
#
# 定义点落在**动作契约模块**而不是执行器:它是一个动作参数的取值集,legacy 的
# schema hint、v2 的能力投影(下面两条 ``ActionParam``)与两条协议的解析层读的
# 必须是同一份。``reasoning_retrieval`` 原样 re-export 这三个名字,历史导入点
# (`from app.services.reasoning_retrieval import ENUMERATE_SCOPES`)照旧成立。
ENUMERATE_SCOPE_ALL = "all"
ENUMERATE_SCOPE_CURRENT_NOTEBOOK = "current_notebook"
# 默认 = 旧行为:列出检索范围内的**全部**参与集文档,与集合地图 `sources: N` 的
# 联邦口径一致。缺省/空串/非法值/非字符串一律落到这里(fail-open,连 fail_closed
# 也不抛:范围不是动作合法性问题,动作照旧成立,只是范围按默认走)。
ENUMERATE_SCOPES = (ENUMERATE_SCOPE_ALL, ENUMERATE_SCOPE_CURRENT_NOTEBOOK)


# --- 参数形状 ---------------------------------------------------------------
#: 参数的取值类型。``text`` = 自由文本;``text_list`` = 字符串列表;
#: ``enum`` = 只接受 ``choices`` 里的值;``sections`` = update_outline 的整份
#: 章节结构(形状校验在 ``parse_outline_sections``,那是一个可单测的纯函数)。
PARAM_TEXT = "text"
PARAM_TEXT_LIST = "text_list"
PARAM_ENUM = "enum"
PARAM_SECTIONS = "sections"


@dataclass(frozen=True)
class ActionParam:
    """一个动作参数的形状声明。

    ``required`` 与 ``required_group`` 是两种"必填":前者是单个参数缺了就没法
    执行(``expand_graph`` 的 object_id);后者是一组里至少要有一个
    (``enumerate_*`` 的 kind/object_type 与 collection —— ``collection="sources"``
    刻意没有子类型,这正是既有 fail_closed 校验必须放它过去的那一条)。
    """

    name: str
    kind: str
    required: bool = False
    required_group: str = ""
    choices: Tuple[str, ...] = ()
    #: 一句话说明,进 prompt 的参数行。措辞写"该给什么"而不是"你给错了"。
    note: str = ""


@dataclass(frozen=True)
class ActionDefinition:
    """一个动作的静态定义(与运行时状态无关)。"""

    action_id: str
    params: Tuple[ActionParam, ...]
    #: 执行它会不会产生新证据。这是"检索动作携带 sufficient=true 即矛盾"这条
    #: 校验的唯一判据 —— update_outline / consult_memory 不产生证据,所以它们
    #: 与 sufficient=true 并存是合法的(设计稿 §5.2)。
    produces_evidence: bool
    #: 预算类别:同一类别的动作共用一个 per-run 配额池。``""`` = 不扣配额。
    budget_category: str
    #: 重复身份:哪些参数(规范化后)构成"同一次请求"。空 = 该动作不做重复拦截。
    identity_fields: Tuple[str, ...]
    #: 依赖条件的稳定名字,仅供文档/测试对照 —— 真正的判据在
    #: ``build_reflect_capabilities`` 里,一处实现。
    depends_on: Tuple[str, ...] = ()


_QUERY_PARAM = ActionParam(
    "query", PARAM_TEXT, note="检索串;留空则回退到原问题。"
)

#: ``scope`` 的参数说明,两个枚举动作共用一份。措辞与 legacy prompt 里那段
#: (``prompts.py`` 的 "By default the roster lists EVERY document…")同口径:
#: 默认覆盖整个检索范围、与 `[Collections in scope]` 的 sources 计数一致,只有
#: 问题明确在问「当前笔记本」时才收窄。它只对 ``collection="sources"`` 有意义,
#: 另两个集合的执行器根本没有这个参数。
#:
#: 第二半句(「先比两个数」)是 legacy ``prompts.py`` 那段 "Use the [Collections
#: in scope] counts to decide BEFORE acting … do NOT try to page through it —
#: answer with the count, a few representative examples, and an explicit
#: suggestion to narrow the request (one source, one section, one topic)" 的对等
#: 表述。v2 的能力投影此前只搬了前半句「默认列全范围」,于是模型在一个 48 839 篇
#: 的库里读到 sources 计数之后,唯一学到的是「默认就该全列」——生产上八次 run
#: 各用一个动作把整轮行池换成一段无序前缀。两个数的**字面**必须与它们真正的
#: 出处对齐:计数来自集合地图行 ``sources: N (current notebook: M)``,额度来自
#: 同一行末尾由 ``reasoning_retrieval._allowance_suffix`` 每轮现拼的
#: ``listing allowance left: R rows``——说的不是同一个字面,模型就得自己猜该拿
#: 哪两个数比。服务端的规模守卫是同一件事的兜底,不是它的替代:守卫只保证额度不
#: 被一次动作吃光,「别翻页、按计数作答」仍然只能由模型自己决定。
_ENUMERATE_SCOPE_NOTE = (
    "只对 collection=\"sources\" 有意义。默认(留空)= 列出检索范围内的**全部**"
    "文档(当前笔记本 + 已勾选的参考库),与 [Collections in scope] 的 sources "
    "计数同口径;只有问题明确在问当前笔记本时才填 \"current_notebook\"。"
    "换一档是**另一份清单**(续跑账目按范围记键),不算重复请求。"
    "动作之前先比两个数:[Collections in scope] 的 sources 计数(填 "
    "\"current_notebook\" 时看括号里那个数)与同一行末尾的 listing allowance "
    "left: R rows。装得下就一次列全;计数远大于 R 时**不要**逐页翻——把额度花光"
    "也只能看到其中很小一部分,应当按计数 + 几条代表性样本作答,并明确建议把请求"
    "收窄到一个来源、一节或一个主题。"
)

#: ``update_outline`` 的 ``sections`` 参数说明。v2 的 ``arguments`` 在 schema
#: hint 里是开放对象 ``{}``(形状校验在 ``parse_outline_sections``,不在传输
#: 闸),模型对嵌套节对象的**唯一**认知来源就是这句话——与 legacy
#: ``prompts.py`` 里 ``outline_action`` 段(``reflect_prompt``)描述的是同一份
#: 合同,只是从"一大段英文指令"收缩成"一条参数行",字段名与措辞口径对齐,
#: 免得模型换个协议就换一套拼法(``evidence_keys`` 就是这么漏出来的)。上限数字
#: 全部从 ``reports.policy`` 的大纲常量插值,不手写,常量一动这句话跟着动。
_UPDATE_OUTLINE_SECTIONS_NOTE = (
    "**整份**大纲;省略的节会被丢弃。每节是一个对象,字段:"
    "id(短而稳定的句柄,下一轮原样重提用它认哪一节)、"
    "title(问题的语言,一句话)、"
    "parent(可选,填另一节的 id 表示挂在它下面,只支持一层嵌套)、"
    "evidence(该节绑定的证据 key 列表,**逐字**抄自候选证据卡上的 `key=`,"
    "不是候选序号也不是来源标题)、"
    "remove_evidence(要从该节撤销绑定的旧 key 列表;省略 evidence 里的旧 key "
    "不会删除它,只有出现在 remove_evidence 才会撤销)。上限:至多 "
    f"{OUTLINE_MAX_SECTIONS} 节、标题至多 {OUTLINE_TITLE_CHARS} 字符、每节"
    f"最多绑定 {OUTLINE_MAX_EVIDENCE} 个证据 key。"
)

ACTION_DEFINITIONS: Mapping[str, ActionDefinition] = MappingProxyType({
    definition.action_id: definition
    for definition in (
        ActionDefinition(
            ANSWER_ACTION, (), False, "", (),
        ),
        ActionDefinition(
            ADD_SUBQUERY_ACTION,
            (
                ActionParam("query", PARAM_TEXT, required=True,
                            note="一条自足的检索串。"),
                ActionParam("types", PARAM_TEXT_LIST,
                            note="要检索的知识对象类型子集;留空=全部。"),
                ActionParam("prefer", PARAM_ENUM,
                            choices=("keyword", "semantic", "balanced"),
                            note="关键词/语义/均衡。"),
            ),
            True, "", ("query",),
        ),
        ActionDefinition(
            SEARCH_ELEMENTS_ACTION, (_QUERY_PARAM,), True,
            "element_search", (), ("element_search_budget",),
        ),
        ActionDefinition(
            SEARCH_CHUNKS_ACTION, (_QUERY_PARAM,), True,
            "chunk_search", (),
            ("chunk_search_wiring", "chunk_search_budget"),
        ),
        ActionDefinition(
            EXACT_LOOKUP_ACTION,
            (ActionParam("term", PARAM_TEXT, required=True,
                         note="文档里**逐字**出现的名称(命令/API/选项/参数)。"),),
            True, "exact_lookup", ("term",),
            ("source_scope", "exact_lookup_wiring", "exact_lookup_budget"),
        ),
        ActionDefinition(
            EXPAND_GRAPH_ACTION,
            (
                ActionParam("object_id", PARAM_TEXT, required=True,
                            note="候选里的知识对象 id。"),
                ActionParam("edge_type", PARAM_TEXT, note="可选,限定关系类型。"),
                ActionParam("direction", PARAM_ENUM,
                            choices=("out", "in", "both")),
            ),
            True, "", ("object_id",), ("kg_in_scope", "source_scope"),
        ),
        ActionDefinition(
            PPR_RETRIEVE_ACTION, (_QUERY_PARAM,), True, "ppr", (),
            ("kg_in_scope", "source_scope", "ppr_wiring", "ppr_budget"),
        ),
        ActionDefinition(
            EXPAND_COMMUNITY_ACTION,
            (ActionParam("focal", PARAM_TEXT,
                         note="要横向对比的实体名;留空=当前最高分候选。"),),
            True, "", ("focal",),
            ("kg_in_scope", "source_scope", "community_policy"),
        ),
        ActionDefinition(
            FOLLOW_CHAIN_ACTION,
            (
                ActionParam("start_object_id", PARAM_TEXT, required=True,
                            note="**必须**是当前候选里的 id。"),
                ActionParam("target_object_id", PARAM_TEXT, note="可选终点。"),
                ActionParam("edge_type", PARAM_TEXT, note="可选,限定关系类型。"),
                ActionParam("direction", PARAM_ENUM,
                            choices=("out", "in", "both")),
            ),
            True, "follow_chain",
            ("start_object_id", "target_object_id", "edge_type", "direction"),
            ("kg_in_scope", "source_scope", "chain_budget", "has_candidates"),
        ),
        ActionDefinition(
            ENUMERATE_ELEMENTS,
            (
                ActionParam("kind", PARAM_ENUM, required_group="collection",
                            note="要列出的元素类型。"),
                ActionParam("collection", PARAM_ENUM, required_group="collection",
                            choices=("sources",),
                            note="只填 \"sources\" 表示列**文档目录本身**;"
                                 "它优先于 kind。"),
                ActionParam("scope", PARAM_ENUM, choices=ENUMERATE_SCOPES,
                            note=_ENUMERATE_SCOPE_NOTE),
                ActionParam("source_id", PARAM_TEXT, note="可选,限定到一篇。"),
                ActionParam("source_title", PARAM_TEXT,
                            note="可选,候选里**逐字**抄来的来源标题。"),
            ),
            True, "enumeration",
            ("kind", "collection", "scope", "source_id"),
            ("enumeration_wiring", "enumeration_budget"),
        ),
        ActionDefinition(
            ENUMERATE_KG_OBJECTS,
            (
                ActionParam("object_type", PARAM_ENUM,
                            required_group="collection",
                            note="要列出的知识对象类型。"),
                ActionParam("collection", PARAM_ENUM, required_group="collection",
                            choices=("sources",),
                            note="只填 \"sources\" 表示列**文档目录本身**;"
                                 "它优先于 object_type。"),
                ActionParam("scope", PARAM_ENUM, choices=ENUMERATE_SCOPES,
                            note=_ENUMERATE_SCOPE_NOTE),
            ),
            True, "enumeration", ("object_type", "collection", "scope"),
            ("kg_in_scope", "enumeration_wiring", "enumeration_budget"),
        ),
        ActionDefinition(
            CONSULT_MEMORY, (), False, "consult_memory", (),
            ("consult_wiring", "consult_budget", "not_last_turn"),
        ),
        ActionDefinition(
            UPDATE_OUTLINE,
            (ActionParam("sections", PARAM_SECTIONS, required=True,
                         note=_UPDATE_OUTLINE_SECTIONS_NOTE),),
            False, "outline", (), ("outline_wiring", "outline_budget"),
        ),
    )
})

#: prompt 与 schema 的动作展示顺序(确定性,不随 dict 遍历漂移)。
ACTION_ORDER: Tuple[str, ...] = tuple(ACTION_DEFINITIONS)


# --- 不可用原因(闭集) ------------------------------------------------------
# 每一个都复用执行处已有的 skip reason,不新造同义词——排查时"prompt 说不可用"
# 与"执行处 skip 了"必须能对上同一个词。
REASON_NO_SUBQUERY_CHANNEL = "subquery_channel_unavailable"
REASON_SOURCE_SCOPE = "source_scope_unsafe_channel"
REASON_NO_GRAPH = "no_kg_in_scope"
REASON_PPR_DISABLED = "ppr_disabled"
REASON_PPR_CAP = "ppr_retrieve_cap"
REASON_EXACT_DISABLED = "exact_lookup_disabled"
REASON_EXACT_CAP = "exact_lookup_cap"
REASON_CHUNK_DISABLED = "chunk_search_disabled"
REASON_CHUNK_CAP = "chunk_search_cap"
REASON_ELEMENT_CAP = "element_search_cap"
REASON_COMMUNITY_DISABLED = "community_expansion_disabled"
REASON_CHAIN_CAP = "follow_chain_cap"
REASON_CHAIN_NO_CANDIDATES = "chain_no_candidates"
REASON_ENUM_DISABLED = "enumeration_disabled"
REASON_ENUM_BUDGET = "enumeration_budget"
REASON_CONSULT_DISABLED = "consult_memory_disabled"
REASON_CONSULT_CAP = "consult_memory_cap"
REASON_CONSULT_LAST_TURN = "consult_memory_last_turn"
REASON_OUTLINE_DISABLED = "outline_disabled"
REASON_OUTLINE_BUDGET = "outline_budget"
REASON_OVERFLOW_REPAIR_ONLY = "outline_overflow_repair_only"

UNAVAILABLE_REASONS: frozenset = frozenset({
    REASON_NO_SUBQUERY_CHANNEL, REASON_SOURCE_SCOPE, REASON_NO_GRAPH, REASON_PPR_DISABLED, REASON_PPR_CAP,
    REASON_EXACT_DISABLED, REASON_EXACT_CAP, REASON_CHUNK_DISABLED,
    REASON_CHUNK_CAP, REASON_ELEMENT_CAP, REASON_COMMUNITY_DISABLED,
    REASON_CHAIN_CAP, REASON_CHAIN_NO_CANDIDATES, REASON_ENUM_DISABLED,
    REASON_ENUM_BUDGET, REASON_CONSULT_DISABLED, REASON_CONSULT_CAP,
    REASON_CONSULT_LAST_TURN, REASON_OUTLINE_DISABLED, REASON_OUTLINE_BUDGET,
    REASON_OVERFLOW_REPAIR_ONLY,
})

#: prompt 里最多披露几条不可用原因。有界是合同的一部分:不可用的动作可能多达
#: 十来个(无图 + 受限范围),整串重放每轮都要顶掉一大块回喂预算,而模型真正需要
#: 的只是"这几条路走不通,换别的"。
UNAVAILABLE_DISCLOSE_MAX = 6


@dataclass(frozen=True)
class UnavailableAction:
    action_id: str
    reason: str


@dataclass(frozen=True)
class ReflectCapabilities:
    """一轮 reflect 的不可变能力投影。三处消费者共用这**一个**对象。"""

    actions: Tuple[str, ...]
    unavailable: Tuple[UnavailableAction, ...]
    #: 每个**可用**动作的参数(枚举参数的 choices 已按本轮白名单收窄)。
    params: Mapping[str, Tuple[ActionParam, ...]]

    @property
    def recognized_actions(self) -> Tuple[str, ...]:
        """全部**可识别**的动作 id(与本轮可用性无关)。

        schema 的 `next_action` 枚举读它而不是 `actions`:通用形状闸把含 `|` 的
        示例串当闭集(strict 与 repair 两条路径都生效),按本轮配额收窄枚举就等于
        让「配额耗尽但可识别」的动作在到达 `parse_reflect_v2` 之前被判
        `invalid_enum` —— 重试耗尽后整份反思退成 fail-open 的 answer,一次换通道
        的机会变成整个循环终止(`9af6a035e` 刚修掉的形态,比 legacy 还差)。

        可用性由 prompt 的动作清单与 `parse_reflect_v2` 读 `actions` 的白名单承担:
        那两处才能说清「为什么本轮不可用」,并把它记成一条零 I/O、可继续的观察。
        这是一个恒定值而不是字段——构造处填错就没法与三处消费者不同步。
        """
        return ACTION_ORDER

    def has(self, action_id: str) -> bool:
        return action_id in self.actions

    def params_for(self, action_id: str) -> Tuple[ActionParam, ...]:
        return self.params.get(action_id, ())

    def param(self, action_id: str, name: str) -> Optional[ActionParam]:
        for spec in self.params_for(action_id):
            if spec.name == name:
                return spec
        return None

    def reason_for(self, action_id: str) -> str:
        for row in self.unavailable:
            if row.action_id == action_id:
                return row.reason
        return ""

    @property
    def only_answer(self) -> bool:
        """除 ``answer`` 外没有任何可执行动作。

        服务端据此直接收尾(设计稿 §5.1),不再花一次模型调用去请求一个只可能
        回 answer 的决定,也不伪造一条"模型判定充分"的 reflect 步。
        """
        return self.actions == (ANSWER_ACTION,)


@dataclass(frozen=True)
class ReflectCapabilityFacts:
    """投影的**全部**输入。每一项都是调用方已经持有的事实,不为投影新做探测。

    计数器一律是"还剩几次"(``*_left``),不是"已经用了几次":剩余量才是这里唯一
    关心的东西,而把"上限 - 已用"这道减法留在调用处,可以让配额来自档位表还是
    settings 这件事完全不进这个模块。
    """

    # 范围与图的事实
    kg_in_scope: bool = True
    scope_restricted: bool = False
    has_candidates: bool = True
    # 通道接线(部署开关 ∧ 调用方策略位,调用处已经合并成一个 bool)
    chunk_search_active: bool = False
    exact_lookup_active: bool = True
    ppr_active: bool = True
    community_active: bool = True
    enumeration_active: bool = False
    consult_memory_active: bool = False
    outline_active: bool = False
    # 剩余配额
    element_searches_left: int = 0
    chunk_searches_left: int = 0
    exact_lookups_left: int = 0
    ppr_left: int = 0
    follow_chain_left: int = 0
    consult_left: int = 0
    outline_updates_left: int = 0
    enum_rows_left: int = 0
    enum_pages_left: int = 0
    enum_payload_left: int = 0
    # 枚举白名单(唯一字面量定义点在 collection_catalog;这里只是把它带进来)
    element_kinds: Tuple[str, ...] = ()
    object_types: Tuple[str, ...] = ()
    # consult 的产出只进**下一轮**上下文,末轮执行等于白花一步。
    last_turn: bool = False
    # 大纲的一次性溢出纠错资格(普通额度耗尽后仍可用一次)。
    outline_repair_available: bool = False
    # 本轮是"终态大纲溢出纠错轮":只许同结构换键,绝不能借机再发一次检索。
    terminal_overflow_repair: bool = False


def _first_blocker(*checks: Tuple[bool, str]) -> str:
    """返回第一条不成立的条件的原因码;全部成立返回空串。

    顺序即优先级,而顺序是刻意的:**通道级**原因(没有图 / 范围不允许 / 没接线)
    排在**配额级**之前。对模型来说"这条路本轮根本不存在"与"这条路还剩 0 次"是
    两种不同的下一步,前者要换通道,后者可能只是换个问法也没用——先说更根本的
    那个。
    """
    for ok, reason in checks:
        if not ok:
            return reason
    return ""


def build_reflect_capabilities(
    facts: ReflectCapabilityFacts,
) -> ReflectCapabilities:
    """一次纯内存投影:事实 → 本轮可用动作 + 参数 + 不可用原因。

    ``answer`` 永远可用——一个连"停下来作答"都没有的动作面不是更严格,是死锁。
    """
    blockers: Dict[str, str] = {}
    graph_ok = facts.kg_in_scope
    scope_ok = not facts.scope_restricted
    enum_budget_ok = (
        facts.enum_rows_left >= 1
        and facts.enum_pages_left >= 1
        and facts.enum_payload_left >= 1
    )

    # add_subquery 有两条腿:知识图谱检索,以及无图 run 上的原文段落补检
    # (`_search_passages_if_graphless`)。两条都不在时它必然空手——与"无图就摘掉
    # 五个图动作"完全同一类判断,只是这个动作的条件是两条腿的**或**,不是一条。
    blockers[ADD_SUBQUERY_ACTION] = _first_blocker(
        (graph_ok or facts.chunk_search_active, REASON_NO_SUBQUERY_CHANNEL),
    )
    blockers[SEARCH_ELEMENTS_ACTION] = _first_blocker(
        (facts.element_searches_left >= 1, REASON_ELEMENT_CAP),
    )
    blockers[SEARCH_CHUNKS_ACTION] = _first_blocker(
        (facts.chunk_search_active, REASON_CHUNK_DISABLED),
        (facts.chunk_searches_left >= 1, REASON_CHUNK_CAP),
    )
    blockers[EXACT_LOOKUP_ACTION] = _first_blocker(
        (scope_ok, REASON_SOURCE_SCOPE),
        (facts.exact_lookup_active, REASON_EXACT_DISABLED),
        (facts.exact_lookups_left >= 1, REASON_EXACT_CAP),
    )
    blockers[EXPAND_GRAPH_ACTION] = _first_blocker(
        (graph_ok, REASON_NO_GRAPH),
        (scope_ok, REASON_SOURCE_SCOPE),
    )
    blockers[PPR_RETRIEVE_ACTION] = _first_blocker(
        (graph_ok, REASON_NO_GRAPH),
        (scope_ok, REASON_SOURCE_SCOPE),
        (facts.ppr_active, REASON_PPR_DISABLED),
        (facts.ppr_left >= 1, REASON_PPR_CAP),
    )
    blockers[EXPAND_COMMUNITY_ACTION] = _first_blocker(
        (graph_ok, REASON_NO_GRAPH),
        (scope_ok, REASON_SOURCE_SCOPE),
        (facts.community_active, REASON_COMMUNITY_DISABLED),
    )
    # 起点必须是**已持有的**候选(执行处的 chain_start_not_candidate 判据)。
    # 候选池空时这个动作没有任何合法起点,把它摆出来只会换回一条必然的 skip
    # ——图只存在于参考库、而本库一个候选都没捞到时,正是这个形状。
    blockers[FOLLOW_CHAIN_ACTION] = _first_blocker(
        (graph_ok, REASON_NO_GRAPH),
        (scope_ok, REASON_SOURCE_SCOPE),
        (facts.has_candidates, REASON_CHAIN_NO_CANDIDATES),
        (facts.follow_chain_left >= 1, REASON_CHAIN_CAP),
    )
    # 范围收窄排在接线之前:调用方的 `enumeration_active` 已经把「范围受限」折进
    # 去了,只报 `enumeration_disabled` 会与执行处的纵深防御分支对不上——那里记的
    # 是 `source_scope_unsafe_channel`。本模块承诺复用执行处的 skip reason,所以
    # 收窄是原因时就报收窄。
    blockers[ENUMERATE_ELEMENTS] = _first_blocker(
        (scope_ok, REASON_SOURCE_SCOPE),
        (facts.enumeration_active and bool(facts.element_kinds),
         REASON_ENUM_DISABLED),
        (enum_budget_ok, REASON_ENUM_BUDGET),
    )
    blockers[ENUMERATE_KG_OBJECTS] = _first_blocker(
        (graph_ok, REASON_NO_GRAPH),
        (scope_ok, REASON_SOURCE_SCOPE),
        (facts.enumeration_active and bool(facts.object_types),
         REASON_ENUM_DISABLED),
        (enum_budget_ok, REASON_ENUM_BUDGET),
    )
    blockers[CONSULT_MEMORY] = _first_blocker(
        (facts.consult_memory_active, REASON_CONSULT_DISABLED),
        (facts.consult_left >= 1, REASON_CONSULT_CAP),
        (not facts.last_turn, REASON_CONSULT_LAST_TURN),
    )
    blockers[UPDATE_OUTLINE] = _first_blocker(
        (facts.outline_active, REASON_OUTLINE_DISABLED),
        (facts.outline_updates_left >= 1 or facts.outline_repair_available,
         REASON_OUTLINE_BUDGET),
    )

    if facts.terminal_overflow_repair:
        # 终态纠错轮:除了交一份同结构换键的大纲,任何检索动作都不该再被提供
        # ——那一轮的全部意义就是把溢出的绑定换进来然后收尾。
        for action_id in blockers:
            if action_id != UPDATE_OUTLINE:
                blockers[action_id] = REASON_OVERFLOW_REPAIR_ONLY

    available = [ANSWER_ACTION]
    unavailable = []
    for action_id in ACTION_ORDER:
        if action_id == ANSWER_ACTION:
            continue
        reason = blockers[action_id]
        if reason:
            unavailable.append(UnavailableAction(action_id, reason))
        else:
            available.append(action_id)

    params: Dict[str, Tuple[ActionParam, ...]] = {}
    for action_id in available:
        params[action_id] = _narrow_params(action_id, facts)
    return ReflectCapabilities(
        actions=tuple(available),
        unavailable=tuple(unavailable),
        params=MappingProxyType(params),
    )


def _narrow_params(
    action_id: str, facts: ReflectCapabilityFacts,
) -> Tuple[ActionParam, ...]:
    """按本轮事实收窄一个可用动作的参数分支。

    "已耗尽的动作不留下可填写的孤立参数"这条要求在动作层面由 ``available``
    保证;这里管的是**同一个动作内部**的参数分支:无图 run 里 ``add_subquery``
    的 ``types`` 指的是知识对象类型,没有图就没有这个概念,留着只会让模型去想
    一个它填不出所以然的槽位。枚举的 kind/object_type 则从白名单实例化——
    白名单的唯一字面量定义点在 ``collection_catalog``,这里只是把调用方带来的
    那一份原样装进 ``choices``。
    """
    definition = ACTION_DEFINITIONS[action_id]
    narrowed = []
    for spec in definition.params:
        if action_id == ADD_SUBQUERY_ACTION and spec.name == "types":
            if not facts.kg_in_scope:
                continue
        if action_id == ENUMERATE_ELEMENTS and spec.name == "kind":
            spec = ActionParam(
                spec.name, spec.kind, spec.required, spec.required_group,
                facts.element_kinds, spec.note,
            )
        if action_id == ENUMERATE_KG_OBJECTS and spec.name == "object_type":
            spec = ActionParam(
                spec.name, spec.kind, spec.required, spec.required_group,
                facts.object_types, spec.note,
            )
        narrowed.append(spec)
    return tuple(narrowed)
