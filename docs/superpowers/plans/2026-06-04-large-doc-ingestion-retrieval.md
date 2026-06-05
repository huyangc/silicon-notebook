# 大型结构化文档稳健摄取与检索 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐条实施。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 在不引入新基建的前提下，让 notebook 正确解析大型结构化技术手册（代码/表格/层级不丢、噪声不进）、可控成本地建 KG（LLM 调用从 ~4330 降到百级、失败可恢复）、并用 numpy 矩阵化检索替掉纯 Python O(N) 余弦。

**Architecture:** 新增单一结构化 Markdown 解析器 `structural_markdown.py`（markdown-it-py），由 `parsers.parse_markdown` 和 `kg/parsing.parse_elements` 两个薄适配器复用；重写 KG 窗口化为"贪心打包+合并碎小节"；`_embed_source` 改逐批落库容错；`retrieval` 增 numpy 相似度助手并保留 `cosine` 作奇偶校验基准。SQLite + 现有依赖（已装 `markdown-it-py 4.0.0`、`numpy 2.4.1`），零新基建。

**Tech Stack:** Python 3.13、FastAPI、SQLite、pydantic v2、markdown-it-py、numpy、pytest。

**对应 spec:** `docs/superpowers/specs/2026-06-04-large-doc-ingestion-retrieval-design.md`

**通用约定：**
- 所有命令默认在仓库根目录执行（当前 worktree 根）。测试统一：`PYTHONPATH=backend python -m pytest <路径> -v`。
- 所有 git commit 末尾加一行：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`（下文命令省略，提交时补上）。
- 每个任务结束都跑该任务新增/相关测试 + 受影响的既有测试，绿了再 commit。

---

## File Structure（决策锁定）

| 文件 | 职责 | 本计划动作 |
|---|---|---|
| `backend/app/services/structural_markdown.py` | **唯一**的结构化 Markdown→块解析（markdown-it-py） | 新增 |
| `backend/app/services/parsers.py` | 文件→`SourceElement`（存储/embedding） | 改 `parse_markdown` 走共享解析 |
| `backend/app/services/kg/parsing.py` | 文件→`SourceElementQ`（KG 窗口化，带 char 跨度） | 改 `parse_elements` 走共享解析 |
| `backend/app/services/kg/windowing.py` | prose→窗口 | 重写 `make_windows` 为打包式 |
| `backend/app/services/kg_ingest.py` | 窗口→KG（LLM） | 加 workers 参数；窗口数照旧由 `KnowledgeGraph.total_windows` 暴露 |
| `backend/app/services/embedding.py` | embedder 工厂 + Fake | 加 `embed_in_chunks` 容错助手 |
| `backend/app/services/embedding_dashscope.py` | dashscope 向量化 | 批大小走 config |
| `backend/app/services/retrieval.py` | 打分/融合 | 加 `cosine_sims`（numpy）；`score_*` 接 sims；保留 `cosine` |
| `backend/app/services/sqlite_repository.py` | 仓库/摄取/ask | `_embed_source` 逐批落库；`_run_extraction` 传 config 旋钮 + 窗口告警；`ask` 用 numpy sims + 矩阵缓存 |
| `backend/app/core/config.py` | 配置 | 新增旋钮 |
| `backend/tests/test_*.py` | 测试 | 新增解析/窗口/容错/检索/特征化测试 |

---

## Phase 0 — 配置旋钮（地基，无行为变化）

### Task 0: 在 Settings 增加可配置旋钮

**Files:**
- Modify: `backend/app/core/config.py`（在 `embed_dim` 字段之后插入）

- [ ] **Step 1: 加配置字段**

在 `backend/app/core/config.py` 的 `embed_dim: int = Field(1024, env="EMBED_DIM")` 之后插入：

```python
    # --- 大文档摄取/检索旋钮（2026-06-04 大文档加固）---
    # KG 窗口化：相邻 prose 贪心打包到 target 字符、相邻窗口 overlap。
    kg_window_target_chars: int = Field(9000, env="KG_WINDOW_TARGET_CHARS")
    kg_window_overlap_chars: int = Field(450, env="KG_WINDOW_OVERLAP_CHARS")
    # KG 抽取并发线程数。
    kg_extract_workers: int = Field(16, env="KG_EXTRACT_WORKERS")
    # 单文档窗口数超过此值 → 记 WARN（不截断、不丢弃，仍全量抽取）。
    kg_window_warn_threshold: int = Field(1200, env="KG_WINDOW_WARN_THRESHOLD")
    # embedding：每条截断长度、每条 API 批大小、落库分块大小。
    embed_truncate_chars: int = Field(2000, env="EMBED_TRUNCATE_CHARS")
    embed_batch_size: int = Field(10, env="EMBED_BATCH_SIZE")
    embed_persist_chunk: int = Field(200, env="EMBED_PERSIST_CHUNK")
    # 检索：top-N 知识对象、top-K 元素。
    retrieval_top_n: int = Field(12, env="RETRIEVAL_TOP_N")
    retrieval_element_limit: int = Field(8, env="RETRIEVAL_ELEMENT_LIMIT")
```

- [ ] **Step 2: 校验可导入**

Run: `PYTHONPATH=backend python -c "from app.core.config import Settings; s=Settings(); print(s.kg_window_target_chars, s.embed_batch_size, s.retrieval_top_n)"`
Expected: 输出 `9000 10 12`

- [ ] **Step 3: Commit**

```bash
git add backend/app/core/config.py
git commit -m "feat(config): 大文档摄取/检索可配置旋钮"
```

---

## Phase 1 — 统一结构化 Markdown 解析（问题 A）

### Task 1.1: 新增 `structural_markdown.py`（核心解析器）

**Files:**
- Create: `backend/app/services/structural_markdown.py`
- Test: `backend/tests/test_structural_markdown.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_structural_markdown.py`：

```python
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
    # 换行保留、命令不被压平
    assert "set_message -severity info\nadd_ring -width 5" in code[0].text


