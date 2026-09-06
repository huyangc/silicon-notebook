#!/usr/bin/env python3
"""`CLAUDE.md` 的体量棘轮。由 `scripts/check_contracts.sh` 运行（G1）。

这个门存在的理由是实测的棘轮：`CLAUDE.md` 在 2026-07-24 到 2026-08-27 之间从
20 KB 涨到 307 KB（约 16 万字符、每次会话固定注入约 9 万 token），315 次提交里
没有一次是净删——每个 PR 往里追加一段特性结论，从不回收。抽样统计显示，这些段落
里**中位 98.5%** 的标识符在其他仓库文档里已经有更全的版本，也就是说
增长的几乎全部是第三份副本，而不是新信息。

`CLAUDE.md` 与别的文档不同：它是 Claude Code **每次请求**都要重新付费的固定成本，
所以它的判据不是「这条规则对不对」，而是「与该特性无关的下一个改动还需要它吗」。
不需要，就写进负责该主题的 canonical document；只有仓库级 Agent 工作流或路由变化
才更新 `AGENTS.md`。这些文档不进 Claude Code 的常驻上下文，且随代码一起维护。

因此这里钉两个数：

1. **总字符数** —— 拦住整体膨胀。
2. **单行字符数** —— 拦住真正的病灶形态。旧文件里单条 markdown bullet 最长
   12,552 字符，是一整篇设计后记塞进一个列表项；总量闸对「一次加一条」这种加法
   反应太迟钝，而行长闸在第一次提交时就报红。

两个数都是**精确相等**判定，不是上限——与 `architecture_boundary_baseline.json`
的 `function_length_ceiling` 同款「只许降、降了必须同步 baseline」语义。为什么
必须是相等而不是 `<=`，有两条独立的理由，缺一条这个门就名存实亡：

- **留余量 = 发免费额度。** 最初那版写 20,000 / 1,200，对当时 14,362 / 823 的
  文件留出 5,638 字符与 377 字符/行——一条典型的特性段落照样进得来，而这个门
  要拦的恰恰就是它（codex #607 R1 P2）。
- **只拒「大于」会攒出陈旧余量。** 后来的改动把文件改短却不下调常量时，检查照样
  通过；那段没人记账的空间会被下一次追加白白吃掉，而这个门承诺的「每次净增长
  都有一行显式 baseline diff」当场失效（codex #607 R2 P2）。

所以两个方向都要报红：涨了要回答判据那一问（多半是内容放错文件），降了要把
baseline 跟着降——后者不是麻烦，它就是棘轮本身。

刻意**不**做的事：不检查内容、不做重复检测、不解析章节结构。那些需要语义判断，
交给评审；这个门只负责让「文件体量变了」这件事必须经过一行显式的、看得见的 diff。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "CLAUDE.md"

# 精确等于当前实际值。只许降——降了必须在同一个 PR 里把这两个数一起降。
#
# 3,399 → 3,499:加了「按钮按下要有可见反馈、结果落在按钮自身」这条两行规则。它满足
# 判据——不是某个特性的结论,而是对**以后每一个**加按钮的改动都成立的红线,而写不写
# 反馈恰恰是 UI 改动最容易漏、又最难在评审里凭 diff 看出来的一项。细节(基线选择器、
# 为什么是元素级、结果态与按下态的分工)留在 AGENTS.md 的 Interactive feedback,
# 这里只留一句加去处。
# 3,499 → 3,571：明确共享开发规则的权威文档与载体边界，适用于后续所有任务；
# 执行、验证停止条件及交付细则仍由 docs/development.md / _zh.md 拥有。
BASELINE_CHARS = 3_571
BASELINE_LINE_CHARS = 79

_GREW_ADVICE = """
涨了多半是「这段内容放错文件」而不是「baseline 该调大」。先回答判据：
  与该特性无关的下一个改动，还需要这条规则吗？
  否 → 写进 architecture.md 或 docs/ 下负责该主题的中英文权威文档；只有仓库级
       Agent 工作流或文档路由变化时才更新 AGENTS.md；
       精确数值上限一律只登记在 docs/product-and-api.md / _zh.md。
  是 → 用一句话写进 CLAUDE.md 的对应规则，细节仍然留在 owning document，并在
       路由表里给出去处；确实净增长了，就在**同一个 PR 里**把下面报出的新值
       写回 baseline，并在 PR 里说明为什么这条规则对未来所有改动都成立。
       那一行 diff 就是评审信号。
""".rstrip()

_SHRANK_ADVICE = """
降了是好事，但 baseline 必须跟着降：留在原处的那段空间没人记账，下一次追加就会
白白吃掉它，而这个门承诺的「每次净增长都有一行显式 diff」当场失效。把下面报出的
新值写回 scripts/check_claude_md_budget.py 即可。
""".rstrip()


def main() -> int:
    if not TARGET.exists():
        print(f"CLAUDE.md 体量门：找不到 {TARGET}", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8")
    chars = len(text)
    lines = text.splitlines()
    longest = max((len(line) for line in lines), default=0)

    grew: list[str] = []
    shrank: list[str] = []

    if chars > BASELINE_CHARS:
        grew.append(
            f"总字符数 {chars:,}，baseline {BASELINE_CHARS:,}"
            f"（多了 {chars - BASELINE_CHARS:,}）"
        )
    elif chars < BASELINE_CHARS:
        shrank.append(
            f"总字符数 {chars:,}，baseline 仍是 {BASELINE_CHARS:,}"
            f"（应下调为 {chars:,}）"
        )

    if longest > BASELINE_LINE_CHARS:
        offenders = [
            (i, len(line))
            for i, line in enumerate(lines, start=1)
            if len(line) > BASELINE_LINE_CHARS
        ]
        shown = ", ".join(f"第 {i} 行 {n:,} 字符" for i, n in offenders[:5])
        more = f"，另有 {len(offenders) - 5} 行" if len(offenders) > 5 else ""
        grew.append(
            f"单行字符数 baseline {BASELINE_LINE_CHARS:,}，超出的有：{shown}{more}"
        )
    elif longest < BASELINE_LINE_CHARS:
        shrank.append(
            f"最长行 {longest:,}，baseline 仍是 {BASELINE_LINE_CHARS:,}"
            f"（应下调为 {longest:,}）"
        )

    if grew or shrank:
        print("CLAUDE.md 体量门 FAILED", file=sys.stderr)
        for line in (*grew, *shrank):
            print(f"  {line}", file=sys.stderr)
        if grew:
            print(_GREW_ADVICE, file=sys.stderr)
        if shrank:
            print(_SHRANK_ADVICE, file=sys.stderr)
        return 1

    print(
        f"CLAUDE.md 体量门 OK: {chars:,} 字符、最长行 {longest:,}，"
        f"与 baseline 精确一致"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
