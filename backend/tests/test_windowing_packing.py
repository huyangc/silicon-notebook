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
