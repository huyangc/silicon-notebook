"""E2(固定状态真实决策)的驱动器、代理与 12 例 case 集。

计划真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
§2 Q5/Q6 与 §3 T-EX5 / T-EX6;设计真源
`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md` §9.2。

本文件两节,各守自己的那一半:

* **T-EX6 · 形态覆盖对账 + 逐例形状校验**(下面第一节)刻意**不**经过 T-EX5 的
  `load_state_probe_cases`。那个加载器是「畸形当场响亮失败」的实现,拿它来对账
  等于让被测者自己出考题——加载器哪天把一条约束松掉,这一节照样绿。所以这一节
  用自己的 `_load_cases_raw()` 直接读 JSON,判据逐条从计划的 Q5/T-EX6 抄下来。
* **T-EX5 · 驱动器 / 代理 / 记录面**(第二节,`--- T-EX5 ---` 之后)测
  `app.eval.reflect_state_probe`,全部用进程内 fake、零真实模型。「12 例过校验
  器」这一条在那一节里**真调** `load_state_probe_cases`(汇合时约定),而形态
  对账仍走 `_load_cases_raw()`——两条判据同源、互不 import。

## 汇合义务(T-EX6 评审留档,T-EX5 落地时已逐条对齐)

1. `state_probes.json` 每例的两个新键 `question_key` / `probe_shape`
   —— 落在 `reflect_state_probe.REQUIRED_CASE_KEYS`;
2. 剧本步的可选键 `assessment`(值形状见 `test_each_case_assessment_only_references_its_own_aspects`)
   —— 落在 `reflect_state_probe.SCRIPT_STEP_OPTIONAL_KEYS`;
3. `settings_overrides` 白名单收窄到两个检索上限键
   (`reasoning_max_element_searches` / `reasoning_max_chunk_searches`),
   渲染预算键(`reasoning_reflect_state_chars` /
   `reasoning_reflect_evidence_chars_by_effort`)任何 case 都不得覆盖
   —— 落在 `SETTINGS_OVERRIDE_WHITELIST` / `FORBIDDEN_OVERRIDE_KEYS`;
4. `zero_hit` 形态的哨兵前缀 `absent_probe.` —— 仍只由本文件的形态对账守
   (加载器不认形态语义,它只认闭集与结构);
5. `STATE_PROBES_PATH` 归 `app.eval.reflect_t0` 包导出,与 `QUESTIONS_PATH`
   同处 —— `load_state_probe_case_set()` 读的就是它,没有第二份路径常量。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.eval.reflect_t0 import QUESTIONS_PATH, STATE_PROBES_PATH, load_questions

#: 期望的例数(计划 T-EX6 的形态表)。写死是判据的一半:少一例的 case 集照样
#: 能过「每种形态至少一例」,而 §9.2 要的是 12 例。
EXPECTED_CASE_COUNT = 12

#: 形态 → 期望例数。九种形态、12 例,逐格与计划 T-EX6 的表对齐。
EXPECTED_SHAPE_COUNTS: dict[str, int] = {
    "single_fact": 2,
    "complex_condition": 2,
    "graph_in_scope": 1,
    "no_graph": 1,
    "roster": 1,
    "large_collection": 1,
    "zero_hit": 1,
    "repeat_request": 1,
    "tool_exhausted": 2,
}

#: 剧本里允许出现的动作(Q5 第一条:只用查询串型参数)。
ALLOWED_SCRIPT_ACTIONS = frozenset({
    "search_elements", "search_chunks", "add_subquery",
    "enumerate_elements", "enumerate_kg_objects", "exact_lookup",
})

#: 明令不进本期剧本的三个动作:它们的必填参数是候选池里的 `object_id`。
FORBIDDEN_SCRIPT_ACTIONS = frozenset({
    "expand_graph", "follow_chain", "ppr_retrieve",
})

#: 任何一层的键名里都不许出现的 id 槽位。`source_title` 也在内:B 语料的文件名
#: 带内部 `src-` 前缀,抄进 fixture 就等于把一个 id 签进仓库。
FORBIDDEN_KEYS = frozenset({
    "object_id", "source_id", "source_ids", "source_title",
    "expand_object_id", "start_object_id", "target_object_id",
    "notebook_id", "notebook", "chunk_id", "element_id", "id_",
})

#: 值里不许出现的 id / 连接串前缀。三个 id 前缀是仓库里真实在用的
#: (`nb-…` / `src-…` / `ko-…`),后面几个挡住凭据与生产 URL(§8.2)。
FORBIDDEN_VALUE_FRAGMENTS = (
    "nb-", "src-", "ko-", "postgresql://", "postgres://",
    "http://", "https://",
)

#: `settings_overrides` 的白名单(Q5 第三条)。字段名逐字来自
#: `app.core.config.Settings`,拼错会被下面的用例当场抓住。
#:
#: 只收两个**检索上限**键——Q5 只点名三种造形态用法(元素额度=1 / 同查询两轮 /
#: 必然零命中),从未授权动渲染预算旋钮。渲染预算键单独关进
#: `FORBIDDEN_OVERRIDE_KEYS`,两处判据独立存在:白名单万一哪天被放宽,下面那条
#: 显式断言仍单独兜底。
SETTINGS_OVERRIDE_WHITELIST = frozenset({
    "reasoning_max_element_searches",
    "reasoning_max_chunk_searches",
})

#: 渲染预算键——它们是四臂(P/D/L 与既有 legacy)对照的默认前提。任何 case 都
#: 不许覆盖它们:调小它们去逼出压缩边界,与调剧本去凑是同一件事的两种写法,
#: 还会让被改的那一例跑在与其余十一例不可比的预算上(§9.2「缺数据不补造」)。
FORBIDDEN_OVERRIDE_KEYS = frozenset({
    "reasoning_reflect_state_chars",
    "reasoning_reflect_evidence_chars_by_effort",
})

#: 语料格闭集。E2 只用这两格:「有图 / 无图」由格承载而不由图动作承载
#: (Q5 的实现口径收窄)。
ALLOWED_CORPUS_CELLS = frozenset({"A_nokg", "B_kg"})

#: 每例必备的键,与可选键分开列:多一个闭集外的键要被看见(投影闭集的同一条
#: 纪律),少一个必填键更要。
REQUIRED_CASE_KEYS = frozenset({
    "case_key", "probe_shape", "corpus_cell", "question_key", "question",
    "intent_contract", "script", "state_points",
})
OPTIONAL_CASE_KEYS = frozenset({"settings_overrides"})

#: `zero_hit` 形态的哨兵前缀。`state_probes.json` 的 note 里逐字声明过它:
#: 公开语料里不存在这个名称,所以那一次 `exact_lookup` 必然零命中。
ZERO_HIT_SENTINEL = "absent_probe."


def _load_cases_raw() -> list[dict]:
    """直接读 case 集的 JSON。**不经过** T-EX5 的加载器(见模块 docstring)。"""
    raw = json.loads(STATE_PROBES_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "顶层必须是对象"
    assert raw["version"] == 1
    cases = raw["cases"]
    assert isinstance(cases, list)
    return cases


def _cases_by_key() -> dict[str, dict]:
    cases = _load_cases_raw()
    by_key = {case["case_key"]: case for case in cases}
    assert len(by_key) == len(cases), "case_key 撞了"
    return by_key


def _cases_by_shape(shape: str) -> list[dict]:
    return [case for case in _load_cases_raw()
            if case["probe_shape"] == shape]


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """深度遍历一个 JSON 值,产出 `(路径, 叶子值)`。键名也当叶子产出一次,
    这样「键里带 id」与「值里带 id」用同一把尺子量。"""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            yield child, key
            yield from _walk(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")
    else:
        yield path, node


# --- (a) 形态覆盖对账 --------------------------------------------------------


def test_the_case_set_has_exactly_the_twelve_planned_cases():
    """12 例、九种形态逐格对上计划 T-EX6 的表。

    变异:删掉一例(或把两例的 `probe_shape` 改成同一个)⇒ 这条红。只断言
    「每种形态至少一例」的话,一个 9 例的 case 集照样绿,而 §9.2 要的矩阵是
    12 例 × 3 状态点 × 2 重复 × 3 臂 = 216 次逻辑调用。
    """
    cases = _load_cases_raw()
    assert len(cases) == EXPECTED_CASE_COUNT
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["probe_shape"]] = counts.get(case["probe_shape"], 0) + 1
    assert counts == EXPECTED_SHAPE_COUNTS


@pytest.mark.parametrize("shape", sorted(EXPECTED_SHAPE_COUNTS))
def test_every_planned_shape_has_at_least_one_case(shape: str):
    """§9.2 点名的九种形态逐格有例。与上面那条**不是**重复:那条钉总数与分布,
    这条是逐形态参数化的,红的时候直接说出是哪一种形态丢了。"""
    assert _cases_by_shape(shape), shape


def test_single_fact_cases_carry_exactly_one_mandatory_aspect():
    """单事实 = 契约只有一个必答方面、且是 ranked 档。

    方面数直接决定 v2 的方面块字节与 lean 自评段字节(计划 M3),所以「单事实」
    这个形态在 E2 里的可观测承载就是它。
    """
    for case in _cases_by_shape("single_fact"):
        contract = case["intent_contract"]
        assert len(contract["mandatory_topics"]) == 1, case["case_key"]
        assert contract["result_scope"] == "ranked", case["case_key"]
        assert not contract.get("constraints"), case["case_key"]


def test_complex_condition_cases_carry_three_aspects_and_constraints():
    """复杂条件 = 三个必答方面 + 非空 `constraints`(计划 T-EX6 的表)。

    变异:把 `constraints` 清空 ⇒ 红。冻结约束是不可压缩区的一部分,没有它这一
    例与「三方面的单事实题」在渲染上没有区别。
    """
    cases = _cases_by_shape("complex_condition")
    assert len(cases) == 2
    for case in cases:
        contract = case["intent_contract"]
        assert len(contract["mandatory_topics"]) == 3, case["case_key"]
        assert contract["constraints"], case["case_key"]
        assert all(isinstance(item, str) and item.strip()
                   for item in contract["constraints"]), case["case_key"]


def test_the_graph_shape_is_carried_by_the_corpus_cell_not_by_a_graph_action():
    """有图 / 无图由**语料格**承载(Q5 的实现口径收窄)。

    有图例落 `B_kg` 且剧本里一个图动作都没有;无图例落 `A_nokg`。这一条是那次
    收窄的守卫:哪天有人「顺手」把 `expand_graph` 加回有图例的剧本,它就红——
    而那个动作的必填参数是候选池里的 `object_id`,fixture 里根本给不出。
    """
    graph_cases = _cases_by_shape("graph_in_scope")
    assert len(graph_cases) == 1
    for case in graph_cases:
        assert case["corpus_cell"] == "B_kg", case["case_key"]
        actions = {step["next_action"] for step in case["script"]}
        assert not (actions & FORBIDDEN_SCRIPT_ACTIONS), case["case_key"]

    nograph_cases = _cases_by_shape("no_graph")
    assert len(nograph_cases) == 1
    for case in nograph_cases:
        assert case["corpus_cell"] == "A_nokg", case["case_key"]


def test_the_roster_shape_really_enumerates_the_source_collection():
    """目录 ⇒ 剧本里真的有 `enumerate_elements`,且是文档目录本身那一档。

    只断言形态标签不断言动作,等于允许一个「标着 roster、其实只在做向量检索」
    的例子混进 case 集(A/B §3.4 覆盖对账的原意)。
    """
    cases = _cases_by_shape("roster")
    assert len(cases) == 1
    for case in cases:
        rosters = [
            step for step in case["script"]
            if step["next_action"] == "enumerate_elements"
        ]
        assert rosters, case["case_key"]
        assert any(step["arguments"].get("collection") == "sources"
                   for step in rosters), case["case_key"]


def test_the_large_collection_shape_enumerates_kg_objects_across_the_scope():
    """大集合 ⇒ `enumerate_kg_objects` + `scope="all"`。

    `scope` 是身份串的一部分(`v2_request_identity`),`all` 那一档才是「整个
    检索范围」——它是唯一有机会顶到 PR-1 规模守卫(`oversize_listing`)的那一
    档。**是否真的触发守卫不由 fixture 断言**:那个判据读的是集合地图算出来的
    计数与本轮剩余额度,是运行期事实,冻在 fixture 里就成了编造。
    """
    cases = _cases_by_shape("large_collection")
    assert len(cases) == 1
    for case in cases:
        steps = [
            step for step in case["script"]
            if step["next_action"] == "enumerate_kg_objects"
        ]
        assert steps, case["case_key"]
        assert any(step["arguments"].get("scope") == "all"
                   for step in steps), case["case_key"]
        assert case["corpus_cell"] == "B_kg", case["case_key"]


def test_the_failure_shape_uses_a_query_that_cannot_hit():
    """失败 ⇒ 剧本里有一次**必然零命中**的请求。

    承载它的是 `exact_lookup`:那是词法精确探测,一个公开语料里不存在的名称
    必然拿回零行。向量检索做不到这件事——它总会回最近邻,「零命中」在那条通道
    上不可构造。
    """
    cases = _cases_by_shape("zero_hit")
    assert len(cases) == 1
    for case in cases:
        terms = [
            step["arguments"].get("term", "")
            for step in case["script"]
            if step["next_action"] == "exact_lookup"
        ]
        assert terms, case["case_key"]
        assert any(term.startswith(ZERO_HIT_SENTINEL)
                   for term in terms), case["case_key"]


def test_the_repeat_shape_issues_the_same_request_two_turns_running():
    """重复 ⇒ 同一动作、同一参数,**连着两轮**。

    「连着」是判据的一半:同一条查询隔了三轮再来一次,在观察账上是两条普通行;
    紧邻两轮才让重复本身成为模型这一轮要读的状态。
    """
    cases = _cases_by_shape("repeat_request")
    assert len(cases) == 1
    for case in cases:
        script = case["script"]
        pairs = [
            (first, second)
            for first, second in zip(script, script[1:])
            if (first["next_action"], first["arguments"])
            == (second["next_action"], second["arguments"])
        ]
        assert pairs, case["case_key"]


def test_the_tool_exhausted_shape_pairs_a_budget_of_one_with_a_hard_pick():
    """工具耗尽 ⇒ `reasoning_max_element_searches=1` **且**剧本硬选它两次以上。

    两半都要:只把额度调成 1 而剧本从不再选它,模型永远看不到「这条路已经走不
    通」;只让剧本重复选而不调额度,默认 5 次额度下前两次都会正常执行。
    """
    cases = _cases_by_shape("tool_exhausted")
    assert len(cases) == 2
    for case in cases:
        overrides = case.get("settings_overrides") or {}
        assert overrides.get("reasoning_max_element_searches") == 1, \
            case["case_key"]
        picks = [step for step in case["script"]
                 if step["next_action"] == "search_elements"]
        assert len(picks) >= 2, case["case_key"]


def test_the_two_corpus_cells_are_both_represented_per_paired_shape():
    """形态表里标了 A/B 两格的三种形态,两格各一例。

    单事实 / 复杂条件 / 工具耗尽这三种各两例,一例 `A_nokg` 一例 `B_kg`:同一
    形态在有图与无图两侧各测一次,是「有图/无图由格承载」这条收窄的另一半。
    """
    for shape in ("single_fact", "complex_condition", "tool_exhausted"):
        cells = sorted(case["corpus_cell"] for case in _cases_by_shape(shape))
        assert cells == ["A_nokg", "B_kg"], shape


# --- (b) 12 例逐例形状校验 ---------------------------------------------------


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_has_exactly_the_declared_keys(case_key: str):
    """必填键齐、可选键只能是白名单里那一个、闭集外的键一律拒。"""
    case = _cases_by_key()[case_key]
    keys = set(case)
    assert REQUIRED_CASE_KEYS <= keys, REQUIRED_CASE_KEYS - keys
    assert keys <= REQUIRED_CASE_KEYS | OPTIONAL_CASE_KEYS, \
        keys - (REQUIRED_CASE_KEYS | OPTIONAL_CASE_KEYS)


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_is_free_of_database_ids(case_key: str):
    """Q5 第一条:**一例都不含数据库 id**。

    键名与叶子值用同一把尺子量:一个 `{"object_id": "…"}` 会被键名那一半抓住,
    一个把 id 塞进 `query` 字符串的写法会被值那一半抓住。

    变异(计划 T-EX6 的验收):往任意一例的某个动作参数里塞一个 `object_id`
    ⇒ 这条红(`test_planting_an_object_id_in_a_case_is_caught` 就是那次变异的
    留档,它拿一份改坏的副本走同一把判据)。
    """
    for path, value in _walk(_cases_by_key()[case_key]):
        assert value not in FORBIDDEN_KEYS, f"{case_key}: {path}"
        if isinstance(value, str):
            lowered = value.lower()
            for fragment in FORBIDDEN_VALUE_FRAGMENTS:
                assert fragment not in lowered, f"{case_key}: {path} = {value!r}"


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_script_only_uses_query_string_actions(case_key: str):
    """剧本只用查询串型参数的动作,三个 id 动作一次都不出现。"""
    case = _cases_by_key()[case_key]
    assert case["script"], case_key
    for index, step in enumerate(case["script"], 1):
        action = step["next_action"]
        assert action in ALLOWED_SCRIPT_ACTIONS, f"{case_key} 第 {index} 轮"
        assert action not in FORBIDDEN_SCRIPT_ACTIONS, f"{case_key} 第 {index} 轮"
        # 检索动作与 `sufficient=true` 不能同轮成立(`parse_reflect_v2` 的
        # `_V2_SUFFICIENT_CONTRADICTION`):剧本里每一轮都是检索,所以恒 false。
        assert step["sufficient"] is False, f"{case_key} 第 {index} 轮"
        assert isinstance(step["arguments"], dict), f"{case_key} 第 {index} 轮"


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_freezes_a_usable_intent_contract(case_key: str):
    """Q5 第二条:每例冻一份 `QueryIntentContract`,`mandatory_topics` ≥ 1。

    契约用真模型校验一次(不是自己再写一份形状判据):它就是 rig
    `_prepare_search_intent` 里 `QueryIntentContract(**contract)` 那一行会吃到
    的东西。非空契约 ⇒ `run()` 拿到非空 `intent_queries` ⇒ 零意图调用。
    """
    from app.models.ask import QueryIntentContract

    case = _cases_by_key()[case_key]
    contract = QueryIntentContract(**case["intent_contract"])
    assert contract.mandatory_topics, case_key
    assert contract.objective.strip(), case_key
    assert contract.resolved_question.strip(), case_key
    # 代答过澄清项的题,权威方向会从用户原文换成合成后的 `resolved_question`
    # (`_prepare_search_intent` 的 `authoritative` 判据)。E2 要的是冻结的
    # 「已确认且本来就没歧义」那一档,所以这两格必须是空的。
    assert contract.needs_clarification is False, case_key
    assert contract.clarification_answers == [], case_key


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_settings_override_stays_inside_the_whitelist(case_key: str):
    """Q5 第三条:覆盖只许落在白名单两个检索上限键上,而且必须是 `Settings`
    真有的字段。

    白名单是字面量,`Settings` 是真源——两边对不上时(比如哪天字段改名)这条
    红,而不是让一份永远不生效的覆盖静静躺在 fixture 里。
    """
    from app.core.config import Settings

    overrides = _cases_by_key()[case_key].get("settings_overrides")
    if overrides is None:
        return
    assert isinstance(overrides, dict) and overrides, case_key
    for key in overrides:
        assert key in SETTINGS_OVERRIDE_WHITELIST, f"{case_key}: {key}"
        assert key in Settings.model_fields, f"{case_key}: {key}"


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_no_case_overrides_the_rendering_budget_keys(case_key: str):
    """独立于白名单的显式断言:即便白名单哪天被放宽,这条红线单独兜底。

    变异:给 `sf-a-gsm8k` 加 `{"reasoning_reflect_state_chars": 200}`(企图借
    调小渲染预算逼出 `context_rebuilds` 而不是让剧本自然跑到压缩边界)⇒ 这条
    红——这正是三处收窄之一被否掉的那一招(§9.2「缺数据不补造」)。
    """
    overrides = _cases_by_key()[case_key].get("settings_overrides") or {}
    hit = set(overrides) & FORBIDDEN_OVERRIDE_KEYS
    assert not hit, (case_key, hit)


def test_the_override_whitelist_matches_the_settings_fields_it_names():
    """白名单本身逐格对上 `Settings`。上一条只在有覆盖的例子上跑,一个空覆盖的
    case 集会让白名单里的拼写错误一直没人发现。"""
    from app.core.config import Settings

    for key in SETTINGS_OVERRIDE_WHITELIST:
        assert key in Settings.model_fields, key


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_state_points_are_three_strictly_increasing_turns(
    case_key: str,
):
    """三个状态点、严格单调递增、每个都是 ≥ 1 的整数、且 ≤ 剧本长度。

    「≤ 剧本长度」是越界拒:第 k 个状态点要求剧本能把前 k 轮全部重放出来,一个
    指向剧本之外的轮号只能靠兜底编一轮观察,那正是 §9.2 不许的「补造」。
    """
    points = _cases_by_key()[case_key]["state_points"]
    assert isinstance(points, list) and len(points) == 3, case_key
    assert all(isinstance(p, int) and not isinstance(p, bool) and p >= 1
               for p in points), case_key
    assert points == sorted(points) and len(set(points)) == 3, case_key
    assert points[-1] <= len(_cases_by_key()[case_key]["script"]), case_key


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_corpus_cell_is_one_of_the_two_e2_cells(case_key: str):
    assert _cases_by_key()[case_key]["corpus_cell"] in ALLOWED_CORPUS_CELLS


def test_planting_an_object_id_in_a_case_is_caught():
    """变异留档:把一例改坏(塞一个 `object_id`)⇒ 无 id 判据必须红。

    改坏的是一份**内存里的副本**,不是磁盘上的 fixture —— 这条用例要留证据说
    「那把判据真的会响」,而不是靠人手改一次文件、看一眼红、再改回去。
    """
    case = json.loads(json.dumps(_cases_by_key()["gr-b-mla-vs-kivi"]))
    case["script"][0]["arguments"]["object_id"] = "ko-deadbeef01"

    hits = [
        (path, value) for path, value in _walk(case)
        if value in FORBIDDEN_KEYS
        or (isinstance(value, str)
            and any(fragment in value.lower()
                    for fragment in FORBIDDEN_VALUE_FRAGMENTS))
    ]
    # 两半都该响:键名 `object_id` 与值 `ko-…` 各中一次。
    assert len(hits) == 2, hits
    assert {value for _, value in hits} == {"object_id", "ko-deadbeef01"}


# --- (c) 剧本长度 ≥ 最大状态点 + 1 -------------------------------------------


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_script_outlasts_its_last_state_point_by_one_turn(
    case_key: str,
):
    """剧本长度 ≥ 最大状态点轮号 + 1。

    第 k 个状态点的跑法是「剧本推进第 1..k 轮,第 k+1 轮转发给真客户端」
    (计划 M3)。所以第 k+1 轮必须**在剧本的覆盖范围之内是多余的、但在轮数预算
    之内是允许的**:剧本给不出第 k+1 轮时,驱动器只能在那一轮兜底,而那一轮恰好
    是唯一要测的真实决策轮。

    另一头由 `standard` / `deep` 两档的 `max_reasoning_steps=8` 封顶:第 k+1 轮
    必须还在预算里,否则那次调用根本不会发生。
    """
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    case = _cases_by_key()[case_key]
    last = max(case["state_points"])
    assert len(case["script"]) >= last + 1, (
        case_key, len(case["script"]), case["state_points"])
    budget = min(ask_retrieval_limits(effort).max_reasoning_steps
                 for effort in ("standard", "deep"))
    assert last + 1 <= budget, (case_key, last, budget)


# --- (d) 与 questions.json 的交叉引用 ----------------------------------------


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_question_key_resolves_in_the_frozen_question_set(
    case_key: str,
):
    """`question_key` 必须是 `questions.json` 的 ask 题号,题面**逐字**相同,
    而且 `corpus_cell` 的语料字母与那道题所属的语料一致。

    三件事一起断言,因为它们守的是同一句话:题面与语料沿用既有 34 题,不另起
    一套(§11「复用现有 A/B 闭集投影与题集」)。逐字比题面是那一条里最容易被
    绕过的一半 —— 一个「差不多」的改写既拿不到那道题的 gold,也让两份 fixture
    从此各说各话。语料字母的判据与 rig 的 `cell.split("_", 1)[0]` 同一条。
    """
    case = _cases_by_key()[case_key]
    by_key = {row["key"]: row for row in load_questions()["ask"]}
    question = by_key.get(case["question_key"])
    assert question is not None, (case_key, case["question_key"])
    assert case["question"] == question["zh"], case_key
    assert case["corpus_cell"].split("_", 1)[0] == question["corpus"], case_key


def test_the_case_set_spreads_over_distinct_questions():
    """12 例用 12 道**不同**的题。

    同一道题在两例里出现,会让「形态」与「题目」这两维在分析时混在一起:那两例
    的差值既可以读成形态的作用,也可以读成同一道题的两次重复。
    """
    keys = [case["question_key"] for case in _load_cases_raw()]
    assert len(set(keys)) == len(keys) == EXPECTED_CASE_COUNT


def test_the_case_set_ships_next_to_the_question_set_it_references():
    """两份 fixture 同目录、同一个包。`state_probes.json` 的路径由
    `QUESTIONS_PATH` 派生而不是自己再拼一遍 —— 题集搬家时 `.exists()` 会先红,
    而不是留下一个指向空气的常量。

    「同目录」这一半独立求值:从 `app.eval.reflect_t0` 包自己的 `__file__` 重算
    一遍包目录再比,不是拿 `STATE_PROBES_PATH.parent == QUESTIONS_PATH.parent`
    ——那条式子由 `STATE_PROBES_PATH = QUESTIONS_PATH.parent / "state_probes.json"`
    的定义本身保证恒成立,是个不可能变红的重言式。
    """
    import app.eval.reflect_t0 as reflect_t0_package

    assert STATE_PROBES_PATH.exists()
    assert STATE_PROBES_PATH.name == "state_probes.json"
    package_dir = Path(reflect_t0_package.__file__).resolve().parent
    assert STATE_PROBES_PATH.parent == package_dir, STATE_PROBES_PATH
    assert QUESTIONS_PATH.parent == package_dir, QUESTIONS_PATH


# --- (e) 剧本步真的解析成它声明的那个动作 -------------------------------------


#: 剧本步的键闭集,按 `parse_reflect_v2` 实际读的键定(`app.services.
#: reasoning_retrieval.parse_reflect_v2`):`next_action`/`sufficient`/
#: `arguments` 是必填,`reason`/`assessment` 是它认但可以缺省的两个可选键。
#: 闭集外的键(比如拼错的 `assesment`)不会报错——它只是被 `dict.get` 静默
#: 忽略——所以形状判据要在 fixture 这一侧堵。
SCRIPT_STEP_REQUIRED_KEYS = frozenset({"next_action", "sufficient", "arguments"})
SCRIPT_STEP_OPTIONAL_KEYS = frozenset({"reason", "assessment"})


#: 剧本动作 → 它在 `_reflect_capabilities` 里真实扣减的那个 `*_left` 字段
#: (`app.services.reasoning_retrieval._reflect_capabilities`:
#: `max(0, 上限 - 已用)`)。`add_subquery` 不在这张表里——它的可用性只看通道
#: 接线(图在范围内,或 `chunk_search_active`),不消耗任何配额
#: (`build_reflect_capabilities` 的 `ADD_SUBQUERY_ACTION` 判据)。枚举的两个
#: 动作合用同一个 `enum_pages_left` 池(per-RUN 共享,`ask_retrieval_policy`
#: 的文档字符串)。
_QUOTA_FIELD_BY_ACTION: dict[str, str] = {
    "search_elements": "element_searches_left",
    "search_chunks": "chunk_searches_left",
    "exact_lookup": "exact_lookups_left",
    "enumerate_elements": "enum_pages_left",
    "enumerate_kg_objects": "enum_pages_left",
}

#: 生产默认额度,与 `test_reasoning_retrieval._full_house_facts` 同一组数:
#: `Settings` 的 `reasoning_max_element_searches` / `reasoning_max_chunk_searches`
#: / `reasoning_max_exact_lookups`,加 `ask_retrieval_limits("standard")` 的
#: `enum_pages_per_run`。只列剧本会真的碰到的三个检索上限 + 一个枚举页池——
#: ppr / follow_chain / consult / outline / enum_rows / enum_payload 这份
#: case 集里没有一步用得到它们把着的动作(`expand_graph` / `follow_chain` /
#: `ppr_retrieve` 明令不进剧本,`consult_memory` / `update_outline` 不是
#: reflect 的检索动作),给多大都不改变任何一例的判定,仍按满额度填。
_PRODUCTION_CAPABILITY_QUOTAS: dict[str, int] = {
    "element_searches_left": 5,
    "chunk_searches_left": 3,
    "exact_lookups_left": 3,
    "enum_pages_left": 4,
}


def _capability_facts_before_turn(
    case: dict, turn_index: int,
) -> "ReflectCapabilityFacts":
    """第 `turn_index` 轮(1-based)决策前的能力事实。

    生产默认额度按 `case["script"][:turn_index - 1]` 里同类动作出现的**次数**
    跨轮累加消耗——与生产 `_reflect_capabilities` 同一条减法
    (`max(0, 上限 - 已用)`),不是满额度不扣减。`settings_overrides` 里的
    `reasoning_max_element_searches` / `reasoning_max_chunk_searches` 覆盖对应
    的**上限**而不是剩余量,与 `Settings` 的覆盖语义一致。

    按语料格(`corpus_cell == "B_kg"`)切 `kg_in_scope`,其余通道全部接通、
    `has_candidates=True`,让每一步唯一可能不可用的原因就是配额耗尽——这一节
    只回答"标签对了、剧本每一步是否也解析成模型该看到的那个动作,以及会不会
    在本 run 没钉住的那几个 per-run 池子里撞上配额上限"。
    """
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.reasoning_actions import ReflectCapabilityFacts

    overrides = case.get("settings_overrides") or {}
    quotas = dict(_PRODUCTION_CAPABILITY_QUOTAS)
    if "reasoning_max_element_searches" in overrides:
        quotas["element_searches_left"] = overrides[
            "reasoning_max_element_searches"]
    if "reasoning_max_chunk_searches" in overrides:
        quotas["chunk_searches_left"] = overrides[
            "reasoning_max_chunk_searches"]

    used = {field: 0 for field in quotas}
    for step in case["script"][: turn_index - 1]:
        field = _QUOTA_FIELD_BY_ACTION.get(step["next_action"])
        if field in used:
            used[field] += 1

    return ReflectCapabilityFacts(
        kg_in_scope=case["corpus_cell"] == "B_kg",
        scope_restricted=False, has_candidates=True,
        chunk_search_active=True, exact_lookup_active=True, ppr_active=True,
        community_active=True, enumeration_active=True,
        consult_memory_active=True, outline_active=True,
        element_searches_left=max(
            0,
            quotas["element_searches_left"] - used["element_searches_left"]),
        chunk_searches_left=max(
            0, quotas["chunk_searches_left"] - used["chunk_searches_left"]),
        exact_lookups_left=max(
            0, quotas["exact_lookups_left"] - used["exact_lookups_left"]),
        ppr_left=3, follow_chain_left=3, consult_left=2,
        outline_updates_left=6, enum_rows_left=200,
        enum_pages_left=max(
            0, quotas["enum_pages_left"] - used["enum_pages_left"]),
        enum_payload_left=256_000,
        element_kinds=tuple(ENUMERABLE_ELEMENT_KINDS),
        object_types=tuple(ENUMERABLE_KG_OBJECT_TYPES),
        last_turn=False, outline_repair_available=False,
        terminal_overflow_repair=False,
    )


#: 期望的「本轮不可用」轮号集合——按生产默认额度 + `settings_overrides` 跨轮
#: 消耗算出来的**结果**,不是随手挑的输入。10 例全在额度内(空集,最紧的是
#: `lc-b-cost-survey` 3/4 枚举页与 `zh-a-latent-cot` 2/3 精确查找);两个
#: `tool_exhausted` 例的元素检索额度被 `settings_overrides` 收到 1,剧本第二次
#: 硬选 `search_elements` 时(以及此后每一次)额度已耗尽——原因固定是
#: `unavailable_action:element_search_cap`(`app.services.reasoning_actions`
#: 的 `REASON_ELEMENT_CAP`)。
EXPECTED_UNAVAILABLE_TURNS: dict[str, frozenset] = {
    "sf-a-gsm8k": frozenset(),
    "sf-b-jamba-ratio": frozenset(),
    "cc-a-macro-arch": frozenset(),
    "cc-b-kv-criteria": frozenset(),
    "gr-b-mla-vs-kivi": frozenset(),
    "ng-a-recurrent-depth": frozenset(),
    "ro-b-source-roster": frozenset(),
    "lc-b-cost-survey": frozenset(),
    "zh-a-latent-cot": frozenset(),
    "rp-b-duplicate-sources": frozenset(),
    "te-a-benchmarks": frozenset({2, 4}),
    "te-b-kivi-bits": frozenset({2, 5}),
}


def test_the_expected_unavailable_turns_cover_exactly_the_twelve_cases():
    """`EXPECTED_UNAVAILABLE_TURNS` 与 case 集逐把 key 对上,不多不少。

    这条只钉字典本身的覆盖面——各轮号是否真的解析成 `unavailable_action:*`
    由 `test_each_case_script_step_really_parses_into_its_declared_action`
    逐轮核实,这里不重复。
    """
    assert set(EXPECTED_UNAVAILABLE_TURNS) == set(_cases_by_key())


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_script_step_only_uses_the_keys_parse_reflect_v2_reads(
    case_key: str,
):
    """剧本步的键纳入闭集:必填三键齐全,闭集外的键一律拒。

    变异:把某一步的 `assessment` 拼成 `assesment` ⇒ 这条红。拼错的键不会让
    `parse_reflect_v2` 报错(它只是被 `dict.get` 静默忽略),所以那份自评会在
    生产里悄悄失踪而不留任何痕迹——这条测的正是"闭集外有没有多余的键",不是
    "解析会不会崩"。
    """
    case = _cases_by_key()[case_key]
    for index, step in enumerate(case["script"], 1):
        keys = set(step)
        assert SCRIPT_STEP_REQUIRED_KEYS <= keys, (case_key, index, keys)
        allowed = SCRIPT_STEP_REQUIRED_KEYS | SCRIPT_STEP_OPTIONAL_KEYS
        assert keys <= allowed, (case_key, index, keys - allowed)


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_script_step_really_parses_into_its_declared_action(
    case_key: str,
):
    """剧本每一轮真的过 `parse_reflect_v2`,解出的动作与那一轮声明的一致——
    除了 `EXPECTED_UNAVAILABLE_TURNS` 点名的那几轮,那几轮**必须**解析成
    `unavailable_action:element_search_cap`。

    此前的用例只校 `next_action ∈ 闭集`、`sufficient is False`、`arguments`
    是 dict,止步于键的类型——一个 case 声明 `next_action: "search_elements"`
    却因为参数本身不合法而被解析成 `__reflect_invalid__` 伪动作,那些用例照样
    绿。这条按语料格(kg / 无图)+ `settings_overrides` + 剧本前序动作的真实
    累计消耗逐轮构造 `ReflectCapabilityFacts`(`_capability_facts_before_turn`,
    生产默认额度,不是充到吃不穿的满额度),把每一步的原始字典喂给真
    `parse_reflect_v2`。

    变异(均已手工验证按预期变红,未落盘):
    - `sf-a-gsm8k` 第 3 轮的 `arguments.prefer` 从 `"balanced"` 改成
      `"bogus"` ⇒ `invalid_argument:prefer`;
    - `sf-b-jamba-ratio` 第 3 轮的 `arguments.types` 从 `["claim"]` 改成字符串
      `"claim"` ⇒ `invalid_argument:types`;
    - `lc-b-cost-survey` 第一步的 `arguments.object_type` 从 `"procedure"`
      改成 `"method"`(不在 `ENUMERABLE_KG_OBJECT_TYPES` 白名单里)⇒
      `invalid_argument:object_type`;
    - `ro-b-source-roster` 第一步的 `arguments.collection` 从 `"sources"`
      改成 `"kg_objects"` ⇒ `invalid_argument:collection`;
    - `sf-a-gsm8k` 追加两轮 `search_chunks`(第 4 次,默认上限 3)⇒ 第 4 次
      那一轮 `unavailable_action:chunk_search_cap`,不是先前满额度写法漏掉的
      那种"标签对了、通道其实已经耗尽"(评审 P2-1)。
    """
    from app.services.reasoning_actions import build_reflect_capabilities
    from app.services.reasoning_retrieval import (
        REFLECT_INVALID_ACTION, parse_reflect_v2,
    )

    case = _cases_by_key()[case_key]
    expected_unavailable = EXPECTED_UNAVAILABLE_TURNS[case_key]
    for index, step in enumerate(case["script"], 1):
        facts = _capability_facts_before_turn(case, index)
        caps = build_reflect_capabilities(facts)
        decision = parse_reflect_v2(step, caps)
        if index in expected_unavailable:
            assert decision.next_action == REFLECT_INVALID_ACTION, (
                case_key, index, decision.invalid_reason)
            assert decision.invalid_reason == (
                "unavailable_action:element_search_cap"), (
                case_key, index, decision.invalid_reason)
            continue
        assert decision.next_action != REFLECT_INVALID_ACTION, (
            case_key, index, decision.invalid_reason)
        assert decision.next_action == step["next_action"], (case_key, index)


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_assessment_only_references_its_own_aspects(case_key: str):
    """剧本里出现的 `assessment`,只能引用**该例自己的契约**推出的方面 id。

    方面 id 由 `build_aspect_ledger(intent_contract, question)` 按契约的
    `mandatory_topics` 顺序确定性生成(`a1..aN`),不是 fixture 自己声明的
    字面量——这条用真账本推出合法 id 集合,而不是重写一份形状判据。
    `unresolved` 组的 `status` 还要落在 `ASPECT_UNRESOLVED_STATUSES` 闭集里。

    变异:把 `cc-a-macro-arch` 第 5 轮的 `aspect_id` 从 `"a3"` 改成 `"a9"`
    (该例契约只有 3 个必答方面,合法 id 顶多到 `a3`)⇒ 这条红。
    """
    from app.domain.retrieval_termination import ASPECT_UNRESOLVED_STATUSES
    from app.services.reasoning_aspects import build_aspect_ledger

    case = _cases_by_key()[case_key]
    ledger = build_aspect_ledger(case["intent_contract"], case["question"])
    valid_ids = {row.aspect_id for row in ledger.snapshot()}
    assert valid_ids, case_key
    for index, step in enumerate(case["script"], 1):
        assessment = step.get("assessment")
        if assessment is None:
            continue
        assert isinstance(assessment, dict), (case_key, index)
        for group, statuses in (
            ("supported", None), ("unresolved", ASPECT_UNRESOLVED_STATUSES),
        ):
            for row in assessment.get(group, []):
                assert isinstance(row, dict), (case_key, index, group, row)
                assert row.get("aspect_id") in valid_ids, (
                    case_key, index, group, row)
                if statuses is not None:
                    assert row.get("status") in statuses, (
                        case_key, index, group, row)


# --- (f) 必答主题问句不会被下游折回 objective ---------------------------------


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_mandatory_topic_question_avoids_the_unresolved_reference_closed_set(
    case_key: str,
):
    """`mandatory_topics[].question` 一个都不许命中
    `query_intent._UNRESOLVED_REFERENCE` / `_GENERIC_REQUEST`。

    两个闭集直接从 `app.services.query_intent` import,不在这里重写一份——那
    才是 `confirmed_research_question` 真正会跑的判据。命中任何一个,那一条
    主题问句会被整条替换成 `objective`/`resolved_question`(下一条用例核实这
    件事的可观测后果),多个必答方面从此在渲染出的「必须覆盖的问题」里塌成
    重复的同一行,而 E2 要比较的正是 A/B 两格这段稳定前缀的字节数(评审
    P2-2)。

    变异:把任意一条问句改回含「这个」/「那个」等闭集词 ⇒ 这条红。
    """
    from app.services.query_intent import _GENERIC_REQUEST, _UNRESOLVED_REFERENCE

    case = _cases_by_key()[case_key]
    for topic in case["intent_contract"]["mandatory_topics"]:
        question = topic["question"]
        assert not _GENERIC_REQUEST.fullmatch(question), (case_key, question)
        assert not _UNRESOLVED_REFERENCE.search(question), (case_key, question)


@pytest.mark.parametrize("case_key", sorted(_cases_by_key()))
def test_each_case_confirmed_research_question_keeps_every_mandatory_topic(
    case_key: str,
):
    """真调一次 `confirmed_research_question`:渲染出的必答行数必须等于
    `mandatory_topics` 的条数。

    上一条用例只核了输入(问句不落在闭集正则里);这一条核实那件事真正要保证
    的可观测后果——`confirmed_research_question` 一旦把某条主题问句判成
    "指代不明"或"泛泛请求",就会把它整条换成 `base`(objective 或
    resolved_question)再去重,「必须覆盖的问题」那一段就会比 `mandatory_topics`
    短。三个必答方面塌成一行、且与首行逐字重复,正是评审 P2-2 在
    `cc-a-macro-arch` 上实测到的失败场景。
    """
    from app.services.query_intent import confirmed_research_question

    case = _cases_by_key()[case_key]
    contract = case["intent_contract"]
    combined = confirmed_research_question(contract, case["question"])
    topic_lines = [
        line for line in combined.splitlines() if line.startswith("- ")
    ]
    assert len(topic_lines) == len(contract["mandatory_topics"]), (
        case_key, combined)


# ============================================================================
# --- T-EX5:驱动器 / 代理 / 记录面 ---
# ============================================================================
#
# 这一节测 `app.eval.reflect_state_probe`。**全部用进程内 fake,零真实模型、零
# 网络**:「真客户端」是一个记录替身,它按脚本返回一份决定并填 `call_stats`
# 出参(形状与 `app/core/llm.py` 的 `_record_call_stats` 逐键相同)。
#
# 库是一个 `tmp_path` 上的一次性 SQLite,**刻意不绑任何 embedding 替身**:
# 关键词/FTS 通道就足以把候选池填出来,而不绑替身让这一节完全不 import
# `tests/model_testkit`——E2 的接缝是 `model_clients` 代理,不需要 provider
# 覆写(计划 M3 的原话)。
import copy  # noqa: E402
import re  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.domain.reasoning_trace_stats import (  # noqa: E402
    REFLECT_CONTEXT_DETAIL_KEYS,
    assert_projection_values,
)
from app.eval import reflect_state_probe as state_probe  # noqa: E402
from app.eval.reflect_manifest import assert_manifest  # noqa: E402
from app.eval.reflect_state_probe import (  # noqa: E402
    EXECUTED_ACTION_STEP_TYPES,
    NON_ACTION_STEP_TYPES,
    PROBE_ROW_ALWAYS_NONE,
    PROBE_ROW_KEYS,
    PROBE_UNREADABLE_ACTION,
    REFLECT_CHAT_WORKLOAD,
    STATE_POINT_LABELS,
    STOP_DECISION_GAP,
    STOP_DECISION_REASON,
    ForwardedTurn,
    ProbeModelClients,
    ScriptedReflectDriver,
    StateProbeError,
    _count,
    _settings_with_case_overrides,
    assert_no_action_executed_after_forward,
    assert_probe_row_closed,
    build_probe_row,
    case_set_digest,
    load_state_probe_case_set,
    load_state_probe_cases,
    run_state_probe_point,
    state_probe_manifest_facts,
    summarize_state_probe,
)
from app.models.schemas import NotebookCreate  # noqa: E402
from app.services.sqlite_repository import SQLiteRepository  # noqa: E402

#: 四条臂:短码 → `reasoning_reflect_optimization` 的取值。与
#: `app.eval.reflect_ab.ARMS` 的 v2 侧四格同一批取值(那边带 policy 维,这里
#: E2 恒 v2 所以只留第二维)。
PROBE_ARMS: tuple[tuple[str, str], ...] = (
    ("B", "off"),
    ("P", "prefix_snapshot"),
    ("D", "prefix_delta"),
    ("L", "prefix_delta_lean"),
)

#: 一份 reflect(而不是 plan)的 schema hint 替身。驱动器分岔的判据是
#: `"sub_queries" in schema_hint`(`_SeqLLM` 的同一条),所以只要不含那个词。
REFLECT_HINT = '{"next_action":"answer","sufficient":false,"arguments":{}}'

#: 命名红线正则:字段名与摘要键名一律不许出现这些形状(计划 §5 风险 4)。
FORBIDDEN_NAME_SHAPES = re.compile(r"cache_hit|hit_rate|hitrate|命中|hit rate")


def _synthetic_raw(**case_overrides: Any) -> dict:
    """一份**合成**的最小 case 集(顶层 + 一例)。

    合成而不是拿 12 例里的某一个改:这一节要逐条试加载器的拒绝路径,而 12 例
    那份 fixture 的每一格都被上一节的对账用例钉住了——在它上面改一个字段去试
    「加载器会不会拒」,红的时候分不清是加载器对了还是 fixture 坏了。
    """
    case: dict[str, Any] = {
        "case_key": "syn-b-scores",
        "probe_shape": "single_fact",
        "corpus_cell": "B_kg",
        "question_key": "A-q24",
        "question": "GSM8K 上的得分是多少?",
        "intent_contract": {
            "objective": "取出这篇论文在 GSM8K 上的得分。",
            "resolved_question": "GSM8K 上的得分是多少?",
            "intent_type": "explain",
            "result_scope": "ranked",
            "completeness_required": False,
            "entities": ["GSM8K"],
            "mandatory_topics": [{
                "id": "t1", "title": "GSM8K 得分",
                "question": "GSM8K 上的得分是多少?",
                "retrieval_queries": ["GSM8K 得分"],
            }],
            "constraints": [],
            "expected_output": "一个数值。",
            "confidence": 0.9,
            "needs_clarification": False,
            "confirmed": True,
        },
        "script": [
            {"next_action": "add_subquery", "sufficient": False,
             "reason": "先补一条", "arguments": {"query": "循环次数设置步骤"}},
            {"next_action": "add_subquery", "sufficient": False,
             "reason": "再补一条", "arguments": {"query": "GSM8K 得分表概述"}},
            {"next_action": "add_subquery", "sufficient": False,
             "reason": "第三条", "arguments": {"query": "得分与循环次数的关系"}},
            {"next_action": "add_subquery", "sufficient": False,
             "reason": "第四条", "arguments": {"query": "测试时循环的口径"}},
        ],
        "state_points": [1, 2, 3],
    }
    case.update(copy.deepcopy(case_overrides))
    return {"version": 1, "note": "synthetic", "cases": [case]}


def _synthetic_case(**case_overrides: Any):
    return load_state_probe_cases(_synthetic_raw(**case_overrides))[0]


@pytest.fixture
def probe_repo(tmp_path, monkeypatch):
    """一次性 SQLite 仓库 + 两个 KG 对象。**不绑 embedding 替身**(见节首)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'probe.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 与 `test_reasoning_retrieval.rrepo` 同一条隔离:清空真实端点,免得本地
    # `.env` 让这一节打真实网络。
    for name in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                 "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                 "REASONING_LLM_MODEL"):
        monkeypatch.setenv(name, "")
    repo = SQLiteRepository(Settings())
    notebook = repo.create_notebook(NotebookCreate(name="state-probe"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "GSM8K 得分表概述", "section_path": "1"},
         "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "循环次数设置步骤", "section_path": "2"},
         "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "depends_on", "evidence": []},
    ])
    return repo, notebook.id


