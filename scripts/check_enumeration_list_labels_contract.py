#!/usr/bin/env python3
"""跨栈契约:前端 answer-panel.tsx 的三张清单标签表必须逐字等于「后端标签 + 『清单』」，
且三张表的渲染值在全域内互不重名。

集合枚举工具（design doc `docs/reasoning-enumeration-tools-design.md` §2.4/2.6/§6.2）的元素 /
知识对象 / 来源清单名有三份真源：后端 `backend/app/services/reasoning_retrieval.py` 的
`_ELEMENT_KIND_LABELS` / `_KG_OBJECT_LABELS` / `_SOURCE_COLLECTION_LABELS`（trace summary 与账目
回喂用它们拼「XX清单」）与前端 `frontend/app/answer-panel.tsx` 的 `ELEMENT_KIND_LIST_LABELS` /
`KG_OBJECT_LIST_LABELS` / `SOURCE_LIST_LABELS`（结果卡标题用它们）。任一侧改名、加删 key 而另一
侧没跟，用户会在 trace 里看到一个名字、在结果卡上看到另一个名字。这条守卫钉住两件事:

1. 前端表的每个值都逐字等于对应后端表的值加上后缀「清单」，key 集合也必须相同——
   多一个键、少一个键、或某个键的值不满足这个后缀关系，都判 MISMATCH。
2. 三张前端表（元素清单 ∪ 知识对象清单 ∪ 来源清单）的渲染值在全域内不得重名。`formula` 在
   元素与知识对象两侧都存在（文档里的公式元素 vs 已抽取的公式知识对象），如果都渲染成
   「公式清单」，模型账目回喂里会同时出现两条同名却矛盾的「已列完/未列完」陈述，reflect
   也就无法区分该续跑哪一个。来源清单只有一个标签，但同样进这条唯一性检查——它与另外八个
   共用同一份账目。

后端一侧直接 import 运行时对象（与 `check_object_type_labels_contract.py` 对
`OBJECT_TYPE_LABELS` 的做法一致）：Python 值天然精确，不需要额外解析。前端一侧没有 Python
import 入口，只能做**严格消费**的源码解析——同一份文件里逐字匹配两个 `const NAME = {...}`
对象字面量声明，只接受裸标识符/带引号 key + 带引号字符串 value 的规范条目。任何
spread（`...base`）、computed key（`[k]: v`）、非字面量 value、简写、模板串、转义、重复键、
每个名字有 0 份或 >1 份声明，都是 GuardError 硬失败而不是静默跳过——静默跳过就是放行:
运行时对象已经变了，守卫却还在比一张残缺的表。

已知的刻意取舍（与 `check_object_type_labels_contract.py` 相同）：注释剥离是字符串感知的线性
扫描，不解析正则字面量；若 answer-panel.tsx 里出现含 `//` 或 `/*` 的正则/裸撇号会被误判并
报错——方向是**假红**（吵闹但可修），不是假绿。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TSX = ROOT / "frontend/app/answer-panel.tsx"
sys.path.insert(0, str(ROOT / "backend"))
from app.services.reasoning_retrieval import (  # noqa: E402
    _ELEMENT_KIND_LABELS as BACKEND_ELEMENT_LABELS,
    _KG_OBJECT_LABELS as BACKEND_KG_OBJECT_LABELS,
    _SOURCE_COLLECTION_LABELS as BACKEND_SOURCE_LABELS,
)

LIST_SUFFIX = "清单"
FRONTEND_ELEMENT_CONST = "ELEMENT_KIND_LIST_LABELS"
FRONTEND_KG_OBJECT_CONST = "KG_OBJECT_LIST_LABELS"
# 来源清单只有一个标签,但仍然做成同形的对象字面量表(后端按 collection 取键):
# 本守卫的解析器只认这一种形状,给它一张表就不必为一个标签另开一条解析路径——
# 那条新路径本身会成为下一个「解析不了就静默跳过」的缺口。
FRONTEND_SOURCE_CONST = "SOURCE_LIST_LABELS"


class GuardError(RuntimeError):
    """前端表不是守卫能严格判定的规范形式，或后端/前端表不匹配——硬失败，不静默跳过。"""


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


def strip_ts_comments(src: str) -> str:
    """剥掉 TS/TSX 的 `//` 与 `/* */` 注释，字符串内的同形字符不误伤。

    必须在**搜索声明之前**对整份文件做一次:否则一份正确声明被整个塞进 `/* */`、后面再放一份
    错误的真实声明时，寻找声明的正则会先选中注释里那份。
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
                raise GuardError("块注释 /* 未闭合，无法可靠剥注释")
            i = end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    if quote is not None:
        raise GuardError(f"字符串字面量 {quote} 未闭合，无法可靠剥注释")
    return "".join(out)


