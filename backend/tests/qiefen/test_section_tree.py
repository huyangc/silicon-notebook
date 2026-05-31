from app.services.qiefen.models import SourceElementQ
from app.services.qiefen.section_tree import build_section_tree


def _heading(text, level, line):
    return SourceElementQ(id=f"H{line}", type="heading", file="x.md",
                          line_start=line, line_end=line, char_start=0,
                          char_end=1, text=("#" * level) + " " + text,
                          metadata={"level": level})


def test_breadcrumb_paths():
    els = [
        _heading("2. Architecture", 1, 1),
        _heading("2.2 Sparse Retrieval", 2, 2),
        _heading("3. Scaling Laws", 1, 3),
    ]
    nodes = build_section_tree(els)
    paths = [n.path for n in nodes]
    assert "2. Architecture" in paths
    assert "2. Architecture > 2.2 Sparse Retrieval" in paths
    assert "3. Scaling Laws" in paths


def test_single_heading_no_levels():
    els = [_heading("Abstract", 1, 9)]
    nodes = build_section_tree(els)
    assert nodes[0].path == "Abstract"
    assert nodes[0].title == "Abstract"