def _probe_settings(optimization: str) -> Settings:
    """一条臂的 Settings。三格与 rig 的 `_settings_by_arm` 同向:v2 总闸开、
    测量开(四臂用同一把尺子)、策略位是这条臂。`reasoning_stale_limit` 放宽是
    因为空手轮不该把剧本提前掐断(照 `test_reasoning_retrieval._v2_repo`)。"""
    settings = Settings()
    settings.reasoning_reflect_v2_enabled = True
    settings.reasoning_reflect_measure_context = True
    settings.reasoning_reflect_optimization = optimization
    settings.reasoning_stale_limit = 9
    return settings


class _FakeRealClient:
    """「真客户端」的替身:记录每一次调用,返回一份可配置的决定 + 填 `call_stats`。

    `supports_call_stats = True` 与 sink 的键名逐格照 `app/core/llm.py` 的
    `_record_call_stats`(`status` / `call_wall_ms` / `attempts` /
    `attempts_observed` / `finish_reason` / `response_chars`)。
    """

    configured = True
    supports_call_stats = True

    def __init__(self, payload: Any, *, raise_with: Exception | None = None,
                 status: str = "ok"):
        self.payload = payload
        self.raise_with = raise_with
        self.status = status
        self.calls: list[dict] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        body = json.dumps(self.payload, ensure_ascii=False)
        self.calls.append({
            "messages": [dict(row) for row in messages],
            "schema_hint": schema_hint,
            "kwargs": dict(kwargs),
        })
        sink = kwargs.get("call_stats")
        if isinstance(sink, dict):
            sink.update({
                "status": self.status, "call_wall_ms": 42, "attempts": 1,
                "attempts_observed": True, "finish_reason": "stop",
                "response_chars": len(body),
            })
        if self.raise_with is not None:
            raise self.raise_with
        return body


