import base64

from app.services.structural_markdown import parse_blocks

# `<svg></svg>` base64-encoded — a mime markdown-it-py's validateLink rejects
# outright (only data:image/(gif|png|jpeg|webp) tokenize as `image`), so the
# whole `![alt](data:...)` literal falls through as plain inline text unless
# `_inline_text`'s sub() strips it (修1).
_SVG_B64 = base64.b64encode(b"<svg></svg>").decode()

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


def test_list_items_no_duplicate_blocks():
    blocks = parse_blocks(SAMPLE)
    bullet_paras = [b for b in blocks if b.type == "paragraph" and b.text in ("bullet one", "bullet two")]
    assert bullet_paras == []          # 列表项不应再产出重复 paragraph
    items = [b for b in blocks if b.type == "list_item"]
    assert len(items) == 2


def test_empty_alt_image_dropped():
    # spec D4：无 caption 的裸 ![](path) 是噪声，应丢弃（不入元素/不被检索）
    blocks = parse_blocks("![](images/only.png)\n")
    assert [b for b in blocks if b.type == "image"] == []


def test_html_table_block_not_dropped():
    md = "<table><tr><th>Opt</th><th>Desc</th></tr><tr><td>-x</td><td>do x</td></tr></table>\n"
    blocks = parse_blocks(md)
    tables = [b for b in blocks if b.type == "table"]
    assert len(tables) == 1
    assert "Opt" in tables[0].text and "-x" in tables[0].text and "do x" in tables[0].text


def test_orphan_anchor_attached_to_next_block():
    blocks = parse_blocks('<a id="z"></a>\n\nJust a paragraph.\n')
    assert all("<a id=" not in b.text for b in blocks)
    para = [b for b in blocks if b.type == "paragraph"][0]
    assert para.anchor_id == "z"


# --- 修1: rejected-mime data-URI literal stripped inside _inline_text -------
# (list_item / heading / table cell / mixed-paragraph text — everywhere the
# paragraph-only fullmatch branch in parse_blocks does not reach).


def test_list_item_data_uri_literal_stripped_no_alt():
    # Alt-less: stripping leaves empty text, so — same as any empty list item
    # — no block is emitted at all (existing `if txt:` guard, unrelated to
    # this fix). Anchoring on a document with other content sidesteps the
    # unrelated `if blocks:` raw-text fallback boundary in parse_markdown_text
    # (a document consisting *solely* of this list item is a known, unfixed
    # residual gap — see task report).
    md = f"Intro paragraph.\n\n- ![](data:image/svg+xml;base64,{_SVG_B64})\n"
    blocks = parse_blocks(md)
    assert all("base64" not in b.text for b in blocks)
    assert [b for b in blocks if b.type == "list_item"] == []


def test_list_item_data_uri_literal_stripped_keeps_alt():
    blocks = parse_blocks(f"- ![a schematic](data:image/svg+xml;base64,{_SVG_B64})\n")
    items = [b for b in blocks if b.type == "list_item"]
    assert len(items) == 1
    assert items[0].text == "a schematic"
    assert "base64" not in items[0].text


def test_escaped_bracket_alt_data_uri_literal_still_stripped():
    """codex R1 P1: alt 含 `\\]` 转义的合法图片写法。markdown-it 会把文本里的
    `\\]` 解转义成裸 `]`（`![foo\\]bar](data:...` 存活为 `![foo]bar](data:...`），
    剥离正则必须容忍 alt 内的 `]`，否则整条 base64 泄进段落文本。"""
    md = f"![foo\\]bar](data:image/svg+xml;base64,{_SVG_B64})\n"
    blocks = parse_blocks(md)
    assert all("base64" not in b.text for b in blocks)
    paras = [b for b in blocks if b.type == "paragraph"]
    assert len(paras) == 1
    assert paras[0].text == "foo]bar"


def test_escaped_bracket_alt_in_list_item_stripped():
    md = f"- before ![foo\\]bar](data:image/svg+xml;base64,{_SVG_B64}) after\n"
    blocks = parse_blocks(md)
    items = [b for b in blocks if b.type == "list_item"]
    assert len(items) == 1
    assert "base64" not in items[0].text
    assert "foo]bar" in items[0].text


