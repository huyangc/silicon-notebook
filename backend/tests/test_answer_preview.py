from app.services.answer_preview import answer_preview


def pv(md, n=300):
    return answer_preview(md, n)[0]


def test_citation_markers_removed():
    assert pv("结论成立[k1]，并且[k2, k3]也是【k4】（k5）") == "结论成立，并且也是"


def test_citation_marker_takes_the_blank_before_it():
    assert pv("It exists [k1]. Next [k2][k3], then [k4] more") == "It exists. Next, then more"


def test_heading_emphasis_code_unwrapped():
    out = pv("# 标题\n\n**粗** 和 *斜* 与 ~~删~~ 以及 `code` 和 __b__ _i_")
    assert out == "标题\n粗 和 斜 与 删 以及 code 和 b i"


def test_lists_become_bullets():
    assert pv("- 甲\n* 乙\n1. 丙\n2) 丁") == "• 甲\n• 乙\n• 丙\n• 丁"


def test_links_keep_text_images_dropped():
    out = pv("见[官网](http://x.com/a)与![图](http://x.com/i.png)结束")
    assert out == "见官网与结束"


def test_table_block_replaced_once():
    md = "前\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n后"
    assert pv(md) == "前\n（表格，见链接）\n后"


def test_display_math_replaced_inline_math_kept():
    md = "行内 $x+1$ 保留\n\n$$\nE=mc^2\n$$\n\n\\[a^2\\]\n\n尾"
    assert pv(md) == "行内 x+1 保留\n（公式，见链接）\n（公式，见链接）\n尾"


def test_fenced_code_kept():
    assert pv("先\n```python\nprint(1)\n  x = 2\n```\n后") == "先\nprint(1)\n  x = 2\n后"


def test_html_stripped():
    assert pv("a<br/>b <span class=\"x\">c</span><!-- hi -->d") == "ab cd"


def test_truncates_at_sentence_end_and_marks():
    text, trunc = answer_preview("第一句。第二句。第三句很长很长很长", 10)
    assert trunc is True
    assert text == "第一句。第二句。…"
    assert len(text) <= 10


def test_truncates_at_paragraph_boundary():
    text, trunc = answer_preview("甲甲甲甲\n\n乙乙乙乙乙乙乙乙", 8)
    assert (text, trunc) == ("甲甲甲甲…", True)


def test_hard_cut_when_no_boundary():
    text, trunc = answer_preview("a" * 100, 50)
    assert trunc is True
    assert text == "a" * 49 + "…"


def test_counts_code_points_not_bytes():
    s = "中😀" * 10  # 20 code points, 40+ bytes
    assert answer_preview(s, 20) == (s, False)
    text, trunc = answer_preview(s, 19)
    assert trunc is True and len(text) == 19 and text.endswith("…")


def test_short_answer_not_truncated():
    assert answer_preview("很短。", 300) == ("很短。", False)


def test_empty():
    assert answer_preview("", 300) == ("", False)
    assert answer_preview("  \n\n ", 300) == ("", False)
    assert answer_preview("![x](y)[k1]", 300) == ("", False)


def test_angle_brackets_in_code_and_math_survive_html_strip():
    assert pv("用 `Vec<T>` 与 `a<b>c`") == "用 Vec<T> 与 a<b>c"
    assert pv("$a<b$ 且 $c>d$ 还有<b>粗</b>") == "a<b 且 c>d 还有粗"


def test_unclosed_display_math_is_plain_text():
    text, trunc = answer_preview("前文\n\n$$\nE=mc^2\n\n后文保留", 300)
    assert "后文保留" in text and "（公式，见链接）" not in text and trunc is False
    assert "后文保留" in pv("\\[ x\n\n后文保留")


