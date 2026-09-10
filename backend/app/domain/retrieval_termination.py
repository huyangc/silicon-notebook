"""一次检索 run 的**结束事实**与必答方面快照(设计稿 2026-09-07 §7)。

纯 DTO:两个 frozen dataclass 和两个闭集常量表。放在 `app.domain` 是因为它要
沿 `ReasoningResult → ReasoningEvidenceSnapshot → ResponseDraftInput` 这条链
跨到 application 层,而那一层只许 import 具名的 domain 模块;服务层持有生成它
的规则(见 `app.services.reasoning_aspects`),这里一个判据都不放。

**它记录的是「这次检索为什么停下来」,不是「答案对不对」。** 三个口径必须始终
分开(§7.2):模型说某个方面有支撑(`model_supported`,即这里的方面状态)、这批
证据进了合成 prompt(`synthesis_admitted`)、最终答案真的引用了它
(`answer_cited`)。后两者由 Ask/Report 的装配与既有引用校验各自回答,这个模块
既不知道也不假装知道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

# --- 方面状态(闭集) --------------------------------------------------------
#: 还没有任何模型判断。**初始值**,也是模型从未报告过的方面的终值。
ASPECT_UNKNOWN = "unknown"
#: 有一些支撑,但模型认为还不够回答这个方面。
ASPECT_PARTIAL = "partial"
#: 模型判定这个方面已被现有证据支撑。始终是 `model_assessed`——服务端只确认
#: 引用的键合法,从不自己判断语义支撑(§7.1)。
ASPECT_SUPPORTED = "supported"
#: 找到的材料互相矛盾。
ASPECT_CONFLICTING = "conflicting"

ASPECT_STATUSES: Tuple[str, ...] = (
    ASPECT_UNKNOWN, ASPECT_PARTIAL, ASPECT_SUPPORTED, ASPECT_CONFLICTING,
)
#: 模型在 `assessment.unresolved` 里可以声明的状态。`supported` 不在其中:那一组
#: 有自己的键(`assessment.supported`),而"未解决且已支撑"不是一个状态。
ASPECT_UNRESOLVED_STATUSES: Tuple[str, ...] = (
    ASPECT_PARTIAL, ASPECT_CONFLICTING, ASPECT_UNKNOWN,
)

# --- 结束原因(闭集,§7.2) --------------------------------------------------
#: 模型明确结束且自报充分,方面记录里没有已知未解决项。
TERMINATION_MODEL_SUFFICIENT = "model_sufficient"
#: 模型选择结束,但仍有 unknown/partial/conflicting;或自报充分与方面记录矛盾。
TERMINATION_MODEL_PARTIAL = "model_partial"
#: 正常步骤预算耗尽。
TERMINATION_STEP_BUDGET = "step_budget"
#: 既有无进展熔断触发。
TERMINATION_STALE = "stale"
#: 除 answer 外无可执行动作,服务端直接收尾。
TERMINATION_NO_EXECUTABLE_ACTION = "no_executable_action"
#: 模型/JSON 在既有重试之后仍失败,决定是 fail-open 兜底。**不是**"模型认为够了"。
TERMINATION_MODEL_DEGRADED = "model_degraded"
#: 检索器/工具异常触发既有 fail-open 收尾,证据收集未正常完成。
#:
#: ⚠ 循环内**可达**的失败面比名字看起来窄:动作执行器里只有 `add_subquery` 的
#: 原文半(无图库补本库原文那条路)带 fail-open + `note_failed` 侧信道,首轮播种
#: 的 `_chunk_seed_search` 同款。`_action_search_chunks` 等其余执行器**没有**
#: fail-open——检索器异常直接穿出 `run()`,按取消/阶段错误的既有合同终止,根本走
#: 不到这里(§7.2「取消和不可恢复阶段错误仍是异常」)。所以这个 reason 覆盖的是
#: 「已经被既有 fail-open 吞掉、只在观察账上留下 failed」的那一类,不是所有异常。
TERMINATION_RETRIEVAL_DEGRADED = "retrieval_degraded"

TERMINATION_REASONS: Tuple[str, ...] = (
    TERMINATION_MODEL_SUFFICIENT,
    TERMINATION_MODEL_PARTIAL,
    TERMINATION_STEP_BUDGET,
    TERMINATION_STALE,
    TERMINATION_NO_EXECUTABLE_ACTION,
    TERMINATION_MODEL_DEGRADED,
    TERMINATION_RETRIEVAL_DEGRADED,
)

#: 服务端把一个自报 supported 的方面降级时留下的稳定依据码。空串 = 没有降级。
DEMOTION_KEYS_REJECTED = "evidence_keys_rejected"
DEMOTION_KEYS_MISSING = "evidence_keys_missing"

# --- assessment 载荷的协议常量(§7.1) ---------------------------------------
# 它们同时被 prompt(告诉模型上限)与校验(拒绝越界载荷)读,所以住在两边都能
# import 的这一层:各写一份字面量就会出现"提示说 8、校验按 6 拒"的分叉。
#
# ⚠ 这两个上限**只针对模型生成的内部载荷**,绝不用来裁剪用户的主题、问题或
# 约束(§7.1)。超限的处理是**按方面**拒绝那一条更新(见
# `ASPECT_REJECTION_REASONS`),不是把内容截短,也不再作废整轮。
#: 一个方面最多能带几个证据键。
REFLECT_ASPECT_MAX_EVIDENCE_KEYS = 8
#: 一个方面的 `gap`(模型写的"还缺什么")最多多少字符。
REFLECT_ASPECT_GAP_MAX_CHARS = 240

#: 一组自评(`supported`/`unresolved`)的行数上限:相对方面数的倍率,与它并行的
#: 绝对行数上限,取**较小**的那一个当上限(`reasoning_aspects._group_row_cap`)。
#: 方面少的时候按倍率收紧,方面多的时候由绝对值兜住;这道闸只防"载荷大得不像
#: 一次自评",组内条数早已不再与方面总数逐一比对(T-BF7 评审 P1)。与上面两个
#: 常量同样的理由住在这里:prompt(T-PL3 lean 自评段)与校验
#: (`reasoning_aspects._group_row_cap`)都要读同一份数字,不能各写一份字面量。
REFLECT_ASPECT_GROUP_ROWS_FACTOR = 4
REFLECT_ASPECT_GROUP_ROWS_HARD_MAX = 64

#: 一条 assessment **逐方面**被拒时的稳定原因码(闭集,§6「动作与 assessment
#: 独立校验」)。每一条都只说明「**这一个方面**的这次更新不成立」:模型写了一个
#: 不在清单里的 id、同一个方面给了互相冲突的两条判断、某个方面的证据键或 gap
#: 超过协议上限、某个方面那一行的 `evidence_keys`/`gap`/`status` 字段类型或取值
#: 不合协议。它们**不作废同一轮里其它合法的方面更新,更不作废那一轮真实的检索
#: 动作**——一次自评笔误吞掉一次已经通过全部参数校验的检索,正是生产 68 个
#: v2 run 里 17 轮(每轮约 40 秒)白烧的根因。
#:
#: ⚠ **后四条原来在整份那一族**(评审 F3)。它们都是**某一行的字段**错误,而那
#: 一行自报的 `aspect_id` 已经确定是本账本里的哪一个方面——「这一个方面这次没被
#: 采纳」因此有可明确解释的读法,另外几个方面的判断以及那一轮真实的检索动作与
#: 它无关。留在整份那一族等于让一个写错了 `status` 的方面继续吞掉整轮,而 §6 要
#: 的正是相反的东西。行本身**不是对象**(`item_not_object`)时读不出 `aspect_id`,
#: 所以那一条仍然整份拒绝。
#:
#: 闭集之外的原因码(`not_object` / `<group>_not_list` / `item_not_object` /
#: `<group>_overflow`)说的是**整份载荷的形状**不成立,按原样整轮拒绝:那种载荷
#: 里"模型到底怎么判的"没有可明确解释的读法,而 §6 只要求接受"可明确解释的有效
#: 方面更新"。`<group>_overflow` 自 T-BF7 评审起只剩**防超大载荷**那一档
#: (见 `reasoning_aspects._group_row_cap`),行数与方面数的比对已经由逐方面
#: 去重/冲突判据取代。
#:
#: 三个消费者共用这一份闭集(所以它住在两边都 import 得到的 domain 层):
#: `services.reasoning_aspects` 产生它,`services.reasoning_observation` 据它
#: 判「这条 skip 不是一次动作观察」(同轮那次动作真的执行了,它自己另有一行),
#: `domain.reasoning_trace_stats` 据它数 `assessment_rejections`。
ASPECT_REJECTION_REASONS: Tuple[str, ...] = (
    "unknown_aspect", "duplicate_aspect",
    "evidence_keys_overflow", "gap_overflow",
    "evidence_keys_not_list", "evidence_key_not_string",
    "gap_not_string", "invalid_status",
)

#: assessment 被拒时那条 skip 步的原因码前缀。整份形状错误(整轮 invalid)与
#: 逐方面拒绝(动作照常执行)**共用这个前缀**:评估口径按原因码词面延续,两者
#: 由后缀是否属于 `ASPECT_REJECTION_REASONS` 区分。
ASSESSMENT_SKIP_REASON_PREFIX = "invalid_assessment:"

#: **服务端签发的集合身份键**的前缀(§7.1)。目录题("这个库里有哪些文档?")的
#: 支撑不是任何一条细粒度证据,而是「这个集合已经被完整列出」这件事本身——枚举
#: 条目按合同不进候选池、也没有对模型可见的 id,所以没有它,一个覆盖完整的目录
#: 问答**结构上**永远拿不到 supported。
#:
#: 它只由服务端为 **coverage 完整**的枚举链签发并展示(见
#: `reasoning_retrieval.enum_evidence_key`);模型自己拼一个出来,`AspectLedger`
#: 照常剔除(合法集是服务端算出来的那一份,不是按前缀放行)。§7.1 原来那条
#: 「集合/来源身份不能冒充细粒度证据」因此收窄成「**未完整**枚举的集合身份不能
#: 冒充」——完整枚举是一个服务端自己记的、可核对的事实,而不是模型的自述。
ASPECT_COLLECTION_KEY_PREFIX = "enum:"


@dataclass(frozen=True, slots=True)
class AspectSnapshot:
    """一个必答方面在 run 结束那一刻的样子。

    ``question`` 是**用户审阅过的原文**(Ask 的冻结 mandatory_topic,或报告本节
    的 intent_question,或整条问题),不是文档内容、也不是模型改写过的版本。
    ``gap`` 相反,是**模型写的**一句话,渲染时按模型文本处理。
    """

    aspect_id: str
    question: str
    status: str
    evidence_keys: Tuple[str, ...] = ()
    gap: str = ""
    #: 这个状态来自模型的 `assessment`。服务端从不自己判定语义支撑,所以
    #: `status == supported` 必然 `model_assessed`;反过来不成立(模型报告过
    #: 一个 partial 也是 model_assessed)。
    model_assessed: bool = False
    #: 服务端把一个自报 supported 的方面降下来的依据(见上面两个常量)。
    demotion: str = ""
    #: 服务端**问过之后**模型仍然没有对这个方面给出任何判断。与
    #: `model_assessed=False` 刻意分开:后者只说"这一格没有模型判断",而它可能
    #: 只是因为 run 早早被熔断/预算收尾,模型根本没走到收尾那一步;这一格说的
    #: 是"模型宣布证据已足、服务端退回并明确要它逐个自评、它第二次仍然没给"
    #: (§7.1)。放量评估要能把这两种沉默分开数,否则一次协议不合作会被读成一次
    #: 正常的中途收尾。
    assessment_omitted: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalTermination:
    """一次 run 的结束事实。**只在 reflect v2 下生成**,legacy 恒为 None。"""

    reason: str
    unresolved_aspect_ids: Tuple[str, ...] = ()
    #: 终止那一轮模型自己是否声称证据已足。与 `reason` 分开:一次没被恢复的
    #: 通道故障完全可能发生在模型自报充分的同一轮,而把这两件事折成一个字段就
    #: 再也分不出"模型怎么说的"与"服务端看到了什么"。
    model_assessed_sufficient: bool = False
    #: 这次 run 里**最后一次执行仍是失败**的通道(action_id)。纯披露字段:
    #: `reason` 不读它(判据见 `classify_termination`)。一条通道在这里出现只
    #: 说明"这条路这次没走通",不说明整次检索失败——两个口径分开,正是为了不让
    #: "哪条路没走通"与"这次检索有没有正常收尾"互相冒充(§7.2)。
    unrecovered_channels: Tuple[str, ...] = ()
    aspects: Tuple[AspectSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """闭集守卫:`reason` 与每个方面的 `status` 都必须在各自的闭集里。

        这两个闭集是跨层契约——终态沿 `ReasoningResult → ReasoningEvidenceSnapshot
        → ResponseDraftInput` 走到披露与合成,那边按 reason 查文案、按 status 决
        定怎么说"未解决"。一个拼错的字符串在下游只会静默退化成"检索结束"这类兜底
        文案,谁都不会红;在这里响亮地失败,是让它变成一次**构造期**的错误。

        DTO 只认识自己的闭集,不认识生成规则(那在 `app.services.reasoning_aspects`)
        ——这是形状校验,不是判据。

        **`unrecovered_channels` 刻意不校验元素。**它不是闭集:元素是动作 id
        (`add_subquery` / `search_chunks` …),而动作空间由能力投影按档位/图状态/
        配额逐轮决定(§5.1),在这一层复制一份动作名清单等于给同一个闭集立第二个
        权威——两边分叉时先红的会是这个无辜的 DTO,而真正该红的是能力投影。上面
        两个闭集不同:`reason` 与 `status` 的取值就在本模块里定义,校验它们是自证。
        """
        if self.reason not in TERMINATION_REASONS:
            raise ValueError(f"unknown termination reason: {self.reason!r}")
        for aspect in self.aspects:
            if aspect.status not in ASPECT_STATUSES:
                raise ValueError(
                    f"unknown aspect status: {aspect.status!r}")


@dataclass(frozen=True, slots=True)
class AspectDelivery:
    """最终装配之后的方面复核(§7.2)。**三个口径,各答各的问题。**

    * ``model_supported``——模型说这个方面有支撑(= `AspectSnapshot.status ==
      supported`,服务端只验过键合法,没验语义);
    * ``synthesis_admitted``——它绑的证据里**至少有一条真的进了合成 prompt**;
    * ``answer_cited``——最终答案里真的出现了绑到那条证据的 `[k]` 锚点。

    三者是包含关系吗?**不是,而且不许假设是。**一个方面可以 model_supported 却
    没进 prompt(预算截掉),可以进了 prompt 却没被引用(模型没写它),也可以被引用
    却从来不是 supported(模型标 partial 却仍引了那条证据)。折成一个"支撑度"就
    再也分不出"谁说的"与"发生了什么"——这正是 §7.2 要求分开记的理由。

    ``undelivered`` 是这里唯一的**判断**:一个 supported/partial 方面绑过证据,
    而那些证据**全部**被最终装配的预算/过滤挡在外面。它是一个并列集合,不是
    `AspectSnapshot.status` 的第五档——状态闭集说的是"模型这一轮怎么判的",而
    未送达说的是"服务端最后送了什么进 prompt",两本账混一起就又要靠猜来还原。
    绑定为空的方面不在其中:什么都没绑,就谈不上"被移除"。
    """

    model_supported: Tuple[str, ...] = ()
    synthesis_admitted: Tuple[str, ...] = ()
    answer_cited: Tuple[str, ...] = ()
    undelivered: Tuple[str, ...] = ()