#: 模型在状态点上选的那次检索。E2 的判据是它**一次都不执行**,所以这个查询串
#: 刻意与剧本里任何一条都不同——它要么出现在 `attempted` 里(说明执行了),要么
#: 一个字都不出现。
MODEL_PICKED_QUERY = "模型自己挑的那次元素检索"

MODEL_SEARCH_DECISION = {
    "next_action": "search_elements", "sufficient": False,
    "arguments": {"query": MODEL_PICKED_QUERY},
    "reason": "模型想再查一次元素",
}


def _run_point(probe_repo, case, state_point_index, client, *,
               arm="B", optimization="off", repeat=0):
    repo, notebook_id = probe_repo
    return run_state_probe_point(
        repo, _probe_settings(optimization), case, state_point_index, client,
        notebook_id=notebook_id, arm=arm, optimization=optimization,
        repeat=repeat, effort="standard",
    )


# --- (f) 加载器逐条拒 ---------------------------------------------------------


def test_the_loader_accepts_every_shipped_case_and_reports_a_digest():
    """**12 例逐例过真加载器**(T-EX6 汇合时约定的那一条)。

    形态对账仍走 `_load_cases_raw()`(见模块 docstring):那一节问「这一例是不是
    它声明的那种形态」,这一条问「加载器认不认这 12 例」。两条都要有——只有前者
    时,一个加载器把某条约束写反了没人会发现;只有后者时,一个标着 roster 其实
    在做向量检索的例子照样过。
    """
    cases, digest = load_state_probe_case_set()
    assert len(cases) == EXPECTED_CASE_COUNT
    assert sorted(case.case_key for case in cases) == sorted(_cases_by_key())
    # 摘要是给 manifest 的短码:十六位十六进制,与 rig 的语料签名同形。
    assert re.fullmatch(r"[0-9a-f]{16}", digest), digest
    for case in cases:
        assert len(case.state_points) == 3
        assert case.state_points[-1] < len(case.script)


