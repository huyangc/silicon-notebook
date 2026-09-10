"""运行 manifest 的纯构造(`app/eval/reflect_manifest.py`)。

计划真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
Q9(键集与落点)、T-EX1(用例 a-d)。评审后修正(ex1 修正轮,两轮):18 键闭集、
`matrix` 子键契约、全通道共同必填键、E3 补 `corpus_signature_by_cell` 必填——
用例相应补齐(见各组注释)。

第二轮评审的根因是四张契约表全部被用例自身反读:`REQUIRED_KEYS_BY_CHANNEL` 等
常量既是生产码的判据来源,又是这里参数化用例的取值来源,砍键/改名/删子键
因此从不报红。这一轮补的第一件事就是把四张表逐字面量钉死(见「契约表字面量」
一节),让参数化仍然可以反读常量(参数化关心「每个当下声明的键都被单独测到」),
但常量本身的收缩/改名会被独立的字面量相等断言拦住。

这里一次 I/O 都不做:`build_manifest` 只从调用方传入的关键字事实构造并校验一份
字典。三条通道各自的薄适配(`git rev-parse`、写文件)由各自 rig 子命令的测试
(T-EX4/T-EX7/T-EX8)覆盖。
"""
from __future__ import annotations

import re

import pytest

import app.eval.reflect_manifest as manifest
from app.domain.reasoning_trace_stats import _SHORT_CODE_MAX_LEN
from app.eval.reflect_manifest import (
    CHANNELS,
    MANIFEST_KEYS,
    REQUIRED_KEYS_ALL_CHANNELS,
    REQUIRED_KEYS_BY_CHANNEL,
    REQUIRED_MATRIX_KEYS_BY_CHANNEL,
    assert_manifest,
    assert_manifest_closed,
    assert_manifest_values,
    build_manifest,
)


def _e1_facts(**overrides) -> dict:
    """E1(前缀敏感性探针)一份最小合法事实集。"""
    facts = {
        "channel": "e1",
        "seed": 20260911,
        "sample_digest": "a1b2c3d4e5f60718",
        "matrix": {"tiers": 3, "arms": 2, "blocks": 4, "calls_per_series": 4},
        "code_sha": "deadbeef12345678",
        "started_at": "2026-09-11T00:00:00Z",
        "finished_at": "2026-09-11T00:10:00Z",
        "stopped_by_budget": False,
    }
    facts.update(overrides)
    return facts


def _e2_facts(**overrides) -> dict:
    """E2(固定状态真实决策)一份最小合法事实集。"""
    facts = {
        "channel": "e2",
        "corpus_signature_by_cell": {
            "A_nokg": "1a2b3c4d5e6f7081",
            "B_kg": "9f8e7d6c5b4a3928",
        },
        "case_set_digest": "c0ffee1234567890",
        "matrix": {"cases": 12, "state_points": 3, "arms": 3, "repeats": 1},
        "code_sha": "deadbeef12345678",
        "started_at": "2026-09-11T00:00:00Z",
        "finished_at": "2026-09-11T00:10:00Z",
        "stopped_by_budget": False,
    }
    facts.update(overrides)
    return facts


def _e3_facts(**overrides) -> dict:
    """E3(真实自主循环 + 完整 Ask)一份最小合法事实集。

    `corpus_signature_by_cell` 是评审 P3-8/修正轮新加的必填键——`ab` 早就把它
    算好了,E3 的冻结 manifest 不该比 E2 少「跑在哪份语料上」这个事实。
    """
    facts = {
        "channel": "e3",
        "intent_contract_digest_by_question": {
            "q001": "0011223344556677",
            "q002": "8899aabbccddeeff",
        },
        "corpus_signature_by_cell": {
            "A_nokg": "1a2b3c4d5e6f7081",
            "B_kg": "9f8e7d6c5b4a3928",
        },
        "arm_order_seed": None,
        "matrix": {
            "questions": 2, "cells": 2, "efforts": 2, "arms": 3, "repeats": 1,
        },
        "code_sha": "deadbeef12345678",
        "started_at": "2026-09-11T00:00:00Z",
        "finished_at": "2026-09-11T00:10:00Z",
        "stopped_by_budget": False,
    }
    facts.update(overrides)
    return facts