def test_table_is_single_structured_block():
    blocks = parse_blocks(SAMPLE)
    tables = _by_type(blocks, "table")
    assert len(tables) == 1
    # 行列可读、表头与单元格都在
    assert "Option" in tables[0].text and "-arg1" in tables[0].text and "does y" in tables[0].text
    # 原始结构留在 raw
    assert "|" in tables[0].raw


def test_anchor_only_paragraph_dropped_and_id_attached():
    blocks = parse_blocks(SAMPLE)
    # 没有任何块的正文是纯 <a id> 锚点
    assert all("<a id=" not in b.text for b in blocks)
    # 锚点 id 归到紧随其后的标题
    sub = [b for b in blocks if b.type == "heading" and b.text == "Sub Section"][0]
    assert sub.anchor_id == "anchor_x"


def test_image_becomes_caption_block_not_raw_syntax():
    blocks = parse_blocks(SAMPLE)
    imgs = _by_type(blocks, "image")
    assert len(imgs) == 1
    assert imgs[0].text == "A waveform"          # alt 作 caption
    assert imgs[0].metadata.get("src") == "images/wave.png"
    assert "![" not in imgs[0].text               # 不残留 markdown 图片语法


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_structural_markdown.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.structural_markdown`）

- [ ] **Step 3: 写实现**

创建 `backend/app/services/structural_markdown.py`：

```python
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

# 纯 <a id="..."></a> 锚点（可多个连排），整段是噪声 -> 丢弃、id 归到下一个标题。
_ANCHOR_ONLY = re.compile(r'^\s*(?:<a\s+id="[^"]*"\s*>\s*</a>\s*)+$', re.IGNORECASE)
_ANCHOR_ID = re.compile(r'<a\s+id="([^"]*)"', re.IGNORECASE)


@dataclass
class Block:
    type: str                      # heading|paragraph|list_item|code_block|table|image|blockquote
    text: str                      # 供检索/embedding 的可读文本
    raw: str = ""                  # 原始片段（代码块 verbatim、表格原结构）
    level: int = 0                 # heading 1..6，其余 0
    lang: str = ""                 # 代码块语言
    char_start: int = 0
    char_end: int = 0
    line_start: int = 1            # 1-based
    line_end: int = 1
    section_path: str = ""
    anchor_id: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)


def _make_md() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table")


def _line_char_offsets(text: str) -> List[int]:
    """offsets[i] = 第 i 行（0-based）起始字符位；末尾追加 len(text) 作哨兵。"""
    offsets: List[int] = []
    off = 0
    for line in text.split("\n"):
        offsets.append(off)
        off += len(line) + 1  # +1 = '\n'
    offsets.append(len(text))
    return offsets


def _span(text: str, offs: List[int], tok_map) -> tuple[int, int, int, int]:
    """markdown-it token.map=[start_line, end_line)（0-based）-> (char_start, char_end, line_start_1b, line_end_1b)。"""
    l0, l1 = tok_map
    l0 = max(0, min(l0, len(offs) - 1))
    l1 = max(l0 + 1, min(l1, len(offs) - 1))
    char_start = offs[l0]
    char_end = offs[l1]
    return char_start, char_end, l0 + 1, l1


def _inline_text(tok) -> str:
    """inline token -> 纯文本（剥 html、保留图片 alt 文本作占位）。"""
    if tok is None or not tok.children:
        return (tok.content if tok else "") or ""
    parts: List[str] = []
    for c in tok.children:
        if c.type == "text":
            parts.append(c.content)
        elif c.type == "code_inline":
            parts.append(c.content)
        elif c.type == "image":
            parts.append(c.content or "")  # alt
        elif c.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        # html_inline（如 <a id>）忽略
    return "".join(parts).strip()


def _table_text(tokens, i: int) -> str:
    """从 table_open(i) 起，按行收集 th/td 文本，行内 ' | ' 连接、行间 ' ; ' 连接。"""
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

    # heading 栈 -> section 面包屑
    heading_stack: List[tuple[int, str]] = []  # (level, title)
    pending_anchor: Optional[str] = None

    def section_path() -> str:
        return " > ".join(title for _, title in heading_stack)

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]

        if t.type == "heading_open":
            level = int(t.tag[1])  # h2 -> 2
            inline = tokens[i + 1] if i + 1 < n else None
            title = _inline_text(inline)
            cs, ce, ls, le = _span(text, offs, t.map)
            # 维护栈：弹出 >= 当前 level 的，再压入
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(Block(type="heading", text=title, raw=text[cs:ce],
                                level=level, char_start=cs, char_end=ce,
                                line_start=ls, line_end=le, section_path=section_path(),
                                anchor_id=pending_anchor))
            pending_anchor = None
            i += 3  # heading_open, inline, heading_close
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
            # 跳到 table_close
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
            # li 内首个 inline 作为条目文本
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
            # 纯锚点段落：丢弃，id 记给下一个标题
            if _ANCHOR_ONLY.match(raw_inline):
                m = _ANCHOR_ID.search(raw_inline)
                if m:
                    pending_anchor = m.group(1)
                i += 3
                continue
            # 仅含单张图片的段落：作 image 块（alt 作 caption）
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_structural_markdown.py -v`
Expected: 全部 PASS（8 项）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/structural_markdown.py backend/tests/test_structural_markdown.py
git commit -m "feat(parse): 新增结构化 Markdown 解析器(markdown-it-py)"
```

---

### Task 1.2: `parsers.parse_markdown` 改走共享解析

**Files:**
- Modify: `backend/app/services/parsers.py:81-141`（替换 `parse_markdown`）
- Test: `backend/tests/test_parsers_markdown.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_parsers_markdown.py`：

```python
from pathlib import Path
from app.services.parsers import parse_markdown

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_parsers_markdown.py -v`
Expected: FAIL（现 `parse_markdown` 无 `code_block`/`table` 类型、有锚点段落）

- [ ] **Step 3: 写实现**

替换 `backend/app/services/parsers.py` 中整个 `parse_markdown`（第 81–141 行）为：

