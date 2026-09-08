"""reflect v2 开闸前 A/B(T-AB2)的门内用例。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-ab-design_zh.md`
(§5 rig 形状、§7 投影与隐私、§10 验收)。

**rig 本体要网络与真实模型,不进标准门**(沿用 T0 的边界);进门的是:

* `backend/app/eval/reflect_ab.py` 的全部纯逻辑——gold 加载与校验、三跳锚点
  解析的 unknown 路径、完整性断言正则的真/假阳性、成本三键的时间窗切片、配对;
* `ab` 子命令的 `--dry-run` 枚举与预检;
* 隐私守卫:写出去的每一行 `set(row) ⊆ AB_PROJECTION_KEYS`,且不含
  `question` / `answer` / `summary` / `reason` / `title` / `*_id`。

用例里**一次真实模型都不调**:`_run_ab_unit` 那条路把 `run_ab_once` 换成一个
返回假 `AskResponse` 的替身(与 T0 用例把假模型绑进 `rrepo` 同一条口径——不为
一条编排路径付一次真实 provider 往返)。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.reasoning_trace_stats import RUN_PROJECTION_KEYS, assert_projection_values
from app.eval.reflect_ab import (
    AB_ONLY_KEYS,
    AB_PROJECTION_KEYS,
    DEFERRED_JUDGED_KEYS,
    UNKNOWN_USAGE,
    AbGold,
    GoldError,
    assert_ab_closed,
    assert_arm_matches_evidence,
    base_question_key,
    completeness_claim_candidate,
    count_anchors_on_gold,
    count_citations_out_of_scope,
    count_invalid_tool_calls,
    count_unresolved_anchors,
    gold_for,
    load_ab_gold,
    mark_paired,
    pair_id,
    project_ab_run,
    resolve_gold_sources,
    slice_llm_usage,
)
from app.eval.reflect_t0 import load_questions
# `search` 的接线用例借的是 reasoning 检索自己那套测试替身;`ab` 只借 `rrepo`
# (一个 SQLite 上的真 repo)与建两个 KG 节点的那个 helper——协议对号那条断言
# 不需要模型答话就能验。
from tests.test_reasoning_retrieval import (  # noqa: F401 — rrepo 是 fixture
    _seed_two_nodes,
    rrepo,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    """按路径加载 `scripts/` 里的脚本模块(它们不是包的一部分)。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rig = _load("reflect_shadow_rig")


# ---------------------------------------------------------------------------
# 闭集键与隐私守卫(§7.3;§10-2「隐私守卫变异必红」)
# ---------------------------------------------------------------------------

#: 一行投影里**绝不允许**出现的键名片段。`*_id` 那条按后缀判:哈希桶
#: (`merge_key`)是单向的,不在此列,而它的键名恰好也不以 `_id` 结尾。
FORBIDDEN_KEY_MARKERS = ("question", "answer", "summary", "reason", "title")


def test_ab_keys_extend_t0_and_do_not_replace_it():
    """`AB_PROJECTION_KEYS = RUN_PROJECTION_KEYS ∪ §7.1`(§7.1 首句)。

    A/B 跑的就是同一条 `ReasoningRetriever.run`,T0 那一半原封不动;这条断言挡
    的是「A/B 侧另起一份投影」——那会让两批数据没法放进同一张表。
    """
    assert RUN_PROJECTION_KEYS < AB_PROJECTION_KEYS
    assert AB_PROJECTION_KEYS == RUN_PROJECTION_KEYS | AB_ONLY_KEYS
    assert not (RUN_PROJECTION_KEYS & AB_ONLY_KEYS)


def test_no_projection_key_can_carry_free_text_by_its_name():
    """闭集里不许有 `question`/`answer`/`summary`/`reason`/`title`/`*_id`。

    这一条是形状层的第一道闸(第二道是运行期的 `assert_ab_closed` /
    `assert_projection_values`)。`finish_reason_codes` 是**允许**的:它是一张
    「短码 → 次数」的计数表,不是模型写的那段 reason 自由文本——所以按整词而
    不是按子串判。
    """
    for key in AB_PROJECTION_KEYS:
        assert not key.endswith("_id"), key
        parts = set(key.split("_"))
        assert not (parts & set(FORBIDDEN_KEY_MARKERS)) or key in {
            "question_key", "finish_reason_codes", "answer_chars",
            "answer_empty", "skip_reasons", "fallback_reasons",
            "termination_reason",
        }, key


def test_adding_an_answer_key_turns_the_guard_red():
    """§10-2 的变异用例:往投影里加一个 `answer` 键,守卫必红。"""
    row = {"arm": "v2", "repeat": 1}
    assert_ab_closed(row)
    with pytest.raises(ValueError, match="AB_PROJECTION_KEYS"):
        assert_ab_closed({**row, "answer": "模型写的那一大段"})


def test_free_text_inside_an_allowed_key_is_still_refused():
    """键名对、值是自由文本,一样进不去。

    `assert_projection_values` 不看键名、只看值的形状,所以自由文本挂在闭集内
    哪个键下都拦得住——A/B 侧因此不必再写一份值校验。
    """
    with pytest.raises(ValueError):
        assert_projection_values({"model_contract": "只看 Qwen-VL 和 DeepSeek"})


# ---------------------------------------------------------------------------
# gold 的加载与校验(§3.2;T-AB1 的「加载与校验」那一半)
# ---------------------------------------------------------------------------


def _questions(*rows: dict) -> dict:
    return {"ask": list(rows), "report": []}


