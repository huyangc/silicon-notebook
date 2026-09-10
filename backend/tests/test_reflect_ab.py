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
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from app.core.config import REFLECT_OPTIMIZATION_IMPLEMENTED
from app.domain.reasoning_trace_stats import (
    OPTIMIZATIONS,
    RUN_PROJECTION_KEYS,
    assert_projection_values,
)
from app.eval.reflect_ab import (
    AB_ONLY_KEYS,
    AB_PROJECTION_KEYS,
    ARM_POLICIES,
    ARMS,
    DEFERRED_JUDGED_KEYS,
    PAIR_ARM_COUNT,
    UNKNOWN_USAGE,
    AbGold,
    ArmSpecError,
    GoldError,
    arm_label,
    assert_ab_closed,
    assert_arm_matches_evidence,
    assert_optimization_matches_evidence,
    base_question_key,
    format_arm,
    parse_arms,
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
from app.eval.reflect_manifest import assert_manifest
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


def test_an_element_with_an_empty_section_path_is_a_miss_not_unknown():
    """元素在库里、`metadata.section_path` 是空串 ⇒ **不命中**,不是 unknown。

    `structural_markdown.section_path()` 对「首个标题之前的块」返回 ""——那不是
    一次解析失败,是一条真实的「这段文字不在任何一节下面」。判成 unknown 会让
    一个这样的锚点就把整个 run 的这一列废掉(codex 质量评审 P2-11)。
    """
    assert count_anchors_on_gold(
        [_anchor(element_id="e1")], _A_GOLD,
        element_sections={"e1": ""}, source_titles={},
    ) == 0
    # 同一个 run 里另有一个真命中的元素时,空 section 的那个只是不进分子。
    assert count_anchors_on_gold(
        [_anchor(key="k1", element_id="e1"), _anchor(key="k2", element_id="e2")],
        _A_GOLD,
        element_sections={"e1": "", "e2": "Abstract"}, source_titles={},
    ) == 1


def test_a_chunk_anchor_hits_gold_through_its_own_section_path():
    """chunk 锚点的 `location_label` 就是 `chunk.section_path`(§7.1 的 chunk 跳)。

    `build_chunks` 按 600 字聚合多个元素,`element_id` 只在 chunk 恰好只含一个
    元素时非空(`evidence_context.py:495`),所以多数 chunk 锚点根本没有
    `element_id`。此前这一跳缺席,它们进了分母却进不了分子——`anchors_on_gold`
    因此系统性偏低(codex 规格评审 P1-2)。
    """
    hits = count_anchors_on_gold(
        [
            _anchor(key="k1", element_id="e1"),
            _anchor(key="k2", object_id="c1", object_type="chunk",
                    location_label="4.1 Setup"),
            _anchor(key="k3", object_id="c2", object_type="chunk",
                    location_label="7. Appendix"),
        ],
        _A_GOLD,
        element_sections={"e1": "Abstract"}, source_titles={},
    )
    assert hits == 2


def test_a_chunk_anchor_without_a_label_resolves_through_the_chunks_table():
    """没带 `location_label` 的 chunk ⇒ 按 `chunk_id` 查 `chunks.section_path`。"""
    anchors = [_anchor(key="k1", object_id="c1", object_type="chunk")]
    assert count_anchors_on_gold(
        anchors, _A_GOLD, element_sections={}, source_titles={},
        chunk_sections={"c1": "4.1 Setup"},
    ) == 1
    # 查询表整体缺席(那一步查库失败)⇒ 整键 unknown,不猜「零命中」。
    assert count_anchors_on_gold(
        anchors, _A_GOLD, element_sections={}, source_titles={},
        chunk_sections=None,
    ) is None
    # 表在、这个 chunk 不在表里 ⇒ 一次真实的解析失败 ⇒ unknown。
    assert count_anchors_on_gold(
        anchors, _A_GOLD, element_sections={}, source_titles={},
        chunk_sections={"other": "4.1"},
    ) is None


def test_an_anchor_with_neither_an_element_nor_a_section_is_a_failed_hop():
    """A 格里既没有 `element_id`、也没有任何 section 信息 ⇒ 解析失败 ⇒ unknown。

    A 格(`gold_section_path`)是单篇语料格,跑在 `A_nokg` 上、没有知识图谱,
    所以这条路上不会出现「纯 KG 对象锚点」那种天然没有 section 的锚点;真出现
    一个三样都没有的锚点,那就是一次名副其实的解析失败,不能折成「不命中」
    (§7.1「任一跳解析不到 ⇒ 整键 unknown」)。
    """
    assert count_anchors_on_gold(
        [_anchor(key="k1", element_id="e1"), _anchor(key="k2")],
        _A_GOLD,
        element_sections={"e1": "Abstract"}, source_titles={},
    ) is None


def test_the_b_shape_still_exempts_anchors_that_carry_no_source_id():
    """B 格保留那条豁免:chunk 与元素锚点都带 `source_id`,分子漏不掉。

    豁免只放过纯 KG 对象锚点——B_kg 上它们真实存在,把它们算成解析失败会让
    几乎每个 run 的这一列变成 unknown,等于把这条判据废掉。
    """
    hits = count_anchors_on_gold(
        [_anchor(key="k1", source_id="s1"), _anchor(key="k2")],
        _B_GOLD,
        element_sections={},
        source_titles={"s1": "src-193b32d112_06_KIVI_mineru.md"},
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


def test_the_b_shape_dedupes_by_anchor_identity_not_by_source():
    """B 格按锚点身份去重,不是按 `source_id` 折叠(codex #703 R2 P2-1)。

    同一篇 gold 论文的 3 个不同锚点(1 个元素 + 2 个不同 chunk)全部命中,
    即使它们共享同一个 `source_id`,也要各算各的——折成来源集合会让
    `anchors_on_gold` 悄悄变成「命中来源数」,在 `anchors_total=3` 时把指标
    腰斩成 1。
    """
    hits = count_anchors_on_gold(
        [
            _anchor(key="k1", source_id="s1", element_id="e1"),
            _anchor(key="k2", source_id="s1", object_id="c1", object_type="chunk"),
            _anchor(key="k3", source_id="s1", object_id="c2", object_type="chunk"),
        ],
        _B_GOLD,
        element_sections={},
        source_titles={"s1": "src-193b32d112_06_KIVI_mineru.md"},
    )
    assert hits == 3
    # 同一个锚点(同一个 element_id)在锚点列表里重复出现只算 1。
    dup_hits = count_anchors_on_gold(
        [
            _anchor(key="k1", source_id="s1", element_id="e1"),
            _anchor(key="k2", source_id="s1", element_id="e1"),
        ],
        _B_GOLD,
        element_sections={},
        source_titles={"s1": "src-193b32d112_06_KIVI_mineru.md"},
    )
    assert dup_hits == 1
    # 非 gold 来源的锚点不计——即便它带着看起来合法的身份信息。
    non_gold_hits = count_anchors_on_gold(
        [_anchor(key="k1", source_id="s2", element_id="e9")],
        _B_GOLD,
        element_sections={},
        source_titles={"s2": "src-1675b22a0f_Jamba_mineru.md"},
    )
    assert non_gold_hits == 0


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


def test_a_rejected_aspect_is_not_a_bad_tool_call():
    """逐方面被拒的自评不是一次坏的工具调用(T-BF7):那个工具真的执行了。

    整份形状不成立的那一族仍然计入——那一轮真的整轮作废、工具一次都没打出去。

    变异:把 `_NOT_A_TOOL_CALL_REASONS` 缩回只有 `kg_unavailable` ⇒ 第一条红,
    v2 臂会凭空多出一批不存在的坏调用。
    """
    assert count_invalid_tool_calls({
        "invalid_assessment:unknown_aspect": 3,
        "invalid_assessment:duplicate_aspect": 2,
        "invalid_assessment:evidence_keys_overflow": 1,
        "invalid_assessment:gap_overflow": 1,
    }) == 0
    assert count_invalid_tool_calls({
        "invalid_assessment:item_not_object": 1,
        "invalid_assessment:not_object": 2,
    }) == 3


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


def test_llm_usage_token_totals_are_unknown_when_any_call_lacks_that_counter():
    """provider 可能不回 usage(被拒后去掉 usage 选项重试;`_usage_dict` 允许缺
    字段):把缺的那次当 0 会把部分和报成全程总量,A/B 成本对照假装省了钱
    (codex #703 R3 P2)。两个 token 计数各自判完整;调用数与 finish_reason 照常。"""
    start = datetime(2026, 9, 9, 12, 0, 0)
    end = start + timedelta(seconds=30)
    with_gap = [_llm_record(1), _llm_record(2)]
    with_gap[1] = {**with_gap[1], "usage": {"completion_tokens": 5}}  # 缺 prompt_tokens
    usage = slice_llm_usage(with_gap, start=start, end=end)
    assert usage.model_calls == 2
    assert usage.prompt_tokens is None
    assert usage.completion_tokens == 10
    assert usage.finish_reason_codes == {"stop": 2}

    no_usage = [_llm_record(1), {**_llm_record(2), "usage": None}]
    usage = slice_llm_usage(no_usage, start=start, end=end)
    assert usage.model_calls == 2
    assert usage.prompt_tokens is None and usage.completion_tokens is None
    assert_projection_values({"prompt_tokens": usage.prompt_tokens,
                              "completion_tokens": usage.completion_tokens})


def test_accumulate_treats_a_missing_counter_as_unknown_not_zero():
    """`_accumulate` 的 unknown 口径基线,直接对着这个纯函数断言。

    它不属于某一个具体计数:`slice_llm_usage` 今天只用它累 prompt/completion,
    将来若开始读 `cached_tokens` / `reasoning_tokens`,用的也必须是同一把尺子
    ——这个用例钉的是尺子,不是某次读数(此前的名字暗示后者,名不副实)。

    之所以单独钉住:窗口里混着「provider 报了」与「provider 没报」两种记录会是
    常态(`_usage_dict` 缺字段就不写键,T-PS2:绝不写 0),而把缺的那次当 0,会让
    「根本没测」看起来像「测了,结果是零」,这两者在采纳判据里指向相反结论。规则
    因此是:窗口内任一次调用缺该计数 ⇒ 整个 run 该计数 unknown。
    """
    accumulate = importlib.import_module("app.eval.reflect_ab")._accumulate

    assert accumulate(0, 9) == 9                    # 报了就累加
    assert accumulate(9, 0) == 9                    # provider 报的 0 是真读数
    assert accumulate(9, None) is None              # 这次没报 ⇒ unknown
    assert accumulate(None, 9) is None              # 已经 unknown 就一直 unknown
    assert accumulate(9, True) is None              # bool 不是计数
    assert accumulate(9, "9") is None               # 字符串不是计数


def test_slice_llm_usage_ignores_the_new_counters_for_now():
    """新键进了 llm.jsonl,但成本三键的切片**这一期不读它们**:窗口归因还只报
    prompt/completion。写在这里是为了让「哪天开始读」成为一次显式改动,而不是
    某个字典展开顺手带出来的副作用。"""
    start = datetime(2026, 9, 9, 12, 0, 0)
    rich = _llm_record(1)
    rich["usage"] = {**rich["usage"], "cached_tokens": 4, "reasoning_tokens": 2}
    usage = slice_llm_usage([rich], start=start, end=start + timedelta(seconds=30))

    assert usage.model_calls == 1
    assert usage.prompt_tokens == 10 and usage.completion_tokens == 5
    assert not hasattr(usage, "cached_tokens")


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


@pytest.mark.parametrize("raw, expected", [
    ("stop", "stop"),
    ("  length  ", "length"),
    ("content filter", "content_filter"),        # 带空白
    ("length/stop", "length_stop"),              # 带斜杠
    ("模型侧异常", "_____"),                       # 非 ASCII 也归一,不抛
    ("", "unknown"),
    (None, "unknown"),
    (17, "unknown"),
    ("x" * 200, "x" * 64),                       # 超长截断到短码上限
])
def test_a_provider_finish_reason_is_normalised_into_a_short_code(raw, expected):
    """provider 报什么形状都不该在**投影那一步**炸掉整批(codex 质量评审 P2-7)。

    `finish_reason` 是 provider 自己的字符串,合同上只保证有值。原样落进
    `finish_reason_codes` 会被 `assert_projection_values` 拒掉——而那一拒发生在
    整个 run 已经跑完之后,一次坏读数于是打掉的是几百次模型调用的一批。
    """
    from app.eval.reflect_ab import normalize_finish_reason

    assert normalize_finish_reason(raw) == expected
    assert_projection_values({"finish_reason_codes": {expected: 1}})


def test_a_non_short_code_finish_reason_survives_the_whole_slice():
    """端到端:切片吐出来的那张表整体过得了投影校验。"""
    start = datetime(2026, 9, 9, 12, 0, 0)
    usage = slice_llm_usage(
        [_llm_record(1, finish="content filter"), _llm_record(2, finish="stop")],
        start=start, end=start + timedelta(seconds=30),
    )
    assert usage.finish_reason_codes == {"content_filter": 1, "stop": 1}
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
    # `steps` 不是 `project_ab_run` 的关键字参数(它是第一个位置参数),所以从
    # overrides 里单独摘出来:要换一条轨迹的用例不必把上面这一整串再抄一遍。
    steps = overrides.pop("steps", None)
    kwargs.update(overrides)
    return project_ab_run(_steps() if steps is None else steps, **kwargs)


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
    # 与 `main()` 逐字同一步:哨兵默认值(`--database-url` / `--concurrency` /
    # `--repeats`)由 `apply_shared_arg_defaults` 填,不在这里手抄一半。
    rig.apply_shared_arg_defaults(args)
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


def test_run_ab_once_refuses_a_repo_whose_optimization_disagrees(rrepo, monkeypatch):
    """声明 `prefix_snapshot`、repo 那份 Settings 却跑 `off` ⇒ 当场报错。

    臂的第二维**只有这一次**核对机会:两条 v2 臂的轨迹形状逐字相同,事后从产物
    里反推不出来。最常见的一种不符是静默降级——v2 总闸没开或 Knowhow 否决时
    `reflect_optimization()` 恒返回 `off`,配置里写了什么都不看,那时两条臂在跑
    同一件事而数据看起来完全正常。

    这里把 v2 总闸打开(否则先撞上 `reflect_v2_active` 那条断言),只让第二维
    对不上:错的那一条断言必须是关于 optimization 的。
    """
    notebook = _seed_two_nodes(rrepo)
    # 改**实例**字段(pydantic 的字段住在实例上,不在类上);monkeypatch 负责
    # 还原,否则同一个 `rrepo` 的后续用例会跑在一份它没要的策略上。
    monkeypatch.setattr(rrepo.settings, "reasoning_reflect_v2_enabled", True)
    with pytest.raises(RuntimeError, match="optimization"):
        rig.run_ab_once(
            rrepo, notebook=notebook.id,
            item={"question": "RTL到GDSII流程", "effort": "standard"},
            arm="v2", optimization="prefix_snapshot", contract=None,
            on_trace=None, cancel_event=threading.Event(),
            actor_id="ab-owner",
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


def _repos_by_arm(
    arms: Sequence[tuple[str, str]] = rig.AB_DEFAULT_ARMS,
) -> dict[tuple[str, str], Any]:
    """每条臂各一个 repo(编排层只按 `arm` 取用,不读它们的任何字段)。

    真跑时它们是两个 `create_repository(settings, migrate=False, seed=False)`
    ——臂由 repo 承载,因为 `AskService._build_reasoning_retriever` 读的是构造
    这个 repo 的那一份 Settings,`ask_reasoning` 没有别的入口(见 `run_ab_once`
    的说明)。用一个 repo 加一个 settings 参数,两条臂会双双跑 legacy。

    键是 `(policy, optimization)` 这一对(T-PS5 的二维臂):只按 policy 索引的
    话,`v2:off` 与 `v2:prefix_snapshot` 会取到同一个 repo,于是两条臂双双跑同
    一档 optimization——而数据从形状上完全看不出这件事。
    """
    return {
        arm: SimpleNamespace(
            name=f"{format_arm(*arm)}-repo",
            settings=SimpleNamespace(arm=format_arm(*arm)),
        )
        for arm in arms
    }


def _fact() -> dict:
    return {
        "notebook": "nb-1", "sources": 1,
        "source_rows": [{"id": "s1", "title": "recurrent-depth.md"}],
        "source_titles": {"s1": "recurrent-depth.md"},
        "kg_in_scope": False, "corpus_signature": "fedcba9876543210",
    }


# ---------------------------------------------------------------------------
# gold 来源解析的跑前断言按 `(corpus_cell, question_key)` 缓存(§5.5-6)
# ---------------------------------------------------------------------------


def test_gold_resolution_preflight_checks_every_selected_cell_for_the_same_question(
    tmp_path,
):
    """`B_kg` 与 `B_nokg` 同时选中时,同一题在两个格各自解析一遍(codex #703 R2 P2-2)。

    此前缓存只按 `question_key` 记「查过了」:第一个格解析通过就不会再碰第二个
    格,第二个格里 gold 短名缺失/歧义会绕过这道跑前断言,变成一行误导的
    0 或虚高。
    """
    gold_by_key = {
        "B-q06": AbGold(question_key="B-q06", corpus="B", gold_sources=("KIVI",)),
    }
    facts = {
        "B_kg": {"source_rows": [{"id": "s1", "title": "src-a_KIVI_mineru.md"}]},
        "B_nokg": {"source_rows": [{"id": "s2", "title": "src-b_Jamba_mineru.md"}]},
    }
    units = [
        _unit(question_key="B-q06", corpus_cell="B_kg"),
        _unit(question_key="B-q06", corpus_cell="B_nokg"),
    ]
    runner = rig.Runner(dry_run=True, out_dir=tmp_path)
    with pytest.raises(GoldError, match=r"\[B_nokg\].*KIVI.*0 个来源"):
        rig._ab_assert_gold_resolves(
            runner, units, gold_by_key, facts, resolve_gold_sources,
        )


def test_gold_resolution_preflight_passes_when_every_selected_cell_resolves(
    tmp_path,
):
    """两个格都能解析 ⇒ 通过,且两格各自拿到自己笔记本里的 `source_id`。"""
    gold_by_key = {
        "B-q06": AbGold(question_key="B-q06", corpus="B", gold_sources=("KIVI",)),
    }
    facts = {
        "B_kg": {"source_rows": [{"id": "s1", "title": "src-a_KIVI_mineru.md"}]},
        "B_nokg": {"source_rows": [{"id": "s2", "title": "src-b_KIVI_mineru.md"}]},
    }
    units = [
        _unit(question_key="B-q06", corpus_cell="B_kg"),
        _unit(question_key="B-q06", corpus_cell="B_nokg"),
    ]
    runner = rig.Runner(dry_run=True, out_dir=tmp_path)
    checked = rig._ab_assert_gold_resolves(
        runner, units, gold_by_key, facts, resolve_gold_sources,
    )
    assert checked == 2
    # 两格各自解析到的是自己笔记本里的 source_id,不是同一个。
    assert resolve_gold_sources(
        gold_by_key["B-q06"], facts["B_kg"]["source_rows"]
    ) == {"KIVI": "s1"}
    assert resolve_gold_sources(
        gold_by_key["B-q06"], facts["B_nokg"]["source_rows"]
    ) == {"KIVI": "s2"}


def test_gold_resolution_preflight_does_not_recheck_the_same_cell_twice(tmp_path):
    """同一个 `(corpus_cell, question_key)` 出现在多个重复轮次里只解析一次。"""
    gold_by_key = {
        "B-q06": AbGold(question_key="B-q06", corpus="B", gold_sources=("KIVI",)),
    }
    calls: list[str] = []

    def _spy(gold, source_rows):
        calls.append(gold.question_key)
        return {}

    facts = {"B_kg": {"source_rows": [{"id": "s1", "title": "KIVI"}]}}
    units = [
        _unit(question_key="B-q06", corpus_cell="B_kg", repeat=1),
        _unit(question_key="B-q06", corpus_cell="B_kg", repeat=2),
    ]
    runner = rig.Runner(dry_run=True, out_dir=tmp_path)
    checked = rig._ab_assert_gold_resolves(runner, units, gold_by_key, facts, _spy)
    assert checked == 1
    assert calls == ["B-q06"]


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
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
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
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
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
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
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
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
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


def test_the_rig_and_the_projection_agree_on_what_the_arms_are():
    """臂的词表在两处出现(rig 声明侧、投影侧),必须同一份。

    分叉不会当场报错,而是安静地伤到 `mark_paired`:它按臂身份判「两条臂都在
    场」,词表少一项就会把每一行标成未配对,整张差值表凭空空掉。

    二维化之后 rig 侧不再自己写一份词表——`AB_DEFAULT_ARMS` 只是**默认值**,
    合法组合的闭集只有 `reflect_ab.ARMS` 一处。这条断言因此改成钉三件事:
    默认臂全部合法、policy 那一维与 `POLICIES` 同源、闭集里 legacy 只有 `off`。
    """
    assert set(rig.AB_DEFAULT_ARMS) <= set(ARMS)
    assert set(ARM_POLICIES) == set(rig.POLICIES)
    assert [arm for arm in ARMS if arm[0] == "legacy"] == [("legacy", "off")]


def test_parse_arms_reads_both_the_one_and_two_dimensional_spellings():
    """`--arms` 的两种写法产出同一种结构;省略第二维一律补 `off`。"""
    assert parse_arms("legacy,v2") == [("legacy", "off"), ("v2", "off")]
    assert parse_arms("v2:off,v2:prefix_snapshot") == [
        ("v2", "off"), ("v2", "prefix_snapshot")]
    # 一条臂也收:`--only-policy` 的二维版(重跑被打废的一侧)。
    assert parse_arms(" v2:prefix_snapshot ") == [("v2", "prefix_snapshot")]


@pytest.mark.parametrize("spec, why", [
    ("", "空写法"),
    ("v2,", "空段"),
    ("shadow", "未知 policy"),
    ("legacy:prefix_delta_lean", "合法值的非法组合(lean 变体)"),
    ("v2:snapshot", "未知 optimization"),
    ("legacy:prefix_snapshot", "合法值的非法组合"),
    ("v2:off:extra", "不是 policy[:optimization] 的形状"),
    ("v2,v2", "重复臂"),
])
def test_parse_arms_refuses_every_spelling_that_would_produce_fake_data(spec, why):
    """每一种写错法各自被拒。每一种不拒都会安静地产出一批假数据(见 `parse_arms`)。

    PR-3(T-PD7)把 `("v2","prefix_delta")` 放进 `ARMS` 之后,`v2:prefix_delta`
    不再是这条用例的素材——它现在是合法臂(见
    `test_parse_arms_reads_the_new_prefix_delta_spellings`)。PR-4(T-PL6)把
    `("v2","prefix_delta_lean")` 也放进 `ARMS` 之后,原来占位的『本期未实现的
    取值』素材同样不再成立:`v2:prefix_delta_lean` 现在合法(见
    `test_parse_arms_reads_the_new_prefix_delta_lean_spellings`),换成
    `legacy:prefix_delta_lean`——两个字段各自都在闭集里,只有这一对不成立,
    覆盖新格在 legacy 侧的非法组合。
    """
    with pytest.raises(ArmSpecError):
        parse_arms(spec)


def test_parse_arms_reads_the_new_prefix_delta_spellings():
    """T-PD7:`ARMS` 放开 `("v2","prefix_delta")` 一格之后的三种新写法。"""
    assert parse_arms("v2:prefix_delta") == [("v2", "prefix_delta")]
    assert parse_arms("v2:off,v2:prefix_delta") == [
        ("v2", "off"), ("v2", "prefix_delta")]
    assert parse_arms("legacy,v2:prefix_delta") == [
        ("legacy", "off"), ("v2", "prefix_delta")]


def test_parse_arms_reads_the_new_prefix_delta_lean_spellings():
    """T-PL6:`ARMS` 放开 `("v2","prefix_delta_lean")` 最后一格之后的新写法。

    第二维五格全部合法之后,D↔L 这一对(唯一只差自评合同的配对臂)也要能被
    `--arms` 点名跑批。
    """
    assert parse_arms("v2:prefix_delta_lean") == [("v2", "prefix_delta_lean")]
    assert parse_arms("v2:off,v2:prefix_delta_lean") == [
        ("v2", "off"), ("v2", "prefix_delta_lean")]
    assert parse_arms("v2:prefix_delta,v2:prefix_delta_lean") == [
        ("v2", "prefix_delta"), ("v2", "prefix_delta_lean")]


@pytest.mark.parametrize("optimization", ["prefix_delta", "prefix_delta_lean"])
def test_legacy_prefix_delta_error_lists_all_five_arms(optimization):
    """`legacy:prefix_delta`/`legacy:prefix_delta_lean` 仍是合法值的非法组合;
    错误文案现在列出五格。

    参数化到两个非 off 取值,恢复 legacy × 三个非 off 取值(`prefix_snapshot`
    见 `test_parse_arms_refuses_every_spelling_that_would_produce_fake_data`)
    的全交叉——T-PL6 把素材从 `legacy:prefix_delta` 换成 `legacy:prefix_delta_lean`
    之后,前者一度没有任何用例再点名;`_parse_one_arm` 的组合闸如果按取值放行
    (例如误写成 `optimization != "prefix_delta" and (policy, optimization)
    not in ARMS`),只测 lean 那一格看不出来。

    变异:往 `ARMS` 加一行却漏了这条用例 ⇒ 断言的臂数与 `ARMS` 实际长度分叉时,
    这条会先红(而不是被动等 `len(ARMS)` 悄悄变化)。
    """
    with pytest.raises(ArmSpecError) as excinfo:
        parse_arms(f"legacy:{optimization}")
    text = str(excinfo.value)
    assert len(ARMS) == 5
    for arm in ARMS:
        assert format_arm(*arm) in text
    # 过期文案不许回潮:`prefix_delta_lean` 已经在 `ARMS` 里,不该再被说成
    # "不在这份 ARMS 闭集里,T-PL6 起放开"。
    assert "起放开" not in text
    assert "不在这份 ARMS 闭集里" not in text


def test_the_arm_label_keeps_the_two_v2_arms_in_different_directories():
    """`raw/<label>/` 与 `calls-<label>.jsonl` 的短码。

    `off` 落回光秃秃的 policy(既有产物路径一个字节没变);非 `off` 必须带上第
    二维,否则两条 v2 臂的存档会撞进同一个路径,后跑的静默覆盖前一条。
    """
    assert arm_label("legacy", "off") == "legacy"
    assert arm_label("v2", "off") == "v2"
    assert arm_label("v2", "prefix_snapshot") == "v2-prefix_snapshot"
    labels = {arm_label(*arm) for arm in ARMS}
    assert len(labels) == len(ARMS)


def test_the_declared_optimization_must_match_the_runtime_reading():
    """臂的第二维只有运行时直接读数这一份证据(轨迹里反推不出来)。"""
    assert_optimization_matches_evidence("prefix_snapshot", "prefix_snapshot")
    assert_optimization_matches_evidence("off", "off")
    with pytest.raises(RuntimeError, match="prefix_snapshot"):
        # 最常见的一种不符:v2 总闸没开 / Knowhow 否决 ⇒
        # `reflect_optimization()` 恒返回 `off`,两条臂于是在跑同一件事。
        assert_optimization_matches_evidence("prefix_snapshot", "off")
    with pytest.raises(ValueError, match="unknown optimization"):
        assert_optimization_matches_evidence("snapshot", "snapshot")


def test_pairing_is_two_dimensional_and_a_single_arm_is_never_paired():
    """`mark_paired` 的判据二维化:两条 v2 臂不能被当成同一条。"""
    off = _project(arm="v2", optimization="off")
    snapshot = _project(arm="v2", optimization="prefix_snapshot")
    mark_paired([off, snapshot])
    assert off["paired"] is True and snapshot["paired"] is True
    # 只跑一臂 ⇒ `paired=False`,进单臂基线表而不是配对差值表。
    lonely = _project(arm="v2", optimization="prefix_snapshot")
    mark_paired([lonely])
    assert lonely["paired"] is False
    # 判据是「这一批的两条臂」,不是 `len(ARMS)`(合法组合闭集有五格)。
    assert PAIR_ARM_COUNT == 2 and len(ARMS) > PAIR_ARM_COUNT


def test_a_legacy_arm_may_never_declare_an_optimization():
    """`legacy:prefix_snapshot` 在投影侧也拒:两个字段各自合法,只有这一对不成立。"""
    with pytest.raises(ValueError, match="unknown arm"):
        _project(arm="legacy", optimization="prefix_snapshot")


def test_arms_v2_side_covers_every_implemented_optimization():
    """`ARMS` 的 v2 侧必须与**已实现**闭集 `REFLECT_OPTIMIZATION_IMPLEMENTED`
    逐个对上号(T-PL6)。

    `REFLECT_OPTIMIZATION_IMPLEMENTED`(`app.core.config`,今天与
    `OPTIMIZATIONS` 逐字相同,因为 `REFLECT_OPTIMIZATION_PLANNED` 现在是空)与
    `ARMS`(rig 能跑的臂,`reflect_ab`)是两处独立登记——`config` 的 `Literal`
    放开一格不会自动让 rig 收它。这条断言防的是那道题在 T-PL1 已经出现过一次
    的分叉:`prefix_delta_lean` 在闭集里有位置却迟迟没进 `ARMS`。

    右边刻意不取 `OPTIMIZATIONS`:后者是 `IMPLEMENTED + PLANNED`,合法的
    staging 态(先把下一格登记进 `PLANNED` + `Literal` + domain 闭集,`ARMS`
    还没跟上)会让 `OPTIMIZATIONS` 先长一格,而那一格根本还跑不起来
    (`Settings()` 构造时会被校验器拒绝)——等号右边取 `OPTIMIZATIONS` 会在这
    个合法中间态上误红,把作者引向往 `ARMS` 里塞一条实际上会构造失败的臂。
    另加一条方向断言:`REFLECT_OPTIMIZATION_IMPLEMENTED` 必须是 `OPTIMIZATIONS`
    的子集——已实现的必须先被登记,顺序不能反。

    变异:从 `ARMS` 删掉 `("v2","prefix_delta_lean")` 这一行 ⇒ 五格闭集与四值
    `REFLECT_OPTIMIZATION_IMPLEMENTED` 不再一一对应,这条先红。
    """
    assert len(ARMS) == 5
    v2_optimizations = {optimization for policy, optimization in ARMS
                         if policy == "v2"}
    assert v2_optimizations == set(REFLECT_OPTIMIZATION_IMPLEMENTED)
    assert set(REFLECT_OPTIMIZATION_IMPLEMENTED) <= set(OPTIMIZATIONS)


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


# ---------------------------------------------------------------------------
# 预检的其余硬前提(§5.4 / §5.5-2 / §5.5-3)
# ---------------------------------------------------------------------------


def test_ab_refuses_no_intent_because_the_contract_assertion_would_be_vacuous():
    """§5.5-3 要「两臂引用同一条契约」;`--no-intent` 下根本没有契约可比。

    那时两臂各自现算检索方向,比的已经不是同一件事,而同一性断言退化成
    `digest(None) == digest(None)` 恒真——一条永远绿的断言比没有断言更坏
    (codex 质量评审 P2-8)。
    """
    problem = rig._ab_preflight(_ab_args(no_intent=True), ["A_nokg"])
    assert "--no-intent" in problem and "§5.5-3" in problem


@pytest.mark.parametrize("round_", [0, -1, 4])
def test_ab_refuses_a_round_outside_the_repeat_range(round_):
    """`--round` 是下标不是舒适度阈值:越界该报错,不该 clamp 成边界值。

    clamp 会让 `--round 4 --repeats 3` 安静地把第 3 轮重跑一遍,两轮数据混进
    同一格。
    """
    problem = rig._ab_preflight(_ab_args(round=round_, repeats=3), ["A_nokg"])
    assert "--round" in problem and "1..3" in problem


def test_ab_accepts_a_round_inside_the_range():
    assert rig._ab_preflight(_ab_args(round=3, repeats=3), ["A_nokg"]) == ""


def test_ab_refuses_a_main_database_url_that_is_the_test_database():
    """两个 URL 同指一处 ⇒ 「主库快照」量的是 ab 自己正在写的那个库。

    那样这条断言要么必然假红(ab 真的往里写了),要么什么都不证明——两种都让
    §5.5-2 失效(codex 质量评审 P1-1)。
    """
    same = "postgresql://127.0.0.1:5432/nb_t0_test"
    problem = rig._ab_preflight(
        _ab_args(database_url=same, source_db_url=same + "/"), ["A_nokg"],
    )
    assert "--source-db-url" in problem and "--database-url" in problem


# ---------------------------------------------------------------------------
# 「主库零接触」的证据资格与收尾时机(§5.5-2)
# ---------------------------------------------------------------------------


def test_a_baseline_that_counts_nothing_is_not_evidence():
    """三张证据表全 `None` ⇒ before == after 恒成立 ⇒ 断言恒真。

    `_readonly_counts` 对每张数不出来的表都记 `None`,所以一条指错库/连不上的
    `--source-db-url` 会让 §5.5-2 静默通过(codex 质量评审 P1-1)。
    """
    assert rig.readonly_baseline_problem(
        {"ask_jobs": None, "answers": None, "conversations": None,
         "knowledge_objects": None, "retrieval_experiences": None}
    ) != ""
    # 有一张数得出来就够:剩下的 `None` 是「这次不看它」,不是「没连上」。
    assert rig.readonly_baseline_problem(
        {"ask_jobs": 0, "answers": None, "conversations": None}
    ) == ""


def _ab_run_harness(monkeypatch, tmp_path, *, counts, loop):
    """把 `_run_ab` 的每个真实依赖换掉,只留下要验的那一段编排。

    这条路真跑要连两个库、构造两个仓储、读部署 TOML;用例一个都不需要——要验
    的是「基线不合格时整批停在第一个模型调用之前」与「收尾断言在异常路径上也
    跑」这两件编排事实。
    """
    settings = SimpleNamespace(llm_log_path=str(tmp_path / "llm" / "llm.jsonl"))
    repo = SimpleNamespace(
        maintenance=SimpleNamespace(
            resolve_owner_profile=lambda name: SimpleNamespace(id="ab-owner"),
        ),
        settings=settings,
    )
    # `os.environ.update(...)` 不受 monkeypatch 管辖,会漏进同进程的别的用例。
    monkeypatch.setattr(rig, "_ab_process_env", lambda args: {})
    monkeypatch.setattr(rig, "_settings_by_arm",
                        lambda arms: {arm: settings for arm in arms})
    monkeypatch.setattr(rig, "_search_repository", lambda s: repo)
    monkeypatch.setattr(rig, "assert_knowhow_reflect_v2_off", lambda r, s: None)
    monkeypatch.setattr(rig, "_ab_notebook_ids",
                        lambda url, cells: {cell: "nb-1" for cell in cells})
    monkeypatch.setattr(
        rig, "_ab_corpus_facts",
        lambda args, runner, r, cells, nbs, kg: {c: _fact() for c in cells},
    )
    monkeypatch.setattr(rig, "_ab_assert_gold_resolves",
                        lambda *a, **kw: 0)
    monkeypatch.setattr(rig, "_ab_model_contract",
                        lambda s: ("0123456789abcdef", "fake"))
    seen: list[str] = []

    def _counts(url):
        seen.append(url)
        return dict(counts)

    monkeypatch.setattr(rig, "_readonly_counts", _counts)
    monkeypatch.setattr(rig, "_ab_loop", loop)
    return seen


def test_ab_stops_with_exit_2_when_the_main_database_baseline_is_all_unknown(
    tmp_path, monkeypatch, capsys,
):
    """§5.5-2 未验证 ≠ 通过:基线点不出任何一张证据表就整批不跑。"""
    ran: list[int] = []
    seen = _ab_run_harness(
        monkeypatch, tmp_path,
        counts={"ask_jobs": None, "answers": None, "conversations": None},
        loop=lambda *a, **kw: ran.append(1) or 0,
    )
    args = _ab_args(dry_run=False, out_dir=str(tmp_path))
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    assert rig._run_ab(args, runner, [_unit()], ["A_nokg"]) == 2
    err = capsys.readouterr().err
    assert "证据表" in err
    # 跑批从来没开始,收尾那次快照也就不该发生。
    assert ran == [] and len(seen) == 1


def test_the_readonly_assertion_runs_even_when_the_batch_blows_up(
    tmp_path, monkeypatch, capsys,
):
    """收尾的「主库零接触」核对在 `finally` 里(codex 质量评审 P2-5)。

    此前它在 try 之后:跑批中途抛出去时整条断言被跳过——而异常路径恰恰是最该
    量一次的时刻。原始异常照常往上抛,不被那次核对掩盖。
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("批里炸了")

    seen = _ab_run_harness(
        monkeypatch, tmp_path,
        counts={"ask_jobs": 3, "answers": 1, "conversations": 1},
        loop=_boom,
    )
    args = _ab_args(dry_run=False, out_dir=str(tmp_path))
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    with pytest.raises(RuntimeError, match="批里炸了"):
        rig._run_ab(args, runner, [_unit()], ["A_nokg"])
    capsys.readouterr()
    assert len(seen) == 2, "收尾快照没跑"


def test_a_failed_batch_exits_non_zero(tmp_path, monkeypatch, capsys):
    """跑完但每个 run 都 `status=failed` 不能看起来像一次成功的跑批。

    `_ab_loop` 的返回值是 T-EX8 之后的三元组
    `(failed, stopped_by_budget, intent_contract_digest_by_question)`——这里
    `stopped_by_budget=False`(没给 `--max-wall-minutes`),退出码仍然只由
    `failed` 决定。
    """
    _ab_run_harness(
        monkeypatch, tmp_path,
        counts={"ask_jobs": 3, "answers": 1, "conversations": 1},
        loop=lambda *a, **kw: (2, False, {}),
    )
    args = _ab_args(dry_run=False, out_dir=str(tmp_path))
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    assert rig._run_ab(args, runner, [_unit()], ["A_nokg"]) == 1
    assert "2 个 run FAILED" in capsys.readouterr().err
    assert (tmp_path / "manifest.json").exists()


def test_the_readonly_violation_message_names_the_command(tmp_path, capsys):
    """文案不写死「search」:`ab` 的读者不该以为报错来自另一条没跑的命令。"""
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    with pytest.raises(RuntimeError, match="这次 ab 之后变了"):
        rig._assert_readonly(
            runner, {"ask_jobs": 1}, {"ask_jobs": 2}, command="ab",
        )
    capsys.readouterr()


# ---------------------------------------------------------------------------
# 进程环境与 LLM 日志的归属(§5.5-4;§7.2)
# ---------------------------------------------------------------------------


#: `ab` 在共用清单之上**多的那一项**(T-PS5 / 拍板 Q2)。写成一个常量而不是
#: 在断言里内联一个字面量:下面那条「除此之外逐字相同」的守卫要能一眼看出它
#: 允许的差集恰好是这一个键,而不是「差集不为空就放行」。
AB_ONLY_ENV_KEYS = {"REASONING_REFLECT_MEASURE_CONTEXT"}


def test_both_process_envs_carry_exactly_the_same_keys():
    """注入三闸此前在两处逐字重复;漏一项在数据上看不出任何区别。

    键集只差 `ab` 那一项测量开关,其余逐字相同——这条断言是那份清单只有一份的
    证据(codex 质量评审 P2-9)。差集**按名字**钉住:允许「不为空」会让下一个
    往 `_ab_process_env` 里加的任何一项都免检。
    """
    args = _ab_args(env_file="/tmp/does-not-matter.env")
    assert (set(rig._ab_process_env(args)) - set(rig._search_process_env(args))
            == AB_ONLY_ENV_KEYS)
    assert set(rig._search_process_env(args)) - set(rig._ab_process_env(args)) == set()
    without = _ab_args(env_file=None)
    assert (set(rig._ab_process_env(without))
            - set(rig._search_process_env(without)) == AB_ONLY_ENV_KEYS)
    assert "SILICON_NOTEBOOK_ENV_FILE" not in rig._ab_process_env(without)


def _isolate_arm_env(monkeypatch) -> None:
    """`_settings_by_arm` 直接写 `os.environ`(它要的正是「像后端那样起来」)。

    先用 `monkeypatch.setenv` 把这三项各设一次:monkeypatch 在**这一刻**记下原值,
    teardown 时无条件还原/删除,不管中间被谁直接改过。不做这一步,这几条用例会
    把 `REASONING_REFLECT_V2_ENABLED=true` 漏给同一个 worker 里后面的用例——
    `search` 那条路的接线用例于是跑在一份它没要的策略上。
    """
    monkeypatch.setenv("REASONING_REFLECT_V2_ENABLED", "false")
    monkeypatch.setenv("REASONING_REFLECT_OPTIMIZATION", "off")
    monkeypatch.setenv("REASONING_REFLECT_MEASURE_CONTEXT", "true")


def test_each_arm_gets_a_settings_built_with_its_own_optimization(monkeypatch):
    """每条臂的 `Settings` 是**按那条臂的环境构造出来**的,不是热改一个字段。

    `Settings` 带跨字段校验与别名解析,绕过构造器改一个值不会重跑它们;而这里要
    的正是「像后端那样按这两个开关起来的一份配置」。臂由 repo 承载
    (`AskService._build_reasoning_retriever` 读构造期的 Settings),所以这一份
    构造错了,那条臂整批跑的就是另一件事,而数据从形状上看不出来。
    """
    _isolate_arm_env(monkeypatch)
    arms = [("legacy", "off"), ("v2", "prefix_snapshot")]
    built = rig._settings_by_arm(arms)
    assert set(built) == set(arms)
    assert built[("legacy", "off")].reasoning_reflect_v2_enabled is False
    assert built[("legacy", "off")].reasoning_reflect_optimization == "off"
    assert built[("v2", "prefix_snapshot")].reasoning_reflect_v2_enabled is True
    assert (built[("v2", "prefix_snapshot")].reasoning_reflect_optimization
            == "prefix_snapshot")
    assert all(s.reasoning_reflect_measure_context for s in built.values())


def test_settings_by_arm_reads_back_the_newly_implemented_prefix_delta(
    monkeypatch
):
    """T-PD7:新格 `("v2","prefix_delta")` 逐臂 Settings 构造回读核对。

    `prefix_delta` 在 T-PD1 之前会在 `Settings()` 构造期就被
    `validate_reflect_optimization` 拒绝;这条钉住放开一格之后它能像
    `prefix_snapshot` 一样真的起来,而不是仍被 config 的校验器挡在半路。
    """
    _isolate_arm_env(monkeypatch)
    arms = [("v2", "off"), ("v2", "prefix_delta")]
    built = rig._settings_by_arm(arms)
    assert set(built) == set(arms)
    assert built[("v2", "prefix_delta")].reasoning_reflect_v2_enabled is True
    assert (built[("v2", "prefix_delta")].reasoning_reflect_optimization
            == "prefix_delta")
    assert all(s.reasoning_reflect_measure_context for s in built.values())


@pytest.mark.parametrize("arms", [
    [("v2", "prefix_delta"), ("v2", "prefix_delta_lean")],
    [("v2", "off"), ("v2", "prefix_delta_lean")],
])
def test_settings_by_arm_reads_back_the_newly_opened_prefix_delta_lean(
    monkeypatch, arms
):
    """T-PL6:最后一格 `("v2","prefix_delta_lean")` 逐臂 Settings 构造回读核对。

    与 `prefix_delta` 同一条钉法:两种搭配(D↔L 那一对配对臂,以及 off↔L)都
    要能真的起来,而不是仍被 config 的校验器挡在半路。
    """
    _isolate_arm_env(monkeypatch)
    built = rig._settings_by_arm(arms)
    assert set(built) == set(arms)
    assert built[("v2", "prefix_delta_lean")].reasoning_reflect_v2_enabled is True
    assert (built[("v2", "prefix_delta_lean")].reasoning_reflect_optimization
            == "prefix_delta_lean")
    assert all(s.reasoning_reflect_measure_context for s in built.values())


def test_an_env_file_that_pins_the_optimization_stops_the_batch(monkeypatch):
    """显式环境变量被盖住 ⇒ 响亮失败,不静默跑出一批臂对不上号的数据。

    `--env-file` 指向主 checkout 的 `.env`,那里写死一个
    `REASONING_REFLECT_OPTIMIZATION` 是完全可能的;盖住之后两条臂会双双跑同一
    档,差值表恒等于噪声。
    """
    _isolate_arm_env(monkeypatch)

    class _Pinned:
        """构造出来的那一份恒是 `off`,不管环境里设了什么。"""

        reasoning_reflect_v2_enabled = True
        reasoning_reflect_optimization = "off"
        reasoning_reflect_measure_context = True

    import app.core.config as config

    monkeypatch.setattr(config, "Settings", _Pinned)
    with pytest.raises(RuntimeError, match="REASONING_REFLECT_OPTIMIZATION"):
        rig._settings_by_arm([("v2", "prefix_snapshot")])


def test_an_arm_whose_measurement_switch_is_off_stops_the_batch(monkeypatch):
    """一把尺子那条约束也是硬断言,不是一行告警(拍板 Q2)。"""
    _isolate_arm_env(monkeypatch)
    monkeypatch.setenv("REASONING_REFLECT_MEASURE_CONTEXT", "false")
    with pytest.raises(RuntimeError, match="MEASURE_CONTEXT"):
        rig._settings_by_arm([("v2", "off")])


def test_the_ab_arms_share_one_measurement_ruler():
    """拍板 Q2:测量开关与 `optimization` 正交,**两臂都开**。

    只给 `prefix_snapshot` 那一臂开,`off` 臂的测量列会整列缺失,差值表于是只
    剩一侧有数——而每一行看起来都很正常。
    """
    args = _ab_args()
    assert rig._ab_process_env(args)["REASONING_REFLECT_MEASURE_CONTEXT"] == "true"
    # 每条臂各自的 optimization **不在**进程环境里:进程环境只能有一个值,而
    # 一批有两条臂(`_settings_by_arm` 在构造每一份 Settings 时逐臂设)。
    assert "REASONING_REFLECT_OPTIMIZATION" not in rig._ab_process_env(args)


def test_the_two_process_envs_still_point_at_different_databases():
    """同一份清单、两个 `DATABASE_URL`:`search` 连主库,`ab` 连一次性测试库。"""
    args = _ab_args()
    assert rig._ab_process_env(args)["DATABASE_URL"] == args.database_url
    for gate in ("RETRIEVAL_EXPERIENCE_INJECT_ENABLED",
                 "REASONING_CONSULT_MEMORY_ENABLED", "AGENT_PROFILE_ENABLED"):
        assert rig._ab_process_env(args)[gate] == "false"


def test_the_llm_log_is_pinned_to_the_out_dir_and_read_back_from_there(tmp_path):
    """成本三键的归因前提:这个 run 的模型调用写在**只有它在写**的那份日志里。

    默认落点 `.local/logs/llm.jsonl` 是整台机器共用的,同一天里别的后端/冒烟/
    rig 会话都往同一个文件追加——时间窗切片于是把别人的 token 记到本 run 上
    (codex 质量评审 P1-2 / 规格评审 P2-2)。
    """
    args = _ab_args(out_dir=str(tmp_path / "ab"))
    pinned = rig._ab_process_env(args)["LLM_LOG_PATH"]
    assert pinned == str((tmp_path / "ab" / "llm" / "llm.jsonl").resolve())
    # 读侧同源:`_ab_llm_log_dir` 读的就是这一条 env 装出来的 `Settings` 字段,
    # 所以写在哪、读在哪不会分叉。
    assert rig._ab_llm_log_dir(
        SimpleNamespace(llm_log_path=pinned)
    ) == (tmp_path / "ab" / "llm").resolve()


def test_only_the_records_written_after_the_run_started_are_read_back(tmp_path):
    """每 run 重读整天日志是 O(N²) + 内存尖峰(codex 质量评审 P2-6)。

    offset 只负责「不重读别的 run 已经数过的行」,归因判据仍是时间窗。
    """
    log_dir = tmp_path / "llm"
    (log_dir / "u1").mkdir(parents=True)
    path = log_dir / "u1" / "llm-2026-09-09.jsonl"
    path.write_text(json.dumps(_llm_record(-100)) + "\n", encoding="utf-8")
    offsets = rig._ab_llm_offsets(log_dir)
    assert offsets == {str(path): path.stat().st_size}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_llm_record(1)) + "\n")
    fresh = rig._ab_read_llm_records(log_dir, offsets)
    assert len(fresh) == 1
    # 不给 offset 就是从头读(第一个 run 之前目录还不存在的那种情况)。
    assert len(rig._ab_read_llm_records(log_dir, {})) == 2


def test_llm_offsets_on_a_directory_that_does_not_exist_yet(tmp_path):
    assert rig._ab_llm_offsets(tmp_path / "nope") == {}
    assert rig._ab_read_llm_records(tmp_path / "nope", {}) == []


def test_read_llm_records_lets_an_unreadable_file_error_out_instead_of_skipping_it(
    tmp_path,
):
    """一个被 glob 到的文件打不开(权限)⇒ `OSError` 原样往上抛,不吞成
    「这个文件此刻没有新增行」那种沉默的空列表(codex #703 R1 P2)。目录本身
    还不存在(见上一条用例)是另一件事,继续兜成空列表。
    """
    log_dir = tmp_path / "llm"
    (log_dir / "u1").mkdir(parents=True)
    path = log_dir / "u1" / "llm-2026-09-09.jsonl"
    path.write_text(json.dumps(_llm_record(0)) + "\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(OSError):
            rig._ab_read_llm_records(log_dir, {})
    finally:
        path.chmod(0o644)


def test_usage_for_window_is_unknown_when_a_file_cannot_be_read(tmp_path):
    """`_ab_usage_for_window` 据此把这个 run 的成本三键落成 unknown,不是
    当成「这个 run 一次模型都没调」的沉默 0(codex #703 R1 P2)。"""
    log_dir = tmp_path / "llm"
    (log_dir / "u1").mkdir(parents=True)
    path = log_dir / "u1" / "llm-2026-09-09.jsonl"
    path.write_text(json.dumps(_llm_record(0)) + "\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        start = datetime(2026, 9, 9, 12, 0, 0)
        usage = rig._ab_usage_for_window(
            log_dir, start, start + timedelta(seconds=30), concurrency=1,
        )
    finally:
        path.chmod(0o644)
    assert usage.model_calls is None
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None


def test_usage_for_window_is_unknown_when_the_llm_log_is_disabled(
    tmp_path, monkeypatch,
):
    """`LLM_LOG_ENABLED=false` ⇒ 成本三键恒 unknown,连读都不读(codex #703
    R1 P2)。Ask 在这个开关关着时照样调模型,只是不写交互日志——这时候把空
    列表喂给 `slice_llm_usage` 会投出一批看起来精确、实则全错的
    `model_calls=0`。这里连日志目录里明明有一条落在窗口内的记录都不该被读到。
    """
    log_dir = tmp_path / "llm"
    (log_dir / "u1").mkdir(parents=True)
    path = log_dir / "u1" / "llm-2026-09-09.jsonl"
    start = datetime(2026, 9, 9, 12, 0, 0)
    path.write_text(json.dumps(_llm_record(0)) + "\n", encoding="utf-8")

    def _must_not_be_called(*a, **kw):
        raise AssertionError("llm_log_enabled=False 不该再去读日志文件")

    monkeypatch.setattr(rig, "_ab_read_llm_records", _must_not_be_called)
    usage = rig._ab_usage_for_window(
        log_dir, start, start + timedelta(seconds=30), concurrency=1,
        llm_log_enabled=False,
    )
    assert usage is UNKNOWN_USAGE


# ---------------------------------------------------------------------------
# 语料/索引指纹的组成(§7.1,codex #703 R1 P2)
# ---------------------------------------------------------------------------


def _insert_source_row(repo: Any, source_id: str, notebook_id: str, title: str) -> None:
    """往 `sources` 表插一行,只为这里的指纹用例——与 `test_reflect_t0_scripts`
    的同名 helper 同一条口径(公开建来源接口要走真实上传/解析流程,这里只需要
    `id`/`notebook_id`/`title` 三个字段就位)。"""
    with repo._connect() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "text", "extracted", "extracted",
             title, "", "2026-09-08T00:00:00", "2026-09-08T00:00:00"),
        )


def _insert_chunk_row(repo: Any, chunk_id: str, notebook_id: str, source_id: str) -> None:
    with repo._connect() as db:
        db.execute(
            "INSERT INTO chunks "
            "(id, notebook_id, source_id, text, section_path, element_ids, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (chunk_id, notebook_id, source_id, "text", "", "[]",
             "2026-09-08T00:00:00"),
        )


def _update_source_title(repo: Any, source_id: str, title: str) -> None:
    with repo._connect() as db:
        db.execute("UPDATE sources SET title = ? WHERE id = ?", (title, source_id))


def test_corpus_signature_is_stable_across_repeated_calls_on_unchanged_state(
    rrepo, tmp_path,
):
    notebook = _seed_two_nodes(rrepo)
    _insert_source_row(rrepo, "src-1", notebook.id, "paper_v1.md")
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    fact = {
        "notebook": notebook.id, "kg_in_scope": False,
        "sources": 1, "source_rows": rig._ab_source_rows(database_url, notebook.id),
    }
    first = rig._ab_corpus_signature("A_nokg", fact, database_url)
    second = rig._ab_corpus_signature("A_nokg", fact, database_url)
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{16}", first)


def test_corpus_signature_changes_when_a_source_title_changes(rrepo, tmp_path):
    """换一篇论文(标题变了)必须让签名跟着变——旧实现只哈希来源条数,同一个
    格换一篇论文条数不变,签名也不变(codex #703 R1 P2)。"""
    notebook = _seed_two_nodes(rrepo)
    _insert_source_row(rrepo, "src-1", notebook.id, "paper_v1.md")
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    before = rig._ab_corpus_signature("A_nokg", {
        "notebook": notebook.id, "kg_in_scope": False, "sources": 1,
        "source_rows": rig._ab_source_rows(database_url, notebook.id),
    }, database_url)
    _update_source_title(rrepo, "src-1", "a_completely_different_paper.md")
    after = rig._ab_corpus_signature("A_nokg", {
        "notebook": notebook.id, "kg_in_scope": False, "sources": 1,
        "source_rows": rig._ab_source_rows(database_url, notebook.id),
    }, database_url)
    assert before != after


def test_corpus_signature_changes_when_the_chunk_count_changes(rrepo, tmp_path):
    """重新分块会改 `chunks` 的行数,即使来源条数/标题/`kg_in_scope` 都不变
    (codex #703 R1 P2)。"""
    notebook = _seed_two_nodes(rrepo)
    _insert_source_row(rrepo, "src-1", notebook.id, "paper_v1.md")
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    fact = {
        "notebook": notebook.id, "kg_in_scope": False, "sources": 1,
        "source_rows": rig._ab_source_rows(database_url, notebook.id),
    }
    before = rig._ab_corpus_signature("A_nokg", fact, database_url)
    _insert_chunk_row(rrepo, "chunk-1", notebook.id, "src-1")
    after = rig._ab_corpus_signature("A_nokg", fact, database_url)
    assert before != after


def test_corpus_signature_treats_never_built_kg_differently_from_a_zero_count(
    rrepo, tmp_path,
):
    """`unified_kg_state` 里没有这一行(还没建过 KG)与「建过但对象数是 0」是
    两件不同的事,签名不该把它们混成同一个值(codex #703 R1 P2)。

    同一个笔记本先后取两次签名,中间只改 `unified_kg_state` 这一件事——来源
    这一段(id/title/updated_at)与 `chunks`/`source_elements` 行数全程不变,
    真正把两次签名分开的只能是「有没有那一行 `unified_kg_state`」这一项。

    `notebook_store.create_notebook` 本身就会插一行 `unified_kg_state`
    (`object_count` 默认 0,已用真实调用核实过)——SQLite 侧一个真实存在的
    笔记本因此**恒有**这一行,「压根没那一行」在这条路上不会自然出现。这里
    用一次显式 `DELETE` 造出这个边界(测的是 `_ab_corpus_signature` 自己对
    「查不到行」的处理,不是「哪条生产路径会走到这里」)。
    """
    from app.models.schemas import NotebookCreate

    notebook = rrepo.create_notebook(NotebookCreate(name="nb-kg-state"))
    _insert_source_row(rrepo, "src-1", notebook.id, "paper_v1.md")
    database_url = f"sqlite:///{tmp_path / 't.db'}"
    fact = {
        "notebook": notebook.id, "kg_in_scope": True, "sources": 1,
        "source_rows": rig._ab_source_rows(database_url, notebook.id),
    }
    with rrepo._connect() as db:
        db.execute(
            "DELETE FROM unified_kg_state WHERE notebook_id = ?", (notebook.id,)
        )
    never_built = rig._ab_corpus_signature("B_kg", fact, database_url)
    with rrepo._connect() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, object_count, updated_at) "
            "VALUES (?, 0, ?)",
            (notebook.id, "2026-09-08T00:00:00"),
        )
    built_empty = rig._ab_corpus_signature("B_kg", fact, database_url)
    assert never_built != built_empty


# ---------------------------------------------------------------------------
# 枚举链终态与锚点查询表(§7.1)
# ---------------------------------------------------------------------------


def _coverage(complete: bool | None):
    return SimpleNamespace(complete=complete)


def test_coverage_complete_reads_the_enumeration_result_sets():
    """枚举链终态住在 `result_sets[*].coverage`,不在 `result_coverage`。

    `result_coverage` 是 `StructuredBatchCoverage`,只有表格批量那条路会写。只读
    它的后果不是少一个信号,而是**反号**:目录题真的枚举完整时它仍是 `None`,
    `completeness_claim_candidate` 于是把「共 12 篇」判成虚报候选,所有目录题
    两臂全被强制进人工(codex 规格评审 P1-1)。
    """
    complete = SimpleNamespace(
        result_sets=[SimpleNamespace(coverage=_coverage(True))],
        result_coverage=None,
    )
    partial = SimpleNamespace(
        result_sets=[SimpleNamespace(coverage=_coverage(False))],
        result_coverage=None,
    )
    none = SimpleNamespace(result_sets=[], result_coverage=None)
    assert rig._ab_coverage_complete(complete) is True
    assert rig._ab_coverage_complete(partial) is False
    assert rig._ab_coverage_complete(none) is None
    # 两条链都在场:有一条 partial 就不算 complete——那句「共 N 篇」可能正是
    # 关于没枚举完的那条说的,一条无关的 complete 不能替它免掉人工复核
    # (codex #703 R3 P2)。
    mixed = SimpleNamespace(
        result_sets=[SimpleNamespace(coverage=_coverage(False)),
                     SimpleNamespace(coverage=_coverage(True))],
        result_coverage=_coverage(False),
    )
    assert rig._ab_coverage_complete(mixed) is False
    all_complete = SimpleNamespace(
        result_sets=[SimpleNamespace(coverage=_coverage(True)),
                     SimpleNamespace(coverage=_coverage(True))],
        result_coverage=None,
    )
    assert rig._ab_coverage_complete(all_complete) is True
    # 表格批量那条路仍然算数(它自己那份 coverage 是它的权威)。
    table_only = SimpleNamespace(result_sets=[], result_coverage=_coverage(True))
    assert rig._ab_coverage_complete(table_only) is True


def test_a_complete_enumeration_stops_the_completeness_candidate():
    """端到端:枚举链报 complete ⇒ 「共 12 篇」不再是虚报候选。"""
    response = SimpleNamespace(
        result_sets=[SimpleNamespace(coverage=_coverage(True))],
        result_coverage=None,
    )
    assert completeness_claim_candidate(
        "共 12 篇。", coverage_complete=rig._ab_coverage_complete(response),
    ) is False


def test_an_all_empty_id_list_short_circuits_instead_of_building_in_nothing():
    """`["", "", ""]` 非空、唯一 id 集合却是空的 ⇒ 拼出来的是 `IN ()`。

    在 SQLite 上它返回 `[]`(每个锚点都「查不到」⇒ 整键 unknown),在 PostgreSQL
    上是一句语法错、被外层大 `except` 吞成同一个 unknown(codex 规格评审 P2-1)。
    这里用一个连不上的 URL 分辨两种返回:`{}` = 短路了(压根没查),`None` =
    真去查了并且失败。
    """
    dead = "postgresql://127.0.0.1:1/nope"
    assert rig._ab_unique_ids(["", None, "  ", "e1", "e1"]) == ["e1"]
    assert rig._ab_element_sections(dead, ["", "", ""]) == {}
    assert rig._ab_chunk_sections(dead, ["", None]) == {}
    # 真有 id 时才去查,查不通就是 unknown。
    assert rig._ab_element_sections(dead, ["e1"]) is None


def test_the_b_shape_never_queries_the_element_or_chunk_tables():
    """B 格走 `source_titles`(已在 `_ab_corpus_facts` 点清),不该再打两条查询。"""
    def _boom(*args, **kwargs):
        raise AssertionError("B 格不该查库")

    saved = (rig._ab_element_sections, rig._ab_chunk_sections)
    rig._ab_element_sections = _boom
    rig._ab_chunk_sections = _boom
    try:
        anchors = [SimpleNamespace(element_id="e1", object_id="", object_type="",
                                   location_label="")]
        assert rig._ab_section_lookups("db", _B_GOLD, anchors) == (None, None)
        # 没 gold 的题同理:`count_anchors_on_gold` 对它恒返回 None。
        assert rig._ab_section_lookups("db", None, anchors) == (None, None)
    finally:
        rig._ab_element_sections, rig._ab_chunk_sections = saved


def test_the_a_shape_only_asks_the_chunks_table_about_labelless_chunks():
    """带 `location_label` 的 chunk 锚点已经自带 section,不必再查一次库。"""
    asked: dict[str, list[str]] = {}

    def _record(name):
        def _inner(url, ids):
            asked[name] = list(ids)
            return {}
        return _inner

    saved = (rig._ab_element_sections, rig._ab_chunk_sections)
    rig._ab_element_sections = _record("elements")
    rig._ab_chunk_sections = _record("chunks")
    try:
        rig._ab_section_lookups("db", _A_GOLD, [
            SimpleNamespace(element_id="e1", object_id="c0", object_type="chunk",
                            location_label=""),
            SimpleNamespace(element_id="", object_id="c1", object_type="chunk",
                            location_label="4.1 Setup"),
            SimpleNamespace(element_id="", object_id="c2", object_type="chunk",
                            location_label=""),
        ])
    finally:
        rig._ab_element_sections, rig._ab_chunk_sections = saved
    assert asked["elements"] == ["e1", "", ""]
    assert asked["chunks"] == ["c2"]


# ---------------------------------------------------------------------------
# 失败 run 的落行、契约同一性与并发收摊(§5.5-3;codex 质量评审 P2-3/P2-4)
# ---------------------------------------------------------------------------


def _fixed_intents(monkeypatch, contract: Any = None):
    """把 `_IntentCache.get` 换成一个固定返回,用例里不算真实意图契约。"""
    payload = contract if contract is not None else {"resolved_question": "q"}
    monkeypatch.setattr(
        rig._IntentCache, "get",
        lambda self, repo, settings, item, *, actor_id, notebook: payload,
    )
    return payload


def _run_one_unit(tmp_path, unit, args, *, state=None, intents=None,
                  facts=None, runner=None):
    """跑一个配对单元,回 `(读回来的行, state)`。"""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = runner or rig.Runner(dry_run=False, out_dir=out_dir)
    state = state or {"verified": set(), "index": 0, "total": 2, "failed": 0,
                      "lock": threading.Lock()}
    rows_path = out_dir / "ab-runs.jsonl"
    with rows_path.open("a", encoding="utf-8") as rows_handle, \
            (out_dir / "ab-runs.log").open("a", encoding="utf-8") as log:
        rig._run_ab_unit(
            unit, args=args, runner=runner,
            facts=facts or {"A_nokg": _fact()}, repos_by_arm=_repos_by_arm(),
            actor_id="ab-owner", profile=None, concurrency=1,
            intents=intents or rig._IntentCache(
                out_dir / "intents.jsonl", enabled=False),
            gold_by_key=load_ab_gold(load_questions()),
            model_contract="0123456789abcdef", log_dir=out_dir,
            clock=datetime.now, state=state,
            rows_handle=rows_handle, log=log,
        )
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    return rows, state


def test_a_scope_failure_lands_one_failed_row_per_arm(
    tmp_path, monkeypatch, capsys,
):
    """声明了范围却解析不到唯一匹配 ⇒ 两臂各落一行 `status=failed`。

    此前这条路直接 `return`:整个单元从 `ab-runs.jsonl` 里凭空消失,而分析脚本
    只读 JSONL——样本数因此偏向跑成的那一侧(codex 质量评审 P2-4)。
    """
    ran: list[str] = []
    monkeypatch.setattr(rig, "resolve_scope_source_ids", lambda *a, **kw: None)
    monkeypatch.setattr(rig, "run_ab_once",
                        lambda *a, **kw: ran.append("跑了") or _FakeResponse())
    rows, state = _run_one_unit(
        tmp_path, _unit(scope_source_titles=("KIVI",)),
        _ab_args(only_policy=[], out_dir=str(tmp_path / "ab"), dry_run=False),
    )
    capsys.readouterr()
    assert ran == [], "范围没解析出来就不该真的去跑"
    assert len(rows) == 2
    assert {row["arm"] for row in rows} == {"legacy", "v2"}
    # 失败行按 rig **声明**的臂归组:空轨迹没有协议证据,不给声明标签的话 v2 的
    # 失败会被投影成 legacy(codex #700 R9/R10 P2 的同一条口径)。
    assert {row["policy_version"] for row in rows} == {"legacy", "v2"}
    for row in rows:
        assert row["status"] == "failed"
        assert set(row) <= AB_PROJECTION_KEYS
        assert_projection_values(row)
        # 数值键落 unknown 而不是 0:`answer_chars=0` 会被读成「模型答了个空串」。
        assert row["answer_chars"] is None and row["citations"] is None
        assert row["latency_ms_total"] is None
        # 工作负载维度照常落,失败行要能与成功行放进同一张分组表。
        assert row["effort"] == "standard" and row["corpus_cell"] == "A_nokg"
        assert row["question_key"] == "A-q08" and row["repeat"] == 1
        assert row["corpus_signature"] == "fedcba9876543210"
    assert state["failed"] == 2


def test_an_arm_that_blows_up_lands_a_failed_row_without_losing_the_other_arm(
    tmp_path, monkeypatch, capsys,
):
    """一条臂崩了不作废另一条,而且崩掉的那条也要在数据集里留下一行。

    「哪一侧更容易崩」本身就是 A/B 要量的东西之一;把失败静静吞掉会让它变成
    「v2 的样本更少」这种看不出来的偏差(codex 质量评审 P2-4)。
    """
    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        for step in _steps(arm):
            on_trace(SimpleNamespace(step_type=step["step_type"], summary="",
                                     detail=step["detail"], duration_ms=3))
        if arm == "v2":
            raise RuntimeError("provider 502")
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    rows, state = _run_one_unit(
        tmp_path, _unit(),
        _ab_args(only_policy=[], out_dir=str(tmp_path / "ab"), dry_run=False),
    )
    capsys.readouterr()
    by_arm = {row["arm"]: row for row in rows}
    assert set(by_arm) == {"legacy", "v2"}
    assert by_arm["legacy"]["status"] == "done"
    assert by_arm["legacy"]["answer_chars"] > 0
    assert by_arm["v2"]["status"] == "failed"
    assert by_arm["v2"]["policy_version"] == "v2"
    # 失败前捕获到的轨迹步不丢(codex #700 R13 P2):丢掉它们会把
    # `reflect_turns` 记成 0,失败多的那一侧看起来「反思轮更少」。
    assert by_arm["v2"]["reflect_turns"] == by_arm["legacy"]["reflect_turns"]
    assert state["failed"] == 1
    # 崩掉的那条臂不消费「第一个样本」的名额——它没有协议证据可对。名额按
    # `(policy, optimization)` 记(二维臂)。
    assert state["verified"] == {("legacy", "off")}
    # 崩在半路的 run 照样有墙钟:它确实占了这么久(计划 §3 T-PS5 验收)。
    assert isinstance(by_arm["v2"]["run_wall_ms"], int)
    assert by_arm["v2"]["latency_ms_total"] is None
    # 跑成的那一条也要有——E1 的头条结论正是这一列的臂间差。只钉失败行的话,
    # 日后重排 `_run_ab_arm` 尾部会让成功 run 的墙钟全线落 `None`,而数据看起来
    # 完整、配对差值表那一格恒空,一个用例都不红(codex 质量评审 P2-2)。
    assert isinstance(by_arm["legacy"]["run_wall_ms"], int)
    assert by_arm["legacy"]["run_wall_ms"] == by_arm["legacy"]["latency_ms_total"]
    log_text = (tmp_path / "ab" / "ab-runs.log").read_text(encoding="utf-8")
    assert "status=failed reason=RuntimeError" in log_text
    assert "provider 502" not in log_text  # 只记类名,不记异常正文


def test_the_contract_digest_is_taken_fresh_inside_the_arm_loop(
    tmp_path, monkeypatch, capsys,
):
    """§5.5-3 的断言必须**在臂的循环里各取各比**(codex 规格评审 P2-3)。

    循环外取一次、循环内拿同一个对象比自己的短码,那条断言恒真——而它要挡的
    恰恰是「哪天有人把 `intents.get` 挪进臂的循环」:挪进来之后数据照样出得来,
    两臂却已经在比两件不同的事了。
    """
    handed: list[dict] = []
    # 取第 1 次(基线)、第 2 次(第一条臂)拿到同一份;第 3 次(第二条臂)换人。
    contracts = [{"a": 1}, {"a": 1}, {"a": 2}]
    fetched: list[int] = []

    def _get(self, repo, settings, item, *, actor_id, notebook):
        fetched.append(1)
        return contracts[min(len(fetched) - 1, len(contracts) - 1)]

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        handed.append(contract)
        for step in _steps(arm):
            on_trace(SimpleNamespace(step_type=step["step_type"], summary="",
                                     detail=step["detail"], duration_ms=1))
        return _FakeResponse()

    monkeypatch.setattr(rig._IntentCache, "get", _get)
    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    args = _ab_args(only_policy=[], out_dir=str(tmp_path / "ab"), dry_run=False)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="不是同一份"):
        _run_one_unit(
            tmp_path, _unit(), args,
            intents=rig._IntentCache(out_dir / "intents.jsonl", enabled=True),
        )
    capsys.readouterr()
    # 第一条臂拿到的是与基线相同的那一份;第二条臂再取一次,发现换人了就停。
    assert handed == [{"a": 1}]
    # 基线一次 + 每条臂各一次 = 3 次:循环外取一次、循环内比同一个对象的写法
    # 只会取 1 次,那种写法下这条断言恒真。
    assert len(fetched) == 3