```python
def parse_markdown(source_id: str, path: Path) -> List[SourceElement]:
    from app.services.structural_markdown import parse_blocks

    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = parse_blocks(text)
    elements: List[SourceElement] = []
    counters: Dict[str, int] = {}
    for block in blocks:
        counters[block.type] = counters.get(block.type, 0) + 1
        ordinal = counters[block.type]
        metadata: Dict[str, Any] = {
            "parser": "markdown",
            "section_path": block.section_path,
            "char_start": block.char_start,
            "char_end": block.char_end,
            "line_start": block.line_start,
            "line_end": block.line_end,
        }
        if block.type == "heading":
            metadata["heading_level"] = block.level
            if block.anchor_id:
                metadata["anchor_id"] = block.anchor_id
        if block.type == "code_block":
            metadata["lang"] = block.lang
        if block.type == "image":
            metadata.update(block.metadata)  # src
        elements.append(
            _element(
                source_id,
                block.type,
                f"Markdown {block.type} {ordinal}",
                block.text,
                metadata,
            )
        )
    return elements or parse_plain_text(source_id, path, "markdown")
```

注：`_element`（第 524 行）会做 `" ".join(text.split())` 压平——这会把代码块换行也压掉。**需让代码块/表格保真**。改 `_element`（第 531 行）为按类型保留：

替换 `backend/app/services/parsers.py:524-539` 的 `_element`：

```python
def _element(
    source_id: str,
    element_type: str,
    location_label: str,
    text: str,
    metadata: Dict[str, Any],
) -> SourceElement:
    # 代码块/表格保真（保留换行/结构）；其余压平空白。
    if element_type in ("code_block", "table"):
        clean_text = text.strip("\n")
    else:
        clean_text = " ".join(text.split())
    return SourceElement(
        id="",
        source_id=source_id,
        element_type=element_type,
        location_label=location_label,
        text=clean_text,
        metadata=metadata,
    )
```

- [ ] **Step 4: 跑测试确认通过 + 既有解析测试不回归**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_parsers_markdown.py backend/tests/test_structural_markdown.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsers.py backend/tests/test_parsers_markdown.py
git commit -m "feat(parse): parse_markdown 走结构化解析(代码/表格保真,丢锚点)"
```

---

### Task 1.3: `kg/parsing.parse_elements` 改走共享解析（保留 SourceElementQ 契约）

**Files:**
- Modify: `backend/app/services/kg/parsing.py:73-140`（替换 `parse_elements`）
- Test: `backend/tests/test_kg_parsing_structural.py`

约束：`SourceElementQ.text` 仍是 `text[char_start:char_end]` 的契约不变；`make_windows._PROSE_TYPES` 需要 code_block 进入窗口 → 映射 code_block→`paragraph`、image→`figure_caption`、table→`table`、list_item→`list_item`、heading→`heading`。**保证 `test_kg_ingest.py` 既有窗口测试仍绿。**

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_kg_parsing_structural.py`：

```python
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


def test_code_block_is_prose_element_for_windowing():
    els = parse_elements(MD, "doc.md", None)
    # code 块作为 paragraph 型 prose（会被窗口化、喂给 LLM）
    code = [e for e in els if "set_db x y" in e.text]
    assert code and code[0].type == "paragraph"


def test_no_anchor_prose_elements():
    els = parse_elements(MD, "doc.md", None)
    assert all("<a id=" not in e.text for e in els)


def test_table_element_present():
    els = parse_elements(MD, "doc.md", None)
    assert any(e.type == "table" and "v1" in e.text for e in els)


def test_char_spans_round_trip():
    els = parse_elements(MD, "doc.md", None)
    for e in els:
        # 契约：text 是源切片（允许首尾空白差异）
        assert e.char_start <= e.char_end <= len(MD)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_kg_parsing_structural.py -v`
Expected: FAIL（旧 `parse_elements` 把代码块逐行当 paragraph、锚点成 prose）

- [ ] **Step 3: 写实现**

替换 `backend/app/services/kg/parsing.py` 的 `parse_elements`（第 73–140 行）为（保留模块顶部其余函数 `build_section_tree` 等不动）：

```python
# 结构化块类型 -> SourceElementQ.type（保证 code 进 prose 被窗口化）
_QTYPE_MAP = {
    "heading": "heading",
    "paragraph": "paragraph",
    "list_item": "list_item",
    "code_block": "paragraph",
    "table": "table",
    "image": "figure_caption",
    "blockquote": "paragraph",
}


def parse_elements(
    text: str, source_file: str, line_range: Optional[List[int]] = None
) -> List[SourceElementQ]:
    from app.services.structural_markdown import parse_blocks

    blocks = parse_blocks(text)
    lo, hi = (line_range or [1, len(text.split("\n"))])
    elements: List[SourceElementQ] = []
    counter = 0
    for b in blocks:
        if not (lo <= b.line_start <= hi):
            continue
        raw = text[b.char_start:b.char_end]
        if not raw.strip():
            continue
        counter += 1
        elements.append(SourceElementQ(
            id=f"SE-{b.line_start}-{counter}",
            type=_QTYPE_MAP.get(b.type, "paragraph"),
            file=source_file,
            line_start=b.line_start, line_end=b.line_end,
            char_start=b.char_start, char_end=b.char_end,
            text=raw,
        ))
    return elements
```

注：`build_section_tree`（第 161 行起）仍按 heading 元素工作；保持不动。

- [ ] **Step 4: 跑测试 + 既有 KG 测试不回归（关键）**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_kg_parsing_structural.py backend/tests/test_kg_ingest.py backend/tests/kg/ -v`
Expected: 全部 PASS（尤其 `test_extract_graph_counts_failed_windows`、`test_canonicalize_merges_across_windows`、`test_windowing.py`）

> 若 `backend/tests/kg/test_windowing.py` 因元素粒度变化失败：核对断言是否依赖"逐行 paragraph"。结构化后一段多行 prose 合为一个元素属预期改进——按新粒度更新该断言（保持其语义意图：窗口/section 划分正确）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/parsing.py backend/tests/test_kg_parsing_structural.py
git commit -m "feat(kg): parse_elements 走结构化解析(代码入prose,丢锚点)"
```

---

## Phase 2 — KG 成本护栏 + Embedding 容错（问题 B）

### Task 2.1: 重写 `make_windows` 为"贪心打包 + 合并碎小节"

