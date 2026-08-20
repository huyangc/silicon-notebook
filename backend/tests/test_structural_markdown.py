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


def test_angle_bracket_data_uri_destination_stripped():
    """codex R5 P1: CommonMark 尖括号目标形态 `![alt](<data:...>)`——被拒 mime
    时字面量留在文本里, 两个清扫正则都必须认这个拼写。"""
    md = f"![alt](<data:image/svg+xml;base64,{_SVG_B64}>)\n"
    blocks = parse_blocks(md)
    assert blocks
    assert all("base64" not in b.text for b in blocks)
    assert any(b.text == "alt" for b in blocks)


def test_angle_bracket_data_uri_in_list_item_stripped():
    md = f"- see ![alt](<data:image/svg+xml;base64,{_SVG_B64}>) here\n"
    blocks = parse_blocks(md)
    items = [b for b in blocks if b.type == "list_item"]
    assert len(items) == 1
    assert "base64" not in items[0].text
    assert "see alt here" in items[0].text


def test_angle_bracket_with_paren_alt_stripped_by_literal_regex():
    """alt 带圆括号时只有精确正则能命中(fallback 排除圆括号)——钉住精确正则
    自己的尖括号支持。"""
    md = f"![see (fig)](<data:image/svg+xml;base64,{_SVG_B64}>)\n"
    blocks = parse_blocks(md)
    assert all("base64" not in b.text for b in blocks)
    assert any("see (fig)" in b.text for b in blocks)


def test_angle_bracket_with_nested_alt_stripped_by_fallback_regex():
    """alt 带嵌套方括号时只有 fallback 正则能命中(精确正则不认 `[`)——钉住
    fallback 自己的尖括号支持。"""
    md = f"![a [nested] alt](<data:image/svg+xml;base64,{_SVG_B64}>)\n"
    blocks = parse_blocks(md)
    assert all("base64" not in b.text for b in blocks)
    assert any("a [nested] alt" in b.text for b in blocks)


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


# ---------------------------------------------------------------------------
# 图片描述块：`![alt](src)` 之后紧跟的 `> **图片描述**` 引用块
# ---------------------------------------------------------------------------

DESCRIBED = """![某个图注](images/a.png)

> **图片描述**
> 这是第一行描述
> 这是第二行描述
>

尾段。
"""


def test_image_description_quote_folds_into_the_image_block():
    blocks = parse_blocks(DESCRIBED)
    img = _by_type(blocks, "image")
    assert len(img) == 1
    assert img[0].text == "某个图注"          # 图注仍然只是 alt
    assert img[0].metadata["description"] == "这是第一行描述 这是第二行描述"
    # 折叠掉的是「引用块另成一个普通段落」这件事，不是它的内容。
    assert not [b for b in blocks if b.type == "paragraph" and "描述" in b.text]
    assert [b.type for b in blocks] == ["image", "image_description", "paragraph"]


def test_image_description_block_keeps_a_verbatim_source_span():
    """KG 侧按 `text[char_start:char_end]` 切原文当元素文本，跨度必须逐字对得上。"""
    block = _by_type(parse_blocks(DESCRIBED), "image_description")[0]
    sliced = DESCRIBED[block.char_start:block.char_end]
    assert sliced.startswith("> **图片描述**")
    assert "这是第二行描述" in sliced
    assert block.raw == sliced


def test_plain_quote_after_image_stays_an_ordinary_paragraph():
    blocks = parse_blocks("![a](x.png)\n\n> 普通引用\n> 第二行\n")
    assert [b.type for b in blocks] == ["image", "paragraph"]
    assert blocks[0].metadata.get("description") is None
    assert blocks[1].text == "普通引用 第二行"


def test_image_description_requires_nothing_between_it_and_the_image():
    """中间隔了别的内容就不是这张图的描述——照旧当普通引用。"""
    blocks = parse_blocks("![a](x.png)\n\n中间段落\n\n> **图片描述**\n> 描述\n")
    assert [b.type for b in blocks] == ["image", "paragraph", "paragraph"]
    assert blocks[0].metadata.get("description") is None
    assert blocks[2].text == "图片描述 描述"


