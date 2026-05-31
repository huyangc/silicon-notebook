from app.services.qiefen.models import SourceElementQ
from app.services.qiefen.atomizer import atomize


def test_sentence_atoms_have_verbatim_spans_and_types():
    src = ("Heading\n\n"
           "While MoE scales capacity, Transformers lack lookup. "
           "To address this, we introduce conditional memory via Engram.")
    # paragraph element covering the 3rd line region:
    start = src.index("While")
    el = SourceElementQ(id="SE1", type="paragraph", file="x.md",
                        line_start=3, line_end=3, char_start=start,
                        char_end=len(src), text=src[start:])
    atoms = atomize(src, [el], section_id="SEC", profile="article_research")
    assert len(atoms) == 2
    for a in atoms:
        assert src[a.source_span.char_start:a.source_span.char_end] == a.raw_text
    assert atoms[0].atom_type == "claim_sentence"
    assert atoms[1].atom_type == "method_sentence"  # "we introduce"


def test_formula_element_becomes_formula_atom():
    src = "$$ C_j = C_{j0} \\tag{2} $$"
    el = SourceElementQ(id="SE2", type="formula", file="x.md", line_start=1,
                        line_end=1, char_start=0, char_end=len(src), text=src)
    atoms = atomize(src, [el], section_id="SEC", profile="textbook")
    assert len(atoms) == 1
    assert atoms[0].atom_type == "formula_atom"
    assert src[atoms[0].source_span.char_start:atoms[0].source_span.char_end] \
        == atoms[0].raw_text


def test_table_splits_into_header_and_row_atoms_with_verbatim_spans():
    src = ("<table><tr><td>Hierarchy</td><td>Design</td></tr>"
           "<tr><td>Systems</td><td>Specs</td></tr>"
           "<tr><td>Devices</td><td>Models</td></tr></table>")
    el = SourceElementQ(id="SE-T", type="table", file="x.md", line_start=1,
                        line_end=1, char_start=0, char_end=len(src), text=src)
    atoms = atomize(src, [el], section_id="SEC", profile="textbook")
    assert [a.atom_type for a in atoms] == [
        "table_header_atom", "table_row_atom", "table_row_atom"]
    for a in atoms:
        assert src[a.source_span.char_start:a.source_span.char_end] == a.raw_text
    assert atoms[0].raw_text == "<tr><td>Hierarchy</td><td>Design</td></tr>"


def test_textbook_selectivity_drops_narrative_keeps_value_sentences():
    src = ("An analog signal is a signal that is defined over a continuous range. "
           "It was a sunny day in the lab and nothing much happened here today.")
    el = SourceElementQ(id="SE-P", type="paragraph", file="x.md", line_start=1,
                        line_end=1, char_start=0, char_end=len(src), text=src)
    atoms = atomize(src, [el], section_id="SEC", profile="textbook")
    # The definition survives ("is a ..."); the narrative filler is dropped.
    assert len(atoms) == 1
    assert atoms[0].raw_text.startswith("An analog signal")
    assert atoms[0].atom_type == "concept_definition_atom"


def test_bare_image_embed_is_not_a_figure_atom():
    src = "![](images/fig1.png)"
    el = SourceElementQ(id="SE-IMG", type="figure_caption", file="x.md",
                        line_start=1, line_end=1, char_start=0,
                        char_end=len(src), text=src)
    atoms = atomize(src, [el], section_id="SEC", profile="textbook")
    assert atoms == []
