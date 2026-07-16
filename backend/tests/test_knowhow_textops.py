"""Task 5 (knowhow-tables PR-1): textops — pure, zero-LLM text transforms the
deterministic projector (Task 5's KnowhowProjector) relies on for machine-side
output: stripping images to a placeholder, and turning a cell's markdown into
structured steps / a deduped tool-name list.
See docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 5.

Task 2 (knowhow-tables PR-2+3): three more pure transforms the cell-level
node-model projector needs — ``node_name``/``value_key`` (a KO's display name
and cross-row merge identity, design doc §④) and ``compose_row_title`` (the
synthesized row label for anchor-less/blank-anchor rows, design doc §①).
"""
from __future__ import annotations

from app.services.knowhow.textops import (
    compose_row_title,
    node_name,
    parse_steps,
    split_tools,
    strip_images,
    value_key,
)


# ---------------------------------------------------------------------------
# strip_images
# ---------------------------------------------------------------------------


def test_strip_images_replaces_image_with_alt_placeholder():
    assert strip_images("见下图：![示波器接线](asset://abc123) 所示") == (
        "见下图：（图示：示波器接线） 所示"
    )


def test_strip_images_with_empty_alt_uses_bare_placeholder():
    assert strip_images("![](asset://abc123)") == "（图示）"


def test_strip_images_replaces_multiple_images():
    md = "步骤一 ![截图A](asset://a) 步骤二 ![截图B](asset://b)"
    assert strip_images(md) == "步骤一 （图示：截图A） 步骤二 （图示：截图B）"


def test_strip_images_inline_within_sentence_leaves_surrounding_text_intact():
    md = "拆下面板 ![拆解示意](asset://x) 后即可看到芯片"
    assert strip_images(md) == "拆下面板 （图示：拆解示意） 后即可看到芯片"


def test_strip_images_no_image_returns_text_unchanged():
    assert strip_images("普通文本，没有图片。") == "普通文本，没有图片。"


def test_strip_images_empty_string():
    assert strip_images("") == ""


def test_strip_images_none_treated_as_empty():
    assert strip_images(None) == ""


# ---------------------------------------------------------------------------
# parse_steps
# ---------------------------------------------------------------------------


def test_parse_steps_ordered_list():
    md = "1. 关闭电源\n2. 拆下面板\n3. 更换电容"
    assert parse_steps(md) == ["关闭电源", "拆下面板", "更换电容"]


def test_parse_steps_ordered_list_with_paren_marker():
    md = "1) 关闭电源\n2) 拆下面板"
    assert parse_steps(md) == ["关闭电源", "拆下面板"]


def test_parse_steps_unordered_list_dash():
    md = "- 检查波形\n- 测量电压\n- 记录读数"
    assert parse_steps(md) == ["检查波形", "测量电压", "记录读数"]


def test_parse_steps_unordered_list_asterisk_and_plus():
    md = "* 第一项\n+ 第二项"
    assert parse_steps(md) == ["第一项", "第二项"]


def test_parse_steps_continuation_line_merges_into_previous_step():
    md = "1. 第一步\n   继续说明第一步\n2. 第二步"
    assert parse_steps(md) == ["第一步 继续说明第一步", "第二步"]


def test_parse_steps_blank_lines_between_items_are_ignored():
    md = "1. 第一步\n\n2. 第二步"
    assert parse_steps(md) == ["第一步", "第二步"]


def test_parse_steps_leading_prose_before_first_marker_is_dropped():
    md = "步骤如下：\n1. 第一步\n2. 第二步"
    assert parse_steps(md) == ["第一步", "第二步"]


def test_parse_steps_no_list_structure_returns_empty_list():
    md = "这是一段没有列表结构的正文说明，描述现象和处理方式。"
    assert parse_steps(md) == []


def test_parse_steps_empty_string_returns_empty_list():
    assert parse_steps("") == []


def test_parse_steps_none_returns_empty_list():
    assert parse_steps(None) == []


# ---------------------------------------------------------------------------
# split_tools
# ---------------------------------------------------------------------------


def test_split_tools_list_items():
    md = "- 示波器\n- 万用表\n- 逻辑分析仪"
    assert split_tools(md) == ["示波器", "万用表", "逻辑分析仪"]


def test_split_tools_ordered_list_items():
    md = "1. 示波器\n2. 万用表"
    assert split_tools(md) == ["示波器", "万用表"]


def test_split_tools_plain_newline_separated_no_markers():
    md = "示波器\n万用表\n逻辑分析仪"
    assert split_tools(md) == ["示波器", "万用表", "逻辑分析仪"]


def test_split_tools_dedup_key_is_casefold_keeps_first_seen_casing():
    md = "- Oscilloscope\n- oscilloscope\n- OSCILLOSCOPE\n- 万用表"
    assert split_tools(md) == ["Oscilloscope", "万用表"]


def test_split_tools_drops_empty_entries():
    md = "- 示波器\n-   \n\n- 万用表\n"
    assert split_tools(md) == ["示波器", "万用表"]