def test_the_progress_total_follows_the_arms_that_are_actually_run(
    tmp_path, monkeypatch, capsys,
):
    """`--only-policy` 只跑一侧时,分母不该恒按两臂算(codex 评审 P3)。"""
    monkeypatch.setattr(
        rig, "run_ab_once",
        lambda repo, **kw: (
            [kw["on_trace"](SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1))
             for step in _steps(kw["arm"])],
            _FakeResponse(),
        )[1],
    )
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)
    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    # T-EX8:`_ab_loop` 现在返回三元组
    # `(failed, stopped_by_budget, intent_contract_digest_by_question)`。
    failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, [_unit(question_key="A-q08"), _unit(question_key="A-q14")],
        {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir,
        clock=datetime.now,
    )
    capsys.readouterr()
    assert failed == 0
    assert stopped_by_budget is False
    lines = (out_dir / "ab-runs.log").read_text(encoding="utf-8").splitlines()
    assert [line.split()[0] for line in lines] == ["0001/2", "0002/2"]


def test_a_concurrent_batch_cancels_the_remaining_units_after_the_first_abort(
    tmp_path, monkeypatch, capsys,
):
    """守卫:§5.5 的硬断言不成立 ⇒ 收摊,余下单元不再执行。

    此前这里是 `with ThreadPoolExecutor(...)` + `as_completed`,退出走的是默认
    `shutdown(wait=True)`:剩下的单元一个不少地照跑完,「先修再跑整批」这句话
    不成立,Ctrl-C 也停不下来(codex 质量评审 P2-3)。

    第一个单元(`Q00`)回一份 legacy 形状的轨迹,而它声明的臂是 v2 ⇒
    `assert_arm_matches_evidence` 抛;其余单元卡在**各自的** `cancel_event.wait()`
    上,只有被 `abort()` 设过之后才会醒来。断言:异常真的抛出来、没有全部 8 个
    单元都执行到、而且在跑的那个被及时唤醒(不是靠 5s 超时自己醒的)。
    """
    from app.services.cancellation import AskCancelled

    total = 8
    units = [_unit(question_key=f"Q{i:02d}") for i in range(total)]
    executed: list[str] = []
    lock = threading.Lock()

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        with lock:
            executed.append(item["question_key"])
        if item["question_key"] == "Q00":
            # legacy 形状:没有终态披露步,而声明的臂是 v2。
            on_trace(SimpleNamespace(step_type="reflect", summary="",
                                     detail={"next_action": "answer"},
                                     duration_ms=1))
            return _FakeResponse()
        if cancel_event.wait(timeout=5):
            raise AskCancelled()
        raise TimeoutError("cancel_event 一直没被设置——abort() 没生效")

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="policy_version"):
        rig._ab_loop(
            args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
            actor_id="ab-owner", profile=None, concurrency=2,
            gold_by_key=load_ab_gold(load_questions()),
            model_contract="0123456789abcdef", log_dir=out_dir,
            clock=datetime.now,
        )
    elapsed = time.monotonic() - started
    capsys.readouterr()
    assert len(executed) < total, executed
    # 队列里还没开始跑的靠 `shutdown(cancel_futures=True)` 撤掉;**已经在跑、卡在
    # 自己 cancel_event 上**的那一个只能靠 `abort()` 主动 `.set()` 唤醒。它上面
    # 那个 5s 超时就是留给「abort() 没生效」的:那种退化不会让断言失败,只会让
    # 用例慢一拍,所以这里把它钉成响亮失败。
    assert elapsed < 3.0, f"in-flight 单元没有被 abort() 及时唤醒({elapsed:.1f}s)"