**Files:**
- Modify: `backend/app/services/kg/windowing.py:24-49`（替换 `make_windows`）
- Test: `backend/tests/test_windowing_packing.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_windowing_packing.py`：

```python
from app.services.kg.windowing import make_windows, windows_with_elements


def test_many_tiny_sections_are_merged():
    # 20 个各自只有一句话的小节；target 足够大 -> 应远少于 20 个窗口
    md = "".join(f"## S{i}\n\nshort sentence number {i}.\n\n" for i in range(20))
    wins = make_windows(md, "doc.md", None, n=9000, m=450)
    assert 1 <= len(wins) <= 3, f"碎小节应被合并, got {len(wins)}"


def test_oversized_section_is_split_with_overlap():
    big = "word " * 4000  # ~20000 chars 单段
    md = f"## Big\n\n{big}\n"
    wins = make_windows(md, "doc.md", None, n=9000, m=450)
    assert len(wins) >= 2
    # 相邻窗口有重叠
    assert wins[1].char_start < wins[0].char_end


def test_two_small_sections_still_two_windows_when_target_tiny():
    # 复刻 test_kg_ingest 的前提：n=40 时两小节仍切出 >=2 窗口
    text = "# A\n\nEngram is a memory architecture\n\n# B\n\nEngram is a memory architecture indeed.\n\n"
    pairs = windows_with_elements(text, "doc.md", None, 40, 5)
    assert len(pairs) >= 2


def test_windows_pair_with_overlapping_prose():
    md = "## S\n\n" + ("alpha " * 2000) + "\n"
    pairs = windows_with_elements(md, "doc.md", None, n=9000, m=450)
    assert all(els for _, els in pairs)  # 每个窗口都配到了 prose 元素
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_windowing_packing.py -v`
Expected: FAIL（`test_many_tiny_sections_are_merged`：旧逻辑每 section 一窗 → 20 窗）

- [ ] **Step 3: 写实现**

替换 `backend/app/services/kg/windowing.py` 的 `make_windows`（第 24–49 行）为：

```python
def make_windows(text: str, source_file: str, line_range: Optional[List[int]],
                 n: int = 9000, m: int = 450) -> List[Window]:
    """按文档顺序把 prose 元素贪心打包到 ~n 字符的窗口（吸收碎小相邻小节）。
    单个超 n 的元素在其跨度内按 step=n-m 切并保留 overlap。窗口 section_path
    取打包起点元素所在小节。相邻窗口重叠 ~m 字符。"""
    elements = parse_elements(text, source_file, line_range)
    sections = build_section_tree(elements)
    headings = [e for e in elements if e.type == "heading"]
    sec_by_line = sorted((h.line_start, s.path) for h, s in zip(headings, sections))
    prose = [e for e in elements if e.type in _PROSE_TYPES]
    prose.sort(key=lambda e: e.char_start)

    windows: List[Window] = []
    step = max(1, n - m)
    i = 0
    while i < len(prose):
        w_start = prose[i].char_start
        sec = _section_of_line(prose[i].line_start, sec_by_line)
        j = i
        while j < len(prose) and (prose[j].char_end - w_start) <= n:
            j += 1
        if j == i:
            # 单元素超 n：在其跨度内切
            e = prose[i]
            s = e.char_start
            while s < e.char_end:
                windows.append(Window(char_start=s, char_end=min(s + n, e.char_end),
                                      section_path=sec, file=source_file))
                if s + n >= e.char_end:
                    break
                s += step
            i += 1
            continue
        w_end = prose[j - 1].char_end
        windows.append(Window(char_start=w_start, char_end=w_end,
                              section_path=sec, file=source_file))
        # 推进并保留 ~m overlap：回退到首个 char_start >= w_end-m 的元素
        nxt = i + 1
        for k in range(i + 1, j):
            if prose[k].char_start >= w_end - m:
                nxt = k
                break
        else:
            nxt = j
        i = max(i + 1, nxt)
    windows.sort(key=lambda w: w.char_start)
    return windows
```

- [ ] **Step 4: 跑测试 + 既有 KG 窗口测试不回归（关键）**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_windowing_packing.py backend/tests/test_kg_ingest.py backend/tests/kg/test_windowing.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/windowing.py backend/tests/test_windowing_packing.py
git commit -m "feat(kg): 窗口化改贪心打包+合并碎小节(4330->百级)"
```

---

### Task 2.2: `extract_graph` 接 workers 参数；`_run_extraction` 传 config 旋钮 + 窗口告警

**Files:**
- Modify: `backend/app/services/kg_ingest.py:16,97-127`
- Modify: `backend/app/services/sqlite_repository.py:1094-1102`
- Test: `backend/tests/test_kg_ingest.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_kg_ingest.py` 末尾追加：

```python
def test_extract_graph_accepts_workers_param():
    import json
    payload = json.dumps({"nodes": [{"local_id": "a", "type": "Concept",
                                      "name": "Engram", "ev": 0}], "edges": []})
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic", workers=4)
    assert g.total_windows >= 1
    assert any(n.name == "Engram" for n in g.nodes)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_kg_ingest.py::test_extract_graph_accepts_workers_param -v`
Expected: FAIL（`extract_graph() got an unexpected keyword argument 'workers'`）

- [ ] **Step 3: 写实现**

`backend/app/services/kg_ingest.py`：把 `extract_graph` 签名（第 97–98 行）改为接受 `workers`：

```python
def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, workers: int = _WORKERS) -> KnowledgeGraph:
```

并把第 110 行 `workers = max(1, min(_WORKERS, len(pairs)))` 改为：

```python
        workers = max(1, min(workers, len(pairs)))