def test_load_ab_gold_reads_the_shipped_question_set():
    """题集是同一份 `questions.json`(§3.1 原地扩充,不复制不改名)。"""
    gold = load_ab_gold(load_questions())
    assert len(gold) == 34
    assert gold["A-q01"].gold_section_path == ("Abstract", "1. Scaling")
    # B 侧的 gold 由 T-AB1 补;现在还没有,而「还没有」必须表现成空 gold(相关
    # 键 unknown),不是加载失败。
    assert gold["B-q06"].gold_sources == ()
    assert gold["B-q06"].gold_facts == ()


def test_gold_facts_over_the_row_cap_fail_loudly():
    with pytest.raises(GoldError, match="至多 3 条"):
        load_ab_gold(_questions({
            "key": "B-q06", "corpus": "B",
            "gold_facts": ["一", "二", "三", "四"],
        }))


def test_gold_facts_over_the_char_cap_fail_loudly():
    with pytest.raises(GoldError, match="至多 40 字"):
        load_ab_gold(_questions({
            "key": "B-q06", "corpus": "B", "gold_facts": ["长" * 41],
        }))


def test_malformed_gold_shapes_fail_loudly_instead_of_being_cleaned():
    """写成字符串、或混进 null,都必须抛——不做「尽力而为」的清洗。

    悄悄清洗的代价是这道题在整批数据里安静地少算命中,而那正是 §3.2 要求
    「跑批之前响亮失败」的那类事故。
    """
    with pytest.raises(GoldError, match="必须是字符串列表"):
        load_ab_gold(_questions({
            "key": "B-q06", "corpus": "B", "gold_sources": "KIVI",
        }))
    with pytest.raises(GoldError, match="非空字符串"):
        load_ab_gold(_questions({
            "key": "B-q06", "corpus": "B", "gold_sources": ["KIVI", None],
        }))


def test_english_variants_share_the_chinese_question_gold():
    """题面语言不改变那条事实落在哪一节、出自哪一篇。"""
    gold_by_key = load_ab_gold(load_questions())
    assert base_question_key("A-q01-en") == "A-q01"
    assert gold_for(gold_by_key, "A-q01-en") is gold_for(gold_by_key, "A-q01")


def test_gold_sources_must_resolve_to_exactly_one_source():
    """§5.5-6:每个短名恰好命中一个来源,0 个或 ≥2 个都在跑批之前失败。"""
    gold = AbGold(question_key="B-q06", corpus="B", gold_sources=("KIVI",))
    rows = [
        {"id": "src-1", "title": "src-193b32d112_06_KIVI_mineru.md"},
        {"id": "src-2", "title": "src-1675b22a0f_Jamba_mineru.md"},
    ]
    assert resolve_gold_sources(gold, rows) == {"KIVI": "src-1"}
    with pytest.raises(GoldError, match="命中 0 个"):
        resolve_gold_sources(gold, rows[1:])
    with pytest.raises(GoldError, match="命中 2 个"):
        resolve_gold_sources(gold, rows + [{"id": "src-3", "title": "KIVI-v2.md"}])


# ---------------------------------------------------------------------------
# `anchors_on_gold` 的三跳解析(§7.1;§10-1「任一跳失败 ⇒ unknown」)
# ---------------------------------------------------------------------------

_A_GOLD = AbGold(
    question_key="A-q08", corpus="A", gold_section_path=("4.1", "Abstract"),
)
_B_GOLD = AbGold(question_key="B-q06", corpus="B", gold_sources=("KIVI",))


def _anchor(**fields: str) -> dict:
    return {"key": "k1", "element_id": "", "source_id": "", **fields}


def test_anchors_on_gold_counts_distinct_elements_under_the_gold_section():
    hits = count_anchors_on_gold(
        [_anchor(key="k1", element_id="e1"), _anchor(key="k2", element_id="e2"),
         _anchor(key="k3", element_id="e1")],
        _A_GOLD,
        element_sections={"e1": "4.1 Setup", "e2": "6. Related work"},
        source_titles={},
    )
    assert hits == 1


def test_anchors_on_gold_is_unknown_when_the_lookup_table_is_missing():
    """rig 那一步查库失败 ⇒ 整键 unknown,而不是「一个都没命中」。"""
    assert count_anchors_on_gold(
        [_anchor(element_id="e1")], _A_GOLD,
        element_sections=None, source_titles={},
    ) is None


def test_anchors_on_gold_is_unknown_when_one_element_does_not_resolve():
    """带了 `element_id`、却在查询表里查不到 ⇒ 一次真实的解析失败 ⇒ unknown。"""
    assert count_anchors_on_gold(
        [_anchor(key="k1", element_id="e1"), _anchor(key="k2", element_id="missing")],
        _A_GOLD,
        element_sections={"e1": "4.1 Setup"}, source_titles={},
    ) is None


def test_an_element_with_an_empty_section_path_is_unknown_not_a_miss():
    """元素在库里、`metadata.section_path` 是空串 ⇒ 前缀匹配无从谈起 ⇒ unknown。"""
    assert count_anchors_on_gold(
        [_anchor(element_id="e1")], _A_GOLD,
        element_sections={"e1": ""}, source_titles={},
    ) is None


def test_an_anchor_without_an_element_id_is_not_a_failed_hop():
    """KG 对象锚点没有 `element_id`——那是另一类锚点,不是解析失败。

    把它当成失败会让几乎每个 run 的这一列都变成 unknown,等于把这条判据废掉;
    它照常进 `anchors_total` 的分母,只是不进分子。
    """
    hits = count_anchors_on_gold(
        [_anchor(key="k1", element_id="e1"), _anchor(key="k2")],
        _A_GOLD,
        element_sections={"e1": "Abstract"}, source_titles={},
    )
    assert hits == 1


