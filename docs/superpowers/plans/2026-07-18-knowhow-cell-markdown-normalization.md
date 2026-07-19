# Knowhow 格子 Markdown 规整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把从 Excel 逐字导入的 knowhow 格子内容（Alt+Enter 换行 / Tab 缩进 / `•` 项目符号 / `A.`/`a.` 字母编号）规整成干净 CommonMark，让查看渲染与 `parse_steps` 同时正确；覆盖存量回填与增量（导入/粘贴/编辑）。

**Architecture:** 核心是一个后端管线 `reformat_cell(raw) = LLM 重排 → 零-LLM 内容不变式硬校验 → 不过则退规则规整器`，返回候选交人工确认。规则规整器 `rule_normalize` 与内容不变式 `content_signature` 共用同一套 marker/缩进检测（单一真源）。落点：编辑器「规整格式」按钮＋粘贴、导入 inline 规则规整、批量、回填脚本。前端另有一份 TS 版 `rule_normalize`，与 Python 版跑同一份 golden 夹具做 parity。

**Tech Stack:** Python 3.13（backend，FastAPI + SQLite repository）、pytest；Next.js/React + TypeScript（frontend），node --test（`.test.mjs`）；LLM 走 per-user rewrite client（复用现有 `optimize_cell` 基建）。

## Global Constraints

