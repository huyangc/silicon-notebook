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
import re
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
        "gap_ms": None,
    }
    row.update(overrides)
    return row


#: 命名红线的正则(T-EX3-d/f,codex #T-EX3 F5):同时挡 `cache_hit`/hit
#: rate/命中率这三个既有形状,以及 `percent`/`_pct` 这类百分比结论字段——
#: 后两个是 F5 新增,因为旧版红线只挡四个硬编码名字,`speedup_pct` 这类改
#: 一个字母就能绕过硬编码黑名单。
_BANNED_NAME_PATTERN = re.compile(r"cache_hit|hit_rate|命中|percent|_pct", re.IGNORECASE)


def _assert_no_banned_keys(value: object) -> None:
    """递归扫 `value` 里出现的**每一个**字典键,不管嵌套多深(F5)。

    只扫 `summarize_probe` 这类纯 dict/list 输出的真实键,不需要处理
    `Mapping`/元组等更宽的协议——production 代码从不返回除 dict/list/标量
    之外的容器形状。
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            assert not _BANNED_NAME_PATTERN.search(str(key)), key
            _assert_no_banned_keys(sub)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_banned_keys(item)


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


def test_stable_first_flags_depends_on_seed_and_tier():
    """(T-EX3-b,codex #T-EX3 F6)臂序的种子/档位依赖不是恒真断言。

    旧版只靠 `probe_plan(seed=101) != probe_plan(seed=202)` 表达「不同 seed
    不同」,但标记值本身已经含 seed,这个不等式与臂序无关、恒成立——把臂序
    的随机源换成一个忽略 seed/tier 的固定源(M2)后 41 个用例全绿。这里直接
    对 `_stable_first_flags` 的返回值(哪几个区组 stable 先)做断言:同一
    tier 下,多个 seed 里至少有两个给出不同分法;同一 seed 下,三档的分法
    不全同。
    """
    flags_by_seed = {
        seed: tuple(probe._stable_first_flags(DEFAULT_BLOCKS, seed, "short"))
        for seed in (1, 2, 3, 4, 5)
    }
    assert len(set(flags_by_seed.values())) > 1

    per_tier = {
        tier: tuple(probe._stable_first_flags(DEFAULT_BLOCKS, 101, tier))
        for tier in DEFAULT_TIERS
    }
    assert len(set(per_tier.values())) > 1


def test_build_marker_pair_is_deterministic_per_call():
    for _ in range(3):
        assert build_marker_pair(5, "short", 2, ARM_STABLE, 3) == build_marker_pair(5, "short", 2, ARM_STABLE, 3)
    assert build_marker_pair(5, "short", 2, ARM_STABLE, 3) != build_marker_pair(6, "short", 2, ARM_STABLE, 3)


def test_build_marker_pair_differs_by_tier():
    """(T-EX3-F1)序列身份含 `tier`:同一个 `(seed, block_index, arm, call_index)`
    换一个 `tier` 必须换一组标记值——否则三个长度档会共用同一批标记与
    (`render_sample` 互为前缀的)同一段正文前缀。
    """
    assert build_marker_pair(5, "short", 2, ARM_STABLE, 3) != build_marker_pair(5, "medium", 2, ARM_STABLE, 3)
    assert build_marker_pair(5, "short", 2, ARM_STABLE, 3) != build_marker_pair(5, "long", 2, ARM_STABLE, 3)


# --- (c) 标记等长等数 + 不相交 + 过校验 -----------------------------------------

def test_marker_pair_equal_length_and_count_across_arms():
    """(T-EX3-c) 两臂标记长度、数量(2 个:head+tail)逐格相等。"""
    for tier in DEFAULT_TIERS:
        for call_index in range(4):
            head_s, tail_s = build_marker_pair(9, tier, 0, ARM_STABLE, call_index)
            head_d, tail_d = build_marker_pair(9, tier, 0, ARM_DISTURBED, call_index)
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


def test_marker_streams_do_not_overlap_across_series():
    """(T-EX3-F1)序列身份是 `(tier, block_index, arm)`:默认计划有 3 档 × 4
    区组 × 2 臂 = 24 个序列,对应 24 条互不相交的标记流。`tier` 缺席时,不同
    档位的同一 `(block_index, arm)` 会复用同一批标记值——这条用例直接对
    24 个序列做两两不交检查,而不是只比较两臂之间。
    """
    plan = probe_plan(seed=41)
    series_heads: dict[tuple, set[str]] = {}
    series_tails: dict[tuple, set[str]] = {}
    for row in plan:
        key = (row["tier"], row["block_index"], row["arm"])
        series_heads.setdefault(key, set()).add(row["head"])
        series_tails.setdefault(key, set()).add(row["tail"])

    assert len(series_heads) == len(DEFAULT_TIERS) * DEFAULT_BLOCKS * 2

    seen_heads: set[str] = set()
    for key, heads in series_heads.items():
        assert not (heads & seen_heads), f"{key} head collides with an earlier series"
        seen_heads |= heads

    seen_tails: set[str] = set()
    for key, tails in series_tails.items():
        assert not (tails & seen_tails), f"{key} tail collides with an earlier series"
        seen_tails |= tails


def test_marker_pair_passes_experiment_message_markers_validation():
    """(T-EX3-c) 每格 head/tail 都能过 `experiment_message_markers` 的非空 str 校验。"""
    from app.core.llm import experiment_message_markers

    plan = probe_plan(seed=23, blocks=2)
    for row in plan[:6]:
        with experiment_message_markers(row["head"], row["tail"]):
            pass


def test_marker_pair_new_identifier_per_series():
    """每个序列 = `(tier, block_index, arm)` 换新标识:同一臂在不同 block
    或不同 tier 上的 head/tail 不重复(codex #T-EX3 F1)。"""
    heads = set()
    for tier in DEFAULT_TIERS:
        for block_index in range(4):
            head, _tail = build_marker_pair(1, tier, block_index, ARM_STABLE, 0)
            assert head not in heads
            heads.add(head)


def test_marker_variant_switch_changes_values_but_not_shape():
    head_a, tail_a = build_marker_pair(1, "short", 0, ARM_STABLE, 0, marker_variant=0)
    head_b, tail_b = build_marker_pair(1, "short", 0, ARM_STABLE, 0, marker_variant=1)
    assert (head_a, tail_a) != (head_b, tail_b)
    assert len(head_a) == len(head_b)
    assert len(tail_a) == len(tail_b)


def test_build_marker_pair_rejects_unknown_arm():
    with pytest.raises(ValueError):
        build_marker_pair(1, "short", 0, "off", 0)


# --- (F2) 两臂哪端固定/哪端逐次变化 ---------------------------------------------

def test_build_marker_pair_stable_head_fixed_tail_varies():
    """(T-EX3-F2)stable:head 对 `call_index` 恒定(集合大小 1),tail 逐次
    不同(4 个不同值)。变异角色对调(M1)必须让这条用例翻红。
    """
    heads = {build_marker_pair(3, "short", 0, ARM_STABLE, i)[0] for i in range(4)}
    tails = {build_marker_pair(3, "short", 0, ARM_STABLE, i)[1] for i in range(4)}
    assert len(heads) == 1
    assert len(tails) == 4


def test_build_marker_pair_disturbed_head_varies_tail_fixed():
    """(T-EX3-F2)disturbed:反过来,head 逐次不同(4 个不同值),tail 对
    `call_index` 恒定(集合大小 1)。变异角色对调(M1)必须让这条用例翻红。
    """
    heads = {build_marker_pair(3, "short", 0, ARM_DISTURBED, i)[0] for i in range(4)}
    tails = {build_marker_pair(3, "short", 0, ARM_DISTURBED, i)[1] for i in range(4)}
    assert len(heads) == 4
    assert len(tails) == 1


# --- (F3) gap_ms 值形状 --------------------------------------------------------

def test_probe_row_gap_ms_accepts_numeric_and_none():
    """(T-EX3-F3)`gap_ms`:序列首格 `None`(没有「上一次调用」),其余格是
    数值。"""
    assert_probe_row_closed(_make_row(call_index=0, gap_ms=None))
    assert_probe_row_closed(_make_row(call_index=1, gap_ms=812))


# --- (d) 闭集 + 命名红线 + 行值形状 ----------------------------------------------

def test_probe_row_keys_has_no_banned_terms():
    """(T-EX3-d) 命名红线:一条主动用例,不只是 docstring。

    `_BANNED_NAME_PATTERN`(codex #T-EX3 F5)同时扫 `PROBE_ROW_KEYS`、
    `VERDICTS`,以及一份**真实** `summarize_probe(...)` 输出的全部键(递归)
    ——静态的两个常量挡不住往 `summarize_probe` 返回值里加一个新键。
    """
    for key in PROBE_ROW_KEYS:
        assert not _BANNED_NAME_PATTERN.search(key), key
    for key in VERDICTS:
        assert not _BANNED_NAME_PATTERN.search(key), key

    summary = summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))
    _assert_no_banned_keys(summary)


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