def test_a_concurrent_abort_logs_only_the_exception_class_not_its_text(
    tmp_path, monkeypatch, capsys,
):
    """并发收摊那句 `runner.say("ab aborted", ...)` 曾经把 `str(exc)` 原样内插
    进去(截 200 字不算脱敏):`_write_ab_raw` 撞上 `PermissionError` 时,异常
    文本里带着本机的绝对路径,会被原样打进终端与 `ab-runs.log`;别的异常也
    可能带 provider 侧的请求细节(codex #703 R1 P2)。这里让 Q00 在
    `_write_ab_raw` 那一步抛一个带假路径的 `PermissionError`,其余单元照
    `test_a_concurrent_batch_cancels_the_remaining_units_after_the_first_abort`
    的老办法卡在各自的 `cancel_event` 上,断言 stdout 与 `ab-runs.log` 都不含
    那段路径,只含类名。
    """
    from app.services.cancellation import AskCancelled

    total = 4
    units = [_unit(question_key=f"Q{i:02d}") for i in range(total)]
    executed: list[str] = []
    lock = threading.Lock()

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        with lock:
            executed.append(item["question_key"])
        if item["question_key"] == "Q00":
            for step in _steps(arm):
                on_trace(SimpleNamespace(step_type=step["step_type"], summary="",
                                         detail=step["detail"], duration_ms=1))
            return _FakeResponse()
        if cancel_event.wait(timeout=5):
            raise AskCancelled()
        raise TimeoutError("cancel_event 一直没被设置——abort() 没生效")

    def _boom_write_ab_raw(*a, **kw):
        raise PermissionError(
            "[Errno 13] Permission denied: '/Users/secret/path/ab/raw.json'"
        )

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    monkeypatch.setattr(rig, "_write_ab_raw", _boom_write_ab_raw)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    with pytest.raises(PermissionError):
        rig._ab_loop(
            args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
            actor_id="ab-owner", profile=None, concurrency=2,
            gold_by_key=load_ab_gold(load_questions()),
            model_contract="0123456789abcdef", log_dir=out_dir,
            clock=datetime.now,
        )
    out = capsys.readouterr().out
    log_text = (out_dir / "ab-runs.log").read_text(encoding="utf-8")
    assert "/Users/secret/path" not in out
    assert "/Users/secret/path" not in log_text
    assert "PermissionError" in out