```

`backend/app/services/sqlite_repository.py` 的 `_run_extraction`：把第 1095–1096 行的 `extract_graph(...)` 调用改为传入 config 旋钮，并在拿到 `graph` 后做窗口告警。替换第 1094–1102 行：

```python
            raw_text = self._source_raw_text(source, elements)
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=self.settings.kg_window_target_chars,
                m=self.settings.kg_window_overlap_chars,
                workers=self.settings.kg_extract_workers,
            )
            warn = self.settings.kg_window_warn_threshold
            if graph.total_windows > warn:
                self.event_log.logger.warning(
                    "KG windows %s exceed warn threshold %s for source %s (%s) — "
                    "extracting in full, no truncation",
                    graph.total_windows, warn, source_id, source.file_name,
                )
            objects, relations = kg_ingest.build_records(graph, source.id, source.title, elements)
            n_obj, n_rel = self.store_kg(source.notebook_id, source.id, objects, relations)
            fw, tw = graph.failed_windows, graph.total_windows
            with self._connect() as db:
                db.execute("UPDATE extraction_runs SET status='completed', error_message=?, updated_at=? WHERE id=?",
                           (f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type} windows_failed={fw}/{tw}", _now(), run_id))
```

- [ ] **Step 4: 跑测试 + 编译校验**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_kg_ingest.py -v`
Expected: 全部 PASS

Run: `PYTHONPATH=backend python -c "import app.services.sqlite_repository"`
Expected: 无异常

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_ingest.py backend/app/services/sqlite_repository.py backend/tests/test_kg_ingest.py
git commit -m "feat(kg): 窗口/并发走config + 窗口数超阈值告警(不截断)"
```

---

### Task 2.3: `embed_in_chunks` 容错助手

**Files:**
- Modify: `backend/app/services/embedding.py`（追加函数）
- Test: `backend/tests/test_embed_resilience.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_embed_resilience.py`：

```python
from app.services.embedding import embed_in_chunks


def test_failed_chunk_does_not_lose_others():
    texts = [f"t{i}" for i in range(25)]

    def embed_fn(batch):
        if "t10" in batch:                 # 第二个 chunk(10..19) 整批失败
            raise RuntimeError("boom")
        return [[float(len(t))] for t in batch]

    out = embed_in_chunks(embed_fn, texts, chunk_size=10)
    assert len(out) == 25                  # 与输入对齐
    assert out[0] == [2.0]                 # chunk0 成功
    assert out[10] is None and out[19] is None   # chunk1 整批失败 -> None
    assert out[20] == [3.0]                # chunk2 成功（'t20' 长度3）


def test_all_success():
    out = embed_in_chunks(lambda b: [[1.0] for _ in b], ["a", "b", "c"], chunk_size=2)
    assert out == [[1.0], [1.0], [1.0]]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_embed_resilience.py -v`
Expected: FAIL（`ImportError: cannot import name 'embed_in_chunks'`）

- [ ] **Step 3: 写实现**

在 `backend/app/services/embedding.py` 末尾追加：

```python
def embed_in_chunks(embed_fn, texts, chunk_size=200, logger=None):
    """逐块调用 embed_fn，单块异常则该块全记 None 并继续（不影响其余块）。
    返回与 texts 对齐的列表，元素为向量或 None。embed_fn(list[str]) -> list[vector]。"""
    out = [None] * len(texts)
    for start in range(0, len(texts), chunk_size):
        chunk = texts[start:start + chunk_size]
        try:
            vectors = embed_fn(chunk)
        except Exception as exc:  # noqa: BLE001 — best-effort，单块失败不阻塞全篇
            if logger is not None:
                logger.warning("embed chunk [%s:%s] failed: %s", start, start + len(chunk), exc)
            continue
        for offset, vec in enumerate(vectors):
            out[start + offset] = list(vec)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_embed_resilience.py -v`
Expected: PASS（2 项）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/embedding.py backend/tests/test_embed_resilience.py
git commit -m "feat(embed): embed_in_chunks 单块失败隔离助手"
```

---

### Task 2.4: `_embed_source` 改逐块落库；批大小走 config

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:1121-1149`（重写 `_embed_source`）
- Modify: `backend/app/services/embedding_dashscope.py:7,27-33`（批大小走 config）
- Test: `backend/tests/test_embed_resilience.py`（追加 dashscope 批大小用例）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_embed_resilience.py` 末尾追加：

```python
def test_dashscope_batch_size_from_config(monkeypatch):
    import app.services.embedding_dashscope as mod
    sizes = []
    class _Emb:
        def create(self, model, input):
            sizes.append(len(input))
            return type("R", (), {"data": [type("D", (), {"embedding": [0.1]})() for _ in input]})()
    class _Client:
        embeddings = _Emb()
    monkeypatch.setattr(mod, "OpenAI", lambda **kw: _Client())
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://x"); monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "5")
    from app.core.config import Settings
    e = mod.DashscopeEmbedder(Settings())
    e.embed_texts([f"t{i}" for i in range(12)])
    assert sizes and max(sizes) <= 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_embed_resilience.py::test_dashscope_batch_size_from_config -v`
Expected: FAIL（当前 `_BATCH=10` 硬编码，max size=10）

- [ ] **Step 3: 写实现**

`backend/app/services/embedding_dashscope.py`：把批大小与截断改为读 settings。替换第 27–33 行 `embed_texts`：

```python
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        batch = max(1, min(getattr(self.settings, "embed_batch_size", 10), 10))
        trunc = getattr(self.settings, "embed_truncate_chars", 2000)
        out: List[List[float]] = []
        for i in range(0, len(texts), batch):
            chunk = [t[:trunc] for t in texts[i:i + batch]]
            resp = self._ensure().embeddings.create(model=self.model, input=chunk)
            out.extend(list(d.embedding) for d in resp.data)
        return out
```

（`_BATCH = 10` 常量可保留作上限注释，不再直接用。）

`backend/app/services/sqlite_repository.py`：重写 `_embed_source`（第 1121–1149 行）为逐块落库：

```python
    def _embed_source(self, source_id: str) -> None:
        if not self.settings.embedder_configured:
            return
        source = self.get_source(source_id)
        elements = self.source_elements(source_id)
        pending = [el for el in elements if el.text.strip()]
        if not pending:
            return
        from app.services.embedding import embed_in_chunks
        trunc = self.settings.embed_truncate_chars
        texts = [el.text[:trunc] for el in pending]
        vectors = embed_in_chunks(
            self.embedder.embed_texts, texts,
            chunk_size=self.settings.embed_persist_chunk,
            logger=self.event_log.logger,
        )
        now = _now()
        stored = 0
        with self._connect() as db:
            for element, vector in zip(pending, vectors):
                if vector is None:
                    continue
                stored += 1
                db.execute(
                    """
                    INSERT OR REPLACE INTO element_embeddings
                    (element_id, source_id, notebook_id, vector, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (element.id, source_id, source.notebook_id, json.dumps(vector), now),
                )
        self.event_log.logger.info(
            "embedded %s/%s elements for source %s", stored, len(pending), source_id
        )
```

- [ ] **Step 4: 跑测试 + 编译校验**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_embed_resilience.py backend/tests/test_embedding.py -v`
Expected: 全部 PASS（注：`test_embedding.py::test_dashscope_embedder_batches_and_no_retries` 仍应绿——3 条 ≤ 默认 10 一批）

Run: `PYTHONPATH=backend python -c "import app.services.sqlite_repository"`
Expected: 无异常

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/services/embedding_dashscope.py backend/tests/test_embed_resilience.py
git commit -m "feat(embed): _embed_source 逐块落库容错; 批大小走config"
```

---

## Phase 3 — numpy 矩阵化检索（问题 C）

### Task 3.1: `cosine_sims` numpy 助手 + 奇偶校验

**Files:**
- Modify: `backend/app/services/retrieval.py`（追加 `cosine_sims`，保留 `cosine`）
- Test: `backend/tests/test_retrieval_numpy.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_retrieval_numpy.py`：

```python
import math
from app.services.retrieval import cosine, cosine_sims


def test_cosine_sims_matches_cosine():
    q = [0.1, 0.2, 0.3, 0.4]
    vecs = {
        "a": [0.1, 0.2, 0.3, 0.4],   # 同向 -> 1.0
        "b": [0.4, 0.3, 0.2, 0.1],
        "c": [-0.1, -0.2, -0.3, -0.4],  # 反向 -> -1.0
    }
    sims = cosine_sims(q, vecs)
    for k, v in vecs.items():
        assert math.isclose(sims[k], cosine(q, v), abs_tol=1e-6)
    assert math.isclose(sims["a"], 1.0, abs_tol=1e-6)


def test_cosine_sims_empty_and_zero():
    assert cosine_sims([], {"a": [1.0]}) == {}
    assert cosine_sims([1.0, 0.0], {}) == {}
    # 零向量 -> 0
    assert cosine_sims([0.0, 0.0], {"z": [0.0, 0.0]})["z"] == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_retrieval_numpy.py -v`
Expected: FAIL（`ImportError: cannot import name 'cosine_sims'`）

- [ ] **Step 3: 写实现**

在 `backend/app/services/retrieval.py` 的 `cosine`（第 149 行）之后追加：

```python
def cosine_sims(query_vector, id_to_vec):
    """一次矩阵运算算出 query 对一批向量的余弦相似度。返回 {id: sim}。
    等价于对每个 id 调 cosine(query_vector, vec)，但用 numpy 批量计算。"""
    import numpy as np

    if not query_vector or not id_to_vec:
        return {}
    ids = list(id_to_vec.keys())
    mat = np.asarray([id_to_vec[i] for i in ids], dtype=np.float64)  # [N, dim]
    q = np.asarray(query_vector, dtype=np.float64)                   # [dim]
    if mat.ndim != 2 or mat.shape[1] != q.shape[0]:
        # 维度不齐 -> 退回逐条 cosine，保证健壮
        return {i: cosine(query_vector, id_to_vec[i]) for i in ids}
    qn = float(np.linalg.norm(q))
    row_norms = np.linalg.norm(mat, axis=1)               # [N]
    denom = row_norms * qn
    dots = mat @ q                                        # [N]
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(denom > 0, dots / denom, 0.0)
    return {i: float(s) for i, s in zip(ids, sims)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_retrieval_numpy.py -v`
Expected: PASS（2 项）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_retrieval_numpy.py
git commit -m "feat(retrieval): cosine_sims numpy 批量余弦助手"
```

---

### Task 3.2: `score_*` 接预算 sims；`ask()` 走 numpy 路径

**Files:**
- Modify: `backend/app/services/retrieval.py`（`score_knowledge` 第 223-284 行、`score_elements` 第 299-328 行）
- Modify: `backend/app/services/sqlite_repository.py:2611-2634`（ask 计算并下传 sims；top-N 走 config）
- Test: `backend/tests/test_retrieval_numpy.py`（追加）

设计：给 `score_knowledge`/`score_elements` 增可选参数 `element_sims`/`knowledge_sims`（`{id: float}`）。提供时用查表，不提供时退回 `cosine()`（保持既有调用/测试不变）。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_retrieval_numpy.py` 末尾追加：

```python
from app.services.retrieval import score_elements


def test_score_elements_uses_precomputed_sims():
    q = [1.0, 0.0]
    elements = [
        {"element_id": "e1", "source_id": "s", "element_type": "paragraph",
         "text": "alpha beta", "vector": [1.0, 0.0]},
    ]
    # 传入与向量不一致的 sims，证明走的是 sims 而非重算 vector
    out = score_elements(q, elements, query_vector=q,
                         element_sims={"e1": 0.99}, limit=8)
    assert out and abs(out[0].score - (0.4 * 0 + 0.6 * 0.99) / 1.0) < 0.05
```

（说明：关键词分对 "alpha beta" 与 query 数字无重叠→0，语义=0.99，融合≈0.594。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_retrieval_numpy.py::test_score_elements_uses_precomputed_sims -v`
Expected: FAIL（`score_elements() got an unexpected keyword argument 'element_sims'`）

- [ ] **Step 3: 写实现**

`backend/app/services/retrieval.py`，`score_elements`（第 299–304 行）签名加参数，并改语义计算：

```python
def score_elements(
    query: str,
    elements: List[dict],
    query_vector: Optional[List[float]] = None,
    limit: int = 8,
    element_sims: Optional[Dict[str, float]] = None,
) -> List[RetrievedElement]:
```

把第 308–312 行的语义计算改为：

```python
        semantic = 0.0
        vector = element.get("vector")
        has_vector = bool(query_vector and (element_sims is not None or vector))
        if has_vector:
            if element_sims is not None:
                semantic = element_sims.get(element["element_id"], 0.0)
            else:
                semantic = cosine(query_vector, vector)
```

`score_knowledge`（第 223–231 行）签名加参数：

```python
def score_knowledge(
    query: str,
    objects: List[dict],
    object_type: str,
    query_vector: Optional[List[float]] = None,
    element_vectors: Optional[Dict[str, List[float]]] = None,
    knowledge_vectors: Optional[Dict[str, List[float]]] = None,
    scenario: Optional[Dict[str, str]] = None,
    element_sims: Optional[Dict[str, float]] = None,
    knowledge_sims: Optional[Dict[str, float]] = None,
) -> List[RetrievedKnowledge]:
```

把第 250–262 行的语义计算改为优先查 sims：

```python
        semantic = 0.0
        has_vector = False
        if query_vector:
            if knowledge_sims is not None:
                s = knowledge_sims.get(object_id)
                if s is not None:
                    has_vector = True
                    semantic = max(semantic, s)
            elif knowledge_vectors:
                payload_vec = knowledge_vectors.get(object_id)
                if payload_vec:
                    has_vector = True
                    semantic = max(semantic, cosine(query_vector, payload_vec))
            for ev in evidence:
                eid = getattr(ev, "element_id", "") or ""
                if element_sims is not None:
                    s = element_sims.get(eid)
                    if s is not None:
                        has_vector = True
                        semantic = max(semantic, s)
                elif element_vectors:
                    vector = element_vectors.get(eid)
                    if vector:
                        has_vector = True
                        semantic = max(semantic, cosine(query_vector, vector))
```

`backend/app/services/sqlite_repository.py`，`ask()`：在第 2615 行 `element_vectors = self._element_vectors(elements)` 之后、打分循环之前，计算 sims；并把 `_TOP_N` 换成 config。替换第 2615–2634 行：

```python
        element_vectors = self._element_vectors(elements)

        from app.services.retrieval import cosine_sims
        element_sims = cosine_sims(query_vector, element_vectors) if query_vector else None
        knowledge_sims = cosine_sims(query_vector, knowledge_vectors) if query_vector else None

        scored_all: List[RetrievedKnowledge] = []
        for t in _KG_TYPES:
            objs = kg_objs[t]
            if not objs:
                continue
            scored_all.extend(
                score_knowledge(
                    query, objs, t, query_vector, element_vectors, knowledge_vectors, None,
                    element_sims=element_sims, knowledge_sims=knowledge_sims,
                )
            )
        scored_all.sort(
            key=lambda it: it.score * _TYPE_WEIGHT.get(it.object_type, 0.5),
            reverse=True,
        )
        top_hits: List[RetrievedKnowledge] = scored_all[:self.settings.retrieval_top_n]
```

- [ ] **Step 4: 跑测试 + 既有检索测试不回归**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_retrieval_numpy.py backend/tests/test_retrieval.py backend/tests/test_ask_redesign.py -v`
Expected: 全部 PASS

Run: `PYTHONPATH=backend python -c "import app.services.sqlite_repository"`
Expected: 无异常

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/app/services/sqlite_repository.py backend/tests/test_retrieval_numpy.py
git commit -m "feat(retrieval): ask 走numpy批量余弦; score_* 接预算sims; top-N走config"
```

---

### Task 3.3: 每 notebook 向量矩阵缓存（避免每次 ask 重解析 JSON）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_element_vectors`/`_knowledge_vectors` 或新增缓存层 + 失效钩子）
- Test: `backend/tests/test_vector_cache.py`

说明：当前每次 `ask` 都从 SQLite 读 + `json.loads` 全部向量。numpy 矩阵已消除 Python 循环；本任务再缓存"已解析向量字典"，键 = (notebook_id, 向量行数, max(created_at))，摄取后自动失效。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_vector_cache.py`：

```python
from app.services.vector_cache import VectorCache


def test_cache_hit_and_version_invalidation():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"e1": [1.0, 0.0]}

    c = VectorCache()
    v1 = c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    v2 = c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    assert v1 == v2 and calls["n"] == 1          # 同版本命中，不重复 loader

    c.get("nb1", version=("count=2", "ts=20"), loader=loader)
    assert calls["n"] == 2                        # 版本变 -> 重新 loader

    c.invalidate("nb1")
    c.get("nb1", version=("count=2", "ts=20"), loader=loader)
    assert calls["n"] == 3                        # 失效后重载
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_vector_cache.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.vector_cache`）

- [ ] **Step 3: 写实现**

创建 `backend/app/services/vector_cache.py`：

```python
"""进程内每-notebook 向量字典缓存（单用户单进程足够）。
版本键变化（向量行数/最新时间戳）即自动重载；摄取/删除时显式 invalidate。"""
from __future__ import annotations

