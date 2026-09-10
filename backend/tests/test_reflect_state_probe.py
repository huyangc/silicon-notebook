"""E2(固定状态真实决策)的驱动器、代理与 12 例 case 集。

计划真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
§2 Q5/Q6 与 §3 T-EX5 / T-EX6;设计真源
`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md` §9.2。

本文件目前只有**形态覆盖对账 + 逐例形状校验**(T-EX6)这一节;它刻意**不**经过
T-EX5 的 `load_state_probe_cases`:那个加载器是「畸形当场响亮失败」的实现,拿它
来对账等于让被测者自己出考题——加载器哪天把一条约束松掉,这一节照样绿。所以
这里用自己的 `_load_cases_raw()` 直接读 JSON,判据逐条从计划的 Q5/T-EX6 抄下来。
T-EX5 落地后,驱动器与代理那一节(自带 `load_state_probe_cases` 的用例族)会并入
本文件,两节各自守自己的那一半。

## 汇合义务(T-EX6 评审留档,T-EX5 落地时对齐)

1. `state_probes.json` 每例的两个新键 `question_key` / `probe_shape`;
2. 剧本步的可选键 `assessment`(值形状见 `test_each_case_assessment_only_references_its_own_aspects`);
3. `settings_overrides` 白名单收窄到两个检索上限键
   (`reasoning_max_element_searches` / `reasoning_max_chunk_searches`),
   渲染预算键(`reasoning_reflect_state_chars` /
   `reasoning_reflect_evidence_chars_by_effort`)任何 case 都不得覆盖;
4. `zero_hit` 形态的哨兵前缀 `absent_probe.`;
5. `STATE_PROBES_PATH` 归 `app.eval.reflect_t0` 包导出,与 `QUESTIONS_PATH`
   同处——不要在 T-EX5 的加载器模块里另起一份。
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
    """12 例、八种形态逐格对上计划 T-EX6 的表。

    变异:删掉一例(或把两例的 `probe_shape` 改成同一个)⇒ 这条红。只断言
    「每种形态至少一例」的话,一个 8 例的 case 集照样绿,而 §9.2 要的矩阵是
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
    `QUESTIONS_PATH` 派生而不是自己再拼一遍 —— 题集搬家时这条会红,而不是留下
    一个指向空气的常量。"""
    assert STATE_PROBES_PATH.exists()
    assert STATE_PROBES_PATH.parent == QUESTIONS_PATH.parent
    assert STATE_PROBES_PATH.name == "state_probes.json"
    assert Path(STATE_PROBES_PATH).is_file()


# --- (e) 剧本步真的解析成它声明的那个动作 -------------------------------------


#: 剧本步的键闭集,按 `parse_reflect_v2` 实际读的键定(`app.services.
#: reasoning_retrieval.parse_reflect_v2`):`next_action`/`sufficient`/
#: `arguments` 是必填,`reason`/`assessment` 是它认但可以缺省的两个可选键。
#: 闭集外的键(比如拼错的 `assesment`)不会报错——它只是被 `dict.get` 静默
#: 忽略——所以形状判据要在 fixture 这一侧堵。
SCRIPT_STEP_REQUIRED_KEYS = frozenset({"next_action", "sufficient", "arguments"})
SCRIPT_STEP_OPTIONAL_KEYS = frozenset({"reason", "assessment"})


def _full_house_capability_facts(*, kg_in_scope: bool) -> "ReflectCapabilityFacts":
    """按语料格(kg / 无图)构造满额度事实,预算给到不可能被剧本吃穿的量。

    与 `test_reasoning_retrieval._full_house_facts` 同构造(全部通道开着、预算
    远超剧本长度),只按 `corpus_cell` 切一个变量。(c) 那条用例已经钉死剧本长度
    ≤ `min(standard, deep).max_reasoning_steps == 8`,所以任何 `*_left` 给到
    两位数就不可能因为预算耗尽把一次合法请求判成不可用——这里刻意**不**建模
    `settings_overrides`(比如 `tool_exhausted` 例的元素额度=1):那是 run() 的
    执行期状态机要管的事,这一节只回答"标签对了、剧本每一步是否也解析成模型
    该看到的那个动作"。
    """
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.reasoning_actions import ReflectCapabilityFacts

    return ReflectCapabilityFacts(
        kg_in_scope=kg_in_scope, scope_restricted=False, has_candidates=True,
        chunk_search_active=True, exact_lookup_active=True, ppr_active=True,
        community_active=True, enumeration_active=True,
        consult_memory_active=True, outline_active=True,
        element_searches_left=20, chunk_searches_left=20,
        exact_lookups_left=20, ppr_left=20, follow_chain_left=20,
        consult_left=20, outline_updates_left=20, enum_rows_left=2_000,
        enum_pages_left=40, enum_payload_left=2_560_000,
        element_kinds=tuple(ENUMERABLE_ELEMENT_KINDS),
        object_types=tuple(ENUMERABLE_KG_OBJECT_TYPES),
        last_turn=False, outline_repair_available=False,
        terminal_overflow_repair=False,
    )


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
    """剧本每一轮真的过 `parse_reflect_v2`,解出的动作与那一轮声明的一致。

    此前的用例只校 `next_action ∈ 闭集`、`sufficient is False`、`arguments`
    是 dict,止步于键的类型——一个 case 声明 `next_action: "search_elements"`
    却因为参数本身不合法而被解析成 `__reflect_invalid__` 伪动作,那些用例照样
    绿。这条按语料格(kg / 无图)构造满额度 `ReflectCapabilityFacts`,把每一步
    的原始字典喂给真 `parse_reflect_v2`,断言解出来的 `next_action` 就是那一步
    声明的那个、且不是 `__reflect_invalid__`。

    变异(均已手工验证按预期变红,未落盘):
    - `sf-a-gsm8k` 第 3 轮的 `arguments.prefer` 从 `"balanced"` 改成
      `"bogus"` ⇒ `invalid_argument:prefer`;
    - `sf-b-jamba-ratio` 第 3 轮的 `arguments.types` 从 `["claim"]` 改成字符串
      `"claim"` ⇒ `invalid_argument:types`;
    - `lc-b-cost-survey` 第一步的 `arguments.object_type` 从 `"procedure"`
      改成 `"method"`(不在 `ENUMERABLE_KG_OBJECT_TYPES` 白名单里)⇒
      `invalid_argument:object_type`;
    - `ro-b-source-roster` 第一步的 `arguments.collection` 从 `"sources"`
      改成 `"kg_objects"` ⇒ `invalid_argument:collection`。
    """
    from app.services.reasoning_actions import build_reflect_capabilities
    from app.services.reasoning_retrieval import (
        REFLECT_INVALID_ACTION, parse_reflect_v2,
    )

    case = _cases_by_key()[case_key]
    facts = _full_house_capability_facts(
        kg_in_scope=case["corpus_cell"] == "B_kg")
    caps = build_reflect_capabilities(facts)
    for index, step in enumerate(case["script"], 1):
        decision = parse_reflect_v2(step, caps)
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
