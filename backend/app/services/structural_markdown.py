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
            parts.append(c.content)
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


def parse_blocks(text: str) -> List[Block]:
    md = _make_md()
    tokens = md.parse(text)
    offs = _line_char_offsets(text)
    blocks: List[Block] = []

    heading_stack: List[tuple[int, str]] = []
    pending_anchor: Optional[str] = None

    def section_path() -> str:
        return " > ".join(title for _, title in heading_stack)

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
            blocks.append(Block(type="heading", text=title, raw=text[cs:ce],
                                level=level, char_start=cs, char_end=ce,
                                line_start=ls, line_end=le, section_path=section_path(),
                                anchor_id=pending_anchor))
            pending_anchor = None
            i += 3
            continue

        if t.type == "fence":
            cs, ce, ls, le = _span(text, offs, t.map)
            blocks.append(Block(type="code_block", text=t.content.rstrip("\n"),
                                raw=text[cs:ce], lang=(t.info or "").strip(),
                                char_start=cs, char_end=ce, line_start=ls, line_end=le,
                                section_path=section_path()))
            i += 1
            continue

        if t.type == "table_open":
            cs, ce, ls, le = _span(text, offs, t.map)
            blocks.append(Block(type="table", text=_table_text(tokens, i),
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

        if t.type == "list_item_open":
            cs, ce, ls, le = _span(text, offs, t.map)
            txt = ""
            j = i + 1
            while j < n and tokens[j].type != "list_item_close":
                if tokens[j].type == "inline":
                    txt = _inline_text(tokens[j])
                    break
                j += 1
            blocks.append(Block(type="list_item", text=txt, raw=text[cs:ce],
                                char_start=cs, char_end=ce, line_start=ls, line_end=le,
                                section_path=section_path()))
            i += 1
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
                if caption:
                    blocks.append(Block(type="image", text=caption, raw=text[cs:ce],
                                        char_start=cs, char_end=ce, line_start=ls, line_end=le,
                                        section_path=section_path(), metadata={"src": src}))
                i += 3
                continue
            txt = _inline_text(inline)
            if txt:
                blocks.append(Block(type="paragraph", text=txt, raw=text[cs:ce],
                                    char_start=cs, char_end=ce, line_start=ls, line_end=le,
                                    section_path=section_path()))
            i += 3
            continue

        i += 1

    return blocks