def test_summarize_probe_reports_repeat_observation_medians_too():
    """(T-EX3-F4)`repeat_observation` 与 `first_observation` 对称:重复观测
    各臂的中位墙钟单独可读,不需要从配对差值反推「重复观测比首次快了多少」
    ——design §9.1「并比较后续相对首次的变化」要求这一列能直接看到。
    """
    rows = [
        _make_row(call_index=0, call_wall_ms=4000, arm=ARM_STABLE, block_index=0),
        _make_row(call_index=0, call_wall_ms=4500, arm=ARM_DISTURBED, block_index=0),
        _make_row(call_index=1, call_wall_ms=1000, arm=ARM_STABLE, block_index=0),
        _make_row(call_index=1, call_wall_ms=1500, arm=ARM_DISTURBED, block_index=0),
    ]
    summary = summarize_probe(rows)
    first = summary["overall"]["first_observation"]
    repeat = summary["overall"]["repeat_observation"]
    assert first["stable_median_wall_ms"] == 4000
    assert first["disturbed_median_wall_ms"] == 4500
    assert repeat["stable_median_wall_ms"] == 1000
    assert repeat["disturbed_median_wall_ms"] == 1500


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
    """(T-EX3-e,codex #T-EX3 F12)`cache_hit` 行只进 `local_cache_exit_rows`
    这一桶,不得同时也算进 `failed_row_count`——分桶必须互斥。去掉
    `remaining` 里的 cache_hit 过滤(M13)会让这行同时落进两个计数,这条用例
    必须翻红。
    """
    rows = [
        _make_row(status="cache_hit", call_wall_ms=1),
        _make_row(status="ok"),
    ]
    summary = summarize_probe(rows)
    assert summary["local_cache_exit_rows"] == 1
    assert summary["failed_row_count"] == 0
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


