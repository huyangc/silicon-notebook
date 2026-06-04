from app.services.structural_markdown import parse_blocks

SAMPLE = """# Top Title

<a id="anchor_x"></a>
## Sub Section

Intro paragraph here.

```tcl
set_message -severity info
add_ring -width 5
```

| Option | Description |
| --- | --- |
| -arg1 | does x |
| -arg2 | does y |

- bullet one
- bullet two

![A waveform](images/wave.png)
"""


def _by_type(blocks, t):
    return [b for b in blocks if b.type == t]


def test_headings_have_levels_and_paths():
    blocks = parse_blocks(SAMPLE)
    heads = _by_type(blocks, "heading")
    assert [(h.text, h.level) for h in heads] == [("Top Title", 1), ("Sub Section", 2)]


def test_code_block_kept_verbatim_with_lang():
    blocks = parse_blocks(SAMPLE)
    code = _by_type(blocks, "code_block")
    assert len(code) == 1
    assert code[0].lang == "tcl"
    assert "set_message -severity info\nadd_ring -width 5" in code[0].text


def test_table_is_single_structured_block():
    blocks = parse_blocks(SAMPLE)
    tables = _by_type(blocks, "table")
    assert len(tables) == 1
    assert "Option" in tables[0].text and "-arg1" in tables[0].text and "does y" in tables[0].text
    assert "|" in tables[0].raw


def test_anchor_only_paragraph_dropped_and_id_attached():
    blocks = parse_blocks(SAMPLE)
    assert all("<a id=" not in b.text for b in blocks)
    sub = [b for b in blocks if b.type == "heading" and b.text == "Sub Section"][0]
    assert sub.anchor_id == "anchor_x"


def test_image_becomes_caption_block_not_raw_syntax():
    blocks = parse_blocks(SAMPLE)
    imgs = _by_type(blocks, "image")
    assert len(imgs) == 1
    assert imgs[0].text == "A waveform"
    assert imgs[0].metadata.get("src") == "images/wave.png"
    assert "![" not in imgs[0].text


def test_section_path_breadcrumb_on_content():
    blocks = parse_blocks(SAMPLE)
    intro = [b for b in blocks if b.type == "paragraph" and b.text.startswith("Intro")][0]
    assert intro.section_path == "Top Title > Sub Section"


def test_char_spans_are_valid_slices():
    blocks = parse_blocks(SAMPLE)
    for b in blocks:
        assert 0 <= b.char_start <= b.char_end <= len(SAMPLE)


def test_list_items_split():
    blocks = parse_blocks(SAMPLE)
    items = [b for b in blocks if b.type == "list_item"]
    assert {b.text for b in items} == {"bullet one", "bullet two"}
