"""前后端上传格式白名单一致性守卫。

后端白名单真源是 `app.services.parser_registry.SUPPORTED_SOURCE_SUFFIXES`（由
builtin 引擎的扩展名表派生，source_routes 从它 import）；前端的
`DEFAULT_SUPPORTED_SOURCE_EXTENSIONS`（frontend/app/system-api.ts）是「系统配置返回
前可用」的兼容默认值，运行时权威清单由 /system/config 下发、直接来自后端注册表。
兼容默认值与后端注册表仍是同一事实的两份手工同步拷贝（服务端下发覆盖不了「配置
还没返回」的窗口），此前只靠注释约定；`LEGACY_OFFICE_EXTENSIONS`（page.tsx）的
「另存为」提示列表则绝不能与已支持格式重叠。上传提示文案已由服务端清单派生，
不再是手工拷贝，无需守卫。

两轮 codex 评审揪出的假绿路径（本文件的 `test_extract_string_array_*` 单测逐条钉住）：
  1. 数组里注释掉一项（`// "xls",`）—— 若不剥注释，正则会把注释文本当成真实内容。
  2. 用 `/* */` 把一份"看起来正确"的声明整个包起来，后面再放一份缺项的真声明——
     必须先剥注释再找声明，否则正则会先命中注释里那份。
  3. 后端加后缀、前端只在注释里加同名字符串（`// "rtf", // TODO`）——同 1，
     不剥注释就会误读注释文本为真实条目。
  4. 数组不是以字面量 `];` 收尾（如 `] as const;`）时，非贪婪 `.*?\\];`（DOTALL）
     会跳过真正的收口方括号，一路扫到文件后面某个不相关的 `];`，把中间大段无关
     代码的字符串字面量一并吞进结果——评审实测吞下 26 个无关字面量却不报错。
     修法是显式方括号深度计数（`_array_literal_body`），只认与开括号配对的收口
     括号，不依赖字面量收尾形状。
  5. 同名声明出现 >1 次（剥注释后）时无法判定运行时到底生效哪一份——必须硬失败，
     不能悄悄选中第一个匹配。
  6. `: string[]` 类型标注不是必需语法（`const X = [...]` 同样合法 TS）——声明识别
     正则把类型标注做成可选，而不是把没写标注的合法写法当成"提取失败"误报。

复用 `scripts/check_object_type_labels_contract.py` 的 `strip_ts_comments()`（同一份
字符串感知注释剥离实现，那边的 docstring 记录的正是同一类假绿路径）而不是重新实现一份
——重新实现只能证明这份复制品会剥注释，证明不了真正被两个守卫共用的那份函数会剥注释。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from app.services.parser_registry import SUPPORTED_SOURCE_SUFFIXES

ROOT = Path(__file__).resolve().parents[2]
PAGE_TSX = ROOT / "frontend" / "app" / "page.tsx"
SYSTEM_API_TS = ROOT / "frontend" / "app" / "system-api.ts"

_GUARD_SPEC = importlib.util.spec_from_file_location(
    "otl_guard_for_source_format_parity",
    ROOT / "scripts" / "check_object_type_labels_contract.py",
)
_otl_guard = importlib.util.module_from_spec(_GUARD_SPEC)
_GUARD_SPEC.loader.exec_module(_otl_guard)
strip_ts_comments = _otl_guard.strip_ts_comments
_StripGuardError = _otl_guard.GuardError

_STRING_LITERAL_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'')

# strip_ts_comments() 的字符串感知扫描不解析正则字面量（其own docstring 记录了这条
# 刻意取舍）：page.tsx 里别处的 `/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi` 这类
# 正则字面量会把朴素扫描器的引号/注释状态机带偏，对**整份** 40 万字符的文件直接剥
# 注释会在离目标声明几千行外的无关代码上炸掉 GuardError——这不是我们要测的东西。
# 所以只围绕目标声明截一个有界的行窗口，只对窗口剥注释；窗口太窄可能截在一段跨行
# 注释/字符串中间（真实撞见过：目标声明后 40 行内可能有一段跨越
# 边界的 /** JSDoc */），于是窗口按行数几何增长直到能安全剥注释，或增长到覆盖全文件
# 仍失败才放弃（后者意味着目标声明本身所在的局部区域就有不平衡的引号/注释，那是真
# 异常，应该失败）。
_WINDOW_INITIAL_MARGIN_LINES = 40
_WINDOW_GROWTH_FACTOR = 4


def _bounded_stripped_window(source: str, name: str, *, file_label: str) -> str:
    """围绕 `name` 的 `const/let/var` 声明截取一个有界行窗口，剥注释后返回。"""
    decl_re = re.compile(rf"\b(?:const|let|var)\s+{re.escape(name)}\b")
    matches = list(decl_re.finditer(source))
    if not matches:
        raise AssertionError(
            f"源码里连 `const/let/var {name}` 这个声明形状都找不到（{file_label}）——"
            "常量可能被改名、删除，或声明关键字缺失。"
        )
    lines = source.split("\n")
    start_line = source.count("\n", 0, matches[0].start())
    end_line = source.count("\n", 0, matches[-1].end())

    margin = _WINDOW_INITIAL_MARGIN_LINES
    last_error: Exception | None = None
    while True:
        lo = max(0, start_line - margin)
        hi = min(len(lines), end_line + margin + 1)
        window = "\n".join(lines[lo:hi])
        try:
            return strip_ts_comments(window)
        except _StripGuardError as exc:
            last_error = exc
            if lo == 0 and hi == len(lines):
                break
            margin *= _WINDOW_GROWTH_FACTOR
    raise AssertionError(
        f"围绕 {name} 声明（{file_label}）截取的窗口反复无法安全剥注释，"
        f"即便扩到整份文件仍失败：{last_error}——目标声明所在的局部区域本身存在"
        "不平衡的引号/注释，这是真实异常，不是窗口太窄。"
    )

def _array_literal_body(text: str, open_idx: int) -> str:
    """从 `[`（位于 open_idx）起取平衡方括号内的原始内容，字符串感知。

    显式深度计数而非非贪婪正则：非贪婪 `.*?\\];` 在数组不是以字面量 `];` 收尾时
    （如 TS 的 `] as const;`）会越过真正的收口方括号，扫到文件后面某个无关的
    `];`，把中间的无关内容一并纳入捕获——这里只认与 open_idx 配对的那个收口
    方括号，不依赖收尾形状。
    """
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
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
        i += 1
    raise AssertionError(f"数组字面量（起始于字符偏移 {open_idx}）的方括号未闭合。")


def _find_unique_array_open_bracket(stripped_source: str, name: str, *, file_label: str) -> int:
    """在**已剥注释**的源码里定位 `name` 数组声明的开括号，要求唯一匹配。

    `: string[]` 类型标注做成可选——`const X = [...]` 不带标注同样是合法 TS，把它
    当"提取失败"是误报；但声明与 `=` 之间、`=` 与 `[` 之间只允许空白，不接受
    "看起来像"数组声明实则是别的表达式的写法。
    """
    head_re = re.compile(
        rf"\b(?:const|let|var)\s+{re.escape(name)}\b(?:\s*:\s*string\[\])?\s*=\s*\["
    )
    matches = list(head_re.finditer(stripped_source))
    if not matches:
        raise AssertionError(
            f"未能在 {file_label}（剥注释后）中定位 {name} 数组声明——"
            "常量可能被改名、删除，或声明形状偏离 `const NAME[: string[]] = [...]`；"
            "这个守卫必须先能提取到内容才能校验一致性。"
        )
    if len(matches) > 1:
        raise AssertionError(
            f"{name} 在 {file_label} 中（剥注释后）匹配到 {len(matches)} 处声明——"
            "存在多份声明时无法判定运行时到底生效哪一份，必须清理为唯一声明；"
            "这也堵住了「正确声明被 /* */ 包起来、后面另放一份错误声明」的假绿路径"
            "（剥注释后前者已从源码消失，如果还能匹配到 >1 处，说明确实是两份都在生效）。"
        )
    return matches[0].end() - 1  # 指向匹配末尾的那个 "["


def extract_string_array(source: str, name: str, *, file_label: str) -> list[str]:
    """从整份（未剥注释的）TS 源码中提取名为 `name` 的字符串数组常量。"""
    stripped = _bounded_stripped_window(source, name, file_label=file_label)
    open_idx = _find_unique_array_open_bracket(stripped, name, file_label=file_label)
    body = _array_literal_body(stripped, open_idx)
    items = [g1 or g2 for g1, g2 in _STRING_LITERAL_RE.findall(body)]
    if not items:
        raise AssertionError(
            f"{name} 数组在 {file_label} 中被找到但解析出的字符串列表为空——"
            "这同样是守卫必须 fail 而不是静默通过的情形。"
        )
    return items


# --------------------------------------------------------------------------
# 集成测试：对真实的 frontend/app/page.tsx 与后端白名单做一致性校验。
# --------------------------------------------------------------------------


def test_frontend_backend_supported_suffixes_match():
    source = SYSTEM_API_TS.read_text(encoding="utf-8")
    frontend_exts = extract_string_array(
        source, "DEFAULT_SUPPORTED_SOURCE_EXTENSIONS", file_label=str(SYSTEM_API_TS)
    )
    frontend_suffixes = {f".{ext}" for ext in frontend_exts}
    backend_suffixes = set(SUPPORTED_SOURCE_SUFFIXES)

    only_frontend = frontend_suffixes - backend_suffixes
    only_backend = backend_suffixes - frontend_suffixes
    assert frontend_suffixes == backend_suffixes, (
        "前端兼容默认清单与后端注册表白名单不一致。\n"
        f"仅前端有（{SYSTEM_API_TS}）: {sorted(only_frontend)}\n"
        "仅后端有（backend/app/services/parser_registry.py 的 builtin 引擎）: "
        f"{sorted(only_backend)}"
    )


def test_legacy_office_hint_excludes_supported_formats():
    source = PAGE_TSX.read_text(encoding="utf-8")
    legacy_exts = extract_string_array(
        source, "LEGACY_OFFICE_EXTENSIONS", file_label=str(PAGE_TSX)
    )
    overlap = {f".{ext}" for ext in legacy_exts} & set(SUPPORTED_SOURCE_SUFFIXES)
    assert not overlap, (
        "LEGACY_OFFICE_EXTENSIONS（旧格式另存为引导提示）里出现了后端已支持格式，"
        f"重叠: {sorted(overlap)}——已支持格式不该再触发「请另存为」提示。"
    )




# --------------------------------------------------------------------------
# 单元测试：直接给 extract_string_array 喂合成 TS 片段，逐条钉住评审列出的假绿路径。
# 这些测试独立于 page.tsx 的真实内容，即使真实文件从不出现某种写法，回归也能守住。
# --------------------------------------------------------------------------


def test_extract_string_array_ignores_commented_out_entry():
    src = """
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", // "xls",
];
"""
    items = extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")
    assert items == ["pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm"]


def test_extract_string_array_ignores_declaration_hidden_in_block_comment():
    src = """
