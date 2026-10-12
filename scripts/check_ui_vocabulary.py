#!/usr/bin/env python3
"""Guard: user-facing Chinese UI copy must not leak internal jargon.

Scope follows the **trust boundary, not the directory**. Three sources of copy reach
a user's screen, and all three are scanned:

  1. frontend/app and frontend/features *.ts/*.tsx *rendered text* — string
     literals plus JSX text nodes.
  2. backend `user_error(status, "…")` messages — `api/deps.py` marks exactly these
     4xx `detail` strings with `X-User-Message: 1`, and `frontend/app/errors.ts` is
     deny-by-default: a marked detail is shown to the user **verbatim**. Marking a
     string is therefore a promise that it is user copy, so it inherits the copy
     rules. Scoping this guard to `frontend/app` alone is what let 「基准库」and 「晋升队列」
     ship inside marked 403s while the guard stayed green.
  3. backend **reasoning-trace summaries** — the server writes them, the front end
     renders them **verbatim** (`answer-panel.tsx` prints `step.summary` with no
     mapping of its own), so they are user copy that never passes through
     `frontend/app`. `TraceStep(..., summary=...)` calls in
     `TRACE_SUMMARY_MODULES` are scanned as user-facing copy.

  For channels 2 and 3 the copy does not have to be a literal at the call site:
  `_IndirectCopy` follows variables, conditionals, concatenation, `%`/`.format`,
  `X.get(k, default)`, module constants and module-helper returns. Both channels
  also strip ALL_CAPS_UNDERSCORE environment-variable names before matching
  (`BACKEND_ENV_VAR`) — a config key named in an error message is not jargon.

  Bare `HTTPException(detail=str(exc))` is deliberately **not** scanned: it is never
  displayed (the front end falls back to a generic message by status code) and its
  detail is a diagnostics/MCP contract. `tests/test_user_error.py` guards that split.

  4. `release-notes/*.md` (README excluded) — the release manifest hands each note to
     the front end's update dialog verbatim; each line is scanned with the same rules.

Any hit fails the build. Run by scripts/check.sh.

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

The blacklist below covers `docs/ui-vocabulary.md`「界面词汇表」row by row. Two rows are covered
only in their unambiguous compound forms, on purpose (documented in that section):

  * 节点 / 边 — the graph view legitimately draws nodes and edges ("图谱技术上下文"
    in the table), and 边 is a substring of 旁边/边框/边距. Only 孤立节点, 补连边,
    关系边, 边审 are matchable; bare use stays a human-review item.
  * 知识库 — the legitimate Knowledge tab name; lint cannot tell it from misuse.

Also intentionally NOT blacklisted: 知识图谱, 索引 (user-dictionary words; only their
jargon modifiers CSR/ANN/暴力检索 and the abbreviation KG are).

The companion check — raw enum fallback (`MAP[x] ?? x`, `label(m, x, x)`) — lives in
`frontend/tests/guards/raw-enum-fallback.test.mjs`, not here. It needs the *context* of the
expression to tell a rendered lookup from internal normalisation, which means a real
TypeScript AST; doing that from Python would mean spawning node and dragging
`frontend/node_modules` in as a prerequisite for this otherwise dependency-free
script. `npm run test` collects `*.test.mjs` recursively, so `scripts/check.sh` still
runs it as a hard gate. Split of duties: this file guards *rendered text*, that one
guards *code shape*.

`--extra-root <dir>` (repeatable) is a self-check hook for out-of-tree deployment
plugins (`EXTENSIONS_CONFIG`; see `docs/deployment-extensions-sop*.md`): plugin source lives
outside this repository, so it cannot ship in `frontend/app`/`frontend/features` or
`backend/app` and therefore is not reachable by the two default scan roots above no
matter how the guard is invoked. Each `--extra-root` directory's `**/*.py` is scanned
for `user_error(...)` messages with the same rules as the default backend scan
(same blacklist, same `SANCTIONED_UI` allowance, same f-string handling) — a plugin
package that wants CI-grade assurance runs this guard against its own tree the same
way this repository runs it against `backend/app`. It is deliberately scoped to
*only* extend the scan face: passing no `--extra-root` leaves every check (including
`MIN_USER_ERROR_SITES`, which only ever counts the default `backend/app` root, never
an extra one — an empty or tiny plugin tree must not be able to push the floor check
below its real threshold) and every line of output byte-for-byte unchanged from
before this option existed.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_PRODUCTION_DIRS = (
    ROOT / "frontend" / "app",
    ROOT / "frontend" / "features",
)
BACKEND_APP = ROOT / "backend" / "app"
# 第四条通道:release-notes/*.md 由打包清单原样交给前端弹窗展示,是用户可见文案。
RELEASE_NOTES_DIR = ROOT / "release-notes"

# 后端「这段文案会原样上屏」的唯一标记函数(app/api/deps.py)。
USER_ERROR = "user_error"

# 非空性下限:守卫按函数名做 AST 匹配,一旦 helper 改名 / 换成装饰器 / 调用写法变了,
# 匹配数会掉到 0 而退出码仍是 0 —— 正是本次要根治的那种假绿。这不是精确台账
# (新增站点不该让构建红),只是「扫描面塌了」的哨兵。当前实际 21 处。
MIN_USER_ERROR_SITES = 15

# —— 第三条通道:服务端写、前端逐字渲染的推理轨迹摘要 ——————————————————
# 扫描面是**具名模块**而不是整个 backend/app:轨迹摘要的产出方是有限的几个服务
# 模块(推理检索器,以及会自己写轨迹步的提问、全局问答、插件问答、表格分析、结构化
# 检索),把 `TraceStep(summary=…)` 这个形状泛化到全仓会把一堆同名 kwarg 的非 UI
# 调用拖进来,而收益是零。新增一个会写轨迹步的模块时,把它加进这张表——这是有意的
# 显式登记,不是靠 glob 猜。
#
# 路径按 `ROOT` 直接写死,**刻意不经过 `BACKEND_APP`**:那个常量是可被重定向的
# (`--extra-root` 的仓库外插件自检、以及把它指向 tmp 目录的几条自检用例),而这条
# 通道的扫描面是本仓库里具名模块,与插件自检面没有任何关系。跟着一个会被
# 重定向的根走,只会让"把后端根指到别处"的那几种调用顺带把这条通道也一起关掉。
TRACE_SUMMARY_MODULES = (
    ROOT / "backend" / "app" / "services" / "reasoning_retrieval.py",
    ROOT / "backend" / "app" / "services" / "ask_service.py",
    ROOT / "backend" / "app" / "services" / "global_ask.py",
    ROOT / "backend" / "app" / "services" / "plugin_ask_engine.py",
    ROOT / "backend" / "app" / "services" / "spreadsheet_analysis.py",
    ROOT / "backend" / "app" / "services" / "structured_retrieval.py",
)
TRACE_STEP = "TraceStep"
# Non-vacuity floor: keep the existing guard while removing retired modules.
MIN_TRACE_SUMMARY_SITES = 50

# 这条通道开通**之前**就已存在的一条违规,登记为例外而不是静默放行:
# 「本笔记本尚未构建知识图谱…」里的「构建知识图谱」按词表该写成「整理知识图谱」,
# 但这句是被 `docs/product-and-api*.md` 逐字冻结的协议串(文档明写「含其中的半角
# 逗号」),还被 `backend/tests/fixtures/repository_contract/ask_responses.json` 这份
# 仓库黄金 fixture 钉住。改它要同改文档、既有用例与 fixture,属于独立的一次改动;
# 登记在这里是为了让它可见且**有界**——例外按逐字全串匹配,改一个字就重新违规。
# 后续项登记在 `fangan_todo.md` 的「问答」一节。
GRANDFATHERED_TRACE_SUMMARIES = frozenset({
    "本笔记本尚未构建知识图谱,本轮只用原文检索与集合清单",
})

# ASCII acronyms/words: matched only when not glued to another ASCII letter, so
# identifiers (currentNotebook, MemoryPanel, schemaBusy, PKG) cannot trip them.
ASCII_TERMS = {
    "KG": re.compile(r"(?<![A-Za-z])KG(?![A-Za-z])"),
    "CSR": re.compile(r"(?<![A-Za-z])CSR(?![A-Za-z])"),
    "ANN": re.compile(r"(?<![A-Za-z])ANN(?![A-Za-z])"),
    # 档位说明曾长期无人渲染，攒下「不把枚举伪装成大 TopN」这种开发者措辞，直到档位
    # 控件收敛才第一次上屏。检索取数方式对用户没有意义，界面只讲效果。
    "TopN": re.compile(r"(?<![A-Za-z])top-?n(?![A-Za-z])", re.IGNORECASE),
    "chunk": re.compile(r"(?<![A-Za-z])chunks?(?![A-Za-z])", re.IGNORECASE),
    "notebook": re.compile(r"(?<![A-Za-z])notebooks?(?![A-Za-z])", re.IGNORECASE),
    "Memory": re.compile(r"(?<![A-Za-z])memory(?![A-Za-z])", re.IGNORECASE),
    "schema": re.compile(r"(?<![A-Za-z])schemas?(?![A-Za-z])", re.IGNORECASE),
    "deprecated": re.compile(r"(?<![A-Za-z])deprecated(?![A-Za-z])", re.IGNORECASE),
    "spreadsheet analysis": re.compile(
        r"(?<![A-Za-z])spreadsheet analysis(?![A-Za-z])", re.IGNORECASE
    ),
    "workbook analysis": re.compile(
        r"(?<![A-Za-z])workbook analysis(?![A-Za-z])", re.IGNORECASE
    ),
    "analysis issue": re.compile(
        r"(?<![A-Za-z])analysis issues?(?![A-Za-z])", re.IGNORECASE
    ),
    "parse failure": re.compile(
        r"(?<![A-Za-z])parse failures?(?![A-Za-z])", re.IGNORECASE
    ),
    # —— 图谱质量分析视图（批 1）新增。这些挡的是**中文文案里夹带英文黑话**;裸标识符
    # (`const canonicalId`) 不在扫描面内——本守卫只在含中文的单元里匹配,且先剥注释/
    # 标识符/插值。
    #
    # `canonical` 是**补一个既有缺口**,不只是本特性的需要:界面词汇表一直
    # 把它与 projection/tier/chunk/KG/schema 并列为黑话,ASCII_TERMS 却从来没有它,
    # 直到本特性往词表加行、触发「词表 ⊇ 守卫覆盖」自检才暴露。
    "canonical": re.compile(r"(?<![A-Za-z])canonical(?![A-Za-z])", re.IGNORECASE),
    "cluster": re.compile(r"(?<![A-Za-z])clusters?(?![A-Za-z])", re.IGNORECASE),
    "community": re.compile(r"(?<![A-Za-z])communit(?:y|ies)(?![A-Za-z])", re.IGNORECASE),
    "outlier": re.compile(r"(?<![A-Za-z])outliers?(?![A-Za-z])", re.IGNORECASE),
    "centrality": re.compile(r"(?<![A-Za-z])centrality(?![A-Za-z])", re.IGNORECASE),
    # 检索结束事实(设计稿 2026-09-07 §7.2):一个必答方面的支撑全被最终装配的
    # 预算/过滤挡在合成之外,界面说「未送达」。内部名不上屏。
    "undelivered": re.compile(r"(?<![A-Za-z])undelivered(?![A-Za-z])", re.IGNORECASE),
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
    # —— 建图 / 入图 一行。「入图」是子串匹配,中文没有词边界,所以两侧都要挡掉
    # 正常词组:后面跟「谱」是「加入图谱」这类动宾,前面是「插」则是「插入图片」
    # (编辑器里最普通不过的一句话,曾误报 3 处)。只有 未入图 / 已入图 / 入图状态
    # 这种状态说法才是黑话。
    "建图": re.compile(r"建图"),
    # 「构建 / 建立 / 重建 + 知识图谱」作动作 → 界面词是「整理知识图谱」。裸「构建」
    # 「建立」是正常动词,不进黑名单;只有跟「知识图谱」连写才是黑话。
    "构建知识图谱": re.compile(r"(?:构建|建立|重建)知识图谱"),
    "入图": re.compile(r"(?<!插)入图(?!谱)"),
    # —— 抽取一行。「抽取字段」是本表给 schema 定的**界面词**,不能自己杀自己。
    "抽取": re.compile(r"抽取(?!字段)"),
    "补抽": re.compile(r"补抽"),
    "重抽": re.compile(r"重抽"),
    # —— 索引一行(CSR/ANN 在 ASCII_TERMS)
    "向量检索索引": re.compile(r"向量检索索引"),
    "暴力检索": re.compile(r"暴力检索"),
    # —— 节点 / 边 两行:只收无歧义复合形态,理由见上方 docstring 与 docs/ui-vocabulary.md
    "孤立节点": re.compile(r"孤立节点"),
    "补连边": re.compile(r"补连边"),
    "关系边": re.compile(r"关系边"),
    "边审": re.compile(r"边审"),
    # —— 「Agent 记录」两种行:界面上一律叫「调用记录」,而它按哪一档能力被准许
    # (ask:execute 这类协议串)不上屏,只说这次调用做了什么。两个词都是无歧义的
    # 复合形态,不会误伤正常措辞。
    "调用记账": re.compile(r"调用记账"),
    "能力档": re.compile(r"能力档"),
    # 检索结束事实(设计稿 §7.2)的服务端记账名。界面只说「方面」——「必答」是
    # 契约侧的措辞,上屏会让用户以为自己漏答了什么。无歧义复合形态,不误伤。
    "必答方面": re.compile(r"必答方面"),
    # —— 其余各行
    "投影": re.compile(r"投影"),
    "预审": re.compile(r"预审"),
    "去重": re.compile(r"去重"),
    "晋升": re.compile(r"晋升"),
    # —— Agentic Memory P1「AI 对这个库的理解」。「巡固」是该特性的后台整理动作，是
    # 内部实现词，界面词是「重新整理」。裸「画像」**不**收进这里——图谱质量分析视图
    # 早已把「来源画像」用作合法界面词（source profile，见 kg-analysis-view.tsx），
    # 裸子串匹配会连它一起误杀；与裸「节点」/「边」同一处理：登记进 NOT_LINTABLE，
    # 靠人工把关。但**无歧义的复合形态**照「孤立节点」/「关系边」的既有政策收进黑名
    # 单——「底座画像」/「覆盖层画像」不可能是「来源画像」的子串，却是本特性开发者
    # 最顺手写出的措辞（后端注释与文档通篇是「底座层/覆盖层」）。
    "巡固": re.compile(r"巡固"),
    "底座/覆盖层画像": re.compile(r"(?:底座|覆盖层)画像"),
    # —— 图谱质量分析视图（批 1）。界面词是「主题板块」。
    # 「社区」是这四个新词条里**唯一**真正会被误写进中文文案的:图算法里叫社区,
    # 顺手就写成「社区规模」。本产品语境下没有「用户社区」这种正当用法,故直接收。
    # 「簇」只收**复合形态**「canonical 簇 / 概念簇 / 合并簇」:裸「簇」在中文里罕见,
    # 但真要出现(如某天写「簇状图」)不该被这条规则误杀,与「边」那行同一个道理。
    "社区": re.compile(r"社区"),
    "概念簇": re.compile(r"(?:canonical|概念|合并)\s*簇"),
    # —— Agentic Memory P3「Agent 记录」。「观察队列」是该特性 agent_observations
    # 表的内部实现措辞（后端注释/文档通篇如此），界面词是「Agent 记录」——与「巡固」/
    # 「底座/覆盖层画像」同一处理:无歧义复合形态直接收进黑名单。
    "观察队列": re.compile(r"观察队列"),
    # —— Agentic Memory P3「我的回答偏好」。「回答风格偏好」是 config.py 注释里
    # 「检索/回答风格偏好」文档的内部措辞，界面词是「我的回答偏好」。
    "回答风格偏好": re.compile(r"回答风格偏好"),
}

CJK = re.compile(r"[一-鿿]")
INTERP = re.compile(r"\$\{[^{}]*\}")  # template ${...}

# 放行的界面短语:图谱对象类型/字段管理功能的界面名,经产品决策(2026-07-23)定为
# 「图谱 Schema」——EDA/芯片场景下用户反而熟悉 "Schema" 一词。这是本守卫对 schema
# 唯一放行的**复合短语**,像剥 ${…} 插值一样在匹配前从单元里剥掉;裸 schema / 裸
# Schema(不带「图谱」前缀)仍然照抓(见 ASCII_TERMS["schema"] 与其正例)。真源为
# docs/ui-vocabulary.md「界面词汇表」schema 行的放行注记。改动此处必须同步那条注记与
# backend/tests/test_ui_vocabulary_guard.py 的正反例。
#
# 前置断言 (?<![一-鿿]):放行短语的「图谱」前不能紧挨 CJK,否则会吃掉「知识图谱」的
# 尾字「图谱」——「构建知识图谱 Schema」里的「构建知识图谱」违规会被一起剥掉而漏报
# (code-quality 评审 P2)。所有合法用法(<Database/> 图谱 Schema、<h2>图谱 Schema</h2>)
# 的「图谱」前都是空格 / `>` / 串首,非 CJK,不受影响。
# 后置断言 (?![A-Za-z0-9_]):只放行完整单词 Schema,不放行它做**标识符前缀**的更长
# 形态——否则「图谱 Schemas」「图谱 Schematics」「图谱 Schema2」「图谱 Schema_v2」会被
# 当成放行短语剥掉而漏掉其中的 schema 黑话(codex 第 2/3 轮 P2)。这里刻意比
# ASCII_TERMS["schema"] 的 (?![A-Za-z]) **更严**:数字/下划线也算词字符,`Schema2`、
# `Schema_v2` 都不是那个被放行的单词,必须仍能被 ASCII 规则抓到。
# 该剥离统一放在 terms_in() 里,故前端渲染文本与后端 user_error() 两条扫描路径**对称**
# 生效(codex 第 2 轮 P2:界面词是全局契约,后端可展示文案里的「图谱 Schema」同样放行)。
SANCTIONED_UI = re.compile(r"(?<![一-鿿])图谱\s*Schema(?![A-Za-z0-9_])")
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
    # 放行的界面短语(「图谱 Schema」)在匹配前剥掉,同 ${…} 插值。放在这里(而非各调用点)
    # 让前端渲染文本 scan() 与后端 user_error() scan_user_error() 两条路径对称生效。
    unit = SANCTIONED_UI.sub(" ", unit)
    found = [name for name, pat in CJK_TERMS.items() if pat.search(unit)]
    found += [name for name, pat in ASCII_TERMS.items() if pat.search(unit)]
    return found


def scan(path: Path) -> list[tuple[int, str, str]]:
    """Blacklisted jargon in this file's rendered text: (line, term, unit)."""
    blanked = blank_comments(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str, str]] = []

    def record(off: int, text: str, unit: str) -> None:
        if not CJK.search(unit):
            return
        # A JSX text run starts right after the previous tag's `>`, so it usually
        # opens with a newline + indentation; counting newlines up to its *start*
        # would name the line above the copy. Skip the run's leading whitespace so
        # the reported line is the one the developer has to edit.
        off += len(unit) - len(unit.lstrip())
        # 放行短语的剥离已下沉进 terms_in()(前后端两条扫描路径共用);报错时仍用原始
        # unit,保证裸 schema/Schema 依旧被抓到。
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


