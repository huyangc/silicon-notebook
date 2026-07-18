#!/usr/bin/env python3
"""Guard: user-facing Chinese UI copy in frontend/app must not leak internal jargon.

Scans the *rendered text* of every frontend/app *.ts/*.tsx file — string literals
plus JSX text nodes — for a blacklist of internal terms. Any hit fails the build.
Run by scripts/check.sh.

Scope & deliberate limitation (see MEMORY severity lesson — a word blacklist is not
a semantic checker, so this does NOT claim full coverage):

  * A blacklisted term is flagged only when it sits inside a unit that also contains
    a CJK character — i.e. real Chinese UI copy. Pure-ASCII code/ids never trip it.
  * Comments (`// …`, `/* … */`) are blanked before scanning, so jargon left in
    code comments is intentionally ignored (the brief keeps internal names in code).
  * `${…}` template and `{…}` JSX interpolations are stripped from each unit before
    matching, so identifiers like `currentNotebook`, `{s.n_chunks}` are NOT flagged
    — only literal rendered text is. ASCII acronyms additionally require no adjacent
    ASCII letter, so `PKG`, `Scanner`, `currentNotebook` cannot false-positive.

The blacklist below covers AGENTS.md「界面词汇表」row by row. Two rows are covered
only in their unambiguous compound forms, on purpose (documented in that section):

  * 节点 / 边 — the graph view legitimately draws nodes and edges ("图谱技术上下文"
    in the table), and 边 is a substring of 旁边/边框/边距. Only 孤立节点, 补连边,
    关系边, 边审 are matchable; bare use stays a human-review item.
  * 知识库 — the legitimate Knowledge tab name; lint cannot tell it from misuse.

Also intentionally NOT blacklisted: 知识图谱, 索引 (user-dictionary words; only their
jargon modifiers CSR/ANN/暴力检索 and the abbreviation KG are).

Second, independent check — raw enum fallback (`MAP[x] ?? x`): a lookup that falls
back to its own key leaks the backend's English enum id to the user the moment the
backend adds a value. `vocabulary.ts` exists to make that unwritable (`label()` takes
a mandatory neutral fallback); this guard stops the pattern from creeping back in.
Unlike the blacklist, it scans *code*, not rendered text.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "frontend" / "app"

# ASCII acronyms/words: matched only when not glued to another ASCII letter, so
# identifiers (currentNotebook, MemoryPanel, schemaBusy, PKG) cannot trip them.
ASCII_TERMS = {
    "KG": re.compile(r"(?<![A-Za-z])KG(?![A-Za-z])"),
    "CSR": re.compile(r"(?<![A-Za-z])CSR(?![A-Za-z])"),
    "ANN": re.compile(r"(?<![A-Za-z])ANN(?![A-Za-z])"),
    "chunk": re.compile(r"(?<![A-Za-z])chunks?(?![A-Za-z])", re.IGNORECASE),
    "notebook": re.compile(r"(?<![A-Za-z])notebooks?(?![A-Za-z])", re.IGNORECASE),
    "Memory": re.compile(r"(?<![A-Za-z])memory(?![A-Za-z])", re.IGNORECASE),
    "schema": re.compile(r"(?<![A-Za-z])schemas?(?![A-Za-z])", re.IGNORECASE),
    "deprecated": re.compile(r"(?<![A-Za-z])deprecated(?![A-Za-z])", re.IGNORECASE),
}

# CJK jargon. Regex (not plain substring) so the ambiguous rows can carry their
# context condition in the pattern itself rather than in a reviewer's head.
CJK_TERMS = {
    # —— 基准库 / 个人层 一行
    "基准库": re.compile(r"基准库"),
    "基准语料": re.compile(r"基准语料"),
    "底层库": re.compile(r"底层库"),
    "权威参考层": re.compile(r"权威参考层"),
    "个人层": re.compile(r"个人层"),
    # —— 建图 / 入图 一行。「入图」要排除「加入图谱」这类正常动宾:只在
    # 「入图」直接跟结束或非「谱」字时算黑话(未入图 / 已入图 / 入图状态)。
    "建图": re.compile(r"建图"),
    # 「构建 / 建立 / 重建 + 知识图谱」作动作 → 界面词是「整理知识图谱」。裸「构建」
    # 「建立」是正常动词,不进黑名单;只有跟「知识图谱」连写才是黑话。
    "构建知识图谱": re.compile(r"(?:构建|建立|重建)知识图谱"),
    "入图": re.compile(r"入图(?!谱)"),
    # —— 抽取一行。「抽取字段」是本表给 schema 定的**界面词**,不能自己杀自己。
    "抽取": re.compile(r"抽取(?!字段)"),
    "补抽": re.compile(r"补抽"),
    "重抽": re.compile(r"重抽"),
    # —— 索引一行(CSR/ANN 在 ASCII_TERMS)
    "向量检索索引": re.compile(r"向量检索索引"),
    "暴力检索": re.compile(r"暴力检索"),
    # —— 节点 / 边 两行:只收无歧义复合形态,理由见上方 docstring 与 AGENTS.md
    "孤立节点": re.compile(r"孤立节点"),
    "补连边": re.compile(r"补连边"),
    "关系边": re.compile(r"关系边"),
    "边审": re.compile(r"边审"),
    # —— 其余各行
    "投影": re.compile(r"投影"),
    "预审": re.compile(r"预审"),
    "去重": re.compile(r"去重"),
    "晋升": re.compile(r"晋升"),
}

# 兜底即原值:`MAP[x] ?? x` / `MAP[x] || x`,以及经 label() 的同款写法
# `label(MAP, v, v)`。反向引用保证「兜底表达式与取值表达式逐字相同」——只认这个
# 形态,fallback 是字面量/别的表达式都放行(那是正当兜底)。
RAW_FALLBACK = re.compile(
    r"(?<![.\w$])[A-Za-z_$][\w$]*\s*\[\s*(?P<key>[^\[\]{}()]+?)\s*\]\s*(?:\?\?|\|\|)\s*(?P=key)(?![\w$.])"
)
LABEL_RAW_FALLBACK = re.compile(
    r"(?<![.\w$])label\(\s*[^,()]+,\s*(?P<val>[^,()]+?)\s*,\s*(?P=val)\s*\)"
)

CJK = re.compile(r"[一-鿿]")
INTERP = re.compile(r"\$\{[^{}]*\}")  # template ${...}
STRING = re.compile(r'"(?:[^"\\]|\\.)*"' r"|'(?:[^'\\]|\\.)*'" r"|`(?:[^`\\]|\\.)*`", re.S)
JSXTEXT = re.compile(r"[^<>{}]+")     # bare JSX text: a run between tags/expressions


# A `/` right after one of these (or at the start) opens a regex literal, not a
# division — regex bodies may contain quotes that would otherwise desync the string
# scanner (and then swallow a later `// 中文` comment). Covers the (,= cases in use.
_REGEX_OK_BEFORE = set("([{,;:=!&|?+-*%^~<>")


def blank_comments(text: str) -> str:
    """Replace // and /* */ comments *and regex-literal bodies* with spaces (newlines
    kept, length preserved — offsets stay valid for line numbers), respecting string,
    template, and regex literals so comment markers inside them survive. Quoted strings
    reset to code at a newline (they can't span lines), which bounds any mis-parse to a
    single line.

    Regex bodies are blanked rather than copied because the naive STRING regex that
    later extracts string literals does not know about regex literals: a body such as
    `/\\son\\w+\\s*=\\s*("[^"]*"|'[^']*'|[^\\s>]+)/gi` (page.tsx sanitizeTableHtml)
    contains unbalanced quotes, and copying it verbatim made STRING match from inside
    the regex to some later quote — dragging whole spans of code into the "rendered
    text" units and reporting them as UI copy. Regex bodies are code and are never
    rendered, so blanking loses nothing."""
    out: list[str] = []
    i, n, state, last = 0, len(text), "code", ""
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if state == "code":
            if two == "//":
                out.append("  "); i += 2; state = "line"; continue
            if two == "/*":
                out.append("  "); i += 2; state = "block"; continue
            if c == "/" and (last == "" or last in _REGEX_OK_BEFORE):
                out.append(c); i += 1; in_class = False
                while i < n:                       # blank the regex body
                    ch = text[i]
                    if ch == "\n":
                        break                      # unterminated on this line → recover
                    if ch == "\\" and i + 1 < n:
                        out.append("  "); i += 2; continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        out.append(c); i += 1; break
                    out.append(" ")
                    i += 1
                last = "/"; continue
            out.append(c)
            if c not in " \t":
                last = c
            state = {"'": "sq", '"': "dq", "`": "tpl"}.get(c, "code")
            i += 1; continue
        if state in ("sq", "dq", "tpl"):
            if c == "\n" and state != "tpl":
                out.append("\n"); state = "code"; last = ""; i += 1; continue
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(text[i + 1]); i += 2; continue
                i += 1; continue
            if (state, c) in (("sq", "'"), ("dq", '"'), ("tpl", "`")):
                state = "code"; last = c
            i += 1; continue
        if state == "line":
            out.append("\n" if c == "\n" else " "); state = "code" if c == "\n" else "line"
            i += 1; continue
        if state == "block":
            if two == "*/":
                out.append("  "); i += 2; state = "code"; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


def blank_strings(text: str) -> str:
    """Replace string literals with equal-length blanks (newlines kept). Their
    content is scanned separately; blanking them leaves bare JSX text as the only
    remaining CJK in the file, so JSX-text extraction can't drag in code."""
    return STRING.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)), text)