def test_anchors_on_gold_uses_source_titles_for_the_b_shape():
    hits = count_anchors_on_gold(
        [_anchor(key="k1", source_id="s1"), _anchor(key="k2", source_id="s2")],
        _B_GOLD,
        element_sections={},
        source_titles={"s1": "src-193b32d112_06_KIVI_mineru.md",
                       "s2": "src-1675b22a0f_Jamba_mineru.md"},
    )
    assert hits == 1
    assert count_anchors_on_gold(
        [_anchor(source_id="s1")], _B_GOLD,
        element_sections={}, source_titles=None,
    ) is None
    assert count_anchors_on_gold(
        [_anchor(source_id="ghost")], _B_GOLD,
        element_sections={}, source_titles={"s1": "KIVI.md"},
    ) is None


def test_a_question_without_gold_skips_the_metric_instead_of_scoring_zero():
    """§3.2:没 gold 的题在该指标上跳过,不计 0。"""
    assert count_anchors_on_gold(
        [_anchor(element_id="e1")], None,
        element_sections={"e1": "4.1"}, source_titles={},
    ) is None
    empty = AbGold(question_key="B-q01", corpus="B")
    assert count_anchors_on_gold(
        [_anchor(element_id="e1")], empty,
        element_sections={"e1": "4.1"}, source_titles={},
    ) is None


# ---------------------------------------------------------------------------
# 引用有效性与权限(§7.1;§9 硬判据 1/2)
# ---------------------------------------------------------------------------


def test_unresolved_anchors_count_markers_the_server_never_issued():
    """`AskResponse.anchors` 里结构上不可能有解析不到的锚点,差集只能从正文来。

    `[ k3 ]`(带空白)是刻意留在计数里的:`parse_anchors` 的 `MARKER_RE` 不认
    它,那就是一次「模型引了、系统没认出来」的真实失败。
    """
    answer = "结论见 [k1],另见 【k2】 与 [ k3 ] 以及 [k1]。"
    assert count_unresolved_anchors(answer, [{"key": "k1"}, {"key": "k2"}]) == 1
    assert count_unresolved_anchors("没有任何标记", [{"key": "k1"}]) == 0


def test_citations_out_of_scope_is_unknown_when_the_allowed_set_is_unknown():
    """「没查到范围」与「一条都没越界」是两件事,而这一列进的是硬判据。"""
    assert count_citations_out_of_scope(
        [{"source_id": "s1"}], allowed_source_ids=None, notebook_id="nb",
    ) is None


def test_citations_out_of_scope_counts_narrowed_and_cross_notebook_leaks():
    allowed = frozenset({"s1"})
    citations = [
        {"source_id": "s1", "notebook_id": ""},        # 在范围内
        {"source_id": "s9", "notebook_id": ""},        # 越出声明范围
        {"source_id": "", "notebook_id": "other-nb"},  # 跨库命中
        {"source_id": "", "notebook_id": ""},          # 记忆/KG:判不出,不计
    ]
    assert count_citations_out_of_scope(
        citations, allowed_source_ids=allowed, notebook_id="nb",
    ) == 2


# ---------------------------------------------------------------------------
# 完整性虚报的候选信号(§7.1;§10-1 要求真/假阳性 fixture)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [
    "这个笔记本里一共 6 篇论文,逐一列出如下:…",
    "以上就是全部内容。",
    "所有来源都已覆盖。",
    "Here is the complete list of papers.",
    "Every paper in this notebook uses KV compression.",
])
def test_completeness_claim_true_positives(answer):
    assert completeness_claim_candidate(answer, coverage_complete=None) is True


@pytest.mark.parametrize("answer", [
    "这篇论文提出了一种递归深度架构。",
    "KIVI 把 key 量化到 2 bit。",
    "The paper reports a 2x speedup.",
    "根据检索到的证据,至少有两条技术路线。",
])
def test_completeness_claim_false_positives_stay_out(answer):
    assert completeness_claim_candidate(answer, coverage_complete=None) is False


def test_a_backed_enumeration_is_not_a_completeness_claim():
    """枚举链自己签发了 `complete` ⇒ 那句「一共 6 篇」有据,不是虚报候选。"""
    assert completeness_claim_candidate(
        "一共 6 篇论文", coverage_complete=True,
    ) is False
    # 覆盖对象缺席 = 没有任何东西背书这句话 ⇒ 照样是候选。
    assert completeness_claim_candidate(
        "一共 6 篇论文", coverage_complete=False,
    ) is True


# ---------------------------------------------------------------------------
# 坏工具调用(§7.1,复用 T0 的 `skip_reasons`)
# ---------------------------------------------------------------------------


def test_invalid_tool_calls_reuse_the_t0_skip_reasons_projection():
    assert count_invalid_tool_calls({
        "duplicate_subquery": 2, "enumeration_unavailable": 1,
        "scope_invalid": 1, "step_budget": 3,
    }) == 4


def test_kg_unavailable_is_a_corpus_fact_not_a_bad_call():
    """`A_nokg` 每个 run 都会有它;计进去这一列就成了「跑在哪个格」的复读。"""
    assert count_invalid_tool_calls({"kg_unavailable": 5}) == 0
    assert count_invalid_tool_calls({"kg_gap_unavailable": 2}) == 2


