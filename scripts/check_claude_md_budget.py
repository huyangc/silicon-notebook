#!/usr/bin/env python3
"""`CLAUDE.md` 的体量硬门。由 `scripts/check_contracts.sh` 运行（G1）。

这个门存在的理由是实测的棘轮：`CLAUDE.md` 在 2026-07-24 到 2026-08-27 之间从
20 KB 涨到 307 KB（约 16 万字符、每次会话固定注入约 9 万 token），315 次提交里
没有一次是净删——每个 PR 往里追加一段特性结论，从不回收。抽样统计显示，这些段落
里**中位 98.5%** 的标识符在 `AGENTS.md` + `docs/` 里已经有更全的版本，也就是说
增长的几乎全部是第三份副本，而不是新信息。

`CLAUDE.md` 与别的文档不同：它是 Claude Code **每次请求**都要重新付费的固定成本，
所以它的判据不是「这条规则对不对」，而是「与该特性无关的下一个改动还需要它吗」。
不需要，就写进 `AGENTS.md` / `docs/`——那两处不进 Claude Code 的常驻上下文，且随
代码一起维护。

因此这里钉两个数：

1. **总字符数** —— 拦住整体膨胀。
2. **单行字符数** —— 拦住真正的病灶形态。旧文件里单条 markdown bullet 最长
   12,552 字符，是一整篇设计后记塞进一个列表项；总量闸对「一次加一条」这种加法
   反应太迟钝，而行长闸在第一次提交时就报红。

两个上限都是**只许降**的：真需要写进常驻规范的通用约束，应当短。上调阈值不是修复，
是把这个门关掉——要上调必须在 PR 里说明为什么这条规则对未来所有改动都成立。

刻意**不**做的事：不检查内容、不做重复检测、不解析章节结构。那些需要语义判断，
交给评审；这个门只负责让「往里倒特性文档」这个动作立刻变得可见。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "CLAUDE.md"

# 零余量原则：这两个数只许降。当前实际值见失败信息里的 "现在"。
MAX_CHARS = 20_000
MAX_LINE_CHARS = 1_200

_ADVICE = """
这不是「把上限调大」的信号，而是「这段内容放错文件」的信号。判据：
  与该特性无关的下一个改动，还需要这条规则吗？
  否 → 写进 AGENTS.md（开发契约真源）或 docs/ 下负责该主题的中英文权威文档；
       精确数值上限一律只登记在 docs/product-and-api.md / _zh.md。
  是 → 用一句话写进 CLAUDE.md 的对应红线，细节仍然留在 AGENTS.md，
       并在第四章的路由表里给出去处。
""".rstrip()


def main() -> int:
    if not TARGET.exists():
        print(f"CLAUDE.md 体量门：找不到 {TARGET}", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8")
    failures: list[str] = []

    if len(text) > MAX_CHARS:
        failures.append(
            f"总字符数超限：现在 {len(text):,}，上限 {MAX_CHARS:,}"
            f"（超出 {len(text) - MAX_CHARS:,}）"
        )

    long_lines = [
        (i, len(line))
        for i, line in enumerate(text.splitlines(), start=1)
        if len(line) > MAX_LINE_CHARS
    ]
    if long_lines:
        shown = ", ".join(f"第 {i} 行 {n:,} 字符" for i, n in long_lines[:5])
        more = f"，另有 {len(long_lines) - 5} 行" if len(long_lines) > 5 else ""
        failures.append(
            f"单行字符数超限（上限 {MAX_LINE_CHARS:,}）：{shown}{more}"
        )

    if failures:
        print("CLAUDE.md 体量门 FAILED", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        print(_ADVICE, file=sys.stderr)
        return 1

    longest = max((len(l) for l in text.splitlines()), default=0)
    print(
        f"CLAUDE.md 体量门 OK: {len(text):,}/{MAX_CHARS:,} 字符，"
        f"最长行 {longest:,}/{MAX_LINE_CHARS:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