def test_quote_without_a_preceding_image_is_untouched():
    blocks = parse_blocks("> **图片描述**\n> 没有图片\n")
    assert [b.type for b in blocks] == ["paragraph"]
    assert blocks[0].text == "图片描述 没有图片"


def test_marker_only_quote_is_not_a_description_block():
    """光一个标记什么描述都没带来，折叠它只会把这行字弄丢。"""
    blocks = parse_blocks("![a](x.png)\n\n> **图片描述**\n")
    assert [b.type for b in blocks] == ["image", "paragraph"]
    assert blocks[0].metadata.get("description") is None


def test_description_marker_may_carry_the_text_on_the_same_line():
    blocks = parse_blocks("![a](x.png)\n\n> **图片描述**：紧跟在冒号后面\n> 续行\n")
    assert blocks[0].metadata["description"] == "紧跟在冒号后面 续行"


def test_marker_lookalike_prose_is_not_a_description_block():
    """「图片描述如下：……」是正常引用：标记后面跟内容必须隔一个冒号。"""
    blocks = parse_blocks("![a](x.png)\n\n> 图片描述如下：一张示意图\n")
    assert [b.type for b in blocks] == ["image", "paragraph"]
    assert blocks[0].metadata.get("description") is None


def test_plain_unbolded_marker_is_accepted():
    blocks = parse_blocks("![a](x.png)\n\n> 图片描述\n> 没加粗也认\n")
    assert blocks[0].metadata["description"] == "没加粗也认"


def test_description_quote_needs_no_blank_line_before_it():
    """引用块能打断段落（CommonMark），所以紧贴着写也是同一形态。"""
    blocks = parse_blocks("![a](x.png)\n> **图片描述**\n> 紧贴着写\n")
    assert [b.type for b in blocks] == ["image", "image_description"]
    assert blocks[0].metadata["description"] == "紧贴着写"


def test_description_paragraphs_are_joined_by_newline():
    blocks = parse_blocks("![a](x.png)\n\n> **图片描述**\n>\n> 第一段\n>\n> 第二段\n")
    assert blocks[0].metadata["description"] == "第一段\n第二段"


def test_quote_containing_a_list_is_folded_too():
    """约定是「后续的**所有**引用行都是描述」——VLM 写的图片描述常带项目符号，
    列表/标题/表格/嵌套引用的正文都挂在 inline 上，照收。"""
    blocks = parse_blocks(
        "![a](x.png)\n\n> **图片描述**\n> 图中展示三级流水线：\n> - 取指\n> - 写回\n"
    )
    assert [b.type for b in blocks] == ["image", "image_description"]
    assert blocks[0].metadata["description"] == "图中展示三级流水线：\n取指\n写回"


def test_quote_containing_a_nested_quote_is_folded_too():
    """`>>` 与「块内嵌套一层」都只是更多的引用行，两者结果必须一致——否则同一份
    描述块会因为图片有没有 alt 而走不同的路（预判与折叠调的是同一个函数）。"""
    for doc in (
        "![a](x.png)\n\n>> **图片描述**\n>> 描述\n",
        "![](x.png)\n\n>> **图片描述**\n>> 描述\n",
    ):
        blocks = parse_blocks(doc)
        assert [b.type for b in blocks] == ["image", "image_description"], doc
        assert blocks[0].metadata["description"] == "描述", doc


def test_marker_line_is_judged_from_the_raw_source():
    """契约是「标记行只有标记本身」，而 markdown-it 会把列表/标题的结构前缀从
    `inline.content` 上剥掉——`> - 图片描述` 的 content 正是 `图片描述`。按 content
    判会把一条普通的引用列表认成标记行，连同它的结构一起折进上面那张图。"""
    for doc, kept in (
        ("![a](x.png)\n\n> - 图片描述\n> 正文\n", "list_item"),
        ("![a](x.png)\n\n> 1. 图片描述\n> 正文\n", "list_item"),
        ("![a](x.png)\n\n> # 图片描述\n> 正文\n", "heading"),
    ):
        blocks = parse_blocks(doc)
        assert blocks[0].type == "image", doc
        assert blocks[0].metadata.get("description") is None, doc
        assert not [b for b in blocks if b.type == "image_description"], doc
        assert kept in [b.type for b in blocks], doc