def test_invalid_tool_calls_are_unknown_when_skip_reasons_are():
    assert count_invalid_tool_calls(None) is None


# ---------------------------------------------------------------------------
# 成本三键的时间窗切片(§7.2)
# ---------------------------------------------------------------------------


def _llm_record(offset_s: float, *, prompt=10, completion=5, finish="stop"):
    stamp = datetime(2026, 9, 9, 12, 0, 0) + timedelta(seconds=offset_s)
    row: dict[str, Any] = {"ts": stamp.isoformat(), "kind": "chat",
                           "usage": {"prompt_tokens": prompt,
                                     "completion_tokens": completion}}
    if finish is not None:
        row["finish_reason"] = finish
    return row


def test_llm_usage_sums_only_the_records_inside_the_window():
    start = datetime(2026, 9, 9, 12, 0, 0)
    usage = slice_llm_usage(
        [_llm_record(-1), _llm_record(0), _llm_record(5), _llm_record(31)],
        start=start, end=start + timedelta(seconds=30),
    )
    assert usage.model_calls == 2
    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 10
    assert usage.finish_reason_codes == {"stop": 2}


def test_a_missing_finish_reason_becomes_a_short_code_not_an_empty_string():
    """空串过不了投影的短码校验,而「这次没报 finish_reason」本身要计数——
    T0 那 20% 空正文靠这一列归因(§9 硬判据 4)。"""
    start = datetime(2026, 9, 9, 12, 0, 0)
    usage = slice_llm_usage(
        [_llm_record(1, finish=None), _llm_record(2, finish="length")],
        start=start, end=start + timedelta(seconds=30),
    )
    assert usage.finish_reason_codes == {"length": 1, "unknown": 1}
    assert_projection_values({"finish_reason_codes": usage.finish_reason_codes})


def test_concurrency_above_one_forces_the_three_cost_keys_to_unknown():
    """§4.4:窗口重叠时的成本数比没有更坏,所以整份 usage 落 unknown。"""
    assert UNKNOWN_USAGE.model_calls is None
    assert UNKNOWN_USAGE.prompt_tokens is None
    assert UNKNOWN_USAGE.completion_tokens is None
    assert UNKNOWN_USAGE.finish_reason_codes is None
    row = _project(usage=UNKNOWN_USAGE)
    for key in ("model_calls", "prompt_tokens", "completion_tokens",
                "finish_reason_codes"):
        assert row[key] is None, key
    # 掐在 rig 外侧的墙钟仍然留着——它不靠日志切片。
    assert row["latency_ms_total"] == 4200


# ---------------------------------------------------------------------------
# 一行投影(§7.1)
# ---------------------------------------------------------------------------


def _steps(arm: str = "v2") -> list[dict]:
    """一条最小但**真实形状**的轨迹:检索 + 一轮反思 + (v2 的)终态披露 + 合成。

    终态披露步的 detail 键是 `termination`(不是 `termination_reason`)——那是
    `_v2_termination` 认的那一个,也是「这是 v2」的唯一证据。legacy 的轨迹里
    没有这一步,`policy_version` 因此反推成 legacy。
    """
    steps: list[dict] = [
        {"step_type": "retrieve", "detail": {"count": 3, "phase": "seed"}},
        {"step_type": "reflect", "detail": {"next_action": "answer",
                                            "sufficient": True}},
        {"step_type": "skip", "detail": {"reason": "duplicate_subquery"}},
    ]
    if arm == "v2":
        steps.append({"step_type": "skip",
                      "detail": {"reason": "retrieval_termination",
                                 "termination": "model_sufficient"}})
    steps.append(
        {"step_type": "synthesis", "detail": {"anchors": 2, "included_chunks": 3}}
    )
    return steps


def _project(**overrides: Any) -> dict:
    kwargs: dict[str, Any] = dict(
        arm="v2", effort="standard", repeat=1, question_key="A-q08",
        corpus_cell="A_nokg",
        answer="训练用了 3.5B 参数 [k1] 与 800B token [k2],另见 [k9]。",
        citations=[{"source_id": "s1", "notebook_id": ""}],
        anchors=[{"key": "k1", "element_id": "e1", "source_id": "s1"},
                 {"key": "k2", "element_id": "e2", "source_id": "s1"}],
        coverage_complete=None, kg_in_scope=False, sources_count=1,
        has_intent_contract=True, notebook_id="nb-1", gold=_A_GOLD,
        element_sections={"e1": "4.1 Setup", "e2": "5. Results"},
        source_titles={"s1": "recurrent-depth.md"},
        allowed_source_ids=frozenset({"s1"}),
        usage=UNKNOWN_USAGE, latency_ms_total=4200,
        model_contract="0123456789abcdef", corpus_signature="fedcba9876543210",
    )
    kwargs.update(overrides)
    return project_ab_run(_steps(), **kwargs)


def test_projection_carries_the_t0_half_untouched():
    """T0 那一半仍然走 `project_run`,不在 A/B 侧另算一遍。"""
    row = _project()
    assert row["policy_version"] == "v2"
    assert row["termination_reason"] == "model_sufficient"
    assert row["reflect_turns"] == 1
    assert row["skip_reasons"]["duplicate_subquery"] == 1
    assert row["kg_in_scope"] is False