# --- T-PS4 测量列在 A/B 侧的继承 ---------------------------------------------


def test_ab_keys_inherit_the_measurement_columns_automatically():
    """`AB_PROJECTION_KEYS = RUN_PROJECTION_KEYS | AB_ONLY_KEYS`,所以 T0 侧新增
    的九个测量列**自动**进 A/B 闭集,不需要在这边再登记一遍。

    这条钉的是「不要在 A/B 侧另抄一份键集」:抄一份就会在下一次 T0 扩集时分叉,
    两批数据从此进不了同一张表。

    变异:把 `AB_PROJECTION_KEYS` 改成手写的字面量集合 ⇒ 这条红。
    """
    measurement_keys = {
        "run_wall_ms", "model_calls_real", "attempts_observed", "optimization",
        "context_chars", "prefix_bytes_median", "prefix_bytes_min",
        "prefix_turns", "response_chars_total",
    }
    assert measurement_keys <= RUN_PROJECTION_KEYS
    assert measurement_keys <= AB_PROJECTION_KEYS
    # 它们属于 T0 那一半,不该被复制进 A/B 自己的增量键集。
    assert not (measurement_keys & AB_ONLY_KEYS)


def test_the_privacy_guard_still_holds_after_the_measurement_columns():
    """键名层的第一道闸对新键照样成立(`*_id` / 自由文本词根)。"""
    for key in ("run_wall_ms", "model_calls_real", "attempts_observed",
                "optimization", "context_chars", "prefix_bytes_median",
                "prefix_bytes_min", "prefix_turns", "response_chars_total"):
        assert not key.endswith("_id"), key
        assert not (set(key.split("_")) & set(FORBIDDEN_KEY_MARKERS)), key
        # 统一硬约束 §2:字段命名不出现 `cache_hit` / 命中率。
        assert "cache_hit" not in key, key


