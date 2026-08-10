from app.services.kg.parsing import parse_elements

MD = """# A

para one here.

```tcl
set_db x y
```

<a id="z"></a>
## B

| c1 | c2 |
| --- | --- |
| v1 | v2 |
"""


def test_code_block_typed_as_code_block_not_prose():
    # code_block was previously mapped to "paragraph" (a bug); it should be "code_block"
    # so it is excluded from KG extraction windows while still stored as a SourceElementQ.
    els = parse_elements(MD, "doc.md", None)
    code = [e for e in els if "set_db x y" in e.text]
    assert code and code[0].type == "code_block"


def test_no_anchor_prose_elements():
    els = parse_elements(MD, "doc.md", None)
    assert all("<a id=" not in e.text for e in els)


def test_table_element_present():
    els = parse_elements(MD, "doc.md", None)
    assert any(e.type == "table" and "v1" in e.text for e in els)


def test_char_spans_round_trip():
    els = parse_elements(MD, "doc.md", None)
    for e in els:
        assert e.char_start <= e.char_end <= len(MD)


# --- data-URI image elements never carry raw base64 into KG windows (修1) --

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_captioned_data_uri_image_element_uses_caption_not_raw_slice():
    text = f"![a wiring diagram](data:image/png;base64,{PNG_B64})\n"
    els = parse_elements(text, "doc.md", None)
    imgs = [e for e in els if e.type == "figure_caption"]
    assert len(imgs) == 1
    assert imgs[0].text == "a wiring diagram"
    assert "base64" not in imgs[0].text
    # codex R2 P2: 证据跨度必须真实——char span 切原文得到的就是图注本身,
    # 而不是 `![a wiri` 这样的字面量前缀(证据匹配按这对偏移消费)。
    assert text[imgs[0].char_start:imgs[0].char_end] == "a wiring diagram"


def test_normalized_caption_without_truthful_span_skips_kg_element():
    """codex R3 P2: 图注被 markdown-it 归一化(NUL → U+FFFD)后在原文里找不到
    逐字位置时, 不得伪造前缀跨度当证据——直接不产出该 KG 元素(图注仍经
    parsers.py 的 element/chunk 通道参与检索, 那条路不带 char 偏移)。
    (转义括号 `\\]` 的 alt 实测在 image token 里保留原文形态、跨度可真实
    定位, 不属于本分支; 见同文件 caption_not_raw_slice 用例。)"""
    text = f"![foo\x00bar](data:image/png;base64,{PNG_B64})\n"
    els = parse_elements(text, "doc.md", None)
    assert not [e for e in els if e.type == "figure_caption"]
    assert all("base64" not in e.text for e in els)
    # 所有存活元素的跨度切原文必须与自身 text 逐字一致(证据契约)。
    for e in els:
        assert text[e.char_start:e.char_end] == e.text


def test_uncaptioned_data_uri_image_produces_no_kg_element():
    text = f"![](data:image/png;base64,{PNG_B64})\n"
    els = parse_elements(text, "doc.md", None)
    assert not [e for e in els if e.type == "figure_caption"]
    assert all("base64" not in e.text for e in els)