def test_projection_computes_every_deterministic_ab_key():
    row = _project()
    assert row["arm"] == "v2"
    assert row["repeat"] == 1
    assert row["answer_chars"] > 0
    assert row["answer_empty"] is False
    assert row["citations"] == 1
    assert row["anchors_total"] == 2
    assert row["anchors_on_gold"] == 1           # e1 在 4.1;e2 在 5.
    assert row["anchors_unresolved"] == 1        # 正文引了 k9,服务端没签发
    assert row["citations_out_of_scope"] == 0
    assert row["completeness_claim"] is False
    assert row["invalid_tool_calls"] == 1
    assert row["model_contract"] == "0123456789abcdef"
    assert row["corpus_signature"] == "fedcba9876543210"


def test_the_judged_and_human_keys_stay_unknown_in_this_task():
    """T-AB2 不给判分模型任何东西(§12 的任务边界),但键集现在就闭上。"""
    row = _project()
    for key in DEFERRED_JUDGED_KEYS:
        assert key in AB_PROJECTION_KEYS
        assert row[key] is None, key


def test_an_empty_answer_is_recorded_as_empty_not_as_missing():
    row = _project(answer="", anchors=[], citations=[])
    assert row["answer_empty"] is True
    assert row["answer_chars"] == 0
    assert row["anchors_total"] == 0
    assert row["anchors_unresolved"] == 0


def test_every_projected_row_stays_inside_the_closed_set():
    row = _project()
    assert set(row) <= AB_PROJECTION_KEYS
    assert_ab_closed(row)
    assert_projection_values(row)


def test_an_unknown_arm_is_refused_before_anything_is_computed():
    with pytest.raises(ValueError, match="unknown arm"):
        _project(arm="shadow")


# ---------------------------------------------------------------------------
# 声明 vs 证据、配对(§5.5-1、§7.1 `paired`)
# ---------------------------------------------------------------------------


def test_arm_and_inferred_policy_version_must_agree():
    assert_arm_matches_evidence("v2", "v2")
    with pytest.raises(RuntimeError, match="policy_version"):
        assert_arm_matches_evidence("v2", "legacy")


def test_pairing_needs_both_arms_on_the_same_question_effort_and_round():
    legacy = _project(arm="legacy")
    v2 = _project(arm="v2")
    lonely = _project(arm="v2", repeat=2)
    mark_paired([legacy, v2, lonely])
    assert legacy["paired"] is True and v2["paired"] is True
    assert lonely["paired"] is False
    assert pair_id(legacy) == pair_id(v2) != pair_id(lonely)


# ---------------------------------------------------------------------------
# `ab` 的枚举与预检(§4.2、§5.4)
# ---------------------------------------------------------------------------


def _ab_args(**overrides: Any):
    args = rig.build_parser().parse_args([
        "--dry-run", "--limit", "2",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ])
    args.base_url = f"http://127.0.0.1:{args.port}"
    args.database_url_explicit = True
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_ab_plan_puts_the_repeat_round_outermost():
    """§4.2:被截断的前缀必须是一份**配对完整、平衡**的数据集。"""
    units = rig.ab_plan(
        load_questions(), cells=["A_nokg"], efforts=("standard", "deep"),
        repeats=2, round_=None, lang="zh", limit=1,
    )
    assert [unit["repeat"] for unit in units] == [1, 1, 2, 2]
    assert [unit["effort"] for unit in units] == [
        "standard", "deep", "standard", "deep",
    ]


def test_ab_plan_does_not_carry_the_arm_or_an_idempotency_key():
    """臂是单元**内部**的事,不是枚举的一维;`ab` 也不写 `ask_jobs` 一行。"""
    units = rig.ab_plan(
        load_questions(), cells=["A_nokg"], efforts=("standard",), repeats=1,
        round_=None, lang="zh", limit=1,
    )
    assert len(units) == 1
    assert "policy" not in units[0]
    assert "client_request_id" not in units[0]


def test_ab_round_flag_runs_only_that_round():
    units = rig.ab_plan(
        load_questions(), cells=["A_nokg"], efforts=("standard",), repeats=3,
        round_=2, lang="zh", limit=1,
    )
    assert [unit["repeat"] for unit in units] == [2]


def test_ab_cells_default_to_the_main_matrix_not_to_all_four():
    """把 P1 探针与刻意不做的 `A_kg` 卷进默认值,等于每次都默默多跑一倍预算。"""
    assert rig.ab_cells([]) == ["A_nokg", "B_kg"]
    assert rig.ab_cells(["B_nokg"]) == ["B_nokg"]


def test_ab_refuses_a_database_url_that_is_not_a_test_database():
    """§5.5-2:`ab` 会往库里**写** conversation 与 answer 行。"""
    problem = rig._ab_preflight(
        _ab_args(database_url="postgresql://127.0.0.1:5432/silicon_notebook"),
        ["A_nokg"],
    )
    assert "_test" in problem


def test_ab_refuses_to_run_without_an_explicit_database_url():
    problem = rig._ab_preflight(
        _ab_args(database_url_explicit=False), ["A_nokg"],
    )
    assert "--database-url" in problem


def test_ab_requires_the_main_database_url_for_the_zero_touch_assertion():
    """没有主库连接,§5.5-2 只能记「未验证」——而未验证不等于通过。"""
    problem = rig._ab_preflight(_ab_args(source_db_url=None), ["A_nokg"])
    assert "--source-db-url" in problem