_CHANNEL_BUILDERS = {"e1": _e1_facts, "e2": _e2_facts, "e3": _e3_facts}


# --- 契约表字面量:四张表逐字钉死 --------------------------------------------
#
# 这四格是第二轮评审的核心修正:没有它们,下面所有「反读常量」的参数化用例
# 都测不出常量本身被砍键/改名/删子键——常量收缩了,参数化也跟着收缩,看起来
# 全绿。仿照 `test_reasoning_trace_stats.py::test_the_sparse_detail_key_registry_matches_the_plan`
# 的写法:字面量摆在用例里,与生产码的字面量各自独立维护,任何一处漂移都会让
# 这四格里的某一格报红。


def test_manifest_keys_match_the_plan_literal():
    assert MANIFEST_KEYS == frozenset({
        "code_sha", "channel", "arms", "optimization_by_arm",
        "common_baseline", "corpus_signature_by_cell", "case_set_digest",
        "sample_digest", "intent_contract_digest_by_question",
        "model_contract", "seed", "arm_order_seed", "order", "matrix",
        "budgets", "started_at", "finished_at", "stopped_by_budget",
    })
    assert len(MANIFEST_KEYS) == 18


def test_required_keys_all_channels_match_the_plan_literal():
    assert REQUIRED_KEYS_ALL_CHANNELS == frozenset({
        "code_sha", "started_at", "finished_at", "stopped_by_budget",
    })


def test_required_keys_by_channel_match_the_plan_literal():
    assert REQUIRED_KEYS_BY_CHANNEL == {
        "e1": frozenset({"seed", "matrix", "sample_digest"}),
        "e2": frozenset(
            {"corpus_signature_by_cell", "matrix", "case_set_digest"}
        ),
        "e3": frozenset({
            "intent_contract_digest_by_question", "arm_order_seed", "matrix",
            "corpus_signature_by_cell",
        }),
    }


def test_required_matrix_keys_by_channel_match_the_plan_literal():
    assert REQUIRED_MATRIX_KEYS_BY_CHANNEL == {
        "e1": frozenset({"tiers", "blocks", "arms", "calls_per_series"}),
        "e2": frozenset({"cases", "state_points", "arms", "repeats"}),
        "e3": frozenset({"questions", "cells", "efforts", "arms", "repeats"}),
    }


# --- 用例 (a):三条通道各一份最小合法 manifest ------------------------------


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_minimal_manifest_is_legal_for_every_channel(channel):
    build_facts = _CHANNEL_BUILDERS[channel]
    row = build_manifest(**build_facts())
    assert row["channel"] == channel
    # 独立调用各道闸,确认它们对 `build_manifest` 已经接受的行不再挑刺。
    assert_manifest_closed(row)
    assert_manifest_values(row)
    assert_manifest(row)


def test_build_manifest_returns_exactly_the_facts_it_was_given():
    facts = _e1_facts(code_sha="deadbeef12345678")
    row = build_manifest(**facts)
    assert row == facts


def test_mutating_the_caller_dict_after_build_does_not_affect_the_manifest():
    # P3-5 修正:`build_manifest` 必须 `deepcopy`,不能只做浅拷贝——调用方常把
    # 自己手上还在累加的 `matrix`/`budgets` 字典原样传进来,校验完继续复用。
    caller_facts = _e1_facts()
    row = build_manifest(**caller_facts)
    caller_facts["matrix"]["tiers"] = 999
    caller_facts["budgets"] = {"max_wall_minutes": 1}
    assert row["matrix"]["tiers"] == 3
    assert "budgets" not in row


# --- 用例 (b):每条通道各缺一个必填键(含全通道共同必填) ⇒ 逐格参数化报错 -----
#
# 两组都做成 (channel, key) 的全覆盖笛卡尔积,而不是每条通道挑一个代表——
# 挑代表会让「共同必填表被砍到只剩代表键覆盖的那几个」逃逸(第二轮评审实测
# `REQUIRED_KEYS_ALL_CHANNELS` 删 `finished_at` 仍然全绿,因为三格代表用例
# 分别测的是 code_sha/started_at/stopped_by_budget,没有一格碰 finished_at)。
# 每一格还额外断言报错文里点名了被删的那个键,而不只是泛泛匹配
# "missing required key"——挡住「报错文案换了措辞但键名信息丢了」这种退化。