def test_the_case_set_digest_tracks_the_file_bytes():
    """摘要量的是**字节**,不是解析后的结构。

    只改一个注释的编辑同样让「同一个摘要」这句话不再成立,而 manifest 冻的正是
    「这次跑读的是哪一份剧本」。
    """
    raw = STATE_PROBES_PATH.read_bytes()
    assert case_set_digest(raw) == case_set_digest(raw)
    assert case_set_digest(raw) != case_set_digest(raw + b"\n")
    with pytest.raises(TypeError):
        case_set_digest(raw.decode("utf-8"))  # type: ignore[arg-type]


def test_the_loader_rejects_a_top_level_key_outside_the_closed_set():
    """顶层键闭集(T-EX6 评审把这一格留给加载器)。"""
    raw = _synthetic_raw()
    raw["seed"] = 7
    with pytest.raises(ValueError, match="TOP_LEVEL_KEYS"):
        load_state_probe_cases(raw)


def test_the_loader_rejects_an_unknown_case_set_version():
    raw = _synthetic_raw()
    raw["version"] = 2
    with pytest.raises(ValueError, match="version"):
        load_state_probe_cases(raw)


def _script_planting(**leaf: Any) -> list[dict]:
    """一份四步剧本,第一步的 `arguments` 额外带 `leaf`。"""
    return [
        {"next_action": "add_subquery", "sufficient": False,
         "arguments": {"query": "x", **leaf}},
        {"next_action": "add_subquery", "sufficient": False,
         "arguments": {"query": "y"}},
        {"next_action": "add_subquery", "sufficient": False,
         "arguments": {"query": "z"}},
        {"next_action": "add_subquery", "sufficient": False,
         "arguments": {"query": "w"}},
    ]


def test_this_files_forbidden_keys_copy_matches_the_loaders_closed_set():
    """本文件顶部的 `FORBIDDEN_KEYS`(section (b) 形态对账用的那份字面量副本)
    与加载器的 `FORBIDDEN_ID_KEYS` 必须逐字相同。

    下面那条参数化用例刻意不拿生产常量算参数列表(见其 docstring),而是用这份
    独立副本——这条兑账断言补上那样做丢掉的另一半:两份闭集分道扬镳时,这里
    响亮说出来,而不是让本文件的覆盖悄悄漏掉生产新增的某个键。
    """
    assert FORBIDDEN_KEYS == state_probe.FORBIDDEN_ID_KEYS


@pytest.mark.parametrize("forbidden_key", sorted(FORBIDDEN_KEYS))
def test_the_loader_rejects_a_planted_database_id(forbidden_key):
    """Q5 第一条,**逐键**版:每一个已知的数据库 id 键名单独植一次
    (T-EX5 评审 P3#9)。

    此前只有 `object_id` 被这条用例植过——砍掉加载器 `FORBIDDEN_ID_KEYS` 里的
    `source_id` / `source_ids`,全部十一个键里的其余十个,347 例照绿。这里刻意
    按**本文件自己的** `FORBIDDEN_KEYS`(模块顶部,section (b) 的同一份闭集)
    参数化,不导入生产 `FORBIDDEN_ID_KEYS` 来算参数列表:若参数直接来自生产
    常量,生产那边哪天把整个键**移出**闭集,`sorted(...)` 会跟着变短,少了的
    那几个参数化用例根本不会被收集——测试数量悄悄减少却全绿,看起来像是通过。
    两份闭集同源但不互相 import,与加载器 docstring 里「形态对账不经过加载器」
    的同一条纪律。

    `aspect_id` 刻意不在这份闭集里(剧本的 `assessment` 要用它,已核),
    `arguments` / `intent_contract` 这两块自由 Mapping 上的驼峰变体(如
    `sourceId`)不在覆盖范围内,今天靠 `FORBIDDEN_VALUE_FRAGMENTS` 的 `src-`
    值判据兜底——这层兜底成立的前提是仓库的 source id 一直带 `src-` 前缀,
    登记为已知缺口。
    """
    with pytest.raises(ValueError, match="syn-b-scores"):
        load_state_probe_cases(_synthetic_raw(
            script=_script_planting(**{forbidden_key: "abc"})))


def test_the_loader_rejects_a_planted_id_value_fragment():
    """Q5 第一条,值那一半:一个 `ko-…` 串塞进查询里,不需要落在某个特定键名上
    ——`FORBIDDEN_VALUE_FRAGMENTS` 按叶子值扫,与键名判据独立。"""
    with pytest.raises(ValueError, match="syn-b-scores"):
        load_state_probe_cases(_synthetic_raw(
            script=_script_planting(query="ko-deadbeef01 的邻居")))


def test_the_id_prefix_fragment_check_has_a_word_boundary_anchor():
    """T-EX5 评审 P3#13:三个 id 前缀(`nb-`/`src-`/`ko-`)要过**词首锚**,不是
    无边界子串匹配。

    不锚的话,一个普通英文词中段恰好含 `ko-` 的题面(比如提到 "Gecko-3" 这个
    真实存在的模型名)会被误判成带 id 片段;而合法阳性用例(`ko-` 真的作为
    id 前缀出现在串首或紧跟空白/括号)仍然要被拦住,不能借锚点松绑真正的红线。
    """
    # 阴性:`ko-` 出现在词中段,不在词首,也不紧跟空白/括号——放行。
    load_state_probe_cases(_synthetic_raw(
        script=_script_planting(query="关于 Gecko-3 型号的说明")))
    # 阳性:`ko-` 在串首——仍然拒。
    with pytest.raises(ValueError, match="id-prefix"):
        load_state_probe_cases(_synthetic_raw(
            script=_script_planting(query="ko-deadbeef01 的邻居")))
    # 阳性:`ko-` 紧跟在左括号之后——仍然拒。
    with pytest.raises(ValueError, match="id-prefix"):
        load_state_probe_cases(_synthetic_raw(
            script=_script_planting(query="参见(ko-deadbeef01)的记录")))


def test_the_loader_rejects_a_case_without_the_frozen_contract():
    raw = _synthetic_raw()
    del raw["cases"][0]["intent_contract"]
    with pytest.raises(ValueError, match="intent_contract"):
        load_state_probe_cases(raw)


def test_the_loader_rejects_a_contract_with_no_mandatory_topics():
    """Q5 第二条的可观测后果:方面账为空 ⇒ v2 的方面块塌掉,四臂那段稳定前缀
    的字节数就不再可比(计划 M3:方面数直接决定 T 的方面块与 L 的自评段)。"""
    contract = _synthetic_raw()["cases"][0]["intent_contract"]
    contract["mandatory_topics"] = []
    with pytest.raises(ValueError, match="mandatory_topics"):
        load_state_probe_cases(_synthetic_raw(intent_contract=contract))


def test_the_loader_rejects_a_contract_that_still_needs_clarification():
    """E2 要的是「已确认且本来就没歧义」那一档:代答过澄清项的题,权威方向会从
    用户原文换成合成后的 `resolved_question`,两档的首轮种子不是同一批。"""
    contract = _synthetic_raw()["cases"][0]["intent_contract"]
    contract["needs_clarification"] = True
    with pytest.raises(ValueError, match="already-confirmed"):
        load_state_probe_cases(_synthetic_raw(intent_contract=contract))


def test_the_loader_rejects_a_settings_override_outside_the_whitelist():
    with pytest.raises(ValueError, match="whitelisted"):
        load_state_probe_cases(_synthetic_raw(
            settings_overrides={"reasoning_max_steps": 3}))


def test_the_loader_rejects_a_rendering_budget_override():
    """独立于白名单的显式红线:调小渲染预算去逼出压缩边界,与调剧本去凑是同一
    件事的两种写法(§9.2「缺数据不补造」)。"""
    with pytest.raises(ValueError, match="rendering budget"):
        load_state_probe_cases(_synthetic_raw(
            settings_overrides={"reasoning_reflect_state_chars": 200}))


def test_the_loader_accepts_the_two_whitelisted_retrieval_caps():
    case = _synthetic_case(
        settings_overrides={"reasoning_max_element_searches": 1})
    assert case.settings_overrides == {"reasoning_max_element_searches": 1}


@pytest.mark.parametrize("bad_value", ["1", -1, 0, True])
def test_the_loader_rejects_a_malformed_settings_override_value(bad_value):
    """`settings_overrides` 此前只校了三遍**键**,一次都没校验**值**
    (T-EX5 评审 P2#3)。

    `model_copy(update=...)` 不跑校验器,而白名单两个键在 `Settings` 上连
    `ge=0` 都没有,所以一个 JSON 里的 `"1"`(字符串)会一路混到
    `reasoning_retrieval` 里炸一个裸 `TypeError`;`-1` / `0` 会让那一例的检索
    通道整轮悄悄跑在「额度已耗尽」这个它没声明的状态上,五道运行期事后核一条
    都不管值合不合法;`True` 当 1 用,同样不是这份 case 想表达的整数。四种都要
    在加载这一步就响亮拒。
    """
    with pytest.raises(ValueError, match="must be an int"):
        load_state_probe_cases(_synthetic_raw(
            settings_overrides={"reasoning_max_element_searches": bad_value}))


@pytest.mark.parametrize("points,why", [
    ([1, 2], "exactly 3"),
    ([1, 2, 3, 4], "exactly 3"),
    ([1, 3, 2], "strictly increasing"),
    ([1, 2, 2], "strictly increasing"),
    ([0, 1, 2], ">= 1"),
    # 剧本有 4 步,所以最后一个状态点最多是 3:轮 4 是要转发的那一轮,它必须
    # 落在剧本已经想清楚的范围**之内**。
    ([1, 2, 4], "does not cover"),
    ([2, 3, 9], "does not cover"),
])
def test_the_loader_rejects_state_points_that_break_the_replay_contract(
    points, why,
):
    """三个、严格单调、≥ 1、且 `< len(script)`。

    最后那一条是越界拒:第 k 个状态点的跑法是「剧本推进第 1..k 轮,第 k+1 轮
    转发」,一个指向剧本之外的轮号只能靠兜底编一轮观察——正是 §9.2 不许的补造。
    """
    with pytest.raises(ValueError, match=why):
        load_state_probe_cases(_synthetic_raw(state_points=points))


def test_the_loader_rejects_a_script_step_key_outside_the_closed_set():
    """拼错的 `assesment` 不会让 `parse_reflect_v2` 报错(它只被 `dict.get`
    静默忽略),所以形状判据要在加载这一侧堵。"""
    script = _synthetic_raw()["cases"][0]["script"]
    script[1]["assesment"] = {"supported": []}
    with pytest.raises(ValueError, match="closed set"):
        load_state_probe_cases(_synthetic_raw(script=script))


def test_the_loader_rejects_a_script_step_that_claims_sufficiency():
    """剧本每一轮都是检索,所以 `sufficient` 恒 false:检索动作与
    `sufficient=true` 不能同轮成立(`_V2_SUFFICIENT_CONTRADICTION`)。停止决定
    由驱动器自己在第 k+1 轮发,不由剧本发。"""
    script = _synthetic_raw()["cases"][0]["script"]
    script[0]["sufficient"] = True
    with pytest.raises(ValueError, match="sufficient=false"):
        load_state_probe_cases(_synthetic_raw(script=script))


@pytest.mark.parametrize("action", sorted(FORBIDDEN_SCRIPT_ACTIONS))
def test_the_loader_rejects_the_three_id_hungry_actions(action):
    """三个图动作的必填参数是候选池里的 `object_id`,而驱动器只看得见渲染后的
    文本——给不出一个真实候选的 id(登记为债务,计划 Q5)。"""
    script = _synthetic_raw()["cases"][0]["script"]
    script[0] = {"next_action": action, "sufficient": False, "arguments": {}}
    with pytest.raises(ValueError, match="candidate-pool object id"):
        load_state_probe_cases(_synthetic_raw(script=script))


def test_the_loader_rejects_a_duplicate_case_key():
    raw = _synthetic_raw()
    raw["cases"].append(copy.deepcopy(raw["cases"][0]))
    with pytest.raises(ValueError, match="twice"):
        load_state_probe_cases(raw)


@pytest.mark.parametrize("case_key", [
    "sf a gsm8k",  # 带空格,不是短码
    "a" * 70,  # 超过 64 字符
])
def test_the_loader_rejects_a_case_key_that_is_not_a_short_code(case_key):
    """`case_key` 是 `PROBE_ROW_KEYS` 成员,要过短码闸(≤64 字符、无空白、
    `[A-Za-z0-9_:\\-.+]`)——加载器此前只校验「非空串」(T-EX5 评审 P2#5)。

    不在这里提前拦住的话,一个形状不合的 `case_key` 要等到 `build_probe_row`
    才被 `assert_probe_row_closed` 拒:那时一次真模型调用与一轮 embedding
    已经烧掉了。
    """
    with pytest.raises(ValueError, match="short code"):
        load_state_probe_cases(_synthetic_raw(case_key=case_key))


# --- (a) 推进到第 k 轮后转发恰一次 --------------------------------------------


