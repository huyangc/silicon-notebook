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
    Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple,
)

from app.domain.retrieval_termination import (
    ASPECT_COLLECTION_KEY_PREFIX,
    ASPECT_CONFLICTING, ASPECT_PARTIAL, ASPECT_REJECTION_REASONS,
    ASPECT_SUPPORTED, ASPECT_UNKNOWN,
    ASPECT_UNRESOLVED_STATUSES, AspectDelivery, AspectSnapshot,
    DEMOTION_KEYS_MISSING, DEMOTION_KEYS_REJECTED,
    REFLECT_ASPECT_GAP_MAX_CHARS, REFLECT_ASPECT_GROUP_ROWS_FACTOR,
    REFLECT_ASPECT_GROUP_ROWS_HARD_MAX, REFLECT_ASPECT_MAX_EVIDENCE_KEYS,
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
#: 同一份账在 `prefix_snapshot` 布局下拆成的两半的标题(前缀复用设计 §4.3/§4.5)。
#: 契约半住在 C(问题之后、run 内逐字节不变),状态半住在 T(user 段末尾、每轮
#: 重渲染)。标题各自成句而不是共用 `ASPECT_BLOCK_TITLE`:两块在同一条消息里
#: 相距很远,共用一个标题读起来像同一份清单被贴了两遍。
ASPECT_CONTRACT_BLOCK_TITLE = "【必答方面 — 用户确认过的必答清单，原文与约束】"
ASPECT_STATUS_BLOCK_TITLE = "【必答方面的服务端记账 — 本轮当前状态】"
ASPECT_BLOCK_NOTE = (
    "（上面的状态是此前某一轮模型自己的判断，不是服务端对语义支撑的证明；"
    "本轮请在同一份 JSON 的 assessment 里重新给出，省略的方面保留现状。）"
)
#: 同一句话的 `prefix_delta_lean`(L)版本,由 `render_aspect_status_block` 的
#: `lean` 闸**二选一**替换上面那一份(不是追加)。
#:
#: 只差后半句,而那后半句正是 L 这条臂唯一改掉的东西:上面那一份要求模型「本轮
#: 重新给出」全量自评,而 L 的系统段自评合同
#: (`prompts._V2_LEAN_ASSESSMENT_INSTRUCTION`)明确说「省略的方面保留服务端已经
#: 记着的状态、已支撑项不必重述、省略不花代价也不会被追问」。两句同时在场是最坏
#: 形态——同一份 prompt 里一句要全量、一句要增量,模型只能猜哪一句算数,而 A/B
#: 表会把由此产生的行为差异记到「布局」头上。所以两处都是替换,不是叠加。
#:
#: 前半句一个字不改:那句话说的是「这些状态的来源是模型自己,不是服务端的证明」
#: ——L 下它比 `off` 更要紧,因为 L 里这个块是方面账**唯一**的完整落点,模型不再
#: 每轮重述一遍。
ASPECT_BLOCK_NOTE_LEAN = (
    "（上面的状态是此前某一轮模型自己的判断，不是服务端对语义支撑的证明；"
    "本轮只需在 assessment 里给出有变化的方面，省略的保留现状"
    "（不必重述已支撑项）。）"
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
    #: 这个方面**最近一次**被服务端逐条拒绝的稳定原因码(闭集
    #: `ASPECT_REJECTION_REASONS`)。**consume-on-render**:两个布局各自那一个渲染
    #: 点(`off` 的 `render_aspect_block`、`prefix_snapshot` 的
    #: `render_aspect_status_block`,`_reflect_v2_context` 按布局闸二选一、互斥)
    #: 渲染出「服务端未采纳」那一格的同时清掉它,与
    #: `nudge_pending` 同款一次性语义。挂成一次性而不是常驻状态,是因为它讲的是
    #: 「你上一轮那条自评没被采纳」这件具体的事——每轮重复同一句斥责,模型读到的
    #: 是一句与它这一轮做了什么无关的话,而它这一轮很可能已经改对了。
    #:
    #: 它**不进** `snapshot()`:快照是终态事实(这个方面最后什么状态、绑了哪些
    #: 证据),而这一格是渲染用的一次性提示,写进跨层 DTO 只会让下游多一个它不
    #: 该解释的字段。
    rejected: str = ""

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

    @property
    def content(self) -> Tuple[str, Tuple[str, ...], str, str]:
        """**落账之后账本会长的样子**,不含方面身份。

        这是「完全相同的重复项」的判据(§6):同一个 aspect_id 的两行,`content`
        相等 ⇒ 无论采纳哪一条,账本落定后逐字段相同,所以确定性去重根本不需要
        "取第一条还是最后一条"这个决定——那正是设计稿点名不许随手做的选择。
        判据落在**规范化之后**(证据键已剔非法、已保序去重,`gap` 已按上限截,
        `status` 已按降级规则调整),所以两行只在"抄错了哪一个池外的键"上不同、
        落账结果完全一样时,仍然算同一条;真的会让账本落成两种样子的那种重复,
        `content` 必然不等,走冲突那条路。
        """
        return (self.status, self.keys, self.gap, self.demotion)


@dataclass(frozen=True)
class AssessmentOutcome:
    """`AspectLedger.apply` 的结果:**整份**判读 + **逐方面**判读(§6)。

    两格刻意分开,因为它们导致的处理完全不同:

    * `error` 非空 ⇒ **整份载荷形状不成立**(`not_object` /
      `<group>_not_list` / `item_not_object` / `<group>_overflow`),账本一个字
      都没改,调用方照旧把这一轮折成一条零 I/O 的 invalid 观察。这一格非空时
      `accepted`/`rejections` 恒为空:整份被拒就是整份被拒,不留半份副作用。
    * `rejections` 非空 ⇒ 某几个方面的更新**按方面**被拒(闭集
      `ASPECT_REJECTION_REASONS`),其余合法方面照常落账,**同一轮的检索动作
      照常执行**。调用方据它记披露与 skip 步,不折叠这一轮。

    `accepted` 是真正落账的方面 id(保序)。它**空着**是一件有后果的事:一份行
    数不为零、却逐方面全被拒的载荷,在账本上留下的读数与一份根本没给的自评逐字
    相同,所以收尾轮的「必须自评」闸按它判,而不是按"有没有行"判(见
    `reasoning_retrieval._absorb_assessment` / `_nudge_missing_assessment`)。
    """

    error: str = ""
    accepted: Tuple[str, ...] = ()
    #: `(aspect_id, why)`。`aspect_id` 只可能是**本账本自己的** id;模型写了一个
    #: 不在清单里的 id 时这一格是空串(`why == "unknown_aspect"` 已经说清了是
    #: 哪回事),而那串来自模型的自由文本从不被带进 trace/披露——一个可以由模型
    #: 决定字面的 id 出现在服务端记录里,只会给伪造留一条路。
    rejections: Tuple[Tuple[str, str], ...] = ()


#: `normalize_assessment_payload` 认的 (a) 设计稿列表形的顶层键。方面 id 由
#: `_ASPECT_ID_PREFIX` 确定性生成(`a1..aN`),永远不会撞上这两个字面量,所以
#: 「出现了其中任意一个」这条判据不会把一份真正的 (b) 映射形误判成列表形。
#:
#: 判据是**交集**而不是「子集」:`apply()` 归一前只遍历这两组、其余顶层键一概
#: 不看,所以一份带了额外顶层键的列表形(`{"supported": [...], "note": "…"}`
#: ——模型顺手加一句说明是常见写法)在归一之前本来就能被正常吸收。用子集判据
#: 会把它推进映射形分支,于是 `supported` 那个**列表**被当成某个方面 id 的
#: 判断体、`isinstance(body, Mapping)` 为假 ⇒ 整份载荷变成两条 `unknown`,
#: 模型明确说了"已支撑"的方面反而被记成没表态。归一只该拓宽能被读懂的形状,
#: 不该让原本读得懂的一份变得读不懂。
_ASSESSMENT_LISTFORM_KEYS = frozenset({"supported", "unresolved"})


def normalize_assessment_payload(raw: object) -> object:
    """把 `assessment` 的形状别名归一成 `apply()` 认识的列表形。

    2026-09-09 本机 deepseek-v4-flash 关思考实测:55 次收尾自评里 41 次没有按
    设计稿写 `{"supported": [...], "unresolved": [...]}`,而是按方面 id 直接
    映射:`{"a1": {"status": "partial", "supported": false, "evidence_keys": [],
    "gap": "..."}}`(`supported` 有时是布尔、有时缺省,`status` 同理)。这份映射
    被 `apply()` 当成两组都没给的空载荷——不是"模型不填",是**形状不合**。

    只做**形状**归一,不碰语义:三种输入都归一到同一份列表形之后,`apply()` 既有
    的键合法性、上限、重复校验原样生效,越权/超限仍然被拒。非 dict 输入原样
    返回,交给调用方(`apply()` 的 `not_object` 分支、`assessment_is_empty()`
    的非 Mapping 分支)处理——那不是这个函数的判断范围。

    认三种形状:

    (a) 设计稿列表形:顶层出现 `supported`/`unresolved` 任一键就按这种形状处理
        (陌生顶层键原样忽略,与归一前 `apply()` 只遍历两组的语义一致),两组
        原样通过(仍会走 (c) 的 `id` 别名归一)。
    (b) 按方面 id 的映射:顶层**不含** `supported`/`unresolved` 任一键时按这种
        形状处理——每个键是一个方面 id,值是该方面这一轮的判断。判定见
        `_normalize_mapping_form`(两格冲突时取保守的那一边)。
    (c) 列表形的 item 带 `id` 而非 `aspect_id`:两个键都认,`id` 只在缺
        `aspect_id` 时补位。
    """
    if not isinstance(raw, Mapping):
        return raw
    if _ASSESSMENT_LISTFORM_KEYS & set(raw.keys()):
        return _normalize_listform_ids(raw)
    return _normalize_mapping_form(raw)


def _normalize_listform_ids(raw: Mapping) -> dict:
    """(c):列表形 item 里的 `id` 别名补成 `aspect_id`。不改其它任何东西。"""
    out: dict = {}
    for group in ("supported", "unresolved"):
        rows = raw.get(group)
        if rows is None:
            continue
        if not isinstance(rows, (list, tuple)):
            # 非列表原样传递,交给 `apply()` 既有的 `<group>_not_list` 校验。
            out[group] = rows
            continue
        out[group] = [
            {**row, "aspect_id": row["id"]}
            if (isinstance(row, Mapping) and "aspect_id" not in row
                and isinstance(row.get("id"), str))
            else row
            for row in rows
        ]
    return out


def _normalize_mapping_form(raw: Mapping) -> dict:
    """(b):按方面 id 的映射 → `apply()` 认识的列表形。

    `supported`(布尔)与 `status`(字面量)是同一件事的两种写法,模型两格都给
    而且**互相矛盾**时取**保守的那一边**——归一是形状转换,不该在两个读数之间
    替模型挑更乐观的那个:

    * `supported is True` 且 `status` 缺省或就是 `supported` ⇒ 进 `supported`;
    * `status` 明确给了别的值(`partial`/`conflicting`/`unknown`,或任何不合法
      的字面量)⇒ **以 status 为准**进 `unresolved`(不合法的归一成 `unknown`,
      `gap` 照常透传)。模型写下一个具体的未解决档位,比它顺手带上的
      `supported: true` 信息量大得多,而两者只能有一个成立;
    * `status == "supported"` 但 `supported is False` ⇒ 同理进 `unresolved` 的
      `unknown`:一份自相矛盾的载荷不足以支撑「这个方面已经有支撑」这个结论,
      而 `unknown` 正是"服务端没拿到可用判断"的那一档。

    保守的代价是一个真的已支撑的方面晚一轮拿到 supported(模型下一轮可以重报);
    取宽的代价是服务端据一份矛盾载荷宣布支撑,而它正是 §7.1 要防的那件事。
    """
    supported: List[dict] = []
    unresolved: List[dict] = []
    for aspect_id, body in raw.items():
        if not isinstance(aspect_id, str):
            # 方面 id 永远是字符串;一个非字符串键不可能指向任何真实方面,丢弃它
            # 比伪造一条 `apply()` 认不出的 id 更安全。
            continue
        fields = body if isinstance(body, Mapping) else {}
        status = fields.get("status")
        status = status if isinstance(status, str) else ""
        item: dict = {"aspect_id": aspect_id}
        evidence_keys = fields.get("evidence_keys")
        if evidence_keys is not None:
            item["evidence_keys"] = evidence_keys
        gap = fields.get("gap")
        if gap is not None:
            item["gap"] = gap
        if status == ASPECT_SUPPORTED:
            is_supported = fields.get("supported") is not False
        else:
            is_supported = not status and fields.get("supported") is True
        if is_supported:
            supported.append(item)
        else:
            item["status"] = (
                status if status in ASPECT_UNRESOLVED_STATUSES
                else ASPECT_UNKNOWN)
            unresolved.append(item)
    out: dict = {}
    if supported:
        out["supported"] = supported
    if unresolved:
        out["unresolved"] = unresolved
    return out


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
        "nudge_answered", "unknown_aspect_rejections", "_lean_assessment",
        "_assessment_enabled",
    )

    def __init__(
        self, questions: Sequence[str], *, source: str,
        constraints: Sequence[str] = (), lean_assessment: bool = False,
        assessment_enabled: bool = True,
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
        #: 追问句「还欠着」——被退回之后**只渲染一次**(两个布局各自一个渲染点、
        #: 互斥:`render_aspect_block` / `render_aspect_status_block`)。
        #: 与 `assessment_prompts` 分开:那一格是"一共退回过几次"的计数(判据是
        #: 它),这一格是"这一句现在还该不该出现"的一次性开关。
        self.nudge_pending: bool = False
        #: 最近这一次追问**已经被回应**(自上次发出追问以来吸收到过非空自评)。
        #: 只服务 `restore_pending_nudge`:一次降级轮要重新置位追问之前,得先分清
        #: 「那一句还欠着」与「模型早就照办了、这一轮只是失败了」。判据必须是
        #: "自上次追问以来",不是"这次 run 里有没有给过自评"——后者会让一次早期
        #: 自评永久豁免掉后面所有的追问。
        self.nudge_answered: bool = False
        #: 上一轮有几条自评因为**方面 id 不在这份清单里**被拒。它挂不到任何一条
        #: 记录上(那个 id 本来就不存在),所以单独计数,由两个布局各自那一个渲染
        #: 点(`render_aspect_block` / `render_aspect_status_block`,互斥)渲染成
        #: 一行并同时清零(consume-on-render,与
        #: `_AspectRecord.rejected` 同款)。只记条数、不记那个 id:它是模型的自由
        #: 文本,合法 id 就在同一个块里逐行列着,回显一遍不增加任何信息。
        self.unknown_aspect_rejections: int = 0
        #: 这次 run 走的是 `prefix_delta_lean`(L)的**轻量自评合同**:模型每轮只
        #: 报变化,收尾轮省略自评不再被退回追问(前缀复用设计 §6,PR-4 计划拍板
        #: Q1)。**run 级、建账时冻结**——这是**结构性**的,不只是约定:私有槽
        #: `_lean_assessment` 只在这里(`__init__`)被写一次,`lean_assessment`
        #: 对外只读(下面的只读 property,没有 setter),中途翻位会被 Python 当场
        #: 拒成 `AttributeError`,不是"没人这么写"的君子协定。中途翻位会让这次
        #: run 的自评合同前后不一致(前半程被追问过、后半程不追问),而那条臂在
        #: A/B 表上仍标着一个名字,于是那张表上的差异归因是假的(拍板 Q2)。
        #:
        #: 它只被 `note_missing_assessment` 一处读到(那是追问链唯一的闸),另外
        #: 由 `classify_termination` 原样带上终态 DTO 供合成侧披露。默认 `False`
        #: 让 `off`/`prefix_snapshot`/`prefix_delta` 三臂逐字节落在既有语义上。
        self._lean_assessment: bool = bool(lean_assessment)
        self._assessment_enabled = bool(assessment_enabled)

    # --- 读 ---------------------------------------------------------------
    @property
    def assessment_enabled(self) -> bool:
        """Frozen per run; disabling assessment retains the user contract."""
        return self._assessment_enabled

    @property
    def lean_assessment(self) -> bool:
        """`prefix_delta_lean`(L)那条臂的轻量自评合同开关,**只读**。

        唯一写点是 `__init__`(见 `_lean_assessment` 那格的注释)——没有 setter,
        `ledger.lean_assessment = ...` 结构性地拒成 `AttributeError`,建账之后
        这次 run 的自评合同不可能中途翻位。
        """
        return self._lean_assessment

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

        ``lean_assessment``(L 那条臂)= **这次 run 的自评合同本来就不要求收尾轮
        补齐账目**(前缀复用设计 §6:「不为了补齐账目再专门退回一轮」)。系统段
        已经把这件事写给模型看了,所以省略不是不合作,而是照合同办事;退回一轮去
        追问一份合同里明说可以省的东西,买回来的只有那一轮的钱(实测约 40s)。
        这条闸排在最前面,`may_prompt` 与追问额度都不再看——L 下这两格从头到尾
        都是初值。

        **三格分工**(拍板 Q5,别让它们互相冒充):

        * ``_AspectRecord.model_assessed`` / ``AspectSnapshot.model_assessed``
          为 False = 「这一格**没有**模型的判断」。它不解释为什么没有。
        * ``assessment_omitted``(run 级与 per-row 两格)= 「服务端**问过之后**它
          仍然不给」。判据里含着一次真实发生过的追问,所以 **L 下永不置位**——
          那条臂一次都没问过,把没问过记成"问过了它不给"会让放量评估把一次照章
          省略读成一次协议不合作。这也是为什么 L 不走下面 `may_prompt=False`
          那两行:那两行的全部内容就是写这一格。
        * ``RetrievalTermination.lean_assessment`` = 「这条臂**本来就不问**」。
          run 级事实挂 run 级,合成侧据它决定要不要披露「尚未逐项核验」
          (`render_termination_block`),而不是从 per-aspect 的沉默里反推。
        """
        if not self.assessment_enabled or self.lean_assessment:
            return False
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
        self.nudge_answered = False
        return True

    def restore_pending_nudge(self) -> None:
        """追问那一句没送达就不算被消费,重新置位(§5.2 的降级轮)。

        两个布局各自那一个渲染点(`render_aspect_block` / `render_aspect_status_block`)
        都把「渲染 = 已经说给模型听了」当成消费点,而一轮
        降级说的正是**那一次模型调用没有成交**:prompt 渲染出来了,回来的却是
        provider 故障后的兜底,模型没读到这一句、更没机会照办。不重新置位的话,
        服务端花了一整轮把收尾退回去、追问额度也扣掉了,换回来的是一句谁都没
        看见的话。

        两道闸,免得这一格变成"每轮都挂着的斥责":从来没发出过追问
        (`assessment_prompts == 0`)不置位;上一次追问已经被回应
        (`nudge_answered`,`apply` 吸收到非空自评时置上)也不置位——那时该做的是
        继续检索,不是再交一遍同一份自评。
        """
        if self.assessment_prompts and not self.nudge_answered:
            self.nudge_pending = True

    def consume_rejection_notes(self) -> Tuple[Dict[str, str], int]:
        """「上一轮哪几条自评没被采纳」→ `({aspect_id: why}, 未知 id 条数)`。

        **一次性**:读完就清,与 `nudge_pending` 同款语义(见
        `_AspectRecord.rejected`)——"渲染 = 已经说给模型听了"。分两格返回是因为
        两者挂的位置不同:前者是某个方面那一行上的一格,后者挂不到任何一行上,
        只能单独说一句。

        **调用点是两个布局各自那一个渲染点,一共两处、互斥**:`off` 的
        `render_aspect_block` 与 `prefix_snapshot` 的 `render_aspect_status_block`,
        由 `_reflect_v2_context` 按布局闸二选一,所以每轮仍然恰好消费一次。要找
        全部消费点就是这两个——别只按 `render_aspect_block` 找,那样会漏掉 P 那
        一半(T-PS8 之前这里确实只有一个调用点)。

        ⚠ **降级轮不重新置位**(知情取舍,与 `restore_pending_nudge` 刻意不同)。
        那一格要恢复,是因为服务端**退回了一整轮**去换一份自评:成本真的付出去
        了,而追问句谁都没看见。这一格只是一句解释,渲染它的那一轮若因 provider
        故障没成交,代价是模型可能把同一个错误的 id 再写一次——一条自评,不是
        一轮检索。为它多存一份"消费前的快照"再挂一条恢复路径,收益撑不起那份
        状态。
        """
        notes = {
            row.aspect_id: row.rejected for row in self._records if row.rejected
        }
        for row in self._records:
            row.rejected = ""
        unknown, self.unknown_aspect_rejections = (
            self.unknown_aspect_rejections, 0)
        return notes, unknown

    # --- 写(唯一入口) ----------------------------------------------------
    def apply(
        self, assessment: object, *, allowed_keys: Set[str],
    ) -> AssessmentOutcome:
        """把一份 `assessment` 落进账本。返回 `AssessmentOutcome`(见那里)。

        校验分**两层**(设计稿 §6「动作与 assessment 独立校验」):

        * **整份形状**不成立(不是对象、某一组不是列表、某一条不是对象、某一组
          的行数超过 `_group_row_cap` 那道防超大载荷的硬上限)⇒ 整份拒绝,账本
          一个字都不改。这一类载荷里"模型到底怎么判的"没有可明确解释的读法,
          半份被吸收的评估比没有评估更危险——模型下一轮看到的状态既不是它说的,
          也不是服务端算的。
        * **单个方面**不成立(闭集 `ASPECT_REJECTION_REASONS`:未知方面 id、
          同一方面的冲突重复项、这个方面的证据键数或 `gap` 超限、这一行的
          `evidence_keys`/`gap`/`status` 字段类型或取值不合协议)⇒ **只拒这一个
          方面**,它保留旧状态并在下一轮的方面块里披露;同一份载荷里其它合法的
          方面照常落账,而**这一轮的检索动作照常执行**(调用方不再折叠整轮)。
          一次自评笔误不该吞掉一次已经通过全部参数校验的检索。

        ⚠ **组内条数不再与方面总数比**(T-BF7 评审 P1)。那条判据原来落在
        `_plan_row` 之前,于是下面那句"完全相同的重复项确定性去重"在结构上根本
        走不到——重复项天然多占一行。规划之后再看已经没有比的必要:`planned` 按
        `aspect_id` 归并、id 必须是本账本的,落账候选数恒不超过方面总数。

        同一个方面的**完全相同**重复项确定性去重(判据见 `_Update.content`),
        **冲突**的重复项按 `duplicate_aspect` 拒掉这个方面并保留旧状态——不取
        首条也不取末条,那等于替模型决定它到底怎么判的。

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
        再出现在后续任何一轮(渲染侧也会清,两处同向,见 `render_aspect_block` /
        `render_aspect_status_block`)。

        **入口先经 `normalize_assessment_payload` 做形状归一**:按方面 id 的
        映射写法(2026-09-09 本机实测里占多数)与设计稿的列表写法在这里被当成
        同一件事,下面的校验只认归一之后的形状。
        """
        if not self.assessment_enabled:
            return AssessmentOutcome()
        assessment = normalize_assessment_payload(assessment)
        if not isinstance(assessment, Mapping):
            return AssessmentOutcome(error="not_object")
        planned: "Dict[str, _Update]" = {}
        rejected: "Dict[str, str]" = {}
        unknown = 0
        for group, statuses in (
            ("supported", ()), ("unresolved", ASPECT_UNRESOLVED_STATUSES),
        ):
            rows = assessment.get(group)
            if rows is None:
                continue
            if not isinstance(rows, (list, tuple)):
                return AssessmentOutcome(error=f"{group}_not_list")
            if len(rows) > _group_row_cap(len(self._records)):
                # **只防超大载荷**,不再拿行数与方面数比(评审 P1)。理由见
                # `_group_row_cap`。
                return AssessmentOutcome(error=f"{group}_overflow")
            for row in rows:
                aspect_id, update, why = self._plan_row(
                    row, statuses, allowed_keys)
                if why and why not in ASPECT_REJECTION_REASONS:
                    # 整份形状错误:**在写任何一格之前**返回,所以这条路上的
                    # 账本、披露槽位、追问状态全都一个字没动(半份副作用正是
                    # 这一族要防的东西)。
                    return AssessmentOutcome(error=why)
                if why and not aspect_id:
                    # 未知方面 id。挂不到任何一条记录上,所以按**条**计数而不是
                    # 按 id 归并——两条自评各写了一个清单外的 id 是两件事,而那
                    # 两个字符串是模型的自由文本,服务端一个都不留。
                    unknown += 1
                    continue
                if why:
                    _mark_rejected(rejected, planned, aspect_id, why)
                    continue
                if aspect_id in rejected:
                    # 这个方面已经被拒(冲突重复或它自己越界),后续同 id 的行
                    # 一律不再落账:一个已经说不清怎么判的方面,不该由排在后面
                    # 的某一行替它定下来。
                    continue
                prior = planned.get(aspect_id)
                if prior is not None and prior.content != update.content:
                    _mark_rejected(
                        rejected, planned, aspect_id, "duplicate_aspect")
                    continue
                if prior is None:
                    planned[aspect_id] = update
                # `prior.content == update.content` ⇒ 完全相同的重复项,保留已
                # 有的那一条(两条落账结果逐字段相同,不是"取首条"这个选择)。
        return self._commit(planned, rejected, unknown)

    def _commit(
        self, planned: "Dict[str, _Update]", rejected: "Dict[str, str]",
        unknown: int,
    ) -> AssessmentOutcome:
        """把规划结果一次性写进账本。**整份形状错误永远走不到这里。**

        ⚠ 判据是 `planned`,**不是**"这一轮有没有行"。追问那两格问的是「模型有没有
        真的交上一份自评」,而一份逐方面全被拒的载荷一格都没落账——它与沉默在账本
        上的读数逐字相同。改成 `if planned or rejected or unknown` 会让一次全拒的
        载荷把 `nudge_pending` 清掉(那一句本该在下一轮渲染给模型),并把
        `nudge_answered` 置上——于是之后一轮降级时 `restore_pending_nudge` 认定
        「模型早就照办了」,不再重新置位一句谁都没看见的追问。收尾那道闸另有判据
        (`_absorb_assessment` 传下去的 `accepted`),两处同向、各挡一半。
        """
        if planned:
            self.nudge_pending = False
            self.nudge_answered = True
        for update in planned.values():
            record = update.record
            record.status = update.status
            record.evidence_keys = update.keys
            record.gap = update.gap
            record.model_assessed = True
            record.demotion = update.demotion
        for aspect_id, why in rejected.items():
            self._by_id[aspect_id].rejected = why
        self.unknown_aspect_rejections += unknown
        return AssessmentOutcome(
            accepted=tuple(planned),
            rejections=tuple(rejected.items())
            + (("", "unknown_aspect"),) * unknown,
        )

    def _plan_row(
        self, row: object, statuses: Tuple[str, ...], allowed_keys: Set[str],
    ) -> Tuple[str, Optional[_Update], str]:
        """一行自评 → `(aspect_id, 待落账的更新, 原因码)`。**不写任何状态。**

        原因码非空时更新为 `None`;它属于 `ASPECT_REJECTION_REASONS` ⇒ 只拒这
        一个方面,否则 ⇒ 整份载荷不成立。`aspect_id` 只在它真是本账本的 id 时
        非空(未知 id 那一格恒为空串,理由见 `AssessmentOutcome.rejections`)。

        ⚠ **读出 `aspect_id` 之后的每一条错误都是逐方面的**(T-BF7 评审 F3)。
        字段类型/取值不合协议(`evidence_keys` 不是列表、键不是字符串、`gap` 不是
        字符串、`status` 不在闭集)讲的都是**这一行**的事,而这一行归属哪个方面
        已经确定;把它们留在整份那一族,等于让一个写错了 `status` 的方面继续吞掉
        同一轮里另外几个合法方面的判断与那次真实的检索动作。整份那一族因此只剩
        「读不出归属」的两条:载荷不是对象、这一行不是对象。
        """
        if not isinstance(row, Mapping):
            return "", None, "item_not_object"
        raw_id = row.get("aspect_id")
        if not isinstance(raw_id, str) or raw_id not in self._by_id:
            return "", None, "unknown_aspect"
        aspect_id = raw_id
        raw_keys = row.get("evidence_keys")
        if raw_keys is None:
            raw_keys = ()
        if not isinstance(raw_keys, (list, tuple)):
            return aspect_id, None, "evidence_keys_not_list"
        if len(raw_keys) > REFLECT_ASPECT_MAX_EVIDENCE_KEYS:
            # 按方面拒绝、并如实披露稳定原因(§6)。**绝不截成前 8 个**:截断会
            # 把一份"我引了 12 条"的判断悄悄变成一份服务端替它挑过的"已充分"。
            return aspect_id, None, "evidence_keys_overflow"
        if any(not isinstance(key, str) for key in raw_keys):
            return aspect_id, None, "evidence_key_not_string"
        gap = row.get("gap", "")
        if gap is None:
            gap = ""
        if not isinstance(gap, str):
            return aspect_id, None, "gap_not_string"
        if len(gap) > REFLECT_ASPECT_GAP_MAX_CHARS:
            return aspect_id, None, "gap_overflow"
        status = ASPECT_SUPPORTED
        if statuses:
            status = row.get("status", ASPECT_PARTIAL)
            if status is None or status == "":
                status = ASPECT_PARTIAL
            if not isinstance(status, str) or status not in statuses:
                return aspect_id, None, "invalid_status"
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
        return aspect_id, _Update(
            record=self._by_id[aspect_id], status=status, keys=keys,
            gap=_clip(gap, REFLECT_ASPECT_GAP_MAX_CHARS),
            demotion=demotion), ""


#: `REFLECT_ASPECT_GROUP_ROWS_FACTOR` / `REFLECT_ASPECT_GROUP_ROWS_HARD_MAX`
#: (倍率与并行的绝对行数上限)住在 `app.domain.retrieval_termination`:T-PL3
#: 的 lean 自评段要把这两个数插值进 prompt,而 prompts 不能 import 这个服务
#: 模块——理由与上面那两个常量相同。


def _group_row_cap(aspect_count: int) -> int:
    """一组(`supported`/`unresolved`)最多允许几行。超出 ⇒ 整份 `<group>_overflow`。

    ⚠ **这条上限只防超大载荷,不再是"行数不许超过方面数"**(T-BF7 评审 P1)。
    原来的判据 `len(rows) > len(self._records)` 落在 `_plan_row` **之前**,于是
    「同一个方面完全相同的重复项确定性去重」这条 §6 要求在结构上不可能兑现:
    重复项天然多占一行,一个单方面的账收到两条逐字相同的 supported 就整份作废,
    连同那一轮已经通过全部参数校验的检索动作一起。判据挪到规划之后就自然消失
    ——`planned` 按 `aspect_id` 归并、id 必须是本账本的,所以落账候选数**恒**
    不超过方面总数;真正会重复的那一族由 `duplicate_aspect`(冲突)与
    `_Update.content` 去重(完全相同)各自处理,一格都不多占。
    留下来的只是一道"这份载荷大得不像一次自评"的闸:它不表达任何语义判断,只
    避免服务端为一份几万行的数组白跑一遍规划。方面少的时候按倍率收紧(1 个
    方面不该收到 20 行),方面多的时候由绝对值兜住(16 个方面 × 4 = 64 恰好
    压在绝对值上)。
    """
    return min(
        aspect_count * REFLECT_ASPECT_GROUP_ROWS_FACTOR,
        REFLECT_ASPECT_GROUP_ROWS_HARD_MAX,
    )


def _mark_rejected(
    rejected: "Dict[str, str]", planned: "Dict[str, _Update]",
    aspect_id: str, why: str,
) -> None:
    """记下「这个方面本轮不采纳」,并撤掉它此前已经规划好的更新。

    撤掉是**保留旧状态**这条要求的落点(§6):同一个方面先给了一条能落账的
    判断、后面又给了一条与它冲突的,服务端不许拿前一条当答案——那就是"取首条"。
    原因码只记**第一个**:一个方面在同一轮里可以既冲突又超限,而披露与 skip 步
    要的是一个稳定的、与它第一次出问题同源的码,不是一串。

    只处理**本账本自己的** id:未知 id 谈不上"撤掉它的更新",另按条计数。
    """
    planned.pop(aspect_id, None)
    rejected.setdefault(aspect_id, why)


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

    刻意**不看内容合法性**:这个纯函数只回答"载荷里有没有行",合法性由 `apply`
    判(整份形状不成立就折成 `invalid_assessment:<why>`,单个方面越界就只拒那一
    个方面)。收尾轮的「必须自评」闸要的是两者的**合取结果**——「这一轮实际落账
    为空」——所以调用方(`_nudge_missing_assessment`)把这个判据与 `apply` 返回的
    `accepted` 并起来看,而不是在这里偷偷把"给了但不合法"改写成"没给":那样一份
    只错了一个方面、另外几个方面照常落账的自评也会收到一句要它重填的追问。

    非 Mapping(模型回了个列表/字符串)也算空:`apply` 对它返回 `not_object`,
    但那条路只在字段存在时才走到;这里只回答"有没有表态",答案同样是没有。

    **入口先经 `normalize_assessment_payload`**,理由与 `apply` 同:一份按方面
    id 映射写的、内容非空的自评,顶层没有 `supported`/`unresolved` 键,若不先
    归一会被这里误判成"没有表态",进而触发一次不该发生的追问。
    """
    assessment = normalize_assessment_payload(assessment)
    if not isinstance(assessment, Mapping):
        return True
    for group in ("supported", "unresolved"):
        rows = assessment.get(group)
        if isinstance(rows, (list, tuple)) and rows:
            return False
    return True


def build_aspect_ledger(
    intent_detail: object, question: str, *, lean: bool = False,
    assessment_enabled: bool = True,
) -> AspectLedger:
    """按 §7.1 的三条来源建账。**不新增任何模型调用、不做重规划。**

    1. Ask:冻结意图的 `mandatory_topics`(用户在确认门审阅过的那份原文);
    2. Report:本节的 `intent_questions`(随大纲一起被确认过);
    3. 没有正式契约的兼容路径:整条输入问题作为唯一方面。

    三条都只读调用方已经持有的结构,一次查询都不发。相关约束(`constraints`)
    带在账上一起渲染,但**不各自成为一个方面**:约束是"答案要满足什么",不是
    "还要去查什么",给它一个能被独立标成 supported 的 id 只会造出一批永远停在
    unknown 的方面。

    `lean` = 这次 run 走 `prefix_delta_lean` 的轻量自评合同(见
    `AspectLedger.lean_assessment`)。**建账时冻结一次**,所以它必须落在**全部
    三个**返回点上:漏掉任一条来源,那条来源的 run 会在 L 臂标签下跑着 D 的
    追问合同——一次假的臂标签比一次崩溃更贵,因为它只在 A/B 表上看得出来。
    默认 `False` 让既有三臂与全部现有调用点逐字节不变。
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
            constraints=constraints, lean_assessment=lean,
            assessment_enabled=assessment_enabled)
    section_questions = _bounded_unique(
        _text(row) for row in (detail.get("intent_questions") or ()))
    if section_questions:
        return AspectLedger(
            section_questions, source=ASPECT_SOURCE_SECTION_QUESTIONS,
            constraints=constraints, lean_assessment=lean,
            assessment_enabled=assessment_enabled)
    whole = _text(question)
    return AspectLedger(
        [whole] if whole else [], source=ASPECT_SOURCE_WHOLE_QUESTION,
        constraints=constraints, lean_assessment=lean,
        assessment_enabled=assessment_enabled)


def render_aspect_block(ledger: AspectLedger) -> str:
    """方面清单 → 服务器状态块的一段。

    **不受 `state_chars` 约束**:§4 明确把「已冻结约束/必答主题」列在"按现有
    输入/协议边界保留、不得整体裁尾"的那一类里。可压缩的是观察账与历史建议,
    不是用户确认过的必答清单。

    每一行:id | 状态 | 已绑定证据数 | 方面原文 | 缺口。原文与约束是用户内容,
    渲染时只折叠(`_fold`)不截长;`gap` 是模型文本,写进账本时就已按模型文本的
    上限截过。

    ⚠ **这个函数有一处写:** 渲染追问句的同时消费掉 `ledger.nudge_pending`
    (一次性,见下面的注释)。**这个函数**在生产只有一个调用点、每轮至多一次
    (`_reflect_v2_context` 的 `off` 分支;P 那条臂走
    `render_aspect_status_block`,两者互斥),所以"渲染 = 已经说给模型听了"在这里
    是准确的;真要加第二个调用点(诊断、预览),那一个必须先想清楚它算不算
    "说过了"。

    ⚠ **这是 `off` 布局的那一份,逐字节冻结。** `prefix_snapshot` 布局把同一份账
    拆成 `render_aspect_contract_block`(C,run 内不变)+ `render_aspect_status_block`
    (T,每轮重渲染),`_reflect_v2_context` 按闸二选一——两条路径**互斥**,所以那
    一处消费仍然是每轮恰好一次。这个函数不由两半组装:off 的行把原文与状态排在
    同一行,拆开之后拼不回同一串字节,而关闭态字节等价是硬约束。

    ⚠ **开闸前成本表项(不改行为)**:这个块**每一轮逐字重渲染**并整块进 prompt,
    没有增量。契约上界 ≈ 20KB/轮:16 个方面 × (方面原文 ≤ 冻结契约的单条上限
    + `gap` ≤ 240 字符 + 固定前后缀),再加约束行。它不受 `state_chars` 约束
    (见上),所以这份成本是**确定发生**的,不会被观察账那边的压缩吸收——放量
    评估时按「轮数 × 20KB」计入每 run 的输入 token,与 T3 的证据卡预算并列。
    """
    rows = ledger.snapshot()
    if not rows:
        return ""
    rejections, unknown_rejections = ledger.consume_rejection_notes()
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
        if row.aspect_id in rejections:
            # 上一轮这个方面的自评**没被采纳**(它的状态因此还是上面那个旧值),
            # 与「服务端降级」并列而不是合并:降级说的是"你引的键不算数,我把
            # 你的判断降了一档",未采纳说的是"这一条我根本没落账"。一次性,理由
            # 见 `_AspectRecord.rejected`。
            parts.append(f"服务端未采纳: {rejections[row.aspect_id]}")
        lines.append(" | ".join(parts))
    if unknown_rejections:
        # 挂不到任何一行上的那一类:模型写的方面 id 不在上面这份清单里。只说
        # 条数与该怎么办——那个 id 是模型的自由文本,回显一遍不增加任何信息,
        # 而合法的 id 就在上面逐行列着。
        lines.append(
            f"（上一轮有 {unknown_rejections} 条自评的方面 id 不在上面的清单里，"
            "服务端未采纳；请只使用上面每行开头的那个 id。）")
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


def render_aspect_contract_block(ledger: AspectLedger) -> str:
    """方面账的**契约半**:id ↔ 原文,加上约束条件(前缀复用设计 §4.3)。

    只在 `prefix_snapshot` 布局下被调用,落在 C(问题之后)。`render_aspect_block`
    是 `off` 的那一份,一个字节都没动——两半合起来**不重建**那一份:off 的行把
    原文与状态排在同一行(`- a1 | 已支撑 | 已绑定证据 2 条 | 原文`),按块拆开
    之后拼不回同一串字节,所以 off 走的仍是原来那个函数,而不是"由两半组装"。

    这个函数**读到的每一样都是 run 级不变量**,于是 C 在一个 run 内逐字节稳定:
    方面 id 与原文由用户确认过的契约定型(`AspectLedger` 不许增删改名,见
    `_V2_ASSESSMENT_INSTRUCTION`),`constraints` 同样在建账时冻结。状态、已绑定
    证据数、缺口、降级、未采纳、追问句一格都不在这里——它们逐轮变化,全在状态半。

    **零副作用**:不消费 `nudge_pending`、不消费未采纳披露。那两处消费随状态半走
    (`render_aspect_status_block`)。全仓因此是**两个消费点、互斥**——off 一个、
    P 一个,`_reflect_v2_context` 按布局闸二选一,每轮仍恰好消费一次。

    不受 `state_chars` 约束,理由同 `render_aspect_block`:用户确认过的必答清单与
    约束不属于可压缩区,只折叠(`_fold`)不截长。
    """
    rows = ledger.snapshot()
    if not rows:
        return ""
    lines = [ASPECT_CONTRACT_BLOCK_TITLE]
    for row in rows:
        lines.append(f"- {row.aspect_id} | {_fold(row.question)}")
    if ledger.constraints:
        lines.append(
            "约束条件: " + "、".join(_fold(item) for item in ledger.constraints))
    return "\n".join(lines)


def render_aspect_status_block(
    ledger: AspectLedger, *, lean: bool = False,
) -> str:
    """方面账的**状态半**:每轮变化的那一半(前缀复用设计 §4.5)。

    P 与 D(含 L)的共用成型点 `_prefix_context`(`reasoning_retrieval.py`)在这里
    调用,落在 T(user 段末尾)。**方面原文不在这里**——它在 C 里说过一遍了,T 只
    按 id 报当前状态,这正是 §4.3 「完整任务只表达一次」的那一半。

    ⚠ **这个函数有两处写**,与 `render_aspect_block` 里那两处逐字同款:渲染未采纳
    披露的同时消费掉它(`consume_rejection_notes`),渲染追问句的同时消费掉
    `nudge_pending`。两个布局各自只有一个调用点、每轮一次
    (`_reflect_v2_context` 按闸二选一),所以"渲染 = 已经说给模型听了"在这里同样
    准确;`_survive_reflect_failure` 那条重新置位的路径也照旧成立(它认的是同一
    格 `nudge_pending`)。

    `lean` 只换**尾注那一句**(`ASPECT_BLOCK_NOTE` → `ASPECT_BLOCK_NOTE_LEAN`,
    二选一替换),其余每一格逐字不动:全部方面照旧逐行列出、`（已支撑 n/N）`、
    降级/未采纳/未知 id 披露、两处消费副作用。**状态半在 L 下刻意不收窄成"只列
    未落定 + 计数"**:(a) 设计 §4.5 明写「其余方面不从状态列表消失」;(b) L 下
    模型停止重述,这个块因此成了方面账**唯一**的完整落点——`demotion` 与
    「服务端未采纳」都挂在那几行上;(c) L 与 D 除自评合同外任何一处差异都会毁掉
    归因(拍板 Q4)。追问句那一段在 L 下恒不可达(`note_missing_assessment` 的
    第一条闸从不置位 `nudge_pending`),但**不删**:`off` 走的是
    `render_aspect_block`,不是这一个函数;这个函数由 P/D(含 L)共用,三条臂都
    要它。
    """
    rows = ledger.snapshot()
    if not rows:
        return ""
    rejections, unknown_rejections = ledger.consume_rejection_notes()
    lines = [
        f"{ASPECT_STATUS_BLOCK_TITLE}"
        f"（已支撑 {ledger.supported_count()}/{len(rows)}）"
    ]
    for row in rows:
        parts = [
            f"- {row.aspect_id}",
            _STATUS_LABELS.get(row.status, row.status),
            f"已绑定证据 {len(row.evidence_keys)} 条",
        ]
        if row.gap:
            parts.append(f"缺口: {row.gap}")
        if row.demotion:
            parts.append(f"服务端降级: {row.demotion}")
        if row.aspect_id in rejections:
            parts.append(f"服务端未采纳: {rejections[row.aspect_id]}")
        lines.append(" | ".join(parts))
    if unknown_rejections:
        lines.append(
            f"（上一轮有 {unknown_rejections} 条自评的方面 id 不在上面的清单里，"
            "服务端未采纳；请只使用上面每行开头的那个 id。）")
    lines.append(ASPECT_BLOCK_NOTE_LEAN if lean else ASPECT_BLOCK_NOTE)
    if ledger.nudge_pending:
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


def termination_summary(reason: str, *, assessment_enabled: bool = True) -> str:
    if not assessment_enabled and reason in (
            TERMINATION_MODEL_SUFFICIENT, TERMINATION_MODEL_PARTIAL):
        return "检索结束：模型决定作答"
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


def _termination_aspect_list(questions: Sequence[str]) -> str:
    """事实块里的一段方面列举:按 `_TERMINATION_BLOCK_MAX_ASPECTS` 截断,超出的
    补一句 `(and N more)`。

    两个调用点共用**同一个截断口径**(未解决那一行、L 的未评估那一行):两份手写
    的截断会在下一次调上限时分叉,而它们说的是同一件事——"列举本身不是证据,
    截断只损失枚举、不损失结论"。
    """
    shown = questions[:_TERMINATION_BLOCK_MAX_ASPECTS]
    more = len(questions) - len(shown)
    return "; ".join(shown) + (f" (and {more} more)" if more > 0 else "")


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

    ⚠ **L 专属的第四行:未评估不冒充缺失**(设计 §6 末段,拍板 Q6)。上面那一行
    「Questions the retrieval did not resolve」在三臂下混着两种东西:模型查过、
    确实没找到的方面,与模型压根没对它表过态的方面。`off`/P/D 下第二种是异常
    (收尾缺自评会被退回追问),而 **L 下它是常态**——那条臂的自评合同明说未走到
    的方面可以留着不评。于是同一行文本在 L 下的含义悄悄从"查过没有"漂成"没查
    过",而合成读到的是前者,答案里就会出现一句"笔记本里没有这份材料"的假结论。
    这一行把差额如实说出来,**先事实、后自报**:有几个方面没被逐项核验、是哪几个
    (复用 `_TERMINATION_BLOCK_MAX_ASPECTS` 的截断口径,超出的补一句
    `and N more`),再带上模型自己的结束判断(`model_assessed_sufficient`,与
    `reason` 分开的那一格)——那半句更宽松的自报判读放在计数与列举之后,不放句首,
    免得给合成模型留一句可以援引来跳过缺口说明的话,最后钉一句这**不是**
    "资料不存在"。

    判据挂在 `termination.lean_assessment`(run 级)而不是"有没有
    `model_assessed=False` 的行":后者在三臂下同样能为真(run 早早被熔断、模型
    没走到收尾),那时多出这一行会改掉 B/P/D 的合成字节,而三臂字节等价是本期
    硬约束。**两个条件都要**:是 L,且这一份终态里真有未评估的未解决方面。
    `directive` 尾句与上面三条硬边界一字不改——这一行是**披露**,不是新指令,
    尾句那条「不许把这段读成"库里没有"」正好已经覆盖它。
    """
    if termination is None:
        return ""
    if (not termination.assessment_enabled
            and termination.reason in (
                TERMINATION_MODEL_SUFFICIENT, TERMINATION_MODEL_PARTIAL)
            and not termination.unrecovered_channels):
        return ""
    fact = _TERMINATION_PROMPT_FACTS.get(termination.reason, "")
    if (not termination.assessment_enabled and termination.reason in (
            TERMINATION_MODEL_SUFFICIENT, TERMINATION_MODEL_PARTIAL)):
        fact = "the planner chose to stop retrieval"
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
        lines.append(
            "- Questions the retrieval did not resolve: "
            + _termination_aspect_list(open_questions)
        )
    # L 专属的披露(见 docstring 的 ⚠ 段)。`unassessed` 是 `open_questions` 的
    # 子集——同一份未解决清单再过一道「这一格根本没有模型判断」的闸,所以上面那
    # 一行为空时这一行结构性也为空,不会出现"没有未解决方面却说有几个未核验"。
    # 三臂(off/P/D)在这里白付零工作:计算挪进闸里,不是 L 就不算这笔账。
    if termination.lean_assessment:
        unassessed = [
            _fold(by_id[aspect_id].question)
            for aspect_id in termination.unresolved_aspect_ids
            if aspect_id in by_id and by_id[aspect_id].question
            and not by_id[aspect_id].model_assessed
        ]
        if unassessed:
            count = len(unassessed)
            lines.append(
                f"- {count} mandatory aspect{'s' if count != 1 else ''} "
                + ("were" if count != 1 else "was")
                + " never assessed item by item: "
                + _termination_aspect_list(unassessed)
                + ". The planner "
                + ("reported" if termination.model_assessed_sufficient
                   else "did not report")
                + " the evidence as sufficient when it stopped; that only "
                "means THIS retrieval did not check them one by one — it is "
                "NOT a finding that the notebook lacks the material."
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
    if directive and not nothing_open and termination.assessment_enabled:
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
    if not termination.assessment_enabled:
        return {
            "termination_reason": termination.reason,
            "termination_summary": termination_summary(
                termination.reason, assessment_enabled=False),
            "unrecovered_channels": list(termination.unrecovered_channels),
        }
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

    `lean_assessment` 只是把账本上那一格 run 级事实**原样带上**终态 DTO,不参与
    上面任何一条判据:终态 reason 闭集在 L 下一格不改(拍板 Q7)——改它会让四臂
    的终态分布再也不能横向比。它服务的是合成侧那一行披露
    (`render_termination_block`),那边需要知道"这条臂本来就不逐项问",而这件事
    从 per-aspect 的沉默里反推不出来。
    """
    unresolved = ledger.unresolved_ids() if ledger.assessment_enabled else ()
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
        aspects=ledger.snapshot() if ledger.assessment_enabled else (),
        lean_assessment=ledger.lean_assessment,
        assessment_enabled=ledger.assessment_enabled,
    )
