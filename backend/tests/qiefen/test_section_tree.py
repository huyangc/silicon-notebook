from app.services.qiefen.models import SourceElementQ
from app.services.qiefen.section_tree import build_section_tree


def _heading(text, level, line):
    return SourceElementQ(id=f"H{line}", type="heading", file="x.md",
                          line_start=line, line_end=line, char_start=0,
                          char_end=1, text=("#" * level) + " " + text,
                          metadata={"level": level})


def test_numeric_breadcrumb_paths():
    # engram-style: trailing dot after the number, nesting from the label.
    els = [
        _heading("2. Architecture", 1, 1),
        _heading("2.1. Overview", 1, 2),
        _heading("2.2. Sparse Retrieval via Hashed N-grams", 1, 3),
        _heading("3. Scaling Laws", 1, 4),
    ]
    paths = [n.path for n in build_section_tree(els)]
    assert paths == ["2", "2 > 2.1", "2 > 2.2", "3"]


def test_chapter_word_stripped_and_unnumbered_attaches_to_chain():
    # cmos-style: "Chapter 1 ..." -> "1"; PROBLEMS (unnumbered) -> current chain.
    els = [
        _heading("Chapter 1 Introduction and Background", 1, 1),
        _heading("1.1 Analog Integrated Circuit Design", 1, 2),
        _heading("1.5 Summary", 1, 3),
        _heading("PROBLEMS", 1, 4),
    ]
    paths = [n.path for n in build_section_tree(els)]
    assert paths[0] == "1"
    assert paths[1] == "1 > 1.1"
    assert paths[2] == "1 > 1.5"
    assert paths[3].startswith("1 ")
    assert paths[3].endswith("PROBLEMS")


def test_single_unnumbered_heading_is_bare_title():
    nodes = build_section_tree([_heading("Abstract", 1, 9)])
    assert nodes[0].path == "Abstract"
    assert nodes[0].title == "Abstract"
