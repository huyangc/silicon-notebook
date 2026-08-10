"""唯一的结构化 Markdown 解析：raw markdown -> 带 char 跨度/层级/section 面包屑的块序列。

被两个适配器复用：parsers.parse_markdown（-> SourceElement，供存储/embedding）和
kg/parsing.parse_elements（-> SourceElementQ，供 KG 窗口化）。用 markdown-it-py 的
commonmark 预设并启用 table（不启用 linkify，避免 linkify-it-py 依赖）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from markdown_it import MarkdownIt

_ANCHOR_ONLY = re.compile(r'^\s*(?:<a\s+id="[^"]*"\s*>\s*</a>\s*)+$', re.IGNORECASE)
_ANCHOR_ID = re.compile(r'<a\s+id="([^"]*)"', re.IGNORECASE)

# markdown-it-py's validateLink only allows `data:image/(gif|png|jpeg|webp)`;
# any other mime (svg+xml, bmp, avif, ...) fails link validation and the
# image never tokenizes as an `image` child — the whole `![alt](data:...)`
# literal (base64 payload and all) falls through as plain inline text
# instead. Two consumers strip it down to just the alt text before it can
# reach chunking/embedding/KG: the paragraph_open handler below fullmatches
# it to special-case a paragraph whose *entire* inline content is exactly
# one such literal (emits a dedicated `image` Block instead of a paragraph),
# and `_inline_text`'s `sub()` call catches every other position — mixed
# paragraph text, list items, headings, table cells — where the literal
# survives as an ordinary `text` child instead.
# Alt-text matching must tolerate a `]` inside the alt: markdown-it has
# already UNESCAPED `\]` to `]` by the time the rejected literal reaches a
# text child (`![foo\]bar](data:...)` survives as `![foo]bar](data:...)`),
# and the raw-text fallback sees the escaped form. So the alt group accepts
# any run of non-bracket chars plus `]` not immediately followed by `(` —
# it stops only at the real `](` delimiter. `[` stays disallowed (a nested
# `![a[x]](data:...)` is pathological and fails open to the old behavior).
# The scheme is matched case-insensitively: URI schemes are, and markdown-it
# accepts `DATA:`/`Data:` variants.
# The destination accepts both plain and CommonMark angle-bracket forms
# (`](data:...)` and `](<data:...>)`, codex R5 P1) — markdown-it rejects
# unsupported mimes in either spelling and leaves the literal in text.
_DATA_URI_IMAGE_LITERAL = re.compile(
    r"!\[((?:[^\[\]]|\](?!\())*)\]\(\s*<?\s*(?i:data):[^)>]*>?\s*\)"
)


def _unescape_md_brackets(alt: str) -> str:
    """还原 alt 里的 `\\[`/`\\]` 转义——markdown-it 的 token 化路径给出的是
    解转义后的 alt，剥离路径（raw 文本仍带转义）对齐同一表现。"""
    return alt.replace("\\[", "[").replace("\\]", "]")


# Image-anchored fallback sweep (codex R3 P2, narrowed in R4 P2): an alt the
# literal matcher cannot parse (nested brackets `![a [nested] alt](data:...)`,
# arbitrary depth) would otherwise carry the full base64 payload through.
# Anchoring on `![` keeps ordinary user-authored data LINKS
# (`[ordinary](data:text/plain;base64,...)`) and bare `](data:...)` fragments
# untouched — sanitization only ever targets image syntax. The non-greedy alt
# excludes parens so a preceding complete image (`![a](x) and ![b](data:...)`)
# cannot be swallowed into the alt; an image alt that itself contains parens
# AND a data destination stays unmatched (registered pathological boundary).
_DATA_URI_IMAGE_FALLBACK = re.compile(
    r"!\[([^()]*?)\]\(\s*<?\s*(?i:data):[^)>]*>?\s*\)"
)


def contains_data_uri_image_literal(text: str) -> bool:
    """原文里是否存在（任一形态的）data URI 图片字面量。

    供 `kg/parsing.parse_elements` 判定：容器块（段落/列表/表格/标题）的
    verbatim 切片带着这种字面量时，无法同时满足「载荷不进 KG 窗口」与
    「证据跨度切原文即元素文本」两条契约，只能跳过该块。
    """
    return bool(
        _DATA_URI_IMAGE_LITERAL.search(text)
        or _DATA_URI_IMAGE_FALLBACK.search(text)
    )


def strip_data_uri_image_literals(text: str) -> str:
    """把 `![alt](data:...)` 图片字面量剥成 alt 文本（空 alt 剥成空串）。

    `_inline_text`、`_html_to_text` 与 `parsers.parse_markdown_text` 的裸文本
    兜底共用这一个收口：任何要把 markdown 原文当纯文本吐出的路径都必须先过
    它，保证 base64 载荷不进元素文本。对不含字面量的文本是 no-op。两段式：
    先按完整字面量剥成纯 alt，alt 无法解析（嵌套方括号）的形态再按带 `![`
    锚点的兜底正则剥；普通链接的 `](data:...)` 不受影响。
    """
    text = _DATA_URI_IMAGE_LITERAL.sub(
        lambda m: _unescape_md_brackets(m.group(1)), text
    )
    return _DATA_URI_IMAGE_FALLBACK.sub(
        lambda m: _unescape_md_brackets(m.group(1)), text
    )


@dataclass
class Block:
    type: str
    text: str
    raw: str = ""
    level: int = 0
    lang: str = ""
    char_start: int = 0
    char_end: int = 0
    line_start: int = 1
    line_end: int = 1
    section_path: str = ""
    anchor_id: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)


def _make_md() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table")


def _line_char_offsets(text: str) -> List[int]:
    offsets: List[int] = []
    off = 0
    for line in text.split("\n"):
        offsets.append(off)
        off += len(line) + 1
    offsets.append(len(text))
    return offsets


def _span(text: str, offs: List[int], tok_map) -> tuple[int, int, int, int]:
    l0, l1 = tok_map
    l0 = max(0, min(l0, len(offs) - 1))
    l1 = max(l0 + 1, min(l1, len(offs) - 1))
    char_start = offs[l0]
    char_end = offs[l1]
    return char_start, char_end, l0 + 1, l1


def _inline_text(tok) -> str:
    if tok is None or not tok.children:
        return (tok.content if tok else "") or ""
    parts: List[str] = []
    for c in tok.children:
        if c.type == "text":
            # A rejected-mime data-URI image literal (see module docstring
            # comment above `_DATA_URI_IMAGE_LITERAL`) never tokenizes as an
            # `image` child — it survives as plain `text` content, base64 and
            # all. Strip it down to just the alt text (empty alt -> empty
            # string) wherever it shows up: list items, headings, table
            # cells, and paragraph text mixed with other prose all route
            # through this loop. This is a no-op for text that doesn't
            # contain such a literal (修1).
            parts.append(strip_data_uri_image_literals(c.content))
        elif c.type == "code_inline":
            parts.append(c.content)
        elif c.type == "image":
            parts.append(c.content or "")
        elif c.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts).strip()


def _table_text(tokens, i: int) -> str:
    rows: List[str] = []
    cur: List[str] = []
    depth = 0
    j = i
    while j < len(tokens):
        t = tokens[j]
        if t.type == "table_open":
            depth += 1
        elif t.type == "table_close":
            depth -= 1
            if depth == 0:
                break
        elif t.type == "tr_open":
            cur = []
        elif t.type == "tr_close":
            if cur:
                rows.append(" | ".join(cur))
        elif t.type == "inline":
            cur.append(_inline_text(t))
        j += 1
    return " ; ".join(r for r in rows if r.strip())


def _html_to_text(html: str) -> str:
    """HTML 片段 -> 可读文本：单元格用 ' | ' 连接、行用 ' ; ' 连接、去标签。

    markdown-it 对 html_block 内部不做 token 化，`<details>`/`<table>` 里的
    `![alt](data:...)` 字面量（不论 mime 是否在白名单）都会原样留在文本里，
    所以这里是 html 路径的消毒收口——与 `_inline_text`/裸文本兜底同一契约：
    base64 载荷绝不进元素文本。`<img src="data:...">` 形态无需另行处理，
    去标签正则已把整个标签（含属性里的载荷）删掉。
    """
    s = re.sub(r"(?i)</t[dh]>", " | ", html)
    s = re.sub(r"(?i)</tr>", " ; ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return strip_data_uri_image_literals(" ".join(s.split()).strip(" |;"))


def parse_blocks(text: str) -> List[Block]:
    md = _make_md()
    tokens = md.parse(text)
    offs = _line_char_offsets(text)
    blocks: List[Block] = []

    heading_stack: List[tuple[int, str]] = []
    pending_anchor: Optional[str] = None

    def section_path() -> str:
        return " > ".join(title for _, title in heading_stack)

    def emit(blk: Block) -> None:
        nonlocal pending_anchor
        if pending_anchor and blk.anchor_id is None:
            blk.anchor_id = pending_anchor
        pending_anchor = None
        blocks.append(blk)

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]

        if t.type == "heading_open":
            level = int(t.tag[1])
            inline = tokens[i + 1] if i + 1 < n else None
            title = _inline_text(inline)
            cs, ce, ls, le = _span(text, offs, t.map)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            emit(Block(type="heading", text=title, raw=text[cs:ce],
                       level=level, char_start=cs, char_end=ce,
                       line_start=ls, line_end=le, section_path=section_path()))
            i += 3
            continue

        if t.type == "fence":
            cs, ce, ls, le = _span(text, offs, t.map)
            emit(Block(type="code_block", text=t.content.rstrip("\n"),
                       raw=text[cs:ce], lang=(t.info or "").strip(),
                       char_start=cs, char_end=ce, line_start=ls, line_end=le,
                       section_path=section_path()))
            i += 1
            continue

        if t.type == "table_open":
            cs, ce, ls, le = _span(text, offs, t.map)
            emit(Block(type="table", text=_table_text(tokens, i),
                       raw=text[cs:ce], char_start=cs, char_end=ce,
                       line_start=ls, line_end=le, section_path=section_path()))
            depth = 0
            while i < n:
                if tokens[i].type == "table_open":
                    depth += 1
                elif tokens[i].type == "table_close":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            i += 1
            continue

        if t.type == "html_block":
            cs, ce, ls, le = _span(text, offs, t.map)
            content = (t.content or "").strip()
            low = content.lower()
            if low.startswith("<table") or low.startswith("<details"):
                emit(Block(type="table", text=_html_to_text(content), raw=text[cs:ce],
                           char_start=cs, char_end=ce, line_start=ls, line_end=le,
                           section_path=section_path()))
            else:
                stripped = _html_to_text(content)
                if stripped:
                    emit(Block(type="paragraph", text=stripped, raw=text[cs:ce],
                               char_start=cs, char_end=ce, line_start=ls, line_end=le,
                               section_path=section_path()))
            i += 1
            continue

        if t.type == "list_item_open":
            cs, ce, ls, le = _span(text, offs, t.map)
            parts: List[str] = []
            j = i + 1
            depth = 1
            while j < n and depth > 0:
                tj = tokens[j]
                if tj.type == "list_item_open":
                    depth += 1
                elif tj.type == "list_item_close":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                elif tj.type == "inline":
                    it = _inline_text(tj)
                    if it:
                        parts.append(it)
                j += 1
            txt = " ".join(parts)
            if txt:
                emit(Block(type="list_item", text=txt, raw=text[cs:ce],
                           char_start=cs, char_end=ce, line_start=ls, line_end=le,
                           section_path=section_path()))
            i = j
            continue

        if t.type == "paragraph_open":
            inline = tokens[i + 1] if i + 1 < n else None
            raw_inline = (inline.content if inline else "") or ""
            cs, ce, ls, le = _span(text, offs, t.map)
            if _ANCHOR_ONLY.match(raw_inline):
                m = _ANCHOR_ID.search(raw_inline)
                if m:
                    pending_anchor = m.group(1)
                i += 3
                continue
            img = None
            if inline and inline.children:
                imgs = [c for c in inline.children if c.type == "image"]
                texty = [c for c in inline.children
                         if c.type == "text" and c.content.strip()]
                if len(imgs) == 1 and not texty:
                    img = imgs[0]
            if img is not None:
                caption = (img.content or "").strip()
                src = img.attrs.get("src", "") if hasattr(img, "attrs") else ""
                # URI schemes are case-insensitive and markdown-it accepts
                # `DATA:`/`Data:` image sources — match them all here.
                if caption or src[:5].lower() == "data:":
                    emit(Block(type="image", text=caption, raw=text[cs:ce],
                               char_start=cs, char_end=ce, line_start=ls, line_end=le,
                               section_path=section_path(), metadata={"src": src}))
                i += 3
                continue
            literal_m = _DATA_URI_IMAGE_LITERAL.fullmatch(raw_inline.strip())
            if literal_m is not None:
                # A data-URI image markdown-it rejected outright (disallowed
                # mime): keep only the alt text, if any; drop the base64
                # entirely. Mixed content (literal + other text in the same
                # paragraph) does not match this `fullmatch` — it falls
                # through to `_inline_text(inline)` below, whose `sub()` call
                # strips the literal to alt text there instead (修1).
                alt = _unescape_md_brackets(literal_m.group(1)).strip()
                if alt:
                    emit(Block(type="paragraph", text=alt, raw=text[cs:ce],
                               char_start=cs, char_end=ce, line_start=ls, line_end=le,
                               section_path=section_path()))
                else:
                    # No alt to keep — but a *document* consisting solely of
                    # this literal must not leave `blocks` empty, or the
                    # parse_markdown_text `if blocks:` fallback (修3) falls
                    # through to raw-text splitting and dumps the base64
                    # right back in. Emit an empty-caption image Block: the
                    # existing image-branch filters (parsers.py `if not
                    # caption and not asset_id: continue`; kg/parsing.py 修1)
                    # already turn this into zero downstream elements.
                    emit(Block(type="image", text="", raw=text[cs:ce],
                               char_start=cs, char_end=ce, line_start=ls, line_end=le,
                               section_path=section_path()))
                i += 3
                continue
            txt = _inline_text(inline)
            if txt:
                emit(Block(type="paragraph", text=txt, raw=text[cs:ce],
                           char_start=cs, char_end=ce, line_start=ls, line_end=le,
                           section_path=section_path()))
            i += 3
            continue

        i += 1

    return blocks
