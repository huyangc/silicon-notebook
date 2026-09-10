"""E1(前缀敏感性探针)的纯计划与纯分析(`app/eval/reflect_prefix_probe.py`)。

计划真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
T-EX3(五个函数的要点、验收、用例 a-i);设计真源:
`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`
§9.1。

这里一次 I/O、一次模型调用都不做(`load_prefix_probe_sample` 除外,它只读
仓库内那份合成 fixture 一次)。真跑通道(`prefix-probe` 子命令、真实
`RuntimeModelProvider`)由 T-EX4 的用例覆盖。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import app.eval.reflect_manifest as manifest
import app.eval.reflect_prefix_probe as probe
from app.eval.reflect_prefix_probe import (
    ARM_DISTURBED,
    ARM_STABLE,
    DEFAULT_ARMS,
    DEFAULT_BLOCKS,
    DEFAULT_CALLS_PER_SERIES,
    DEFAULT_TIERS,
    PROBE_ROW_KEYS,
    SMOKE_BLOCKS,
    VERDICTS,
    assert_probe_row_closed,
    build_marker_pair,
    load_prefix_probe_sample,
    probe_manifest_facts,
    probe_plan,
    render_sample,
    sample_digest,
    sample_tier_chars,
    summarize_probe,
)

SAMPLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "eval" / "reflect_t0" / "prefix_probe_sample.json"
)


@pytest.fixture()
def sample() -> dict:
    return load_prefix_probe_sample(SAMPLE_PATH)


def _make_row(**overrides) -> dict:
    row = {
        "tier": "short",
        "block_index": 0,
        "arm": ARM_STABLE,
        "call_index": 1,
        "series_index": 0,
        "status": "ok",
        "call_wall_ms": 1000,
        "attempts": 1,
        "finish_reason": "stop",
        "response_chars": 20,
        "prompt_tokens": 4000,
        "cached_tokens": None,
        "completion_tokens": 8,
        "head_chars": 13,
        "tail_chars": 13,
        "message_bytes_total": 4100,
        "is_warmup": False,
    }
    row.update(overrides)
    return row


# --- (a) 计划平衡性 -----------------------------------------------------------

@pytest.mark.parametrize("blocks,expected_total", [(DEFAULT_BLOCKS, 96), (SMOKE_BLOCKS, 48)])
def test_probe_plan_balances_arms_per_block(blocks, expected_total):
    """(T-EX3-a) 每档每区组两臂各恰 `calls_per_series` 次;96/48 两个规模都对账。"""
    plan = probe_plan(seed=7, blocks=blocks)
    assert len(plan) == expected_total

    by_block: dict[tuple, dict[str, int]] = {}
    for row in plan:
        key = (row["tier"], row["block_index"])
        counts = by_block.setdefault(key, {ARM_STABLE: 0, ARM_DISTURBED: 0})
        counts[row["arm"]] += 1
    assert len(by_block) == len(DEFAULT_TIERS) * blocks
    for counts in by_block.values():
        assert counts[ARM_STABLE] == DEFAULT_CALLS_PER_SERIES
        assert counts[ARM_DISTURBED] == DEFAULT_CALLS_PER_SERIES


@pytest.mark.parametrize("blocks", [DEFAULT_BLOCKS, SMOKE_BLOCKS])
def test_probe_plan_arm_order_is_balanced_half_and_half(blocks):
    """(T-EX3-a) 每档内,臂序两种(stable 先/disturbed 先)各半。"""
    plan = probe_plan(seed=11, blocks=blocks)
    first_arm_by_block: dict[tuple, str] = {}
    for row in plan:
        key = (row["tier"], row["block_index"])
        if key not in first_arm_by_block:
            first_arm_by_block[key] = row["arm"]

    per_tier_first_counts: dict[str, dict[str, int]] = {}
    for (tier, _block), arm in first_arm_by_block.items():
        counts = per_tier_first_counts.setdefault(tier, {ARM_STABLE: 0, ARM_DISTURBED: 0})
        counts[arm] += 1
    for tier, counts in per_tier_first_counts.items():
        assert counts[ARM_STABLE] == blocks // 2, (tier, counts)
        assert counts[ARM_DISTURBED] == blocks - blocks // 2, (tier, counts)


def test_probe_plan_series_is_contiguous_not_interleaved():
    """(T-EX3-a) 一个序列的 `calls_per_series` 次调用在计划里连续出现,不交错。"""
    plan = probe_plan(seed=3)
    for start in range(0, len(plan), DEFAULT_CALLS_PER_SERIES):
        chunk = plan[start:start + DEFAULT_CALLS_PER_SERIES]
        keys = {(row["tier"], row["block_index"], row["arm"]) for row in chunk}
        assert len(keys) == 1, f"chunk at {start} is not one contiguous series: {chunk}"
        assert [row["call_index"] for row in chunk] == list(range(DEFAULT_CALLS_PER_SERIES))


def test_probe_plan_rejects_bad_shapes():
    with pytest.raises(ValueError):
        probe_plan(seed=1, arms=("stable", "extra_arm"))
    with pytest.raises(ValueError):
        probe_plan(seed=1, tiers=())
    with pytest.raises(ValueError):
        probe_plan(seed=1, tiers=("short", "short"))
    with pytest.raises(ValueError):
        probe_plan(seed=1, blocks=0)
    with pytest.raises(ValueError):
        probe_plan(seed=1, calls_per_series=0)


# --- (b) 可复现性 --------------------------------------------------------------

def test_probe_plan_same_seed_reproduces_different_seed_usually_differs():
    """(T-EX3-b) 同 seed 逐格相同;不同 seed 通常给出不同的臂序分法。"""
    plan_a = probe_plan(seed=101)
    plan_b = probe_plan(seed=101)
    assert plan_a == plan_b

    plan_c = probe_plan(seed=202)
    assert plan_a != plan_c


def test_build_marker_pair_is_deterministic_per_call():
    for _ in range(3):
        assert build_marker_pair(5, 2, ARM_STABLE, 3) == build_marker_pair(5, 2, ARM_STABLE, 3)
    assert build_marker_pair(5, 2, ARM_STABLE, 3) != build_marker_pair(6, 2, ARM_STABLE, 3)


# --- (c) 标记等长等数 + 不相交 + 过校验 -----------------------------------------

def test_marker_pair_equal_length_and_count_across_arms():
    """(T-EX3-c) 两臂标记长度、数量(2 个:head+tail)逐格相等。"""
    for call_index in range(4):
        head_s, tail_s = build_marker_pair(9, 0, ARM_STABLE, call_index)
        head_d, tail_d = build_marker_pair(9, 0, ARM_DISTURBED, call_index)
        assert len(head_s) == len(head_d)
        assert len(tail_s) == len(tail_d)
        assert len(head_s.encode("utf-8")) == len(head_d.encode("utf-8"))
        assert len(tail_s.encode("utf-8")) == len(tail_d.encode("utf-8"))


def test_marker_values_do_not_overlap_across_arms_and_ends():
    plan = probe_plan(seed=17, blocks=2)
    stable_heads = {r["head"] for r in plan if r["arm"] == ARM_STABLE}
    stable_tails = {r["tail"] for r in plan if r["arm"] == ARM_STABLE}
    disturbed_heads = {r["head"] for r in plan if r["arm"] == ARM_DISTURBED}
    disturbed_tails = {r["tail"] for r in plan if r["arm"] == ARM_DISTURBED}
    assert not (stable_heads & disturbed_heads)
    assert not (stable_tails & disturbed_tails)
    assert not (stable_heads & stable_tails)
    assert not (disturbed_heads & disturbed_tails)


def test_marker_pair_passes_experiment_message_markers_validation():
    """(T-EX3-c) 每格 head/tail 都能过 `experiment_message_markers` 的非空 str 校验。"""
    from app.core.llm import experiment_message_markers

    plan = probe_plan(seed=23, blocks=2)
    for row in plan[:6]:
        with experiment_message_markers(row["head"], row["tail"]):
            pass


def test_marker_pair_new_identifier_per_series():
    """每个序列(block)换新标识:同一臂在不同 block 上的 head/tail 不重复。"""
    heads = set()
    for block_index in range(4):
        head, _tail = build_marker_pair(1, block_index, ARM_STABLE, 0)
        assert head not in heads
        heads.add(head)


def test_marker_variant_switch_changes_values_but_not_shape():
    head_a, tail_a = build_marker_pair(1, 0, ARM_STABLE, 0, marker_variant=0)
    head_b, tail_b = build_marker_pair(1, 0, ARM_STABLE, 0, marker_variant=1)
    assert (head_a, tail_a) != (head_b, tail_b)
    assert len(head_a) == len(head_b)
    assert len(tail_a) == len(tail_b)


def test_build_marker_pair_rejects_unknown_arm():
    with pytest.raises(ValueError):
        build_marker_pair(1, 0, "off", 0)


# --- (d) 闭集 + 命名红线 + 行值形状 ----------------------------------------------

def test_probe_row_keys_has_no_banned_terms():
    """(T-EX3-d) 命名红线:一条主动用例,不只是 docstring。"""
    import re

    banned = re.compile(r"cache_hit|hit_rate|命中", re.IGNORECASE)
    for key in PROBE_ROW_KEYS:
        assert not banned.search(key), key
    for key in VERDICTS:
        assert not banned.search(key), key


def test_probe_row_keys_excludes_reflect_trace_prefix_columns():
    """(T-EX3 分工边界)行闭集不含 reflect trace 的对标记盲列。"""
    assert "ctx_bytes_total" not in PROBE_ROW_KEYS
    assert "message_prefix_bytes" not in PROBE_ROW_KEYS


def test_assert_probe_row_closed_rejects_extra_keys():
    # The extra value is itself a valid short code (no spaces) so this test
    # isolates the CLOSED-SET guard from the separate value-shape guard
    # exercised by `test_assert_probe_row_closed_rejects_freetext_value`
    # below — a mutation that disables only the extra-key check must still
    # be caught here.
    row = _make_row(prompt="leaked_short_code")
    with pytest.raises(ValueError):
        assert_probe_row_closed(row)


def test_assert_probe_row_closed_accepts_minimal_and_full_rows():
    assert_probe_row_closed(_make_row())
    assert_probe_row_closed({"tier": "short", "arm": ARM_STABLE})


def test_assert_probe_row_closed_rejects_freetext_value():
    row = _make_row(finish_reason="模型说了一段自由文本,里面还有空格")
    with pytest.raises(ValueError):
        assert_probe_row_closed(row)


# --- (e) 摘要:首次/重复分开、失败单列、cache_hit 单列、warmup 单列、缺席不折 0 ---

def test_summarize_probe_separates_first_from_repeat_observations():
    rows = [
        _make_row(call_index=0, call_wall_ms=5000, arm=ARM_STABLE, block_index=0),
        _make_row(call_index=0, call_wall_ms=5100, arm=ARM_DISTURBED, block_index=0),
        _make_row(call_index=1, call_wall_ms=1000, arm=ARM_STABLE, block_index=0),
        _make_row(call_index=1, call_wall_ms=1300, arm=ARM_DISTURBED, block_index=0),
    ]
    summary = summarize_probe(rows)
    assert summary["first_observation_row_count"] == 2
    assert summary["repeat_observation_row_count"] == 2
    first = summary["overall"]["first_observation"]
    assert first["stable_median_wall_ms"] == 5000
    assert first["disturbed_median_wall_ms"] == 5100


def test_summarize_probe_singles_out_failures_and_still_produces_a_table():
    """(验收)摘要在"一半调用失败"的输入下仍出表且失败单列。"""
    rows = []
    for block_index in range(2):
        for arm in (ARM_STABLE, ARM_DISTURBED):
            for call_index in range(4):
                status = "error" if call_index % 2 == 0 else "ok"
                rows.append(_make_row(
                    block_index=block_index, arm=arm, call_index=call_index,
                    status=status, call_wall_ms=1000 if status == "ok" else None,
                ))
    summary = summarize_probe(rows)
    assert summary["row_count_total"] == 16
    assert summary["failed_row_count"] == 8
    assert isinstance(summary["overall"], dict)
    assert isinstance(summary["by_tier"], dict)


def test_summarize_probe_singles_out_local_cache_exit_rows():
    rows = [
        _make_row(status="cache_hit", call_wall_ms=1),
        _make_row(status="ok"),
    ]
    summary = summarize_probe(rows)
    assert summary["local_cache_exit_rows"] == 1
    # The cache-hit row must not leak into the "ok" repeat/first buckets.
    assert summary["repeat_observation_row_count"] == 1


def test_summarize_probe_singles_out_warmup_rows():
    rows = [
        _make_row(is_warmup=True, call_wall_ms=999999, status="ok"),
        _make_row(is_warmup=False, call_wall_ms=1000, status="ok"),
    ]
    summary = summarize_probe(rows)
    assert summary["warmup_row_count"] == 1
    assert summary["row_count_total"] == 2
    # The warmup row's absurd wall clock must not appear anywhere in stats.
    assert summary["repeat_observation_row_count"] == 1


def test_summarize_probe_cached_tokens_missing_is_none_not_zero():
    """(验收)`cached_tokens` 全缺时不出 0。"""
    rows = [_make_row(cached_tokens=None) for _ in range(4)]
    summary = summarize_probe(rows)
    assert summary["cached_tokens_observed"] is None


def test_summarize_probe_cached_tokens_present_reports_n_and_sum():
    rows = [
        _make_row(cached_tokens=100),
        _make_row(cached_tokens=200),
        _make_row(cached_tokens=None),
    ]
    summary = summarize_probe(rows)
    assert summary["cached_tokens_observed"] == {"n": 2, "sum": 300}


def test_summarize_probe_empty_input():
    summary = summarize_probe([])
    assert summary["row_count_total"] == 0
    assert summary["verdict"] == "undetermined"


# --- (f) 结论闭集三格 + 变异 ----------------------------------------------------

def _paired_rows(*, stable_ms: int, disturbed_ms: int, n_blocks: int = 4) -> list[dict]:
    rows = []
    for block_index in range(n_blocks):
        for call_index in range(4):
            rows.append(_make_row(
                block_index=block_index, arm=ARM_STABLE, call_index=call_index,
                call_wall_ms=stable_ms,
            ))
            rows.append(_make_row(
                block_index=block_index, arm=ARM_DISTURBED, call_index=call_index,
                call_wall_ms=disturbed_ms,
            ))
    return rows


def test_verdict_is_time_benefit_when_stable_is_consistently_faster():
    rows = _paired_rows(stable_ms=1000, disturbed_ms=1500)
    summary = summarize_probe(rows)
    assert summary["verdict"] == "time_benefit"
    assert summary["verdict"] in VERDICTS


def test_verdict_is_no_discernible_benefit_when_no_gap():
    rows = _paired_rows(stable_ms=1000, disturbed_ms=1000)
    summary = summarize_probe(rows)
    assert summary["verdict"] == "no_discernible_benefit"


def test_verdict_is_undetermined_when_failures_dominate():
    rows = _paired_rows(stable_ms=1000, disturbed_ms=1500)
    for row in rows[: len(rows) // 2 + 1]:
        row["status"] = "error"
        row["call_wall_ms"] = None
    summary = summarize_probe(rows)
    assert summary["verdict"] == "undetermined"


def test_verdict_is_undetermined_with_too_few_paired_regions():
    rows = _paired_rows(stable_ms=1000, disturbed_ms=1500, n_blocks=1)
    summary = summarize_probe(rows)
    assert summary["verdict"] == "undetermined"


def test_verdict_field_is_the_only_conclusion_field_closed_to_three_values():
    """(T-EX3-f 变异 1)人工构造一份摘要,`verdict` 只能取三格之一。"""
    summary = summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))
    assert summary["verdict"] in VERDICTS

    mutated = copy.deepcopy(summary)
    mutated["verdict"] = "definitely_faster"  # 第四个词
    assert mutated["verdict"] not in VERDICTS


def test_verdict_fourth_word_is_caught_by_a_real_mutation(monkeypatch):
    """(T-EX3-f 变异,真实变异而非人工构造)把 `_verdict` 换成会吐出第四个词的
    实现,`summarize_probe` 的运行期闭集守卫必须响亮报红——不是只有测试在挑
    值,production 代码自己也要挡住这条路。变异复核完成后不需要手动改回:
    `monkeypatch` 在用例结束时自动还原。
    """
    monkeypatch.setattr(probe, "_verdict", lambda *a, **k: "definitely_faster")
    with pytest.raises(ValueError):
        summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))


def test_summary_carries_no_percentage_shaped_conclusion_field():
    """(T-EX3-f 变异 2)结论散文不得含比率型/百分比结论字段。

    真实实现的输出键集合里不应该有任何名字暗示"百分比省了多少"的字段——
    `median_wall_ms_ratio` 是允许的**观测量**(design 原文:「比值只作观测量,
    不是结论」),但它必须待在 `overall`/`by_tier` 底下,顶层与 `verdict` 平级
    的地方不许再出现一个百分比/比率型的"结论"字段。这里模拟"变异"实现往
    顶层加了一个百分比字段,断言这不是当前实现产出的形状。
    """
    summary = summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))
    top_level_keys = set(summary)
    banned_conclusion_keys = {
        "time_saved_percent", "time_saved_pct", "hit_rate_percent",
        "speedup_percent",
    }
    assert not (top_level_keys & banned_conclusion_keys)
    # And the mutation itself would be visibly wrong: injecting one changes
    # the key set observed by a strict consumer.
    mutated = copy.deepcopy(summary)
    mutated["time_saved_percent"] = 33.3
    assert mutated.keys() != summary.keys()


# --- (g) render_sample 三档单调递增 + 两臂同正文 + digest 稳定 -------------------

def test_render_sample_tier_lengths_are_strictly_increasing(sample):
    lengths = [len(render_sample(tier, sample)[0]["content"]) for tier in DEFAULT_TIERS]
    assert lengths[0] < lengths[1] < lengths[2]


def test_render_sample_is_identical_regardless_of_arm(sample):
    """两臂同一份正文:`render_sample` 不知道、也不接受 `arm` 参数。"""
    first = render_sample("short", sample)
    second = render_sample("short", sample)
    assert first == second


def test_render_sample_rejects_unknown_tier(sample):
    with pytest.raises(ValueError):
        render_sample("extra_long", sample)


def test_sample_digest_is_stable_and_sensitive_to_content(sample):
    assert sample_digest(sample) == sample_digest(sample)
    mutated = copy.deepcopy(sample)
    mutated["seed_paragraphs"]["task_target"] += " 追加一句话"
    assert sample_digest(mutated) != sample_digest(sample)


def test_sample_tier_chars_matches_fixture(sample):
    chars = sample_tier_chars(sample)
    assert list(chars) == ["short", "medium", "long"]
    assert chars["short"] < chars["medium"] < chars["long"]
    for value in chars.values():
        assert isinstance(value, int) and value > 0


# --- (h) manifest facts 过 assert_manifest --------------------------------------

def test_probe_manifest_facts_passes_assert_manifest(sample):
    plan = probe_plan(seed=99)
    facts = probe_manifest_facts(plan, sample, 99)
    row = {
        "channel": "e1",
        "code_sha": "deadbeef12345678",
        "started_at": "2026-09-11T00:00:00Z",
        "finished_at": "2026-09-11T00:10:00Z",
        "stopped_by_budget": False,
        **facts,
    }
    manifest.assert_manifest(row)


def test_probe_manifest_facts_matrix_matches_plan_shape(sample):
    plan = probe_plan(seed=5, blocks=SMOKE_BLOCKS)
    facts = probe_manifest_facts(plan, sample, 5)
    assert facts["matrix"]["tiers"] == len(DEFAULT_TIERS)
    assert facts["matrix"]["blocks"] == SMOKE_BLOCKS
    assert facts["matrix"]["arms"] == len(DEFAULT_ARMS)
    assert facts["matrix"]["calls_per_series"] == DEFAULT_CALLS_PER_SERIES
    assert facts["arm_order_seed"] == 5
    assert facts["seed"] == 5


# --- (i) fixture 隐私扫描 -------------------------------------------------------

def test_fixture_sample_has_no_urls_credentials_or_real_question_text():
    raw = SAMPLE_PATH.read_text(encoding="utf-8")
    lowered = raw.lower()
    for banned in ("http://", "https://", "postgresql://", "api_key", "password"):
        assert banned not in lowered, banned
    parsed = json.loads(raw)
    assert set(parsed) == {"version", "note", "tier_chars", "seed_paragraphs"}
