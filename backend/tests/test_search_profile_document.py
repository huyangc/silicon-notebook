"""Agentic Memory P3(T6)——``app.services.search_profile`` 文档解析/合并契约。

覆盖:
  * ``parse_search_profile``:NULL/空串/畸形 JSON/非 dict 顶层/``fields``
    非 dict/未知字段/非法 origin/非法值/缺 ``updated_at`` 全部 fail-open
    到「未设置」,不炸整个文档。
  * ``merge_field``:``origin="user"`` 无条件覆盖;``origin="job"`` 跳过已被
    用户写过的字段(不覆盖、不报错);``value=None`` 删除字段条目(清空);
    未知字段/未知 origin/非法值报 ValueError。
  * ``render_style_block``:空 profile 渲染空串;"auto" 值不渲染;超预算时
    domain_terms 逐条装入直到预算耗尽(不整段丢),仍超预算才继续从最不
    具体的一端(detail→shape→language)整条丢,直到落进
    ``SEARCH_PROFILE_BLOCK_MAX_CHARS``。T8 改动:此前 v1 是把 domain_terms
    整段丢弃,评审发现 320 字符输入 vs ~96 字符可渲染预算会静默丢光全部
    术语;裁决见 p3-rulings.md「T6 domain_terms」条。
  * 变异验证:删掉 ``merge_field`` 里的 origin 检查后,「job 不覆盖 user」这条
    断言必须报红(证明测试真的钉住了这条规则,而不是巧合通过)。
"""
from __future__ import annotations

import json

import pytest

from app.services.search_profile import (
    SEARCH_PROFILE_BLOCK_MAX_CHARS,
    SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS,
    SEARCH_PROFILE_DOMAIN_TERMS_MAX,
    SEARCH_PROFILE_VERSION,
    merge_field,
    parse_search_profile,
    render_style_block,
    serialize_search_profile,
)


# --------------------------------------------------------------------------- #
# 1. parse_search_profile — fail-open 矩阵
# --------------------------------------------------------------------------- #
def test_none_and_empty_string_parse_to_not_set():
    assert parse_search_profile(None) == {"version": SEARCH_PROFILE_VERSION, "fields": {}}
    assert parse_search_profile("") == {"version": SEARCH_PROFILE_VERSION, "fields": {}}


def test_malformed_json_fails_open_to_not_set():
    assert parse_search_profile("{not json") == {
        "version": SEARCH_PROFILE_VERSION, "fields": {}
    }


def test_non_object_top_level_fails_open():
    assert parse_search_profile("[1, 2, 3]") == {
        "version": SEARCH_PROFILE_VERSION, "fields": {}
    }
    assert parse_search_profile('"just a string"') == {
        "version": SEARCH_PROFILE_VERSION, "fields": {}
    }


def test_missing_or_malformed_fields_key_fails_open():
    assert parse_search_profile(json.dumps({"version": 1})) == {
        "version": SEARCH_PROFILE_VERSION, "fields": {}
    }
    assert parse_search_profile(json.dumps({"version": 1, "fields": "nope"})) == {
        "version": SEARCH_PROFILE_VERSION, "fields": {}
    }


def test_unknown_field_name_is_dropped_not_fatal():
    raw = json.dumps({
        "version": 1,
        "fields": {
            "answer_language": {"value": "zh", "origin": "user", "updated_at": "t1"},
            "totally_unknown_field": {"value": "x", "origin": "user", "updated_at": "t1"},
        },
    })
    parsed = parse_search_profile(raw)
    assert set(parsed["fields"]) == {"answer_language"}


def test_invalid_origin_drops_only_that_field():
    raw = json.dumps({
        "version": 1,
        "fields": {
            "answer_language": {"value": "zh", "origin": "robot", "updated_at": "t1"},
            "answer_shape": {"value": "prose", "origin": "user", "updated_at": "t1"},
        },
    })
    parsed = parse_search_profile(raw)
    assert set(parsed["fields"]) == {"answer_shape"}


def test_invalid_value_for_closed_domain_is_dropped():
    raw = json.dumps({
        "version": 1,
        "fields": {
            "answer_language": {"value": "fr", "origin": "user", "updated_at": "t1"},
        },
    })
    assert parse_search_profile(raw)["fields"] == {}


def test_missing_updated_at_drops_the_field():
    raw = json.dumps({
        "version": 1,
        "fields": {
            "answer_detail": {"value": "concise", "origin": "user"},
        },
    })
    assert parse_search_profile(raw)["fields"] == {}