def test_ab_dry_run_prints_the_run_count_and_the_call_ceiling(capsys):
    """§10-3:缩批 dry-run 打印正确的 run 数、模型调用上界与产物落点。"""
    assert rig.main([
        "--dry-run", "--limit", "2", "--round", "1",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    out = capsys.readouterr().out
    # 4 题(两格各 2)× 2 档 × 1 轮 = 8 个单元 × 2 臂 = 16 个 run。
    assert "planned runs  16(8 个配对单元 × 2 臂" in out
    assert "⇒  ≤ " in out and "synthesis 16" in out
    assert "ab-runs.jsonl" in out
    assert "不进数据集/不进仓库" in out


def test_ab_dry_run_warns_that_concurrency_kills_the_cost_keys(capsys):
    """§10-3:`--concurrency 2` 必须当场打印「成本三键将为 unknown」。"""
    assert rig.main([
        "--dry-run", "--limit", "1", "--round", "1", "--concurrency", "2",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out and "时间窗切片" in out


def test_ab_dry_run_touches_nothing(tmp_path, capsys):
    """dry-run 不连库、不起后端、不发请求,也不落任何文件。"""
    out_dir = tmp_path / "ab"
    assert rig.main([
        "--dry-run", "--limit", "1", "--round", "1", "--out-dir", str(out_dir),
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    capsys.readouterr()
    assert not out_dir.exists()


def test_ab_call_estimate_charges_two_arms_and_one_synthesis_per_run():
    """跑的是完整 Ask:检索侧每一项 × 2 臂,再加每个 run 一次合成。"""
    units = rig.ab_plan(
        load_questions(), cells=["A_nokg"], efforts=("standard",), repeats=1,
        round_=None, lang="zh", limit=1,
    )
    text = rig._ab_call_estimate(units, 1, no_intent=False)
    assert "intent 1 + plan 0" in text
    assert "synthesis 2" in text


# ---------------------------------------------------------------------------
# 意图契约的提交形态(§5.5-3)
# ---------------------------------------------------------------------------


def test_submitting_a_frozen_contract_survives_the_service_side_refinalize():
    """`AskService._confirmed_reasoning_intent` 会对提交的契约**再 finalize 一次**。

    `_IntentCache` 存的是第一次 finalize **之后**的那一份(`ambiguities` 已被
    清空、`clarification_answers` 已填)。直接提交会让那批代答在第二次 finalize
    里被清掉,于是检索问题少掉「用户确认」那几条补充、权威判据还会翻个个儿。
    `ab_intent_confirmation` 按 `clarification_answers` 还原 `ambiguities` 再
    提交,第二次 finalize 因此拿到与第一次逐字相同的输入。
    """
    from app.services.query_intent import finalize_query_intent

    seed = {
        "objective": "只看 Qwen-VL 这一篇", "resolved_question": "Qwen-VL 怎么处理图像输入",
        "ambiguities": [{"id": "a1", "question": "看哪一篇?", "required": True,
                         "options": ["Qwen-VL"]}],
    }
    frozen = finalize_query_intent(
        seed, resolved_question=seed["resolved_question"],
        answers=[{"id": "a1", "answer": "Qwen-VL"}],
    )
    assert frozen["clarification_answers"]

    confirmation = rig.ab_intent_confirmation(frozen, seed["objective"])
    refinalized = finalize_query_intent(
        confirmation.contract.model_dump(),
        resolved_question=confirmation.resolved_question,
        answers=[row.model_dump() for row in confirmation.answers],
    )
    assert refinalized["clarification_answers"] == frozen["clarification_answers"]
    assert refinalized["resolved_question"] == frozen["resolved_question"]
    assert refinalized["result_scope"] == frozen["result_scope"]
    # 代答过的题:`auto_confirmed_clear_intent` 必须为假(确认后的方向是权威)。
    assert confirmation.answers


def test_a_question_without_clarifications_submits_no_answers():
    from app.services.query_intent import finalize_query_intent

    frozen = finalize_query_intent(
        {"objective": "这篇论文的核心贡献是什么?",
         "resolved_question": "这篇论文的核心贡献是什么?"},
    )
    confirmation = rig.ab_intent_confirmation(frozen, frozen["objective"])
    assert confirmation.answers == []
    assert confirmation.contract.ambiguities == []


def test_contract_digest_changes_when_the_contract_does():
    """§5.5-3 比的是短码——契约正文既不进日志也不进数据集。"""
    one = rig.ab_contract_digest({"resolved_question": "a", "entities": ["x"]})
    two = rig.ab_contract_digest({"entities": ["x"], "resolved_question": "a"})
    three = rig.ab_contract_digest({"resolved_question": "b", "entities": ["x"]})
    assert one == two != three
    assert_projection_values({"model_contract": one})


# ---------------------------------------------------------------------------
# 协议对号:声明的臂 vs 传进去的 Settings(§5.5-1 的事前那一半)
# ---------------------------------------------------------------------------


def test_run_ab_once_refuses_a_settings_that_disagrees_with_the_arm(rrepo):
    """声明 v2、settings 却是 legacy ⇒ 当场报错,一次模型都不调。"""
    notebook = _seed_two_nodes(rrepo)
    with pytest.raises(RuntimeError, match="reflect_v2_active"):
        rig.run_ab_once(
            rrepo, notebook=notebook.id,
            item={"question": "RTL到GDSII流程", "effort": "standard"},
            arm="v2", contract=None, on_trace=None,
            cancel_event=threading.Event(), actor_id="ab-owner",
        )


def test_knowhow_keeps_reflect_v2_off_even_when_the_switch_is_on(rrepo):
    """§5.5-5:Knowhow 不设臂,两条臂对它逐字相同。"""
    v2_settings = rrepo.settings.model_copy()
    v2_settings.reasoning_reflect_v2_enabled = True
    # 断言的前提先立住:同一份 settings 在**不**关 allow_reflect_v2 时是开的。
    from app.services.reasoning_retrieval import ReasoningRetriever

    assert ReasoningRetriever.from_repository(
        rrepo, v2_settings, None
    ).reflect_v2_active() is True
    rig.assert_knowhow_reflect_v2_off(rrepo, v2_settings)


# ---------------------------------------------------------------------------
# 一个配对单元的编排(不调真实模型)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """一个刚好够投影用的假 `AskResponse`。"""

    def __init__(self, answer: str = "结论 [k1]。") -> None:
        self.answer = answer
        self.conclusion = "结论"
        self.evidence_level = "grounded"
        self.citations = [SimpleNamespace(source_id="s1", notebook_id="",
                                          model_dump=lambda: {"source_id": "s1"})]
        self.anchors = [SimpleNamespace(key="k1", element_id="e1", source_id="s1",
                                        model_dump=lambda: {"key": "k1"})]
        self.result_coverage = None


def _unit(**overrides: Any) -> dict:
    unit = {
        "question_key": "A-q08", "corpus_cell": "A_nokg", "effort": "standard",
        "repeat": 1, "question": "训练用了多少参数?", "lang": "zh",
        "shape": "single_paper", "scope_source_titles": (),
    }
    unit.update(overrides)
    return unit


def _repos_by_arm() -> dict[str, Any]:
    """两条臂各一个 repo(编排层只按 `arm` 取用,不读它们的任何字段)。

    真跑时它们是两个 `create_repository(settings, migrate=False, seed=False)`
    ——臂由 repo 承载,因为 `AskService._build_reasoning_retriever` 读的是构造
    这个 repo 的那一份 Settings,`ask_reasoning` 没有别的入口(见 `run_ab_once`
    的说明)。用一个 repo 加一个 settings 参数,两条臂会双双跑 legacy。
    """
    return {
        arm: SimpleNamespace(name=f"{arm}-repo", settings=SimpleNamespace(arm=arm))
        for arm in ("legacy", "v2")
    }


def _fact() -> dict:
    return {
        "notebook": "nb-1", "sources": 1,
        "source_rows": [{"id": "s1", "title": "recurrent-depth.md"}],
        "source_titles": {"s1": "recurrent-depth.md"},
        "kg_in_scope": False, "corpus_signature": "fedcba9876543210",
    }


def test_a_pair_unit_runs_both_arms_back_to_back_and_writes_two_paired_rows(
    tmp_path, monkeypatch, capsys,
):
    """一个单元 = 两条臂背靠背 ⇒ 两行、都 `paired=True`、写进同一份 JSONL。

    这条把编排层的四件事一起钉住:两臂都跑到、两行都在闭集里、
    `assert_arm_matches_evidence` 在每条臂的第一个 run 之后真的跑过、日志与
    数据集里一个问题原文/答案正文的字都没有。
    """
    seen: list[str] = []

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None):
        seen.append(arm)
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="人话摘要",
                detail=step["detail"], duration_ms=7,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections",
                        lambda url, ids: {"e1": "4.1 Setup"})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)

    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False)
    rows_path = out_dir / "ab-runs.jsonl"
    log_path = out_dir / "ab-runs.log"
    with rows_path.open("a", encoding="utf-8") as rows_handle, \
            log_path.open("a", encoding="utf-8") as log:
        rig._run_ab_unit(
            _unit(), args=args, runner=runner, facts={"A_nokg": _fact()},
            repos_by_arm=_repos_by_arm(),
            actor_id="ab-owner", profile=None, concurrency=1,
            intents=rig._IntentCache(out_dir / "intents.jsonl", enabled=False),
            gold_by_key=load_ab_gold(load_questions()),
            model_contract="0123456789abcdef",
            log_dir=out_dir, clock=datetime.now, state={
                "verified": set(), "index": 0, "total": 2,
                "lock": threading.Lock(),
            },
            rows_handle=rows_handle, log=log,
        )
    capsys.readouterr()
    assert sorted(seen) == ["legacy", "v2"]
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    assert len(rows) == 2
    assert {row["arm"] for row in rows} == {"legacy", "v2"}
    assert all(row["paired"] is True for row in rows)
    for row in rows:
        assert set(row) <= AB_PROJECTION_KEYS


def test_the_written_dataset_carries_no_free_text(tmp_path, monkeypatch, capsys):
    """§7.3 的隐私守卫,断言的是**真的写出去的那几行**,不是构造出来的。"""
    secret_question = "只看 Qwen-VL 和 DeepSeek-V2 这两篇"
    secret_answer = "它们各自这样处理图像输入 [k1]。"

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None):
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary=secret_question,
                detail=step["detail"], duration_ms=7,
            ))
        return _FakeResponse(answer=secret_answer)

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)

    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    rows_path = out_dir / "ab-runs.jsonl"
    log_path = out_dir / "ab-runs.log"
    with rows_path.open("a", encoding="utf-8") as rows_handle, \
            log_path.open("a", encoding="utf-8") as log:
        rig._run_ab_unit(
            _unit(question=secret_question), args=args, runner=runner,
            facts={"A_nokg": _fact()}, repos_by_arm=_repos_by_arm(),
            actor_id="ab-owner", profile=None, concurrency=1,
            intents=rig._IntentCache(out_dir / "intents.jsonl", enabled=False),
            gold_by_key=load_ab_gold(load_questions()),
            model_contract="0123456789abcdef",
            log_dir=out_dir, clock=datetime.now, state={
                "verified": set(), "index": 0, "total": 1,
                "lock": threading.Lock(),
            },
            rows_handle=rows_handle, log=log,
        )
    capsys.readouterr()
    dataset = rows_path.read_text(encoding="utf-8")
    log_text = log_path.read_text(encoding="utf-8")
    for blob in (dataset, log_text):
        assert secret_question not in blob
        assert secret_answer not in blob
    rows = [json.loads(line) for line in dataset.splitlines()]
    assert len(rows) == 1
    # 只跑了一侧 ⇒ 不进配对差值表(§4.2)。
    assert rows[0]["paired"] is False
    # 正文只落 `.local` 的 raw 存档,那一份刻意不收窄(§7.3)。
    raw = json.loads(
        (out_dir / "raw" / "v2" / "A-q08_A_nokg_standard_r1.json")
        .read_text(encoding="utf-8")
    )
    assert raw["question"] == secret_question
    assert raw["answer"] == secret_answer