def test_an_ab_row_without_measurements_carries_them_as_unknown():
    """没开测量的 A/B run:新列全 `None`,`optimization` 落 rig 声明的那一格。

    T-PS5 之后 `optimization` **恒有声明**(`project_ab_run` 的默认值 `off` =
    一维写法),所以这一列不再是 unknown;线上导出那条路仍然是 unknown,由
    `test_reasoning_trace_stats` 的 `_optimization` 用例钉住。

    `model_calls`(日志行数)与 `model_calls_real`(真正发出的请求数)是两套口径,
    一个有值不代表另一个有值——这里正好把两者的独立性钉住。
    """
    row = _project()
    assert row["optimization"] == "off"
    for key in ("run_wall_ms", "model_calls_real", "attempts_observed",
                "context_chars", "prefix_bytes_median", "prefix_bytes_min",
                "prefix_turns", "response_chars_total"):
        assert row[key] is None, key
    assert_ab_closed(row)
    assert_projection_values(row)


def test_an_ab_row_with_measurements_passes_both_ab_guards():
    """测量开着时,整行仍然过 `assert_ab_closed` + `assert_projection_values`。

    这是 T-PS4 对 A/B 侧唯一的验收:新列的**值形状**(数值 / 短码 → 数值字典)
    过得了那道不看键名的闸。
    """
    steps = _steps()
    steps[1] = {"step_type": "reflect", "detail": {
        "next_action": "answer", "sufficient": True,
        "ctx_chars_s": 800, "ctx_chars_c": 1200, "ctx_bytes_total": 9600,
        "message_prefix_bytes": 3134, "call_attempts": 2, "response_chars": 90,
        "cards_shown": 6, "cards_omitted": 1, "call_wall_ms": 2100,
    }}
    row = _project(steps=steps)
    assert row["model_calls_real"] == 2
    assert row["attempts_observed"] is True
    assert row["context_chars"] == {"s": 800, "c": 1200, "bytes_total": 9600}
    # 首轮没有可比的上一轮,但这条轨迹只有一轮 reflect 且写侧给了值,于是三格齐。
    assert row["prefix_bytes_median"] == 3134
    assert row["prefix_turns"] == 1
    assert row["response_chars_total"] == 90
    assert set(row) <= AB_PROJECTION_KEYS
    assert_ab_closed(row)
    assert_projection_values(row)


def test_free_text_in_a_measurement_key_is_refused_on_an_ab_row():
    """隐私守卫的变异形态:往新列里塞自由文本,A/B 侧照样被拦。

    这道闸不看键名,所以不需要在 A/B 侧为新键再写一份规则——这条用例就是在证明
    那一点。
    """
    row = dict(_project())
    row["context_chars"] = {"note": "把摘要整块搬到了末尾"}
    with pytest.raises(ValueError, match="context_chars"):
        assert_projection_values(row)


# ---------------------------------------------------------------------------
# per-call 表的 rig 侧薄适配(计划 §3 T-PS5)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _call_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """rig 真跑时的两个并列目录(`rig_llm_log_path` / `rig_event_log_dir`)。"""
    return tmp_path / "llm", tmp_path / "events"


def test_the_per_call_adapter_reads_both_logs_and_joins_them(tmp_path):
    """薄适配把两个目录按各自的 glob 铺开,归因逻辑全在纯函数里。

    两串日志落在**两个并列目录**,glob 因此互不相交:llm 目录里多一份
    `events-*.jsonl`(或反过来)也不会被当成对面那一串读进来。
    """
    log_dir, event_dir = _call_dirs(tmp_path)
    _write_jsonl(log_dir / "u1" / "llm-2026-09-10.jsonl", [
        {"kind": "chat", "status": "ok", "support_id": "mdl-a",
         "latency_ms": 900, "attempts": 1, "response_chars": 30},
    ])
    _write_jsonl(event_dir / "u1" / "events-2026-09-10.jsonl", [
        {"kind": "model_scheduler", "status": "ok", "support_id": "mdl-a",
         "workload_id": "reasoning_reflect", "queue_latency_ms": 4,
         "execution_latency_ms": 880},
        # 同一份事件日志里还躺着别的 kind:它们不是 per-call 的调度事实。
        {"kind": "http_request", "status": "ok", "path": "/api/ask"},
    ])
    rows = rig._rig_call_rows(
        log_dir, event_dir, llm_offsets={}, event_offsets={},
        tags={"arm": "v2", "optimization": "prefix_snapshot",
              "question_key": "A-q08", "corpus_cell": "A_nokg",
              "effort": "standard", "repeat": 1},
    )
    assert len(rows) == 1
    assert rows[0]["join"] == "joined"
    assert rows[0]["queue_latency_ms"] == 4
    assert rows[0]["optimization"] == "prefix_snapshot"


def test_the_per_call_adapter_only_reads_the_increment_after_the_offsets(tmp_path):
    """偏移与成本三键那条路同源:一个 run 只读它自己那一段,不重读整天日志。"""
    log_dir, event_dir = _call_dirs(tmp_path)
    llm_path = log_dir / "u1" / "llm-2026-09-10.jsonl"
    event_path = event_dir / "u1" / "events-2026-09-10.jsonl"
    _write_jsonl(llm_path, [{"kind": "chat", "support_id": "mdl-old"}])
    _write_jsonl(event_path, [{"kind": "model_scheduler", "support_id": "mdl-old"}])
    llm_offsets = rig._ab_llm_offsets(log_dir)
    event_offsets = rig._ab_llm_offsets(event_dir, glob=rig.RIG_EVENT_LOG_GLOB)
    _write_jsonl(llm_path, [{"kind": "chat", "support_id": "mdl-new"}])
    _write_jsonl(event_path, [{"kind": "model_scheduler", "support_id": "mdl-new"}])
    rows = rig._rig_call_rows(
        log_dir, event_dir, llm_offsets=llm_offsets,
        event_offsets=event_offsets, tags=None,
    )
    assert [row["support_id"] for row in rows] == ["mdl-new"]


def test_the_per_call_adapter_survives_a_value_error_from_the_join(
    tmp_path, monkeypatch,
):
    """归因抛 `ValueError` ⇒ 空列表,不是让整批在第 N 个单元中止。

    这是**第二层**:第一层在 `reflect_context_bench._short_code`,它把不合形状
    的 provider 串收成 unknown,那一行别的列照样是真的。这一层保住的只是「整批
    不会白烧」——串行路这张表在 `_run_ab_arm` 的 `return` 表达式里求值,并发路
    它在 `_ab_loop` 的 `finally` 里(抛出去还会顶掉在途异常并跳过失败计数)。
    """
    log_dir, event_dir = _call_dirs(tmp_path)
    _write_jsonl(log_dir / "u1" / "llm-2026-09-10.jsonl",
                 [{"kind": "chat", "support_id": "mdl-a"}])
    import app.eval.reflect_context_bench as bench

    def _boom(*a, **kw):
        raise ValueError("projection value at 'finish_reason' ...")

    monkeypatch.setattr(bench, "join_calls", _boom)
    assert rig._rig_call_rows(
        log_dir, event_dir, llm_offsets={}, event_offsets={}, tags=None,
    ) == []


def test_the_per_call_adapter_survives_a_directory_that_does_not_exist(tmp_path):
    """第一个 run 之前两个目录都还不存在:空列表,不是异常。

    per-call 表是诊断产物,读不到它不该让一个跑成了的 run 整体作废。
    """
    log_dir, event_dir = _call_dirs(tmp_path)
    assert rig._rig_call_rows(
        log_dir, event_dir, llm_offsets={}, event_offsets={}, tags=None,
    ) == []


def test_a_concurrent_batch_writes_its_calls_table_without_an_arm(tmp_path):
    """并发那一批:run 级标签(含 `arm`)全 unknown,文件名跟着叫 unknown。

    `support_id` 只把日志行与它自己的调度事件对上号,它不知道这次调用属于哪个
    run;硬塞一条臂进去比不出表更坏——那是一批看起来精确、实则一半记错了臂的
    行(与并发下成本三键强制 unknown 同一条口径)。
    """
    tags = rig._rig_call_tags(_unit(), ("v2", "prefix_snapshot"),
                              attributed=False)
    assert tags is None
    attributed = rig._rig_call_tags(_unit(), ("v2", "prefix_snapshot"),
                                    attributed=True)
    assert attributed["arm"] == "v2"
    assert attributed["optimization"] == "prefix_snapshot"
    assert rig.CALLS_UNATTRIBUTED_LABEL == "unknown"


def test_writing_zero_call_rows_creates_no_file(tmp_path):
    """一个 run 一次模型都没调(或日志关着)⇒ 不建一个空文件。"""
    rig._write_call_rows(tmp_path, "v2", [])
    assert not (tmp_path / "calls-v2.jsonl").exists()
    rig._write_call_rows(tmp_path, "v2", [{"support_id": "mdl-a"}])
    assert (tmp_path / "calls-v2.jsonl").read_text("utf-8").strip()


def test_call_rows_accumulate_across_units_instead_of_truncating(tmp_path):
    """这个文件是**追加**的:串行下每个单元每条臂都往同一份表里写一次。

    翻成截断的代价看不出来:文件在、非空、`ab-runs.jsonl` 完全正常,只是
    `calls-<arm>.jsonl` 里只剩最后一个单元的调用行,前面几十个单元的账没了。
    """
    rig._write_call_rows(tmp_path, "v2", [{"support_id": "mdl-a"},
                                          {"support_id": "mdl-b"}])
    rig._write_call_rows(tmp_path, "v2", [{"support_id": "mdl-c"}])
    lines = (tmp_path / "calls-v2.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["support_id"] for line in lines] == [
        "mdl-a", "mdl-b", "mdl-c",
    ]


def test_a_serial_batch_attributes_its_calls_to_the_arm_that_made_them(
    tmp_path, monkeypatch, capsys,
):
    """串行跑批:per-call 行落进 `calls-<arm>.jsonl`,带全 run 级标签。

    串行下窗口是**独占**的(偏移 → EOF 恰好是这一个 run 写的那几行),所以
    run→call 的归因成立;并发下不成立,由下一条用例钉住。
    """
    out_dir = tmp_path / "ab"
    log_dir, event_dir = _call_dirs(out_dir)

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        # run 期间模型调用真的写进这两串日志(生产由 `app/core/llm.py` 与
        # `model_provider._emit` 写;这里直接造那两行)。
        support = f"mdl-{arm}-{optimization}"
        _write_jsonl(log_dir / "u1" / "llm-2026-09-10.jsonl", [
            {"kind": "chat", "status": "ok", "support_id": support,
             "latency_ms": 700, "attempts": 1, "response_chars": 12},
        ])
        _write_jsonl(event_dir / "u1" / "events-2026-09-10.jsonl", [
            {"kind": "model_scheduler", "status": "ok", "support_id": support,
             "workload_id": "reasoning_reflect", "queue_latency_ms": 3,
             "execution_latency_ms": 690},
        ])
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="人话摘要",
                detail=step["detail"], duration_ms=7,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False)
    arms = [("v2", "off"), ("v2", "prefix_snapshot")]
    rig._ab_loop(
        args, runner, [_unit(question_key="A-q08")], {"A_nokg": _fact()},
        _repos_by_arm(arms), actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=log_dir,
        event_dir=event_dir, arms=arms, reference_arm=arms[0],
        clock=datetime.now,
    )
    capsys.readouterr()
    for arm in arms:
        path = out_dir / f"calls-{arm_label(*arm)}.jsonl"
        rows = [json.loads(line)
                for line in path.read_text("utf-8").splitlines()]
        assert [row["join"] for row in rows] == ["joined"]
        assert rows[0]["arm"] == arm[0]
        assert rows[0]["optimization"] == arm[1]
        assert rows[0]["question_key"] == "A-q08"
        assert rows[0]["queue_latency_ms"] == 3
    # 两条臂的表分开:一条臂的调用不会出现在另一条臂的文件里。
    assert not (out_dir / f"calls-{rig.CALLS_UNATTRIBUTED_LABEL}.jsonl").exists()


def test_a_concurrent_batch_joins_by_support_id_but_claims_no_arm(
    tmp_path, monkeypatch, capsys,
):
    """并发跑批:⋈ 照做(`support_id` 不受窗口影响),run 级标签全 unknown。

    这正是计划 §3 T-PS5 验收那句「并发下按 `support_id` 归因,切不干净仍
    unknown」的两半:对得上号的行照样两侧齐全,而**这次调用属于哪个 run** 是
    时间窗才能回答的问题,并发下它切不干净,所以那几列一律 unknown。
    """
    out_dir = tmp_path / "ab"
    log_dir, event_dir = _call_dirs(out_dir)
    lock = threading.Lock()

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        support = f"mdl-{item['question_key']}-{arm}"
        with lock:
            _write_jsonl(log_dir / "u1" / "llm-2026-09-10.jsonl", [
                {"kind": "chat", "status": "ok", "support_id": support,
                 "latency_ms": 700, "attempts": 1},
            ])
            _write_jsonl(event_dir / "u1" / "events-2026-09-10.jsonl", [
                {"kind": "model_scheduler", "status": "ok",
                 "support_id": support, "queue_latency_ms": 3},
            ])
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=7,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False,
                    no_intent=True)
    units = [_unit(question_key="A-q08"), _unit(question_key="A-q14")]
    rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=2,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=log_dir,
        event_dir=event_dir, clock=datetime.now,
    )
    capsys.readouterr()
    # 逐臂的表**不出**:那一维在并发下不成立。
    for arm in rig.AB_DEFAULT_ARMS:
        assert not (out_dir / f"calls-{arm_label(*arm)}.jsonl").exists()
    path = out_dir / f"calls-{rig.CALLS_UNATTRIBUTED_LABEL}.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert len(rows) == 4                       # 2 题 × 2 臂
    assert all(row["join"] == "joined" for row in rows)   # ⋈ 照样对得上号
    assert all(row["queue_latency_ms"] == 3 for row in rows)
    for row in rows:
        for key in ("arm", "optimization", "question_key", "repeat"):
            assert row[key] is None, key


def test_a_second_concurrent_batch_does_not_eat_the_first_batch_events_again(
    tmp_path, monkeypatch, capsys,
):
    """往同一个 `--out-dir` 追跑第二批:事件偏移按**事件的 glob** 取。

    整批偏移那两行是两串日志各取各的(`RIG_EVENT_LOG_GLOB`)。事件那一串若按
    llm 的默认 glob 取偏移,`events-*.jsonl` 一份都匹配不上 ⇒ offsets 恒空 ⇒ 每
    一批都从字节 0 把事件重读一遍,第二批的 `calls-unknown.jsonl` 里于是躺着上
    一批事件的副本(`event_only` 行),而 llm 那一侧偏移正常、行数看起来只多不
    少,没有任何一处会报错。
    """
    out_dir = tmp_path / "ab"
    log_dir, event_dir = _call_dirs(out_dir)
    lock = threading.Lock()

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        support = f"mdl-{item['question_key']}-{arm}"
        with lock:
            _write_jsonl(log_dir / "u1" / "llm-2026-09-10.jsonl", [
                {"kind": "chat", "status": "ok", "support_id": support,
                 "latency_ms": 700, "attempts": 1},
            ])
            _write_jsonl(event_dir / "u1" / "events-2026-09-10.jsonl", [
                {"kind": "model_scheduler", "status": "ok",
                 "support_id": support, "queue_latency_ms": 3},
            ])
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=7,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False,
                    no_intent=True)
    path = out_dir / f"calls-{rig.CALLS_UNATTRIBUTED_LABEL}.jsonl"

    def _run_batch(question_key: str) -> list[dict]:
        before = (len(path.read_text("utf-8").splitlines())
                  if path.exists() else 0)
        rig._ab_loop(
            args, runner, [_unit(question_key=question_key)],
            {"A_nokg": _fact()}, _repos_by_arm(), actor_id="ab-owner",
            profile=None, concurrency=2,
            gold_by_key=load_ab_gold(load_questions()),
            model_contract="0123456789abcdef", log_dir=log_dir,
            event_dir=event_dir, clock=datetime.now,
        )
        capsys.readouterr()
        lines = path.read_text("utf-8").splitlines()[before:]
        return [json.loads(line) for line in lines]

    first = _run_batch("A-q08")
    assert len(first) == 2                                  # 1 题 × 2 臂
    second = _run_batch("A-q14")
    # 第二批只认自己那两次调用:上一批的事件不再被吃第二遍。
    assert len(second) == 2
    assert all(row["join"] == "joined" for row in second)
    assert {row["support_id"] for row in second} == {
        "mdl-A-q14-legacy", "mdl-A-q14-v2"}
    assert not ({row["support_id"] for row in first}
                & {row["support_id"] for row in second})