def test_split_tools_single_tool_no_list():
    assert split_tools("示波器") == ["示波器"]


def test_split_tools_empty_string_returns_empty_list():
    assert split_tools("") == []


def test_split_tools_none_returns_empty_list():
    assert split_tools(None) == []


# ---------------------------------------------------------------------------
# node_name (knowhow-tables PR-2+3 Task 2: a KO's display name — design doc
# §④ "名=格值首行截断")
# ---------------------------------------------------------------------------


def test_node_name_short_single_line_is_unchanged():
    assert node_name("过冲问题") == "过冲问题"


def test_node_name_takes_first_line_only():
    assert node_name("过冲问题\n第二行说明") == "过冲问题"


def test_node_name_strips_surrounding_whitespace_before_taking_first_line():
    assert node_name("\n\n  过冲问题  \n第二行") == "过冲问题"


def test_node_name_truncates_at_40_chars_with_ellipsis():
    text = "甲" * 45
    result = node_name(text)
    assert result == "甲" * 40 + "…"


def test_node_name_exactly_40_chars_no_ellipsis():
    text = "甲" * 40
    assert node_name(text) == text


def test_node_name_empty_string_returns_empty():
    assert node_name("") == ""


def test_node_name_none_returns_empty():
    assert node_name(None) == ""


def test_node_name_whitespace_only_returns_empty():
    assert node_name("   \n  ") == ""


# ---------------------------------------------------------------------------
# value_key (knowhow-tables PR-2+3 Task 2: cross-row merge identity — design
# doc §④ "同列同值跨行归并", KO id = sha1(table_id|column_name|value_key))
# ---------------------------------------------------------------------------


def test_value_key_casefolds():
    assert value_key("Oscilloscope") == value_key("oscilloscope") == value_key("OSCILLOSCOPE")


def test_value_key_ignores_surrounding_and_internal_whitespace():
    assert value_key("示波器") == value_key("  示波器  ") == value_key("示 波 器")


def test_value_key_ignores_punctuation():
    assert value_key("示波器") == value_key("示波器。") == value_key("示波器!")


def test_value_key_distinguishes_different_values():
    assert value_key("示波器") != value_key("万用表")


def test_value_key_short_value_is_the_normalized_text_itself():
    # <=80 chars after normalization -> the normalized string IS the key (no
    # hashing) so it stays human-legible for debugging/direct DB inspection.
    assert value_key("示波器") == "示波器"
    assert value_key("Oscilloscope") == "oscilloscope"


def test_value_key_long_value_hashes_to_32_hex_chars():
    long_text = "甲" * 200
    result = value_key(long_text)
    assert len(result) == 32
    assert all(c in "0123456789abcdef" for c in result)


def test_value_key_long_value_is_deterministic():
    long_text = "甲" * 200
    assert value_key(long_text) == value_key(long_text)


def test_value_key_long_value_normalizes_before_hashing():
    """Two long values differing only by whitespace/punctuation/case still
    collide to the same key — the >80 threshold is measured AFTER
    normalization, and normalization runs before the hash, not after."""
    base = "甲" * 90
    padded = " ".join(list("甲" * 90)) + "!!!"  # same content, lots of noise
    assert value_key(base) == value_key(padded)


def test_value_key_empty_string_returns_empty():
    assert value_key("") == ""


def test_value_key_none_returns_empty():
    assert value_key(None) == ""


# ---------------------------------------------------------------------------
# compose_row_title (knowhow-tables PR-2+3 Task 2: synthesized row label for
# anchor-less/blank-anchor rows — design doc §① "行标题自动合成")
# ---------------------------------------------------------------------------


def test_compose_row_title_joins_up_to_three_nonempty_cells_with_middle_dot():
    assert compose_row_title(["2026-01-01", "示波器记录", "正常"]) == (
        "2026-01-01 · 示波器记录 · 正常"
    )


def test_compose_row_title_skips_empty_cells():
    assert compose_row_title(["", "示波器记录", "", "正常"]) == "示波器记录 · 正常"


def test_compose_row_title_stops_after_three_segments():
    assert compose_row_title(["一", "二", "三", "四", "五"]) == "一 · 二 · 三"


def test_compose_row_title_takes_first_line_of_each_cell():
    assert compose_row_title(["标题行\n正文说明", "第二格"]) == "标题行 · 第二格"


def test_compose_row_title_truncates_each_segment_at_16_chars():
    long_segment = "甲" * 20
    result = compose_row_title([long_segment])
    assert result == "甲" * 16  # no ellipsis marker for this synthesized title


def test_compose_row_title_all_empty_cells_returns_empty_string():
    """The "行N" fallback is the CALLER's job (projection.py), not this
    function's — an all-empty input just yields "" here."""
    assert compose_row_title(["", "   ", ""]) == ""


def test_compose_row_title_empty_list_returns_empty_string():
    assert compose_row_title([]) == ""