def _paired_rows_per_block(pairs: list[tuple[int, int]]) -> list[dict]:
    """像 `_paired_rows`,但每个区组的 stable/disturbed 墙钟各自可控——用来
    构造「中位差为正、但区组间方向不一致」这类 `_paired_rows` 表达不出的形状
    (F7/F8)。
    """
    rows = []
    for block_index, (stable_ms, disturbed_ms) in enumerate(pairs):
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


def test_consistency_threshold_is_pinned_at_075():
    """(T-EX3-f,codex #T-EX3 F7)`_CONSISTENCY_THRESHOLD` 的值本身被钉住。

    把阈值降到 0.0(M24)会让任何非零一致性都判 `time_benefit`,41 个用例
    全绿——这条用例直接对常量取值做回归钉,阈值改了就先在这里翻红,不需要
    等到某个更复杂的场景用例间接暴露。
    """
    assert probe._CONSISTENCY_THRESHOLD == 0.75


def test_verdict_requires_consistency_not_just_positive_median_delta():
    """(T-EX3-f,codex #T-EX3 F7)一致性低于阈值时,即便区组间中位差为正,
    也不能判 `time_benefit`——这是 `_verdict` 的第二个必要条件,丢掉它
    (M28,`if median_delta > 0:` 不再检查 `consistency_ratio`)41 个用例
    全绿。

    5 个区组的 delta(disturbed - stable)分别是 -300/-200/100/200/300:
    排序后中位数是 100(为正),但只有 3/5 = 0.6 个区组的符号与中位数一致,
    低于 `_CONSISTENCY_THRESHOLD`(0.75)——真实实现必须判
    `no_discernible_benefit`。
    """
    pairs = [
        (1000, 700), (1000, 800), (1000, 1100), (1000, 1200), (1000, 1300),
    ]
    rows = _paired_rows_per_block(pairs)
    summary = summarize_probe(rows)
    overall = summary["overall"]
    assert overall["median_wall_ms_delta"] == 100
    assert overall["consistency_ratio"] == pytest.approx(0.6)
    assert summary["verdict"] == "no_discernible_benefit"