def test_crlf_sources_fold_the_same_as_lf():
    """行尾格式不能改变关联与检索行为：`_line_char_offsets` 只按 `\n` 切行，CRLF
    原文的标记行会留一个 `\r`。前端 `scanLines` 本来就剥 CR，服务端不剥就是同一份
    文档两侧判定相反（而且是前端更宽的那个方向）。"""
    lf = "![a](x.png)\n\n> **图片描述**\n> 正文\n"
    blocks = parse_blocks(lf.replace("\n", "\r\n"))
    assert [b.type for b in blocks] == ["image", "image_description"]
    assert blocks[0].metadata["description"] == "正文"
    assert [b.metadata.get("description") for b in parse_blocks(lf)] == ["正文", None]


def test_quote_containing_an_opaque_block_is_not_folded():
    """围栏/HTML/缩进代码的正文挂在 token.content 上、没有 inline 子节点，折进去
    就会把这段内容**静默丢掉**（折叠后引用块不再单独成段元素）——整块不认。

    三条都必须扫完整个引用块才看得见：不透明块出现在首行正文**之后**，只看第一条
    内容行会全判成有描述。"""
    for doc in (
        "![a](x.png)\n\n> **图片描述**\n> 引导\n> ```\n> code\n> ```\n",
        "![a](x.png)\n\n> **图片描述**\n> 引导\n> <div>x</div>\n",
        "![a](x.png)\n\n> **图片描述**\n> 引导\n>\n>     code\n",
    ):
        blocks = parse_blocks(doc)
        assert blocks[0].type == "image", doc
        assert blocks[0].metadata.get("description") is None, doc
        assert not [b for b in blocks if b.type == "image_description"], doc


def test_content_that_emits_no_block_still_breaks_the_adjacency():
    """相邻判据必须按**原文**判，不能按 token 判：链接引用定义与锚点段落都不产出
    任何 Block（前者连 token 都没有），按 token 判会把它们当成「紧挨着」而误折叠。

    图片带 alt 的那支钉住折叠判据；不带 alt 的那支钉住 image 分支的预判——两者
    必须同判，否则会留下一个既无图注也无描述的空图片块。"""
    for lead in ("![alt](a.png)", "![](a.png)"):
        for filler in ("[foo]: /url", '<a id="x"></a>'):
            doc = f"{lead}\n\n{filler}\n\n> **图片描述**\n> 描述\n"
            blocks = parse_blocks(doc)
            assert not [b for b in blocks if b.type == "image_description"], doc
            images = [b for b in blocks if b.type == "image"]
            assert all(b.metadata.get("description") is None for b in images), doc
            # 无 alt 那支：连块都不该产出（空图注 + 空描述 = 下游只能丢掉的孤儿）。
            if lead == "![](a.png)":
                assert images == [], doc
            assert [b.text for b in blocks if b.type == "paragraph"] == ["图片描述 描述"], doc


def test_uncaptioned_image_with_a_description_is_still_emitted():
    """没有 alt、src 也不是 data URI 的图片：老判据整条丢掉，连描述一起没了。"""
    blocks = parse_blocks("![](images/a.png)\n\n> **图片描述**\n> 只有描述\n")
    assert [b.type for b in blocks] == ["image", "image_description"]
    assert blocks[0].text == ""
    assert blocks[0].metadata["description"] == "只有描述"


def test_uncaptioned_image_without_a_description_is_still_dropped():
    """反向护栏：上面那条放行的是「带描述」，不是「所有无图注图片」。"""
    blocks = parse_blocks("![](images/a.png)\n\n> 普通引用\n")
    assert [b.type for b in blocks] == ["paragraph"]