def test_the_two_v2_arms_write_to_different_call_tables_and_raw_dirs(tmp_path):
    """两条 v2 臂的产物路径必须分开,否则后跑的静默覆盖前一条。"""
    off = rig.arm_label("v2", "off")
    snapshot = rig.arm_label("v2", "prefix_snapshot")
    assert off != snapshot
    rig._write_call_rows(tmp_path, off, [{"support_id": "mdl-a"}])
    rig._write_call_rows(tmp_path, snapshot, [{"support_id": "mdl-b"}])
    assert (tmp_path / f"calls-{off}.jsonl").exists()
    assert (tmp_path / f"calls-{snapshot}.jsonl").exists()
    assert rig._ab_raw_path(tmp_path, off, _unit()) != rig._ab_raw_path(
        tmp_path, snapshot, _unit())


def test_the_raw_archive_splits_the_two_v2_arms_through_the_production_writer(
    tmp_path,
):
    """存档分目录这件事要**经生产调用点**钉住,不是只钉 `_ab_raw_path`。

    直接给 `_ab_raw_path` 传 label 的那条断言够不到 `_write_ab_raw` 里的接线:
    哪天有人在那里退回一维(`arm[0]`),两条 v2 臂会写进同一个
    `raw/v2/<key>_<cell>_<effort>_r<n>.json`,后跑的静默覆盖先跑的,存档只剩一
    半——而 `ab-runs.jsonl` 完全正常,没有任何一处会报错。
    """
    unit = _unit()
    for arm in (("v2", "off"), ("v2", "prefix_snapshot")):
        rig._write_ab_raw(tmp_path, arm, unit, _FakeResponse(), [], None)
    written = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in (tmp_path / "raw").rglob("*.json")
    )
    # 两份存档,路径逐字如下(`off` 那一格落回光秃秃的 policy 目录)。
    stem = "A-q08_A_nokg_standard_r1.json"
    assert written == [f"raw/v2-prefix_snapshot/{stem}", f"raw/v2/{stem}"]
    # 存档正文里的 `arm` 仍是**命令行写法**,人工抽样时与 `--arms` 逐字相同。
    payload = json.loads(
        (tmp_path / "raw" / "v2-prefix_snapshot" / stem).read_text("utf-8"))
    assert payload["arm"] == rig.format_arm("v2", "prefix_snapshot")


# ---------------------------------------------------------------------------
# `--arms` 在 CLI 上的两端(预检与 dry-run 枚举)
# ---------------------------------------------------------------------------


def test_the_preflight_refuses_arms_and_only_policy_together():
    """矛盾指令不猜:「只跑 v2」在二维 `--arms` 下有两种读法,数据集不一样。"""
    args = _ab_args(arms="v2:off,v2:prefix_snapshot", only_policy=["v2"])
    assert "不能同时给" in rig._ab_preflight(args, ["A_nokg"])


def test_the_preflight_rejects_an_illegal_arm_before_anything_runs():
    args = _ab_args(arms="legacy:prefix_snapshot")
    assert "非法组合" in rig._ab_preflight(args, ["A_nokg"])
    ok = _ab_args(arms="v2:off,v2:prefix_snapshot")
    assert rig._ab_preflight(ok, ["A_nokg"]) == ""


def test_the_preflight_refuses_a_filter_that_leaves_no_arm():
    """零臂的计划长得完全像一次正常的预演,连 dry-run 都看不出来。

    这一格**今天在 CLI 上到不了**:`--only-policy` 的 `choices` 就是 `POLICIES`,
    两个值都在 `AB_DEFAULT_ARMS` 里,过滤永远非空。守卫留着是防两个集合日后分叉
    (往 `POLICIES` 加了第三种协议、却没往 `AB_DEFAULT_ARMS` 加对应臂),所以这
    条用例只能手搓 Namespace 才走得到。
    """
    args = _ab_args(only_policy=["legacy"])
    assert rig._ab_preflight(args, ["A_nokg"]) == ""
    assert rig.ab_arms(args) == [("legacy", "off")]
    assert set(rig.POLICIES) <= {arm[0] for arm in rig.AB_DEFAULT_ARMS}
    # `--arms v2:prefix_snapshot --only-policy legacy` 已经被上一条互斥挡住;
    # 这里造的是「默认臂里没有这个 policy」那一种(CLI 上不可达,见 docstring)。
    empty = _ab_args(only_policy=["nonsense"])
    assert "一条臂都不剩" in rig._ab_preflight(empty, ["A_nokg"])


def test_the_preflight_refuses_a_three_armed_batch():
    """一次一对。三条臂的批次每一行看起来都正常,而配对差值表整批为空。

    每个配对单元落三行,`mark_paired` 的门槛是 `PAIR_ARM_COUNT`,于是 12 行
    `paired` 全 False——数据集、日志、投影一切正常,只是一个配对结论都出不来,
    而这批已经烧掉了几百次模型调用(codex 规格评审 P2-1 / 质量评审 P3-4)。
    """
    args = _ab_args(arms="legacy,v2:off,v2:prefix_snapshot")
    problem = rig._ab_preflight(args, ["A_nokg"])
    assert "一次只收一对臂" in problem
    assert "分两批跑" in problem
    # 一对照旧放行(一维与二维两种写法都是一对)。
    assert rig._ab_preflight(_ab_args(arms="legacy,v2"), ["A_nokg"]) == ""
    assert rig._ab_preflight(
        _ab_args(arms="v2:off,v2:prefix_snapshot"), ["A_nokg"]) == ""
    # 只跑一条臂仍然合法(`--only-policy` 的二维版:重跑被打废的一侧)。
    assert rig._ab_preflight(_ab_args(arms="v2:prefix_snapshot"),
                             ["A_nokg"]) == ""


def test_the_three_armed_batch_really_would_have_lost_every_pair():
    """上一条拦的不是一个假想:三条臂真的会让 `paired` 全线 False。"""
    arms = rig.parse_arms("legacy,v2:off,v2:prefix_snapshot")
    assert len(arms) > rig.PAIR_ARM_COUNT
    rows = [{"arm": arm[0], "optimization": arm[1], "question_key": "A-q08",
             "corpus_cell": "A_nokg", "effort": "standard", "repeat": 1}
            for arm in arms]
    mark_paired(rows)
    assert [row["paired"] for row in rows] == [False, False, False]


def test_an_empty_arms_string_is_refused_instead_of_running_the_default():
    """`--arms ""` 是**给了一个空写法**,不是「没给」。

    脚本里写 `--arms "$ARMS"` 而变量恰好为空时,静默退化会让整批(上百次调用)
    安静地跑成 legacy-vs-v2,而那不是要做的实验——数据看起来完美,只是答非所问。
    """
    args = _ab_args(arms="")
    problem = rig._ab_preflight(args, ["A_nokg"])
    assert "为空或有空段" in problem
    with pytest.raises(ArmSpecError):
        rig.ab_arms(args)
    # 真的**不给**时照旧是默认两臂(既有一维用法一个字节没变)。
    assert rig.ab_arms(_ab_args()) == list(rig.AB_DEFAULT_ARMS)
    assert rig.build_parser().parse_args([
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test", "ab",
    ]).arms is None


