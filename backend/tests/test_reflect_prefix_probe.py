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
    PROBE_OUTPUT_INSTRUCTION,
    PROBE_SCHEMA_HINT,
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


def test_probe_plan_rejects_odd_blocks():
    """(P3-8)奇数区组让 `_stable_first_flags` 多出来的那一个恒分给
    disturbed 先,三档之间同向叠加成一个系统性顺序偏置——E1 实际只跑
    `SMOKE_BLOCKS`(2)与 `DEFAULT_BLOCKS`(4)两个规模,拒绝奇数不影响任何
    既有调用方。"""
    with pytest.raises(ValueError):
        probe_plan(seed=1, blocks=3)


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


def test_stable_first_flags_are_pinned_for_seed_101():
    """(P3-1)臂序种子的跨进程/跨版本可复现性此前没有金标钉住——
    `test_stable_first_flags_depends_on_seed_and_tier` 只断言"存在差异",
    把随机源换成 `random.Random(hash((seed, tier, "arm_order")))`(变异
    Q2)后依然能凑出"至少两个不同分法",41 个用例照样全绿;而 `hash()`
    每进程随机化,同一个 `arm_order_seed` 换一台机器就换一种分法,§13
    「冻结 manifest 可复现」当场失真。这里把 `(seed=101, tier)` 的具体
    flags 列表钉成字面量金标(自己跑一次 `_stable_first_flags` 取值写死)。
    """
    assert probe._stable_first_flags(4, 101, "short") == [False, True, False, True]
    assert probe._stable_first_flags(4, 101, "medium") == [True, False, False, True]
    assert probe._stable_first_flags(4, 101, "long") == [True, True, False, False]


def test_series_index_is_globally_unique_and_non_decreasing():
    """(P3-2)`series_index` 是整份计划里第几个序列(全局单调递增),用来在
    分析侧把一个序列的四行认成一组。把 `series_index = block_index`(变异
    Q19)后,三档 × 两臂的 24 个序列会共用 4 个编号,T-EX4 的分组会直接
    错——这里直接钉住"24 个序列、24 个互不相同的编号、整条计划非降"。
    """
    plan = probe_plan(seed=13)
    series_indices = [row["series_index"] for row in plan]
    assert len(set(series_indices)) == len(DEFAULT_TIERS) * DEFAULT_BLOCKS * 2
    assert series_indices == sorted(series_indices)


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
    """(P3-3)`head`/`tail` 各自恒以 `"H"`/`"T"` 开头,`stable_heads &
    stable_tails` 这类跨端集合交集因此**不管 `_marker_code` 怎么写都恒空**
    ——这两条曾是重言式断言,变异 Q23(payload 去掉 `component`)全绿。改成
    去掉前缀字母、直接比较码体本身:同一 `(seed, tier, block_index, arm,
    index, marker_variant)` 缺了 `component` 区分 head/tail 时,`call_index
    == head_index`(如 stable 臂第 0 次调用)的那些行会让 head/tail 码体
    撞在一起,这里才真的挡得住。
    """
    plan = probe_plan(seed=17, blocks=2)
    stable_heads = {r["head"] for r in plan if r["arm"] == ARM_STABLE}
    stable_tails = {r["tail"] for r in plan if r["arm"] == ARM_STABLE}
    disturbed_heads = {r["head"] for r in plan if r["arm"] == ARM_DISTURBED}
    disturbed_tails = {r["tail"] for r in plan if r["arm"] == ARM_DISTURBED}
    assert not (stable_heads & disturbed_heads)
    assert not (stable_tails & disturbed_tails)

    stable_head_bodies = {h[1:] for h in stable_heads}
    stable_tail_bodies = {t[1:] for t in stable_tails}
    disturbed_head_bodies = {h[1:] for h in disturbed_heads}
    disturbed_tail_bodies = {t[1:] for t in disturbed_tails}
    assert not (stable_head_bodies & stable_tail_bodies)
    assert not (disturbed_head_bodies & disturbed_tail_bodies)


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