- **零新表 / 不 bump `SCHEMA_VERSION`**：本特性不加表、不改结构，纯代码 + 一次性回填。
- **架构守卫**：facade 新成员走 allowlist + 一跳委托；`test_repository_surface_manifest` 行号敏感——新增/移动**测试**会移动行号，须重生成 `EXPECTED_PATCH_DELTAS`（见 memory「surface-manifest行号脆弱」）；改架构文档措辞须同步 `test_architecture_documentation.py`。
- **效率约束（用户强约束）**：导入默认走**规则规整**（零 LLM）；LLM 精整只在**人工触发**（编辑器按钮 / 批量 / 回填 `--use-llm`）。
- **内容不变式放宽标点**：`content_signature` 只保留 **CJK 表意文字 + ASCII 字母 + 数字**，剥离所有空白/list marker/强调符/**标点与符号**。数字/字母/CJK 逐字严校。
- **`rule_normalize` 永不抛异常**：任何异常 → 返回原文。
- **Python 解释器**：本机用 root conda（`/opt/homebrew/Caskroom/miniconda/base/bin/python`，记为 `$PYBIN`），worktree 无 venv。**worktree 无 `frontend/node_modules`**：前端测试/构建借 root checkout（`/Users/hzf/workspace/silicon_notebook/frontend`）的 node_modules 跑，或从 root 验证后 patch（见 memory「多代理共用checkout」）。
- **面向用户文案友好**：按钮/提示用中文、不暴露技术细节（不出现 "invariant" / "signature" 等）。
- **分支**：`claude/knowhow-md-normalize`（已建，off master）。频繁提交，保持线性（PR 走 Rebase and merge）。
- **wire 命名**：列的内容类型 DB 字段是 `role`，wire 字段是 `kind`（`anchor/procedure/entity/attribute`）——沿用现状，不要改。

---

### Task 1: 规则规整器 `rule_normalize` + 共享 marker/缩进检测

**Files:**
- Create: `backend/app/services/knowhow/md_normalize.py`
- Create: `backend/tests/test_md_normalize_rule.py`
- Create: `backend/tests/fixtures/knowhow_normalize_golden.json`（前后端 parity 共享真源）

**Interfaces:**
- Produces:
  - `classify_line(line: str) -> LineInfo` — `LineInfo` 是 `@dataclass`：`kind: str`（`'bullet'|'ordered'|'alpha'|'prose'|'blank'`）、`depth: int`、`body: str`、`marker: str`。
  - `rule_normalize(raw: str) -> str` — 幂等、永不抛。
  - 模块级常量 `BULLET_GLYPHS = "•●◦▪‣·"`。

- [ ] **Step 1: 建 golden 夹具（前后端共享真源）**

`backend/tests/fixtures/knowhow_normalize_golden.json`：数组，每项 `{name, raw, expect_contains, expect_absent}`。`expect_contains`/`expect_absent` 是「输出必须/不得包含的行」，比脆弱的整串相等更稳。用真实 DeepSeek-V4 格子做种子：

```json
[
  {
    "name": "bullets_under_alpha_header",
    "raw": "通过用底层走线加shielding并使用1W1S rule，同时增大R、C，增大线延。\nA. 增大 R 和 C 的考量\n\t• 增加RC时间常数，delay正比于R*C\n\t• 增大 R： 导致转换时间（Transition Time）变慢。\nB. 1W1S rule 与 Shielding 的作用\n\t• Shielding (加屏蔽线)： 保证信号完整性。",
    "expect_contains": ["**A. 增大 R 和 C 的考量**", "- 增加RC时间常数，delay正比于R*C", "**B. 1W1S rule 与 Shielding 的作用**", "- Shielding (加屏蔽线)： 保证信号完整性。"],
    "expect_absent": ["\t", "•"]
  },
  {
    "name": "ordered_with_alpha_substeps",
    "raw": "\n单条path跨多corner s/h打架\n\n1. 获取available buffer在各个corner下的延时\n2. 提取Hold违例path\n4. 计算是否会导致setup违例：\n\ta. cornerA下，margin充足\n\tb. 所有corner裕量充足",
    "expect_contains": ["单条path跨多corner s/h打架", "1. 获取available buffer在各个corner下的延时", "  - cornerA下，margin充足", "  - 所有corner裕量充足"],
    "expect_absent": ["\t", "\ta."]
  },
  {
    "name": "tab_prefixed_ordered_still_lists",
    "raw": "实际操作：\n\t1. 遍历各个corner\n\t2. 输出delta delay较大的net",
    "expect_contains": ["实际操作：", "1. 遍历各个corner", "2. 输出delta delay较大的net"],
    "expect_absent": ["\t1.", "\t2."]
  },
  {
    "name": "two_prose_lines_stay_separate",
    "raw": "分析 pt\n修复 innovus",
    "expect_contains": ["分析 pt", "修复 innovus"],
    "expect_absent": ["分析 pt 修复 innovus"]
  },
  {
    "name": "idempotent_clean_markdown",
    "raw": "**A. 标题**\n\n- 一\n- 二",
    "expect_contains": ["**A. 标题**", "- 一", "- 二"],
    "expect_absent": ["\t", "•"]
  }
]
```

- [ ] **Step 2: 写失败测试 `test_md_normalize_rule.py`**

```python
import json, pathlib, re
import pytest
from app.services.knowhow.md_normalize import rule_normalize, classify_line, BULLET_GLYPHS

_GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "knowhow_normalize_golden.json").read_text("utf-8"))

@pytest.mark.parametrize("case", _GOLDEN, ids=[c["name"] for c in _GOLDEN])
def test_rule_normalize_golden(case):
    out = rule_normalize(case["raw"])
    lines = out.split("\n")
    for needle in case["expect_contains"]:
        assert needle in lines, f"{case['name']}: expected line {needle!r} in:\n{out}"
    for absent in case["expect_absent"]:
        assert absent not in out, f"{case['name']}: {absent!r} should be gone in:\n{out}"

def test_no_leading_tab_ever():
    out = rule_normalize("\t• a\n\t\tb. nested")
    assert not any(l.startswith("\t") for l in out.split("\n"))

def test_bullet_glyph_becomes_dash():
    assert rule_normalize("• foo").split("\n")[0] == "- foo"

def test_section_header_alpha_at_col0_is_bolded():
    assert "**A. 考量**" in rule_normalize("A. 考量\n\t• x").split("\n")

def test_never_raises_on_garbage():
    # must return a str, never throw, even on pathological input
    assert isinstance(rule_normalize("\t\t\t)(*&^%\n\x00\n•••"), str)

def test_empty_stays_empty():
    assert rule_normalize("") == ""
    assert rule_normalize("   \n  ") == ""

def test_idempotent():
    once = rule_normalize(_GOLDEN[0]["raw"])
    assert rule_normalize(once) == once
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_md_normalize_rule.py -q`
Expected: FAIL（`ModuleNotFoundError: app.services.knowhow.md_normalize`）

- [ ] **Step 4: 实现 `md_normalize.py`（规则规整器 + 共享检测）**

```python
"""Excel 习惯排版 → 干净 CommonMark 的确定性规整器（零 LLM）。

也导出共享的 marker/缩进检测，供 content_signature 复用（单一真源），
以便「规整器判为 list marker 的字符」与「校验器剥离的字符」严格一致。
"""
from __future__ import annotations
import re
from dataclasses import dataclass

BULLET_GLYPHS = "•●◦▪‣·"

_BULLET_RE = re.compile(rf"^([{re.escape(BULLET_GLYPHS)}]|[-*+])[ \t]+(.*)$")
_ORDERED_RE = re.compile(r"^(\d+)[.)、][ \t]+(.*)$")
_ALPHA_RE = re.compile(r"^([A-Za-z])[.)、][ \t]+(.*)$")


@dataclass
class LineInfo:
    kind: str          # 'bullet' | 'ordered' | 'alpha' | 'prose' | 'blank'
    depth: int         # 缩进层级
    body: str          # marker 之后的正文
    marker: str = ""   # ordered 的数字 / alpha 的字母


def _indent_depth(line: str) -> int:
    """每个前导 TAB = 1 层；随后每 2 个前导空格 = 1 层。"""
    i, tabs = 0, 0
    while i < len(line) and line[i] == "\t":
        tabs += 1
        i += 1
    spaces = 0
    while i < len(line) and line[i] == " ":
        spaces += 1
        i += 1
    return tabs + spaces // 2


def classify_line(line: str) -> LineInfo:
    if not line.strip():
        return LineInfo("blank", 0, "")
    depth = _indent_depth(line)
    stripped = line.lstrip("\t ")
    m = _BULLET_RE.match(stripped)
    if m:
        return LineInfo("bullet", depth, m.group(2).strip())
    m = _ORDERED_RE.match(stripped)
    if m:
        return LineInfo("ordered", depth, m.group(2).strip(), m.group(1))
    m = _ALPHA_RE.match(stripped)
    if m:
        return LineInfo("alpha", depth, m.group(2).strip(), m.group(1))
    return LineInfo("prose", depth, stripped.strip())


def rule_normalize(raw: str) -> str:
    """规整入口——永不抛：任何异常返回原文。"""
    try:
        return _normalize(raw)
    except Exception:
        return raw


def _normalize(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    src = raw.replace("\r\n", "\n").replace("\r", "\n")
    infos = [classify_line(l) for l in src.split("\n")]
    non_blank = [i for i in infos if i.kind != "blank"]
    if not non_blank:
        return ""
    cell_min = min(i.depth for i in non_blank)   # 单元格最小缩进（通常 0）

    out: list[str] = []
    prev = "start"                     # 'prose' | 'list' | 'header' | 'start'
    group_base: "int | None" = None    # 当前列表组「顶层」的源缩进；渲染层级 = depth - group_base
    for info in infos:
        if info.kind == "blank":
            continue                   # 间距完全由 prev 重推；空行不重置列表组
        # 顶格 alpha（A. / B.，处在 cell_min）= 分节标题 → 加粗段落；缩进的 alpha 是子项
        if info.kind == "alpha" and info.depth == cell_min:
            if out:
                out.append("")
            out.append(f"**{info.marker}. {info.body}**")
            prev, group_base = "header", None
            continue
        if info.kind in ("bullet", "ordered", "alpha"):
            if prev != "list" or group_base is None:
                group_base = info.depth          # 新列表组的顶层基线（关键：随 prose/header 重置）
            level = max(0, info.depth - group_base)
            indent = "  " * level
            marker = f"{info.marker}. " if info.kind == "ordered" else "- "
            if prev in ("prose", "header"):
                out.append("")                   # 列表开始前补空行
            out.append(f"{indent}{marker}{info.body}")
            prev = "list"
        else:  # prose
            if out:
                out.append("")                   # 相邻散行各自成段
            out.append(info.body)
            prev, group_base = "prose", None

    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result
```

**算法要点（实现者注意）：** 渲染缩进层级 = `depth - group_base`，`group_base` 是「当前连续列表组顶层的源缩进」，**遇到 prose/分节标题就重置**。这样：① `**A.**` 标题下 Tab 缩进的 `•` 是**顶层**列表（`- `，因为标题重置了组、bullet 成为新组顶层）；② `1.` 下 `\ta.` 才是**嵌套**（`  - `，同组内 depth 更深）；③ 整体带 1 个 Tab 的 `\t1.` 列表归到顶层。空行不重置列表组，只有 prose/header 重置。

- [ ] **Step 5: 跑测试直到全绿（TDD 迭代）**

Run: `cd backend && $PYBIN -m pytest tests/test_md_normalize_rule.py -q`
Expected: PASS（全部）。若 `bullets_under_alpha_header` 的 `- ` 被多缩进，或 `ordered_with_alpha_substeps` 的 `  - ` 没缩进，重点检查 `group_base` 重置时机（prose/header 重置、空行不重置）与 `level` 计算；若 golden 某行差一个空格，调 `expect_contains` 或 emitter，勿改语义。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/knowhow/md_normalize.py backend/tests/test_md_normalize_rule.py backend/tests/fixtures/knowhow_normalize_golden.json
git commit -m "feat(knowhow): 规则型 Excel→CommonMark 规整器 rule_normalize"
```

---

### Task 2: 内容不变式校验 `content_signature` / `content_invariant`（放宽标点）

**Files:**
- Modify: `backend/app/services/knowhow/md_normalize.py`（追加）
- Create: `backend/tests/test_md_normalize_invariant.py`

**Interfaces:**
- Consumes: `classify_line`（Task 1，判定并剥离 list marker）。
- Produces:
  - `content_signature(md: str) -> str` — 只保留 CJK+字母+数字的有序序列。
  - `content_invariant(before: str, after: str) -> bool` — signature 相等 **且** 图片引用集合、代码块内容逐字未变。

- [ ] **Step 1: 写失败测试 `test_md_normalize_invariant.py`**

```python
from app.services.knowhow.md_normalize import content_signature, content_invariant

def test_format_only_change_passes():
    before = "A. 考量\n\t• 增大 R： 变慢\n\t• 增大 C： 变化"
    after = "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"   # 只改格式 + 半角冒号 + 去空格
    assert content_invariant(before, after) is True

def test_punctuation_normalization_is_allowed():
    assert content_invariant("增大 R： 导致（Transition）", "增大 R:导致(Transition)") is True

def test_dropped_sentence_is_rejected():
    assert content_invariant("第一句。第二句。", "第一句。") is False

def test_changed_number_is_rejected():
    assert content_invariant("频率1800M", "频率1000M") is False

def test_reordered_bullets_rejected():
    assert content_invariant("- 甲\n- 乙", "- 乙\n- 甲") is False

def test_dropped_word_cjk_rejected():
    assert content_invariant("不是让信号走得慢", "是让信号走得慢") is False

def test_image_ref_must_survive():
    assert content_invariant("见 ![图](asset://x1)", "见 图示") is False
    assert content_invariant("见 ![图](asset://x1)", "**见** ![图](asset://x1)") is True

def test_signature_drops_markers_and_punct_keeps_words():
    assert content_signature("\t• 增大 R： 变慢（快）") == content_signature("- 增大R:变慢(快)")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_md_normalize_invariant.py -q`
Expected: FAIL（`ImportError: cannot import name 'content_signature'`）

- [ ] **Step 3: 实现（追加到 `md_normalize.py`）**

```python
import unicodedata

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")


def _is_content_char(ch: str) -> bool:
    """内容 = CJK 表意文字 / ASCII 字母 / 数字。标点、符号、空白一律不算。"""
    if ch.isascii() and (ch.isalnum()):
        return True
    cat = unicodedata.category(ch)          # 'Lo' = CJK 等表意文字；'Nd'/'Nl' = 数字
    return cat.startswith("L") or cat.startswith("N")


def content_signature(md: str) -> str:
    """剥掉所有格式与标点，返回有序「有意义字符序列」。放宽标点：标点/符号不计入。"""
    text = md or ""
    text = _IMAGE_RE.sub(" ", text)         # 图片单独校验，不进 signature
    text = _FENCE_RE.sub(" ", text)         # 代码块单独校验
    out = []
    for raw_line in text.split("\n"):
        info = classify_line(raw_line)       # 复用规整器的 marker 检测 → 剥 list marker
        body = info.body if info.kind != "blank" else ""
        body = _EMPHASIS_RE.sub("", body)    # 剥强调符
        body = body.lstrip("#> ").strip()    # 剥标题符/引用符
        for ch in body:
            if _is_content_char(ch):
                out.append(ch)
    return "".join(out)


def _image_refs(md: str) -> list[str]:
    return _IMAGE_RE.findall(md or "")


def _code_blocks(md: str) -> list[str]:
    return _FENCE_RE.findall(md or "")


def content_invariant(before: str, after: str) -> bool:
    if content_signature(before) != content_signature(after):
        return False
    if _image_refs(before) != _image_refs(after):
        return False
    if _code_blocks(before) != _code_blocks(after):
        return False
    return True
```

注意 `classify_line` 对**顶格 alpha**（`A. xxx`）返回 `kind='alpha'` 会把 `A` 当 marker 剥掉——但顶格 alpha 是分节标题、其字母算内容。修正：`content_signature` 里对 `info.kind == 'alpha' and info.depth == 0` 的行，改用整行原文（去强调符）而非 `info.body`，让 `A` 保留。实现时在循环内加这一分支并让 `test_signature_drops_markers_and_punct_keeps_words` 等通过（`A. 考量` 的 `A` 应保留，`\ta.`（缩进）的 `a` 应剥）。补一条测试：

```python
def test_section_header_letter_is_content_but_nested_letter_is_format():
    # 顶格 A. 的 A 算内容；缩进 a. 的 a 算格式
    assert content_signature("A. 考量") == content_signature("**A. 考量**")   # A 保留、两边一致
    assert content_signature("A. 考量") != content_signature("考量")            # 丢了 A → 不同
    assert content_signature("\ta. 子项") == content_signature("- 子项")        # 缩进 a 剥掉
```

- [ ] **Step 4: 跑测试直到全绿**

Run: `cd backend && $PYBIN -m pytest tests/test_md_normalize_invariant.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/knowhow/md_normalize.py backend/tests/test_md_normalize_invariant.py
git commit -m "feat(knowhow): 内容不变式校验 content_invariant（放宽标点）"
```

---

### Task 3: LLM 规整 `llm_reformat` + 编排 `reformat_cell`

**Files:**
- Modify: `backend/app/services/knowhow/api.py`（新增函数，紧邻现有 `optimize_cell` :692 / `_optimize_cell_prompt` :664）
- Create: `backend/tests/test_knowhow_reformat.py`

**Interfaces:**
- Consumes: `md_normalize.rule_normalize` / `content_invariant`（Task 1/2）；per-user rewrite client（现有 `repo._runtime.models.rewrite_llm_client`，见 `optimize_cell`）。
- Produces:
  - `_reformat_cell_prompt(content_md: str, column_name: str, kind: str) -> str`
  - `reformat_cell(repo, content_md: str, column_name: str, kind: str) -> dict` — 返回 `{"candidate_md": str, "source": str, "changed": bool}`；`source ∈ {"llm","rule/llm-failed","rule/no-llm"}`。**从不写库**。

- [ ] **Step 1: 写失败测试 `test_knowhow_reformat.py`**（用假 client 覆盖三分支）

```python
import types
import pytest
from app.services.knowhow import api as kh_api

RAW = "A. 考量\n\t• 增大 R： 变慢\n\t• 增大 C： 变化"

def _repo_with_llm(reply):
    """构造一个最小 repo stub，其 rewrite client 返回给定 JSON。"""
    client = types.SimpleNamespace(chat_json=lambda *a, **k: {"reformatted_md": reply})
    models = types.SimpleNamespace(rewrite_llm_client=client)
    runtime = types.SimpleNamespace(models=models)
    return types.SimpleNamespace(_runtime=runtime)

def _repo_no_llm():
    models = types.SimpleNamespace(rewrite_llm_client=None)
    return types.SimpleNamespace(_runtime=types.SimpleNamespace(models=models))

def test_llm_pass_uses_llm_candidate():
    good = "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"   # 只改格式 → 过校验
    out = kh_api.reformat_cell(_repo_with_llm(good), RAW, "修复方法", "procedure")
    assert out["source"] == "llm"
    assert out["candidate_md"] == good
    assert out["changed"] is True

def test_llm_changed_content_falls_back_to_rule():
    bad = "**A. 考量**\n\n- 增大 R:变快"   # 删了内容 → 校验不过
    out = kh_api.reformat_cell(_repo_with_llm(bad), RAW, "修复方法", "procedure")
    assert out["source"] == "rule/llm-failed"
    assert "•" not in out["candidate_md"] and "\t" not in out["candidate_md"]

def test_no_llm_uses_rule():
    out = kh_api.reformat_cell(_repo_no_llm(), RAW, "修复方法", "procedure")
    assert out["source"] == "rule/no-llm"
    assert "**A. 考量**" in out["candidate_md"].split("\n")

def test_empty_cell_no_change():
    out = kh_api.reformat_cell(_repo_no_llm(), "", "修复方法", "procedure")
    assert out["changed"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_knowhow_reformat.py -q`
Expected: FAIL（`AttributeError: module 'app.services.knowhow.api' has no attribute 'reformat_cell'`）

- [ ] **Step 3: 实现 `_reformat_cell_prompt` + `llm_reformat` + `reformat_cell`（api.py）**

先读现有 `optimize_cell`（`api.py:692-736`）与 `_optimize_cell_prompt`（`:664-689`）确认 client 取用与 `chat_json` 调用形态，然后紧邻新增：

```python
from app.services.knowhow import md_normalize

_REFORMAT_SCHEMA_HINT = '{"reformatted_md": ""}'


def _reformat_cell_prompt(content_md: str, column_name: str, kind: str) -> str:
    procedure_clause = (
        "- 「方法步骤」列：若各步骤**已分行**，整理成有序列表（1. 2. 3. ...）；不要把一行拆成多行。\n"
        if kind == "procedure" else ""
    )
    return (
        f"你是表格知识库的排版助手。下面是「{column_name}」列某个格子的内容，"
        "它可能来自 Excel，带有 `•` 项目符号、`A.`/`a.` 编号、Tab 缩进、软换行。\n"
        "请**只整理每一行的排版标记**，把它变成干净的 CommonMark：\n"
        "- `•` 等符号转成 `- `；顶格的 `A.`/`B.` 分节标题用 `**加粗**`；缩进的 `a.`/`b.` 子项转成嵌套 `- `。\n"
        "- 可在段落/列表之间增删**空行**。\n"
        f"{procedure_clause}"
        "**保持行结构**：不要拆分或合并任何一行——每行的文字与总行数保持不变，只改行首标记、缩进、强调符与行间空行。\n"
        "**严禁**：改动、增删、翻译任何文字；调换行/句顺序；改动数字。\n"
        "允许：整理标点的全角/半角及其间距。\n"
        "`![说明](asset://...)` 图片引用必须原样保留。\n"
        "只输出整理后的 markdown 正文，不要解释、不要代码围栏包裹。\n\n"
        f"当前内容：\n{content_md}\n\n"
        f'严格按此 JSON 返回：{_REFORMAT_SCHEMA_HINT}'
    )

# 注：prompt 明令「保持行结构（不拆分/合并行）」，与 content_signature 的**按行**校验一致——
# 这样 LLM 不会去做校验必然拒绝的整行拆分。行内多步骤挤在一行的 procedure 格，
# LLM 与 rule_normalize 都不拆（一致、可预期）；用户可手动分行或用编辑器的有序列表按钮。


def llm_reformat(repo, content_md: str, column_name: str, kind: str) -> "str | None":
    """调 LLM 只做排版整理；client 不可用/失败返回 None。"""
    client = getattr(repo._runtime.models, "rewrite_llm_client", None)
    if client is None:
        return None
    try:
        prompt = _reformat_cell_prompt(content_md, column_name, kind)
        reply = client.chat_json(
            [{"role": "user", "content": prompt}],
            schema_hint=_REFORMAT_SCHEMA_HINT,
        )
        out = (reply or {}).get("reformatted_md")
        return out if isinstance(out, str) and out.strip() else None
    except Exception:
        return None


def reformat_cell(repo, content_md: str, column_name: str, kind: str) -> dict:
    """LLM 重排 → 内容不变式校验 → 不过退规则。返回候选，从不写库。

    source 区分 `rule/llm-failed` vs `rule/no-llm`：靠前置的 client 判定，
    绝不真调两次 LLM。
    """
    raw = content_md or ""
    if not raw.strip():
        return {"candidate_md": raw, "source": "rule/no-llm", "changed": False}
    client = getattr(repo._runtime.models, "rewrite_llm_client", None)
    if client is None:
        cand = md_normalize.rule_normalize(raw)
        return {"candidate_md": cand, "source": "rule/no-llm", "changed": cand != raw}
    cand = llm_reformat(repo, raw, column_name, kind)   # client 已确认存在
    if cand is not None and md_normalize.content_invariant(raw, cand):
        return {"candidate_md": cand, "source": "llm", "changed": cand != raw}
    cand = md_normalize.rule_normalize(raw)
    return {"candidate_md": cand, "source": "rule/llm-failed", "changed": cand != raw}
```

- [ ] **Step 4: 跑测试直到全绿**

Run: `cd backend && $PYBIN -m pytest tests/test_knowhow_reformat.py -q`
Expected: PASS（若 `chat_json` 参数名与现有 `optimize_cell` 不同，按现有真实签名对齐 stub 与实现）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/knowhow/api.py backend/tests/test_knowhow_reformat.py
git commit -m "feat(knowhow): reformat_cell 编排（LLM 重排+校验+规则兜底）"
```

---

### Task 4: `/reformat` HTTP 端点 + 前端 wire

**Files:**
- Modify: `backend/app/api/routes.py`（紧邻现有 optimize 端点 `:968-998`）
- Modify: `frontend/app/knowhow-model.ts`（紧邻 `optimizeKnowhowCell` `:570-579`）
- Modify: `backend/tests/test_knowhow_api.py`（或对应现有 knowhow 路由测试文件）

**Interfaces:**
- Consumes: `reformat_cell`（Task 3）。
- Produces:
  - HTTP `POST /notebooks/{nb}/knowhow/{table}/rows/{row}/cells/{col}/reformat` → `{"candidate_md","source","changed"}`（读**已保存**的 `content_md`）。
  - 前端 `reformatKnowhowCell(notebookId, tableId, rowId, columnId) => Promise<{candidateMd, source, changed}>`。

- [ ] **Step 1: 写失败测试**（镜像现有 optimize 路由测试；用 TestClient，mock reformat 结果或用 no-llm 走规则）

```python
def test_reformat_endpoint_returns_candidate(client, seeded_knowhow_cell):
    nb, table, row, col = seeded_knowhow_cell   # 内容含 '\t• x'
    r = client.post(f"/notebooks/{nb}/knowhow/{table}/rows/{row}/cells/{col}/reformat")
    assert r.status_code == 200
    body = r.json()
    assert "•" not in body["candidate_md"]
    assert body["source"] in ("llm", "rule/llm-failed", "rule/no-llm")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_knowhow_api.py -k reformat -q`
Expected: FAIL（404）

- [ ] **Step 3: 实现后端端点**（镜像 `optimize_knowhow_cell` `routes.py:973-998` 的**结构**：校验 cell/table/row 存在，不存在→404；读**已保存**的 `content_md`；调 `knowhow_api.reformat_cell(repo, content_md, column["name"], column["kind"])`；返回其 dict。**与 optimize 关键不同**：`reformat_cell` 对「未配置 LLM」等情况已在内部优雅兜底（返回 `source=rule/no-llm`，**不抛** `ModelNotConfiguredError`），所以本端点**不做** `ModelNotConfiguredError→400` 映射——始终 200 返回候选，前端据 `source` 展示。）

- [ ] **Step 4: 实现前端 wire（knowhow-model.ts）**

```ts
export const reformatKnowhowCell = (notebookId: string, tableId: string, rowId: string, columnId: string) =>
  apiFetch<{ candidate_md: string; source: string; changed: boolean }>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}/reformat`,
    { method: "POST" },
  ).then((w) => ({ candidateMd: w.candidate_md, source: w.source, changed: w.changed }));
```

- [ ] **Step 5: 跑后端测试确认通过 + Commit**

Run: `cd backend && $PYBIN -m pytest tests/test_knowhow_api.py -k reformat -q` → PASS

```bash
git add backend/app/api/routes.py frontend/app/knowhow-model.ts backend/tests/test_knowhow_api.py
git commit -m "feat(knowhow): /reformat 端点 + 前端 wire"
```

⚠️ 若新增测试移动了 `routes.py`/repository 的行号，跑并按提示更新 surface_manifest 守卫（见 Global Constraints）。

---

### Task 5: 导入 inline 规则规整（增量·零 LLM）

**Files:**
- Modify: `backend/app/services/knowhow/api.py`（`import_table` `:201-205`，及 `commit_append` `:619-623` 的等价落库处）
- Modify: `backend/tests/test_knowhow_import.py`（或现有导入测试文件）

**Interfaces:**
- Consumes: `rule_normalize`（Task 1）。
- Produces: 导入落库前每个非空 cell 值经 `rule_normalize`。

- [ ] **Step 1: 写失败测试**（导入一个 xlsx/grid，其 cell 含 `\t•`，断言存库后 `content_md` 无 `\t`/`•`、含 `- `）

```python
def test_import_normalizes_excel_idioms(repo, nb_id):
    grid_rows = [["概念X", "A. 考量\n\t• 增大 R\n\t• 增大 C"]]
    # 走真实 import_table（或其内部 add_knowhow_row 循环）后：
    stored = _first_procedure_cell(repo, nb_id)
    assert "\t" not in stored and "•" not in stored
    assert "- 增大 R" in stored.split("\n")
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（存的是 verbatim）

- [ ] **Step 3: 实现**——在 `import_table` 落库循环里包一层：

```python
from app.services.knowhow.md_normalize import rule_normalize
...
for row in rows:
    cells = {column_ids[i]: rule_normalize(value) for i, value in enumerate(row) if value}
    repo.add_knowhow_row(table_id, cells)
```

`commit_append` 的等价落库处同样处理。**只规则、不 LLM**（守效率约束）。

- [ ] **Step 4: 跑测试确认通过 + Commit**

```bash
git add backend/app/services/knowhow/api.py backend/tests/test_knowhow_import.py
git commit -m "feat(knowhow): 导入落库前 inline 规则规整"
```

---

### Task 6: 回填脚本 `backfill_knowhow_md.py`（存量·dry-run 优先）

**Files:**
- Create: `scripts/backfill_knowhow_md.py`
- Create: `backend/tests/test_backfill_knowhow_md.py`
- Modify: `README.md` / `README_zh.md`（CLI 用法，保持通用口径）

**Interfaces:**
- Consumes: `reformat_cell`（Task 3）、repository 读写 knowhow cells。
- Produces: CLI `python scripts/backfill_knowhow_md.py --notebook <id> [--apply] [--use-llm]`；默认 dry-run 打印每格 `before/after/source/changed`。

- [ ] **Step 1: 写失败测试**（构造一个含脏格的 notebook，跑脚本的核心函数 `plan_backfill(repo, notebook_id, use_llm) -> list[dict]`，断言 dry-run 不写库、`apply_backfill` 才写、幂等第二次 `changed=0`）

```python
def test_backfill_dry_run_does_not_write(repo, nb_with_dirty_cells):
    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    assert any(p["changed"] for p in plan)
    # 库未变
    assert _still_has_tab(repo, nb_with_dirty_cells)

def test_backfill_apply_then_idempotent(repo, nb_with_dirty_cells):
    apply_backfill(repo, plan_backfill(repo, nb_with_dirty_cells, use_llm=False))
    assert not _still_has_tab(repo, nb_with_dirty_cells)
    plan2 = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    assert all(not p["changed"] for p in plan2)   # 幂等
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现脚本**——`plan_backfill` 遍历 notebook 所有 knowhow 表/行/列，对每个非空 cell 调 `reformat_cell`（`use_llm=False` 时强制走 `rule_normalize`），产出计划；`apply_backfill` 在单事务里对 `changed` 的 cell 调 `repo.update_knowhow_cell`（复用现有写路径，自动置 row pending → reprojection）。`main()` 解析 `--notebook/--apply/--use-llm`，dry-run 打印 diff 表，非 `--apply` 不写。入口从主 checkout 根跑（worktree 无 .env）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_backfill_knowhow_md.py -q` → PASS

- [ ] **Step 5: 写 README（中英）+ Commit**

`README.md` / `README_zh.md` 加一节：脚本用途、`--notebook/--apply/--use-llm` 语义、默认 dry-run、先看 diff 再 `--apply`。

```bash
git add scripts/backfill_knowhow_md.py backend/tests/test_backfill_knowhow_md.py README.md README_zh.md
git commit -m "feat(knowhow): 存量格子 Markdown 规整回填脚本（dry-run 优先）"
```

---

### Task 7: 前端 TS `ruleNormalize` + parity 测试

**Files:**
- Modify: `frontend/app/knowhow-cell-editor-logic.ts`（追加纯函数）
- Create: `frontend/app/knowhow-normalize.test.mjs`（镜像现有 `.test.mjs` 用法，见 `knowhow-optimize-logic` 的测试）

**Interfaces:**
- Produces: `ruleNormalize(raw: string): string` — 与 Python `rule_normalize` **同语义**，读同一份 `backend/tests/fixtures/knowhow_normalize_golden.json` 做 parity。

- [ ] **Step 1: 写失败 parity 测试 `knowhow-normalize.test.mjs`**

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ruleNormalize } from "./knowhow-cell-editor-logic.ts";

const golden = JSON.parse(readFileSync(new URL("../../backend/tests/fixtures/knowhow_normalize_golden.json", import.meta.url)));
for (const c of golden) {
  test(`ruleNormalize golden: ${c.name}`, () => {
    const out = ruleNormalize(c.raw);
    const lines = out.split("\n");
    for (const needle of c.expect_contains) assert.ok(lines.includes(needle), `${c.name}: missing ${needle}\n${out}`);
    for (const absent of c.expect_absent) assert.ok(!out.includes(absent), `${c.name}: ${absent} should be gone`);
  });
}
```

（若本仓库 `.test.mjs` 不能直接 import `.ts`，按现有 `knowhow-optimize-logic` 测试的既定方式接入——先读它怎么做。）

- [ ] **Step 2: 跑测试确认失败**（从 root：`cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/knowhow-normalize.test.mjs`，因 worktree 无 node_modules）
Expected: FAIL（`ruleNormalize` 未导出）

- [ ] **Step 3: 实现 `ruleNormalize`（TS，逐行移植 Python 版）**——同样的 marker/缩进检测、baseline 归零、顶格 alpha 加粗、`•`→`-`、散行成段、`\n{3,}`→`\n\n`、trim。保持与 Python 逐行对应，便于 parity。

- [ ] **Step 4: 跑 parity 测试直到全绿**（同 root 命令）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/app/knowhow-cell-editor-logic.ts frontend/app/knowhow-normalize.test.mjs
git commit -m "feat(knowhow): 前端 ruleNormalize（与后端 parity）"
```

---

### Task 8: 编辑器「规整格式」按钮 + 粘贴即时规整

**Files:**
- Modify: `frontend/app/knowhow-cell-editor.tsx`（工具栏 `:914-968`；`handlePaste` `:757-782`；handler 区）
- Modify: `frontend/app/knowhow-cell-editor-logic.ts`（若按钮标签/纯逻辑需要）

**Interfaces:**
- Consumes: `reformatKnowhowCell`（Task 4）、`ruleNormalize`（Task 7）。

- [ ] **Step 1: 粘贴即时规整**——在 `handlePaste` 里，若粘贴的是纯文本且含 `\t` 或 `•`（`BULLET_GLYPHS`），`preventDefault` 后把 `ruleNormalize(pastedText)` 插入光标处（可 Cmd+Z 撤销）；图片粘贴保持现有分支不变。加纯逻辑单测：`ruleNormalize` 已覆盖，此处补一个「决定是否规整」的谓词 `shouldNormalizePaste(text)` 单测。

- [ ] **Step 2: 「规整格式」按钮**——在工具栏「优化表达」旁加按钮（标签常量放 `-logic.ts`，如 `TOOLBAR_REFORMAT_LABEL="规整格式"`；List/Wand 图标）。handler `handleReformat` 调 `reformatKnowhowCell`；`changed=false` 时提示「已经是规整格式」；否则走**与「优化表达」相同的 before/after compare 面板**：接受 → 填回 textarea（仍需手动保存）/ 直接编辑 / 放弃。与「优化表达」并存，语义区分（规整=只改格式；优化=改措辞）。按钮在有未保存改动时禁用（同 optimize，后端读已存内容）。

- [ ] **Step 3: 验证（从 root 浏览器预览）**——用 preview 工具起前端（root），打开一个含脏格的 knowhow 表，点「规整格式」看 compare 面板出干净列表；粘贴一段带 `\t•` 的文本看即时变 `- `。用 read_page/screenshot 留证。

- [ ] **Step 4: Commit**

```bash
git add frontend/app/knowhow-cell-editor.tsx frontend/app/knowhow-cell-editor-logic.ts
git commit -m "feat(knowhow): 编辑器规整格式按钮 + 粘贴即时规整"
```

---

### Task 9: 批量「一键规整整表 / 整行」

**Files:**
- Modify: `frontend/app/knowhow-optimize-logic.ts`（复用整行状态机 `:99-249`）
- Modify: `frontend/app/knowhow-cell-editor.tsx` 或表格容器（批量入口按钮）

**Interfaces:**
- Consumes: `reformatKnowhowCell`（Task 4）。

- [ ] **Step 1: 纯逻辑**——把现有「优化整行」状态机泛化/复制成「规整整行/整表」：逐格调 `reformatKnowhowCell`，累积 `{cell, before, after, changed}`，汇总后**人工整体确认**再逐格保存。加状态机单测（镜像现有 optimize 整行测试）。
- [ ] **Step 2: UI 入口**——表级「一键规整」按钮；进度与 optimize 整行一致；确认面板列出将改动的格子数。
- [ ] **Step 3: 验证 + Commit**

```bash
git add frontend/app/knowhow-optimize-logic.ts frontend/app/knowhow-cell-editor.tsx
git commit -m "feat(knowhow): 批量一键规整整表/整行"
```

---

### Task 10: 跑 DeepSeek-V4 回填 + 端到端验证 + 收尾 PR

**Files:** 无新代码（执行 + 验证）

- [ ] **Step 1: 全量后端测试**（从 root，带 .env/库的从主 checkout 根跑；纯逻辑可 worktree）
Run: `cd /Users/hzf/workspace/silicon_notebook/backend && $PYBIN -m pytest tests/test_md_normalize_rule.py tests/test_md_normalize_invariant.py tests/test_knowhow_reformat.py tests/test_knowhow_import.py tests/test_backfill_knowhow_md.py -q`
Expected: 全 PASS
- [ ] **Step 2: 架构守卫**：`$PYBIN -m pytest backend/tests/test_repository_surface_manifest.py backend/tests/test_architecture_documentation.py -q`；按提示更新 `EXPECTED_PATCH_DELTAS` / 文档。
- [ ] **Step 3: DeepSeek-V4 回填 dry-run**（从主 checkout 根，真实库）：`$PYBIN scripts/backfill_knowhow_md.py --notebook nb-a73f16940c`。**把 diff 交用户过目**（用户明确要求提交前看 diff）。用户点头后 `--apply`（可加 `--use-llm`）。
- [ ] **Step 4: 端到端**：起前端预览（root），打开 DeepSeek-V4 `know-how沉淀-转置` 表，确认截图格（修复方法）从跑马字变「A 小标题 + 列表 / B 小标题 + 列表」；抽查 `parse_steps` 对该格从 0 步骤变多步骤（可加一条集成断言或脚本打印）。留 screenshot。
- [ ] **Step 5: 收尾 PR**：`git fetch origin && git rebase origin/master`（保持线性）；push；`gh pr create --base master`。PR 描述引用 spec + 关键 before/after。

---

## Self-Review 记录（写计划者自查）

- **Spec 覆盖**：§4.1 校验→T2；§4.2 规则器→T1；§4.3 LLM→T3；§4.4 编排→T3；§5.1 按钮+粘贴→T8/T4/T7；§5.2 导入→T5；§5.3 批量→T9；§5.4 回填→T6；§6 数据流→T10 验证；§8 测试→各任务 TDD。无遗漏。
- **类型一致**：`reformat_cell` 返回 `{candidate_md, source, changed}` 在 T3 定义、T4 端点透传、T6 回填消费、T8 前端 `{candidateMd,...}` 映射——一致。`rule_normalize`(py)/`ruleNormalize`(ts) 同语义、同 golden。
- **Placeholder**：核心算法（T1/T2/T3）给了完整可运行代码；wiring/前端（T4/5/8/9）给了精确文件:行 + 代表性代码 + 「先读现有 optimize 模式再镜像」的明确指示（现有模式已在 spec/agent 报告中定位）。
- **已知取舍**（非缺陷）：`parse_steps` 扁平——规整后顶格 `**B.**` 会并入前一步、缩进子项变平级步骤；均优于现状（0 步骤/跑马字），spec 非目标已记。
