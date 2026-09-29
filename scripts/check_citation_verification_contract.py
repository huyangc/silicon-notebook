#!/usr/bin/env python3
"""跨栈契约：全局问答引用核对的卡片标签与说明句，前后端逐字同一份。

说明句与标签有两份真源：前端 `frontend/app/citation-verification.ts`（`CITATION_VERIFICATION_LABELS`、
`citationCheckReasonText()`、`citationCheckNotice()`，笔记本/全局窗口与公开页共用，是**规范写法**，也是
唯一真正把句子渲染给读者的地方）与后端孪生 `backend/app/services/global_citation_check.py`
（`VERIFICATION_LABELS`、`NOTICE_LEADS`、`NOTICE_TAIL`、`REASON_*`、`citation_check_notice()`）。
后端目前没有任何呈现面输出这句话（MCP 只给计数），推理轨迹的「核对」步复用它的原因文本；
这条守卫保证哪天后端需要输出句子时，拿到的与网页上是同一句。

钉住三件事：

1. 标签表：前端 `CITATION_VERIFICATION_LABELS` 严格解析（复用
   `check_enumeration_list_labels_contract.frontend_labels` 的零求值解析器：spread、computed
   key、重复键、多份声明一律硬失败）后必须与后端 `VERIFICATION_LABELS` 完全相等。
2. 说明句模板：由后端常量推出前端源码里应有的**逐字**片段，并且**锚定在函数体里**——
   两种时态的开头与「冒号 + 原因 + 句号 + 结尾」模板必须出现在 `citationCheckNotice` 的
   函数体内，原因分句「N 条<标签>」、连接符「、」与「共 N 条」必须出现在
   `citationCheckReasonText` 的函数体内（函数体按字符串感知的括号配对截取，不会越过函数的
   收尾大括号），原因顺序按 `REASON_ORDER` 的数组字面量逐项比较。所以把片段挪到文件里别处
   的诱饵常量、同时改掉函数里真正用到的文案，守卫照样报红。注释先剥离，写在注释里的旧文案
   不算数。
3. 不钉行号：只认源码结构与片段本身。
"""
from __future__ import annotations

import importlib.util
import re
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


def notice_fragments() -> dict[str, str]:
    """Fragments that must sit INSIDE ``citationCheckNotice``'s body."""
    return {
        "live lead": f'"{NOTICE_LEADS["live"]}"',
        "snapshot lead": f'"{NOTICE_LEADS["snapshot"]}"',
        "notice template": "${lead}：${citationCheckReasonText(check)}。" + NOTICE_TAIL + "`",
    }


def reason_fragments() -> dict[str, str]:
    """Fragments that must sit INSIDE ``citationCheckReasonText``'s body."""
    return {
        "reason clause": REASON_CLAUSE.format(
            count="${count(check[reason])}",
            label="${CITATION_VERIFICATION_LABELS[reason]}",
        ),
        "reason joiner": f'.join("{REASON_JOINER}")',
        "reason fallback": REASON_FALLBACK.format(count="${count(check.failed)}"),
    }


def _closing(text: str, open_idx: int, pair: str) -> int:
    """Index of the bracket closing ``text[open_idx]`` (string-aware)."""
    opener, closer = pair
    depth = 0
    quote = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise GuardError(f"{opener}…{closer} 未闭合")


def function_body(text: str, name: str) -> str:
    """The body of the ONE ``function name(...) {...}`` declaration."""
    declarations = list(re.finditer(rf"\bfunction\s+{re.escape(name)}\s*\(", text))
    if len(declarations) != 1:
        raise GuardError(f"需要恰好一份 function {name} 声明，找到 {len(declarations)} 份")
    params_close = _closing(text, declarations[0].end() - 1, "()")
    brace = text.find("{", params_close)
    if brace == -1:
        raise GuardError(f"function {name} 没有函数体")
    return text[brace + 1:_closing(text, brace, "{}")]


def reason_order(text: str) -> tuple[str, ...]:
    """The string items of the ONE ``const REASON_ORDER ... = [...]`` literal."""
    declarations = list(re.finditer(r"\bconst\s+REASON_ORDER\b[^=;]*=\s*\[", text))
    if len(declarations) != 1:
        raise GuardError(f"需要恰好一份 REASON_ORDER 声明，找到 {len(declarations)} 份")
    open_idx = declarations[0].end() - 1
    body = text[open_idx + 1:_closing(text, open_idx, "[]")]
    items = [item.strip() for item in body.split(",") if item.strip()]
    if not all(re.fullmatch(r'"[a-z_]+"', item) for item in items):
        raise GuardError(f"REASON_ORDER 不是纯字符串字面量数组: {body.strip()}")
    return tuple(item.strip('"') for item in items)


def check(path: Path | None = None) -> list[str]:
    """Every mismatch as a line of text; empty when the two sides agree."""
    target = path or DEFAULT_TS
    problems: list[str] = []
    try:
        labels = _parser.frontend_labels(FRONTEND_LABELS_CONST, target)
        source = _parser.strip_ts_comments(target.read_text(encoding="utf-8"))
        bodies = {
            "citationCheckNotice": (function_body(source, "citationCheckNotice"), notice_fragments()),
            "citationCheckReasonText": (
                function_body(source, "citationCheckReasonText"), reason_fragments(),
            ),
        }
        order = reason_order(source)
    except GuardError as exc:
        return [f"无法判定 {target.name}: {exc}"]
    if labels != dict(VERIFICATION_LABELS):
        problems.append(
            f"标签表 MISMATCH: backend={dict(VERIFICATION_LABELS)} frontend={labels}"
        )
    if tuple(VERIFICATION_LABELS) != WIRE_ORDER:
        problems.append(f"后端标签表键序不是 {WIRE_ORDER}: {tuple(VERIFICATION_LABELS)}")
    if order != WIRE_ORDER:
        problems.append(f"REASON_ORDER MISMATCH: expected {WIRE_ORDER}, frontend {order}")
    for function, (body, fragments) in bodies.items():
        for name, fragment in fragments.items():
            if fragment not in body:
                problems.append(f"说明句 {name} 未逐字出现在 {function} 的函数体里: {fragment}")
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
