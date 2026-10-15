from app.services.answer_preview import answer_preview


def pv(md, n=300):
    return answer_preview(md, n)[0]


def test_citation_markers_removed():
    assert pv("结论成立[k1]，并且[k2, k3]也是【k4】（k5）") == "结论成立，并且也是"


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
