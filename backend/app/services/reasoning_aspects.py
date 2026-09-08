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
    ASPECT_COLLECTION_KEY_PREFIX,
    ASPECT_CONFLICTING, ASPECT_PARTIAL, ASPECT_SUPPORTED, ASPECT_UNKNOWN,
    ASPECT_UNRESOLVED_STATUSES, AspectDelivery, AspectSnapshot,
    DEMOTION_KEYS_MISSING, DEMOTION_KEYS_REJECTED,
    REFLECT_ASPECT_GAP_MAX_CHARS, REFLECT_ASPECT_MAX_EVIDENCE_KEYS,
    RetrievalTermination,
    TERMINATION_MODEL_DEGRADED, TERMINATION_MODEL_PARTIAL,
    TERMINATION_MODEL_SUFFICIENT, TERMINATION_NO_EXECUTABLE_ACTION,
    TERMINATION_RETRIEVAL_DEGRADED, TERMINATION_STALE,
    TERMINATION_STEP_BUDGET,
)
from app.services.reasoning_actions import ACTION_DEFINITIONS
from app.services.reasoning_observation import (
    STATUS_EMPTY, STATUS_FAILED, STATUS_PARTIAL, STATUS_SUCCESS, _clip,
)

# 两个 assessment 载荷的协议常量(`REFLECT_ASPECT_MAX_EVIDENCE_KEYS` /
# `REFLECT_ASPECT_GAP_MAX_CHARS`)住在 `app.domain.retrieval_termination`:
# prompt 与校验都要读它们,而 prompts 不能 import 这个服务模块。

#: 方面数上限。复用 `QueryIntentContract.mandatory_topics` 的既有契约上限
#: (`max_length=16`),不另立一个会与它分叉的数。
REFLECT_ASPECT_MAX_COUNT = 16

#: 收尾载荷缺 `assessment` 时,服务端最多**退回并追问几次**。
#:
#: 1 是刻意的。2026-09-08 的本机实测里 16 个 run 全部终于 `model_partial`,而
#: 其中 14 个的根因是同一件事:模型在 `answer` + `sufficient=true` 的那一轮
#: 根本没填 `assessment`,方面账因此一格都没更新,合成 prompt 于是恒收到「仍有
#: 方面没有完整支撑」——一句与检索实际成色无关的话。追问一次把「忘了填」这一类
#: 救回来;追问第二次要花的是模型不合作时的真金白银(一轮反思调用 + 一步预算),
#: 而它换不回新的信息:第二次仍然不填,说明这不是遗漏。所以第二次直接接受收尾,
#: 并把「问过了、它没给」如实记进快照(`assessment_omitted`),不再空转。
REFLECT_ASSESSMENT_MAX_PROMPTS = 1

#: 方面 id 的构造:契约顺序 + 1 起编,`a1..aN`。确定性(同一份契约永远得到同一
#: 组 id),而且**不含用户内容**——id 会被模型抄回来,让它承载问题原文等于给
#: 一个可伪造的自由文本槽位再开一条路。
_ASPECT_ID_PREFIX = "a"

#: 方面原文来自用户契约,**不截长**(§7.1「用户内容不得静默截掉」)。折叠(换行/
#: 控制字符/字段分隔符会在渲染出来的清单里伪造出一整行)只在**渲染时**做,见
#: `_fold`:账本与快照里存的是用户审阅过的那份原文,分隔符替换属于某一种渲染的
#: 自我保护,不该改写下游(披露、诊断、以后的结构化输出)读到的用户内容。
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
#: 收尾载荷缺 `assessment` 被退回之后,下一轮回喂的那一句。**指名道姓列出方面
#: id**:上一轮那份载荷证明了泛泛一句「请填 assessment」不够——它在系统段里已经
#: 说过一遍了。这里说的是「就这几个 id,一个都不能少」,并把再次省略的后果写在
#: 同一句里,让模型知道沉默不会换来另一次追问。
#: 措辞刻意**只描述现在要做什么**,不描述上一轮发生了什么。「那一轮已被退回、
#: 未执行任何检索」这类回述只在紧接着被退回的下一轮为真;这一句一旦挂在整个
#: run 的每一轮上(接入时的形状),它在第三轮之后就是一句假话——而模型据以推断
#: 「我上一轮什么都没查到」的正是这句话。现在它只渲染一次(`nudge_pending`),
#: 回述也一并去掉,两条改动同向:让这句话在任何一轮读起来都成立。
ASPECT_ASSESSMENT_NUDGE = (
    "⚠ 你上一次的收尾载荷（next_action=answer，或任何带 sufficient=true 的"
    "动作）没有给出 assessment。本轮的 JSON 必须带 assessment，并对下面每一个"
    "方面 id 各给一条判断——有支撑的放进 supported 并附上证据卡上的键，还缺"
    "东西的放进 unresolved 并附上 status 与 gap：{ids}。"
    "若再次省略，服务端将按「仍有方面没有完整支撑」收尾。"
)