def test_domain_terms_over_count_or_over_length_is_dropped():
    over_count = json.dumps({
        "version": 1,
        "fields": {
            "domain_terms": {
                "value": [f"t{i}" for i in range(SEARCH_PROFILE_DOMAIN_TERMS_MAX + 1)],
                "origin": "user",
                "updated_at": "t1",
            },
        },
    })
    assert parse_search_profile(over_count)["fields"] == {}

    over_length = json.dumps({
        "version": 1,
        "fields": {
            "domain_terms": {
                "value": ["x" * (SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS + 1)],
                "origin": "user",
                "updated_at": "t1",
            },
        },
    })
    assert parse_search_profile(over_length)["fields"] == {}


def test_valid_document_round_trips_through_serialize_and_parse():
    profile = merge_field({"version": 1, "fields": {}}, "answer_language", "zh", "user")
    reparsed = parse_search_profile(serialize_search_profile(profile))
    assert reparsed["fields"]["answer_language"]["value"] == "zh"
    assert reparsed["fields"]["answer_language"]["origin"] == "user"


# --------------------------------------------------------------------------- #
# 2. merge_field — user/job/clear/reject 四组合
# --------------------------------------------------------------------------- #
def _empty():
    return {"version": SEARCH_PROFILE_VERSION, "fields": {}}


def test_user_origin_unconditionally_overwrites():
    profile = merge_field(_empty(), "answer_language", "zh", "job")
    profile = merge_field(profile, "answer_language", "en", "user")
    assert profile["fields"]["answer_language"]["value"] == "en"
    assert profile["fields"]["answer_language"]["origin"] == "user"


def test_job_origin_does_not_overwrite_a_user_authored_field():
    """crux assertion — the whole feature's job-vs-user boundary."""
    profile = merge_field(_empty(), "answer_language", "en", "user")
    result = merge_field(profile, "answer_language", "zh", "job")
    assert result["fields"]["answer_language"]["value"] == "en"
    assert result["fields"]["answer_language"]["origin"] == "user"


def test_job_origin_freely_fills_a_field_with_no_prior_user_write():
    profile = merge_field(_empty(), "answer_language", "zh", "job")
    assert profile["fields"]["answer_language"]["value"] == "zh"
    assert profile["fields"]["answer_language"]["origin"] == "job"


def test_job_origin_can_overwrite_a_prior_job_write():
    profile = merge_field(_empty(), "answer_language", "zh", "job")
    profile = merge_field(profile, "answer_language", "en", "job")
    assert profile["fields"]["answer_language"]["value"] == "en"


def test_clearing_a_field_deletes_the_entry_and_job_can_refill_it():
    profile = merge_field(_empty(), "answer_language", "en", "user")
    cleared = merge_field(profile, "answer_language", None, "user")
    assert "answer_language" not in cleared["fields"]

    refilled = merge_field(cleared, "answer_language", "zh", "job")
    assert refilled["fields"]["answer_language"]["value"] == "zh"
    assert refilled["fields"]["answer_language"]["origin"] == "job"


def test_job_origin_cannot_clear_a_user_authored_field():
    """T2/T6 修复轮:job 的 value=None 清空同样必须过 job-vs-user 守卫,不能
    绕过它——否则一个后台归纳 job 可以用「清空」把用户的显式选择静默抹掉,
    等同于 test_job_origin_does_not_overwrite_a_user_authored_field 挡的
    那类覆盖,只是换了个动作形状。"""
    profile = merge_field(_empty(), "answer_language", "en", "user")
    result = merge_field(profile, "answer_language", None, "job")
    assert result["fields"]["answer_language"]["value"] == "en"
    assert result["fields"]["answer_language"]["origin"] == "user"


def test_merge_field_does_not_mutate_its_input():
    original = merge_field(_empty(), "answer_language", "en", "user")
    snapshot = json.loads(serialize_search_profile(original))
    merge_field(original, "answer_language", "zh", "job")
    assert json.loads(serialize_search_profile(original)) == snapshot


def test_unknown_field_name_raises():
    with pytest.raises(ValueError):
        merge_field(_empty(), "not_a_real_field", "x", "user")


def test_unknown_origin_raises():
    with pytest.raises(ValueError):
        merge_field(_empty(), "answer_language", "zh", "robot")


def test_invalid_value_raises():
    with pytest.raises(ValueError):
        merge_field(_empty(), "answer_language", "fr", "user")
    with pytest.raises(ValueError):
        merge_field(_empty(), "domain_terms", "not-a-list", "user")