def test_the_driver_forwards_exactly_once_and_the_call_stats_are_read(
    probe_repo,
):
    """(a) 剧本推进到第 k 轮 ⇒ 第 k+1 轮转发**恰一次**,读数被读到。

    四件事一起断言,因为它们是同一件事的四个面:转发次数、转发的是哪一轮的
    消息、`call_stats` 出参真的被读到(否则墙钟/请求数全线 unknown 而没有任何
    一条用例会红)、以及转发时那两格被换掉的 kwargs(`bypass_cache=True` 让
    「这批数不含本地缓存出口」是结构事实而不是推理)。
    """
    case = _synthetic_case()
    client = _FakeRealClient(MODEL_SEARCH_DECISION)
    point = _run_point(probe_repo, case, 1, client)

    assert len(client.calls) == 1
    driver = point.driver
    assert driver.forward_count == 1
    assert driver.plan_calls == 0
    # 第 k+1 轮:状态点序号 1 ⇒ 轮号 2 ⇒ 转发落在第 3 轮。
    assert driver.state_point_turn == case.state_points[1] == 2
    assert driver.forwarded is not None
    assert driver.forwarded.turn == 3
    assert driver.turns == 3
    # 转发的正是那一轮已经定型的两条消息与那个 hint,原样。
    forwarded_call = client.calls[0]
    assert forwarded_call["messages"] == [
        dict(row) for row in driver.forwarded.messages]
    assert forwarded_call["schema_hint"] == driver.forwarded.schema_hint
    assert forwarded_call["kwargs"]["bypass_cache"] is True
    assert forwarded_call["kwargs"]["call_stats"] is driver.forwarded.stats

    assert point.row["call_wall_ms"] == 42
    assert point.row["call_attempts"] == 1
    assert point.row["status"] == "ok"
    assert point.row["finish_reason"] == "stop"
    assert point.row["response_chars"] > 0
    # 三个测量读数被**镜像**回反思层的 sink,所以 trace 上那一轮也带着墙钟
    # (剧本轮如实空着——`_measure_reflect_call` 的「缺键 ⇒ 不写」)。
    reflects = [step for step in point.result.trace
                if step.step_type == "reflect"]
    assert len(reflects) == 3
    assert reflects[-1].detail.get("call_wall_ms") == 42
    assert "call_wall_ms" not in reflects[0].detail


def test_the_mirrored_stats_keys_match_the_measure_call_keys_source():
    """`_MIRRORED_STATS_KEYS` 是反思层 `_MEASURE_CALL_KEYS`(`call_stats` 出参
    的三个源键)的抄本(T-EX5 评审 P3#8)。

    单独把其中一个键(比如 `attempts`)改名 ⇒ 347 例照绿——只有
    `call_wall_ms` 被上面那条真 run 用例断言。写侧源键改名而这份抄本没跟上,
    转发那一轮的 reflect detail 会静默缺那一格,`.local/raw` 之外的读表人
    (`model_calls_real` / `response_chars_total` 这类 run 级投影)会把它读成
    unknown 而不留任何痕迹——E2 自己的行不受影响,因为它读的是驱动器自己的
    sink,不经这份镜像。
    """
    from app.services.reasoning_retrieval import _MEASURE_CALL_KEYS

    assert set(ScriptedReflectDriver._MIRRORED_STATS_KEYS) == set(
        _MEASURE_CALL_KEYS)


def test_the_scripted_turns_carry_no_call_stats_of_their_own(probe_repo):
    """剧本轮不调任何客户端,所以它们在 trace 上**没有**墙钟。

    反过来写(给剧本轮也镜像一个数)会让 `model_calls_real` 把一条只发过一次
    真实请求的 run 读成发过 k+1 次。
    """
    point = _run_point(probe_repo, _synthetic_case(), 2,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    reflects = [step for step in point.result.trace
                if step.step_type == "reflect"]
    assert len(reflects) == 4
    assert [("call_wall_ms" in step.detail) for step in reflects] == [
        False, False, False, True]


def test_the_stop_decision_short_codes_stay_short_codes():
    """`STOP_DECISION_GAP` / `STOP_DECISION_REASON` 是要进方面账与轨迹的定宽
    短码,不是人话(T-EX5 评审 P3#7)。

    这两个常量同时改成人话(含空格/CJK)此前 347 例照绿——E2 的 run 恰好在
    这一轮结束,没有「后续渲染」去暴露它,但模块 docstring 自己写的是「这条
    纪律不因此松开」。
    """
    for value in (STOP_DECISION_GAP, STOP_DECISION_REASON):
        assert_projection_values({"_": value})


# --- (a2) case 的 settings_overrides 真的接线 ---------------------------------

#: 「元素检索额度 = 1」的合成剧本:第 1 轮 `search_elements` 用掉唯一那一格额度,
#: 第 2 轮再硬选同一个动作 ⇒ 能力面上它已经不可用。与 12 例里两个
#: `tool_exhausted` 例(`te-a-benchmarks` / `te-b-kivi-bits`)同一条造法。
_EXHAUSTED_SCRIPT: list[dict] = [
    {"next_action": "search_elements", "sufficient": False,
     "reason": "先查原文", "arguments": {"query": "GSM8K 得分表"}},
    {"next_action": "search_elements", "sufficient": False,
     "reason": "再查一次原文", "arguments": {"query": "GSM8K 得分按任务拆分"}},
    {"next_action": "add_subquery", "sufficient": False,
     "reason": "补一条", "arguments": {"query": "GSM8K 得分表概述"}},
    {"next_action": "add_subquery", "sufficient": False,
     "reason": "再补一条", "arguments": {"query": "循环次数设置步骤"}},
]

#: 元素检索额度被 `settings_overrides` 收到 1 之后,第 2 轮那次
#: `search_elements` 在能力面上的固定原因码(`reasoning_actions.
#: REASON_ELEMENT_CAP`,经 `parse_reflect_v2` 折成 `unavailable_action:*`)。
ELEMENT_CAP_REASON = "unavailable_action:element_search_cap"


def _exhausted_case(**over: Any):
    return _synthetic_case(
        script=copy.deepcopy(_EXHAUSTED_SCRIPT), state_points=[1, 2, 3], **over)


def _skip_reasons_in(result) -> list:
    return [
        (step.detail or {}).get("reason") for step in result.trace
        if step.step_type == "skip"
    ]


def test_a_case_settings_override_really_bites_on_a_real_run(probe_repo):
    """Q5 第三条:一例带 `reasoning_max_element_searches=1` ⇒ 额度**真的**咬住。

    这条守的是「加载了、冻结了、运行路径一次都不读」那一格:`settings_overrides`
    是 12 例里两个 `tool_exhausted` 例(`te-a-benchmarks` / `te-b-kivi-bits`)
    造形态的**唯一**手段,不接线的话它们测的不是「工具耗尽」——那两例的剧本第 2
    轮那次 `search_elements` 会照常执行,而 T-EX6 逐例钉住的「期望不可用轮号」
    在运行期压根不成立,同时四臂一致、轮数核、「不执行」三道核全部照过。

    对照那一半是载重的:同一份剧本**不带**覆盖时(生产默认 5 次额度)第 2 轮
    照样可用、两次 `search_elements` 都执行。所以「`run_state_probe_point` 不套
    覆盖」这个变异会让上半红、下半照绿——两半合起来才钉住「覆盖只在带覆盖的
    那一例上生效」。
    """
    case = _exhausted_case(
        settings_overrides={"reasoning_max_element_searches": 1})
    assert case.settings_overrides == {"reasoning_max_element_searches": 1}
    point = _run_point(probe_repo, case, 1,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    step_types = [step.step_type for step in point.result.trace]
    assert ELEMENT_CAP_REASON in _skip_reasons_in(point.result), [
        (step.step_type, step.detail) for step in point.result.trace]
    # 第 1 轮那次**执行过**(`search_elements` 落的是 `fallback`),否则「额度被
    # 用掉了」这句话就不成立,第 2 轮的不可用只是通道压根没接通。
    assert step_types.count("fallback") == 1, step_types

    plain = _run_point(probe_repo, _exhausted_case(), 1,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    plain_types = [step.step_type for step in plain.result.trace]
    assert ELEMENT_CAP_REASON not in _skip_reasons_in(plain.result), plain_types
    assert plain_types.count("fallback") == 2, plain_types


def test_the_four_arms_get_one_and_the_same_case_override(probe_repo):
    """同一 case 的四条臂用**同一份**覆盖,且臂配置的原件不被就地改。

    三格判据:

    * 每条臂拿到的都是那一例声明的额度;
    * 四份有效配置除 `reasoning_reflect_optimization` 之外**逐字段相同**——
      覆盖是 case 维的,不许在某条臂上多套/少套一格;
    * 传进来的那份臂配置**没被改**:它是调用方按臂构造一次、给这条臂全部 12 例
      复用的对象,就地 `setattr` 会让第一个带覆盖的 case 把额度永久改小。
    """
    case = _exhausted_case(
        settings_overrides={"reasoning_max_element_searches": 1})
    shapes: set[str] = set()
    for arm, optimization in PROBE_ARMS:
        base = _probe_settings(optimization)
        effective = _settings_with_case_overrides(base, case)
        assert effective is not base
        assert base.reasoning_max_element_searches == 5
        assert effective.reasoning_max_element_searches == 1
        dumped = effective.model_dump()
        assert dumped.pop("reasoning_reflect_optimization") == optimization
        shapes.add(json.dumps(dumped, sort_keys=True, default=str))
    assert len(shapes) == 1, len(shapes)
    # 没有覆盖的 case 原样拿到那份臂配置(不做无谓的复制)。
    plain_base = _probe_settings("off")
    assert _settings_with_case_overrides(
        plain_base, _synthetic_case()) is plain_base


def test_a_settings_object_that_cannot_be_copied_is_refused():
    """副本拿不到 ⇒ 抛,而不是退回去就地改那份臂配置。"""
    case = _exhausted_case(
        settings_overrides={"reasoning_max_element_searches": 1})

    class _Plain:
        reasoning_max_element_searches = 5

    with pytest.raises(StateProbeError, match="model_copy"):
        _settings_with_case_overrides(_Plain(), case)


def test_a_settings_copy_that_returns_itself_is_refused():
    """`model_copy` 返回自己 ⇒ 抛:那等于把「不就地改」这条纪律悄悄取消。"""
    case = _exhausted_case(
        settings_overrides={"reasoning_max_element_searches": 1})

    class _Selfish:
        reasoning_max_element_searches = 5

        def model_copy(self, update=None):
            return self

    with pytest.raises(StateProbeError, match="own settings object"):
        _settings_with_case_overrides(_Selfish(), case)


def test_a_settings_override_that_does_not_take_is_refused():
    """副本上读不出声明的值 ⇒ 抛。

    `model_copy(update=...)` 不跑校验器,而 `Settings` 是 `extra="ignore"`:
    一个字段改了名之后,覆盖会静默变成一个没人读的多余字段,而这一格的数据
    看起来与正常数据毫无区别。
    """
    case = _exhausted_case(
        settings_overrides={"reasoning_max_element_searches": 1})

    class _Deaf:
        reasoning_max_element_searches = 5

        def model_copy(self, update=None):
            return _Deaf()

    with pytest.raises(StateProbeError, match="did not take"):
        _settings_with_case_overrides(_Deaf(), case)


# --- (b) 不执行 ---------------------------------------------------------------


def test_the_model_chosen_action_is_never_executed(probe_repo):
    """(b) **E2 的第一条验收**:模型选的动作一次都不执行。

    模型在状态点上回一条 `search_elements`,断言三件事:

    1. 转发那一轮之后 trace 里**没有任何动作步**
       (`assert_no_action_executed_after_forward` 在编排里已经跑过一遍,这里
       独立再验一次终态形状);
    2. `attempted` 里没有那个查询串——它压根没被提交给检索;
    3. 那个查询串在整条 trace 的 detail 里一个字都不出现(决定正文只在驱动器的
       留存面上,供 `.local/raw`)。

    变异(计划 T-EX10 (c)):驱动器把模型的决定**返回给 `run()`** ⇒ 1 与 2 同时红。
    """
    point = _run_point(probe_repo, _synthetic_case(), 1,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    steps = list(point.result.trace)
    last_reflect = max(index for index, step in enumerate(steps)
                       if step.step_type == "reflect")
    after = [step.step_type for step in steps[last_reflect + 1:]]
    assert after, "收尾步应当存在(至少一个 answer)"
    assert not (set(after) & EXECUTED_ACTION_STEP_TYPES), after

    assert all(MODEL_PICKED_QUERY not in row["query"]
               for row in point.result.attempted)
    serialized = json.dumps(
        [dict(step.detail or {}) for step in steps], ensure_ascii=False)
    assert MODEL_PICKED_QUERY not in serialized
    # 决定正文**在**驱动器的留存面上——那是 `.local/raw` 的产地。
    assert MODEL_PICKED_QUERY in (point.driver.forwarded.raw or "")


def test_the_scripted_turns_do_advance_the_state(probe_repo):
    """「不执行」只针对**模型选的**那一个动作:剧本选的动作照常执行——那正是
    状态点被推进出来的方式。这一条与上面那条互为对照,少了它,一个把**每一个**
    动作都拦掉的驱动器也能让上面那条绿,而那时四条臂的第 k+1 轮站在一个空的
    初始状态上。"""
    case = _synthetic_case()
    point = _run_point(probe_repo, case, 2,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    attempted = [row["query"] for row in point.result.attempted]
    # 前 k 轮(状态点序号 2 ⇒ 轮号 3)的剧本查询逐条落在 `attempted` 上。
    for step in case.script[:case.state_points[2]]:
        query = step["arguments"]["query"]
        assert query in attempted, (query, attempted)


class _FakeStep:
    """最小 trace 步替身:`assert_no_action_executed_after_forward` 只读
    `step_type`。"""

    def __init__(self, step_type: str):
        self.step_type = step_type
        self.detail: dict = {}


class _FakeResult:
    def __init__(self, *step_types: str):
        self.trace = tuple(_FakeStep(name) for name in step_types)


def test_an_action_step_after_the_forwarded_turn_is_refused():
    """(b) 「验收的核心断言」自己的负向用例。

    这条守的是那个函数**整段被删掉也没人发现**的那一格:它此前零直接用例,
    删掉调用点、函数体首行改 `return`、或把 `retrieve` 挪到「非动作」那一半,
    261 例全绿。判据形状是 `reflect → 动作步` ——转发那一轮之后出现任何一个
    动作步,就意味着模型选的动作被执行了,整格作废。
    """
    with pytest.raises(StateProbeError, match="was executed after"):
        assert_no_action_executed_after_forward(
            _FakeResult("reflect", "retrieve", "answer"))
    # 收尾步不是动作:`answer` / `rerank` 落在转发轮之后是**正常**终态。
    assert_no_action_executed_after_forward(
        _FakeResult("reflect", "answer", "rerank"))
    # 前 k 轮的动作步在最后一个 reflect **之前**,一个都不算违规。
    assert_no_action_executed_after_forward(
        _FakeResult("reflect", "retrieve", "reflect", "answer"))
    # 一条 reflect 步都没有 ⇒ 判据压根无从成立,同样响亮拒。
    with pytest.raises(StateProbeError, match="no reflect step"):
        assert_no_action_executed_after_forward(_FakeResult("plan", "retrieve"))


def test_the_no_action_guard_really_runs_on_the_real_probe_path(
    probe_repo, monkeypatch,
):
    """(b) 那道守卫在 `run_state_probe_point` 的路上**真的被调到**。

    上一条测的是判据本身,这条测的是接线:把守卫换成一个抛哨兵的函数,整格必须
    抛出那个哨兵。少了它,「删掉调用点」这个变异只让一句 docstring 变成谎话,
    而每一条用例照绿。
    """

    class _Sentinel(Exception):
        pass

    def boom(result):
        raise _Sentinel("guard ran")

    monkeypatch.setattr(
        state_probe, "assert_no_action_executed_after_forward", boom)
    with pytest.raises(_Sentinel):
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))


def test_the_arm_evidence_guard_really_runs_on_the_real_probe_path(
    probe_repo, monkeypatch,
):
    """臂标签对号(事后核 #1,`assert_optimization_matches_evidence`)在
    `run_state_probe_point` 的路上**真的被调到**(T-EX5 评审 P2#1)。

    删掉这个调用点(变异 Q27)347 例全绿:转发次数、`plan_calls`、轮数、
    「不执行」四道核全过,`test_the_four_arms_differ_in_how_the_forwarded_turn_is_blocked`
    也不会红——那条比的是四条**真配置**臂之间的差,不是标签与证据的一致性。
    这条守的是接线,不是判据本身。`assert_optimization_matches_evidence` 是
    `run_state_probe_point` 里的一处**函数内**导入(源自 `app.eval.reflect_ab`),
    所以哨兵要打在源模块上,monkeypatch 打在 `state_probe` 自己身上不会生效。
    """
    import app.eval.reflect_ab as reflect_ab

    class _Sentinel(Exception):
        pass

    def boom(optimization, observed):
        raise _Sentinel("guard ran")

    monkeypatch.setattr(
        reflect_ab, "assert_optimization_matches_evidence", boom)
    with pytest.raises(_Sentinel):
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))


def test_the_executed_half_is_anchored_on_what_executing_an_action_looks_like(
    probe_repo,
):
    """(b) 闭集的**分类**那一半按动作面真源对号,不只核并集。

    `test_the_action_step_closed_set_covers_every_trace_step_type` 双向核的是
    `EXECUTED ∪ NON_ACTION` 与源码字面量相等——它挡不住「把 `retrieve` 从
    EXECUTED 挪到 NON_ACTION」:并集一个字没变,而「不执行」这条验收从此对
    `add_subquery` 结构性失效。

    所以这条拿**执行本身**当真源:一条真 run 里剧本第 1 轮的 `add_subquery`
    与第 2 轮的 `search_elements` 都是模型可选的检索动作,它们被执行时当场记下
    的步名(`retrieve` / `fallback`——后者是 `search_elements` 的历史步名)就是
    「一个动作真的执行了」在 trace 上的样子,必须落在 EXECUTED 那一半。
    """
    case = _synthetic_case(script=[
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "先补一条", "arguments": {"query": "GSM8K 得分表概述"}},
        {"next_action": "search_elements", "sufficient": False,
         "reason": "降级查原文", "arguments": {"query": "GSM8K 得分"}},
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第三条", "arguments": {"query": "循环次数设置步骤"}},
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第四条", "arguments": {"query": "得分与循环次数的关系"}},
    ], state_points=[1, 2, 3])
    point = _run_point(probe_repo, case, 1,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    steps = list(point.result.trace)
    reflect_at = [index for index, step in enumerate(steps)
                  if step.step_type == "reflect"]
    assert len(reflect_at) == 3, [step.step_type for step in steps]
    emitted = [
        {step.step_type for step in steps[reflect_at[i] + 1:reflect_at[i + 1]]}
        for i in range(len(reflect_at) - 1)
    ]
    assert "retrieve" in emitted[0], emitted
    assert "fallback" in emitted[1], emitted
    for step_type in ("retrieve", "fallback"):
        assert step_type in EXECUTED_ACTION_STEP_TYPES, step_type
        assert step_type not in NON_ACTION_STEP_TYPES, step_type
    # 而这条 run 的终态仍然干净:模型选的那个动作一次都没执行。
    assert_no_action_executed_after_forward(point.result)


def test_the_test_database_gains_no_rows_from_a_probe_point(probe_repo):
    """Q7 的只读证据:跑完一格,库里的行数一格没长。

    E2 只跑检索、不合成、不落库(`retrieval_run(event_log=None)` 是那条唯一会
    往库里写的旁路被刻意不接的地方)。这一条量的是**测试库自己**——E2 结构上
    就不该写它,所以这条断言是有意义的。
    """
    repo, _ = probe_repo
    # 前四张是候选池的产地,后五张是一条**完整** Ask 会写的那几张——E2 不合成、
    # 不落库,所以它们必须一行都不长(`ask_trace_steps` / `answers` 尤其:
    # 它们是「这条 run 被当成一次真实 Ask 记下来了」的证据)。
    tables = ("knowledge_objects", "knowledge_relations", "source_elements",
              "chunks", "sources", "notebooks",
              "ask_jobs", "ask_trace_steps", "answers",
              "retrieval_experiences")

    def counts() -> dict:
        with repo._connect() as db:
            return {
                table: db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                for table in tables
            }

    before = counts()
    _run_point(probe_repo, _synthetic_case(), 1,
               _FakeRealClient(MODEL_SEARCH_DECISION))
    assert counts() == before


# --- (c) 四臂同一状态点 -------------------------------------------------------


def test_the_four_arms_replay_the_same_script_at_the_same_state_point(
    probe_repo,
):
    """(c) **E2 的第二条验收**:四臂前 k 轮的动作序列逐格相同。

    「同一状态点」这句话的可复核形式就是它:同一份剧本被四条臂各从第 1 轮重放
    到第 k 轮,前 k 轮送出去的动作序列必须逐格相同,而且四条臂在那 k 轮里执行
    的检索也必须落成同一批 `attempted` 查询——否则第 k+1 轮那两条消息量的不是
    同一个状态。
    """
    case = _synthetic_case()
    scripted: dict[str, tuple] = {}
    attempted: dict[str, tuple] = {}
    for arm, optimization in PROBE_ARMS:
        point = _run_point(
            probe_repo, case, 2, _FakeRealClient(MODEL_SEARCH_DECISION),
            arm=arm, optimization=optimization)
        scripted[arm] = point.driver.scripted_actions()
        attempted[arm] = tuple(row["query"] for row in point.result.attempted)
    assert len(set(scripted.values())) == 1, scripted
    assert scripted["B"] == ("add_subquery",) * 3
    assert len(set(attempted.values())) == 1, attempted


def test_the_four_arms_differ_in_how_the_forwarded_turn_is_blocked(probe_repo):
    """(c) 后半:第 k+1 轮的 system / user 分块**四臂各不相同**。

    三格判据,各守一条既有形状(PR-2/3/4):

    * `off` 的 system 段是那一轮**现渲染**的动态指令;`prefix_snapshot` 起三条
      臂的 system 段是本 run 的静态目录(S)。所以 S 的字节数在 off 与 P 之间
      必须不同(PR-2 既有的 S 段差分)。
    * 三条前缀臂的消息形状相同(`system(S)` + 一条 `user(C+K+D+T)`),差别在
      K/D 的内容判据:D/L 的 `delta_blocks` 有值而 off/P 缺席。
    * L 只在 D 之上换自评合同,所以它的 S 段比 D 更长而 `delta_blocks` 相同。
    """
    case = _synthetic_case()
    rows: dict[str, dict] = {}
    for arm, optimization in PROBE_ARMS:
        point = _run_point(
            probe_repo, case, 2, _FakeRealClient(MODEL_SEARCH_DECISION),
            arm=arm, optimization=optimization)
        rows[arm] = point.row
    assert rows["B"]["ctx_chars_s"] != rows["P"]["ctx_chars_s"]
    assert rows["P"]["ctx_chars_s"] != rows["D"]["ctx_chars_s"]
    assert rows["D"]["ctx_chars_s"] < rows["L"]["ctx_chars_s"]
    assert rows["B"]["delta_blocks"] is None
    assert rows["P"]["delta_blocks"] is None
    assert rows["D"]["delta_blocks"] == rows["L"]["delta_blocks"]
    assert rows["D"]["delta_blocks"] >= 1
    # 四条臂的 provider-facing 字节数两两不同:布局真的换了。
    byte_totals = [rows[arm]["message_bytes_total"] for arm, _ in PROBE_ARMS]
    assert len(set(byte_totals)) == len(byte_totals), byte_totals


def test_the_five_context_char_columns_each_read_their_own_detail_key():
    """(c) 五格字符数 + 总字节数各读**自己**的 reflect 步 detail 键,不是邻居的
    (T-EX5 评审 P2#2)。

    `REFLECT_CONTEXT_DETAIL_KEYS`(`app/domain/reasoning_trace_stats.py`)是
    写侧真源。此前只有 `ctx_chars_s` / `ctx_bytes_total` 被上面那条四臂差分
    用例咬住,`c`/`k`/`d`/`t` 四格在成功路径上零断言——单独把
    `reflect_detail.get("ctx_chars_c")` 改成 `get("chars_c")` 347 例照绿。这里
    给六个真实键名各喂一个互不相同的值,直接调 `build_probe_row`,任何一格读
    错邻居的键都会在这条用例上现出原形。
    """
    case = _synthetic_case()
    driver = ScriptedReflectDriver(
        case, 0, _FakeRealClient({}), stop_aspect_id="a1")
    driver.forwarded = ForwardedTurn(
        turn=1, messages=(), schema_hint="", raw=None, stats={"status": "ok"})
    reflect_detail = {
        key: 100 + index
        for index, key in enumerate(REFLECT_CONTEXT_DETAIL_KEYS.values())
    }
    assert len(set(reflect_detail.values())) == len(reflect_detail), (
        "sentinel values must be pairwise distinct for this test to mean "
        "anything")
    row = build_probe_row(
        case=case, state_point_index=0, repeat=0, arm="B", optimization="off",
        driver=driver, reflect_detail=reflect_detail, aspects_total=1,
    )
    for code, key in REFLECT_CONTEXT_DETAIL_KEYS.items():
        expected = reflect_detail[key]
        if code == "bytes_total":
            assert row["message_bytes_total"] == expected, code
        else:
            assert row[f"ctx_chars_{code}"] == expected, code


# --- (d) 代理只截 reasoning_agent ---------------------------------------------


class _RecordingClients:
    """真 repo 那一侧的记录替身:数每一次 `chat` / `embedding` / `rerank`。"""

    def __init__(self):
        self.chat_calls: list[str] = []
        self.embedding_calls: list[str] = []
        self.rerank_calls: list[str] = []
        self.settings = "sentinel-settings"

    def chat(self, workload_id):
        self.chat_calls.append(workload_id)
        return f"chat:{workload_id}"

    def embedding(self, workload_id):
        self.embedding_calls.append(workload_id)
        return f"embed:{workload_id}"

    def rerank(self, workload_id):
        self.rerank_calls.append(workload_id)
        return f"rerank:{workload_id}"


def test_the_proxy_only_intercepts_the_reflect_workload():
    """(d) 代理只覆写 `chat("reasoning_agent")`,其余全部原样透传。

    截住全部 workload 是一条很容易顺手写下的「简化」,而它会静默毁掉这批数据:
    检索必须的 `retrieval_query_embedding` 是 E2 剩下的唯一一处真实模型消耗
    (计划 M3),截掉它之后每一条 run 都在一个空的候选池上跑,四臂的第 k+1 轮
    消息于是全都短得一样、差异消失——而没有任何一条断言会红。
    """
    driver = ScriptedReflectDriver(
        _synthetic_case(), 0, _FakeRealClient({}), stop_aspect_id="a1")
    delegate = _RecordingClients()
    proxy = ProbeModelClients(delegate, driver)

    assert proxy.chat(REFLECT_CHAT_WORKLOAD) is driver
    assert delegate.chat_calls == []
    assert proxy.chat("ask_answer") == "chat:ask_answer"
    assert delegate.chat_calls == ["ask_answer"]
    # embedding / rerank 压根不经过代理的分岔,走 `__getattr__` 委托。
    assert proxy.embedding("retrieval_query_embedding") == (
        "embed:retrieval_query_embedding")
    assert proxy.rerank("retrieval_rerank") == "rerank:retrieval_rerank"
    assert delegate.embedding_calls == ["retrieval_query_embedding"]
    assert delegate.rerank_calls == ["retrieval_rerank"]
    # 非方法属性同样委托:`retrieval` 与两个集合服务是
    # `_construct_reasoning_retriever` 真的会从 `model_clients`/`repository`
    # 取的;`settings` 只是借来验证委托本身对任意非方法属性都成立——生产路径上
    # 它是 `_construct_reasoning_retriever` 的**显式参数**,不经这条委托取
    # (T-EX5 评审 P3#12)。
    assert proxy.settings == "sentinel-settings"
    with pytest.raises(AttributeError):
        proxy.no_such_attribute  # noqa: B018
    assert proxy.probe_chat_calls == (REFLECT_CHAT_WORKLOAD, "ask_answer")


def test_a_real_probe_run_asks_the_proxy_for_nothing_but_reflect(probe_repo):
    """(d) 在**真的一条 run** 上再验一次:整条 run 里代理只被问过
    `reasoning_agent`。

    上一条是纯单元,这一条守的是「服务端还有没有第二处会向 `model_clients`
    要一个 chat 客户端」——`reasoning_retrieval` 里除了 `reflect()` 还有一处
    (`:4617`),哪天它在 Ask 检索路径上被走到,这一条会如实说出来。
    """
    repo, notebook_id = probe_repo
    seen: list[str] = []
    original = ProbeModelClients.chat

    def spying_chat(self, workload_id):
        seen.append(workload_id)
        return original(self, workload_id)

    ProbeModelClients.chat = spying_chat  # type: ignore[method-assign]
    try:
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))
    finally:
        ProbeModelClients.chat = original  # type: ignore[method-assign]
    assert seen, "reflect() 至少问过一次"
    assert set(seen) == {REFLECT_CHAT_WORKLOAD}, seen


