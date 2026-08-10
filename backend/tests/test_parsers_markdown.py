import json
from pathlib import Path
from app.services.parsers import parse_markdown, parse_markdown_text, parse_source_file

# 1x1 transparent PNG, valid base64.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

MD = """# Title

<a id="a1"></a>
## Cmd

Use the command:

```tcl
set_message -severity info
```

| Opt | Desc |
| --- | --- |
| -x | do x |
"""


def _write(tmp_path, text):
    p = tmp_path / "doc.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_no_anchor_noise_elements(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    assert all("<a id=" not in e.text for e in els)


def test_code_block_is_one_element_verbatim(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    code = [e for e in els if e.element_type == "code_block"]
    assert len(code) == 1
    assert "set_message -severity info" in code[0].text
    assert code[0].metadata.get("lang") == "tcl"


def test_table_is_one_element(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    tables = [e for e in els if e.element_type == "table"]
    assert len(tables) == 1 and "-x" in tables[0].text


def test_section_path_in_metadata(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    para = [e for e in els if e.text.startswith("Use the command")][0]
    assert para.metadata.get("section_path") == "Title > Cmd"


# --- markdown image ingestion -------------------------------------------


def _fake_persist(returns="asset-1"):
    calls = []

    def persist(img_bytes, img_name):
        calls.append((img_bytes, img_name))
        return returns

    persist.calls = calls
    return persist


def test_image_alt_text_lands_in_caption_metadata():
    text = "![a plot of x vs y](figure.png)\n"
    els = parse_markdown_text("s1", text)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("caption") == "a plot of x vs y"
    assert imgs[0].metadata.get("src") == "figure.png"
    assert imgs[0].text == "a plot of x vs y"


def test_caption_reaches_chunking():
    """metadata.caption 修复主体：图注要能让 build_chunks 把该 element 纳入检索。"""
    from app.services.chunking import build_chunks

    text = "![a plot of x vs y](figure.png)\n"
    els = parse_markdown_text("s1", text)
    img = [e for e in els if e.element_type == "image"][0]
    # 镜像 source_elements_for_chunking 的扁平化输出形态
    flat = {
        "id": img.id or "e1",
        "element_type": img.element_type,
        "text": img.text,
        "caption": img.metadata.get("caption") or "",
    }
    chunks = build_chunks([flat], target_chars=600, overlap_chars=0)
    joined = " ".join(c["text"] for c in chunks)
    assert "a plot of x vs y" in joined


def test_data_uri_image_persists_and_avoids_raw_metadata():
    text = f"![a diagram]({'data:image/png;base64,' + PNG_B64})\n"
    persist = _fake_persist(returns="asset-abc")
    els = parse_markdown_text("s1", text, persist_image=persist)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    img = imgs[0]
    assert img.metadata.get("asset_id") == "asset-abc"
    assert img.metadata.get("caption") == "a diagram"
    assert "src" not in img.metadata
    for value in img.metadata.values():
        assert "base64" not in str(value)
    assert len(persist.calls) == 1
    img_bytes, img_name = persist.calls[0]
    assert img_name.endswith(".png")
    import base64 as _b64
    assert img_bytes == _b64.b64decode(PNG_B64, validate=True)


def test_data_uri_without_persist_closure_no_crash():
    # 有 caption：无法持久化，但保留纯文本元素（无 src/asset_id）。
    text_with_caption = f"![labelled]({'data:image/png;base64,' + PNG_B64})\n"
    els = parse_markdown_text("s1", text_with_caption, persist_image=None)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert "src" not in imgs[0].metadata
    assert "asset_id" not in imgs[0].metadata
    assert imgs[0].metadata.get("caption") == "labelled"

    # 无 caption：既不可显示又不可检索，元素被丢弃。
    text_bare = f"![]({'data:image/png;base64,' + PNG_B64})\n"
    els_bare = parse_markdown_text("s1", text_bare, persist_image=None)
    assert not [e for e in els_bare if e.element_type == "image"]


def test_data_uri_disallowed_mime_skips_persist():
    # markdown-it 自带的 validateLink 只放行 data:image/(gif|png|jpeg|webp);
    # 这一集合与 ALLOWED_MIME_EXTENSIONS 恰好重合，因此真实 markdown 解析路径
    # 永远不会把不在白名单内的 mime 交给我们的 image 分支——直接单测这道
    # parsers.py 自己的防线（防未来 markdown-it 配置/版本变化、或直接调用）。
    from app.services.parsers import _persist_markdown_data_uri

    svg_b64 = __import__("base64").b64encode(b"<svg></svg>").decode()
    persist = _fake_persist()
    src = f"data:image/svg+xml;base64,{svg_b64}"
    asset_id = _persist_markdown_data_uri(src, persist, 1)
    assert asset_id == ""
    assert persist.calls == []


def test_data_uri_malformed_base64_does_not_raise():
    from app.services.parsers import _persist_markdown_data_uri

    persist = _fake_persist()
    asset_id = _persist_markdown_data_uri(
        "data:image/png;base64,!!!not-base64!!!", persist, 1
    )
    assert asset_id == ""
    assert persist.calls == []


def test_data_uri_persist_failure_still_keeps_captioned_element():
    """无论持久化因何种原因失败（mime/解码/闭包本身），有 caption 的元素都保留。"""
    text = f"![an icon]({'data:image/png;base64,' + PNG_B64})\n"
    failing_persist = _fake_persist(returns=None)
    els = parse_markdown_text("s1", text, persist_image=failing_persist)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert "asset_id" not in imgs[0].metadata
    assert imgs[0].metadata.get("caption") == "an icon"


def test_empty_alt_data_uri_persist_success_gets_placeholder_text():
    text = f"![]({'data:image/png;base64,' + PNG_B64})\n"
    persist = _fake_persist(returns="asset-xyz")
    els = parse_markdown_text("s1", text, persist_image=persist)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("asset_id") == "asset-xyz"
    assert "caption" not in imgs[0].metadata
    assert imgs[0].text == "Markdown 图 1"


def test_uncaptioned_regular_src_image_still_dropped():
    text = "![](figure.png)\n"
    els = parse_markdown_text("s1", text)
    assert not [e for e in els if e.element_type == "image"]


def test_regular_src_with_alt_matches_prior_shape_plus_caption():
    text = "![a caption](figure.png)\n"
    els = parse_markdown_text("s1", text)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("src") == "figure.png"
    assert imgs[0].metadata.get("caption") == "a caption"


def test_parse_source_file_forwards_persist_image_for_markdown(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(f"![a diagram]({'data:image/png;base64,' + PNG_B64})\n", encoding="utf-8")
    persist = _fake_persist(returns="asset-forwarded")
    els = parse_source_file("s1", str(p), "doc.md", persist_image=persist)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("asset_id") == "asset-forwarded"
    assert len(persist.calls) == 1


# --- 修3: bare data-URI documents must not fall through to the raw-text ----
# fallback (`if blocks:` not `if elements:`).


def test_bare_data_uri_document_parses_to_empty_list():
    """纯 data URI 图片文档（无 caption、无 persist_image）必须精确得到空
    列表，不能落进「整份原文当纯文本拆段」的兜底把 base64 倾进段落元素。"""
    text = f"![]({'data:image/png;base64,' + PNG_B64})\n"
    els = parse_markdown_text("s1", text, persist_image=None)
    assert els == []
    # 通用断言：即便未来产出非空，也不得含 base64 载荷。
    for e in els:
        assert "base64" not in e.text
        assert "base64" not in json.dumps(e.metadata)


def test_uppercase_data_scheme_persists_and_never_enters_metadata():
    """codex R1 P2: markdown-it 接受 `DATA:image/png;base64,...` 为图片，
    分支判断必须大小写不敏感——否则整条 base64 URI 落进 metadata["src"]。"""
    text = f"![cap](DATA:image/png;base64,{PNG_B64})\n"
    persist = _fake_persist(returns="asset-upper")
    els = parse_markdown_text("s1", text, persist_image=persist)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("asset_id") == "asset-upper"
    assert "src" not in imgs[0].metadata
    for e in els:
        assert "base64" not in e.text
        assert "base64" not in json.dumps(e.metadata)
    assert len(persist.calls) == 1


def test_lone_rejected_mime_list_item_fallback_never_leaks_base64():
    """整份文档只有「无 alt + 白名单外 mime 的 data URI 列表项」时：
    `_inline_text` 剥空 → 空文本列表项被 parse_blocks 丢弃 → blocks 为空 →
    走裸文本兜底。兜底路径必须先经 strip_data_uri_image_literals 消毒，
    绝不能把 base64 载荷按空行拆段倒回段落元素。"""
    text = f"- ![](data:image/svg+xml;base64,{'A' * 4000})\n"
    els = parse_markdown_text("s1", text, persist_image=None)
    for e in els:
        assert "base64" not in e.text
        assert "base64" not in json.dumps(e.metadata)


# --- 修2: mime rejected by markdown-it's validateLink (e.g. svg+xml) must ---
# not leak the raw data-URI literal into a paragraph element.


def test_svg_data_uri_with_alt_becomes_alt_only_paragraph():
    svg_b64 = __import__("base64").b64encode(b"<svg></svg>").decode()
    text = f"![a schematic](data:image/svg+xml;base64,{svg_b64})\n"
    els = parse_markdown_text("s1", text)
    assert len(els) == 1
    assert els[0].element_type == "paragraph"
    assert els[0].text == "a schematic"
    assert "base64" not in els[0].text


def test_svg_data_uri_without_alt_produces_zero_elements():
    svg_b64 = __import__("base64").b64encode(b"<svg></svg>").decode()
    text = f"![](data:image/svg+xml;base64,{svg_b64})\n"
    els = parse_markdown_text("s1", text)
    assert els == []


def test_svg_data_uri_no_block_text_contains_base64():
    from app.services.structural_markdown import parse_blocks

    svg_b64 = __import__("base64").b64encode(b"<svg></svg>").decode()
    for alt in ("a schematic", ""):
        text = f"![{alt}](data:image/svg+xml;base64,{svg_b64})\n"
        blocks = parse_blocks(text)
        assert all("base64" not in b.text for b in blocks)


# --- 修4: `_persist_markdown_data_uri` boundary fixes ------------------------


def test_uppercase_base64_marker_persists():
    text = f"![an icon](data:image/png;BASE64,{PNG_B64})\n"
    persist = _fake_persist(returns="asset-case")
    els = parse_markdown_text("s1", text, persist_image=persist)
    imgs = [e for e in els if e.element_type == "image"]
    assert len(imgs) == 1
    assert imgs[0].metadata.get("asset_id") == "asset-case"
    assert len(persist.calls) == 1


def test_empty_payload_returns_empty_string_and_never_calls_persist():
    from app.services.parsers import _persist_markdown_data_uri

    persist = _fake_persist()
    asset_id = _persist_markdown_data_uri("data:image/png;base64,", persist, 1)
    assert asset_id == ""
    assert persist.calls == []
