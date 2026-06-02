from app.services.kg.windowing import make_windows, windows_with_elements

def test_windows_cover_with_overlap_and_section():
    # 25-char "sections": one heading + body
    text = "# 1 Intro\n\n" + ("A" * 50) + "\n\n# 2 Body\n\n" + ("B" * 50) + "\n"
    wins = make_windows(text, source_file="x.md", line_range=None, n=30, m=6)
    assert wins, "expected windows"
    # every window is a contiguous source slice with a section path
    for w in wins:
        assert 0 <= w.char_start < w.char_end <= len(text)
        assert w.section_path
    # overlap: consecutive windows in the same section overlap by ~m
    same = [w for w in wins if w.section_path == wins[0].section_path]
    if len(same) >= 2:
        assert same[1].char_start < same[0].char_end


def test_windows_with_elements_pairs_prose_within_window():
    text = ("# 1 Intro\n\n" + ("Alpha beta gamma. " * 4)
            + "\n\n# 2 Body\n\n" + ("Delta epsilon zeta. " * 4) + "\n")
    pairs = windows_with_elements(text, source_file="x.md", line_range=None,
                                  n=9000, m=450)
    assert pairs, "expected window/element pairs"
    sections = {w.section_path for w, _ in pairs}
    assert len(sections) >= 2, "expected >=1 window per section"
    for w, els in pairs:
        assert els, "each window should carry non-empty prose elements"
        # every element overlaps the window span and is sorted by char_start
        starts = [e.char_start for e in els]
        assert starts == sorted(starts)
        for e in els:
            assert e.char_start < w.char_end and e.char_end > w.char_start
