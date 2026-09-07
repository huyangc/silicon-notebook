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
# 约束(§7.1)。超限的处理是把这一轮的决定折成 invalid,不是把内容截短。
#: 一个方面最多能带几个证据键。
REFLECT_ASPECT_MAX_EVIDENCE_KEYS = 8
#: 一个方面的 `gap`(模型写的"还缺什么")最多多少字符。
REFLECT_ASPECT_GAP_MAX_CHARS = 240


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


@dataclass(frozen=True, slots=True)
class RetrievalTermination:
    """一次 run 的结束事实。**只在 reflect v2 下生成**,legacy 恒为 None。"""

    reason: str
    unresolved_aspect_ids: Tuple[str, ...] = ()
    #: 终止那一轮模型自己是否声称证据已足。与 `reason` 分开:
    #: `retrieval_degraded` 完全可能发生在模型自报充分的同一轮,而把这两件事
    #: 折成一个字段就再也分不出"模型怎么说的"与"服务端看到了什么"。
    model_assessed_sufficient: bool = False
    aspects: Tuple[AspectSnapshot, ...] = field(default_factory=tuple)
