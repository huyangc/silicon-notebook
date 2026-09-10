"""E2(固定状态的真实决策):脚本化驱动器 + `model_clients` 代理 + 闭集记录面。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`
§9.2;实施真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
§1 M3(接缝位置与「零模型推进」的两个前提)、§2 Q5(case 约束)/Q6(记录面)/
Q7(测试库只读)、§3 T-EX5。

**E2 的全部价值在两句话上,这个模块的每一处判据都在守它们**:

1. **模型选出的动作一次都不执行。** 驱动器把第 k+1 轮收到的那两条消息原样转发
   给真客户端调一次、把回来的决定**留存**下来,然后向 `run()` 返回一条**脚本化
   的停止决定**。于是模型选的动作既不进 trace、也不落库、也不改变下一状态——
   E2 测的是「固定观察下的策略」,不是它自洽的真实轨迹(§9.2 原话)。
2. **四臂站在同一个状态点上。** 同一份剧本被四条臂各自从第 1 轮重放到第 k 轮
   (全部由剧本推进,零真实调用),所以前 k 轮的动作序列逐格相同;差别只在
   第 k+1 轮那两条消息**怎么分块**(布局由 `settings.reasoning_reflect_optimization`
   承载)。不重放而去序列化 `_ReasoningRunState`,就是把「同一状态点」这句话
   换成一句没人能复核的断言。

**服务端零改动。** 接缝是 `model_clients`——它在 `_construct_reasoning_retriever`
里已经是一个参数(`model_clients=repository`),而 `reflect()` 每轮从
`self.model_clients.chat("reasoning_agent")` **现取**客户端。所以 E2 只需要一个
`__getattr__` 委托到真 repo、只覆写 `chat()` 的薄代理(`ProbeModelClients`),
`app/services/**` 与三个热函数一个字节不动(计划 M8)。

**这个模块不 import `scripts/`,也不 import `backend/tests/model_testkit.py`。**
后者的 `bind_chat_client` 是给测试用的 provider 覆写,把它拉进实验路径会让 E2
依赖测试目录;前者是 CLI 适配层,方向必须是 rig → 这里。重的
`app.services.*` 一律**函数内**导入(先例:`app/eval/mrl_truncation.py`),
所以 `import app.eval.reflect_state_probe` 本身不拉起检索栈。

**`None` 是一等值**(沿用 T0 §4.1 与兄弟模块 `reflect_context_bench`):sink 空、
provider 不回、测量关,一律 `None` = unknown,**绝不折成 0 / False**。

**命名红线**:这张表里没有、也不许有任何 `cache_hit` / 命中率 / hit rate 形状的
**键名**。`status` 是 `app/core/llm.py` 写进 `call_stats` 的既有事实字段(闭集
`ok` / `cache_hit` / `cancelled` / `error`),值原样透传;摘要里那一格叫
`local_cache_exit_rows`,不叫 cache_hit。

**隐私**:行里只装数值与短码。请求正文、模型决定的 `reason` / `gap` 原文、题面
一个字都不进行(`assert_probe_row_closed` 用与投影同一把尺子逐值自检)。决定
JSON 全文与四臂各自的消息正文只经 `ScriptedReflectDriver` 的留存面交给调用方
写进操作者私有的 `.local/raw/`(§8.2)。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.domain.reasoning_trace_stats import assert_projection_values
from app.domain.retrieval_termination import ASPECT_UNKNOWN

# --- 常量与闭集 ---------------------------------------------------------------

#: 反思用的那个 workload id。`reasoning_retrieval.reflect()` 里写的是字面量
#: `self.model_clients.chat("reasoning_agent")`,这里具名一次给代理用;
#: 两处对不上时的故障形态是「代理谁都没截住、E2 直接打了真模型一整条 run」,
#: 所以用例里有一条守卫把这个字面量与服务端源码对号(见
#: `test_the_probe_intercepts_the_workload_reflect_actually_asks_for`)。
REFLECT_CHAT_WORKLOAD = "reasoning_agent"

#: `state_probes.json` 的顶层键闭集(T-EX6 评审把这一格留给本加载器)。
#: `note` 是给人读的一句话,`version` 是形状版本,`cases` 是数据本体。
TOP_LEVEL_KEYS: frozenset[str] = frozenset({"version", "note", "cases"})

#: 当前认得的 case 集形状版本。对不上就响亮拒——一份形状变过的 fixture 悄悄被
#: 老加载器读进来,比读不进来危险得多。
CASE_SET_VERSION = 1

#: 每例必备 / 可选的键(Q5 + T-EX6 落地后的两个新键 `question_key` /
#: `probe_shape`)。多一个闭集外的键要被看见(与投影闭集同一条纪律),少一个
#: 必填键更要。
REQUIRED_CASE_KEYS: frozenset[str] = frozenset({
    "case_key", "probe_shape", "corpus_cell", "question_key", "question",
    "intent_contract", "script", "state_points",
})
OPTIONAL_CASE_KEYS: frozenset[str] = frozenset({"settings_overrides"})

#: 剧本步的键闭集,按 `parse_reflect_v2` 真正读的键定。闭集外的键(比如拼错的
#: `assesment`)不会让解析报错——它只是被 `dict.get` 静默忽略,那份自评于是在
#: 生产里悄悄失踪而不留任何痕迹。所以形状判据要在加载这一侧堵。
SCRIPT_STEP_REQUIRED_KEYS: frozenset[str] = frozenset({
    "next_action", "sufficient", "arguments",
})
SCRIPT_STEP_OPTIONAL_KEYS: frozenset[str] = frozenset({"reason", "assessment"})

#: `settings_overrides` 白名单(Q5 第三条)。只收两个**检索上限**键:Q5 只点名
#: 三种造形态用法(元素额度 = 1 / 同查询两轮 / 必然零命中),从未授权动渲染预算
#: 旋钮。
SETTINGS_OVERRIDE_WHITELIST: frozenset[str] = frozenset({
    "reasoning_max_element_searches",
    "reasoning_max_chunk_searches",
})

#: 渲染预算键——四臂对照的默认前提。任何 case 都不许覆盖它们:调小它们去逼出
#: 压缩边界,与调剧本去凑是同一件事的两种写法,还会让被改的那一例跑在与其余
#: 十一例不可比的预算上(§9.2「缺数据不补造」)。白名单之外**再独立拒一次**:
#: 白名单万一哪天被放宽,这条红线单独兜底。
FORBIDDEN_OVERRIDE_KEYS: frozenset[str] = frozenset({
    "reasoning_reflect_state_chars",
    "reasoning_reflect_evidence_chars_by_effort",
})

#: 剧本里允许出现的动作(Q5 第一条:只用查询串型参数)。
ALLOWED_SCRIPT_ACTIONS: frozenset[str] = frozenset({
    "search_elements", "search_chunks", "add_subquery",
    "enumerate_elements", "enumerate_kg_objects", "exact_lookup",
})

#: 明令不进本期剧本的三个动作:它们的必填参数是候选池里的 `object_id`,而驱动器
#: 只看得见渲染后的文本,给不出一个真实候选的 id(登记为债务,见计划 Q5)。
FORBIDDEN_SCRIPT_ACTIONS: frozenset[str] = frozenset({
    "expand_graph", "follow_chain", "ppr_retrieve",
})

#: 任何一层的键名里都不许出现的 id 槽位。`source_title` 也在内:B 语料的文件名
#: 带内部 `src-` 前缀,抄进 fixture 就等于把一个 id 签进仓库。
FORBIDDEN_ID_KEYS: frozenset[str] = frozenset({
    "object_id", "source_id", "source_ids", "source_title",
    "expand_object_id", "start_object_id", "target_object_id",
    "notebook_id", "notebook", "chunk_id", "element_id", "id_",
})

#: 值里不许出现的 id / 连接串片段。三个 id 前缀是仓库里真实在用的
#: (`nb-…` / `src-…` / `ko-…`),后面几个挡住凭据与生产 URL(§8.2)。
FORBIDDEN_VALUE_FRAGMENTS: tuple[str, ...] = (
    "nb-", "src-", "ko-", "postgresql://", "postgres://",
    "http://", "https://",
)

#: 语料格闭集。E2 只用这两格:「有图 / 无图」由格承载而不由图动作承载
#: (Q5 的实现口径收窄)。
ALLOWED_CORPUS_CELLS: frozenset[str] = frozenset({"A_nokg", "B_kg"})

#: 每例的状态点个数(§9.2:初始状态 / 后续状态 / 压缩边界各一个)。
STATE_POINT_COUNT = 3

#: 三个状态点在报告里的档名,**按序号**对号(0/1/2)。§9.2 要求「对初始状态、
#: 后续状态、压缩边界分别报告」。
STATE_POINT_LABELS: tuple[str, ...] = (
    "initial", "follow_up", "compaction_boundary",
)


class StateProbeError(BaseException):
    """E2 的剧本/编排出了与「同一状态点」不相容的事,当场响亮失败。

    单开一个类型是为了让 rig(T-EX7)能把「这一格的剧本没写对」与「这次模型
    调用失败了」分开:后者是数据(失败单列,§9.2),前者不是——一格状态点没
    对上的数据混进表里,会跑出一批看起来完全正常、其实四臂状态点不同的数。

    ⚠ **继承 `BaseException` 而不是 `Exception`,这是刻意的。** 驱动器住在
    `reflect()` 的调用位上,而 `_reflect_v2_attempt` 对模型侧的任何
    `Exception` 都有一条 fail-open 合同(`except Exception` ⇒
    `_reflect_fallback`)——那条合同是对的:一次 provider 故障不该让整次 Ask
    死掉。但它会把驱动器的「这条 run 已经不是它自称的那个状态点」洗成一次
    **假的模型兜底**,run 于是继续往下跑,而唯一还看得见这件事的地方是编排层
    事后那道轮数核对。`BaseException` 让它像 `KeyboardInterrupt` 一样穿过所有
    fail-open,在出问题的那一行就停。

    代价要说清:`except Exception` 的调用方(包括 rig 的批处理循环)**抓不到**
    它。这正是想要的效果——「剧本没写对」要停批而不是被记成一行失败;T-EX7
    如果要按格容错,必须显式 `except StateProbeError`。
    """


# --- case schema:加载与校验 ---------------------------------------------------


@dataclass(frozen=True)
class StateProbeCase:
    """一例自包含的固定状态剧本。字段与 `state_probes.json` 逐格对应。

    `script` / `state_points` / `intent_contract` / `settings_overrides` 都是
    **深拷贝**后冻在这里的原始 JSON 值:同一例要被四条臂各重放一次,任何一条
    臂在运行期改动了它,后面几条臂就不再站在同一个状态点上。
    """

    case_key: str
    probe_shape: str
    corpus_cell: str
    question_key: str
    question: str
    intent_contract: Mapping[str, Any]
    script: tuple[Mapping[str, Any], ...]
    state_points: tuple[int, ...]
    settings_overrides: Mapping[str, Any] = field(default_factory=dict)

    def state_point_turn(self, state_point_index: int) -> int:
        """第 `state_point_index`(0/1/2)个状态点的**轮号**(1-based)。

        行里存的是序号而不是轮号(见 `PROBE_ROW_KEYS` 的说明),轮号由这里从
        case 集确定性地取回。
        """
        if not 0 <= state_point_index < len(self.state_points):
            raise StateProbeError(
                f"{self.case_key}: state point index {state_point_index} is "
                f"outside 0..{len(self.state_points) - 1}"
            )
        return self.state_points[state_point_index]


def _walk_json(node: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """深度遍历一个 JSON 值,产出 `(路径, 叶子值)`。**键名也当叶子产出一次**,
    这样「键里带 id」与「值里带 id」用同一把尺子量。"""
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, key
            yield from _walk_json(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk_json(value, f"{path}[{index}]")
    else:
        yield path, node


def _reject(case_key: str, message: str) -> None:
    raise ValueError(f"state probe case {case_key!r}: {message}")


def _assert_no_database_ids(case_key: str, case: Mapping) -> None:
    for path, value in _walk_json(case):
        if value in FORBIDDEN_ID_KEYS:
            _reject(case_key, f"carries a database id slot at {path}")
        if isinstance(value, str):
            lowered = value.lower()
            for fragment in FORBIDDEN_VALUE_FRAGMENTS:
                if fragment in lowered:
                    _reject(
                        case_key,
                        f"carries a forbidden value fragment {fragment!r} "
                        f"at {path}",
                    )


def _assert_intent_contract(case_key: str, contract: object) -> None:
    """Q5 第二条:每例冻一份可用的 `QueryIntentContract`,必答方面非空。

    用**真模型**校验一次(而不是在这里再写一份形状判据):这就是 rig 那条
    `QueryIntentContract(**contract)` 会吃到的东西。非空契约 ⇒ `run()` 拿到非空
    `intent_queries` ⇒ 零意图调用(计划 M3)。
    """
    from app.models.ask import QueryIntentContract

    if not isinstance(contract, Mapping):
        _reject(case_key, "intent_contract must be a mapping")
    try:
        frozen = QueryIntentContract(**dict(contract))
    except Exception as exc:  # noqa: BLE001 — 形状不合当场说清是哪一例
        _reject(case_key, f"intent_contract is unusable: {type(exc).__name__}")
        raise  # pragma: no cover — `_reject` 必抛,这一句只为类型收敛
    if not frozen.mandatory_topics:
        _reject(case_key, "intent_contract has no mandatory_topics")
    if frozen.needs_clarification or frozen.clarification_answers:
        # 代答过澄清项的题,权威方向会从用户原文换成合成后的 `resolved_question`
        # (`_prepare_search_intent` 的 `authoritative` 判据)。E2 要的是冻结的
        # 「已确认且本来就没歧义」那一档。
        _reject(
            case_key,
            "intent_contract must be the already-confirmed, unambiguous "
            "shape (no needs_clarification, no clarification_answers)",
        )


def _assert_script(case_key: str, script: object) -> tuple[Mapping, ...]:
    if not isinstance(script, (list, tuple)) or not script:
        _reject(case_key, "script must be a non-empty list")
    steps: list[Mapping] = []
    for index, step in enumerate(script, 1):
        if not isinstance(step, Mapping):
            _reject(case_key, f"script step {index} is not an object")
        keys = set(step)
        missing = SCRIPT_STEP_REQUIRED_KEYS - keys
        if missing:
            _reject(
                case_key,
                f"script step {index} is missing key(s): "
                + ", ".join(sorted(missing)),
            )
        allowed = SCRIPT_STEP_REQUIRED_KEYS | SCRIPT_STEP_OPTIONAL_KEYS
        extra = keys - allowed
        if extra:
            _reject(
                case_key,
                f"script step {index} carries key(s) outside the closed set: "
                + ", ".join(sorted(extra)),
            )
        action = step["next_action"]
        if action in FORBIDDEN_SCRIPT_ACTIONS:
            _reject(
                case_key,
                f"script step {index} uses {action!r}, whose arguments need a "
                "candidate-pool object id (registered as debt, see plan Q5)",
            )
        if action not in ALLOWED_SCRIPT_ACTIONS:
            _reject(case_key, f"script step {index} uses unknown action {action!r}")
        if step["sufficient"] is not False:
            # 检索动作与 `sufficient=true` 不能同轮成立
            # (`parse_reflect_v2` 的 `_V2_SUFFICIENT_CONTRADICTION`);剧本里
            # 每一轮都是检索,所以这一格恒 false。停止决定由驱动器自己发,不
            # 由剧本发——那是第 k+1 轮的事。
            _reject(case_key, f"script step {index} must carry sufficient=false")
        if not isinstance(step["arguments"], Mapping):
            _reject(case_key, f"script step {index} arguments must be an object")
        steps.append(step)
    return tuple(steps)


def _assert_state_points(
    case_key: str, points: object, script_length: int,
) -> tuple[int, ...]:
    """三个状态点、严格单调递增、每个 ≥ 1、且**严格小于**剧本长度。

    `< len(script)` 而不是 `<=`:第 k 个状态点的跑法是「剧本推进第 1..k 轮,
    第 k+1 轮转发给真客户端」,所以剧本必须**还能容纳**第 k+1 轮——第 k+1 轮的
    那一步不会被采用(驱动器在那一轮转发并自己发停止决定),它的存在只是为了
    证明第 k+1 轮落在剧本已经想清楚的范围内、驱动器不必在那一轮兜底编一轮观察
    (§9.2「缺数据不补造」)。
    """
    if not isinstance(points, (list, tuple)) or len(points) != STATE_POINT_COUNT:
        _reject(
            case_key,
            f"state_points must be a list of exactly {STATE_POINT_COUNT} turns",
        )
    for point in points:
        if isinstance(point, bool) or not isinstance(point, int) or point < 1:
            _reject(case_key, f"state point {point!r} must be an int >= 1")
    ordered = list(points)
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        _reject(case_key, f"state_points must be strictly increasing: {ordered}")
    if ordered[-1] >= script_length:
        _reject(
            case_key,
            f"last state point {ordered[-1]} needs a turn {ordered[-1] + 1} the "
            f"script does not cover (script has {script_length} steps)",
        )
    return tuple(ordered)


def _assert_settings_overrides(case_key: str, overrides: object) -> Mapping:
    from app.core.config import Settings

    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping) or not overrides:
        _reject(case_key, "settings_overrides, when present, must be non-empty")
    hit = set(overrides) & FORBIDDEN_OVERRIDE_KEYS
    if hit:
        _reject(
            case_key,
            "settings_overrides may never touch the rendering budget key(s): "
            + ", ".join(sorted(hit)),
        )
    for key in overrides:
        if key not in SETTINGS_OVERRIDE_WHITELIST:
            _reject(case_key, f"settings_overrides key {key!r} is not whitelisted")
        if key not in Settings.model_fields:
            _reject(case_key, f"settings_overrides key {key!r} is not a Settings field")
    return dict(overrides)


def load_state_probe_cases(raw: Mapping) -> list[StateProbeCase]:
    """`state_probes.json` 的加载与校验器。**畸形当场 `ValueError`,带 `case_key`。**

    逐条兑现 Q5 的四条约束:

    * **无数据库 id** —— 键名与叶子值用同一把尺子递归扫(`_walk_json`);
    * **必须带冻结契约** —— 过真 `QueryIntentContract`,`mandatory_topics` 非空,
      且必须是「已确认且本来就没歧义」那一档;
    * **`settings_overrides` 只许两键白名单** —— 渲染预算键另行独立拒;
    * **三个状态点、严格单调、`< len(script)`** —— 越界拒。

    再加剧本步的键闭集与动作闭集。判据与
    `backend/tests/test_reflect_state_probe.py` 的形态对账那一节**同源但不互相
    import**:那一节刻意不经过这个加载器——拿被测者自己出考题,加载器哪天把一条
    约束松掉,那一节照样绿。
    """
    if not isinstance(raw, Mapping):
        raise ValueError("state probe case set must be a mapping")
    extra = set(raw) - TOP_LEVEL_KEYS
    if extra:
        raise ValueError(
            "state probe case set carries keys outside TOP_LEVEL_KEYS: "
            + ", ".join(sorted(extra))
        )
    if raw.get("version") != CASE_SET_VERSION:
        raise ValueError(
            f"state probe case set version must be {CASE_SET_VERSION}, got "
            f"{raw.get('version')!r}"
        )
    cases = raw.get("cases")
    if not isinstance(cases, (list, tuple)) or not cases:
        raise ValueError("state probe case set has no cases")

    loaded: list[StateProbeCase] = []
    seen: set[str] = set()
    for position, case in enumerate(cases, 1):
        if not isinstance(case, Mapping):
            raise ValueError(f"state probe case #{position} is not an object")
        case_key = case.get("case_key")
        if not isinstance(case_key, str) or not case_key:
            raise ValueError(f"state probe case #{position} has no case_key")
        if case_key in seen:
            _reject(case_key, "case_key is used twice")
        seen.add(case_key)
        keys = set(case)
        missing = REQUIRED_CASE_KEYS - keys
        if missing:
            _reject(case_key, "missing key(s): " + ", ".join(sorted(missing)))
        allowed = REQUIRED_CASE_KEYS | OPTIONAL_CASE_KEYS
        surplus = keys - allowed
        if surplus:
            _reject(
                case_key,
                "carries key(s) outside the closed set: "
                + ", ".join(sorted(surplus)),
            )
        if case["corpus_cell"] not in ALLOWED_CORPUS_CELLS:
            _reject(case_key, f"unknown corpus_cell {case['corpus_cell']!r}")
        for text_key in ("probe_shape", "question_key", "question"):
            value = case[text_key]
            if not isinstance(value, str) or not value.strip():
                _reject(case_key, f"{text_key} must be a non-empty string")
        _assert_no_database_ids(case_key, case)
        _assert_intent_contract(case_key, case["intent_contract"])
        script = _assert_script(case_key, case["script"])
        points = _assert_state_points(case_key, case["state_points"], len(script))
        overrides = _assert_settings_overrides(
            case_key, case.get("settings_overrides"))
        loaded.append(StateProbeCase(
            case_key=case_key,
            probe_shape=case["probe_shape"],
            corpus_cell=case["corpus_cell"],
            question_key=case["question_key"],
            question=case["question"],
            intent_contract=copy.deepcopy(dict(case["intent_contract"])),
            script=tuple(copy.deepcopy(dict(step)) for step in script),
            state_points=points,
            settings_overrides=copy.deepcopy(dict(overrides)),
        ))
    return loaded


def case_set_digest(raw_bytes: bytes) -> str:
    """case 集**字节**的短码摘要,进 manifest 的 `case_set_digest`(T-EX1)。

    量的是文件字节而不是解析后的结构:manifest 要冻住的是「这次跑读的是哪一份
    剧本」,而一次只改注释的编辑同样让「同一个摘要」这句话不再成立。十六位十六
    进制与 rig 的 `_ab_corpus_signature` 同形,所以两个摘要在报告里长得一样、
    都过 manifest 的短码闸。
    """
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError("case_set_digest takes the raw file bytes")
    return hashlib.sha256(bytes(raw_bytes)).hexdigest()[:16]


def load_state_probe_case_set() -> tuple[list[StateProbeCase], str]:
    """从 `app.eval.reflect_t0` 包读 12 例 case 集 → `(cases, digest)`。

    路径常量是包导出的 `STATE_PROBES_PATH`,**不在这里另起一份**(T-EX6 汇合
    义务第 5 条)。这是这个模块里唯一一处 I/O,单独一格是为了让上面那些纯函数
    在标准门里能被 fixture 直接喂。
    """
    from app.eval.reflect_t0 import STATE_PROBES_PATH

    raw_bytes = STATE_PROBES_PATH.read_bytes()
    cases = load_state_probe_cases(json.loads(raw_bytes.decode("utf-8")))
    return cases, case_set_digest(raw_bytes)


# --- 脚本化驱动器 -------------------------------------------------------------

#: 停止决定里那条自评行的 `gap`(定宽短码,不是人话):它会被
#: `AspectLedger.apply` 落进方面账并在后续渲染里出现,所以必须是一句**不含任何
#: 模型/题面文本**的固定标记。E2 的 run 在这一轮就结束,没有「后续渲染」,但这
#: 条纪律不因此松开。
STOP_DECISION_GAP = "probe_stop"

#: 停止决定的 `reason`。同款:固定短码,不是自由文本。
STOP_DECISION_REASON = "probe_stop"


@dataclass
class ForwardedTurn:
    """第 k+1 轮那一次**真实**调用的全部留存。正文只到这里,不进投影行。

    * `messages` / `schema_hint` —— 转发给真客户端的那两条消息与 hint 原样;
      四臂各自的分块差异就在这里(用例 (c) 与 `.local/raw/` 都读它)。
    * `raw` —— 真客户端返回的原始 JSON 文本(转发失败时 `None`)。
    * `stats` —— 那一次调用的 `call_stats` 读数(`app/core/llm.py` 的四个出口
      都写 `status` / `call_wall_ms` / `attempts`)。
    * `error` —— 转发抛异常时只留**类名**;异常消息可能带请求正文,一个字都不留。
    """

    turn: int
    messages: tuple[Mapping[str, str], ...]
    schema_hint: str
    raw: str | None = None
    stats: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ScriptedTurn:
    """一轮由剧本推进的调用的留存(system 段 / messages / schema hint)。"""

    turn: int
    messages: tuple[Mapping[str, str], ...]
    schema_hint: str
    payload: Mapping[str, Any]


class ScriptedReflectDriver:
    """duck-typed chat 客户端:前 k 轮按剧本走,第 k+1 轮转发一次真实调用。

    形状抄的是 `backend/tests/test_reasoning_retrieval.py` 那两个既有替身:
    `_SeqLLM` 的剧本机制(按 `"sub_queries" in schema_hint` 分 plan / reflect)
    + `_GatedV2LLM` 的留存机制(每轮的 system 段 / messages / schema hint),
    再加**状态点处转发给真客户端**这一件新事。

    三条响亮失败,一条都不兜底:

    * **plan 分支被触达** ⇒ 抛。E2 每例冻了一份意图契约,`run()` 拿到非空
      `intent_queries` 就不调 plan 的 LLM(计划 M3)。真被调到,说明这一格的
      意图没有冻住,那条 run 的第 k+1 轮已经不是它自称的那个状态点。
    * **第 k+2 轮被触达** ⇒ 抛。第 k+1 轮返回的是停止决定,`run()` 应当当场收尾;
      还有下一轮,说明那条停止决定没被采用(比如被 `missing_assessment` 折回),
      而那一轮的消息形状已经不是要测的那一个。
    * **转发次数不是 1** ⇒ 由 `run_state_probe_point` 事后核(见那里)。

    转发**失败不是异常**:一次死掉的调用同样是一个数据点(§9.2「失败/格式不符
    单列」),所以异常被收进 `ForwardedTurn.error`、这一轮照样返回停止决定,让
    这条 run 干净收尾。取消是唯一的例外——`CoreCancellation` 整个基类照旧上抛,
    与 `reasoning_retrieval` 对取消的既有口径同款。
    """

    #: `reflect()` 第一件事就是 `getattr(client, "configured", False)`;为假会
    #: 走 `_reflect_fallback("model_unconfigured")`,一次模型都不调。
    configured = True

    #: 声明支持 `call_stats` 出参,于是 `_call_stats_kwargs` 会把反思层自己的
    #: sink 传进来。转发那一轮把真实调用的三个测量读数**镜像**进去(见
    #: `_mirror_measure_keys`),剧本轮什么都不写——`_measure_reflect_call` 的
    #: 「缺键 ⇒ 不写」于是让 trace 上只有真实那一轮带墙钟,剧本轮如实空着。
    supports_call_stats = True

    def __init__(
        self,
        case: StateProbeCase,
        state_point_index: int,
        real_client: Any,
        *,
        stop_aspect_id: str,
    ) -> None:
        self.case = case
        self.state_point_index = state_point_index
        self.state_point_turn = case.state_point_turn(state_point_index)
        self.real_client = real_client
        self.stop_aspect_id = stop_aspect_id
        #: 已经发生的 reflect 轮数(1-based 的最后一轮号)。
        self.turns = 0
        self.scripted_turns: list[ScriptedTurn] = []
        self.forwarded: ForwardedTurn | None = None
        self.plan_calls = 0

    # --- 留存面 ---

    @property
    def forward_count(self) -> int:
        return 1 if self.forwarded is not None else 0

    def system_prompt(self, turn: int) -> str:
        """第 `turn` 轮(1-based)的 system 段。转发轮走 `forwarded`。"""
        return self._messages_for(turn)[0]["content"]

    def _messages_for(self, turn: int) -> tuple[Mapping[str, str], ...]:
        if self.forwarded is not None and self.forwarded.turn == turn:
            return self.forwarded.messages
        for scripted in self.scripted_turns:
            if scripted.turn == turn:
                return scripted.messages
        raise StateProbeError(
            f"{self.case.case_key}: no reflect turn {turn} was recorded")

    def scripted_actions(self) -> tuple[str, ...]:
        """前 k 轮**送出去的**动作序列。四臂同剧本时它必须逐格相同——这是
        「同一状态点」这句话的可复核形式(用例 (c))。"""
        return tuple(
            str(scripted.payload["next_action"])
            for scripted in self.scripted_turns
        )

    # --- 传输面 ---

    def chat_json(self, messages, schema_hint, **kwargs) -> str:
        if "sub_queries" in schema_hint:
            # `_SeqLLM` 的同一条判据。E2 结构上不该走到这里。
            self.plan_calls += 1
            raise StateProbeError(
                f"{self.case.case_key}: the plan LLM was called, so this run's "
                "intent was not frozen (see plan M3)"
            )
        self.turns += 1
        turn = self.turns
        frozen = tuple(dict(row) for row in messages)
        if turn <= self.state_point_turn:
            payload = self._script_payload(turn)
            self.scripted_turns.append(ScriptedTurn(
                turn=turn, messages=frozen, schema_hint=schema_hint,
                payload=payload))
            return json.dumps(payload, ensure_ascii=False)
        if turn == self.state_point_turn + 1:
            self._forward(turn, frozen, messages, schema_hint, kwargs)
            return json.dumps(
                self.stop_decision_payload(), ensure_ascii=False)
        raise StateProbeError(
            f"{self.case.case_key}: reflect turn {turn} was reached, but the "
            f"scripted stop decision at turn {self.state_point_turn + 1} was "
            "supposed to end this run — the stop decision was not adopted"
        )

    def _script_payload(self, turn: int) -> dict[str, Any]:
        """剧本第 `turn` 步 → 一份 v2 合法载荷。**按闭集逐键装**,不 `dict(step)`:
        加载器已经拒过闭集外的键,这里再走一次同一份闭集,让「fixture 里多一个键
        会不会被送上线」不依赖两处判据之一。"""
        step = self.case.script[turn - 1]
        payload: dict[str, Any] = {
            key: copy.deepcopy(step[key]) for key in SCRIPT_STEP_REQUIRED_KEYS
        }
        for key in SCRIPT_STEP_OPTIONAL_KEYS:
            if key in step:
                payload[key] = copy.deepcopy(step[key])
        return payload

    def stop_decision_payload(self) -> dict[str, Any]:
        """第 k+1 轮返回给 `run()` 的**脚本化停止决定**。

        形状不是随手挑的,三格各有一条判据把它钉死:

        * `next_action="answer"` + `sufficient=True` —— `answer` 永远可用
          (`build_reflect_capabilities` 的原话:「一个连停下来作答都没有的动作
          面不是更严格,是死锁」),而它 `produces_evidence=False`,所以
          `sufficient=True` 不会撞上 `_V2_SUFFICIENT_CONTRADICTION`。
        * `assessment` **必须非空且至少落账一行** —— 收尾载荷一格自评都没落账
          时,`_nudge_missing_assessment` 会把这一轮折成
          `invalid_reason="missing_assessment"` 并再要一轮
          (`reasoning_retrieval:5606`)。那就直接把 run 推到第 k+2 轮、撞上上面
          那条响亮失败。所以这里带**一行 `unresolved`**:它不需要
          `evidence_keys`(那些键是服务端签发的运行期身份,fixture 里给不出),
          `status=unknown` 落在 `ASPECT_UNRESOLVED_STATUSES` 闭集里,于是
          `AspectLedger.apply` 把它记进 `accepted`,收尾闸放行。
        * `gap` / `reason` 是**定宽短码**,不是人话:这两格会进方面账与轨迹,
          而 E2 的产物里不许有任何自由文本。

        这条决定与真实模型返回的那一份**没有任何关系**:模型那一份只被留存
        (`ForwardedTurn.raw`),一个字节都不会被 `run()` 消费。
        """
        return {
            "next_action": "answer",
            "sufficient": True,
            "arguments": {},
            "reason": STOP_DECISION_REASON,
            "assessment": {
                "unresolved": [{
                    "aspect_id": self.stop_aspect_id,
                    "status": ASPECT_UNKNOWN,
                    "gap": STOP_DECISION_GAP,
                }],
            },
        }

    #: 镜像回反思层 sink 的键:`_MEASURE_CALL_KEYS` 的三个源键。刻意**不**镜像
    #: `finish_reason` / `status`:反思层只在异常路径上读 `finish_reason`
    #: (`_reflect_fallback_reason`),把真实调用的那一个塞过去会让「这次真实
    #: 调用的 provider 说了什么」参与到 E2 自己的停止决定上。
    _MIRRORED_STATS_KEYS: tuple[str, ...] = (
        "call_wall_ms", "attempts", "response_chars",
    )

    def _mirror_measure_keys(self, sink: object, stats: Mapping) -> None:
        if not isinstance(sink, dict):
            return
        for key in self._MIRRORED_STATS_KEYS:
            value = stats.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                sink[key] = value

    def _forward(
        self, turn: int, frozen: tuple[Mapping[str, str], ...],
        messages: Any, schema_hint: str, kwargs: Mapping[str, Any],
    ) -> None:
        """把**这一轮已经定型的那两条消息与这个 schema_hint 原样**转发一次。

        调用方(反思层)传下来的 kwargs 原样转发,只动两格:

        * `call_stats` 换成驱动器自己的 sink(反思层那一个另外**镜像**三个测量
          键进去,见 `_mirror_measure_keys`),这样这一次调用的读数既进了 E2 的
          行、也让 trace 上那一轮如实带着墙钟;
        * `bypass_cache=True` —— 让「这批数一定不含本地响应缓存出口」成为一条
          **结构事实**而不是一条推理(计划 Q2 对 E1 的同一条理由;E2 不传
          `response_validator`,本地缓存的双门本来就不放行,但 `status="cache_hit"`
          这个第四出口不该有机会污染 E2 的 `status` 分布)。
        """
        from app.domain.cancellation import CoreCancellation

        sink: dict[str, Any] = {}
        forward_kwargs = dict(kwargs)
        caller_sink = forward_kwargs.pop("call_stats", None)
        forward_kwargs["call_stats"] = sink
        forward_kwargs["bypass_cache"] = True
        record = ForwardedTurn(
            turn=turn, messages=frozen, schema_hint=schema_hint, stats=sink)
        self.forwarded = record
        try:
            record.raw = self.real_client.chat_json(
                messages, schema_hint, **forward_kwargs)
        except CoreCancellation:
            raise
        except Exception as exc:  # noqa: BLE001 — 失败是数据,不是崩溃
            record.error = type(exc).__name__
        finally:
            self._mirror_measure_keys(caller_sink, sink)


# --- `model_clients` 代理 -----------------------------------------------------


class ProbeModelClients:
    """E2 与服务端之间的**唯一**接触面:只覆写 `chat("reasoning_agent")`。

    `_construct_reasoning_retriever` 把整个 repository 当 `model_clients` 用,
    同时还从它取 `retrieval` / `collection_catalog` / `collection_enumeration`
    / `settings`。所以这个代理走 `__getattr__` **全量委托**,只在 `chat()` 上
    分一次岔:`reasoning_agent` 给驱动器,其余(embedding / rerank / 合成)原样
    透传给真 repo。

    截住全部 workload 是一条很容易顺手写下的「简化」,而它会静默毁掉这批数据:
    检索必须的 `retrieval_query_embedding` 是 E2 剩下的唯一一处真实模型消耗
    (计划 M3),截掉它之后每一条 run 都在一个空的候选池上跑,四臂的第 k+1 轮
    消息于是全都短得一样、差异消失——而没有任何一条断言会红。用例 (d) 与
    `test_the_proxy_only_intercepts_the_reflect_workload` 守这一格。
    """

    def __init__(self, delegate: Any, driver: ScriptedReflectDriver) -> None:
        self._probe_delegate = delegate
        self._probe_driver = driver
        #: 每一次 `chat(workload_id)` 的 workload,按序留存:「代理只截了那一个」
        #: 是可复核的,不是一句声明。
        self._probe_chat_calls: list[str] = []

    def chat(self, workload_id: str) -> Any:
        self._probe_chat_calls.append(workload_id)
        if workload_id == REFLECT_CHAT_WORKLOAD:
            return self._probe_driver
        return self._probe_delegate.chat(workload_id)

    @property
    def probe_chat_calls(self) -> tuple[str, ...]:
        return tuple(self._probe_chat_calls)

    def __getattr__(self, name: str) -> Any:
        # `_probe_` 前缀的自有属性绝不走委托:`__init__` 赋值之前的一次查找会
        # 在这里递归回自己,而 `AttributeError` 才是那时的正确答案。
        if name.startswith("_probe_"):
            raise AttributeError(name)
        return getattr(self._probe_delegate, name)


# --- 闭集记录面 ---------------------------------------------------------------

#: E2 逐格投影行的**全部**顶层键(Q6 原文)。隐私守卫按它断言
#: `set(row) ⊆` 这个集合,所以往行里加一个 `decision_reason` 原文会直接把用例
#: 打红(与 `reflect_ab.AB_PROJECTION_KEYS` / `reflect_context_bench.CALL_ROW_KEYS`
#: 同一条纪律)。
#:
#: 几格的口径要说清:
#:
#: * `state_point` 是**序号**(0/1/2),不是轮号。§9.2 的报告按初始 / 后续 /
#:   压缩边界三档分组,而轮号在 12 例之间不可比(第三个状态点在不同例上是第 5
#:   或第 6 轮),拿轮号分组会把三档打散成七八档。轮号本身由
#:   `StateProbeCase.state_point_turn` 从 case 集确定性取回,而那份 case 集的
#:   身份由 manifest 的 `case_set_digest` 冻住——没有任何事实因此丢失。
#: * `message_prefix_bytes` **恒 `None`**,见 `PROBE_ROW_ALWAYS_NONE`。
#: * `assessment_rows` / `decision_action` / `decision_sufficient` 读的是**模型
#:   那一份**决定(`ForwardedTurn.raw`),不是 trace 上那一轮的
#:   `assessment_rows`——后者量的是驱动器自己发的停止决定(恒 1 行),把它写进
#:   这一列会让「模型在这个状态点自评了几个方面」变成一个常数。
#: * `context_rebuilds` / `context_fallback` / `delta_blocks` 与五块字符数、
#:   `message_bytes_total` 读的是转发那一轮的 reflect 步 detail:它们描述的正是
#:   **真的发出去的**那两条消息。
PROBE_ROW_KEYS: frozenset[str] = frozenset({
    "case_key", "state_point", "repeat", "arm", "optimization",
    "call_wall_ms", "call_attempts", "response_chars",
    "status", "finish_reason",
    "message_bytes_total", "message_prefix_bytes",
    "ctx_chars_s", "ctx_chars_c", "ctx_chars_k", "ctx_chars_d", "ctx_chars_t",
    "decision_action", "decision_sufficient",
    "assessment_rows", "aspects_total",
    "context_rebuilds", "context_fallback", "delta_blocks",
    "compaction_boundary_reached",
})

#: 在 E2 行上**恒为 `None`** 的键(Q6)。
#:
#: `message_prefix_bytes` 量的是「上一轮 → 这一轮」的公共前缀,而 E2 每条 run
#: 只在**一轮**上调真实模型:它之前那 k 轮的消息虽然也被同一套测量量过,却
#: 一条都没有发出去,拿它当基准算出来的字节数回答的是另一个问题
#: (「如果那一轮发出去过」),不是这条 run 的事实。与
#: `_measure_reflect_messages` 首轮记 `None` 的口径逐字相同
#: (`reasoning_retrieval:546`)。
#:
#: **更不能**拿同 case 同臂内相邻状态点之间的差充当它:那是两条**各自独立**的
#: run,连 provider 侧的会话都不是同一个。`assert_probe_row_closed` 因此把这一格
#: 升级成硬断言——「改成拿相邻状态点做差」这个变异会在写行的那一刻就红,而不是
#: 等到有人读那张表时才发现。
PROBE_ROW_ALWAYS_NONE: tuple[str, ...] = ("message_prefix_bytes",)

#: `call_stats["status"]` 里表示「这次调用没成」的两格(`app/core/llm.py` 的
#: 取消出口与错误出口)。摘要按它单列失败,不混进主统计(§9.2)。
PROBE_FAILED_STATUSES: frozenset[str] = frozenset({"cancelled", "error"})

#: 本地响应缓存那个出口的 `status`。E2 转发时显式 `bypass_cache=True`,所以
#: **结构上**不该出现;真出现了要单列而不是混进成功里——名字里不带 cache_hit
#: (命名红线),叫 `local_cache_exit`。
PROBE_LOCAL_CACHE_EXIT_STATUS = "cache_hit"

#: 成功那一格。
PROBE_OK_STATUS = "ok"

#: 摘要里按中位数报告的数值列。**不产出任何比率型结论**(§9.2 / 计划 §5 风险 4):
#: 这里只有中位数与样本数,没有比值、没有百分比、没有分母。
PROBE_SUMMARY_MEDIAN_KEYS: tuple[str, ...] = (
    "call_wall_ms", "response_chars", "message_bytes_total",
    "ctx_chars_s", "ctx_chars_c", "ctx_chars_k", "ctx_chars_d", "ctx_chars_t",
    "assessment_rows", "delta_blocks",
)

#: 模型这一轮的 `next_action` 读不成短码时落的固定短码。**不是**把模型的自由
#: 文本原样存进去(那就是一段模型可控的文本进了投影行);也不是 `None`
#: ——`None` 已经被「压根解析不出这份载荷」占了,两件事必须分得开。
PROBE_UNREADABLE_ACTION = "unreadable_action"


def _count(raw: object) -> int | None:
    """数值列的取值:非数(缺席 / `None` / 字符串 / `bool`)⇒ unknown,不是 0。

    `bool` 单独挡掉:`isinstance(True, int)` 为真,不挡的话一个写错类型的字段
    会静默落成 `1`。口径与 `reflect_context_bench._count` 逐字相同。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _flag(raw: object) -> bool | None:
    """布尔列的取值:非 `bool` ⇒ unknown。`False` 与 unknown 必须分得开
    ——前者是「量到了,答案是没有」,后者是「没量」。"""
    return raw if isinstance(raw, bool) else None