def test_the_probe_intercepts_the_workload_reflect_actually_asks_for():
    """`REFLECT_CHAT_WORKLOAD` 与服务端那个字面量对号。

    两处对不上时的故障形态是「代理谁都没截住、E2 直接打了真模型一整条 run」
    ——那一批数据不但没有意义,还烧掉了真实预算。所以判据不是「这个串看起来
    对」,而是拿 `reasoning_retrieval` 的源码找那一行。
    """
    import inspect

    from app.services import reasoning_retrieval

    source = inspect.getsource(reasoning_retrieval)
    assert f'self.model_clients.chat("{REFLECT_CHAT_WORKLOAD}")' in source


# --- (e) 越界响亮失败 ---------------------------------------------------------


def test_reaching_turn_k_plus_two_fails_loudly():
    """(e) 第 k+2 轮被触达 ⇒ 抛,不兜底。

    第 k+1 轮返回的是停止决定,`run()` 应当当场收尾;还有下一轮说明那条停止
    决定没被采用,而那一轮的消息形状已经不是要测的那一个。静默兜底(比如再返回
    一条停止决定)会让这一格产出一行看起来完全正常、其实站错了状态点的数据。

    变异(报告里那三条之一):把这一支改成「再返回一条停止决定」⇒ 这条红。
    """
    driver = ScriptedReflectDriver(
        _synthetic_case(), 0, _FakeRealClient(MODEL_SEARCH_DECISION),
        stop_aspect_id="a1")
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
    # 轮 1 = 剧本;轮 2 = 转发 + 停止决定。
    assert json.loads(driver.chat_json(messages, REFLECT_HINT))[
        "next_action"] == "add_subquery"
    assert json.loads(driver.chat_json(messages, REFLECT_HINT))[
        "sufficient"] is True
    with pytest.raises(StateProbeError, match="was not adopted"):
        driver.chat_json(messages, REFLECT_HINT)


def test_the_plan_branch_fails_loudly():
    """(e) plan 分支被触达 ⇒ 抛。

    E2 每例冻了一份意图契约,`run()` 拿到非空 `intent_queries` 就不调 plan 的
    LLM(计划 M3)。真被调到说明这一格的意图没有冻住,那条 run 的第 k+1 轮
    已经不是它自称的那个状态点。判据与 `_SeqLLM` 同一条:
    `"sub_queries" in schema_hint`。
    """
    driver = ScriptedReflectDriver(
        _synthetic_case(), 0, _FakeRealClient({}), stop_aspect_id="a1")
    with pytest.raises(StateProbeError, match="plan LLM"):
        driver.chat_json([{"role": "user", "content": "q"}],
                         '{"sub_queries":[]}')
    assert driver.plan_calls == 1


def test_the_stop_decision_must_actually_be_adopted(probe_repo, monkeypatch):
    """停止决定里那一行自评是**载重**的,不是装饰。

    收尾载荷一格自评都没落账时,`_nudge_missing_assessment` 会把这一轮折成
    `missing_assessment` 并再要一轮 ⇒ run 走到第 k+2 轮 ⇒ 上面那条响亮失败。
    这一条把那件事在真 run 上验出来:把停止决定的 `assessment` 摘掉,整格必须
    炸,而不是静静产出一行站错状态点的数据。
    """
    monkeypatch.setattr(
        ScriptedReflectDriver, "stop_decision_payload",
        lambda self: {"next_action": "answer", "sufficient": True,
                      "arguments": {}, "reason": "probe_stop"})
    with pytest.raises(StateProbeError, match="was not adopted"):
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))


def test_a_forward_failure_is_recorded_as_data_not_as_a_crash(probe_repo):
    """一次死掉的调用是一个**数据点**(§9.2「失败/格式不符单列」),不是崩溃。

    异常只留**类名**:异常消息可能带请求正文,一个字都不留(§8.2)。这一格的
    行照样出得来,`decision_*` 三格如实 unknown。
    """
    client = _FakeRealClient(
        MODEL_SEARCH_DECISION,
        raise_with=RuntimeError("provider said: 这里是请求正文的一段"),
        status="error")
    point = _run_point(probe_repo, _synthetic_case(), 1, client)
    assert point.driver.forwarded is not None
    assert point.driver.forwarded.error == "RuntimeError"
    assert point.driver.forwarded.raw is None
    assert point.row["status"] == "error"
    assert point.row["decision_action"] is None
    assert point.row["decision_sufficient"] is None
    assert point.row["assessment_rows"] is None
    # 墙钟仍然是真的:一次死掉的调用同样烧了墙钟。
    assert point.row["call_wall_ms"] == 42