def test_a_first_run_whose_evidence_disagrees_with_the_arm_stops_the_batch(
    tmp_path, monkeypatch, capsys,
):
    """声明 v2、轨迹里却一个 termination 都没有 ⇒ 整批停(§5.5-1 的事后那一半)。"""
    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None):
        # legacy 形状的轨迹:没有终态披露步。
        on_trace(SimpleNamespace(step_type="reflect", summary="",
                                 detail={"next_action": "answer"}, duration_ms=1))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)

    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    with (out_dir / "ab-runs.jsonl").open("a", encoding="utf-8") as rows_handle, \
            (out_dir / "ab-runs.log").open("a", encoding="utf-8") as log:
        with pytest.raises(RuntimeError, match="policy_version"):
            rig._run_ab_unit(
                _unit(), args=args, runner=runner, facts={"A_nokg": _fact()},
                repos_by_arm=_repos_by_arm(),
                actor_id="ab-owner", profile=None, concurrency=1,
                intents=rig._IntentCache(out_dir / "intents.jsonl", enabled=False),
                gold_by_key=load_ab_gold(load_questions()),
                model_contract="0123456789abcdef",
                log_dir=out_dir, clock=datetime.now, state={
                    "verified": set(), "index": 0, "total": 1,
                    "lock": threading.Lock(),
                },
                rows_handle=rows_handle, log=log,
            )
    capsys.readouterr()