@pytest.mark.parametrize(
    "channel, missing_key",
    [
        (channel, key)
        for channel in CHANNELS
        for key in sorted(REQUIRED_KEYS_BY_CHANNEL[channel])
    ],
)
def test_missing_a_required_key_is_refused_per_channel(channel, missing_key):
    facts = _CHANNEL_BUILDERS[channel]()
    del facts[missing_key]
    with pytest.raises(
        ValueError,
        match=rf"missing required key\(s\): {re.escape(missing_key)}",
    ):
        build_manifest(**facts)


@pytest.mark.parametrize(
    "channel, missing_key",
    [
        (channel, key)
        for channel in CHANNELS
        for key in sorted(REQUIRED_KEYS_ALL_CHANNELS)
    ],
)
def test_missing_a_common_required_key_is_refused_per_channel(
    channel, missing_key
):
    # F3 修正:全通道共同必填(`code_sha`/时间戳/`stopped_by_budget`)不是某条
    # 通道自己的必填表能覆盖到的——每条通道 × 每个共同必填键都单独钉一格。
    facts = _CHANNEL_BUILDERS[channel]()
    del facts[missing_key]
    with pytest.raises(
        ValueError,
        match=rf"missing required key\(s\): {re.escape(missing_key)}",
    ):
        build_manifest(**facts)


def test_every_channel_has_at_least_one_required_key_declared():
    # 挡住「哪天顺手把某条通道的必填表清空了」——那样 (b) 那组参数化用例会悄悄
    # 缩水到零格,看起来全绿,其实什么都没测。这一格与上面的字面量断言互补:
    # 字面量断言挡「表被改成别的字面量」,这一格挡「表被清空」这种极端退化。
    for channel in CHANNELS:
        assert REQUIRED_KEYS_BY_CHANNEL[channel]


def test_a_manifest_without_a_channel_is_refused():
    with pytest.raises(ValueError, match="channel"):
        build_manifest(seed=1, matrix={})


def test_an_unknown_channel_is_refused():
    with pytest.raises(ValueError, match="channel"):
        build_manifest(channel="e4", seed=1, matrix={})


# --- 用例 (b'):`matrix` 子键契约(F2 修正,第二轮改成逐子键全覆盖) -----------


@pytest.mark.parametrize(
    "channel, subkey",
    [
        (channel, key)
        for channel in CHANNELS
        for key in sorted(REQUIRED_MATRIX_KEYS_BY_CHANNEL[channel])
    ],
)
def test_a_matrix_missing_its_channel_subkey_is_refused(channel, subkey):
    facts = _CHANNEL_BUILDERS[channel]()
    matrix = dict(facts["matrix"])
    del matrix[subkey]
    facts["matrix"] = matrix
    with pytest.raises(
        ValueError,
        match=rf"matrix' .* missing required key\(s\): {re.escape(subkey)}",
    ):
        build_manifest(**facts)


@pytest.mark.parametrize("channel", CHANNELS)
def test_an_empty_matrix_is_refused(channel):
    facts = _CHANNEL_BUILDERS[channel](matrix={})
    with pytest.raises(ValueError, match="non-empty mapping"):
        build_manifest(**facts)


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_non_mapping_matrix_is_refused(channel):
    # P3-4 修正:「非 Mapping」与「非空」是两条独立判据,各自要有用例挡住——
    # 把整条 `if not isinstance(matrix, Mapping) or not matrix` 换成
    # `if False:` 之前只有空字典这一格会红,`matrix=3` 这种非字典输入全绿。
    facts = _CHANNEL_BUILDERS[channel](matrix=3)
    with pytest.raises(ValueError, match="non-empty mapping"):
        build_manifest(**facts)