def test_a_cancellation_during_the_forward_still_propagates(probe_repo):
    """取消是唯一不被收成数据的那一格:`CoreCancellation` 整个基类照旧上抛,
    与 `reasoning_retrieval` 对取消的既有口径同款。"""
    from app.domain.cancellation import AskCancelled

    driver = ScriptedReflectDriver(
        _synthetic_case(), 0,
        _FakeRealClient(MODEL_SEARCH_DECISION, raise_with=AskCancelled()),
        stop_aspect_id="a1")
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
    driver.chat_json(messages, REFLECT_HINT)
    with pytest.raises(AskCancelled):
        driver.chat_json(messages, REFLECT_HINT)


def test_the_orchestrator_refuses_a_run_that_never_reached_the_state_point(
    probe_repo, monkeypatch,
):
    """转发次数不是 1 ⇒ 抛。

    一条在到达状态点之前就收尾的 run(动作面只剩 `answer`、熔断、步数预算用尽)
    产出的行会被归到一个它没跑到的状态点上,而那一行在表里看起来与正常行毫无
    区别。判据用**转发次数**而不是「有没有异常」:前者是那件事本身。
    """
    monkeypatch.setattr(
        ScriptedReflectDriver, "_forward",
        lambda self, *args, **kwargs: None)
    with pytest.raises(StateProbeError, match="forwarded 0 time"):
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))


def test_a_laundered_plan_call_is_refused_after_the_run(probe_repo, monkeypatch):
    """plan 的 LLM 被调到 ⇒ 事后核抛,**哪怕那条异常在路上被洗掉了**。

    驱动器的 plan 分支自己就抛,可那条异常要穿过一整条 fail-open 的链才回到编排
    层(`app/services/query_rewrite.py:151` 的 `except Exception: return fallback`
    是其中一处)。今天靠 `StateProbeError` 继承 `BaseException` 穿过去——而那是
    **单点**:窄成 `Exception` 之后,一次被洗成 `fallback` 的 plan 调用会让转发
    次数、reflect 轮数、「不执行」三道核**全部照过**,产出一行看起来完全正常的
    数据,只是这条 run 的意图并没有被冻住(计划 M3)、还白烧了一次 plan 预算。

    这里直接留下那次调用**唯一的痕迹**(计数),不复现整条洗白链:要测的是
    「这个计数有没有人读」,而 `:569` 此前记了它却没有任何读点。
    """
    original = ScriptedReflectDriver.chat_json

    def bumping(self, messages, schema_hint, **kwargs):
        self.plan_calls = 1
        return original(self, messages, schema_hint, **kwargs)

    monkeypatch.setattr(ScriptedReflectDriver, "chat_json", bumping)
    with pytest.raises(StateProbeError, match="plan LLM was called"):
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))


def test_a_same_round_reflect_retry_makes_the_turn_count_check_fail_loudly(
    probe_repo, monkeypatch,
):
    """reflect 轮数核守的是**重试漂移**,这条把那件事真跑出来。

    `_reflect_v2` 在首次尝试兜底且原因是 `output_budget_exhausted` 时会原样再调
    一次(`reasoning_retrieval:4698`/`:4702`)。那时驱动器数的是**尝试次数**、
    trace 上的 reflect 步数才是**轮数**:第一轮吃掉两步剧本,于是转发提前一个
    状态点发生——这一行会被归到一个它没跑到的状态点上,而转发次数仍然恰好是 1、
    「不执行」照样成立。唯一看得见它的就是轮数核。

    故障形状照抄生产与 `test_reasoning_retrieval._FlakyReflectLLM`:传输层解析
    空正文抛 `MalformedModelResponse(finish_reason="length") from
    ModelJsonRepairError`。**先调一次原方法再抛**是刻意的——那一步剧本与
    `driver.turns` 都已经被这次尝试消耗掉了,这正是漂移的来源。
    """
    from app.core.model_json import (
        ModelJsonRepairError, parse_model_json_object,
    )
    from app.services.model_work import MalformedModelResponse

    original = ScriptedReflectDriver.chat_json
    failed_at: list[int] = []

    def flaky(self, messages, schema_hint, **kwargs):
        payload = original(self, messages, schema_hint, **kwargs)
        if not failed_at:
            failed_at.append(self.turns)
            stats = kwargs.get("call_stats")
            if isinstance(stats, dict):
                stats["finish_reason"] = "length"
            try:
                parse_model_json_object("", schema_hint, allow_repair=True)
            except ModelJsonRepairError as exc:
                raise MalformedModelResponse(finish_reason="length") from exc
            raise AssertionError("空正文必须被传输层判 empty")
        return payload

    monkeypatch.setattr(ScriptedReflectDriver, "chat_json", flaky)
    with pytest.raises(StateProbeError, match="reflect step"):
        _run_point(probe_repo, _synthetic_case(), 1,
                   _FakeRealClient(MODEL_SEARCH_DECISION))
    assert failed_at == [1]


# --- (g) 闭集与隐私 -----------------------------------------------------------


def _one_row(probe_repo, **kwargs) -> dict:
    return _run_point(probe_repo, _synthetic_case(), 1,
                      _FakeRealClient(MODEL_SEARCH_DECISION), **kwargs).row


def test_the_probe_row_is_exactly_the_closed_key_set(probe_repo):
    """(g) 行的键集**逐字**等于 `PROBE_ROW_KEYS`(Q6 那一串)。

    「⊆」不够:少一格会让那一列在整批里静默缺席,而闭集的意义是数据集形状合同。
    """
    row = _one_row(probe_repo)
    assert set(row) == set(PROBE_ROW_KEYS)
    assert_probe_row_closed(row)


def test_planting_the_decision_reason_text_in_a_row_is_red(probe_repo):
    """(g) 隐私:往行里加 `decision_reason` 原文 ⇒ 红。

    两半各挡一件事:闭集挡「多了一个键」,值形状闸挡「往一个允许的键里塞一段
    自由文本」。第二半用例特别重要——一个把模型的理由塞进 `status` 的写法在
    键集上完全合法。
    """
    row = _one_row(probe_repo)
    with pytest.raises(ValueError, match="PROBE_ROW_KEYS"):
        assert_probe_row_closed(
            {**row, "decision_reason": "模型想再查一次元素"})
    with pytest.raises(ValueError, match="unsupported shape"):
        assert_probe_row_closed({**row, "status": "模型说 够了"})


#: 模型在状态点上回的一份**带自评**的检索决定。三格产地判据全靠它:
#: `assessment` 两组合计 3 行,而这一例的方面账本只有 1 个必答方面、驱动器自己
#: 那条停止决定恒 1 行——三个数两两不同,所以「读的是模型那一份」是可复核的。
MODEL_ASSESSED_DECISION = {
    "next_action": "search_elements",
    "sufficient": False,
    "arguments": {"query": MODEL_PICKED_QUERY},
    "reason": "模型想再查一次元素",
    "assessment": {
        "supported": [
            {"aspect_id": "a1", "evidence_keys": ["ck-1"]},
            {"aspect_id": "a2", "evidence_keys": ["ck-2"]},
        ],
        "unresolved": [
            {"aspect_id": "a3", "status": "unknown", "gap": "还没看到得分表"},
        ],
    },
}


def test_the_decision_columns_read_the_model_payload_not_the_trace(probe_repo):
    """(g) Q6 的三格「模型那份决定」列:短码 / bool / 行数各有一条真 run 判据。

    此前这三格在成功路径上零断言:`assessment_rows` 写死 1、只数 `supported`
    一半、`decision_action` 写死 `"answer"` —— 三个变异全绿。而这三列正是设计
    §9.2 要人工核的「动作可执行性 / 是否正确停止 / 方面绑定」的**全部**读数,
    退化成常量之后报告的作者无从分辨。

    三个数两两不同,所以产地是可复核的而不是重言式:

    * 模型那一份自评 **3** 行(`supported` 2 + `unresolved` 1);
    * trace 上那一轮量的是**驱动器自己发的停止决定**,恒 **1** 行;
    * 这一例的方面账本只有 **1** 个必答方面(`aspects_total`)。
    """
    point = _run_point(probe_repo, _synthetic_case(), 1,
                       _FakeRealClient(MODEL_ASSESSED_DECISION))
    assert point.row["decision_action"] == "search_elements"
    assert point.row["decision_sufficient"] is False
    assert point.row["assessment_rows"] == 3
    reflects = [step for step in point.result.trace
                if step.step_type == "reflect"]
    assert reflects[-1].detail.get("assessment_rows") == 1
    assert point.row["aspects_total"] == 1
    # 决定正文(`reason` / `gap` 原文)一个字都不进行。
    assert_probe_row_closed(point.row)


def _three_topic_case(**over: Any):
    contract = copy.deepcopy(_synthetic_raw()["cases"][0]["intent_contract"])
    contract["mandatory_topics"] = [
        {"id": "t1", "title": "循环次数", "question": "循环次数是多少?",
         "retrieval_queries": ["循环次数"]},
        {"id": "t2", "title": "得分表", "question": "得分表长什么样?",
         "retrieval_queries": ["得分表"]},
        {"id": "t3", "title": "评测集口径", "question": "评测集口径是什么?",
         "retrieval_queries": ["评测集口径"]},
    ]
    return _synthetic_case(intent_contract=contract, **over)


def test_aspects_total_reflects_the_ledger_size_not_a_constant(probe_repo):
    """(g) `aspects_total` 是这条 run 的方面账本大小,不是写死的常量
    (T-EX5 评审 P2#6)。

    `aspects_total=1` 写死 ⇒ 347 例照绿(变异 Q20):唯一断言它的
    `test_the_decision_columns_read_the_model_payload_not_the_trace` 恰好用的
    是 1 方面的合成例。这一例冻 3 个必答方面,分母必须如实报 3——这一列正是
    §9.2「方面绑定」人工核的分母,12 例里有 7 例是 2–3 方面。
    """
    point = _run_point(probe_repo, _three_topic_case(), 1,
                       _FakeRealClient(MODEL_SEARCH_DECISION))
    assert point.row["aspects_total"] == 3


def test_a_next_action_in_plain_prose_lands_on_the_unreadable_short_code(
    probe_repo,
):
    """(g) `next_action` 读不成短码 ⇒ 固定短码,既不是原文也不是 `None`。

    `None` 已经被「压根解析不出这份载荷」占了(转发失败那一格),两件事必须分得
    开:一个是「模型选了个读不出来的动作」,另一个是「没有决定可读」。原文更不
    行——那就是一段模型可控的自由文本进了投影行。
    """
    point = _run_point(probe_repo, _synthetic_case(), 1, _FakeRealClient({
        "next_action": "我觉得还得再查一次元素",
        "sufficient": True,
        "arguments": {},
    }))
    assert point.row["decision_action"] == PROBE_UNREADABLE_ACTION
    assert point.row["decision_sufficient"] is True
    # 载荷读出来了、只是没带 `assessment` ⇒ 0 行是一句真话,不是 unknown。
    assert point.row["assessment_rows"] == 0
    assert_probe_row_closed(point.row)


def test_the_row_state_point_is_the_index_not_the_turn(probe_repo):
    """(g) `state_point` 行键是**序号** 0/1/2,不是轮号。

    12 例的真实轮号是 `[1,3,5]` / `[1,3,6]` / `[1,2,5]`。写成轮号之后
    `_bucket_label` 只认得 0/1/2,§9.2 那三档报告会静默塌成「一档 + 一堆
    未分档」,而 `by_state_point` 照样出表、每一格的数都对——没有任何一条断言
    会红。所以这一例的状态点刻意取 `[1,3,4]`:序号与轮号逐格不同。
    """
    case = _synthetic_case(script=[
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第一条", "arguments": {"query": "GSM8K 得分表概述"}},
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第二条", "arguments": {"query": "循环次数设置步骤"}},
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第三条", "arguments": {"query": "得分与循环次数的关系"}},
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第四条", "arguments": {"query": "测试时循环的口径"}},
        {"next_action": "add_subquery", "sufficient": False,
         "reason": "第五条", "arguments": {"query": "得分表的脚注口径"}},
    ], state_points=[1, 3, 4])
    rows = []
    for index in (0, 1, 2):
        point = _run_point(probe_repo, case, index,
                           _FakeRealClient(MODEL_SEARCH_DECISION))
        assert point.row["state_point"] == index
        assert case.state_point_turn(index) == (1, 3, 4)[index]
        rows.append(point.row)
    # 三档因此按序号对号(轮号 1/3/4 里有两个压根不是合法序号)。
    summary = summarize_state_probe(rows)
    assert list(summary["by_state_point"]) == list(STATE_POINT_LABELS)


def test_no_probe_row_or_summary_key_is_cache_hit_shaped(probe_repo):
    """(g) 命名红线:字段名与摘要键名一律不出现 `cache_hit` / 命中率形状。

    这是一条**主动**用例,不只是 docstring(计划 T-EX3 (d) 对 E1 的同一条纪律)。
    `status` 的**值**里仍然可能出现 `cache_hit`(那是 `app/core/llm.py` 写进
    `call_stats` 的既有事实字段,原样透传);红线管的是**键名**,而摘要里那一格
    叫 `local_cache_exit_rows`。

    变异(计划 T-EX10 (b) 的 E2 版):把摘要那一格改名成 `cache_hit_rows` ⇒ 这条红。
    """
    for key in PROBE_ROW_KEYS:
        assert not FORBIDDEN_NAME_SHAPES.search(key), key

    summary = summarize_state_probe([_one_row(probe_repo)])

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not FORBIDDEN_NAME_SHAPES.search(str(key)), f"{path}.{key}"
                walk(value, f"{path}.{key}")

    walk(summary)
    assert "local_cache_exit_rows" in summary


def test_the_summary_produces_no_ratio_shaped_key(probe_repo):
    """摘要**不产出任何比率型结论**(§9.2 / 计划 §5 风险 4/5)。

    E2 的归因边界不允许这一层替读表人下结论:P↔B 的差里混着指令/工具说明的
    布局改动,D↔L 在固定状态点上只看得见净增的那一侧。所以这里只有计数与中位
    数——一个 `ratio` / `pct` / `speedup` 形状的键出现,就说明有人把那条边界
    越过去了。
    """
    summary = summarize_state_probe([_one_row(probe_repo)])
    ratio_shapes = re.compile(r"ratio|pct|percent|speedup|_rate|gain")

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not ratio_shapes.search(str(key)), f"{path}.{key}"
                walk(value, f"{path}.{key}")

    walk(summary)


# --- (h) message_prefix_bytes 恒 None ----------------------------------------


def test_message_prefix_bytes_stays_none_on_every_arm(probe_repo):
    """(h) `message_prefix_bytes` 在 E2 行上**恒 `None`**(Q6)。

    它量的是「上一轮 → 这一轮」的公共前缀,而 E2 每条 run 只在**一轮**上调
    真实模型:之前那 k 轮的消息虽然也被同一套测量量过,却一条都没发出去。
    trace 上那一轮**确实带着**一个数(测量层照常算),这一列刻意不投影它——
    下面那条断言把「trace 有值」与「行里恒 None」两件事同时钉住,免得有人读到
    「恒 None」以为是测量没开。
    """
    case = _synthetic_case()
    for arm, optimization in PROBE_ARMS:
        point = _run_point(probe_repo, case, 2,
                           _FakeRealClient(MODEL_SEARCH_DECISION),
                           arm=arm, optimization=optimization)
        assert point.row["message_prefix_bytes"] is None
        reflects = [step for step in point.result.trace
                    if step.step_type == "reflect"]
        assert reflects[-1].detail.get("message_prefix_bytes") is not None


def test_diffing_two_adjacent_state_points_into_that_column_is_red(probe_repo):
    """(h) 变异:拿同 case 同臂内相邻状态点的字节数**做差**充当它 ⇒ 红。

    那是两条**各自独立**的 run,连 provider 侧的会话都不是同一个;这个差值
    看着完全正常,却回答了另一个问题。`assert_probe_row_closed` 因此把这一格
    升级成硬断言——变异会在**写行**的那一刻就红,而不是等到有人读那张表。
    """
    case = _synthetic_case()
    rows = [
        _run_point(probe_repo, case, index,
                   _FakeRealClient(MODEL_SEARCH_DECISION),
                   arm="D", optimization="prefix_delta").row
        for index in (1, 2)
    ]
    mutated = {
        **rows[1],
        "message_prefix_bytes": (
            rows[1]["message_bytes_total"] - rows[0]["message_bytes_total"]),
    }
    with pytest.raises(ValueError, match="must stay None"):
        assert_probe_row_closed(mutated)
    assert PROBE_ROW_ALWAYS_NONE == ("message_prefix_bytes",)


