"""必答方面记录与结束原因(设计稿 2026-09-07 §7)。

**纯数据 + 纯函数,零 I/O。**不读 settings、不调模型、不查库、不认识
`ReasoningRetriever`。它回答两个问题:

1. 这次提问**必须回答**哪几件事,每一件现在有没有支撑(`AspectLedger`);
2. 这次检索**为什么停下来**(`classify_termination`)。

三本账互不覆盖(§7.1):

* **方向账**(`attempted` / `label_of`)回答「这条已确认方向有没有被执行过」;
* **方面账**(本模块)回答「这个必答问题有没有被证据支撑」;
* **枚举 coverage** 回答「这个物理集合有没有被列全」。

一条方向跑过了不等于它问的那件事有了答案,一个方面全 supported 也不等于任何
集合被列全——所以三者各有自己的权威,谁也不许替谁作证。

**方面来源从不由模型决定。**它来自用户审阅过的冻结契约(Ask 的
`mandatory_topics`、报告本节的 `intent_questions`),没有契约时以整条问题作为
唯一方面。绝不从模型新提的子查询数量反推必答清单:那等于让被评价方自己出题。

**模型的判断始终标为 `model_assessed`。**服务端只确认三件事(§7.1):方面 id 合
法、证据键在池内且**曾真实展示**、集合/来源身份不能冒充细粒度证据。语义支撑
是模型说的,这里不新增 grounded 分数,也不改 evidence_level 阈值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Iterable, List, Mapping, Optional, Sequence, Set, Tuple,
)

from app.domain.retrieval_termination import (
    ASPECT_CONFLICTING, ASPECT_PARTIAL, ASPECT_SUPPORTED, ASPECT_UNKNOWN,
    ASPECT_UNRESOLVED_STATUSES, AspectSnapshot,
    DEMOTION_KEYS_MISSING, DEMOTION_KEYS_REJECTED,
    REFLECT_ASPECT_GAP_MAX_CHARS, REFLECT_ASPECT_MAX_EVIDENCE_KEYS,
    RetrievalTermination,
    TERMINATION_MODEL_DEGRADED, TERMINATION_MODEL_PARTIAL,
    TERMINATION_MODEL_SUFFICIENT, TERMINATION_NO_EXECUTABLE_ACTION,
    TERMINATION_RETRIEVAL_DEGRADED, TERMINATION_STALE,
    TERMINATION_STEP_BUDGET,
)
from app.services.reasoning_observation import (
    STATUS_EMPTY, STATUS_FAILED, STATUS_PARTIAL, STATUS_SUCCESS, _clip,
)

# 两个 assessment 载荷的协议常量(`REFLECT_ASPECT_MAX_EVIDENCE_KEYS` /
# `REFLECT_ASPECT_GAP_MAX_CHARS`)住在 `app.domain.retrieval_termination`:
# prompt 与校验都要读它们,而 prompts 不能 import 这个服务模块。

#: 方面数上限。复用 `QueryIntentContract.mandatory_topics` 的既有契约上限
#: (`max_length=16`),不另立一个会与它分叉的数。
REFLECT_ASPECT_MAX_COUNT = 16

#: 方面 id 的构造:契约顺序 + 1 起编,`a1..aN`。确定性(同一份契约永远得到同一
#: 组 id),而且**不含用户内容**——id 会被模型抄回来,让它承载问题原文等于给
#: 一个可伪造的自由文本槽位再开一条路。
_ASPECT_ID_PREFIX = "a"

#: 方面原文来自用户契约,**不截长**(§7.1「用户内容不得静默截掉」)。折叠仍然
#: 要做:换行/控制字符/字段分隔符会在渲染出来的清单里伪造出一整行。
#: `_clip` 只在超过上限时才截,给一个到不了的上限就是"只折叠不截长"。
_NO_TRUNCATION = 1 << 30

# --- 方面来源(可观测,不参与判据) -----------------------------------------
ASPECT_SOURCE_INTENT_TOPICS = "intent_topics"
ASPECT_SOURCE_SECTION_QUESTIONS = "section_questions"
ASPECT_SOURCE_WHOLE_QUESTION = "whole_question"

_STATUS_LABELS: Mapping[str, str] = {
    ASPECT_UNKNOWN: "未确认",
    ASPECT_PARTIAL: "部分支撑",
    ASPECT_SUPPORTED: "已支撑",
    ASPECT_CONFLICTING: "证据冲突",
}

ASPECT_BLOCK_TITLE = "【必答方面 — 用户确认过的必答清单，服务端记账】"
ASPECT_BLOCK_NOTE = (
    "（上面的状态是此前某一轮模型自己的判断，不是服务端对语义支撑的证明；"
    "本轮请在同一份 JSON 的 assessment 里重新给出，省略的方面保留现状。）"
)


def _text(value: object) -> str:
    """用户契约里的一段原文 → 单行、无控制字符、分隔符已归一,**不截长**。"""
    return _clip(str(value or ""), _NO_TRUNCATION)


def _topic_text(row: object) -> str:
    """一条 mandatory_topic → 它的问题原文。

    两种形状都要认:Ask 侧 `ReasoningIntentProjection.mandatory_topics` 是
    **字符串列表**(每条就是 `topic.question`),而 `QueryIntentContract` 自己
    的行是 `{"id","title","question",...}` 的字典。只认其中一种,另一条路径的
    方面清单就会静默变空,而"没有方面"在下游与"全部已支撑"长得一样近。
    """
    if isinstance(row, Mapping):
        for key in ("question", "title"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return _text(value)
        return ""
    return _text(row) if isinstance(row, str) else ""


def _bounded_unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= REFLECT_ASPECT_MAX_COUNT:
            break
    return out


@dataclass
class _AspectRecord:
    aspect_id: str
    question: str
    status: str = ASPECT_UNKNOWN
    evidence_keys: Tuple[str, ...] = ()
    gap: str = ""
    model_assessed: bool = False
    demotion: str = ""

    def snapshot(self) -> AspectSnapshot:
        return AspectSnapshot(
            aspect_id=self.aspect_id, question=self.question,
            status=self.status, evidence_keys=self.evidence_keys,
            gap=self.gap, model_assessed=self.model_assessed,
            demotion=self.demotion,
        )


@dataclass
class _Update:
    """一条已经通过全部校验、等着被写进账本的更新。"""

    record: _AspectRecord
    status: str
    keys: Tuple[str, ...]
    gap: str
    demotion: str


class AspectLedger:
    """run 内的必答方面账。只在 reflect v2 总闸开着时被构造(关闭态零新状态)。

    写入只有一个入口 `apply`,而且是**全量替换**:模型这一轮说了哪个方面,那个
    方面的支撑判断就整份换成新的(不像 outline 的证据键那样并集),否则一次错误
    的 supported 就永远撤不回来。没被报告的方面**保留现状**——省略是"我这一轮
    没说它",不是"删掉它";必答清单本身只能由用户的契约改变(§7.1)。
    """

    __slots__ = ("_records", "_by_id", "source", "constraints")

    def __init__(
        self, questions: Sequence[str], *, source: str,
        constraints: Sequence[str] = (),
    ) -> None:
        self._records: List[_AspectRecord] = [
            _AspectRecord(
                aspect_id=f"{_ASPECT_ID_PREFIX}{index + 1}", question=question)
            for index, question in enumerate(questions)
        ]
        self._by_id = {row.aspect_id: row for row in self._records}
        self.source = source
        self.constraints: Tuple[str, ...] = tuple(constraints)

    # --- 读 ---------------------------------------------------------------
    @property
    def aspect_ids(self) -> Tuple[str, ...]:
        return tuple(row.aspect_id for row in self._records)

    def snapshot(self) -> Tuple[AspectSnapshot, ...]:
        return tuple(row.snapshot() for row in self._records)

    def unresolved_ids(self) -> Tuple[str, ...]:
        """状态不是 `supported` 的方面。**从未被报告的 unknown 也算未解决**——
        一个模型一次都没提过的必答项不该因为沉默而看起来像已经完成。"""
        return tuple(
            row.aspect_id for row in self._records
            if row.status != ASPECT_SUPPORTED)

    def supported_count(self) -> int:
        return sum(
            1 for row in self._records if row.status == ASPECT_SUPPORTED)

    def bound_keys(self) -> Tuple[str, ...]:
        """各方面当前绑着的证据键,**按方面轮转**(§6.2 第一档)。

        轮转而不是按方面顺序拼接:先到先得会让第一个方面的 8 个键把第一档的
        预算吃掉,后面几个方面绑的证据在下一轮 prompt 里结构性不可见——而那正
        是模型判断"还差哪一块"要看的东西。轮转是确定性的(按方面顺序取第 k 个
        键),同一份账永远得到同一个序列。

        `conflicting` 的键也带上:它们是**已经找到的、互相矛盾的**那批材料,
        模型下一轮要做的正是再看它们一眼。只有 `unknown` 没有键可带。
        """
        rows = [row.evidence_keys for row in self._records if row.evidence_keys]
        out: List[str] = []
        seen: Set[str] = set()
        depth = 0
        while any(len(keys) > depth for keys in rows):
            for keys in rows:
                if depth < len(keys) and keys[depth] not in seen:
                    seen.add(keys[depth])
                    out.append(keys[depth])
            depth += 1
        return tuple(out)

    # --- 写(唯一入口) ----------------------------------------------------
    def apply(
        self, assessment: object, *, allowed_keys: Set[str],
    ) -> str:
        """把一份 `assessment` 落进账本。返回空串 = 接受;否则是稳定的原因码。

        校验是**全有或全无**:任何一条越界都让整份载荷被拒(调用方据此把这一轮
        折成一条 invalid 观察),账本一个字都不改。半份被吸收的评估比没有评估更
        危险——模型下一轮看到的状态既不是它说的,也不是服务端算的。

        `allowed_keys` = 候选池里**曾真实展示给模型**的那批键
        (`outline_binding_keys` 的口径)。它天然排除枚举条目 id 与来源 id,所以
        "集合/来源身份冒充细粒度证据"在这里是结构上不可能,不是靠一条黑名单。
        非法键**剔除**(不是拒绝整份载荷:模型抄错一个键不该让它对另外几个方面
        的判断也一起作废),剔完没有支撑的项按 §7.1 不得保持 supported。
        """
        if not isinstance(assessment, Mapping):
            return "not_object"
        updates: List[_Update] = []
        claimed: Set[str] = set()
        for group, statuses in (
            ("supported", ()), ("unresolved", ASPECT_UNRESOLVED_STATUSES),
        ):
            rows = assessment.get(group)
            if rows is None:
                continue
            if not isinstance(rows, (list, tuple)):
                return f"{group}_not_list"
            if len(rows) > len(self._records):
                return f"{group}_overflow"
            for row in rows:
                error = self._plan_row(
                    row, group, statuses, claimed, updates, allowed_keys)
                if error:
                    return error
        for update in updates:
            record = update.record
            record.status = update.status
            record.evidence_keys = update.keys
            record.gap = update.gap
            record.model_assessed = True
            record.demotion = update.demotion
        return ""

    def _plan_row(
        self, row: object, group: str, statuses: Tuple[str, ...],
        claimed: Set[str], updates: List[_Update], allowed_keys: Set[str],
    ) -> str:
        if not isinstance(row, Mapping):
            return "item_not_object"
        aspect_id = row.get("aspect_id")
        if not isinstance(aspect_id, str) or aspect_id not in self._by_id:
            return "unknown_aspect"
        if aspect_id in claimed:
            # 同一个方面同时出现在两组、或在同一组里重复(§7.1)。两种情况下
            # "模型到底怎么判的"都没有答案,静默取最后一条等于替它决定。
            return "duplicate_aspect"
        claimed.add(aspect_id)
        raw_keys = row.get("evidence_keys")
        if raw_keys is None:
            raw_keys = ()
        if not isinstance(raw_keys, (list, tuple)):
            return "evidence_keys_not_list"
        if len(raw_keys) > REFLECT_ASPECT_MAX_EVIDENCE_KEYS:
            return "evidence_keys_overflow"
        if any(not isinstance(key, str) for key in raw_keys):
            return "evidence_key_not_string"
        gap = row.get("gap", "")
        if gap is None:
            gap = ""
        if not isinstance(gap, str):
            return "gap_not_string"
        if len(gap) > REFLECT_ASPECT_GAP_MAX_CHARS:
            return "gap_overflow"
        status = ASPECT_SUPPORTED
        if statuses:
            status = row.get("status", ASPECT_PARTIAL)
            if status is None or status == "":
                status = ASPECT_PARTIAL
            if not isinstance(status, str) or status not in statuses:
                return "invalid_status"
        keys, demotion = _legal_keys(raw_keys, allowed_keys)
        if status == ASPECT_SUPPORTED and demotion:
            # 非法键剔除后没有支撑的项**不得保持 supported**(§7.1)。降到哪一档
            # 由依据决定:抄了键但一个都不在池里 ⇒ 它至少指向了什么,记 partial;
            # 一个键都没给 ⇒ 这条 supported 从头到尾没有支撑,记 unknown。
            status = (
                ASPECT_PARTIAL if demotion == DEMOTION_KEYS_REJECTED
                else ASPECT_UNKNOWN)
        else:
            demotion = ""
        updates.append(_Update(
            record=self._by_id[aspect_id], status=status, keys=keys,
            gap=_clip(gap, REFLECT_ASPECT_GAP_MAX_CHARS), demotion=demotion))
        return ""


def _legal_keys(
    raw_keys: Sequence[object], allowed_keys: Set[str],
) -> Tuple[Tuple[str, ...], str]:
    """保序去重 + 只留**曾真实展示**的键。返回 (合法键, 降级依据)。"""
    keys: List[str] = []
    seen: Set[str] = set()
    for key in raw_keys:
        key = str(key)
        if key and key not in seen and key in allowed_keys:
            seen.add(key)
            keys.append(key)
    if keys:
        return tuple(keys), ""
    if raw_keys:
        return (), DEMOTION_KEYS_REJECTED
    return (), DEMOTION_KEYS_MISSING


def build_aspect_ledger(intent_detail: object, question: str) -> AspectLedger:
    """按 §7.1 的三条来源建账。**不新增任何模型调用、不做重规划。**

    1. Ask:冻结意图的 `mandatory_topics`(用户在确认门审阅过的那份原文);
    2. Report:本节的 `intent_questions`(随大纲一起被确认过);
    3. 没有正式契约的兼容路径:整条输入问题作为唯一方面。

    三条都只读调用方已经持有的结构,一次查询都不发。相关约束(`constraints`)
    带在账上一起渲染,但**不各自成为一个方面**:约束是"答案要满足什么",不是
    "还要去查什么",给它一个能被独立标成 supported 的 id 只会造出一批永远停在
    unknown 的方面。
    """
    detail = intent_detail if isinstance(intent_detail, Mapping) else {}
    constraints = tuple(
        _text(item) for item in (detail.get("constraints") or ())
        if _text(item)
    )[:REFLECT_ASPECT_MAX_COUNT]
    topics = _bounded_unique(
        _topic_text(row) for row in (detail.get("mandatory_topics") or ()))
    if topics:
        return AspectLedger(
            topics, source=ASPECT_SOURCE_INTENT_TOPICS,
            constraints=constraints)
    section_questions = _bounded_unique(
        _text(row) for row in (detail.get("intent_questions") or ()))
    if section_questions:
        return AspectLedger(
            section_questions, source=ASPECT_SOURCE_SECTION_QUESTIONS,
            constraints=constraints)
    whole = _text(question)
    return AspectLedger(
        [whole] if whole else [], source=ASPECT_SOURCE_WHOLE_QUESTION,
        constraints=constraints)


def render_aspect_block(ledger: AspectLedger) -> str:
    """方面清单 → 服务器状态块的一段。

    **不受 `state_chars` 约束**:§4 明确把「已冻结约束/必答主题」列在"按现有
    输入/协议边界保留、不得整体裁尾"的那一类里。可压缩的是观察账与历史建议,
    不是用户确认过的必答清单。

    每一行:id | 状态 | 已绑定证据数 | 方面原文 | 缺口。原文与约束是用户内容,
    只折叠不截长;`gap` 是模型文本,按模型文本的上限截。
    """
    rows = ledger.snapshot()
    if not rows:
        return ""
    lines = [
        f"{ASPECT_BLOCK_TITLE}"
        f"（已支撑 {ledger.supported_count()}/{len(rows)}）"
    ]
    for row in rows:
        parts = [
            f"- {row.aspect_id}",
            _STATUS_LABELS.get(row.status, row.status),
            f"已绑定证据 {len(row.evidence_keys)} 条",
            row.question,
        ]
        if row.gap:
            parts.append(f"缺口: {row.gap}")
        if row.demotion:
            # 服务端把自报 supported 降下来的依据。写在这一行上,模型下一轮才
            # 知道它引的键为什么不算数(而不是以为服务端随手改了它的判断)。
            parts.append(f"服务端降级: {row.demotion}")
        lines.append(" | ".join(parts))
    if ledger.constraints:
        lines.append("约束条件: " + "、".join(ledger.constraints))
    lines.append(ASPECT_BLOCK_NOTE)
    return "\n".join(lines)


def evidence_bound_keys(
    ledger: Optional[AspectLedger], outline_keys: Sequence[str],
) -> List[str]:
    """证据卡第一档的键序(§6.2)。

    = 「各方面已绑定的键(按方面轮转)」+「大纲绑定里还没被带上的键」。两者都是
    「当前已绑定且存活的证据的代表」,合起来才是第一档;顺序上方面在前,因为
    大纲的键上一轮模型刚看过整份结构,而方面代表是它自己说"这条撑住了这一项"
    的那些。

    T3 的留底规则(`_FRESH_RESERVE_RATIO`)一个字都不变:它切的是第一档**整体**
    的份额,与这一档里怎么排序无关。
    """
    keys = list(ledger.bound_keys()) if ledger is not None else []
    seen = set(keys)
    for key in outline_keys:
        key = str(key)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


# --- 结束原因(§7.2) --------------------------------------------------------
#: `run()` 里那三条真正让循环停下来的 skip 原因码。判据读 trace 而不是在每个
#: break 上再挂一个赋值:那几处都在零松弛长度天花板下的 `run()` 里,而 trace
#: 本来就是这些事实的权威记录(每一条都是执行处刚写下的结构化 detail)。
SKIP_NO_EXECUTABLE_ACTION = "no_executable_action"
SKIP_STALE_CIRCUIT_BREAKER = "stale_circuit_breaker"

#: 终态 trace 步的稳定原因码。它是 run 级叙述,不产生动作观察。
TERMINATION_SKIP_REASON = "retrieval_termination"

_TERMINATION_SUMMARIES: Mapping[str, str] = {
    TERMINATION_MODEL_SUFFICIENT: "检索结束：每个必答方面都已找到支撑",
    TERMINATION_MODEL_PARTIAL: "检索结束：仍有必答方面没有完整支撑",
    TERMINATION_STEP_BUDGET: "检索结束：步骤预算用完",
    TERMINATION_STALE: "检索结束：连续多轮没有新进展",
    TERMINATION_NO_EXECUTABLE_ACTION: "检索结束：除作答外已无可执行的动作",
    TERMINATION_MODEL_DEGRADED: "检索结束：反思调用失败，按降级收尾",
    TERMINATION_RETRIEVAL_DEGRADED: "检索结束：检索通道异常，证据收集未正常完成",
}


def termination_summary(reason: str) -> str:
    return _TERMINATION_SUMMARIES.get(reason, "检索结束")


def _terminal_marker(trace: Sequence[object]) -> Optional[Tuple[str, bool, bool]]:
    """trace 里**第一个**真正让循环停下来的标记 → (种类, 自报充分, 是否兜底)。

    取第一个而不是最后一个,正是 §7.2 的「保留真实首先触发的终止条件」:大纲
    溢出纠错轮跑在 stale 熔断/模型收尾**之后**,它自己那一轮的 reflect 决定
    (完全可能带着 `sufficient=true` 的 update_outline)不许把已经发生的
    stale/预算原因改写成"充分"。第一个标记之后循环最多再走一轮纠错就结束,所以
    "第一个"与"真正让它停下来的那个"是同一件事。
    """
    for step in trace:
        step_type = str(getattr(step, "step_type", "") or "")
        detail = getattr(step, "detail", None)
        detail = detail if isinstance(detail, Mapping) else {}
        if step_type == "skip":
            reason = str(detail.get("reason", "") or "")
            if reason == SKIP_NO_EXECUTABLE_ACTION:
                return (TERMINATION_NO_EXECUTABLE_ACTION, False, False)
            if reason == SKIP_STALE_CIRCUIT_BREAKER:
                return (TERMINATION_STALE, False, False)
        elif step_type == "reflect":
            if detail.get("fallback_reason"):
                # fail-open 兜底:`next_action` 恒为 answer、`sufficient` 恒为
                # True,但那两格**不是模型判的**。先认它,免得下面那一支把一次
                # provider 失败读成"模型认为证据够了"(§5.2)。
                return (TERMINATION_MODEL_DEGRADED, False, True)
            if (detail.get("next_action") == "answer"
                    or bool(detail.get("sufficient"))):
                return ("model_end", bool(detail.get("sufficient")), False)
    return None


#: 「这一次真的执行到底了」的三档。零 I/O 的那几档(duplicate / unavailable /
#: invalid)连试都没试,不能算作一次通道恢复的证明。
_EXECUTED_STATUSES = (STATUS_SUCCESS, STATUS_EMPTY, STATUS_PARTIAL)


def _retrieval_degraded(observations: Sequence[object]) -> bool:
    """有没有哪条通道**最后一次执行是炸的**(§7.2)。

    判据按**通道**(action_id)而不是整条时间线:「单个可恢复工具失败若随后继续
    完成检索,只作为 observation 留存」说的是**那个工具**又跑通了,不是别的通道
    跑通了。按时间线取最后一条会让"原文库整个读不出来、于是一段正文都没有"被
    紧随其后的一次空手元素检索(执行成功、返回 0 条)盖掉——而那两件事该导致的
    披露正好相反。

    `empty` 算恢复:通道是通的、这个问法在库里真的没有内容,与"没查成"严格
    区分(这也是 `note_failed` 侧信道存在的全部理由)。
    """
    last: dict = {}
    for row in observations:
        status = str(getattr(row, "status", "") or "")
        if status == STATUS_FAILED or status in _EXECUTED_STATUSES:
            last[str(getattr(row, "action_id", "") or "")] = status
    return any(status == STATUS_FAILED for status in last.values())


def classify_termination(
    trace: Sequence[object], observations: Sequence[object],
    ledger: AspectLedger,
) -> RetrievalTermination:
    """服务端在 run 收尾生成的结束事实(§7.2)。

    优先级:异常降级 → 服务端能力收尾/熔断 → 模型的正常结束 → 预算耗尽。

    * `model_degraded` 排第一:那一轮根本没有模型决定可言,把它读成任何一种
      "模型说的"都是谎报。
    * `retrieval_degraded` 排第二,**会盖过模型自报的充分**:证据收集没有正常
      完成这件事不因为模型说"够了"而消失。模型怎么说的仍然保留在
      `model_assessed_sufficient` 里,两个口径不合并。
    * `step_budget` 排最后,而且只在 trace 里**一个终止标记都没有**时才出现
      ——不在最终步骤号等于 max_steps 时覆盖同一轮模型已经作出的正常结束决定。

    取消与不可恢复的阶段错误从不走到这里:它们在 `run()` 里照常上抛,不会被
    包装成一份"成功生成的终态"(§7.2)。
    """
    unresolved = ledger.unresolved_ids()
    marker = _terminal_marker(trace)
    kind, model_sufficient, degraded = marker or (
        TERMINATION_STEP_BUDGET, False, False)
    if degraded:
        reason = TERMINATION_MODEL_DEGRADED
    elif _retrieval_degraded(observations):
        reason = TERMINATION_RETRIEVAL_DEGRADED
    elif kind == "model_end":
        # 「自报充分与方面记录矛盾」与「模型结束但仍有未解决」在 §7.2 是同一个
        # 结果(`model_partial`),所以这里一条判据同时覆盖两种。
        reason = (
            TERMINATION_MODEL_SUFFICIENT
            if model_sufficient and not unresolved
            else TERMINATION_MODEL_PARTIAL)
    else:
        reason = kind
    return RetrievalTermination(
        reason=reason,
        unresolved_aspect_ids=unresolved,
        model_assessed_sufficient=model_sufficient and not degraded,
        aspects=ledger.snapshot(),
    )