/*
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
];
*/
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm",
];
"""
    items = extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")
    # 必须读到后面那份真声明（缺 "xls"），不能被注释里那份"看起来正确"的声明骗过。
    assert items == ["pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm"]


def test_extract_string_array_ignores_extra_entry_hidden_in_line_comment():
    src = """
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
  // "rtf", // TODO
];
"""
    items = extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")
    assert "rtf" not in items


def test_extract_string_array_rejects_duplicate_declarations():
    src = """
const SUPPORTED_SOURCE_EXTENSIONS: string[] = ["pdf"];
const SUPPORTED_SOURCE_EXTENSIONS: string[] = ["pdf", "docx"];
"""
    with pytest.raises(AssertionError):
        extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")


def test_extract_string_array_bounded_capture_does_not_scan_past_close_bracket():
    noise_literals = ", ".join(f'"noise{i}"' for i in range(26))
    src = f"""
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
] as const;

const UNRELATED_LOOKALIKE = [
  {noise_literals}
];
"""
    items = extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")
    assert items == [
        "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
    ]


def test_extract_string_array_accepts_declaration_without_type_annotation():
    src = 'const SUPPORTED_SOURCE_EXTENSIONS = ["pdf", "docx"];'
    items = extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")
    assert items == ["pdf", "docx"]


def test_extract_string_array_fails_when_constant_renamed():
    src = 'const SUPPORTED_SOURCE_EXTENSIONS_X: string[] = ["pdf"];'
    with pytest.raises(AssertionError):
        extract_string_array(src, "SUPPORTED_SOURCE_EXTENSIONS", file_label="<test>")