# --- (j) 压缩边界 -------------------------------------------------------------


def test_the_compaction_boundary_is_false_when_nothing_was_rebuilt(probe_repo):
    """(j) `context_rebuilds == 0` ⇒ `compaction_boundary_reached` 如实 `False`。

    §9.2「缺数据不补造」:第三个状态点没越过压缩边界是一件要被看见的事,不是
    一件要靠调剧本(或调小渲染预算)消灭的事。这份合成剧本的三轮远不到重建
    预算,所以 D 臂如实报 0 / False。
    """
    point = _run_point(probe_repo, _synthetic_case(), 2,
                       _FakeRealClient(MODEL_SEARCH_DECISION),
                       arm="D", optimization="prefix_delta")
    assert point.row["context_rebuilds"] == 0
    assert point.row["compaction_boundary_reached"] is False
    assert point.row["context_fallback"] is False


def test_a_non_delta_arm_reports_unknown_not_false_for_the_boundary(probe_repo):
    """`off` / `prefix_snapshot` 压根没有 `context_rebuilds` / `context_fallback`
    这两个观测 ⇒ 这两格是 `None`,**不是** `False`。

    unknown ≠ False 是这个仓库的一条既有纪律:`False` 是「量到了,答案是没有」,
    `None` 是「没量」。折成 `False` 会让 off/P 两条臂在摘要里凭空多出一批
    「没越过压缩边界」/「没有不可逆回退过」的格子,而它们从来没被问过这个问题
    ——`context_fallback` 这一半此前零断言,`_flag` 退化成 `return bool(raw)`
    照绿(T-EX5 评审 P3#10)。P 压根没有回退机制,`context_fallback` 折成
    `False` 尤其容易被误读成「P 从没不可逆回退过」这一句它没资格说的话。
    """
    for arm, optimization in (("B", "off"), ("P", "prefix_snapshot")):
        point = _run_point(probe_repo, _synthetic_case(), 2,
                           _FakeRealClient(MODEL_SEARCH_DECISION),
                           arm=arm, optimization=optimization)
        assert point.row["context_rebuilds"] is None
        assert point.row["compaction_boundary_reached"] is None
        assert point.row["context_fallback"] is None


def _fake_row(**over) -> dict:
    row = {key: None for key in PROBE_ROW_KEYS}
    row.update({
        "case_key": "syn-b-scores", "state_point": 0, "repeat": 0,
        "arm": "D", "optimization": "prefix_delta", "status": "ok",
        "call_wall_ms": 100, "call_attempts": 1, "response_chars": 200,
        "message_bytes_total": 1000, "message_prefix_bytes": None,
        "ctx_chars_s": 1, "ctx_chars_c": 2, "ctx_chars_k": 3,
        "ctx_chars_d": 4, "ctx_chars_t": 5,
        "decision_action": "answer", "decision_sufficient": True,
        "assessment_rows": 1, "aspects_total": 1,
        "context_rebuilds": 0, "context_fallback": False, "delta_blocks": 2,
        "compaction_boundary_reached": False,
    })
    row.update(over)
    assert_probe_row_closed(row)
    return row


def test_the_summary_reports_the_three_state_point_buckets_separately():
    """§9.2「对初始状态、后续状态、压缩边界分别报告」——按状态点**序号**分档。

    序号读不出来的行落 `unbucketed`,不猜它属于哪一档。
    """
    rows = [
        _fake_row(state_point=0, call_wall_ms=10),
        _fake_row(state_point=1, call_wall_ms=20),
        _fake_row(state_point=2, call_wall_ms=30),
        _fake_row(state_point=7, call_wall_ms=40),
    ]
    summary = summarize_state_probe(rows)
    assert set(summary["by_state_point"]) == {*STATE_POINT_LABELS, "unbucketed"}
    assert summary["by_state_point"]["initial"]["D"]["call_wall_ms_p50"] == 10
    assert summary["by_state_point"][
        "compaction_boundary"]["D"]["call_wall_ms_p50"] == 30
    assert summary["rows_total"] == 4


def test_the_summary_lists_failures_cache_exits_and_unreached_boundaries():
    """(j) 后半 + §9.2「失败/格式不符单列」。

    三类各单列,一类都不许混进主统计:失败(取消 / 出错)、本地响应缓存出口
    (转发时显式 `bypass_cache=True`,所以它**结构上**该是 0;不是 0 就说明那条
    结构事实被谁破掉了)、以及 `compaction_boundary_reached=False` 的格子。
    """
    rows = [
        _fake_row(status="ok", context_rebuilds=1,
                  compaction_boundary_reached=True),
        _fake_row(status="error", compaction_boundary_reached=False),
        _fake_row(status="cancelled", compaction_boundary_reached=False),
        _fake_row(status="cache_hit", context_rebuilds=1,
                  compaction_boundary_reached=True),
        _fake_row(status=None, context_rebuilds=None,
                  compaction_boundary_reached=None),
    ]
    summary = summarize_state_probe(rows)
    assert summary["failed_rows"] == 2
    assert summary["local_cache_exit_rows"] == 1
    assert summary["status_unknown_rows"] == 1
    assert summary["compaction_boundary_not_reached_rows"] == 2
    assert summary["compaction_boundary_unknown_rows"] == 1
    cell = summary["by_state_point"]["initial"]["D"]
    assert cell["n_ok"] == 1 and cell["n_failed"] == 2
    assert cell["n_local_cache_exit"] == 1
    assert cell["n_boundary_reached"] == 2
    assert cell["n_boundary_not_reached"] == 2
    assert cell["n_boundary_unknown"] == 1


def test_the_summary_tells_absent_decisions_from_unreadable_ones():
    """(j) `n_decision_absent`(压根没有载荷可读)与
    `n_decision_unreadable_action`(载荷读出来了、但 `next_action` 不是短码)是
    两件事,不共用一个格子(T-EX5 评审 P2#4)。

    转发失败 / 非 JSON 落 `decision_action=None`,已经被 `n_failed` 数过一遍;
    模型给了个读不出来的动作落 `decision_action=PROBE_UNREADABLE_ACTION`,
    §9.2「格式不符」单指后者。此前两者共用一格 `n_decision_unreadable`、判据是
    `decision_action is None`——数 `PROBE_UNREADABLE_ACTION` 那一半反而全绿。
    """
    rows = [
        _fake_row(decision_action=None),
        _fake_row(decision_action=None),
        _fake_row(decision_action=PROBE_UNREADABLE_ACTION),
        _fake_row(decision_action="answer"),
    ]
    cell = summarize_state_probe(rows)["by_state_point"]["initial"]["D"]
    # 两格计数刻意不同(2 对 1),这样把两个判据互换的变异也会被这条用例咬住
    # ——两格计数若碰巧相同,一次判据互换会悄悄绿过去。
    assert cell["n_decision_absent"] == 2
    assert cell["n_decision_unreadable_action"] == 1


def test_the_summary_medians_use_nearest_rank_and_count_their_samples():
    """中位数取**最近秩**(偶数条取偏小的那个),不取两数平均——平均出来的
    毫秒数不是任何一次真实调用的墙钟。缺席的观测不折 0,只是不进样本。"""
    rows = [
        _fake_row(call_wall_ms=10),
        _fake_row(call_wall_ms=20),
        _fake_row(call_wall_ms=30),
        _fake_row(call_wall_ms=40),
        _fake_row(call_wall_ms=None),
    ]
    cell = summarize_state_probe(rows)["by_state_point"]["initial"]["D"]
    assert cell["call_wall_ms_p50"] == 20
    assert cell["call_wall_ms_n"] == 4
    assert cell["n_rows"] == 5


def test_the_summary_survives_a_batch_where_every_call_failed():
    """一半(乃至全部)调用失败的输入下摘要仍然出表,失败单列、中位数缺席。"""
    rows = [_fake_row(status="error", call_wall_ms=None) for _ in range(3)]
    summary = summarize_state_probe(rows)
    assert summary["failed_rows"] == 3
    cell = summary["by_state_point"]["initial"]["D"]
    assert cell["call_wall_ms_p50"] is None
    assert cell["call_wall_ms_n"] == 0


def test_count_excludes_bool_even_though_bool_is_an_int_subclass():
    """`isinstance(True, int)` 为真,`_count` 必须单独挡掉 `bool`——不挡的话
    一个写错类型的字段会静默落成 `1`(T-EX5 评审 P3#11)。"""
    assert _count(True) is None
    assert _count(False) is None
    assert _count(1) == 1
    assert _count(0) == 0
    assert _count(None) is None
    assert _count("1") is None


# --- (i) manifest 事实 --------------------------------------------------------


def _e2_facts(cases, **over):
    """一份过闸的 E2 manifest 事实,只让调用方换它关心的那几格。"""
    kwargs = dict(
        cases=cases, arms=["B", "P", "D"], repeats=2,
        case_set_digest_code="0" * 16,
        optimization_by_arm={"B": "off", "P": "prefix_snapshot",
                             "D": "prefix_delta"},
        corpus_signature_by_cell={"A_nokg": "a1b2c3d4e5f60718",
                                  "B_kg": "0718f6e5d4c3b2a1"},
        common_baseline="pr1_baseline",
        order="case:state_point:repeat:arm",
        code_sha="0123456789abcdef0123456789abcdef01234567",
        started_at="2026-09-11T00:00:00Z",
        finished_at="2026-09-11T01:00:00Z",
        arm_order_seed=None,
        model_contract={"workload": "reasoning_agent"},
        budgets={"reasoning_timeout_seconds": 90},
    )
    kwargs.update(over)
    return state_probe_manifest_facts(**kwargs)


def test_the_manifest_facts_pass_every_manifest_gate():
    """(i) E2 的 manifest 事实过 `assert_manifest` 的四道闸。

    `matrix` 的四个必填子键是**维度基数**(int):`cases × state_points × arms ×
    repeats` 相乘就是这一批的总 run 数,与 dry-run 逐字钉死的规模数同源
    (`REQUIRED_MATRIX_KEYS_BY_CHANNEL["e2"]`);第五个子键 `planned_runs` 就是
    那个乘积本身。
    """
    cases, digest = load_state_probe_case_set()
    facts = _e2_facts(cases, case_set_digest_code=digest)
    assert_manifest(facts)
    assert facts["channel"] == "e2"
    assert facts["matrix"] == {
        "cases": 12, "state_points": 3, "arms": 3, "repeats": 2,
        "planned_runs": 216}
    # 12 × 3 × 3 × 2 = 216 —— 设计 §9.2 的三臂规模。`planned_runs` 是那个乘积
    # 本身(T-EX8 质量评审拍板的额外子键),不是第五个维度:它让 manifest 与
    # `state-probe-*.jsonl` 的行数对账不必靠读表人手算四个数的积。
    matrix = facts["matrix"]
    assert (matrix["cases"] * matrix["state_points"]
            * matrix["arms"] * matrix["repeats"]) == 216
    assert matrix["planned_runs"] == 216
    assert isinstance(matrix["planned_runs"], int)
    assert not isinstance(matrix["planned_runs"], bool)
    assert facts["stopped_by_budget"] is False
    # E1 专属的 `sample_digest` 不该出现在 E2 的 manifest 里。
    assert "sample_digest" not in facts


def test_the_planned_runs_subkey_tracks_the_four_cardinalities():
    """`planned_runs` 是四个基数的**乘积**,不是一个写死的规模数。

    写死 216 在三臂那一批上完全看不出来——而四臂那一批(计划里 B/P/D/L 的
    完整矩阵)是 288。这条换两个维度各验一次:臂数与重复轮数。
    """
    cases, _ = load_state_probe_case_set()
    four_arms = _e2_facts(
        cases, arms=["B", "P", "D", "L"],
        optimization_by_arm={"B": "off", "P": "prefix_snapshot",
                             "D": "prefix_delta", "L": "prefix_delta_lean"})
    assert_manifest(four_arms)
    assert four_arms["matrix"]["arms"] == 4
    assert four_arms["matrix"]["planned_runs"] == 288
    single = _e2_facts(cases, arms=["B"], repeats=1,
                       optimization_by_arm={"B": "off"})
    assert_manifest(single)
    assert single["matrix"]["planned_runs"] == 36


def test_the_manifest_facts_refuse_an_empty_case_set():
    with pytest.raises(ValueError, match="at least one case"):
        state_probe_manifest_facts(
            cases=[], arms=["B"], repeats=1, case_set_digest_code="0" * 16,
            optimization_by_arm={"B": "off"}, corpus_signature_by_cell={},
            common_baseline="pr1_baseline", order="x",
            code_sha="0" * 40, started_at="t", finished_at="t")


# --- 动作步闭集的对账守卫 -----------------------------------------------------


def test_the_action_step_closed_set_covers_every_trace_step_type():
    """两份闭集合起来必须覆盖服务端全部 `step_type=` 字面量。

    「不执行」这条验收是按 `EXECUTED_ACTION_STEP_TYPES` 判的。服务端哪天新增
    一个动作步而这里没跟上,那条断言就会变成一条**只对旧动作成立**的判断——
    模型选的新动作被执行了,而没有任何一条用例会红。所以判据是拿源码对号,
    不是拿一份手抄的清单自证。
    """
    import inspect

    from app.services import reasoning_retrieval

    source = inspect.getsource(reasoning_retrieval)
    literals = set(re.findall(r'step_type="([a-z_]+)"', source))
    assert literals, "没有从源码里找到任何 step_type 字面量"
    known = EXECUTED_ACTION_STEP_TYPES | NON_ACTION_STEP_TYPES
    assert literals <= known, literals - known
    # 反向:闭集里不许有服务端压根不写的步名(一份长期没人维护的清单会让
    # 「覆盖」这句话越来越空)。
    assert known <= literals, known - literals
    assert not (EXECUTED_ACTION_STEP_TYPES & NON_ACTION_STEP_TYPES)


def test_the_summary_buckets_come_out_in_reading_order():
    """三档按 `STATE_POINT_LABELS`(初始 → 后续 → 压缩边界)排,不按字母序。

    字母序会把「压缩边界」排到最前面,而这份摘要是要被人从上往下读的
    (§9.2「对初始状态、后续状态、压缩边界分别报告」)。
    """
    rows = [_fake_row(state_point=index) for index in (2, 0, 1, 5)]
    summary = summarize_state_probe(rows)
    assert list(summary["by_state_point"]) == [
        "initial", "follow_up", "compaction_boundary", "unbucketed"]
    # 一档没有行时不出空格子(而不是出一个 n_rows=0 的假格)。
    thin = summarize_state_probe([_fake_row(state_point=1)])
    assert list(thin["by_state_point"]) == ["follow_up"]


def test_failed_call_durations_never_enter_the_latency_median():
    """失败/超时格带数值墙钟,但主统计只从成功格取(codex #709 R3 P2)。

    三格 90 秒超时 + 一格 20 ms 成功:`call_wall_ms_p50` 必须是 20、`_n` 是 1,
    `n_failed` 仍数到 3;上下文读数(`ctx_chars_s`)在调用之前就定型,四行都算。
    变异:把 `PROBE_SUMMARY_OK_ONLY_KEYS` 清空 ⇒ p50 变成 90000、n 变成 4 ⇒ 红。
    """
    from app.eval.reflect_state_probe import (
        PROBE_OK_STATUS, PROBE_SUMMARY_OK_ONLY_KEYS, _summarize_cell,
    )

    def _row(status: str, wall: int) -> dict:
        return {
            "status": status, "call_wall_ms": wall, "response_chars": wall,
            "ctx_chars_s": 100, "decision_action": None,
            "compaction_boundary_reached": None,
        }

    rows = [_row("error", 90000), _row("error", 90000), _row("error", 90000),
            _row(PROBE_OK_STATUS, 20)]
    cell = _summarize_cell(rows)
    assert cell["n_failed"] == 3 and cell["n_ok"] == 1
    assert cell["call_wall_ms_p50"] == 20 and cell["call_wall_ms_n"] == 1
    assert cell["response_chars_p50"] == 20 and cell["response_chars_n"] == 1
    assert cell["ctx_chars_s_p50"] == 100 and cell["ctx_chars_s_n"] == 4
    assert PROBE_SUMMARY_OK_ONLY_KEYS == frozenset({"call_wall_ms", "response_chars"})
    # 全失败的格:主统计缺席而不是 90000。
    all_failed = _summarize_cell(rows[:3])
    assert all_failed["call_wall_ms_p50"] is None and all_failed["call_wall_ms_n"] == 0