def _object_literal_body(text: str, open_idx: int) -> str:
    """从 `{` 起取平衡括号内的对象体（字符串感知）。"""
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
    raise GuardError("对象字面量的花括号未闭合")


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切条目（字符串感知 + 括号深度感知）。"""
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


def parse_label_entries(body: str, *, const_name: str) -> dict[str, str]:
    """严格解析对象体。任何非规范条目或重复键都抛 GuardError，不静默跳过。"""
    labels: dict[str, str] = {}
    for entry in _split_top_level(body):
        m = _ENTRY_RE.match(entry)
        if m is None:
            raise GuardError(
                f"{const_name} 含守卫无法严格判定的条目（spread / computed key / "
                "非字面量值 / 简写 / 模板串 / 转义），运行时对象可能与守卫读到的不一致，"
                f"拒绝放行: {entry.strip()!r}"
            )
        key = next(
            g for g in (m["bare_key"], m["dq_key"], m["sq_key"]) if g is not None
        )
        value = m["dq_val"] if m["dq_val"] is not None else m["sq_val"]
        if key in labels:
            raise GuardError(
                f"{const_name} 重复键 {key!r}：JS 与 dict() 都会静默折叠成最后一个，"
                "守卫显式拒绝"
            )
        labels[key] = value
    return labels


def frontend_labels(const_name: str, path: Path | None = None) -> dict[str, str]:
    """从 `path`（默认 answer-panel.tsx）里严格提取名为 `const_name` 的对象字面量。"""
    source = (path or DEFAULT_TSX).read_text(encoding="utf-8")
    text = strip_ts_comments(source)
    escaped = re.escape(const_name)
    any_decl_re = re.compile(rf"\b(?:const|let|var)\s+{escaped}\b")
    literal_decl_re = re.compile(rf"\b(?:const|let|var)\s+{escaped}\b[^=;{{]*=\s*\{{")

    declarations = any_decl_re.findall(text)
    if not declarations:
        raise GuardError(
            f"{(path or DEFAULT_TSX).name}: 未找到 {const_name} 声明"
        )
    if len(declarations) > 1:
        raise GuardError(
            f"{(path or DEFAULT_TSX).name}: 找到 {len(declarations)} 份 {const_name} "
            "声明，守卫无法判定哪一份在运行时生效"
        )
    literal = literal_decl_re.search(text)
    if literal is None:
        raise GuardError(
            f"{(path or DEFAULT_TSX).name}: {const_name} 不是 `= {{…}}` 对象字面量声明，"
            "守卫无法零求值判定其内容"
        )
    body = _object_literal_body(text, literal.end() - 1)
    return parse_label_entries(body, const_name=const_name)


def _expected_from_backend(backend: dict[str, str]) -> dict[str, str]:
    return {key: f"{value}{LIST_SUFFIX}" for key, value in backend.items()}


def _report_mismatch(label: str, expected: dict[str, str], actual: dict[str, str]) -> None:
    print(f"{label} label 跨栈契约 MISMATCH", file=sys.stderr)
    print(f"  expected (backend + 「{LIST_SUFFIX}」): {expected}", file=sys.stderr)
    print(f"  actual (frontend)                    : {actual}", file=sys.stderr)
    diff = {
        k: (expected.get(k), actual.get(k))
        for k in set(expected) | set(actual)
        if expected.get(k) != actual.get(k)
    }
    print(f"  差异(expected, actual): {diff}", file=sys.stderr)


def main(tsx_path: Path | None = None) -> int:
    try:
        frontend_elements = frontend_labels(FRONTEND_ELEMENT_CONST, tsx_path)
        frontend_kg_objects = frontend_labels(FRONTEND_KG_OBJECT_CONST, tsx_path)
        frontend_sources = frontend_labels(FRONTEND_SOURCE_CONST, tsx_path)
    except GuardError as exc:
        print("enumeration list label 跨栈契约无法判定", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 1

    ok = True
    pairs = (
        ("元素清单 (element kind)", BACKEND_ELEMENT_LABELS, frontend_elements),
        ("知识对象清单 (KG object type)", BACKEND_KG_OBJECT_LABELS, frontend_kg_objects),
        ("来源清单 (sources collection)", BACKEND_SOURCE_LABELS, frontend_sources),
    )
    for label, backend, frontend in pairs:
        expected = _expected_from_backend(dict(backend))
        if expected != frontend:
            ok = False
            _report_mismatch(label, expected, frontend)

    all_values = (
        list(frontend_elements.values())
        + list(frontend_kg_objects.values())
        + list(frontend_sources.values())
    )
    duplicates = sorted({v for v in all_values if all_values.count(v) > 1})
    if duplicates:
        ok = False
        print(
            "enumeration list label 契约 MISMATCH: 元素清单/知识对象清单/来源清单存在重名 "
            f"{duplicates}（三套标签必须全域唯一，否则模型账目回喂与结果卡会各叫一个名）",
            file=sys.stderr,
        )

    if not ok:
        return 1

    print(
        "enumeration list label 契约 OK: elements="
        f"{sorted(BACKEND_ELEMENT_LABELS)} kg_objects={sorted(BACKEND_KG_OBJECT_LABELS)}"
        f" sources={sorted(BACKEND_SOURCE_LABELS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