def _short_code(raw: object) -> str | None:
    """短码列(`status` / `finish_reason` / `decision_action`)的取值。

    只校形状不校词面(词表由写侧 `app/core/llm.py` 拥有,读侧照抄一份只会
    分叉),判据直接问 `assert_projection_values` 本人。非串 / 空串 / 形状不合
    一律 `None` = unknown。

    ⚠ `finish_reason` 有一处口径要读者知道:`llm.py` 的 ok 出口写的是
    `finish_reason or ""`,空**串**意思是「provider 没说」,而这里把空串收成
    `None`,于是「provider 没说」与「压根没有观测」在这一列里同形。与
    `reflect_context_bench._short_code` 同一把尺子,不在 E2 这一侧另开一格。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        assert_projection_values({"_": raw})
    except ValueError:
        return None
    return raw


def assert_probe_row_closed(row: Mapping) -> None:
    """E2 逐格行的形状 + 值 + 恒 `None` 三道自检。rig 写每一行之前调它一次。

    三道各挡一件事:

    1. **闭集** —— 往行里加一个闭集外的键(`decision_reason` 原文是最典型的
       那一个)直接抛;
    2. **值形状** —— 复用 `assert_projection_values`(投影短码的闭集真源),
       所以一段自由文本不管挂在闭集内哪个键下都被拦住;
    3. **恒 `None`** —— `PROBE_ROW_ALWAYS_NONE` 那几格必须缺席或为 `None`,
       见那里的说明。
    """
    extra = set(row) - PROBE_ROW_KEYS
    if extra:
        raise ValueError(
            "state probe row carries keys outside PROBE_ROW_KEYS: "
            + ", ".join(sorted(extra))
        )
    assert_projection_values(row)
    for key in PROBE_ROW_ALWAYS_NONE:
        if row.get(key) is not None:
            raise ValueError(
                f"state probe row key {key!r} must stay None in E2 "
                f"(got {row[key]!r}); see PROBE_ROW_ALWAYS_NONE"
            )


def _decision_facts(forwarded: ForwardedTurn | None) -> dict[str, Any]:
    """模型那一份决定 → 三格数值/短码。**正文不进行。**

    读不出载荷(转发失败、返回的不是 JSON 对象)⇒ 三格全 unknown。读出来了但
    压根没带 `assessment` ⇒ `assessment_rows = 0`:那是一句关于这份载荷的真话
    (「模型这一轮一个方面都没自评」),与「没量到」不是同一件事。
    """
    unknown = {
        "decision_action": None,
        "decision_sufficient": None,
        "assessment_rows": None,
    }
    if forwarded is None or forwarded.raw is None:
        return unknown
    try:
        data = json.loads(forwarded.raw)
    except (ValueError, TypeError):
        return unknown
    if not isinstance(data, Mapping):
        return unknown
    action = _short_code(data.get("next_action"))
    if action is None:
        # 载荷读出来了、`next_action` 却不是短码(空串、一段人话、超长串):
        # 「模型选了个读不出来的动作」与「压根没有载荷」是两件事。
        action = PROBE_UNREADABLE_ACTION
    assessment = data.get("assessment")
    rows = 0
    if isinstance(assessment, Mapping):
        for group in ("supported", "unresolved"):
            values = assessment.get(group)
            if isinstance(values, (list, tuple)):
                rows += len(values)
    return {
        "decision_action": action,
        "decision_sufficient": _flag(data.get("sufficient")),
        "assessment_rows": rows,
    }


def _compaction_boundary_reached(rebuilds: int | None) -> bool | None:
    """`context_rebuilds ≥ 1` ⇒ 这一格真的越过了压缩边界(§9.2 第三个状态点)。

    三值,不是两值:

    * `None` —— 这条臂压根没有这个观测(`context_rebuilds` 只在两条 delta 臂上
      无条件出现,`off` / `prefix_snapshot` 下缺席)。unknown ≠ False。
    * `False` —— 量到了,重建次数是 0:第三个状态点**没有**落在压缩边界之后。
      如实标 False 并在摘要里单列,**不调剧本去凑**(§9.2「缺数据不补造」)。
    * `True` —— 量到了,至少重建过一次。
    """
    if rebuilds is None:
        return None
    return rebuilds >= 1


def build_probe_row(
    *,
    case: StateProbeCase,
    state_point_index: int,
    repeat: int,
    arm: str,
    optimization: str,
    driver: ScriptedReflectDriver,
    reflect_detail: Mapping[str, Any],
    aspects_total: int | None,
) -> dict[str, Any]:
    """转发那一轮的三份事实 → 一行闭集投影。

    三份产地各管一半,不互相代入:

    * **`driver.forwarded.stats`** —— 那一次真实调用的传输读数(墙钟 / 请求数 /
      正文字符数 / `status` / `finish_reason`);
    * **`reflect_detail`** —— 转发那一轮的 reflect 步 detail,即**真的发出去的**
      那两条消息的块长/字节数与 delta 三键;
    * **`driver.forwarded.raw`** —— 模型那一份决定的三格数值/短码
      (`_decision_facts`)。

    行**不**自己算 `case_key` 之外的任何标签:臂、优化档、重复轮由调用方声明,
    与 `reflect_ab` 那条「投影里的 `optimization` 是声明而不是证据」的既有纪律
    同款(证据由 `assert_optimization_matches_evidence` 逐臂当场对号)。
    """
    stats = driver.forwarded.stats if driver.forwarded is not None else {}
    rebuilds = _count(reflect_detail.get("context_rebuilds"))
    row: dict[str, Any] = {
        "case_key": case.case_key,
        "state_point": state_point_index,
        "repeat": repeat,
        "arm": arm,
        "optimization": optimization,
        "call_wall_ms": _count(stats.get("call_wall_ms")),
        "call_attempts": _count(stats.get("attempts")),
        "response_chars": _count(stats.get("response_chars")),
        "status": _short_code(stats.get("status")),
        "finish_reason": _short_code(stats.get("finish_reason")),
        "message_bytes_total": _count(reflect_detail.get("ctx_bytes_total")),
        "message_prefix_bytes": None,
        "ctx_chars_s": _count(reflect_detail.get("ctx_chars_s")),
        "ctx_chars_c": _count(reflect_detail.get("ctx_chars_c")),
        "ctx_chars_k": _count(reflect_detail.get("ctx_chars_k")),
        "ctx_chars_d": _count(reflect_detail.get("ctx_chars_d")),
        "ctx_chars_t": _count(reflect_detail.get("ctx_chars_t")),
        "aspects_total": _count(aspects_total),
        "context_rebuilds": rebuilds,
        "context_fallback": _flag(reflect_detail.get("context_fallback")),
        "delta_blocks": _count(reflect_detail.get("delta_blocks")),
        "compaction_boundary_reached": _compaction_boundary_reached(rebuilds),
        **_decision_facts(driver.forwarded),
    }
    assert_probe_row_closed(row)
    return row


def _median(values: Sequence[int | float]) -> int | float | None:
    """最近秩中位数(偶数条取偏小的那个),**不取两数平均**。

    平均出来的毫秒数不是任何一次真实调用的墙钟。秩的算法与
    `reasoning_trace_stats._prefix_bytes` / `analyze_reasoning_trace._nearest`
    逐字等价(`ceil(0.5n) - 1 == (n - 1) // 2`),三处对同一批数不会给出两个
    「中位数」。
    """
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[(len(ordered) - 1) // 2]


def _bucket_label(state_point: object) -> str | None:
    if isinstance(state_point, bool) or not isinstance(state_point, int):
        return None
    if 0 <= state_point < len(STATE_POINT_LABELS):
        return STATE_POINT_LABELS[state_point]
    return None


def _summarize_cell(rows: Sequence[Mapping]) -> dict[str, Any]:
    """一格(某一档状态点 × 某一条臂)的读数。计数 + 中位数,**零比率**。"""
    statuses = [row.get("status") for row in rows]
    cell: dict[str, Any] = {
        "n_rows": len(rows),
        "n_ok": sum(1 for value in statuses if value == PROBE_OK_STATUS),
        "n_failed": sum(1 for value in statuses if value in PROBE_FAILED_STATUSES),
        "n_local_cache_exit": sum(
            1 for value in statuses if value == PROBE_LOCAL_CACHE_EXIT_STATUS),
        "n_status_unknown": sum(1 for value in statuses if value is None),
        "n_decision_unreadable": sum(
            1 for row in rows if row.get("decision_action") is None),
        "n_boundary_reached": sum(
            1 for row in rows if row.get("compaction_boundary_reached") is True),
        "n_boundary_not_reached": sum(
            1 for row in rows if row.get("compaction_boundary_reached") is False),
        "n_boundary_unknown": sum(
            1 for row in rows
            if row.get("compaction_boundary_reached") is None),
    }
    for key in PROBE_SUMMARY_MEDIAN_KEYS:
        observed = [
            row[key] for row in rows
            if isinstance(row.get(key), (int, float))
            and not isinstance(row.get(key), bool)
        ]
        cell[f"{key}_p50"] = _median(observed)
        cell[f"{key}_n"] = len(observed)
    return cell


def summarize_state_probe(rows: Sequence[Mapping]) -> dict[str, Any]:
    """E2 逐格行 → 分档摘要。**按初始状态 / 后续状态 / 压缩边界三档分别报告。**

    §9.2 原话就是这三档,`STATE_POINT_LABELS` 按状态点序号 0/1/2 对号;序号读不
    出来的行落 `unbucketed`(不猜它属于哪一档)。每档内再按臂分格,因为 E2 唯一
    要比的就是「同一状态点上四条臂差多少」。

    **三类要单列的东西**,一个都不许混进主统计:

    * `failed_rows` —— `status` 落在 `PROBE_FAILED_STATUSES`(取消 / 出错)。
      §9.2:「失败/格式不符单列,不只挑快且成功的请求」;
    * `local_cache_exit_rows` —— `status` 是本地响应缓存那个出口。转发时显式
      `bypass_cache=True`,所以它**结构上**该是 0;不是 0 就说明那条结构事实
      被谁破掉了,这一格是唯一能看见它的地方。键名不含 cache_hit(命名红线);
    * `compaction_boundary_not_reached_rows` —— `compaction_boundary_reached`
      如实为 `False` 的格子。第三个状态点没越过压缩边界是一件要被看见的事,
      不是一件要靠调剧本消灭的事(§9.2「缺数据不补造」)。

    **不产出任何比率型结论**:这份摘要里没有比值、没有百分比、没有分母。四条臂
    之间怎么比,由读表人在 §10 的口径下自己做——E2 的归因边界(P↔B 的差里混着
    指令/工具说明的布局改动;D↔L 在固定状态点上只看得见净增的那一侧)不允许
    这一层替他下结论(计划 §5 风险 5)。
    """
    by_bucket: dict[str, dict[str, list[Mapping]]] = {}
    for row in rows:
        label = _bucket_label(row.get("state_point")) or "unbucketed"
        arm = row.get("arm")
        arm_key = arm if isinstance(arm, str) and arm else "unknown"
        by_bucket.setdefault(label, {}).setdefault(arm_key, []).append(row)
    # 档的顺序按 `STATE_POINT_LABELS`(初始 → 后续 → 压缩边界),不按字母序:
    # 这份摘要是要被人从上往下读的,字母序会把「压缩边界」排到最前面。
    ordered_labels = [
        label for label in (*STATE_POINT_LABELS, "unbucketed")
        if label in by_bucket
    ]
    summary: dict[str, Any] = {
        "rows_total": len(rows),
        "by_state_point": {
            label: {
                arm: _summarize_cell(cell_rows)
                for arm, cell_rows in sorted(by_bucket[label].items())
            }
            for label in ordered_labels
        },
        "failed_rows": sum(
            1 for row in rows if row.get("status") in PROBE_FAILED_STATUSES),
        "local_cache_exit_rows": sum(
            1 for row in rows
            if row.get("status") == PROBE_LOCAL_CACHE_EXIT_STATUS),
        "status_unknown_rows": sum(
            1 for row in rows if row.get("status") is None),
        "compaction_boundary_not_reached_rows": sum(
            1 for row in rows
            if row.get("compaction_boundary_reached") is False),
        "compaction_boundary_unknown_rows": sum(
            1 for row in rows
            if row.get("compaction_boundary_reached") is None),
    }
    return summary


# --- manifest 事实 -----------------------------------------------------------


def state_probe_manifest_facts(
    *,
    cases: Sequence[StateProbeCase],
    arms: Sequence[str],
    repeats: int,
    case_set_digest_code: str,
    optimization_by_arm: Mapping[str, str],
    corpus_signature_by_cell: Mapping[str, str],
    common_baseline: str,
    order: str,
    code_sha: str,
    started_at: str,
    finished_at: str,
    arm_order_seed: object = None,
    model_contract: Mapping[str, Any] | None = None,
    budgets: Mapping[str, Any] | None = None,
    stopped_by_budget: bool = False,
) -> dict[str, Any]:
    """E2 通道的 manifest 事实(**不**写文件、**不**调 `git`)。

    调用方(T-EX7 的 `cmd_state_probe` 收尾)把这份 dict 直接喂给
    `app.eval.reflect_manifest.build_manifest(**facts)`,由那边的四道闸做闭集 /
    值形状 / 通道必填 / `matrix` 子键校验。这里只负责一件事:把 E2 自己知道的
    维度基数算对。

    `matrix` 的四个必填子键是**维度基数**(int),不是分格明细
    (`REQUIRED_MATRIX_KEYS_BY_CHANNEL["e2"]`):`cases × state_points × arms ×
    repeats` 相乘就是这一批的总 run 数,与 dry-run 逐字钉死的规模数同源。

    第五个子键 `planned_runs` 就是那个乘积(T-EX8 质量评审拍板:所有通道一律
    另写这个**额外**子键)。它不是第五个维度,而是把「读者自己做那次乘法」这一
    步写进产物:一份 manifest 与一份 `state-probe-*.jsonl` 摆在一起时,
    「计划跑几格」与「实际出了几行」的对账不该依赖读表人手算四个数的积。基数
    子键一个都不动,所以这条口径与 `REQUIRED_MATRIX_KEYS_BY_CHANNEL` 的注释
    (「分格明细另开子键,不要塞进基数子键本身」)不冲突。

    `state_points` 取 `STATE_POINT_COUNT` 而不是 `len(case.state_points)` 的
    最大值:加载器已经把每例的状态点个数钉成 3,取一个「实测最大值」只会在
    某天有人放宽加载器时悄悄跟着变,而这批数的矩阵形状本该是一个常数。
    """
    if not cases:
        raise ValueError("state probe manifest needs at least one case")
    matrix = {
        "cases": len(cases),
        "state_points": STATE_POINT_COUNT,
        "arms": len(arms),
        "repeats": int(repeats),
    }
    matrix["planned_runs"] = (
        matrix["cases"] * matrix["state_points"]
        * matrix["arms"] * matrix["repeats"]
    )
    facts: dict[str, Any] = {
        "channel": "e2",
        "code_sha": code_sha,
        "arms": list(arms),
        "optimization_by_arm": dict(optimization_by_arm),
        "common_baseline": common_baseline,
        "corpus_signature_by_cell": dict(corpus_signature_by_cell),
        "case_set_digest": case_set_digest_code,
        "arm_order_seed": arm_order_seed,
        "order": order,
        "matrix": matrix,
        "started_at": started_at,
        "finished_at": finished_at,
        # E2 不实施整批墙钟掐停(那是 E3 / T-EX8 的事),所以调用方恒写 False。
        # 键本身任何时候都必须在场(`REQUIRED_KEYS_ALL_CHANNELS`)。
        "stopped_by_budget": bool(stopped_by_budget),
    }
    if model_contract is not None:
        facts["model_contract"] = dict(model_contract)
    if budgets is not None:
        facts["budgets"] = dict(budgets)
    return facts


# --- 冻结意图 -----------------------------------------------------------------


def prepare_frozen_intent(
    contract: Mapping[str, Any], question: str, effort: str,
) -> dict[str, Any]:
    """已确认契约 → `run()` 的三个入参。**零模型调用地推进到状态点的前提之一。**

    * `research_question` —— 合成后的检索问题(`confirmed_research_question`);
    * `intent_queries` —— 首轮种子。**非空时 `run()` 不调 plan 的 LLM**,所以
      驱动器的 plan 分支结构上不可达(计划 M3);
    * `intent_detail` —— `ReasoningIntentProjection.as_json_mapping()`,v2 的
      方面账从它拿 `mandatory_topics`。

    `max_queries` 与生产 Ask 同式 `max(max_initial_subqueries, 1 + 必答方面数)`:
    档位限的是首轮**并发宽度**,不是「哪些必答方面配得到一个种子」;按宽度截会
    在检索器看到它们之前就丢掉靠后的方面。

    ⚠ **登记的一处重复**:这个函数与 `scripts/reflect_shadow_rig.py` 的
    `_prepare_search_intent` 是同一份逻辑的两份实现。**计划对这一格是沉默的**
    ——没有哪句话授权复制,也没有哪句话禁止;这条路是实现自选的,债务在这里
    登记(收敛归 T-EX7,见下)。理由:E2 的编排住在 `app/eval/`
    (纯逻辑、进标准门),而 rig 是 CLI 适配层,方向只能是 rig → 这里;把 E2
    改成 `import scripts.reflect_shadow_rig` 会让实验模块依赖 CLI。收敛的办法
    是 T-EX7 落地时让 rig 的 `_prepare_search_intent` **改调这个函数**(签名与
    返回形状逐格相同,`contract=None` 那半留在 rig 侧),此前两处并存 —— 已写进
    给 T-EX7 的接口清单。
    """
    from app.application.ask_reasoning import ReasoningIntentProjection
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.models.ask import QueryIntentContract
    from app.services.query_intent import (
        confirmed_intent_queries,
        confirmed_research_question,
    )

    frozen = QueryIntentContract(**dict(contract))
    payload = frozen.model_dump()
    # 与 `_prepare_reasoning_ask` / rig 同一个判据:提交了契约、没有待澄清项、
    # **没有提交过澄清答案** ⇒ 用户原文仍是首要权威。加载器已经把 E2 的 case
    # 钉在这一档上(`_assert_intent_contract`),所以这里恒真;判据仍照写,免得
    # 哪天加载器放宽之后这里静默换了权威方向。
    authoritative = (
        not frozen.needs_clarification and not frozen.clarification_answers
    )
    limits = ask_retrieval_limits(effort)
    return {
        "research_question": confirmed_research_question(
            payload, question, objective_is_authoritative=authoritative,
        ),
        "intent_queries": confirmed_intent_queries(
            payload, question, objective_is_authoritative=authoritative,
            max_queries=max(
                limits.max_initial_subqueries, 1 + len(frozen.mandatory_topics)
            ),
        ),
        "intent_detail": ReasoningIntentProjection(
            resolved_question=frozen.resolved_question,
            result_scope=frozen.result_scope,
            completeness_required=frozen.completeness_required,
            retrieval_effort=effort,
            entities=tuple(frozen.entities),
            constraints=tuple(frozen.constraints),
            excluded_topics=tuple(frozen.excluded_topics),
            assumptions=tuple(frozen.assumptions),
            expected_output=frozen.expected_output,
            mandatory_topics=tuple(
                topic.question for topic in frozen.mandatory_topics
            ),
        ).as_json_mapping(),
    }


# --- 一个状态点的编排 ---------------------------------------------------------

#: 「一个检索动作真的执行了」的 trace 步类型闭集。**「不执行」这条验收就是按它
#: 判的**:转发那一轮之后,trace 里不许再有这几种步。
#:
#: 逐格来自 `reasoning_retrieval` 的写点(注意 `search_elements` 落的是
#: `fallback` 而不是 `retrieve`——那是它的历史步名)。
#:
#: 两条守卫各管一半,缺哪一半都会让「不执行」变成一条**只对旧动作成立**的断言:
#:
#: * **覆盖面** —— 这份闭集与「非动作步」那一份合起来必须覆盖服务端全部
#:   `step_type=` 字面量(`test_the_action_step_closed_set_covers_every_trace_
#:   step_type` 拿源码双向对号)。服务端哪天新增一个动作步而这里没跟上,那个
#:   新动作就不在判据里;
#: * **分类** —— 覆盖面只核**并集**,把 `retrieve` 从这半挪到另一半照样过。
#:   所以分类那一半按**动作面真源**对号:一条真 run 里剧本的 `add_subquery` /
#:   `search_elements` 真的被执行,它们当场记下的步名(`retrieve` / `fallback`)
#:   必须落在这一半(`test_the_executed_half_is_anchored_on_what_executing_an_
#:   action_looks_like`)。
EXECUTED_ACTION_STEP_TYPES: frozenset[str] = frozenset({
    "retrieve", "fallback", "search_chunks", "exact_lookup", "enumerate",
    "expand", "expand_community", "follow_chain", "ppr", "consult_memory",
    "outline",
})

#: 不表示「执行了一个检索动作」的 trace 步类型。`skip` 在内:它恰恰是「这一轮
#: 什么都没做」的那条零 I/O 观察。
NON_ACTION_STEP_TYPES: frozenset[str] = frozenset({
    "plan", "reflect", "skip", "answer", "rerank", "profile", "experience",
})


@dataclass
class StateProbePoint:
    """一个 `(case, 状态点, 臂, 重复轮)` 格子的产物。

    `row` 进 `state-probe-<arm>.jsonl`(闭集、零正文);`driver` 与 `result`
    留给调用方做 `.local/raw/` 存档与事后核对——**决定正文只在这里**。
    """

    row: dict[str, Any]
    driver: ScriptedReflectDriver
    result: Any
    prepared: Mapping[str, Any]
    aspects_total: int


def _reflect_steps(result: Any) -> list[Mapping[str, Any]]:
    return [
        dict(getattr(step, "detail", None) or {})
        for step in getattr(result, "trace", ())
        if getattr(step, "step_type", "") == "reflect"
    ]


def assert_no_action_executed_after_forward(result: Any) -> None:
    """**验收的核心断言**:转发那一轮之后,trace 里一个动作步都没有。

    判据是「最后一个 `reflect` 步之后」而不是「最后一步」:转发轮之后 `run()`
    会照常记收尾步(`answer`,可能还有 `rerank`),那些不是动作。落在
    `EXECUTED_ACTION_STEP_TYPES` 里的任何一步出现在那之后,就意味着模型选的
    动作被执行了——那时这批数据不再是 §9.2 说的那件事,整格作废。
    """
    steps = list(getattr(result, "trace", ()))
    last_reflect = -1
    for index, step in enumerate(steps):
        if getattr(step, "step_type", "") == "reflect":
            last_reflect = index
    if last_reflect < 0:
        raise StateProbeError("this run has no reflect step at all")
    offenders = [
        getattr(step, "step_type", "")
        for step in steps[last_reflect + 1:]
        if getattr(step, "step_type", "") in EXECUTED_ACTION_STEP_TYPES
    ]
    if offenders:
        raise StateProbeError(
            "the model's chosen action was executed after the forwarded turn: "
            + ", ".join(offenders)
        )


def _settings_with_case_overrides(settings_for_arm: Any, case: StateProbeCase) -> Any:
    """`settings_for_arm` 的一份**副本** + 这一例的 `settings_overrides`(Q5 第三条)。

    Q5 允许每例带一小组覆盖去**造形态**(`reasoning_max_element_searches=1` 造
    「工具耗尽」),并要求同一 case 的四条臂用**同一份**覆盖。两件事都由这里
    兑现,且必须发生在建检索器**之前**:两个白名单键都是 `self.settings` 的读点
    (`reasoning_max_element_searches` 直读、`reasoning_max_chunk_searches` 经
    `reasoning_action_policy(self.settings)`),检索器一建好就把 settings 存住了。

    **副本而不是原地改**:`settings_for_arm` 是调用方按臂构造的一份配置,同一份
    对象要被这条臂的全部 12 例复用。就地 `setattr` 会让第一个带覆盖的 case 把
    额度永久改小,后面每一例都跑在一个它没声明的额度上——而四臂一致、轮数核、
    「不执行」三道核全部照过,产出的数据看起来完全正常。

    没有覆盖的 case **原样返回**那份臂配置:一次无谓的复制只会多出一个「副本与
    原件哪个才是这条臂」的问题。

    事后两道核,不成立当场 `StateProbeError`:

    * 每一个覆盖键在副本上**真的读出了声明的值** —— `model_copy(update=...)`
      不跑校验器,一个拼错的键会静默变成一个没人读的多余字段(`Settings` 的
      `extra="ignore"`);加载器已经按 `Settings.model_fields` 拒过一次,这里是
      运行期的第二道,守的是「加载器哪天放宽」与「字段改名」;
    * 副本**不是**原件 —— 一个返回 `self` 的 `model_copy` 实现会把上面那条
      「不原地改」的纪律悄悄取消。
    """
    if not case.settings_overrides:
        return settings_for_arm
    overrides = dict(case.settings_overrides)
    copier = getattr(settings_for_arm, "model_copy", None)
    if not callable(copier):
        raise StateProbeError(
            f"{case.case_key}: settings_for_arm "
            f"({type(settings_for_arm).__name__}) has no model_copy(), so this "
            "case's settings_overrides cannot be applied without mutating the "
            "settings this arm shares with every other case"
        )
    effective = copier(update=overrides)
    if effective is settings_for_arm:
        raise StateProbeError(
            f"{case.case_key}: model_copy() returned the arm's own settings "
            "object, so applying this case's settings_overrides would mutate "
            "every other case on this arm"
        )
    for key, value in overrides.items():
        if getattr(effective, key, None) != value:
            raise StateProbeError(
                f"{case.case_key}: settings override {key}={value!r} did not "
                f"take on the copied settings (reads "
                f"{getattr(effective, key, None)!r})"
            )
    return effective


def run_state_probe_point(
    repo: Any,
    settings_for_arm: Any,
    case: StateProbeCase,
    state_point_index: int,
    real_client: Any,
    *,
    notebook_id: Any,
    arm: str,
    optimization: str,
    repeat: int,
    effort: str,
    cancel_event: Any = None,
    actor_id: str = "",
    scope_source_ids: Sequence[str] | None = None,
    on_step: Any = None,
) -> StateProbePoint:
    """跑**一个**状态点:代理 → 检索器 → 冻结意图 → 一条 run → 一行。

    `settings_for_arm` 只需要**这条臂**那一份配置(v2 总闸 / 测量 / 策略位):
    这一例的 `settings_overrides` 由这里在建检索器之前套上一份副本
    (`_settings_with_case_overrides`),调用方**不必也不该**自己先套——两处都套
    不会出错,但「谁负责」这件事只能有一个答案,否则总有一天两份覆盖对不上。

    形状与生产接线的差别只有一处:`model_clients` 换成了 `ProbeModelClients`。
    三层 scope 与生产 Ask 同形(rig 的 `run_search_once` 的逐字复刻),少哪一层
    都不是「少记一点日志」:

    * `model_work_scope(INTERACTIVE)` —— 模型调用的归属与优先级(Ask 是交互档);
    * `retrieval_run(run_kind="ask_reasoning", event_log=None)` —— 请求级的查询
      向量 memo 与扇出预算。**`event_log=None`** 是 Q7 的一半:那个事件汇是这条
      路上唯一会往库里写的旁路,E2 结构上就不该写测试库;
    * `source_scope_context` —— `scope_source_ids` 为空时是个 no-op。

    事后五道核,任何一道不成立当场 `StateProbeError`——**不修行、不兜底**:

    1. 声明的 optimization 与 `reflect_optimization()` 的直接读数逐臂对号
       (复用 `reflect_ab.assert_optimization_matches_evidence`,与
       `run_ab_once` 同一处纪律);
    2. 转发恰好发生**一次**;
    3. plan 的 LLM 一次都没被调到(`driver.plan_calls == 0`)。驱动器的 plan
       分支自己就抛,但那条 `StateProbeError` 会经过
       `query_rewrite.py` 的 `except Exception: return fallback` ——今天
       `BaseException` 让它穿过去,而这道事后核让「plan 被调到」不再只由那一个
       基类选择兜着:轮数核与「不执行」核对一次被洗成 fallback 的 plan 调用
       **三道全过**,那一行看起来与正常行毫无区别,只是这条 run 的意图并没有
       被冻住(计划 M3)、还白烧了一次 plan 预算;
    4. reflect 轮数恰好是 `k + 1`(多一轮说明停止决定没被采用,少一轮说明这条
       run 在到达状态点之前就收尾了——两种情况下这一行都会被归到一个它没跑到的
       状态点上;`_reflect_v2` 的**同轮加预算重试**也在这里露头:那时
       `driver.turns` 数的是尝试次数而 `details` 数的是 reflect 步数,转发因此
       提前一个状态点发生);
    5. 转发那一轮之后 trace 里没有动作步(`assert_no_action_executed_after_forward`)。
    """
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.eval.reflect_ab import assert_optimization_matches_evidence
    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.reasoning_aspects import build_aspect_ledger
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval_run import retrieval_run
    from app.services.source_scope import source_scope_context

    prepared = prepare_frozen_intent(case.intent_contract, case.question, effort)
    if not prepared["intent_queries"]:
        raise StateProbeError(
            f"{case.case_key}: the frozen contract produced no intent queries, "
            "so run() would call the plan LLM (see plan M3)"
        )
    # 方面 id 与方面总数都从**真账本**取,不在这里重写一份 `a1..aN` 的生成规则:
    # 停止决定里那一行自评必须引用一个 `run()` 的账本真的认得的 id,否则它会被
    # 记成 `unknown_aspect`、一格都不落账,收尾闸于是把这一轮折回、run 走到
    # 第 k+2 轮。账本按 `intent_detail`(而不是契约原文)建,与 `run()` 里
    # `_v2_build_aspect_ledger` 读的是同一份东西。
    ledger = build_aspect_ledger(prepared["intent_detail"], case.question)
    aspect_ids = [record.aspect_id for record in ledger.snapshot()]
    if not aspect_ids:
        raise StateProbeError(
            f"{case.case_key}: the frozen contract yields an empty aspect ledger")

    driver = ScriptedReflectDriver(
        case, state_point_index, real_client, stop_aspect_id=aspect_ids[0])
    proxy = ProbeModelClients(repo, driver)
    retriever = ReasoningRetriever.from_repository(
        proxy, _settings_with_case_overrides(settings_for_arm, case),
        cancel_event)
    assert_optimization_matches_evidence(
        optimization, retriever.reflect_optimization())

    question = prepared["research_question"] or case.question
    scope = (
        {"mode": "include", "source_ids": list(scope_source_ids)}
        if scope_source_ids else None
    )
    with model_work_scope(
        priority=ModelPriority.INTERACTIVE, actor_id=actor_id,
        notebook_id=notebook_id, question=question,
    ):
        with retrieval_run(
            run_kind="ask_reasoning", event_log=None, actor_id=actor_id,
            cancel_event=cancel_event,
        ):
            with source_scope_context(notebook_id, scope, None):
                result = retriever.run(
                    notebook_id, question, "", on_step=on_step,
                    intent_queries=list(prepared["intent_queries"]),
                    limits=ask_retrieval_limits(effort),
                    intent_detail=prepared["intent_detail"],
                )

    if driver.forward_count != 1:
        raise StateProbeError(
            f"{case.case_key} state point {state_point_index}: the real client "
            f"was forwarded {driver.forward_count} time(s), expected exactly 1 "
            f"(reflect turns observed: {driver.turns})"
        )
    if driver.plan_calls:
        # 驱动器的 plan 分支自己就抛,可那条异常要穿过一整条 fail-open 的链才
        # 回到这里(`query_rewrite.py` 的 `except Exception: return fallback`
        # 是其中一处)。`StateProbeError` 继承 `BaseException` 正是为此,而这道
        # 事后核让这件事不再只由那一个基类选择兜着:窄一格就静默产出一行。
        raise StateProbeError(
            f"{case.case_key} state point {state_point_index}: the plan LLM was "
            f"called {driver.plan_calls} time(s), so this run's intent was not "
            "frozen (see plan M3)"
        )
    details = _reflect_steps(result)
    expected_turns = case.state_point_turn(state_point_index) + 1
    if len(details) != expected_turns:
        raise StateProbeError(
            f"{case.case_key} state point {state_point_index}: this run has "
            f"{len(details)} reflect step(s), expected {expected_turns}"
        )
    assert_no_action_executed_after_forward(result)

    row = build_probe_row(
        case=case, state_point_index=state_point_index, repeat=repeat,
        arm=arm, optimization=optimization, driver=driver,
        reflect_detail=details[-1], aspects_total=len(aspect_ids),
    )
    return StateProbePoint(
        row=row, driver=driver, result=result, prepared=prepared,
        aspects_total=len(aspect_ids),
    )