def terms_in(unit: str) -> list[str]:
    found = [name for name, pat in CJK_TERMS.items() if pat.search(unit)]
    found += [name for name, pat in ASCII_TERMS.items() if pat.search(unit)]
    return found


def scan(path: Path) -> list[tuple[int, str, str]]:
    """Blacklisted jargon in this file's rendered text: (line, term, unit)."""
    blanked = blank_comments(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str, str]] = []

    def record(off: int, text: str, unit: str) -> None:
        if CJK.search(unit):
            for term in terms_in(unit):
                hits.append((text.count("\n", 0, off) + 1, term, unit.strip()))

    # Pass 1 — string literals (title=/label:/placeholder/toast/…). Drop ${…}.
    for m in STRING.finditer(blanked):
        record(m.start(), blanked, INTERP.sub(" ", m.group(0)[1:-1]))
    # Pass 2 — bare JSX text between tags. With strings blanked, any run of
    # non-tag/non-brace chars that still contains CJK is genuine rendered text.
    nostr = blank_strings(blanked)
    for m in JSXTEXT.finditer(nostr):
        record(m.start(), nostr, m.group(0))
    return hits


def scan_raw_fallback(path: Path) -> list[tuple[int, str]]:
    """`MAP[x] ?? x` / `label(MAP, v, v)` in this file's *code*: (line, snippet).

    Deliberately not restricted to CJK units — this is a code shape, not copy, and
    the leak it causes (backend enum id rendered verbatim) is invisible until the
    backend grows a value. Comments are still blanked so a documented counter-example
    in a comment does not fail the build.

    Known-good shapes this must NOT flag, verified by the self-tests:
      * `label(MAP, v, "中性兜底")` — the sanctioned API; fallback is a real fallback.
      * `Object.hasOwn(M, t) ? M[t] : t` — kg-type-mark.tsx's deliberate pass-through
        for user-defined knowhow object types (the raw string *is* the user's own
        column name, so echoing it is honest, not a leak).
    """
    blanked = blank_comments(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for pattern in (RAW_FALLBACK, LABEL_RAW_FALLBACK):
        for m in pattern.finditer(blanked):
            hits.append((blanked.count("\n", 0, m.start()) + 1, m.group(0).strip()))
    return sorted(hits)


def main() -> int:
    files = sorted(
        p for p in APP.rglob("*")
        if p.suffix in (".ts", ".tsx") and ".test." not in p.name and not p.name.endswith(".d.mts")
    )
    if not files:
        print("check_ui_vocabulary: no frontend/app sources found", file=sys.stderr)
        return 1
    violations: list[str] = []
    fallbacks: list[str] = []
    for path in files:
        rel = path.relative_to(ROOT)
        for line, term, snippet in scan(path):
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in rendered text: {snippet!r}")
        for line, snippet in scan_raw_fallback(path):
            fallbacks.append(f"  {rel}:{line}: {snippet}")
    if violations:
        print("UI vocabulary contract MISMATCH — internal jargon in rendered text:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        print(
            "\nRewrite the copy per AGENTS.md「界面词汇表」(e.g. 基准库→公共知识库, "
            "chunk→段, KG/CSR/ANN→索引, 抽取→分析). Internal names stay in code, not UI.",
            file=sys.stderr,
        )
    if fallbacks:
        print("\nRaw enum fallback — a lookup that falls back to its own key:", file=sys.stderr)
        print("\n".join(fallbacks), file=sys.stderr)
        print(
            "\n后端每加一个枚举值,这个写法就把英文 id 直接渲染给用户。改用 "
            "vocabulary.ts 的 label(MAP, value, '中性兜底')——它强制传兜底词,"
            "写不出「兜底即原值」。",
            file=sys.stderr,
        )
    if violations or fallbacks:
        return 1
    print(
        f"UI vocabulary contract OK: scanned {len(files)} frontend/app files "
        f"({len(CJK_TERMS) + len(ASCII_TERMS)} blacklisted terms + raw-enum-fallback)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
