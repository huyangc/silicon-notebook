"""Task 5 (knowhow-tables PR-1): textops — pure, zero-LLM text transforms the
deterministic projector (Task 5's KnowhowProjector) relies on for machine-side
output: stripping images to a placeholder, and turning a cell's markdown into
structured steps / a deduped tool-name list.
See docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 5.
"""
from __future__ import annotations

from app.services.knowhow.textops import parse_steps, split_tools, strip_images


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