def test_every_channel_has_at_least_one_required_matrix_key_declared():
    # 同上,防止 `REQUIRED_MATRIX_KEYS_BY_CHANNEL` 哪天被顺手清空、上面几组
    # 用例悄悄失去意义。
    for channel in CHANNELS:
        assert REQUIRED_MATRIX_KEYS_BY_CHANNEL[channel]


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_planned_runs_extra_subkey_is_legal_on_every_channel(channel):
    """`planned_runs` 是三条通道共同的**额外**子键(T-EX8 评审 P1-2 拍板):
    基数相乘不总等于 run 数(E3 各格题集不相交 ⇒ `cells` 不是乘数),所以每条
    通道另写一个真实的 run 数上界。它不进 `REQUIRED_MATRIX_KEYS_BY_CHANNEL`
    (那张表钉的是维度基数),但必须过得了值形状闸——这一格钉住「`matrix` 允许
    额外子键」这条性质对它成立,免得日后有人把 `matrix` 收成闭集之后三条通道
    的对账键一起静默消失。
    """
    facts = _CHANNEL_BUILDERS[channel]()
    matrix = dict(facts["matrix"])
    matrix["planned_runs"] = 408
    facts["matrix"] = matrix
    row = build_manifest(**facts)
    assert row["matrix"]["planned_runs"] == 408
    assert_manifest(row)


# --- 用例 (c):隐私变异 ------------------------------------------------------
#
# 三格 poison 参数化 + 独立的字典键测试,`match` 收紧到"unsupported
# shape|non-short-code"这两条具体报错文——不能只用无 `match` 的
# `pytest.raises(ValueError)`。第二轮评审实测:把 `budgets` 从 `MANIFEST_KEYS`
# 里删掉之后,这些格捕到的是"manifest carries keys outside MANIFEST_KEYS:
# budgets"(闭集守卫先触发),值形状闸一次都没被执行,但因为断言不看错误文本
# 内容,照样全绿。收紧之后,闭集收缩导致的报错理由不再能冒充隐私报错。

_VALUE_SHAPE_ERROR = "unsupported shape|non-short-code"


@pytest.mark.parametrize(
    "poison",
    [
        "postgresql://user:pw@host/db",
        "http://internal.example.com/v1/secret",
        "这是一段题面原文,不许进 manifest",
    ],
)
def test_a_leaked_connection_string_or_free_text_in_budgets_is_refused(poison):
    facts = _e1_facts(budgets={"max_wall_minutes": 60, "leaked": poison})
    with pytest.raises(ValueError, match=_VALUE_SHAPE_ERROR):
        build_manifest(**facts)


def test_a_leaked_free_text_as_a_dict_key_in_budgets_is_refused():
    # F4 修正:污染点落在字典**键**上(而不是值上)同样必须被拒——删掉
    # `_assert_manifest_value` 里的键判据会让这一格从红变绿。
    facts = _e1_facts(budgets={"这是一段题面原文,不许进 manifest": 1})
    with pytest.raises(ValueError, match=_VALUE_SHAPE_ERROR):
        build_manifest(**facts)


def test_a_leaked_connection_string_nested_under_corpus_signature_is_refused():
    facts = _e2_facts(
        corpus_signature_by_cell={"A_nokg": "postgresql://user:pw@host/db"}
    )
    with pytest.raises(ValueError, match=_VALUE_SHAPE_ERROR):
        build_manifest(**facts)


def test_a_leaked_connection_string_nested_inside_a_list_is_refused():
    # P3-3 修正:列表分支不递归会让这一格从红变绿(`arms` 是列表值)。
    facts = _e1_facts(arms=["legacy:off", "postgresql://u:p@h/db"])
    with pytest.raises(ValueError, match=_VALUE_SHAPE_ERROR):
        build_manifest(**facts)


def test_a_leaked_free_text_dict_key_two_levels_deep_is_refused():
    # P3-3 修正:字典键判据只在最外层字典生效会让这一格从红变绿——污染点落在
    # `matrix.tiers` 这一层(两层深),而不是 `matrix` 本身。顶层四个必填子键
    # 仍然齐全,所以 `_assert_matrix_shape` 不会先一步拦下,污染必须靠值形状
    # 闸的递归才能被抓到。
    facts = _e1_facts(matrix={
        "tiers": {"题面原文": 1}, "arms": 2, "blocks": 4,
        "calls_per_series": 4,
    })
    with pytest.raises(ValueError, match=_VALUE_SHAPE_ERROR):
        build_manifest(**facts)


