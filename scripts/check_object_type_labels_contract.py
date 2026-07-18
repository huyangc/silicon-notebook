#!/usr/bin/env python3
"""跨栈契约:前端 kg-type-mark.tsx 的 KG_TYPE_LABELS 内置项必须逐字等于后端
OBJECT_TYPE_LABELS。任一侧改了 object_type 的显示名而另一侧没跟,这里失败。
object_type 有前后端两份真源(后端 API 下发 + 前端只有 type 字符串时的小表),
severity 那次的漏网教训就是「没有守卫钉住两份真源」。由 scripts/check.sh 运行。

守卫的核心原则是**严格消费**:只接受一种规范形式(单一 `const KG_TYPE_LABELS = {…}`
对象字面量,条目全是 `key: "字面量"`)。凡是守卫无法在零求值前提下判定运行时结果的写法
——spread(`...base`)、computed key(`[k]: v`)、非字面量值(`concept: someVar`)、简写、
模板串、转义、重复键、多份声明——一律 GuardError 硬失败,**绝不静默跳过**。静默跳过等于
放行:运行时对象已经变了,守卫却还在比一张残缺的表(review 揪出的假绿路径)。

已知的刻意取舍:注释剥离是字符串感知的线性扫描,但不解析正则字面量。若 kg-type-mark.tsx
里出现含 `//` 或 `/*` 的正则/裸撇号,扫描会误判并抛错——方向是**假红**(吵闹但可修),
不是假绿。守卫宁可错杀。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TSX = ROOT / "frontend/app/kg-type-mark.tsx"
sys.path.insert(0, str(ROOT / "backend"))
from app.services.extraction_profiles import OBJECT_TYPE_LABELS  # noqa: E402


class GuardError(RuntimeError):
    """前端小表不是守卫能严格判定的规范形式 —— 硬失败,不静默跳过。"""


# 唯一接受的条目形式:裸标识符或带引号的 key + 带引号的字符串字面量 value。
_ENTRY_RE = re.compile(
    r"""^\s*
        (?: (?P<bare_key>[A-Za-z_$][A-Za-z0-9_$]*)
          | "(?P<dq_key>[^"\\]*)"
          | '(?P<sq_key>[^'\\]*)' )
        \s*:\s*
        (?: "(?P<dq_val>[^"\\]*)" | '(?P<sq_val>[^'\\]*)' )
        \s*$""",
    re.X,
)
_ANY_DECL_RE = re.compile(r"\b(?:const|let|var)\s+KG_TYPE_LABELS\b")
_LITERAL_DECL_RE = re.compile(r"\b(?:const|let|var)\s+KG_TYPE_LABELS\b[^=;{]*=\s*\{")


def strip_ts_comments(src: str) -> str:
    """剥掉 TS/TSX 的 `//` 与 `/* */` 注释,字符串内的同形字符不误伤。

    必须在**搜索声明之前**对整份文件做:否则把一份正确的声明整个塞进 /* */、后面再放
    一份错误的真实声明,寻找声明的正则会先选中注释里那份(review 揪出的假绿路径)。
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            if end == -1:
                raise GuardError("块注释 /* 未闭合,无法可靠剥注释")
            i = end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    if quote is not None:
        raise GuardError(f"字符串字面量 {quote} 未闭合,无法可靠剥注释")
    return "".join(out)


def _object_literal_body(text: str, open_idx: int) -> str:
    """从 `{` 起取平衡括号内的对象体(字符串感知)。"""
    depth = 0
    i, n = open_idx, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
        i += 1
    raise GuardError("KG_TYPE_LABELS 对象字面量的花括号未闭合")


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切条目(字符串感知 + 括号深度感知)。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(body[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def parse_label_entries(body: str) -> dict[str, str]:
    """严格解析对象体。任何非规范条目或重复键都抛 GuardError,不静默跳过。"""
    labels: dict[str, str] = {}
    for entry in _split_top_level(body):
        m = _ENTRY_RE.match(entry)
        if m is None:
            raise GuardError(
                "KG_TYPE_LABELS 含守卫无法严格判定的条目(spread / computed key / "
                "非字面量值 / 简写 / 模板串 / 转义),运行时对象可能与守卫读到的不一致,"
                f"拒绝放行: {entry.strip()!r}"
            )
        key = next(
            g for g in (m["bare_key"], m["dq_key"], m["sq_key"]) if g is not None
        )
        value = m["dq_val"] if m["dq_val"] is not None else m["sq_val"]
        if key in labels:
            raise GuardError(
                f"KG_TYPE_LABELS 重复键 {key!r}:JS 与 dict() 都会静默折叠成最后一个,"
                "守卫显式拒绝(否则两份真源的差异会被折叠掩盖)"
            )
        labels[key] = value
    return labels


def frontend_labels(path: Path | None = None) -> dict[str, str]:
    source = (path or DEFAULT_TSX).read_text(encoding="utf-8")
    text = strip_ts_comments(source)
    declarations = _ANY_DECL_RE.findall(text)
    if not declarations:
        raise GuardError("kg-type-mark.tsx: 未找到 KG_TYPE_LABELS 声明")
    if len(declarations) > 1:
        raise GuardError(
            f"kg-type-mark.tsx: 找到 {len(declarations)} 份 KG_TYPE_LABELS 声明,"
            "守卫无法判定哪一份在运行时生效"
        )
    literal = _LITERAL_DECL_RE.search(text)
    if literal is None:
        raise GuardError(
            "kg-type-mark.tsx: KG_TYPE_LABELS 不是 `= {…}` 对象字面量声明,"
            "守卫无法零求值判定其内容"
        )
    body = _object_literal_body(text, literal.end() - 1)
    return parse_label_entries(body)


def main(path: Path | None = None) -> int:
    backend = dict(OBJECT_TYPE_LABELS)
    try:
        frontend = frontend_labels(path)
    except GuardError as exc:
        print("object_type label 跨栈契约无法判定", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 1
    if backend != frontend:
        print("object_type label 跨栈契约 MISMATCH", file=sys.stderr)
        print(f"  backend : {backend}", file=sys.stderr)
        print(f"  frontend: {frontend}", file=sys.stderr)
        diff = {k: (backend.get(k), frontend.get(k))
                for k in set(backend) | set(frontend)
                if backend.get(k) != frontend.get(k)}
        print(f"  差异(backend, frontend): {diff}", file=sys.stderr)
        return 1
    print(f"object_type label 契约 OK: {sorted(backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