from typing import Callable, Dict, Hashable, Tuple


class VectorCache:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[Hashable, dict]] = {}

    def get(self, key: str, version: Hashable, loader: Callable[[], dict]) -> dict:
        cached = self._store.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        value = loader()
        self._store[key] = (version, value)
        return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
```

接入 `backend/app/services/sqlite_repository.py`（接线步骤，最小改动）：
1. 在仓库 `__init__` 里加：`self._vector_cache = VectorCache()`（import：`from app.services.vector_cache import VectorCache`）。
2. 新增私有方法读版本键：

```python
    def _embedding_version(self, db, notebook_id: str):
        row = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            "FROM element_embeddings WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        krow = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            "FROM knowledge_embeddings WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return (row["c"], row["ts"], krow["c"], krow["ts"])
```

3. 在 `ask()` 里把 `knowledge_vectors`/`element_vectors` 的获取包一层缓存（用 `_embedding_version` 作 version、原加载逻辑作 loader）。
4. 在 `_invalidate_unified_cache` 同址追加 `self._vector_cache.invalidate(notebook_id)`，并在 `process_source` 成功末尾、`store_kg`、`delete_source` 路径确保调用到（这些位置已调用 `_invalidate_unified_cache` 或 `rebuild_unified_kg`；在 `_invalidate_unified_cache` 内一并失效即可覆盖）。

- [ ] **Step 4: 跑测试 + 编译 + ask 冒烟**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_vector_cache.py backend/tests/test_ask_redesign.py -v`
Expected: 全部 PASS