def _message_literal(node: ast.expr | None) -> str | None:
    """The literal text of a `user_error()` message argument, or None if it isn't one.

    f-string interpolations are dropped and the literal spans joined — the same rule
    the frontend pass applies to `${…}`, so `f"{n} 个来源待抽取"` still trips 抽取.
    This is the *leaf* rule; `_IndirectCopy` builds the indirect forms (variable,
    conditional, concatenation, helper return…) on top of it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return " ".join(parts) if parts else None
    return None


# 后端两条通道(user_error、轨迹摘要)专用:匹配前剥掉全大写下划线环境变量名。
# 「请设置 SILICON_NOTEBOOK_AUTH_OPTIONAL」里的 NOTEBOOK 是配置键,不是界面黑话;
# 裸 notebook / 裸 KG 不含下划线,照抓。刻意**不**放进 terms_in():前端渲染文本与
# 更新说明里没有这种写法,放宽它们的扫描面只会少报。
BACKEND_ENV_VAR = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])")


def _backend_terms_in(unit: str) -> list[str]:
    return terms_in(BACKEND_ENV_VAR.sub(" ", unit))


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
# 间接写法的解析深度上限:变量 → 条件 → 拼接 → helper return 这类链条实际不超过
# 三四层;上限只是防环(`x = x + "…"`)与防组合爆炸,不是覆盖面的取舍。
MAX_INDIRECTION_DEPTH = 5
# `+` 两侧各自有多个单元时做笛卡儿积;超过这个数就截断,只防病态组合。
MAX_UNITS_PER_EXPR = 64


def _dedupe(units: list[str]) -> list[str]:
    return list(dict.fromkeys(units))


def _bindings(scope: ast.AST) -> tuple[set[str], dict[str, list[ast.expr]]]:
    """Names bound directly in ``scope`` (a module or a function), and the values of
    the simple ``name = value`` / ``name: T = value`` / ``(name := value)`` ones.

    Nested functions, classes and lambdas are their own scope and are not entered.
    Parameters, loop targets, ``with … as``, tuple unpacking and ``+=`` mark a name as
    local without giving it a value — a local without a statically known value
    shadows the module constant of the same name, exactly like Python scoping.
    """
    local: set[str] = set()
    values: dict[str, list[ast.expr]] = {}
    if isinstance(scope, _FUNCTION_NODES):
        args = scope.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                    *(a for a in (args.vararg, args.kwarg) if a is not None)]:
            local.add(arg.arg)

    def mark_targets(target: ast.expr) -> None:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                local.add(sub.id)

    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPE_NODES):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local.add(node.name)
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local.add(target.id)
                    values.setdefault(target.id, []).append(node.value)
                else:
                    mark_targets(target)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            local.add(node.target.id)
            if node.value is not None:
                values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            local.add(node.target.id)
            values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
            mark_targets(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            mark_targets(node.optional_vars)
        stack.extend(ast.iter_child_nodes(node))
    return local, values


class _IndirectCopy:
    """Resolve a user-visible copy expression into the text *units* it can display.

    The two backend channels (``user_error`` messages and trace summaries) used to
    accept only a literal at the call site, so copy written one step away — assigned
    to a variable first, picked by a conditional, concatenated, or returned by a
    module helper — was skipped silently (「横向对比:纳入 … 个同社区实体」shipped that
    way). Supported forms, each bounded by ``MAX_INDIRECTION_DEPTH``:

      * str constant / f-string — ``_message_literal`` (interpolations dropped);
      * ``a if c else b`` — each branch is its **own** unit, so text from the two
        branches is never glued into a phantom hit;
      * ``a + b`` — the two sides joined into one unit (an unresolvable side counts as
        an interpolation: a blank);
      * ``"…" % args`` — the left side; ``"…".format(...)`` — the receiver;
      * ``X.get(k, default)`` — every value of the dict ``X`` resolves to, plus the
        default;
      * a name — the assignments in the enclosing function(s) first, then the module
        level, following Python scoping (a parameter shadows the module constant and
        stays unresolvable);
      * a call to a function defined at this module's top level — every ``return``.

    Anything else (``str(exc)``, ``exc.user_message``, imported constants, ``X[k]``…)
    resolves to no unit and stays deliberately unchecked, as before.
    """

    def __init__(self, tree: ast.Module) -> None:
        self._parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
        self._module = tree
        self._scopes: dict[ast.AST, tuple[set[str], dict[str, list[ast.expr]]]] = {}
        self._functions = {
            node.name: node for node in tree.body if isinstance(node, _FUNCTION_NODES)
        }

    def _scope(self, node: ast.AST) -> tuple[set[str], dict[str, list[ast.expr]]]:
        if node not in self._scopes:
            self._scopes[node] = _bindings(node)
        return self._scopes[node]

    def _enclosing_functions(self, node: ast.AST) -> list[ast.AST]:
        chain: list[ast.AST] = []
        cur = self._parents.get(node)
        while cur is not None:
            if isinstance(cur, _FUNCTION_NODES):
                chain.append(cur)
            cur = self._parents.get(cur)
        return chain

    def _name_values(self, node: ast.Name) -> list[ast.expr]:
        for function in self._enclosing_functions(node):
            local, values = self._scope(function)
            if node.id in local:
                return values.get(node.id, [])
        return self._scope(self._module)[1].get(node.id, [])

    def _dicts(self, node: ast.expr, depth: int) -> list[ast.Dict]:
        if depth < 0:
            return []
        if isinstance(node, ast.Dict):
            return [node]
        if isinstance(node, ast.Name):
            return [d for v in self._name_values(node) for d in self._dicts(v, depth - 1)]
        return []

    def units(self, node: ast.expr | None, depth: int = MAX_INDIRECTION_DEPTH) -> list[str]:
        if node is None or depth < 0:
            return []
        literal = _message_literal(node)
        if literal is not None:
            return [literal]
        nxt = depth - 1
        if isinstance(node, ast.IfExp):
            return _dedupe(self.units(node.body, nxt) + self.units(node.orelse, nxt))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.units(node.left, nxt), self.units(node.right, nxt)
            if not left and not right:
                return []
            joined = [l + r for l in (left or [" "]) for r in (right or [" "])]
            return _dedupe(joined)[:MAX_UNITS_PER_EXPR]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self.units(node.left, nxt)
        if isinstance(node, ast.Name):
            return _dedupe([u for v in self._name_values(node) for u in self.units(v, nxt)])
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "format":
                return self.units(func.value, nxt)
            if isinstance(func, ast.Attribute) and func.attr == "get":
                found = [
                    u for d in self._dicts(func.value, nxt)
                    for v in d.values for u in self.units(v, nxt)
                ]
                if len(node.args) > 1:
                    found += self.units(node.args[1], nxt)
                return _dedupe(found)
            if isinstance(func, ast.Name) and func.id in self._functions:
                helper = self._functions[func.id]
                if any(f is helper for f in self._enclosing_functions(node)):
                    return []  # helper 递归调用自己:没有新信息
                return _dedupe([
                    u for ret in self._returns(helper) for u in self.units(ret, nxt)
                ])
        return []

    @staticmethod
    def _returns(function: ast.AST) -> list[ast.expr]:
        found: list[ast.expr] = []
        stack = list(ast.iter_child_nodes(function))
        while stack:
            node = stack.pop()
            if isinstance(node, _SCOPE_NODES):
                continue
            if isinstance(node, ast.Return) and node.value is not None:
                found.append(node.value)
            stack.extend(ast.iter_child_nodes(node))
        return found


def scan_user_error(path: Path) -> tuple[int, list[tuple[int, str, str]]]:
    """Blacklisted jargon in this file's `user_error()` messages.

    Messages written indirectly (variable, conditional, concatenation, module
    constant, module helper return — see ``_IndirectCopy``) are resolved; what
    still cannot be resolved statically is skipped on purpose and covered at
    runtime by `tests/test_user_error.py`.

    Returns (number of user_error call sites seen, hits) — the count feeds the
    non-vacuity floor, so a rename that silently empties the scan is caught.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolver = _IndirectCopy(tree)
    sites, hits = 0, set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # `user_error(...)` 与 `deps.user_error(...)` 都要认。
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != USER_ERROR:
            continue
        sites += 1
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        message = kwargs.get("message", node.args[1] if len(node.args) > 1 else None)
        for text in resolver.units(message):
            for term in _backend_terms_in(text):
                hits.add((node.lineno, term, text))
    return sites, sorted(hits)