def _text(value: object) -> str:
    """用户契约里的一段原文。只去两端空白,**内容一个字都不动**。"""
    return str(value or "").strip()


def _fold(text: str) -> str:
    """渲染态的一行:折成单行、去控制字符、归一分隔符,**不截长**。

    两个调用点,都是**某一种行式渲染的自我保护**,不是用户内容的规范化:
    `render_aspect_block` 的 `a | b | c` 行,与 `render_termination_block` 里那条
    `; ` 分隔的未解决方面清单。存进快照的仍是原文,所以主题里真的带着 `|` 或 `;`
    的用户不会在别处看到自己的问题被改写成 `，`。
    """
    return _clip(text, _NO_TRUNCATION)


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
    assessment_omitted: bool = False

    def snapshot(self) -> AspectSnapshot:
        return AspectSnapshot(
            aspect_id=self.aspect_id, question=self.question,
            status=self.status, evidence_keys=self.evidence_keys,
            gap=self.gap, model_assessed=self.model_assessed,
            demotion=self.demotion,
            assessment_omitted=self.assessment_omitted,
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

    __slots__ = (
        "_records", "_by_id", "source", "constraints",
        "assessment_prompts", "assessment_omitted", "nudge_pending",
    )

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
        #: 服务端已经因为「收尾载荷缺 assessment」退回过几轮(见
        #: `note_missing_assessment`)。
        self.assessment_prompts: int = 0
        #: 追问用完之后模型仍然没给自评 ⇒ 这次 run 的方面账是**模型没参与**的
        #: 那一种,不是"它判断还差东西"。
        self.assessment_omitted: bool = False
        #: 追问句「还欠着」——被退回之后**只渲染一次**(见 `render_aspect_block`)。
        #: 与 `assessment_prompts` 分开:那一格是"一共退回过几次"的计数(判据是
        #: 它),这一格是"这一句现在还该不该出现"的一次性开关。
        self.nudge_pending: bool = False

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

    # --- 写:收尾载荷缺自评(§7.1) ----------------------------------------
    def note_missing_assessment(self, *, may_prompt: bool = True) -> bool:
        """模型在收尾那一轮没有给出 `assessment`。返回 True = 退回并追问一次。

        为什么不接受这种收尾:一份**收尾载荷**(`answer`,或任何带
        `sufficient=true` 的动作)的字面意思是「这次检索到此为止/每个必答方面都
        已经有支撑」,而**方面账是这句话的唯一落点**。载荷里没有 `assessment`,
        账本就一格都不更新,于是同一份决定在两处给出互相矛盾的读数——模型说
        够了,服务端的必答清单上全是 `unknown`。`classify_termination` 据后者判
        `model_partial`,合成 prompt 因此恒挂一句「仍有方面没有完整支撑」。这不是
        "保守",而是**服务端根本没拿到模型的判断**却按它作了叙述。

        追问一次而不是无限次(见 `REFLECT_ASSESSMENT_MAX_PROMPTS`)。用完之后接受
        收尾,并把还没被判断过的方面标上 `assessment_omitted`——它们的状态仍然是
        `unknown`(服务端从不替模型判 supported),但快照里从此能分出"模型没走到
        这一步"与"问过了、它不给"。

        ``may_prompt=False`` = 调用方已经知道**这一轮之后不会再有下一轮**(步数
        用尽,或这一轮是服务端强制的大纲溢出纠错轮——它无论如何都要 break)。这时
        退回没有任何收益:追问句会渲染给一个不会到来的回合,而被折成 invalid 的
        收尾会把模型自己的 `sufficient` 判读换成一句 `step_budget`。所以直接按
        「第二次沉默」处理——接受收尾、记 `assessment_omitted`,追问一次的额度
        留在原地。

        没有方面的 run(兼容路径下问题原文为空)返回 False:没有清单可以逐个自评,
        追问一句"请对下列 0 个方面各给一条判断"只会烧掉一轮。
        """
        if not self._records:
            return False
        if (not may_prompt
                or self.assessment_prompts >= REFLECT_ASSESSMENT_MAX_PROMPTS):
            self.assessment_omitted = True
            for row in self._records:
                if not row.model_assessed:
                    row.assessment_omitted = True
            return False
        self.assessment_prompts += 1
        self.nudge_pending = True
        return True

    # --- 写(唯一入口) ----------------------------------------------------
    def apply(
        self, assessment: object, *, allowed_keys: Set[str],
    ) -> str:
        """把一份 `assessment` 落进账本。返回空串 = 接受;否则是稳定的原因码。

        校验是**全有或全无**:任何一条越界都让整份载荷被拒(调用方据此把这一轮
        折成一条 invalid 观察),账本一个字都不改。半份被吸收的评估比没有评估更
        危险——模型下一轮看到的状态既不是它说的,也不是服务端算的。

        `allowed_keys` 是**两份服务端签发的身份的并集**(调用方 `_absorb_assessment`
        算出来的那一份):

        * 候选池里**曾真实展示给模型**的细粒度证据键(`outline_binding_keys` 的
          口径)。它天然排除枚举条目 id 与来源 id,所以"条目/来源身份冒充细粒度
          证据"在这里是结构上不可能,不是靠一条黑名单;
        * 本 run 内 coverage 报告 **complete** 的枚举链的集合完整性键
          (`complete_enumeration_keys`,`enum:` 前缀)。它是目录题唯一可能的支撑
          ——一份列完了的目录是服务端自己记下的事实。未完整(`open`/`conflict`)
          的链不签发键,模型自拼的同样进不了这个集合。

        换句话说:这个集合**不再只是细粒度证据键**,所以不能再按"含 `:` 或 `src=`
        的键一定是集合身份、一定非法"这类形状判断——合法与否只看它在不在这一份
        服务端算出来的集合里。

        非法键**剔除**(不是拒绝整份载荷:模型抄错一个键不该让它对另外几个方面
        的判断也一起作废),剔完没有支撑的项按 §7.1 不得保持 supported。

        吸收到**非空**自评时顺手清掉 `nudge_pending`:追问已经被回应,那一句不该
        再出现在后续任何一轮(渲染侧也会清,两处同向,见 `render_aspect_block`)。
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
        if updates:
            self.nudge_pending = False
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


def assessment_is_empty(assessment: object) -> bool:
    """这份 `assessment` 有没有对**任何一个**方面表态。

    三种载荷在这里是同一件事:字段缺省(`None`)、`{}`、以及
    `{"supported": [], "unresolved": []}`。协议上它们都说"我这一轮没有新判断",
    而收尾那一轮不允许没有判断(见 `note_missing_assessment`)。

    刻意**不看内容合法性**:一份带了行但越界的载荷由 `apply` 判(它会整份拒并
    折成 `invalid_assessment:<why>`),两条路各说各的——把"给了但不合法"也算成
    "没给",模型会收到一句要它填它其实已经填了的东西的追问。

    非 Mapping(模型回了个列表/字符串)也算空:`apply` 对它返回 `not_object`,
    但那条路只在字段存在时才走到;这里只回答"有没有表态",答案同样是没有。
    """
    if not isinstance(assessment, Mapping):
        return True
    for group in ("supported", "unresolved"):
        rows = assessment.get(group)
        if isinstance(rows, (list, tuple)) and rows:
            return False
    return True


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
    渲染时只折叠(`_fold`)不截长;`gap` 是模型文本,写进账本时就已按模型文本的
    上限截过。

    ⚠ **这个函数有一处写:** 渲染追问句的同时消费掉 `ledger.nudge_pending`
    (一次性,见下面的注释)。生产只有一个调用点、每轮一次(`_reflect_v2_context`),
    所以"渲染 = 已经说给模型听了"在这里是准确的;真要加第二个调用点(诊断、
    预览),那一个必须先想清楚它算不算"说过了"。

    ⚠ **开闸前成本表项(不改行为)**:这个块**每一轮逐字重渲染**并整块进 prompt,
    没有增量。契约上界 ≈ 20KB/轮:16 个方面 × (方面原文 ≤ 冻结契约的单条上限
    + `gap` ≤ 240 字符 + 固定前后缀),再加约束行。它不受 `state_chars` 约束
    (见上),所以这份成本是**确定发生**的,不会被观察账那边的压缩吸收——放量
    评估时按「轮数 × 20KB」计入每 run 的输入 token,与 T3 的证据卡预算并列。
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
            _fold(row.question),
        ]
        if row.gap:
            parts.append(f"缺口: {row.gap}")
        if row.demotion:
            # 服务端把自报 supported 降下来的依据。写在这一行上,模型下一轮才
            # 知道它引的键为什么不算数(而不是以为服务端随手改了它的判断)。
            parts.append(f"服务端降级: {row.demotion}")
        lines.append(" | ".join(parts))
    if ledger.constraints:
        lines.append(
            "约束条件: " + "、".join(_fold(item) for item in ledger.constraints))
    lines.append(ASPECT_BLOCK_NOTE)
    if ledger.nudge_pending:
        # 追问**只挂被退回的下一轮那一次**,渲染完就消费掉。挂在整个 run 的每一
        # 轮上有两个代价:一是这句话回述的是"上一次收尾"这件具体的事,第三轮之后
        # 它就是一句假话;二是模型已经照办之后仍每轮收到同一句斥责,而它下一步该
        # 做的是继续检索,不是再交一遍同一份自评。模型给出非空自评时 `apply` 那边
        # 同样清掉这一格,两处同向。
        lines.append(ASPECT_ASSESSMENT_NUDGE.format(
            ids="、".join(ledger.aspect_ids)))
        ledger.nudge_pending = False
    return "\n".join(lines)


def evidence_bound_keys(
    ledger: Optional[AspectLedger], outline_keys: Sequence[str],
) -> List[str]:
    """证据卡第一档的键序(§6.2)。

    = 「大纲绑定的键」+「各方面已绑定、还没被带上的键(按方面轮转)」。两者都是
    「当前已绑定且存活的证据的代表」,合起来才是第一档。

    **大纲在前、方面补位**:第一档在紧预算下会被截,谁排在后面谁先被挤出去。
    大纲的键是上一轮模型自己写进结构里的绑定,一旦从卡上消失,下一轮它就只能
    在"这条证据还在不在"上瞎猜(而 `update_outline` 的证据键校验按池子算数);
    方面代表则可以从方面块的「已绑定证据 N 条」看到还在。反过来排(方面在前)
    的代价正是紧预算下挤掉上一轮刚绑好的大纲证据。

    T3 的留底规则(`_FRESH_RESERVE_RATIO`)一个字都不变:它切的是第一档**整体**
    的份额,与这一档里怎么排序无关。
    """
    keys: List[str] = []
    seen: Set[str] = set()
    for key in outline_keys:
        key = str(key)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    for key in (ledger.bound_keys() if ledger is not None else ()):
        if key not in seen:
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

#: 结束原因 → **上屏**的中文短句。这份表是界面文案:它经收尾那条 skip 步的
#: `summary` 与合成终步的 `termination_summary` 两处逐字渲染,所以受
#: `docs/ui-vocabulary.md` 的界面词汇表约束(界面只说「方面」,「必答」是契约侧的
#: 记账措辞)。`scripts/check_ui_vocabulary.py` 的第三条扫描通道直接读这张表。
_TERMINATION_SUMMARIES: Mapping[str, str] = {
    TERMINATION_MODEL_SUFFICIENT: "检索结束：每个方面都已找到支撑",
    TERMINATION_MODEL_PARTIAL: "检索结束：仍有方面没有完整支撑",
    TERMINATION_STEP_BUDGET: "检索结束：步骤预算用完",
    TERMINATION_STALE: "检索结束：连续多轮没有新进展",
    TERMINATION_NO_EXECUTABLE_ACTION: "检索结束：除作答外已无可执行的动作",
    TERMINATION_MODEL_DEGRADED: "检索结束：反思调用失败，按降级收尾",
    TERMINATION_RETRIEVAL_DEGRADED: "检索结束：检索通道异常，证据收集未正常完成",
}


def termination_summary(reason: str) -> str:
    return _TERMINATION_SUMMARIES.get(reason, "检索结束")


# --- 合成侧:服务端事实块与最终装配复核(§7.2 后半) --------------------------
#: 结束原因 → **给模型看的**一句英文事实。刻意与 `_TERMINATION_SUMMARIES`
#: (给用户看的中文短句)分开:那一份是 UI 文案,改它是产品用词决定;这一份是
#: 合成 prompt 的输入,改它会改答案。共用一份字符串等于让一次 UI 措辞调整悄悄
#: 变成一次模型行为变更。两份的**键**仍是同一个闭集(测试钉住)。
_TERMINATION_PROMPT_FACTS: Mapping[str, str] = {
    TERMINATION_MODEL_SUFFICIENT:
        "retrieval finished with every mandatory aspect reported supported",
    TERMINATION_MODEL_PARTIAL:
        "retrieval stopped while some mandatory aspects were still open",
    TERMINATION_STEP_BUDGET: "retrieval ran out of steps",
    TERMINATION_STALE: "retrieval stopped after several rounds with no progress",
    TERMINATION_NO_EXECUTABLE_ACTION:
        "retrieval stopped because no tool other than answering was available",
    TERMINATION_MODEL_DEGRADED:
        "the retrieval planner failed and the run ended on a fallback",
    TERMINATION_RETRIEVAL_DEGRADED:
        "a retrieval channel failed and evidence collection did not complete",
}

#: 事实块里最多列几个未解决方面。它挡的是"16 个方面全部未解决"那一版把整段
#: 事实说明撑成一屏——列举本身不是证据,截断只损失枚举、不损失结论(后面那句
#: 「还有 N 项」如实补上数)。
_TERMINATION_BLOCK_MAX_ASPECTS = 6


def render_termination_block(
    termination: Optional[RetrievalTermination], *, directive: bool = True,
) -> str:
    """结束事实 → 合成 prompt 里的一段**服务端事实**(§7.2)。

    `None`(legacy / 关闭态)返回空串,调用方据此逐字节退回接入前的 prompt。

    `directive`:块尾那条祈使句(「在正文里说清哪些点没被覆盖」)要不要跟着这一份
    事实一起给。**按节合成时只有最后一节给**——每节都给会让同一句"请说明缺口"
    在一篇答案里被执行 k 次,读者看到的是每一节末尾各挂一段免责声明,而缺口本来
    只需要在全篇说一次。其余各节仍拿到**完整的事实**(哪一件事没查着、哪条通道
    没恢复),只是不再各自被要求就它写一段;单次合成默认给。

    尾句在**这次检索确实没有缺口可说**时也省掉:`model_sufficient` 且没有未解决
    方面、没有未恢复通道时,「说清哪些点没被覆盖」指向的是一个空集合,而一条指向
    空集合的祈使句只会诱导模型编一个缺口出来交差。

    三条硬边界,都写在块里让模型看得见:

    1. **不是证据**——它不带 `[k]` id,也永远不该被引用。检索为什么停下来是
       服务端的记录,不是笔记本里的内容;允许它被引用等于给答案开一条"引用服务端
       自己"的路。
    2. **不是"这些内容不存在"**——未解决只说明这次检索没找到,后续补取、别的
       问法、别的库都可能有。把它读成否定结论就是用一次检索的边界去否定世界。
    3. **不许改写答案的语气**——模型该做的是在正文里如实说明缺口,不是整段拒答。

    未解决方面带上**问题原文**(用户审阅过的必答清单),因为"哪一件事没查着"正是
    答案要说明的那句话;`gap` 是模型自己写的文本,不进这个块——把上一轮模型的
    自述当服务端事实喂回去,正是 §2 拒绝的"原始 reason 回放"。
    """
    if termination is None:
        return ""
    fact = _TERMINATION_PROMPT_FACTS.get(termination.reason, "")
    if not fact:
        return ""
    by_id = {row.aspect_id: row for row in termination.aspects}
    open_questions = [
        _fold(by_id[aspect_id].question)
        for aspect_id in termination.unresolved_aspect_ids
        if aspect_id in by_id and by_id[aspect_id].question
    ]
    lines = [
        "Retrieval status (server fact about THIS run — it is NOT a knowledge "
        "item, carries no [k] id, and must NEVER be cited):",
        f"- {fact}.",
    ]
    if open_questions:
        shown = open_questions[:_TERMINATION_BLOCK_MAX_ASPECTS]
        more = len(open_questions) - len(shown)
        lines.append(
            "- Questions the retrieval did not resolve: "
            + "; ".join(shown)
            + (f" (and {more} more)" if more > 0 else "")
        )
    if termination.unrecovered_channels:
        lines.append(
            "- Retrieval channels that failed and never recovered in this run: "
            + ", ".join(termination.unrecovered_channels)
        )
    nothing_open = (
        termination.reason == TERMINATION_MODEL_SUFFICIENT
        and not open_questions
        and not termination.unrecovered_channels
    )
    if directive and not nothing_open:
        lines.append(
            "Say plainly in the answer which of those points the evidence below "
            "does not cover, and do not present a partial result as complete. Do "
            "NOT refuse to answer and do NOT treat this note as proof that the "
            "notebook lacks the material — it only states where THIS retrieval "
            "stopped."
        )
    return "\n".join(lines)


def review_aspect_delivery(
    termination: Optional[RetrievalTermination], *,
    admitted_keys: Set[str], cited_keys: Set[str],
) -> AspectDelivery:
    """最终装配之后的方面复核(§7.2)。**纯函数,不读库、不重排、不改状态。**

    `admitted_keys` = 真正进了合成 prompt 的证据身份(Ask/Report 各自从最终
    `id_map` 的 `object_id` 取);`cited_keys` = 最终答案解析回来的锚点身份。两者
    都是**装配之后**才知道的,所以这次复核只可能发生在这里——设计稿明确不要求
    提前重排,也不为复核再读一次库。

    `undelivered` 的判据只有一条:这个方面绑过证据(`evidence_keys` 非空)、状态是
    supported/partial/conflicting,而那些键**一个都没进 prompt**。全被预算/过滤
    挡掉的"已支撑"在屏幕上与真的有支撑长得一样,而它其实没有——降为未送达是
    §7.2 点名要的。只绑上一部分的**不**算未送达:模型看见了其中一条,那条支撑
    真实送达了。

    `conflicting` 与 `partial` 同构地计入:三种状态说的都是「模型看见过这几条
    材料并据此作了判断」,而这次复核问的正是"那几条材料还在不在合成里"。把冲突
    项排除在外,会让「模型看到两条互相矛盾的证据、而它们全被预算挤掉了」这一种
    静默通过——那恰恰是最该报出来的一格:答案里那句"存在分歧"背后已经空无一物。
    `unknown` 结构上没有键可绑,不进这个判据。
    """
    if termination is None:
        return AspectDelivery()
    model_supported: List[str] = []
    admitted: List[str] = []
    cited: List[str] = []
    undelivered: List[str] = []
    for aspect in termination.aspects:
        # 集合身份键(`enum:…`)不进这本账的任何一格。它不是候选池里的一条证据,
        # 所以既不会出现在 `admitted_keys`(那是最终 `id_map` 的 object_id),也
        # 不会出现在 `cited_keys`(答案里的 `[k]` 锚点)——把它留在集合里,一个
        # **只**靠"目录已列全"支撑的方面会被恒判未送达,而那条支撑其实压根不走
        # 证据预算这条路(枚举结果另有自己的合成通道)。只绑集合键的方面因此在
        # 三格里都不出现:如实的"这一格答不了",而不是一个假的"没送达"。
        keys = {
            key for key in aspect.evidence_keys
            if not key.startswith(ASPECT_COLLECTION_KEY_PREFIX)
        }
        if aspect.status == ASPECT_SUPPORTED:
            model_supported.append(aspect.aspect_id)
        if keys & admitted_keys:
            admitted.append(aspect.aspect_id)
        elif keys and aspect.status in (
                ASPECT_SUPPORTED, ASPECT_PARTIAL, ASPECT_CONFLICTING):
            undelivered.append(aspect.aspect_id)
        if keys & cited_keys:
            cited.append(aspect.aspect_id)
    return AspectDelivery(
        model_supported=tuple(model_supported),
        synthesis_admitted=tuple(admitted),
        answer_cited=tuple(cited),
        undelivered=tuple(undelivered),
    )


def termination_synthesis_detail(
    termination: Optional[RetrievalTermination], *,
    admitted_keys: Set[str], cited_keys: Set[str],
) -> dict:
    """合成终步 trace detail 的 v2-only 稀疏键(§7.2)。

    `None` 返回空 dict,调用方 `update` 一个空 dict ⇒ 关闭态的 detail 键集逐字节
    不变。三个口径各占一个键,谁都不许替谁作证:`aspects_model_supported` 是模型
    说的,`aspects_synthesis_admitted` 是真的进了 prompt 的,`aspects_answer_cited`
    是答案真的引了的。合起来读才知道"已支撑"这三个字在这一轮到底成色如何。
    """
    if termination is None:
        return {}
    delivery = review_aspect_delivery(
        termination, admitted_keys=admitted_keys, cited_keys=cited_keys)
    return {
        "termination_reason": termination.reason,
        # 中文短句由服务端给出:前端只渲染字段,不自造一份会与闭集分叉的映射。
        "termination_summary": termination_summary(termination.reason),
        "aspects_total": len(termination.aspects),
        "aspects_pending": len(termination.unresolved_aspect_ids),
        "aspects_model_supported": len(delivery.model_supported),
        "aspects_synthesis_admitted": len(delivery.synthesis_admitted),
        "aspects_answer_cited": len(delivery.answer_cited),
        "aspects_undelivered": len(delivery.undelivered),
        "unrecovered_channels": list(termination.unrecovered_channels),
    }


def admitted_evidence_keys(
    id_map: Mapping[str, object],
    cluster_fold: Optional[Mapping[str, str]] = None,
) -> Set[str]:
    """最终 `id_map` → 真正进了 prompt 的证据身份集合。

    `id_map` 的值是各 `*_context` builder 建的 evidence 字典,`object_id` 是它们
    共同的身份键(KG 对象 id / chunk_id / element_id),与方面账里的
    `evidence_keys` 同一个口径(两者都源自 `outline_binding_keys`)。**按 id_map 而
    不是按候选池算**:候选进没进 prompt 由预算截断决定,而这次复核问的正是"被截
    掉了没有"。

    `cluster_fold` = 「被折叠掉的成员 object_id → 代表 object_id」,由
    `EvidenceContextService.knowledge_context` 的 `fold_sink` 在装配时**顺手**记下
    (零新增查询:折叠表本来就要算,这里只是把已经算出的对应关系带出来)。KG 证据
    按 canonical 簇去重,进 prompt 的是代表那条命中的 `object_id`;被折叠掉的成员
    的内容**确实送达了模型**(它们与代表同属一个簇),只是身份换成了代表的那个。
    不折进来,一个绑在成员 id 上的方面会被误报「未送达」——多参考库场景下同一个
    概念在各库各有一份对象 id,这不是边角情况。代表自己没能进 prompt(被范围闸、
    `node_context` 缺失或预算挡下)时,它不在 `id_map` 里,成员因此也不会被算作
    送达——折叠表在这里天然是保守的。

    ⚠ **深度报告一侧眼下不传这张表**,那条路径因此是**保守口径:可能多报未送达**。
    报告的 KG 装配走 `report_engine.knowledge_context_with_outline`,给它加一个
    sink 参数会改端口签名,而那正是几条既有用例的测试替身逐字钉住的形状;补齐它
    要连同那些替身一起同步,是独立的一次改动(登记在 `fangan_todo.md`)。保守的
    方向是对的:多报"没送达"只会让服务端把一个其实送到了的方面记成未送达,不会
    反过来把真的没送达的说成送到了。
    """
    keys: Set[str] = set()
    for value in id_map.values():
        if isinstance(value, Mapping):
            key = str(value.get("object_id") or "")
            if key:
                keys.add(key)
    for member, representative in (cluster_fold or {}).items():
        if representative in keys:
            keys.add(str(member))
    return keys


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


def _observation_status(row: object) -> str:
    """一条观察的状态,零 I/O 的那几档折成空串(它们不是一次执行)。"""
    status = str(getattr(row, "status", "") or "")
    if status == STATUS_FAILED or status in _EXECUTED_STATUSES:
        return status
    return ""


def _is_retrieval_action(action_id: str) -> bool:
    """这个动作 id 会不会产生新证据(§7.2 的「真实检索执行」判据)。

    `update_outline` / `consult_memory` 不做检索 I/O(`ACTION_DEFINITIONS` 里
    `produces_evidence=False`,与 prompt/schema/白名单同一份定义,不新造第二份
    口径)——它们的 success 观察不该被当成"这次 run 后来又查成了"。缺一份定义
    的 action_id(理论上不会发生,`ActionObservationLedger` 只在 v2 下产生,而
    v2 的动作全部登记在 `ACTION_DEFINITIONS` 里)保守地当作非检索动作,不让一个
    拼错的 id 冒充恢复证据。
    """
    definition = ACTION_DEFINITIONS.get(action_id)
    return bool(definition is not None and definition.produces_evidence)


def _retrieval_degraded(observations: Sequence[object]) -> bool:
    """这次 run 的**最后一次真实检索 I/O 执行**是不是炸的(§7.2)。

    判据按**时间线**取最后一次执行,而不是按通道取"有没有哪条通道最后一次是
    炸的":§7.2 写的是「单个可恢复工具失败若随后继续完成检索,只作为 observation
    留存,不强制把整个 run 标为 retrieval_degraded」——**继续完成检索**说的是这次
    run 后来还是查成了,没有限定必须是同一条通道。按通道判会把「KG 播种炸了 →
    换 `search_chunks` 查全 → 模型自报充分」误标成整次降级,而那次 run 的证据
    收集其实正常完成了。

    扫描先按 `_is_retrieval_action` 过滤掉 `update_outline`/`consult_memory`
    这类不产证据的动作:它们不做检索 I/O,一次大纲更新/记忆咨询的成功观察不能
    冒充"最后一次真的又查成了",否则检索失败之后紧跟一次大纲更新耗尽预算收尾,
    会把 `retrieval_degraded` 悄悄吃掉、误报成 `stale`/`step_budget`。

    没被恢复的那条通道不因此消失:它另走 `_unrecovered_channels`,进
    `RetrievalTermination.unrecovered_channels` 供披露,不占 `reason`。两个口径
    分开,正是为了不让"哪条路没走通"与"这次检索有没有正常收尾"互相冒充。

    `empty` 算一次执行:通道是通的、这个问法在库里真的没有内容,与"没查成"严格
    区分(这也是 `note_failed` 侧信道存在的全部理由)。
    """
    for row in reversed(list(observations)):
        if not _is_retrieval_action(str(getattr(row, "action_id", "") or "")):
            continue
        status = _observation_status(row)
        if status:
            return status == STATUS_FAILED
    return False


def _unrecovered_channels(observations: Sequence[object]) -> Tuple[str, ...]:
    """最后一次执行仍是 `failed` 的那些检索通道(action_id),**按首次出现顺序**。

    纯披露:`reason` 不读它。一条通道在这里出现,只说明"这条路这次 run 没走通",
    不说明这次检索失败了——`search_elements` 全程炸掉、而 `search_chunks` 查回了
    答案所需的全部原文,是一次正常完成的 run 加一条要如实说出去的通道故障。

    与 `_retrieval_degraded` 同一份 `_is_retrieval_action` 过滤:`update_outline`
    / `consult_memory` 不产证据,不算检索通道,不该出现在这份披露清单里。
    """
    last: dict = {}
    for row in observations:
        action_id = str(getattr(row, "action_id", "") or "")
        if not _is_retrieval_action(action_id):
            continue
        status = _observation_status(row)
        if status:
            last[action_id] = status
    return tuple(
        action_id for action_id, status in last.items()
        if status == STATUS_FAILED)


def classify_termination(
    trace: Sequence[object], observations: Sequence[object],
    ledger: AspectLedger,
) -> RetrievalTermination:
    """服务端在 run 收尾生成的结束事实(§7.2)。

    优先级:异常降级 → 服务端能力收尾/熔断 → 模型的正常结束 → 预算耗尽。

    * `model_degraded` 排第一:那一轮根本没有模型决定可言,把它读成任何一种
      "模型说的"都是谎报。
    * `retrieval_degraded` 排第二,但**只在模型没有正常结束这次 run 时**成立:
      判据是「没有 model_end 标记 且 最后一次真实 I/O 执行是 failed」。模型走到
      answer/sufficient 那一步意味着它看着已经到手的证据决定停下,这时把 run
      标成"检索通道异常收尾"是把一次恢复了的故障说成整次失败(§7.2 的「单个
      可恢复工具失败若随后继续完成检索…不强制标 degraded」)。没被恢复的通道
      照样如实披露,走 `unrecovered_channels`,不占 `reason`。
    * 反过来,最后一次执行炸掉之后走到 stale/预算收尾的,`retrieval_degraded`
      仍然盖过 `stale`/`step_budget`:那两个原因说"没进展/没步数了",而真正
      发生的是"最后一次去查的时候查不动了"。
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
    elif kind != "model_end" and _retrieval_degraded(observations):
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
        unrecovered_channels=_unrecovered_channels(observations),
        aspects=ledger.snapshot(),
    )