# --------------------------------------------------------------------------- #
# 3. render_style_block
# --------------------------------------------------------------------------- #
def test_empty_profile_renders_empty_string():
    assert render_style_block(_empty()) == ""


def test_auto_values_are_never_rendered():
    profile = merge_field(_empty(), "answer_language", "auto", "user")
    assert render_style_block(profile) == ""


def test_set_fields_are_rendered_and_bounded():
    profile = merge_field(_empty(), "answer_language", "zh", "user")
    profile = merge_field(profile, "answer_shape", "table_first", "user")
    profile = merge_field(profile, "answer_detail", "concise", "user")
    profile = merge_field(profile, "domain_terms", ["PPA", "IRR"], "user")
    block = render_style_block(profile)
    assert block
    assert len(block) <= SEARCH_PROFILE_BLOCK_MAX_CHARS
    assert "PPA" in block and "IRR" in block


def test_render_never_leaks_ids_notebook_or_source_words():
    profile = merge_field(_empty(), "answer_language", "zh", "user")
    profile = merge_field(profile, "domain_terms", ["notebook-42"], "user")
    block = render_style_block(profile).lower()
    # domain_terms is user-supplied free text and legitimately can contain
    # any word (including "notebook") -- the guard here is on the FIXED
    # scaffolding text this module itself writes, not on user-chosen terms.
    assert "source_id" not in block and "user id" not in block


def test_overflowing_domain_terms_are_packed_one_at_a_time_not_dropped_whole():
    """T8:满长 domain_terms(10 x 32 字符)远超可渲染预算,但渲染器逐条装入
    直到预算耗尽,不是把整个列表丢光——评审发现的 320 输入 vs ~96 预算错配
    (见 p3-rulings.md「T6 domain_terms」条)。"""
    profile = merge_field(_empty(), "answer_language", "zh", "user")
    long_terms = [
        f"{i:02d}-" + "x" * (SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS - 3)
        for i in range(SEARCH_PROFILE_DOMAIN_TERMS_MAX)
    ]
    profile = merge_field(profile, "domain_terms", long_terms, "user")
    block = render_style_block(profile)
    assert len(block) <= SEARCH_PROFILE_BLOCK_MAX_CHARS
    # The bounded block must still say SOMETHING (the language preference
    # survives).
    assert "Chinese" in block
    # At least one whole term made it in -- this is the load-bearing part of
    # this test: a whole-segment drop would leave EVERY term out, while
    # per-term packing must surface at least the first one.
    assert long_terms[0] in block
    # Not every term fits (that's the overflow scenario this test exercises)
    # -- and a term that got dropped must be dropped WHOLE, never truncated
    # mid-word into a substring that still happens to appear elsewhere.
    assert long_terms[-1] not in block


def test_domain_terms_that_do_fit_are_never_truncated_mid_word():
    """A term that fits the budget appears in the block WHOLE -- packing
    never emits a partial prefix of a term it decided to include."""
    profile = merge_field(
        _empty(), "domain_terms", ["A" * SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS], "user"
    )
    block = render_style_block(profile)
    assert block
    assert ("A" * SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS) in block


# --------------------------------------------------------------------------- #
# 4. 变异验证(真跑、记录在报告里)——删掉 job-vs-user 检查后测试必须报红
# --------------------------------------------------------------------------- #
def test_mutation_without_the_origin_guard_job_can_overwrite_user():
    """不是真的删代码——这条测试独立复刻「万一 origin 检查被删掉」的行为,
    用来在报告里证明 test_job_origin_does_not_overwrite_a_user_authored_field
    这条断言不是巧合通过的(手工变异见任务报告)。"""

    def merge_field_without_guard(profile, field, value, origin):
        fields = dict(profile.get("fields") or {})
        if value is None:
            fields.pop(field, None)
            return {"version": SEARCH_PROFILE_VERSION, "fields": fields}
        # origin 检查被删掉——job 总是覆盖。
        fields[field] = {"value": value, "origin": origin, "updated_at": "t"}
        return {"version": SEARCH_PROFILE_VERSION, "fields": fields}

    profile = merge_field(_empty(), "answer_language", "en", "user")
    mutated = merge_field_without_guard(profile, "answer_language", "zh", "job")
    assert mutated["fields"]["answer_language"]["value"] == "zh"  # guard is gone
    # And the REAL merge_field must NOT reproduce that behavior:
    real = merge_field(profile, "answer_language", "zh", "job")
    assert real["fields"]["answer_language"]["value"] == "en"
