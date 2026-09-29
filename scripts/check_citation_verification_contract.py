#!/usr/bin/env python3
"""跨栈契约：全局问答引用核对的卡片标签与说明句，前后端逐字同一份。

说明句与标签有两份真源：后端 `backend/app/services/global_citation_check.py`
（`VERIFICATION_LABELS`、`NOTICE_LEADS`、`NOTICE_TAIL`、`REASON_*`，MCP 等后端呈现面经
`citation_check_notice()` 取句子）与前端 `frontend/app/citation-verification.ts`
（`CITATION_VERIFICATION_LABELS`、`citationCheckNotice()`，笔记本/全局窗口与公开页共用）。
前端是规范写法；任一侧改一个字而另一侧没跟，用户就会在网页上读到一句、在 MCP 里读到另一句。

钉住三件事：

1. 标签表：前端 `CITATION_VERIFICATION_LABELS` 严格解析（复用
   `check_enumeration_list_labels_contract.frontend_labels` 的零求值解析器：spread、computed
   key、重复键、多份声明一律硬失败）后必须与后端 `VERIFICATION_LABELS` 完全相等。
2. 说明句模板：两种时态的开头、冒号 + 原因 + 句号 + 结尾、原因分句「N 条<标签>」、连接符
   「、」、无分类计数时的「共 N 条」、原因顺序——都由后端常量推出前端源码里应有的**逐字**
   片段，逐一要求出现（注释已剥离，所以写在注释里的旧文案不算数）。
3. 不钉行号：只认源码片段本身。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TS = ROOT / "frontend/app/citation-verification.ts"
sys.path.insert(0, str(ROOT / "backend"))
from app.services.global_citation_check import (  # noqa: E402
    NOTICE_LEADS, NOTICE_TAIL, REASON_CLAUSE, REASON_FALLBACK, REASON_JOINER,
    VERIFICATION_LABELS,
)

FRONTEND_LABELS_CONST = "CITATION_VERIFICATION_LABELS"
# The wire / display order of the three kinds; the frontend's REASON_ORDER.
WIRE_ORDER = ("changed", "source_gone", "unverifiable")

_spec = importlib.util.spec_from_file_location(
    "_enumeration_label_guard",
    ROOT / "scripts" / "check_enumeration_list_labels_contract.py",
)
_parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parser)
GuardError = _parser.GuardError


def expected_fragments() -> dict[str, str]:
    """Frontend source fragments derived from the backend literals."""
    order = ", ".join(f'"{kind}"' for kind in WIRE_ORDER)
    return {
        "live lead": f'"{NOTICE_LEADS["live"]}"',
        "snapshot lead": f'"{NOTICE_LEADS["snapshot"]}"',
        "notice template": "${lead}：${citationCheckReasonText(check)}。" + NOTICE_TAIL + "`",
        "reason clause": REASON_CLAUSE.format(
            count="${count(check[reason])}",
            label="${CITATION_VERIFICATION_LABELS[reason]}",
        ),
        "reason joiner": f'.join("{REASON_JOINER}")',
        "reason fallback": REASON_FALLBACK.format(count="${count(check.failed)}"),
        "reason order": f"REASON_ORDER: readonly CitationVerification[] = [{order}]",
    }


def check(path: Path | None = None) -> list[str]:
    """Every mismatch as a line of text; empty when the two sides agree."""
    target = path or DEFAULT_TS
    problems: list[str] = []
    try:
        labels = _parser.frontend_labels(FRONTEND_LABELS_CONST, target)
    except GuardError as exc:
        return [f"无法判定 {FRONTEND_LABELS_CONST}: {exc}"]
    if labels != dict(VERIFICATION_LABELS):
        problems.append(
            f"标签表 MISMATCH: backend={dict(VERIFICATION_LABELS)} frontend={labels}"
        )
    if tuple(VERIFICATION_LABELS) != WIRE_ORDER:
        problems.append(f"后端标签表键序不是 {WIRE_ORDER}: {tuple(VERIFICATION_LABELS)}")
    source = _parser.strip_ts_comments(target.read_text(encoding="utf-8"))
    for name, fragment in expected_fragments().items():
        if fragment not in source:
            problems.append(f"说明句 {name} 未逐字出现在 {target.name}: {fragment}")
    return problems


def main(path: Path | None = None) -> int:
    problems = check(path)
    if problems:
        print("citation verification 跨栈契约 MISMATCH", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"citation verification 契约 OK: labels={sorted(VERIFICATION_LABELS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
