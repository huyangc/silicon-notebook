from app.services.qiefen.source_elements import parse_elements, line_offsets


def test_line_offsets_absolute():
    text = "a\nbb\nccc\n"
    offs = line_offsets(text)
    assert offs[1] == 0   # line 1 starts at 0
    assert offs[2] == 2   # after "a\n"
    assert offs[3] == 5   # after "bb\n"


def test_abstract_paragraph_span_is_verbatim(source_text):
    src = source_text("engram_paper_mineru.md")
    els = parse_elements(src, "engram_paper_mineru.md", line_range=[9, 11])
    # Heading "Abstract" (line 9) + the paragraph (line 11).
    heading = [e for e in els if e.type == "heading"]
    para = [e for e in els if e.type == "paragraph"]
    assert heading and para
    p = para[0]
    assert p.char_start == 526
    assert src[p.char_start:p.char_end] == p.text
    assert p.text.startswith("While Mixture-of-Experts (MoE)")