def test_a_key_outside_manifest_keys_is_refused():
    facts = _e1_facts(unexpected_key="nope")
    with pytest.raises(ValueError, match="MANIFEST_KEYS"):
        build_manifest(**facts)


def test_assert_manifest_closed_and_values_are_independently_callable():
    row = build_manifest(**_e1_facts())
    assert_manifest_closed(row)
    assert_manifest_values(row)
    with pytest.raises(ValueError, match="MANIFEST_KEYS"):
        assert_manifest_closed({**row, "extra": "x"})
    with pytest.raises(ValueError, match=_VALUE_SHAPE_ERROR):
        assert_manifest_values(
            {**row, "budgets": {"leaked": "postgresql://u:p@h/db"}}
        )


def test_assert_manifest_is_the_full_entry_point():
    # P3-5 修正:导出的全量入口一次串起闭集 + 值形状 + 通道必填 + matrix 子键,
    # 外部不用分别记住四道闸的调用顺序,也不用碰私有的 `_assert_matrix_shape`。
    row = build_manifest(**_e1_facts())
    assert_manifest(row)  # 已经合法的行不挑刺
    with pytest.raises(ValueError, match="MANIFEST_KEYS"):
        assert_manifest({**row, "extra": "x"})
    with pytest.raises(ValueError, match="missing required key"):
        assert_manifest({k: v for k, v in row.items() if k != "seed"})
    with pytest.raises(ValueError, match="matrix"):
        assert_manifest({**row, "matrix": {}})


# --- 用例 (d):`arm_order_seed=None` 合法,但键不可缺 -------------------------


def test_e3_allows_a_null_arm_order_seed():
    row = build_manifest(**_e3_facts(arm_order_seed=None))
    assert "arm_order_seed" in row
    assert row["arm_order_seed"] is None


def test_e3_refuses_a_missing_arm_order_seed_even_though_null_is_legal():
    facts = _e3_facts()
    del facts["arm_order_seed"]
    with pytest.raises(ValueError, match="arm_order_seed"):
        build_manifest(**facts)


# --- 短码尺子边界(P3-6) -----------------------------------------------------


def test_the_short_code_length_boundary_is_pinned_at_64_chars():
    # 用私有常量派生边界值(不重复写魔数 64 两遍),但断言的是字面量 64——
    # 挡住「共享尺子悄悄从 64 收紧/放宽,而这个模块的调用方(题键很容易超过
    # 一个更短的上限,比如 24)在真跑收尾处才炸」这种退化。
    assert _SHORT_CODE_MAX_LEN == 64
    ok = "a" * _SHORT_CODE_MAX_LEN
    too_long = "a" * (_SHORT_CODE_MAX_LEN + 1)
    row = build_manifest(**_e1_facts(sample_digest=ok))
    assert row["sample_digest"] == ok
    with pytest.raises(ValueError, match="unsupported shape"):
        build_manifest(**_e1_facts(sample_digest=too_long))


# --- 命名红线 ---------------------------------------------------------------


def test_no_manifest_key_names_a_cache_hit_rate():
    # P3-7 修正:同一条命名红线要多扫两张表的子键,不能只扫顶层
    # `MANIFEST_KEYS`——`REQUIRED_KEYS_BY_CHANNEL` / `REQUIRED_MATRIX_KEYS_BY_CHANNEL`
    # 里新增的键名同样受这条纪律约束。
    all_names: set[str] = set(MANIFEST_KEYS)
    for required in REQUIRED_KEYS_BY_CHANNEL.values():
        all_names |= required
    for matrix_keys in REQUIRED_MATRIX_KEYS_BY_CHANNEL.values():
        all_names |= matrix_keys
    for key in all_names:
        assert not re.search(r"cache_hit|hit_rate|命中", key)


# --- 覆盖对账:必填表只用闭集内的键 ------------------------------------------


def test_required_keys_are_a_subset_of_manifest_keys():
    assert REQUIRED_KEYS_ALL_CHANNELS <= MANIFEST_KEYS
    for channel in CHANNELS:
        assert REQUIRED_KEYS_BY_CHANNEL[channel] <= MANIFEST_KEYS
