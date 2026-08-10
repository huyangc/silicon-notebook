from app.services.kg.windowing import make_windows, windows_with_elements


def test_many_tiny_sections_are_merged():
    md = "".join(f"## S{i}\n\nshort sentence number {i}.\n\n" for i in range(20))
    wins = make_windows(md, "doc.md", None, n=9000, m=450)
    assert 1 <= len(wins) <= 3, f"碎小节应被合并, got {len(wins)}"


def test_oversized_section_is_split_with_overlap():
    big = "word " * 4000
    md = f"## Big\n\n{big}\n"
    wins = make_windows(md, "doc.md", None, n=9000, m=450)
    assert len(wins) >= 2
    assert wins[1].char_start < wins[0].char_end


def test_two_small_sections_still_two_windows_when_target_tiny():
    text = "# A\n\nEngram is a memory architecture\n\n# B\n\nEngram is a memory architecture indeed.\n\n"
    pairs = windows_with_elements(text, "doc.md", None, 40, 5)
    assert len(pairs) >= 2


def test_windows_pair_with_overlapping_prose():
    md = "## S\n\n" + ("alpha " * 2000) + "\n"
    pairs = windows_with_elements(md, "doc.md", None, n=9000, m=450)
    assert all(els for _, els in pairs)


def test_captioned_oversized_data_uri_image_does_not_explode_window_count():
    """有 caption 的超大内嵌 data URI 图片：figure_caption 元素在源 markdown 里
    的原始跨度仍是几十万字符（base64 payload 本身），但 kg/parsing.py 已把该
    元素的 char_end 收缩到 caption 自身长度（不改 windowing.py）。收缩前，
    make_windows 会把这个"跨度巨大、内容极小"的单个元素按 n 重新切成几十个
    窗口，每个窗口对同一句 caption 各发一次 kg_extract 调用，纯属浪费；收缩
    后窗口数应回到与图片跨度无关的小常数。

    caption 长度特意取 > m（重叠量 450）：这是为了绕开 windowing.py 既有的
    "短尾元素被重叠逻辑再次配对进下一个窗口"通用行为——该行为对任何短的
    尾部元素都会触发（用纯段落验证过，与本次 image 修复无关），不是本次
    要修的问题，也不应因为选了个巧合的短 caption 而在这条新测试里误触发。
    """
    caption = "a" * 500
    huge_b64 = "A" * 100_000
    md = (
        "intro paragraph before the figure. " * 5 + "\n\n"
        + f"![{caption}](data:image/png;base64,{huge_b64})\n\n"
        + "closing paragraph after the figure. " * 5
    )

    wins = make_windows(md, "doc.md", None, n=9000, m=450)
    assert len(wins) == 2, f"caption 收缩后窗口数应是小常数, got {len(wins)}"

    pairs = windows_with_elements(md, "doc.md", None, n=9000, m=450)
    caption_pairings = sum(
        1 for _, els in pairs if any(e.type == "figure_caption" for e in els)
    )
    assert caption_pairings == 1, (
        f"同一 figure_caption 元素不该被配对进多个窗口, got {caption_pairings}"
    )