def test_dry_run_ab_enumerates_the_second_dimension_and_the_shared_ruler(capsys):
    """dry-run 说清这一批的臂、两臂共用的那把尺子、调用数与请求上界。"""
    assert rig.main([
        "--dry-run", "--limit", "1", "--repeats", "1",
        "--arms", "v2:off,v2:prefix_snapshot",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    printed = capsys.readouterr().out
    assert "v2:off, v2:prefix_snapshot" in printed
    assert "REASONING_REFLECT_MEASURE_CONTEXT=true" in printed
    assert "次逻辑调用" in printed and "请求上界 ≤" in printed
    # per-call 表与两串隔离日志的落点都在计划里说清楚。
    assert "calls-v2.jsonl" in printed
    assert "calls-v2-prefix_snapshot.jsonl" in printed
    assert "EVENT_LOG_DIR=" in printed


def test_dry_run_ab_enumerates_the_newly_implemented_prefix_delta_arm(capsys):
    """T-PD7:`--arms v2:off,v2:prefix_delta` 走的是同一条 dry-run 枚举路径。

    与上一条同款断言,证据换成新格——`ARMS` 放开一格不需要 dry-run 枚举那半改
    一行代码,这条钉住的正是那件事。
    """
    assert rig.main([
        "--dry-run", "--limit", "1", "--repeats", "1",
        "--arms", "v2:off,v2:prefix_delta",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    printed = capsys.readouterr().out
    assert "v2:off, v2:prefix_delta" in printed
    assert "REASONING_REFLECT_MEASURE_CONTEXT=true" in printed
    assert "calls-v2.jsonl" in printed
    assert "calls-v2-prefix_delta.jsonl" in printed


def test_dry_run_ab_enumerates_the_newly_opened_prefix_delta_lean_arm(capsys):
    """T-PL6:`--arms v2:prefix_delta,v2:prefix_delta_lean` 走同一条 dry-run 枚举
    路径。

    与前两条同款断言,证据换成最后一格(D↔L 那一对配对臂)——`ARMS` 放开一格
    不需要 dry-run 枚举那半改一行代码,这条钉住的正是那件事,顺带覆盖 D↔L 这
    一对合法组合的请求上界打印。

    上界数字逐字钉死(而不是只断子串存在):`--arms` 换成哪一对合法组合都不该
    改变这条估算——臂对里换成 lean 不该让上界打折。变异:把上界估算改成按臂
    身份打折(例如 lean 少算轮数)会让这条先红,而不是留到真跑时才发现规划
    的调用预算算少了一倍。
    """
    assert rig.main([
        "--dry-run", "--limit", "1", "--repeats", "1",
        "--arms", "v2:prefix_delta,v2:prefix_delta_lean",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    printed = capsys.readouterr().out
    assert "v2:prefix_delta, v2:prefix_delta_lean" in printed
    assert "REASONING_REFLECT_MEASURE_CONTEXT=true" in printed
    assert "intent 2 + plan 0 + reflect ≤ 96 + synthesis 8" in printed
    assert "≤ 106 次逻辑调用;请求上界 ≤ 212" in printed
    assert "calls-v2-prefix_delta.jsonl" in printed
    assert "calls-v2-prefix_delta_lean.jsonl" in printed


def test_default_dry_run_output_is_unchanged_by_the_new_arm(capsys):
    """T-PD7 的反面:不给 `--arms` 时,默认两臂 dry-run 的产物计数逐字不变。

    `ARMS` 从四格长到五格(T-PL6 加了最后一格 `v2:prefix_delta_lean`),
    `AB_DEFAULT_ARMS` 仍是既有的一维两臂(`legacy`、`v2:off`)——加一格合法组合
    不该改动任何没点名它的既有命令。
    """
    assert rig.main([
        "--dry-run", "--limit", "2", "--round", "1",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    out = capsys.readouterr().out
    assert "planned runs  16(8 个配对单元 × 2 臂" in out
    assert "prefix_delta" not in out


def test_the_one_dimensional_spelling_keeps_its_artifact_paths_byte_identical(
    tmp_path, capsys,
):
    """不给 `--arms` 的既有命令:**产物路径**一个字节没变。

    逐字不变的是路径,不是 dry-run 的输出字节:那一行的臂名现在打成
    `legacy:off, v2:off`,另外多打了测量、隔离日志与 per-call 表三行,而计划也
    没要求 dry-run 输出不变。会让既有数据接不上的是路径——`raw/<arm>/` 与
    `calls-<arm>.jsonl` 里的 `<arm>` 必须仍是光秃秃的 policy(`arm_label` 对
    `off` 那一格的约定),否则二维化之前跑的那批存档与新批对不上号。
    """
    for arm in rig.AB_DEFAULT_ARMS:
        assert rig.arm_label(*arm) == arm[0]
        assert rig._ab_raw_path(tmp_path, rig.arm_label(*arm), _unit()) == (
            tmp_path / "raw" / arm[0] / "A-q08_A_nokg_standard_r1.json"
        )
    rig._write_call_rows(tmp_path, rig.arm_label(*rig.AB_DEFAULT_ARMS[0]),
                         [{"support_id": "mdl-a"}])
    assert (tmp_path / "calls-legacy.jsonl").exists()
    assert rig.AB_DEFAULT_ARMS == (("legacy", "off"), ("v2", "off"))
    # 枚举本身也没变:不给 `--arms` 就是既有的一维两臂。
    assert rig.main([
        "--dry-run", "--limit", "1", "--repeats", "1",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    printed = capsys.readouterr().out
    assert "legacy:off, v2:off" in printed
    assert "calls-legacy.jsonl" in printed and "calls-v2.jsonl" in printed
    assert "prefix_snapshot" not in printed


# ---------------------------------------------------------------------------
# T-EX8:整批墙钟预算 + manifest 接进 `ab`
# ---------------------------------------------------------------------------


def test_a_deadline_reached_after_round_one_skips_round_two_entirely(
    tmp_path, monkeypatch, capsys,
):
    """回归:重复轮在最外层——第 1 轮跑完之后预算才到点,第 2 轮**整个不派发**
    (§13「不偷偷补跑到矩阵齐全」+ 既有「重复轮是最外层循环」性质的组合)。
    """
    calls: list[int] = []

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        calls.append(int(item["repeat"]))
        time.sleep(0.15)
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    units = [
        _unit(question_key="A-q08", repeat=1),
        _unit(question_key="A-q08", repeat=2),
    ]
    deadline = time.monotonic() + 0.1  # 第 1 轮的 0.15s 睡眠会跨过这条线
    failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        deadline=deadline,
    )
    capsys.readouterr()
    assert calls == [1]                 # 第 2 轮从未派发
    assert stopped_by_budget is True
    assert failed == 0
    rows = [
        json.loads(line)
        for line in (out_dir / "ab-runs.jsonl").read_text().splitlines()
    ]
    assert {row["repeat"] for row in rows} == {1}


def test_a_cancellation_without_a_deadline_still_escapes_and_aborts_the_batch(
    tmp_path, monkeypatch,
):
    """回归守卫:`deadline=None`(不给 `--max-wall-minutes`)时,`AskCancelled`
    必须像今天一样**继续往上抛**,不能被 T-EX8 新加的「按 deadline 转成
    cancelled 行」分支悄悄接住——那条分支的判据是 `deadline is not None and
    time.monotonic() >= deadline`,`deadline=None` 时恒假。这条覆盖的是既有
    `test_a_concurrent_batch_cancels_the_remaining_units_after_the_first_abort`
    没盖到的一格:那条用例的 fatal 异常来自 `assert_arm_matches_evidence`
    (`RuntimeError`),不是从 `_run_ab_arm` 内部的 `AskCancelled` 转换路径来的,
    换句话说它测不出「转换分支的判据被写掉」这件事。

    (变异验证,人工执行:把 `_run_ab_arm` 里的判据从
    `deadline is not None and time.monotonic() >= deadline` 改成 `True`
    ⇒ 这条用例从绿变红;改回后复绿。见任务报告。)
    """
    from app.services.cancellation import AskCancelled

    def _fake_run(repo, **kw):
        raise AskCancelled()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    with pytest.raises(AskCancelled):
        rig._run_ab_arm(
            _unit(), arm=("v2", "off"), fact=_fact(), repo=None,
            actor_id="ab-owner", contract=None, concurrency=1,
            scope_ids=None, gold=None, model_contract="0123456789abcdef",
            log_dir=out_dir, event_dir=out_dir, clock=datetime.now,
            out_dir=out_dir, database_url=args.database_url,
            # `deadline` 不给 ⇒ 默认 `None`,是这条用例要钉住的那一格。
        )


def test_max_wall_minutes_defaults_to_none(capsys):
    """不给 `--max-wall-minutes` ⇒ `args.max_wall_minutes is None`。

    这是「不给 ⇒ 今天的行为逐字相同」的第一道闸:下游 `_ab_loop` /
    `_ab_run_batch` / `_run_ab_arm` 的每一条 T-EX8 新分支都先判
    `deadline is not None`,而 `deadline` 只在这个值非 `None` 时才非 `None`。
    """
    args = _ab_args()
    assert args.max_wall_minutes is None


def test_a_batch_with_no_deadline_dispatches_every_unit_and_matches_today(
    tmp_path, monkeypatch, capsys,
):
    """验收 (a):不给 ⇒ 派发计数、行数、退出码同今天。

    两个单元 × 两条臂 = 4 次 `run_ab_once` 调用、4 行落盘、`failed=0`、
    `stopped_by_budget=False`——`deadline=None`(不显式传,取函数默认值)时
    T-EX8 的每一条新分支都不生效。
    """
    calls: list[str] = []

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        calls.append(f"{item['question_key']}:{arm}")
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    # `no_intent=True`(`_ab_preflight` 会拒它,但这里直调 `_ab_loop`,不经过
    # 那道闸)让 `_IntentCache` 直接短路成 `enabled=False`,不需要另外伪造一个
    # 会话意图契约就能验完派发计数与行数——与
    # `test_ab_loop_keeps_the_two_arms_together_even_when_units_run_concurrently`
    # 同一条捷径。
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False,
                    no_intent=True)
    units = [_unit(question_key="A-q08"), _unit(question_key="A-q14")]
    failed, stopped_by_budget, digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
    )
    capsys.readouterr()
    assert len(calls) == 4                 # 2 单元 × 2 臂
    assert failed == 0
    assert stopped_by_budget is False
    # `no_intent=True` ⇒ `_IntentCache(enabled=False)`,`.get()` 恒返回 `None`
    # 且从不写 `_rows`——`intent_contract_digest_by_question` 因此恒空,这是
    # 这条捷径本身的性质,不是 T-EX8 的判据(有意图缓存时的取值见
    # `test_the_manifest_keys_pass_the_shared_contract_and_hide_the_url`)。
    assert digests == {}
    rows = [
        json.loads(line)
        for line in (out_dir / "ab-runs.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 4
    assert all(row["status"] == "done" for row in rows)


def test_an_expired_deadline_dispatches_nothing_and_reports_the_budget_stop(
    tmp_path, monkeypatch, capsys,
):
    """验收 (b):已经过期的 deadline ⇒ 零派发、`stopped_by_budget=True`。

    `deadline` 传一个已经在过去的单调时刻——`_ab_loop` 的第一次(也是唯一一次)
    `_ab_run_batch` 调用在真正进入循环之前就该发现「已经到点」,一次
    `run_ab_once` 都不调,`ab-runs.jsonl` 一行都不落。
    """
    calls: list[str] = []

    def _fake_run(repo, **kw):
        calls.append(kw.get("item", {}).get("question_key", "?"))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False)
    units = [_unit(question_key="A-q08"), _unit(question_key="A-q14")]
    failed, stopped_by_budget, digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        deadline=time.monotonic() - 1.0,
    )
    capsys.readouterr()
    assert calls == []
    assert failed == 0
    assert stopped_by_budget is True
    assert digests == {}
    rows_path = out_dir / "ab-runs.jsonl"
    assert not rows_path.exists() or rows_path.read_text() == ""


def test_an_expired_deadline_also_stops_a_concurrent_batch_with_zero_dispatch(
    tmp_path, monkeypatch, capsys,
):
    """(b) 的并发变体:并发路在整批开跑前若已过点也是**零派发**,不靠
    `abort()`/Timer 唤醒——那条机制是给 mid-batch 到点准备的,已经过期的
    deadline 应该在提交任何 future 之前就被拦下。

    「零派发」这一条**不足以**区分两道判据(评审 P3-4):删掉批入口那道前置判
    之后,`max(0.0, deadline - now) == 0` 的 Timer 会抢在 worker 之前跑完
    `abort()`,而 `worker()` 入口也自己按墙钟早退——`calls` 照样是空的,一场
    竞态兜住了一条本该由前置判负责的性质。所以这里另钉两件只有前置判成立才
    为真的事实:**线程池压根没被构造**,以及终端上**没有** `ab aborted` 那一行
    (预算到点的收摊路与前置判是两回事,后者根本走不到收摊)。
    """
    calls: list[str] = []
    pools: list[int] = []
    real_pool = rig.ThreadPoolExecutor

    def _fake_run(repo, **kw):
        calls.append(kw.get("item", {}).get("question_key", "?"))
        return _FakeResponse()

    def _recording_pool(*a, **kw):
        pools.append(1)
        return real_pool(*a, **kw)

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "ThreadPoolExecutor", _recording_pool)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False)
    units = [_unit(question_key=f"A-q{i:02d}") for i in range(4)]
    failed, stopped_by_budget, digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=2,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        deadline=time.monotonic() - 1.0,
    )
    out = capsys.readouterr().out
    assert calls == []
    assert failed == 0
    assert stopped_by_budget is True
    assert pools == [], "批入口的前置判没成立:线程池被构造了"
    assert "ab aborted" not in out


def test_a_mid_batch_deadline_cancels_the_in_flight_units_and_skips_the_queue(
    tmp_path, monkeypatch, capsys,
):
    """验收 (c):中途到点 ⇒ 在途单元落 `cancelled` 行,队列单元不落行不发调用。

    6 个单元、`--concurrency 2`:恰好 2 个单元能立刻占住两个 worker 线程,各自
    卡在自己的 `cancel_event.wait()` 上;`--max-wall-minutes` 的 deadline 在
    200ms 后到点,唤醒这两个在途单元(`AskCancelled` → `status="cancelled"`
    行);其余 4 个单元此时还在线程池的内部队列里,`worker()` 入口的
    `aborted.is_set()` 检查让它们**从未**调用 `run_ab_once`。
    """
    from app.services.cancellation import AskCancelled

    total = 6
    concurrency = 2
    units = [_unit(question_key=f"Q{i:02d}") for i in range(total)]
    executed: list[str] = []
    lock = threading.Lock()

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        with lock:
            executed.append(item["question_key"])
        if cancel_event.wait(timeout=5):
            raise AskCancelled()
        raise TimeoutError("cancel_event 一直没被设置——budget timer 没生效")

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    started = time.monotonic()
    deadline = time.monotonic() + 0.2
    failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=concurrency,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        deadline=deadline,
    )
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out
    assert stopped_by_budget is True
    assert failed == 0                       # 删失,不是失败(§10 M5-4)
    assert elapsed < 3.0, f"budget timer 没有及时唤醒在途单元({elapsed:.1f}s)"
    in_flight = set(executed)
    assert len(in_flight) == concurrency, executed
    assert len(in_flight) < total
    rows = [
        json.loads(line)
        for line in (out_dir / "ab-runs.jsonl").read_text().splitlines()
    ]
    # 只跑 v2 一侧(`--only-policy v2`)⇒ 每个在途单元一行,不是两行。
    assert {row["question_key"] for row in rows} == in_flight
    assert all(row["status"] == "cancelled" for row in rows)
    assert len(rows) == concurrency
    # 事后翻**日志**的人也要看到 `cancelled`(评审 F9):这一行此前把 `status`
    # 硬编码成 `failed`,于是 JSONL 说删失、`.log` 说失败,读日志的人把预算掐断
    # 读成「这一批模型全崩了」。
    log_text = (out_dir / "ab-runs.log").read_text(encoding="utf-8")
    assert "status=cancelled" in log_text
    assert "status=failed" not in log_text
    # 人看的那条 say 也不叫 FAILED(评审 P2-4):同一屏上「N 行 FAILED」紧接
    # 一句「整批墙钟预算到点」是自相矛盾的。
    assert "ab cancelled" in out
    assert "ab failed" not in out
    assert "CANCELLED 预算到点" in out


def test_the_manifest_keys_pass_the_shared_contract_and_hide_the_url(tmp_path):
    """验收 (d) 之一:manifest 键集过 `assert_manifest`;隐私守卫(数据库连接串
    与题面原文都不进 manifest)。
    """
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    secret_url = "postgresql://user:hunter2@127.0.0.1:5432/nb_t0_test"
    args = _ab_args(
        only_policy=[], out_dir=str(out_dir), dry_run=False,
        database_url=secret_url,
    )
    units = [
        _unit(question_key="A-q08", effort="standard"),
        _unit(question_key="A-q14", effort="deep"),
    ]
    cells = ["A_nokg"]
    arms = rig.ab_arms(args)
    settings = SimpleNamespace(reasoning_timeout_seconds=90,
                               reasoning_max_retries=1)
    rig._write_ab_manifest(
        runner, args, units=units, cells=cells, arms=arms,
        facts={"A_nokg": _fact()}, model_contract="0123456789abcdef",
        settings=settings,
        intent_contract_digest_by_question={
            "A-q08": rig.ab_contract_digest({"a": 1}),
            "A-q14": rig.ab_contract_digest({"a": 2}),
        },
        started_at="2026-09-11T00:00:00", finished_at="2026-09-11T00:01:00",
        stopped_by_budget=False,
    )
    text = (out_dir / "manifest.json").read_text(encoding="utf-8")
    row = json.loads(text)
    # `build_manifest` 已经在 `_write_ab_manifest` 内部验过一遍;这里对**读回来
    # 的**行再跑一遍,钉住"写出去的就是过了闸的那一份"。
    assert_manifest(row)
    assert secret_url not in text
    assert "postgresql://" not in text
    assert _unit(question_key="A-q08")["question"] not in text
    assert row["channel"] == "e3"
    assert row["arm_order_seed"] is None
    assert row["common_baseline"] == "off"
    assert row["stopped_by_budget"] is False
    assert row["matrix"] == {
        "questions": 2, "cells": 1, "efforts": 2, "arms": 2, "repeats": 3,
        # 额外子键:真实 run 数上界(评审 P1-2)。基数相乘对不上账——E3 各格
        # 题集不相交,`cells` 不是乘数。
        "planned_runs": 4,
    }
    assert row["budgets"]["max_wall_minutes"] is None
    assert row["budgets"]["reasoning_timeout_seconds"] == 90
    # 重试预算复用那个镜像常量,不再第三次抄配置默认值(评审 P3-1);常量本身
    # 由 `test_reflect_t0_scripts.py` 钉在 `Settings().reasoning_max_retries` 上。
    assert row["budgets"]["reasoning_attempt_budget"] == rig.REASONING_ATTEMPT_BUDGET
    # **按值**断言,不只查键在场(评审 P2-2):`assert_manifest` 的四道闸对空
    # list / 空 dict 一律放行,所以「`facts[cell]["corpus_signature"]` 这一跳被
    # 重构断了」这种事全部四道闸都拦不住,而「这批跑在哪份语料上」正是这个键
    # 被列为 E3 必填的理由。
    assert row["arms"] == ["legacy:off", "v2:off"]
    assert row["optimization_by_arm"] == {"legacy:off": "off", "v2:off": "off"}
    assert row["corpus_signature_by_cell"] == {
        "A_nokg": _fact()["corpus_signature"],
    }
    assert set(row["intent_contract_digest_by_question"]) == {"A-q08", "A-q14"}
    assert row["model_contract"] == "0123456789abcdef"
    # 历史那一份也落了盘(评审 P3-7),内容与最新那份同一行。
    history = (out_dir / "manifests.jsonl").read_text().splitlines()
    assert len(history) == 1
    assert json.loads(history[0]) == row


def test_the_manifest_repeats_is_one_under_round_and_round_index_is_separate(
    tmp_path,
):
    """验收 (d) 之二:`--round` 下 `matrix.repeats` 恒为 1,轮号另记
    `matrix.round_index`,不塞进 `repeats`(拍板台账「T-EX1 quality 评审」)。
    """
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(
        only_policy=[], out_dir=str(out_dir), dry_run=False,
        round=2, repeats=3,
    )
    units = [_unit(question_key="A-q08")]
    rig._write_ab_manifest(
        runner, args, units=units, cells=["A_nokg"], arms=rig.ab_arms(args),
        facts={"A_nokg": _fact()}, model_contract=None,
        settings=SimpleNamespace(),
        intent_contract_digest_by_question={},
        started_at="t0", finished_at="t1", stopped_by_budget=False,
    )
    row = json.loads((out_dir / "manifest.json").read_text())
    assert row["matrix"]["repeats"] == 1
    assert row["matrix"]["round_index"] == 2


def _default_ab_units(**plan_overrides: Any) -> list[dict]:
    """真实 `ab_plan` 出来的默认矩阵(不手造 units)。

    手造的 units 能拼出 `ab` 里**出不来**的形状——例如同一道题落在两个语料格
    上:`ab_plan` 走 `ask_plan`,后者按 `row["corpus"] == corpus` 过滤,所以每
    道题只属于一个格(评审 P1-2 实测)。manifest 的对账用例必须跑在真实形状上,
    否则钉住的是一条生产上永远不成立的性质。
    """
    plan = {
        "cells": list(rig.AB_DEFAULT_CELLS), "efforts": rig.EFFORTS,
        "repeats": 3, "round_": None, "lang": "zh", "limit": None,
    }
    plan.update(plan_overrides)
    return rig.ab_plan(load_questions(), **plan)


def test_the_manifest_planned_runs_matches_the_real_default_matrix(tmp_path):
    """验收 (d) 之三:`matrix` 的账要**对得上真实 run 数**(评审 P1-2)。

    默认矩阵(两格、34 题、2 档、2 臂、3 轮)真实落 408 行,而五个基数相乘给
    816——差的正好是 `cells`:`ab` 各格的题集**不相交**,`cells` 不是乘数。
    读的人按「五个基数相乘」对账会看到 `ab-runs.jsonl` 只有一半、而
    `stopped_by_budget=false`,结论变成「这份数据集损坏/少跑了一半」,恰好是
    冻结 manifest 要消除的那种误读。所以另写一个 `planned_runs` 子键,并在这里
    把「相乘 == 实际上界」这件事钉死。

    `questions` 仍然是**真实题数**(不是 `(格,题)` 配对数)。这两个数在 `ab` 的
    任何真实调用里恰好相等(题集不相交),所以那条去重在生产上是空操作——真正
    需要守的账是下面这条乘法,不是一个造不出来的同题双格。
    """
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False,
                    repeats=3)
    units = _default_ab_units()
    arms = rig.ab_arms(args)
    cells = list(rig.AB_DEFAULT_CELLS)
    rig._write_ab_manifest(
        runner, args, units=units, cells=cells, arms=arms,
        facts={cell: _fact() for cell in cells}, model_contract=None,
        settings=SimpleNamespace(),
        intent_contract_digest_by_question={},
        started_at="t0", finished_at="t1", stopped_by_budget=False,
    )
    matrix = json.loads((out_dir / "manifest.json").read_text())["matrix"]
    assert matrix["questions"] == 34
    assert matrix["cells"] == 2
    assert matrix["efforts"] == 2
    assert matrix["arms"] == 2
    assert matrix["repeats"] == 3
    # 真实上界:34 题 × 2 档 × 2 臂 × 3 轮。
    assert matrix["planned_runs"] == 34 * 2 * 2 * 3
    assert matrix["planned_runs"] == len(units) * len(arms)
    # 而五个基数相乘多出一个 `cells` 倍——这条断言就是「`cells` 不是乘数」这句
    # 话本身,顺手挡住「哪天有人把 planned_runs 改成五键相乘」。
    assert (matrix["questions"] * matrix["cells"] * matrix["efforts"]
            * matrix["arms"] * matrix["repeats"]
            == matrix["planned_runs"] * matrix["cells"])


def test_the_manifest_only_records_the_cells_the_batch_really_ran(tmp_path):
    """验收 (d) 之四:`cells` 与 `corpus_signature_by_cell` 从 **units 实跑格**
    收窄,不数 `--cell` 的声明格数(评审 F4)。

    `--only-question A-q08` 在默认两格下出 6 个单元、全在 `A_nokg`。此前
    manifest 写 `cells: 2` 并带上一个这批压根没跑的 `B_kg` 签名——「冻结这批跑
    在哪份语料上」这个事实当场变成假的,而五基数相乘也跟着差一倍。
    """
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False,
                    only_question=["A-q08"], repeats=3)
    units = _default_ab_units(only_questions=["A-q08"])
    assert {unit["corpus_cell"] for unit in units} == {"A_nokg"}
    cells = list(rig.AB_DEFAULT_CELLS)
    b_fact = dict(_fact(), corpus_signature="00112233445566aa")
    rig._write_ab_manifest(
        runner, args, units=units, cells=cells, arms=rig.ab_arms(args),
        facts={"A_nokg": _fact(), "B_kg": b_fact}, model_contract=None,
        settings=SimpleNamespace(),
        intent_contract_digest_by_question={},
        started_at="t0", finished_at="t1", stopped_by_budget=False,
    )
    row = json.loads((out_dir / "manifest.json").read_text())
    assert row["matrix"]["cells"] == 1
    assert row["matrix"]["questions"] == 1
    assert row["matrix"]["planned_runs"] == len(units) * 2
    # 没跑的那个格的签名**不进** manifest。
    assert row["corpus_signature_by_cell"] == {
        "A_nokg": _fact()["corpus_signature"],
    }
    assert b_fact["corpus_signature"] not in (out_dir / "manifest.json").read_text()


def test_the_manifest_json_is_the_latest_and_the_jsonl_is_the_history(tmp_path):
    """评审 P3-7:`manifest.json` 覆盖写(最新),`manifests.jsonl` 追加(历史)。

    同一个 out-dir 里 `ab-runs.jsonl` 是 append 模式、默认 out-dir 又是固定的
    `.local/t0`:只覆盖写的话,在 SHA a1 跑一批、改代码到 b2 再跑一批之后,数据
    文件里两批的行都在、manifest 只剩 b2 ⇒ a1 那些行被归到 b2 的代码上,
    `code_sha` 这个锚点反过来说了假话。
    """
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False)
    for started in ("t0", "t2"):
        rig._write_ab_manifest(
            runner, args, units=[_unit()], cells=["A_nokg"],
            arms=rig.ab_arms(args), facts={"A_nokg": _fact()},
            model_contract=None, settings=SimpleNamespace(),
            intent_contract_digest_by_question={},
            started_at=started, finished_at="t9", stopped_by_budget=False,
        )
    history = [
        json.loads(line)
        for line in (out_dir / "manifests.jsonl").read_text().splitlines()
    ]
    assert [row["started_at"] for row in history] == ["t0", "t2"]
    latest = json.loads((out_dir / "manifest.json").read_text())
    assert latest["started_at"] == "t2"
    assert latest == history[-1]