def scan_trace_summaries(path: Path) -> tuple[int, list[tuple[int, str, str]]]:
    """Blacklisted jargon in this module's server-written trace summaries.

    Scan ``TraceStep(..., summary=...)`` values rendered by the front end, literal
    or written indirectly (see ``_IndirectCopy``). Summaries that still cannot be
    resolved statically are skipped, exactly like an unresolvable ``user_error``
    message. Returns (resolvable sites seen, hits) — the count feeds the
    non-vacuity floor.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolver = _IndirectCopy(tree)
    sites, hits = 0, set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != TRACE_STEP:
            continue
        for keyword in node.keywords:
            if keyword.arg != "summary":
                continue
            units = resolver.units(keyword.value)
            if units:
                sites += 1
            for text in units:
                if text in GRANDFATHERED_TRACE_SUMMARIES:
                    continue
                for term in _backend_terms_in(text):
                    hits.add((node.lineno, term, text))
    return sites, sorted(hits)


def scan_release_note(path: Path) -> list[tuple[int, str, str]]:
    """Blacklisted jargon in one release note (markdown shown to users verbatim).

    Each line is a unit; the same CJK-gated ``terms_in`` rules as the frontend scan apply.
    """
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not CJK.search(line):
            continue
        for term in terms_in(line):
            hits.append((lineno, term, line.strip()))
    return hits


def _release_note_files() -> list[Path]:
    if not RELEASE_NOTES_DIR.is_dir():
        return []
    return sorted(p for p in RELEASE_NOTES_DIR.glob("*.md") if p.name != "README.md")


def _rel(path: Path) -> str:
    """Repo-relative path for reporting; falls back to the absolute one when the
    scan root has been redirected (the self-tests point it at a tmpdir)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extra-root",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "repeatable; scan this directory's **/*.py user_error(...) messages in "
            "addition to backend/app (out-of-tree deployment plugin self-check — see "
            "the module docstring). Only widens the scan face: omitting it leaves "
            "every check and every line of output unchanged."
        ),
    )
    # `argv is None` here means "called with no argument at all" (e.g. the direct
    # `guard.main()` calls in test_ui_vocabulary_guard.py), not "read this process's
    # sys.argv" — unlike the CLI entry point below, which explicitly forwards
    # `sys.argv[1:]`. Falling back to argparse's own None-means-sys.argv default
    # would make every in-process `main()` call parse pytest's own arguments.
    return parser.parse_args(argv if argv is not None else [])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    extra_roots = [Path(p) for p in args.extra_root]

    files = sorted(
        p
        for directory in FRONTEND_PRODUCTION_DIRS
        for p in directory.rglob("*")
        if p.suffix in (".ts", ".tsx")
        and ".test." not in p.name
        and not p.name.endswith(".d.mts")
    )
    if not files:
        print("check_ui_vocabulary: no frontend production sources found", file=sys.stderr)
        return 1
    violations: list[str] = []
    for path in files:
        rel = _rel(path)
        for line, term, snippet in scan(path):
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in rendered text: {snippet!r}")

    # 后端侧:只有被 user_error() 标记为「可展示」的文案才受词表约束。
    py_files = sorted(BACKEND_APP.rglob("*.py"))
    if not py_files:
        print("check_ui_vocabulary: no backend/app sources found", file=sys.stderr)
        return 1
    marked_sites = 0
    for path in py_files:
        rel = _rel(path)
        sites, hits = scan_user_error(path)
        marked_sites += sites
        for line, term, snippet in hits:
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in user_error message: {snippet!r}")

    # 仓库外插件自检面(裁决 3):只扩大扫描面,MIN_USER_ERROR_SITES 只对默认根计数,
    # 不传 --extra-root 时这整段不产生任何输出或副作用。
    extra_root_sites = 0
    for root in extra_roots:
        for path in sorted(root.rglob("*.py")):
            rel = _rel(path)
            sites, hits = scan_user_error(path)
            extra_root_sites += sites
            for line, term, snippet in hits:
                snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
                violations.append(
                    f"  {rel}:{line}: 「{term}」in user_error message (extra root): {snippet!r}"
                )
    if extra_roots:
        print(
            f"check_ui_vocabulary: extra-root scan covered {extra_root_sites} "
            f"{USER_ERROR}() call site(s) across {len(extra_roots)} root(s)"
        )

    if marked_sites < MIN_USER_ERROR_SITES:
        print(
            f"check_ui_vocabulary: only {marked_sites} {USER_ERROR}() call sites found in "
            f"backend/app (expected ≥ {MIN_USER_ERROR_SITES}). 这条检查失去了扫描面——"
            f"helper 是不是改名 / 换了调用写法?别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    # 第三条通道:服务端写、前端逐字渲染的推理轨迹摘要(见模块 docstring)。
    # 刻意排在 MIN_USER_ERROR_SITES **之后**:两条后端通道各有自己的「扫描面塌了」
    # 哨兵,而一次把 BACKEND_APP 指错的误配置会同时触发两条。先报 user_error 那条
    # 是因为它先于这条存在,原有那条自检用例的输出契约不该被这次扩面改写。
    trace_sites = 0
    for path in TRACE_SUMMARY_MODULES:
        if not path.exists():
            print(
                f"check_ui_vocabulary: trace-summary module missing: {_rel(path)} "
                f"(update TRACE_SUMMARY_MODULES)",
                file=sys.stderr,
            )
            return 1
        sites, hits = scan_trace_summaries(path)
        trace_sites += sites
        rel = _rel(path)
        for line, term, snippet in hits:
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(
                f"  {rel}:{line}: 「{term}」in trace summary: {snippet!r}"
            )

    note_files = _release_note_files()
    for path in note_files:
        rel = _rel(path)
        for line, term, snippet in scan_release_note(path):
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in release note: {snippet!r}")

    if trace_sites < MIN_TRACE_SUMMARY_SITES:
        print(
            f"check_ui_vocabulary: only {trace_sites} literal trace summaries found "
            f"across {len(TRACE_SUMMARY_MODULES)} module(s) (expected ≥ "
            f"{MIN_TRACE_SUMMARY_SITES}). 这条通道失去了扫描面——{TRACE_STEP} 是不是"
            f"改名 / 摘要改成了变量拼装?别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    if violations:
        print("UI vocabulary contract MISMATCH — internal jargon in user-visible copy:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        print(
            "\nRewrite the copy per docs/ui-vocabulary.md「界面词汇表」(e.g. 基准库→公共知识库, "
            "chunk→段, KG/CSR/ANN→索引, 抽取→分析). Internal names stay in code, not UI.",
            file=sys.stderr,
        )
        return 1
    ok_message = (
        f"UI vocabulary contract OK: scanned {len(files)} frontend production files + "
        f"{marked_sites} backend {USER_ERROR}() messages + "
        f"{trace_sites} trace summaries "
        f"({len(CJK_TERMS) + len(ASCII_TERMS)} blacklisted terms)"
    )
    if extra_roots:
        ok_message += f" + {extra_root_sites} extra-root {USER_ERROR}() messages"
    print(ok_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