def test_marker_payload_separators_prevent_tier_field_boundary_collisions():
    """(P3-11)`_marker_code` 的 payload 只有 `tier` 是自由字符串,字段之间靠
    `:` 分隔符唯一定位边界——去掉全部分隔符(变异 Q1)后,`tier="a"` +
    `block_index=11` 与 `tier="a1"` + `block_index=1` 这类组合会拼出逐字
    相同的字符串(`"1a11..."` == `"1a11..."`),而当前实现是真的不会碰
    (分隔符纪律真实生效)。用 `tiers=("a", "a1"), blocks=12` 直接构造这个
    边界,按序列(而不是按逐行)比较——同一序列内 head(stable)/tail
    (disturbed)本来就该在 `call_index` 之间恒定,只有跨序列的标记流才
    要求两两不交。
    """
    plan = probe_plan(seed=1, tiers=("a", "a1"), blocks=12)
    series_heads: dict[tuple, set[str]] = {}
    series_tails: dict[tuple, set[str]] = {}
    for row in plan:
        key = (row["tier"], row["block_index"], row["arm"])
        series_heads.setdefault(key, set()).add(row["head"])
        series_tails.setdefault(key, set()).add(row["tail"])

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


def test_summarize_probe_singles_out_invalid_output_rows():
    """(codex #709 R2 P2-1)`status == "invalid_output"` 单列
    `invalid_output_rows`,不进 `failed_row_count`(它不是传输失败),也不进
    `first_observation`/`repeat_observation`(它没有完成固定输出任务,墙钟不能
    当成一次已验证的计时观测)——三桶必须互斥。

    变异:去掉 `summarize_probe` 里的 `invalid_output_rows` 过滤(把
    `after_cache_exit` 直接当 `remaining` 用)会让这三行全部落进
    `failed_row_count`/`ok_rows`,这条用例必须翻红。
    """
    rows = [
        _make_row(status="invalid_output", call_wall_ms=1, call_index=1),
        _make_row(status="invalid_output", call_wall_ms=1, call_index=0),
        _make_row(status="ok", call_index=1),
    ]
    summary = summarize_probe(rows)
    assert summary["invalid_output_rows"] == 2
    assert summary["failed_row_count"] == 0
    assert summary["first_observation_row_count"] == 0
    assert summary["repeat_observation_row_count"] == 1
    assert summary["row_count_total"] == 3


def test_summarize_probe_invalid_output_rows_do_not_pair_into_regions():
    """(codex #709 R2 P2-1)`invalid_output` 行被排除在 `ok_rows`/重复观测之外
    ——一个区组的某条臂**只剩** `invalid_output` 观测时,那个区组配不出
    `n_regions_paired`(「主统计 n_regions_paired 不含那些格」)。

    变异:同上一条,去掉 `invalid_output_rows` 的过滤会让这一行重新混进
    `ok_rows`,`block_index=0` 也会配出一对,`n_regions_paired` 变成 2。
    """
    rows = [
        _make_row(block_index=0, arm=ARM_STABLE, call_index=1, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=1,
                  status="invalid_output", call_wall_ms=1),
        _make_row(block_index=1, arm=ARM_STABLE, call_index=1, call_wall_ms=1000),
        _make_row(block_index=1, arm=ARM_DISTURBED, call_index=1, call_wall_ms=1500),
    ]
    summary = summarize_probe(rows)
    assert summary["invalid_output_rows"] == 1
    assert summary["overall"]["n_regions_paired"] == 1


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