def test_decimal_and_url_dots_are_not_sentence_ends():
    text, trunc = answer_preview("圆周率约为3.14159265358979323846264338327950288", 20)
    assert trunc is True and text != "圆周率约为3.…" and text.endswith("…")
    text, _ = answer_preview("see http://a.b.c/dd/ee/ff/gg/hh/ii/jj/kk", 25)
    assert text == "see http://a.b.c/dd/ee/f…"
    text, _ = answer_preview("All work is done. Next sentence goes on", 25)
    assert text == "All work is done.…"  # sentence end followed by whitespace still counts


def test_boundary_shorter_than_half_budget_hard_cuts():
    text, trunc = answer_preview("好。" + "字" * 100, 40)
    assert trunc is True and text == "好。" + "字" * 37 + "…"


def test_pathological_inputs_are_fast_and_bounded():
    import time

    cases = [
        "[" * 200_000,
        "![" * 100_000,
        "# " + "#" * 200_000 + " x",
        "**a" * 70_000,
        "`a" * 100_000,
        "<!--" * 50_000,
        ("a|b\n" + "-" * 50) * 5_000,
        "$$\n" * 70_000,
        "\\[\n" * 70_000,
        "x\n" * 100_000,
    ]
    for md in cases:
        t0 = time.perf_counter()
        text, _ = answer_preview(md, 300)
        assert time.perf_counter() - t0 < 0.2, md[:10]
        assert len(text) <= 300


def test_clipped_long_line_is_marked_truncated():
    text, trunc = answer_preview("a" * 5000, 3000)
    assert trunc is True and text.endswith("…") and len(text) <= 3000


def test_fence_close_needs_same_char_and_length():
    md = "````\nin\n```\nstill\n````\n后"
    assert pv(md) == "in\n```\nstill\n后"
    assert pv("~~~\na\n```\nb\n~~~\nc") == "a\n```\nb\nc"


def test_unclosed_code_block_keeps_rest():
    assert pv("```\na\nb") == "a\nb"


def test_pipe_line_without_separator_row_is_not_table():
    assert pv("a | b\n-\nc") == "a | b\n-\nc"
    assert "（表格" not in pv("a | b\nc | d")
    assert pv("x | y\n--|--\n1 | 2") == "（表格，见链接）"


def test_crlf_and_cr_line_endings():
    assert pv("# 标题\r\n\r\n- 甲\r\n- 乙\r\n") == "标题\n• 甲\n• 乙"
    assert pv("甲\r乙") == "甲\n乙"


def test_nested_lists():
    assert pv("- a\n  - b\n    1. c") == "• a\n• b\n• c"


def test_tiny_max_chars():
    assert answer_preview("你好世界", 1) == ("…", True)
    assert answer_preview("你", 1) == ("你", False)
    text, trunc = answer_preview("甲。" * 100, 40)
    assert trunc is True and len(text) <= 40 and text.endswith("…")
    assert answer_preview("abc", 0) == ("", True)


def test_early_stop_with_unread_input_reports_truncated():
    text, trunc = answer_preview("a" * 20 + "\n" + "b" * 19 + "\n" + "c" * 10, 40)
    assert trunc is True and text.endswith("…") and len(text) <= 40


def test_early_stop_inside_code_block_reports_truncated():
    md = "```\n" + "x   \n" * 12 + "```\n尾巴"
    text, trunc = answer_preview(md, 40)
    # trailing spaces no longer inflate the read budget: everything fits, nothing lost
    assert (text, trunc) == ("x\n" * 12 + "尾巴", False)
    text, trunc = answer_preview("```\n" + "x   \n" * 30 + "```\n尾巴", 40)
    assert trunc is True and len(text) <= 40


def test_trailing_fence_only_is_not_unread_content():
    assert answer_preview("a" * 20 + "\n" + "b" * 19 + "\n```\n", 40) == ("a" * 20 + "\n" + "b" * 19, False)


def test_fenced_dollar_does_not_close_prose_math():
    md = "$$ 开头未闭合的正文段落\n重要结论一\n```latex\n$$\n```\n尾段"
    out = pv(md)
    assert "重要结论一" in out and "尾段" in out
    assert pv("\\[ 未闭合\n重要\n```\n\\]\n```\n尾") .count("重要") == 1