Run: `PYTHONPATH=backend python -c "import app.services.sqlite_repository"`
Expected: 无异常

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/vector_cache.py backend/app/services/sqlite_repository.py backend/tests/test_vector_cache.py
git commit -m "feat(retrieval): 每notebook向量矩阵缓存(版本键失效)"
```

---

## Phase 4 — 集成与真机验证

### Task 4.1: Innovus 样本特征化测试（缺样本则 skip）

**Files:**
- Test: `backend/tests/test_innovus_characterization.py`

- [ ] **Step 1: 写测试**

创建 `backend/tests/test_innovus_characterization.py`：

```python
import os
import pathlib
import pytest

from app.services.structural_markdown import parse_blocks
from app.services.kg.windowing import make_windows

SAMPLE = pathlib.Path(
    os.environ.get("INNOVUS_SAMPLE", "/Users/hzf/Downloads/doc/innovusUG/innovusUG_complete.md")
)


@pytest.fixture
def text():
    if not SAMPLE.exists():
        pytest.skip(f"Innovus 样本缺失: {SAMPLE}")
    return SAMPLE.read_text(encoding="utf-8", errors="replace")


def test_no_anchor_noise_blocks(text):
    blocks = parse_blocks(text)
    anchor_blocks = [b for b in blocks if "<a id=" in b.text]
    assert anchor_blocks == [], f"不应有锚点噪声块, got {len(anchor_blocks)}"