def test_uppercase_data_scheme_uncaptioned_image_still_emitted():
    """codex R1 P2: URI scheme 大小写不敏感——`DATA:image/png` 会被 markdown-it
    token 化成 image，发射条件必须同样认它，否则无 alt 的大写 scheme 图片在
    structural 层就被丢弃。"""
    blocks = parse_blocks("![](DATA:image/png;base64,iVBORw0KGgo=)\n")
    imgs = [b for b in blocks if b.type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("src", "")[:5].lower() == "data:"


def test_uppercase_data_scheme_raw_fallback_strip():
    """裸文本兜底共用的 strip 也必须认大写 scheme。"""
    from app.services.structural_markdown import strip_data_uri_image_literals

    raw = f"![alt](DATA:image/svg+xml;base64,{_SVG_B64})"
    assert strip_data_uri_image_literals(raw) == "alt"


def test_ordinary_data_link_preserved_verbatim():
    """codex R4 P2: 清扫只针对图片语法——普通链接 `[x](data:...)` 是用户正文,
    目标端不得被摘除。"""
    md = "See [ordinary](data:text/plain;base64,AAAA) and x](data:secret) too.\n"
    blocks = parse_blocks(md)
    paras = [b for b in blocks if b.type == "paragraph"]
    assert len(paras) == 1
    # markdown-it 拒绝 data:text/plain 链接,字面量整体留在文本里——必须原样。
    assert "[ordinary](data:text/plain;base64,AAAA)" in paras[0].text
    assert "x](data:secret)" in paras[0].text


def test_nested_bracket_alt_data_uri_destination_swept():
    """codex R3 P2: alt 含嵌套方括号时字面量正则不匹配——目标端兜底清扫必须
    单独把 `](data:...)` 收掉, base64 不得进任何 block 文本。"""
    md = f"![a [nested] alt](data:image/svg+xml;base64,{_SVG_B64})\n"
    blocks = parse_blocks(md)
    assert blocks
    assert all("base64" not in b.text for b in blocks)
    assert any("a [nested] alt" in b.text for b in blocks)


def test_html_block_data_uri_literal_sanitized():
    """codex R2 P1: markdown-it 对 html_block 内部不做 token 化——`<details>` 里的
    `![alt](data:...)` 字面量(白名单 mime 也一样)会原样穿过 `_html_to_text`
    进元素文本。消毒收口必须覆盖 html 路径。"""
    md = (
        "<details>\n"
        "<summary>figs</summary>\n"
        f"![a diagram](data:image/png;base64,{_SVG_B64})\n"
        "</details>\n"
    )
    blocks = parse_blocks(md)
    assert blocks, "details html block should still emit"
    assert all("base64" not in b.text for b in blocks)
    assert any("a diagram" in b.text for b in blocks)


def test_html_block_generic_container_data_uri_sanitized():
    md = f"<div>\n![x](data:image/svg+xml;base64,{_SVG_B64})\n</div>\n"
    blocks = parse_blocks(md)
    assert all("base64" not in b.text for b in blocks)


def test_heading_data_uri_literal_stripped_to_alt():
    blocks = parse_blocks(f"# ![alt text](data:image/svg+xml;base64,{_SVG_B64})\n")
    heads = [b for b in blocks if b.type == "heading"]
    assert len(heads) == 1
    assert heads[0].text == "alt text"
    assert "base64" not in heads[0].text


def test_table_cell_data_uri_literal_stripped():
    md = (
        "| Col |\n"
        "| --- |\n"
        f"| ![a schematic](data:image/svg+xml;base64,{_SVG_B64}) |\n"
    )
    blocks = parse_blocks(md)
    tables = [b for b in blocks if b.type == "table"]
    assert len(tables) == 1
    assert "base64" not in tables[0].text
    assert "a schematic" in tables[0].text


def test_mixed_paragraph_data_uri_literal_stripped_keeps_surrounding_text():
    md = f"Before text ![a schematic](data:image/svg+xml;base64,{_SVG_B64}) after text\n"
    blocks = parse_blocks(md)
    paras = [b for b in blocks if b.type == "paragraph"]
    assert len(paras) == 1
    assert "base64" not in paras[0].text
    assert "Before text" in paras[0].text
    assert "after text" in paras[0].text
    assert "a schematic" in paras[0].text


def test_mixed_paragraph_data_uri_literal_stripped_no_alt():
    md = f"Before text ![](data:image/svg+xml;base64,{_SVG_B64}) after text\n"
    blocks = parse_blocks(md)
    paras = [b for b in blocks if b.type == "paragraph"]
    assert len(paras) == 1
    assert "base64" not in paras[0].text
    assert "Before text" in paras[0].text
    assert "after text" in paras[0].text


def test_allowlisted_mime_image_in_paragraph_still_becomes_image_block():
    # Regression: png (allowlisted mime) tokenizes as a real `image` child
    # regardless of this fix; unaffected by the `_inline_text` sub() no-op.
    blocks = parse_blocks("![A waveform](images/wave.png)\n")
    imgs = [b for b in blocks if b.type == "image"]
    assert len(imgs) == 1
    assert imgs[0].text == "A waveform"


def test_plain_text_without_data_uri_unaffected():
    blocks = parse_blocks("- Just a normal bullet with no images at all\n")
    items = [b for b in blocks if b.type == "list_item"]
    assert len(items) == 1
    assert items[0].text == "Just a normal bullet with no images at all"
