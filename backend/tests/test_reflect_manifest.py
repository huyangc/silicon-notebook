"""运行 manifest 的纯构造(`app/eval/reflect_manifest.py`)。

计划真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
Q9(键集与落点)、T-EX1(用例 a-d)。评审后修正(ex1 修正轮):18 键闭集、
`matrix` 子键契约、全通道共同必填键——用例相应补齐(见各组注释)。

这里一次 I/O 都不做:`build_manifest` 只从调用方传入的关键字事实构造并校验一份
字典。三条通道各自的薄适配(`git rev-parse`、写文件)由各自 rig 子命令的测试
(T-EX4/T-EX7/T-EX8)覆盖。
"""
from __future__ import annotations

import re

import pytest

import app.eval.reflect_manifest as manifest
from app.eval.reflect_manifest import (
    CHANNELS,
    MANIFEST_KEYS,
    REQUIRED_KEYS_ALL_CHANNELS,
    REQUIRED_KEYS_BY_CHANNEL,
    REQUIRED_MATRIX_KEYS_BY_CHANNEL,
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
    """E3(真实自主循环 + 完整 Ask)一份最小合法事实集。"""
    facts = {
        "channel": "e3",
        "intent_contract_digest_by_question": {
            "q001": "0011223344556677",
            "q002": "8899aabbccddeeff",
        },
        "arm_order_seed": None,
        "matrix": {"questions": 2, "efforts": 2, "arms": 3, "repeats": 1},
        "code_sha": "deadbeef12345678",
        "started_at": "2026-09-11T00:00:00Z",
        "finished_at": "2026-09-11T00:10:00Z",
        "stopped_by_budget": False,
    }
    facts.update(overrides)
    return facts


_CHANNEL_BUILDERS = {"e1": _e1_facts, "e2": _e2_facts, "e3": _e3_facts}


# --- 用例 (a):三条通道各一份最小合法 manifest ------------------------------


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_minimal_manifest_is_legal_for_every_channel(channel):
    build_facts = _CHANNEL_BUILDERS[channel]
    row = build_manifest(**build_facts())
    assert row["channel"] == channel
    # 独立调用两道闸,确认它们对 `build_manifest` 已经接受的行不再挑刺。
    assert_manifest_closed(row)
    assert_manifest_values(row)


def test_build_manifest_returns_exactly_the_facts_it_was_given():
    facts = _e1_facts(code_sha="deadbeef12345678")
    row = build_manifest(**facts)
    assert row == facts


# --- 用例 (b):每条通道各缺一个必填键(含全通道共同必填) ⇒ 逐格参数化报错 -----


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
    with pytest.raises(ValueError, match="missing required key"):
        build_manifest(**facts)


@pytest.mark.parametrize(
    "channel, missing_key",
    [("e1", "code_sha"), ("e2", "started_at"), ("e3", "stopped_by_budget")],
)
def test_missing_a_common_required_key_is_refused_per_channel(channel, missing_key):
    # F3 修正:全通道共同必填(`code_sha`/时间戳/`stopped_by_budget`)不是某条
    # 通道自己的必填表能覆盖到的——各通道各挑一个共同必填键单独钉一格。
    facts = _CHANNEL_BUILDERS[channel]()
    del facts[missing_key]
    with pytest.raises(ValueError, match="missing required key"):
        build_manifest(**facts)


def test_every_channel_has_at_least_one_required_key_declared():
    # 挡住「哪天顺手把某条通道的必填表清空了」——那样 (b) 那组参数化用例会悄悄
    # 缩水到零格,看起来全绿,其实什么都没测。
    for channel in CHANNELS:
        assert REQUIRED_KEYS_BY_CHANNEL[channel]


def test_a_manifest_without_a_channel_is_refused():
    with pytest.raises(ValueError, match="channel"):
        build_manifest(seed=1, matrix={})


def test_an_unknown_channel_is_refused():
    with pytest.raises(ValueError, match="channel"):
        build_manifest(channel="e4", seed=1, matrix={})


# --- 用例 (b'):`matrix` 子键契约(F2 修正) -----------------------------------


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_matrix_missing_its_channel_subkey_is_refused(channel):
    facts = _CHANNEL_BUILDERS[channel]()
    matrix = dict(facts["matrix"])
    subkey = sorted(REQUIRED_MATRIX_KEYS_BY_CHANNEL[channel])[0]
    del matrix[subkey]
    facts["matrix"] = matrix
    with pytest.raises(ValueError, match="matrix"):
        build_manifest(**facts)


@pytest.mark.parametrize("channel", CHANNELS)
def test_an_empty_matrix_is_refused(channel):
    facts = _CHANNEL_BUILDERS[channel](matrix={})
    with pytest.raises(ValueError, match="matrix"):
        build_manifest(**facts)


def test_every_channel_has_at_least_one_required_matrix_key_declared():
    # 同上,防止 `REQUIRED_MATRIX_KEYS_BY_CHANNEL` 哪天被顺手清空、上面两组用例
    # 悄悄失去意义。
    for channel in CHANNELS:
        assert REQUIRED_MATRIX_KEYS_BY_CHANNEL[channel]


# --- 用例 (c):隐私变异 ------------------------------------------------------


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
    with pytest.raises(ValueError):
        build_manifest(**facts)


def test_a_leaked_free_text_as_a_dict_key_in_budgets_is_refused():
    # F4 修正:污染点落在字典**键**上(而不是值上)同样必须被拒——删掉
    # `_assert_manifest_value` 里的键判据会让这一格从红变绿。
    facts = _e1_facts(budgets={"这是一段题面原文,不许进 manifest": 1})
    with pytest.raises(ValueError):
        build_manifest(**facts)


def test_a_leaked_connection_string_nested_under_corpus_signature_is_refused():
    facts = _e2_facts(
        corpus_signature_by_cell={"A_nokg": "postgresql://user:pw@host/db"}
    )
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
        assert_manifest_values(
            {**row, "budgets": {"leaked": "postgresql://u:p@h/db"}}
        )


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


# --- 命名红线 ---------------------------------------------------------------


def test_no_manifest_key_names_a_cache_hit_rate():
    for key in MANIFEST_KEYS:
        assert not re.search(r"cache_hit|hit_rate|命中", key)


# --- 覆盖对账:必填表只用闭集内的键 ------------------------------------------


def test_required_keys_are_a_subset_of_manifest_keys():
    assert REQUIRED_KEYS_ALL_CHANNELS <= MANIFEST_KEYS
    for channel in CHANNELS:
        assert REQUIRED_KEYS_BY_CHANNEL[channel] <= MANIFEST_KEYS