def test_summarize_region_pairs_uses_median_not_mean():
    """(T-EX3-f,codex #T-EX3 F8)design §9.1 明写「**中位**墙钟差与比值」;
    换成 `statistics.mean`(M19)41 个用例全绿——这条用例构造一批
    均值≠中位数的观测,精确锁定中位数。

    一个区组内,disturbed 的三次重复观测是 1000/1000/4000:中位数 1000、
    均值 2000。stable 恒 1000。用中位数时 delta 应为 0,用均值时会是 1000。
    """
    rows = [
        _make_row(block_index=0, arm=ARM_STABLE, call_index=1, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_STABLE, call_index=2, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_STABLE, call_index=3, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=1, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=2, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=3, call_wall_ms=4000),
    ]
    result = probe._summarize_region_pairs(rows)
    assert result["median_wall_ms_delta"] == 0


def test_by_tier_only_reads_its_own_tier_rows():
    """(T-EX3-e,codex #T-EX3 F8)`by_tier[tier]` 只用那一档自己的行——三档
    各给一个不同的 delta,`by_tier` 分档报告不能悄悄退化成三份相同的全量
    报告(M22:`by_tier` 不分档,每档都读全量重复观测,41 个用例全绿)。
    """
    tier_deltas = {"short": 100, "medium": 300, "long": 900}
    rows = []
    for tier, delta in tier_deltas.items():
        for block_index in range(2):
            for call_index in range(1, 4):
                rows.append(_make_row(
                    tier=tier, block_index=block_index, arm=ARM_STABLE,
                    call_index=call_index, call_wall_ms=1000,
                ))
                rows.append(_make_row(
                    tier=tier, block_index=block_index, arm=ARM_DISTURBED,
                    call_index=call_index, call_wall_ms=1000 + delta,
                ))
    summary = summarize_probe(rows)
    for tier, delta in tier_deltas.items():
        assert summary["by_tier"][tier]["median_wall_ms_delta"] == delta, tier


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
    """(T-EX3-f 变异 2,codex #T-EX3 F5/F11)结论散文不得含比率型/百分比结论
    字段——用命名红线正则递归扫真实输出,不是一份四个硬编码名字的黑名单
    (`speedup_pct` 这类改一个字母就能绕过硬编码黑名单)。

    `median_wall_ms_ratio` 是允许的**观测量**(design 原文:「比值只作观测量,
    不是结论」),它待在 `overall`/`by_tier` 底下,不含任何红线词,所以不受
    这条扫描影响。
    """
    summary = summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))
    _assert_no_banned_keys(summary)


@pytest.mark.parametrize("banned_key", ["speedup_pct", "cache_hit_rate", "命中率"])
def test_banned_conclusion_key_injected_at_top_level_is_caught_by_a_real_mutation(
    monkeypatch, banned_key,
):
    """(T-EX3-f 变异,真实变异而非人工构造,codex #T-EX3 F5)把
    `summarize_probe` 换成一个会在顶层多塞一个红线词字段的实现,命名红线
    扫描必须挡住——覆盖 `speedup_pct`/`cache_hit_rate`/`命中率` 这三种在
    评审变异 M29/M30/M31 里实测绕过旧版四词黑名单的形状。
    """
    real_summarize_probe = probe.summarize_probe

    def _mutated(rows):
        result = real_summarize_probe(rows)
        result[banned_key] = 33.3
        return result

    monkeypatch.setattr(probe, "summarize_probe", _mutated)
    summary = probe.summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))
    with pytest.raises(AssertionError):
        _assert_no_banned_keys(summary)


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