def test_has_intact_code_blocks(text):
    blocks = parse_blocks(text)
    code = [b for b in blocks if b.type == "code_block"]
    assert len(code) >= 100               # 手册含大量命令块
    assert any("\n" in b.text for b in code)  # 至少有多行代码整块保留


def test_window_count_is_hundreds_not_thousands(text):
    wins = make_windows(text, "innovus.md", None, n=9000, m=450)
    assert len(wins) < 1200, f"窗口数应为百级(<warn阈值), got {len(wins)}"
```

- [ ] **Step 2: 跑测试**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_innovus_characterization.py -v`
Expected: PASS（样本在场时）或 SKIP（缺样本时）。窗口数应远小于旧的 4330。

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_innovus_characterization.py
git commit -m "test: Innovus 样本特征化(锚点≈0/代码整块/窗口百级)"
```

---

### Task 4.2: 全量回归 + check.sh + 真机重摄取验证

**Files:** 无（验证任务）

- [ ] **Step 1: 跑全量后端测试**

Run: `PYTHONPATH=backend python -m pytest backend/tests/ -v`
Expected: 全绿（既有 + 新增）

- [ ] **Step 2: 跑项目检查脚本（py_compile + smoke + 前端 lint）**

Run: `bash scripts/check.sh`
Expected: 无错误退出（smoke_backend 通过）

- [ ] **Step 3: 真机重摄取验证（依赖 root master 服务，按用户偏好）**

按用户偏好"基于 root master 启动服务"。在 root master 跑后端后：
1. 上传或对已存在的大文档源调用 `POST /sources/{source_id}/parse` 触发重摄取（`process_source` 会先清旧 elements/embeddings/KG 再重跑——清理逻辑已存在于第 873–880、1081–1083 行）。
2. 观察 `.local/logs/events.jsonl`：`extract` 阶段窗口数应为百级；若超 `kg_window_warn_threshold` 应有 WARN 但不截断。
3. 观察 `embed` 阶段日志 `embedded N/M elements`；制造一次 embedder 故障验证非全篇丢失（可选）。
4. 提一个针对手册命令的问题（如 U1），确认答案能引用到未压平的命令/表格原文，且检索响应为亚秒级。

- [ ] **Step 4: 收尾**

确认所有任务 commit 完成后，按 superpowers:finishing-a-development-branch 决定合并/PR。

---

## Self-Review（对照 spec 核对）

**1. Spec coverage：**
- §4.A 结构化解析 → Task 1.1/1.2/1.3 ✓（代码整块、表格结构化、锚点丢弃、图片 caption、section_path、char 跨度）
- §4.B1 高效窗口化 → Task 2.1 ✓；§4.B2 安全阀 → Task 2.2 ✓；§4.B3 embedding 逐批容错 → Task 2.3/2.4 ✓；§4.B4 配置旋钮 → Task 0 + 2.2 + 2.4 ✓
- §4.C numpy 检索 → Task 3.1/3.2 ✓；矩阵缓存 → Task 3.3 ✓；top-N 可配 → Task 3.2 ✓
- §6 迁移/重摄取 → Task 4.2（复用已存在的清理逻辑）✓
- §7 测试 → 各 Task 的测试 + Task 4.1 特征化 ✓

**2. Placeholder scan：** 无 TBD/TODO；所有代码步骤含完整代码与命令。✓

**3. Type consistency：**
- `Block` 字段（type/text/raw/level/lang/char_start/char_end/line_start/line_end/section_path/anchor_id/metadata）在 1.1 定义，1.2/1.3/4.1 一致引用。✓
- `parse_blocks(text)->List[Block]`、`parse_elements(text, file, line_range)->List[SourceElementQ]`、`make_windows(text, file, line_range, n, m)`、`extract_graph(..., n, m, workers)`、`cosine_sims(query_vector, id_to_vec)->{id:float}`、`embed_in_chunks(embed_fn, texts, chunk_size, logger)`、`score_*(..., element_sims, knowledge_sims)`、`VectorCache.get/invalidate` 全程签名一致。✓
- 既有测试回归点已显式标注（test_kg_ingest 窗口数、test_embedding 批大小、test_retrieval）。✓

**4. Ambiguity：** 窗口 overlap 以"回退到首个 char_start ≥ w_end−m 的元素"明确定义；config 默认值在 Task 0 锁定；测试运行命令统一为 `PYTHONPATH=backend python -m pytest`。✓