def test_dry_run_ab_prints_the_budget_lines(capsys):
    """验收 (e):dry-run 三行文案——整批墙钟预算、单次超时、到点行为。"""
    assert rig.main([
        "--dry-run", "--limit", "1", "--round", "1",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    out = capsys.readouterr().out
    assert "wall-clock budget" in out
    assert "未设置(--max-wall-minutes 不给,今天的行为逐字相同" in out
    assert "single-call timeout" in out
    # **不硬写 90s**(评审 F3 / P3-1):硬写的话,配置默认值哪天调到 120,rig 与
    # 用例会**一致地**停在陈旧的 90 而全绿。这个数从 `Settings` 取——那也是
    # `REASONING_TIMEOUT_SECONDS_DEFAULT` 这个镜像常量被钉住的那一个值(守卫在
    # `test_reflect_t0_scripts.py`)。
    from app.core.config import Settings

    assert f"{Settings().reasoning_timeout_seconds}s" in out
    assert "on budget" in out
    assert "停止派发新单元" in out
    assert "status=cancelled" in out
    assert "不偷偷补跑到矩阵齐全" in out


def test_dry_run_ab_prints_the_given_max_wall_minutes(capsys):
    """给了 `--max-wall-minutes` 时,dry-run 打的是那个值,不是「未设置」。"""
    assert rig.main([
        "--dry-run", "--limit", "1", "--round", "1",
        "--max-wall-minutes", "45",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    out = capsys.readouterr().out
    assert "--max-wall-minutes 45.0" in out
    assert "未设置" not in out


def test_git_sha_falls_back_to_unknown_without_raising(monkeypatch):
    """验收 (f):`git rev-parse` 失败(非 git checkout、`git` 缺失、超时……)
    ⇒ `code_sha="unknown"`,不抛。
    """
    def _boom(*a, **kw):
        raise FileNotFoundError("git 不在 PATH 上")

    monkeypatch.setattr(rig.subprocess, "run", _boom)
    assert rig._ab_git_sha() == "unknown"


def test_git_sha_falls_back_to_unknown_on_a_nonzero_exit(monkeypatch):
    """`git rev-parse` 存在但以非零码退出(例如浅克隆缺 `.git`)同样落
    `"unknown"`——`check=True` 让 `CalledProcessError` 走同一条 `except`。
    """
    def _nonzero(*a, **kw):
        raise rig.subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])

    monkeypatch.setattr(rig.subprocess, "run", _nonzero)
    assert rig._ab_git_sha() == "unknown"


def _fake_git(monkeypatch, *, head="", status="", head_exc=None,
              status_exc=None):
    """按 argv 分派的假 `git`:`rev-parse` 与 `status --porcelain` 各一条出口。"""
    def _run(cmd, **kw):
        if cmd[1] == "rev-parse":
            if head_exc is not None:
                raise head_exc
            return SimpleNamespace(stdout=head, returncode=0)
        assert cmd[1:] == ["status", "--porcelain"], cmd
        if status_exc is not None:
            raise status_exc
        return SimpleNamespace(stdout=status, returncode=0)

    monkeypatch.setattr(rig.subprocess, "run", _run)


_FULL_SHA = "6c2a6155f" + "0" * 31        # 40 位,形状与真实 HEAD 相同


def test_git_sha_on_a_clean_tree_is_the_bare_full_sha(monkeypatch):
    """干净工作树 ⇒ 40 位全 SHA,不带后缀(docstring 说的是「全 SHA」,不是短码
    ——评审 P3-5 ①)。
    """
    _fake_git(monkeypatch, head=_FULL_SHA + "\n", status="")
    assert rig._ab_git_sha() == _FULL_SHA


def test_git_sha_marks_a_dirty_worktree(monkeypatch):
    """有未提交改动 ⇒ 拼 `-dirty`(评审 P3-5 ②)。

    rig 被 worktree 里的未提交改动驱动是这个程序的常态;一个光秃秃的 commit SHA
    会把这批数据锚到一份**不含实际跑的代码**的提交上,而 `code_sha` 的全部意义
    就是那条锚。
    """
    _fake_git(monkeypatch, head=_FULL_SHA + "\n",
              status=" M scripts/reflect_shadow_rig.py\n")
    assert rig._ab_git_sha() == _FULL_SHA + "-dirty"


def test_git_sha_does_not_claim_clean_when_status_itself_fails(monkeypatch):
    """`git status` 自己读不出来时**不默认干净**:拼 `-dirty_unknown`。

    静默当干净会让 manifest 说一句它没验过的话——与 §5.5-2「未验证 ≠ 通过」
    同一条纪律。
    """
    _fake_git(monkeypatch, head=_FULL_SHA + "\n",
              status_exc=rig.subprocess.TimeoutExpired(["git", "status"], 5))
    assert rig._ab_git_sha() == _FULL_SHA + "-dirty_unknown"


def test_git_sha_is_unknown_when_rev_parse_times_out(monkeypatch):
    """`timeout=5` 那一条守卫(评审 P3-5 ③):超时 ⇒ `unknown`,不抛、不挂死。

    收尾时的一次诊断性 `git` 调用不该让一次跑了几小时的批报废——也不该让它
    卡住不返回。
    """
    _fake_git(
        monkeypatch,
        head_exc=rig.subprocess.TimeoutExpired(["git", "rev-parse"], 5),
    )
    assert rig._ab_git_sha() == "unknown"


def test_git_sha_is_unknown_on_empty_output(monkeypatch):
    """退出码 0 但 stdout 是空的(见过的一种浅克隆形态)⇒ `unknown`,不落一个
    空字符串 `code_sha`(评审 P3-5 ③)。
    """
    _fake_git(monkeypatch, head="\n")
    assert rig._ab_git_sha() == "unknown"


def test_the_manifest_code_sha_stays_a_short_code_even_when_dirty(tmp_path,
                                                                 monkeypatch):
    """`-dirty` 后缀不能把 `code_sha` 顶出 `_is_short_code` 的字符集/长度门槛。

    40 位 SHA + 后缀仍在 64 字符内,`-`/`_` 都在短码字符集里——这一格让那件事
    由 `assert_manifest` 真判一次,而不是靠人算。
    """
    _fake_git(monkeypatch, head=_FULL_SHA + "\n", status=" M a\n")
    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=[], out_dir=str(out_dir), dry_run=False)
    rig._write_ab_manifest(
        runner, args, units=[_unit()], cells=["A_nokg"],
        arms=rig.ab_arms(args), facts={"A_nokg": _fact()},
        model_contract=None, settings=SimpleNamespace(),
        intent_contract_digest_by_question={},
        started_at="t0", finished_at="t1", stopped_by_budget=False,
    )
    row = json.loads((out_dir / "manifest.json").read_text())
    assert row["code_sha"].endswith("-dirty")
    assert_manifest(row)


# --- 串行同轮中途到点 / 端到端退出码(评审 F1 / F2 / P1-1) -------------------


def test_a_mid_round_deadline_stops_dispatching_the_rest_of_a_serial_batch(
    tmp_path, monkeypatch, capsys,
):
    """评审 F1 / P1-1 / P3-4:**同一轮里**串行路中途到点 ⇒ 停止派发。

    此前唯一涉及串行到点的用例把两个单元放在**不同重复轮**,第 2 轮被
    `_ab_run_batch` 的批入口检查兜住,走不到 `for unit in batch` 循环顶部那道
    判——删掉那五行(变异 M1)之后三份用例文件全绿。而 `--concurrency 1` 是
    **默认值**(SQLite 下还是强制值),失败场景就是「一轮 24 个单元、预算在第 5
    个之后到点,剩下 19 个照跑到底、整批远超预算」。

    到点这件事**不靠竞态**:第一个单元的假 `run_ab_once` 一直等到
    `time.monotonic() >= deadline` 才交卷,所以循环顶那道判必然看见到点,不依赖
    sleep 的时长猜测。
    """
    total = 3
    units = [_unit(question_key=f"Q{i:02d}", repeat=1) for i in range(total)]
    calls: list[str] = []
    deadline = time.monotonic() + 0.05

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        calls.append(item["question_key"])
        while time.monotonic() < deadline:
            time.sleep(0.005)
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        deadline=deadline,
    )
    capsys.readouterr()
    # 派发数 < 单元数:第一个单元跑完就跨过了预算线。
    assert calls == ["Q00"]
    assert len(calls) < total
    assert stopped_by_budget is True
    assert failed == 0
    rows = [
        json.loads(line)
        for line in (out_dir / "ab-runs.jsonl").read_text().splitlines()
    ]
    # 已跑的那个单元有行(`status=done`,它自己跑完了,不是删失);没派发的两个
    # **一行都没有**——不落行、不发调用。
    assert [row["question_key"] for row in rows] == ["Q00"]
    assert rows[0]["status"] == "done"


def test_a_budget_stopped_batch_exits_non_zero_and_freezes_it_in_the_manifest(
    tmp_path, monkeypatch, capsys,
):
    """验收 (b) 的后两半(评审 F2 / P1-1):预算到点 ⇒ **退出码 1** +
    manifest 的 `stopped_by_budget=true`。

    此前两条 (b) 用例都直调 `_ab_loop`,压根不经过 `_run_ab`,于是退出码与
    manifest 都没被看过:变异「删掉 `if stopped_by_budget: return 1`」全绿,而
    `stopped_by_budget=True` 的 manifest 从来没有被端到端产出过一次。失败场景是
    §13「不偷偷补跑」的另一半——一次被预算掐掉一半矩阵的批以退出码 0 收尾,
    `&&` 串起来的后续 `analyze` 照常出一份「结论」。
    """
    _ab_run_harness(
        monkeypatch, tmp_path,
        counts={"ask_jobs": 3, "answers": 1, "conversations": 1},
        loop=lambda *a, **kw: (0, True, {}),
    )
    args = _ab_args(dry_run=False, out_dir=str(tmp_path), max_wall_minutes=30.0)
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    assert rig._run_ab(args, runner, [_unit()], ["A_nokg"]) == 1
    err = capsys.readouterr().err
    assert "整批墙钟预算到点" in err
    assert "--max-wall-minutes=30.0" in err
    row = json.loads((tmp_path / "manifest.json").read_text())
    assert row["stopped_by_budget"] is True
    assert row["budgets"]["max_wall_minutes"] == 30.0
    assert row["budgets"]["batch_deadline_seconds"] == 1800


def test_the_budget_exit_message_still_reports_the_failed_runs(
    tmp_path, monkeypatch, capsys,
):
    """评审 P3-8:预算那条 `return` 在 `if failed:` **之前**,所以两件事同时成立
    时只打一句——那一句得把 `failed` 数带上。

    不带的话「预算到点」会盖掉「这一批里还有 2 个 run 真的崩了」:失败数不进
    manifest 闭集(没有这个键),stderr 是它唯一的落点。
    """
    _ab_run_harness(
        monkeypatch, tmp_path,
        counts={"ask_jobs": 3, "answers": 1, "conversations": 1},
        loop=lambda *a, **kw: (2, True, {}),
    )
    args = _ab_args(dry_run=False, out_dir=str(tmp_path), max_wall_minutes=5.0)
    runner = rig.Runner(dry_run=False, out_dir=tmp_path)
    assert rig._run_ab(args, runner, [_unit()], ["A_nokg"]) == 1
    err = capsys.readouterr().err
    assert "整批墙钟预算到点" in err
    assert "2 个 run FAILED" in err


def test_the_intent_digest_table_only_covers_the_questions_this_batch_ran(
    tmp_path, monkeypatch, capsys,
):
    """评审 P2-1:`intent_contract_digest_by_question` 只写**这一批实跑的题**。

    `_IntentCache.__init__` 会把 out-dir 上已有的 `intents.jsonl` 全部读进
    `_rows`(那个文件存在的理由就是断点重跑不重付模型钱),而默认 out-dir 是
    固定的 `.local/t0`。不求交的话「先跑一次全量、再跑 `--limit 1` 复现某题」
    会让 manifest 声称一道题的批用了两道题的契约,`matrix.questions=1` 与这张表
    当场自相矛盾。
    """
    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)

    out_dir = tmp_path / "ab"
    out_dir.mkdir()
    # 上一次跑批留下的缓存:两道题。这一批只跑其中一道。**不** monkeypatch
    # `_IntentCache.get`——要验的正是那份从磁盘读回来的 `_rows`。
    (out_dir / "intents.jsonl").write_text(
        "".join(
            json.dumps({"question_key": key, "contract": {"resolved": key}},
                       ensure_ascii=False) + "\n"
            for key in ("A-q08", "A-q14")
        ),
        encoding="utf-8",
    )
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    _failed, _stopped, digests = rig._ab_loop(
        args, runner, [_unit(question_key="A-q08")], {"A_nokg": _fact()},
        _repos_by_arm(), actor_id="ab-owner", profile=None, concurrency=1,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
    )
    capsys.readouterr()
    assert set(digests) == {"A-q08"}
    assert digests["A-q08"] == rig.ab_contract_digest({"resolved": "A-q08"})


def test_the_budget_timer_is_cancelled_before_the_batch_returns(
    tmp_path, monkeypatch, capsys,
):
    """评审 F6:`_ab_run_batch` 返回前必须 `cancel()` 掉那一轮的 Timer。

    不 cancel 的失败场景(`--repeats 3 --max-wall-minutes 90 --concurrency 4`):
    第 1、2 轮的陈旧 Timer 在第 3 轮里到点触发,`abort()` 关的是**第 1/2 轮
    闭包的** `aborted`/`active_events`(早已空),于是第 3 轮在途单元不被唤醒、
    也不停止派发,而 `state["stopped_by_budget"]` 已被翻真——另有一个窄窗口会让
    一次跑满的批被误报成预算掐断、退 1。
    """
    timers: list[Any] = []
    real_timer = threading.Timer

    class _RecordingTimer(real_timer):          # type: ignore[misc, valid-type]
        def __init__(self, interval, function, *a, **kw):
            super().__init__(interval, function, *a, **kw)
            self.cancel_calls = 0
            timers.append(self)

        def cancel(self):
            self.cancel_calls += 1
            super().cancel()

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig.threading, "Timer", _RecordingTimer)
    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    units = [_unit(question_key=f"Q{i:02d}", repeat=1) for i in range(2)]
    _failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=2,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        # 预算宽到跑不完:这一批**不该**因为预算收尾,Timer 该被原样撤掉。
        deadline=time.monotonic() + 600.0,
    )
    capsys.readouterr()
    assert stopped_by_budget is False
    assert len(timers) == 1, "每轮一个 Timer"
    assert timers[0].cancel_calls == 1
    assert timers[0].finished.is_set()


class _StubTimer:
    """建了不跑的 `threading.Timer` 替身,把回调交给用例自己在精确时刻调。

    两条并发判据(worker 入口的墙钟判、登记处的同锁复核)防的都是「Timer 还没
    跑到 / 恰好夹在两步之间」这种窗口——真跑里那是一场竞态,靠 sleep 撞不出确定
    性。把 Timer 摘掉、由用例决定回调什么时候发生,那个窗口就变成一条确定的
    时序(评审 F5 / P2-3 的两条修正各自因此有了单独会红的用例)。
    """

    instances: list[Any] = []

    def __init__(self, interval, function, *a, **kw) -> None:
        self.interval = interval
        self.function = function
        self.daemon = False
        self.cancel_calls = 0
        _StubTimer.instances.append(self)

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancel_calls += 1


@pytest.fixture
def stub_timer(monkeypatch):
    _StubTimer.instances = []
    monkeypatch.setattr(rig.threading, "Timer", _StubTimer)
    return _StubTimer


def _fake_run_returning_ok(executed: list[str], lock: threading.Lock):
    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        with lock:
            executed.append(item["question_key"])
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    return _fake_run


def test_a_worker_that_wakes_up_after_the_deadline_never_dispatches(
    tmp_path, monkeypatch, capsys, stub_timer,
):
    """评审 F5 之一:`worker()` 入口按**墙钟**自己判一次,不只判 `aborted`。

    Timer 线程被 GIL/OS 延迟时 `aborted` 在到点后短暂仍为假,只判它的 worker 会
    在那个窗口里又从队列取一个新单元、发出一次完整 Ask(默认最坏
    `reasoning_timeout_seconds × attempt_budget` ≈ 3 分钟),`--max-wall-minutes`
    因此被越过而那一行落的是 `status=done`。这里把 Timer 换成「建了不跑」的替身
    (回调永不触发 ⇒ `aborted` 永远为假),预算线由假 `run_ab_once` 自己跨过:
    前两个单元跑完时已经到点,后面的单元只能靠入口那道墙钟判拦住。
    """
    total = 4
    concurrency = 2
    units = [_unit(question_key=f"Q{i:02d}", repeat=1) for i in range(total)]
    executed: list[str] = []
    lock = threading.Lock()
    deadline = time.monotonic() + 0.05

    def _fake_run(repo, *, notebook, item, arm, contract, on_trace,
                  cancel_event, actor_id, scope_source_ids=None,
                  optimization="off"):
        with lock:
            executed.append(item["question_key"])
        while time.monotonic() < deadline:
            time.sleep(0.005)
        for step in _steps(arm):
            on_trace(SimpleNamespace(
                step_type=step["step_type"], summary="",
                detail=step["detail"], duration_ms=1,
            ))
        return _FakeResponse()

    monkeypatch.setattr(rig, "run_ab_once", _fake_run)
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    _failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=concurrency,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        deadline=deadline,
    )
    out = capsys.readouterr().out
    # Timer 的回调一次都没跑过 ⇒ `aborted` 恒假,能拦住剩下两个单元的只有入口
    # 那道墙钟判。
    assert stub_timer.instances and "ab aborted" not in out
    assert len(executed) == concurrency, executed
    assert len(executed) < total
    # 到点这件事仍然被如实记下来了(那半只有 worker 早退那条路能写)。
    assert stopped_by_budget is True


def test_a_worker_that_loses_the_abort_race_is_caught_by_the_lock_recheck(
    tmp_path, monkeypatch, capsys, stub_timer,
):
    """评审 F5 之二 / P2-3:`aborted` 检查与 `cancel_event` 登记在同一把锁下复核。

    与 codex #700 R21 P2 在 T0 rig(`_search_loop_concurrent`)上修过的是同一型
    缺陷。窗口是「过了入口 `aborted` 检查」与「`active_events.append`」之间:
    `abort()` 恰好在这里快照,这个单元的 `cancel_event` 就永远不会被设,它带着
    一个不会被唤醒的取消事件发出一次完整 Ask,而 `shutdown(wait=True)` 只能等它
    跑完——`--max-wall-minutes` 被越过,那一行还落 `status=done`。

    确定性做法:窗口里只有两步——`threading.Event()` 与取锁,所以把
    `threading.Event` 换成一个替身,在**第一个由 worker 线程构造的** Event
    (那就是 `cancel_event`,`_run_ab_arm` 那一处被 `cancel_event or …` 短路)
    上先替 Timer 发一次回调,窗口因此变成一条固定时序。

    「由 worker 线程构造」这个判据不能换成「第 N 个 Event」:`threading.Thread.
    __init__` 自己就建一个 `Event`(`self._started`),线程池扩容时由**提交
    线程**建——按序号数会把回调发在任何 worker 起跑之前,那时两个 worker 都被
    入口的 `aborted` 检查拦住,复核那道判压根不参与,变异照样全绿(实测过)。
    """
    units = [_unit(question_key=f"Q{i:02d}", repeat=1) for i in range(2)]
    executed: list[str] = []
    lock = threading.Lock()
    real_event = threading.Event
    caller = threading.current_thread()
    fired: list[int] = []

    def _event_factory():
        event = real_event()
        if (threading.current_thread() is not caller
                and not fired and stub_timer.instances):
            fired.append(1)
            stub_timer.instances[0].function()
        return event

    monkeypatch.setattr(rig.threading, "Event", _event_factory)
    monkeypatch.setattr(rig, "run_ab_once",
                        _fake_run_returning_ok(executed, lock))
    monkeypatch.setattr(rig, "_ab_element_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_chunk_sections", lambda url, ids: {})
    monkeypatch.setattr(rig, "_ab_usage_for_window",
                        lambda *a, **kw: UNKNOWN_USAGE)
    _fixed_intents(monkeypatch)

    out_dir = tmp_path / "ab"
    runner = rig.Runner(dry_run=False, out_dir=out_dir)
    args = _ab_args(only_policy=["v2"], out_dir=str(out_dir), dry_run=False)
    _failed, stopped_by_budget, _digests = rig._ab_loop(
        args, runner, units, {"A_nokg": _fact()}, _repos_by_arm(),
        actor_id="ab-owner", profile=None, concurrency=2,
        gold_by_key=load_ab_gold(load_questions()),
        model_contract="0123456789abcdef", log_dir=out_dir, clock=datetime.now,
        # 远期 deadline:入口那道墙钟判在这条用例里恒假,要验的只有同锁复核。
        deadline=time.monotonic() + 600.0,
    )
    capsys.readouterr()
    assert fired == [1], "预算回调没在 worker 建 cancel_event 的那一刻发出"
    assert stopped_by_budget is True
    # 输掉这场竞态的 worker **一个模型调用都不发**:没有它,那个单元会带着一个
    # 永不被设的 cancel_event 跑完一次完整 Ask。
    assert executed == []
    rows_path = out_dir / "ab-runs.jsonl"
    assert not rows_path.exists() or rows_path.read_text() == ""


# --- `--max-wall-minutes` 的 preflight(评审 F10 / P3-2)----------------------


@pytest.mark.parametrize("value", ["0", "-5", "nan"])
def test_the_preflight_refuses_a_non_positive_or_nan_wall_budget(value, capsys):
    """`_ab_preflight` 的地盘:「一份『零个 run』的计划看起来完全像一次正常的
    预演」。`--max-wall-minutes` 的两种坏值各自的后果:

    * `0` / `-5` ⇒ deadline 一开始就在过去:走完全部 preflight(含主库快照、
      gold 解析)之后零派发、写一份 `stopped_by_budget=true` 的 manifest、退 1
      ——读的人分不清是参数打错还是预算真用完;
    * `nan` ⇒ 所有 `>= deadline` 比较**恒假**,预算永不生效(既不早停也不报
      错),而 `budgets` 里落一个 `NaN` 让 `manifest.json` 不再是合法 JSON。

    与 `--round` 越界同一条纪律:不 clamp、响亮报错(退 2),`--dry-run` 也拦。
    """
    assert rig.main([
        "--dry-run", "--limit", "1", "--round", "1",
        "--max-wall-minutes", value,
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 2
    err = capsys.readouterr().err
    assert "--max-wall-minutes 必须是一个正数分钟数" in err


def test_the_preflight_accepts_a_positive_wall_budget(capsys):
    """正数照过(上面那道闸不能顺手把正常用法也拦掉)。"""
    assert rig.main([
        "--dry-run", "--limit", "1", "--round", "1",
        "--max-wall-minutes", "45",
        "--database-url", "postgresql://127.0.0.1:5432/nb_t0_test",
        "--source-db-url", "postgresql://127.0.0.1:5432/nb_main",
        "ab",
    ]) == 0
    capsys.readouterr()