def test_ab_loop_keeps_the_two_arms_together_even_when_units_run_concurrently(
    tmp_path, monkeypatch, capsys,
):
    """`--concurrency > 1` 并发的是**单元**,不是臂(§4.2)。

    钉两件事:

    * 每个单元的两条臂仍然在**同一个线程**里背靠背跑——把臂拆到不同线程会让
      「同题同档同时刻,只差一个开关」这句话不再成立,而 A/B 的全部说服力都在
      这句话上;
    * 重复轮之间有栅栏:第 2 轮的第一个 run 不早于第 1 轮的最后一个 run。被
      截断的前缀因此仍然是一份完整轮的数据集。
    """
    threads_by_unit: dict[tuple, set[str]] = {}
    order: list[int] = []

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None):
        key = (item["question_key"], item["repeat"])
        threads_by_unit.setdefault(key, set()).add(threading.current_thread().name)
        order.append(int(item["repeat"]))
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False,
                    no_intent=True)
    units = [
        _unit(question_key="A-q08", repeat=1), _unit(question_key="A-q14", repeat=1),
        _unit(question_key="A-q08", repeat=2), _unit(question_key="A-q14", repeat=2),
    ]
    rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=2,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
    )
    capsys.readouterr()
    assert len(threads_by_unit) == 4
    for key, threads in threads_by_unit.items():
        assert len(threads) == 1, key
    assert order == sorted(order), order
    rows = [
        json.loads(line)
        for line in (out_dir / "ab-runs.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 8
    assert all(row["paired"] is True for row in rows)
    assert all(set(row) <= AB_PROJECTION_KEYS for row in rows)


def test_the_rig_and_the_projection_agree_on_what_the_two_arms_are():
    """臂的词表在两处出现(rig 声明侧、投影侧),必须逐字相同。

    分叉不会当场报错,而是安静地伤到 `mark_paired`:它按 `len(ARMS)` 判「两臂
    都在场」,词表少一项就会把每一行标成未配对,整张差值表凭空空掉。
    """
    from app.eval.reflect_ab import ARMS

    assert rig.AB_ARMS == ARMS
    assert set(ARMS) == set(rig.POLICIES)


def test_the_arm_lives_on_the_repository_not_on_a_settings_argument(rrepo):
    """A/B 的两个 repo 不是浪费,是 Ask 这条路的形状决定的。

    `search` 能共用一个 repo:它直调
    `ReasoningRetriever.from_repository(repo, settings)`,检索器读的是**传进去**
    的那一份 Settings。Ask 没有这个入口——`AskService._build_reasoning_retriever`
    写死 `settings=self.settings`,而 `self.settings` 就是构造这个 repo 时用的
    那一份。这条用例把两件事钉住:

    1. `repo.settings` 与 Ask 组件读的那一份是**同一个对象**;
    2. 生产路径构造出来的检索器读的也是它。

    任何一条变了,`ab` 的「每条臂一个 repo」就该跟着重新审——而不是等到跑完
    408 个 run 才发现两条臂其实都是 legacy(那种数据从形状上完全看不出来)。
    """
    ask = rrepo._runtime.ask_component
    assert ask.settings is rrepo.settings
    retriever = ask._build_reasoning_retriever(cancel_event=None, user_id="u")
    assert retriever.settings is rrepo.settings