def test_summarize_probe_returns_pinned_top_level_and_nested_key_set():
    """(P3-7)`summarize_probe` 的返回键集合钉成字面量白名单——命名红线正则
    (`_BANNED_NAME_PATTERN`)只挡红线词**形状**,挡不住悄悄新增一个合法但
    未经审视的顶层键(变异 Q7 的姊妹场景:少键;多键同样值得挡)。任何新增
    顶层键、或 `overall`/`by_tier` 内层键都必须先过这条用例。
    """
    summary = summarize_probe(_paired_rows(stable_ms=1000, disturbed_ms=1500))
    assert set(summary) == {
        "row_count_total", "warmup_row_count", "local_cache_exit_rows",
        "invalid_output_rows", "failed_row_count", "first_observation_row_count",
        "repeat_observation_row_count", "cached_tokens_observed",
        "by_tier", "overall", "verdict",
    }
    scope_keys = {
        "n_regions_paired", "median_wall_ms_delta", "median_wall_ms_ratio",
        "consistency_ratio", "first_observation", "repeat_observation",
    }
    observation_keys = {"stable_median_wall_ms", "disturbed_median_wall_ms"}
    assert set(summary["overall"]) == scope_keys
    assert set(summary["overall"]["first_observation"]) == observation_keys
    assert set(summary["overall"]["repeat_observation"]) == observation_keys
    for tier_summary in summary["by_tier"].values():
        assert set(tier_summary) == scope_keys


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
    """(T-EX3-f,codex #T-EX3 F8;P3-10)design §9.1 明写「**中位**墙钟差与
    比值」;换成 `statistics.mean`(M19)41 个用例全绿——这条用例构造一批
    均值≠中位数的观测,精确锁定中位数。离群值放在**首位**(不是末位):
    换成"取区组内第一个样本"(变异 Q27,`stable_vals[0]`)时,末位离群值
    版本恰好也等于中位数,挡不住这个变异;首位离群值能同时挡住 mean 与
    `[0]` 这两种错误实现。

    一个区组内,disturbed 的三次重复观测是 4000/1000/1000(按 call_index
    1/2/3 的顺序,离群值排第一):中位数 1000、均值 2000、`vals[0]` 是
    4000。stable 恒 1000。用中位数时 delta 应为 0,用均值或"取第一个"时
    都不是 0。
    """
    rows = [
        _make_row(block_index=0, arm=ARM_STABLE, call_index=1, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_STABLE, call_index=2, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_STABLE, call_index=3, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=1, call_wall_ms=4000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=2, call_wall_ms=1000),
        _make_row(block_index=0, arm=ARM_DISTURBED, call_index=3, call_wall_ms=1000),
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


def test_render_sample_output_instruction_present_and_length_is_exact(sample):
    """(P2-2)`render_sample` 三档正文都必须真的**含有**完整的输出指令,而且
    正文总长度精确等于 `sample_tier_chars` 声明的目标——此前没有任何用例
    断言这两件事:去掉整段输出指令(变异 Q25)、`growing_target` 不再扣
    分隔符与指令长度(变异 Q4)、`_fill_to_length` 去掉 `[:target_chars]`
    截断(变异 Q3)全部全绿。这里同时钉住"在场"与"精确长度"。
    """
    tier_chars = sample_tier_chars(sample)
    for tier in DEFAULT_TIERS:
        content = render_sample(tier, sample)[0]["content"]
        assert PROBE_OUTPUT_INSTRUCTION in content, tier
        assert len(content) == tier_chars[tier], tier


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


def test_sample_tier_chars_rejects_non_increasing_targets(sample):
    """(P2-1)三档字符数必须严格递增(`short < medium < long`,按
    `DEFAULT_TIERS` 顺序)——此前这条性质只由 fixture 自身恰好满足的取值
    间接成立,换一份打平或倒序的 `tier_chars` 时没有任何守卫会挡住。"""
    mutated = copy.deepcopy(sample)
    mutated["tier_chars"] = {"short": 8000, "medium": 8000, "long": 16000}
    with pytest.raises(ValueError):
        sample_tier_chars(mutated)


def test_render_sample_rejects_target_at_or_below_fixed_floor(sample):
    """(P2-1)`render_sample` 在声明的档位字符数不超过"四个固定块 + 分隔符
    + 输出指令"这条地板时必须响亮拒绝,而不是让 `max(0, …)` 静默把 K 块
    砍成空正文——那样三档会渲染出**逐字相同**的正文,E1 的"长度档"这一整
    个维度会塌成一格,而且没有任何一条既有用例(包括 `sample_tier_chars`
    的正整数闸)挡得住,因为 100/200/300 本身就是合法的正整数且严格递增。
    这份 fixture 的地板是 336(固定块)+10(分隔符)+38(输出指令)=384 字符;
    这里把三档目标全部设在地板以下。
    """
    mutated = copy.deepcopy(sample)
    mutated["tier_chars"] = {"short": 100, "medium": 200, "long": 300}
    with pytest.raises(ValueError):
        render_sample("short", mutated)


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


def test_probe_manifest_facts_rejects_calls_per_series_mismatch(sample):
    """(P2-3)`matrix["calls_per_series"]` 从 `plan` 反推(`max(call_index)+1`),
    关键字参数只做交叉校验——调用方给 CLI 加 `--calls-per-series` 却忘了
    把同一个值传进 `probe_plan` 时,这里必须先炸,不能让冻结的 matrix 与
    `probe-*.jsonl` 的实跑行数对不上账。
    """
    plan = probe_plan(seed=1, blocks=SMOKE_BLOCKS, calls_per_series=4)
    with pytest.raises(ValueError):
        probe_manifest_facts(plan, sample, 1, calls_per_series=99)


def test_probe_manifest_facts_rejects_arms_mismatch(sample):
    """(P2-3)`matrix["arms"]` 同样从 `plan` 反推;传一个与 plan 实际臂集合
    不一致的 `arms` 关键字参数必须响亮拒绝。"""
    plan = probe_plan(seed=1, blocks=SMOKE_BLOCKS)
    with pytest.raises(ValueError):
        probe_manifest_facts(plan, sample, 1, arms=("stable",))


def test_probe_manifest_facts_rejects_empty_plan(sample):
    """(P2-3)空 `plan`(调用方按 `--only-tier` 过滤后传进来)此前会得到
    `matrix={'tiers': 0, 'blocks': 0, ...}` 且 `assert_manifest` 放行——
    这条性质本身没有意义,必须在这里就响亮拒绝。"""
    with pytest.raises(ValueError):
        probe_manifest_facts([], sample, 1)


def test_probe_manifest_facts_rejects_tier_not_declared_in_sample(sample):
    """(P3-4)`plan` 里出现了 `sample` 没声明字符数的档位时,必须是带消息的
    `ValueError`,不是裸 `KeyError`(`all_tier_chars[tier]` 此前的样子)。"""
    plan = probe_plan(seed=1, tiers=("xl",), blocks=SMOKE_BLOCKS)
    with pytest.raises(ValueError):
        probe_manifest_facts(plan, sample, 1)


def test_probe_manifest_facts_returns_pinned_key_set(sample):
    """(Q7)`probe_manifest_facts` 的返回键集合钉字面量——把
    `optimization_by_arm`/`common_baseline`/`order` 从返回值里删掉的变异
    (Q7)在既有用例下全绿,因为这三个键在 E1 里不是 `assert_manifest` 的
    必填集。这里直接钉住函数自己承诺产出的全部键。
    """
    plan = probe_plan(seed=5, blocks=SMOKE_BLOCKS)
    facts = probe_manifest_facts(plan, sample, 5)
    assert set(facts) == {
        "seed", "sample_digest", "matrix", "arms", "order",
        "optimization_by_arm", "common_baseline", "arm_order_seed",
    }


def test_load_prefix_probe_sample_rejects_non_dict_json(tmp_path):
    """(P3-9)`load_prefix_probe_sample` 的 `-> dict` 曾经是一句不成立的类型
    声明:一份 JSON 数组的 `--sample-file` 会原样返回 `list`,随后在
    `sample_tier_chars` 撞一个不带上下文的 `AttributeError`。这里直接对
    加载器本身断言:非 `dict` 顶层结构必须响亮拒绝,不把加载的内容带进
    异常消息。
    """
    path = tmp_path / "not_a_dict.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_prefix_probe_sample(path)


# --- (i) fixture 隐私扫描 -------------------------------------------------------

def test_fixture_sample_has_no_urls_credentials_or_real_question_text():
    raw = SAMPLE_PATH.read_text(encoding="utf-8")
    lowered = raw.lower()
    for banned in ("http://", "https://", "postgresql://", "api_key", "password"):
        assert banned not in lowered, banned
    parsed = json.loads(raw)
    assert set(parsed) == {"version", "note", "tier_chars", "seed_paragraphs"}



def test_probe_schema_hint_is_in_the_repository_example_dialect():
    """E1 的 schema hint 必须过 `ScheduledJsonChatClient` 真实那道校验。

    真机上 `reasoning_agent` 是 JSON-repair 工作负载,每格响应都经
    `parse_model_json_object` + `validate_model_json_shape(content, hint)`;
    hint 写成 JSON-Schema 方言时,合法的 `{"ok": true}` 与 hint 顶层键零交集,
    整批 97 格全部 `missing_expected_key`(2026-09-11 本机实跑)。
    """
    from app.core.model_json import (
        ModelJsonRepairError, parse_model_json_object, validate_model_json_shape,
    )

    for content in ('{"ok": true}', '{"ok": false}'):
        parsed = parse_model_json_object(content, PROBE_SCHEMA_HINT, allow_repair=False)
        validate_model_json_shape(parsed.content, PROBE_SCHEMA_HINT)
    with pytest.raises(ModelJsonRepairError):
        validate_model_json_shape('{"type": "object"}', PROBE_SCHEMA_HINT)
    with pytest.raises(ModelJsonRepairError):
        validate_model_json_shape("{}", PROBE_SCHEMA_HINT)