def test_render_sample_block_order_is_fixed_S_C_K_D_T(sample):
    """(T-EX3-g,codex #T-EX3 F9)五块顺序固定为静态指令(S)→动态能力(C)→
    证据卡池(K)→增量提示(D)→任务目标(T),三档都一样。把 K 挪到最前
    (M23)41 个用例全绿——(g) 原有用例只钉了三档长度单调递增与两次调用相等,
    从没检查过块的相对顺序。
    """
    paragraphs = sample["seed_paragraphs"]
    for tier in DEFAULT_TIERS:
        content = render_sample(tier, sample)[0]["content"]
        positions = [
            content.index(paragraphs["static_instructions"]),
            content.index(paragraphs["capability_catalog"]),
            content.index(paragraphs["evidence_cards"]),
            content.index(paragraphs["delta_notes"]),
            content.index(paragraphs["task_target"]),
        ]
        assert positions == sorted(positions), (tier, positions)


def test_render_sample_output_instruction_is_identical_across_tiers(sample):
    """(T-EX3-g,codex #T-EX3 F9)固定的小 JSON 输出任务跨档逐字相同——让输出
    指令带上 `tier`(M27)41 个用例全绿。任务目标块之后的那一段文本(输出
    指令所在处)三档必须逐字一致。
    """
    task_target = sample["seed_paragraphs"]["task_target"]
    tails = set()
    for tier in DEFAULT_TIERS:
        content = render_sample(tier, sample)[0]["content"]
        tail = content[content.rindex(task_target) + len(task_target):]
        tails.add(tail)
    assert len(tails) == 1


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


def test_fixture_tier_chars_match_config_defaults(sample):
    """(T-EX3-g,codex #T-EX3 F10)三档目标字符数「派生自」config 的登记
    默认值这句话不能只留在 fixture 的 `note` 散文里——`config.py:49-50` 明写
    「字面量只在这里出现一次……调用点绝不复制」,这份 fixture 把数值复制成了
    JSON 字面量,这条用例把两边对上账:config 改了默认值,这里先红。
    """
    from app.core.config import DEFAULT_REFLECT_EVIDENCE_CHARS_BY_EFFORT as config_defaults

    chars = sample_tier_chars(sample)
    assert chars["short"] == config_defaults["overview"]
    assert chars["medium"] == config_defaults["deep"]
    assert chars["long"] == config_defaults["exhaustive"]


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


def test_probe_manifest_facts_tier_chars_only_includes_planned_tiers(sample):
    """(T-EX3-h,codex #T-EX3 F13)`matrix["tier_chars"]` 只写 `plan` 里实际
    跑过的档位,不是样本声明的全部档位——`matrix["tiers"]` 数的是前者,
    `probe_plan(tiers=("short",))` 时如果 `tier_chars` 还报三档,这两个数字
    就对不上账。
    """
    plan = probe_plan(seed=5, tiers=("short",), blocks=SMOKE_BLOCKS)
    facts = probe_manifest_facts(plan, sample, 5)
    assert facts["matrix"]["tiers"] == 1
    assert set(facts["matrix"]["tier_chars"]) == {"short"}


# --- (i) fixture 隐私扫描 -------------------------------------------------------

def test_fixture_sample_has_no_urls_credentials_or_real_question_text():
    raw = SAMPLE_PATH.read_text(encoding="utf-8")
    lowered = raw.lower()
    for banned in ("http://", "https://", "postgresql://", "api_key", "password"):
        assert banned not in lowered, banned
    parsed = json.loads(raw)
    assert set(parsed) == {"version", "note", "tier_chars", "seed_paragraphs"}
